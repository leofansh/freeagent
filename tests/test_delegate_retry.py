"""两个真机发现的缺口，各配一条守卫。

1. **失败可重派** —— ``already_dispatched`` 原来只问「有没有派过」，
   于是失败任务永远变死任务，而落库的 note 偏偏写着「可以重派」。
2. **错因必须落库** —— ``_record_result`` 原来是 ``summary or detail``，
   而 summary 恒有值，于是 detail 从没进过库；真正的错因只在终端被截断。

第 2 条的测试刻意检查**产物全文里能搜到那句错** —— 因为这次的失败
（``Upstream request failed: Insufficient…``）就是被 500 字符上限切掉的，
而终端那一行是唯一出现过它的地方。
"""
import json
from datetime import datetime as _dt
from pathlib import Path

import pytest

from freeagent.app import build_app
from freeagent.delegate import run_once
from freeagent.domain import RecordType, ValidationError
from freeagent.services.clock import FrozenClock
from freeagent.services.delegate import (
    DelegationPolicy,
    already_dispatched,
    eligible_tasks,
)


class _RecordingRunner:
    def __init__(self, code=0, out=""):
        self.calls = []
        self._code, self._out = code, out

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        return self._code, self._out, self._out


class _AllowAllGate:
    def check(self, **kw):
        return None


