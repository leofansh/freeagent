"""测试自身的纪律（V1.106）。

**为什么单独一个文件**：本仓的守卫分两类，``test_doc_contract.py`` 管「文档与
实现是否一致」，其余测试管「行为对不对」。**没有一类管「测试本身是否在说谎」**
——而那是最难发现的一种：它不报红。

V1.106 从 openwork（``evals/scripts/spec-lint/lint.ts`` 的 ``honest`` 规则）
借来的就是这一层。它原话是：

    no skips, sleeps or self-graded evidence, and every test asserts
    something a person or client observes.

最后半句**本仓不查**（理由见文件末尾「刻意不做」），前三条各一条测试。

判据全部是**可观察事实**（AST 里的调用节点、字面量），不看措辞、不猜意图：
一旦靠正则判「理由写得够不够好」，这层守卫自己就会变成噪音源，然后被
白名单绕过——那正是 ``postmortem/lenses.md``「漏判代价远小于错判」说的。
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: 允许出现的**真实** ``time.sleep`` 调用数（文件 → (上限, 这些等待是什么性质)）。
#:
#: 刻意用 AST 计数而不是文本 grep：``test_feishu.py`` 里有一处
#: ``"time.sleep(30)"`` 是**载荷字符串**（被发给被测程序的命令），grep 会
#: 把它也算成一次等待，于是基线从第一版起就是错的。
#:
#: 数字是**上限不是背书**：加一条就必须来改这张表，改表那一刻就是 review 点。
#: 收紧不用改（留的空位不构成问题），所以删一条时不必回头调数字。
SLEEP_BUDGET: dict[str, tuple[int, str]] = {
    "test_delegate_gate.py": (
        1,
        "线程里轮询 sender.sent，0.01 只是节流 —— 它在等一个**条件**，不是在等时间",
    ),
    "test_feishu.py": (
        8,
        "飞书 bridge 是阻塞 subprocess 循环，测的是真线程的停启与退避；"
        "FrozenClock 改不掉线程调度，只能真等",
    ),
    "test_feishu_status.py": (
        1,
        "等一个心跳周期（interval=0.02 却睡 0.15）。这条**本该改成轮询** read_status，"
        "登记不等于认可",
    ),
    "test_prompt_payload.py": (
        2,
        "等真模型回写 variant（sleep 1.5）。**这两条是待改**：应轮询 "
        "_read_back_variant 直到超时，而不是赌 1.5 秒够用",
    ),
    "test_web_session.py": (
        1,
        "连接重试的退避等待 —— 此刻服务端还没开始监听，没有可观察状态可等",
    ),
}

#: 认得出的「占位理由」：写了等于没写。
_PLACEHOLDER_REASONS = {"todo", "skip", "暂时跳过", "有问题", "以后再补", "待定"}


def _calls(path: Path):
    """逐个给出 ``(行号, 点分调用名, Call 节点)``。"""
    for node in ast.walk(_parsed(path)):
        if isinstance(node, ast.Call):
            yield node.lineno, ast.unparse(node.func), node


def _py_files():
    """**pytest 真会收集的文件**。

    刻意不是「tests 下所有 .py」：``tests/manual/e2e_browser_smoke.py`` 是
    手工脚本，不进收集、不算验证，它里面那一处 ``time.sleep(0.5)`` 是在轮询
    页面状态（``break`` 出来），正当用法。把它算进来只会逼人登记一份
    「不管的东西为什么不管」——那种表最后没人看。
    """
    for path in sorted(TESTS.rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path
    yield TESTS / "conftest.py"


@lru_cache(maxsize=None)
def _parsed(path: Path) -> ast.Module:
    """解析结果按文件缓存：四条规则各扫一遍全仓，不缓存就是 4 倍重复解析。"""
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _reason_of(call: ast.Call) -> str | None:
    """取 ``reason=`` 的字面量。**变量不算** —— 理由必须写在跳过点旁边。"""
    for keyword in call.keywords:
        if keyword.arg != "reason":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return None
    return None


def test_跳过的理由必须写在跳过点旁边():
    """``pytest.mark.skipif`` 必须有**字面量** ``reason``。

    为什么要求字面量而不只是「有个 reason」：理由藏进常量或拼接字符串之后，
    读代码的人在 ``@needs_node`` 这一行看不到「缺什么」，只能再跳一次。
    而跳过的全部代价就在这里 —— 半年后没人知道这条为什么不跑。

    另一个容易漏的点：本仓四处 ``skipif`` 的条件都是「这台机器上有没有装
    node / opencode」。这类跳过**不是**「不需要验证」，而是「这次没验证」，
    两者的差别正是 :func:`tests.conftest.pytest_terminal_summary` 要打印出来的。
    """
    offenders: list[str] = []
    for path in _py_files():
        for lineno, dotted, call in _calls(path):
            if dotted != "pytest.mark.skipif":
                continue
            reason = _reason_of(call)
            where = f"{path.relative_to(TESTS.parent)}:{lineno}"
            if reason is None:
                offenders.append(
                    f"{where}: reason 不是字面量。跳过点旁边要能直接读出「缺什么」，"
                    "别把它藏进变量或拼接字符串。"
                )
            elif len(reason.strip()) < 4 or reason.strip().lower() in _PLACEHOLDER_REASONS:
                offenders.append(
                    f"{where}: reason 是 {reason!r}，等于没写。"
                    "改成「缺什么 + 补上什么就能跑」，例如 "
                    "'没装 node，跳过 JS 语法检查（npm i -g node 后自动生效）'。"
                )
    assert not offenders, (
        "跳过必须说明缺什么，否则它和「通过」在输出里长得一样：\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_没有无条件跳过():
    """禁 ``pytest.mark.skip`` / ``xfail``（无条件的、永远不跑的两种写法）。

    ``skipif`` 至少有条件可查；无条件的 ``skip`` 是「这条永远不跑」，而
    ``xfail`` 是「预期会失败」——两者在终端里都算非红，于是**可以长期挂着
    没人管**。要临时停用一条测试，正确做法是删掉它并说明为什么（diff 里
    看得见），或者让它红。

    **扫 ``ast.Attribute`` 而不是 ``ast.Call``**（反向验证时踩到的）：
    ``@pytest.mark.skip`` 不带括号时 AST 里**根本没有 Call 节点**，它就是个
    Attribute。第一版按 Call 扫，探针里那条裸装饰器被漏判 —— 而裸写正是
    最常见的写法。教训是通用的：判「某个东西被用了」要按它在 AST 里的**节点
    类型**写，不能按「看起来像一次调用」写。
    """
    offenders: list[str] = []
    for path in _py_files():
        seen: set[tuple[int, str]] = set()
        for node in ast.walk(_parsed(path)):
            if not isinstance(node, ast.Attribute):
                continue
            dotted = ast.unparse(node)
            if dotted in ("pytest.mark.skip", "pytest.mark.xfail") and (
                node.lineno,
                dotted,
            ) not in seen:
                seen.add((node.lineno, dotted))
                offenders.append(
                    f"{path.relative_to(TESTS.parent)}:{node.lineno}: {dotted}"
                )
    assert not offenders, (
        "不许用无条件的 skip / xfail —— 它们让「不验证」看起来像「没问题」：\n"
        + "\n".join(f"  {o}" for o in offenders)
        + "\n要么删掉这条测试并说明理由，要么改成带条件的 skipif 并写明缺什么。"
    )


def test_不给自己打分():
    """禁 ``assert True`` 一类**恒真断言**。

    恒真断言是「这条测试跑过了」和「这条验证了点什么」之间唯一的差别，
    而它在输出里是绿的。真需要「只验证不抛异常」时，写在函数名与
    docstring 里（本仓 ``test_release_is_idempotent`` 一类就是如此），
    不要写一行假的 assert 充数。
    """
    offenders: list[str] = []
    for path in _py_files():
        for node in ast.walk(_parsed(path)):
            if not isinstance(node, ast.Assert):
                continue
            where = f"{path.relative_to(TESTS.parent)}:{node.lineno}"
            test = node.test
            if isinstance(test, ast.Constant) and bool(test.value):
                offenders.append(f"{where}: assert {ast.unparse(test)}")
            elif (
                isinstance(test, ast.Compare)
                and all(isinstance(c, ast.Constant) for c in [test.left, *test.comparators])
            ):
                # ``assert 1 == 1``：两边都是字面量，结果与程序行为无关。
                offenders.append(f"{where}: assert {ast.unparse(test)}")
    assert not offenders, (
        "恒真断言让「跑过了」冒充「验证过了」：\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_等待不超过登记的次数():
    """每个测试文件里 ``time.sleep`` 的**真实调用**数不得超过登记上限。

    **不是禁 sleep**：本仓有正当用法（线程里轮询条件时的节流、等一个没有
    可观察状态的连接重试）。要治的是另一种 —— 用「睡够大概就行了」代替
    「等到状态出现」，它的失败是随机红，绿了也说不清在等什么。

    所以规则是「每条都必须有一句为什么」（:data:`SLEEP_BUDGET` 的第二个值），
    而不是「不许出现」。新增一条就该停止写代码、先回答那个为什么。
    """
    counts: dict[str, int] = {}
    for path in _py_files():
        if path == Path(__file__):
            continue
        n = 0
        for _lineno, dotted, _call in _calls(path):
            if dotted in ("time.sleep", "sleep"):
                n += 1
        if n:
            counts[path.name] = n

    over: list[str] = []
    for name, n in sorted(counts.items()):
        budget = SLEEP_BUDGET.get(name, (0, "未登记"))
        if n > budget[0]:
            over.append(
                f"{name}: 有 {n} 处，登记的是 {budget[0]} 处"
                f"（现有这些的性质：{budget[1]}）"
            )
    assert not over, (
        "多出来的等待没有登记过理由。二选一：\n"
        "  · 改成等可观察状态（条件轮询 / FrozenClock 推进 / probe.eventually 式的重试）；\n"
        "  · 确实只能真等，就在 SLEEP_BUDGET 里提高上限并写清「在等什么、为什么没有状态可等」。\n"
        + "\n".join(f"  {o}" for o in over)
    )


# --------------------------------------------------------------------- 刻意不做
#
# **「每个 test_ 函数必须有 assert」这条不查。**
#
# 抄之前先数了一遍：本仓有 14 个「只调用、断言不抛异常」的测试
# （``test_release_is_idempotent`` / ``test_frozen`` / ``test_stop_is_idempotent``
# 一类）。那是合法语义 —— 幂等与「不崩」本身就是断言对象，写一行
# ``assert True`` 反而更差。而真正的空测试要绕过这条检查，成本只是一行
# ``assert True``（那条由 :func:`test_不给自己打分` 单独堵）。
#
# 也就是说：这条检查会**错判 14 个老实测试**，只换来拦不住伪造的收益。
# 按 ``postmortem/lenses.md``「错判代价远大于漏判」——不做。
#
# 同理不查「测试是否在 mock 里自证」（openwork 的 ``real-product`` 规则）：
# 本仓大量测试是**服务层单元测试**，注入替身是设计而非作弊；而「哪些 rep
# 是自证」要语义判断，正则判不了。要拦的话正确位置在执行期（测试用例自己
# 声明），不是静态扫描。
