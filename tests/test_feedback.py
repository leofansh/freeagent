"""执行层反馈回流（2026-10-17）：从只追加日志里读出「派得怎么样」。

## 这个文件在守什么

一次架构自审发现两件事，它们其实是**同一件事的两面**：

1. 委派失败只在任务上留一条 ``delegation_failed`` 文本。第三次踩同一个坑
   不会被提示 —— 那些记录**躺在库里，没人读**。
2. 产出验收只改 artifact 状态 + 追加一条记录，**不影响任何排序信号、
   不进恢复契约**。于是「上次这么做被要求改了两次」这个事实，
   对下一次派发毫无影响。

合起来：**执行层的信息没有系统性地回流到治理层。**
而那是「Agent 之上的 Agent」最核心的动作 —— 上层存在的意义就是
从下层的结果里学到东西。

## 这组测试大半在守「不做什么」

本模块最大的风险不是「算错」，而是**不知不觉变成替用户做决定**。
所以这些断言与「不做什么」同等重要：

* 不自动重派（11.10.5）
* 不进排序打分（第七章）
* 不给失败加标签（沿用 ``attempt_state`` 的纪律）
* 不新增表 —— 全部结论从 ``task_records`` 现推

其中「不给失败加标签」那条最容易在后续改动里被破坏：一旦有人为了
「让统计更准」给失败加一个 ``reason_kind`` 字段，判据就从**形状**
退化成**标签**，而标签会把「这次的形状」固化成「这个东西的属性」。
所以这里专门钉住：**错因原样保留，不归类**。
"""

from __future__ import annotations

import datetime

import pytest

from freeagent.app import build_app
from freeagent.domain import RecordType
from freeagent.services.clock import FrozenClock
from freeagent.services.feedback import feedback_report, read_outcome

NOW = datetime.datetime(2026, 10, 17, 9, 0, 0)


@pytest.fixture
def env(tmp_path):
    clock = FrozenClock(NOW)
    app = build_app(tmp_path / "agent.db", clock=clock)
    role = app.roles.create("工作")
    yield app, role, clock
    app.close()


def _records(app, task_id):
    return app.record_repo.list_for_task(task_id)


def _attempt(app, task_id, clock, *, seconds, succeeded=False, reason=""):
    """落一次尝试。**时间戳必须严格递增 —— 一次尝试内部也是。**

    ``list_for_task`` 的旧排序是 ``ts ASC, id ASC``，而 id 是随机的 ——
    同一条时间戳上的多条记录顺序曾是随机的。实测（2026-10-17）：
    把 ``delegation_dispatched`` 与 ``delegation_failed`` 落在同一微秒，
    读回来可能 ``failed`` 排在前面，于是 ``attempt_state`` 判成
    ``refused``（派发前被拒）而不是 ``failed``（执行期失败）——
    **结论整个反过来，而界面上两者长得一模一样。**

    **V1.37 已根治**（``task_records.seq``，设计文档 12.10.6）：同刻顺序
    现在是确定性的写入序。这条 ``_attempt`` 保留递增时间戳的写法不动 ——
    它保的形状（一次尝试内部有先后）本来就是真实派发的形状。
    """
    base = NOW + datetime.timedelta(seconds=60 + seconds)
    app.record_repo.append(
        task_id, RecordType.DELEGATION_DISPATCHED, "派出", base
    )
    ts = base + datetime.timedelta(milliseconds=1)
    if succeeded:
        app.record_repo.append(task_id, RecordType.DELEGATION_SUCCEEDED, "做完了", ts)
    else:
        app.record_repo.append(
            task_id, RecordType.DELEGATION_FAILED, reason or "失败", ts
        )


# ── 单条事务的结局 ──────────────────────────────────────────────────────── #

class TestReadOutcome:
    def test_never_dispatched(self, env):
        app, role, _clock = env
        t = app.tasks.create("还没派", [role.id])
        assert read_outcome(_records(app, t.id)).outcome == "never"

    def test_succeeded(self, env):
        app, role, clock = env
        t = app.tasks.create("成功了", [role.id])
        _attempt(app, t.id, clock, seconds=0, succeeded=True)
        assert read_outcome(_records(app, t.id)).outcome == "succeeded"

    def test_execution_failure_is_distinguishable_from_refusal(self, env):
        """**执行期失败**与**派发前被拒**必须能分开。

        两者该做的下一步完全不同：前者可重派，后者重试一万次也是同一结果
        （见 ``already_dispatched`` 的注释）。合成一句「失败」等于
        让用户自己猜。
        """
        app, role, clock = env
        failed = app.tasks.create("跑失败", [role.id])
        _attempt(app, failed.id, clock, seconds=0, reason="余额不足")
        assert read_outcome(_records(app, failed.id)).outcome == "failed"

        # 派发前被拒：**没有** delegation_dispatched，只有 failed
        refused = app.tasks.create("派不出去", [role.id])
        app.record_repo.append(
            refused.id, RecordType.DELEGATION_FAILED, "白名单拒绝",
            NOW + datetime.timedelta(seconds=65),
        )
        assert read_outcome(_records(app, refused.id)).outcome == "refused"

    def test_reason_is_kept_verbatim_not_classified(self, env):
        """错因**原样保留**，不做归类。

        这是最容易在后续改动里被破坏的一条：一旦为了「统计更准」给失败
        加一个 ``reason_kind``，判据就从**形状**退化成**标签**，而标签会
        把「这次的形状」固化成「这个东西的属性」。
        """
        app, role, clock = env
        t = app.tasks.create("失败", [role.id])
        raw = "HTTP 402 insufficient balance（opencode）"
        _attempt(app, t.id, clock, seconds=0, reason=raw)
        out = read_outcome(_records(app, t.id))
        assert out.reason == raw
        # 没有「类别」这个字段可断言 —— 那正是要的：无处归类
        assert not hasattr(out, "reason_kind")


