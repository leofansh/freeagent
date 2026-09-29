"""跨重启事件去重的测试。

重点不是「重复会被认出来」—— 那在内存 dict 上太容易了。重点是三条
更难发现的：

1. **换个进程还认得出来**（这才是「跨重启」的意思）；
2. **坏文件、读不了、写不下都不许把启动搞崩**（它是缓存，不是依赖）；
3. 内存版与落盘版**参数不许漂移**（渠道层不能 import 飞书包，只能复制数值）。
"""

from __future__ import annotations

import json

from freeagent.feishu import dedup as dd
from freeagent.feishu.dedup import (
    CACHE_FILE_NAME,
    DEDUP_MAX_ENTRIES,
    DEDUP_TTL_HOURS,
    SeenEventStore,
    default_path,
)
from freeagent.services import channel as ch


def _store(path, **kw):
    """给个确定性的「现在」。"""
    return SeenEventStore(path, **kw)


class TestPath:
    def test_default_path_is_under_given_home(self, tmp_path):
        assert default_path(tmp_path) == tmp_path / CACHE_FILE_NAME

    def test_uses_freeagent_home_env(self, tmp_path, monkeypatch):
        """和 ``config.config_path`` 同一套解析：FREEAGENT_HOME → ~/.freeagent。

        刻意不用「另写一遍路径解析」—— 复制粘贴的解析迟早漂移，而漂移的
        表现是「写 A 目录、读 B 目录找」，缓存永远不命中且**没有任何报错**。
        """
        monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
        assert default_path() == tmp_path / CACHE_FILE_NAME


class TestDeduplication:
    def test_first_time_is_new_second_time_is_duplicate(self, tmp_path):
        s = _store(tmp_path / CACHE_FILE_NAME)
        assert s.is_duplicate("ev-1") is False
        assert s.is_duplicate("ev-1") is True

    def test_distinct_ids_are_all_new(self, tmp_path):
        s = _store(tmp_path / CACHE_FILE_NAME)
        assert [s.is_duplicate(f"ev-{i}") for i in range(5)] == [False] * 5

    def test_empty_id_never_marks_duplicate(self, tmp_path):
        """空 ``event_id`` 不能污染表。

        飞书事件偶尔没有 header，``event_id`` 会是空串。若空串被记进去，
        之后**每一条**无 id 的事件都会被当成第一条的重投而丢掉。
        """
        s = _store(tmp_path / CACHE_FILE_NAME)
        assert s.is_duplicate("") is False
        assert s.is_duplicate("") is False
        assert len(s) == 0


