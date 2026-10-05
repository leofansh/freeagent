"""Plan 状态必须活过 LRU 淘汰与重启（设计文档 12.7.2）。

## 这组测试挡的是什么

修之前，``Repl._plan`` 只活在内存里，而 ``ChannelService._repls`` 是
**LRU** —— 到上限 ``popitem(last=False)`` 丢整个 Repl，Plan 与模式一并消失。

实测症状（不是报错，是**静默丢失**）::

    A 进入 plan 说了两句 → B 插进来挤掉 A → A 再说一句
    → 新 Repl 的 _plan 是 []、模式退回 build，且没有任何提示

报错他会重来；静默丢失他只会以为系统记错了。**所以这些断言盯的是
「恢复后内容还在」，不是「恢复过程没抛异常」。**

## 头号陷阱：测的时候别绕过新建路径

第一版这个测试自己 ``new`` 了个 Repl 塞进 ``_repls``，于是
``_run`` 里的「新建 → restore_plan」那条路**根本没被走到**，测出来是空的
—— 假失败。

所以下面全部只走 :meth:`ChannelService._run`，不碰 ``_repls``。
``test_does_not_bypass_the_restore_path`` 专门把这条钉死。
"""

import io

import pytest

from freeagent.app import build_app
from freeagent.cli.app import MODE_PLAN, Repl
from freeagent.services.channel import ChannelService
from freeagent.services.plan_state import PLAN_TTL_SECONDS, PlanStore


@pytest.fixture()
def svc(tmp_path):
    app = build_app(tmp_path / "a.db")
    # max_chats=1：第二个会话一进来就必然挤掉第一个
    service = ChannelService(app, max_chats=1)
    yield service, app
    app.close()


def _run(svc: ChannelService, chat: str, text: str) -> None:
    svc._run(chat, "ou_x", text)


# --------------------------------------------------------------------------- #
# 跨 LRU 淘汰
# --------------------------------------------------------------------------- #
def test_plan_survives_lru_eviction(svc):
    """A 被挤掉之后，Plan 与模式**都要**回来。"""
    service, app = svc
    _run(service, "chat_A", "/mode-plan")
    _run(service, "chat_A", "第一件事")
    _run(service, "chat_A", "第二件事")

    _run(service, "chat_B", "/mode-plan")   # 挤掉 A
    assert "chat_A" not in service._repls, "本测试的前提是 A 已被淘汰"

    _run(service, "chat_A", "第三件事")      # A 重连 -> 新建 -> restore_plan
    repl = service._repls["chat_A"]
    assert repl._plan == ["第一件事", "第二件事", "第三件事"], \
        f"Plan 跨淘汰丢了：{repl._plan}"
    assert repl._mode == MODE_PLAN, "模式没跟着恢复"


def test_recovery_still_creates_no_tasks(svc):
    """恢复**不能**破坏零副作用 —— 恢复只是把文字读回来。"""
    service, app = svc
    _run(service, "chat_A", "/mode-plan")
    _run(service, "chat_A", "要做的事")
    _run(service, "chat_B", "/mode-plan")
    _run(service, "chat_A", "还是不做")
    assert app.tasks.list_all() == [], "恢复过程建了事务 —— 零副作用被破了"


def test_does_not_bypass_the_restore_path(svc):
    """**只能**通过 ChannelService 的真实路径存取 Plan。

    这条挡的是「测试自己 new 一个 Repl 塞进 _repls」那种写法 ——
    那样 ``_run`` 里的 restore_plan 压根没被走到，测试会假失败，
    而真机上 Plan 照样丢。断言落点：走 ``_run`` 就能恢复。
    """
    service, _app = svc
    _run(service, "chat_A", "/mode-plan")
    _run(service, "chat_A", "东西")
    _run(service, "chat_B", "/mode-plan")
    _run(service, "chat_A", "别做")
    assert service._repls["chat_A"]._plan == ["东西", "别做"]


# --------------------------------------------------------------------------- #
# 跨重启（落盘的全部意义）
# --------------------------------------------------------------------------- #
def test_plan_survives_process_restart(tmp_path):
    """**换一个 app 实例**（模拟重启）仍能读回。

    这条才是落盘的正当理由：LRU 淘汰可以在同一进程内恢复，而**重启**不行 ——
    内存里什么都没了。
    """
    app1 = build_app(tmp_path / "a.db")
    svc1 = ChannelService(app1, max_chats=8)
    svc1._run("chat_A", "ou_x", "/mode-plan")
    svc1._run("chat_A", "ou_x", "重启前说的")
    app1.close()

    app2 = build_app(tmp_path / "a.db")
    try:
        svc2 = ChannelService(app2, max_chats=8)
        svc2._run("chat_A", "ou_x", "重启后说的")
        assert svc2._repls["chat_A"]._plan == ["重启前说的", "重启后说的"]
    finally:
        app2.close()


