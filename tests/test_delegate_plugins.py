"""委派实例要不要加载用户 opencode 插件（``delegate.plugins``）。

## 为什么这个开关存在

V1.31 起委派跑的是**系统 opencode**（看得见用户真实配置），于是用户原生的
插件、MCP、技能、agent 全都进来了 —— 功能上是对的，代价是三样：

1. **插件代码在闸门内运行**。``tool.execute.before/after`` 钩子**不受
   permission 管**，所以闸门只管得住「工具」，管不住插件自己。
2. **agent 人格被换掉**。本机 steering 插件把 agent 变成
   ``Sisyphus - ultraworker``（实测：会话的 ``agent`` / ``mode`` 都是它）。
3. **每轮约 2 万 input tokens**。实测回一句「OK」烧 20805 input tokens。

## 为什么默认是 ``inherit`` 而不是更保守的 ``off``

改默认会**静默削掉用户正在用的工具**（本机就有 codegraph / context7 /
grep_app / lsp / websearch 五个 MCP 服务器），那是功能回退，不该由实现替
用户决定。而安全性**不靠这个开关** —— 执行期闸门已经失败关闭（11.8.1 的
``*: ask``）。所以这里是「给选择 + 默认不变」，不是「默认更严」。

## 为什么未知取值要报错

``true`` / ``none`` / ``""`` 都能被读成布尔或空值。一旦把未知值当成
「继承」，用户以为自己关了插件、实际插件全在，而**界面上看不出任何异常**
—— 那是最难发现的一类失效。所以配置层与纯函数两层都失败关闭。
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from freeagent.config import (
    DEFAULT_PLUGIN_MODE,
    PLUGIN_MODES,
    Config,
    config_from_settings,
    load_config,
    save_config,
)
from freeagent.delegate import _default_server_factory, plugin_extra_env
from freeagent.domain import ValidationError
from freeagent.services.delegate import DelegationPolicy


def _write_config(home: Path, payload: dict) -> None:
    (home / "config.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# =============================================================================
# 纯函数：模式 → 子进程环境变量
# =============================================================================
class TestPluginExtraEnv:
    def test_off_sets_opencode_pure(self):
        """``off`` → ``OPENCODE_PURE=1``。

        实测有效：带这个变量起实例，``GET /mcp`` 返回空 —— 插件注入的
        5 个 MCP 服务器全部消失。变量名不是笔误，opencode 自己就叫 pure
        （``packages/opencode/src/flag/flag.ts`` 的 ``OPENCODE_PURE``）。
        """
        assert plugin_extra_env("off") == {"OPENCODE_PURE": "1"}

    def test_inherit_passes_nothing(self):
        """``inherit`` → **空 dict**，刻意不传任何变量。

        传一个「什么都不做」的变量看起来更对称，但那样就分不清
        「忘了配」与「配了等于没配」。
        """
        assert plugin_extra_env("inherit") == {}

    @pytest.mark.parametrize("mode", ["true", "false", "none", "", "OFF", "0"])
    def test_unknown_mode_fails_closed(self, mode):
        """未知取值**报错**，不当成 inherit。

        这是本文件最要紧的一条：把 ``true`` 静默当成「继承」的话，用户以为
        关了插件、实际插件全在，且界面上看不出任何异常。
        """
        with pytest.raises(ValueError, match="未知的插件模式"):
            plugin_extra_env(mode)

    def test_error_names_both_legal_values(self):
        with pytest.raises(ValueError) as exc:
            plugin_extra_env("nope")
        message = str(exc.value)
        assert "inherit" in message and "off" in message, (
            "报错要说出路（12.7.2 的 R3）：得让人知道能填哪两个值"
        )


# =============================================================================
# 配置：默认、取值校验、往返
# =============================================================================
class TestConfigPlugins:
    def test_default_is_inherit(self, tmp_path):
        """没写就是 ``inherit`` —— 保持 V1.31 以来的行为，功能不退化。"""
        cfg = load_config(home=tmp_path)
        assert cfg.delegate_plugins == DEFAULT_PLUGIN_MODE == "inherit"

    def test_explicit_off_is_read(self, tmp_path):
        _write_config(tmp_path, {"delegate": {"plugins": "off"}})
        assert load_config(home=tmp_path).delegate_plugins == "off"

    @pytest.mark.parametrize("mode", ["true", "none", "", "OFF", "1"])
    def test_bad_mode_is_rejected_at_load(self, tmp_path, mode):
        """错值在**读配置时**就炸，不留到派发那一刻。

        派发时炸的代价是：一条委派走到起实例才失败，而配置早就该拦。
        """
        _write_config(tmp_path, {"delegate": {"plugins": mode}})
        with pytest.raises(ValidationError) as exc:
            load_config(home=tmp_path)
        message = str(exc.value)
        assert "plugins" in message
        assert "inherit" in message and "off" in message, "要说清能填哪两个"

    def test_legal_modes_are_exactly_these_two(self):
        """枚举只有两个值，且默认是其中之一 —— 别哪天悄悄加了第三个。"""
        assert PLUGIN_MODES == ("inherit", "off")
        assert DEFAULT_PLUGIN_MODE in PLUGIN_MODES

    def test_round_trips_through_save(self, tmp_path):
        """存了要能读回来，否则「配了没生效」极难排查。"""
        save_config(Config(delegate_plugins="off", home=str(tmp_path)))
        assert load_config(home=tmp_path).delegate_plugins == "off"

    def test_policy_carries_the_mode(self, tmp_path):
        """配置 → 策略：闸门执行时拿到的是同一个值。"""
        _write_config(tmp_path, {"delegate": {"plugins": "off"}})
        policy = load_config(home=tmp_path).delegate_policy()
        assert policy.plugins == "off"

    def test_policy_default_when_unset(self):
        """连配置都不读也要有确定行为（测试里大量直接造策略）。"""
        assert DelegationPolicy().plugins == "inherit"


# =============================================================================
# 设置页不许碰它
# =============================================================================
class TestSettingsPageCannotTouchIt:
    def test_settings_merge_carries_it_from_base(self, tmp_path):
        """在设置页存一次**别的**设置，不能顺手把插件模式改了。

        与 ``delegate.projects`` 同一个理由：那是执行期行为，界面不该顺手带走。
        """
        base = Config(delegate_plugins="off", home=str(tmp_path))
        merged = config_from_settings({"model": "m"}, base=base)
        assert merged.delegate_plugins == "off"

    def test_settings_page_cannot_set_it(self, tmp_path):
        """想从界面改它 ⇒ 报「不认识的设置项」，而不是默默接受。"""
        base = Config(home=str(tmp_path))
        with pytest.raises(ValidationError, match="不认识的设置项"):
            config_from_settings({"plugins": "off"}, base=base)


# =============================================================================
# 工厂接线：模式真的进了子进程环境，且闸门两种模式都开着
# =============================================================================
class TestFactoryWiring:
    def test_off_reaches_the_child_env(self):
        """模式 → ``extra_env`` → 子进程。这一段曾经没人接。"""
        server = _default_server_factory(Path("."), "opencode", plugins="off")
        assert server._extra_env == {"OPENCODE_PURE": "1"}

    def test_inherit_reaches_nothing(self):
        server = _default_server_factory(Path("."), "opencode", plugins="inherit")
        assert server._extra_env == {}

    @pytest.mark.parametrize("mode", PLUGIN_MODES)
    def test_gate_stays_on_in_both_modes(self, mode):
        """⚠️ 关插件**不等于**关闸门 —— 两件事，必须都开着。

        这个开关调节的是「agent 能用什么」，不是「用之前问不问」。
        把它俩绑在一起会让人以为关插件能顺便提高安全。
        """
        server = _default_server_factory(Path("."), "opencode", plugins=mode)
        assert server._apply_gate is True, f"{mode} 模式下闸门被关了"
        assert server._isolate is False, "仍然是「看得见真实配置」那条路"

    def test_production_partial_shape(self):
        """生产接线用的是 ``partial``，这里照抄那一行验一遍。

        ``ServerFactory`` 协议是 ``(project, command)``，所以带默认值的
        关键字参数不能破坏它 —— 否则测试注入的假工厂与真实工厂就分家了。
        """
        make_server = functools.partial(_default_server_factory, plugins="off")
        server = make_server(Path("."), "opencode")   # 只给两个位置参数
        assert server._extra_env == {"OPENCODE_PURE": "1"}

    def test_two_positional_args_still_work(self):
        """不传 plugins（老调用方式）⇒ 继承，且不报错。"""
        server = _default_server_factory(Path("."), "opencode")
        assert server._extra_env == {}
