"""``/ask`` 的守卫（设计文档 11.10.6 / 11.10.4）。

## 三条不能违反

1. **执行器要权限时绝不自动放行** —— 只读配置下不该出现这种请求；
   出现了就是「配置没生效」，而自动放行会把那道防线整个绕过去，
   症状还只是「看起来能用」。
2. **文本** ``delta`` 累加、``updated`` 覆盖（11.8.1 实测：298/414 条是
   delta，混淆会让同一段文字重复 19 次）。
3. **超预算不静默截断** —— 必须说清「用尽预算、停在哪」。
"""

from __future__ import annotations

import contextlib

import pytest

from freeagent.services.ask import ask_executor


class _FakeServer:
    """按脚本产出事件的假执行器。

    刻意**不**起真进程：这里要断言的是「本模块怎么解读事件」，
    真起一个 opencode 会把单测变成集成测试（设计文档 11.8.1 同一个取舍）。
    """

    def __init__(self, script, *, record=None):
        self._script = script
        self.aborted: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self._record = record if record is not None else {}

    # -- OpenCodeServer 用到的那几个方法-- #
    def create_session(self) -> str:
        return "ses_fake"

    def prompt_async(self, session_id, question, model=None) -> None:
        self.prompts.append((session_id, question))

    def events(self):
        yield from self._script

    def abort(self, session_id) -> None:
        self.aborted.append(session_id)


def _factory(server):
    @contextlib.contextmanager
    def _make(project, command, *, permission_config=None):
        server._record["permission_config"] = permission_config
        server._record["project"] = project
        yield server

    return _make


def _ask(script, *, server=None, **kw):
    srv = server if server is not None else _FakeServer(script)
    out = ask_executor(
        "这个项目是干什么的？", project=".", make_server=_factory(srv), **kw
    )
    return out, srv


def _delta(text):
    return ("message.part.delta", {"messageID": "m1", "partID": "p1",
                                  "field": "text", "delta": text})


def _updated(text):
    return ("message.part.updated", {"messageID": "m1",
                                     "part": {"type": "text", "text": text}})


def _tool():
    return ("tool", {"callID": "c1"})


IDLE = ("session.idle", {"sessionID": "ses_fake"})


# --------------------------------------------------------------------------- #
# 文本怎么攒
# --------------------------------------------------------------------------- #

class TestTextAccumulation:
    def test_deltas_are_accumulated(self):
        """delta 是**增量片段** —— 必须累加。"""
        out, _ = _ask([_delta("这个"), _delta("项目"), _delta("是自用工具"), IDLE])
        assert out.ok is True
        assert out.text == "这个项目是自用工具"

    def test_updated_overwrites_instead_of_accumulating(self):
        """⚠️ updated 是**整块全文** —— 拿它累加会把同一段话重复 N 次。

        实测过：一轮 19 条 updated 累加，正文就重复 19 遍。
        """
        out, _ = _ask([
            _delta("答"),
            _updated("完整答案"),
            _updated("完整答案"),
            IDLE,
        ])
        assert out.text == "完整答案", f"updated 被当增量累加了：{out.text!r}"

    def test_empty_answer_is_not_an_error(self):
        """什么都没说 ≠ 失败 —— 它可能只是想了一会儿就放弃了。"""
        out, _ = _ask([IDLE])
        assert out.ok is True
        assert out.text == ""


# --------------------------------------------------------------------------- #
# ⚠️ 最要紧的一条
# --------------------------------------------------------------------------- #

class TestPermissionRequestIsNeverAutoAllowed:
    def test_aborts_and_refuses(self):
        """只读配置下出现授权请求 ⇒ **中止 + 如实报错**。

        自动放行会把「只读」这道防线整个绕过去，而症状只是「能用」。
        """
        script = [
            _delta("让我看看"),
            ("permission.asked", {"id": "per_1", "permission": "edit",
                                   "patterns": ["a.py"]}),
        ]
        out, srv = _ask(script)
        assert out.ok is False
        assert srv.aborted == ["ses_fake"], "中止了却没有记录 —— 会话还挂着"

    def test_the_reason_says_it_is_not_the_users_fault(self):
        """理由必须指向「配置没生效」，而不是让用户以为该放宽权限。"""
        out, _ = _ask([("permission.asked", {"id": "per_1",
                                             "permission": "edit"})])
        assert "只读配置没生效" in out.reason
        assert "别急着放宽" in out.reason

    def test_partial_answer_is_kept(self):
        """已经拿到的半截**要留着** —— 丢掉等于让用户重跑一次。"""
        out, _ = _ask([_delta("我先读了 README"),
                        ("permission.asked", {"id": "p", "permission": "bash"})])
        assert out.partial == "我先读了 README"

    def test_read_only_config_is_actually_passed_in(self):
        """⚠️ 传给服务的必须是**只读**那份，而不只是「恰好没触发权限」。

        前者防住「配置没生效」，后者防不住 —— 一个「什么都没发生」
        的假阳性会让这条测试永远绿着，而真机上第一次改文件就出事。
        """
        srv = _FakeServer([IDLE])
        _ask([IDLE], server=srv)
        cfg = srv._record["permission_config"]
        assert cfg is not None, "没传权限配置 —— 落回 opencode 默认（大多 allow）"
        rules = cfg["permission"]
        assert rules["edit"] == "deny"
        assert rules["bash"] == "deny"


