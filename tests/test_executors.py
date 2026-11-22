"""执行器接缝：版本化适配器 + 版本闸门。

## 这组测试守住什么

1. **协议变更收敛在一个文件。** 权限字段名、事件形状、答复载荷三样东西
   只能住在 :mod:`freeagent.services.executors` 里 —— 散开就是复盘 0001
   那个形状：漏改一处，闸门**静默失效**而不是跑不起来。
2. **未验证的版本必须响。** V2 适配器是照**文档**写的、没对着真实实例验过，
   所以它每个方法都要抛。猜一个「看起来很像对的」数组元素形状比拒绝更危险。
3. **取不到版本不许回落。** 回落正是那个 bug 的形状。

## 不测什么

**不测「V2 能不能跑」** —— 测不了，没有真实 V2 实例。这里测的是
「我们知道自己不知道」这件事被如实表达出来了。
"""

from __future__ import annotations

import dataclasses

import pytest

from freeagent import delegate as dg
from freeagent.services import executors as ex
from freeagent.services import opencode_server as oc


def _synthetic_unverified(major: int = 7, executor: str = "opencode") -> ex.ExecutorAdapter:
    """造一个**认识但没验过**的适配器（协议函数全抛）。

    ## 为什么现在需要一个「合成」的反例，而不是借 hermes

    早些时候 hermes 是注册表里那个 ``verified=False`` 的载体，测试借它来
    验证「未验证的执行器每个协议入口都要响」。hermes 后来对着真机验过
    （/ask 那条线，11.10.8），``verified`` 转正了 —— 它不再是「未验证」。
    但「未验证必须响」这条纪律**依然成立**（哪天又登记一个只读文档没实测的
    执行器时，它不能给出一个猜的形状）。所以这里用合成的未验证适配器来
    承载这条纪律的测试，而不是改坏真适配器。

    每个入口的报错**指名缺的是哪一块**（``_PIECES`` 那张表）—— 这是
    :func:`test_error_names_the_missing_piece` 逐入口断言的基础：漏一个入口
    = 那个入口给出一个猜的形状，而笼统的「不支持」让人没法照着做。
    """
    _PIECES = {
        "build_permission_config": "permission 配置形状",
        "build_read_only_config": "只读配置形状",
        "parse_request": "授权请求事件形状",
        "build_reply": "答复载荷形状",
        "parse_question": "提问事件形状",
        "build_question_reply": "提问答复载荷形状",
    }

    def _make_raiser(piece: str):
        def _raise(*_a: object, **_k: object) -> Any:
            raise ex.UnverifiedExecutorError(
                f"{executor} 主版本 {major} 的 {piece}尚未验证。"
                "要接它：对着真实实例实测，填 "
                "`freeagent/services/executors.py` 里对应适配器的形状函数，"
                "再把 `verified` 改成 True。"
            )
        return _raise

    base = ex.adapter_for(1)
    assert base is not None
    return dataclasses.replace(
        base,
        executor=executor,
        major=major,
        verified=False,
        dispatchable=False,
        **{method: _make_raiser(piece) for method, piece in _PIECES.items()},
    )


class TestRegistry:
    def test_known_is_not_the_same_as_dispatchable(self):
        """「知道」与「能派发」是两个概念，混淆它就是闸门失效的前置。

        opencode 的 V1/V2 都 ``verified=True`` 且 ``dispatchable=True``，
        所以它俩已知即可派。hermes ``verified=True`` 但 ``dispatchable=False``
        （只 /ask、不委派，11.10.8）—— 所以「知道它、验过它」不等于
        「能委派它」。
        """
        assert ex.known_majors("opencode") == (1, 2)
        assert ex.dispatchable_majors("opencode") == (1, 2)
        assert ex.dispatchable_majors("hermes") == ()

    def test_unknown_major_returns_none_and_never_falls_back(self):
        """**最关键的一条。** 回落是原 bug 的形状：字段名对不上、
        配置被静默忽略、闸门失效，而外面看着一切正常。"""
        for probe in (None, 0, 3, 99, -1):
            assert ex.adapter_for(probe) is None, f"{probe!r} 不该拿到适配器"

    def test_unknown_executor_has_no_adapters(self):
        assert ex.known_majors("nonexistent") == ()
        assert ex.dispatchable_majors("nonexistent") == ()
        assert ex.adapter_for(1, "nonexistent") is None

    def test_v1_adapter_is_verified(self):
        a = ex.adapter_for(1)
        assert a is not None and a.verified
        assert a.executor == "opencode"

    def test_v2_adapter_is_verified_too(self):
        """V2 已对着真机验过（实测 opencode 2.0.29），与 V1 同为可派发。"""
        a = ex.adapter_for(2)
        assert a is not None
        assert a.verified is True


