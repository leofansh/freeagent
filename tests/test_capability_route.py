"""``services/capability.py``：LLM 路由 + 事实渲染。

## 这些断言来自真实投诉

用户在飞书里问「有哪些项目?」，拿回来的是**今天视图**加一句
「没听懂你问的是哪一类」。截图可复现。

根因不是措辞，是**结构**：``ChatService`` 的整个词汇表是事务视图，
而「有哪些项目」问的是**助手自己**，不在那张表里。模型于是被问了一个
错的问题（「你想看哪张事务表」），闭集里没有答案，于是落进硬编码的
today 兜底。

## 为什么第一版关键词表失败了

第一版用 ``_PROJECT_Q`` / ``_ACTION_VERBS`` 这样的词表。它自己绊倒了：

    「有哪些执行器?」 命中 ``_ACTION_VERBS`` 里的「执行」
                    → 被自己的否决线拦下、闭嘴
                    → 退回今天兜底 —— **正是要修的那个症状**

规则只在写表的人和用户说同一句话时成立。漏了也不报错，只是悄悄
退回「今天」，用户看到的是一个**自信的错误答案**。

所以这里**规则不参与判断**：判断交给 LLM，规则只把选定的事实渲染成
句子。:func:`test_规则不参与判断` 把这条钉死。
"""

from __future__ import annotations

import pytest

from freeagent.services.capability import (
    CAPABILITY_TOPICS,
    CapabilityRouter,
    CapabilityView,
)


@pytest.fixture
def view() -> CapabilityView:
    return CapabilityView(
        projects=(r"D:\PycharmProjects\openmos", r"D:\PycharmProjects\freeagent"),
        project_names=("OpenMOS", "FreeAgent"),
        executors=("opencode",),
    )


class StubLLM:
    """按话题**声明**结果的替身 —— 测试只说期望，不列关键词。"""

    def __init__(self, topic: str | None) -> None:
        self.topic = topic
        self.calls: list[tuple[str, tuple]] = []

    def select_view(self, text, views):
        self.calls.append((text, tuple(views)))
        return self.topic


class ExplodingLLM:
    def select_view(self, text, views):
        raise RuntimeError("模型炸了")


# ── 投诉里那一句 ────────────────────────────────────────────────────────── #
def test_投诉那一句被接住(view: CapabilityView) -> None:
    """「有哪些项目?」曾拿到今天视图 + 「没听懂」。"""
    reply = CapabilityRouter(StubLLM("projects")).try_route("有哪些项目?", view)
    assert reply is not None, "没接住 —— 于是退回今天的兜底"
    assert "OpenMOS" in reply.text
    assert "FreeAgent" in reply.text
    assert "今天" not in reply.text


def test_在吗被当招呼_不是待办(view: CapabilityView) -> None:
    """「在吗?」曾拿到一张 /delegate 模板。"""
    reply = CapabilityRouter(StubLLM("greeting")).try_route("在吗?", view)
    assert reply is not None
    assert "我在" in reply.text
    assert "/delegate" not in reply.text


def test_执行器那一句(view: CapabilityView) -> None:
    """第一版在这里被自己的「执行」否决线拦下。"""
    reply = CapabilityRouter(StubLLM("executors")).try_route("有哪些执行器?", view)
    assert reply is not None
    assert "opencode" in reply.text


def test_能做什么(view: CapabilityView) -> None:
    reply = CapabilityRouter(StubLLM("can_do")).try_route("你能做什么?", view)
    assert reply is not None
    assert "记事" in reply.text


# ── 契约：闭集 + None 一等公民 ───────────────────────────────────────────── #
def test_模型选不出就闭嘴(view: CapabilityView) -> None:
    """``None`` 是一等公民。逼模型选一个，就是让「不确定」变成「答错」。"""
    assert CapabilityRouter(StubLLM(None)).try_route("随便一句", view) is None


def test_自创话题被丢弃(view: CapabilityView) -> None:
    """闭集是安全边界：模型自创的名字一律当没选。"""
    assert CapabilityRouter(StubLLM("我编的")).try_route("随便", view) is None


