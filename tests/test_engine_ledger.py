"""执行器的改动账（12.10.13 / V1.110）。

守的核心不是「数对了」，而是**三句话不许混成一句**：

- 零改动必须说「零改动」，不许说成「完成」；
- 读不到必须说「读不到」，不许落成「零改动」；
- 只翻了半本账必须说「至少」，不许报成整本。

外加一条设计守卫：**不许回去读执行器的库**（V2 上那条路会静默报零改动，
见 ``services.engine_ledger`` 模块文档）。
"""

from __future__ import annotations

import inspect
import json

import pytest

from freeagent.services import engine_ledger as mod
from freeagent.services.engine_ledger import (
    CHANGED,
    NO_CHANGE,
    UNREADABLE,
    EngineLedger,
    read_session_ledger,
    render_ledger,
)


class FakeServer:
    """假的执行器服务：按顺序吐预设响应，并记下被调过的路径。"""

    def __init__(self, *responses):
        # responses: (code, payload) —— payload 是 dict 时自动转 JSON bytes
        self._responses = list(responses)
        self.paths: list[str] = []
        self.calls = 0

    def call(self, path, method="GET", body=None, *, timeout=30.0):
        self.paths.append(path)
        assert self.calls < len(self._responses), f"多调了一次：{path}"
        code, payload = self._responses[self.calls]
        self.calls += 1
        if isinstance(payload, (dict, list)):
            return code, json.dumps(payload).encode()
        if isinstance(payload, str):
            return code, payload.encode()
        return code, payload


def page(messages, cursor=None):
    body = {"data": messages}
    if cursor:
        body["cursor"] = {"next": cursor}
    return 200, body


def tool(name, **state):
    part = {"type": "tool", "name": name}
    part["state"] = state
    return {"type": "assistant", "content": [part]}


def write(path, tool_name="write"):
    return tool(tool_name, input={"filePath": path})


def shell(code):
    return tool("shell", input={"command": "x"}, metadata={"exit": code})


class TestReadsTheSameThreeWays:
    def test_no_session_id_is_unreadable(self):
        ledger = read_session_ledger("")
        assert ledger.state == UNREADABLE
        assert "会话号" in ledger.reason

    def test_no_server_is_unreadable_not_empty(self):
        """没给服务 ≠ 服务说没改动。这条错了就会把环境问题报成「零改动」。"""
        ledger = read_session_ledger("ses_x", server=None)
        assert ledger.state == UNREADABLE
        assert "服务" in ledger.reason

    def test_session_not_found_is_unreadable(self):
        server = FakeServer((404, {"message": "Session not found"}))
        ledger = read_session_ledger("ses_gone", server=server)
        assert ledger.state == UNREADABLE
        assert "找不到" in ledger.reason

    def test_connection_failure_says_so(self):
        server = FakeServer((-1, "connection refused"))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.state == UNREADABLE
        assert "连不上" in ledger.reason

    def test_auth_failure_says_so(self):
        server = FakeServer((401, {"message": "Authentication required"}))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.state == UNREADABLE
        assert "鉴权" in ledger.reason

    def test_other_http_code_keeps_the_number(self):
        server = FakeServer((500, "boom"))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.state == UNREADABLE
        assert "500" in ledger.reason

    def test_broken_json_is_unreadable(self):
        server = FakeServer((200, "not json at all"))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.state == UNREADABLE

    def test_missing_data_list_is_unreadable(self):
        """形状不认识就是读不到 —— 当「没有消息」处理会落成零改动。"""
        server = FakeServer((200, {"rows": []}))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.state == UNREADABLE


class TestCounts:
    def test_empty_page_is_zero_change_not_failure(self):
        """零改动**不是失败**：有些委派本来就不该改文件。"""
        server = FakeServer(page([]))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.state == NO_CHANGE
        assert ledger.file_count == 0

    def test_write_and_edit_both_count(self):
        server = FakeServer(page([write("D:/a.py"), write("D:/b.py", "edit")]))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.state == CHANGED
        assert dict(ledger.files) == {"D:/a.py": 1, "D:/b.py": 1}

    def test_repeated_file_is_counted_and_sorted(self):
        msgs = [write("D:/hot.py")] * 3 + [write("D:/cold.py")]
        server = FakeServer(page(msgs))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.files[0] == ("D:/hot.py", 3)
        assert ledger.files[1] == ("D:/cold.py", 1)

    def test_read_tool_is_not_a_change(self):
        """「读过」不是「改过」。多认一个的代价是把浏览说成改动。"""
        server = FakeServer(page([tool("read", input={"filePath": "D:/a.py"})]))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.state == NO_CHANGE

    def test_path_key_is_accepted_too(self):
        server = FakeServer(page([tool("write", input={"path": "D:/c.py"})]))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.file_count == 1

    def test_nonzero_exit_counts_and_none_does_not(self):
        msgs = [shell(1), shell(2), shell(0), shell(None)]
        server = FakeServer(page(msgs))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.bash_failed == 2

    def test_bash_name_from_v1_is_accepted(self):
        server = FakeServer(page([tool("bash", metadata={"exit": 3})]))
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.bash_failed == 1

    def test_malformed_message_does_not_void_the_ledger(self):
        server = FakeServer(
            page([{"type": "assistant", "content": "字符串不是列表"},
                  {"type": "assistant"}, "整条不是字典",
                  write("D:/ok.py")])
        )
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.state == CHANGED
        assert ledger.file_count == 1


