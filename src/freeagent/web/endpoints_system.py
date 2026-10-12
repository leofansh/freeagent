"""系统级写操作：**恢复出厂**（清空全部事务数据）。

为什么单独一个模块：这是整个 web 层唯一**真正删数据**的端点，和
事务写操作（软删 / 状态迁移）放在一起会稀释它的特殊性 —— 扫一眼
``endpoints_write`` 找不到它，正是想要的效果。

口令在这里校验，服务层只认 ``confirmed`` 布尔。为什么不让服务层
自己比口令：口令是**入口的事**（各有各的输入方式），服务层管执行
（检测 / 备份 / 抹）。两件事分开，任何一侧的改动不会悄悄改变另一侧
的语义。
"""

from __future__ import annotations

from ..app import App
from ..domain import FreeAgentError
from ..services.reset import (
    RESET_PHRASE,
    ResetRefused,
    db_path_of,
    reset_factory,
)

__all__ = ["reset_data"]


def reset_data(app: App, payload: dict) -> dict:
    """``POST /api/system/reset-data``。口令不对 / 服务在跑 ⇒ 400。"""
    phrase = str(payload.get("phrase") or "")
    if phrase != RESET_PHRASE:
        # 口令不对时把口令**是什么**告诉用户 —— 这正是界面该做的
        # （用户看得到提示），而拒绝本身不附带任何执行。
        raise FreeAgentError(f"口令不对。要执行请逐字输入：{RESET_PHRASE}")
    try:
        result = reset_factory(db_path_of(app.conn), confirmed=True)
    except ResetRefused as exc:
        # 服务层拒绝（服务在跑 / 备份失败 / 平台检测不到）原样上抛：
        # 那些消息是写给用户的，带「为什么」和「下一步做什么」。
        raise FreeAgentError(str(exc)) from exc
    return {
        "kind": "reset",
        "db": str(result.db),
        "backup": str(result.backup),
        "deleted": {k: v for k, v in result.deleted.items() if v},
        "restore_hint": result.restore_hint,
    }
