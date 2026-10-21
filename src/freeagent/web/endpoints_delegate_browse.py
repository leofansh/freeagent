"""可授权根的**只读**目录浏览（GET）。设计文档 12.7.1.1 约束 1–4。

这个端点只回答「范围内有哪些目录」；把某个目录写进白名单是
``POST /api/delegate/projects`` 的 ``allow_pick``（约束 5）。查与授权
分开的理由同 :mod:`.endpoints_delegate_projects` 的模块 docstring：
查零风险，授权不是 —— 长得一样就分不清了。

路径**不由客户端给绝对值**：只传「第几个根 + 根内相对路径」，拼接与
越根校验都在 :mod:`..services.delegate_browse` 里（服务端拼，收到
绝对路径当非法）。注册表里没登记的 handler 不会有人调 —— 本函数必须
经 :data:`freeagent.web.routes.READ_ROUTES` 注册，且**不进**任何写路由。
"""

from __future__ import annotations

from typing import Any

from ..app import App
from ..config import load_config
from ..domain import ValidationError
from ..services import delegate_browse
from .endpoints_feishu import state_home_for

__all__ = ["delegate_browse_list"]


def delegate_browse_list(app: App, q: dict[str, list[str]]) -> dict[str, Any]:
    """列 ``roots[root]`` 下 ``path`` 处的直接子目录。

    ``delegate.browse_roots`` 为空时**不报错**，返回 ``enabled: False``
    加一句人话原因：浏览默认就是关的，把「没配置」当错误弹出来会让
    没开这个功能的用户以为坏了。
    """
    cfg = load_config(state_home_for(app))
    roots = cfg.delegate_browse_roots
    if not delegate_browse.enabled(roots):
        return {
            "kind": "delegate_browse",
            "enabled": False,
            "reason": "「可授权根」delegate.browse_roots 未配置"
            "（默认空 = 浏览关闭）。在 config.json 里给一组绝对路径即可开启。",
        }

    raw_root = (q.get("root") or ["0"])[0]
    try:
        index: object = int(raw_root)
    except ValueError:
        # 0 是「第 0 个根」的正常默认；只有非整数才算输入坏掉。
        raise ValidationError(f"root 必须是整数根编号，收到：{raw_root!r}") from None
    rel = (q.get("path") or [""])[0]

    dirs = delegate_browse.list_dirs(roots, index, rel)
    return {
        "kind": "delegate_browse",
        "enabled": True,
        "roots": list(roots),
        "root": index,
        "path": rel,
        "dirs": dirs,
    }
