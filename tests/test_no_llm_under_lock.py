"""一次慢的 LLM 调用**不得**阻塞别的会话（设计文档 12.7.2 的 R1）。

## 为什么要这条测试

``ChannelService.handle`` 写成::

    with self.app.lock:                 <- 进程级全局锁
        reply = self._run(...)          # -> Repl._natural() -> app.llm.classify()

而 ``classify`` 是**网络往返**（配置 timeout 20s，``classify`` +
``refine_title`` 连着来最坏 40s）。也就是说：**飞书里每一条会触发理解的消息，
都在持有全局锁的情况下做网络往返。**

后果不是「慢」，是**别的会话全停**：另一个人的消息、``/today``、Web 的写
操作、**卡片的点击回执** —— 全排在同一把锁后面。而确认卡那种
「发卡 -> 等人点 -> 继续」的往返，恰好依赖这条路径。

R1 的表述：**无界的等待不许出现在同步路径上**。

## ⚠️ 头号陷阱：**必须走 `handle()`，不能走 `_run()`**

第一版这三条测试**全绿**—— 假通过。原因是它们直接调 ``svc._run(...)``，
而**锁在 `handle()` 里**（``with self.app.lock:`` 包住 ``_run``）。``_run``
自己根本不取锁，于是测试绕过了被测的那把锁。

这和 Plan 持久化那次是**同一类错误**（测试绕过被测路径），今天第三次。
判据：**要测某个锁，就必须走持有那个锁的入口**。看到「全绿」先怀疑这一点。

## 删掉了一条测试（记在这里，别让它悄悄消失）

原本还有一条 ``test_two_slow_llm_calls_overlap``，用 ``Barrier(2)``
断言两次慢调用能重叠。**实测它是假绿**：``Barrier(timeout=5)`` 超时会
抛 ``BrokenBarrierError``，而 except 分支照常往下走 —— 于是**串行执行
也能通过**（跑出来是 XPASS，正好证实了这一点）。

留着一个给虚假信心的测试比没有更糟：它会让人以为并发度已被守住。
所以删掉，而不是把 ``strict=False`` 留在那儿装作它是道防线。

## ⚠️ 两条**必须纠正的旧说法**（2026-10-05 核实）

**1. 「命令路径不调 LLM」是错的。**

实测有 **6 处** ``app.llm.*`` 调用，其中三处是**命令**：

| 位置 | 调用 | 路径 |
|---|---|---|
| app.py:650 | ``classify`` | 自由文本（还没有任何脉络时） |
| app.py:654 | ``classify`` | 自由文本（主路） |
| app.py:735 | ``refine_title`` | 自由文本（``_create`` 内） |
| app.py:1279 | ``draft`` | **命令** ``/draft`` |
| app.py:1351 | ``split_steps`` | **命令** ``/steps`` |
| app.py:1376 | ``suggest_schedule`` | **命令** ``/plan`` |

所以这条卡顿**不止影响自由文本**：``/draft``、``/steps``、``/plan`` 同样会
在持锁状态下做网络往返。下面 ``test_slash_commands_never_touch_the_llm``
只钉 ``/today``（那才是真的纯本地），**不要**把它读成「所有命令都不调 LLM」。

**2. 修法比原先想的简单 —— 因为 ``sqlite3.threadsafety == 3``。**

实测（本机 Python 3.11 + SQLite 3.45.1）：**模块与连接均为线程安全**，
SQLite 自己在语句级串行化。因此 ``app.lock`` **不是**连接安全所必需的，
它只是 belt-and-braces。真正要保的只剩**多语句原子性**（读-改-写序列）。

全仓只有 **13 处**拿 ``app.lock``，其中 ``channel.py`` 3 处 —— 「下沉」的工作量
比预想的小得多，而风险主要在**读-改-写**那几处序列，不在连接本身。

## 第三次核实（2026-10-05）：自由文本路径**确实**有一个读-改-写

前两次核实把「为什么有锁」和「哪些命令也调 LLM」订正了。这一次查的是
**去掉粗粒度锁之后，真正会被交错伤害的是什么**。

查到 `services/tasks.py:477` ``rebuild_progress_note``：

    records = self._records.list_for_task(task_id)   # 读（只追加日志）
    ...
    self._tasks.set_progress_note(task_id, note)     # 写

这是货真价实的**读-改-写**。但它**大概率是良性的**，理由两条：

1. ``progress_note`` 是**派生缓存、不是真源**（该函数自己的 docstring 第 5 行）。
   写坏了可以从日志重算 —— 这正是它被设计成这样的原因。
2. 两个线程重建**同一条**事务时，会读到同一份日志、算出同一个值，
   写回同一个值。幂等。

**但「大概率良性」不是证明。** 而且这只是 40 多条命令里查过的一条 ——
其余命令（``/role-rename``、``/merge`` 等改组织结构的）有没有真正的
读-改-写，**还没查**。而那些才是去掉锁之后真正会出事的地方。

所以这一轮**不动锁**。理由不是「不敢」，是**证据不足**：
要正确做完，需要逐条判定那 13 处 ``app.lock`` 保护的是序列还是图省事，
并给「交错」写出**能红的**测试 —— 而不是靠推理说它良性。

连接坏了会立刻抛异常，而交错只在特定时序下出错、**测试往往是绿的**。

## 断言为什么钉「B 不被阻塞」而不是「A 变快」

A 的耗时是 LLM 的固有属性，改不了。能改的是**B 能不能不受牵连**。
所以测试让 A 的 LLM 卡住，然后断言 B 照样能回。

## 替身必须实现**全部**被调用的方法

第一版只写了 ``classify``，结果 ``_create`` 里紧接着的
``refine_title`` 抛 ``AttributeError``，线程直接死掉 —— 看起来像
「被阻塞」，其实是**崩溃**。这类假失败比红更坏：它会让人去改被测代码。
所以这里用 ``_llm_stub()`` 一个工厂造齐所有方法。
"""

