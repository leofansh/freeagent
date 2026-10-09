"""多执行器注册表与探针的守卫（设计文档 11.10.2 / 11.10.7）。

## 这份测试为什么存在

``/agents`` 从「只列 opencode」变成「列注册表里全部执行器」之后，
最大的风险不是功能坏掉，而是**清单说谎** —— 少列一个、或者把一个
没验过的说成能派。本项目 V1.16 记过的最大一类事故正是
「文档/声明比事实强」，所以这里锁四条：

1. **注册表说有，清单就必须有** —— 不许静默跳过
2. **没验过 = 一桶都不给，且不可派发** —— 不许"至少能读"
3. **三种不可派发给三种不同的话** —— 该做的事不同
4. **协议形状不许猜** —— 未验证的适配器调用即抛，不给半截形状

## 一个前提

hermes 与 opencode **不是同一种接法**（HTTP+SSE vs stdio JSON-RPC），
所以"接第二个执行器"不是加一行注册表。这里测的是**登记**这一层
（可见且诚实地不可派发），不是派发那一层。
"""

from __future__ import annotations

import inspect

import pytest

from freeagent.services import executor_probe as P
from freeagent.services import executors as E


@pytest.fixture()
def which(monkeypatch):
    """控制「装没装」。按命令名区分，好让两个执行器状态独立。"""

    def _set(found: bool, *, only: str | None = None) -> None:
        real = P.shutil.which

        def _fake(cmd):
            if only is not None and cmd != only:
                return None
            return "C:/x/fake" if found else None

        monkeypatch.setattr(P.shutil, "which", _fake)
        _set.real = real  # noqa: B010 - 仅为可读性，当前未用

    return _set


@pytest.fixture()
def majors(monkeypatch):
    """分别控制两个执行器「探到哪个主版本」。"""

    def _set(*, opencode=None, hermes=None) -> None:
        import freeagent.delegate as D

        if opencode is not None:
            monkeypatch.setattr(D, "detect_opencode_version", lambda _c="opencode": opencode)
        if hermes is not None:
            monkeypatch.setattr(D, "detect_hermes_version", lambda _c="hermes": hermes)

    return _set


# --------------------------------------------------------------------------- #
# 注册表：有哪些执行器
# --------------------------------------------------------------------------- #

class TestKnownExecutors:
    def test_registry_lists_both(self):
        """11.10.2 要的那份清单：opencode 与 hermes 都在册。"""
        assert set(E.known_executors()) >= {"opencode", "hermes"}

    def test_comes_from_registry_not_a_hand_written_list(self):
        """清单**由注册表导出**，不是另写一份常量。

        两份清单迟早漂移，而漂移方向是「探针说没有、注册表里其实有」。
        """
        assert set(E.known_executors()) == {a.executor for a in E._ADAPTERS}

    def test_no_duplicates(self):
        """同一个执行器有多个版本适配器（opencode V1/V2）时只出现一次。"""
        names = E.known_executors()
        assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# hermes：登记了，但不可派发
# --------------------------------------------------------------------------- #

class TestHermesIsRegisteredButNotDispatchable:
    def test_adapter_exists_and_is_unverified(self):
        adapter = E.adapter_for(0, "hermes")
        assert adapter is not None, "注册表里应当有 hermes"
        assert adapter.verified is False

    def test_registry_does_not_accidentally_enable_opencode_paths(self):
        """加 hermes 不许改变 opencode 的可派发集合。

        这是本次改动的**核心安全断言**：多登记一个执行器是加法，
        不能顺手把既有闸门松开一格。
        """
        assert E.dispatchable_majors("opencode") == (1,)
        assert E.dispatchable_majors("hermes") == ()

    def test_every_protocol_hook_refuses_instead_of_guessing(self):
        """⚠️ 未验证 = **调用即抛**，不给半截形状。

        猜一个「看起来很像对的」形状比拒绝更危险：它会静默地不生效，
        而失效方向是闸门回到 allow（见 postmortem 0001）。
        """
        adapter = E.adapter_for(0, "hermes")
        for hook in (
            adapter.build_permission_config,
            adapter.build_read_only_config,
        ):
            with pytest.raises(E.UnverifiedExecutorError):
                hook()
        for hook in (adapter.parse_request, adapter.parse_question):
            with pytest.raises(E.UnverifiedExecutorError):
                hook({})
        with pytest.raises(E.UnverifiedExecutorError):
            adapter.build_reply("allow")
        with pytest.raises(E.UnverifiedExecutorError):
            adapter.build_question_reply([["答案"]])

    def test_field_names_left_empty_on_purpose(self):
        """hermes 侧没有与本仓三桶对位的 permission 配置面。

        它只有 profile 级的三档审批模式（manual/smart/off），
        与本仓「按动作能不能做」不是同一语义。塞进去假装是键名映射，
        会让以后拼配置的代码拿到一份看起来合理、实则无效的映射。
        """
        assert E.HERMES_FIELD_NAMES == {}