# ── 验收结论（12.9 的闭环产物） ────────────────────────────────────────── #

class TestVerdict:
    def test_accepted_output_is_a_positive_signal(self, env):
        app, role, clock = env
        t = app.tasks.create("验收了", [role.id])
        _attempt(app, t.id, clock, seconds=0, succeeded=True)
        app.artifacts.create_draft(t.id, "产出", "x")
        app.artifacts.accept(app.artifacts.current(t.id).id)
        assert read_outcome(_records(app, t.id)).verdict == "accepted"

    def test_rewritten_output_shows_as_revised(self, env):
        """用户圈改重做过 → 那是**负面**证据，而库里本来没有这个事实。

        改前：只有 DRAFT / ACCEPTED 两种状态，「返工过」这件事
        **完全看不出来** —— 一个被要求改了三次的结果和一个一次通过的
        结果，在库里长得一样。
        """
        app, role, clock = env
        t = app.tasks.create("返工了", [role.id])
        _attempt(app, t.id, clock, seconds=0, succeeded=True)
        app.artifacts.create_draft(t.id, "v1", "a")
        app.artifacts.revise(t.id, "v2")
        app.artifacts.revise(t.id, "v3")
        assert read_outcome(_records(app, t.id)).verdict == "revised"

    def test_single_untouched_draft_is_neither_accepted_nor_revised(self, env):
        """一版草稿还没验收 → **不下结论**。

        刻意不判成「未验收」：那不是失败，只是一件事还没做。
        给它一个类别就是在替用户说「这版不行」。
        """
        app, role, clock = env
        t = app.tasks.create("待验收", [role.id])
        _attempt(app, t.id, clock, seconds=0, succeeded=True)
        app.artifacts.create_draft(t.id, "v1", "a")
        assert read_outcome(_records(app, t.id)).verdict == ""


# ── 聚合视图 ────────────────────────────────────────────────────────────── #

class TestFeedbackReport:
    def test_success_rate_ignores_refusals(self, env):
        """分母只算**真跑过**的那些。

        把派发前被拒算进失败，会让「我还没开始」显示成「我全失败了」——
        而这两者该做的下一步完全不同。
        """
        app, role, clock = env
        ok = app.tasks.create("a", [role.id])
        _attempt(app, ok.id, clock, seconds=0, succeeded=True)
        refused = app.tasks.create("b", [role.id])
        app.record_repo.append(
            refused.id, RecordType.DELEGATION_FAILED, "白名单拒绝",
            NOW + datetime.timedelta(seconds=61),
        )
        rep = feedback_report([ok.id, refused.id], lambda tid: _records(app, tid))
        assert rep.success_rate == 1.0
        assert rep.refused == 1

    def test_never_ran_is_not_zero_percent(self, env):
        """一次都没真跑过 → ``None``，不是 ``0.0``。

        ``0.0`` 意味着「跑了但全失败」，那是个**指控**；
        ``None`` 意味着「还没开始」。混起来会凭空制造一次失败感。
        """
        app, role, _clock = env
        t = app.tasks.create("没派", [role.id])
        rep = feedback_report([t.id], lambda tid: _records(app, tid))
        assert rep.success_rate is None
        assert rep.never == 1

    def test_repeated_failure_is_visible(self, env):
        """**同一个错因第三次**要被算出来 —— 这正是不做模式沉淀的后果。

        改前那些记录躺在库里没人读，用户会第三次踩同一个坑。
        """
        app, role, clock = env
        ids = []
        for i in range(3):
            t = app.tasks.create(f"失败{i}", [role.id])
            _attempt(app, t.id, clock, seconds=i * 10, reason="HTTP 402 余额不足")
            ids.append(t.id)
        other = app.tasks.create("别的错因", [role.id])
        _attempt(app, other.id, clock, seconds=100, reason="端口被占用")
        ids.append(other.id)

        rep = feedback_report(ids, lambda tid: _records(app, tid))
        assert rep.top_reasons[0] == ("HTTP 402 余额不足", 3)
        assert rep.top_reasons[1] == ("端口被占用", 1)
        assert rep.failed == 4

    def test_revised_tasks_name_themselves(self, env):
        app, role, clock = env
        t = app.tasks.create("反复返工的那条", [role.id])
        _attempt(app, t.id, clock, seconds=0, succeeded=True)
        app.artifacts.create_draft(t.id, "v1", "a")
        app.artifacts.revise(t.id, "v2")

        rep = feedback_report(
            [t.id], lambda tid: _records(app, tid),
            lambda tid: app.task_repo.get(tid).title,
        )
        assert rep.revised_tasks == ((t.id, "反复返工的那条", 1),)

    def test_without_titles_it_lists_nothing_rather_than_raw_ids(self, env):
        """没给标题就不列 —— **没给就不猜**。

        回填一个 ``task_id`` 让用户自己认，是 8.2「文本必须能从本函数
        持有的事实推出」的反例：那行文本推出它要用户自己去查。
        """
        app, role, clock = env
        t = app.tasks.create("返工", [role.id])
        _attempt(app, t.id, clock, seconds=0, succeeded=True)
        app.artifacts.create_draft(t.id, "v1", "a")
        app.artifacts.revise(t.id, "v2")
        rep = feedback_report([t.id], lambda tid: _records(app, tid))
        assert rep.revised_tasks == ()


