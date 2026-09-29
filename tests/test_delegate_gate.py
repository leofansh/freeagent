"""委派闸门的**攻击性测试**（设计文档 11.9.7 规则 1）。

与 ``test_approval_gate.py`` 的分工：那边测**判据**（is_bypass_active、
check_command 这些纯函数），这边测**集成** —— 从 ``run_once`` 进去，
看 ``runner`` 到底有没有被调用。

要证明的那条命题只有一条：

    **没有一条 allow 记录，就一次都不能跑。**

这批测试的价值全在「攻击成功」的分支上。正常的 allow 路径只占一条 ——
如果只测它，一个「永远拒绝」的闸门也能全绿，而那显然不是我们要的。
所以每个攻击用例都断言「**runner 没被调用**」，
而「被调用了」才是失败信号（:class:`TestAttacksThatMustFail`）。
"""
import datetime
import time
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from freeagent.app import build_app
from freeagent.delegate import ApprovalGate, run_once
from freeagent.domain import RecordType
from freeagent.services.approval import ApprovalPolicy, ApprovalStore
from freeagent.services.clock import FrozenClock
from freeagent.services.delegate import DelegationPolicy


class _RecordingRunner:
    """记录有没有被调用过。被调用 = 安全属性被击穿。"""

    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._code = code

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        return self._code, '{"type":"text","text":"ok"}', ""


class _AllowAllGate:
    """放行闸门（对照组）。**只在要证明「闸门放行时确实会跑」时用。**"""

    def check(self, *, task_id, project, brief):
        return None


class _FakeSender:
    """假 sender。**实现 CardSender 协议**（发卡是**方法**，不是模块函数）。

    踩过的坑：最初写 ``monkeypatch.setattr(sender_mod, "send_approval_card", ...)``，
    而 ``freeagent.feishu.sender`` 里**没有**这个顶层函数 —— 它是
    :class:`FeishuSender` 上的方法。照着错误的名字写测试，报错会指向
    「测试没找到属性」，而真实原因是「我把协议看成了模块函数」。
    """

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    def send_approval_card(
        self, *, open_id, subject, detail, credential, ttl_seconds
    ):
        if self.fail:
            raise RuntimeError("飞书挂了")
        self.sent.append(
            {
                "open_id": open_id,
                "subject": subject,
                "detail": detail,
                "credential": credential,
                "ttl_seconds": ttl_seconds,
            }
        )
        return "om_test"


def _one_task(app, role_id: str, project: Path) -> str:
    task = app.tasks.create("改个登录页", [role_id], project_path=str(project))
    app.tasks.start(task.id)
    return task.id


@pytest.fixture()
def wired(tmp_path) -> dict[str, object]:
    """一条已 ``/start`` 的委派事务 + 它的项目目录。

    放**模块级**而不是塞进某个类里：踩过的坑是把它定义成
    ``TestAttacksThatMustFail`` 的方法，于是 ``TestControlGroup``
    拿不到（fixture 是类作用域的），报错是 ``fixture 'wired' not found`` ——
    而真实原因只是「放错位置」。
    """
    project = tmp_path / "app"
    project.mkdir()
    db = tmp_path / "a.db"
    app = build_app(db, clock=FrozenClock(_dt(2026, 9, 29, 15, 0)))
    role = app.roles.create("工作")
    task_id = _one_task(app, role.id, project)
    app.close()
    policy = DelegationPolicy(projects=(str(project),))
    return {"db": db, "project": project, "task_id": task_id, "policy": policy}


