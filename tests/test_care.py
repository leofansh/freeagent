"""人生角色关注面（设计文档 11.24 / V1.68）。

断言纪律（同 test_pending.py）：按「状态正确 / 关联正确 / 计数正确」写，
不按「输出里有没有某个词」写。关注面最易错的地方有三件：
① 节奏（cadence）有没有被正确解析成 ``weekly:N`` / ``monthly:N``；
② ``next_due``（下次该看的日期）有没有现算对 —— 到期日**不落库为绝对
时刻**，每次由 cadence 算；③ ``/care done`` 是不是真的把到期推到**下个**
周期（而不是同一天还被当成到期），以及关注面是不是**不因看了而消失**。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from freeagent.app import build_app
from freeagent.domain import NotFoundError, ValidationError
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows

#: 2026-10-21 是周三（ISO weekday 3）。钉住具体时刻，next_due 才可断言。
WEDNESDAY = datetime(2026, 10, 21, 9, 0)


@pytest.fixture
def env(tmp_path):
    frozen = FrozenClock(WEDNESDAY)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    role = application.roles.create("爸爸")
    try:
        yield application, frozen, role
    finally:
        application.close()


def _care(app, role, text="孩子的作业和状态", cadence="weekly:6"):
    return app.care.add(role.id, text, cadence)


# ----------------------------------------------------------------- 服务层
class TestCareService:
    def test_add_requires_existing_role(self, env):
        app, _clock, _role = env
        with pytest.raises(NotFoundError):
            app.care.add("no-such-role", "孩子的作业", "weekly:6")

    def test_add_rejects_bad_cadence(self, env):
        app, _clock, role = env
        with pytest.raises(ValidationError):
            app.care.add(role.id, "孩子的作业", "every-other-week")
        with pytest.raises(ValidationError):
            app.care.add(role.id, "孩子的作业", "weekly:9")
        with pytest.raises(ValidationError):
            app.care.add(role.id, "孩子的作业", "monthly:0")

    def test_add_rejects_empty_text(self, env):
        app, _clock, role = env
        with pytest.raises(ValidationError):
            app.care.add(role.id, "   ", "weekly:1")

    def test_weekly_next_due(self, env):
        """从周三记「每周六」→ next_due = 本周六 10-24。"""
        app, _clock, role = env
        area = _care(app, role, cadence="weekly:6")
        assert area.cadence == "weekly:6"
        assert area.next_due == date(2026, 10, 24)

    def test_monthly_next_due(self, env):
        """从 10-21 记「每月 1 号」→ 本月 1 号已过 → 下月 11-01。"""
        app, _clock, role = env
        area = _care(app, role, cadence="monthly:1")
        assert area.cadence == "monthly:1"
        assert area.next_due == date(2026, 11, 1)

    def test_due_returns_only_that_day(self, env):
        app, _clock, role = env
        due_area = _care(app, role, text="父母的身体", cadence="weekly:3")  # 周三到期=今天
        _care(app, role, text="公司现金流", cadence="monthly:20")  # 20 号
        dues = app.care.due(date(2026, 10, 21))
        assert [d.area.id for d in dues] == [due_area.id]
        assert dues[0].role_name == "爸爸"

    def test_done_rolls_to_next_week(self, env):
        """「看了」之后推到**下个**周期（10-24 周六 → 下周六 10-31）。"""
        app, _clock, role = env
        area = _care(app, role)
        rolled = app.care.done(1)
        assert rolled.next_due == date(2026, 10, 31)
        # 关注面不因看了而消失 —— 列表里还在，只是 next_due 变了。
        assert [a.id for a in app.care.list()] == [area.id]

    def test_done_rejects_out_of_range(self, env):
        app, _clock, role = env
        with pytest.raises(NotFoundError):
            app.care.done(1)

    def test_count(self, env):
        app, _clock, role = env
        assert app.care.count() == 0
        _care(app, role)
        _care(app, role, text="父母的身体", cadence="monthly:1")
        assert app.care.count() == 2

    def test_monthly_without_that_day_rolls_to_month_end(self, env):
        """2 月记「每月 30 号」→ 2 月没有 30 → 顺到 2 月最后一天。"""
        app, frozen, role = env
        frozen.set(datetime(2026, 2, 3, 9, 0))
        area = _care(app, role, cadence="monthly:30")
        assert area.next_due == date(2026, 2, 28)


# --------------------------------------------------------------- 命令层
class TestCareCommands:
    def _repl(self, app):
        import io

        from freeagent.cli.app import Repl

        buf = io.StringIO()
        return Repl(app, out=buf), buf

    def test_list_is_empty_at_first(self, env):
        app, _clock, _role = env
        repl, buf = self._repl(app)
        repl.handle("/care")
        assert "还没有角色关注面" in buf.getvalue()

    def test_record_defaults_to_monday(self, env):
        app, _clock, role = env
        repl, buf = self._repl(app)
        repl.handle("/care 爸爸 孩子的作业")
        assert "每周一" in buf.getvalue()

    def test_record_with_weekday(self, env):
        app, _clock, role = env
        repl, buf = self._repl(app)
        repl.handle("/care 爸爸 孩子的作业和状态 每周六")
        assert "每周六" in buf.getvalue()
        assert [a.cadence for a in app.care.list()] == ["weekly:6"]

    def test_record_with_monthly(self, env):
        app, _clock, role = env
        repl, buf = self._repl(app)
        repl.handle("/care 爸爸 公司本月现金流 每月1号")
        assert "每月1号" in buf.getvalue()
        assert [a.cadence for a in app.care.list()] == ["monthly:1"]

    def test_record_unknown_role_is_explained(self, env):
        app, _clock, _role = env
        repl, buf = self._repl(app)
        repl.handle("/care 不存在 孩子的作业 每周六")
        assert "找不到角色" in buf.getvalue()

    def test_record_bad_cadence_is_explained(self, env):
        app, _clock, role = env
        repl, buf = self._repl(app)
        repl.handle("/care 爸爸 孩子的作业 每天")
        assert "节奏" in buf.getvalue()

    def test_done_rolls(self, env):
        app, _clock, role = env
        _care(app, role, text="父母的身体")
        repl, buf = self._repl(app)
        repl.handle("/care done 1")
        out = buf.getvalue()
        assert "推" in out and "父母的身体" in out
        assert app.care.list()[0].next_due == date(2026, 10, 31)

    def test_done_out_of_range_is_explained(self, env):
        app, _clock, role = env
        repl, buf = self._repl(app)
        repl.handle("/care done 1")
        assert "当前共 0 条" in buf.getvalue()


# ------------------------------------------------------------- 每日总览并入
class TestCareDaily:
    def _channel(self, app):
        from freeagent.services.channel import ChannelService

        return ChannelService(app, allowed_senders=frozenset())

    def test_due_care_appears_in_daily_digest(self, env):
        """今天有到期的关注面 → 即使没有任何任务/待办/该催，也该推。"""
        app, _clock, role = env
        _care(app, role, text="父母的身体", cadence="weekly:3")  # 周三到期=今天
        batch = self._channel(app).due_daily()
        assert batch.day is not None
        assert "父母的身体" in batch.text
        assert "爸爸" in batch.text

    def test_care_not_due_no_daily(self, env):
        app, _clock, role = env
        _care(app, role, text="公司现金流", cadence="monthly:20")  # 20 号，今天 21
        batch = self._channel(app).due_daily()
        assert batch.text == ""
        assert batch.day is None

    def test_done_clears_today_then_returns_next_cycle(self, env):
        """看了之后当天不再是到期 —— 明天（若是下个该日）才再来。"""
        app, _clock, role = env
        _care(app, role, text="父母的身体", cadence="weekly:3")
        app.care.done(1)
        assert self._channel(app).due_daily().text == ""
