"""「只有发起人能批」这条规则的**达成度**测试（V1.16 起）。

## 这批测试和既有那批的区别

`test_delegate_gate.py` 里的「只有发起人能批」全矩阵测的是**判定机制**：
给定 `requested_by`，谁的点击算、谁的不算。那些用例**自己构造** `requested_by`，
所以**全绿也不代表这条规则在真链路上生效** —— V1.16 查出真链路里那个值
取的是「白名单里排序第一的人」。

这批测试锁的是**那个值从哪来**，以及它有没有真的流到
「谁能批」和「卡发给谁」两处。

## 三条命题

1. ``/delegate`` 从飞书发起时，**谁发起的被记进事务**
2. 闸门拿到的 ``requester`` 是**那条事务的发起人**，不是闸门自己的 approver
3. **卡的收件人 == 库里记的发起人** —— 两边不一致就等于「卡给 A、账记 B」

第 3 条最容易被漏：只改 ``requested_by`` 而不改收件人，
规则看起来对了，而**真机上人点不动自己的卡**。
"""
from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path

import pytest

from freeagent.app import build_app
from freeagent.delegate import ApprovalGate, run_once, task_requester
from freeagent.services.approval import ApprovalStore
from freeagent.services.clock import FrozenClock
from freeagent.services.delegate import DelegationPolicy


class _Runner:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._code = code

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        return self._code, '{"type":"text","text":"ok"}', ""


class _Sender:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_approval_card(self, *, open_id, subject, detail, credential,
                           ttl_seconds):
        self.sent.append({"open_id": open_id, "credential": credential})
        return "om_1"


def _app(tmp_path, clock):
    return build_app(tmp_path / "a.db", clock=clock)


def _dt(y, m, d, hh=12):
    return datetime.datetime(y, m, d, hh)


class TestTaskRecordsRequester:
    def test_create_accepts_and_stores_it(self, tmp_path):
        app = _app(tmp_path, FrozenClock(_dt(2026, 10, 1)))
        role = app.roles.create("工作")
        try:
            task = app.tasks.create(
                "活儿", [role.id], project_path=str(tmp_path),
                delegate_chat_id="oc_1", delegate_requested_by="ou_Alice",
            )
            got = app.task_repo.get(task.id)
            assert got.delegate_requested_by == "ou_Alice"
            # 与「发到哪」是**两回事**，两者都要在
            assert got.delegate_chat_id == "oc_1"
        finally:
            app.close()

    def test_absent_is_none_not_empty_string(self, tmp_path):
        """没传就必须是 None —— 空串会在闸门里被当成「有身份但为空」。"""
        app = _app(tmp_path, FrozenClock(_dt(2026, 10, 1)))
        role = app.roles.create("工作")
        try:
            task = app.tasks.create("活儿", [role.id],
                                    project_path=str(tmp_path))
            assert app.task_repo.get(task.id).delegate_requested_by is None
        finally:
            app.close()


class TestTaskRequester:
    class _T:
        def __init__(self, v):
            self.delegate_requested_by = v

    def test_prefers_the_task_value(self):
        assert task_requester(self._T("ou_Alice"), "ou_First") == "ou_Alice"

    def test_falls_back_when_absent(self):
        assert task_requester(self._T(None), "ou_First") == "ou_First"

    def test_falls_back_when_blank(self):
        """空串与 None 同等对待 —— 空串不是「有身份」。"""
        assert task_requester(self._T("   "), "ou_First") == "ou_First"

    def test_tolerates_object_without_the_attribute(self):
        """老对象 / 别的实现没有这个属性时不能炸。"""
        assert task_requester(object(), "ou_First") == "ou_First"


class TestGateUsesRequester:
    """闸门拿到的 requester 真的进了 pending 行，且卡发给同一个人。"""

    def _gate_and_task(self, tmp_path, *, requester, approver):
        app = _app(tmp_path, FrozenClock(_dt(2026, 10, 1)))
        role = app.roles.create("工作")
        task = app.tasks.create(
            "活儿", [role.id], project_path=str(tmp_path),
            delegate_chat_id="oc_1", delegate_requested_by=requester,
        )
        app.tasks.start(task.id)
        # store 要的是**可调用**的 clock（内部调 ``self._clock()``），
        # 不是 ``FrozenClock`` 实例。
        store = ApprovalStore(app.conn, clock=app.clock.now)
        gate = ApprovalGate(
            store=store, sender=_Sender(), approver=approver,
            wait=lambda cred: store.resolve(cred, "allow", decided_by=requester),
        )
        return app, task, store, gate

    def test_pending_row_records_the_requester(self, tmp_path):
        app, task, store, gate = self._gate_and_task(
            tmp_path, requester="ou_Alice", approver="ou_Zed")
        try:
            assert gate.check(task_id=task.id, project=str(tmp_path),
                              brief="x", requester="ou_Alice") is None
            pend = [c for c in store._conn.execute(
                "SELECT credential FROM pending_approvals WHERE subject "
                "LIKE '%Alice%' OR credential LIKE 'dp%'")]
            assert pend, "没有落 pending 行"
            row = store.get(pend[0][0])
            # 关键：requested_by 是**发起人**，不是 approver
            assert row.requested_by == "ou_Alice"
        finally:
            app.close()

    def test_card_goes_to_the_requester_not_the_approver(self, tmp_path):
        """**卡必须发给发起人**。

        只改 requested_by 而不改收件人的话，真机上人点不动自己的卡 ——
        规则「看起来对」而实际不可用。
        """
        app, task, store, gate = self._gate_and_task(
            tmp_path, requester="ou_Alice", approver="ou_Zed")
        sender = gate._sender
        try:
            gate.check(task_id=task.id, project=str(tmp_path), brief="x",
                       requester="ou_Alice")
            assert sender.sent, "没有发卡"
            assert sender.sent[0]["open_id"] == "ou_Alice"
        finally:
            app.close()

    def test_no_requester_falls_back_to_approver(self, tmp_path):
        """终端发起的委派**行为与今天一致** —— 落回 approver。"""
        app, task, store, gate = self._gate_and_task(
            tmp_path, requester=None, approver="ou_Zed")
        sender = gate._sender
        try:
            gate.check(task_id=task.id, project=str(tmp_path), brief="x")
            row = store.get(
                store._conn.execute(
                    "SELECT credential FROM pending_approvals LIMIT 1"
                ).fetchone()[0])
            assert row.requested_by == "ou_Zed"
            assert sender.sent[0]["open_id"] == "ou_Zed"
        finally:
            app.close()