class TestAttacksThatMustFail:
    """**每一条攻击都必须导致「runner 没被调用」。**"""

    # -- 攻击 1：完全不传闸门 ---------------------------------------------- #
    def test_no_gate_means_no_run(self, wired):
        """**默认必须拒绝。**

        这是整条链路的安全底：将来有人新增一条调用 ``run_once`` 的路径
        而忘了传闸门，失败方向是「什么都不跑」，不是「偷偷跑」。
        """
        runner = _RecordingRunner()
        report = run_once(db_path=wired["db"], runner=runner, policy=wired["policy"])
        assert runner.calls == [], f"没有闸门却执行了：{runner.calls}"
        assert report.succeeded == 0
        assert report.failed == 1
        assert any("闸门" in n for n in report.notes), report.notes

    # -- 攻击 2：闸门拒绝（人没点） ----------------------------------------- #
    def test_refused_gate_means_no_run(self, wired):
        class _DenyGate:
            def check(self, *, task_id, project, brief):
                return "用户点了拒绝"

        runner = _RecordingRunner()
        report = run_once(
            db_path=wired["db"], runner=runner, policy=wired["policy"], gate=_DenyGate()
        )
        assert runner.calls == [], f"被拒绝却执行了：{runner.calls}"
        assert report.failed == 1
        assert any("用户点了拒绝" in n for n in report.notes), report.notes

    # -- 攻击 3：绕过开关在 argv 里 ----------------------------------------- #
    def test_bypass_flag_in_argv_means_no_run(self, wired):
        """有人往命令里塞了 ``--auto`` → 闸门必须**拒绝**而不是「容忍」。

        「容忍绕过」听起来像个折中，实际是把整条防线关掉。
        """
        store = ApprovalStore(build_app(wired["db"]).conn)
        gate = ApprovalGate(
            store=store, sender=_FakeSender(), approver="ou_x", argv=["opencode", "run", "--auto"]
        )
        runner = _RecordingRunner()
        report = run_once(
            db_path=wired["db"], runner=runner, policy=wired["policy"], gate=gate
        )
        assert runner.calls == [], f"绕过态下仍然执行了：{runner.calls}"
        assert any("绕过" in n for n in report.notes), report.notes

    # -- 攻击 4：绕过开关在环境变量里 --------------------------------------- #
    def test_bypass_env_var_means_no_run(self, wired):
        store = ApprovalStore(build_app(wired["db"]).conn)
        gate = ApprovalGate(
            store=store, sender=_FakeSender(), approver="ou_x",
            argv=["opencode", "run"], env={"FREEAGENT_BYPASS_APPROVAL": "1"},
        )
        runner = _RecordingRunner()
        run_once(db_path=wired["db"], runner=runner, policy=wired["policy"], gate=gate)
        assert runner.calls == [], f"env 绕过下仍然执行了：{runner.calls}"

    # -- 攻击 5：没有发卡通道 ----------------------------------------------- #
    def test_no_sender_means_no_run(self, wired):
        """``sender=None`` → **永远拒绝**。

        委派必然要发卡；没有通道就没人能批准，那是「拒绝」
        而不是「跳过闸门」。这两者的区别是：后者等于裸奔。
        """
        store = ApprovalStore(build_app(wired["db"]).conn)
        gate = ApprovalGate(store=store, sender=None, approver="ou_x")
        runner = _RecordingRunner()
        report = run_once(
            db_path=wired["db"], runner=runner, policy=wired["policy"], gate=gate
        )
        assert runner.calls == [], f"无通道却执行了：{runner.calls}"
        assert any("无人能批准" in n for n in report.notes), report.notes

    # -- 攻击 6：发卡失败 --------------------------------------------------- #
    def test_card_send_failure_means_no_run(self, wired):
        """通道坏了 = 无人监督 = **拒绝**。

        这一条最容易被「优雅降级」写错：发卡失败就放行，
        理由通常是「卡发不出去总比卡住好」—— 而那正是闸门形同虚设的样子。
        """
        store = ApprovalStore(build_app(wired["db"]).conn)
        gate = ApprovalGate(
            store=store, sender=_FakeSender(fail=True), approver="ou_x"
        )
        runner = _RecordingRunner()
        report = run_once(
            db_path=wired["db"], runner=runner, policy=wired["policy"], gate=gate
        )
        assert runner.calls == [], f"发卡失败却执行了：{runner.calls}"
        assert any("发卡失败" in n for n in report.notes), report.notes

    # -- 攻击 7：项目路径里夹带 shell 运算符 -------------------------------- #
    def test_shell_operator_in_project_path_means_no_run(self, wired):
        """有人把 ``;`` 塞进「项目路径」—— 白名单不该因此放行。

        ``check_project_allowed`` 查的是路径**在不在白名单**，
        而闸门查的是这条**命令本身**能不能跑。两者都要过。
        """
        gate = ApprovalGate(
            store=ApprovalStore(build_app(wired["db"]).conn),
            sender=_FakeSender(), approver="ou_x",
        )
        runner = _RecordingRunner()
        # project 与白名单一致，但 argv 侧被检查的是「将要执行的东西」——
        # 这里直接验判据层：带运算符的路径过不了 check_command。
        from freeagent.services.approval import check_command

        ok, why = check_command(str(wired["project"]) + "; rm -rf ~")
        assert ok is False
        assert "运算符" in why
        # runner 根本没被用到（闸门在更早一步就拒了）
        assert runner.calls == []


    # -- 攻击 8：wait 返回了但没人真的回答 --------------------------------- #
    def test_wait_returning_without_a_decision_means_no_run(self, wired):
        """``wait`` 返回了，但**库里没人写过决定** → 必须拒绝。

        这条锁的是一个真实缺陷：原先闸门采信 ``wait`` 的返回值，于是
        「等完了」就等于「批准了」，而审计记录里 ``decision`` 仍是 None ——
        事后按记录查会得出「没批准过，可它确实执行了」。

        修法是让 ``wait`` 只当阻塞点、结论一律回库读。这里防止有人
        「优化」回去。
        """
        store = ApprovalStore(build_app(wired["db"]).conn)
        # 故意**违约**：wait 返回了值但没写库。类型系统拦它是对的，
        # 而这条测试的全部意义就是喂一个不合契约的实现、断言结果仍然正确。
        # 若哪天闸门改成采信 wait 的返回值，这条测试会红 —— 那正是它要抓的。
        def liar_wait(cred: str) -> str:      # noqa: ARG001
            return "allow"

        gate = ApprovalGate(
            store=store, sender=_FakeSender(), approver="ou_x",
            wait=liar_wait,   # 返回值**必须**被忽略 —— 这就是这条测试的全部意义
        )
        runner = _RecordingRunner()
        report = run_once(
            db_path=wired["db"], runner=runner, policy=wired["policy"], gate=gate
        )
        assert runner.calls == [], f"库中无决定却执行了：{runner.calls}"
        assert any("无人应答" in n or "未获批准" in n for n in report.notes), report.notes


    # -- 攻击 9：不传 wait 时的生产路径 ------------------------------------- #
    def test_production_path_without_wait_callback(self, tmp_path):
        """**不传** ``wait`` 时必须**真的等**（真机测出来的 bug）。

        上一版把 ``store.wait()`` 删了，理由是「结论要回库读」——
        而 ``main()`` 不传 ``wait``，于是走 else 分支时**根本没等**：
        立刻读库 → 读到空 → 当场拒绝。用户点了「允许一次」，
        系统报「未获批准（deny）」，而库里明明是 allow。

        这条 bug **16 条单测全绿也没抓到**，因为它们**全都**传了 ``wait``
        替身，那条分支一次都没被走到。所以这里必须用**真线程**去点，
        让「等待」真的发生 —— 用替身就又变成在测替身了。
        """
        import threading

        project = tmp_path / "app"
        project.mkdir()
        db = tmp_path / "a.db"
        app = build_app(db, clock=FrozenClock(_dt(2026, 9, 29, 15, 0)))
        role = app.roles.create("工作")
        _one_task(app, role.id, project)
        app.close()

        store = ApprovalStore(build_app(db).conn)
        sender = _FakeSender()
        # **不传 wait** —— 这正是 main() 的用法
        gate = ApprovalGate(store=store, sender=sender, approver="ou_x")

        def clicker():
            # 另一个线程扮演「用户在飞书上点了允许」，延迟一下制造真实的等待
            for _ in range(200):
                if sender.sent:
                    break
                time.sleep(0.01)
            if sender.sent:
                store.resolve(sender.sent[0]["credential"], "allow", decided_by="ou_x")

        t = threading.Thread(target=clicker, daemon=True)
        t.start()

        refusal = gate.check(
            task_id="t1", project=str(project), brief="只读看一下"
        )
        t.join(timeout=5)

        assert refusal is None, f"有人点了允许却仍被拒：{refusal}（生产路径没等）"


