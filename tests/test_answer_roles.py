"""``ChatService._answer_roles``：不点名也能列出全部脉络。

## 投诉

问「我有哪些角色脉络?」，拿回来的是**今天视图**加一句「没听懂你问的
是哪一类」。实测于真实 Web 链路。

## 根因：把「没点名」当成了「没有这条信息」

``_dispatch_route`` 里原本是：

    role = self._match_role(text)
    if role is None:
        return None            # 没点名任何脉络

但「没点名」说的是**用户那句话里没有名字**，而脉络列表明明查得到。
把前者当成后者，于是这类问题**无路可走**，一路落到硬编码的 today
兜底 —— 而兜底又不报错，用户看到的是一个**自信的错误答案**。

修法不是新增关键词，而是让 ``role`` 视图多担一件事：
**没点名就列全部**。与 :meth:`_answer_role` 的分工是
「有哪些条」对「某一条名下的事」。
"""

from __future__ import annotations

import pytest

from freeagent.services.chat import ChatReplyKind, ChatService


def _svc(tmp_path, roles: tuple[str, ...]) -> ChatService:
    from freeagent.app import build_app

    app = build_app(tmp_path / "agent.db")
    for name in roles:
        app.roles.create(name)
    return app.chat


def test_问有哪些脉络要列出它们_而不是今天视图(tmp_path) -> None:
    chat = _svc(tmp_path, ("工作项目A", "家务"))
    reply = chat.try_view("我有哪些角色脉络?")
    assert reply is not None, "无路可走 —— 于是退回今天的兜底"
    assert "工作项目A" in reply.text
    assert "家务" in reply.text
    assert "今天没有排进来的事务" not in reply.text


def test_点名时仍然答那一条(tmp_path) -> None:
    """分工：点名 → 那一条名下的事；不点名 → 有哪些条。"""
    chat = _svc(tmp_path, ("工作项目A", "家务"))
    reply = chat.try_view("工作项目A 里有什么?")
    assert reply is not None
    assert "工作项目A" in reply.text


def test_一条脉络也要能列(tmp_path) -> None:
    chat = _svc(tmp_path, ("只有一条",))
    reply = chat.try_view("我有哪些脉络?")
    assert reply is not None
    assert "只有一条" in reply.text


def test_没有脉络时不装懂(tmp_path) -> None:
    """空库要说「还没有」，不能返回空清单 —— 那看起来像「有 0 条」。"""
    chat = _svc(tmp_path, ())
    reply = chat.try_view("我有哪些脉络?")
    assert reply is not None
    assert "还没有任何脉络" in reply.text


def test_兜底提示里的候选都得是真能问到的(tmp_path) -> None:
    """原来那个三选一把用户往不存在的路上引。"""
    chat = _svc(tmp_path, ("工作",))
    reply = chat.respond("随便一句看不懂的话")
    if reply.inferred:
        notice = reply.text.splitlines()[0]
        for phrase in ("我有哪些脉络", "有哪些项目", "你能做什么"):
            assert phrase in notice, f"兜底提示漏了「{phrase}」——那是真能问到的"
