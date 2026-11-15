"""执行器自报结局的对账（12.10.14 / V1.111）。

守的是一条很窄的规矩：**只在会错判的那一侧开口**。

- 我们记成功、它说 failed → 必须说（否则人按「完成」验收，而执行器说没成）；
- 其余全部沉默，包括 outcome 为空 —— 空的四种来源在收尾那一刻分不开，
  报出来只会在大多数正常收尾上多加一句看不懂的话（宁可漏判，不错判）。

还有一条：它**不改 `ok`**。对账是标注，不是判据。
"""

from __future__ import annotations

import json

from freeagent.services.engine_verdict import (
    EngineVerdict,
    disagreement_line,
    read_engine_verdict,
)


class FakeServer:
    def __init__(self, code=200, payload=None):
        self.code = code
        self.payload = payload
        self.paths: list[str] = []

    def call(self, path, method="GET", body=None, *, timeout=30.0):
        self.paths.append(path)
        if isinstance(self.payload, (dict, list)):
            return self.code, json.dumps(self.payload).encode()
        if isinstance(self.payload, str):
            return self.code, self.payload.encode()
        return self.code, b""


def session(outcome="succeeded", **extra):
    data = {"id": "ses_x"}
    if outcome is not None:
        data["outcome"] = outcome
    data.update(extra)
    return {"data": data}


class TestReading:
    def test_reads_the_outcome_verbatim(self):
        server = FakeServer(payload=session("succeeded"))
        assert read_engine_verdict("ses_x", server=server).outcome == "succeeded"

    def test_unknown_value_is_kept_not_translated(self):
        """不认识的结局原样留着 —— 翻译成「失败」是替执行器下结论。"""
        server = FakeServer(payload=session("cancelled"))
        assert read_engine_verdict("ses_x", server=server).outcome == "cancelled"

    def test_missing_outcome_means_not_settled(self):
        """实测：没跑到 idle 的会话，响应里**没有 outcome 这个键**。"""
        server = FakeServer(payload=session(None))
        verdict = read_engine_verdict("ses_x", server=server)
        assert verdict.outcome == ""
        assert "落定" in verdict.reason

    def test_explicit_null_is_the_same_thing(self):
        server = FakeServer(payload={"data": {"id": "ses_x", "outcome": None}})
        assert read_engine_verdict("ses_x", server=server).outcome == ""

    def test_asks_the_session_route_not_the_message_route(self):
        server = FakeServer(payload=session())
        read_engine_verdict("ses_x", server=server)
        assert server.paths == ["/api/session/ses_x"]

    def test_no_session_id(self):
        verdict = read_engine_verdict("")
        assert verdict.outcome == ""
        assert "会话号" in verdict.reason

    def test_no_server(self):
        verdict = read_engine_verdict("ses_x", server=None)
        assert verdict.outcome == ""
        assert "服务" in verdict.reason

    def test_not_found(self):
        server = FakeServer(404, {"message": "Session not found"})
        verdict = read_engine_verdict("ses_x", server=server)
        assert "找不到" in verdict.reason

    def test_connection_failure(self):
        server = FakeServer(-1, "refused")
        assert "连不上" in read_engine_verdict("ses_x", server=server).reason

    def test_auth_failure(self):
        server = FakeServer(401, {"message": "Authentication required"})
        assert "鉴权" in read_engine_verdict("ses_x", server=server).reason

    def test_other_http_code_keeps_the_number(self):
        server = FakeServer(500, "boom")
        assert "500" in read_engine_verdict("ses_x", server=server).reason

    def test_broken_json(self):
        server = FakeServer(200, "not json")
        assert "JSON" in read_engine_verdict("ses_x", server=server).reason

    def test_data_not_an_object(self):
        server = FakeServer(200, {"data": []})
        assert "data" in read_engine_verdict("ses_x", server=server).reason

    def test_outcome_of_wrong_type(self):
        server = FakeServer(200, {"data": {"outcome": 5}})
        verdict = read_engine_verdict("ses_x", server=server)
        assert verdict.outcome == ""
        assert "形状" in verdict.reason


class TestOnlySpeaksOnOneSide:
    def test_we_say_ok_it_says_failed_speaks_up(self):
        line = disagreement_line(EngineVerdict("failed"), ours_ok=True)
        assert "执行器自报" in line
        assert "先别按完成验收" in line

    def test_agreement_says_nothing(self):
        assert disagreement_line(EngineVerdict("succeeded"), ours_ok=True) == ""

    def test_we_already_report_failure_says_nothing(self):
        """两条都判失败时再加一句「它也说失败」只是噪音。"""
        assert disagreement_line(EngineVerdict("failed"), ours_ok=False) == ""

    def test_not_settled_says_nothing(self):
        """空的四种来源分不开 —— 报成「没有佐证」会天天多一句废话。"""
        assert disagreement_line(EngineVerdict(""), ours_ok=True) == ""

    def test_unreadable_says_nothing(self):
        verdict = EngineVerdict("", reason="连不上执行器服务")
        assert disagreement_line(verdict, ours_ok=True) == ""

    def test_unknown_outcome_says_nothing(self):
        assert disagreement_line(EngineVerdict("cancelled"), ours_ok=True) == ""

    def test_wording_does_not_pretend_to_know_the_reason(self):
        """说事实，不编原因 —— 「可能有一条错误漏过去了」原文里带着「可能」。"""
        line = disagreement_line(EngineVerdict("failed"), ours_ok=True)
        assert "可能" in line
