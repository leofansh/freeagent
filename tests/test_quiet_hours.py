"""安静窗口（设计文档 9.8）。

关注点四组：

1. ``QuietHours`` 本身的判定 —— 尤其**跨天**（22:00–08:00 这类）与左闭右开；
2. 配置解析 —— 缺省=关、写得半吊子必须报错（不许静默忽略）；
3. 引擎行为 —— 窗口内 ``due()`` 空摘要但带 ``quiet_until``、**不落令牌**，
   窗口一过原样再推（压后 ≠ 丢弃）；
4. 三条通道都把「压后」说出来 —— 从终端到 Web 到飞书，不许有一处静默。

## 为什么单开一个文件

这几条**不能**塞进 ``test_config.py`` 或 ``test_signals_today_reminders.py``：
安静窗口最要紧的那句话是「**不要复用 energy_windows**」（9.8），
它是个跨越「值对象 / 配置 / 引擎 / 三条通道」的纵向关注点，
拆进别的文件的任何一个角落都会丢掉那个「一句话结论」的语境。
"""

from __future__ import annotations

import json
from datetime import datetime, time

import pytest

from freeagent.app import build_app
from freeagent.config import Config, config_from_settings, load_config, save_config
from freeagent.domain import TaskKind, ValidationError
from freeagent.services.clock import FrozenClock
from freeagent.services.reminders import QuietHours, ReminderEngine
from freeagent.services.sorting import EnergyWindows


# =============================================================================
# 1. QuietHours 的值语义
# =============================================================================
class TestQuietHoursValue:
    def test_same_start_and_end_is_rejected(self):
        """零长度窗口没有意义，且多半是把 08:00 写成了 22:00 —— 报错别吞。"""
        with pytest.raises(ValueError):
            QuietHours(start=time(22, 0), end=time(22, 0))

    def test_crosses_midnight_detection(self):
        assert QuietHours(start=time(22, 0), end=time(8, 0)).crosses_midnight
        assert not QuietHours(start=time(1, 0), end=time(6, 0)).crosses_midnight

    def test_same_day_window_is_half_open(self):
        q = QuietHours(start=time(12, 0), end=time(14, 0))
        assert not q.contains(datetime(2026, 10, 21, 11, 59))
        assert q.contains(datetime(2026, 10, 21, 12, 0))       # 左闭
        assert q.contains(datetime(2026, 10, 21, 13, 59))
        assert not q.contains(datetime(2026, 10, 21, 14, 0))   # 右开

    def test_crossing_midnight_window(self):
        """22:00–08:00：晚上到点、凌晨都算，白天不算。"""
        q = QuietHours(start=time(22, 0), end=time(8, 0))
        assert q.contains(datetime(2026, 10, 21, 23, 30))
        assert q.contains(datetime(2026, 10, 22, 0, 30))
        assert q.contains(datetime(2026, 10, 22, 7, 59))
        assert not q.contains(datetime(2026, 10, 22, 8, 0))    # 右开
        assert not q.contains(datetime(2026, 10, 21, 21, 59))
        assert not q.contains(datetime(2026, 10, 21, 12, 0))

    def test_describe_mentions_crossing(self):
        text = QuietHours(start=time(22, 0), end=time(8, 0)).describe()
        assert "22:00" in text and "08:00" in text and "跨天" in text

    def test_independent_from_energy_windows(self):
        """9.8 的核心：安静窗口**不是** evening=(18,24) 那把桶。

        这条测试是「别再犯」的防线 —— 谁哪天想把它塞进 EnergyWindows，
        先看这里：18 点明明白白不属于 22:00–08:00 的安静窗口。
        """
        q = QuietHours(start=time(22, 0), end=time(8, 0))
        eighteen = datetime(2026, 10, 21, 18, 30)
        assert EnergyWindows().label_for(eighteen) == "晚上"  # 按精力桶是晚上
        assert not q.contains(eighteen)                       # 但**不**静音


# =============================================================================
# 2. 配置解析：缺省=关，半吊子=报错
# =============================================================================
@pytest.fixture
def home(tmp_path):
    return tmp_path


