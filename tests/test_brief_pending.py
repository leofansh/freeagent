"""``brief_pending`` 的契约守卫：单次消费 / 覆盖写 / 按人隔离 / **不落盘**。

为什么每条行为各占一个测试：这几条是**安全属性**，不是普通功能 ——
任何一条破了都对应一类真实故障（见各测试的 docstring）。揉成一个
「全流程」测试的话，第一条断言失败就会挡住后面几条，看不出到底哪条破了。

为什么用 monkeypatch 重置 ``_PENDING`` 而不是用不同 open_id 绕开：
模块状态是**全进程一份**（这正是它的设计），用例之间不重置就会互相污染 ——
而「两个用例单独过、一起跑就红」正是夹具泄漏的经典症状。这里按仓库的
conftest 风格，在测试层把内部 dict 换新，不额外给模块加 reset 接口。
"""

from __future__ import annotations

import os
import pathlib

import pytest

from freeagent.feishu import brief_pending


@pytest.fixture(autouse=True)
def _clean_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例从一张空表开始。

    ``arm`` 等函数在运行时按**全局名**查 ``_PENDING``，所以直接替换模块
    属性即可让它们看到新表，用例结束 monkeypatch 自动还原。
    """
    monkeypatch.setattr(brief_pending, "_PENDING", {})


def _arm(open_id: str = "ou_me", **over: str) -> brief_pending.PendingBrief:
    """一份最小可用的选型快照，字段可被 ``over`` 覆写。"""
    fields = {
        "project": "D:/proj",
        "model": "opencode/big-pickle",
        "agent": "build",
        "variant": "high",
        "chat_id": "oc_group",
        **over,
    }
    return brief_pending.arm(open_id, **fields)


class TestSnapshot:
    def test_arm_then_peek_returns_fields_unchanged(self) -> None:
        """arm 存的选型必须原样读回，且 ``brief`` 与 arm 时一致（空）。"""
        entry = _arm(open_id="ou_a")
        got = brief_pending.peek("ou_a")
        assert got is entry
        assert got.project == "D:/proj"
        assert got.model == "opencode/big-pickle"
        assert got.agent == "build"
        assert got.variant == "high"
        assert got.chat_id == "oc_group"
        assert got.brief == "", "arm 出来时简报必须是空的（还没交）"

    def test_fill_brief_sets_brief_on_armed_record(self) -> None:
        """交来简报要填进已 arm 的记录，且其它字段不动。"""
        _arm(open_id="ou_a")
        filled = brief_pending.fill_brief("ou_a", "把登录页拆出来")
        assert filled is not None
        assert filled.brief == "把登录页拆出来"
        assert filled.model == "opencode/big-pickle"
        assert brief_pending.peek("ou_a").brief == "把登录页拆出来"


class TestSingleUse:
    def test_claim_twice_returns_none_second_time(self) -> None:
        """防「重复点击 / 重投建出两条委派」：取即删，第二次必为空。"""
        _arm(open_id="ou_a")
        first = brief_pending.claim("ou_a")
        assert first is not None
        second = brief_pending.claim("ou_a")
        assert second is None, "第二次 claim 必须拿不到东西，否则会建两条委派"
        assert brief_pending.peek("ou_a") is None

    def test_claim_unknown_open_id_returns_none(self) -> None:
        """没 arm 过的人 claim 只得 None —— 不能凭空造一条记录。"""
        assert brief_pending.claim("ou_never") is None

    def test_peek_does_not_consume(self) -> None:
        """peek 只读：看多少次都还在，不能把记录顺走。"""
        _arm(open_id="ou_a")
        brief_pending.peek("ou_a")
        brief_pending.peek("ou_a")
        assert brief_pending.peek("ou_a") is not None


class TestOverwriteAndIsolation:
    def test_second_arm_overwrites_first_for_same_open_id(self) -> None:
        """同一人重选四段后，新快照必须盖掉旧的（否则建出上一版选型）。"""
        _arm(open_id="ou_a", model="opencode/old")
        _arm(open_id="ou_a", model="opencode/new")
        assert brief_pending.peek("ou_a").model == "opencode/new"

    def test_two_open_ids_do_not_interfere(self) -> None:
        """防「群里另一个成员点一下，取到别人的简报并记成自己」。

        两人各自的 arm / claim / discard 必须互不影响 —— 这是按 open_id
        键控存在的全部理由。
        """
        _arm(open_id="ou_a", model="model-a")
        _arm(open_id="ou_b", model="model-b")

        assert brief_pending.peek("ou_a").model == "model-a"
        assert brief_pending.peek("ou_b").model == "model-b"

        claimed_a = brief_pending.claim("ou_a")
        assert claimed_a is not None and claimed_a.model == "model-a"
        # A 取走后，B 的记录必须还在，且内容不受影响。
        assert brief_pending.peek("ou_b") is not None
        assert brief_pending.peek("ou_b").model == "model-b"

    def test_discard_reports_whether_something_was_there(self) -> None:
        """discard 要如实返回「原来有没有东西」，调用方靠它决定要不要回话。"""
        _arm(open_id="ou_a")
        assert brief_pending.discard("ou_a") is True
        assert brief_pending.peek("ou_a") is None
        assert brief_pending.discard("ou_a") is False


class TestNoPersistence:
    def test_module_never_touches_disk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """本模块**纯内存**：任何一步都不许碰盘。

        为什么值得一条独立测试：落盘会把这份状态拖进 ``oc_selection`` 那条
        跨进程契约（见模块 docstring），而「顺手加一行 ``write_text``」在
        评审里很容易漏过。这里把写盘原语统统换成当场爆炸，再跑完整条生命
        周期 —— 只要有一步落盘就立刻失败，而不是等出现「重启后状态还在」
        这种难查的现象。
        """
        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("brief_pending 是纯内存的，不该写盘")

        for name in ("write_text", "write_bytes", "mkdir", "open", "touch"):
            monkeypatch.setattr(pathlib.Path, name, boom, raising=False)
        monkeypatch.setattr(os, "replace", boom)
        monkeypatch.setattr(os, "remove", boom)
        monkeypatch.setattr(os, "unlink", boom, raising=False)

        entry = _arm(open_id="ou_a")
        brief_pending.fill_brief("ou_a", "写句话")
        assert brief_pending.peek("ou_a") is entry
        assert brief_pending.claim("ou_a") is not None
        assert brief_pending.claim("ou_a") is None
        assert brief_pending.discard("ou_a") is False