# --------------------------------------------------------------------------- #
# 退出规划要清干净
# --------------------------------------------------------------------------- #
def test_confirming_plan_clears_the_stored_state(svc):
    """**确认之后**不留残影 —— 否则下次进来凭空多出一份计划。

    注意是「确认之后」而不是「``/mode-build`` 之后」：``/mode-build`` 现在
    只是**发出确认卡**，此时计划必须还在 —— 否则用户点「取消」就没得取消了，
    而那道「取消」正是确认卡必须提供的第三个选择。
    """
    service, _app = svc
    _run(service, "chat_A", "/mode-plan")
    _run(service, "chat_A", "一件事")
    _run(service, "chat_A", "/mode-build")          # 只发卡，计划留着
    _run(service, "chat_A", "/mode-build ok")    # 确认后才清（前面已发卡）
    _run(service, "chat_B", "/mode-plan")          # 挤掉 A，逼它从盘上读
    _run(service, "chat_A", "你好")
    assert service._repls["chat_A"]._plan == []


def test_plan_survives_a_cancel(svc):
    """「再想想」之后计划**必须还在** —— 包括跨会话淘汰之后。"""
    service, _app = svc
    _run(service, "chat_A", "/mode-plan")
    _run(service, "chat_A", "一件事")
    _run(service, "chat_A", "/mode-build")
    _run(service, "chat_A", "/mode-build cancel")
    _run(service, "chat_B", "/mode-plan")     # 挤掉 A，逼它从盘上读
    # 取消后确认卡已收，所以这句话**会**被接受追加。
    # 这里只关心「原计划还在」—— 那才是「再想想 ≠ 扔掉」的意思。
    _run(service, "chat_A", "/mode-build")    # 再发一次卡，好让下一句被冻结
    got = service._repls["chat_A"]._plan
    assert "一件事" in got, \
        f"取消后计划丢了 —— 那就是「再想想」变成「扔掉」：{got!r}"


# --------------------------------------------------------------------------- #
# 过期当不存在
# --------------------------------------------------------------------------- #
def test_expired_plan_reads_as_absent(tmp_path):
    """超过 TTL 的 Plan **当没有**，不报错也不返回。

    理由与 approval 一致：一份放了很久的 Plan 描述的很可能是上周那件事，
    此刻拿它去动手是危险的。而「过期」不是「失败」，所以不抛异常。
    """
    app = build_app(tmp_path / "a.db")
    try:
        store = PlanStore(app.conn)
        store.save("chat_A", MODE_PLAN, ("老计划",))

        class _Old:
            @staticmethod
            def now():
                import datetime
                return datetime.datetime.now() + datetime.timedelta(
                    seconds=PLAN_TTL_SECONDS + 60)

        assert store.load("chat_A", now=_Old.now()) is None
        # 没过期时读得到
        assert store.load("chat_A") is not None
    finally:
        app.close()


def test_corrupt_row_reads_as_absent(tmp_path):
    """坏行当没有，**绝不抛** —— 读状态失败不该让对话崩掉。"""
    app = build_app(tmp_path / "a.db")
    try:
        app.conn.execute(
            "INSERT INTO plan_sessions (chat_id, mode, lines, updated_at) "
            "VALUES ('chat_X', 'plan', '这不是 JSON', '2026-01-01T00:00:00')")
        app.conn.commit()
        assert PlanStore(app.conn).load("chat_X") is None
    finally:
        app.close()


# --------------------------------------------------------------------------- #
# 终端会话不该被牵连
# --------------------------------------------------------------------------- #
def test_terminal_repl_works_without_a_chat_id(tmp_path):
    """终端没有 ``chat_id`` —— 落盘必须**跳过**，而不是崩。

    ``channel_ctx`` 为 None 时 ``_persist_plan`` 直接 return。
    """
    app = build_app(tmp_path / "a.db")
    try:
        r = Repl(app, out=io.StringIO())
        r.handle("/mode-plan")
        r.handle("终端里说的")
        assert r._plan == ["终端里说的"]
    finally:
        app.close()