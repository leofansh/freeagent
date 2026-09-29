"""读请求体：体积上限、JSON 解析，以及**关连接前必须丢弃未读的数据**。

单独一个文件，因为「请求体怎么处理」是一类关注点：上限是多少、谁来读、
读不完时怎么收尾。塞进 ``server.py`` 会让它一路涨（250 行上限守着）。

体积上限放在这里而不是路由表里，是因为它们是**读**的约束：路由只声明
「这个端点要哪个上限」（见 :mod:`routes_write`），具体怎么读、读到哪算
超，是这个模块的事。

:func:`_drain` 是本文件最要紧的一段，它解释的是一个真实缺陷 ——
细节见那里的说明。
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["read_json", "MAX_BODY", "MAX_IMAGE_BODY"]

#: 普通 JSON 请求的上限。刻意小 —— 这些接口收的都是文字。
MAX_BODY = 64 * 1024

#: 拍照识物专用上限：base64 会把字节放大约 4/3，再加 JSON 里的 data URL
#: 前缀。8 MB 的原图编码后约 11 MB。
#:
#: 刻意**不放宽全局** —— 否则任何接口都能传 11 MB，那是白白扩大攻击面。
MAX_IMAGE_BODY = 12 * 1024 * 1024

#: 丢弃时一次读多少。
_DRAIN_CHUNK = 64 * 1024

#: 最多丢弃这么多。读是有代价的，不能让人声明 10 GB 就真读 10 GB ——
#: 那本身就是个 DoS 面。超了这个数就放弃，RST 照旧（那种请求本来就该被拒）。
_DRAIN_LIMIT = 32 * 1024 * 1024


def _drain(rfile, length: int) -> None:
    """把**已声明但不要**的请求体读掉。

    不读掉就直接关连接，Windows 会发 RST 而不是 FIN —— 接收缓冲区里还有
    没读走的数据时，``close()`` 等于 abort。后果分两层：

    1. 客户端**连已经发出的 400 都读不到**，只能看到 ``ConnectionAbortedError``。
    2. 更麻烦的是它会**连累之后无关的连接**：被中止的套接字留在 TIME_WAIT，
       下一个复用同一四元组的回环连接会被 Windows 直接扔掉，且**不重试**。
       于是受害者每次都不一样，表现为「随机」失败。

    实测中这一条把不相干的场景测试搞挂过两次，凶手一直不在现场。

    另一个分支（JSON 非法）不需要这里：那种情况下请求体已经被完整读走了。
    """
    remaining = min(length, _DRAIN_LIMIT)
    while remaining > 0:
        chunk = rfile.read(min(_DRAIN_CHUNK, remaining))
        if not chunk:
            return          # 客户端没发够就关了，没什么可丢的
        remaining -= len(chunk)


def read_json(
    rfile,
    headers,
    *,
    allow_empty: bool = False,
    limit: int = 0,
) -> dict[str, Any]:
    """读并解析一个 JSON 对象请求体。

    ``limit`` 为 0 表示用默认的 :data:`MAX_BODY`。超限时**先丢弃再报错**
    （见 :func:`_drain`），否则那个 400 响应客户端根本收不到。
    """
    cap = limit or MAX_BODY
    try:
        length = int(headers.get("Content-Length") or 0)
    except ValueError as exc:
        # Content-Length 不可解析就无从得知该丢多少，只能直接关。
        raise ValueError("Content-Length 不合法") from exc
    if length <= 0:
        if allow_empty:
            return {}
        raise ValueError("请求体是空的")
    if length > cap:
        _drain(rfile, length)
        raise ValueError(f"请求体太大（上限 {cap // (1024 * 1024)} MB）")
    raw = rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("请求体不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("请求体必须是对象")
    return data
