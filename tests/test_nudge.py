"""催办草稿（设计文档 20.1.4 / V1.52）。

这个模块最容易犯的错不是「写得不好」，是**越过边界**：替用户把话说完了、
替用户把账结了、或者 —— 最隐蔽 —— 悄悄改了一句语气，把「我等你」写成
「你欠我」。所以断言按**边界**写：

1. 候选口径必须与两个排序信号逐字一致（漂移的症状是「今天视图说该催，
   /nudge 列表里没有」）
2. 草稿里**不许出现库里没有的事实**（第十章 [TODO] 纪律的延伸）
3. 出了草稿**账不能自动结**（承诺仍 open、等候仍卡着）
4. 不在候选里的事务**要被拒绝**，并说清为什么

第 4 件值得写测试：用户敲 `/nudge <id>` 却指了一条没到期的，
默默起草一条用不上的消息，是最难被发现的那种错。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from freeagent.app import build_app
from freeagent.domain import (
    ContactKind,
    PromiseStatus,
    TaskKind,
    ValidationError,
    WaitingKind,
    WaitingOn,
)
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows, compute_signals

#: 2026-10-21 是周三
WEDNESDAY = datetime(2026, 10, 21, 15, 0)


@pytest.fixture
def env(tmp_path: Path):
    frozen = FrozenClock(WEDNESDAY)
    app = build_app(tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows())
    role = app.roles.create("工作")
    try:
        yield app, frozen, role
    finally:
        app.close()


def _task(app, role, title="一件事"):
    return app.tasks.create(title, [role.id])


def _waiting(app, role, *, follow_up_at=None, who="张总回合同",
             contact_id=None, since=datetime(2026, 10, 10, 9, 0), title="等张总回合同"):
    """建一条等候件。``follow_up_at`` 决定它进不进催办候选。"""
    return app.tasks.create(
        title, [role.id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(
            kind=WaitingKind.PERSON, who_or_what=who, since=since,
            follow_up_at=follow_up_at, contact_id=contact_id,
        ),
    )


def _promised(app, role, *, promised_for=None, contact="张总",
              statement="周三前给张总报价单", title="给张总交付报价单"):
    task = app.tasks.create(title, [role.id])
    promise = app.promise.promise(
        statement, contact_name=contact, promised_for=promised_for, task_id=task.id
    )
    return task, promise


# --------------------------------------------------------------------- 候选
class TestTargets:
    def test_empty_when_nothing_is_due(self, env):
        app, _clock, role = env
        _waiting(app, role, follow_up_at=datetime(2026, 10, 25, 9, 0))
        _promised(app, role, promised_for=datetime(2026, 10, 25, 18, 0))
        assert app.nudge.targets() == []
        assert app.nudge.count() == 0

    def test_waiting_overdue_enters(self, env):
        """口径与 OVERDUE_WAIT 对齐：follow_up_at 已过就进。"""
        app, _clock, role = env
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        targets = app.nudge.targets()
        assert [t.task.id for t in targets] == [task.id]
        assert targets[0].direction == "wait"

    def test_promise_overdue_enters(self, env):
        """口径与 PROMISE_OVERDUE 对齐：过了 promised_for 且仍 open 就进。"""
        app, _clock, role = env
        task, _p = _promised(app, role, promised_for=datetime(2026, 10, 16, 18, 0))
        targets = app.nudge.targets()
        assert [t.task.id for t in targets] == [task.id]
        assert targets[0].direction == "promise"

    def test_kept_promise_leaves_the_list(self, env):
        """账结清了就不该再催 —— 否则「/nudge」变成一份永远清不完的清单。"""
        app, _clock, role = env
        _task, promise = _promised(
            app, role, promised_for=datetime(2026, 10, 16, 18, 0)
        )
        app.promise.keep(promise.id)
        assert app.nudge.targets() == []

    def test_done_task_leaves_the_list(self, env):
        """事务做完了就没有要催的了 —— 候选只看未结束。"""
        app, _clock, role = env
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        assert app.nudge.count() == 1
        app.tasks.complete(task.id)
        assert app.nudge.count() == 0

    def test_long_ago_first(self, env):
        """最该催的排最前：拖得越久越在前。"""
        app, _clock, role = env
        recent = _waiting(app, role, follow_up_at=datetime(2026, 10, 20, 9, 0),
                          title="昨天该跟进的")
        old = _waiting(app, role, follow_up_at=datetime(2026, 9, 1, 9, 0),
                       title="上个月就该跟进的")
        targets = app.nudge.targets()
        assert [t.task.id for t in targets] == [old.id, recent.id]

    def test_both_sides_can_be_listed_together(self, env):
        """「我在等」与「我欠着」同一天都要催时，两条都在。"""
        app, _clock, role = env
        wait = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        owed, _p = _promised(app, role, promised_for=datetime(2026, 10, 16, 18, 0))
        directions = {t.task.id: t.direction for t in app.nudge.targets()}
        assert directions == {wait.id: "wait", owed.id: "promise"}

    def test_no_date_is_never_due(self, env):
        """没设跟进日 = 「没说要什么时候催」，不该被当成已到点。"""
        app, _clock, role = env
        _waiting(app, role, follow_up_at=None)
        _promised(app, role, promised_for=None)
        assert app.nudge.targets() == []

    def test_follow_up_at_exactly_now_is_due(self, env):
        """边界：``follow_up_at`` 正好等于此刻算「已过」。

        差一分钟的差别会让「今天设的跟进」整天不被列出来，而症状是
        用户看不出哪里错了 —— 所以这条边界必须钉住。
        """
        app, _clock, role = env
        _waiting(app, role, follow_up_at=WEDNESDAY)
        assert app.nudge.count() == 1

    def test_one_minute_later_is_not_due_yet(self, env):
        app, _clock, role = env
        from datetime import timedelta
        _waiting(app, role, follow_up_at=WEDNESDAY + timedelta(minutes=1))
        assert app.nudge.count() == 0

    def test_contact_name_is_resolved(self, env):
        """挂了关系就能写出人名 —— 没挂的**不猜**。"""
        app, _clock, role = env
        contact = app.promise.contact("张总")
        _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0),
                 contact_id=contact.id)
        _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0),
                 who="王工回消息", contact_id=None, title="等王工回消息")
        targets = {t.task.title: t.contact_name for t in app.nudge.targets()}
        assert targets == {"等张总回合同": "张总", "等王工回消息": None}


# --------------------------------------------------------------------- 草稿
class TestDraft:
    def test_lists_facts_from_the_database(self, env):
        """事实位必须真的来自库，而不是写死的模板句。"""
        app, _clock, role = env
        contact = app.promise.contact("张总")
        task = _waiting(
            app, role, follow_up_at=datetime(2026, 10, 18, 9, 0),
            contact_id=contact.id,
        )
        text = app.nudge.draft_for_task(task.id)
        assert "张总回合同" in text
        assert "2026-10-10" in text, "等了多久的事实要有"
        assert "2026-10-18 09:00" in text, "原定跟进日要有"
        assert "张总" in text

    def test_promise_side_shows_what_i_promised(self, env):
        app, _clock, role = env
        task, _p = _promised(app, role, promised_for=datetime(2026, 10, 16, 18, 0))
        text = app.nudge.draft_for_task(task.id)
        assert "周三前给张总报价单" in text
        assert "2026-10-16 18:00" in text

    def test_two_directions_give_different_text(self, env):
        """「等对方」与「我欠对方」语气相反，模板不能混成一句。

        混了的后果很具体：该道歉的事被写成质问，或者该催的人被安抚了。
        """
        app, _clock, role = env
        wait = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        owed, _p = _promised(app, role, promised_for=datetime(2026, 10, 16, 18, 0))
        wait_text = app.nudge.draft_for_task(wait.id)
        owed_text = app.nudge.draft_for_task(owed.id)
        assert wait_text != owed_text
        assert "问对方进度" in wait_text
        assert "告知延期" in owed_text

    def test_body_is_blank_not_prefilled(self, env):
        """正文留白。系统替用户写好第一句，用户就会直接发出去。"""
        app, _clock, role = env
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        text = app.nudge.draft_for_task(task.id)
        assert "[TODO 开头" in text
        # 「系统一个字都不替你决定」这句话要真的成立：正文段里
        # 除了 TODO、小标题与那句说明，没有别的句子。
        body = text.split("## 正文")[1].split("---")[0]
        for line in (l.strip() for l in body.splitlines()):
            if not line:
                continue
            if line.startswith("（"):  # 小标题后的那句说明
                continue
            assert line.startswith("[TODO"), (
                f"正文里混进了系统替用户写的话：{line!r}"
            )

    def test_says_it_will_not_send(self, env):
        """这一句必须在草稿里 —— 用户复制出去时会连着它一起复制走，
        那正好：发出去的东西自己带着「我没替你说」这句提醒。"""
        app, _clock, role = env
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        assert "不会发" in app.nudge.draft_for_task(task.id)

    def test_does_not_invent_facts(self, env):
        """库里没有的东西不许出现在草稿里。

        这里断言**两件具体的假事**没被编出来：对方的邮箱、上次谈的结论。
        「上次跟进的结论」那条必须留着 TODO —— 编一句「上次说好周四」
        比空着更糟：用户会照着发。
        """
        app, _clock, role = env
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        app.tasks.note(task.id, "打过电话，没打通")  # 库里有记录，但草稿不引用它
        text = app.nudge.draft_for_task(task.id)
        assert "@" not in text, "编出了不存在的邮箱"
        assert "周四" not in text, "编出了不存在的约定"
        assert "打过电话" not in text, "草稿不该把记录当结论用"
        assert "上次跟进的结论：[TODO" in text

    def test_never_says_you_broke_it(self, env):
        """「系统不判违约」这条纪律要一路守到草稿里（11.19.5）。"""
        app, _clock, role = env
        _task, _p = _promised(app, role, promised_for=datetime(2026, 10, 16, 18, 0))
        for target in app.nudge.targets():
            assert "违约" not in app.nudge.draft(target)
        assert "已逾期" in app.nudge.draft(app.nudge.targets()[0])


# ------------------------------------------------------------------- 副作用
class TestNoSideEffects:
    def test_drafting_does_not_settle_the_account(self, env):
        """出了草稿，承诺**仍是 open**、等候**仍卡着**。

        催一次不等于账结了。结算只能由用户显式动作（11.19.5）。
        """
        app, _clock, role = env
        wait = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        owed, promise = _promised(
            app, role, promised_for=datetime(2026, 10, 16, 18, 0)
        )
        app.nudge.draft_for_task(wait.id)
        app.nudge.draft_for_task(owed.id)

        assert app.promise.open_count() == 1, "催过不等于兑现"
        assert app.promise.promises_for_task(owed.id)[0].status is PromiseStatus.OPEN
        after = app.tasks.get(wait.id)
        assert after.state is not None and after.waiting_on is not None
        assert after.waiting_on.follow_up_at == datetime(2026, 10, 18, 9, 0)

    def test_drafting_writes_no_record(self, env):
        """草稿是**只读**操作：不落 TaskRecord。

        它不是「做了一件事」，是「准备了一段文字」。往记录里写一条
        「已催办」会让时间线出现系统代做的事 —— 那些记录是用户回看
        「我当时干了什么」的唯一依据。
        """
        app, _clock, role = env
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        before = len(app.record_repo.list_for_task(task.id))
        app.nudge.draft_for_task(task.id)
        app.nudge.targets()
        assert len(app.record_repo.list_for_task(task.id)) == before


# --------------------------------------------------------------------- 拒绝
class TestRefusals:
    def test_task_with_no_waiting_and_no_promise_is_refused(self, env):
        app, _clock, role = env
        task = app.tasks.create("随手记一件事", [role.id])
        with pytest.raises(ValidationError) as exc:
            app.nudge.draft_for_task(task.id)
        assert "可催的" in str(exc.value) or "没有在等" in str(exc.value)

    def test_not_yet_due_is_refused_with_a_reason(self, env):
        """没到点的也要拒，且要说清「为什么现在不行」。

        静默给一份草稿是这里最糟的结局：用户会以为「催过了」，
        而其实什么都没发生。
        """
        app, _clock, role = env
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 25, 9, 0))
        with pytest.raises(ValidationError) as exc:
            app.nudge.draft_for_task(task.id)
        assert "还没到催的时候" in str(exc.value)

    def test_unknown_task_id_still_raises(self, env):
        app, _clock, _role = env
        with pytest.raises(Exception):
            app.nudge.draft_for_task("nope")


# --------------------------------------------------------- 与信号的同一口径
class TestAgreesWithSignals:
    def test_same_set_as_the_two_overdue_signals(self, env):
        """**同一口径**：今天视图说该催的，/nudge 必须都列出来。

        这是这个模块最容易出的错：催办自己写一套「什么算该催」，
        然后两边慢慢漂移 —— 症状是「今天视图里那条红的说该催，
        /nudge 却列不出来」，用户会怀疑系统坏了。
        """
        app, clock, role = env
        due_wait = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        later_wait = _waiting(app, role, follow_up_at=datetime(2026, 10, 25, 9, 0),
                              title="还没到点")
        overdue_p, _ = _promised(app, role, promised_for=datetime(2026, 10, 16, 18, 0))
        future_p, _ = _promised(app, role, promised_for=datetime(2026, 10, 25, 18, 0),
                                title="还没到期的承诺")
        clock.advance(days=0)

        flagged = set()
        for task in app.tasks.list_open():
            codes = {
                s.code.value
                for s in compute_signals(
                    task, clock.now(), clock.today(),
                    promise=app.promise.promise_of(task.id),
                )
            }
            if "overdue_wait" in codes or "promise_overdue" in codes:
                flagged.add(task.id)

        assert {t.task.id for t in app.nudge.targets()} == flagged
        assert flagged == {due_wait.id, overdue_p.id}
        assert later_wait.id not in flagged and future_p.id not in flagged


# --------------------------------------------------------------------- 命令层
class TestNudgeCommands:
    def _repl(self, app):
        import io

        from freeagent.cli.app import Repl

        buf = io.StringIO()
        return Repl(app, out=buf), buf

    def test_list_says_nothing_is_due(self, env):
        app, _clock, role = env
        _waiting(app, role, follow_up_at=datetime(2026, 10, 25, 9, 0))
        repl, buf = self._repl(app)
        repl.handle("/nudge")
        text = buf.getvalue()
        assert "没有到点" in text
        assert "不会代发" in text, "命令输出本身也要声明不代发"

    def test_list_shows_direction_and_elapsed(self, env):
        app, _clock, role = env
        _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        repl, buf = self._repl(app)
        repl.handle("/nudge")
        text = buf.getvalue()
        assert "等对方" in text
        assert "已过 3 天" in text

    def test_number_drafts(self, env):
        app, _clock, role = env
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        repl, buf = self._repl(app)
        repl.handle("/nudge 1")
        text = buf.getvalue()
        assert task.title in text
        assert "[TODO 开头" in text
        assert "不会发这条" in text

    def test_id_prefix_also_works(self, env):
        """序号与 id 前缀两种写法都要支持 —— 与 /pending 同一个理由。"""
        app, _clock, role = env
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        repl, buf = self._repl(app)
        repl.handle(f"/nudge {task.id[:8]}")
        assert task.title in buf.getvalue()

    def test_numeric_only_id_prefix_is_not_mistaken_for_an_index(self, env):
        """回归：**纯数字的 id 前缀**不许被当成序号。

        ``new_id()`` 是纯 hex，可能是全数字（实测前缀 "61316741" 就是）。
        用 ``isdigit()`` 分流的话，它会被当成序号 61316741，于是
        「没有第 61316741 条」—— 用户敲的是合法 id，却被告知越界。

        所以判据必须是「**试过之后哪种解释成立**」，不是「像不像数字」。
        这里先构造出一个全数字前缀（真的造不出就跳过，不假装通过）。
        """
        app, _clock, role = env
        made: str | None = None
        for _ in range(300):
            task = _waiting(
                app, role, follow_up_at=datetime(2026, 10, 18, 9, 0),
                title=f"候补 {_}",
            )
            if task.id[:8].isdigit():
                made = task.id[:8]
                break
        if made is None:
            pytest.skip("300 次都没撞上全数字 id 前缀，改用等价的注入方式")
        repl, buf = self._repl(app)
        repl.handle(f"/nudge {made}")
        text = buf.getvalue()
        assert "没有第" not in text, f"合法 id 被误判成序号：{text!r}"
        assert "催办草稿" in text

    def test_out_of_range_index_is_explained(self, env):
        app, _clock, role = env
        # id 是随机 hex；`/nudge 5` 先试序号（越界）再试 id 前缀匹配，
        # 若某候选 id 恰好以 "5" 开头（1/16 概率），会被 id 前缀匹配到草稿
        # 而非「没有第 5 条」。所以要保证候选 id 不以 "5" 开头，序号 5
        # 才**确定**越界。重试几乎必成（15/16 每建一次就中）。
        for _ in range(100):
            made = _waiting(
                app, role, follow_up_at=datetime(2026, 10, 18, 9, 0)
            )
            if not made.id[:8].startswith("5"):
                break
        repl, buf = self._repl(app)
        repl.handle("/nudge 5")
        assert "没有第 5 条" in buf.getvalue()

    def test_unknown_prefix_says_look_at_list(self, env):
        app, _clock, role = env
        _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        repl, buf = self._repl(app)
        repl.handle("/nudge deadbeef")
        assert "没有匹配" in buf.getvalue()

    def test_nothing_to_nudge_is_not_an_error(self, env):
        """没到点时给序号要正常说话，不是崩、也不是空草稿。"""
        app, _clock, _role = env
        repl, buf = self._repl(app)
        repl.handle("/nudge 1")
        assert "没有到点的催办" in buf.getvalue()


class TestWaitLinksRelation:
    """``/wait <id> <等谁> 关系=名字`` —— 让等候也挂到关系上（20.1.5）。

    20.1.5 的原话就是「``follow_up`` 即可自然关联到「张总」而非只一条
    wait」。承诺那半边 V1.51 做了，等候这半边到这里补齐 ——
    只挂一半时，「我跟张总之间有几笔没结清的」仍然答不全。
    """

    def _repl(self, app):
        import io

        from freeagent.cli.app import Repl

        buf = io.StringIO()
        return Repl(app, out=buf), buf

    def test_relation_marker_links_the_contact(self, env):
        app, _clock, role = env
        task = _task(app, role)
        repl, buf = self._repl(app)
        repl.handle(f"/wait {task.id[:8]} 张总回合同 关系=张总")
        text = buf.getvalue()
        assert "张总回合同" in text
        assert "已挂到关系对象" in text
        waiting = app.tasks.get(task.id).waiting_on
        assert waiting is not None
        contact = app.promise.find_contact("张总")
        assert contact is not None
        assert waiting.contact_id == contact.id

    def test_without_marker_still_records(self, env):
        """不给 ``关系=`` 也能记 —— 不该要求用户先建一个人才准说「在等他」。"""
        app, _clock, role = env
        task = _task(app, role)
        repl, _buf = self._repl(app)
        repl.handle(f"/wait {task.id[:8]} 对方回话")
        waiting = app.tasks.get(task.id).waiting_on
        assert waiting is not None
        assert waiting.who_or_what == "对方回话"
        assert waiting.contact_id is None

    def test_empty_relation_is_rejected_not_ignored(self, env):
        """``关系=`` 后面空着要说清，不能静默当成「没给」。

        静默忽略的后果是用户以为挂上了、实际没挂，于是「按人聚合」里
        少一笔，而界面上看不出任何差别。
        """
        app, _clock, role = env
        task = _task(app, role)
        repl, buf = self._repl(app)
        repl.handle(f"/wait {task.id[:8]} 张总回合同 关系=")
        assert "关系=" in buf.getvalue()

    def test_contact_view_counts_both_sides(self, env):
        """关系列表里承诺与等候**并排计数** —— 两数才是完整的「没结清的账」。"""
        app, clock, role = env
        contact = app.promise.contact("张总")
        _waiting(
            app, role, follow_up_at=datetime(2026, 10, 18, 9, 0),
            contact_id=contact.id, title="等张总回合同",
        )
        clock.advance(seconds=1)
        task = app.tasks.create("给张总交付报价单", [role.id])
        app.promise.promise("报价单", contact_name="张总", task_id=task.id)
        repl, buf = self._repl(app)
        repl.handle("/contact")
        text = buf.getvalue()
        assert "1 条承诺未兑现" in text
        assert "1 笔在等他" in text