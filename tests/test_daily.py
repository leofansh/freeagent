"""每日「该看总览」主动推送（设计文档 11.21 / V1.60）。

服务层 :meth:`ChannelService.due_daily` / ``confirm_daily`` 的契约：
**只在有事时推、每天一次去重、quiet_hours 压后不落令牌、只摆事实**。

最该被锁住的四条反直觉行为（与 V1.58 角标同口径，不自算判定）：

1. 空的一天**不推**「今天没事」的噪音 —— 全空返回空串。
2. 取而不确认 = 还没送达，明天/下一轮还得推；确认后才落 ``day`` 令牌。
3. quiet_hours 窗口内返回压后说明、**不落令牌**，窗口一过自然再推。
4. 只报数 + 关键标题，不替用户决定先做哪个；今天标题最多列前 3 条。
"""

from __future__ import annotations

from datetime import datetime, timedelta, time
from types import SimpleNamespace as NS

import pytest

from freeagent.app import build_app
from freeagent.services.channel import ChannelService
from freeagent.services.clock import FrozenClock
from freeagent.services.reminders import QuietHours
from freeagent.services.sorting import EnergyWindows
from freeagent.storage.db import SCHEMA_VERSION

NOW = datetime(2026, 10, 23, 9, 0)  # 周五上午


@pytest.fixture()
def env(tmp_path):
    frozen = FrozenClock(NOW)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    channel = ChannelService(application, allowed_senders=frozenset())
    role = application.roles.create("工作")
    return application, channel, frozen, role


def _today(app, role, *, title="跟张总对合同"):
    return app.tasks.create(
        title, [role.id], scheduled_for=app.clock.now().date()
    )


# ------------------------------------------------------------------ 服务层
class TestDailyDigestService:
    def test_empty_day_does_not_push(self, env):
        app, channel, _, _ = env
        batch = channel.due_daily()
        assert batch.text == "" and batch.day is None, (
            "没事的一天不该推「今天没事」的噪音"
        )

    def test_today_schedule_triggers(self, env):
        app, channel, _, role = env
        _today(app, role)
        batch = channel.due_daily()
        assert "该看" in batch.text
        assert "跟张总对合同" in batch.text
        assert batch.day == "2026-10-23"

    def test_pending_triggers(self, env):
        app, channel, _, _ = env
        app.pending.add(
            source="feishu", kind="file", media_key="k1",
            name="报表.pdf", size=1, path="/x",
        )
        batch = channel.due_daily()
        assert "待处理" in batch.text

    def test_overdue_promise_triggers(self, env):
        app, channel, _, role = env
        task = _today(app, role)
        app.promise.promise(
            "周三前交报价单", task_id=task.id,
            promised_for=NOW - timedelta(days=1),
        )
        batch = channel.due_daily()
        assert "承诺逾期" in batch.text

    def test_confirm_dedups_same_day(self, env):
        app, channel, _, role = env
        _today(app, role)
        first = channel.due_daily()
        assert first.text, "第一次该推出去"
        # 取而不确认 = 还没送达，令牌不落，下一轮还得推（同设计文档 9.2）。
        assert channel.due_daily().text, "未确认前不该消失"
        channel.confirm_daily(first)
        assert channel.due_daily().text == "", "确认后当天不再推"
        assert app.daily_digests.already_sent("2026-10-23")

    def test_quiet_hours_postpones_without_token(self, env):
        app, channel, _, role = env
        _today(app, role)
        app.quiet_hours = QuietHours(time(8, 0), time(10, 0))
        batch = channel.due_daily()
        assert "安静窗口内" in batch.text
        assert batch.day is None, "窗口内不落令牌，窗口过自然再推"
        channel.confirm_daily(batch)  # 即便调用方误确认，也不该落
        assert not app.daily_digests.already_sent("2026-10-23")


# ------------------------------------------------------------------- 渲染
class TestRenderDailyDigest:
    def test_caps_three_titles(self):
        from freeagent.cli.render import render_daily_digest

        items = [NS(task=NS(title=f"事{i}")) for i in range(4)]
        text = render_daily_digest(
            "2026-10-23", today_items=items, today_count=4,
            pending_count=0, nudge_count=0, overdue_count=0,
        )
        assert "事0、事1、事2" in text
        assert "…共 4 件" in text, "超过 3 条用计数兜住，不列全"

    def test_only_facts_no_decision(self):
        from freeagent.cli.render import render_daily_digest

        text = render_daily_digest(
            "2026-10-23", today_items=[], today_count=0,
            pending_count=2, nudge_count=0, overdue_count=0,
        )
        assert "待处理 2 条" in text
        assert "先做哪个" in text, "只摆事实，不替用户决定"


# ----------------------------------------------------------------- 迁移
class TestMigration:
    def test_daily_digests_table_and_version(self, env):
        app, *_ = env
        row = app.conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='daily_digests'"
        ).fetchone()
        assert row is not None, "daily_digests 表应已建出"
        assert SCHEMA_VERSION == 20
