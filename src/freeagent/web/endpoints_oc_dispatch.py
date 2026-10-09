"""``POST /api/oc/dispatch`` —— 用当前这套选择，建一条委派事务。

## 为什么它不在 :mod:`.endpoints_oc_selection_write` 里

写侧那两个改的是**选择配置**（落 ``oc_selection`` 那份状态）；
这个函数建的是一条**事务**（落 ``tasks`` 表）。混在一个模块里，
模块名就会骗人——后来的人以为「改选择」会连带建事务。

## 它用的是哪个入口

:meth:`Repl._create_delegation` —— 命令与自然语言**共用**的那一个入口。
白名单闸门、脉络归属、``delegate_chat_id`` / ``delegate_requested_by``
全在那一处（设计文档 12.1.1「一份能力一份实现」）。

## 建了事务**不等于**开始跑

执行器是独立进程，它扫「已确认且已 ``/start``」的事务。所以这里
**不**假装任务已经在跑；返回的话术里说清下一步。
"""

from __future__ import annotations

from typing import Any

from ..app import App
from ..domain import FreeAgentError
from ..services import oc_selection as sel
from .endpoints_feishu import state_home_for


def oc_dispatch(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/oc/dispatch`` —— 用当前这套选择，建一条委派事务。

    ## 为什么**不**拼一条 ``/delegate …`` 命令发给 ``/api/chat``

    看着更省事（复用 ``command_payload``），但有两个真问题：

    1. **空脉络会把参数错位。** :func:`freeagent.cli.app.parse_delegate_args`
       用 ``if seg`` 过滤空段，于是 ``proj | | brief`` 变成**两**段 →
       角色取到 brief、需求为空 → 报「格式不对」。而脉络恰恰常常该留空
       （由 :meth:`Repl._create_delegation` 退回第一条）。
    2. 命令字符串是**第二份参数契约**。字段顺序、分隔符规则一旦和
       :meth:`Repl._cmd_delegate` 那边的理解错开，症状是「建出来的任务
       需求是空的」—— 而那看着像用户没写清楚。

    所以直接调 :meth:`Repl._create_delegation`。

    ## 需求必须**单行**

    实测：提示里只要有换行，opencode 就判定为「复杂任务」并升级到主 agent
    的强模型、**无视** ``--model``，于是必然失败。那是 opencode 的行为，
    不是我们的 —— 所以在这里挡，并说清原因。
    """
    from .commands import _web_repl

    home = state_home_for(app)
    selection = sel.load_selection(home)

    brief = str(body.get("brief") or "").strip()
    if not brief:
        raise FreeAgentError("先说要做什么 —— 一句话就行")
    if "\n" in brief or "\r" in brief:
        raise FreeAgentError(
            "需求要写成一行。含换行时 opencode 会判成复杂任务、"
            "升级到它自己的强模型并无视你选的模型，然后失败。"
        )

    project = str(body.get("project") or "").strip() or selection.project
    if not project:
        raise FreeAgentError(
            "还没选项目。上面选一个，或用 `/delegate <项目> | <脉络> | <需求>`。")

    role_name = str(body.get("role") or "").strip()

    repl = _web_repl(app)
    task, err, role, by_default = repl._create_delegation(project, role_name, brief)
    if err:
        raise FreeAgentError(err)

    # 角色级工作模式兜底（设计文档 11.13.6 / 11.14）：显式选择为空时，
    # 用角色默认 agent 填入选择，使之下游经 delegate_policy 进入运行时。
    if not selection.agent and role and role.default_agent:
        selection.agent = role.default_agent
        sel.save_selection(selection, home)

    note = "（你没指定脉络，用了第一条）" if by_default else ""
    return {
        "ok": True,
        "task_id": task.id,
        "title": task.title,
        "role": role.name,
        "role_by_default": by_default,
        "selection": {
            "project": selection.project, "agent": selection.agent,
            "model": selection.model, "variant": selection.variant,
        },
        "next": (
            note +
            "点「开始做」后，执行器（独立进程）才会接手 —— "
            "它得在跑（python -m freeagent.delegate）。"
            "之后 opencode 每要动手一次，会在飞书给你一张授权卡。"
        ),
    }