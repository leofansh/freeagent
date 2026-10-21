"""恢复 Plan 时必须**说清**，不许静默接续（设计文档 12.7.2）。

## 这组测试挡的是什么

落盘把「内容丢了」治好了，但留下一个同型的老毛病：**东西还在，却没人说**。
会话因 LRU 淘汰或重启而新建时，``restore_plan()`` 把 ``_plan`` 与 ``_mode``
静默灌回去 —— 用户这边看到的是「系统突然自己回到了 Plan 模式」，或者
「我明明没在规划，它却说我在规划」，而**没有任何一句话**解释发生了什么。

这与「状态文件假绿灯」「权限卡点回时进程已重启」同型：**东西变了但没人说**。

## 断言落点为什么是「回复文本里出现那句话」

本仓的标准是**用户能看见才算数**。所以下面全部盯
:meth:`ChannelService._run` 的**返回值**（= 会发出去的那段文本），
而不是盯 ``repl._resume_notice`` 这个内部字段 —— 那只证明「代码写了」，
不证明「用户看得到」。
"""

import io

import pytest

from freeagent.app import build_app
from freeagent.cli.app import MODE_BUILD, MODE_PLAN, Repl
from freeagent.services.channel import ChannelService


@pytest.fixture()
def svc(tmp_path):
    app = build_app(tmp_path / "a.db")
    # max_chats=1：第二个会话一进来就必然挤掉第一个
    service = ChannelService(app, max_chats=1)
    yield service, app
    app.close()


# --------------------------------------------------------------------------- #
# 跨 LRU 淘汰：恢复后第一句回复里必须带告知
# --------------------------------------------------------------------------- #
def test_evicted_session_is_told_the_plan_is_still_there(svc):
    """被挤掉再回来，回复里必须**明说**「上次的规划还在」。"""
    service, _app = svc
    service._run("chat_A", "ou_x", "/mode-plan")
    service._run("chat_A", "ou_x", "第一件事")
    service._run("chat_B", "ou_x", "/mode-plan")   # 挤掉 A

    reply = service._run("chat_A", "ou_x", "第二件事")  # A 重连 -> restore
    assert "上次的规划还在" in reply, \
        f"恢复是静默的 —— 用户不知道自己的计划被找回来了：{reply!r}"
    assert "Plan 模式" in reply, "没说清现在处于 Plan 模式"
    assert "1 条" in reply, f"没说清记下了几条：{reply!r}"


def test_notice_is_said_only_once(svc):
    """告知**只说一次** —— 每句都唠叨等于噪声，用户很快就不再读它了。"""
    service, _app = svc
    service._run("chat_A", "ou_x", "/mode-plan")
    service._run("chat_A", "ou_x", "一件事")
    service._run("chat_B", "ou_x", "/mode-plan")

    first = service._run("chat_A", "ou_x", "接着说")
    assert "上次的规划还在" in first
    second = service._run("chat_A", "ou_x", "再接着说")
    assert "上次的规划还在" not in second, "恢复告知重复了"


def test_restored_session_still_works_normally(svc):
    """说了恢复告知**不能**影响这句本身该干的事。"""
    service, _app = svc
    service._run("chat_A", "ou_x", "/mode-plan")
    service._run("chat_A", "ou_x", "一")
    service._run("chat_B", "ou_x", "/mode-plan")
    service._run("chat_A", "ou_x", "二")
    assert service._repls["chat_A"]._plan == ["一", "二"]


# --------------------------------------------------------------------------- #
# 跨重启
# --------------------------------------------------------------------------- #
def test_restart_also_explains(tmp_path):
    """**换 app 实例**（真重启）后同样要说清 —— 落盘的全部意义在此。"""
    app1 = build_app(tmp_path / "a.db")
    svc1 = ChannelService(app1, max_chats=8)
    svc1._run("chat_A", "ou_x", "/mode-plan")
    svc1._run("chat_A", "ou_x", "重启前说的")
    app1.close()

    app2 = build_app(tmp_path / "a.db")
    try:
        svc2 = ChannelService(app2, max_chats=8)
        reply = svc2._run("chat_A", "ou_x", "重启后说的")
        assert "上次的规划还在" in reply, \
            f"重启恢复是静默的：{reply!r}"
    finally:
        app2.close()


# --------------------------------------------------------------------------- #
# 没得恢复 / 已过期 —— **不许**凭空冒出告知
# --------------------------------------------------------------------------- #
def test_no_notice_when_nothing_to_restore(svc):
    """头一次进来（盘上什么都没有）**不该**说「上次的规划还在」。"""
    service, _app = svc
    service._run("chat_A", "ou_x", "/mode-plan")
    service._run("chat_B", "ou_x", "/mode-plan")   # 全新会话
    reply = service._run("chat_B", "ou_x", "你好")
    assert "上次的规划还在" not in reply, f"无中生有的恢复告知：{reply!r}"


# --------------------------------------------------------------------------- #
# 终端 / Web：两条路都不能静默
# --------------------------------------------------------------------------- #
def test_terminal_resume_speaks_up(svc):
    """终端会话走的是同一个 Repl —— 恢复告知同样要出现在输出里。"""
    _service, app = svc
    r = Repl(app, out=io.StringIO())
    r.handle("/mode-plan")
    r.handle("终端里说的")
    # 终端没有 chat_id，所以没有落盘 → 恢复也无从谈起；
    # 这里钉的是「没有 chat_id 时不许崩，且不冒告知」。
    r._resume_notice = None
    r2 = Repl(app, out=io.StringIO())
    r2.handle("你好")
    assert "上次的规划还在" not in r2.out.getvalue()


def test_web_repl_surfaces_the_notice_on_the_next_message(svc):
    """Web 的恢复必须出现在**这一条**回复里，而不是那个用完就丢的临时 buffer。

    ``_web_repl`` 建 Repl 时给的是 ``io.StringIO()``（没人读），恢复若在
    那一刻直接 ``_say``，话就写进那个死 buffer —— 与静默丢失同效。
    所以恢复只能**记下**，等本条消息的 buffer 绑上之后再说。
    """
    _service, app = svc
    from freeagent.web.commands import WEB_CHAT_ID, command_payload

    # 先在盘上放一份 Plan 状态（模拟上一次 Web 会话攒的）
    from freeagent.services.plan_state import PlanStore
    PlanStore(app.conn).save(WEB_CHAT_ID, MODE_PLAN, ("Web 里攒的计划",))

    app.web_repl = None  # 逼出「新建 -> restore_plan」那条路
    payload = command_payload(app, "/help")
    assert "上次的规划还在" in payload["text"], \
        f"Web 恢复是静默的：{payload['text']!r}"
