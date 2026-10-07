"""执行期逐次授权（设计文档 11.8.1）的回归测试。

三组：
  A. 纯函数 —— 隔离配置 / SSE 解析 / 载荷形状 / reply 映射
  B. 「只有发起人能批」—— 跨用户点卡必须被拒，且给的是**准确**的话
  C. 闭环 —— 假 opencode 服务 + 假 sender，跑通「挂起→发卡→等→回答案」

C 组是重点：不接真 opencode 也能锁住**协议形状**。真 opencode 升级改了
端点或载荷时，这里会红 —— 而单测全绿但协议已经不匹配，是最难发现的那种坏。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.services.approval import (  # noqa: E402
    ApprovalStore,
    Decision,
    _same_person,
)
from freeagent.delegate import ServerFactory  # noqa: E402
from freeagent.services.opencode_server import (  # noqa: E402
    build_isolated_config,
    delegation_permission_config,
    parse_sse_event,
    permission_from_event,
    reply_payload,
)


# ── A. 纯函数 ─────────────────────────────────────────────────────────── #

class TestIsolatedConfig:
    def test_deny_is_last(self) -> None:
        """**deny 必须放最后** —— V1 是 last-match-wins，顺序错了就静默开洞。"""
        perm = delegation_permission_config()["permission"]
        keys = list(perm)
        assert keys[0] == "*", f"通配必须在最前，实际 {keys}"
        assert keys[-1] == "external_directory", f"deny 必须在最后，实际 {keys}"
        assert perm["external_directory"] == "deny"
        assert perm["task"] == "deny"

    def test_mutating_actions_ask(self) -> None:
        """改文件 / 跑命令 / 出网 —— 逐次授权闸门真正要拦的。"""
        perm = delegation_permission_config()["permission"]
        for action in ("edit", "bash", "webfetch", "websearch"):
            assert perm[action] == "ask", f"{action} 必须是 ask"

    def test_read_only_stays_allowed(self) -> None:
        """read/grep/glob 全 ask 会让 agent 一直问 —— 问到最后就是无脑点。"""
        perm = delegation_permission_config()["permission"]
        assert perm["*"] == "allow"

    def test_serialises_as_v1_object(self) -> None:
        """必须是 V1 的**对象**形态（V2 是数组）。"""
        text = build_isolated_config()
        got = json.loads(text)
        assert isinstance(got["permission"], dict), "V1 要对象，不是数组"
        # 键序必须保真，否则 deny 就不在最后了
        assert list(json.loads(text)["permission"])[-1] == "external_directory"

    def test_no_secrets_in_config(self) -> None:
        """配置里不该有口令之类 —— 它会随日志/备份流出去。"""
        text = build_isolated_config().lower()
        for word in ("password", "secret", "token", "api_key", "apikey"):
            assert word not in text, f"隔离配置里不该出现 {word}"


class TestParseSseEvent:
    def test_parses_data_line(self) -> None:
        line = 'data: {"type":"permission.asked","properties":{"id":"per_1"}}'
        assert parse_sse_event(line) == ("permission.asked", {"id": "per_1"})

    @pytest.mark.parametrize("line", [
        "", ":heartbeat", "event: ping", "  ", "data:", "data: not-json",
        "data: [1,2]", "data: {}", 'data: {"properties":{}}',
    ])
    def test_ignores_non_events(self, line: str) -> None:
        """心跳/空行/半包/非法 JSON 一律忽略，**不许炸掉整个流**。"""
        assert parse_sse_event(line) is None


class TestPermissionFromEvent:
    def test_extracts_real_shape(self) -> None:
        """用实测拿到的真载荷形状（metadata.diff 真的在里头）。"""
        got = permission_from_event({
            "id": "per_abc",
            "sessionID": "ses_1",
            "permission": "edit",
            "patterns": ["Users\\me\\proj\\ANSWER.txt"],
            "metadata": {
                "filepath": "C:\\me\\proj\\ANSWER.txt",
                "diff": "Index: x\n@@ -0,0 +1 @@\n+42",
            },
            "always": ["*"],
            "tool": {"messageID": "msg_1", "callID": "call_1"},
        })
        assert got is not None
        assert got.request_id == "per_abc"
        assert got.permission == "edit"
        assert got.diff is not None and "+42" in got.diff
        assert "C:\\me\\proj\\ANSWER.txt" in got.paths
        assert got.suggested_always == ("*",)

    @pytest.mark.parametrize("props", [
        None, "string", {}, {"id": ""}, {"permission": "edit"},
        {"id": "per_1", "permission": ""}, {"id": 1, "permission": "edit"},
    ])
    def test_unusable_returns_none(self, props) -> None:
        """认不出就返回 ``None``，**绝不用空串凑一个**。

        上层要靠这个区分「无法回应」（该报错）与「已拒绝」（该继续跑）。
        两者混起来的后果是 agent 干等、人这边什么都不知道。
        """
        assert permission_from_event(props) is None

    def test_survives_missing_metadata(self) -> None:
        got = permission_from_event({"id": "per_1", "permission": "bash"})
        assert got is not None and got.diff is None and got.paths == ()


class TestReplyPayload:
    def test_allow_maps_to_once(self) -> None:
        """本地 allow → opencode ``once``。**刻意不映射 always**（不做永久授权）。"""
        assert reply_payload("allow") == {"reply": "once"}

    def test_deny_maps_to_reject(self) -> None:
        assert reply_payload("deny") == {"reply": "reject"}

    def test_is_object_with_reply_key(self) -> None:
        """实测：裸字符串 → 400 Expected object；{"action":…} → Missing key。"""
        for decision in ("allow", "deny"):
            body = reply_payload(decision)
            assert isinstance(body, dict)
            assert "reply" in body
            assert set(body) == {"reply"}

    def test_rejects_unknown_decision(self) -> None:
        """未知结论必须炸 —— 静默当成 allow 是最坏的一种错。"""
        with pytest.raises(ValueError):
            reply_payload("maybe")


# ── B. 只有发起人能批 ──────────────────────────────────────────────────── #

@pytest.fixture()
def store():
    from freeagent.app import build_app
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        app = build_app(Path(d) / "a.db")
        yield ApprovalStore(app.conn)
        app.close()


class TestOnlyRequesterMayAnswer:
    def test_requester_can_allow(self, store: ApprovalStore) -> None:
        p = store.ask("改代码", requested_by="ou_owner")
        assert store.resolve(p.credential, "allow", decided_by="ou_owner") is True
        assert store.decide(p.credential) == "allow"

    def test_other_user_cannot_allow(self, store: ApprovalStore) -> None:
        """白名单里的**另一个人**点了 → 不许生效。"""
        p = store.ask("改代码", requested_by="ou_owner")
        assert store.resolve(p.credential, "allow", decided_by="ou_guest") is False
        assert store.decide(p.credential) is None, "越权点击不该留下任何结论"

    def test_other_user_cannot_deny_either(self, store: ApprovalStore) -> None:
        """拒绝也锁 —— 否则别人能「替」发起人否掉，卡片变成 DoS。"""
        p = store.ask("改代码", requested_by="ou_owner")
        assert store.resolve(p.credential, "deny", decided_by="ou_guest") is False

    def test_can_answer_gives_distinct_answer(self, store: ApprovalStore) -> None:
        """桥接靠它说**准确**的话：不是发起人 vs 已决定过 vs 已过期。"""
        p = store.ask("改代码", requested_by="ou_owner")
        assert store.can_answer(p.credential, "ou_owner") is True
        assert store.can_answer(p.credential, "ou_guest") is False

    def test_legacy_row_without_requester_is_answerable(self, store) -> None:
        """旧行没有 requested_by → **不拦**。

        加列之前的待批项不该因为新规则而永远点不动（那会让人以为卡片坏了）。
        """
        p = store.ask("老待批项", requested_by=None)
        assert store.can_answer(p.credential, "ou_anyone") is True
        assert store.resolve(p.credential, "allow", decided_by="ou_anyone") is True

    def test_same_person_across_id_shapes(self, store: ApprovalStore) -> None:
        """open_id 与租户级 user_id 是**两个串** —— 必须是同一个人。

        踩过的坑（实测）：白名单两种都收，只按单边比会把
        「他自己点自己发起的卡」判成越权。那种误拒比不拦更糟：
        它让人以为闸门坏了。
        """
        open_id = "ou_7d1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b"
        user_id = open_id[3:]        # 租户级 user_id 是后缀
        p = store.ask("改代码", requested_by=open_id)
        assert store.resolve(p.credential, "allow", decided_by=user_id) is True

    @pytest.mark.parametrize("a,b,expected", [
        ("ou_abc12345", "ou_abc12345", True),
        ("ou_x", "ou_x", True),
        ("ou_12345678", "12345678", True),
        ("", "ou_12345678", False),
        ("ou_12345678", "", False),
        ("ou_aaaaaaaa", "ou_bbbbbbbb", False),
        ("ou_12345678", "ou_123456789", False),   # 前缀不算同一人
    ])
    def test_same_person_matrix(self, a, b, expected) -> None:
        assert _same_person(a, b) is expected

    def test_short_suffix_does_not_match(self) -> None:
        """短到可能巧合的后缀不放行 —— 那等于给越权开后门。"""
        assert _same_person("ou_abcdefgh", "ou_zzzzzzzz_h") is False


# ── C. 闭环（假服务 + 假 sender）─────────────────────────────────────── #

class _FakeServer:
    """只实现 run_with_tool_gate 用到的方法。

    刻意**不**用 mock 魔法：形状对不上就会在这里炸，
    而不是等到真机上炸（真机上炸要人点一张卡才能发现）。
    """

    def __init__(self, events, *, replies=None) -> None:
        self._events = events
        self.replies = list(replies or [])
        self.prompts: list[tuple[str, str]] = []
        #: 最近一次 ``prompt_async`` 收到的三个「用哪个」字段。
        #: 存在是为了让测试能断言它们真的被透传了 —— 签名跟上只防崩溃，
        #: 防不住「传了但传的是空串」。
        self.last_options: dict[str, str] = {}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.closed = True

    def create_session(self) -> str:
        return "ses_fake"

    def prompt_async(self, session_id, brief, *, model="", agent="",
                       variant="", directory=None):
        # ``agent`` / ``variant`` 是飞书四段选择接进来的（2026-10-07）。
        # 签名跟着 :meth:`OpenCodeServer.prompt_async` 走，缺一个就是
        # 「委派时 TypeError」—— 34 条测试一起红，读起来像大范围回归，
        # 实际只是替身没跟上。
        self.prompts.append((session_id, brief))
        self.last_options = {"model": model, "agent": agent, "variant": variant}

    def events(self, **kw):
        yield from self._events

    def reply_permission(self, request_id, decision, *, directory=None) -> None:
        self.replies.append((request_id, decision))


class _FakeSender:
    def __init__(self, *, auto: Decision = "allow") -> None:
        # 显式标注属性类型：只写 ``self.auto = auto`` 时检查器会推成 ``str``，
        # 于是下面 ``resolve(credential, self.auto, …)`` 报「str 不能赋给 Decision」。
        self.auto: Decision = auto
        self.cards: list[dict[str, Any]] = []
        self._store: ApprovalStore | None = None

    def bind(self, store: ApprovalStore) -> None:
        self._store = store

    def send_tool_card(
        self, *, open_id: str, subject: str, detail: str,
        credential: str, ttl_seconds: int,
    ) -> str:
        self.cards.append({"open_id": open_id, "subject": subject,
                           "detail": detail, "credential": credential,
                           "ttl": ttl_seconds})
        if self._store is not None and self.auto:
            self._store.resolve(credential, self.auto, decided_by=open_id)
        return "om_test"


def _asked(rid: str, *, perm: str = "edit", diff: str = "+42") -> tuple[str, Any]:
    return ("permission.asked", {
        "id": rid, "sessionID": "ses_fake", "permission": perm,
        "patterns": ["C:/p/x.txt"],
        "metadata": {"filepath": "C:/p/x.txt", "diff": diff},
        "always": ["*"],
    })


class _StubTask:
    id = "task_fake"
    project_path = "C:/p"


class TestToolGateLoop:
    def _run(self, server, sender, store, approver="ou_owner", **policy_kw):
        from freeagent.delegate import run_with_tool_gate
        from freeagent.services.delegate import DelegationPolicy
        sender.bind(store)
        factory: ServerFactory = lambda project, command: server  # noqa: E731
        policy = DelegationPolicy(
            projects=("C:/p",),
            # 默认给一个模型（模拟 config.json 里写了），让 ``**policy_kw``
            # 能覆盖它。反过来（``policy_kw.setdefault``）就没法测「显式
            # 传 model 覆盖配置」这条了 —— 而那正是选择生效的路径。
            **{"model": "opencode/big-pickle", **policy_kw},
        )
        return run_with_tool_gate(
            _StubTask(), Path("C:/p"), "做点事",
            policy=policy,
            store=store, sender=sender, approver=approver,
            server_factory=factory,
        )

    def test_chosen_agent_model_variant_reach_opencode(
            self, store: ApprovalStore) -> None:
        """飞书里选的「工作模式 / 模型 / 推理档」真的一路送到 opencode。

        ## 为什么这条测试重要

        这是**接缝**测试。四段选择把值存进 ``oc_selection.json``，
        ``DelegationPolicy`` 读它，``run_with_tool_gate`` 把它传给
        ``prompt_async`` —— 三处都对、接缝漏一处，症状都是
        「我明明选了 High，实际没生效」，而且**没有任何报错**
        （opencode 对不存在的 variant 也返回 204）。

        而签名跟上只防「崩溃」，防不住「传的是空串」——
        那正是最可能的漏法，所以这里断言**值**而不只是「不报错」。
        """
        server = _FakeServer([("session.idle", {})])
        self._run(server, _FakeSender(), store,
                  agent="Sisyphus - ultraworker",
                  model="opencode/fledge-alpha-free",
                  variant="high")
        assert server.last_options == {
            "model": "opencode/fledge-alpha-free",
            "agent": "Sisyphus - ultraworker",
            "variant": "high",
        }

    def test_unselected_options_are_empty_strings(
            self, store: ApprovalStore) -> None:
        """没选就传空串 —— **不是**传 None，也不是省略。

        空串在 :meth:`prompt_async` 里被当作「没给」而不带进载荷；
        传 None 会变成 ``variant: null``，而 OpenCode 那边「不存在的档」
        与「不指定」行为不同，且它**不报错** —— 于是无从察觉。
        """
        server = _FakeServer([("session.idle", {})])
        self._run(server, _FakeSender(), store)
        assert server.last_options["agent"] == ""
        assert server.last_options["variant"] == ""

    def test_full_loop_allow(self, store: ApprovalStore) -> None:
        server = _FakeServer([_asked("per_1"), ("session.idle", {})])
        sender = _FakeSender(auto="allow")
        out = self._run(server, sender, store)
        assert out.ok is True, out.summary
        assert server.replies == [("per_1", "allow")]
        assert len(sender.cards) == 1
        assert server.closed is True, "退出必须收口进程组"

    def test_full_loop_deny(self, store: ApprovalStore) -> None:
        server = _FakeServer([_asked("per_1"), ("session.idle", {})])
        out = self._run(server, _FakeSender(auto="deny"), store)
        assert server.replies == [("per_1", "deny")]

    def test_card_shows_the_diff(self, store: ApprovalStore) -> None:
        """没有 diff，用户点的是「信任」不是「确认」。"""
        server = _FakeServer([_asked("per_1"), ("session.idle", {})])
        sender = _FakeSender(auto="allow")
        self._run(server, sender, store)
        detail = sender.cards[0]["detail"]
        assert "+42" in detail, f"卡上必须有 diff，实际：{detail[:200]}"
        assert "edit" in detail
        assert "再问一次" in detail or "仅这一次" in detail

    def test_every_action_asks_again(self, store: ApprovalStore) -> None:
        """两次动作 = **两张卡**。一次批准不覆盖下一次。"""
        server = _FakeServer([
            _asked("per_1"), _asked("per_2", perm="bash"), ("session.idle", {}),
        ])
        sender = _FakeSender(auto="allow")
        out = self._run(server, sender, store)
        assert len(sender.cards) == 2, "第二次动作必须再问一次"
        assert server.replies == [("per_1", "allow"), ("per_2", "allow")]
        assert len({c["credential"] for c in sender.cards}) == 2, \
            "两次必须是**不同**凭据，否则一张卡能批两次"

    def test_no_sender_refuses(self, store: ApprovalStore) -> None:
        """没有飞书通道 → **拒绝派发**。

        绝不降级成「无人值守跑」—— 那正是闸门形同虚设的样子。
        """
        from freeagent.delegate import run_with_tool_gate
        from freeagent.services.delegate import DelegationPolicy
        server = _FakeServer([("session.idle", {})])
        factory: ServerFactory = lambda project, command: server  # noqa: E731
        out = run_with_tool_gate(
            _StubTask(), Path("C:/p"), "做点事",
            policy=DelegationPolicy(projects=("C:/p",)),
            store=store, sender=None, approver="ou_owner",
            server_factory=factory,
        )
        assert out.ok is False
        assert "没有飞书通道" in out.summary
        assert server.replies == [], "被拒的委派不该发任何指令"

    def test_unusable_event_does_not_pretend_denied(self, store) -> None:
        """认不出的挂起请求：不回答案、也不谎称已拒。

        混成「已拒绝」会让 agent 干等；谎称「已回」更糟。
        """
        server = _FakeServer([("permission.asked", {"junk": 1}),
                              ("session.idle", {})])
        sender = _FakeSender(auto="allow")
        out = self._run(server, sender, store)
        assert out.ok is True
        assert server.replies == [], "认不出就不能瞎回"
        assert sender.cards == []

    def test_non_requester_click_does_not_allow(self, store, monkeypatch) -> None:
        """别人点 → opencode 收到的是 **reject**（不是 allow）。

        ⚠️ 这条**必须注入短 TTL**：越权点击被拒后那一行**仍未决**（这是对的 ——
        卡片该继续留给真正的发起人点），于是会一路等到 TTL 到期才降级成拒绝。
        不注入的话这条测试会真等 ``DELEGATE_TTL_SECONDS``（1800s）而挂住。
        注入后走的是**真实的过期降级**路径，不是把 wait 替掉。
        """
        import freeagent.services.approval as appr
        monkeypatch.setattr(appr, "DELEGATE_TTL_SECONDS", 1)

        server = _FakeServer([_asked("per_1"), ("session.idle", {})])
        sender = _FakeSender(auto="allow")
        original = _FakeSender.send_tool_card

        def hijack(self, *, open_id, subject, detail, credential, ttl_seconds):
            self.cards.append({"credential": credential, "open_id": open_id,
                               "ttl": ttl_seconds})
            self._store.resolve(credential, "allow", decided_by="ou_guest")
            return "om_test"

        _FakeSender.send_tool_card = hijack  # type: ignore[method-assign]
        try:
            self._run(server, sender, store, approver="ou_owner")
        finally:
            _FakeSender.send_tool_card = original  # type: ignore[method-assign]
        assert server.replies == [("per_1", "deny")], \
            "越权点击必须变成拒绝，绝不能变成放行"

    def test_server_error_becomes_failure(self, store: ApprovalStore) -> None:
        """服务起不来 → 记失败，**不能**当成功。"""
        from freeagent.services.opencode_server import ServerError

        class _Boom:
            def __enter__(self):
                raise ServerError("起不来")

            def __exit__(self, *exc):
                return None

        from freeagent.delegate import run_with_tool_gate
        from freeagent.services.delegate import DelegationPolicy
        factory: ServerFactory = lambda project, command: _Boom()  # noqa: E731
        out = run_with_tool_gate(
            _StubTask(), Path("C:/p"), "做点事",
            policy=DelegationPolicy(projects=("C:/p",)),
            store=store, sender=_FakeSender(), approver="ou_owner",
            server_factory=factory,
        )
        assert out.ok is False
        assert "起不来" in out.summary


class TestTtlIsOneNumber:
    """**卡上的 TTL 与实际等待时长必须是同一个数。**

    用户是**按卡上那个时间**做决定的（11.9.4 同一条纪律），所以两处
    不等就是「卡上写一套、实际做另一套」。

    踩过的坑（真机端到端跑出来的）：原实现把 ``ApprovalContext`` **对象**
    传给了 ``ApprovalPolicy.for_context()``（它要的是场景**名**），于是落到
    「认不出的场景一律最严」分支，TTL 变成 **0** —— 卡上写着「0s 内有效」。
    而 ``store.wait()`` 另用自己的默认 600s，于是出现「卡上 0 秒、
    实际等了 10 分钟」。

    所以断言的是「**两者相等**」且「**为正**」—— 只断言「不报错」的话，
    0 秒那种 bug 照样漏过去。
    """

    def _ttl_and_wait(self, store, monkeypatch):
        """跑一次，把 (卡上 TTL, wait 实际用的 timeout) 都抓出来。"""
        from freeagent.delegate import _ask_one_tool
        from freeagent.services.approval import ApprovalContext
        from freeagent.services.opencode_server import ToolPermission

        seen: dict[str, int] = {}
        real_wait = ApprovalStore.wait

        # ``should_stop`` 必须列出：它是为了「用户能中途叫停」加的，
        # 而这个 spy 只关心 TTL 的值。写成 **kwargs 的话签名与真实实现
        # 脱钩 —— 生产代码改错了它也不会红。
        def spy_wait(self, credential, *, poll_seconds=0.5,
                     timeout_seconds=0, should_stop=None):
            seen["wait_timeout"] = timeout_seconds
            return "allow"

        monkeypatch.setattr(ApprovalStore, "wait", spy_wait)
        sender = _FakeSender(auto="allow")
        sender.bind(store)
        req = ToolPermission(request_id="per_1", permission="edit",
                             paths=("C:/p/x",), diff="+1")
        _ask_one_tool(_NullOC(), req, store=store, sender=sender,
                      approver="ou_owner",
                      context=ApprovalContext("remote", who="ou_owner"))
        return seen, sender.cards[0]["ttl"] if sender.cards else None

    def test_card_ttl_is_positive(self, store, monkeypatch) -> None:
        """卡上 TTL **必须为正**。0 = 「一出生就过期」，是 bug 不是策略。"""
        _, card_ttl = self._ttl_and_wait(store, monkeypatch)
        assert card_ttl is not None
        assert card_ttl > 0, f"卡上 TTL={card_ttl} —— 用户看到的是「{card_ttl}s 内有效」"

    def test_card_ttl_equals_wait_timeout(self, store, monkeypatch) -> None:
        """两处必须相等 —— 不等就是「卡上写一套、实际做另一套」。"""
        seen, card_ttl = self._ttl_and_wait(store, monkeypatch)
        assert "wait_timeout" in seen, "wait 没被调到"
        assert card_ttl == seen["wait_timeout"], (
            f"卡上 {card_ttl}s、实际等 {seen['wait_timeout']}s —— "
            "用户按卡上那个时间决定，却拿到另一个时限"
        )

    def test_remote_ttl_is_the_delegate_one(self, store, monkeypatch) -> None:
        """remote 档就该用 DELEGATE_TTL_SECONDS（1800），不是默认的 600。"""
        from freeagent.services.approval import DELEGATE_TTL_SECONDS
        seen, card_ttl = self._ttl_and_wait(store, monkeypatch)
        assert card_ttl == DELEGATE_TTL_SECONDS
        assert seen["wait_timeout"] == DELEGATE_TTL_SECONDS


class _NullOC:
    def reply_permission(self, request_id, decision, *, directory=None):
        pass


class TestSchemaMigration:
    def test_requested_by_column_exists(self, store: ApprovalStore) -> None:
        cols = {r[1] for r in store._conn.execute(
            "PRAGMA table_info(pending_approvals)").fetchall()}
        assert "requested_by" in cols, "迁移没加上 requested_by"

    def test_row_to_obj_tolerates_missing_column(self) -> None:
        """旧行读不出来会连带把「加列」变成「炸库」。"""
        from freeagent.services.approval import _row_to_obj
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (credential TEXT, subject TEXT, detail TEXT,"
                     " asked_at TEXT, expires_at TEXT, decision TEXT,"
                     " decided_by TEXT, decided_at TEXT, open_message_id TEXT)")
        row = conn.execute("SELECT * FROM t").fetchone()
        # 构造一个真实形状的旧行
        conn.execute("INSERT INTO t VALUES ('c','s','d','2026-01-01T00:00:00',"
                     "'2026-01-01T00:00:00',NULL,NULL,NULL,NULL)")
        got = _row_to_obj(conn.execute("SELECT * FROM t").fetchone())
        assert got.requested_by is None
        assert row is None or True


def test_init_schema_is_idempotent() -> None:
    """迁移可重复跑（设计文档要求：必须能在已有库上原地跑）。"""
    import tempfile
    from freeagent.app import build_app
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "a.db"
        app = build_app(db)
        app.close()
        app2 = build_app(db)   # 第二次开：走 migrate
        app2.close()
        app3 = build_app(db)   # 第三次：再 migrate 一次
        conn = app3.conn
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(pending_approvals)").fetchall()}
        assert "requested_by" in cols
        app3.close()
