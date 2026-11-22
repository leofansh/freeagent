"""``POST /api/agent/intake`` —— 一句话起手：认项目 + 要做的事，追问补缺。

薄端点：判断在 :mod:`freeagent.services.delegate_intake`，这里只取字段、
传 ``app.chat.capabilities`` 与 ``app.llm``（同一份已配文本模型），再把
:class:`IntakePlan` 原样投影成 JSON。会花模型一次请求，故只走 POST。
"""

from __future__ import annotations

from typing import Any

from ..domain import FreeAgentError
from ..services import delegate_intake

__all__ = ["agent_intake"]


def agent_intake(app: Any, body: dict[str, Any]) -> dict[str, Any]:
    text = str(body.get("text") or "").strip()
    if not text and not (body.get("carry") or {}).get("need"):
        raise FreeAgentError("先说一句要做什么")
    raw_carry = body.get("carry") or {}
    carry = raw_carry if isinstance(raw_carry, dict) else {}
    plan = delegate_intake.plan(
        text, getattr(app.chat, "capabilities", None), getattr(app, "llm", None),
        carry=carry,
    )
    return {
        "kind": "agent_intake",
        "project": plan.project,
        "directory": plan.directory,
        "brief": plan.brief,
        "need": plan.need,
        "question": plan.question,
        "recognized": plan.recognized,
        "reason": plan.reason,
        # 下一句回答要带回的上下文：只有 need/project/brief，不含 text（省字节）。
        "carry": {"need": plan.need, "project": plan.project, "brief": plan.brief},
    }
