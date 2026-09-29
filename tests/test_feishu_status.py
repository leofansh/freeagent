"""状态上报（设计方案 12.7）。

重点不是「能写出 JSON」，而是**陈旧判定**：进程被强杀时状态文件会留在盘上，
界面若照着它显示「已连接」，就是对着尸体报健康 —— 那比没有状态更坏，
因为它给出虚假的安心。
"""

from __future__ import annotations

import threading
import time

from freeagent.feishu.status import (
    HEARTBEAT_SECONDS,
    STALE_AFTER_SECONDS,
    StatusReporter,
    is_stale,
    read_status,
    status_path,
    write_status,
)

BOT = "ou_bot_abc123"


class TestStaleness:
    """陈旧判定 —— 本模块存在的头号理由。"""

    def test_missing_file_is_stale(self):
        assert is_stale(None, now=1000.0), "没有状态文件就是没在跑"

    def test_missing_timestamp_is_stale(self):
        assert is_stale({}, now=1000.0), "没有心跳时间戳 = 不可信，宁可说陈旧"

    def test_wrong_type_timestamp_is_stale(self):
        assert is_stale({"updated_at": "1000"}, now=1000.0), (
            "字符串时间戳不可信，宁可说陈旧也不要假装健康"
        )

    def test_fresh_is_not_stale(self):
        assert not is_stale({"updated_at": 1000.0}, now=1000.0 + 10)

    def test_old_is_stale(self):
        assert is_stale(
            {"updated_at": 1000.0}, now=1000.0 + STALE_AFTER_SECONDS + 1
        )


class TestAtomicWrite:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "bridge_status.json"
        write_status(p, {"state": "ready", "connected": True})
        got = read_status(p)
        assert got is not None and got["state"] == "ready"

    def test_no_half_written_tmp_left(self, tmp_path):
        """原子写：断电不留半截文件。"""
        p = tmp_path / "bridge_status.json"
        write_status(p, {"state": "ready"})
        assert not p.with_suffix(".json.tmp").exists()

    def test_broken_file_returns_none_not_raises(self, tmp_path):
        """半截文件**不许**让界面 500 —— 当「陈旧」处理即可。"""
        p = tmp_path / "bridge_status.json"
        p.write_text("{ 半截", encoding="utf-8")
        assert read_status(p) is None
        assert is_stale(read_status(p), now=1e9)

    def test_non_dict_payload_returns_none(self, tmp_path):
        p = tmp_path / "bridge_status.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        assert read_status(p) is None


class TestReporter:
    def test_update_writes_immediately(self, tmp_path):
        """启动阶段那些状态正是用户急着看的，等一个心跳会被当成界面坏了。"""
        p = tmp_path / "bridge_status.json"
        r = StatusReporter(p, clock=lambda: 123.0)
        r.update(state="starting", connected=False)
        snap = read_status(p)
        assert snap is not None and snap["state"] == "starting"

    def test_payload_has_pid_and_timestamp(self, tmp_path):
        p = tmp_path / "bridge_status.json"
        StatusReporter(p, clock=lambda: 123.0).update(state="ready")
        snap = read_status(p)
        assert isinstance(snap["pid"], int)
        assert snap["updated_at"] == 123.0

    def test_fields_merge_rather_than_replace(self, tmp_path):
        """后一次 update 不该把之前的字段抹掉。"""
        p = tmp_path / "bridge_status.json"
        r = StatusReporter(p, clock=lambda: 1.0)
        r.update(state="starting", connected=False)
        r.update(state="ready", connected=True)
        snap = read_status(p)
        assert snap["state"] == "ready" and snap["connected"] is True
        assert isinstance(snap["pid"], int), "pid 不该被后续 update 抹掉"

    def test_heartbeat_keeps_fields(self, tmp_path):
        p = tmp_path / "bridge_status.json"
        r = StatusReporter(p, interval=0.02, clock=lambda: 1.0)
        r.update(dedup_entries=7)
        r.start()
        try:
            time.sleep(0.15)                 # 至少一个心跳周期
        finally:
            r.stop()
        assert read_status(p)["dedup_entries"] == 7

    def test_stop_is_idempotent(self, tmp_path):
        p = tmp_path / "bridge_status.json"
        r = StatusReporter(p, interval=0.02)
        r.update(state="ready")
        r.start()
        r.stop()
        r.stop()                            # 再停一次不能炸

    def test_start_is_idempotent(self, tmp_path):
        p = tmp_path / "bridge_status.json"
        r = StatusReporter(p, interval=0.02)
        r.start()
        r.start()                           # 不该起第二个线程
        r.stop()

    def test_write_failure_does_not_raise(self, tmp_path):
        """状态上报是**纯观测**，它掀翻主流程是本末倒置。"""
        p = tmp_path / "no_such_dir" / "x" / "bridge_status.json"
        blocked = tmp_path / "blocked"
        blocked.write_text("not a dir", encoding="utf-8")
        p = blocked / "bridge_status.json"  # 父路径是文件 -> 写必失败
        r = StatusReporter(p)
        r.update(state="ready")             # 不抛就算过


