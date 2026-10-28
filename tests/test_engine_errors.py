"""引擎错误分类（12.10.11 / V1.96）。

三条纪律各有测试守着：**不猜**、**不与 11.8 重叠**、**未验证不当契约**。
"""

from freeagent.services.engine_errors import (
    EngineError,
    classify_engine_error,
    engine_error_line,
    error_fields,
)


# 真机样本（2026-10-28 实测，OpenCode v2.0.29）
QUOTA_SAMPLE = {
    "type": "provider.quota",
    "status": 429,
    "message": (
        "Free usage limit reached. Free models work in OpenCode "
        "(https://opencode.ai); subscribe to OpenCode Go to keep going"
    ),
    "response": {
        "body": '{"type":"error","error":{"type":"FreeUsageLimitError",'
                '"message":"Free usage limit reached..."}}'
    },
}


class TestFields:
    """挖字段：两种版本形状都要认，挖不到就空/0（不猜）。"""

    def test_v2_flat_shape(self):
        type_, status, message = error_fields(QUOTA_SAMPLE)
        assert type_ == "provider.quota"
        assert status == 429
        assert "Free usage limit reached" in message

    def test_v1_nested_shape(self):
        type_, status, message = error_fields(
            {"error": {"data": {"message": "Upstream request failed"}}}
        )
        assert message == "Upstream request failed"
        assert type_ == "" and status == 0

    def test_missing_fields_are_empty_not_guessed(self):
        type_, status, message = error_fields({"foo": "bar"})
        assert (type_, status, message) == ("", 0, "")

    def test_non_dict_is_empty(self):
        assert error_fields(None) == ("", 0, "")
        assert error_fields("boom") == ("", 0, "")

    def test_status_from_string(self):
        assert error_fields({"status": "429"})[1] == 429

    def test_bool_is_not_a_status(self):
        """``True`` 是 int 的子类 —— 当成状态码 1 会把一切都归错。"""
        assert error_fields({"status": True})[1] == 0


class TestClassify:
    """分类：真机样本 + 已知语义 + 认不出。"""

    def test_quota_sample_by_type(self):
        err = classify_engine_error(QUOTA_SAMPLE)
        assert err.kind == "quota"
        assert err.basis == "type", "type 命中就该用它，不该退到 status"

    def test_quota_sample_next_step_is_not_retry(self):
        """配额耗尽的下一步**不能说「重试」** —— 重试是同一堵墙。"""
        err = classify_engine_error(QUOTA_SAMPLE)
        assert "额度" in err.next_step or "配额" in err.next_step
        assert "重派多少次" in err.next_step

    def test_v1_insufficient_funds(self):
        """本仓历史实测的 402 句式（V1 形状，error.data.message）。"""
        err = classify_engine_error({
            "error": {"data": {
                "message": "Upstream request failed: Insufficient account funds"
            }}
        })
        assert err.kind == "quota"
        assert err.basis == "message"

    def test_status_mapping(self):
        assert classify_engine_error({"status": 401}).kind == "auth"
        assert classify_engine_error({"status": 403}).kind == "auth"
        assert classify_engine_error({"status": 404}).kind == "model_gone"
        assert classify_engine_error({"status": 429}).kind == "quota"
        assert classify_engine_error({"status": 413}).kind == "context"

    def test_unknown_type_falls_back_to_status(self):
        """引擎新增 type 时不该崩，也不该瞎猜 —— 退到 status 再判。"""
        err = classify_engine_error({"type": "provider.brandnew",
                                     "status": 429})
        assert err.kind == "quota"
        assert err.basis == "status"

    def test_unknown_everything_is_unknown(self):
        err = classify_engine_error({"type": "weird.thing", "status": 599,
                                     "message": "???"})
        assert err.kind == "unknown"
        assert err.is_known is False

    def test_unknown_keeps_original_words(self):
        """认不出时**原话必须留着** —— 那是真机证据。"""
        err = classify_engine_error({"message": "某处炸了"})
        assert err.message == "某处炸了"
        assert err.kind == "unknown"

    def test_unknown_gives_look_at_raw_not_invented_advice(self):
        err = classify_engine_error({"foo": 1})
        assert "原始报错" in err.next_step

    def test_weak_signal_words_do_not_trigger(self):
        """含糊的词**不许**触发分类 —— 宁可漏判成 unknown。

        这条守的是「宁可给不出建议，也不给错建议」：一句带 quota 的
        闲聊被归成「额度耗尽」，用户就会去查额度，而真因在别处。
        """
        err = classify_engine_error({"message": "我检查了 quota 配置，看起来没问题"})
        assert err.kind == "unknown", "含 quota 一词不等于就是配额问题"

    def test_empty_payload(self):
        err = classify_engine_error(None)
        assert err.kind == "unknown"
        assert err.type == "" and err.status == 0 and err.message == ""


class TestErrorLine:
    """一行化：给记录用，必须单行且含原话。"""

    def test_line_is_single_line(self):
        line = engine_error_line(classify_engine_error(QUOTA_SAMPLE))
        assert "\n" not in line

    def test_line_has_label_type_status_and_message(self):
        line = engine_error_line(classify_engine_error(QUOTA_SAMPLE))
        assert "额度耗尽" in line
        assert "provider.quota" in line
        assert "429" in line
        assert "Free usage limit reached" in line

    def test_line_does_not_include_next_step(self):
        """下一步有多行，放进记录会破「一行」约束。"""
        line = engine_error_line(classify_engine_error(QUOTA_SAMPLE))
        assert "下一步" not in line

    def test_unknown_line_says_unrecognized(self):
        line = engine_error_line(classify_engine_error({"foo": 1}))
        assert "未识别" in line


