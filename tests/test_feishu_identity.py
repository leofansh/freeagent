"""探测 bot 自身 ``open_id`` 的测试。

重点是三件事：

1. **字段在 ``body["bot"]`` 里**，不在顶层 —— 读错层会得到空 ``open_id``，
   而空 ``open_id`` 恰好让 @ 门控「看起来能跑」（谁都不等于它，于是群里
   没人能叫醒 bot），所以必须在这里就炸掉。
2. **写一个形状、读另一个形状**这类 bug 只有「存了再读」才测得出来。
   写完立刻断言文件存在是过的，要等重启才暴露。
3. 探测失败**不许**让桥接起不来，但必须让调用方知道门控要失败关闭。
"""

from __future__ import annotations

import json

import pytest

from freeagent.feishu import identity as ident
from freeagent.feishu.identity import (
    BOT_INFO_PATH,
    CACHE_FILE_NAME,
    CACHE_TTL_SECONDS,
    BotIdentity,
    IdentityError,
    identity_path,
    load_cached_identity,
    parse_bot_identity,
    resolve_identity,
    save_identity,
)

ALICE = "ou_alice"
BOT = "ou_bot_abc123"


class FakeSender:
    """只提供 ``get_json``。**不发网络请求** —— 所以这层能离线测。"""

    def __init__(self, body=None, error: Exception | None = None):
        self.body = body if body is not None else {"bot": {"open_id": BOT}}
        self.error = error
        self.calls: list[str] = []

    def get_json(self, path: str):
        self.calls.append(path)
        if self.error is not None:
            raise self.error
        return self.body


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
class TestParseBotIdentity:
    def test_reads_from_bot_field(self):
        got = parse_bot_identity({
            "code": 0, "msg": "ok",
            "bot": {
                "open_id": BOT, "app_name": "我的助手",
                "activate_status": "2",
            },
        })
        assert got == BotIdentity(BOT, "我的助手", "2")

    def test_top_level_open_id_is_not_enough(self):
        """**核心**：字段不在顶层。只读顶层会拿到空 open_id。

        而空 open_id 不会报错，只会让 @ 门控变成「群里没人能叫醒 bot」——
        安静地不响应，比抛异常难查一百倍。
        """
        with pytest.raises(IdentityError, match="bot"):
            parse_bot_identity({"open_id": BOT})

    @pytest.mark.parametrize("body", [
        {},
        {"bot": None},
        {"bot": "字符串"},
        {"bot": {}},
        {"bot": {"open_id": ""}},
        {"bot": {"open_id": "   "}},
        {"bot": {"open_id": 123}},
    ])
    def test_missing_open_id_raises(self, body):
        with pytest.raises(IdentityError):
            parse_bot_identity(body)

    def test_optional_fields_tolerate_absence(self):
        got = parse_bot_identity({"bot": {"open_id": BOT}})
        assert got.app_name == ""
        assert got.activate_status == ""

    def test_open_id_is_stripped(self):
        assert parse_bot_identity({"bot": {"open_id": f" {BOT} "}}).open_id == BOT


