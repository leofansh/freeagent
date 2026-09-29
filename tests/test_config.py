"""配置读写与运行期替换。

三组关注点：

1. **安全边界**：Key 永不落盘 —— 且是靠类型防住的，不是靠约定。
2. **不可信输入**：界面传来的东西必须逐字段解析、给人话错误。
3. **热替换**：设置页改完立即生效，不靠重启（但 Key 换不了，见下）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from freeagent.app import build_app
from freeagent.config import (
    API_KEY_ENV,
    Config,
    config_from_settings,
    config_path,
    load_config,
    overridden_by_env,
    save_config,
)
from freeagent.domain import TaskKind, ValidationError
from freeagent.services.sorting import EnergyWindows
from freeagent.storage.db import connect, resolve_db_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """这些测试关心配置优先级，必须先清掉可能残留的环境变量。"""
    for name in (
        "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_TIMEOUT",
        "FREEAGENT_RULES_ONLY", "FREEAGENT_ALLOW_FALLBACK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(API_KEY_ENV, raising=False)


@pytest.fixture
def home(tmp_path):
    return tmp_path


# =============================================================================
# 安全：Key 永不落盘
# =============================================================================
class TestKeyNeverPersisted:
    def test_config_has_no_api_key_field(self):
        """``Config`` 根本没有 ``api_key`` 字段 —— 它是现读环境变量的 property。

        所以「把 Key 存进配置文件」在类型层面就不可能发生，
        少一层依赖少一个错。
        """
        assert "api_key" not in {f.name for f in Config.__dataclass_fields__.values()}

    def test_save_config_writes_no_key(self, home):
        config = Config(model="m", home=str(home))
        path = save_config(config)
        raw = path.read_text(encoding="utf-8")
        assert "api_key" not in raw and "sk-" not in raw

    def test_save_config_never_writes_llm_enabled(self, home):
        """``llm_enabled`` 由环境变量单方面决定。

        写进文件会造成「界面关了、文件里还开着」的错觉。
        """
        path = save_config(Config(llm_enabled=True, home=str(home)))
        assert "llm_enabled" not in path.read_text(encoding="utf-8")

    def test_saved_config_round_trips(self, home):
        windows = EnergyWindows(morning=(6, 11), evening=(19, 23))
        save_config(
            Config(
                model="deepseek-reasoner",
                base_url="https://example.com/v1",
                timeout=45.0,
                energy_windows=windows,
                allow_fallback=False,
                home=str(home),
            )
        )
        loaded = load_config(home)
        assert loaded.model == "deepseek-reasoner"
        assert loaded.base_url == "https://example.com/v1"
        assert loaded.timeout == 45.0
        assert loaded.allow_fallback is False
        assert loaded.energy_windows.morning == (6, 11)
        assert loaded.energy_windows.evening == (19, 23)

    def test_omitting_energy_windows_means_disabled(self, home):
        """不写 ``energy_windows`` 应当读成「未启用」，不是崩。"""
        save_config(Config(home=str(home)))
        assert load_config(home).energy_windows is None

    def test_config_path_sits_next_to_db(self, home):
        assert config_path(home) == home / "config.json"


class TestHomeEnvWhitespace:
    """``FREEAGENT_HOME`` 的尾随空白。

    踩过的坑（真实踩到，不是设想的）：冒烟时在 cmd 里写
    ``set FREEAGENT_HOME=%TEMP%\\x && python ...``，那个 ``&&`` 前的空格
    被算进了值里，于是得到 ``...\\x \\agent.db``。Windows 会**剥掉路径组件
    末尾的空格**，结果：

    - ``mkdir`` 建的目录名和实际用的路径对不上
    - ``is_dir()`` 报存在、``os.access(W_OK)`` 报可写（都是假象）
    - ``sqlite3.connect()`` 抛 ``unable to open database file`` —— 一句
      完全不含线索的话，排查成本极高

    所以这里既要测「解析结果被 strip 了」，也要测「真能连上」：
    只测字符串的话，很容易留下一个「strip 了但仍连不上」的洞。
    """

    def test_trailing_space_is_stripped(self, tmp_path, monkeypatch):
        target = tmp_path / "data"
        monkeypatch.setenv("FREEAGENT_HOME", f"{target} ")
        assert config_path().parent == target
        assert resolve_db_path().parent == target

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_blank_means_unset(self, blank, monkeypatch):
        """空白值当没设，回落到 ``~/.freeagent`` —— 别建出名为空白的目录。"""
        monkeypatch.setenv("FREEAGENT_HOME", blank)
        assert config_path() == Path.home() / ".freeagent" / "config.json"
        assert resolve_db_path() == Path.home() / ".freeagent" / "agent.db"

    def test_unset_falls_back_to_home(self, monkeypatch):
        monkeypatch.delenv("FREEAGENT_HOME", raising=False)
        assert config_path() == Path.home() / ".freeagent" / "config.json"

    def test_actually_connectable_with_trailing_space(self, tmp_path, monkeypatch):
        """真连一次。这才是原始症状所在，光断言字符串不够。"""
        target = tmp_path / "data"
        monkeypatch.setenv("FREEAGENT_HOME", f"{target} ")
        conn = connect(resolve_db_path())
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()

    def test_db_and_config_land_in_same_dir(self, tmp_path, monkeypatch):
        """两处解析必须一致，否则「库在 A、配置在 B」比单个路径错更难查。"""
        target = tmp_path / "data"
        monkeypatch.setenv("FREEAGENT_HOME", f"{target} ")
        assert resolve_db_path().parent == config_path().parent


# =============================================================================
# 不可信输入
# =============================================================================
class TestConfigFromSettings:
    def test_partial_update_keeps_other_fields(self, home):
        base = Config(model="a", base_url="https://x.dev", timeout=12.0,
                      home=str(home))
        out = config_from_settings({"timeout": 30.0}, base=base)
        assert out.timeout == 30.0
        assert out.model == "a" and out.base_url == "https://x.dev"

    @pytest.mark.parametrize("bad", [
        {"model": "  "},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": 601},
        {"timeout": "abc"},
        {"base_url": "ftp://x"},
        {"base_url": "example.com"},
        {"energy_windows": "yes"},
        {"energy_windows": {"morning": [10]}},
        {"energy_windows": {"morning": [10, 2]}},
        {"energy_windows": {"morning": [-1, 5]}},
        {"energy_windows": {"morning": [5, 25]}},
        {"unknown_field": 1},
    ])
    def test_rejects_bad_input_with_readable_message(self, home, bad):
        base = Config(home=str(home))
        with pytest.raises(ValidationError) as ei:
            config_from_settings(bad, base=base)
        assert str(ei.value), "错误必须给出原因，不能是空消息"

    def test_rejects_api_key_loudly(self, home):
        """塞 Key 必须**报错**，不能静默忽略。

        静默忽略更危险：用户会以为 Key 已经存好了。

        这条在改成「界面可填 Key」之后**依然成立**，只是含义变了：正常路径上
        web 端点会先把 ``api_key`` 摘走写进 ``llm.env``，所以走到这里只可能是
        「写入流程漏了一步」。那正是更该炸的时刻 —— 静默忽略会让用户看着界面上
        「已保存」，而 Key 其实压根没存。
        """
        with pytest.raises(ValidationError) as ei:
            config_from_settings(
                {"api_key": "sk-oops"}, base=Config(home=str(home))
            )
        message = str(ei.value)
        assert message, "错误必须给出原因，不能是空消息"
        # 必须说清是**哪个环节**出的问题，而不是继续说「这里不能填 Key」——
        # 那会把用户引到已经不存在的地方（去设环境变量）。
        assert "llm.env" in message, "要指明 Key 本该写到哪，否则用户不知道该改哪"
        # 错误文案里绝不能出现他刚填的那个 Key。
        assert "sk-oops" not in message, "错误信息里回显了用户刚填的 Key"

    def test_trailing_slash_is_normalized(self, home):
        out = config_from_settings(
            {"base_url": "https://x.dev/v1/"}, base=Config(home=str(home))
        )
        assert out.base_url == "https://x.dev/v1"

    def test_energy_true_means_default_bands(self, home):
        out = config_from_settings(
            {"energy_windows": True}, base=Config(home=str(home))
        )
        assert out.energy_windows == EnergyWindows()

    def test_energy_false_means_disabled(self, home):
        base = Config(energy_windows=EnergyWindows(), home=str(home))
        out = config_from_settings({"energy_windows": False}, base=base)
        assert out.energy_windows is None


# =============================================================================
# 环境变量覆盖：必须如实告诉用户
# =============================================================================
class TestEnvOverride:
    def test_reports_overridden_fields(self, home, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_MODEL", "from-env")
        monkeypatch.setenv("DEEPSEEK_TIMEOUT", "99")
        config = load_config(home)
        assert set(overridden_by_env(config)) == {"model", "timeout"}

    def test_no_override_when_env_absent(self, home):
        assert overridden_by_env(load_config(home)) == []

    def test_env_beats_file(self, home, monkeypatch):
        save_config(Config(model="from-file", timeout=10.0, home=str(home)))
        monkeypatch.setenv("DEEPSEEK_MODEL", "from-env")
        monkeypatch.setenv("DEEPSEEK_TIMEOUT", "99")
        config = load_config(home)
        assert config.model == "from-env" and config.timeout == 99.0
        # 文件里的值没丢，只是不生效 —— 去掉环境变量就恢复
        monkeypatch.delenv("DEEPSEEK_MODEL")
        monkeypatch.delenv("DEEPSEEK_TIMEOUT")
        assert load_config(home).model == "from-file"

    def test_rules_only_reported_as_override(self, home, monkeypatch):
        monkeypatch.setenv("FREEAGENT_RULES_ONLY", "1")
        assert "llm_enabled" in overridden_by_env(load_config(home))


# =============================================================================
# 热替换
# =============================================================================
class TestApplyConfig:
    def test_energy_windows_take_effect_without_restart(self, home):
        """改精力档位后打分必须**真的变** —— 这才叫「立即生效」。

        用一个能被档位区分开的场景：任务提醒在 23:00。
        默认档位（晚上 18–24）会命中「晚上」；把晚上收窄到 18–22 后，
        23:00 落在所有档位之外，ENERGY_FIT 信号必须消失。
        """
        clock = _Midnight()
        app = build_app(home / "agent.db", clock=clock)
        role = app.roles.create("工作")
        task = app.tasks.create(
            "深夜提醒", [role.id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 26, 23, 0),
        )
        app.tasks.schedule(task.id, date(2026, 9, 26))

        def energy_reasons() -> list[str]:
            return [
                s.reason
                for item in app.today.view().items
                if item.task.id == task.id
                for s in item.signals
                if s.code.value == "energy_fit"
            ]

        # 默认配置里精力档位是**关闭**的，所以先启用
        app.apply_config(
            config_from_settings({"energy_windows": True}, base=load_config(home))
        )
        assert any("晚上" in r for r in energy_reasons()), "启用默认档位应命中晚上"

        # 不重启，直接收窄晚上档位：23:00 落到所有档位之外
        app.apply_config(
            config_from_settings(
                {"energy_windows": {"morning": [5, 12], "afternoon": [12, 18],
                                    "evening": [18, 22]}},
                base=load_config(home),
            )
        )
        assert energy_reasons() == [], "收窄档位后 23:00 不该再命中"
        app.close()

    def test_disabling_energy_windows_removes_signal(self, home):
        clock = _Midnight()
        app = build_app(home / "agent.db", clock=clock)
        role = app.roles.create("工作")
        task = app.tasks.create("有事做", [role.id])
        app.tasks.schedule(task.id, date(2026, 9, 26))
        assert app.today.view().items, "排了期就该出现在今天视图"

        app.apply_config(
            config_from_settings({"energy_windows": True}, base=load_config(home))
        )
        app.apply_config(
            config_from_settings({"energy_windows": None}, base=load_config(home))
        )
        assert app.energy_windows is None
        app.close()

    def test_apply_config_rebuilds_provider(self, home):
        app = build_app(home / "agent.db")
        before = app.llm_name
        app.apply_config(Config(model="deepseek-reasoner", home=str(home)))
        assert app.llm_name == before, "没 Key 时仍是规则层（不静默换实现）"
        app.close()

    def test_saved_file_matches_applied_config(self, home):
        app = build_app(home / "agent.db")
        new = config_from_settings(
            {"model": "m2", "timeout": 33.0}, base=load_config(home)
        )
        save_config(new)
        app.apply_config(new)
        assert app.config.model == "m2"
        reloaded = load_config(home)
        assert (reloaded.model, reloaded.timeout) == ("m2", 33.0)
        app.close()


class _Midnight:
    """固定在深夜 23 点 —— 用来区分「晚上」档位命中与否。"""

    def now(self) -> datetime:
        return datetime(2026, 9, 26, 23, 0)

    def today(self) -> date:
        return date(2026, 9, 26)