class TestUnverifiedRefusesLoudly:
    """**未验证**的执行器，每个协议入口都要抛，而不是给一个猜的形状。

    原名 ``TestV2RefusesLoudly``，写的是「V2 照文档写、没实测」。
    V2 后来对着真机验过了；hermes 也验过 /ask 那条线了。于是这条纪律
    **换成了合成载体**（:func:`_synthetic_unverified`）：守的不再是
    「某具名的执行器不许用」，而是「**任何没对着真机验过的执行器不许用**」——
    哪天又登记一个只读文档没实测的执行器时，它必须响。

    注意抛错的机制：不是 ``verified`` 字段自动生效，而是适配器里的
    协议函数**本身**被换成抛错的桩。所以这个测试守的是「桩真的在每个入口
    都装上了」—— 漏一个入口，就等于在那个入口给出了一个猜的形状。

    换载体而不是删测试：猜形状会让 permission 被静默忽略、
    闸门失效且方向回到 allow，那是本仓最恨的一类失败。
    """

    @pytest.mark.parametrize(
        "call",
        [
            lambda a: a.build_permission_config(),
            lambda a: a.parse_request({"id": "per_1", "permission": "bash"}),
            lambda a: a.build_reply("allow"),
        ],
        ids=["permission_config", "parse_request", "reply_payload"],
    )
    def test_every_entry_point_raises(self, monkeypatch, call):
        a = _synthetic_unverified()
        monkeypatch.setattr(ex, "_ADAPTERS", (*ex._ADAPTERS, a))
        with pytest.raises(ex.UnverifiedExecutorError) as exc:
            call(a)
        msg = str(exc.value)
        # 错误信息要能照着做：说清缺的是什么、要去哪改
        assert "尚未验证" in msg
        assert "executors.py" in msg

    def test_error_names_the_missing_piece(self, monkeypatch):
        """要指名**缺的是哪一块**，而不是笼统的「不支持」。

        逐入口点名，因为漏一个入口 = 那个入口会给出一个猜的形状。
        """
        a = _synthetic_unverified()
        monkeypatch.setattr(ex, "_ADAPTERS", (*ex._ADAPTERS, a))
        expected = {
            "build_permission_config": ("permission 配置形状", ()),
            "build_read_only_config": ("只读配置形状", ()),
            "parse_request": ("授权请求事件形状", ({"id": "per_1"},)),
            "build_reply": ("答复载荷形状", ("allow",)),
            "parse_question": ("提问事件形状", ({"id": "q_1"},)),
            "build_question_reply": ("提问答复载荷形状", ("form_1", ["a"])),
        }
        for method, (piece, args) in expected.items():
            with pytest.raises(ex.UnverifiedExecutorError) as exc:
                getattr(a, method)(*args)
            assert piece in str(exc.value), f"{method} 要指名缺「{piece}」"

    def test_hermes_is_verified_but_not_dispatchable(self):
        """hermes 验过 /ask、不可委派、且**已停用**（V1.83）。

        ``verified``（验过接线）与 ``dispatchable``（能委派）是两个概念，
        V1.83 又加了 ``disabled``（决定不用它、代码保留作接缝）——
        见 :class:`~freeagent.services.executors.ExecutorAdapter` 的字段说明。
        """
        a = ex.adapter_for(0, "hermes")
        assert a is not None
        assert "hermes" in ex.known_executors()
        assert ex.known_majors("hermes") == (0,)
        assert a.verified is True, "hermes 的 /ask 那条线已对着真机验过"
        assert a.dispatchable is False, "hermes 不委派（11.10.8）"
        assert a.disabled is True, "hermes 已停用（V1.83）"
        assert ex.dispatchable_majors("hermes") == ()

    def test_known_renames_are_recorded_as_data(self):
        """**我们确实知道**的改名要被记下来且可断言 —— 那部分不是猜的。"""
        assert ex.V2_FIELD_RENAMES["shell"] == "shell"      # was bash
        assert ex.V2_FIELD_RENAMES["subagent"] == "subagent"  # was task
        assert ex.V2_FIELD_RENAMES["container"] == "permissions"  # was object
        # 刻意**不**含没确认的那些
        assert "edit" not in ex.V2_FIELD_RENAMES
        assert "external_dir" not in ex.V2_FIELD_RENAMES


