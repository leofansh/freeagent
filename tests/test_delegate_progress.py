"""执行期进度卡片（设计文档 11.16）。

## 这个文件守什么

``tests/test_cardkit_stream.py`` 守的是**发送层**（端点、sequence、2.0 结构）。
这个文件守的是**接线层**：一次委派从发卡到关卡，中间该更新几次、
每次 sequence 是多少、发卡失败会不会把委派带崩。

## 为什么接线层要单独测

发送层全绿但接线漏了，症状是「飞书里那张卡从头到尾只写着『正在启动』」，
而**离线测试一条都不会红** —— 因为没有任何一层知道用户看不到进度。
反过来，接线层自己也会出错：
进度状态在错误的初始化位置（异常路径读不到值）、
发卡失败变成委派失败、sequence 两条路径各数各的。

## 全部离线

时间由注入的 :class:`FakeClock` 驱动，所以节流是**确定性**可测的，
不 sleep、不 flaky。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from freeagent.app import build_app  # noqa: E402
from freeagent.delegate import (  # noqa: E402
    MIN_PROGRESS_INTERVAL,
    ProgressCard,
    run_with_tool_gate,
    tool_event_label,
)
from freeagent.services.approval import ApprovalStore, Decision  # noqa: E402
from freeagent.services.delegate import DelegationPolicy  # noqa: E402
from freeagent.services.opencode_server import ServerError  # noqa: E402


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #
class FakeClock:
    """可注入的单调时钟。**刻意能手动推进** —— 靠 sleep 等节流窗口
    是 flaky 的根源（慢的机器红快的机器绿）。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeServer:
    """只实现 :func:`run_with_tool_gate` 用到的方法。

    与 ``tests/test_tool_gate.py`` 同款形状 —— 刻意不用 mock 魔法：
    形状对不上会在这里炸，而不是等到真机上要人点卡才发现。
    """

    def __init__(self, events, *, replies=None) -> None:
        # **刻意不 list(events)**：事件流可以是生成器，而生成器里有
        # ``clock.advance(...)``。提前 list() 会在构造时把时钟推到未来，
        # 于是「时长上限」「节流窗口」这些依赖时序的断言全部失效
        # （第一次就踩：时间上限测试红成一个不存在的 bug）。
        self._events = events
        self.replies: list[tuple[str, str]] = list(replies or [])
        #: 提问答复送回 opencode 的记录。
        self.question_replies: list[tuple[str, list[list[str]]]] = []
        self.prompts: list[tuple[str, str]] = []
        self.aborted = False
        self.closed = False
        #: 建会话就炸 —— 用来验「异常路径也要能关卡片」。
        self.raise_on_session: Exception | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.closed = True

    def create_session(self) -> str:
        if self.raise_on_session is not None:
            raise self.raise_on_session
        return "ses_fake"

    def prompt_async(self, session_id, brief, *, model="", agent="",
                      variant="", directory=None):
        self.prompts.append((session_id, brief))

    def events(self, **kw):
        yield from self._events

    def reply_permission(self, request_id, decision, *, directory=None) -> None:
        self.replies.append((request_id, decision))

    def reply_question(self, request_id, answers, *, directory=None) -> None:
        self.question_replies.append((request_id, answers))

    def abort(self, session_id) -> None:
        self.aborted = True


class _FakeToolSender:
    """执行期授权卡 **与** 提问卡的替身 —— 自问自答，假装人点了「允许」。

    两个方法在**同一个替身**里，因为 ``run_with_tool_gate`` 的 sender 参数
    本来就同时当这两种 sender 用（看它的类型标注：``ToolCardSender |
    QuestionCardSender``）。分开写两个替身会让测试看不出这件事。

    :param answers: 提问的答复，逐题。空元组 = 不答（用来测未答路径）。
    """

    def __init__(self, store: ApprovalStore, *, auto: Decision = "allow",
                 answers: tuple[str, ...] = ()) -> None:
        # 显式标成 ``Decision`` 而不是 ``str``：``ApprovalStore.resolve``
        # 收的是 Literal['allow','deny']，传 ``str`` 检查器会报
        # 「str 不能赋给 Literal」。写默认值的子类必须标类型。
        self.auto: Decision = auto
        self.cards: list[dict[str, Any]] = []
        self._store = store
        self._reply_answer = answers
        #: 提问卡单独记 —— 它没有按钮，答复走文本，语义与授权卡不同。
        self.question_cards: list[dict[str, Any]] = []

    def send_tool_card(self, *, open_id, subject, detail, credential,
                       ttl_seconds, reply_to="") -> str:
        self.cards.append({"open_id": open_id, "credential": credential,
                           "reply_to": reply_to})
        self._store.resolve(credential, self.auto, decided_by=open_id)
        return "om_test"

    def send_question_card(self, *, open_id, subject, detail, credential,
                           ttl_seconds, reply_to="") -> str:
        self.question_cards.append({"open_id": open_id, "subject": subject,
                                    "credential": credential,
                                    "reply_to": reply_to})
        # 答复走 :meth:`ApprovalStore.put_answer` —— 与真桥接同一条路。
        # 刻意不去构造一个假文本通道：那会让测试测的是替身而不是代码。
        for text in self._reply_answer:
            self._store.put_answer(credential, text, answered_by=open_id)
        return "om_q"


