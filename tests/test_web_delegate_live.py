"""委派实时交互（设计文档 12.8 / V1.35）：落库投影 + 三端点 + 路由 + 闸门。

## 为什么这组测试要单独一个文件

它跨三层（``delegate.py`` 的投影、``web`` 的端点、``ApprovalStore`` 的闸门），
而每一层单测都覆盖不到跨层的那条真问题：**WEB 点的「允许」到底有没有
落到执行器读的那张表上**。第一版把端点直接测（没走 ``dispatch_post``），
路径拼错一路绿灯 —— 而界面上表现为「点了没反应」。
所以这里既有函数级断言，也有**过路由表**的断言，还有**真起 HTTP 服务**
的端到端一条。

## 时间必须可注入

陈旧判定、TTL、过期全靠时钟。夹具给 :class:`FrozenClock`，
所以「61 秒后判为卡住」「1800 秒后判为过期」都是确定性的，不 sleep。
"""
from __future__ import annotations

import datetime
import json
import threading
import urllib.error
import urllib.request

import pytest

from freeagent.app import build_app
from freeagent.domain import FreeAgentError, RecordType
from freeagent.services.approval import ApprovalStore
from freeagent.services.clock import FrozenClock
from freeagent.web import endpoints_delegate_action as act
from freeagent.web import endpoints_delegate_live as live
from freeagent.web.routes import READ_ROUTES
from freeagent.web.routes_write import dispatch_post
from freeagent.web.server import create_server
from freeagent.web.session import SESSION_HEADER, SESSION_TOKEN

NOW = datetime.datetime(2026, 10, 13, 10, 0, 0)


@pytest.fixture
def env(tmp_path):
    clock = FrozenClock(NOW)
    app = build_app(tmp_path / "agent.db", clock=clock)
    role = app.roles.create("工作")
    task = app.tasks.create(
        "改一下 README", [role.id],
        intent="改一下 README", project_path="D:/p",
    )
    yield app, task, clock
    app.close()


def _store(app, clock) -> ApprovalStore:
    # 刻意注入同一个时钟：**过期判定必须同源**，
    # 否则会出现「界面上还没过期、库里已经拒了」。
    return ApprovalStore(app.conn, clock=clock.now)


def _write_progress(app, clock, task_id, **kw):
    base = dict(status="正在执行", recent="", tool_calls=0, allowed=0,
                project="D:/p")
    base.update(kw)
    app.task_repo.set_delegate_progress(task_id=task_id, now=clock.now(), **base)


# ── 只读快照 ──────────────────────────────────────────────────────────── #