class TestVersionGate:
    def test_supported_major_is_derived_from_registry(self):
        """闸门放行的版本来自注册表，不是硬编码常量。

        刻意**不**写死成某个数字：V1 与 V2 都对着真机验过
        （``verified=True``），所以「最新那个」会随注册表变。
        断言的是**来源**，不是当前值。
        """
        assert dg.SUPPORTED_OPENCODE_MAJOR == max(ex.dispatchable_majors("opencode"))
        assert dg.SUPPORTED_OPENCODE_MAJOR in ex.dispatchable_majors("opencode")

    def test_both_verified_majors_pass(self):
        """V1 与 V2 都对着真机验过 —— 两个都必须放行。"""
        assert dg.version_mismatch_reason(1) is None
        assert dg.version_mismatch_reason(2) is None

    def test_unprobeable_is_refused(self):
        r = dg.version_mismatch_reason(None) or ""
        assert "探不到" in r

    def test_unverified_major_is_refused_with_its_own_reason(self, monkeypatch):
        # 「未验证」与「不认识」是**两条不同的拒绝分支**，做的事不同：
        # 前者是「本仓知道有这个版本但没对着真机验过」，后者是「压根没见过」。
        # 触发前者需要注册表里**存在**一个 verified=False 的条目 ——
        # 用合成的未验证适配器当例子（hermes 已转正，不再是载体）。
        as_v7 = _synthetic_unverified(major=7)
        monkeypatch.setattr(ex, "_ADAPTERS", (*ex._ADAPTERS, as_v7))
        r = dg.version_mismatch_reason(7) or ""
        assert "未验证" in r
        # 要指出**下一步怎么接**，而不只是「不支持」——
        # 拒绝理由必须能照着做，否则用户只能去猜。
        assert "executors.py" in r, "要指名去哪个文件填形状"
        assert "verified" in r, "要指名改哪个字段才能放行"

    def test_unknown_major_is_refused_with_a_different_reason(self, monkeypatch):
        """两种拒绝必须**各不相同** —— 该做的事不同，说法就该不同。"""
        as_v7 = _synthetic_unverified(major=7)
        monkeypatch.setattr(ex, "_ADAPTERS", (*ex._ADAPTERS, as_v7))
        unknown = dg.version_mismatch_reason(99) or ""
        unverified = dg.version_mismatch_reason(7) or ""
        assert unknown != unverified
        assert "不认识" in unknown
        assert "升级本仓" in unknown or "降回" in unknown

    def test_the_message_lists_known_versions(self):
        r = dg.version_mismatch_reason(99) or ""
        assert "[1, 2]" in r, "要告诉用户本仓知道哪些版本"

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("1.18.31", 1),
            ("2.0.6", 2),
            ("opencode 1.2.3", 1),
            ("", None),
            ("no digits here", None),
        ],
    )
    def test_version_parsing_is_pure(self, text, expected):
        """纯函数，无 IO —— 版本探测要能被测就得先把解析和调用拆开。"""
        assert dg.parse_major_version(text) == expected

    @pytest.mark.parametrize("text", ["v3", "3", "3.", ".1"])
    def test_bare_or_malformed_version_is_refused_not_guessed(self, text):
        """**没有点号的版本号一律拒绝**，不是宽松地取那个整数。

        踩过的坑（本条就是为此而写）：我本来期望 ``v3`` 能解析出 3，
        实现返回 ``None``。查下来**实现是对的** ——
        :func:`parse_major_version` 的契约是「解析不了就返回 None（不放行）」，
        理由也写在它自己的 docstring 里：「宁可因为格式变化停下来问，
        也不要猜一个大版本出来然后照着一份**可能已经过时**的文档接线」。

        而 ``3`` 这种裸数字**含义不明**：可能是主版本，也可能是别的什么。
        猜错的后果是拿着 V1 的字段名去接线 V2 —— 也就是复盘 0001 那个形状：
        不报错，闸门静默失效。所以此处**保持严格**。
        """
        assert dg.parse_major_version(text) is None
        # 且拒绝理由要说人话，不是「版本不对」
        reason = dg.version_mismatch_reason(dg.parse_major_version(text)) or ""
        assert "探不到" in reason