class TestAcrossRestart:
    """**这一组是「跨重启去重」的全部意义所在。**

    原来的实现在进程内 ``OrderedDict`` 里记，注释说「重启后忘掉是可接受的，
    飞书重投只发生在短时间内」。这个权衡恰好不成立：重启 → 立刻重连 →
    收到重投，正落在「短时间内」这个窗口内，后果是重复建事务 ——
    也就是去重本来要防的那件事。
    """

    def test_new_process_still_remembers(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        first = _store(path)
        first.is_duplicate("ev-reconnect")
        del first                                   # 模拟进程退出

        second = _store(path)                        # 模拟重连后的新进程
        assert second.is_duplicate("ev-reconnect") is True, (
            "长连接重连会重投近期事件；重启后忘了就会重复建事务"
        )

    def test_other_ids_still_new_after_restart(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        first = _store(path)
        first.is_duplicate("ev-a")
        del first
        assert _store(path).is_duplicate("ev-b") is False

    def test_count_survives_restart(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        first = _store(path)
        for i in range(3):
            first.is_duplicate(f"ev-{i}")
        assert len(_store(path)) == 3


class TestTtlAndCap:
    def test_expired_id_is_no_longer_duplicate(self, tmp_path):
        """超过 TTL 就不再视为可能重投。"""
        clock = [1000.0]
        s = _store(tmp_path / CACHE_FILE_NAME, now=lambda: clock[0])
        s.is_duplicate("ev-1")

        clock[0] += dd.DEDUP_TTL_HOURS * 3600 + 1
        assert s.is_duplicate("ev-1") is False, "TTL 到了就该重新处理"

    def test_just_inside_ttl_is_still_duplicate(self, tmp_path):
        clock = [1000.0]
        s = _store(tmp_path / CACHE_FILE_NAME, now=lambda: clock[0])
        s.is_duplicate("ev-1")
        clock[0] += dd.DEDUP_TTL_HOURS * 3600 - 10
        assert s.is_duplicate("ev-1") is True

    def test_stale_entries_evicted_on_load(self, tmp_path):
        """读盘时先按 TTL 剪一次，别把一堆过期记录又写回去。

        注意时钟要设在记录的**之后**：踩过的坑是一开始把「现在」设成
        1000、而记录是 1000 和 9000 —— 于是没有任何一条被判定为过期，
        断言「全被剪掉」就必然失败。那是测试写错，不是代码错。
        """
        path = tmp_path / CACHE_FILE_NAME
        path.write_text(
            json.dumps({"seen": {"ev-old": 1_000.0, "ev-new": 90_000.0}}),
            encoding="utf-8",
        )
        # ttl 默认 24h=86400；now=100000 时 cutoff=13600
        s = _store(path, now=lambda: 100_000.0)
        # 先断言**读盘后**的状态：过期的已经剪掉了。
        # 不能在问过之后才断言条数 —— ``is_duplicate`` 会把问到的 id 记进去
        # （那正是它的职责），于是「问过 ev-old」反而会让 ev-old 回到表里。
        assert len(s) == 1, f"过期那条读盘时就该剪掉，实际剩 {len(s)} 条"
        assert s.is_duplicate("ev-new") is True, "TTL 内的仍算重投"
        assert s.is_duplicate("ev-old") is False, "过期的不算重投，要重新处理"

    def test_cap_trims_oldest_first(self, tmp_path):
        clock = [0.0]
        s = _store(
            tmp_path / CACHE_FILE_NAME, max_entries=3, now=lambda: clock[0]
        )
        for i in range(6):
            clock[0] += 1
            s.is_duplicate(f"ev-{i}")
        assert len(s) == 3, "上限是给「有人拿它灌垃圾」兜底的"
        # 最新的一条**当然还在**表里 —— 剪的是最旧的。
        assert s.is_duplicate("ev-5") is True, "最新的该留着"
        assert s.is_duplicate("ev-0") is False, "最旧的被剪了"

    def test_duplicate_refreshes_timestamp(self, tmp_path):
        """重复投递要刷新时间戳。

        不刷新的话：一条被反复重投的事件带着一个很旧的时间戳，被 TTL
        剪掉后**下一轮又会被当成新事件**重新处理 —— 去重反而制造了重复。
        """
        clock = [0.0]
        s = _store(
            tmp_path / CACHE_FILE_NAME,
            ttl_seconds=100,
            now=lambda: clock[0],
        )
        s.is_duplicate("ev-1")
        for _ in range(3):
            clock[0] += 50              # 总共 150 > ttl=100
            s.is_duplicate("ev-1")
        assert len(s) == 1, "重投期间该条不该被剪掉"

    def test_duplicate_refresh_survives_restart(self, tmp_path):
        """刷新必须**落盘**。只刷内存的话，重启就等于没刷新。

        踩过的坑：上面那条 ``test_duplicate_refreshes_timestamp`` 全程用同一个
        实例，**从不重开**，所以它测的其实只是内存刷新 —— 于是「刷新没落盘」
        这个 bug 从它下面溜过去了。补这条时特意把实例重建一次：这才对应
        真实场景（「重启 → 立刻重连 → 收到重投」，见模块 docstring）。

        破坏路径：磁盘上留着最初那个 0，重启后 cutoff=50，``0 < 50`` 成立 →
        该条被 TTL 剪掉 → 重投事件被当成新事件重复处理。
        """
        path = tmp_path / CACHE_FILE_NAME
        clock = [0.0]
        s = _store(path, ttl_seconds=100, now=lambda: clock[0])
        s.is_duplicate("ev-1")            # 首次：落盘 {ev-1: 0.0}

        for _ in range(3):
            clock[0] += 50                # 累计 150 > ttl=100
            assert s.is_duplicate("ev-1") is True

        # **重启**：新实例只从磁盘读，时间仍冻结在 150
        again = _store(path, ttl_seconds=100, now=lambda: clock[0])
        assert again.is_duplicate("ev-1") is True, (
            "刷新没落盘：磁盘里还是最初那个旧时间戳，重启后被 TTL 剪掉，"
            "这条重投事件会被当成新事件重复处理"
        )

    def test_frozen_clock_at_zero_is_honored(self, tmp_path):
        """冻结在 ``0.0`` 的时钟必须被当真。

        踩过的坑（不是「``or`` 静默替换」那么戏剧性）：``now`` 的标注原先写成
        ``float | None``，而实现当**函数**用（``self._now()``）—— 标注与用法
        不符，且所有既有测试都传 lambda，于是这条契约从没被验证过。按标注传
        ``now=0.0`` 会在第一次 ``self._now()`` 处抛 ``TypeError: 'float'
        object is not callable``：至少是响的，但足以让「冻结时钟」这个测试
        手段本身用不了。

        从磁盘断言而不是直接调 ``_now()``：要验的是「写下去的是不是 0」，
        也就是用户真正依赖的那个后果。
        """
        path = tmp_path / CACHE_FILE_NAME
        s = _store(path, now=lambda: 0.0)
        s.is_duplicate("ev-1")
        assert json.loads(path.read_text(encoding="utf-8"))["seen"] == {"ev-1": 0.0}


class TestResilience:
    """坏文件 / 读写失败一律不许抛 —— 这是缓存，不是依赖。"""

    def test_corrupt_file_is_quarantined_not_fatal(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        path.write_text("{ 这不是 json", encoding="utf-8")
        s = _store(path)
        assert len(s) == 0, "坏表当空表用"
        assert (tmp_path / f"{CACHE_FILE_NAME}.corrupt").exists(), (
            "要留现场 —— 静默删掉就永远查不出「为什么去重没生效」"
        )
        assert s.last_error

    def test_wrong_shape_is_quarantined(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
        assert len(_store(path)) == 0
        assert (tmp_path / f"{CACHE_FILE_NAME}.corrupt").exists()

    def test_missing_seen_key_is_quarantined(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        path.write_text(json.dumps({"other": 1}), encoding="utf-8")
        assert len(_store(path)) == 0

    def test_corrupt_entries_are_dropped_silently(self, tmp_path):
        """个别条目坏掉不该让整张表报废。"""
        path = tmp_path / CACHE_FILE_NAME
        path.write_text(
            json.dumps({"seen": {"ok": 1000.0, "bad": "x", 7: 1000.0}}),
            encoding="utf-8",
        )
        s = _store(path, now=lambda: 1000.0)
        assert s.is_duplicate("ok") is True
        assert not (tmp_path / f"{CACHE_FILE_NAME}.corrupt").exists()

    def test_unwritable_location_degrades_to_memory(self, tmp_path):
        """写不下去就退化成「只在本进程有效」，也就是改造前的行为。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")      # 用文件占住目录位置
        s = _store(blocker / "sub" / CACHE_FILE_NAME)
        assert s.is_duplicate("ev-1") is False         # 构造没炸
        assert s.is_duplicate("ev-1") is True          # 内存里仍生效
        assert s.last_error, "降级要说出来，否则用户以为去重生效了"

    def test_empty_file_is_not_corruption(self, tmp_path):
        """``touch`` 出来的空文件当空表，不该被改名。"""
        path = tmp_path / CACHE_FILE_NAME
        path.write_text("", encoding="utf-8")
        s = _store(path)
        assert len(s) == 0
        assert not (tmp_path / f"{CACHE_FILE_NAME}.corrupt").exists()


class TestAtomicWrite:
    def test_no_temp_file_left_behind(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        s = _store(path)
        s.is_duplicate("ev-1")
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"临时文件没清掉：{leftovers}"

    def test_content_is_readable_json_with_seen_key(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        s = _store(path)
        s.is_duplicate("ev-1")
        assert json.loads(path.read_text(encoding="utf-8"))["seen"].keys() == {"ev-1"}

    def test_quarantine_keeps_original_content(self, tmp_path):
        """留现场的意思是**能看出当时写进去了什么**。"""
        path = tmp_path / CACHE_FILE_NAME
        path.write_text('{"seen": ', encoding="utf-8")
        _store(path)
        kept = (tmp_path / f"{CACHE_FILE_NAME}.corrupt").read_text(encoding="utf-8")
        assert kept == '{"seen": '


class TestParityWithChannel:
    """内存版与落盘版参数不许漂移。

    ``services`` 不能 import ``feishu``（那会顺着 SDK 爬进核心包），
    所以数值是**刻意复制**的。复制就得有人盯着，这个测试就是那个人。
    """

    def test_ttl_matches(self):
        assert ch.DEFAULT_DEDUP_TTL_SECONDS == DEDUP_TTL_HOURS * 3600

    def test_cap_matches(self):
        assert ch.DEFAULT_DEDUP_MAX_ENTRIES == DEDUP_MAX_ENTRIES

    def test_doc_records_the_numbers(self):
        """文档里的 24 小时 / 2048 条必须和代码一致。"""
        from pathlib import Path

        doc = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "个人事务助手设计方案.md"
        ).read_text(encoding="utf-8")
        assert str(DEDUP_TTL_HOURS) in doc, f"文档里找不到 {DEDUP_TTL_HOURS} 小时"
        assert str(DEDUP_MAX_ENTRIES) in doc, f"文档里找不到 {DEDUP_MAX_ENTRIES} 条"
