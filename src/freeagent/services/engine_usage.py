"""引擎用量读取（V1.133）。

## 为什么单独一层

前台想让「用了多少」可见，但引擎给的形状**没有版本承诺**（表名、字段名都
变过 —— V1.110 踩过直接读 SQLite 的坑）。所以这一层只做一件事：拿官方
`GET /api/session/{sid}` 的 JSON，认得出的字段才报数，认不出就如实说
「读不到 + 原因」。

## 三条纪律

1. **不猜、不报 0**：读不出来时 `state="unreadable"`，`input/output/total`
   全为 0 只是占位，调用方必须先看 `state` 才能显示；把「读不到」显示成
   「用了 0」等于说谎。
2. **不把原文整段带进 UI**：失败原因只留状态码与片段（≤120 字），
   引擎可能把会话正文塞在错误体里。
3. **不抛异常**：这是呈现层的附加信息，读不到不该让引擎状态页整体失败。
   因此 `call` 抛异常也归 unreadable（fail-closed + 说得清）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

__all__ = ["EngineUsage", "read_session_usage"]


@dataclass(frozen=True, slots=True)
class EngineUsage:
    """一次会话的 token 用量。**先看 `state`，再看数字**。"""

    state: str = "unreadable"
    input: int = 0
    output: int = 0
    total: int = 0
    reason: str = ""

    @property
    def readable(self) -> bool:
        return self.state == "ok"


def _unreadable(reason: str) -> EngineUsage:
    return EngineUsage(state="unreadable", reason=reason[:160])


def _count(value: Any) -> int | None:
    """只认非负整数（bool 是 int 的子类，必须排除）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def _first(mapping: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in mapping:
            n = _count(mapping[key])
            if n is not None:
                return n
    return None


def read_session_usage(
    session_id: str,
    call: Callable[..., Any],
    *,
    timeout: float = 5.0,
) -> EngineUsage:
    """读一次会话用量。`call` 的形状与 ``opencode_server.call`` 一致：
    ``(path, method, body, timeout) -> (code, raw_bytes)``。
    """
    if not session_id:
        return _unreadable("没有会话 id，读不到用量")
    path = "/api/session/" + quote(session_id, safe="")
    try:
        code, raw = call(path, "GET", None, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 附加信息不能让主视图失败
        return _unreadable(f"读用量时连不上引擎：{exc}")
    if code != 200:
        brief = raw.decode("utf-8", "replace")[:120] if isinstance(raw, bytes) else str(raw)[:120]
        return _unreadable(f"引擎返回 {code}：{brief}")
    try:
        data = json.loads(raw) if isinstance(raw, bytes) else json.loads(str(raw))
    except (ValueError, TypeError):
        return _unreadable("引擎回的不是 JSON，读不出用量")
    if not isinstance(data, dict):
        return _unreadable("引擎回的 JSON 不是对象，读不出用量")
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    tokens = payload.get("tokens")
    if isinstance(tokens, dict):
        inp = _first(tokens, ("input", "inputTokens", "prompt", "promptTokens"))
        out = _first(tokens, ("output", "outputTokens", "completion", "completionTokens"))
        tot = _first(tokens, ("total", "totalTokens"))
        if inp is None and out is None and tot is None:
            return _unreadable("tokens 里的字段认不出（键：" +
                               ", ".join(sorted(str(k) for k in tokens)) + "）")
        return EngineUsage(
            state="ok", input=inp or 0, output=out or 0,
            total=tot if tot is not None else (inp or 0) + (out or 0),
        )
    n = _count(tokens)
    if n is not None:
        return EngineUsage(state="ok", total=n)
    return _unreadable("tokens 的形状认不出，不猜")
