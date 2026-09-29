"""连通性测试与列模型 —— 「这家到底能不能用」。

和 :mod:`llm_settings` 分开是因为这是两件不同的事，**失败方式也不同**：
那边是「配错了」，这边是「探不通」。混在一起时，两边的错误文案会互相污染，
最后变成一句谁也看不懂的「请求失败」。

两条纪律：

1. **失败要能指导下一步。** 401、连不上、超时、格式不对、余额不足 ——
   用户要靠这个区分「改 Key」「改地址」「充值」，因为这三件事的操作位置
   完全不同。所以 :func:`discover_models` 里的 :class:`DiscoveryError` 一定
   带上原因，而不是丢一个状态码出来。

2. **失败**返回**而不抛。** 一次诊断的结果（包括失败）就是要显示给用户的
   东西。抛异常在这个场景下等于把最需要的信息扔掉。

端点走 POST 而不是 GET：它们会**花掉一次真实请求**（计费），不该存在
「顺手 GET 一下就触发」的路径上。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ..domain import FreeAgentError, LLMError
from ..services.llm.providers import DiscoveryError, discover_models

__all__ = [
    "list_models",
    "llm_models",
    "llm_test",
    "test_connection",
]

#: 连通性测试用的探测消息。**必须极短且只要求 1 个 token** ——
#: 这是一次真实请求，会计费；用户点它是为了确认「能通」，
#: 不是为了看模型聊得多好。
_PROBE_MESSAGE = "ping"
_PROBE_MAX_TOKENS = 1

#: 不用鉴权的本机服务（Ollama）没有 Key。构造器拒空串，所以给个占位值：
#: 它只会被拼进一个被服务端忽略的 ``Authorization`` 头。
_NO_AUTH_PLACEHOLDER = "no-auth-required"


def _resolve_probe_key(config, overrides: Mapping[str, Any]) -> str:
    """测连通性时该用哪个 Key。

    用户刚在框里敲了但**还没保存**的 Key 优先：否则「填 Key → 点测试 →
    保存」这条路会去测旧的那个 Key，然后报一个和真实原因无关的 401 ——
    而这个 401 会让用户以为自己刚填的 Key 是错的。
    """
    typed = str(overrides.get("api_key") or "").strip()
    if typed:
        return typed
    key = config.api_key
    if key is not None:
        return key.use()
    profile = config.provider_profile
    if profile is not None and not profile.needs_key:
        return _NO_AUTH_PLACEHOLDER
    raise FreeAgentError(
        f"还没配 {config.key_env_var}，没法测。"
        "先在上面填一下，或设同名环境变量。"
    )


def _probe_key(config, overrides: Mapping[str, Any]) -> str | None:
    """列模型用的 Key。**没有也不报错** —— 有的本地服务压根不要鉴权，
    而 ``/models`` 对没 Key 的反代也可能放行。返回 ``None`` 表示「不带这个头」。"""
    typed = str(overrides.get("api_key") or "").strip()
    if typed:
        return typed
    key = config.api_key
    return key.use() if key is not None else None


def test_connection(config, overrides: Mapping[str, Any]) -> dict[str, Any]:
    """真发一次请求验证「能不能用」。

    刻意发**真实请求**而不是只打 ``/models``：``/models`` 通不代表
    ``chat/completions`` 通（有些反代只实现前者），而用户真正关心的是后者。
    代价是要计费，所以探测消息压到 1 个 token。
    """
    from ..services.llm.deepseek import urllib_transport

    model = str(overrides.get("model") or config.model).strip()
    base_url = str(overrides.get("base_url") or config.base_url).strip().rstrip("/")
    if not base_url:
        raise FreeAgentError("没有接口地址可测")
    if not model:
        raise FreeAgentError("没有模型名可测")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _PROBE_MESSAGE}],
        "max_tokens": _PROBE_MAX_TOKENS,
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {_resolve_probe_key(config, overrides)}"}
    endpoint = f"{base_url}/chat/completions"

    try:
        raw = urllib_transport(endpoint, payload, headers, float(config.timeout))
    except LLMError as exc:
        return {"kind": "llm_test", "ok": False, "detail": str(exc)}

    # ``max_tokens=1`` 常被截断成空串，所以**不能**要求探测出内容 ——
    # 「通了」由 HTTP 成功和响应结构决定，不由「有没有字」决定。
    detail = "能通，接口有响应。"
    if isinstance(raw, (str, bytes)):
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        if isinstance(body, dict) and not isinstance(body.get("choices"), list):
            return {
                "kind": "llm_test",
                "ok": False,
                "detail": "地址能连上，但返回的不是 OpenAI 兼容格式"
                          "（缺 choices）。这个地址可能不是 OpenAI 兼容服务。",
            }
    return {"kind": "llm_test", "ok": True, "detail": detail}


def list_models(config, overrides: Mapping[str, Any]) -> dict[str, Any]:
    """列模型（``GET /models``），供下拉用。失败如实带回原因。"""
    base_url = str(overrides.get("base_url") or config.base_url).strip().rstrip("/")
    if not base_url:
        raise FreeAgentError("没有接口地址可列模型")
    try:
        models = discover_models(
            base_url, api_key=_probe_key(config, overrides)
        )
    except DiscoveryError as exc:
        return {"kind": "llm_models", "ok": False, "detail": str(exc), "models": []}
    return {
        "kind": "llm_models",
        "ok": True,
        "detail": f"找到 {len(models)} 个模型。",
        "models": list(models),
    }


# -- POST 端点 --------------------------------------------------------------
# 放这儿而不是 endpoints_read：这两个**收请求体**、是用户触发的动作，
# 不是「读状态」。endpoints_read 只放只读端点（见 routes 的模块说明）。


def _config_of(app):
    from ..config import load_config

    return getattr(app, "config", None) or load_config()


def llm_test(app, body: dict[str, Any]) -> dict[str, Any]:
    """POST ``/api/llm/test``。失败**返回**而不是抛 ——
    失败原因就是要显示给用户的东西。"""
    return test_connection(_config_of(app), body or {})


def llm_models(app, body: dict[str, Any]) -> dict[str, Any]:
    """POST ``/api/llm/models``。失败如实带回原因。"""
    return list_models(_config_of(app), body or {})
