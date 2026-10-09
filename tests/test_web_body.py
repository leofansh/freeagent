"""请求体读取（:mod:`freeagent.web.body`）的守卫。

重点是 :func:`_drain`。那段代码修的是一个真实缺陷：超限时如果不把已声明的
请求体读掉就直接关连接，Windows 会发 RST，客户端**连已经发出的 400 都收不到**，
而且被中止的套接字留在 TIME_WAIT，会连累之后复用同一四元组的无关连接。

那两条后果都不是「测试不稳」—— 第一条是产品行为，第二条会随机弄挂不相干的
测试。所以这里钉住它：用假 rfile 直接断言「到底读了多少字节」。
"""

from __future__ import annotations

import io
import json

import pytest

from freeagent.web.body import (
    MAX_BODY,
    MAX_IMAGE_BODY,
    _DRAIN_LIMIT,
    discard_body,
    read_json,
)


class FakeRfile:
    """记录被读走多少字节的假流。

    用假流而不是真 socket：这里要断言的是「服务端**打算**读多少」，
    真 socket 只能间接观察到「客户端有没有收到 400」——那会把这条测试
    变成上一轮那种随机失败。
    """

    def __init__(self, data: bytes = b"") -> None:
        self.stream = io.BytesIO(data)
        self.read_bytes = 0

    def read(self, size: int) -> bytes:
        chunk = self.stream.read(size)
        self.read_bytes += len(chunk)
        return chunk


def headers(length: int) -> dict[str, str]:
    return {"Content-Length": str(length)}


