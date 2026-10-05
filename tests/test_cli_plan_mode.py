"""Plan 模式第一刀：进入/退出 + 零副作用（设计文档 12.7.2）

## 这一刀做什么、为什么不做什么

**做了**：``/mode-plan`` 进出、自由文本在 Plan 下**不落库**、多轮累积、退出时
复述。

**没做**：LLM 的 ``action`` 维度、Build 模式的确认卡。所以 ``/mode-build``
**现在还不会执行任何东西**，它只是把攒下的计划列出来。这是有意的——先有
一个说清边界的骨架，好过交付一个看起来能跑、实际静默降级的版本。

## 守的不变量

1. **Plan 期零副作用**：不建事务、不改文件。这条由 ``_dispatch`` 在自由文本
   分支上**先判模式**保证，而不是塞进 ``_natural``——后者是「记事」的正路，
   在里面混模式判断，它就同时负责两件事，而 Plan 期多出一条没要过的事务正是
   这个模式要杜绝的。
2. **不假装听懂**：Plan 期只回「记下了」，**不**给方案。08:01 那次它满口
   答应「我去查」，执行时才被拒——说清边界比假装懂重要。

## 命名：踩了两个坑，都写在这里

- **不叫 ``/plan``**：那个名字已经是「按命中信号给排期参考」（``/plan <id>``）。
  撞名的症状**极难查**——不报「未知命令」、也不建任何东西，只回一句
  「用法：/plan <id>」。
- **方法名不叫 ``_cmd_plan``**：本文件下方**已有**同名方法（拆步骤那个）。
  Python 类体里**后定义覆盖先定义**，于是 ``self._cmd_plan`` 解析到了**它** ——
  Plan 入口静默接到「拆步骤」上。改名 ``_cmd_mode_plan`` / ``_cmd_mode_build``
  才断开。**静默的错绑比报错难查得多。**

## 测试要盯住的那条

``test_plan_mode_creates_nothing`` 断言的是**库里一条都没有**（``tasks.list()``
为空），而不是「回话说得好听」。理由：这一刀的全部价值就是零副作用，而
「说没做」和「真的没做」在输出上无法区分。
"""

import io

import pytest

from freeagent.app import build_app
from freeagent.cli.app import MODE_BUILD, MODE_PLAN, Repl


@pytest.fixture()
def repl(tmp_path):
    app = build_app(tmp_path / "a.db")
    r = Repl(app, out=io.StringIO())
    yield r, app
    app.close()


def _run(repl: Repl, *lines: str) -> str:
    buf = repl.out
    buf.truncate(0)
    buf.seek(0)
    for line in lines:
        repl.handle(line)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 零副作用 —— 这一刀的全部价值
# --------------------------------------------------------------------------- #
def test_plan_mode_creates_nothing(repl):
    """Plan 期说了三句，**库里一条都不能有**。

    断言的是 ``tasks.list()`` 为空，而不是「回话说得好听」——「说没做」和
    「真的没做」在输出上分不出来。
    """
    r, app = repl
    app.roles.create("工作")

    _run(r,
         "/mode-plan",
         "我要让 OpenMOS 那边加上密码定期提醒",
         "先确认这个功能有没有",
         "顺便看看提醒怎么发")

    assert app.tasks.list_all() == [], "Plan 期建了事务 —— 零副作用被破了"
    assert len(r._plan) == 3


def test_plan_mode_asks_no_role_question(repl):
    """Plan 期**不该触发角色追问**——那是「记事」正路才做的事。

    这条守住第1 条不变量的**另一半**：零副作用不只是「没建事务」，也包括
    「没把用户拖进一条与 Plan 无关的追问流」。
    """
    r, app = repl
    app.roles.create("工作")
    _run(r, "/mode-plan", "随便说点什么")
    assert r._pending is None, "Plan 期不该发起角色追问"


# --------------------------------------------------------------------------- #
# 模式可见 + 边界说清
# --------------------------------------------------------------------------- #
def test_entering_plan_says_it_writes_nothing(repl):
    r, _app = repl
    out = _run(r, "/mode-plan")
    assert "不落库" in out, "必须明说 Plan 期不落库"
    assert r._mode == MODE_PLAN


