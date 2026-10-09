"""标题边缘清洗回归 —— 「记一下：X」不该建出「：X」。

## 这个 bug 是怎么实测出来的

WEB 接口模拟测试里，对话入口收到「记一下：给客户A发季度报价单，下周三前要」，
建出的**事务标题是「：给客户A发季度报价单，下周三前要」** —— 冒号留在头上。

根因：开场白表 ``_LEADING_FILLERS`` 里只有「记一下」，**不含**它后面那个冒号。
剥完开场白之后 ``lstrip()`` 只剥空白，冒号就留下了。

## 为什么两份实现都测

``rules``（离线兜底）和 ``deepseek``（联网）各有一份 ``refine_title``，
**两份都有这个 bug**。只修一份的症状是「联网时标题干净、离线降级时带冒号」——
那种差异没人会注意到。所以这里断言两者**结果一致**。
"""

from __future__ import annotations

import pytest

from freeagent.services.llm.deepseek import (
    DeepSeekConfig,
    DeepSeekProvider,
)
from freeagent.services.llm.rules import (
    TITLE_EDGE_CHARS,
    RuleBasedProvider,
    strip_title_edges,
)

SECRET = "sk-test-MUST-NEVER-LEAK"


def _reply(content: str) -> str:
    import json

    return json.dumps({"choices": [{"message": {"content": content}}]})


def _provider(returning: str) -> DeepSeekProvider:
    """按**真实接线**造：``build_provider`` 给的 provider 都带 rules 兜底。

    不带 fallback 的桩是不真实的 —— 那样测到的是「空标题直接抛 LLMError」，
    而生产里根本走不到那条路。
    """

    class _Transport:
        def __call__(self, url, payload, headers, timeout) -> str:
            return _reply(returning)

    return DeepSeekProvider(
        DeepSeekConfig(), SECRET, transport=_Transport(),
        fallback=RuleBasedProvider(),
    )


class TestStripTitleEdges:
    """剥边缘这件事本身。"""

    @pytest.mark.parametrize(
        "raw,want",
        [
            ("：给客户A发季度报价单", "给客户A发季度报价单"),
            (":给客户A发季度报价单", "给客户A发季度报价单"),
            ("，给客户A发季度报价单", "给客户A发季度报价单"),
            ("给客户A发季度报价单：", "给客户A发季度报价单"),
            ("  「给客户A发季度报价单」  ", "给客户A发季度报价单"),
            ("：：：多层冒号", "多层冒号"),
        ],
    )
    def test_edges_go_away(self, raw: str, want: str) -> None:
        assert strip_title_edges(raw) == want

    def test_middle_colon_is_kept(self) -> None:
        """中间的冒号是内容，不是残留 —— 不能剥。"""
        assert strip_title_edges("报价单：A 客户版本") == "报价单：A 客户版本"

    def test_all_nothing_left(self) -> None:
        assert strip_title_edges("：： ") == ""

    def test_backtick_in_the_set(self) -> None:
        """反引号也在集合里 —— 模型爱用 markdown 包标题。"""
        assert strip_title_edges("`标题`") == "标题"


class TestBothProvidersStripTheColon:
    """两份 ``refine_title`` 都要干净，且**结果一致**。"""

    @pytest.mark.parametrize(
        "text",
        [
            "记一下：给客户A发季度报价单",
            "记一下: 给客户A发季度报价单",
            "帮我记一下：窗户螺丝松了要修",
        ],
    )
    def test_rules_provider_has_no_leading_punctuation(self, text: str) -> None:
        title = RuleBasedProvider().refine_title(text)
        assert title[0] not in TITLE_EDGE_CHARS, f"标题带着残留：{title!r}"

    def test_rules_provider_exact(self) -> None:
        assert RuleBasedProvider().refine_title(
            "记一下：给客户A发季度报价单"
        ) == "给客户A发季度报价单"

    def test_deepseek_provider_strips_what_model_left(self) -> None:
        """模型照提示词剥了「记一下」却留下冒号 —— 代码层必须兜住。

        提示词里的「剥掉开场白」对模型只是建议；模型把冒号留在头上
        是实测行为（不是假设），所以这层清洗不能只依赖提示词。
        """
        title = _provider("：给客户A发季度报价单").refine_title(
            "记一下：给客户A发季度报价单"
        )
        assert title == "给客户A发季度报价单"

    def test_two_providers_agree(self) -> None:
        """不一致就是漂移：症状只会在离线降级时才冒出来。"""
        text = "记一下：给客户A发季度报价单"
        from_rules = RuleBasedProvider().refine_title(text)
        from_llm = _provider("：给客户A发季度报价单").refine_title(text)
        assert from_rules == from_llm


class TestTitleStillUsable:
    """修过头也是 bug —— 别把内容剥没了。"""

    def test_empty_after_strip_falls_back(self) -> None:
        """模型只回了冒号：剥完是空的，**必须**降级到 rules 而不是抛错。

        抛 LLMError 的代价是「用户说了一句正常的话，记不进去，还弹个错」；
        降级到 rules 的代价只是标题朴素一点。后者明显更可接受 ——
        rules 那份自己也保证绝不返回空串。
        """
        title = _provider("：").refine_title("记一下：给客户A发季度报价单")
        assert title.strip()
        assert title[0] not in TITLE_EDGE_CHARS

    def test_long_input_still_capped(self) -> None:
        from freeagent.services.llm.rules import TITLE_MAX_LEN

        long_text = "记一下：" + "这是一件很长很长的事情" * 10
        assert len(RuleBasedProvider().refine_title(long_text)) <= TITLE_MAX_LEN