def body_of(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


# --- 正常读取 ------------------------------------------------------------ #


def test_reads_a_plain_object():
    raw = body_of({"a": 1})
    assert read_json(FakeRfile(raw), headers(len(raw))) == {"a": 1}


def test_nested_and_unicode_survive():
    raw = body_of({"名字": "周报", "n": [1, 2, {"k": "值"}]})
    rfile = FakeRfile(raw)
    assert read_json(rfile, headers(len(raw)))["名字"] == "周报"


def test_empty_body_rejected_by_default():
    with pytest.raises(ValueError, match="空的"):
        read_json(FakeRfile(b""), headers(0))


def test_empty_body_allowed_when_asked():
    assert read_json(FakeRfile(b""), headers(0), allow_empty=True) == {}


def test_non_dict_json_rejected():
    raw = b"[1,2,3]"
    with pytest.raises(ValueError, match="必须是对象"):
        read_json(FakeRfile(raw), headers(len(raw)))


def test_malformed_json_rejected():
    raw = b"{oops"
    with pytest.raises(ValueError, match="合法 JSON"):
        read_json(FakeRfile(raw), headers(len(raw)))


def test_undecodable_bytes_rejected():
    raw = b"\xff\xfe\x00"
    with pytest.raises(ValueError, match="合法 JSON"):
        read_json(FakeRfile(raw), headers(len(raw)))


def test_bad_content_length_rejected():
    with pytest.raises(ValueError, match="Content-Length"):
        read_json(FakeRfile(b""), {"Content-Length": "abc"})


# --- 体积上限 ------------------------------------------------------------ #


def test_default_cap_is_the_small_one():
    """普通端点的上限不能被图片接口带上去。"""
    assert MAX_BODY < MAX_IMAGE_BODY
    raw = body_of({"x": "y" * (MAX_BODY + 10)})
    with pytest.raises(ValueError, match="太大"):
        read_json(FakeRfile(raw), headers(len(raw)))


def test_explicit_limit_overrides_default():
    """路由声明的大上限对拍照识物生效。"""
    raw = body_of({"x": "y" * (MAX_BODY + 10)})
    assert read_json(FakeRfile(raw), headers(len(raw)), limit=MAX_IMAGE_BODY)


# --- drain：这一段是本次修复的核心 ---------------------------------------- #


def test_oversized_body_is_drained_before_refusing():
    """超限时**必须把已声明的请求体读掉**。

    不读就关连接 → Windows 发 RST → 客户端收不到那个 400。
    """
    declared = 300 * 1024
    rfile = FakeRfile(b"x" * declared)
    with pytest.raises(ValueError, match="太大"):
        read_json(rfile, headers(declared))
    assert rfile.read_bytes == declared, "超限后没读干净，连接会以 RST 收尾"


def test_drain_is_bounded():
    """声明 10 GB 不能真读 10 GB —— 那本身就是 DoS 面。"""
    huge = 10 * 1024 * 1024 * 1024
    rfile = FakeRfile(b"x" * _DRAIN_LIMIT)      # 客户端只发了这么多
    with pytest.raises(ValueError, match="太大"):
        read_json(rfile, headers(huge))
    assert rfile.read_bytes == _DRAIN_LIMIT
    assert rfile.read_bytes < huge


def test_drain_stops_when_client_sent_less_than_declared():
    """客户端谎报长度（或中途断开）时不能死等。"""
    rfile = FakeRfile(b"x" * 10)
    with pytest.raises(ValueError, match="太大"):
        read_json(rfile, headers(999_999))
    assert rfile.read_bytes == 10, "对端已经关了还在读就是挂死"


def test_body_within_cap_is_not_double_read():
    """正常路径不该被 drain 逻辑碰。"""
    raw = body_of({"a": 1})
    rfile = FakeRfile(raw)
    assert read_json(rfile, headers(len(raw))) == {"a": 1}
    assert rfile.read_bytes == len(raw)


def test_malformed_json_path_needs_no_drain():
    """JSON 非法时请求体已经整个读走了，不必再丢一次。"""
    raw = b"{oops"
    rfile = FakeRfile(raw)
    with pytest.raises(ValueError, match="合法 JSON"):
        read_json(rfile, headers(len(raw)))
    assert rfile.read_bytes == len(raw)


# --- discard_body：拒绝路径的那道闸 ---------------------------------------- #
#
# 为什么单独一组：``_drain`` 只在「体积超限」时被调用，而 **401 / 404 两条
# 拒绝路压根不经过 read_json** —— 它们直接回响应，请求体一个字节都没读。
#
# 实测（2026-10-08）：``tests/test_web.py::test_wrong_post_path_404`` 偶发
# ``WinError 10053``，凶手就在这里。它之所以**看起来**像「测试不稳」，是因为
# :func:`_drain` 文档里写的第2 层后果 —— 被中止的套接字会**连累之后无关的
# 连接**，于是受害者每次都不一样。


def test_declared_body_is_drained():
    """拒绝前必须把已声明的请求体**读干净**。

    不读就关连接 → Windows 发 RST → 客户端**连已经发出的 404 都读不到**。
    """
    declared = 4096
    rfile = FakeRfile(b"x" * declared)
    discard_body(rfile, headers(declared))
    assert rfile.read_bytes == declared, "拒绝前没读干净，连接会以 RST 收尾"


def test_no_content_length_reads_nothing():
    """没有 Content-Length（GET 落到这条路上）就不该去读。

    刻意不是「读一点试试」—— 那会阻塞在一个没有长度的流上。
    """
    rfile = FakeRfile(b"")
    discard_body(rfile, {})
    assert rfile.read_bytes == 0


def test_zero_length_reads_nothing():
    rfile = FakeRfile(b"")
    discard_body(rfile, headers(0))
    assert rfile.read_bytes == 0


def test_undecodable_length_is_ignored_not_raised():
    """Content-Length 不可解析时**不抛**。

    这条路的调用方正在「准备拒绝这个请求」，而拒绝一个请求时抛异常
    会把 404 变成 500 —— 拒绝失败比拒绝本身更糟。
    """
    rfile = FakeRfile(b"abc")
    discard_body(rfile, {"Content-Length": "abc"})   # 不抛即通过
    assert rfile.read_bytes == 0


def test_discard_is_bounded_by_the_drain_limit():
    """声明 10 GB 不能真读 10 GB —— 那本身就是 DoS 面。"""
    huge = 10 * 1024 * 1024 * 1024
    rfile = FakeRfile(b"x" * _DRAIN_LIMIT)
    discard_body(rfile, headers(huge))
    assert rfile.read_bytes == _DRAIN_LIMIT
    assert rfile.read_bytes < huge


def test_discard_stops_when_peer_sent_less_than_declared():
    """对端谎报长度（或中途断开）时不能死等。"""
    rfile = FakeRfile(b"x" * 10)
    discard_body(rfile, headers(999_999))
    assert rfile.read_bytes == 10, "对端已经关了还在读就是挂死"
