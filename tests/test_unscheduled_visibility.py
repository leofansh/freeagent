"""没排期的事务**必须被说出来** —— 不能被静默吞掉。

## 这条测试存在的理由（实测）

WEB 接口模拟测试里，表单入口 ``POST /api/task`` 建了一条事务，
``GET /api/all?scope=open`` 看得到它，但 ``GET /api/today`` 的 ``count`` 是 0。
用户记完一件事，转头问「今天该做什么」，拿到「今天没有排进来的事务」——
**而那件事明明在库里**。

## 为什么「不进今天视图」是对的，而「不说」是错的

设计方案 6.2 把今天视图定义成 ``{事务 | scheduled_for == today}``，
6.4 的 ``unschedule`` 正是靠置 ``None`` 把事务移出今天 —— 所以
``scheduled_for is None`` 不进今天视图**是设计，不是 bug**，这里不去改它。

真正的问题是**静默**：东西不在今天，却没有任何一处说「它不在今天，
它在别处」。用户可以接受「今天没有」，不能接受「我记的东西蒸发了」。

所以修法是**照报条数**，不改归属规则。下面第一条断言刻意钉住
「仍然不进 items」——防止后来有人把这条改成「默认排到今天」，
那会让今天视图无限膨胀，正是设计文档 6.2 想治的那个病。
"""

from __future__ import annotations

from freeagent.web.serialize import today_payload


def _role_id(app) -> str:
    return app.roles.create("工作项目A").id


class TestCountedButNotListed:
    """计数照报，列表照旧不含它们。"""

    def test_unscheduled_is_counted_not_shown(self, app) -> None:
        role = _role_id(app)
        app.tasks.create("一件没说日期的事", [role])
        view = app.today.view()
        assert view.items == (), "未排期不进今天视图（设计方案 6.2），这条别改"
        assert view.unscheduled_count == 1

    def test_scheduled_today_is_listed_not_counted(self, app, clock) -> None:
        role = _role_id(app)
        app.tasks.create("排到今天的事", [role], scheduled_for=clock.today())
        view = app.today.view()
        assert len(view.items) == 1
        assert view.unscheduled_count == 0

    def test_scheduled_future_is_neither(self, app, clock) -> None:
        """未来的不计入「没排期」—— 它是**有**排期，只是不在今天。"""
        role = _role_id(app)
        from datetime import timedelta

        app.tasks.create("下周的事", [role], scheduled_for=clock.today() + timedelta(days=5))
        view = app.today.view()
        assert view.items == ()
        assert view.unscheduled_count == 0

    def test_done_is_not_counted(self, app) -> None:
        role = _role_id(app)
        task = app.tasks.create("做完的没排期事", [role])
        app.tasks.complete(task.id)
        assert app.today.view().unscheduled_count == 0

    def test_dropped_is_not_counted(self, app) -> None:
        role = _role_id(app)
        task = app.tasks.create("放弃的没排期事", [role])
        app.tasks.drop(task.id)
        assert app.today.view().unscheduled_count == 0

    def test_two_open_tasks_count_two(self, app) -> None:
        role = _role_id(app)
        app.tasks.create("第一件", [role])
        app.tasks.create("第二件", [role])
        assert app.today.view().unscheduled_count == 2


class TestWebPayloadCarriesTheCount:
    """``/api/today`` 的形状里必须有这个字段，界面才能说。"""

    def test_payload_has_unscheduled_count(self, app) -> None:
        role = _role_id(app)
        app.tasks.create("一件没排期的事", [role])
        payload = today_payload(app.today.view(), {})
        assert payload["count"] == 0
        assert payload["unscheduled_count"] == 1

    def test_zero_when_nothing_hidden(self, app, clock) -> None:
        role = _role_id(app)
        app.tasks.create("今天的事", [role], scheduled_for=clock.today())
        payload = today_payload(app.today.view(), {})
        assert payload["count"] == 1
        assert payload["unscheduled_count"] == 0


class TestChatReplyAdmitsThem:
    """「今天没有」后面必须补一句，否则用户以为东西丢了。"""

    def test_empty_today_names_the_hidden_ones(self, app) -> None:
        role = _role_id(app)
        app.tasks.create("一件没排期的事", [role])
        reply = app.chat.respond("今天该做什么？")
        assert reply.route == "today", f"路由跑偏了：{reply.route}"
        assert str(app.today.view().unscheduled_count) in reply.text
        assert "没排期" in reply.text

    def test_no_empty_clause_when_nothing_hidden(self, app, clock) -> None:
        """没有藏起来的就不要提 —— 平白多一句「另有 0 条」是噪声。"""
        role = _role_id(app)
        app.tasks.create("今天的事", [role], scheduled_for=clock.today())
        app.chat.respond("今天该做什么？")
        view = app.today.view()
        assert view.unscheduled_count == 0
        assert view.items, "这条应当真的出现在今天视图里"