class TestLiveSnapshot:
    def test_empty_is_not_an_error(self, env):
        app, _task, _clock = env
        got = live.delegate_live(app)
        assert got["kind"] == "delegate_live"
        assert got["items"] == []

    def test_poll_seconds_defaults_to_two(self, env):
        app, _task, _clock = env
        assert live.delegate_live(app)["poll_seconds"] == 2.0

    def test_poll_seconds_zero_disables_polling(self, env, monkeypatch):
        app, _task, _clock = env
        monkeypatch.setenv("FREEAGENT_LIVE_POLL_SECONDS", "0")
        assert live.delegate_live(app)["poll_seconds"] == 0.0

    def test_poll_seconds_garbage_falls_back(self, env, monkeypatch):
        """填错**不许拦住启动**：这个数字只影响多快看到更新。"""
        app, _task, _clock = env
        monkeypatch.setenv("FREEAGENT_LIVE_POLL_SECONDS", "快一点")
        assert live.delegate_live(app)["poll_seconds"] == 2.0

    def test_progress_row_is_visible(self, env):
        app, task, clock = env
        _write_progress(app, clock, task.id, status="正在执行",
                        recent="read src/a.py", tool_calls=3, allowed=2)
        item = live.delegate_live(app)["items"][0]
        assert item["task_id"] == task.id
        assert item["status"] == "正在执行"
        assert item["recent"] == "read src/a.py"
        assert (item["tool_calls"], item["allowed"]) == (3, 2)
        assert item["stale"] is False

    def test_stale_after_sixty_seconds(self, env):
        """投影不是真源：**有行不等于在跑**。执行器被强杀时行留在盘上，
        而「有行」当成「在跑」会把一条僵尸委派显示成正在执行。"""
        app, task, clock = env
        _write_progress(app, clock, task.id)
        clock.advance(seconds=61)
        item = live.delegate_live(app)["items"][0]
        assert item["stale"] is True

    def test_broken_timestamp_is_treated_as_stale(self, env):
        """坏时间戳 → 显示成「卡住」。反过来的默认值会把僵尸显示成在跑。"""
        app, task, clock = env
        _write_progress(app, clock, task.id)
        app.conn.execute(
            "UPDATE delegate_progress SET updated_at = ? WHERE task_id = ?",
            ("不是时间", task.id))
        app.conn.commit()
        item = live.delegate_live(app)["items"][0]
        assert item["stale"] is True

    def test_endpoint_has_no_side_effect(self, env):
        """纯只读：每 2 秒调一次的前置条件是**任何**副作用都等于事故
        （`GET /api/reminders` 曾经把提醒吃掉，见 routes._reminders）。"""
        app, task, clock = env
        store = _store(app, clock)
        p = store.request_tool_call(permission="edit", paths=["a"],
                                    task_id=task.id)
        for _ in range(3):
            live.delegate_live(app)
        assert store.decide(p.credential) is None, "读端点把待批项答复掉了"

    def test_stale_open_row_offers_recovery_actions(self, env):
        """12.11.1（V1.39）：陈旧且未结束的委派给「重新派发 / 放弃」按钮。
        这是从「只给文案」改过来的 —— 文案要人去终端，不友好。"""
        app, task, clock = env
        _write_progress(app, clock, task.id)
        clock.advance(seconds=61)
        item = live.delegate_live(app)["items"][0]
        assert item["stale"] is True
        assert item["actions"] == ["redispatch", "drop"]

    def test_fresh_row_has_no_recovery_actions(self, env):
        """正在正常跑的不给按钮 —— 怕误触把一条活着的东西重派/放弃。"""
        app, task, clock = env
        _write_progress(app, clock, task.id)
        assert live.delegate_live(app)["items"][0]["actions"] == []

    def test_finished_row_has_no_recovery_actions(self, env):
        """已结束（DONE/DROPPED）不给按钮：重派会被服务层以「已经做完了」
        拒掉，放一个必然报错的按钮比不放更糟。"""
        app, task, clock = env
        _write_progress(app, clock, task.id)
        app.tasks.drop(task.id, reason="测试")
        clock.advance(seconds=61)
        item = live.delegate_live(app)["items"][0]
        assert item["stale"] is True
        assert item["actions"] == []


# ── 待决项载荷 ────────────────────────────────────────────────────────── #