class TestBackendKnowledgeLivesInOnePlace:
    """协议知识只在适配器里。"""

    def test_tool_permission_has_a_single_source(self):
        """两处都暴露同一个类 —— 重复定义会**遮蔽**导入的那个，
        于是 isinstance 检查在跨模块时静默失效。"""
        assert oc.ToolPermission is ex.ToolPermission

    def test_delegation_config_comes_from_the_adapter(self):
        assert oc.delegation_permission_config() == \
            ex.adapter_for(1).build_permission_config()

    def test_permission_config_keeps_the_v1_shape(self):
        """V1 的字段名与键序 —— 键序有意义（last matching rule wins）。

        兜底是 ``*: ask`` 而不是 ``*: allow``（2026-10-14 实测改的）：
        未登记的工具必须逐次问人，否则 opencode 将来新增一个能改文件的
        工具时，会**静默继承 allow** —— 闸门失效的方向是放开，不是收紧。
        """
        cfg = oc.delegation_permission_config()
        assert "permission" in cfg, "V1 用 permission 对象"
        body = cfg["permission"]
        keys = list(body)
        assert keys[0] == "*", "通配必须最前，否则后面逐条 allow 就没有兜底意义"
        assert body["*"] == "ask", "未登记的一律要人点头 —— 这是 fail-closed"
        assert keys[-1] == "external_directory", "deny 必须最后，否则被覆盖"
        assert body["external_directory"] == "deny", "永不越界"
        assert body["task"] == "deny", "不让它拉子代理"

    def test_write_tools_all_ask(self):
        """会改文件的每个工具都要逐次问 —— 漏一个就是一个后门。"""
        body = oc.delegation_permission_config()["permission"]
        for tool in ("edit", "write", "multiedit", "apply_patch", "bash", "skill"):
            assert body[tool] == "ask", f"{tool} 必须逐次授权"

    def test_read_tools_are_allowed_so_gates_do_not_turn_deaf(self):
        """读与检索不拦 —— 全拦会一路问到用户开始无脑点「允许」。

        这条与上一条是一对：写要问、读不问，闸门才既安全又可用。
        """
        body = oc.delegation_permission_config()["permission"]
        for tool in ("read", "list", "glob", "grep", "codesearch", "lsp"):
            assert body[tool] == "allow", f"{tool} 是只读，不该拦"

    def test_reply_payload_shape_is_preserved(self):
        """实测：裸字符串 → 400 Expected object；必须带 reply 键。"""
        assert oc.reply_payload("allow") == {"reply": "once"}
        assert oc.reply_payload("deny") == {"reply": "reject"}

    def test_reply_payload_never_grants_permanent_authorisation(self):
        """刻意不映射到 always —— 本项目明确不做永久授权（11.9.7）。"""
        for decision in ("allow", "deny"):
            assert "always" not in oc.reply_payload(decision)

    def test_unknown_decision_raises(self):
        with pytest.raises(ValueError, match="未知结论"):
            oc.reply_payload("maybe")

    def test_parse_request_normalises_the_payload(self):
        req = oc.permission_from_event({
            "id": "per_1",
            "permission": "bash",
            "patterns": ["/tmp/a"],
            "metadata": {"filepath": "/tmp/b", "diff": "--- a\n+++ b"},
            "tool": {"messageID": "msg_1", "callID": "call_1"},
        })
        assert req is not None
        assert req.request_id == "per_1"
        assert req.permission == "bash"
        # 两个路径都在
        assert req.paths == ("/tmp/a", "/tmp/b")
        assert req.diff == "--- a\n+++ b"
        # 执行器内部坐标被丢掉
        assert not hasattr(req, "callID")

    @pytest.mark.parametrize(
        "props",
        [
            None,
            "not a dict",
            {},
            {"permission": "bash"},              # 缺 id
            {"id": "per_1"},                     # 缺动作名
            {"id": "", "permission": "bash"},    # 空 id
            {"id": "per_1", "permission": ""},   # 空动作名
        ],
    )
    def test_unanswerable_requests_return_none_not_empty(self, props):
        """返回 None 而不是空字符串 —— 上层会把「无法回应」当成「已拒绝」，
        两种错误的处理方式完全不同。"""
        assert oc.permission_from_event(props) is None