# --------------------------------------------------------------------------- #
# 缓存往返
# --------------------------------------------------------------------------- #
class TestCacheRoundTrip:
    """**「存了再读」是唯一能测出形状不一致的写法。**

    早期实现写的是扁平结构（``open_id`` 在顶层），读的时候却去调解析响应的
    那个函数 —— 于是每写一次缓存，下次启动就把它当损坏文件改名留现场，
    身份永远命中不了缓存，每次启动都重探一遍网络。
    """

    def test_save_then_load_returns_same_identity(self, tmp_path):
        want = BotIdentity(BOT, "我的助手", "2")
        save_identity(want, tmp_path)
        assert load_cached_identity(tmp_path) == want

    def test_round_trip_survives_restart(self, tmp_path):
        """换个「进程」读：只留一个形状就必然能读回来。"""
        save_identity(BotIdentity(BOT), tmp_path, now=1000.0)
        again = load_cached_identity(tmp_path, now=1000.0)
        assert again is not None and again.open_id == BOT

    def test_cache_uses_same_shape_as_api_response(self, tmp_path):
        """缓存里的身份层和响应里的 ``bot`` 层同构 → 同一个解析器服务两条路径。"""
        path = identity_path(tmp_path)
        save_identity(BotIdentity(BOT, "助手"), tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["bot"]["open_id"] == BOT
        assert parse_bot_identity(payload).open_id == BOT

    def test_no_temp_file_left_behind(self, tmp_path):
        save_identity(BotIdentity(BOT), tmp_path)
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"临时文件没清掉：{leftovers}"

    def test_directory_is_created(self, tmp_path):
        target = tmp_path / "nested" / "deeper"
        assert load_cached_identity(target) is None      # 先读：没有
        save_identity(BotIdentity(BOT), target)
        assert (target / CACHE_FILE_NAME).is_file()


class TestCacheFailure:
    def test_missing_cache_returns_none(self, tmp_path):
        assert load_cached_identity(tmp_path) is None, "没缓存不该抛"

    def test_corrupt_cache_is_quarantined(self, tmp_path):
        path = identity_path(tmp_path)
        path.write_text("{ 不是 json", encoding="utf-8")
        assert load_cached_identity(tmp_path) is None
        assert (tmp_path / f"{CACHE_FILE_NAME}.corrupt").exists(), (
            "坏文件要留现场 —— 静默删掉就永远查不出「为什么每次都要重探」"
        )

    def test_wrong_shape_is_quarantined(self, tmp_path):
        """扁平结构（旧版本留下的）要被识别为坏，而不是当成没有缓存。"""
        path = identity_path(tmp_path)
        path.write_text(
            json.dumps({"open_id": BOT, "fetched_at": 1000.0}), encoding="utf-8"
        )
        assert load_cached_identity(tmp_path, now=1000.0) is None
        assert (tmp_path / f"{CACHE_FILE_NAME}.corrupt").exists()

    def test_expired_is_not_corruption(self, tmp_path):
        """**过期不是损坏** —— 不该改名、不该留现场文件。

        过期只说明「该重探一次」，是正常生命周期的一部分。把它当损坏
        会让每次重启都堆一个 ``.corrupt`` 垃圾文件。
        """
        save_identity(BotIdentity(BOT), tmp_path, now=0.0)
        later = CACHE_TTL_SECONDS + 1
        assert load_cached_identity(tmp_path, now=later) is None
        assert not (tmp_path / f"{CACHE_FILE_NAME}.corrupt").exists()
        assert identity_path(tmp_path).is_file(), "过期不该动原文件"

    def test_just_inside_ttl_is_valid(self, tmp_path):
        save_identity(BotIdentity(BOT), tmp_path, now=1000.0)
        got = load_cached_identity(tmp_path, now=1000.0 + CACHE_TTL_SECONDS - 1)
        assert got is not None

    def test_empty_file_is_not_corruption(self, tmp_path):
        path = identity_path(tmp_path)
        path.write_text("", encoding="utf-8")
        assert load_cached_identity(tmp_path) is None
        assert not (tmp_path / f"{CACHE_FILE_NAME}.corrupt").exists()


# --------------------------------------------------------------------------- #
# 组合入口
# --------------------------------------------------------------------------- #
class TestResolveIdentity:
    def test_prefers_cache_no_network(self, tmp_path):
        save_identity(BotIdentity(BOT, "缓存里的"), tmp_path)
        sender = FakeSender(error=AssertionError("不该联网"))
        got, note = resolve_identity(sender, home=tmp_path)
        assert got.open_id == BOT
        assert "缓存" in note
        assert sender.calls == [], "命中缓存就不该发网络请求"

    def test_falls_back_to_network_then_caches(self, tmp_path):
        sender = FakeSender()
        got, note = resolve_identity(sender, home=tmp_path)
        assert got.open_id == BOT
        assert sender.calls == [BOT_INFO_PATH]
        assert "联网" in note
        assert identity_path(tmp_path).is_file(), "探到了就该缓存，下次不用再探"

    def test_network_failure_returns_none_and_reason(self, tmp_path):
        """**降级不许抛** —— 桥接要能起来，只是群里不响应。

        抛异常会让 bot 完全起不来，那比「群里安静」严重得多。
        """
        sender = FakeSender(error=RuntimeError("network down"))
        got, note = resolve_identity(sender, home=tmp_path)
        assert got is None
        assert "network down" in note
        assert "群" in note, "说明里要说清后果是群里不响应"

    def test_failed_probe_caches_nothing(self, tmp_path):
        resolve_identity(FakeSender(error=RuntimeError("boom")), home=tmp_path)
        assert not identity_path(tmp_path).exists(), "失败不该留下空缓存"

    def test_bad_response_shape_is_handled(self, tmp_path):
        got, note = resolve_identity(FakeSender(body={"open_id": BOT}), home=tmp_path)
        assert got is None
        assert "bot" in note

    def test_unwritable_cache_still_returns_identity(self, tmp_path):
        """探到了但存不下：这次仍可用，只是不缓存。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        got, note = resolve_identity(FakeSender(), home=blocker / "sub")
        assert got is not None and got.open_id == BOT, "存不下不该让这次也失败"
        assert "写缓存失败" in note