# --------------------------------------------------------------------------- #
# hermes 探针的三条纪律
# --------------------------------------------------------------------------- #

class TestHermesProbeDiscipline:
    def test_not_installed_says_so(self, which):
        which(False)
        got = P.probe_hermes()
        assert got.installed is False
        assert got.detected_major is None, "没装时不许有版本号"
        assert got.dispatchable is False
        assert "找不到" in got.unavailable_reason

    def test_undetectable_version_is_none_not_zero(self, which, majors):
        """⚠️ 探不到是 ``None`` 不是 ``0``。

        对 hermes 这条尤其要紧：它**真实的主版本就是 0**（v0.21.3）。
        若探不到时回落成 0，就会与「探到了 0.x」混成同一种状态，
        而前者该去查安装、后者该去验协议 —— 完全不同。
        """
        which(True)
        majors(hermes=None)
        got = P.probe_hermes()
        assert got.detected_major is None
        assert "探不到" in got.unavailable_reason

    def test_installed_but_unverified_gets_no_bucket(self, which, majors):
        """装了、也探到版本了，但没验过 —— **一桶都不给**。"""
        which(True)
        majors(hermes=0)
        got = P.probe_hermes()
        assert got.installed is True
        assert got.detected_major == 0
        assert got.dispatchable is False
        assert got.capabilities == ()
        assert "未验证" in got.unavailable_reason

    def test_unverified_reason_says_it_is_from_docs_not_from_a_real_run(
        self, which, majors
    ):
        """「没验过」的理由必须说清是**读文档读来的**，不是「缺个字段」。

        只说一句通用的「未验证」，用户会以为补个字段名就能开。
        真正的缺口是：一次真机都没跑过（11.8.1 的教训正是照文档接线）。
        """
        which(True)
        majors(hermes=0)
        reason = P.probe_hermes().unavailable_reason
        assert "真机" in reason
        assert "8642" in reason or "API Server" in reason

    def test_unverified_reason_warns_against_the_openai_shortcut(self, which, majors):
        """⚠️ 必须点明**不能**把它当 OpenAI 兼容后端直接问。

        那条面（``/v1/chat/completions``）上工具已在服务端执行完毕，
        本仓看不到也拦不住 —— 是最诱人也最危险的走法：
        接上去「能用」，而 11.8 的闸门已经被绕过去了。
        """
        which(True)
        majors(hermes=0)
        reason = P.probe_hermes().unavailable_reason
        assert "拦不住" in reason or "捷径" in reason

    def test_unknown_and_unverified_differ(self, which, majors):
        """「不认识」与「认识但没验过」给**不同**的话。"""
        which(True)
        majors(hermes=0)
        unverified = P.probe_hermes().unavailable_reason
        which(True)
        majors(hermes=99)
        unknown = P.probe_hermes().unavailable_reason
        assert unverified and unknown
        assert unverified != unknown
        assert "未验证" in unverified
        assert "不认识" in unknown

    def test_probe_signature_cannot_dispatch(self):
        """与 opencode 同一条纪律：探针签名里不许出现「派给谁」。"""
        sig = inspect.signature(P.probe_hermes)
        assert list(sig.parameters) == ["command"]
        assert sig.parameters["command"].default == "hermes"

    def test_probe_is_read_only_on_the_database(self):
        """探针不落库 —— 它只回答「装没装」。"""
        src = inspect.getsource(P.probe_hermes) + inspect.getsource(P._probe_executor)
        for forbidden in ("INSERT", "UPDATE", "DELETE", "CREATE TABLE"):
            assert forbidden not in src.upper(), f"探针里出现了 {forbidden}"