class TestPendingPayload:
    def test_web_originated_is_decidable(self, env):
        app, task, clock = env
        store = _store(app, clock)
        store.request_tool_call(permission="edit", paths=["a.py"],
                                diff="+x", task_id=task.id)
        card = live.delegate_live(app)["items"][0]["pending"][0]
        assert card["can_decide"] is True
        assert card["task_id"] == task.id

    def test_detail_carries_diff_verbatim(self, env):
        """diff 原样透传。**没有 diff 用户点的就是信任而不是确认**
        —— 截断或重拼都会破坏这件事。"""
        app, task, clock = env
        store = _store(app, clock)
        store.request_tool_call(permission="edit", paths=["a.py"],
                                diff="-old\n+new", task_id=task.id)
        card = live.delegate_live(app)["items"][0]["pending"][0]
        assert "-old" in card["detail"] and "+new" in card["detail"]

    def test_feishu_originated_is_read_only_with_a_way_out(self, env):
        app, task, clock = env
        store = _store(app, clock)
        p = store.request_tool_call(permission="edit", paths=["a"],
                                    task_id=task.id)
        app.conn.execute(
            "UPDATE pending_approvals SET requested_by = 'ou_x' WHERE credential = ?",
            (p.credential,))
        app.conn.commit()
        card = live.delegate_live(app)["items"][0]["pending"][0]
        assert card["can_decide"] is False
        # 12.7.2 R3：拒绝要给出路。说不清去哪儿批 = 让人猜。
        assert "飞书" in card["blocked_reason"]

    def test_decided_and_expired_are_not_listed(self, env):
        """已决定 / 已过期都不该再显示成「等你批」——
        用户点一个必然无效的按钮，比不显示更糟。"""
        app, task, clock = env
        store = _store(app, clock)
        done = store.request_tool_call(permission="edit", paths=["a"],
                                       task_id=task.id)
        store.resolve(done.credential, "allow")
        store.request_tool_call(permission="bash", paths=["ls"],
                                task_id=task.id)
        clock.advance(seconds=1801)
        _write_progress(app, clock, task.id)
        items = live.delegate_live(app)["items"]
        assert len(items) == 1, "在跑的那条该在，但它得没有待批项"
        assert items[0]["pending"] == []

    def test_question_shows_the_next_open_slot(self, env):
        """一次问一个：第二问答完后要显示**下一个问题**，
        而不是把整组重列一遍（用户答的就不是他要答的那个了）。"""
        app, task, clock = env
        store = _store(app, clock)
        q = store.request_question(subject="哪种写法？",
                                   questions=["A 还是 B？", "还是 C？"],
                                   task_id=task.id)
        card = live.delegate_live(app)["items"][0]["pending"][0]
        assert card["kind"] == "question"
        assert card["question"] == "A 还是 B？"
        store.put_answer(q.credential, "A", answered_by="")
        card = live.delegate_live(app)["items"][0]["pending"][0]
        assert card["question"] == "还是 C？"

    def test_fully_answered_question_disappears(self, env):
        """``put_answer`` **不写** decision 列 —— 少这一步过滤，
        面板上会出现一张「等回答」的死胡同卡。"""
        app, task, clock = env
        store = _store(app, clock)
        q = store.request_question(subject="q", questions=["只有一问"],
                                   task_id=task.id)
        store.put_answer(q.credential, "答了", answered_by="")
        _write_progress(app, clock, task.id)
        assert live.delegate_live(app)["items"][0]["pending"] == []

    def test_zombie_row_for_a_deleted_task_does_not_poison_the_panel(self, env):
        """真机 2026-10-18：进度行还在、事务已删 → 面板整体 400。

        根因是端点按「``repo.get`` 返回 None」写的兜底，而 ``get()`` 的
        契约是**抛** ``NotFoundError``（返回 None 的是 ``find()``）。一条
        坏行毒死整个实时面板 —— 好委派的进度也跟着看不见。修法：改用
        ``find()``，坏行降级显示「(事务已不存在)」。
        """
        app, task, clock = env
        # 一行**正常**的 + 一行事务已删的：坏行不许影响好行的显示。
        ghost_id = "task_deleted_with_progress_row_still_there"
        _write_progress(app, clock, task.id, tool_calls=2, allowed=2)
        _write_progress(app, clock, ghost_id, status="正在执行")
        items = live.delegate_live(app)["items"]
        by_id = {i["task_id"]: i for i in items}
        assert by_id[task.id]["title"], "好行必须照常显示"
        assert by_id[ghost_id]["title"] == "(事务已不存在)"

    def test_orphan_pending_is_still_shown(self, env):
        """没有归属的待批项（本地操作类）照样显示 —— 藏起来用户就以为
        系统坏了。"""
        app, _task, clock = env
        store = _store(app, clock)
        store.request_tool_call(permission="edit", paths=["a"])
        items = live.delegate_live(app)["items"]
        assert len(items) == 1 and items[0]["task_id"] == ""
        assert items[0]["status"] == "等你决定"


# ── 写端点 ────────────────────────────────────────────────────────────── #

