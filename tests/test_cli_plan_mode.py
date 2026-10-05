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


def test_exit_lists_the_plan_and_admits_no_execution(repl):
    """``/mode-build`` **必须明说没有执行任何东西**。

    因为确认卡还没实现。如果它说得像执行过了，用户会以为委派已经发起——
    而这正是本项目反复吃的亏（状态文件假绿灯、``known_names`` 死代码、
    「点一下就行」）。**说清边界比看起来完整重要。**
    """
    r, app = repl
    _run(r, "/mode-plan", "第一件事", "第二件事")
    out = _run(r, "/mode-build")

    assert r._mode == MODE_BUILD
    assert "还没有执行任何东西" in out, (
        "必须承认尚未执行 —— 否则用户会以为委派已发起"
    )
    assert "第一件事" in out and "第二件事" in out
    assert app.tasks.list_all() == [], "build 也还没实现，不该建任何东西"


def test_exit_with_no_plan_says_already_build(repl):
    r, _app = repl
    out = _run(r, "/mode-build")
    assert "已经在 build" in out, "空计划时要说清，而不是默默切模式"


# --------------------------------------------------------------------------- #
# 撞名防护
# --------------------------------------------------------------------------- #
def test_plan_command_is_still_the_scheduling_one(repl):
    """``/plan`` **必须仍是「排期参考」**，被我错绑过。

    当时把模式入口写成 ``/plan``，Python 类体里后定义覆盖先定义，于是
    ``self._cmd_plan`` 解析到了拆步骤那个——症状是 ``/plan <id>`` 什么都不做，
    而 ``/plan`` 也不进模式。
    """
    r, app = repl
    task_id = app.tasks.create("一件事", [app.roles.create("工作").id]).id
    out = _run(r, f"/plan {task_id}")
    assert "用法" not in out, "/plan 被模式入口抢走了"
    assert r._mode == MODE_BUILD, "/plan 不该切模式"


def test_slash_still_works_inside_plan(repl):
    """Plan 里的 ``/`` 仍走命令——Plan 管的是**自由文本**。

    否则用户问着问着想 ``/today`` 看一眼都得先退出 Plan，那不叫规划。
    """
    r, app = repl
    _run(r, "/mode-plan", "先记着")
    _run(r, "/today")
    assert r._mode == MODE_PLAN, "/today 不该把模式切走"
    assert r._plan == ["先记着"], "/today 不该被当成计划内容"


# --------------------------------------------------------------------------- #
# 独立性：同型 bug 今日已犯三次，这里必须有测试
# --------------------------------------------------------------------------- #
def test_mode_handlers_are_distinct_methods(repl):
    """Plan 与 build 的处理器**必须是两个不同的方法**。

    这是``_cmd_plan`` 覆盖事件的直接防线：若它们变成同一个名字，Python 会
    静默让后者生效，而 ``/plan`` 的原功能当场消失。
    """
    r, _app = repl
    assert r._cmd_mode_plan is not r._cmd_mode_build
    assert r._cmd_mode_plan.__name__ != r._cmd_mode_build.__name__