class TestQuietHoursConfig:
    def test_absent_means_disabled(self, home):
        save_config(Config(home=str(home)))
        assert load_config(home).quiet_hours is None

    @pytest.mark.parametrize("blank", [None, False, [], ["", ""]])
    def test_blank_forms_all_mean_disabled(self, home, blank):
        """清空一个可选窗口是合理动作，不该逼用户去删键。"""
        payload = {} if blank is None else {"quiet_hours": blank}
        (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        assert load_config(home).quiet_hours is None

    def test_round_trips(self, home):
        hours = QuietHours(start=time(22, 0), end=time(8, 0))
        save_config(Config(quiet_hours=hours, home=str(home)))
        loaded = load_config(home)
        assert loaded.quiet_hours is not None
        assert loaded.quiet_hours.start == time(22, 0)
        assert loaded.quiet_hours.end == time(8, 0)
        # 落盘是 ["22:00","08:00"] 这种可读形式，不是 {"start":...}
        raw = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert raw["quiet_hours"] == ["22:00", "08:00"]

    @pytest.mark.parametrize("bad", [
        ["22:00"],                    # 只有一个
        ["22:00", "08:00", "09:00"],  # 三个
        [22, 8],                      # 不是 HH:MM 字符串
        ["22", "08"],                 # 缺分钟
        ["25:00", "08:00"],           # 小时越界
        ["22:60", "08:00"],           # 分钟越界
        ["8am", "9am"],               # 宽松写法不接受
        ["22:00", ""],                # 半个
    ])
    def test_bad_shapes_are_rejected(self, home, bad):
        payload = {"quiet_hours": bad}
        (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValidationError):
            load_config(home)

    def test_from_settings_can_change_it(self, home):
        base = Config(quiet_hours=QuietHours(start=time(22, 0), end=time(8, 0)),
                      home=str(home))
        out = config_from_settings({"quiet_hours": ["23:30", "07:00"]}, base=base)
        assert out.quiet_hours.start == time(23, 30)
        assert out.quiet_hours.end == time(7, 0)

    def test_from_settings_disabled_by_null(self, home):
        base = Config(quiet_hours=QuietHours(start=time(22, 0), end=time(8, 0)),
                      home=str(home))
        out = config_from_settings({"quiet_hours": None}, base=base)
        assert out.quiet_hours is None

    def test_from_settings_omitting_keeps_base(self, home):
        """不传就别动它 —— 漏掉的后果是「存一次设置就清空安静窗口」。"""
        hours = QuietHours(start=time(22, 0), end=time(8, 0))
        base = Config(quiet_hours=hours, home=str(home))
        out = config_from_settings({"timeout": 20.0}, base=base)
        # 值相等即可 —— 设置往返会走一遍 ["22:00","08:00"] 文本形式，
        # 得到的是等值的新对象，不是同一个引用。
        assert out.quiet_hours == hours


# =============================================================================
# 3. 引擎行为：压后而不丢弃
# =============================================================================
@pytest.fixture()
def quiet_env(tmp_path):
    clock = FrozenClock(datetime(2026, 10, 21, 23, 0))  # 周三 23:00，在窗口内
    app = build_app(
        tmp_path / "agent.db",
        clock=clock,
        energy_windows=EnergyWindows(),
        quiet_hours=None,   # 先不启用，用例自己 set_quiet_hours
    )
    role = app.roles.create("杂项")
    yield app, clock, role
    app.close()


def _reminder(app, role, at: datetime):
    return app.tasks.create(
        "吃药", [role.id], kind=TaskKind.REMINDER, reminder_time=at
    )


class TestEngineQuietBehaviour:
    def test_inside_window_due_is_empty_but_flags_quiet(self, quiet_env):
        app, clock, role = quiet_env
        _reminder(app, role, datetime(2026, 10, 21, 22, 30))
        app.reminders.set_quiet_hours(QuietHours(start=time(22, 0), end=time(8, 0)))
        digest = app.reminders.due()
        assert digest.is_empty                 # 不推
        assert digest.is_quiet                 # 但要说得出「为什么没推」
        assert digest.quiet_until == datetime(2026, 10, 22, 8, 0)
        assert digest.quiet_note() and "压后" in digest.quiet_note()

    def test_no_acknowledge_inside_window_so_it_refires_after(self, quiet_env):
        """窗口内不落令牌 —— 窗口一过这条提醒必须原样再来（压后 ≠ 丢弃）。"""
        app, clock, role = quiet_env
        _reminder(app, role, datetime(2026, 10, 21, 22, 30))
        app.reminders.set_quiet_hours(QuietHours(start=time(22, 0), end=time(8, 0)))
        assert app.reminders.due().is_empty

        clock.set(datetime(2026, 10, 22, 8, 30))   # 窗口结束
        digest = app.reminders.due()
        assert digest.count == 1
        assert "吃药" in digest.text()
        # 22:30 → 08:30 超过 6 小时，如实标「已错过」而不是当刚发生（9.3）
        assert len(digest.missed) == 1

    def test_outside_window_pushes_normally(self, quiet_env):
        app, clock, role = quiet_env
        _reminder(app, role, datetime(2026, 10, 21, 22, 30))
        app.reminders.set_quiet_hours(QuietHours(start=time(1, 0), end=time(6, 0)))
        digest = app.reminders.due()
        assert digest.count == 1 and not digest.is_quiet

    def test_disabled_engine_never_goes_quiet(self, quiet_env):
        app, clock, role = quiet_env
        _reminder(app, role, datetime(2026, 10, 21, 22, 30))
        digest = app.reminders.due()          # quiet_hours 默认 None
        assert digest.count == 1 and not digest.is_quiet

    def test_quiet_note_is_none_when_not_quiet(self):
        from freeagent.services.reminders import ReminderDigest

        assert ReminderDigest().quiet_note() is None

    def test_due_count_is_informational_and_ignores_window(self, quiet_env):
        """due_count 是「到点了多少条」的事实计数，不该被窗口改写。"""
        app, clock, role = quiet_env
        _reminder(app, role, datetime(2026, 10, 21, 22, 30))
        app.reminders.set_quiet_hours(QuietHours(start=time(22, 0), end=time(8, 0)))
        assert app.reminders.due_count() == 1


# =============================================================================
# 4. 三条通道都要把「压后」说出来（不许有一处静默）
# =============================================================================
class TestChannelsSurfaceTheQuietNote:
    """9.8 的诚实纪律：窗口内必须说得出「为什么没推」。

    三处并列各测一次 —— 少了哪一处，那条通道就会安静地「什么都没发生」，
    而用户无从分辨「真没有提醒」和「被窗口压后了」。
    """

    def test_terminal_tick_shows_note(self, quiet_env):
        import io

        from freeagent.cli.app import Repl

        app, clock, role = quiet_env
        _reminder(app, role, datetime(2026, 10, 21, 22, 30))
        app.reminders.set_quiet_hours(QuietHours(start=time(22, 0), end=time(8, 0)))
        buf = io.StringIO()
        repl = Repl(app, out=buf)
        repl.handle("/tick", check_reminders=False)
        assert "安静窗口" in buf.getvalue()

    def test_terminal_tick_still_says_nothing_when_truly_empty(self, quiet_env):
        """没有配置安静窗口时，空就是空 —— 不能凭空冒出一句窗口提示。"""
        import io

        from freeagent.cli.app import Repl

        app, clock, role = quiet_env
        buf = io.StringIO()
        repl = Repl(app, out=buf)
        repl.handle("/tick", check_reminders=False)
        assert "没有到点的提醒" in buf.getvalue()

    def test_web_payload_carries_quiet_note(self, quiet_env):
        from freeagent.web import serialize_api

        app, clock, role = quiet_env
        _reminder(app, role, datetime(2026, 10, 21, 22, 30))
        app.reminders.set_quiet_hours(QuietHours(start=time(22, 0), end=time(8, 0)))
        payload = serialize_api.reminder_payload(app.reminders.due())
        assert payload["count"] == 0
        assert payload["text"] is None
        assert payload["quiet"] and "压后" in payload["quiet"]

    def test_channel_reminder_batch_shows_note_without_confirming(self, quiet_env):
        """飞书侧：说出「压后」，但**不**把 digest 交出去确认。

        若不返回 digest=None，确认那一步会把（空的）令牌落库 —— 窗口过后
        这条提醒就不会再来了，那正是「压后变丢弃」的 bug。
        """
        from freeagent.services.channel import ChannelService

        app, clock, role = quiet_env
        _reminder(app, role, datetime(2026, 10, 21, 22, 30))
        app.reminders.set_quiet_hours(QuietHours(start=time(22, 0), end=time(8, 0)))
        channel = ChannelService(app)
        batch = channel.due_reminders()
        assert batch.text and "安静窗口" in batch.text
        assert batch.digest is None                 # 不交接 → 不会落令牌
        channel.confirm_reminders(batch)            # 空操作

        clock.set(datetime(2026, 10, 22, 8, 30))    # 窗口过后仍然推得出来
        assert "吃药" in (channel.due_reminders().text or "")


