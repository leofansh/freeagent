"""hermes API Server 客户端的**假服务端夹具**测试（设计文档 11.10.8 判据 6）。

## 为什么要这个夹具

判据 2（危险命令 deny）与判据 4（stop → cancelled）的真机实测**被 hermes
模型账户阻塞**（免费档到期、付费档余额不足，``GET /v1/models`` 只有
``hermes-agent``）—— 那不是 FreeAgent 的错，是 hermes 侧没配可用模型。
但「客户端能不能正确走一遍协议」不能因此就悬着：那正是 V1.16 记过的
「文档比事实强」。所以这里用一个 :mod:`http.server` 的假服务端，把
**协议形状**（``input`` 字段、``run_id`` 字段、SSE 帧、``/stop``）锁死，
不依赖真机模型。真机传输层的形状（判据 5）已经对着真实 8642 验过，
本夹具锁的是同一套形状，两边互相印证。

假服务端只实现 client 真正会打的四个端点，别的不理（收到就 404）——
client 打了一个它不该打的面，正好在这里露出来。
"""
from __future__ import annotations

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

from freeagent.services import hermes_client as hc


# --------------------------------------------------------------------------- #
# 假服务端
# --------------------------------------------------------------------------- #

class _FakeHermesHandler(BaseHTTPRequestHandler):
    """按脚本回放四个端点。``events_script`` 挂在 server 上供 handler 读。"""

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - HTTP 方法名就是这写法
        if self.path == "/v1/capabilities":
            self._json(200, {"object": "hermes.api_server.capabilities",
                             "features": {"run_submission": True}})
            return
        if self.path.startswith("/v1/runs/") and self.path.endswith("/events"):
            # SSE：逐帧写 data: <json>\n\n，写完关连接。
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for frame in self.server.events_script:  # type: ignore[attr-defined]
                line = ("data: " + json.dumps(frame) + "\n\n").encode()
                self.wfile.write(f"{len(line):x}\r\n".encode())
                self.wfile.write(line)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode()) if raw else {}
        except ValueError:
            body = {}
        if self.path == "/v1/runs":
            # 锁协议：问题在 ``input`` 字段（判据 5 真机实测）。
            self.server.received_runs.append(body)  # type: ignore[attr-defined]
            if "input" not in body:
                self._json(400, {"error": "Missing 'input' field"})
                return
            self._json(202, {"run_id": "run_test", "status": "started", "replayed": False})
            return
        if "/stop" in self.path:
            self.server.received_stops.append(self.path)  # type: ignore[attr-defined]
            self._json(200, {"status": "stopped"})
            return
        self._json(404, {"error": "not found"})

    def log_message(self, *_: Any) -> None:  # 别刷日志
        return


def _make_fake(events_script: list[dict[str, Any]]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHermesHandler)
    server.daemon_threads = True
    server.events_script = events_script  # type: ignore[attr-defined]
    server.received_runs = []  # type: ignore[attr-defined]
    server.received_stops = []  # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address
    return server, f"http://127.0.0.1:{port}"


@contextlib.contextmanager
def _fake(events_script: list[dict[str, Any]]):
    """起假服务端，测试结束**关 socket** —— 不然 pytest 把 ResourceWarning 当错。"""
    server, base = _make_fake(events_script)
    try:
        yield server, base
    finally:
        server.shutdown()
        server.server_close()


def _make_server(base_url: str) -> Callable[..., hc.HermesClient]:
    """给 ask_executor 的 make_server：指到假服务端。"""
    def _factory(project, command, **kw):
        return hc.HermesClient(project, base_url=base_url, api_key="test-key", **kw)
    return _factory


# --------------------------------------------------------------------------- #
# 客户端生命周期（判据 6 · 协议形状）
# --------------------------------------------------------------------------- #

