"""提问链路的适配器层：形状归一化与答复载荷。

与 `test_tool_gate.py` 的分工：那边测 permission 的**集成**（发卡 → 等 →
答复 → opencode 收到什么），这边只测**协议形状**这一层，且只测适配器 ——
即「后端特有的知识有没有被关在这一个文件里」。

## 这批测试真正在守的三件事

1. **不认识就不认，绝不半截解析。** `questions` 里出现非字符串元素时，
   整条返回 `None`，而不是丢掉那一题继续 —— 丢掉的后果是人答 N-1 题、
   agent 拿残缺答案继续干活，且哪儿都不报错。
2. **答复载荷必须是嵌套数组。** V1 实测扁平字符串被 400 拒。
   宁可在这里炸，也别让一个扁平列表悄悄变成 `[[...]]`。
3. **空答复发不出去。** 没人答就不该造一个答复。
"""
import pytest

from freeagent.services.executors import (
    ToolQuestion,
    UnverifiedExecutorError,
    adapter_for,
)

V1 = adapter_for(1)
V2 = adapter_for(2)


class TestParseQuestion:
    def test_measured_payload(self):
        """实测载荷：id + questions[] + options[]。"""
        got = V1.parse_question({
            "id": "que_abc",
            "questions": ["用哪个数据库?", "要不要开缓存?"],
            "options": ["Postgres", "SQLite"],
        })
        assert got == ToolQuestion(
            request_id="que_abc",
            questions=("用哪个数据库?", "要不要开缓存?"),
            options=("Postgres", "SQLite"),
        )

    def test_options_absent_is_empty_not_error(self):
        got = V1.parse_question({"id": "q1", "questions": ["只有问题"]})
        assert got.options == ()

    def test_options_absent_key_vs_empty_list(self):
        """键不存在与空列表都要得到空元组，而不是 None 或报错。"""
        a = V1.parse_question({"id": "q1", "questions": ["x"]})
        b = V1.parse_question({"id": "q1", "questions": ["x"], "options": []})
        assert a.options == b.options == ()

    @pytest.mark.parametrize("props", [
        None, [], "x", 42,
        {},                                        # 没 id
        {"id": "", "questions": ["x"]},            # 空 id
        {"id": 7, "questions": ["x"]},              # id 不是字符串
        {"id": "q1"},                              # 没 questions
        {"id": "q1", "questions": []},              # 空 questions
        {"id": "q1", "questions": "x"},            # questions 不是数组
        {"id": "q1", "questions": ["  "]},          # 全空白
    ])
    def test_unusable_shapes_return_none(self, props):
        """这些都**不是**一条能回应的提问。绝不用半截形状凑。"""
        assert V1.parse_question(props) is None

    def test_non_string_question_aborts_the_whole_thing(self):
        """**核心判断**：丢掉一题比整条不认坏得多。

        丢掉的后果不是「少显示点东西」，而是人答了 N-1 题、agent 拿着
        残缺的答案继续干活，且哪儿都不报错。
        """
        assert V1.parse_question(
            {"id": "q1", "questions": ["好的问题", {"text": "不认识"}]}
        ) is None

    def test_non_string_option_aborts_too(self):
        """选项形状不认识时也不认这条 —— 别渲染出一堆没意义的空行。"""
        assert V1.parse_question(
            {"id": "q1", "questions": ["x"], "options": [{"label": "y"}]}
        ) is None

    def test_blank_option_is_dropped_not_fatal(self):
        """空串选项丢掉即可：它是**值**的问题，不是**形状**的问题。"""
        got = V1.parse_question(
            {"id": "q1", "questions": ["x"], "options": ["A", "  ", "B"]}
        )
        assert got.options == ("A", "B")

    def test_questions_are_stripped(self):
        got = V1.parse_question({"id": "q1", "questions": ["  问题  "]})
        assert got.questions == ("问题",)


class TestSummary:
    def test_counts_only(self):
        got = ToolQuestion(request_id="q", questions=("a", "b"))
        assert got.summary == "2 个问题"

    def test_with_options(self):
        got = ToolQuestion(request_id="q", questions=("a",), options=("x", "y"))
        assert "2 个可选项" in got.summary


