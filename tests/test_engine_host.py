"""常驻宿主的规矩：复用、重建、收口、失败要响亮（21.1 / V1.114）。

替身（:class:`FakeServer`）只实现宿主真正会用到的那几个方法 ——
多实现一个就多一处「测试能过、真机不认」的空间。
"""

from __future__ import annotations

import pathlib
from datetime import datetime

import pytest

from freeagent.services import engine_host as host_mod
from freeagent.services.clock import FrozenClock
from freeagent.services.opencode_server import ServerError, auto_dir_permission_config


class FakeServer:
    """假服务器。**记录被怎么用的**，而不是假装能干活。"""

    instances: list["FakeServer"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kw = kwargs
        self.started = False
        self.closed = 0
        self.healthy_calls = 0
        self.healthy_answer = True
        self.prompts: list[tuple[str, str, dict[str, str]]] = []
        self.aborts = 0
        FakeServer.instances.append(self)

    def start(self) -> "FakeServer":
        self.started = True
        return self

    def create_session(self) -> str:
        return "ses-1"

    def healthy(self) -> bool:
        self.healthy_calls += 1
        return self.healthy_answer

    @property
    def pid(self) -> int:
        return 4242

    def prompt_async(self, session: str, text: str, **kw: str) -> None:
        self.prompts.append((session, text, dict(kw)))

    def events(self, *, timeout: float = 600.0):  # noqa: ANN201 - 替身
        return iter(())

    def abort(self, session: str) -> None:
        self.aborts += 1

    def close(self) -> None:
        self.closed += 1


@pytest.fixture()
def host(tmp_path: pathlib.Path) -> host_mod.EngineHost:
    FakeServer.instances.clear()
    return host_mod.EngineHost(
        server_factory=FakeServer,
        clock=FrozenClock(datetime(2026, 9, 26, 9, 0)),
    )


def _effects(cfg: dict) -> dict[str, str]:
    """把 V1 的 dict / V2 的数组都归一成「动作 → 结论」。

    测试不该因为主版本换形状而变红或变绿 —— 它要验的是**语义**：
    写类放宽了、越界仍然拒绝。
    """
    if isinstance(cfg.get("permission"), dict):
        return dict(cfg["permission"])
    return {r["action"]: r["effect"] for r in cfg.get("permissions", [])}


class TestEnsure:
    def test_first_ensure_spawns_one_server(self, host, tmp_path) -> None:
        session = host.ensure(directory=str(tmp_path))
        assert session == "ses-1"
        assert len(FakeServer.instances) == 1
        server = FakeServer.instances[0]
        assert server.started is True
        # 常驻引擎必须看见用户真实的插件与凭据：不隔离，闸门靠覆盖层。
        assert server.kw["isolate"] is False
        assert server.kw["apply_gate"] is True
        # 默认档不给权限配置 —— 那就是既有委派闸门（逐次问人）。
        assert server.kw["permission_config"] is None
        assert host.state().status == host_mod.READY
        assert host.state().pid == 4242

    def test_same_directory_is_reused(self, host, tmp_path) -> None:
        host.ensure(directory=str(tmp_path))
        host.ensure(directory=str(tmp_path))
        assert len(FakeServer.instances) == 1
        assert FakeServer.instances[0].closed == 0

    def test_switch_directory_closes_the_old_one(self, host, tmp_path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        host.ensure(directory=str(tmp_path / "a"))
        host.ensure(directory=str(tmp_path / "b"))
        assert len(FakeServer.instances) == 2
        # 先收口再起：两个服务同时写同一份项目覆盖层会互相踩。
        assert FakeServer.instances[0].closed == 1
        assert FakeServer.instances[1].closed == 0

    def test_unhealthy_server_is_rebuilt(self, host, tmp_path) -> None:
        host.ensure(directory=str(tmp_path))
        FakeServer.instances[0].healthy_answer = False
        host.ensure(directory=str(tmp_path))
        assert len(FakeServer.instances) == 2
        assert FakeServer.instances[0].closed == 1

    def test_auto_mode_passes_a_lazy_getter(self, host, tmp_path) -> None:
        # 权限配置的键名随主版本变，而版本要等服务自己探出来 ——
        # 所以自动档给的是**可调用对象**，不是当场算好的 dict。
        host.ensure(directory=str(tmp_path), mode=host_mod.ENGINE_AUTO_DIR)
        cfg = FakeServer.instances[0].kw["permission_config"]
        assert callable(cfg)
        effects = _effects(cfg())
        assert effects.get("external_directory") == "deny"
        assert effects.get("task") == "deny"
        assert effects.get("*") == "ask"

    def test_auto_mode_widens_only_the_writing_actions(self) -> None:
        effects = _effects(auto_dir_permission_config())
        assert effects.get("edit") == "allow"
        assert effects.get("bash") == "allow"
        # 出网不是「在目录里干活」：它的后果落在目录外。
        assert effects.get("webfetch") == "ask"
        assert effects.get("websearch") == "ask"

    def test_mode_change_forces_a_restart(self, host, tmp_path) -> None:
        # 授权档是**项目级覆盖层**：同一个服务上换档，旧规则会继续生效
        # —— 而「界面显示自动、实际仍在逐次问」是最难查的一类不一致。
        host.ensure(directory=str(tmp_path), mode=host_mod.ENGINE_ASK)
        host.ensure(directory=str(tmp_path), mode=host_mod.ENGINE_AUTO_DIR)
        assert len(FakeServer.instances) == 2

    def test_relative_directory_is_refused(self, host, tmp_path) -> None:
        with pytest.raises(ValueError, match="绝对路径"):
            host.ensure(directory="src")

    def test_missing_directory_is_refused(self, host, tmp_path) -> None:
        with pytest.raises(ValueError, match="不存在"):
            host.ensure(directory=str(tmp_path / "nope"))

    def test_unknown_mode_is_refused(self, host, tmp_path) -> None:
        with pytest.raises(ValueError, match="授权档"):
            host.ensure(directory=str(tmp_path), mode="yolo")

    def test_spawn_failure_leaves_an_error_and_a_reason(self, host, tmp_path) -> None:
        def boom(**_kw: object) -> FakeServer:
            raise ServerError("opencode 不在")

        host._factory = boom  # 走真实的失败路径
        with pytest.raises(ServerError, match="起引擎失败"):
            host.ensure(directory=str(tmp_path))
        state = host.state()
        assert state.status == host_mod.ERROR
        assert "opencode 不在" in state.error
        assert state.session_id == ""

    def test_failure_after_start_still_closes_the_process(self, host, tmp_path) -> None:
        class HalfBaked(FakeServer):
            def create_session(self) -> str:
                raise ServerError("会话建不出来")

        FakeServer.instances.clear()
        host2 = host_mod.EngineHost(
            server_factory=HalfBaked,
            clock=FrozenClock(datetime(2026, 9, 26, 9, 0)),
        )
        with pytest.raises(ServerError):
            host2.ensure(directory=str(tmp_path))
        # 起了但没建出会话 = 一个没人管的进程；必须当场收口。
        assert HalfBaked.instances[0].closed == 1


class TestLifecycle:
    def test_prompt_folds_newlines(self, host, tmp_path) -> None:
        host.ensure(directory=str(tmp_path))
        host.prompt("第一行\n第二行")
        # 含换行会被 opencode 判成复杂任务并升级它自己的模型 —— 折行不丢内容。
        assert FakeServer.instances[0].prompts == [
            ("ses-1", "第一行 第二行", {"model": "", "agent": "", "variant": ""})
        ]

    def test_prompt_before_ensure_is_loud(self, host) -> None:
        with pytest.raises(ServerError, match="还没就绪"):
            host.prompt("随便说一句")

    def test_halt_closes_and_clears(self, host, tmp_path) -> None:
        host.ensure(directory=str(tmp_path))
        host.halt()
        assert FakeServer.instances[0].closed == 1
        state = host.state()
        assert state.status == host_mod.STOPPED
        assert state.session_id == "" and state.directory == ""
        assert state.pid == 0

    def test_halt_is_idempotent(self, host, tmp_path) -> None:
        host.ensure(directory=str(tmp_path))
        host.halt()
        host.halt()
        assert FakeServer.instances[0].closed == 1