class TestEndToEndInChannel:
    """整条：飞书建委派 → 执行器取 → 闸门记的是**那个人**。"""

    def test_channel_requester_reaches_the_pending_row(self, tmp_path):
        from freeagent.services.channel import ChannelService

        app = _app(tmp_path, FrozenClock(_dt(2026, 10, 1)))
        try:
            app.config = dataclasses.replace(
                app.config, delegate_projects=(str(tmp_path),))
            # 纪录**必须先建**：/delegate 里有 ``_match_role``，找不到就 return，
            # 事务根本不会建出来。
            app.roles.create("工作")
            svc = ChannelService(app, allowed_senders={"ou_Alice"})
            svc.handle(
                "oc_group", ["ou_Alice"],
                f"/delegate {tmp_path} | 工作 | 加个登录",
                event_id="ev-delegate-1",
            )
            # 从库里找出那条委派
            row = app.conn.execute(
                "SELECT id, delegate_chat_id, delegate_requested_by "
                "FROM tasks WHERE project_path IS NOT NULL").fetchone()
            assert row is not None, "没建出委派事务"
            assert row[1] == "oc_group"
            assert row[2] == "ou_Alice", \
                f"发起人没记上（拿到 {row[2]!r}）"
        finally:
            app.close()

    def test_someone_else_in_the_group_is_not_recorded(self, tmp_path):
        """群里别人发起的委派，记的**是他**，不是 Alice。"""
        from freeagent.services.channel import ChannelService

        app = _app(tmp_path, FrozenClock(_dt(2026, 10, 1)))
        try:
            app.config = dataclasses.replace(
                app.config, delegate_projects=(str(tmp_path),))
            # 纪录**必须先建**：/delegate 里有 ``_match_role``，找不到就 return，
            # 事务根本不会建出来。
            app.roles.create("工作")
            svc = ChannelService(app, allowed_senders={"ou_Alice", "ou_Bob"})
            svc.handle("oc_group", ["ou_Bob"],
                       f"/delegate {tmp_path} | 工作 | 别的活",
                       event_id="ev-delegate-2")
            row = app.conn.execute(
                "SELECT delegate_requested_by FROM tasks "
                "WHERE project_path IS NOT NULL").fetchone()
            assert row is not None
            assert row[0] == "ou_Bob"
        finally:
            app.close()


class TestPickApprover:
    """白名单兜底选审批人：**必须优先 ``ou_`` 条目**。

    实测踩坑（2026-10-09）：白名单同时装着 open_id 与租户级 user_id 时，
    ``sorted()[0]`` 落在数字开头的 user_id 上；user_id 与 open_id 后缀
    互不相干，后缀比对认不出同一人 → 卡主人自己点卡被「只有发起人能批」
    误拒。修法：兜底优先选 ``ou_`` 开头的条目。
    """

    def test_mixed_ids_prefers_open_id(self):
        from freeagent.delegate import pick_approver
        users = {"ou_f4f16affe349b16ba04d81711594d51f", "2d1b7bec"}
        assert pick_approver(users) == "ou_f4f16affe349b16ba04d81711594d51f"

    def test_digits_sort_before_letters_is_exactly_the_trap(self):
        # 数字开头排在 ou_ 前面 —— 旧的 sorted()[0] 恰好踩中，钉死防回归。
        assert sorted({"ou_f4f16", "2d1b7bec"})[0] == "2d1b7bec"

    def test_open_id_only_whitelist(self):
        from freeagent.delegate import pick_approver
        assert pick_approver({"ou_a", "ou_b"}) == "ou_a"

    def test_no_open_id_falls_back_to_sorted_first(self):
        from freeagent.delegate import pick_approver
        assert pick_approver({"2d1b7bec", "abc12345"}) == "2d1b7bec"

    def test_empty_whitelist(self):
        from freeagent.delegate import pick_approver
        assert pick_approver(set()) == ""
