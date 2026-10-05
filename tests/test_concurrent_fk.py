"""并发建事务**不许**炸外键 —— 锁必须覆盖整个写入序列（设计文档 12.7.2 的 R1）。

## 这条测试存在的理由：它抓到了一个真 bug

我曾把 :class:`~freeagent.services.channel.ChannelService` 的**进程级**锁换成
**按会话**锁，理由是「自由文本路径没有读-改-写，而 ``sqlite3.threadsafety == 3``」。

并发一跑就炸：

    sqlite3.IntegrityError: FOREIGN KEY constraint failed

## 根因：语句级串行化 ≠ 序列原子

``TaskService.create`` 是**三条语句**：

    INSERT tasks  →  INSERT task_roles  →  INSERT task_records

``threadsafety == 3`` 只保证**语句级**串行。两个线程交错时，一个线程可能在自己的
``task_roles`` 插入那一刻，另一个线程已推进到下一条 —— 于是外键找不到父行。

所以「有没有读-改-写」不是该问的问题；该问的是「**有没有多语句序列**」，
而**建事务这件事本身就是**。

## 第五次「测试绕过被测路径」—— 而这次它反证了一件事

第一版用 ``service._run(...)`` 直接调内部方法，绕过了 ``handle()`` ——
而**全局锁就在 ``handle()`` 里**。并发一跑就炸：

    sqlite3.OperationalError: cannot commit - no transaction is active
    sqlite3.DatabaseError: no more rows available

看着像生产代码有并发 bug，其实**是测试绕过了承重的那把锁**。

而它反过来证明了一件有价值的事：**``_run`` 单独调用是不安全的**，
那把进程级锁真的在承重 —— 这正是「为什么 R1 不能简单地把锁删掉」的答案。

今天同一类错误已经犯到第五次，判据始终是同一条：

> **要测某个不变量，就必须走持有它的那个入口。**

## 写这条测试时踩的坑：造了 0 条事务

第一版用「第0件事要做」当文本，跑出来 **0/6 条** —— 看着像并发把它全弄丢了，
其实是规则层对**不含角色名**的句子会发起**追问**（``clarify=True``），
于是什么都没建。实测：

    '第0件事要做'      -> clarify=True  guesses=[]
    '工作的第0件事'    -> clarify=False guesses=['工作']

**追问把写入路径整个短路了**，这条测试等于没测。而那个 0 长得极像
「并发把数据弄丢了」—— 差一点就去查并发。

## 因此这条测试盯的是**正确性**，不是性能

R1 要解决的是「A 在思考时堵住 B」，那半边是性能。
这一半是**数据完整性**：锁一旦没覆盖住写入序列，表现不是变慢，是**数据坏了**，
而且往往不报错。所以它比性能那半边更不能丢。
"""

import io
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.services.channel import ChannelService  # noqa: E402

WHO = "ou_x"
N_THREADS = 6


@pytest.fixture()
def svc(tmp_path):
    app = build_app(tmp_path / "a.db")
    app.roles.create("工作")
    service = ChannelService(
        app, allowed_senders=frozenset({WHO}), max_chats=16)
    yield service, app
    app.close()


def test_concurrent_task_creation_keeps_fk_intact(svc):
    """N 个会话**同时**建事务 —— 一条都不许因外键失败而丢。

    断言的是「条数 == 线程数」：少一条就说明有一次写入被打断在半路。
    """
    service, app = svc
    errors: list[BaseException] = []

    def make(i: int) -> None:
        try:
            service.handle(f"chat_{i}", WHO, f"工作的第{i}件事要做")
        except BaseException as exc:  # noqa: BLE001 - 收集起来统一断言
            errors.append(exc)

    threads = [threading.Thread(target=make, args=(i,), daemon=True)
               for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, (
        "并发建事务时抛了异常：\n  "
        + "\n  ".join(f"{type(e).__name__}: {e}" for e in errors[:4])
    )
    assert len(app.tasks.list_all()) == N_THREADS, (
        f"只建成 {len(app.tasks.list_all())}/{N_THREADS} 条 —— "
        "有写入被打断在半路（外键序列被交错）"
    )


def test_same_task_gets_complete_role_links(svc):
    """并发之后每条事务的**角色关联**都必须在 —— 不许有孤儿。"""
    service, app = svc

    def make(i: int) -> None:
        service.handle(f"chat_{i}", WHO, f"工作的第{i}件事")

    threads = [threading.Thread(target=make, args=(i,), daemon=True)
               for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    role_ids = {r.id for r in app.roles.list_roles()}
    for task in app.tasks.list_all():
        assert task.role_ids, f"{task.id[:8]} 没有任何角色关联 —— 序列被打断"
        assert set(task.role_ids) <= role_ids, "角色关联指向了不存在的角色"