class TestApprovalAction:
    def _pending(self, env, **kw):
        app, task, clock = env
        store = _store(app, clock)
        opts = dict(permission="edit", paths=["a.py"], task_id=task.id)
        opts.update(kw)
        return store.request_tool_call(**opts)

    def test_allow_writes_the_row(self, env):
        app, _task, clock = env
        p = self._pending(env)
        got = act.approval_action(app, p.credential, {"decision": "allow"})
        assert got["ok"] is True
        assert _store(app, clock).decide(p.credential) == "allow"

    def test_deny_writes_the_row(self, env):
        app, _task, clock = env
        p = self._pending(env)
        act.approval_action(app, p.credential, {"decision": "deny"})
        assert _store(app, clock).decide(p.credential) == "deny"

    def test_decision_must_be_allow_or_deny(self, env):
        app, _task, _clock = env
        p = self._pending(env)
        with pytest.raises(FreeAgentError, match="allow"):
            act.approval_action(app, p.credential, {"decision": "maybe"})

    def test_unknown_field_is_refused(self, env):
        app, _task, _clock = env
        p = self._pending(env)
        with pytest.raises(FreeAgentError, match="不认识这些字段"):
            act.approval_action(app, p.credential,
                                {"decision": "allow", "force": True})

    def test_already_decided_is_refused_with_its_own_words(self, env):
        """「不是发起人 / 已决定过 / 已过期」三件事给三句话。
        混成一句「没写进去」，用户看到的是「这个按钮坏了」。"""
        app, _task, clock = env
        p = self._pending(env)
        act.approval_action(app, p.credential, {"decision": "allow"})
        with pytest.raises(FreeAgentError, match="已经答复过"):
            act.approval_action(app, p.credential, {"decision": "deny"})

    def test_expired_is_refused(self, env):
        app, _task, clock = env
        p = self._pending(env)
        clock.advance(seconds=1801)
        with pytest.raises(FreeAgentError, match="过期"):
            act.approval_action(app, p.credential, {"decision": "allow"})

    def test_feishu_originated_cannot_be_decided_by_web(self, env):
        """**这条是安全边界**：``resolve`` 对空 ``decided_by`` 根本不查，
        所以 WEB 若不自己判，就能用空身份批掉飞书发起的卡
        —— 11.9.7 规则 2「只有发起人能批」当场被击穿。"""
        app, _task, clock = env
        p = self._pending(env)
        app.conn.execute(
            "UPDATE pending_approvals SET requested_by = 'ou_x' WHERE credential = ?",
            (p.credential,))
        app.conn.commit()
        with pytest.raises(FreeAgentError, match="只有发起人能批"):
            act.approval_action(app, p.credential, {"decision": "allow"})

    def test_unknown_credential_says_so(self, env):
        app, _task, _clock = env
        with pytest.raises(FreeAgentError, match="已经不在了"):
            act.approval_action(app, "nope", {"decision": "allow"})


class TestApprovalStop:
    def test_stop_does_both_things(self, env):
        """只回掉当前挂起 → 空等到 TTL；只记停止 → agent 换个方向继续。
        用户点的是「停止这条委派」，两件都该做。"""
        app, task, clock = env
        store = _store(app, clock)
        p = store.request_tool_call(permission="write", paths=["x"],
                                    task_id=task.id)
        act.approval_stop(app, p.credential)
        assert store.is_stop_requested(p.credential) is True
        assert store.decide(p.credential) == "deny"

    def test_stop_twice_says_it_is_already_answered(self, env):
        """第二次点停止会**报错**而不是静默成功 —— 如实说清比假装又做了一次好。

        （真正需要幂等的是 :meth:`ApprovalStore.request_stop` 本身，它
        ``INSERT OR REPLACE``；而端点这道「已答复过」的闸门是有意的：
        它同时挡住了「停止」被当成一次新的 allow/deny 改写。）
        """
        app, task, clock = env
        store = _store(app, clock)
        p = store.request_tool_call(permission="write", paths=["x"],
                                    task_id=task.id)
        act.approval_stop(app, p.credential)
        with pytest.raises(FreeAgentError, match="已经答复过"):
            act.approval_stop(app, p.credential)
        assert store.is_stop_requested(p.credential) is True


class TestQuestionAnswer:
    def _q(self, env, questions):
        app, task, clock = env
        store = _store(app, clock)
        return store.request_question(subject="写法？", questions=questions,
                                      task_id=task.id)

    def test_answer_fills_the_first_slot(self, env):
        app, _task, clock = env
        q = self._q(env, ["A 还是 B？", "还是 C？"])
        got = act.question_answer(app, q.credential, {"text": "A"})
        assert got["slot"] == 0 and got["complete"] is False
        assert _store(app, clock).next_question(q.credential) == "还是 C？"

    def test_answering_everything_reports_complete(self, env):
        app, _task, clock = env
        q = self._q(env, ["A 还是 B？", "还是 C？"])
        act.question_answer(app, q.credential, {"text": "A"})
        got = act.question_answer(app, q.credential, {"text": "C"})
        assert got["complete"] is True
        assert _store(app, clock).is_complete(q.credential)

    def test_blank_answer_is_refused(self, env):
        app, _task, _clock = env
        q = self._q(env, ["x"])
        with pytest.raises(FreeAgentError, match="不能是空的"):
            act.question_answer(app, q.credential, {"text": "   "})

    def test_feishu_question_cannot_be_answered_by_web(self, env):
        app, _task, clock = env
        q = self._q(env, ["x"])
        app.conn.execute(
            "UPDATE pending_approvals SET requested_by = 'ou_x' WHERE credential = ?",
            (q.credential,))
        app.conn.commit()
        with pytest.raises(FreeAgentError, match="只有发起人能"):
            act.question_answer(app, q.credential, {"text": "答"})


