"""委派租约：「在跑」是观察到的，不是从日志推出来的（12.11.3 / V1.94）。

这个文件守三件事，顺序就是它们出错的顺序：

1. **判活**（:class:`TestLeaseState`）—— 租约的三种状态，以及读不懂时
   往哪边倒（答案：往「可能还在跑」倒，因为下游之一是「能不能重派」）。
2. **认领**（:class:`TestReclaimDispatched`）—— 派出之后进程没了，这条
   事务必须能被认领掉。修之前它是**永久的死事务**：形状一直是「在跑」，
   ``/redispatch`` 一直说「等它跑完」。
3. **不自动动手**（:class:`TestNoAutoRedispatch`）—— 认领只落一条失败账，
   续不续由人决定。执行器是子进程，父进程死了它可能还在改文件。

时间一律注入（``FrozenClock``），不靠 sleep。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from freeagent.app import build_app
from freeagent.delegate import dispatch_lease, reclaim_abandoned
from freeagent.domain import RecordType, TaskKind, TaskState
from freeagent.services.clock import FrozenClock
from freeagent.services.delegate import (
    LEASE_LOST,
    LEASE_NONE,
    LEASE_RUNNING,
    LOST_MARK,
    already_dispatched,
    attempt_state,
    dispatch_blocker,
    lease_owner,
    lease_state,
)
from freeagent.storage.repos import LeaseRepo

NOW = datetime(2026, 10, 27, 12, 0)


@pytest.fixture()
def db_path(tmp_path):
    """执行器是**独立进程**，它自己开库 —— 认领也要拿同一个路径重开。"""
    return tmp_path / "a.db"


@pytest.fixture()
def app(db_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    clock = FrozenClock(NOW)
    a = build_app(db_path, clock=clock)
    a.clock = clock                      # 拿到同一个对象，方便推进
    yield a
    a.close()


def _delegation(app, title="改个 bug", *, start=True, project="/tmp/p"):
    role = app.roles.create("开发")
    task = app.tasks.create(title, [role.id], kind=TaskKind.ACTION,
                            project_path=project)
    if start:
        # 返回 **start 之后** 的对象：Task 是不可变快照，拿 create 的返回值
        # 去问「能不能派」永远看到 inbox。
        return app.tasks.start(task.id)
    return task


class TestLeaseState:
    def test_no_lease(self):
        assert lease_state(None, NOW) == LEASE_NONE

    def test_running(self):
        lease = {"expires_at": (NOW + timedelta(minutes=10)).isoformat()}
        assert lease_state(lease, NOW) == LEASE_RUNNING

    def test_lost_after_expiry_and_grace(self):
        lease = {
            "expires_at": (NOW - timedelta(minutes=10)).isoformat(),
        }
        assert lease_state(lease, NOW) == LEASE_LOST

    def test_just_expired_is_still_running(self):
        """刚过期那一小段仍判「在跑」—— 给「杀进程 + 收口记录」留余量。

        方向是刻意的：这里判错成「失联」的后果是**放行一次重派**，
        而重派的代价是两个进程改同一个目录。
        """
        lease = {"expires_at": (NOW - timedelta(seconds=5)).isoformat()}
        assert lease_state(lease, NOW) == LEASE_RUNNING

    def test_unreadable_expiry_is_running(self):
        """读不懂就保守 —— 不把「不知道」当成「可以重派」的依据。"""
        assert lease_state({"expires_at": "不是时间"}, NOW) == LEASE_RUNNING

    def test_owner_is_just_a_name(self):
        assert lease_owner(42) == "pid:42"


class TestLeaseRepo:
    def test_acquire_get_release(self, app):
        repo = LeaseRepo(app.conn)
        repo.acquire("t1", owner="pid:1", acquired_at=NOW,
                     expires_at=NOW + timedelta(minutes=5))
        got = repo.get("t1")
        assert got is not None and got["owner"] == "pid:1"
        repo.release("t1")
        assert repo.get("t1") is None

    def test_acquire_overwrites(self, app):
        """同一条事务只允许一个租约：后占的顶掉先占的。

        允许覆盖而不是「已存在就拒绝」，是因为能走到这里说明判定已放行
        （正当理由之一就是上一条租约已过期）。拒绝写入会让重启后的派发
        永远写不进租约 —— 那正是「一次崩溃 = 一条死事务」。
        """
        repo = LeaseRepo(app.conn)
        repo.acquire("t1", owner="pid:1", acquired_at=NOW,
                     expires_at=NOW + timedelta(minutes=1))
        repo.acquire("t1", owner="pid:2", acquired_at=NOW,
                     expires_at=NOW + timedelta(minutes=9))
        assert repo.get("t1")["owner"] == "pid:2"
        assert len(repo.all()) == 1

    def test_release_is_idempotent(self, app):
        LeaseRepo(app.conn).release("不存在")     # 不抛


class TestDispatchLeaseContext:
    def test_held_inside_released_after(self, app):
        task = _delegation(app)
        with dispatch_lease(app, task.id, 60):
            assert LeaseRepo(app.conn).get(task.id) is not None
        assert LeaseRepo(app.conn).get(task.id) is None

    def test_released_on_exception(self, app):
        """**异常也要放掉** —— 否则一次抛错就留下一条假租约，
        而假租约会让这条事务在过期前一直看起来「在跑」。"""
        task = _delegation(app)
        with pytest.raises(RuntimeError):
            with dispatch_lease(app, task.id, 60):
                raise RuntimeError("执行器炸了")
        assert LeaseRepo(app.conn).get(task.id) is None

    def test_expiry_comes_from_the_hard_timeout(self, app):
        task = _delegation(app)
        with dispatch_lease(app, task.id, 120):
            lease = LeaseRepo(app.conn).get(task.id)
        assert lease["expires_at"] == (NOW + timedelta(seconds=120)).isoformat()


class TestReclaimDispatched:
    """派出之后进程没了 —— 这一格以前接不住。"""

    def _stuck(self, app):
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派给 opencode",
            app.clock.now(),
        )
        return task

    def test_shape_says_running_before_reclaim(self, app):
        """先确认「修之前它长什么样」，否则这条测试证明不了任何事。"""
        task = self._stuck(app)
        records = app.record_repo.list_for_task(task.id)
        assert attempt_state(records) == (True, None)
        assert already_dispatched(records) is True

    def test_reclaim_makes_it_dispatchable_again(self, app, db_path):
        task = self._stuck(app)
        app.close()

        notes = reclaim_abandoned(db_path=db_path)
        assert notes and task.id[:8] in notes[0]

        reopened = build_app(db_path, clock=FrozenClock(NOW))
        try:
            records = reopened.record_repo.list_for_task(task.id)
            assert attempt_state(records) == (False, "failed")
            assert already_dispatched(records) is False
            assert LOST_MARK in records[-1].content
        finally:
            reopened.close()

    def test_reclaim_is_idempotent(self, app, db_path):
        """反复启动不该把失败记录刷成瀑布。"""
        self._stuck(app)
        app.close()
        assert reclaim_abandoned(db_path=db_path)
        assert reclaim_abandoned(db_path=db_path) == ()

    def test_finished_delegation_is_not_touched(self, app, db_path):
        """已经收口的不认领 —— 反过来就是「把做完的记成失败」。"""
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        app.record_repo.append(
            task.id, RecordType.DELEGATION_SUCCEEDED, "做完了", app.clock.now()
        )
        app.close()
        assert reclaim_abandoned(db_path=db_path) == ()

    def test_closed_task_is_not_reclaimed(self, app, db_path):
        """已经手工收口的事务不补失败账。

        认领的目的是**让卡住的能继续**，不是记账。有人把它标成 done/dropped，
        说明「到此为止」这个决定已经做过了；再补一条失败只会往统计里掺噪声。
        """
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        app.tasks.set_state(task.id, TaskState.DROPPED)
        app.close()
        assert reclaim_abandoned(db_path=db_path) == ()

    def test_task_without_project_is_not_touched(self, app, db_path):
        role = app.roles.create("开发")
        task = app.tasks.create("普通事务", [role.id], kind=TaskKind.ACTION)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        app.close()
        assert reclaim_abandoned(db_path=db_path) == ()


class TestNoAutoRedispatch:
    def test_reclaim_does_not_dispatch(self, app, db_path):
        """认领**只落账**，不重跑。重派仍是人的动作（11.10.5）。"""
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        app.close()
        reclaim_abandoned(db_path=db_path)
        reopened = build_app(db_path, clock=FrozenClock(NOW))
        try:
            records = reopened.record_repo.list_for_task(task.id)
            types = [r.type for r in records]
            assert types.count(RecordType.DELEGATION_DISPATCHED) == 1, (
                "认领不该追加派发记录 —— 多一条 dispatched 就是「又派了一次」的证据"
            )
            assert types[-1] is RecordType.DELEGATION_FAILED
        finally:
            reopened.close()


class TestRedispatchNeedsLostLease:
    def test_refuses_without_lease(self, app):
        """没有租约 = 「不知道」，按「可能还在跑」处理（与判定同向）。"""
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        with pytest.raises(Exception) as exc:
            app.tasks.reset_delegation(task.id, "试试")
        assert "正在跑" in str(exc.value)

    def test_refuses_with_live_lease(self, app):
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        LeaseRepo(app.conn).acquire(
            task.id, owner="pid:1", acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        with pytest.raises(Exception) as exc:
            app.tasks.reset_delegation(task.id, "试试")
        assert "正在跑" in str(exc.value)

    def test_allows_after_lease_expired(self, app):
        """失联：**先落终态、再放行**。

        只落 RESET 是不够的 —— ``attempt_state`` 刻意不让 RESET 清「在跑」，
        所以放行之后 ``already_dispatched`` 还会继续拒派，用户看到的是
        一句「能重派了」接着一次「没人派」。
        """
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        LeaseRepo(app.conn).acquire(
            task.id, owner="pid:1", acquired_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(hours=1),
        )
        app.tasks.reset_delegation(task.id, "进程没了")

        records = app.record_repo.list_for_task(task.id)
        assert LOST_MARK in records[-2].content, "先写终态"
        assert records[-1].type is RecordType.DELEGATION_RESET, "再放行"
        assert already_dispatched(records) is False


class TestDispatchBlocker:
    """「为什么没派」—— 把一条已经被判过的结论说出来（P4）。"""

    def test_not_a_delegation(self, app):
        role = app.roles.create("开发")
        task = app.tasks.create("普通事务", [role.id], kind=TaskKind.ACTION)
        assert dispatch_blocker(
            task, [], now=NOW
        ) is None, "没有项目路径的不是委派，本函数不管"

    def test_not_started(self, app):
        task = _delegation(app, start=False)
        why = dispatch_blocker(task, [], now=NOW)
        assert why and "inbox" in why

    def test_eligible_returns_none(self, app):
        task = _delegation(app)
        assert dispatch_blocker(task, [], now=NOW) is None

    def test_running_says_how_long_left(self, app):
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        lease = {"expires_at": (NOW + timedelta(minutes=30)).isoformat()}
        why = dispatch_blocker(
            task, app.record_repo.list_for_task(task.id), lease=lease, now=NOW
        )
        assert why and "正在跑" in why and "30 分钟" in why

    def test_lost_is_named_as_lost(self, app):
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        lease = {"expires_at": (NOW - timedelta(hours=1)).isoformat()}
        why = dispatch_blocker(
            task, app.record_repo.list_for_task(task.id), lease=lease, now=NOW
        )
        assert why and "失联" in why

    def test_refused_carries_the_reason(self, app):
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_FAILED,
            "白名单拒绝：项目不在白名单", app.clock.now(),
        )
        why = dispatch_blocker(
            task, app.record_repo.list_for_task(task.id), now=NOW
        )
        assert why and "白名单" in why

    def test_succeeded_is_said_plainly(self, app):
        task = _delegation(app)
        app.record_repo.append(
            task.id, RecordType.DELEGATION_DISPATCHED, "派", app.clock.now()
        )
        app.record_repo.append(
            task.id, RecordType.DELEGATION_SUCCEEDED, "完成", app.clock.now()
        )
        assert dispatch_blocker(
            task, app.record_repo.list_for_task(task.id), now=NOW
        ) == "已经做完了"
