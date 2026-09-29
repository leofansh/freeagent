"""锁住「包成 SDK 模型后 JSON 不变」这件事。

背景：``register_p2_card_action_trigger`` 的签名要求 handler 返回
``P2CardActionTriggerResponse``，我们返回的是 ``dict[str, Any]``。运行时
两者都能被 SDK 序列化，所以线上一直"看起来没事"。

但"看起来没事"和"改了也不会出事"是两回事 —— 要改就得先证明。
证明方式是实测：``JSON.marshal(model)`` 与 ``JSON.marshal(dict)`` 必须
**完全相等**。

为什么这个测试值得存在：SDK 的 ``P2CardActionTriggerResponse`` 声明了
``toast.i18n`` 和 ``card`` 两个字段，把 dict 塞进去后它们会被初始化成
``None``。SDK 的 ``Encoder`` 靠 ``filter_null`` 逐层剥掉 ``None`` ——
**万一哪天 SDK 换了序列化方式**（或者我们绕过 Encoder），那个
``"i18n": null`` 就会真的发到飞书，然后我们又要花一整轮去查"点了又报错"。
"""
import pytest

pytest.importorskip("lark_oapi")

from lark_oapi.core.json import JSON
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackCard,
    CallBackToast,
    P2CardActionTriggerResponse,
)

from freeagent.feishu import bridge as bridge_mod
from freeagent.feishu.sender import _card_action_response, _decided_card

# 不需要 ``wired`` 那个 fixture：这里只验**序列化形状**。
# ``_card_action({})`` 走的是「载荷不认」分支，在碰数据库之前就返回了。


def _raw_response(granted: bool = True) -> dict:
    return _card_action_response(
        _decided_card("确认结果", "**已允许**", granted=granted),
        "success" if granted else "error",
        "已允许" if granted else "已拒绝",
    )


class TestSdkEnvelopeRoundTrip:
    """``dict`` → 模型 → JSON，必须与 ``dict`` → JSON 逐字节相同。"""

    def test_marshal_is_identical_for_allowed(self):
        raw = _raw_response(True)
        assert JSON.marshal(P2CardActionTriggerResponse(raw)) == JSON.marshal(raw)

    def test_marshal_is_identical_for_denied(self):
        raw = _raw_response(False)
        assert JSON.marshal(P2CardActionTriggerResponse(raw)) == JSON.marshal(raw)

    def test_null_fields_do_not_leak(self):
        """模型必然多出 ``i18n=None``；它不能出现在发出去的 JSON 里。"""
        raw = _raw_response(True)
        text = JSON.marshal(P2CardActionTriggerResponse(raw)) or ""
        assert '"i18n"' not in text, "i18n=None 泄漏到线上了"
        assert "null" not in text, "还有别的 None 漏出去了"

    def test_model_actually_populates_both_fields(self):
        """不是"反正 marshal 一样所以没差别"。

        确认模型**确实**解析出了 toast 和 card —— 也就是说相等不是因为
        模型是空壳，而是因为 Encoder 把两边归一成了同一个东西。
        """
        model = P2CardActionTriggerResponse(_raw_response(True))
        assert isinstance(model.toast, CallBackToast), "toast 没被解析"
        assert isinstance(model.card, CallBackCard), "card 没被解析"
        assert model.toast.type == "success"
        assert model.card.type == "raw"
        assert model.card.data["header"]["template"] == "green"


class TestAdapterReturnsRealModel:
    """``_card_action_sdk`` 必须真的返回模型，不能是 ``cast`` 出来的假象。"""

    def test_returns_sdk_model_not_dict(self):
        result = bridge_mod._card_action_sdk({})
        assert isinstance(result, P2CardActionTriggerResponse), (
            "适配器没有返回 SDK 模型 —— 是不是退回 cast 了？"
        )

    def test_marshal_matches_raw_dict_for_garbage_payload(self):
        """垃圾载荷那条分支也走同一层，不能只有正常路径被包。"""
        raw = bridge_mod._card_action({})
        assert JSON.marshal(bridge_mod._card_action_sdk({})) == JSON.marshal(raw)