class TestBuildQuestionReply:
    def test_nested_array_is_the_shape(self):
        """V1 实测只接受嵌套数组。"""
        got = V1.build_question_reply([["第一题"], ["第二题"]])
        assert got == {"answers": [["第一题"], ["第二题"]]}

    def test_single_answer_still_nested(self):
        got = V1.build_question_reply([["就一个"]])
        assert got == {"answers": [["就一个"]]}

    def test_flat_list_is_refused(self):
        """**核心判断**：`["答案"]` 不接受。

        与其悄悄变成 `[["答案"]]`（于是「两题只答一题」被当成答完了），
        不如在这里炸掉。
        """
        with pytest.raises(Exception):
            V1.build_question_reply(["答案"])

    def test_empty_is_refused(self):
        """没人答就不该造一个答复发回去。"""
        with pytest.raises(Exception):
            V1.build_question_reply([])

    def test_blank_answer_is_refused(self):
        """``[""]`` 会把一个空答案交给 agent。

        `ApprovalStore.put_answer` 已经拒空白，但那道闸门在上一层；
        这里再挡一次 —— 否则将来某个调用方能从旁边绕过去。
        """
        with pytest.raises(Exception):
            V1.build_question_reply([["   "]])

    def test_empty_row_is_refused(self):
        with pytest.raises(Exception):
            V1.build_question_reply([[]])

    def test_error_names_which_question(self):
        """报错要能定位到第几题，否则两题时无从下手。"""
        with pytest.raises(Exception) as exc:
            V1.build_question_reply([["好的"], [""]])
        assert "2" in str(exc.value)


class TestVersionGateAcceptsQuestions:
    """V2 对提问链路**已经对着真机验过**，这里守的是它的实测形状。

    原名 ``TestVersionGateRefusesQuestions``，写的是「V2 的提问也必须拒绝」。
    那在 V2 还没验过时是对的，理由仍然成立：猜一个「看起来很像对」的形状
    比拒绝更危险 —— 错的形状会让 agent 拿到一个自身的回答，而不是人的。
    V2 验过之后，这条纪律的**正面**形态是：按实测载荷解析，且选项**按题分组**
    （V1 那个「扁平选项池」在 V2 上已被证伪）。

    纪律本身没消失，只是载体从「拒绝」换成了「按实测形状解析」。
    """

    #: 一条 V2 ``form.created`` 的实测载荷（形状见 _v2_parse_question 的 docstring）
    _FORM = {
        "id": "frm_1",
        "sessionID": "ses_1",
        "title": "Questions",
        "metadata": {"kind": "question"},
        "fields": [
            {
                "key": "q0",
                "title": "Color preference",
                "type": "string",
                "options": [
                    {"value": "red", "label": "red"},
                    {"value": "blue", "label": "blue"},
                ],
            },
            {"key": "q1", "title": "Size", "type": "string"},
        ],
    }

    def test_v2_question_adapter_parses_the_real_form_shape(self):
        q = V2.parse_question(self._FORM)
        assert q is not None
        assert q.request_id == "frm_1"
        assert q.questions == ("Color preference", "Size")

    def test_v2_options_are_grouped_per_question(self):
        """V2 的 options 挂在 field 上，一题一组。

        若填成扁平 ``options``，第 2 题的选项会混到第 1 张卡上 ——
        那正是 V1 那个被证伪的语义。``options`` 必须留空。
        """
        q = V2.parse_question(self._FORM)
        assert q is not None
        assert q.options == (), "扁平选项池在 V2 上是错的语义"
        assert q.option_groups == (("red", "blue"), ())

    def test_v2_question_reply_uses_the_answer_envelope(self):
        """V2 的答复是 ``{"answer": {"q0": ...}}``，键名按位置推导。"""
        out = V2.build_question_reply([["red"], ["large"]])
        assert out == {"answer": {"q0": "red", "q1": "large"}}

    def test_v2_is_dispatchable(self):
        """V2 现在也是已验证的，所以它应该能派发。"""
        from freeagent.services.executors import dispatchable_majors
        assert 2 in dispatchable_majors()
        assert set(dispatchable_majors()) == {1, 2}
