"""引擎用量读取的规矩（V1.133）：认得出才报数，认不出就说读不到。

为什么单独测：这一层是**呈现层的附加信息**，形状没有版本承诺（V1.110
踩过直接读 SQLite 的坑）。守卫要把「认不出 → unreadable」钉死 ——
把读不到显示成「用了 0」是本仓最坏的一类说谎。
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime

import pytest

from freeagent.services import engine_host as host_mod
from freeagent.services.clock import FrozenClock
from freeagent.services.engine_usage import read_session_usage


def _call(code: int = 200, payload: object = None, raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    seen: list[tuple] = []

    def call(path: str, method: str = "GET", body_arg=None, *, timeout: float = 5.0):
        seen.append((path, method, body_arg, timeout))
        return code, body

    return call, seen


class TestReadSessionUsage:
    def test_reads_all_three_counts(self):
        call, seen = _call(payload={"tokens": {"input": 12, "output": 34, "total": 46}})
        u = read_session_usage("ses-1", call)
        assert (u.state, u.input, u.output, u.total) == ("ok", 12, 34, 46)
        assert u.readable
        assert seen[0][0] == "/api/session/ses-1"

    def test_reads_under_data_and_alias_keys(self):
        call, _ = _call(payload={"data": {"tokens": {"promptTokens": 9,
                                                     "completionTokens": 3}}})
        u = read_session_usage("ses-1", call)
        assert u.readable and u.input == 9 and u.output == 3 and u.total == 12

    def test_plain_number_is_total_only(self):
        call, _ = _call(payload={"tokens": 77})
        u = read_session_usage("ses-1", call)
        assert u.readable and u.total == 77 and u.input == 0

    def test_unknown_shape_is_unreadable_with_keys(self):
        call, _ = _call(payload={"tokens": {"prompt": "文字而不是数"}})
        u = read_session_usage("ses-1", call)
        assert not u.readable and u.state == "unreadable"
        assert "prompt" in u.reason

    def test_bool_is_not_a_count(self):
        call, _ = _call(payload={"tokens": {"input": True}})
        u = read_session_usage("ses-1", call)
        assert not u.readable

    def test_non_200_reports_status_not_zero(self):
        call, _ = _call(code=503, raw=b"upstream down")
        u = read_session_usage("ses-1", call)
        assert not u.readable and "503" in u.reason

    def test_non_json_is_unreadable(self):
        call, _ = _call(raw=b"<html>not json</html>")
        u = read_session_usage("ses-1", call)
        assert not u.readable and "JSON" in u.reason

    def test_call_raising_is_unreadable_not_crash(self):
        def boom(*a, **k):
            raise OSError("connection refused")
        u = read_session_usage("ses-1", boom)
        assert not u.readable and "connection refused" in u.reason

    def test_no_session_id_is_unreadable(self):
        call, seen = _call(payload={"tokens": {}})
        u = read_session_usage("", call)
        assert not u.readable and seen == []


class _UsageServer:
    """宿主替身：带 call，且会数调用次数（验缓存）。"""

    instances: list["_UsageServer"] = []

    def __init__(self, **kwargs):
        self.calls = 0
        self.payload = {"tokens": {"input": 5, "output": 7}}
        _UsageServer.instances.append(self)

    def start(self):
        return self

    def create_session(self):
        return "ses-1"

    def healthy(self):
        return True

    def prompt_async(self, *a, **k):
        pass

    def events(self, *, timeout=600.0):
        return iter(())

    def abort(self, *a, **k):
        pass

    def close(self):
        pass

    def call(self, path, method="GET", body=None, *, timeout=30.0):
        self.calls += 1
        return 200, json.dumps(self.payload).encode("utf-8")


@pytest.fixture()
def usage_host(tmp_path: pathlib.Path) -> host_mod.EngineHost:
    _UsageServer.instances.clear()
    h = host_mod.EngineHost(
        server_factory=_UsageServer,
        clock=FrozenClock(datetime(2026, 9, 26, 9, 0)),
    )
    h.ensure(directory=str(tmp_path), mode=host_mod.ENGINE_ASK)
    return h


class TestHostUsage:
    def test_reads_when_ready(self, usage_host):
        u = usage_host.usage()
        assert u["state"] == "ok" and u["input"] == 5 and u["output"] == 7

    def test_caches_within_ttl(self, usage_host):
        usage_host.usage()
        usage_host.usage()
        assert _UsageServer.instances[0].calls == 1

    def test_max_age_zero_forces_reread(self, usage_host):
        usage_host.usage()
        usage_host.usage(max_age=0)
        assert _UsageServer.instances[0].calls == 2

    def test_unreadable_before_ready(self, tmp_path):
        h = host_mod.EngineHost(server_factory=_UsageServer,
                                clock=FrozenClock(datetime(2026, 9, 26, 9, 0)))
        u = h.usage()
        assert u["state"] == "unreadable" and u["reason"]

    def test_halt_clears_cache(self, usage_host):
        usage_host.usage()
        usage_host.halt()
        assert usage_host.usage()["state"] == "unreadable"

    def test_adapter_without_call_is_unreadable(self, tmp_path):
        class NoCall(_UsageServer):
            call = None  # 类属性遮蔽父类方法 → getattr 拿不到可调用的 call
        h = host_mod.EngineHost(server_factory=NoCall,
                                clock=FrozenClock(datetime(2026, 9, 26, 9, 0)))
        h.ensure(directory=str(tmp_path), mode=host_mod.ENGINE_ASK)
        u = h.usage()
        assert u["state"] == "unreadable" and "接口" in u["reason"]
