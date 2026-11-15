"""一句话要不要带上「家里的事」（设计文档 21.5 / V1.115）。

## 治的是哪个缺口

V1.114 的工作台每次从零开始：引擎收到的是**你刚说的那一句**，不知道你
今天还压着哪几件事、答应过谁什么、以前记过什么约定。豆包工作的那一半
「厚度」就在这儿 —— 它知道你在忙什么。

## 三条纪律

1. **只读，不写。** 刻意不调 ``TodayService.view()``：它会先顺延逾期事务
   并在 ``task_records`` 里留一条记录 —— 注入上下文**不该偷偷改事务**
   （那是「看起来在工作」的另一种形态）。改用 ``scheduled_for()``：同一套
   排序、同样的权重，但只读。
2. **只出一行。** opencode 把含换行的 prompt 当作复杂任务、会升级自己的
   模型；而这一层加的是背景，不是新任务。
3. **读不出来就说读不出来。** 「今天没有事」与「读不到库」在界面上是两句
   不同的话 —— 合成一句，人会以为自己的事务丢了。

## 只摆事实，不替人做决定

带过去的是**标题与承诺原话**，不是「你应该先做这个」。排序是既有的 7.2
权重表算的，这一层不加任何新判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["ContextBrief", "build_context"]

#: 每一类最多带几条。3 / 2 / 3 是「够用且不至于把那句话淹掉」的取舍：
#: 引擎真正需要的是「有没有这件事」，细节它自己会读库 / 读文件。
MAX_TASKS = 3
MAX_PROMISES = 2
MAX_MEMORIES = 3
#: 整段上下文的字数上限。超了先截断**最长的那个字段**并写明截断 ——
#: 静默截断等于隐瞒（与检索结果 ``truncated`` 同一条纪律）。
MAX_CHARS = 420


@dataclass(frozen=True, slots=True)
class ContextBrief:
    """一段要随话一起发出去的上下文。

    ``text`` 是**单行**原文（空串 = 什么都没带）；``note`` 是给人看的一句
    说明（「附带：今天的活 3 条」或「读不到今天的活（…）」）。两个字段
    刻意分开：说明要能短、能显示在界面上，而原文要能不丢信息。
    """

    text: str = ""
    note: str = ""
    ok: bool = True


def build_context(app: Any, query: str, *, limit_chars: int = MAX_CHARS) -> ContextBrief:
    """把今天的事 / 未结清的承诺 / 相关记忆折成一行。

    ``query`` 是用户那句话 —— 记忆按它检索（相关度，不是最近）。
    任何一段读不出来：其余段照常带，``note`` 里写明哪一段没读到、
    为什么（fail-soft 但**说出来**，不静默降级）。
    """
    parts: list[str] = []
    counts: list[str] = []
    problems: list[str] = []

    tasks, task_problem = _today_tasks(app)
    if task_problem:
        problems.append(task_problem)
    elif tasks:
        parts.append("今天的活：" + "；".join(tasks))
        counts.append(f"今天的活 {len(tasks)} 条")

    promises, promise_problem = _open_promises(app)
    if promise_problem:
        problems.append(promise_problem)
    elif promises:
        parts.append("未结清的承诺：" + "；".join(promises))
        counts.append(f"承诺 {len(promises)} 条")

    memories, memory_problem = _memories(app, query)
    if memory_problem:
        problems.append(memory_problem)
    elif memories:
        parts.append("相关记忆：" + "；".join(memories))
        counts.append(f"记忆 {len(memories)} 条")

    text = " | ".join(parts)
    truncated = False
    if len(text) > limit_chars:
        text = text[:limit_chars]
        truncated = True

    bits: list[str] = []
    if counts:
        bits.append("附带：" + "、".join(counts))
    if truncated:
        # 截断必须写明：不然人会以为「这就是全部」。
        bits.append(f"上下文超过 {limit_chars} 字，已截断")
    if problems:
        bits.append("没读到：" + "、".join(problems))
    note = "；".join(bits) if bits else "这一轮没有可带的上下文"
    return ContextBrief(text=text, note=note, ok=not problems)


def _today_tasks(app: Any) -> tuple[list[str], str]:
    """今天的活（按既有权重排序，**只读**）。"""
    try:
        scored = app.today.scheduled_for(app.clock.today())
    except Exception as exc:  # noqa: BLE001 - 注入失败不该让一轮跑不起来
        return [], f"今天的活（{exc}）"
    out: list[str] = []
    for item in list(scored)[:MAX_TASKS]:
        title = getattr(getattr(item, "task", None), "title", "")
        if isinstance(title, str) and title.strip():
            out.append(title.strip())
    return out, ""


def _open_promises(app: Any) -> tuple[list[str], str]:
    """未结清的承诺（原话，不改写）。"""
    try:
        pending = app.promise.open_promises()
    except Exception as exc:  # noqa: BLE001
        return [], f"未结清的承诺（{exc}）"
    out: list[str] = []
    for promise in list(pending)[:MAX_PROMISES]:
        statement = getattr(promise, "statement", "")
        if isinstance(statement, str) and statement.strip():
            out.append(statement.strip())
    return out, ""


def _memories(app: Any, query: str) -> tuple[list[str], str]:
    """按**相关度**取记忆（不是最近的那几条）。"""
    if not (query or "").strip():
        return [], ""
    try:
        result = app.memory.retrieve(query, scope=None, limit=MAX_MEMORIES,
                                     consumer="workbench")
    except Exception as exc:  # noqa: BLE001
        return [], f"相关记忆（{exc}）"
    if getattr(result, "degraded", False):
        # 检索不可用时**不能**报成「没有相关记忆」—— 那是把故障说成事实。
        return [], f"相关记忆（检索不可用：{getattr(result, 'degraded_reason', '')}）"
    out: list[str] = []
    for hit in list(getattr(result, "hits", ()))[:MAX_MEMORIES]:
        entry = getattr(hit, "entry", None)
        content = getattr(entry, "content", "")
        if isinstance(content, str) and content.strip():
            out.append(content.strip()[:60])
    return out, ""
