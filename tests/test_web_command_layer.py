"""Web 对话也要有**命令层** —— 与飞书/终端同一个派发（设计文档 12.7.2）。

## 这组测试挡的是什么

Web 的 ``/api/chat`` 原本直接调 ``ChatService``，于是**没有命令层**：
浏览器里敲 ``/mode-build`` 不会进规划模式。而这一整轮的交互改动
（Plan/Build、六道闸门、终止接口）在浏览器里**一个都验不到**。

## 分叉只有一处，而且比预想的窄

自然语言那条路**本来就是共用的** —— ``Repl._natural`` 就是委托给
``ChatService.respond`` 的（``cli/app.py:621``）。所以缺的只有命令层。

## 而「只分流命令」在原理上不够 —— 写测试才发现

Plan 模式靠**自由文本**累积。若自由文本照旧发给 ``ChatService``，
计划就永远是空的，于是 ``/mode-build`` 报「没有待执行的计划」。
所以路由条件必须包含「**这个会话正在规划中**」。

## 返回结构刻意不动

命令这条路返回**纯文字**，没有 ``items``。所以 ``/today`` 在 Web 上
看到的是文字而不是可点的卡片 —— 这是**已知代价**，不是 bug：
换成 ``ChannelReply`` 会让**所有**自然语言回复都退化成纯文字。
"""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.cli.app import MODE_BUILD, MODE_PLAN  # noqa: E402
from freeagent.web import endpoints_read as E  # noqa: E402


@pytest.fixture()
def web(tmp_path):
    app = build_app(tmp_path / "a.db")
    app.roles.create("工作")
    yield app
    app.close()


def _chat(app, text, **kw):
    return E.chat(app, {"text": text, "last_items": [], **kw})


# --------------------------------------------------------------------------- #
# 命令层存在了
# --------------------------------------------------------------------------- #
def test_today_command_works_in_web(web):
    """/today 现在能用 —— 它此前被当成自然语言送去ChatService。"""
    out = _chat(web, "/today")
    assert "今天" in str(out.get("text", ""))


def test_unknown_command_says_so(web):
    """未知命令要**明确说未知**，不能静默。"""
    out = _chat(web, "/nope-xyz")
    assert "未知命令" in str(out.get("text", ""))


def test_command_reply_has_the_same_shape(web):
    """命令的回复**结构**与自然语言一致 —— 不新造返回格式。

    这条是为了让 Web 的渲染层**完全不用改**：两侧都过 ``chat_payload``。
    """
    out = _chat(web, "/today")
    for field in ("kind", "text", "items", "task_id", "suggestions"):
        assert field in out, f"缺字段 {field} —— 渲染层会崩"


# --------------------------------------------------------------------------- #
# 规划期：自由文本**必须**到 Repl（这条是第一版漏掉的）
# --------------------------------------------------------------------------- #
def test_plan_mode_accumulates_free_text_in_web(web):
    """Plan 模式靠自由文本累积 —— 它必须到 Repl，否则计划永远是空的。"""
    _chat(web, "/mode-plan")
    _chat(web, "第一件事")
    _chat(web, "第二件事")
    out = _chat(web, "/mode-build")

    text = str(out.get("text", ""))
    assert "第一件事" in text and "第二件事" in text, \
        f"计划没累积上：{text!r}"
    assert "还没有执行任何东西" in text, "发卡时必须明说尚未执行"
    assert web.tasks.list_all() == [], "发卡那一刻不该建任何东西"


def test_plan_state_survives_across_requests(web):
    """Repl 是**常驻**的 —— 换个请求，计划仍在。

    这条挡的是「每次请求新建 Repl」：那样角色追问的半截输入会被切断，
    而Plan/Build 本来就是多轮的。
    """
    _chat(web, "/mode-plan")
    _chat(web, "第一件事")
    first = web.web_repl
    _chat(web, "/roles")
    assert web.web_repl is first, "Repl 被重建了 —— 多轮会断"
    out = _chat(web, "/mode-build")
    assert "第一件事" in str(out.get("text", ""))


# --------------------------------------------------------------------------- #
# 规划期结束 → 回到 ChatService（结构化渲染要回来）
# --------------------------------------------------------------------------- #
def test_after_plan_ends_natural_language_goes_back_to_chat_service(web):
    """退出规划后，自然语言**回到** ChatService —— 结构化渲染不能一直丢。

    这条是那条「代价」的守卫：命令/规划期是纯文字，但**平时**必须仍是
    结构化的（``items`` / ``kind``）。
    """
    assert web.web_repl is None or web.web_repl._mode == MODE_BUILD
    out = _chat(web, "改一下 README")
    assert out.get("kind") in {"answer", "recorded", "clarify", "cannot", "help"}
    # 角色推断会追问，所以结构化字段必须齐
    for field in ("items", "task_id", "suggestions"):
        assert field in out


# --------------------------------------------------------------------------- #
# 边界
# --------------------------------------------------------------------------- #
def test_plain_question_still_uses_chat_service(web):
    """普通提问**不该**被抢走 —— 它属于 ChatService。"""
    out = _chat(web, "我有哪些角色？")
    assert out.get("kind") in {"answer", "clarify", "cannot", "help"}
    # ChatService 才有 items；Repl 的纯文字回复 items 为空
    assert "items" in out


def test_last_items_validation_still_applies(web):
    """``last_items`` 的类型校验**不能**因为提前分流而被绕过。"""
    with pytest.raises(Exception):
        E.chat(web, {"text": "改一下 README", "last_items": "不是数组"})