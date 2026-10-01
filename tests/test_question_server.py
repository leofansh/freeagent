"""Layer 2: question wiring on the opencode client.

`test_question_adapter.py` covers the shape; this file covers *this* layer's
only real job: forwarding. The delegation line is mirrored from
`reply_permission` so a protocol change breaks both paths symmetrically.

## What is worth locking here

The permission path and the question path are two different endpoints with
two different payload shapes. The temptation is to merge them into one
`reply(request_id, value)`. That would be the bug this file exists to
prevent: `POST /permission/{id}/reply` wants `{"reply": "once"}`, while
`POST /question/{id}/reply` wants `{"answers": [["..."]]}`. One method
taking "the value" forces a type that is neither, and the 400 shows up on
a real machine, not here.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.services.executors import ToolQuestion  # noqa: E402
from freeagent.services.opencode_server import (  # noqa: E402
    OpenCodeServer,
    question_from_event,
    question_reply_payload,
)


class TestQuestionFromEvent:
    def test_measured_shape(self) -> None:
        got = question_from_event({
            "id": "que_1",
            "sessionID": "ses_1",
            "questions": ["用哪个?"],
            "options": ["A", "B"],
        })
        assert got == ToolQuestion(
            request_id="que_1", questions=("用哪个?",), options=("A", "B"))

    def test_unrecognised_is_none(self) -> None:
        """``None`` means "not a question I can answer" -- **not** "unanswered".

        The caller must be able to tell those apart: the first should send
        nothing, the second should be treated as a timeout. Conflating them
        is how a malformed event turns into a silently-hanging agent.
        """
        assert question_from_event({"id": "q", "questions": [{"x": 1}]}) is None
        assert question_from_event(None) is None


class TestQuestionReplyPayload:
    def test_nested_array(self) -> None:
        assert question_reply_payload([["a"], ["b"]]) == {"answers": [["a"], ["b"]]}

    def test_refuses_flat(self) -> None:
        with pytest.raises(Exception):
            question_reply_payload(["a"])

    def test_refuses_empty(self) -> None:
        with pytest.raises(Exception):
            question_reply_payload([])


class _RecordingServer(OpenCodeServer):
    """Captures calls instead of talking to a real opencode."""

    def __init__(self) -> None:            # noqa: D107 - deliberately no super()
        self.calls: list[tuple[str, str, Any]] = []
        self.auth = ""

    def _must(self, path: str, method: str = "GET", body: Any = None) -> Any:
        self.calls.append((path, method, body))
        return None


class TestReplyQuestion:
    def _server(self) -> _RecordingServer:
        # password/base_url are set in __init__; we call it but stub the process
        # bits we never touch.
        s = _RecordingServer.__new__(_RecordingServer)
        OpenCodeServer.__init__(
            s, project=Path("C:/p"), executable="opencode", port=4096)
        s.calls = []
        return s

    def test_path_is_the_question_endpoint(self) -> None:
        """**Not** the permission endpoint. They are different endpoints."""
        s = self._server()
        s.reply_question("que_1", [["答案"]])
        path, method, _ = s.calls[0]
        assert path == "/question/que_1/reply"
        assert method == "POST"

    def test_body_is_nested(self) -> None:
        s = self._server()
        s.reply_question("que_1", [["第一题"], ["第二题"]])
        _, _, body = s.calls[0]
        assert body == {"answers": [["第一题"], ["第二题"]]}
        # and it survives a JSON round trip as an array of arrays
        assert json.loads(json.dumps(body))["answers"][0] == ["第一题"]

    def test_request_id_is_url_escaped(self) -> None:
        """An unescaped ``/`` would split the path and hit a **different**
        endpoint -- and the failure would look like "the server said no"."""
        s = self._server()
        s.reply_question("que/../admin", [["x"]])
        path = s.calls[0][0]
        assert "/" not in path[len("/question/"):-len("/reply")]
        assert ".." not in path.split("/")[2]

    def test_directory_is_appended_as_query(self) -> None:
        s = self._server()
        s.reply_question("q", [["x"]], directory="D:/proj dir")
        path = s.calls[0][0]
        assert path.startswith("/question/q/reply?directory=")
        assert " " not in path            # escaped
        assert urllib.parse.unquote(path.split("=", 1)[1]) == "D:/proj dir"

    def test_empty_answers_never_reaches_the_wire(self) -> None:
        """Nobody answered -> we must not send an answer at all.

        The adapter refuses it, so nothing is transmitted. That matters:
        an empty array would tell the agent "the human had nothing to say",
        which is a different (and false) statement.
        """
        s = self._server()
        with pytest.raises(Exception):
            s.reply_question("q", [])
        assert s.calls == []


class TestPendingQuestions:
    def test_lists_from_the_question_endpoint(self) -> None:
        s = _RecordingServer.__new__(_RecordingServer)
        OpenCodeServer.__init__(s, project=Path("C:/p"), port=4096)
        s.calls = []
        # _must normally returns the parsed body; make it look like a list
        s._must = lambda path, method="GET", body=None: s.calls.append(
            (path, method, body)) or [{"id": "que_1"}]
        got = s.pending_questions()
        assert got == [{"id": "que_1"}]
        assert s.calls[0][0] == "/question"

    def test_non_list_body_becomes_empty(self) -> None:
        """A dict where we expect a list is **not** something to iterate."""
        s = _RecordingServer.__new__(_RecordingServer)
        OpenCodeServer.__init__(s, project=Path("C:/p"), port=4096)
        s._must = lambda path, method="GET", body=None: {"id": "q"}
        assert s.pending_questions() == []