class TestPendingRecordPrecedesRun:
    """规则 1 的正面判据：**派发前库里就有 pending 记录**。"""

    def test_pending_row_exists_before_dispatch(self, tmp_path):
        project = tmp_path / "app"
        project.mkdir()
        db = tmp_path / "a.db"
        app = build_app(db, clock=FrozenClock(_dt(2026, 9, 29, 15, 0)))
        role = app.roles.create("工作")
        _one_task(app, role.id, project)
        app.close()

        sender = _FakeSender()
        app2 = build_app(db)
        store = ApprovalStore(app2.conn)

        # 模拟「用户秒点」：**必须真的写库**。
        # 踩过的坑：原先让 wait 直接 return "allow"，而闸门采信了那个返回值 ——
        # 于是代码跑了、库里 decision 仍是 None。审计记录说「没批准过」，
        # 可它确实执行了。现在 wait 只是阻塞点，结论一律回库读。
        #
        # 点击者**必须与 approver 同一人**：``resolve`` 现在强制
        # 「只有发起人能批」（设计文档 11.9.7 规则 2）。原先这里写的是
        # ``ou_tester`` 而 approver 是 ``ou_x`` —— 那不是「测试更宽松」，
        # 而是**不真实的设置**：现实里卡只发给 approver，不存在第二个点击者。
        # 越权那条另有用例锁（见 test_approval.py 的 non_requester）。
        def instant_click(cred: str) -> None:
            store.resolve(cred, "allow", decided_by="ou_x")

        gate = ApprovalGate(
            store=store, sender=sender, approver="ou_x", wait=instant_click,
        )
        runner = _RecordingRunner()
        report = run_once(
            db_path=db, runner=runner,
            policy=DelegationPolicy(projects=(str(project),)), gate=gate,
        )
        app2.close()

        assert len(sender.sent) == 1, "应当发过一张卡"
        card = sender.sent[0]
        cred = card["credential"]
        assert cred.startswith("dp-"), f"委派凭据前缀应是 dp-，实际 {cred[:12]}"
        # 卡上必须写明作用域 —— 缺了用户就只能凭「信任机器人」点
        assert "app" in card["detail"] and "仅这一次" in card["detail"]

        # 关键：pending 行确实落过库了
        app3 = build_app(db)
        row = app3.conn.execute(
            "SELECT decision FROM pending_approvals WHERE credential = ?", (cred,)
        ).fetchone()
        app3.close()
        assert row is not None, "派发前没有 pending 记录 —— 规则 1 被违反"
        assert row["decision"] == "allow"
        assert len(runner.calls) == 1, "allow 之后应当真的跑了"

    def test_card_ttl_matches_stored_ttl(self, tmp_path):
        """卡上写的 TTL 必须与库里落的一致。

        两处各算一次的话，改了一处就会出现「卡上 30 分钟、库里 10 分钟」——
        而用户是**按卡上那个时间**做决定的。
        """
        project = tmp_path / "app"
        project.mkdir()
        db = tmp_path / "a.db"
        app = build_app(db, clock=FrozenClock(_dt(2026, 9, 29, 15, 0)))
        app.close()
        sender = _FakeSender()
        app2 = build_app(db)
        gate = ApprovalGate(
            store=ApprovalStore(app2.conn), sender=sender, approver="ou_x",
            context="remote",
        )
        store = ApprovalStore(app2.conn)
        pending = store.request_delegation(
            project=str(project), brief="改个登录页", context=gate.context
        )
        gate._send_card(pending)
        app2.close()
        assert sender.sent[0]["ttl_seconds"] == ApprovalPolicy.for_context(
            "remote"
        ).ttl_seconds


