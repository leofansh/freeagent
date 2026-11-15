"""执行器自报的结局 —— 与本仓那五条判据对账（12.10.14）。

## 为什么要有这个模块

本仓判「这次成不成」靠五条**排除法**：没有会话错误、没有未答提问、不是
用户叫停、没超步数、没超时长。执行器自己也有一份判定（``session.outcome``，
落库列名 ``idle_outcome``）。两者**说法不同源**，所以偶尔会不一致。

不一致里只有一个方向值得开口：**我们记成功、它说 failed**。那意味着某条
错误从我们的事件流里漏过去了，而人正准备按「完成」去验收。

## 只在会错判的那一侧开口

``outcome`` 为空时**什么都不说**，这不是偷懒 —— 空有四种来源，在收尾那一刻
分不开：

1. 我们自己叫停 / 超步数 / 超时长时 abort 了，会话没跑到 idle；
2. 事件流提前结束（进程退了），会话没跑到 idle；
3. **读早了一步**：实测 ``outcome`` 在 ``time_updated`` 之后约 27ms 才写，
   与 ``time_idle`` 同刻；
4. 还在跑（理论上收尾时不该有，但读的是别人的服务）。

把「空」报成「没有佐证」会在 1/2/3 三种情况下**每次都多一句话**，而人只会
记住「卡上又多了个看不懂的行」。所以这里选**宁可漏判，不要错判**（同
11.9.12）：只在执行器明确说 failed 时说，那一句话不会错。

反向（我们判失败、它说 succeeded）同样不开口：失败文案已经给出原因，
再加一句「执行器说它成了」只会让人怀疑整套判定，而**没有可执行的下一步**。
实测这条几乎不可能出现 —— 我们判失败的几条路（叫停 / 超限 / 未答）都以
abort 收场，会话根本不会写 ``succeeded``。

## 不当判据，只做标注

``outcome`` 不进 ``ok``、不进 ``dispatch_blocker``、不进任何写入口。理由与
12.10.13 相同：两个「完成」并列，人不知道该信哪个；而本仓的五条能给出
「卡在哪一步」，执行器的 ``failed`` 只给一个词。
"""

from __future__ import annotations

import dataclasses
import json
import urllib.parse
from typing import Any

from .engine_ledger import LedgerSource

__all__ = [
    "EngineVerdict",
    "read_engine_verdict",
    "disagreement_line",
]

#: 执行器明确说失败的那个值。**只认这一个** —— 别的值（含空）都不开口。
_FAILED = "failed"


@dataclasses.dataclass(frozen=True, slots=True)
class EngineVerdict:
    """执行器自报的结局。**纯数据**，``outcome`` 原样保留（不翻译）。"""

    #: ``"succeeded"`` / ``"failed"`` / ``""``（没落定或读不到）。
    outcome: str = ""
    #: ``outcome`` 为空时：为什么空。非空时为空串。
    reason: str = ""


def _fail(code: int, raw: bytes) -> EngineVerdict:
    detail = raw.decode("utf-8", "replace").strip()[:200]
    if code < 0:
        return EngineVerdict(reason=f"连不上执行器服务（{detail}）")
    if code == 404:
        return EngineVerdict(reason="执行器里找不到这个会话")
    if code == 401:
        return EngineVerdict(reason="执行器要求鉴权（没带上 Authorization 头）")
    return EngineVerdict(reason=f"执行器返回 HTTP {code}（{detail}）")


def read_engine_verdict(
    session_id: str,
    *,
    server: LedgerSource | None = None,
) -> EngineVerdict:
    """问一次执行器「这次会话你自己怎么判的」。**只读。**

    读不到一律返回空 ``outcome`` 并带上原因 —— 调用方据此**不说话**，
    而不是把读不到当成「它说成功了」。
    """
    if not session_id or not session_id.strip():
        return EngineVerdict(reason="没有会话号")
    if server is None:
        return EngineVerdict(reason="没有可用的执行器服务")

    path = "/api/session/" + urllib.parse.quote(session_id.strip(), safe="")
    code, raw = server.call(path)
    if code != 200:
        return _fail(code, raw)
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return EngineVerdict(reason="执行器的响应不是合法 JSON（版本不匹配？）")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return EngineVerdict(reason="执行器的响应里没有 data 对象（版本不匹配？）")
    outcome = data.get("outcome")
    if outcome is None:
        # 键不存在 = 会话没跑到 idle（实测：空值与「没有 idle 消息」一一对应）。
        return EngineVerdict(reason="执行器还没落定（会话没有正常收尾）")
    if not isinstance(outcome, str):
        return EngineVerdict(reason=f"outcome 的形状不认识（{type(outcome).__name__}）")
    return EngineVerdict(outcome=outcome)


def disagreement_line(verdict: EngineVerdict, *, ours_ok: bool) -> str:
    """两边不一致时返回一句话，否则空串。

    **只在「我们记成功、它说 failed」时开口**，理由见模块文档。
    """
    if not ours_ok or verdict.outcome != _FAILED:
        return ""
    return (
        "执行器自报 **failed**，本仓按五条判据记的是完成 —— 两边不一致，"
        "先别按完成验收（会话里可能有一条错误没进我们的事件流）。"
    )
