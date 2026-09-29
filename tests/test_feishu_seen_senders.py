"""seen_senders 的守卫：控制面显示的 ID 必须是**真的**。

## 为什么要有这组测试

这块最容易出的错是「显示得对，但意思是错的」：

- ``matched`` 一度恒为 ``True`` —— 那只说明「这个人在事件里带了 ID」，
  跟白名单毫无关系。而界面上那个标签写着「已在白名单」，于是**界面
  在说谎**，而且是说一个用户最关心的问题。
- 排序错了：用户来查「我的 ID 是多少」，要的是**最新那个**。不排序的话
  字典序（= 首次出现序）会把最早那个放最前，答非所问。
"""

from __future__ import annotations

import time

import pytest

from freeagent.feishu.events import IncomingMessage
from freeagent.feishu.status import SEEN_SENDERS_KEY, record_sender
from freeagent.web.endpoints_feishu import _seen_sender_rows

OU = "ou_alice_app1"
UID = "2d1b7bec"


def _msg(open_id: str = OU, user_id: str = UID, is_group: bool = False, **kw):
    return IncomingMessage(
        event_id=kw.get("event_id", "e1"), chat_id="oc_1",
        sender_open_id=open_id, sender_user_id=user_id,
        text="hi", is_group=is_group, mentioned=True,
    )


# --- record_sender ------------------------------------------------------- #


def test_records_all_available_layers():
    seen: dict[str, dict[str, object]] = {}
    record_sender(seen, _msg())
    row = seen[OU]
    assert row["open_id"] == OU
    assert row["user_id"] == UID
    assert row["union_id"] == ""


def test_keyed_by_the_first_available_id():
    """键要稳定。只剩 user_id 的事件也要能记住同一个人。"""
    seen: dict[str, dict[str, object]] = {}
    record_sender(seen, _msg(open_id="", user_id=UID))
    assert UID in seen


def test_message_with_no_identity_is_not_recorded():
    """没有标识 = 无法回答「你是谁」，记它只是噪音。"""
    seen: dict[str, dict[str, object]] = {}
    record_sender(seen, _msg(open_id="", user_id=""))
    assert seen == {}


def test_keeps_the_table_bounded():
    """状态文件会无限增长，那就会变成「磁盘上的第二份数据库」。"""
    seen: dict[str, dict[str, object]] = {}
    for i in range(30):
        record_sender(seen, _msg(open_id=f"ou_{i}"), keep=20)
    assert len(seen) == 20


def test_eviction_drops_the_least_recently_seen():
    """丢的必须是**最久没见**的。

    这条不显然：如果按字典序排（很常见的写法），丢的会是「最小的字符串」，
    真实数据里那既不是最早出现的、也不是最久没见的 —— 两回事。
    """
    seen: dict[str, dict[str, object]] = {}
    record_sender(seen, _msg(open_id="ou_old"))
    seen["ou_old"]["seen_at"] = 100.0
    for i in range(1, 25):
        record_sender(seen, _msg(open_id=f"ou_{i}"))
        seen[f"ou_{i}"]["seen_at"] = 200.0 + i
    record_sender(seen, _msg(open_id="ou_zzz_recent"))
    seen["ou_zzz_recent"]["seen_at"] = 9999.0
    assert "ou_zzz_recent" in seen
    assert "ou_old" not in seen


def test_is_group_is_recorded():
    """界面上要能标出「这条是群里 @ 的」，因为两条路径的门控不同。"""
    seen: dict[str, dict[str, object]] = {}
    record_sender(seen, _msg(is_group=True))
    assert seen[OU]["is_group"] is True


# --- 端点输出 ------------------------------------------------------------ #


def test_rows_expose_every_layer():
    rows = _seen_sender_rows({OU: {
        "open_id": OU, "user_id": UID, "union_id": "",
        "is_group": False, "seen_at": 1.0,
    }}, frozenset({OU}))
    assert rows[0]["open_id"] == OU
    assert rows[0]["user_id"] == UID


def test_matched_is_true_only_when_the_allowlist_really_matches():
    """**核心**：这个标签说的是「在白名单里」，那就必须是真在白名单里。"""
    entry: dict[str, object] = {OU: {
        "open_id": OU, "user_id": UID, "union_id": "",
        "is_group": False, "seen_at": 1.0,
    }}
    assert _seen_sender_rows(entry, frozenset({OU}))[0]["matched"] is True
    assert _seen_sender_rows(entry, frozenset({UID}))[0]["matched"] is True
    assert _seen_sender_rows(entry, frozenset({"ou_other"}))[0]["matched"] is False
    # 关键回归：白名单**空**时绝不能显示「已在白名单」
    assert _seen_sender_rows(entry, frozenset())[0]["matched"] is False


def test_matched_defaults_to_false_when_allowlist_is_missing():
    """宁可说「没授权」，也不要在信息缺失时说「已授权」。"""
    entry: dict[str, object] = {OU: {"open_id": OU, "user_id": "", "union_id": "", "seen_at": 1.0}}
    assert _seen_sender_rows(entry)[0]["matched"] is False


def test_rows_are_newest_first():
    """用户查「我的 ID 是多少」要的是**最新那个**。"""
    entry: dict[str, object] = {
        "ou_first": {"open_id": "ou_first", "seen_at": 100.0},
        "ou_last": {"open_id": "ou_last", "seen_at": 900.0},
        "ou_mid": {"open_id": "ou_mid", "seen_at": 500.0},
    }
    rows = _seen_sender_rows(entry, frozenset())
    assert [r["open_id"] for r in rows] == ["ou_last", "ou_mid", "ou_first"]


def test_garbage_input_does_not_crash():
    """状态文件可能被强杀写坏 —— 界面不该跟着 500。"""
    assert _seen_sender_rows(None, frozenset()) == []
    assert _seen_sender_rows("not a dict", frozenset()) == []
    assert _seen_sender_rows({OU: "not a dict"}, frozenset()) == []


def test_recorded_rows_carry_no_message_text():
    """**隐私约束**：只记身份，不记「你说了什么」。"""
    seen: dict[str, dict[str, object]] = {}
    record_sender(seen, _msg())
    blob = repr(seen)
    assert "text" not in blob
    assert "hi" not in blob


def test_seen_senders_key_is_the_documented_one():
    """字段名要和控制面读的一致 —— 两处各写一个字符串就会静默读不到。"""
    assert SEEN_SENDERS_KEY == "seen_senders"