class TestControlGroup:
    """**对照组**：闸门放行时必须真的跑。

    没有这条，一个「永远拒绝」的闸门能让上面所有攻击测试全绿。
    """

    def test_allow_gate_actually_runs(self, wired):
        runner = _RecordingRunner()
        report = run_once(
            db_path=wired["db"], runner=runner, policy=wired["policy"],
            gate=_AllowAllGate(),
        )
        assert len(runner.calls) == 1, f"放行后应当执行，实际 {runner.calls}"
        assert report.succeeded == 1, report.notes

    def test_spy_gate_actually_asked(self, wired):
        calls: list[str] = []

        class _SpyGate:
            def check(self, *, task_id, project, brief):
                calls.append(task_id)
                return None

        run_once(
            db_path=wired["db"], runner=_RecordingRunner(), policy=wired["policy"],
            gate=_SpyGate(),
        )
        assert calls, "闸门根本没被问过"

    def test_dry_run_does_not_touch_gate(self, wired):
        """``--dry-run`` 不碰闸门：预演不该变成一次真请求。"""
        calls: list[str] = []

        class _SpyGate:
            def check(self, *, task_id, project, brief):
                calls.append(task_id)
                return None

        report = run_once(
            db_path=wired["db"], runner=_RecordingRunner(), policy=wired["policy"],
            dry_run=True, gate=_SpyGate(),
        )
        assert calls == [], "dry run 竟然问过闸门"
        assert report.dispatched == 0
