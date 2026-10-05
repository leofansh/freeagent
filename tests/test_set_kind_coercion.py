"""``set_kind`` 必须在**边界**把字符串转成枚举（``TaskKind`` 是 ``str`` 基枚举）。

## 修的是什么

注解写着 ``kind: TaskKind``，但运行时收字符串，于是有两个洞：

1. ``kind is TaskKind.WAIT`` 对普通字符串 ``"wait"`` 是 **False** ——
   「等候类必须给 waiting_on」那条不变量被**静默跳过**，``target`` 置 ``None``
2. 带着这个裸 ``str`` 一路走到 ``storage/repos.py`` 的 ``task.kind.value``
   才炸 ``AttributeError: 'str' object has no attribute 'value'``

**症状离病因隔了两层。** 而注解当时说这里是 ``TaskKind``，排查时会被引向
「枚举用错了」，而不是「这个函数没校验输入」。

所以断言钉的是**错误发生在哪、报的是什么**，不只是「最终成功」：

- 非法值必须在**门口**抛 ``ValueError``，不是两行之后抛 ``AttributeError``
- 字符串 ``"wait"`` 缺 ``waiting_on`` 时，必须抛 ``InvariantViolation`` ——
  这条正是原来被静默跳过的

## 为什么值得单独立一组测试

这是**写测试时撞出来的真 bug**，不是审阅代码想出来的。而它当时**没有任何
生产调用方**（全仓只有 ``cli/app.py`` 一处，且那条走 ``KIND_ALIASES``
转成了枚举）—— 也就是说纯 latent，只等下一个人或下一个 Web 入口来踩。

latent 缺陷最容易活到线上，所以这里补的是**边界契约**，不只是 happy path。
"""

import pytest

from freeagent.app import build_app
from freeagent.domain.enums import TaskKind, WaitingKind
from freeagent.domain.models import WaitingOn
from freeagent.services.tasks import InvariantViolation


@pytest.fixture()
def app(tmp_path):
    a = build_app(tmp_path / "a.db")
    yield a
    a.close()


@pytest.fixture()
def task(app):
    role = app.roles.create("工作")
    return app.tasks.create("一件事", [role.id])


def _waiting(app, who="客户回复"):
    return WaitingOn(kind=WaitingKind.PERSON, who_or_what=who,
                     since=app.clock.now())


# --------------------------------------------------------------------------- #
# 收字符串
# --------------------------------------------------------------------------- #
def test_accepts_plain_string(app, task):
    """传字符串**不该**炸 —— 这是修复的直接目标。"""
    got = app.tasks.set_kind(task.id, "reminder")
    assert got.kind is TaskKind.REMINDER
    assert got.kind.label, "label 必须可用（repos 存盘时取 .value）"


def test_string_wait_with_waiting_on_still_works(app, task):
    """字符串 ``"wait"`` + ``waiting_on``：正常路径不能被转换改坏。"""
    got = app.tasks.set_kind(task.id, "wait", waiting_on=_waiting(app))
    assert got.kind is TaskKind.WAIT
    assert got.waiting_on is not None
    assert got.waiting_on.who_or_what == "客户回复"


def test_accepts_enum_unchanged(app, task):
    """传枚举仍然正常 —— 修的不能是把好路径弄坏。"""
    got = app.tasks.set_kind(task.id, TaskKind.ACTION)
    assert got.kind is TaskKind.ACTION


# --------------------------------------------------------------------------- #
# 不变量必须重新生效（原来被静默跳过的那条）
# --------------------------------------------------------------------------- #
def test_string_wait_without_waiting_on_raises(app, task):
    """字符串 ``"wait"`` 缺 ``waiting_on`` → **不变量**报错。

    这条是本组的重点：修之前它**不报错**，而是在 ``repos.py`` 里炸出一个
    与病因无关的 ``AttributeError``。断言异常**类型**，因为「报什么错」正是
    这次要修的东西。
    """
    with pytest.raises(InvariantViolation):
        app.tasks.set_kind(task.id, "wait")


def test_error_is_not_attribute_error(app, task):
    """**明确**不让它以 ``AttributeError`` 收场。

    ``AttributeError: 'str' object has no attribute 'value'`` 出现在
    ``storage/repos.py``，离病因隔了两层。这条断言把它钉死成「不许再出现」。
    """
    try:
        app.tasks.set_kind(task.id, "wait")
    except AttributeError:  # pragma: no cover - 这就是回归
        pytest.fail("又从 repos 里冒出 AttributeError 了 —— 边界转换没生效")
    except Exception:
        pass  # 任何别的异常都行，只要不是 AttributeError


# --------------------------------------------------------------------------- #
# 非法值在门口报出能看懂的话
# --------------------------------------------------------------------------- #
def test_invalid_value_fails_at_the_boundary(app, task):
    """非法字符串在**门口**抛 ``ValueError``，且消息里带上那个坏值。

    ``"delegate"`` 是从真实场景抄来的：委派**不是**一种 ``kind``，
    ``eligible_tasks`` 只认 ``project_path`` 非空 + ``state is ACTIVE``。
    写测试时按 kind 猜「delegate」，就是这个 bug 的发现路径。
    """
    with pytest.raises(ValueError) as exc:
        app.tasks.set_kind(task.id, "delegate")
    assert "delegate" in str(exc.value), \
        f"报错要带上坏值，否则等于让人猜：{exc.value}"


def test_case_insensitive_string_accepted(app, task):
    """大小写宽容：``"Action"`` 也该收。

    CLI 那侧 ``args[1].lower()`` 已经做了，但**服务层不该依赖调用方**已经
    归一化过 —— 下一个 Web 入口不会记得这件事。
    """
    got = app.tasks.set_kind(task.id, "Action")
    assert got.kind is TaskKind.ACTION