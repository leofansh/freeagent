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
    "StaleRevisionError",
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
    """与当前数据状态冲突：删除仍被引用的角色、未完成重定向等。

    也用于**乐观并发**：读到的 ``revision`` 与写入时库里的不一致，
    说明中间有人改过 —— 这时必须拒绝而不是覆盖。
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return f"状态冲突: {self.message}"


class StaleRevisionError(ConflictError):
    """乐观并发失败：你手上那份已经不是最新的了。

    刻意是 :class:`ConflictError` 的子类 —— 上层捕获「状态冲突」就能
    统一处理这类「数据被别人动过」的情况，不必知道具体是哪一种。

    为什么值得单列：``updated_at`` 是**时间戳**，同一秒内的两次写入
    分不出先后，于是「我读到的还是最新的吗」这个问题用时间戳回答不了。
    ``revision`` 是单调计数，比时间戳多一分保证，少一分含糊。
    """

    def __init__(self, entity: str, ident: str, expected: int, actual: int) -> None:
        self.entity = entity
        self.ident = ident
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{entity} {ident} 已被改动（你读到的是第 {expected} 版，"
            f"库里现在是第 {actual} 版），请重新读取后再试"
        )


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
