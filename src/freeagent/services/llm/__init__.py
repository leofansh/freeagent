"""智能层：``LLMProvider`` 协议 + 两种实现。

* :class:`RuleBasedProvider` —— 纯规则、离线、确定性，是降级兜底。
* :class:`DeepSeekProvider` —— 真实模型。**名字里的 DeepSeek 是历史遗留**：
  它其实是标准 OpenAI 兼容客户端（请求体 ``messages``、鉴权 ``Bearer``、
  响应 ``choices[0].message.content``），所以同一个类服务
  :mod:`.providers` 注册表里的全部服务商。改名是纯重构，不影响行为。

Key 的来源是「环境变量优先、``llm.env`` 兜底」，见 :mod:`freeagent.llm_env`。
"""

from __future__ import annotations

from .provider import (
    CANNOT_MUTATE_HINT,
    KIND_ACTION,
    KIND_REMINDER,
    KIND_WAIT,
    NOT_A_TASK_HINT,
    ClassificationResult,
    InputIntent,
    LLMProvider,
    RoleGuess,
    RoleHint,
    SignalRef,
    TaskRef,
    is_question_like,
    read_intent,
)
from .rules import ROLE_KEEP_MIN, ROLE_MATCH_THRESHOLD, RuleBasedProvider
from .providers import PROVIDERS, ProviderProfile, get_profile

#: 不用鉴权的本机服务（Ollama）没有 Key，但构造器会拒空串。
#: 这个占位值只会进一个被服务端忽略的 ``Authorization`` 头。
_NO_AUTH_PLACEHOLDER = "no-auth-required"

__all__ = [
    "CANNOT_MUTATE_HINT",
    "ClassificationResult",
    "DeepSeekConfig",
    "DeepSeekProvider",
    "InputIntent",
    "KIND_ACTION",
    "KIND_REMINDER",
    "KIND_WAIT",
    "LLMProvider",
    "NOT_A_TASK_HINT",
    "PROVIDERS",
    "ProviderProfile",
    "ROLE_KEEP_MIN",
    "ROLE_MATCH_THRESHOLD",
    "RoleGuess",
    "RoleHint",
    "RuleBasedProvider",
    "SignalRef",
    "TaskRef",
    "build_provider",
    "default_provider",
    "get_profile",
    "is_question_like",
    "read_intent",
]


def default_provider() -> LLMProvider:
    """默认实现。V1 不需要任何 API key。"""
    return RuleBasedProvider()


def build_provider(config) -> LLMProvider:
    """按配置挑选智能层实现。

    有可用凭据就用真实模型，并挂上规则层做兜底；否则直接用规则层。
    拿不到 key 时**不报错** —— 没配模型的终端也必须能用。

    这里是「支持多家 LLM」真正落地的地方，而它原本只有一句
    ``if not config.has_credentials``。改动之所以小，是因为客户端早就是
    OpenAI 兼容形状（见 :mod:`.deepseek`）——**协议层一行没动**。
    """
    from .deepseek import DeepSeekConfig, DeepSeekProvider
    from .providers import get_profile

    rules = RuleBasedProvider()
    if not config.has_credentials:
        return rules

    profile = get_profile(getattr(config, "provider", ""))
    if profile is None:
        # 理论上到不了：provider 在 :func:`config.resolve_provider` 就校验过。
        # 这里退成规则层而不是抛异常，是因为**智能层永远不该让程序起不来** ——
        # 退化的表现是「功能少一点」，抛异常的表现是「整个打不开」。
        return rules

    key = config.api_key
    # 不用鉴权的本机服务（Ollama）没有 Key，但 ``DeepSeekProvider`` 构造时会
    # 拒空串 —— 那个检查在「该配却没配」的路径上是有用的，不能为一个例外拆掉。
    # 所以传一个占位值：真实环境里没有鉴权，它只会被拼进一个被忽略的请求头。
    key_text = key.use() if key else _NO_AUTH_PLACEHOLDER

    provider = DeepSeekProvider(
        DeepSeekConfig(
            model=config.model,
            base_url=config.base_url or profile.default_base_url,
            timeout=config.timeout,
            name=profile.label,
        ),
        key_text,
        fallback=rules if config.allow_fallback else None,
    )
    return provider