class TestPaging:
    def test_follows_cursor_across_pages(self):
        server = FakeServer(
            page([write("D:/a.py")], cursor="c1"),
            page([write("D:/b.py")], cursor="c2"),
            page([write("D:/c.py")]),
        )
        ledger = read_session_ledger("ses_x", server=server)
        assert ledger.file_count == 3
        assert ledger.truncated is False

    def test_cursor_is_passed_back_verbatim(self):
        server = FakeServer(page([write("D:/a.py")], cursor="tok/en+1"), page([]))
        read_session_ledger("ses_x", server=server)
        assert server.paths[0] == "/api/session/ses_x/message?limit=100"
        assert "cursor=tok%2Fen%2B1" in server.paths[1]

    def test_empty_page_with_cursor_stops_the_loop(self):
        """实测：最后一页给 0 条、游标仍非空。空页才是终点。

        只看游标会多翻一页；页数上限小的时候还会把自己误标成「翻满」。
        """
        server = FakeServer(
            page([write("D:/a.py")], cursor="c1"),
            page([], cursor="c2"),
        )
        ledger = read_session_ledger("ses_x", server=server)
        assert server.calls == 2
        assert ledger.truncated is False

    def test_hitting_the_page_cap_marks_truncated(self):
        """翻满页数上限 = 报了半本账，必须标出来，否则数字被当成全量。"""
        server = FakeServer(*[page([write(f"D:/f{i}.py")], cursor=f"c{i}")
                              for i in range(5)])
        ledger = read_session_ledger("ses_x", server=server, max_pages=3)
        assert ledger.truncated is True
        assert "至少" in render_ledger(ledger)

    def test_not_truncated_when_the_last_page_ends_cleanly(self):
        server = FakeServer(
            page([write("D:/a.py")], cursor="c1"),
            page([write("D:/b.py")]),
        )
        ledger = read_session_ledger("ses_x", server=server, max_pages=2)
        assert ledger.truncated is False


class TestRender:
    def test_three_states_say_three_different_things(self):
        changed = render_ledger(EngineLedger(CHANGED, files=(("a/b.py", 2),)))
        none = render_ledger(EngineLedger(NO_CHANGE))
        unreadable = render_ledger(EngineLedger(UNREADABLE, reason="连不上"))
        assert "动了" in changed
        assert "没有改动" in none and "不等于做完" in none
        assert "读不到" in unreadable and "别把「没记录」当成「没改」" in unreadable
        assert len({changed, none, unreadable}) == 3

    def test_zero_change_with_failures_mentions_them(self):
        text = render_ledger(EngineLedger(NO_CHANGE, bash_failed=4))
        assert "4 条命令非 0 退出" in text

    def test_paths_are_shortened(self):
        text = render_ledger(
            EngineLedger(CHANGED, files=(("D:\\Py\\proj\\src\\app.py", 1),))
        )
        assert "src/app.py" in text
        assert "D:" not in text

    def test_long_list_is_capped(self):
        files = tuple((f"D:/f{i}.py", 1) for i in range(6))
        text = render_ledger(EngineLedger(CHANGED, files=files), limit=2)
        assert "等 6 个文件" in text


class TestDesignGuard:
    def test_never_reads_the_engine_database(self):
        """回归守卫：V2 把消息搬到了 ``session_message``，读 ``part`` 表的那版
        会**安静地给每次委派报零改动**。这条用例挡的是「有人再把它接回去」。
        """
        source = inspect.getsource(mod)
        # 只查导入：模块文档里**特意**写着 ``opencode.db`` / ``part`` 表
        # （那是「为什么不再这么做」的理由），连那个词一起禁掉就写不出理由了。
        assert "sqlite3" not in source

    def test_reads_the_documented_v2_route(self):
        server = FakeServer(page([]))
        read_session_ledger("ses_x", server=server)
        assert server.paths[0].startswith("/api/session/ses_x/message")