# --------------------------------------------------------------------------- #
# 轮次预算（11.10.4）
# --------------------------------------------------------------------------- #

class TestBudget:
    def test_counts_tool_events_not_rounds(self):
        """⚠️ 预算数的是**工具调用**，不是「跑了几轮」。

        第一版把计数点放在 ``session.idle`` 上，于是预算实际变成了
        「最多跑几轮」—— 而那与 11.10.4 写的「工具调用次数」不是一回事。
        """
        out, _ = _ask([_tool(), _tool(), IDLE], max_tool_calls=20)
        assert out.tool_calls == 2, f"数成了 {out.tool_calls}"

    def test_exceeding_the_budget_stops_and_says_so(self):
        out, srv = _ask([_tool(), _tool(), _tool()], max_tool_calls=2)
        assert out.ok is False
        assert "用尽预算" in out.reason
        assert "没有**接着跑" in out.reason
        assert srv.aborted == ["ses_fake"]

    def test_zero_disables_the_budget(self):
        """逃生舱：``max_tool_calls=0`` 表示不设上限。必须有它，
        否则一个确实需要很多步的提问没有任何合法的走法。"""
        out, _ = _ask([_tool(), _tool(), _tool(), IDLE], max_tool_calls=0)
        assert out.ok is True
        assert out.tool_calls == 3

    def test_budget_stops_reporting_what_it_had(self):
        """半截正文要留着并说清「它还没答完」。"""
        out, _ = _ask([_delta("读到第 3 个文件"),
                        _tool(), _tool(), _tool()], max_tool_calls=2)
        assert out.partial == "读到第 3 个文件"


class TestTimeout:
    def test_expired_clock_stops_and_says_so(self):
        """超时必须**停下并说清**，而不是继续等。

        第一次循环检查就已过期：``deadline`` 用掉第 1 个 tick，
        循环里第 2 个 tick 就是判定。
        """
        ticks = iter([0.0, 99.0])

        def _now():
            return next(ticks)

        out, srv = _ask([_tool(), IDLE], max_seconds=10, now=_now)
        assert out.ok is False
        assert "时长上限" in out.reason
        assert srv.aborted == ["ses_fake"]

    def test_server_failure_is_reported_not_raised(self):
        """起服务失败不该掀翻终端。"""

        @contextlib.contextmanager
        def _boom(*_a, **_kw):
            raise RuntimeError("端口被占")
            yield  # pragma: no cover

        out = ask_executor("问题", project=".", make_server=_boom)
        assert out.ok is False
        assert "起执行器失败" in out.reason


class TestStreamEndedWithoutFinishing:
    """⚠️ **回归**：输出流结束而没等到 ``session.idle`` ≠ 「答完了」。

    服务崩了 / 连接断了 / 流被截断，都会让事件循环**自然结束**。
    原先那条路径直接落到 ``ok=True``，于是用户问了一句、拿到半截甚至全空的
    答复、屏幕上还写着「成功」—— 静默劣化（对照 12.1.2「禁止静默劣化」）。
    """

    def test_it_is_reported_as_a_failure(self):
        out, _ = _ask([_delta("读到第 3 个文件")])       # 没有 session.idle
        assert out.ok is False
        assert "没答完" in out.reason

    def test_the_partial_text_is_kept(self):
        """半截**要留着** —— 用户宁可看到「没答完 + 已拿到的部分」，
        也不要一个看起来正常的空答案。"""
        out, _ = _ask([_delta("读到第 3 个文件")])
        assert out.partial == "读到第 3 个文件"

    def test_an_empty_stream_is_not_success(self):
        """一个事件都没有 ⇒ 绝不能报「成功」且正文为空。"""
        out, _ = _ask([])
        assert out.ok is False
        assert out.text == ""


class TestNothingIsPersisted:
    def test_empty_question_is_refused_without_starting_anything(self):
        out = ask_executor("   ", project=".", make_server=_factory(_FakeServer([])))
        assert out.ok is False
        assert out.reason == "没问什么。"


class TestSessionError:
    def test_session_error_is_reported(self):
        out, _ = _ask([_delta("半句"), ("session.error", {"error": "boom"})])
        assert out.ok is False
        assert out.partial == "半句"