class TestAdapterSwapping:
    def test_set_current_adapter_is_reversible(self):
        """换适配器必须能换回来 —— 它是全局状态，测试之间会互相污染。

        原断言是「切到 V2 会抛 UnverifiedExecutorError」，那写在 V2 还没对着
        真机验过时。V2 现在是实测实现，改成断言**它给出的确实是 V2 的形状**——
        这才是这条纪律的正面 counterpart：切了适配器，配置就得跟着变。
        """
        original = oc.current_adapter()
        v2 = ex.adapter_for(2)
        try:
            oc.set_current_adapter(v2)
            assert oc.current_adapter() is v2
            cfg = oc.delegation_permission_config()
            assert "shell" in str(cfg) or "subagent" in str(cfg), (
                "切到 V2 后配置应是 V2 的形状，而不是 V1 的"
            )
        finally:
            oc.set_current_adapter(None)
        assert oc.current_adapter() is original
        assert "permission" in oc.delegation_permission_config()

    def test_set_current_adapter_none_resets_to_v1(self):
        original = oc.current_adapter()
        oc.set_current_adapter(ex.adapter_for(2))
        oc.set_current_adapter(None)
        assert oc.current_adapter().major == original.major


class TestHermesAdapter:
    """hermes 适配器**停用**后的行为（V1.83 瘦身）。

    hermes 曾 ``verified=True``（/ask 那条线对着真机验过）但
    ``dispatchable=False``（unattended 面做不了 11.8 委派）；V1.83 起加
    ``disabled=True`` —— **决定不用它了，代码保留作接缝**。适配器仍在
    注册表里（``adapter_for`` 仍返回它，好让内部 / 接缝测试能继续对
    ``parse_event`` 等做回归），但探针 / ``/ask`` 按停用对待。
    """

    def test_verified_but_not_dispatchable_and_disabled(self):
        a = ex.adapter_for(0, "hermes")
        assert a is not None
        assert a.verified is True
        assert a.dispatchable is False
        assert a.disabled is True, "hermes 已停用（V1.83）"

    def test_dispatchable_majors_excludes_hermes(self):
        """``dispatchable_majors`` 只按 ``dispatchable`` 过滤 —— 不按 verified。"""
        assert ex.dispatchable_majors("hermes") == ()
        assert ex.known_majors("hermes") == (0,)

    def test_read_only_config_is_empty(self):
        """hermes 无「按工具写 permission 配置」的面 —— 返回空。注意：/ask 的
        「只读改不了」对 hermes 保证不了（V1.81 真机证伪「unattended deny
        双重强制」），所以它只作知识问答、不能开放执行；这里空配置是「没有
        那套面」，不是「有只读保障」。"""
        assert ex.adapter_for(0, "hermes").build_read_only_config() == {}

    def test_permission_config_is_empty(self):
        """委派配置对她不存在 —— 返回空 + 调用方判断不可派，别给假配置。"""
        assert ex.adapter_for(0, "hermes").build_permission_config() == {}

    def test_delegation_only_entries_refuse_loudly(self):
        """只走 /ask 的执行器，委派入口要**响**，不静默返回假形状。"""
        a = ex.adapter_for(0, "hermes")
        with pytest.raises(ValueError, match="不可委派"):
            a.build_reply("allow")
        with pytest.raises(ValueError, match="不可委派"):
            a.build_question_reply(["a"])
        assert a.parse_request({"id": "p", "permission": "bash"}) is None
        assert a.parse_question({"id": "q"}) is None

    @pytest.mark.parametrize(
        "frame, expected",
        [
            ({"event": "message.delta", "delta": "你"}, ("message.part.delta", {"delta": "你"})),
            ({"event": "tool.completed", "tool": "bash"}, ("tool", {"tool": "bash"})),
            ({"event": "tool.started", "tool": "read"}, ("tool.started", {"tool": "read"})),
            ({"event": "run.completed"}, ("session.idle", None)),
            ({"event": "run.cancelled"}, ("session.idle", None)),
            ({"event": "run.failed", "error": "HTTP 404 ..."}, ("session.error", {"message": "HTTP 404 ..."})),
        ],
        ids=["delta", "tool_completed", "tool_started", "completed", "cancelled", "failed_with_error"],
    )
    def test_parse_event_maps_to_ask_vocabulary(self, frame, expected):
        """事件名 + 载荷都要折成 ask 认得的形状 —— 只改名不改形状是 V2 的坑。"""
        a = ex.adapter_for(0, "hermes")
        assert a is not None and a.api is not None
        assert a.api.parse_event(frame) == expected

    @pytest.mark.parametrize(
        "frame",
        [
            "not a dict",
            {"event": "reasoning.available"},
            {"event": "message.delta", "delta": 123},
            {"event": "tool.completed", "tool": ""},
            {},
        ],
        ids=["non_dict", "unmapped_event", "non_str_delta", "empty_tool", "no_event"],
    )
    def test_parse_event_drops_what_it_cannot_use(self, frame):
        """不翻译 / 载荷不整形的帧直接丢掉 —— 不返回猜的形状。"""
        a = ex.adapter_for(0, "hermes")
        assert a is not None and a.api is not None
        assert a.api.parse_event(frame) is None

    def test_run_id_field_is_run_id_not_id(self):
        """真机实测（判据 5）：POST /v1/runs 的响应字段名是 ``run_id``。"""
        assert ex._hermes_run_id_of({"run_id": "run_abc", "status": "started"}) == "run_abc"
        assert ex._hermes_run_id_of({"id": "run_abc"}) == "run_abc", "兼容旧形状但不依赖"
        assert ex._hermes_run_id_of({"status": "started"}) is None