# ── 陈旧行的恢复动作（12.11.1 / V1.39）────────────────────────────────── #

def _make_failed(app, task, clock):
    """把一条委派造成「执行期失败」形状（dispatched → failed）——
    这正是 `reset_delegation` 允许重派的三种前提交集之一。"""
    app.record_repo.append(task.id, RecordType.DELEGATION_DISPATCHED,
                           "派出去了", clock.now())
    app.record_repo.append(task.id, RecordType.DELEGATION_FAILED,
                           "挂了", clock.now())


class TestRecoveryActions:
    """陈旧委派的「重新派发 / 放弃」—— 与终端 ``/redispatch``、``/drop``
    共用服务层写入口，所以这里验「端点翻对了话」。"""

    def test_redispatch_marks_a_failed_delegation(self, env):
        app, task, clock = env
        _make_failed(app, task, clock)
        got = act.delegate_redispatch(app, task.id)
        assert got["ok"] is True and got["action"] == "redispatch"
        types = [r.type for r in app.record_repo.list_for_task(task.id)]
        assert RecordType.DELEGATION_RESET in types

    def test_redispatch_of_a_running_delegation_is_refused(self, env):
        """正在跑不能重派 —— 服务层挡（派第二个进程改同一目录会互相覆盖），
        端点如实回话而不是静默成功。"""
        app, task, clock = env
        app.record_repo.append(task.id, RecordType.DELEGATION_DISPATCHED,
                               "派出去了", clock.now())
        with pytest.raises(FreeAgentError, match="正在跑"):
            act.delegate_redispatch(app, task.id)

    def test_drop_lands_as_dropped(self, env):
        app, task, _clock = env
        got = act.delegate_drop(app, task.id)
        assert got["ok"] is True and got["action"] == "drop"
        assert app.tasks.get(task.id).state.value == "dropped"

    def test_recovery_reason_records_web_origin(self, env):
        """审计：记录里要能看出是「从网页面板点的」，与终端操作对账。"""
        app, task, clock = env
        _make_failed(app, task, clock)
        act.delegate_redispatch(app, task.id)
        contents = [r.content for r in app.record_repo.list_for_task(task.id)
                    if r.type is RecordType.DELEGATION_RESET]
        assert any("网页面板" in c for c in contents)


class TestRecoveryRoutes:
    """过路由表 —— 路径写错就是 404（返回 None），而 404 不报错。"""

    def test_redispatch_route_reaches_the_endpoint(self, env):
        app, task, clock = env
        _make_failed(app, task, clock)
        got = dispatch_post(
            app, ["api", "delegate", "tasks", task.id, "redispatch"],
            lambda **kw: {})
        assert got is not None, "路径没接上（None = 404，界面上是点了没反应）"
        assert got[0] == 200 and got[1]["action"] == "redispatch"

    def test_drop_route_reaches_the_endpoint(self, env):
        app, task, clock = env
        _make_failed(app, task, clock)
        got = dispatch_post(
            app, ["api", "delegate", "tasks", task.id, "drop"], lambda **kw: {})
        assert got is not None and got[1]["action"] == "drop"

    def test_unknown_action_would_be_404(self, env):
        app, task, _clock = env
        assert dispatch_post(
            app, ["api", "delegate", "tasks", task.id, "restart"],
            lambda **kw: {}) is None, "未知动作不该被路由层假装接住"


# ── 路由表（路径写错就是 404，而 404 不报错）──────────────────────────── #

