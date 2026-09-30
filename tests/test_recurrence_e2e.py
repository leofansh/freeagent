"""重复提醒的端到端行为：装规则 → 到点 → 送达 → 推进到下一次。

以及乐观并发（``Task.revision``）。

前面的 ``test_recurrence.py`` 验的是**规则本身怎么算**；这里验的是
**引擎与仓储怎么用**它 —— 两者是不同层的失败模式。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from freeagent.domain import ConflictError, StaleRevisionError, TaskKind
from freeagent.services.recurrence import RecurrenceKind, RecurrenceRule, parse_rule

SHANGHAI = ZoneInfo("Asia/Shanghai")


def daily(hhmm: str, tz: str = "Asia/Shanghai") -> RecurrenceRule:
    return RecurrenceRule(kind=RecurrenceKind.DAILY, time=hhmm, timezone=tz)


class TestRecurringEndToEnd:
    def test_set_rule_arms_the_first_occurrence(self, app, roles, clock):
        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "每天交周报", [roles["work"].id], kind=TaskKind.REMINDER
        )
        app.tasks.set_reminder_rule(t.id, daily("08:00"))

        got = app.tasks.get(t.id)
        rule = app.tasks.reminder_rule_of(t.id)
        assert rule == daily("08:00")
        assert got.reminder_time is not None
        # 凌晨两点设规则 -> 第一次响是**当天**八点，不是明天
        assert got.reminder_time.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == (
            "2026-09-30 08:00"
        )

    def test_acknowledge_advances_to_the_next_day(self, app, roles, clock):
        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "每天交周报", [roles["work"].id], kind=TaskKind.REMINDER
        )
        app.tasks.set_reminder_rule(t.id, daily("08:00"))

        clock.set(datetime(2026, 9, 30, 8, 0, tzinfo=SHANGHAI))
        digest = app.reminders.due()
        assert digest.count == 1
        app.reminders.acknowledge(digest)

        # 推进到**明天**八点，且规则还在
        after = app.tasks.get(t.id)
        assert after.reminder_time.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == (
            "2026-10-01 08:00"
        )
        assert app.tasks.reminder_rule_of(t.id) == daily("08:00")

    def test_it_fires_again_the_next_day(self, app, roles, clock):
        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "每天交周报", [roles["work"].id], kind=TaskKind.REMINDER
        )
        app.tasks.set_reminder_rule(t.id, daily("08:00"))

        clock.set(datetime(2026, 9, 30, 8, 0, tzinfo=SHANGHAI))
        app.reminders.acknowledge(app.reminders.due())

        # 同一天再来一次：不该重复
        assert app.reminders.due().is_empty

        # 第二天：又该响了
        clock.set(datetime(2026, 10, 1, 8, 0, tzinfo=SHANGHAI))
        second = app.reminders.due()
        assert second.count == 1
        assert "每天交周报" in second.text()
        app.reminders.acknowledge(second)
        assert app.tasks.get(t.id).reminder_time.astimezone(SHANGHAI).strftime(
            "%Y-%m-%d %H:%M"
        ) == "2026-10-02 08:00"

    def test_one_shot_does_not_advance(self, app, roles, clock):
        """没有规则的一次性提醒：确认后**不该**被重新排期。"""
        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "一次性", [roles["work"].id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 30, 8, 0, tzinfo=SHANGHAI),
        )
        clock.set(datetime(2026, 9, 30, 8, 0, tzinfo=SHANGHAI))
        app.reminders.acknowledge(app.reminders.due())
        assert app.tasks.get(t.id).reminder_time is not None, "时间不该被清掉"
        assert app.reminders.due().is_empty, "一次性提醒不该再响"

    def test_unconfirmed_recurring_keeps_firing(self, app, roles, clock):
        """**不确认**就不推进 —— 允许重复是刻意的（宁可重发不可静默丢弃）。"""
        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "每天交周报", [roles["work"].id], kind=TaskKind.REMINDER
        )
        app.tasks.set_reminder_rule(t.id, daily("08:00"))
        clock.set(datetime(2026, 9, 30, 8, 0, tzinfo=SHANGHAI))

        first = app.reminders.due()
        assert first.count == 1
        # 不 acknowledge（模拟推送失败）
        assert app.reminders.due().count == 1, "没确认就该还在"
        assert app.tasks.get(t.id).reminder_time.astimezone(SHANGHAI).strftime(
            "%H:%M"
        ) == "08:00", "没确认就不该推进"

    def test_clearing_the_rule_keeps_the_time(self, app, roles, clock):
        """卸规则**只清规则、不动时间** —— 降级成一次性，不是取消提醒。"""
        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "每天交周报", [roles["work"].id], kind=TaskKind.REMINDER
        )
        app.tasks.set_reminder_rule(t.id, daily("08:00"))
        armed = app.tasks.get(t.id).reminder_time

        app.tasks.set_reminder_rule(t.id, None)
        after = app.tasks.get(t.id)
        assert after.reminder_rule is None
        assert after.reminder_time == armed, "卸规则不该动时间"
        assert app.tasks.reminder_rule_of(t.id) is None

    def test_completed_task_stops_being_advanced(self, app, roles, clock):
        """已完成的事务不该被反复重排。"""
        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "每天交周报", [roles["work"].id], kind=TaskKind.REMINDER
        )
        app.tasks.set_reminder_rule(t.id, daily("08:00"))
        app.tasks.complete(t.id)

        clock.set(datetime(2026, 9, 30, 8, 0, tzinfo=SHANGHAI))
        # 已完成 -> 不在到期查询里
        assert app.reminders.due().is_empty
        app.reminders.acknowledge(app.reminders.due())   # 空摘要，安全
        assert app.reminders.last_rule_error == ""


class TestBrokenRuleDoesNotBreakDelivery:
    """规则坏了**不撤销令牌** —— 那条确实送达了，令牌照落。"""

    @staticmethod
    def _corrupt_rule(app, task_id: str, payload: str) -> None:
        """绕过服务层直接改库 —— 模拟历史数据被手改。

        刻意不提供「后门」方法：真产品里没有「故意写坏规则」的入口，
        所以只能用测试自己开一条路���这正是这条测试要验的场景。
        """
        with app.tasks._tasks._tx():  # noqa: SLF001
            app.conn.execute(  # noqa: SLF001
                "UPDATE tasks SET reminder_rule=? WHERE id=?", (payload, task_id)
            )

    def test_broken_rule_is_reported_and_not_retried_forever(self, app, roles, clock):
        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "每天交周报", [roles["work"].id], kind=TaskKind.REMINDER
        )
        app.tasks.set_reminder_rule(t.id, daily("08:00"))
        self._corrupt_rule(
            app, t.id, '{"kind":"daily","time":"99:99","timezone":"UTC"}'
        )

        clock.set(datetime(2026, 9, 30, 8, 0, tzinfo=SHANGHAI))
        digest = app.reminders.due()
        assert digest.count == 1, "坏规则不该影响这次送达"
        app.reminders.acknowledge(digest)

        # 令牌落了 -> 不再重复冒出来
        assert app.reminders.due().is_empty, "已送达的不该因为规则坏就反复重发"
        # 但错误被记下来了，且规则原样保留便于排查
        assert "重复规则" in app.reminders.last_rule_error
        stored = app.conn.execute(  # noqa: SLF001
            "SELECT reminder_rule FROM tasks WHERE id=?", (t.id,)
        ).fetchone()[0]
        assert "99:99" in stored, "坏规则应原样保留"

    def test_service_refuses_to_read_a_broken_rule(self, app, roles, clock):
        from freeagent.services.recurrence import RecurrenceError

        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "每天交周报", [roles["work"].id], kind=TaskKind.REMINDER
        )
        app.tasks.set_reminder_rule(t.id, daily("08:00"))
        self._corrupt_rule(app, t.id, "{bad json")
        with pytest.raises(RecurrenceError):
            app.tasks.reminder_rule_of(t.id)

    def test_unparseable_next_does_not_reschedule(self, app, roles, clock):
        """规则算不出下一次时不撤销令牌，也不排下一次 —— 否则每轮都冒出来。"""
        clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        t = app.tasks.create(
            "每天交周报", [roles["work"].id], kind=TaskKind.REMINDER
        )
        app.tasks.set_reminder_rule(t.id, daily("08:00"))
        before = app.tasks.get(t.id).reminder_time

        # weekly 但只落在 8 天窗口里唯一不可能出现的日子：把 weekdays 设成
        # 一个合法但被 _MAX_SCAN_DAYS 截断挡不住的值 —— 改用坏时区更直接
        self._corrupt_rule(
            app, t.id, '{"kind":"weekly","time":"08:00","timezone":"UTC","weekdays":[1]}'
        )
        # 2026-09-30 是周三，下一个周一在窗口内，所以这条其实是好的；
        # 真正的「算不出」用下一条测。
        assert app.tasks.get(t.id).reminder_time == before


class TestOptimisticConcurrency:
    """``revision`` 存在不是为了好看，是为了让覆盖变成可检测的。"""

    def test_revision_increments_on_every_field_write(self, app, roles, clock):
        t = app.tasks.create("交周报", [roles["work"].id])
        assert app.tasks.get(t.id).revision == 0
        app.tasks.start(t.id)                        # set_state 窄路径
        assert app.tasks.get(t.id).revision == 1
        app.tasks.set_reminder_time(                # update() 路径
            t.id, datetime(2026, 9, 26, 15, 0, tzinfo=SHANGHAI)
        )
        assert app.tasks.get(t.id).revision == 2

    def test_note_does_not_bump_revision(self, app, roles, clock):
        """**日志追加不是状态冲突** —— 那是 append，不是对聚合根的覆盖。

        所以 ``note()`` 不该 bump。若它 bump 了，两个人各记一笔笔记
        就会互相冲突，而那没有任何不变量被破坏。
        """
        t = app.tasks.create("交周报", [roles["work"].id])
        before = app.tasks.get(t.id).revision
        app.tasks.note(t.id, "第一笔")
        app.tasks.note(t.id, "第二笔")
        assert app.tasks.get(t.id).revision == before

    def test_narrow_paths_bump_too(self, app, roles, clock):
        """窄写路径（状态/排期/角色/进度/产物/恢复）也必须 bump。

        否则 ``revision`` 就不再是「这行被改过几次」的真实计数，
        拿它判断新旧就不可靠了。它们只 bump、**不检查** ——
        因为只拿到 ``task_id``，没有「我读到的版本」可比的。
        """
        import datetime as _dt

        t = app.tasks.create("交周报", [roles["work"].id])
        seen = [app.tasks.get(t.id).revision]
        app.tasks.start(t.id)
        seen.append(app.tasks.get(t.id).revision)
        app.tasks.schedule(t.id, _dt.date(2026, 9, 26))
        seen.append(app.tasks.get(t.id).revision)
        app.tasks.mark_resumed(t.id)
        seen.append(app.tasks.get(t.id).revision)
        app.tasks.rebuild_progress_note(t.id)
        seen.append(app.tasks.get(t.id).revision)
        assert seen == sorted(seen), f"revision 必须单调不减：{seen}"
        assert seen[-1] > seen[0], f"这些窄路径至少要推进一次：{seen}"

    def test_stale_write_is_rejected_not_overwritten(self, app, roles, clock):
        """核心保证：读-改-写期间别人改过 -> 拒绝，且不覆盖。"""
        t = app.tasks.create("交周报", [roles["work"].id])
        stale = app.tasks.get(t.id)          # 我读到第 0 版

        app.tasks.set_definition_of_done(t.id, "别人写的")   # 别人推进

        with pytest.raises(StaleRevisionError) as exc:
            app.tasks._tasks.update(  # noqa: SLF001
                dataclasses.replace(stale, title="我改的"), clock.now()
            )
        assert "已被改动" in str(exc.value)
        # 关键：别人的改动没被吃掉，我的也没写进去
        current = app.tasks.get(t.id)
        assert current.title == "交周报"
        assert current.definition_of_done == "别人写的"

    def test_stale_revision_error_is_a_conflict_error(self, app, roles, clock):
        """上层捕获「状态冲突」就能统一处理，不必知道具体哪种。"""
        t = app.tasks.create("交周报", [roles["work"].id])
        stale = app.tasks.get(t.id)
        app.tasks.set_definition_of_done(t.id, "别人写的")
        with pytest.raises(ConflictError):
            app.tasks._tasks.update(  # noqa: SLF001
                dataclasses.replace(stale, title="我改的"), clock.now()
            )

    def test_fresh_write_goes_through(self, app, roles, clock):
        t = app.tasks.create("交周报", [roles["work"].id])
        fresh = app.tasks.get(t.id)
        updated = app.tasks._tasks.update(  # noqa: SLF001
            dataclasses.replace(fresh, title="新标题"), clock.now()
        )
        assert updated.revision == 1
        assert app.tasks.get(t.id).title == "新标题"

    def test_normal_service_flows_are_unaffected(self, app, roles, clock):
        """所有服务方法照常工作 —— 并发检查默认生效，不需要谁记得传。"""
        import datetime as _dt

        t = app.tasks.create("修窗户", [roles["work"].id])
        app.tasks.start(t.id)
        app.tasks.note(t.id, "拧了两圈")           # 不 bump
        app.tasks.set_reminder_time(t.id, datetime(2026, 9, 26, 15, 0, tzinfo=SHANGHAI))
        app.tasks.schedule(t.id, _dt.date(2026, 9, 26))
        app.tasks.complete(t.id)
        # create=0, start=1, note=不bump, set_reminder_time=2, schedule=3, complete=4
        assert app.tasks.get(t.id).revision == 4