class TestLayout:
    def test_path_lives_next_to_dedup_table(self, tmp_path):
        """与去重表/身份缓存同目录，`--db` 换位置时跟着走（沿用 11.9.2）。"""
        p = status_path(tmp_path)
        assert p.name == "bridge_status.json"
        assert p.parent == (tmp_path / "feishu_seen_events.json").parent

    def test_heartbeat_much_faster_than_stale_threshold(self):
        """心跳若接近陈旧阈值，界面会间歇性显示「已陈旧」，立刻被当成 bug。"""
        assert HEARTBEAT_SECONDS * 3 < STALE_AFTER_SECONDS


class TestConnectionState:
    """连接状态**只报确证**（设计方案 12.7）。

    踩过的坑（会造成假健康）：原实现在 ``client.start()`` **之前**就写
    ``connected=True`` 并打「长连接已建立」，而文档还声称有个「短延时确认
    start() 没抛异常」的机制 —— 代码里根本没有那个延时。于是凭据错误时
    ``start()`` 立刻抛出，状态文件却仍写着 ready/connected，心跳照旧，
    界面就一直显示「已连接」，对着一个压根没连上的进程报健康。

    ``WSClient`` 没有「连上了」的回调（只有两个**重连**钩子，初次连接不走），
    所以确证信号只能从它自己打的日志里拿。
    """

    @staticmethod
    def _fake_client(path, *, on_start=None, error=None):
        """假 client：在 ``start()`` 里记下当时的状态，并可打 SDK 的日志。"""

        class FakeClient:
            def __init__(self) -> None:
                self.connected_when_called: object = "not-called"
                self.calls = 0

            def start(self) -> None:
                self.calls += 1
                snap = read_status(path)
                # 这一刻是关键：还没开始连，就不能已经报「已连接」。
                self.connected_when_called = (
                    snap.get("connected") if snap else None
                )
                if on_start is not None:
                    on_start()
                if error is not None:
                    raise error

        return FakeClient()

    @staticmethod
    def _sdk_log(text: str) -> None:
        import logging

        # 用真实的 logger 名字，走真实接线，而不是 mock 掉那一层。
        logging.getLogger("Lark").info(text)

    def test_not_connected_before_start_is_called(self, tmp_path):
        """**核心回归**：`start()` 刚被调用时不得已经是 connected。"""
        from freeagent.feishu.bridge import _run_connection

        p = tmp_path / "bridge_status.json"
        reporter = StatusReporter(p, clock=lambda: 1.0)
        client = self._fake_client(p, on_start=lambda: self._sdk_log(
            "connected to wss://example"))
        _run_connection(client, reporter, "ou_bot")
        assert client.connected_when_called is False

    def test_connected_only_after_the_sdk_says_so(self, tmp_path):
        """只有看到 SDK 的「connected to」才置位。"""
        from freeagent.feishu.bridge import _run_connection

        p = tmp_path / "bridge_status.json"
        reporter = StatusReporter(p, clock=lambda: 1.0)
        client = self._fake_client(p, on_start=lambda: self._sdk_log(
            "connected to wss://example"))
        _run_connection(client, reporter, "ou_bot")
        snap = read_status(p)
        assert snap["connected"] is True
        assert snap["state"] == "ready"

    def test_silence_stays_not_connected(self, tmp_path):
        """**没等到信号就一直不确定** —— 宁可显示「正在连接」。

        这是「假装确证比承认不确定更糟」的落点：信号没来就翻成已连接，
        等于把原来那个 bug 原样搬回来。
        """
        from freeagent.feishu.bridge import _run_connection

        p = tmp_path / "bridge_status.json"
        reporter = StatusReporter(p, clock=lambda: 1.0)
        client = self._fake_client(p)            # 什么都不打
        _run_connection(client, reporter, "ou_bot")
        snap = read_status(p)
        assert snap["connected"] is False
        assert snap["state"] == "starting"

    def test_disconnect_does_not_flip_to_connected(self, tmp_path):
        """⚠️ **子串陷阱**：`disconnected to` 里**含** `connected to`。

        不先判断开，一次断线会被当成重新连上 —— 那正好是这个 bug 的翻版，
        而且只在真断线时才发作，测试极难碰上，所以单独钉住。
        """
        from freeagent.feishu.bridge import _run_connection

        p = tmp_path / "bridge_status.json"
        reporter = StatusReporter(p, clock=lambda: 1.0)
        client = self._fake_client(p, on_start=lambda: self._sdk_log(
            "disconnected to wss://example"))
        _run_connection(client, reporter, "ou_bot")
        snap = read_status(p)
        assert snap["connected"] is False, "断线被当成了连上"

    def test_disconnect_after_connect_drops_the_flag(self, tmp_path):
        """连上之后再断，界面要能如实反映。"""
        from freeagent.feishu.bridge import _run_connection

        p = tmp_path / "bridge_status.json"
        reporter = StatusReporter(p, clock=lambda: 1.0)

        def two_steps() -> None:
            self._sdk_log("connected to wss://example")
            self._sdk_log("disconnected to wss://example")

        client = self._fake_client(p, on_start=two_steps)
        _run_connection(client, reporter, "ou_bot")
        assert read_status(p)["connected"] is False

    def test_start_failure_is_reported_and_re_raised(self, tmp_path):
        """启动就失败时要写清原因，且**不吞异常**。

        留一个 ready 的假状态在盘上，正是原 bug 最恶劣的那种表现。
        """
        from freeagent.feishu.bridge import _run_connection

        p = tmp_path / "bridge_status.json"
        reporter = StatusReporter(p, clock=lambda: 1.0)
        client = self._fake_client(p, error=RuntimeError("凭据无效"))
        try:
            _run_connection(client, reporter, "ou_bot")
        except RuntimeError as exc:
            assert "凭据无效" in str(exc)
        else:
            raise AssertionError("start() 的异常被吞了")
        snap = read_status(p)
        assert snap["connected"] is False
        assert snap["state"] == "down"
        assert "凭据无效" in snap["last_error"]

    def test_ready_state_is_degraded_without_bot_identity(self, tmp_path):
        """没有 bot 身份时即使连上了也是 degraded —— 群里 @ 它不会回。"""
        from freeagent.feishu.bridge import _run_connection

        p = tmp_path / "bridge_status.json"
        reporter = StatusReporter(p, clock=lambda: 1.0)
        client = self._fake_client(p, on_start=lambda: self._sdk_log(
            "connected to wss://example"))
        _run_connection(client, reporter, None)
        snap = read_status(p)
        assert snap["state"] == "degraded"
        assert snap["connected"] is True

    def test_watcher_is_detached_after_start_returns(self, tmp_path):
        """handler 要摘掉，否则会一直挂在 SDK 的 logger 上。"""
        import logging

        from freeagent.feishu.bridge import _run_connection

        p = tmp_path / "bridge_status.json"
        reporter = StatusReporter(p, clock=lambda: 1.0)
        sdk_log = logging.getLogger("Lark")
        before = len(sdk_log.handlers)
        client = self._fake_client(p)
        _run_connection(client, reporter, "ou_bot")
        assert len(sdk_log.handlers) == before

    def test_no_reporter_means_no_status_but_still_starts(self, tmp_path):
        """reporter 为 None（不落盘状态）时照常连接，只是不写状态。"""
        from freeagent.feishu.bridge import _run_connection

        client = self._fake_client(tmp_path / "unused.json")
        _run_connection(client, None, "ou_bot")
        assert client.calls == 1
