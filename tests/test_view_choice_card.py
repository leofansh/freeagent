"""A 方案：只读选项卡 + 点击即执行（设计文档 12.1.1）。

这张卡**刻意无凭据** —— 它只切只读视图，没有副作用，因此没有东西需要授权。
这一点必须被测试钉住：一旦有人「顺手」给它套上 pending 凭据 + TTL + 不可篡改，
就是把那套为**授权**设计的机制用在**没有授权**的动作上 ——
凭空多出「卡片过期了但用户还没点」这类失败模式，而收益是零。
"""
import logging
from typing import Any

import pytest

from freeagent.feishu import bridge as bridge_mod
from freeagent.feishu.sender import VIEW_CHOICE_ACTION, view_choice_card

VIEW_CHOICE: tuple[str, ...] = ("今天该做什么",)


def _card_text(resp: dict[str, Any]) -> str:
    """从回写卡里取出全部可见文字。

    刻意**不**只看 ``header.title`` —— 那个字段是 ``{tag, content}`` 结构，
    而「为什么失败」写在 ``elements`` 里（标题只放「看不了」两个字，
    对用户没解释力）。踩过的坑：测试去断言 title 里有「没连上」，
    于是两次红，而实现其实**是对的**。
    """
    import json

    data = resp["card"]["data"]
    return json.dumps(data, ensure_ascii=False)


class TestChoiceCardIsStateless:
    """**没有凭据、没有 TTL、不查库。**"""

    def test_value_has_no_credential(self):
        card = view_choice_card("你要看哪个？", ("今天该做什么",), chat_id="oc_1")
        values = [
            btn["value"]
            for el in card["elements"] if el["tag"] == "action"
            for btn in el["actions"]
        ]
        assert values, "卡上没有按钮"
        for v in values:
            assert "id" not in v, f"按钮 value 不该带凭据：{v}"
            assert "credential" not in v, f"不该出现 credential：{v}"

    def test_value_carries_view_and_chat(self):
        card = view_choice_card("x", ("全部未结束的事",), chat_id="oc_9")
        btn = [e for e in card["elements"] if e["tag"] == "action"][0]["actions"][0]
        assert btn["value"]["action"] == VIEW_CHOICE_ACTION
        assert btn["value"]["view"] == "全部未结束的事"
        assert btn["value"]["chat"] == "oc_9"

    def test_no_action_id(self):
        """刻意**不**用 ``action_id`` 回卡片：只读结果直接发聊天气泡，
        卡片回写只用来收掉按钮。用回写更新会白花开一次更新卡权限。"""
        card = view_choice_card("x", VIEW_CHOICE, chat_id="oc_1")
        assert "card_link" not in card


class TestCardVersionMatchesApprovalFamily:
    """版本必须与批准卡**同族**（官方 200830：2.0 不能更新成 1.0，反之亦然）。"""

    def test_both_are_json_1_0(self):
        from freeagent.feishu.sender import _approval_card

        choice = view_choice_card("x", ("今天",), chat_id="oc_1")
        approval = _approval_card("x", "y", "ap-1")
        for name, card in (("选项卡", choice), ("批准卡", approval)):
            assert "schema" not in card, f"{name}里出现 schema —— 那是 2.0"
            assert "body" not in card, f"{name}里出现 body —— 2.0 才用"
            assert "config" in card and "elements" in card, f"{name}不像 1.0"


class TestEmptyChoicesRejected:
    def test_empty_raises(self):
        """没有选项就别发卡 —— 那是一张空按钮的卡，用户点了只会困惑。"""
        with pytest.raises(ValueError):
            view_choice_card("x", (), chat_id="oc_1")


class TestClickExecutesTheView:
    """点击真的跑那个视图，并把结果发回聊天窗口。"""

    @pytest.fixture()
    def wired(self, tmp_path, monkeypatch):
        from freeagent.app import build_app
        from freeagent.services.channel import ChannelService
        from freeagent.feishu.config import load_config

        monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path))
        app = build_app(tmp_path / "a.db")
        channel = ChannelService(app, allowed_senders=frozenset({"ou_me"}))
        sent: list[tuple[str, str]] = []

        class _Sender:
            def send_text(self, chat_id, text):
                sent.append((chat_id, text))

        monkeypatch.setattr(bridge_mod, "_card_channel", channel)
        monkeypatch.setattr(bridge_mod, "_card_sender", _Sender())
        yield {"app": app, "sent": sent, "channel": channel}
        app.close()

    def test_click_runs_and_replies(self, wired):
        resp = bridge_mod._run_view_choice(
            {"action": VIEW_CHOICE_ACTION, "view": "今天该做什么", "chat": "oc_1"},
            who="ou_me",
        )
        assert wired["sent"], "结果没有发进聊天窗口"
        chat_id, text = wired["sent"][0]
        assert chat_id == "oc_1"
        assert text.strip(), "发了空结果"

    def test_buttons_are_removed_by_the_writeback(self, wired):
        """回写后**不能**还有 action 元素 —— 留着按钮等于骗人。"""
        resp = bridge_mod._run_view_choice(
            {"action": VIEW_CHOICE_ACTION, "view": "今天该做什么", "chat": "oc_1"},
            who="ou_me",
        )
        data = resp["card"]["data"]
        assert not any(e.get("tag") == "action" for e in data["elements"]), (
            "已切换的卡不该还留着可点的按钮"
        )

    def test_incomplete_value_is_refused(self, wired):
        resp = bridge_mod._run_view_choice(
            {"action": VIEW_CHOICE_ACTION, "view": "今天该做什么"}, who="ou_me"
        )   # 缺 chat
        assert not wired["sent"], "缺 chat 还执行了"
        assert resp is not None

    def test_no_channel_means_no_execution(self, tmp_path, monkeypatch):
        """没连上通道 = 没人能执行 ⇒ **不回话**、不假装成功。"""
        from freeagent.app import build_app

        monkeypatch.setattr(bridge_mod, "_card_channel", None)
        monkeypatch.setattr(bridge_mod, "_card_sender", None)
        resp = bridge_mod._run_view_choice(
            {"action": VIEW_CHOICE_ACTION, "view": "今天", "chat": "oc_1"},
            who="ou_me",
        )
        assert "没连上" in _card_text(resp)

    def test_view_failure_does_not_raise(self, wired, monkeypatch):
        """视图跑挂了要变成**看得见的失败**，不是崩掉整个点击。"""
        def boom(*a, **k):
            raise RuntimeError("视图炸了")

        monkeypatch.setattr(wired["channel"], "handle", boom)
        resp = bridge_mod._run_view_choice(
            {"action": VIEW_CHOICE_ACTION, "view": "今天", "chat": "oc_1"},
            who="ou_me",
        )
        assert not wired["sent"]
        assert "没能跑成" in _card_text(resp)
