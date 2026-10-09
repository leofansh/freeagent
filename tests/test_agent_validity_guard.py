"""工作模式失效守卫（设计文档 11.13.8 / 11.14）的回归测试。

核心要防的故障：用户选了一个后来消失的 agent（项目级 agent 被删、
OpenCode 升级后内置 agent 改名），而 OpenCode 侧对「不存在的 agent」
是**查表取不到就静默用默认**——于是「派出去了，但用的不是你选的模式」。
本测试锁住两端：

  - 失效的 agent → 委派**响亮失败**（不是静默退化）；
  - 有效的 / 空的 agent → 照常进行，且不误杀。

闭环用假 opencode 服务，不接真进程。
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.delegate import run_with_tool_gate  # noqa: E402
from freeagent.domain.models import Role  # noqa: E402
from freeagent.services.approval import ApprovalStore  # noqa: E402
from freeagent.services.delegate import DelegationPolicy  # noqa: E402


_VALID_AGENTS = [
    {"name": "Sisyphus - ultraworker", "mode": "primary",
     "model": {"providerID": "opencode", "modelID": "big"}},
]


class _FakeServer:
    """记录 opencode 实际收到的指令；list_agents 可被测试注入。"""

    def __init__(self, agents) -> None:
        self._agents = list(agents)
        self.aborted: list[str] = []
        self.prompted = False

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def create_session(self) -> str:
        return "ses_fake"

    def list_agents(self) -> list[dict]:
        return self._agents

    def prompt_async(self, session_id, brief, *, model="", agent="",
                     variant="", directory=None) -> None:
        self.prompted = True

    def events(self, **kw):
        yield ("session.idle", {})

    def abort(self, session_id) -> None:
        self.aborted.append(session_id)


class _StubSender:
    """本测试路径不触发任何发卡；若被调用立即暴露。"""

    def send_question_card(self, **kw) -> str:
        raise AssertionError("守卫测试不该走到发卡")

    def send_tool_card(self, **kw) -> str:
        raise AssertionError("守卫测试不该走到发卡")


def _dispatch(agent: str, *, agents=_VALID_AGENTS, tmp_path):
    app = build_app(tmp_path / "a.db")
    store = ApprovalStore(app.conn)
    oc = _FakeServer(agents)
    task = type("T", (), {"id": "t1"})()
    got = run_with_tool_gate(
        task, str(tmp_path), "做点什么",
        policy=DelegationPolicy(command="opencode", agent=agent),
        store=store, sender=_StubSender(), approver="tester",
        server_factory=lambda *a, **k: oc,
    )
    app.close()
    return got, oc


class TestAgentValidityGuard:
    def test_valid_agent_proceeds(self, tmp_path):
        got, oc = _dispatch("Sisyphus - ultraworker", tmp_path=tmp_path)
        assert got.ok is True, got.summary
        assert oc.prompted is True
        assert oc.aborted == []

    def test_empty_agent_skips_check(self, tmp_path):
        # agent 为空时根本不查，也不会误杀。
        got, oc = _dispatch("", tmp_path=tmp_path)
        assert got.ok is True, got.summary
        assert oc.prompted is True
        assert oc.aborted == []

    def test_invalid_agent_fails_loudly(self, tmp_path):
        got, oc = _dispatch("Ghost - gone", tmp_path=tmp_path)
        assert got.ok is False, "失效的 agent 必须响亮失败，而非静默退化"
        assert "失效" in got.summary, got.summary
        assert oc.prompted is False, "失效时不应真的派发给 opencode"
        assert oc.aborted == ["ses_fake"]

    def test_unreachable_agent_list_does_not_kill(self, tmp_path):
        # 取不到列表（连不上）≠ agent 失效：应放行而不是误杀。
        class _Broken(_FakeServer):
            def list_agents(self):
                raise RuntimeError("连不上")

        app = build_app(tmp_path / "a.db")
        store = ApprovalStore(app.conn)
        oc = _Broken(_VALID_AGENTS)
        task = type("T", (), {"id": "t1"})()
        got = run_with_tool_gate(
            task, str(tmp_path), "做点什么",
            policy=DelegationPolicy(command="opencode", agent="Ghost - gone"),
            store=store, sender=_StubSender(), approver="tester",
            server_factory=lambda *a, **k: oc,
        )
        app.close()
        assert got.ok is True, got.summary
        assert oc.prompted is True


class TestRoleDefaultAgent:
    def test_field_exists_and_defaults_none(self) -> None:
        ts = datetime.datetime(2026, 10, 1, 12)
        role = Role(id="r1", name="编码", created_at=ts, updated_at=ts)
        assert role.default_agent is None

    def test_field_can_be_set(self) -> None:
        ts = datetime.datetime(2026, 10, 1, 12)
        role = Role(id="r1", name="编码", created_at=ts, updated_at=ts,
                    default_agent="Sisyphus - ultraworker")
        assert role.default_agent == "Sisyphus - ultraworker"