def _wire(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    db = tmp_path / "a.db"
    app = build_app(db, clock=FrozenClock(_dt(2026, 9, 29, 18, 0)))
    role = app.roles.create("工作")
    task = app.tasks.create("做点事", [role.id], project_path=str(project))
    app.tasks.start(task.id)
    app.close()
    return app, db, project, task, DelegationPolicy(projects=(str(project),))


class _Rec:
    def __init__(self, type_):
        self.type = type_


class TestFailureIsRetryable:
    """规则：最后一条是 ``delegation_failed`` ⇒ **可重派**。"""

    def test_never_dispatched_is_eligible(self):
        assert already_dispatched([]) is False

    def test_in_flight_is_not_redispatched(self):
        """``dispatched`` 之后没有结论 ⇒ **别再派一次**。

        ``--watch`` 每轮都扫，而 opencode 可能跑几分钟。不挡住就会
        连发好几个进程改同一个目录。
        """
        assert already_dispatched([_Rec(RecordType.DELEGATION_DISPATCHED)]) is True

    def test_succeeded_is_not_redispatched(self):
        recs = [
            _Rec(RecordType.DELEGATION_DISPATCHED),
            _Rec(RecordType.DELEGATION_SUCCEEDED),
        ]
        assert already_dispatched(recs) is True

    def test_failed_is_retryable(self):
        """**这条是本次修的那个 bug。**

        原来只问「有没有 dispatched」，于是失败即死任务 ——
        而落库的 note 写着「可以 /note 记下原因后重派」。
        文档承诺了，守卫禁止了。
        """
        recs = [
            _Rec(RecordType.DELEGATION_DISPATCHED),
            _Rec(RecordType.DELEGATION_FAILED),
        ]
        assert already_dispatched(recs) is False, "失败后必须可重派"

    def test_retry_after_failure_then_dispatch_blocks_again(self):
        """重派后再次「在跑」⇒ 又不能派了。

        顺序敏感：只看「有没有失败过」会放进第二个并发进程。
        """
        recs = [
            _Rec(RecordType.DELEGATION_DISPATCHED),
            _Rec(RecordType.DELEGATION_FAILED),
            _Rec(RecordType.DELEGATION_DISPATCHED),
        ]
        assert already_dispatched(recs) is True

    def test_real_retry_actually_dispatches_twice(self, tmp_path):
        """端到端：失败一次之后，**再跑一次真的会派**。"""
        _app, db, _project, task, policy = _wire(tmp_path)

        fail = _RecordingRunner(code=1, out="boom")
        r1 = run_once(db_path=db, runner=fail, policy=policy, gate=_AllowAllGate())
        assert r1.failed == 1

        app2 = build_app(db)
        assert len(eligible_tasks(app2.task_repo, app2.record_repo)) == 1, (
            "失败后应当仍够格被派 —— 这条不成立就等于没修"
        )
        app2.close()

        ok = _RecordingRunner(
            out=json.dumps({"type": "text", "text": "这回成了", "sessionID": "s9"})
        )
        r2 = run_once(db_path=db, runner=ok, policy=policy, gate=_AllowAllGate())
        assert len(ok.calls) == 1, "第二次应当真的执行了"
        assert r2.succeeded == 1, r2.notes


class TestPreDispatchRefusalIsNotRetriedForever:
    """``--watch`` 实测抓到的缺陷（2026-09-30）：配置类失败被无限重试。

    现象：常驻执行器 180 秒扫了 60 轮、写了 60 个产物版本（v51→v60），
    而真正的委派一条没干成 —— 因为每轮都撞同一堵墙（缺飞书凭据 → 无闸门）。

    ``already_dispatched`` 的 docstring 早就预警过「持续失败会刷卡片」，
    但只覆盖了 ``--tool-gate`` 路径。**无闸门时一张卡都没有**，
    代价从「刷屏」变成**静默的数据库膨胀** —— 那句预警完全没提到它。

    根因：「失败可重派」被无条件化了。可有些失败是**确定性**的：
    阻塞在任务之外（缺闸门、白名单不符），不改配置就永远不会成功。
    """

    def _records(self, *types):
        return [_Rec(t) for t in types]

    def test_predispatch_refusal_is_not_retryable(self):
        # 真机形状：只有 failed，**前面没有** dispatched
        assert already_dispatched(
            self._records(RecordType.DELEGATION_FAILED)
        ) is True

    def test_execution_failure_is_still_retryable(self):
        # 真机形状：dispatched -> failed。这一类**必须**还能重派，
        # 否则又回到「一次失败变死任务」那个旧坑。
        assert already_dispatched(
            self._records(RecordType.DELEGATION_DISPATCHED,
                          RecordType.DELEGATION_FAILED)
        ) is False

    def test_never_dispatched_still_eligible(self):
        assert already_dispatched([]) is False

    def test_succeeded_still_blocks(self):
        assert already_dispatched(
            self._records(RecordType.DELEGATION_DISPATCHED,
                          RecordType.DELEGATION_SUCCEEDED)
        ) is True

    def test_in_flight_still_blocks(self):
        assert already_dispatched(
            self._records(RecordType.DELEGATION_DISPATCHED)
        ) is True

    def test_refusal_then_real_attempt_keeps_retry_semantics(self):
        """先被拒、后来真跑过并失败 —— 应当**恢复可重派**。

        否则「一次误操作导致永久卡死」：人修好配置重试一次，
        又因为历史里有条 refused 而再也派不出去。
        """
        assert already_dispatched(
            self._records(RecordType.DELEGATION_FAILED,
                          RecordType.DELEGATION_DISPATCHED,
                          RecordType.DELEGATION_FAILED)
        ) is False

    def test_repeated_refusals_do_not_accumulate(self, tmp_path):
        """真机那 60 轮的形状：反复 refused 之后**不该**再多派一次。"""
        app = build_app(tmp_path / "a.db", clock=FrozenClock(_dt(2026, 9, 30, 17)))
        role = app.roles.create("工作")
        task = app.tasks.create("活儿", [role.id], project_path=str(tmp_path))
        app.tasks.start(task.id)
        for _ in range(3):
            app.record_repo.append(task.id, RecordType.DELEGATION_FAILED,
                                   "没有闸门", app.clock.now())
        assert eligible_tasks(app.task_repo, app.record_repo) == []
        app.close()


class TestErrorReasonReachesTheDatabase:
    """错因必须**落库**，而不只是出现在终端那一行。"""

    #: 一条**合法**的 opencode 错误 JSON，形状取自实测那次失败。
    #: ``trace`` 字段刻意很长，模拟「错因正好落在 300 字符之后」——
    #: 上次就是被那个上限切掉的，而这是最容易复发的地方。
    REAL = (
        '{"type":"error","timestamp":1790672420960,'
        '"sessionID":"ses_f139b7ce1ffepdBRSe4JqUD7c3",'
        '"error":{"name":"APIError",'
        '"data":{"trace":"' + "x" * 600 + '",'
        '"message":"Upstream request failed: Insufficient account funds"}}}'
    )
    #: 同一条，但**不带**那条长 trace —— 最常见的形态。
    SHORT = (
        '{"type":"error","sessionID":"ses_abc",'
        '"error":{"name":"APIError",'
        '"data":{"message":"Upstream request failed: Insufficient account funds"}}}'
    )

    def test_artifact_contains_the_real_reason(self, tmp_path):
        _app, db, _project, task, policy = _wire(tmp_path)
        run_once(
            db_path=db, runner=_RecordingRunner(code=1, out=self.REAL),
            policy=policy, gate=_AllowAllGate(),
        )
        app = build_app(db)
        arts = app.artifacts.list_for_task(task.id)
        app.close()
        body = arts[-1].content
        assert "Insufficient account funds" in body, (
            "产物链里搜不到真正的错因 —— 下次失败没法自己查"
        )
        assert "失败详情" in body, "失败时应当带上 detail 块"
        assert "xxx" in body, "detail 应当整体落库（不只是尾部）"

    def test_record_note_carries_the_reason(self, tmp_path):
        """/note、/task 看到的应该是错因，不是「退出码 1」。

        这是本次修的第二个 bug：记录上限 300 字符，而错因在长 trace 之后
        ——于是记录里只剩「退出码 1（xxxx…）」，用户无法据此行动。
        """
        _app, db, _project, task, policy = _wire(tmp_path)
        run_once(
            db_path=db, runner=_RecordingRunner(code=1, out=self.REAL),
            policy=policy, gate=_AllowAllGate(),
        )
        app = build_app(db)
        recs = [
            r.content for r in app.record_repo.list_for_task(task.id)
            if r.type is RecordType.DELEGATION_FAILED
        ]
        app.close()
        assert recs, "没有失败记录"
        assert "Insufficient" in recs[0], (
            f"失败记录里没有错因：{recs[0]!r}"
        )
        assert "xxx" not in recs[0], "记录里应当是错因，而不是一堆 padding"

    def test_short_error_also_reaches_the_record(self, tmp_path):
        """最常见的形态（错因就在前面）也必须落库。"""
        _app, db, _project, task, policy = _wire(tmp_path)
        run_once(
            db_path=db, runner=_RecordingRunner(code=1, out=self.SHORT),
            policy=policy, gate=_AllowAllGate(),
        )
        app = build_app(db)
        recs = [
            r.content for r in app.record_repo.list_for_task(task.id)
            if r.type is RecordType.DELEGATION_FAILED
        ]
        app.close()
        assert "Insufficient" in recs[0], recs[0]

    def test_plain_text_error_still_reaches_the_record(self, tmp_path):
        """非 JSON 的失败（shell 报错等）退回取首行 —— 别把信息弄丢了。"""
        _app, db, _project, task, policy = _wire(tmp_path)
        run_once(
            db_path=db,
            runner=_RecordingRunner(code=2, out="fatal: not a git repository"),
            policy=policy, gate=_AllowAllGate(),
        )
        app = build_app(db)
        recs = [
            r.content for r in app.record_repo.list_for_task(task.id)
            if r.type is RecordType.DELEGATION_FAILED
        ]
        app.close()
        assert "not a git repository" in recs[0], recs[0]

    def test_success_path_does_not_get_a_failure_block(self, tmp_path):
        """成功时不许塞「失败详情」块 —— 那是噪音。"""
        _app, db, _project, task, policy = _wire(tmp_path)
        run_once(
            db_path=db,
            runner=_RecordingRunner(
                out=json.dumps({"type": "text", "text": "做完了", "sessionID": "s1"})
            ),
            policy=policy, gate=_AllowAllGate(),
        )
        app = build_app(db)
        body = app.artifacts.list_for_task(task.id)[-1].content
        app.close()
        assert "失败详情" not in body


class TestManualRedispatch:
    """``/redispatch``：人显式要求重来（设计文档 11.8 那条已知缺口）。

    ## 为什么需要这一整类

    「派发前就被拒」被**刻意不自动重派** —— 改配置前重试多少次都是同一个
    结果，而 ``--watch`` 实测过那个坑：180 秒扫 60 轮、写 60 个产物版本，
    真正的委派一条没干成。

    但那是「没改配置」的判定。用户改好配置之后总得有办法让它重来，而落库
    那句「可以 /note 记下原因后重派」是**空头承诺**：``note`` 对判定完全
    不可见。于是那条只能变成死任务。

    这类测试锁的是**恢复入口达成度**，不是机制 —— 机制那侧
    （``TestPreDispatchRefusalIsNotRetriedForever`` 已经锁住了「没有 reset
    记录时绝不放行」）不能被本类放宽。
    """

    def _records(self, *types):
        return [_Rec(t) for t in types]

    # ---- 纯形状：不碰数据库 ------------------------------------------------ #

    def test_reset_unlocks_a_refused_attempt(self):
        """派发前被拒 + 人要求重来 ⇒ 可以派了。"""
        assert already_dispatched(
            self._records(RecordType.DELEGATION_FAILED,
                          RecordType.DELEGATION_RESET)
        ) is False, "reset 之后必须可派，否则恢复入口没生效"

    def test_reset_is_a_noop_without_a_previous_attempt(self):
        """没派过就 reset 不该凭空造出一次「已尝试」—— 它本来就该派。"""
        assert already_dispatched(
            self._records(RecordType.DELEGATION_RESET)
        ) is False

    def test_reset_does_NOT_unlock_a_running_delegation(self):
        """⚠️ **最关键的一条**：reset **不许**解除「正在跑」。

        它只清「上次结局」，不清「在跑」。若一并清掉，就放行了一个还在跑
        的委派 —— 而 ``--watch`` 每轮都扫，于是**两个进程改同一个项目目录**。
        那正是 dangling 检查当初要防的事（第一版漏了它，回归测试当场抓住）。

        所以 ``[failed, dispatched, reset]`` 仍判「别派」。
        """
        assert already_dispatched(
            self._records(RecordType.DELEGATION_FAILED,
                          RecordType.DELEGATION_DISPATCHED,
                          RecordType.DELEGATION_RESET)
        ) is True, "reset 绝不能放行正在跑的委派"

    def test_reset_does_not_unlock_succeeded(self):
        """已成功的也不该被 reset 放行 —— 判定侧保守，放行由服务层校验把关。

        这条锁的是**判定侧不越权**：真正的拒绝理由在
        :meth:`reset_delegation`，那里给的是一句面向用户的话。
        """
        assert already_dispatched(
            self._records(RecordType.DELEGATION_DISPATCHED,
                          RecordType.DELEGATION_SUCCEEDED,
                          RecordType.DELEGATION_RESET)
        ) is True

    def test_reset_then_really_dispatched_blocks_again(self):
        """reset 后真的又派了一次 ⇒ 又进入「在跑」，必须重新锁上。

        顺序敏感：只清「有没有失败过」会放进第二个并发进程。
        """
        assert already_dispatched(
            self._records(RecordType.DELEGATION_FAILED,
                          RecordType.DELEGATION_RESET,
                          RecordType.DELEGATION_DISPATCHED)
        ) is True

    # ---- 服务层校验：三条拒绝必须**互相可区分** ---------------------------- #

    def test_refuses_a_running_delegation(self, tmp_path):
        _app, db, _project, task, _policy = _wire(tmp_path)
        # ``_wire`` 返回的 app 已经 close() 了（既有测试都自己 build_app 重开），
        # 复用它会撞「Cannot operate on a closed database」。
        app = build_app(db, clock=FrozenClock(_dt(2026, 9, 30, 17)))
        app.record_repo.append(task.id, RecordType.DELEGATION_DISPATCHED,
                               "派出去了", app.clock.now())
        with pytest.raises(ValidationError) as exc:
            app.tasks.reset_delegation(task.id)
        assert "正在跑" in str(exc.value)
        app.close()

    def test_refuses_a_succeeded_delegation(self, tmp_path):
        _app, db, _project, task, _policy = _wire(tmp_path)
        app = build_app(db, clock=FrozenClock(_dt(2026, 9, 30, 17)))
        app.record_repo.append(task.id, RecordType.DELEGATION_DISPATCHED,
                               "派出去了", app.clock.now())
        # ⚠️ 必须**推进时钟**再落第二条：``list_for_task``按 ``ts ASC, id ASC``
        # 排序，而 ``id`` 是随机的 —— 同一时刻的两条记录顺序**不确定**。
        # 不推进的话这条测试是「有时过有时不过」，而那种 flaky 会被当成
        # 偶发而不被追。
        app.clock.advance(seconds=1)
        app.record_repo.append(task.id, RecordType.DELEGATION_SUCCEEDED,
                               "做完了", app.clock.now())
        with pytest.raises(ValidationError) as exc:
            app.tasks.reset_delegation(task.id)
        assert "做完" in str(exc.value)
        app.close()

    def test_refuses_when_never_dispatched(self, tmp_path):
        _app, db, _project, task, _policy = _wire(tmp_path)
        app = build_app(db, clock=FrozenClock(_dt(2026, 9, 30, 17)))
        with pytest.raises(ValidationError) as exc:
            app.tasks.reset_delegation(task.id)
        assert "还没派出去过" in str(exc.value)
        app.close()

    def test_refuses_a_non_delegation_task(self, tmp_path):
        """普通事务没有「重派」这回事 —— 理由必须与上面三条不同。

        四个理由若都含糊成一句「不能重派」，用户不知道自己该做什么。
        """
        app = build_app(tmp_path / "a.db", clock=FrozenClock(_dt(2026, 9, 30, 17)))
        role = app.roles.create("工作")
        plain = app.tasks.create("普通事", [role.id])
        with pytest.raises(ValidationError) as exc:
            app.tasks.reset_delegation(plain.id)
        assert "不是委派事务" in str(exc.value)
        app.close()

    # ---- 端到端：恢复入口真的让执行器再派一次 -------------------------------- #

    def test_end_to_end_reset_lets_the_executor_dispatch_again(self, tmp_path):
        """被拒 → reset → **真的再派一次**（不是只改了判定）。

        三步都要成立，否则就是「命令说成功了但库里那条还是不让派」——
        症状是用户以为修好了，扫 sixty 轮什么也没发生。
        """
        _app, db, _project, task, policy = _wire(tmp_path)

        # 1) 真机形状的「派发前就被拒」：只有 failed，前面没有 dispatched。
        app = build_app(db, clock=FrozenClock(_dt(2026, 9, 30, 17, 1)))
        app.record_repo.append(task.id, RecordType.DELEGATION_FAILED,
                               "项目不在白名单里", app.clock.now())
        assert eligible_tasks(app.task_repo, app.record_repo) == [], (
            "前提不成立：refused 本来就该被挡住"
        )

        # 2) 人改好配置后要求重来。
        #    ⚠️ 先推进时钟：``reset_delegation`` 内部用 ``clock.now()``，
        #    与上面那条 failed 会是同一刻。而 ``list_for_task`` 按
        #    ``ts ASC, id ASC`` 排、``id`` 随机 —— 不推进就可能读到
        #    「reset 在前、failed 在后」，于是又变回 refused，这条测试
        #    就变成「有时过有时不过」。
        app.clock.advance(seconds=1)
        app.tasks.reset_delegation(task.id, "白名单加好了")
        assert len(eligible_tasks(app.task_repo, app.record_repo)) == 1, (
            "reset 之后它必须重新够格 —— 这条不成立就等于没做恢复入口"
        )
        app.close()

        # 3) 执行器真的跑起来了。
        ok = _RecordingRunner(
            out=json.dumps({"type": "text", "text": "这回成了", "sessionID": "s10"})
        )
        report = run_once(db_path=db, runner=ok, policy=policy, gate=_AllowAllGate())
        assert len(ok.calls) == 1, "执行器应当真的又派了一次"
        assert report.succeeded == 1, report.notes

    def test_reset_is_recorded_with_its_reason(self, tmp_path):
        """reset 落进只追加日志：谁、何时、为何要求重来必须可查。

        不落库就等于「这次重派没有出处」，而审计一条委派为什么重跑
        是排查重复副作用的前提。
        """
        _app, db, _project, task, _policy = _wire(tmp_path)
        app = build_app(db, clock=FrozenClock(_dt(2026, 9, 30, 17)))
        app.record_repo.append(task.id, RecordType.DELEGATION_FAILED,
                               "没有闸门", app.clock.now())
        app.tasks.reset_delegation(task.id, "配好闸门了")
        resets = [r for r in app.record_repo.list_for_task(task.id)
                  if r.type is RecordType.DELEGATION_RESET]
        app.close()
        assert len(resets) == 1, "reset 应当恰好落一条"
        assert "配好闸门了" in resets[0].content
        assert "refused" in resets[0].content, (
            "记录里应当写清上次是什么结局，否则事后看不出重派理由"
        )

    def test_reason_is_optional(self, tmp_path):
        """不写原因也该能用 —— 强制填一个「随便某句话」只会让人瞎填。"""
        _app, db, _project, task, _policy = _wire(tmp_path)
        app = build_app(db, clock=FrozenClock(_dt(2026, 9, 30, 17)))
        app.record_repo.append(task.id, RecordType.DELEGATION_FAILED,
                               "没有闸门", app.clock.now())
        app.tasks.reset_delegation(task.id)
        resets = [r for r in app.record_repo.list_for_task(task.id)
                  if r.type is RecordType.DELEGATION_RESET]
        app.close()
        assert len(resets) == 1
        assert resets[0].content.strip(), "内容不能是空串"
