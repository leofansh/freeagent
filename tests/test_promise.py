"""对外承诺与关系对象（设计文档 11.19 / V1.51）。

与 test_pending.py 同一条纪律：断言按「状态正确 / 关联正确 / 计数正确」
写，而不是按「输出里有没有某个词」写。承诺最易错的地方有四件：
① 承诺有没有真挂上兑现物；② 关系能不能按人聚合；③ 逾期的信号权重是不是
真的高于 DEADLINE_RISK；④ 是不是**只陈述事实、不判违约**。
第 ④ 件错了最隐蔽 —— 系统替用户判了违约，用户却没说过。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from freeagent.app import build_app
from freeagent.domain import ContactKind, PromiseStatus, WaitingKind, WaitingOn
from freeagent.domain.enums import SortSignalCode
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows

#: 2026-10-21 是周三。钉住具体时刻，逾期天数与日期才可断言。
WEDNESDAY = datetime(2026, 10, 21, 15, 0)


@pytest.fixture
def env(tmp_path):
    """一只钉在周三的 App、它自己的时钟、一条能挂事务的角色。"""
    frozen = FrozenClock(WEDNESDAY)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    role = application.roles.create("工作")
    try:
        yield application, frozen, role
    finally:
        application.close()


def _task(app, role, title="给张总交付报价单"):
    return app.tasks.create(title, [role.id])


# --------------------------------------------------------------------- 服务层
class TestPromiseService:
    def test_promise_needs_a_deliverable(self, env):
        """承诺必须有兑现物 —— 没有兑现物的是空头承诺（11.19.2）。"""
        app, _clock, _role = env
        with pytest.raises(Exception) as exc:
            app.promise.promise("周三前交报价单")
        assert "兑现物" in str(exc.value), f"要说清缺什么：{exc.value}"

    def test_promise_rejects_unknown_task(self, env):
        app, _clock, _role = env
        with pytest.raises(Exception):
            app.promise.promise("交报价单", task_id="nope")

    def test_promise_is_open_at_creation(self, env):
        app, _clock, role = env
        task = _task(app, role)
        promise = app.promise.promise("周三前交报价单", task_id=task.id)
        assert promise.status is PromiseStatus.OPEN, "承诺只以 open 落盘"
        assert promise.is_open
        assert promise.resolved_at is None

    def test_contact_is_created_on_demand(self, env):
        """关系对象**没有就建** —— 不该反问「张总是谁」（11.19.2）。"""
        app, _clock, role = env
        task = _task(app, role)
        app.promise.promise("交报价单", contact_name="张总", task_id=task.id)
        assert app.promise.contact_count() == 1
        contact = app.promise.find_contact("张总")
        assert contact is not None
        assert contact.kind is ContactKind.PERSON

    def test_same_name_reuses_the_contact(self, env):
        """同名即同人 —— 可预期优先于聪明。"""
        app, _clock, role = env
        first = _task(app, role)
        second = _task(app, role, title="另一件")
        app.promise.promise("A", contact_name="张总", task_id=first.id)
        app.promise.promise("B", contact_name="张总", task_id=second.id)
        assert app.promise.contact_count() == 1, "同名不该建出两个关系对象"

    def test_promises_are_aggregable_by_contact(self, env):
        """关系一等化的意义：能按人聚合（11.19.3）。"""
        app, _clock, role = env
        first = _task(app, role, title="A")
        second = _task(app, role, title="B")
        app.promise.promise("A", contact_name="张总", task_id=first.id)
        app.promise.promise("B", contact_name="张总", task_id=second.id)
        contact = app.promise.find_contact("张总")
        assert contact is not None
        assert len(app.promise.promises_for_contact(contact.id)) == 2

    def test_promise_attaches_to_one_task(self, env):
        """一条事务一条承诺（11.19.2）—— 一对一，不多对多。"""
        app, _clock, role = env
        task = _task(app, role)
        app.promise.promise("A", task_id=task.id)
        app.promise.promise("B", task_id=task.id)
        assert len(app.promise.promises_for_task(task.id)) == 2, "两笔账都要在"

    def test_resolve_to_kept(self, env):
        app, _clock, role = env
        task = _task(app, role)
        promise = app.promise.promise("A", task_id=task.id)
        done = app.promise.keep(promise.id)
        assert done.status is PromiseStatus.KEPT
        assert done.resolved_at is not None, "兑现要有时刻"
        assert app.promise.open_count() == 0

    def test_resolve_twice_is_refused(self, env):
        """重复 resolve **拒绝** —— 静默改写会把账目搞糊。"""
        app, _clock, role = env
        task = _task(app, role)
        promise = app.promise.promise("A", task_id=task.id)
        app.promise.keep(promise.id)
        with pytest.raises(Exception):
            app.promise.keep(promise.id)

    def test_break_and_withdraw(self, env):
        app, _clock, role = env
        first = _task(app, role, title="A")
        second = _task(app, role, title="B")
        p1 = app.promise.promise("A", task_id=first.id)
        p2 = app.promise.promise("B", task_id=second.id)
        assert app.promise.break_(p1.id).status is PromiseStatus.BROKEN
        assert app.promise.withdraw(p2.id).status is PromiseStatus.WITHDRAWN

    def test_postpone_keeps_it_open(self, env):
        """延期**不算违约** —— 那是用户的正当权利（11.19.5）。"""
        app, clock, role = env
        task = _task(app, role)
        promise = app.promise.promise(
            "A", task_id=task.id, promised_for=WEDNESDAY - timedelta(days=2)
        )
        later = clock.advance(seconds=60) or WEDNESDAY + timedelta(days=1)
        moved = app.promise.postpone(promise.id, later)
        assert moved.status is PromiseStatus.OPEN, "延期后仍是未兑现"
        assert moved.promised_for == later

    def test_overdue_is_a_fact_not_a_verdict(self, env):
        """系统只判「过了日子」，不判「违约了」（11.19.5）。"""
        app, clock, role = env
        task = _task(app, role)
        promise = app.promise.promise(
            "A", task_id=task.id, promised_for=WEDNESDAY - timedelta(days=2)
        )
        assert app.promise.is_overdue(promise, clock.now()) is True
        # 状态仍是 open —— 系统没替用户判违约
        assert promise.status is PromiseStatus.OPEN

    def test_open_promises_sorted_by_due_date(self, env):
        """未兑现的按期限升序 —— 用户要看的是「快到期的」（11.19.3）。"""
        app, clock, role = env
        later_task = _task(app, role, title="晚")
        soon_task = _task(app, role, title="早")
        app.promise.promise(
            "晚的", task_id=later_task.id, promised_for=WEDNESDAY + timedelta(days=7)
        )
        app.promise.promise(
            "早的", task_id=soon_task.id, promised_for=WEDNESDAY + timedelta(days=1)
        )
        items = app.promise.open_promises()
        assert [p.statement for p in items] == ["早的", "晚的"]

    def test_no_due_date_sorts_last(self, env):
        """没给日期的排最后 —— SQLite 的 ASC 会把 NULL 排最前。"""
        app, _clock, role = env
        dated_task = _task(app, role, title="有期限")
        undated_task = _task(app, role, title="没期限")
        app.promise.promise(
            "有期限", task_id=dated_task.id, promised_for=WEDNESDAY + timedelta(days=1)
        )
        app.promise.promise("没期限", task_id=undated_task.id)
        items = app.promise.open_promises()
        assert items[-1].statement == "没期限"


# --------------------------------------------------------------------- 一页账
class TestContactLedger:
    """关系对象名下的「一页账」（20.1.5 的最后一步）。

    断言核心：两半都要在（我欠对方 + 我在等对方），已结清的**不再占版面**，
    且账严格按人分 —— 别人的账不混进这一页。
    """

    def test_ledger_carries_both_sides(self, env):
        """两半齐全：我欠他的（承诺）+ 我在等他的（等候），都带兑现物。"""
        app, clock, role = env
        promised_task = _task(app, role, title="给张总交付报价单")
        app.promise.promise("周三前交报价单", contact_name="张总", task_id=promised_task.id)
        contact = app.promise.find_contact("张总")
        assert contact is not None
        waiting_task = _task(app, role, title="等张总回传合同")
        app.tasks.block(
            waiting_task.id,
            WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what="合同回传",
                since=clock.now(),
                contact_id=contact.id,
            ),
            reason="放一放",
        )

        ledger = app.promise.ledger(contact)
        assert [p.promise.statement for p in ledger.promises] == ["周三前交报价单"]
        assert [p.task_title for p in ledger.promises] == ["给张总交付报价单"]
        assert [w.waiting.who_or_what for w in ledger.waitings] == ["合同回传"]
        assert [w.task.title for w in ledger.waitings] == ["等张总回传合同"]
        assert not ledger.is_empty

    def test_ledger_excludes_resolved_promises(self, env):
        """已兑现的承诺**不再占一页账的版面** —— 那是不重复打扰，不是删历史。"""
        app, _clock, role = env
        task = _task(app, role)
        promise = app.promise.promise("A", contact_name="张总", task_id=task.id)
        app.promise.keep(promise.id)
        contact = app.promise.find_contact("张总")
        assert contact is not None
        ledger = app.promise.ledger(contact)
        assert ledger.promises == []
        assert ledger.is_empty

    def test_ledger_excludes_ended_waitings(self, env):
        """已结束（不再是 open）的等候不进一页账。"""
        app, clock, role = env
        contact = app.promise.contact("张总")
        task = _task(app, role, title="等合同")
        app.tasks.block(
            task.id,
            WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what="合同",
                since=clock.now(),
                contact_id=contact.id,
            ),
            reason="放一放",
        )
        app.tasks.complete(task.id)
        ledger = app.promise.ledger(contact)
        assert ledger.is_empty

    def test_ledger_is_scoped_to_that_contact(self, env):
        """账严格按人分 —— 别人的等候不混进这一页。"""
        app, clock, role = env
        zhang = app.promise.contact("张总")
        li = app.promise.contact("李总")
        task = _task(app, role, title="等李总回传报价")
        app.tasks.block(
            task.id,
            WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what="报价",
                since=clock.now(),
                contact_id=li.id,
            ),
            reason="放一放",
        )
        assert app.promise.ledger(zhang).is_empty
        assert not app.promise.ledger(li).is_empty


# --------------------------------------------------------------------- 排序信号
class TestPromiseSignal:
    def _signals(self, app, task_id):
        from freeagent.services.sorting import compute_signals

        promise = app.promise.promise_of(task_id)
        return compute_signals(
            app.tasks.get(task_id),
            WEDNESDAY,
            WEDNESDAY.date(),
            promise=promise,
        )

    def test_overdue_promise_raises_a_signal(self, env):
        app, _clock, role = env
        task = _task(app, role)
        app.promise.promise(
            "A", task_id=task.id, promised_for=WEDNESDAY - timedelta(days=2)
        )
        codes = [s.code for s in self._signals(app, task.id)]
        assert SortSignalCode.PROMISE_OVERDUE in codes

    def test_signal_weight_beats_deadline_risk(self, env):
        """承诺逾期的权重必须**高于** DEADLINE_RISK（19.5.3）。"""
        from freeagent.domain import SORT_SIGNAL_WEIGHTS

        assert (
            SORT_SIGNAL_WEIGHTS[SortSignalCode.PROMISE_OVERDUE]
            > SORT_SIGNAL_WEIGHTS[SortSignalCode.DEADLINE_RISK]
        )

    def test_signal_reason_states_fact_not_verdict(self, env):
        """理由只说「逾期未兑现」，不判「违约」（11.19.5）。"""
        app, _clock, role = env
        task = _task(app, role)
        app.promise.promise(
            "A", task_id=task.id, promised_for=WEDNESDAY - timedelta(days=2)
        )
        signals = [
            s
            for s in self._signals(app, task.id)
            if s.code is SortSignalCode.PROMISE_OVERDUE
        ]
        assert signals, "该有信号"
        reason = signals[0].reason
        assert "违约" not in reason, f"系统不判违约：{reason!r}"
        assert "逾期" in reason

    def test_not_overdue_no_signal(self, env):
        app, _clock, role = env
        task = _task(app, role)
        app.promise.promise(
            "A", task_id=task.id, promised_for=WEDNESDAY + timedelta(days=2)
        )
        codes = [s.code for s in self._signals(app, task.id)]
        assert SortSignalCode.PROMISE_OVERDUE not in codes

    def test_kept_promise_stops_signalling(self, env):
        """账结了就不该再占权重 —— 那是噪声。"""
        app, _clock, role = env
        task = _task(app, role)
        promise = app.promise.promise(
            "A", task_id=task.id, promised_for=WEDNESDAY - timedelta(days=2)
        )
        app.promise.keep(promise.id)
        codes = [s.code for s in self._signals(app, task.id)]
        assert SortSignalCode.PROMISE_OVERDUE not in codes

    def test_today_view_orders_overdue_promise_first(self, env):
        """逾期的事务该排在今天最前面。"""
        app, _clock, role = env
        plain = app.tasks.create("普通活儿", [role.id], scheduled_for=WEDNESDAY.date())
        promised = app.tasks.create(
            "给张总交付报价单", [role.id], scheduled_for=WEDNESDAY.date()
        )
        app.promise.promise(
            "A", task_id=promised.id, promised_for=WEDNESDAY - timedelta(days=2)
        )
        view = app.today.view()
        titles = [item.task.title for item in view.items]
        assert titles.index("给张总交付报价单") < titles.index("普通活儿")


# --------------------------------------------------------------------- 命令层
class TestPromiseCommands:
    def _repl(self, app):
        import io

        from freeagent.cli.app import Repl

        buf = io.StringIO()
        return Repl(app, out=buf), buf

    def test_list_is_empty_at_first(self, env):
        app, _clock, _role = env
        repl, buf = self._repl(app)
        repl.handle("/promise")
        assert "没有未兑现的承诺" in buf.getvalue()

    def test_record_then_list(self, env):
        app, _clock, role = env
        task = _task(app, role)
        repl, buf = self._repl(app)
        repl.handle(f"/promise 记 {task.id[:8]} 周三前交报价单 关系=张总")
        assert "记下了这条承诺" in buf.getvalue()
        buf.truncate(0)
        buf.seek(0)
        repl.handle("/promise")
        text = buf.getvalue()
        assert "周三前交报价单" in text
        assert "张总" in text, "要显示答应了谁"

    def test_record_rejects_unknown_marker(self, env):
        """看不懂的标记要说清楚怎么写，不能默默吞掉。"""
        app, _clock, role = env
        task = _task(app, role)
        repl, buf = self._repl(app)
        repl.handle(f"/promise 记 {task.id[:8]} 交报价单 张总")
        assert "关系=" in buf.getvalue(), f"要教用户怎么写：{buf.getvalue()!r}"

    def test_keep_marks_resolved(self, env):
        app, _clock, role = env
        task = _task(app, role)
        repl, buf = self._repl(app)
        repl.handle(f"/promise 记 {task.id[:8]} 交报价单")
        buf.truncate(0)
        buf.seek(0)
        repl.handle("/promise keep 1")
        assert "已兑现" in buf.getvalue()
        assert app.promise.open_count() == 0

    def test_index_out_of_range_is_explained(self, env):
        app, _clock, _role = env
        repl, buf = self._repl(app)
        repl.handle("/promise keep 5")
        assert "没有第 5 条" in buf.getvalue()

    def test_contact_command_lists_contacts(self, env):
        app, _clock, role = env
        task = _task(app, role)
        app.promise.promise("A", contact_name="张总", task_id=task.id)
        repl, buf = self._repl(app)
        repl.handle("/contact")
        text = buf.getvalue()
        assert "张总" in text
        assert "1 条承诺未兑现" in text, "要能看出我跟谁之间还有账"

    def test_contact_command_creates(self, env):
        app, _clock, _role = env
        repl, buf = self._repl(app)
        repl.handle("/contact 张总 客户联系人")
        assert app.promise.contact_count() == 1
        contact = app.promise.find_contact("张总")
        assert contact is not None
        assert contact.note == "客户联系人"

    def test_contact_single_arg_shows_ledger_when_exists(self, env):
        """单参数 ``/contact <名>`` 且已存在 → 返回「一页账」，不创建、不吞掉。"""
        app, _clock, role = env
        task = _task(app, role, title="给张总交付报价单")
        app.promise.promise("周三前交报价单", contact_name="张总", task_id=task.id)
        repl, buf = self._repl(app)
        repl.handle("/contact 张总")
        text = buf.getvalue()
        assert "我欠他的" in text
        assert "周三前交报价单" in text
        assert "已记下" not in text, "查账不是记备注，不该出现创建话术"

    def test_contact_single_arg_creates_when_missing(self, env):
        """单参数但**不存在** → 仍创建（记账层不该反问「张总是谁」）。"""
        app, _clock, _role = env
        repl, buf = self._repl(app)
        repl.handle("/contact 王总")
        assert app.promise.find_contact("王总") is not None