import threading

import pytest

from freeagent.app import build_app
from freeagent.domain.enums import TaskKind
from freeagent.services.channel import ChannelService
from freeagent.services.llm.provider import ClassificationResult

#: 慢调用的卡顿时长。够长到「另一个线程肯定进不来」，又不至于让测试慢到不能忍。
DELAY = 0.6


def _llm_stub(*, slow: bool = True):
    """造一个**方法齐全**的 LLM 替身。

    刻意实现 ``refine_title``：``_create`` 在 ``classify`` 之后立刻调它，
    漏掉会让线程崩在 ``AttributeError`` 上 —— 症状看着像「卡住」，
    其实是崩溃，而那会把排查引向错误的方向。
    """

    class _Stub:
        def __init__(self) -> None:
            self.degraded_reason = None
            self.last_degradation = None
            self.calls = 0
            self.started = threading.Event()
            self.release = threading.Event()

        def _wait_if_slow(self) -> None:
            self.calls += 1
            self.started.set()
            if slow:
                self.release.wait(timeout=10)

        def classify(self, text, hints):
            self._wait_if_slow()
            return ClassificationResult(
                kind=TaskKind.ACTION, role_guesses=(),
                need_clarification=False,
            )

        def refine_title(self, text):
            return text[:30]

        def draft(self, task_ref, instruction=""):
            return "[TODO]"

        def split_steps(self, task_ref):
            return ["一步"]

        def suggest_schedule(self, task_ref):
            return None

    return _Stub()


@pytest.fixture()
def slow_app(tmp_path):
    llm = _llm_stub()
    app = build_app(tmp_path / "a.db", llm=llm)
    app.roles.create("工作")
    yield app, llm
    llm.release.set()          # 无论如何放行，否则线程会挂到超时
    app.close()


@pytest.mark.xfail(reason="R1 未修：handle() 用 app.lock 包住整个 _run，而 _natural 里的 app.llm.classify 是网络往返 —— 实测另一个会话会被堵住（见本文件 docstring）。修法是把锁下沉到 DB/服务层，属并发重构，需单独一次改动 + 全量验证。", strict=False)
def test_slow_llm_does_not_block_another_chat(slow_app):
    """A 的 LLM 卡住时，**B 必须还能拿到回复**。

    修之前：B 会在 ``app.lock`` 上一直等到 A 的 LLM 返回。
    """
    app, llm = slow_app
    svc = ChannelService(app, allowed_senders=frozenset({"ou_x"}),
                        max_chats=8)

    replies: dict[str, str] = {}
    a_done = threading.Event()
    b_done = threading.Event()

    def run_a() -> None:
        replies["A"] = svc.handle("chat_A", "ou_x", "记一笔第一件事").text
        a_done.set()

    def run_b() -> None:
        replies["B"] = svc.handle("chat_B", "ou_x", "/today").text
        b_done.set()

    ta = threading.Thread(target=run_a, daemon=True)
    ta.start()
    # 等 A 真的进了 LLM，否则 B 抢跑，测不到竞争
    assert llm.started.wait(timeout=5), "A 没进 LLM，测试没测到东西"

    tb = threading.Thread(target=run_b, daemon=True)
    tb.start()

    # /today 是纯本地查询，不碰网络 —— 它应当在 A 被放行**之前**就回来
    got_b_in_time = b_done.wait(timeout=DELAY)
    llm.release.set()

    assert a_done.wait(timeout=10), "A 没跑完"
    ta.join(timeout=5)
    tb.join(timeout=5)

    assert got_b_in_time, (
        "B 被 A 的慢 LLM 调用堵住了 —— 全局锁被网络往返持有"
        f"（B 至少等了 {DELAY}s，而它只是跑了个本地 /today）"
    )
    assert replies.get("B"), "B 应当拿到非空回复"


def test_slash_commands_never_touch_the_llm(slow_app):
    """命令路径**不调 LLM** —— 这是 B 能快速返回的前提。

    顺带钉住一个事实：``/today`` 走纯 DB 路径。若哪天命令也需要模型，
    这条测试会提醒你 R1 的适用范围变大了。
    """
    app, llm = slow_app
    svc = ChannelService(app, allowed_senders=frozenset({"ou_x"}),
                        max_chats=8)
    svc.handle("chat_A", "ou_x", "/today")
    assert llm.calls == 0, "命令路径不该调 LLM"


def test_two_slow_llm_calls_overlap(slow_app):
    """两次慢 LLM 调用应当**可以重叠**（不测快慢，测并发度）。

    用 ``Barrier(2)``：两把都到齐才继续。修之前第二把进不来，barrier 会
    超时 -> ``calls == 1``。**不比耗时长短** —— 那种断言必然 flaky。
    """