class TestV2ToolLifecycle:
    """真机 2.0.29：工具调用四段用同一个 ``id`` 串联，name / 参数 / 结果分置不同帧。"""

    def _parse(self, frame: dict[str, object]) -> tuple[str, object] | None:
        a = ex.adapter_for(2)
        assert a is not None and a.api is not None
        return a.api.parse_event(frame)

    def test_started_carries_name_and_call_id(self) -> None:
        assert self._parse({
            "type": "session.tool.input.started",
            "data": {"id": "c1", "name": "read"},
        }) == ("tool.started", {"tool": "read", "call_id": "c1"})

    def test_called_carries_input_and_running_without_name(self) -> None:
        kind, payload = self._parse({
            "type": "session.tool.called",
            "data": {"id": "c1", "input": {"path": "D:/a.py"}},
        })
        assert kind == "tool"
        assert isinstance(payload, dict)
        assert payload["call_id"] == "c1"
        assert "tool" not in payload, "called 不带名字，名字由轮次层按 id 回找"
        assert payload["state"]["input"] == {"path": "D:/a.py"}
        assert payload["state"]["status"] == "running"

    def test_success_is_completed_done(self) -> None:
        assert self._parse({
            "type": "session.tool.success", "data": {"id": "c1"},
        }) == ("tool.done", {"call_id": "c1", "state": {"status": "completed"}})

    def test_failed_is_error_done(self) -> None:
        assert self._parse({
            "type": "session.tool.failed", "data": {"id": "c1"},
        }) == ("tool.done", {"call_id": "c1", "state": {"status": "error"}})

    def test_interrupted_execution_maps_to_idle(self) -> None:
        assert self._parse({
            "type": "session.execution.interrupted",
            "data": {"sessionID": "ses-1"},
        }) == ("session.idle", {"sessionID": "ses-1", "outcome": "interrupted"})
