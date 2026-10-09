"""``/ask`` 那份**只读**权限配置的守卫（设计文档 11.10.3 的 ``read`` 桶）。

## 这份配置为什么单独锁

11.10.6 说 ``/ask`` 「不问任何审批」，理由是「它不改任何东西，所以没有
『有副作用』可拦」。**那句话是个假设**：V1 未命中规则时大多默认 ``allow``
（见 11.8.1 的版本陷阱表）。

所以「不改任何东西」必须由**配置强制**。这里锁的就是那个强制：
``edit`` / ``bash`` 必须是 ``deny``，而**不是** ``ask`` —— ``/ask`` 没有闸门，
``ask`` 等于挂死在那儿等人点，而这条命令的设计是「随手用」。

如果这份配置哪天退化成 ``ask``，``/ask`` 就从「问一句」变成
「每一步都停下来问一个没人会答的问题」，而用户看到的症状只是
「怎么没反应」—— 不会有人联想到权限配置。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from freeagent.services.executors import (
    UnverifiedExecutorError,
    V1_FIELD_NAMES,
    adapter_for,
)
from freeagent.services.opencode_server import (
    OpenCodeServer,
    build_isolated_config,
    read_only_permission_config,
)

READ_ONLY = adapter_for(1).build_read_only_config()      # type: ignore[union-attr]
DELEGATION = adapter_for(1).build_permission_config()    # type: ignore[union-attr]
_RULES = READ_ONLY[V1_FIELD_NAMES["container"]]


class TestCannotTouchAnything:
    """「不改任何东西」的实现 —— 这几条是全部安全性所在。"""

    def test_edit_is_denied(self):
        assert _RULES[V1_FIELD_NAMES["edit"]] == "deny"

    def test_shell_is_denied(self):
        """官方 V1 明说 shell 带宿主机**文件/进程/网络**权限。"""
        assert _RULES[V1_FIELD_NAMES["shell"]] == "deny"

    def test_external_directory_is_denied(self):
        assert _RULES[V1_FIELD_NAMES["external_dir"]] == "deny"

    def test_subagent_is_denied(self):
        """11.10.4：不因为「预算还够」而放行执行器拉子代理。"""
        assert _RULES[V1_FIELD_NAMES["subagent"]] == "deny"

    def test_outbound_is_denied(self):
        """出网等于把问题送到别处；``read`` 桶只含「只读地看这个项目」。"""
        assert _RULES[V1_FIELD_NAMES["web_fetch"]] == "deny"
        assert _RULES[V1_FIELD_NAMES["web_search"]] == "deny"

    def test_nothing_writable_is_left_at_ask(self):
        """⚠️ **一条都不许是 ``ask``**。

        ``/ask`` 没有闸门 —— 没有人会来点。所以 ``ask`` 在这里的实际含义是
        「挂死」，而不是「更安全」。这一条把整份配置里的 ``ask`` 一次扫掉，
        免得将来加键时漏看一眼。
        """
        left = {k: v for k, v in _RULES.items() if v == "ask"}
        assert not left, f"只读配置里不该有 ask（会挂死，没人点）：{left}"


class TestRuleOrderMatters:
    """V1 是 ``last matching rule wins`` —— 键序错了就是**静默开洞**。"""

    def test_wildcard_allow_is_first(self):
        keys = list(_RULES)
        assert keys[0] == "*", f"通配必须放最前，实际是 {keys}"

    def test_every_deny_comes_after_the_wildcard(self):
        """deny 放中间会被后面的规则覆盖 —— 官方是**后置覆盖**，不是前置。"""
        keys = list(_RULES)
        star = keys.index("*")
        for k, v in _RULES.items():
            if v == "deny":
                assert keys.index(k) > star, f"{k} 的 deny 排在通配之前，会被覆盖"


class TestDistinctFromDelegation:
    """两份配置**必须**分得开 —— 混用就是闸门失效。"""

    def test_delegation_still_asks_for_edit(self):
        """对照：委派那份是 ``ask``（有人点），只读那份是 ``deny``（没人点）。"""
        rules = DELEGATION[V1_FIELD_NAMES["container"]]
        assert rules[V1_FIELD_NAMES["edit"]] == "ask"

    def test_the_two_configs_are_not_the_same_object(self):
        """每次调用返回**新** dict —— 共用同一个可被就地改掉。"""
        again = adapter_for(1).build_read_only_config()   # type: ignore[union-attr]
        assert again is not READ_ONLY
        again[V1_FIELD_NAMES["container"]]["edit"] = "allow"
        assert _RULES[V1_FIELD_NAMES["edit"]] == "deny", "改一份影响了另一份"


class TestUnverifiedVersionRefuses:
    def test_v2_read_only_config_raises(self):
        """V2 那份**同样**要抛。

        而它比委派那份**更**不能猜：``/ask`` 的全部安全性就是
        「edit/bash 一律 deny」，猜错的形状若让 deny 落空，
        ``/ask`` 就是无人监督的代码执行 —— 而它的设计是「随手用」。
        """
        v2 = adapter_for(2)
        assert v2 is not None and v2.verified is False
        with pytest.raises(UnverifiedExecutorError):
            v2.build_read_only_config()      # type: ignore[union-attr]


class TestInjectionIntoTheIsolatedConfig:
    """``/ask`` 与委派**共用同一个起服务的地方**，靠注入区分。

    所以要锁住两件事：默认那份**没变**（委派仍是 ``ask``），
    以及注入的那份**真的进了配置文件**（否则 ``/ask`` 静默拿到委派那份）。
    """

    def test_default_is_still_the_delegation_config(self):
        """不传参数 ⇒ 委派那份（``edit: ask``）。

        这条是为了「新增参数没顺手改掉默认」—— 那种改动会让**所有**既有
        委派突然失去审批，而症状是「agent 不问了」，看起来像变好了。
        """
        rules = json.loads(build_isolated_config())
        rules = rules[V1_FIELD_NAMES["container"]]
        assert rules[V1_FIELD_NAMES["edit"]] == "ask", "默认被改掉了"

    def test_injected_read_only_reaches_the_config_text(self):
        """注入的只读配置**真的**进了要落盘的那份 JSON。"""
        text = build_isolated_config(read_only_permission_config())
        rules = json.loads(text)[V1_FIELD_NAMES["container"]]
        assert rules[V1_FIELD_NAMES["edit"]] == "deny"
        assert rules[V1_FIELD_NAMES["shell"]] == "deny"

    def test_server_keeps_the_injected_config(self):
        """``OpenCodeServer`` 真的把它存下来了（不是丢了、也不是换了）。

        断言私有属性是这里唯一可行的做法：``start()`` 会起**真进程**，
        而那不是单测该做的事。用一个不存在的可执行文件让它必然起不来，
        于是在构造之后、``start()`` 之前检查即可。
        """
        cfg = read_only_permission_config()
        server = OpenCodeServer(
            project=pathlib.Path("."), executable="__no_such_exe__",
            permission_config=cfg,
        )
        assert server._permission_config is cfg

    def test_delegation_server_defaults_to_none(self):
        """委派那条路不传参数 ⇒ ``None`` ⇒ ``build_isolated_config`` 用默认。

        用 ``None`` 作默认值而不是直接把委派那份塞进去，是为了让
        「这个服务是走哪一份」这件事在代码里看得见。
        """
        server = OpenCodeServer(
            project=pathlib.Path("."), executable="__no_such_exe__",
        )
        assert server._permission_config is None