def test_mode_build_requests_confirmation_but_executes_nothing(repl):
    """``/mode-build`` 必须**发确认卡、且一个字都还没执行**。

    ## 契约变了（2026-10-05）

    旧契约：``/mode-build`` 立刻切 ``build`` 并清空计划。
    新契约：**确认之前保持 plan、不清空**。

    理由是「再想想」这个退路：提前切走的话，用户点了「取消」就回不来了 ——
    而那正是确认卡必须提供的第三个选择。

    「必须明说没有执行任何东西」这条**没变**，而且更重要了：确认卡已经发出，
    若这时说得像执行过了，用户会以为委派已经发起。
    """
    r, app = repl
    _run(r, "/mode-plan", "第一件事", "第二件事")
    out = _run(r, "/mode-build")

    assert r._mode == MODE_PLAN, (
        "确认前不该切模式 —— 否则「取消」回不来"
    )
    assert r._last_plan_confirm == ("第一件事", "第二件事"), \
        f"应当存下待确认的快照，实际 {r._last_plan_confirm!r}"
    assert "还没有执行任何东西" in out, (
        "必须承认尚未执行 —— 否则用户会以为委派已发起"
    )
    assert "第一件事" in out and "第二件事" in out
    assert app.tasks.list_all() == [], "确认之前不该建任何东西"


def test_mode_build_ok_creates_the_tasks(repl):
    """``/mode-build ok`` 才真的建事务，且**走正常路径**（角色要有归属）。"""
    r, app = repl
    _run(r, "/mode-plan", "第一件事", "第二件事")
    # **必须先发卡**（`/mode-build` 无参）才能 `ok`。
    # 参数也要在**同一条命令里**：`_run(r, *lines)` 是逐条 handle，写成
    # `_run(r, "/mode-build", "ok")` 会变成两次 handle，而 "ok" 会被
    # Plan 正确地当成自由文本吞掉 —— 那是测试写错，不是实现错。
    _run(r, "/mode-build")
    out = _run(r, "/mode-build ok")

    assert r._mode == MODE_BUILD, "确认后才切模式"
    assert r._plan == [] and r._last_plan_confirm == ()
    titles = {t.title for t in app.tasks.list_all()}
    assert len(titles) >= 1, "确认后至少该建一条事务"
    assert "未分类" not in titles, \
        "不能掉进「未分类」—— 那说明没走正常路径的角色推断"


def test_mode_build_cancel_keeps_the_plan(repl):
    """``/mode-build cancel`` **保留计划** —— 他可能只是想再想想。"""
    r, app = repl
    _run(r, "/mode-plan", "第一件事")
    out = _run(r, "/mode-build cancel")

    assert r._mode == MODE_PLAN
    assert r._plan == ["第一件事"], "取消不该丢掉计划"
    assert r._last_plan_confirm == (), "但待确认状态要清掉"
    assert app.tasks.list_all() == [], "取消不该建任何东西"
    assert "留着" in out


def test_plan_is_frozen_while_the_card_is_out(repl):
    """确认卡挂着时**不接受新内容** —— 计划被冻结。

    这条**取代**了原来那条「计划变了就拒绝确认」：现在计划根本改不了，
    所以那个场景不再能发生。留着旧测试只会让人以为还能在挂卡时补计划。

    为什么必须冻结：卡上印的是发出时的计划。若这时追加，卡上印的和实际会
    执行的对不上 —— 而用户的回复恰恰是「记下了」，**像是成功了**。
    """
    r, app = repl
    _run(r, "/mode-plan", "第一件事")
    _run(r, "/mode-build")                     # 发出确认卡（冻结）
    out = _run(r, "第二件事")                   # 挂卡时说的话

    assert r._plan == ["第一件事"], "挂卡时不该接受追加"
    assert "没有" in out and "加进去" in out, (
        f"要说清为什么没记进去：{out!r}"
    )
    assert app.tasks.list_all() == [], "Plan 期仍不该建事务"


def test_confirm_without_a_card_is_refused(repl):
    """**没发过卡就不许确认** —— 不许执行一个从没展示过的东西。

    这条是上面那个「测试漏了一步」暴露出来的真保护：若 `/mode-build ok`
    能直接执行，那么「确认」就退化成一句口号，用户从没机会核对计划。
    确认卡的全部意义就是**他核对过**，所以快照必须先由发卡那一步产生。
    """
    r, app = repl
    _run(r, "/mode-plan", "第一件事")
    out = _run(r, "/mode-build ok")

    assert app.tasks.list_all() == [], "没发卡就确认 = 批准了没展示过的东西"
    assert "/mode-build" in out, f"要告诉他怎么走：{out!r}"
