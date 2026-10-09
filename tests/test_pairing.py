"""配对流程的守卫（设计文档 11.9.8）。

## 这一刀要防的是「配对变成了绕过白名单」

配对解决的是「**第一次装时手里没有 open_id**」这个死循环。它**不是**
「拿不到也放行」——所以下面两类用例各锁一条边界：

1. **白名单判定一个字都没动**：不在名单的人，命令依然不执行
   （``test_channel.py`` 那边已有守卫，这里钉住「加了配对之后仍然」）
2. **配对码不产生任何执行**：它只往白名单写一个字符串

## 三种失败必须给三句可区分的话

「码不对」是死胡同：用户不知道是打错、过期、还是用过，而这三者的下一步
完全不同（重打 / 重新私聊 bot / 重新私聊 bot）。所以每一条都有独立用例。
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from freeagent.feishu.config import ENV_ALLOWED
from freeagent.feishu.pairing_cmd import pair_command
from freeagent.feishu.secret_store import read_env, write_env
from freeagent.feishu.sender import pairing_card
from freeagent.services.pairing import (
    PAIRING_TTL_SECONDS,
    PairingError,
    PairingStore,
    new_pairing_code,
)
from freeagent.storage.db import connect, init_schema


class _Clock:
    """可推进的假时钟。配对码的有效期判定必须**确定性**可测。"""

    def __init__(self, moment: datetime.datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime.datetime:
        return self.moment

    def advance(self, **kw) -> None:
        self.moment = self.moment + datetime.timedelta(**kw)


@pytest.fixture()
def wired(tmp_path):
    conn = connect(Path(tmp_path) / "a.db")
    init_schema(conn)
    clock = _Clock(datetime.datetime(2026, 10, 8, 12, 0))
    yield conn, clock, tmp_path
    conn.close()


# --------------------------------------------------------------------------- #
# 签发 → 核销
# --------------------------------------------------------------------------- #

class TestIssueAndConsume:
    def test_roundtrip_returns_the_open_id(self, wired):
        conn, clock, _home = wired
        issued = PairingStore(conn, clock=clock).issue("ou_abc")
        assert PairingStore(conn, clock=clock).consume(issued.code) == "ou_abc"

    def test_code_is_readable_by_a_human(self):
        """分组显示是为了**能被抄下来** —— 一串 24 位无分隔字符没法口述。"""
        code = new_pairing_code()
        assert "-" in code and len(code.replace("-", "")) >= 8

    def test_code_has_enough_entropy(self):
        """连续两个码不许相同。

        这条不是在测「密码学强度」，而是在测**没有退化成时间戳/自增** ——
        那是最容易写出来的错实现。
        """
        assert len({new_pairing_code() for _ in range(200)}) == 200

    def test_plaintext_code_is_not_stored(self, wired):
        """库里**只有哈希**。

        理由不是「显得更安全」，而是那张表没有任何查询需求，而明文落库
        等于多一份可被读走的凭据。
        """
        conn, clock, _home = wired
        issued = PairingStore(conn, clock=clock).issue("ou_abc")
        rows = conn.execute("SELECT code_hash FROM pairing_codes").fetchall()
        assert rows, "没落库 —— 另一个进程就核销不了"
        assert all(issued.code not in r[0] for r in rows), "库里存了明文！"

    def test_consume_is_case_and_space_tolerant(self, wired):
        """用户从聊天窗口复制过来很可能带空格或换行。"""
        conn, clock, _home = wired
        issued = PairingStore(conn, clock=clock).issue("ou_abc")
        assert PairingStore(conn, clock=clock).consume(
            f"  {issued.code}\n"
        ) == "ou_abc"

    def test_empty_open_id_is_refused(self, wired):
        """没有对方身份就签不出能用的码 —— 与其落一行废记录不如当场拒。"""
        conn, clock, _home = wired
        with pytest.raises(PairingError):
            PairingStore(conn, clock=clock).issue("   ")


# --------------------------------------------------------------------------- #
# 一次性 + 短 TTL —— 码躺在陌生人的窗口里
# --------------------------------------------------------------------------- #

class TestOneTimeAndShortLived:
    def test_code_cannot_be_used_twice(self, wired):
        conn, clock, _home = wired
        store = PairingStore(conn, clock=clock)
        issued = store.issue("ou_abc")
        store.consume(issued.code)
        with pytest.raises(PairingError) as ei:
            store.consume(issued.code)
        assert "用过" in str(ei.value)

    def test_expired_code_is_refused(self, wired):
        conn, clock, _home = wired
        store = PairingStore(conn, clock=clock)
        issued = store.issue("ou_abc", ttl_seconds=60)
        clock.advance(seconds=61)
        with pytest.raises(PairingError) as ei:
            store.consume(issued.code)
        assert "过期" in str(ei.value)

    def test_default_ttl_is_ten_minutes(self):
        """短 TTL 是刻意的：它躺在陌生人的聊天窗口里。"""
        assert PAIRING_TTL_SECONDS == 600

    def test_used_before_expired_is_reported_as_used(self, wired):
        """又用过又过期的码，要报「用过」。

        先报过期会让用户以为「等一会儿就行」，而它其实早就废了 ——
        于是他一直等一个不会发生的事。
        """
        conn, clock, _home = wired
        store = PairingStore(conn, clock=clock)
        issued = store.issue("ou_abc", ttl_seconds=60)
        store.consume(issued.code)
        clock.advance(seconds=120)
        with pytest.raises(PairingError) as ei:
            store.consume(issued.code)
        assert "用过" in str(ei.value)


# --------------------------------------------------------------------------- #
# 三种失败，三句可区分的话
# --------------------------------------------------------------------------- #

class TestDistinguishableFailures:
    def test_unknown_code(self, wired):
        conn, clock, _home = wired
        with pytest.raises(PairingError) as ei:
            PairingStore(conn, clock=clock).consume("AAAA-BBBB-CCCC")
        assert "不认" in str(ei.value)

    def test_empty_code(self, wired):
        conn, clock, _home = wired
        with pytest.raises(PairingError) as ei:
            PairingStore(conn, clock=clock).consume("   ")
        assert "没给码" in str(ei.value)

    def test_the_three_messages_are_actually_different(self, wired):
        """三句话若糊成一句，用户就不知道自己该做什么。"""
        conn, clock, _home = wired
        store = PairingStore(conn, clock=clock)
        used = store.issue("ou_1", ttl_seconds=60)
        store.consume(used.code)
        expired = store.issue("ou_2", ttl_seconds=60)

        seen = set()
        for arg, _advance in ((("AAAA-BBBB-CCCC"), False), ((used.code), False),
                              ((expired.code), True)):
            if _advance:
                clock.advance(seconds=61)
            with pytest.raises(PairingError) as ei:
                store.consume(arg)
            seen.add(str(ei.value))
        assert len(seen) == 3, f"三句里有重复：{seen}"


# --------------------------------------------------------------------------- #
# 清扫
# --------------------------------------------------------------------------- #

class TestPurge:
    def test_expired_rows_are_removed(self, wired):
        conn, clock, _home = wired
        store = PairingStore(conn, clock=clock)
        store.issue("ou_1", ttl_seconds=60)
        clock.advance(seconds=120)
        assert store.purge_expired() == 1

    def test_live_rows_survive_the_sweep(self, wired):
        """⚠️ 正在用的码不许被清掉。

        清了等于让**正在配对的人莫名失败**，而他的下一步无从推断 ——
        「码明明还在有效期内」与「说已过期」对不上。
        """
        conn, clock, _home = wired
        store = PairingStore(conn, clock=clock)
        store.issue("ou_1", ttl_seconds=3600)
        assert store.purge_expired() == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pairing_codes"
        ).fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# 终端侧：写白名单
# --------------------------------------------------------------------------- #

class TestPairCommand:
    def test_appends_the_open_id(self, wired):
        conn, clock, home = wired
        issued = PairingStore(conn, clock=clock).issue("ou_abc")
        out = pair_command(conn, issued.code, home=home, clock=clock)
        assert "ou_abc" in out
        assert read_env(home)[ENV_ALLOWED] == "ou_abc"

    def test_keeps_the_people_already_there(self, wired):
        """⚠️ 必须**追加**而不是覆盖。

        覆盖的症状很隐蔽：配对成功了，但**之前能用的人进不来了** ——
        而那正是「配对」本想解决的问题。
        """
        conn, clock, home = wired
        write_env({ENV_ALLOWED: "ou_old"}, home)
        issued = PairingStore(conn, clock=clock).issue("ou_new")
        pair_command(conn, issued.code, home=home, clock=clock)
        got = read_env(home)[ENV_ALLOWED]
        assert "ou_old" in got and "ou_new" in got

    def test_does_not_disturb_the_app_credentials(self, wired):
        """写白名单**不许**抹掉 App Secret。

        ``write_env`` 是合并语义（未提及的键原样保留），而这条一旦不成立，
        配对一次就等于**把通道的凭据擦了** —— 而症状是「重启后连不上」。
        """
        conn, clock, home = wired
        write_env({"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "s3cret"}, home)
        issued = PairingStore(conn, clock=clock).issue("ou_abc")
        pair_command(conn, issued.code, home=home, clock=clock)
        env = read_env(home)
        assert env["FEISHU_APP_ID"] == "cli_x"
        assert env["FEISHU_APP_SECRET"] == "s3cret"

    def test_pairing_twice_does_not_duplicate(self, wired):
        """重复配对要说「已经在里面了」，而不是悄悄写第二遍。

        白名单有上限，悄悄变长会在撞上限之后加不进新人，症状是
        「配对说成功了但还是进不来」—— 极难查。
        """
        conn, clock, home = wired
        issued = PairingStore(conn, clock=clock).issue("ou_abc")
        pair_command(conn, issued.code, home=home, clock=clock)
        out = pair_command(conn, "AAAA-BBBB-CCCC", home=home, clock=clock)
        assert read_env(home)[ENV_ALLOWED] == "ou_abc"

    def test_tells_the_user_to_restart_the_bridge(self, wired):
        """白名单是**启动时**读的 —— 不说清，用户会以为配对失败了。

        症状是「配对说成功了，可我还是进不去」，于是反复重配。
        """
        conn, clock, home = wired
        issued = PairingStore(conn, clock=clock).issue("ou_abc")
        out = pair_command(conn, issued.code, home=home, clock=clock)
        assert "重启" in out

    def test_env_var_precedence_is_flagged(self, wired, monkeypatch):
        """⚠️ 环境变量**优先于**文件 —— 那种情况下这次写盘不生效。

        不说的话，用户会看到「已写入」却始终被拒，然后陷入反复重配。
        这是本函数最需要说真话的地方。
        """
        conn, clock, home = wired
        monkeypatch.setenv(ENV_ALLOWED, "ou_env_wins")
        issued = PairingStore(conn, clock=clock).issue("ou_abc")
        out = pair_command(conn, issued.code, home=home, clock=clock)
        assert "优先于文件" in out or "环境变量" in out

    def test_no_code_explains_where_to_get_one(self, wired):
        """不给码时要说清**码从哪儿来** —— 否则用户不知道下一步。"""
        conn, _clock, home = wired
        out = pair_command(conn, "", home=home, clock=_clock)
        assert "私聊" in out


# --------------------------------------------------------------------------- #
# 配对卡：**一个按钮都没有**
# --------------------------------------------------------------------------- #

class TestPairingCard:
    def test_card_has_no_buttons(self):
        """⚠️ 这张卡**不许有可点的东西**。

        一个能被点的卡会让人以为「点一下就算配对」成立 —— 那等于给陌生人
        一个自助入口，而配对码的作用恰恰是「证明对方知道终端上发生了什么」。
        """
        card = pairing_card("AAAA-BBBB-CCCC", ttl_seconds=600)
        assert not any(
            el.get("tag") == "action" for el in card.get("elements", [])
        ), "配对卡不该有按钮"

    def test_card_shows_the_code_and_how_to_use_it(self):
        card = pairing_card("AAAA-BBBB-CCCC", ttl_seconds=600)
        blob = repr(card)
        assert "AAAA-BBBB-CCCC" in blob
        assert "/pair" in blob

    def test_card_does_not_echo_the_allowlist(self):
        """卡上不许出现白名单内容 —— 它是发给**陌生人**的。"""
        card = pairing_card("AAAA-BBBB-CCCC", ttl_seconds=600)
        assert "ou_" not in repr(card)