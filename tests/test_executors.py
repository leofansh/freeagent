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

import pytest

from freeagent import delegate as dg
from freeagent.services import executors as ex
from freeagent.services import opencode_server as oc


class TestRegistry:
    def test_known_is_not_the_same_as_dispatchable(self):
        """「知道」与「能派发」是两个概念，混淆它就是闸门失效的前置。"""
        assert ex.known_majors("opencode") == (1, 2)
        assert ex.dispatchable_majors("opencode") == (1,)

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

    def test_v2_adapter_exists_but_is_not_verified(self):
        """**存在但未验证** —— 这是本次改动的核心状态。

        有了它，版本闸门才能说「认识但没验过」而不是笼统的「不认识」。
        """
        a = ex.adapter_for(2)
        assert a is not None
        assert a.verified is False


class TestV2RefusesLoudly:
    """V2 照文档写、没实测 —— 所以每个方法都要抛，而不是给一个猜的形状。"""

    @pytest.mark.parametrize(
        "call",
        [
            lambda a: a.build_permission_config(),
            lambda a: a.parse_request({"id": "per_1", "permission": "bash"}),
            lambda a: a.build_reply("allow"),
        ],
        ids=["permission_config", "parse_request", "reply_payload"],
    )
    def test_every_entry_point_raises(self, call):
        a = ex.adapter_for(2)
        with pytest.raises(ex.UnverifiedExecutorError) as exc:
            call(a)
        msg = str(exc.value)
        # 错误信息要能照着做：说清缺的是什么、要去哪改
        assert "尚未验证" in msg
        assert "executors.py" in msg or "官方文档" in msg

    def test_error_names_the_missing_piece(self):
        """具体到「数组元素的形状」，而不是笼统的「不支持」。"""
        a = ex.adapter_for(2)
        with pytest.raises(ex.UnverifiedExecutorError) as exc:
            a.build_permission_config()
        assert "数组元素" in str(exc.value)

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
        """闸门放行的版本来自注册表，不是硬编码常量。"""
        assert dg.SUPPORTED_OPENCODE_MAJOR == 1
        assert dg.SUPPORTED_OPENCODE_MAJOR in ex.dispatchable_majors("opencode")

    def test_expected_version_passes(self):
        assert dg.version_mismatch_reason(1) is None

    def test_unprobeable_is_refused(self):
        r = dg.version_mismatch_reason(None) or ""
        assert "探不到" in r

    def test_unverified_major_is_refused_with_its_own_reason(self):
        r = dg.version_mismatch_reason(2) or ""
        assert "未验证" in r
        assert "数组元素" in r, "要指出到底缺哪一块，而不只是「不支持」"

    def test_unknown_major_is_refused_with_a_different_reason(self):
        """三种拒绝必须**各不相同** —— 该做的事不同，说法就该不同。"""
        unknown = dg.version_mismatch_reason(3) or ""
        unverified = dg.version_mismatch_reason(2) or ""
        assert unknown != unverified
        assert "不认识" in unknown
        assert "升级本仓" in unknown or "降回" in unknown

    def test_the_message_lists_known_versions(self):
        r = dg.version_mismatch_reason(3) or ""
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
        """V1 的字段名与键序 —— 键序有意义（last matching rule wins）。"""
        cfg = oc.delegation_permission_config()
        assert "permission" in cfg, "V1 用 permission 对象"
        body = cfg["permission"]
        assert list(body) == [
            "*", "edit", "bash", "webfetch", "websearch",
            "task", "external_directory",
        ], "通配必须最前、deny 必须最后，否则被 *: allow 覆盖"
        assert body["external_directory"] == "deny", "永不越界"
        assert body["task"] == "deny", "不让它拉子代理"

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
        """换适配器必须能换回来 —— 它是全局状态，测试之间会互相污染。"""
        original = oc.current_adapter()
        v2 = ex.adapter_for(2)
        try:
            oc.set_current_adapter(v2)
            assert oc.current_adapter() is v2
            # 换过去之后调用会响，而不是静默给出 V1 的形状
            with pytest.raises(ex.UnverifiedExecutorError):
                oc.delegation_permission_config()
        finally:
            oc.set_current_adapter(None)
        assert oc.current_adapter() is original
        assert "permission" in oc.delegation_permission_config()

    def test_set_current_adapter_none_resets_to_v1(self):
        original = oc.current_adapter()
        oc.set_current_adapter(ex.adapter_for(2))
        oc.set_current_adapter(None)
        assert oc.current_adapter().major == original.major