class TestRoutes:
    def test_live_is_registered_as_get(self):
        assert "/api/delegate/live" in READ_ROUTES

    def test_approval_route_reaches_the_endpoint(self, env):
        app, task, clock = env
        store = _store(app, clock)
        p = store.request_tool_call(permission="edit", paths=["a"],
                                    task_id=task.id)
        got = dispatch_post(app, ["api", "delegate", "approvals", p.credential],
                            lambda **kw: {"decision": "allow"})
        assert got is not None, "路径没接上（None = 404，界面上是点了没反应）"
        assert got[0] == 200 and got[1]["decision"] == "allow"

    def test_stop_route_reaches_the_endpoint(self, env):
        app, task, clock = env
        store = _store(app, clock)
        p = store.request_tool_call(permission="write", paths=["a"],
                                    task_id=task.id)
        got = dispatch_post(
            app, ["api", "delegate", "approvals", p.credential, "stop"],
            lambda **kw: {})
        assert got is not None and got[1]["decision"] == "stop"

    def test_question_route_reaches_the_endpoint(self, env):
        app, task, clock = env
        store = _store(app, clock)
        q = store.request_question(subject="?", questions=["x"], task_id=task.id)
        got = dispatch_post(app, ["api", "delegate", "questions", q.credential],
                            lambda **kw: {"text": "答"})
        assert got is not None and got[1]["ok"] is True

    def test_path_typo_would_be_404(self, env):
        """对照：错一个字母就是 None —— 证明上面几条真的在走路由表。"""
        app, _t, _c = env
        assert dispatch_post(app, ["api", "delegate", "approval", "x"],
                             lambda **kw: {}) is None


# ── 端到端：真起 HTTP ─────────────────────────────────────────────────── #

class TestOverHttp:
    """真起一次服务，走令牌与序列化那条真链路。

    只测函数的话，「GET 需要令牌」「404 回什么」这类**协议层**的事没人管 ——
    而它们在界面上分别表现为「面板一直空白」与「点了没反应」。
    """

    @pytest.fixture
    def served(self, env, monkeypatch):
        app, task, clock = env
        monkeypatch.setenv("FREEAGENT_LIVE_POLL_SECONDS", "0")
        server = create_server(app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(path, body=None, token=SESSION_TOKEN):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data)
            req.add_header("Content-Type", "application/json")
            if token is not None:
                req.add_header(SESSION_HEADER, token)
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        yield call, app, task, clock
        server.shutdown()
        server.server_close()

    def test_live_needs_the_token(self, served):
        call, _app, _task, _clock = served
        status, _body = call("/api/delegate/live", token=None)
        assert status == 401

    def test_live_returns_the_snapshot(self, served):
        call, app, task, clock = served
        _write_progress(app, clock, task.id, status="正在执行", recent="read")
        status, body = call("/api/delegate/live")
        assert status == 200
        assert body["kind"] == "delegate_live"
        assert body["poll_seconds"] == 0.0
        assert body["items"][0]["recent"] == "read"

    def test_recovery_drop_over_http(self, served):
        """真起 HTTP：陈旧行的「放弃」按钮走的就是这条路径。"""
        call, app, task, _clock = served
        status, body = call("/api/delegate/tasks/" + task.id + "/drop", {})
        assert status == 200 and body["ok"] is True
        assert app.tasks.get(task.id).state.value == "dropped"

    def test_allow_over_http_lands_in_the_db(self, served):
        call, app, task, clock = served
        store = _store(app, clock)
        p = store.request_tool_call(permission="edit", paths=["a"],
                                    task_id=task.id)
        status, body = call(
            "/api/delegate/approvals/" + p.credential, {"decision": "allow"})
        assert status == 200 and body["ok"] is True
        assert store.decide(p.credential) == "allow"

    def test_allowing_twice_returns_400_not_a_fake_success(self, served):
        """已决定过要**报错**，不能回一句「已允许」。
        前者用户知道要重来一次，后者他以为还能改。"""
        call, app, task, clock = served
        store = _store(app, clock)
        p = store.request_tool_call(permission="edit", paths=["a"],
                                    task_id=task.id)
        call("/api/delegate/approvals/" + p.credential, {"decision": "allow"})
        status, body = call(
            "/api/delegate/approvals/" + p.credential, {"decision": "deny"})
        assert status == 400
        assert "已经答复过" in body["error"]
        assert store.decide(p.credential) == "allow", "结论被翻掉了"