class TestHermesClientProtocol:
    def test_start_checks_capabilities(self):
        with _fake([]) as (server, base):
            with hc.HermesClient("C:/x", base_url=base, api_key="k").start() as c:
                assert c.base_url == base

    def test_prompt_submits_input_and_reads_run_id(self):
        with _fake([]) as (server, base):
            with hc.HermesClient("C:/x", base_url=base, api_key="k") as c:
                c.create_session()
                c.prompt_async("tok", "你好，别调用工具")
                # 协议形状：请求体是 ``input``（不是 ``prompt``）。
                assert server.received_runs == [{"input": "你好，别调用工具"}]
                assert c._run_id == "run_test"

    def test_events_translate_sse_frames_to_internal_names(self):
        script = [
            {"event": "message.delta", "delta": "你"},
            {"event": "message.delta", "delta": "好"},
            {"event": "tool.completed", "tool": "bash"},
            {"event": "run.completed"},
        ]
        with _fake(script) as (server, base):
            with hc.HermesClient("C:/x", base_url=base, api_key="k") as c:
                c.create_session()
                c.prompt_async("tok", "问题")
                got = list(c.events())
            assert got == [
                ("message.part.delta", {"delta": "你"}),
                ("message.part.delta", {"delta": "好"}),
                ("tool", {"tool": "bash"}),
                ("session.idle", None),
            ]

    def test_abort_posts_stop(self):
        with _fake([]) as (server, base):
            with hc.HermesClient("C:/x", base_url=base, api_key="k") as c:
                c.create_session()
                c.prompt_async("tok", "问题")
                c.abort("tok")
            assert len(server.received_stops) == 1
            assert server.received_stops[0] == "/v1/runs/run_test/stop"

    def test_missing_input_is_rejected_loudly(self):
        """假服务端按真机形状（缺 ``input`` → 400）回放，client 必须响亮失败。"""
        original = _FakeHermesHandler.do_POST
        with _fake([]) as (server, base):
            def do_POST(self):  # noqa: N802
                # 不写 received_runs，直接 400（模拟真机 Missing input）
                self._json(400, {"error": "Missing 'input' field"})
            _FakeHermesHandler.do_POST = do_POST
            try:
                c = hc.HermesClient("C:/x", base_url=base, api_key="k")
                with c:
                    c.create_session()
                    with pytest.raises(hc.ServerError):
                        c.prompt_async("tok", "问题")
            finally:
                _FakeHermesHandler.do_POST = original


# --------------------------------------------------------------------------- #
# ask_executor 端到端（不依赖真机模型，锁 ask 的事件分支）
# --------------------------------------------------------------------------- #

class TestAskExecutorAgainstFake:
    def test_accumulates_text_until_idle(self):
        script = [
            {"event": "message.delta", "delta": "好的"},
            {"event": "message.delta", "delta": "，明白"},
            {"event": "run.completed"},
        ]
        with _fake(script) as (server, base):
            from freeagent.services.ask import ask_executor
            out = ask_executor(
                "问题", project="C:/x",
                make_server=_make_server(base), command="hermes",
            )
            assert out.ok
            assert out.text == "好的，明白"
            assert out.tool_calls == 0

    def test_permission_asked_aborts_and_posts_stop(self):
        script = [{"event": "approval.request", "id": "a1"}]
        with _fake(script) as (server, base):
            from freeagent.services.ask import ask_executor
            out = ask_executor(
                "问题", project="C:/x",
                make_server=_make_server(base), command="hermes",
            )
            assert out.ok is False
            assert "权限" in out.reason
            # ask 中止时会试着叫停 —— 假服务端应收到 /stop。
            assert len(server.received_stops) == 1

    def test_run_failed_surfaces_the_executor_error(self):
        """``run.failed`` 的 ``error`` 要报成真话（如模型到期/余额不足），
        而不是笼统的「执行器报错」。"""
        script = [{"event": "run.failed", "error": "HTTP 404: model free period ended"}]
        with _fake(script) as (server, base):
            from freeagent.services.ask import ask_executor
            out = ask_executor(
                "问题", project="C:/x",
                make_server=_make_server(base), command="hermes",
            )
            assert out.ok is False
            assert "free period ended" in out.reason, (
                f"应带出执行器的真实 error，却给了 {out.reason!r}"
            )

    def test_tool_completed_counts_against_budget(self):
        """工具轮次预算照常累计 —— 事件名折成内部 ``tool`` 后才算数。"""
        script = [
            {"event": "tool.completed", "tool": "bash"},
            {"event": "message.delta", "delta": "在忙"},
            {"event": "run.completed"},
        ]
        with _fake(script) as (server, base):
            from freeagent.services.ask import ask_executor
            out = ask_executor(
                "问题", project="C:/x",
                make_server=_make_server(base), command="hermes",
                max_tool_calls=1,
            )
            assert out.ok
            assert out.text == "在忙"
            assert out.tool_calls == 1

    def test_ask_factory_accepts_permission_config_kwarg(self):
        """ask_executor 会传 ``permission_config=...`` —— HermesClient 必须接住。"""
        with _fake([{"event": "run.completed"}]) as (server, base):
            from freeagent.services.ask import ask_executor
            out = ask_executor(
                "问题", project="C:/x",
                make_server=_make_server(base), command="hermes",
            )
            assert out.ok
