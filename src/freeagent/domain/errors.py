"""领域错误体系。

所有异常都从 :class:`FreeAgentError` 派生，调用方可以只捕获这一个基类。
"""

from __future__ import annotations

__all__ = [
    "FreeAgentError",
    "NotFoundError",
    "ValidationError",
    "InvariantViolation",
    "ConflictError",
    "LLMError",
]


class FreeAgentError(Exception):
    """本项目所有领域异常的基类。"""


class NotFoundError(FreeAgentError):
    """请求的实体不存在。"""

    def __init__(self, entity: str, ident: str) -> None:
        self.entity = entity
        self.ident = ident
        super().__init__(f"{entity} 不存在: {ident}")

    def __str__(self) -> str:
        return f"{self.entity} 不存在: {self.ident}"


class ValidationError(FreeAgentError):
    """输入不合法：重名、合并链成环、参数越界等。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class InvariantViolation(FreeAgentError):
    """违反设计文档 4.3 节的不变式。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return f"不变式被破坏: {self.message}"


class ConflictError(FreeAgentError):
    """与当前数据状态冲突：删除仍被引用的角色、未完成重定向等。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return f"状态冲突: {self.message}"


class LLMError(FreeAgentError):
    """智能层（模型调用 / 输出校验）失败。

    **绝不携带 API Key 或完整请求体** —— 错误信息会进终端和日志。
    """

    def __init__(self, message: str, *, provider: str = "llm") -> None:
        self.message = message
        self.provider = provider
        super().__init__(message)

    def __str__(self) -> str:
        return f"[{self.provider}] {self.message}"