class _FakeProgressSender:
    """进度卡片的替身。**记录每一次调用**，供断言次数与 sequence。

    :param fail_send: 发卡抛错 —— 验「展示层挂掉不许拖垮委派」。
    :param fail_update: 更新抛错 —— 验「更新失败不许拖垮委派」。
    """

    def __init__(self, *, fail_send: bool = False,
                 fail_update: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []
        self.fail_send = fail_send
        self.fail_update = fail_update

    def send_progress_card(self, *, open_id, title, project, chat_id):
        if self.fail_send:
            raise RuntimeError("飞书抖了")
        self.sent.append({"open_id": open_id, "title": title,
                          "project": project, "chat_id": chat_id})
        return ProgressCard(card_id="card-1", message_id="om_anchor")

    def update_progress_text(self, *, card_id, text, sequence) -> None:
        if self.fail_update:
            raise RuntimeError("飞书抖了")
        self.updates.append({"card_id": card_id, "text": text,
                             "sequence": sequence})

    def close_progress_stream(self, *, card_id, final_text, sequence) -> None:
        self.closed.append({"card_id": card_id, "final_text": final_text,
                            "sequence": sequence})

    @property
    def all_seqs(self) -> list[int]:
        """发卡(1) + 每次更新 + 关卡 的 sequence 序列。

        飞书要求**严格递增**，所以这个序列必须是 1,2,3…而不能有重复或回退。
        """
        seqs = [1] + [u["sequence"] for u in self.updates] \
            + [c["sequence"] for c in self.closed]
        return seqs


class _StubTask:
    id = "task_fake"
    project_path = "C:/p"
    title = "改个东西"
    #: 飞书发起的委派才有。Web 建的没有 —— 那条路不该发卡。
    delegate_chat_id = "oc_chat"


class _StubTaskNoChat(_StubTask):
    delegate_chat_id = ""


def _asked(rid: str = "p1", *, perm: str = "edit") -> tuple[str, Any]:
    return ("permission.asked", {
        "id": rid, "sessionID": "ses_fake", "permission": perm,
        "patterns": ["C:/p/x.txt"],
        "metadata": {"filepath": "C:/p/x.txt", "diff": "+42"},
        "always": ["*"],
    })


def _tool(name: str = "read", **arg: Any) -> tuple[str, Any]:
    """一条工具事件的**最小**形状（part.type == "tool"）。"""
    return ("tool.execute.before", {
        "part": {"type": "tool", "tool": name, "state": {"input": arg}},
    })


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as d:
        app = build_app(Path(d) / "a.db")
        yield ApprovalStore(app.conn)
        app.close()


def _run(store, server, *, progress=None, task=None, clock=None, **kw):
    """跑一次委派。``clock`` 默认给一个 0 秒起点的假时钟。

    ``tool_sender`` 可以传进来，以便断言发出去的授权卡长什么样
    （聚拢测试要看 ``reply_to``）。不传就用一个自造的。
    """
    tool_sender = kw.pop("tool_sender", None) or _FakeToolSender(store)
    factory = lambda project, command: server  # noqa: E731
    policy = DelegationPolicy(projects=("C:/p",), model="opencode/x")
    return run_with_tool_gate(
        task if task is not None else _StubTask(),
        Path("C:/p"), "做点事",
        policy=policy,
        store=store,
        sender=tool_sender,
        progress_sender=progress,
        approver="ou_owner",
        server_factory=factory,
        now=(clock or FakeClock()),
        **kw,
    )


class TestAuthorizationCardsThreadUnderProgressCard:
    """授权卡必须挂到**进度卡下面**（设计文档 11.16，方向 A）。

    ## 为什么这不是「好看问题」

    一次委派产生 N+1 张卡（N 次授权 + 1 张进度），而聊天流里每条消息的
    位置由**发送时刻**固定：进度卡必须最早发（否则没法流式更新），于是
    它必然被 N 张后发的授权卡压在下面 —— 要看「这次委派到哪了」就得往上翻。

    聚拢之后聊天流里只多 1 张卡，其余在它的回复串里，天然按顺序、天然
    不被别的东西盖住。
    """

    def test_tool_cards_reply_to_the_progress_card(self, store) -> None:
        clock = FakeClock()
        prog = _FakeProgressSender()
        sender = _FakeToolSender(store)

        _run(store, _FakeServer([
            _asked("p1"), _asked("p2"), ("session.idle", {}),
        ]), progress=prog, clock=clock, tool_sender=sender)

        assert len(sender.cards) == 2
        for card in sender.cards:
            assert card["reply_to"] == "om_anchor", (
                "授权卡必须挂到进度卡那条消息下面"
            )

    def test_question_cards_also_thread(self, store) -> None:
        """提问卡**同样**要聚拢 —— 否则长任务里提问会孤零零留在外面，
        而那正是最需要上下文的一次提问。"""
        clock = FakeClock()
        prog = _FakeProgressSender()
        sender = _FakeToolSender(store, answers=("只改 README",))

        def _q():
            # 载荷形状照 **V1 实测**：questions 是**字符串数组**，
            # 不是带 header/options 的对象数组。解析器遇到不认识���形状
            # 会整条放弃（宁可响亮失败，也不要残缺的答案），所以这里
            # 写错形状的表现是「一条卡都没发」，而不是发错。
            yield ("question.asked", {
                "id": "que_1", "sessionID": "ses_fake",
                "questions": ["改哪个？"],
            })
            yield ("session.idle", {})

        _run(store, _FakeServer(_q()), progress=prog, clock=clock,
             tool_sender=sender)
        assert sender.question_cards, "提问卡应已发出"
        assert sender.question_cards[0]["reply_to"] == "om_anchor"

    def test_without_progress_card_falls_back_to_plain_send(
            self, store) -> None:
        """**没有进度卡时必须照常发授权卡** —— 闸门不许因为「想好看」而失效。

        两条路都要能走：配了进度卡就聚拢，没配（或发卡失败）就退回原路径。
        """
        clock = FakeClock()
        sender = _FakeToolSender(store)

        out = _run(store, _FakeServer([_asked(), ("session.idle", {})]),
                   progress=None, clock=clock, tool_sender=sender)

        assert out.ok
        assert len(sender.cards) == 1
        assert sender.cards[0]["reply_to"] == "", (
            "没有进度卡就不该有锚点，sender 会退回发到会话"
        )

    def test_when_progress_card_send_fails_no_anchor(self, store) -> None:
        """进度卡**发失败**时（降级成无卡片），授权卡仍要正常发出去。

        这条最容易漏：发卡失败被接住之后 ``progress_anchor_id`` 是 None，
        而 ``or ""`` 那一步正好保证不会把 None 传下去。
        """
        clock = FakeClock()
        prog = _FakeProgressSender(fail_send=True)
        sender = _FakeToolSender(store)

        out = _run(store, _FakeServer([_asked(), ("session.idle", {})]),
                   progress=prog, clock=clock, tool_sender=sender)

        assert out.ok, "发进度卡失败不许拖垮委派"
        assert len(sender.cards) == 1
        assert sender.cards[0]["reply_to"] == ""


# --------------------------------------------------------------------------- #
# 投影：tool_event_label —— 认不出就不显示，绝不猜
# --------------------------------------------------------------------------- #
class TestToolEventLabel:
    def test_reads_name_and_target(self) -> None:
        got = tool_event_label("tool.execute.before", {
            "part": {"type": "tool", "tool": "read",
                     "state": {"input": {"filePath": "D:/a.py"}}},
        })
        assert "read" in got and "D:/a.py" in got

    def test_tool_name_without_argument_still_labelled(self) -> None:
        got = tool_event_label("tool.execute.before", {
            "part": {"type": "tool", "tool": "glob"},
        })
        assert "glob" in got

    def test_command_argument_is_shown(self) -> None:
        got = tool_event_label("tool.execute.before", {
            "part": {"type": "tool", "tool": "bash",
                     "state": {"input": {"command": "pytest -q"}}},
        })
        assert "pytest -q" in got

    def test_top_level_tool_field_is_accepted(self) -> None:
        """有的版本把工具名放顶层而不是 part 里 —— 也要认。"""
        got = tool_event_label("tool_call", {"tool": "grep"})
        assert "grep" in got

    def test_plain_text_part_is_not_a_tool(self) -> None:
        """**纯文本增量不许显示成工具** —— 显示错比不显示更坏：
        用户会据此判断「它在读文件」，而它其实在写答案。"""
        assert tool_event_label("message.part.updated", {
            "part": {"type": "text", "text": "我先看看代码"},
        }) == ""

    def test_unknown_payload_returns_empty(self) -> None:
        """认不出就空串。猜一个工具名会让卡片说假话。"""
        assert tool_event_label("message.updated", {"info": {"id": "x"}}) == ""
        assert tool_event_label("whatever", None) == ""
        assert tool_event_label("whatever", {"part": {"type": "tool"}}) == ""

    def test_target_is_truncated(self) -> None:
        """超长参数要截断 —— 卡片不是日志，一行放不下就换行。"""
        got = tool_event_label("tool.execute.before", {
            "part": {"type": "tool", "tool": "bash",
                     "state": {"input": {"command": "x" * 500}}},
        })
        assert len(got) < 120


# --------------------------------------------------------------------------- #
# 发卡：时机、收件人、降级
# --------------------------------------------------------------------------- #
class TestCardCreation:
    def test_card_is_sent_with_task_and_project(self, store) -> None:
        prog = _FakeProgressSender()
        _run(store, _FakeServer([("session.idle", {})]), progress=prog)
        assert len(prog.sent) == 1
        assert prog.sent[0]["title"] == "改个东西"
        assert prog.sent[0]["project"] == str(Path("C:/p"))
        assert prog.sent[0]["chat_id"] == "oc_chat"
        assert prog.sent[0]["open_id"] == "ou_owner"

    def test_card_falls_back_to_dm_without_chat_id(self, store) -> None:
        """Web 建的委派没有飞书会话 —— **仍然要发**，退化成私聊。

        真机踩过（2026-10-12）：原先的条件是
        ``progress_sender is not None and task.delegate_chat_id``，
        而 Web 端派的委派 ``delegate_chat_id`` **本来就是 None**，
        于是 Web 派的活**永远没有进度卡** —— 现象是「飞书只有权限审批、
        没有进展」。而权限卡一直走私聊发得出去，两者条件不一致，
        差的就是这一条。

        发卡的落点该由「谁在等这条结果」决定，不是由「从哪个入口建」决定。
        """
        prog = _FakeProgressSender()
        out = _run(store, _FakeServer([("session.idle", {})]), progress=prog,
                   task=_StubTaskNoChat())
        assert out.ok
        assert len(prog.sent) == 1, "没有会话也该发一张（走私聊）"
        assert prog.sent[0]["chat_id"] == ""
        assert prog.sent[0]["open_id"] == "ou_owner", "落点是等结果那个人"
        assert len(prog.closed) == 1, "照样要收尾关卡"

    def test_no_card_without_progress_sender(self, store) -> None:
        """``progress_sender=None`` 必须完全无声 —— 不配进度卡片的部署
        不该因为这个功能而多发任何东西。"""
        out = _run(store, _FakeServer([_asked(), ("session.idle", {})]),
                   progress=None)
        assert out.ok

    def test_send_failure_degrades_not_fails(self, store) -> None:
        """**发卡失败不许变成委派失败。**

        这是接线层最容易漏的一条：FeishuError 既不是 ServerError 也不是
        OSError，不接住的话它会直接冲出 :func:`run_with_tool_gate` ——
        一次飞书抖动就让代码改动没跑成，而用户看不出这两件事的因果关系。
        """
        prog = _FakeProgressSender(fail_send=True)
        out = _run(store, _FakeServer([_asked(), ("session.idle", {})]),
                   progress=prog)
        assert out.ok, f"发卡失败拖垮了委派：{out.summary}"
        assert prog.updates == [], "没有卡就不该再尝试更新"
        assert prog.closed == []

    def test_update_failure_does_not_break_the_run(self, store) -> None:
        """更新失败同样只记日志 —— 委派照跑完、照关卡。"""
        prog = _FakeProgressSender(fail_update=True)
        out = _run(store, _FakeServer([_tool(), _asked(), ("session.idle", {})]),
                   progress=prog)
        assert out.ok
        assert prog.updates == []
        assert len(prog.closed) == 1, "更新失败不该让关卡也不做"


# --------------------------------------------------------------------------- #
# 更新：首次强制、节流、sequence
# --------------------------------------------------------------------------- #
class TestProgressUpdates:
    def test_first_tool_event_updates_immediately(self, store) -> None:
        """第一个工具事件**不吃节流** —— 卡片刚发出去只写着「正在启动」，
        用户需要马上看到它真的动起来了。"""
        clock = FakeClock()
        prog = _FakeProgressSender()
        _run(store, _FakeServer([_tool("read"), ("session.idle", {})]),
             progress=prog, clock=clock)
        assert len(prog.updates) == 1
        assert "read" in prog.updates[0]["text"]

    def test_second_tool_event_inside_window_is_throttled(self, store) -> None:
        """5 秒窗口内的第二个工具事件**不更新** —— 不占 API 配额，
        也不让人看到「3 秒内改了两次」这种没意义的抖动。"""
        clock = FakeClock()
        prog = _FakeProgressSender()
        _run(store, _FakeServer([
            _tool("read"), _tool("grep"), _tool("read"), ("session.idle", {}),
        ]), progress=prog, clock=clock)
        assert len(prog.updates) == 1, "窗口内不该连续刷"

    def test_tool_event_after_window_updates_again(self, store) -> None:
        """窗口过了就该更新 —— 「长时间没动静之后的那一次」恰恰最该看到。"""
        clock = FakeClock()

        def _slow():
            yield _tool("read")
            clock.advance(MIN_PROGRESS_INTERVAL + 0.1)
            yield _tool("grep")
            yield ("session.idle", {})

        prog = _FakeProgressSender()
        _run(store, _FakeServer(_slow()), progress=prog, clock=clock)
        assert len(prog.updates) == 2
        assert "grep" in prog.updates[1]["text"]

    def test_first_permission_updates_immediately(self, store) -> None:
        """首次授权强制更新：用户刚批完，正想看它接着干了什么。"""
        clock = FakeClock()
        prog = _FakeProgressSender()
        _run(store, _FakeServer([_asked(), ("session.idle", {})]),
             progress=prog, clock=clock)
        assert len(prog.updates) == 1
        assert "允许" in prog.updates[0]["text"]

    def test_body_carries_project_status_and_counts(self, store) -> None:
        """卡片正文要同时给项目、状态、次数 —— 缺一项用户就得回头翻别的界面。"""
        clock = FakeClock()
        prog = _FakeProgressSender()
        _run(store, _FakeServer([_asked(), ("session.idle", {})]),
             progress=prog, clock=clock)
        text = prog.updates[0]["text"]
        assert str(Path("C:/p")) in text
        assert "正在执行" in text
        assert "1 次" in text and "允许 1 次" in text


# --------------------------------------------------------------------------- #
# sequence：飞书要求严格递增
# --------------------------------------------------------------------------- #
class TestSequenceIsStrictlyIncreasing:
    def test_sequences_are_contiguous(self, store) -> None:
        clock = FakeClock()
        prog = _FakeProgressSender()

        def _flow():
            yield _tool("read")
            clock.advance(MIN_PROGRESS_INTERVAL + 0.1)
            yield _tool("grep")
            clock.advance(MIN_PROGRESS_INTERVAL + 0.1)
            yield _asked()
            yield ("session.idle", {})

        _run(store, _FakeServer(_flow()), progress=prog, clock=clock)
        seqs = prog.all_seqs
        assert seqs == list(range(1, len(seqs) + 1)), (
            f"sequence 必须 1,2,3… 接续，实际 {seqs}"
        )

    def test_first_update_is_two_not_one(self, store) -> None:
        """发卡用掉 sequence=1，所以第一次更新必须是 2 —— 重复 1 会被
        飞书判成时序倒流，内容静默丢失。"""
        clock = FakeClock()
        prog = _FakeProgressSender()
        _run(store, _FakeServer([_tool(), ("session.idle", {})]),
             progress=prog, clock=clock)
        assert prog.updates[0]["sequence"] == 2


# --------------------------------------------------------------------------- #
# 关卡：所有出口都要关，且只关一次
# --------------------------------------------------------------------------- #
class TestCloseOnEveryExit:
    def test_success_closes_with_completed(self, store) -> None:
        prog = _FakeProgressSender()
        _run(store, _FakeServer([("session.idle", {})]), progress=prog)
        assert len(prog.closed) == 1
        assert "已完成" in prog.closed[0]["final_text"]

    def test_session_error_closes_with_error_status(self, store) -> None:
        """会话出错时卡片要说「会话出错」—— 与库里的结论**同一件事**。
        用户看到「已完成」而实际一次都没跑成，是最坏的一种不一致。"""
        prog = _FakeProgressSender()
        out = _run(store, _FakeServer([
            ("session.error", {"error": {"message": "403 拒绝"}}),
        ]), progress=prog)
        assert not out.ok
        assert "会话出错" in prog.closed[0]["final_text"]

    def test_time_cap_closes_with_cap_status(self, store) -> None:
        clock = FakeClock()
        prog = _FakeProgressSender()

        def _slow():
            yield _tool("read")
            clock.advance(DEFAULT_MAX := 30 * 60 + 1)
            yield _tool("read")

        out = _run(store, _FakeServer(_slow()), progress=prog, clock=clock)
        assert not out.ok
        assert "时长超限" in prog.closed[0]["final_text"]

    def test_step_cap_closes_with_cap_status(self, store) -> None:
        clock = FakeClock()
        prog = _FakeProgressSender()

        def _many():
            for i in range(4):
                clock.advance(MIN_PROGRESS_INTERVAL + 0.1)
                yield _asked(f"p{i}")

        out = _run(store, _FakeServer(_many()), progress=prog,
                   clock=clock, max_tool_calls=2)
        assert not out.ok
        assert "步数超限" in prog.closed[0]["final_text"]

    def test_closes_exactly_once(self, store) -> None:
        """重复关卡会让飞书报「sequence 不递增」，且内容可能停在中间态。"""
        clock = FakeClock()
        prog = _FakeProgressSender()
        _run(store, _FakeServer([
            _tool("read"), _asked(), ("session.idle", {}),
        ]), progress=prog, clock=clock)
        assert len(prog.closed) == 1

    def test_closes_when_session_creation_fails(self, store) -> None:
        """**建会话就失败**时也必须关卡。

        这是进度状态初始化的位置最容易漏的一条：状态若写在 ``with`` 内部，
        建会话抛错那条路上根本还没赋值，于是收口代码自己先崩
        （UnboundLocalError），把真正的 ServerError 盖掉。
        """
        prog = _FakeProgressSender()
        server = _FakeServer([])
        server.raise_on_session = ServerError("连不上")
        # 先发卡再失败不成立（发卡在建会话之后），所以这里验证的是
        # 「没发卡时收口不炸」—— 真正的价值在于**不掩盖**原错误。
        out = _run(store, server, progress=prog)
        assert not out.ok
        assert "连不上" in out.summary
        assert prog.closed == [], "没发卡就不该有关卡调用"

    def test_close_failure_does_not_change_verdict(self, store) -> None:
        """关卡失败只记日志 —— 展示层的事不该改结论。"""
        prog = _FakeProgressSender()
        prog.close_progress_stream = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("飞书抖了"))
        out = _run(store, _FakeServer([("session.idle", {})]), progress=prog)
        assert out.ok, "关卡失败把一次成功的委派说成了失败"


# --------------------------------------------------------------------------- #
# 真实链路失败（ServerError）也走同一个收口
# --------------------------------------------------------------------------- #
class TestExceptionPathClosesCard:
    def test_server_error_after_card_closes_it(self, store) -> None:
        """事件流中途抛 ServerError —— 卡片必须关，且关的是**同一个**
        收口点（另抄一份的地方历史上读不到 progress_msg_id）。"""
        prog = _FakeProgressSender()

        class _Boom(_FakeServer):
            def events(self, **kw):
                yield ("tool.execute.before", {
                    "part": {"type": "tool", "tool": "read"},
                })
                raise ServerError("流断了")

        out = _run(store, _Boom([]), progress=prog)
        assert not out.ok
        assert "流断了" in out.summary
        assert len(prog.closed) == 1
        assert "授权链路失败" in prog.closed[0]["final_text"]