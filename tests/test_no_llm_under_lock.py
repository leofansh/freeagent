"""一次慢的 LLM 调用**不得**阻塞别的会话（设计文档 12.7.2 的 R1）。

## 症状

``ChannelService.handle`` 曾经用**进程级**的 ``app.lock`` 包住整个 ``_run``，
而一次飞书往返里含网络等待（``app.py:545`` 的 ``classify``、
``app.py:735`` 的 ``refine_title``；配置 timeout 20s，两次连着来最坏 40s）。

于是 **A 在思考时，B 的 ``/today``、卡片点击回执全排在同一把锁后面**。
实测：B 至少等了 0.6s，而它只是跑了个本地查询。

R1 的表述：**无界的等待不许出现在同步路径上**。

## 修法：锁从「全局」改成「按会话」

不是去掉锁 —— 命令那条路（``/role-rename``、``/merge`` 改的是组织结构）
是读-改-写的高发区，交错会真的把数据搞坏，所以**命令仍走全局锁**。

而自由文本那条路的三个 DB 操作经核实是：

| 操作 | 性质 |
|---|---|
| ``roles.list_roles()`` | 纯读 |
| ``tasks.create()`` | 纯 INSERT（append） |
| ``rebuild_progress_note()`` | 读-改-写，但写的是**派生缓存**且幂等 |

没有真正的读-改-写。加上 ``sqlite3.threadsafety == 3``（SQLite 自己在语句级
串行化），所以那条路只需要**同一会话内**串行，不需要跨会话串行。

于是：**同一会话仍严格串行**（``_pending`` / ``_plan`` / ``_mode`` 因此安全），
**不同会话互不阻塞** —— 而跨会话并发正是我们要的。

追问期间仍走全局锁：那是在接续**上一条**的半截对话，并发插进来会把答案对错号。

## 修法：两道锁各管一段（试错两次才对）

### 第一次尝试：把进程级锁换成按会话锁 —— **失败**

推理是：自由文本那条路没有读-改-写，而 `sqlite3.threadsafety == 3`，
那就该按会话串行、跨会话并发。

实测直接炸：

    sqlite3.IntegrityError: FOREIGN KEY constraint failed

**推理错在两处**：

1. **语句级串行化 ≠ 序列原子。** `TaskService.create` 是**三条语句**
   （`INSERT tasks` → `task_roles` → `task_records`）。两个线程交错时，
   一个线程可能在自己的 `task_roles` 插入那一刻另一个线程已推进到下一条
   —— 于是外键找不到父行。
2. 该问的从来不是「有没有读-改-写」，而是「**有没有多语句序列**」——
   而**建事务这件事本身就是**。

已回滚，并把那次失败留下的守卫留在 `tests/test_concurrent_fk.py`：
将来谁再想动这把锁，立刻会看到为什么不能那么动。

### 第二次尝试（对）：**两道锁各管一段**

| 锁 | 作用域 | 护什么 |
|---|---|---|
| `ChannelService._chat_lock` | **按会话** | Repl 的**内存可变状态**：`_pending` / `_plan` / `_mode` / `_last_items`。同一会话两条消息交错会把答案对错号、把计划搅乱。 |
| `Repl._create` 内的 `app.lock` | **进程级** | **写入序列**的原子性（那三条语句）。 |

外加：命令与追问期仍走**进程级**锁 —— 命令是读-改-写的高发区
（`/role-rename`、`/merge` 改的是组织结构，交错会真的把数据搞坏），
而追问期是在接续上一条的半截对话。命令通常很快，真正慢的是自由文本里
的模型调用。

网络调用（`classify`、`refine_title`）因此**不在任何锁内** ——
那正是 R1 要消掉的等待。

### 代价

`_ensure_role` 自带锁（读-改-写，不加会建出重名角色）；靠 `app.lock` 是
**RLock** 才能与 `_create` 里的嵌套调用共存。

## 断言为什么钉「B 不被阻塞」而不是「A 变快」

钉住 A 的耗时没有意义 —— 慢是 LLM 的固有属性，改不了。能改的是**B 能不能不受
牵连**。所以测试让 A 的 LLM 卡住，然后断言 B 照样能回。用「B 是否被阻塞」
而不是比耗时长短，后者必然 flaky。
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