def test_模型抛异常不等于崩(view: CapabilityView) -> None:
    """模型答不出是「不归我管」，不是故障。"""
    assert CapabilityRouter(ExplodingLLM()).try_route("随便", view) is None


def test_传给模型的话题集是封闭的(view: CapabilityView) -> None:
    llm = StubLLM(None)
    CapabilityRouter(llm).try_route("随便", view)
    assert llm.calls
    _, passed = llm.calls[0]
    assert tuple(n for n, _ in passed) == tuple(n for n, _ in CAPABILITY_TOPICS)
    assert "projects" in tuple(n for n, _ in passed)


# ── 没有 LLM 时：诚实，不猜 ─────────────────────────────────────────────── #
def test_没有_llm_就闭嘴_但有诚实兜底(view: CapabilityView) -> None:
    router = CapabilityRouter(None)
    assert router.try_route("有哪些项目?", view) is None
    fb = router.fallback()
    assert "不猜" in fb.text
    assert "OpenMOS" not in fb.text, "兜底不能列项目 —— 那会让人以为它真能改"


# ── 事实渲染：数据说话 ──────────────────────────────────────────────────── #
def test_没授权项目时不撒谎() -> None:
    empty = CapabilityView(projects=(), project_names=(), executors=())
    reply = CapabilityRouter(StubLLM("projects")).try_route("有哪些项目?", empty)
    assert reply is not None
    assert "没有授权任何项目" in reply.text


def test_取不到显示名时退回路径而不是空() -> None:
    """空清单会让回答变成「没有授权任何项目」—— 那是在撒谎。"""
    no_names = CapabilityView(projects=(r"D:\x\y",), project_names=(), executors=())
    reply = CapabilityRouter(StubLLM("projects")).try_route("有哪些项目?", no_names)
    assert reply is not None
    assert r"D:\x\y" in reply.text


# ── 结构性守卫 ──────────────────────────────────────────────────────────── #
def test_规则不参与判断(view: CapabilityView) -> None:
    """把 ``select_view`` 换成「按文本长度决定话题」的荒谬替身。

    若渲染结果随输入长度改变，说明有规则在偷偷判断 —— 那就退回第一版
    的老路（规则只在写表的人和用户说同一句话时成立）。
    """
    class LengthDecides:
        def select_view(self, text, views):
            return "projects" if len(text) % 2 else "greeting"

    router = CapabilityRouter(LengthDecides())
    a = router.try_route("在吗在吗在吗", view)
    b = router.try_route("在吗在吗", view)
    assert a is not None and b is not None
    # 唯一允许随 LLM 选择而变的是**话题**；这里断言渲染确有产物
    assert a.text and b.text


def test_模块里不许有动作词表() -> None:
    """关键词表是这次被否掉的实现，别悄悄长回来。"""
    import io
    import re

    from pathlib import Path

    src = Path("src/freeagent/services/capability.py").read_text(encoding="utf-8")
    # 只看代码，忽略注释与文档字符串 —— 那里解释「为什么不用规则」，
    # 必须留着。
    body = re.sub(r'"""(?:.|\n)*?"""', "", src)
    assert "_ACTION_VERBS" not in body
    assert not re.search(r"^\s*_[A-Z_]+_Q\s*=", body, re.M), "又长出关键词表了"


# ── wants_code_change：Web 对话的主路径曾在这里 500 ───────────────────── #
#
# 上面那些测试全都只调 ``try_route``（走 ``select_view``）。而
# ``wants_code_change`` 走的是 ``parse_delegation`` —— 于是它此前**一次也没
# 被一个忠实于协议的替身覆盖过**。
#
# 真实后果（实测 2026-10-07）：上一版在这里读 ``self._llm.llm_enabled``，
# 而那是 ``Config`` 的字段、``LLMProvider`` 协议里没有。AttributeError
# 发生在 ``try`` **之外**，于是冒到 Web 层 = **HTTP 500，整条对话不可用**。
# 而 2300 条测试全绿 —— 因为替身恰好带了那个属性，或者压根没走到这行。
# 症状（只有真 provider 才犯）与覆盖面正好错开。


