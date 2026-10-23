"""Web 端：待处理收集器与对外承诺（设计文档 11.18 / 11.19 / V1.51）。

打真实服务器（借 ``test_web`` 的 ``served``/``get``），不引入 requests。

**为什么这三个端点必须自己有测试。** 它们是 V1.50/V1.51 刚补上的：
V1.50 把收集器做完了却没露出来，V1.51 补了端点。两次都是「能力做完了
但没人调得到」—— 这次补测试守的是**别再出现第三个端点没测**。

断言按「状态正确 / 关联正确 / 计数正确」写（同 test_promise.py 的纪律），
最要紧的两条：

1. ``/api/promises`` 逾期的只标 ``overdue``，**响应里不许出现「违约」**——
   判违约是用户的事（11.19.5）。这条最隐蔽：多写一个词就是替用户下判断。
2. ``/api/contacts`` 的 ``open_promises`` 计数必须真的对上，关系一等化
   全部的意义就落在这一行数字上。
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pytest

from freeagent.app import App, build_app
from freeagent.domain import TaskKind, WaitingKind, WaitingOn
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows
from freeagent.web.server import create_server
from freeagent.web.session import SESSION_HEADER, SESSION_TOKEN

# tests/ 不是包，所以复用 test_web 的 ``get`` 只能走 sys.path
# （同一个先例见 test_delegate_progress_db.py:43）。刻意**不**抄一份：
# 抄一份就会出现两个「带令牌的 GET」，哪天令牌规则变了只改一个。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import get, post  # noqa: E402

NOW = datetime(2026, 10, 21, 15, 0)  # 周三


@pytest.fixture()
def served_promise(tmp_path):
    """一只钉在周三的 App + 起好的服务器。

    不用 conftest 的 ``app`` 夹具：它的时钟钉在 2026-09-26，
    而承诺的逾期天数要按「今天」算，两个日期混起来断言会读不懂。
    """
    frozen = FrozenClock(NOW)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    role = application.roles.create("工作")
    server = create_server(application, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", application, role, frozen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        application.close()


def _capture(app: App, name: str) -> None:
    app.pending.add(
        source="feishu",
        kind="file",
        media_key=f"key-{name}",
        name=name,
        size=10,
        path=f"/tmp/{name}",
    )


# ------------------------------------------------------------------- 待处理
class TestPendingEndpoint:
    def test_empty_list_is_zero_not_error(self, served_promise):
        addr, app, _role, _clock = served_promise
        status, data, _h = get(addr, "/api/pending")
        assert status == 200
        assert data["kind"] == "pending"
        assert data["count"] == 0
        assert data["items"] == []

    def test_carries_only_facts(self, served_promise):
        """只摆事实：类型、时间、大小、路径。**没有标题**（11.18）。"""
        addr, app, _role, _clock = served_promise
        _capture(app, "报价单.xlsx")
        _status, data, _h = get(addr, "/api/pending")
        assert data["count"] == 1
        item = data["items"][0]
        assert item["kind"] == "file"
        assert item["name"] == "报价单.xlsx"
        assert item["size"] == 10
        assert item["path"] == "/tmp/报价单.xlsx"
        assert item["created_at"], "收下时间必须带"

    def test_index_matches_clarify_number(self, served_promise):
        """序号即 ``/pending N`` 澄清时用的编号。

        Web 与通道共用同一个序号约定，所以两条入口对「第一条」的理解
        必须一致 —— 这里断言 index 从 1 起（0 起会让用户输 0 澄清失败）。
        """
        addr, app, _role, _clock = served_promise
        _capture(app, "a.txt")
        _capture(app, "b.txt")
        _status, data, _h = get(addr, "/api/pending")
        assert [i["index"] for i in data["items"]] == [1, 2]
        # 序号按先进先出，第 1 条就是最早收下的那条。
        assert data["items"][0]["name"] == "a.txt"

    def test_index_is_stable_when_captured_at_same_instant(self, served_promise):
        """同刻收下时序号仍等于**插入序**（回归，V1.51 修）。

        原来的排序是 ``created_at ASC, id ASC``，而 ``id`` 是随机 hex ——
        时间戳相同时顺序随机，于是「界面上第 1 条」和「输 1 澄清出来的
        第 1 条」可能不是同一条。测试用冻时钟（两条必定同刻）把这个坑
        钉死：修之前这里必红。
        """
        addr, app, _role, _clock = served_promise
        names = [f"{i}.txt" for i in range(5)]
        for name in names:
            _capture(app, name)
        _status, first, _h = get(addr, "/api/pending")
        assert [i["name"] for i in first["items"]] == names
        # 再读一次必须逐字相同：序号是契约，不能读一次变一次。
        _status, second, _h = get(addr, "/api/pending")
        assert first["items"] == second["items"]

    def test_pending_count_reaches_today(self, served_promise):
        """V1.50 的破口正是这个：收集器做完了，``/today`` 却不报数。"""
        addr, app, _role, _clock = served_promise
        _capture(app, "a.txt")
        _capture(app, "b.txt")
        _status, data, _h = get(addr, "/api/today")
        assert data["pending_count"] == 2

    def test_pending_count_zero_when_empty(self, served_promise):
        """默认 0：没捕获过时不该让 today 面板多出一个「0」条目。"""
        addr, _app, _role, _clock = served_promise
        _status, data, _h = get(addr, "/api/today")
        assert data["pending_count"] == 0


# ------------------------------------------------------------------- 承诺
class TestPromisesEndpoint:
    def test_empty(self, served_promise):
        addr, _app, _role, _clock = served_promise
        status, data, _h = get(addr, "/api/promises")
        assert status == 200
        assert data["kind"] == "promises" and data["count"] == 0

    def test_open_promise_is_listed(self, served_promise):
        addr, app, role, _clock = served_promise
        task = app.tasks.create("给张总交付报价单", [role.id])
        app.promise.promise(
            "周三前给张总报价单", contact_name="张总", task_id=task.id
        )
        _status, data, _h = get(addr, "/api/promises")
        assert data["count"] == 1
        row = data["items"][0]
        assert row["statement"] == "周三前给张总报价单"
        assert row["status"] == "open"
        assert row["contact_name"] == "张总", "关系名要跟着出来，否则得二次查询"
        assert row["overdue"] is False

    def test_overdue_is_a_fact_not_a_verdict(self, served_promise):
        """核心契约：只标 ``overdue``，**响应里不许出现「违约」**。"""
        addr, app, role, clock = served_promise
        task = app.tasks.create("给张总交付报价单", [role.id])
        app.promise.promise(
            "上周五前给张总报价单",
            contact_name="张总",
            promised_for=datetime(2026, 10, 16, 18, 0),
            task_id=task.id,
        )
        clock.advance(days=0)  # 时钟就钉在 10-21，已过期限
        _status, data, _h = get(addr, "/api/promises")
        row = data["items"][0]
        assert row["overdue"] is True, "过了日子是事实，必须标出来"
        assert "违约" not in str(data), f"系统不许替用户判违约：{data}"
        assert "违" not in row["statement"]

    def test_no_date_is_never_overdue(self, served_promise):
        """没许期限的承诺不该被判逾期（否则凭空造出压力）。"""
        addr, app, role, _clock = served_promise
        task = app.tasks.create("给张总交付报价单", [role.id])
        app.promise.promise(
            "尽快给张总报价单", contact_name="张总", task_id=task.id
        )
        _status, data, _h = get(addr, "/api/promises")
        row = data["items"][0]
        assert row["promised_for"] is None
        assert row["overdue"] is False

    def test_kept_promise_drops_out(self, served_promise):
        """兑现了的账不再占位置 —— 否则承诺列表只增不减。"""
        addr, app, role, _clock = served_promise
        task = app.tasks.create("给张总交付报价单", [role.id])
        promise = app.promise.promise(
            "周三前给张总报价单", contact_name="张总", task_id=task.id
        )
        app.promise.keep(promise.id)
        _status, data, _h = get(addr, "/api/promises")
        assert data["count"] == 0

    def test_sorted_by_promised_for(self, served_promise):
        addr, app, role, _clock = served_promise
        far = app.tasks.create("远的", [role.id])
        near = app.tasks.create("近的", [role.id])
        app.promise.promise(
            "月底给张总", contact_name="张总",
            promised_for=datetime(2026, 10, 31, 18, 0), task_id=far.id,
        )
        app.promise.promise(
            "明早给张总", contact_name="张总",
            promised_for=datetime(2026, 10, 22, 9, 0), task_id=near.id,
        )
        _status, data, _h = get(addr, "/api/promises")
        assert [r["statement"] for r in data["items"]] == ["明早给张总", "月底给张总"]

    def test_overdue_promise_outranks_plain_task_in_today(self, served_promise):
        """承诺原语贯通到今天视图（11.19.4）。"""
        addr, app, role, _clock = served_promise
        plain = app.tasks.create("随手回个消息", [role.id])
        app.tasks.schedule(plain.id, NOW.date())
        promised = app.tasks.create("给张总交付报价单", [role.id])
        app.tasks.schedule(promised.id, NOW.date())
        app.promise.promise(
            "上周五前给张总报价单",
            contact_name="张总",
            promised_for=datetime(2026, 10, 16, 18, 0),
            task_id=promised.id,
        )
        _status, data, _h = get(addr, "/api/today")
        ids = [i["id"] for i in data["items"]]
        assert ids.index(promised.id) < ids.index(plain.id)
        first = data["items"][0]
        assert any(s["code"] == "promise_overdue" for s in first["signals"])


# ------------------------------------------------------------------- 关系
class TestContactsEndpoint:
    def test_empty(self, served_promise):
        addr, _app, _role, _clock = served_promise
        status, data, _h = get(addr, "/api/contacts")
        assert status == 200
        assert data["kind"] == "contacts" and data["count"] == 0

    def test_lists_contacts(self, served_promise):
        addr, app, _role, _clock = served_promise
        app.promise.contact("张总", note="客户 A")
        _status, data, _h = get(addr, "/api/contacts")
        assert data["count"] == 1
        row = data["items"][0]
        assert row["name"] == "张总"
        assert row["kind"] == "person"
        assert row["note"] == "客户 A"

    def test_open_promise_count_is_correct(self, served_promise):
        """关系一等化的意义全在这行数字上：我跟谁之间还有账。"""
        addr, app, role, _clock = served_promise
        t1 = app.tasks.create("给张总交付报价单", [role.id])
        t2 = app.tasks.create("给张总送合同", [role.id])
        other = app.tasks.create("给李会计转账", [role.id])
        p1 = app.promise.promise("报价单", contact_name="张总", task_id=t1.id)
        app.promise.promise("送合同", contact_name="张总", task_id=t2.id)
        p3 = app.promise.promise("转账", contact_name="李会计", task_id=other.id)

        _status, data, _h = get(addr, "/api/contacts")
        counts = {c["name"]: c["open_promises"] for c in data["items"]}
        assert counts == {"张总": 2, "李会计": 1}

        app.promise.keep(p1.id)  # 结掉一条，张总剩 1
        _status, data, _h = get(addr, "/api/contacts")
        counts = {c["name"]: c["open_promises"] for c in data["items"]}
        assert counts["张总"] == 1

        app.promise.withdraw(p3.id)  # 收回也算结清
        _status, data, _h = get(addr, "/api/contacts")
        counts = {c["name"]: c["open_promises"] for c in data["items"]}
        assert counts["李会计"] == 0

    def test_contact_without_promise_still_listed(self, served_promise):
        """只建关系不给承诺也是合法的（先认人，事后面上）。"""
        addr, app, _role, _clock = served_promise
        app.promise.contact("房东")
        _status, data, _h = get(addr, "/api/contacts")
        assert data["count"] == 1
        assert data["items"][0]["open_promises"] == 0

    def test_waiting_count_is_reported_alongside_promises(self, served_promise):
        """关系不只挂在承诺上，也挂在等候上（20.1.5 的另一半，V1.52）。

        只回承诺数的话，「我跟张总之间有几笔没结清的」只能答一半 ——
        「我在等他回合同」那几笔不显示。两者并排才是完整的账。
        """
        addr, app, role, _clock = served_promise
        contact = app.promise.contact("张总")
        task = app.tasks.create(
            "等张总回合同", [role.id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(
                kind=WaitingKind.PERSON, who_or_what="张总回合同",
                since=NOW, contact_id=contact.id,
            ),
        )
        owed = app.tasks.create("给张总交付报价单", [role.id])
        app.promise.promise("报价单", contact_name="张总", task_id=owed.id)

        _status, data, _h = get(addr, "/api/contacts")
        row = next(c for c in data["items"] if c["name"] == "张总")
        assert row["open_promises"] == 1
        assert row["open_waitings"] == 1

        # 做完了等候件不该再占计数
        app.tasks.complete(task.id)
        _status, data, _h = get(addr, "/api/contacts")
        row = next(c for c in data["items"] if c["name"] == "张总")
        assert row["open_waitings"] == 0

    def test_waiting_without_relation_is_not_counted(self, served_promise):
        """没挂关系的等候不硬凑到某个人头上 —— 那会造出一笔假账。"""
        addr, app, role, _clock = served_promise
        app.promise.contact("房东")
        app.tasks.create(
            "等对方回话", [role.id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(
                kind=WaitingKind.PERSON, who_or_what="对方回话", since=NOW,
            ),
        )
        _status, data, _h = get(addr, "/api/contacts")
        assert data["items"][0]["open_waitings"] == 0


# ------------------------------------------------------------------- 一页账
class TestContactLedgerEndpoint:
    """``/api/contact`` 明细端点（11.20.6，Web「关系」视图的明细落点）。

    列表端点给「几条承诺 + 几笔在等」的计数，这里是逐条账。断言的纪律同
    promises 端点：**只标 overdue，响应里不许出现「违约」** —— 判违约是
    用户的事（11.19.5），这一条最容易因为「顺手写上」而破。
    """

    def _owe_and_wait(self, app, role):
        """张总头上挂一笔逾期承诺 + 一笔在等，返回 (promise, contact, 两事务id)。"""
        contact = app.promise.contact("张总")
        owed = app.tasks.create("给张总交付报价单", [role.id])
        p = app.promise.promise(
            "上周五前给张总报价单",
            contact_name="张总", task_id=owed.id,
            promised_for=datetime(2026, 10, 16, 18, 0),
        )
        wait = app.tasks.create(
            "等张总回合同", [role.id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(
                kind=WaitingKind.PERSON, who_or_what="张总回合同",
                since=NOW, contact_id=contact.id,
            ),
        )
        return p, contact, owed.id, wait.id

    def test_ledger_shows_both_halves(self, served_promise):
        """一页账的要点：我欠他的 + 我在等他的，两半并排才是完整的账。"""
        addr, app, role, _clock = served_promise
        _p, contact, owed_id, wait_id = self._owe_and_wait(app, role)
        status, data, _h = get(addr, "/api/contact?id=" + contact.id)
        assert status == 200
        assert data["kind"] == "contact_ledger"
        assert data["contact"]["name"] == "张总"
        assert len(data["promises"]) == 1
        assert len(data["waitings"]) == 1
        assert data["promises"][0]["task_id"] == owed_id
        assert data["waitings"][0]["task_id"] == wait_id
        assert data["waitings"][0]["who_or_what"] == "张总回合同"

    def test_overdue_is_a_fact_not_a_verdict(self, served_promise):
        """核心契约：逾期的只标 overdue，**响应里不许出现「违约」**。"""
        addr, app, role, _clock = served_promise
        _p, contact, _o, _w = self._owe_and_wait(app, role)
        _status, data, _h = get(addr, "/api/contact?id=" + contact.id)
        assert data["promises"][0]["overdue"] is True, "过了期限是事实，必须标出来"
        assert "违约" not in str(data), f"系统不许替用户判违约：{data}"
        assert "违" not in data["promises"][0]["statement"]

    def test_lookup_by_name_or_id(self, served_promise):
        addr, app, role, _clock = served_promise
        _p, contact, _o, _w = self._owe_and_wait(app, role)
        _s1, by_id, _h = get(addr, "/api/contact?id=" + contact.id)
        # name 含中文：HTTP 请求行只接受 ASCII，路径必须 percent-encode
        # （服务端 parse_qs 会 unquote 回来）。
        _s2, by_name, _h = get(addr, "/api/contact?name=" + quote(contact.name))
        assert by_id["contact"]["id"] == by_name["contact"]["id"]

    def test_unknown_contact_is_400(self, served_promise):
        """查不存在的人要明确报错，绝不静默创建一个（造出一笔假账）。"""
        addr, _app, _role, _clock = served_promise
        status, data, _h = get(addr, "/api/contact?id=nope")
        assert status == 400
        assert "不存在" in data["error"]

    def test_missing_arg_is_400(self, served_promise):
        addr, _app, _role, _clock = served_promise
        status, data, _h = get(addr, "/api/contact")
        assert status == 400

    def test_kept_promise_drops_out_of_ledger(self, served_promise):
        """兑现的账不再占位置 —— 一页账只反映没结清的。"""
        addr, app, role, _clock = served_promise
        p, contact, _o, _w = self._owe_and_wait(app, role)
        app.promise.keep(p.id)
        _status, data, _h = get(addr, "/api/contact?id=" + contact.id)
        assert data["promises"] == []
        assert len(data["waitings"]) == 1


# ------------------------------------------------------------------- 承诺结账
class TestPromiseSettleEndpoint:
    """``POST /api/promise/<id>/<action>`` —— Web「承诺」视图的结账操作。

    结账是**写**操作：兑现（keep）、破了（break）、收回（withdraw）都要真的
    落库，列表立刻少一条（11.19.5「三个终态只由用户显式动作产生」）。
    动作白名单在 :mod:`actions`，动作名是 URL 的一部分，漏过一个就是一条
    没人看过的写路径 —— 所以未知动作必须 404。
    """

    def _open_promise(self, app, role, statement="周三前给张总报价单"):
        task = app.tasks.create(statement, [role.id])
        return app.promise.promise(statement, contact_name="张总", task_id=task.id)

    def test_keep_settles_and_leaves_list(self, served_promise):
        addr, app, role, _clock = served_promise
        p = self._open_promise(app, role)
        status, data = post(addr, "/api/promise/" + p.id + "/keep", {})
        assert status == 200
        assert data["ok"] is True
        _s, listed, _h = get(addr, "/api/promises")
        assert listed["count"] == 0, "兑现了的账不再占位置"

    def test_break_and_withdraw_settle(self, served_promise):
        addr, app, role, _clock = served_promise
        p1 = self._open_promise(app, role, "承诺甲")
        p2 = self._open_promise(app, role, "承诺乙")
        assert post(addr, "/api/promise/" + p1.id + "/break", {})[0] == 200
        assert post(addr, "/api/promise/" + p2.id + "/withdraw", {})[0] == 200
        _s, listed, _h = get(addr, "/api/promises")
        assert listed["count"] == 0

    def test_unknown_action_is_404(self, served_promise):
        addr, app, role, _clock = served_promise
        p = self._open_promise(app, role)
        status, _data = post(addr, "/api/promise/" + p.id + "/bogus", {})
        assert status == 404, "动作不在白名单，必须 404"

    def test_unknown_id_is_400(self, served_promise):
        addr, _app, _role, _clock = served_promise
        status, data = post(addr, "/api/promise/nope/keep", {})
        assert status == 400
        assert "不存在" in data["error"]

# -------------------------------------------------- 澄清（写端点，11.18）
class TestPendingClarifyEndpoint:
    """把捕获澄清成事务（V1.56，Web 待处理视图的操作）。

    澄清是**唯一建事务入口**且必须用户显式触发；Web 端点只翻译成服务层
    调用（12.1.1），不自猜标题、不自建事务。
    """

    def test_clarify_creates_task(self, served_promise):
        addr, app, _role, _clock = served_promise
        _capture(app, "报价单.xlsx")
        item = app.pending.pending()[0]
        status, data = post(addr, "/api/pending/clarify", {"id": item.id, "title": "给张总做报价"})
        assert status == 201
        assert data["ok"] is True
        assert data["title"] == "给张总做报价"
        # 澄清后收集器清空，事务进任务列表 —— 澄清是唯一建事务入口。
        assert app.pending.count() == 0
        assert "给张总做报价" in [t.title for t in app.tasks.list_open()]

    def test_clarify_without_title_uses_honest(self, served_promise):
        """不猜内容（11.18）：没给标题，用诚实标题（图片+时间戳，不编名字）。"""
        addr, app, _role, _clock = served_promise
        app.pending.add(source="feishu", kind="image", media_key="k", name="image", size=20, path="/tmp/i.png")
        item = app.pending.pending()[0]
        status, data = post(addr, "/api/pending/clarify", {"id": item.id})
        assert status == 201
        assert data["title"] == "图片（10-21 15:00）"

    def test_clarify_requires_id(self, served_promise):
        addr, app, _role, _clock = served_promise
        status, data = post(addr, "/api/pending/clarify", {})
        assert status == 400
        assert "待处理项" in data["error"]

    def test_clarify_unknown_id_is_400(self, served_promise):
        addr, app, _role, _clock = served_promise
        status, data = post(addr, "/api/pending/clarify", {"id": "nope"})
        assert status == 400
        assert "不存在" in data["error"]

    def test_clarify_twice_is_400(self, served_promise):
        """重复澄清关死（11.18）：同一条不能澄清两次，报已澄清过。"""
        addr, app, _role, _clock = served_promise
        _capture(app, "a.txt")
        item = app.pending.pending()[0]
        assert post(addr, "/api/pending/clarify", {"id": item.id})[0] == 201
        status, data = post(addr, "/api/pending/clarify", {"id": item.id})
        assert status == 400
        assert "已经澄清" in data["error"]

# ---------------------------------------------------- 忽略（写端点，11.18）
class TestPendingDismissEndpoint:
    """忽略一条捕获（V1.58，Web 待处理视图的「不要了」）。

    与 CLI `/pending drop N` 同一个服务层入口（PendingService.dismiss），
    本版不提供「取消忽略」（代价写入第十五章）。
    """

    def test_dismiss_removes_from_pending(self, served_promise):
        addr, app, _role, _clock = served_promise
        _capture(app, "报价单.xlsx")
        item = app.pending.pending()[0]
        status, data = post(addr, "/api/pending/dismiss", {"id": item.id})
        assert status == 200
        assert data["ok"] is True
        assert app.pending.count() == 0

    def test_dismiss_requires_id(self, served_promise):
        addr, app, _role, _clock = served_promise
        status, data = post(addr, "/api/pending/dismiss", {})
        assert status == 400
        assert "待处理项" in data["error"]

    def test_dismiss_unknown_id_is_400(self, served_promise):
        addr, app, _role, _clock = served_promise
        status, data = post(addr, "/api/pending/dismiss", {"id": "nope"})
        assert status == 400
        assert "不存在" in data["error"]

    def test_dismiss_twice_is_400(self, served_promise):
        """忽略是不可逆动作（11.18）：同一条不能忽略两次。"""
        addr, app, _role, _clock = served_promise
        _capture(app, "a.txt")
        item = app.pending.pending()[0]
        assert post(addr, "/api/pending/dismiss", {"id": item.id})[0] == 200
        status, data = post(addr, "/api/pending/dismiss", {"id": item.id})
        assert status == 400
        assert "已经处理过" in data["error"]


# ---------------------------------------------------- 导航角标（读端点）
class TestSummaryEndpoint:
    """导航角标计数（V1.58）：只报数，不报内容，口径与对应端点一致。"""

    def test_summary_aggregates_counts(self, served_promise):
        addr, app, role, _clock = served_promise
        _capture(app, "报价单.xlsx")  # pending=1
        status, data, _h = get(addr, "/api/summary")
        assert status == 200
        assert data["kind"] == "summary"
        assert data["pending"] == app.pending.count()
        assert data["nudge"] == app.nudge.count()
        assert data["promises_overdue"] == 0
        assert "today" in data

    def test_summary_counts_overdue_promise(self, served_promise):
        addr, app, role, _clock = served_promise
        task = app.tasks.create("给李会计转账", [role.id])
        app.promise.promise(
            "尽快转账", contact_name="李会计", task_id=task.id,
            promised_for=datetime(2026, 10, 20, 15, 0),
        )
        status, data, _h = get(addr, "/api/summary")
        assert data["promises_overdue"] == 1

    def test_summary_counts_nudge_waiting(self, served_promise):
        addr, app, role, _clock = served_promise
        app.tasks.create(
            "等张总回合同", [role.id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(
                kind=WaitingKind.PERSON, who_or_what="张总回合同",
                since=datetime(2026, 10, 10, 9, 0),
                follow_up_at=datetime(2026, 10, 18, 15, 0),
            ),
        )
        status, data, _h = get(addr, "/api/summary")
        assert data["nudge"] == 1