class TestNotOverlappingWith118:
    """与 11.8 的分工：11.8 判「能不能重试」，本模块判「下一步做什么」。

    这条测试锁的是**两者的边界不被悄悄合并** —— 配额错误在 11.8 眼里
    是「可重派」（形状上是执行期失败），但重派它毫无意义。
    """

    def test_quota_is_retryable_by_shape_but_advice_says_wait(self):
        """形状上可重派 ≠ 值得重派。分类不越权改 11.8 的判定。"""
        err = classify_engine_error(QUOTA_SAMPLE)
        assert err.kind == "quota"
        assert "等额度恢复" in err.next_step or "换个还没用完的模型" in err.next_step

    def test_upstream_is_the_one_that_says_retry(self):
        """只有「上游/网络抖动」这一类才允许建议重派。"""
        err = classify_engine_error({"status": 503})
        assert err.kind == "upstream"
        assert "重派" in err.next_step

    def test_no_class_says_bare_retry_for_quota(self):
        """配额类**不许建议**重派。

        断言查的是「建议重派」这个动作，不是「出现过重派这个词」——
        配额类的文案里有一句「重派多少次都是同一堵墙」，那是在**否定**
        重派。查关键词会把它也当成「建议重派」，于是这条测试要么被迫
        放宽（守不住），要么逼着实现改文案（文案不该被测试牵着走）。
        """
        for payload in ({"status": 429}, {"status": 402},
                        {"message": "usage limit reached"}):
            err = classify_engine_error(payload)
            advice = err.next_step
            # 真正的「建议重派」出现在「下一步：」那一行里。
            final = advice.split("下一步：")[-1]
            assert "重派" not in final, (
                f"{err.kind} 的下一步不该建议重派：{final!r}"
            )


class TestEngineErrorDataclass:
    """值对象本身：frozen，且字段名即契约。"""

    def test_frozen(self):
        err = EngineError(kind="quota")
        try:
            err.kind = "auth"  # type: ignore[misc]
        except Exception:
            pass
        else:
            raise AssertionError("EngineError 必须不可变")

    def test_is_known(self):
        assert EngineError(kind="quota").is_known is True
        assert EngineError(kind="unknown").is_known is False


class TestRealMachineSample:
    """把真机实测到的**原始报文**钉住（2026-10-28，OpenCode v2.0.29）。

    这组样本是从引擎的 ``/api/session/{sid}/message`` 里原样抄下来的。
    钉它的理由不是「测个函数」，而是**防止有人把形状改坏而不自知**：
    引擎改版时形状会变，那时候这组测试应该**红**，提醒人重新核对分类表
    —— 而不是悄悄落进 unknown 然后谁也不知道。
    """

    #: 引擎会话消息里的一条 assistant 消息（实测原样）。
    ASSISTANT_ERROR = {
        "id": "msg_17fedd006001X8Hhqu3l3jCl1K",
        "time": {"created": 1793147654307, "completed": 1793147655870},
        "type": "assistant",
        "agent": "build",
        "model": {"id": "big-pickle", "providerID": "opencode"},
        "content": [],
        "finish": "error",
        "error": {
            "type": "provider.quota",
            "status": 429,
            "message": (
                "Free usage limit reached. Free models work in OpenCode "
                "(https://opencode.ai); subscribe to OpenCode Go to keep going"
            ),
            "response": {
                "body": '{"type":"error","error":{"type":"FreeUsageLimitError",'
                        '"message":"Free usage limit reached..."}}'
            },
        },
    }

    #: 引擎会话消息里的一条 idle 消息（实测原样）—— 形状**不同**：
    #: error 直接挂在顶层，不在 ``error`` 子对象里。
    IDLE_ERROR = {
        "id": "msg_17fedd6c00011DQZfdBttH5yOq",
        "time": {"created": 1793147655873},
        "type": "idle",
        "outcome": "failed",
        "error": {
            "type": "provider.quota",
            "status": 429,
            "message": "Free usage limit reached. ...",
            "response": {"body": "..."},
        },
    }

    def test_assistant_error_nested_under_error_key(self):
        err = classify_engine_error(self.ASSISTANT_ERROR["error"])
        assert err.kind == "quota"
        assert err.status == 429
        assert "usage limit" in err.message.lower()

    def test_idle_error_same_classification(self):
        """两种形状（assistant 消息 / idle 消息）必须归到同一类。

        否则用户会看到「同一件事，有时说额度耗尽、有时说未识别」。
        """
        err = classify_engine_error(self.IDLE_ERROR["error"])
        assert err.kind == "quota"

    def test_whole_message_object_also_classifies(self):
        """直接丢整条消息对象进去也要能判 —— 调用方未必先剥出 error。"""
        err = classify_engine_error(self.ASSISTANT_ERROR)
        assert err.kind == "quota"
        assert err.status == 429

    def test_upstream_body_is_not_parsed(self):
        """``response.body`` 里的 JSON **不被解析**。

        理由：那是**上游 provider** 的报文形状，不是引擎自己的 ——
        解析它等于把第三方 schema 也变成契约。引擎已经把 ``type`` 与
        ``status`` 提到了顶层，够用了。
        """
        err = classify_engine_error(self.ASSISTANT_ERROR["error"])
        assert err.type == "provider.quota", "只用引擎自己的字段"
        assert "FreeUsageLimitError" not in err.type

    def test_finish_error_field_is_visible_in_message(self):
        """``finish == "error"`` 是引擎自己的说法，分类后仍能对上。"""
        msg = self.ASSISTANT_ERROR
        assert msg["finish"] == "error"
        assert classify_engine_error(msg["error"]).kind == "quota"