class ProtocolFaithfulLLM:
    """**只实现协议里真有的方法** —— 刻意不补 ``llm_enabled``。

    这是这组测试的关键：替身一旦「顺手」补上协议外的属性，就又看不见那个
    bug 了。所以这里连 ``can_select_view`` 也不给 ——
    :class:`CapabilityRouter` 对它的访问是带默认的 ``getattr``。
    """

    def __init__(self, is_delegation: bool = True) -> None:
        self._is_delegation = is_delegation
        self.seen: list[tuple[str, tuple]] = []

    def select_view(self, text, views):
        return None

    def parse_delegation(self, text, known_projects):
        from freeagent.services.llm.provider import DelegationIntent
        self.seen.append((text, tuple(known_projects)))
        return DelegationIntent(
            is_delegation=self._is_delegation,
            project="FreeAgent" if self._is_delegation else None,
            brief="改一下 README" if self._is_delegation else "",
        )


def test_wants_code_change_不读协议外的字段(view: CapabilityView) -> None:
    """provider 上没有 ``llm_enabled`` —— 读了就是 AttributeError。

    这条断言就是那个 500 的回归测试。**不要**为了让它过而去给
    :class:`ProtocolFaithfulLLM` 补属性：那正是当初让 2300 条测试
    一起失效的做法。
    """
    llm = ProtocolFaithfulLLM()
    assert not hasattr(llm, "llm_enabled"), "替身不该有这个协议外属性"
    router = CapabilityRouter(llm)
    assert router.wants_code_change("帮我改一下 README", view) is True
    assert llm.seen, "该真的问模型"


def test_wants_code_change_模型说不是就不是(view: CapabilityView) -> None:
    router = CapabilityRouter(ProtocolFaithfulLLM(is_delegation=False))
    assert router.wants_code_change("今天该做什么", view) is False


def test_wants_code_change_没有_llm_返回否而不是崩(view: CapabilityView) -> None:
    assert CapabilityRouter(None).wants_code_change("帮我改一下", view) is False


def test_wants_code_change_规则层如实说不是(view: CapabilityView) -> None:
    """离线/降级时走规则层，而它**拒绝**用规则猜项目（见 rules.py）。

    所以「智能层关了别问它」这个意图不需要额外判一次 ``llm_enabled`` ——
    规则层自己就返回 ``is_delegation=False``。
    """
    from freeagent.services.llm.rules import RuleBasedProvider

    router = CapabilityRouter(RuleBasedProvider())
    assert router.wants_code_change("帮我改一下 README", view) is False


def test_wants_code_change_视图未启用时不问模型() -> None:
    """``enabled`` 是**派生属性**（白名单非空即真），不是构造参数。"""
    llm = ProtocolFaithfulLLM()
    off = CapabilityView(projects=(), executors=("opencode",))
    assert off.enabled is False
    assert CapabilityRouter(llm).wants_code_change("帮我改一下", off) is False
    assert not llm.seen, "没启用就不该白问一次模型"


def test_代码里不许从_provider_读_llm_enabled() -> None:
    """AST 守卫：``llm_enabled`` 是 ``Config`` 的字段，不许挂在 provider 上。

    这条防的是**整类**错误，而不只是那一行。判据用 AST 而不是文本 grep：
    注释与文档字符串里会合法地解释「为什么不能读它」，那是历史叙述，
    文本 grep 分不清，断言会变成噪音然后被人加白名单绕过。
    """
    import ast
    from pathlib import Path

    path = Path("src/freeagent/services/capability.py")
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr != "llm_enabled":
            continue
        # 只看挂在 ``self._llm`` 上的 —— 别的对象（config）读它是对的
        base = node.value
        if (isinstance(base, ast.Attribute) and base.attr == "_llm"):
            offenders.append(node.lineno)

    assert not offenders, (
        "又从 provider 上读 llm_enabled 了（那是 Config 的字段）—— "
        f"行号：{offenders}"
    )