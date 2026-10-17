"""产物验收闭环（2026-10-17）：把「待你验收」从空头承诺变成能兑现的动作。

## 这个文件在守什么

委派闭环曾经是**断的**，而断点很隐蔽：

* 执行器交回产出 → :meth:`ArtifactService.create_draft` 落成 ``DRAFT``
* :mod:`freeagent.delegate` 在任务上写「执行器已产出结果，待你验收」
* 飞书照这句话推卡

但 ``ArtifactService.accept`` 的唯一调用点在**终端**
（``cli/app.py::_cmd_accept``）。于是：承诺在飞书、兑现要去另一个终端，
任务永远悬在「待验收」—— 而界面上没有任何地方能点掉它。

它同时撞上两条自家规矩：

* **12.1.1「一份能力一份实现」**：验收在三个界面各做一遍，迟早漂移，
  而漂移的方向是「网页上那个按钮点了 404」。
* **``4657ac0`` 记的那条验证纪律：HTTP 成功 ≠ 用户能看到**。
  一个只有终端能点的按钮，等于没有。

## 为什么这些测试必须**过路由表**，不能直接调端点

第一版把端点直接测了，路径拼错一路绿灯 —— 而界面上表现为
「点了没反应」。所以这里有三层：

1. **服务层**：验收的语义本身（幂等、版本链、留痕）；
2. **过 ``dispatch_post``**：路径与动作名对不对；
3. **真起 HTTP**：会话令牌那条路通不通（``test_web_session`` 已覆盖
   机制，这里只补一条「accept 也在保护之下」）。

## 为什么飞书侧只用一条测试

因为飞书**不需要新代码** —— ``/accept`` 加进
``_CARD_COMMAND_ALLOWLIST`` 之后，复用现成的 ``COMMAND_ACTION``
通道：白名单判定、命令路由、去重、回话、收掉按钮全是白送的。
所以这里只钉住那条白名单（防止有人日后把它删掉而不自知），
以及「从通道真能派发」这一条真问题。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from freeagent.app import build_app
from freeagent.domain import ArtifactStatus, FreeAgentError
from freeagent.services.channel import ChannelService
from freeagent.services.clock import FrozenClock
from freeagent.web.routes_write import dispatch_post
from freeagent.web.server import create_server
from freeagent.web.session import SESSION_HEADER, SESSION_TOKEN

import datetime

NOW = datetime.datetime(2026, 10, 17, 9, 0, 0)


@pytest.fixture
def env(tmp_path):
    """一条有草稿产出、还没验收的事务。"""
    clock = FrozenClock(NOW)
    app = build_app(tmp_path / "agent.db", clock=clock)
    role = app.roles.create("工作")
    task = app.tasks.create(
        "改一下 README", [role.id], intent="改一下 README", project_path="D:/p"
    )
    draft = app.artifacts.create_draft(
        task.id, "opencode 产出 10-17 09:00", "改了三处，其中 README 一处要你确认。"
    )
    yield app, task, draft, clock
    app.close()


def _post(app, parts):
    """过路由表发一个**空体** POST —— 验收不需要请求体。"""
    return dispatch_post(app, parts, lambda **kw: {})


# ── 服务层：验收的语义 ──────────────────────────────────────────────────── #

class TestAcceptSemantics:
    def test_accept_flips_status_and_leaves_trace(self, env):
        app, task, draft, _clock = env
        accepted = app.artifacts.accept(draft.id)
        assert accepted.status is ArtifactStatus.ACCEPTED
        kinds = [r.type.value for r in app.record_repo.list_for_task(task.id)]
        assert "artifact_accepted" in kinds, (
            "验收必须留痕 —— TaskRecord 是事务上下文的唯一真源"
        )

    def test_one_accepted_per_task(self, env):
        """再开一版并采纳 → 上一版降为 SUPERSEDED（规则 2）。"""
        app, task, draft, _clock = env
        app.artifacts.accept(draft.id)
        v2 = app.artifacts.revise(task.id, "第二版")
        app.artifacts.accept(v2.id)
        old = app.artifact_repo.get(draft.id)
        assert old.status is ArtifactStatus.SUPERSEDED
        assert app.artifacts.accepted(task.id).version == 2


# ── WEB 端点：过路由表 ─────────────────────────────────────────────────── #

class TestWebEndpoint:
    def test_accept_over_the_route_table(self, env):
        app, task, draft, _clock = env
        got = _post(app, ["api", "task", task.id, "accept"])
        assert got is not None, "路由不认识 accept —— 界面上就是点了 404"
        status, payload = got
        assert status == 200
        assert payload["version"] == 1
        assert payload["status"] == "accepted"
        assert app.artifacts.current(task.id).status is ArtifactStatus.ACCEPTED

    def test_accept_accepts_a_task_id_prefix(self, env):
        """前端拿到的可能只是前缀 —— 与其他任务端点同一套解析。"""
        app, task, draft, _clock = env
        prefix = task.id[:8]
        _post(app, ["api", "task", prefix, "accept"])
        assert app.artifacts.current(task.id).status is ArtifactStatus.ACCEPTED

    def test_reaccepting_says_why_instead_of_silently_passing(self, env):
        """"已经验收过了" 与 "没有产出" 必须给**不同**的话。

        混成一句「没写进去」的话，用户看到的就是「这个按钮坏了」。
        """
        app, task, draft, _clock = env
        _post(app, ["api", "task", task.id, "accept"])
        with pytest.raises(FreeAgentError) as exc:
            _post(app, ["api", "task", task.id, "accept"])
        assert "已经验收过" in str(exc.value)

    def test_accept_without_output_says_so(self, tmp_path):
        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            task = app.tasks.create("还没派过", [role.id])
            with pytest.raises(FreeAgentError) as exc:
                _post(app, ["api", "task", task.id, "accept"])
            assert "还没有产出" in str(exc.value)
        finally:
            app.close()

    def test_unknown_task_id_is_refused(self, env):
        app, _task, _draft, _clock = env
        with pytest.raises(FreeAgentError):
            _post(app, ["api", "task", "deadbeef", "accept"])

    def test_accept_does_not_swallow_other_task_actions(self, env):
        """新增的两条路径不能吃掉原有的状态迁移。

        这是那个「分支顺序」注释的守门测试：把 accept 挪到
        ``is_allowed_action`` 之后，两者会互相吃掉对方。
        """
        app, task, _draft, _clock = env
        got = dispatch_post(
            app, ["api", "task", task.id, "pause"],
            lambda **kw: {},
        )
        assert got is not None, "pause 被验收路径吃掉了"
        assert got[0] == 200

    def test_reopen_needs_an_accepted_version_first(self, env):
        app, task, draft, _clock = env
        with pytest.raises(FreeAgentError) as exc:
            _post(app, ["api", "task", task.id, "reopen"])
        assert "先验收一版" in str(exc.value)
        _post(app, ["api", "task", task.id, "accept"])
        status, payload = _post(app, ["api", "task", task.id, "reopen"])
        assert status == 200
        assert payload["version"] == 2
        assert payload["status"] == "draft"


# ── 端到端：真起 HTTP ──────────────────────────────────────────────────── #

class TestOverRealHttp:
    def test_accept_works_through_the_session_protected_route(self, env):
        """真起一次服务 —— 挡的是「令牌那关没罩住新端点」。

        直调 ``dispatch_post`` 绕过了令牌检查，所以那条路全绿也不代表
        浏览器里能用。这一条走完整链路。
        """
        app, task, draft, _clock = env
        server = create_server(app, host="127.0.0.1", port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/task/{task.id}/accept",
                data=b"",
                method="POST",
                headers={SESSION_HEADER: SESSION_TOKEN},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            assert payload["status"] == "accepted"
            assert app.artifacts.current(task.id).status is ArtifactStatus.ACCEPTED
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ── 飞书侧：复用现成的命令卡片通道 ──────────────────────────────────────── #

class TestFeishuSide:
    def test_accept_is_in_the_narrow_allowlist(self):
        """钉住白名单里有 ``/accept``。

        它**不在**也没关系——那只是少一个按钮；但它在而测试不知道，
        就等于这条纪律没有守。删掉它时这条会红，那正是要的效果。
        """
        from freeagent.feishu.bridge import _CARD_COMMAND_ALLOWLIST

        assert "/accept" in _CARD_COMMAND_ALLOWLIST

    def test_allowlist_stays_narrow(self):
        """加 ``/accept`` 不许顺带放进危险命令。

        按钮 value 是客户端回传的，而 ``ChannelService.handle`` 见
        ``/`` 开头就派发 —— 照单全收等于开一个「点一下执行任意命令」
        的口子。这条断言是那个窄白名单的守门测试。
        """
        from freeagent.feishu.bridge import _CARD_COMMAND_ALLOWLIST

        forbidden = {
            "/delegate", "/merge", "/role-del", "/role-rename",
            "/redispatch", "/reset", "/draft",
        }
        assert not (forbidden & _CARD_COMMAND_ALLOWLIST), (
            "危险命令进了卡片白名单"
        )

    def test_accept_is_dispatchable_from_a_channel(self, env):
        """从通道真能派发 —— 这就是卡片按钮走的那条路。

        ``_run_card_command`` 把按钮合成 ``<cmd> <id>`` 交给
        ``ChannelService.handle``，所以「通道能派发」等价于
        「卡片能工作」。
        """
        app, task, draft, _clock = env
        cs = ChannelService(app, allowed_senders=frozenset({"ou_me"}))
        reply = cs.handle("oc_chat", "ou_me", f"/accept {task.id}")
        assert not reply.denied
        assert app.artifacts.current(task.id).status is ArtifactStatus.ACCEPTED

    def test_a_stranger_cannot_accept_via_a_card(self, env):
        """白名单那道关对验收同样成立 —— 卡片不是后门。"""
        app, task, draft, _clock = env
        cs = ChannelService(app, allowed_senders=frozenset({"ou_owner"}))
        reply = cs.handle("oc_chat", "ou_stranger", f"/accept {task.id}")
        assert reply.denied
        assert app.artifacts.current(task.id).status is ArtifactStatus.DRAFT