# ── 「不做什么」的守门测试 ──────────────────────────────────────────────── #

class TestWhatItDeliberatelyDoesNotDo:
    def test_reading_feedback_writes_nothing(self, env):
        """"反馈回流"**回流的是信息，不是动作。

        读一次履历之后库里必须**一个字都没多** —— 一旦这个模块开始写库，
        「只读」就名存实亡，而它就该在那个时刻被拦住。
        """
        app, role, clock = env
        t = app.tasks.create("x", [role.id])
        _attempt(app, t.id, clock, seconds=0, reason="失败")
        before = len(_records(app, t.id))
        snapshot = app.tasks.get(t.id).state
        feedback_report([t.id], lambda tid: _records(app, tid))
        assert len(_records(app, t.id)) == before
        assert app.tasks.get(t.id).state == snapshot

    def test_report_carries_no_advice_fields(self, env):
        """结论里**不许**有 ``should_retry`` / ``next_action`` 这类字段。

        与第七章「摆得更清楚 ≠ 替你决定」同构。字段一旦存在，
        迟早有人在界面上把它渲染成一个按钮 —— 而那正是 11.10.5
        「绝不自动触发委派」要防的事。
        """
        from freeagent.services.feedback import FeedbackReport

        forbidden = {"should_retry", "next_action", "advice", "recommendation"}
        assert not (forbidden & set(FeedbackReport.__slots__))

    def test_no_new_table_was_added(self, env):
        """全部结论从 ``task_records`` 现推 —— **不新增表**。

        新表意味着新的持久化格式要记进 ``persistence-changes``、新的迁移
        要过幂等那一关，而它换来的只是「把已有记录读一遍」。
        """
        app, _role, _clock = env
        tables = {
            r[0] for r in app.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "task_records" in tables
        assert not any("feedback" in t or "outcome" in t for t in tables), (
            f"多了不该有的表：{sorted(tables)}"
        )

    def test_outcome_reuses_the_single_derivation(self, env):
        """结局判定**复用** ``attempt_state``，不重写一遍推导。

        两份推导一旦漂移，「这里说成功」与「派发守卫说已派过」就会
        对不上，而那种不一致只在真跑起来时才暴露。
        """
        import ast
        import inspect
        import textwrap

        from freeagent.services import feedback

        # **只看代码，不看 docstring** —— docstring 里要写清这个前提
        # （同秒记录顺序随机），里面必然出现这些词，按全文断言会误伤。
        tree = ast.parse(textwrap.dedent(inspect.getsource(feedback.read_outcome)))
        fn = tree.body[0]
        doc = ast.get_docstring(fn) or ""
        code = inspect.getsource(feedback.read_outcome).replace(doc, "")
        assert "attempt_state(" in code, "必须走那唯一一份推导"
        # 结局**只能**由 attempt_state 给。判定处刻意只解构它的返回值，
        # 不自己遍历 RecordType 累积 in_flight/last ——
        # 那正是第二份推导的写法，会与派发守卫漂移。
        assert "_in_flight, last = attempt_state(" in code
        body_code = "\n".join(
            ast.unparse(stmt) for stmt in fn.body if not isinstance(
                stmt, ast.Expr
            )
        )
        assert "in_flight" not in body_code.replace("_in_flight", ""), (
            "自己累积 in_flight = 第二份推导"
        )