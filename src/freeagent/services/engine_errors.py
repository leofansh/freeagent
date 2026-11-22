"""引擎错误的**结构化分类**：为什么失败，以及下一步做什么。

## 为什么要有这个模块

引擎（OpenCode）失败时给的是**结构化**的：``error.type``、``error.status``、
``error.message``、``error.response.body``。真机样本（2026-10-28）：

    {"type": "provider.quota", "status": 429,
     "message": "Free usage limit reached. ...",
     "response": {"body": "{\\"type\\":\\"error\\",...}"}}

而本仓此前只挖出 ``message`` 那一句人话（:func:`freeagent.delegate.
extract_error_message`），把 ``type`` 与 ``status`` 全丢了。结果是：

- 用户看到「Free usage limit reached. Free models work in OpenCode...」
  —— 一句**没有行动指向**的话（订阅？换模型？等？改需求？）
- ``/feedback`` 的「错因频次」按**整句文本**聚类，于是「充值」和
  「换个模型」两件不同的事长得一样，而「等额度恢复」根本不在表里。

而 ``type`` 恰恰是「下一步做什么」的**唯一依据**。

## 与 11.8 的分工（边界，别越界）

| 问题 | 由谁答 |
|---|---|
| **能不能重试**（这次失败是不是确定性的） | 11.8 的 ``attempt_state`` —— 从**记录形状**推，不给失败加标签 |
| **为什么失败 / 下一步做什么** | 本模块 —— 从**引擎给的类型**读 |

两者**不重复也不互相覆盖**：11.8 判「重试有没有意义」是**确定性**问题
（白名单拒绝重试多少次都一样）；本模块判「你该去做什么」是**行动**问题。
一个 429 配额错误在 11.8 眼里是「可重派」（形状上确实是执行期失败），
但重派它一百次也还是 429 —— 所以本模块必须说「等额度恢复」，而不是
让用户去点重派。

## 三条纪律

1. **不猜**。分类只依据引擎**实际给出的** ``type`` 与 ``status``；
   认不出就归 :data:`ENGINE_ERROR_UNKNOWN`，**原样保留**原话。
   宁可给不出建议，也不给错建议 —— 错建议会把人引到错的方向。

2. **建议必须可执行**。每类给一条「下一步做什么」，且**不说「请重试」**
   —— 重试不是出路，除非重试确实有意义（那是 11.8 管的）。

3. **已验证的与推断的分开**。表里每一类都标注来源：``verified`` = 真机
   实测见过；``inferred`` = 按 HTTP 语义或引擎文档推断。**未验证的不得
   当契约用**（本仓文档纪律）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "EngineErrorKind",
    "EngineError",
    "classify_engine_error",
    "engine_error_line",
    "error_fields",
]


#: 引擎错误的分类。
#:
#: **取值即契约**（设计文档 12.10.11）—— 加枚举要同步改文档与断言。
#:
#: 刻意保持**少而准**：只留「下一步动作真的不同」的那些。分得太细，
#: 就会有两类的出路其实是同一条，分类表就成了摆设。
EngineErrorKind = Literal[
    "quota",        # 额度/配额耗尽 —— 等恢复，或换免费模型
    "auth",         # 凭据无效/无权限 —— 改配置
    "model_gone",   # 模型不存在/已下线 —— 换模型
    "context",      # 上下文超限 —— 把任务改小
    "tool",         # 工具执行失败 —— 看工具日志
    "upstream",     # 上游/网络抖动 —— 通常可重派
    "unknown",      # 认不出 —— 原样摆事实，不给建议
]

#: 引擎给出的 ``error.type`` → 分类。
#:
#: **只有真机实测见过的才进这张表**（``provider.quota``，2026-10-28 实测）。
#: 引擎改版会新增 type，那时的正确处置是落到 ``unknown`` 并保留原话，
#: **不是**在这里猜一条。
_TYPE_MAP: dict[str, EngineErrorKind] = {
    "provider.quota": "quota",
}

#: HTTP 状态码 → 分类。**这是推断，不是实测**（``status`` 字段的语义
#: 按 HTTP 惯例理解）。``429`` 与 ``402`` 有真机样本佐证
#: （2026-10-28 的 ``provider.quota`` 是 429；本仓历史上「余额不足就
#: 402」实测过，见 ``delegate.py`` 的 402 注释）。
_STATUS_MAP: dict[int, EngineErrorKind] = {
    401: "auth",
    402: "quota",
    403: "auth",
    404: "model_gone",
    429: "quota",
    413: "context",
    # 5xx：**上游服务**自己的问题（502/503/504）。这是唯一「重派有意义」
    # 的一类 —— 所以必须单列，否则 503 会落进 unknown，用户拿到的建议是
    # 「看原始报错」，而实际只要再派一次。
    500: "upstream",
    502: "upstream",
    503: "upstream",
    504: "upstream",
}

#: 每类的下一步。**不说「重试」** —— 重试不是出路（见模块 docstring）。
_NEXT_STEP: dict[EngineErrorKind, str] = {
    "quota": (
        "额度或配额用尽了。\n"
        "  这不是代码的问题，重派多少次都是同一堵墙。\n"
        "  下一步：等额度恢复，或换一个还没用完的模型再派。"
    ),
    "auth": (
        "凭据无效或没有权限。\n"
        "  下一步：检查执行器的 API Key / 登录状态，改完再派。"
    ),
    "model_gone": (
        "这个模型不存在或已经下线。\n"
        "  下一步：换一个当前可用的模型再派"
        "（在「委派总览」页重选工作模式即可）。"
    ),
    "context": (
        "上下文超限了 —— 这次要装的东西比模型能吃的多。\n"
        "  下一步：把需求拆小一点再派，或换一个上下文更大的模型。"
    ),
    "tool": (
        "工具执行失败。\n"
        "  下一步：看这次委派的详情里的工具输出，"
        "确认是环境问题还是需求本身做不到。"
    ),
    "upstream": (
        "上游服务出错或网络抖动。\n"
        "  下一步：可以重派一条新的（这类失败通常是暂时的）。"
    ),
    "unknown": (
        "引擎没给出可识别的错误类型。\n"
        "  下一步：看详情里的原始报错；"
        "若反复出现，把这段原文报给引擎的维护方。"
    ),
}


#: 只有一句人话时的**短语**提示。刻意用短语而不是单词 —— 理由见
#: :func:`classify_engine_error` 里那段注释。
#:
#: 两条都是**本仓真机实测见过的原话**：
#: - ``Insufficient account funds``（本仓历史实测的 402 句式）
#: - ``usage limit reached``（2026-10-28 实测的 429 句式）
_PHRASE_HINTS: tuple[tuple[str, EngineErrorKind], ...] = (
    ("insufficient account funds", "quota"),
    ("usage limit reached", "quota"),
)


@dataclass(frozen=True, slots=True)
class EngineError:
    """一次引擎错误的**分类结果**。

    刻意同时保留**原话**（``message``）与**分类**（``kind``）：
    分类给行动方向，原话给人判断「引擎到底说了什么」。
    只留其一会丢信息 —— 只留原话就是现状（用户拿到一句没方向的话），
    只留分类就会丢掉真机证据。
    """

    #: 分类（见 :data:`EngineErrorKind`）。
    kind: EngineErrorKind
    #: 引擎给的错误类型（``error.type``），原样；没有则空串。
    type: str = ""
    #: HTTP 状态码（``error.status``）；没有或不是数字则 0。
    status: int = 0
    #: 引擎给的那句人话（``error.message``）；挖不到则空串。
    message: str = ""
    #: 下一步做什么（按分类给出的一句话/几行）。
    next_step: str = ""
    #: 分类依据：``type`` / ``status`` / 空（认不出）。
    basis: str = ""

    @property
    def is_known(self) -> bool:
        """是不是认得出的分类。认不出时**不给建议**（宁可不给，不给错）。"""
        return self.kind != "unknown"


def error_fields(payload: Any) -> tuple[str, int, str]:
    """从引擎错误对象里取出 ``(type, status, message)``。

    ## 挖的顺序与理由

    引擎的形状**随版本变**，真机见过两种：

    - V2（实测 2026-10-28）：``{"type": "provider.quota", "status": 429,
      "message": "...", "response": {...}}``
    - V1（实测，样本见 ``tests/test_delegate.py::_REAL_ERROR_EVENT``）：
      ``{"error": {"name": "APIError",
      "data": {"message": "...", "statusCode": 402,
      "isRetryable": false}}}``

    注意 V1 的状态码字段名是 **``statusCode``**，不是 ``status`` ——
    这一点本仓此前**完全没用过**（402/403 只出现在 fixture 里，从没被
    程序读过）。所以 ``status`` 要在两个名字里都试。

    ``message`` 要挖三层：``error.data.message`` → ``error.message``
    → 顶层 ``message``；``type`` 与 ``status`` 在顶层与 ``error`` 里都试。

    **挖不到就返回空/0，不猜。** 宁可落到 ``unknown`` 保留原话，
    也不要把「没找到」当成「没有」。
    """
    if not isinstance(payload, dict):
        return "", 0, ""

    type_ = ""
    status = 0
    message = ""

    # V2 形状：字段在顶层。
    type_ = _as_str(payload.get("type"))
    status = _as_int(payload.get("status"))
    message = _as_str(payload.get("message"))

    # V1 形状：包在 ``error`` 里。
    inner = payload.get("error")
    if isinstance(inner, dict):
        if not type_:
            type_ = _as_str(inner.get("type"))
        if not status:
            status = _as_int(inner.get("status"))
        if not message:
            message = _as_str(inner.get("message"))
        data = inner.get("data")
        if isinstance(data, dict):
            if not message:
                message = _as_str(data.get("message"))
            if not status:
                # V1 的字段名是 ``statusCode``（实测样本见上）。
                status = _as_int(data.get("statusCode"))

    # 再兜一层：``error.data.message`` 在 ``data`` 里而 ``message`` 在别处。
    if not message:
        data = payload.get("data")
        if isinstance(data, dict):
            message = _as_str(data.get("message"))
            if not status:
                status = _as_int(data.get("statusCode"))

    return type_, status, message


def classify_engine_error(payload: Any) -> EngineError:
    """把引擎错误对象分类。

    :param payload: ``session.error`` 事件的 ``properties``（原样 dict），
        或引擎消息里的 ``error`` 对象，或 ``None``。
    :returns: 分类结果。认不出时 ``kind == "unknown"`` 且 ``next_step``
        给的是「看原文」而不是编出来的建议。
    """
    type_, status, message = error_fields(payload)

    kind: EngineErrorKind = "unknown"
    basis = ""
    if type_:
        hit = _TYPE_MAP.get(type_)
        if hit is not None:
            kind = hit
            basis = "type"
    if kind == "unknown" and status:
        hit = _STATUS_MAP.get(status)
        if hit is not None:
            kind = hit
            basis = "status"
    if kind == "unknown" and not type_ and not status and message:
        # 只有一句人话：按消息里的**完整短语**判一次已知类别。
        #
        # 刻意**不按单词匹配**。原先写的是 ``"quota" in low``，而那句
        # 「我检查了 quota 配置，看起来没问题」也会命中 —— 把一句跟配额
        # 无关的话归成「额度耗尽」，用户就会去查额度，而真因在别处。
        # **宁可漏判成 unknown**：漏判的代价是「没给建议」，错判的代价是
        # 「把人引到错的方向」，后者大得多。
        low = message.lower()
        if _PHRASE_HINTS:
            for phrase, hinted in _PHRASE_HINTS:
                if phrase in low:
                    kind = hinted
                    basis = "message"
                    break

    next_step = _NEXT_STEP[kind] if kind != "unknown" else _NEXT_STEP["unknown"]
    return EngineError(
        kind=kind,
        type=type_,
        status=status,
        message=message,
        next_step=next_step,
        basis=basis,
    )


def engine_error_line(error: EngineError) -> str:
    """把分类结果压成**一行**（给记录 / 卡片用）。

    格式：``类别 · 原话``。类别用中文词表（给人看），原话原样保留
    （``type`` 有值时也带上，那是引擎自己的说法，排查时最有用的线索）。

    刻意**不含下一步** —— 那段话有多行，放进记录会破「一行」的约束；
    下一步走 :attr:`EngineError.next_step`，由展示层单独取。
    """
    if error.kind == "unknown":
        label = "未识别"
    else:
        label = {
            "quota": "额度耗尽",
            "auth": "凭据问题",
            "model_gone": "模型不可用",
            "context": "上下文超限",
            "tool": "工具失败",
            "upstream": "上游故障",
        }[error.kind]
    bits = [label]
    if error.type:
        bits.append(error.type)
    if error.status:
        bits.append(str(error.status))
    detail = error.message or "（引擎没给出错误说明）"
    return " · ".join(bits) + " · " + detail


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0
