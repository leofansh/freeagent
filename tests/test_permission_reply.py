"""把决策送回引擎的失败路径（V1.137 / #5 失败可恢复）。

早先委派执行器对 ``reply_permission`` 完全不设防，工作台那侧用
``contextlib.suppress(Exception)`` 吞掉 —— 卡面显示「已允许」，引擎却还挂着
请求。这一组守住「有限重试 + 送不到如实返回」这件事本身；两个调用点怎么
把失败摆给人看，由各自的测试守着。
"""

import pytest

from freeagent.services.permission_reply import deliver


class TestDeliver:
    def test_success_on_first_try_does_not_sleep(self):
        calls, sleeps = [], []
        result = deliver(lambda: calls.append(1), sleep=sleeps.append)
        assert result.ok
        assert result.error == ""
        assert len(calls) == 1
        assert sleeps == []

    def test_transient_failure_retries_then_succeeds(self):
        attempts, sleeps = [], []

        def send():
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("connection reset")

        result = deliver(send, sleep=sleeps.append)
        assert result.ok
        assert len(attempts) == 2
        assert sleeps == [0.4]

    def test_exhausted_returns_reason_not_silence(self):
        sleeps = []

        def send():
            raise RuntimeError("engine gone")

        result = deliver(send, sleep=sleeps.append)
        assert not result.ok
        assert "RuntimeError" in result.error
        assert "engine gone" in result.error
        assert sleeps == [0.4, 0.4]

    def test_single_attempt_never_sleeps(self):
        sleeps = []

        def send():
            raise ValueError("nope")

        result = deliver(send, attempts=1, sleep=sleeps.append)
        assert not result.ok
        assert sleeps == []

    def test_custom_delay_is_honored(self):
        sleeps = []

        def send():
            raise OSError("x")

        deliver(send, attempts=2, delay=1.5, sleep=sleeps.append)
        assert sleeps == [1.5]

    def test_zero_attempts_still_sends_once(self):
        # 防御下限：attempts=0 不该变成「一次都不送却报成功」。
        calls = []
        result = deliver(lambda: calls.append(1), attempts=0)
        assert result.ok
        assert len(calls) == 1

    def test_all_exception_shapes_are_caught(self):
        # 适配器的异常形状不统一（ServerError / OSError / 任意异常），
        # 这一层的职责是「一个都不许漏成静默」。
        for exc in (OSError("a"), ValueError("b"), RuntimeError("c")):
            result = deliver(lambda e=exc: (_ for _ in ()).throw(e),
                             attempts=1)
            assert not result.ok
            assert type(exc).__name__ in result.error