# --------------------------------------------------------------------------- #
# probe_all：清单不许说谎
# --------------------------------------------------------------------------- #

class TestProbeAll:
    def test_covers_every_registered_executor(self, which, majors):
        """注册表里有几个，就列几个 —— **不许静默跳过**。"""
        which(True)
        majors(opencode=1, hermes=0)
        probes = P.probe_all()
        assert [p.name for p in probes] == list(E.known_executors())

    def test_a_registered_executor_without_a_probe_says_so(self, monkeypatch, which, majors):
        """⚠️ 登记了但没写探针 → 明说「没探针」，**不是**从清单里消失。

        「消失」会被读成「这个执行器不存在」，于是没人去补。
        """
        which(True)
        majors(opencode=1, hermes=0)
        monkeypatch.setitem(
            E.__dict__, "_ADAPTERS",
            E._ADAPTERS + (
                E.ExecutorAdapter(
                    executor="mystery", major=1, verified=False, field_names={},
                    build_permission_config=lambda: {},
                    build_read_only_config=lambda: {},
                    parse_request=lambda _p: None,
                    build_reply=lambda _d: {},
                    parse_question=lambda _p: None,
                    build_question_reply=lambda _a: {},
                ),
            ),
        )
        probes = P.probe_all()
        mystery = [p for p in probes if p.name == "mystery"]
        assert mystery, "登记了却没从清单里列出来 —— 那正是会被读成『不存在』的静默跳过"
        assert "没写它的探针" in mystery[0].unavailable_reason

    def test_never_dispatchable_when_nothing_is_verified(self, which, majors):
        which(True)
        majors(opencode=99, hermes=0)
        assert all(not p.dispatchable for p in P.probe_all())


# --------------------------------------------------------------------------- #
# 呈现：先说「能派几个」
# --------------------------------------------------------------------------- #

class TestRenderProbes:
    def test_leads_with_how_many_can_dispatch(self, which, majors):
        """用户问「有哪些执行器」，第一眼要的是「能用几个」。"""
        which(True)
        majors(opencode=1, hermes=0)
        first = P.render_probes(P.probe_all()).splitlines()[0]
        assert "1" in first and "可以派发" in first

    def test_lists_every_executor_even_the_unusable_one(self, which, majors):
        """没装的、不能派的**也要列出来**并说清为什么。

        只列能用的，等于把「本仓知道它存在但接不上」这件事藏起来。
        """
        which(True)
        majors(opencode=1, hermes=0)
        text = P.render_probes(P.probe_all())
        assert "opencode" in text
        assert "hermes" in text

    def test_empty_registry_is_called_out(self):
        """注册表空了不是「没有执行器」，是本仓坏了 —— 要说出来。"""
        assert "本仓坏了" in P.render_probes([])


# --------------------------------------------------------------------------- #
# 命令层：``/agents`` 真的列全部
# --------------------------------------------------------------------------- #

class TestAgentsCommand:
    @pytest.fixture()
    def repl(self, app, roles):
        import io

        from freeagent.cli.app import Repl

        buf = io.StringIO()
        return Repl(app, out=buf), buf

    def test_lists_every_registered_executor(self, repl, which, majors):
        """``/agents`` 列的是**注册表里全部**，不是「装了的那几个」。

        这是本次改动的用户可见面：以前它硬编码只探 opencode。
        """
        r, buf = repl
        which(True)
        majors(opencode=1, hermes=0)
        r.handle("/agents")
        out = buf.getvalue()
        assert "opencode" in out
        assert "hermes" in out

    def test_says_hermes_is_not_dispatchable(self, repl, which, majors):
        """hermes 在 ``/agents`` 里必须显示成**不能派**。

        用户若是看到它列出来就以为能用，那正是「声明比事实强」那类事故。
        """
        r, buf = repl
        which(True)
        majors(opencode=1, hermes=0)
        r.handle("/agents")
        out = buf.getvalue()
        assert "现在不能派发" in out
        assert "未验证" in out
