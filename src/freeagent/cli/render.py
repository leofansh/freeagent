"""视图渲染。

两条硬性要求（设计文档第七章）：
* 每条排序信号必须把**理由**一起展示；
* 输出必须标注「启发式提示，不是评分」。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

from ..domain import (
    CONTACT_KIND_LABELS,
    DecisionLog,
    RecordType,
    RestoreView,
    Role,
    ScoredTask,
    Task,
)
from ..services.llm import SignalRef, TaskRef
from ..services.reminders import ReminderDigest
from ..services.sorting import DISCLAIMER_MARKER
from ..services.today import TodayView
from ..services.week import WeekView
from ..services.care import CareDue
from ..services.decision import Confrontation
from ..services.weigh import WeighResult

__all__ = [
    "DISCLAIMER",
    "role_names",
    "render_today",
    "render_task",
    "render_roles",
    "render_role_detail",
    "render_all",
    "render_week",
    "render_pending",
    "render_digest",
    "render_suggestion",
    "render_artifact",
    "render_artifact_list",
    "render_weigh",
    "render_weigh_picked",
    "render_decisions",
    "render_decision_history",
    "to_task_ref",
    "to_signal_refs",
]

#: 与排序层共用同一个常量，避免文案漂移。
DISCLAIMER = DISCLAIMER_MARKER

_ARTIFACT_LABELS = {"draft": "草稿", "accepted": "已采纳", "superseded": "已被取代"}


def role_names(roles: Iterable[Role]) -> dict[str, str]:
    """id → 显示名。唯一实现在 ``web.serialize``，这里转出去避免两份实现。"""
    from ..web.serialize import role_names as _impl

    return _impl(roles)


def _role_label(task: Task, names: dict[str, str]) -> str:
    return "【" + "、".join(names.get(rid, "?") for rid in task.role_ids) + "】"


def _signal_text(scored: ScoredTask) -> str:
    if not scored.signals:
        return "无命中信号"
    return "；".join(s.reason for s in scored.signals)


def _one_line(index: int, scored: ScoredTask, names: dict[str, str]) -> str:
    task = scored.task
    head = f"{index}. {_role_label(task, names)}{task.title}"
    meta = f"{task.kind.label} · {task.state.label}"
    if task.scheduled_for is not None:
        meta += f" · 排 {task.scheduled_for}"
    return f"{head}\n     {meta}  [{scored.total_weight} 分]  {_signal_text(scored)}"


def render_today(
    view: TodayView, names: dict[str, str], *, pending_count: int = 0
) -> str:
    summary = view.rollover.summary()
    rolled = view.rolled_over_ids
    lines: list[str] = []
    if summary:
        lines.append(f"顺延：{summary}")
        lines.append("")
    lines.append(f"今天 · {view.day} · {len(view.items)} 件事（{DISCLAIMER}）")
    if not view.items:
        # 空态别斩钉截铁说「没有」—— 没排期 / 排未来的都在库里（V1.84，
        # 与 unscheduled_count 同构）。不报出来用户会以为刚记的事丢了。
        extras = []
        if view.unscheduled_count:
            extras.append(f"另有 {view.unscheduled_count} 条没排期")
        if view.future_count:
            when = f"，最早 {view.earliest_future}" if view.earliest_future else ""
            extras.append(f"还有 {view.future_count} 条排在后面{when}")
        tail = "；" + "；".join(extras) if extras else ""
        lines.append(f"今天没有排进来的事务。{tail}直接说一句话就能新建。")
        return _with_pending_hint(lines, pending_count)

    lines.append("")
    for index, scored in enumerate(view.items, start=1):
        mark = "  ⟲顺延" if scored.task.id in rolled else ""
        lines.append(_one_line(index, scored, names) + mark)
    lines.append("")
    lines.append("助手不替你决定先做哪个。用 /task <id> 打开某一条。")
    return _with_pending_hint(lines, pending_count)


def _with_pending_hint(lines: list[str], pending_count: int) -> str:
    """有待处理捕获时，给今天视图挂一条**有界**提醒。

    **只在 >0 时出现**：0 条还挂一行是噪音，而今天的主区是排序结果，
    不该被一条常态空行稀释。收集器是工作台，今天只负责提醒
    「还有没收揽的活」—— 细节去 /pending，这里给路（11.18）。
    """
    if pending_count > 0:
        lines.append("")
        lines.append(
            f"待处理 {pending_count} 条（收下的图片/文件还没澄清）"
            " → /pending 查看，/pending <序号> 澄清成事务"
        )
    return "\n".join(lines)


def render_all(
    scored_list: list[ScoredTask], names: dict[str, str], scope: str = "open"
) -> str:
    titles = {
        "open": "全部未结束事务",
        "closed": "已结束事务",
        "all": "全部事务",
    }
    head = titles.get(scope, "全部事务")
    lines = [f"{head} · {len(scored_list)} 条（{DISCLAIMER}）"]
    if not scored_list:
        lines.append("（空）")
        return "\n".join(lines)
    lines.append("")
    for index, scored in enumerate(scored_list, start=1):
        lines.append(_one_line(index, scored, names))
    return "\n".join(lines)


#: 周一…周日。``date.weekday()`` 是 0=周一，所以直接索引。
_WEEKDAY_CN = "一二三四五六日"


def render_week(view: WeekView, names: dict[str, str]) -> str:
    """周回顾（6.5）。

    刻意**不排序**：分区内按事实本身的时间顺序（结束时间倒序、排期升序、
    创建升序）。打了分的列表会暗示「先看哪条」，而回顾要回答的是
    「这一周发生了什么」，不是「现在先做哪个」。
    """
    span = f"{view.start} ~ {view.end - timedelta(days=1)}"
    lines = [f"本周回顾 · {span}（{DISCLAIMER}）"]
    if view.is_empty:
        lines.append("本周还没有可回顾的事实。直接说一句话就能新建。")
        return "\n".join(lines)

    lines.append("")
    lines.append(
        f"本周结束 {len(view.closed)} 件"
        f"（完成 {len(view.completed)} / 放弃 {len(view.dropped)}）"
    )
    if view.closed:
        for index, task in enumerate(view.closed, start=1):
            when = task.completed_at or task.dropped_at
            at = f" {when:%m-%d %H:%M}" if when is not None else ""
            lines.append(
                f"  {index}. {_role_label(task, names)}{task.title}"
                f" —— {task.state.label}{at}"
            )
    else:
        lines.append("  （没有结束的事务）")

    lines.append("")
    if view.slips:
        lines.append(f"卡在哪：本周顺延 {view.slipped_times} 次")
        for item in view.slips:
            lines.append(f"  · {item.task.title} —— 顺延 {item.times} 次")
    else:
        lines.append("卡在哪：本周没有顺延记录。")

    lines.append("")
    if view.next_week:
        lines.append(f"下周承诺 {len(view.next_week)} 件")
        for task in view.next_week:
            day = task.scheduled_for
            assert day is not None            # WeekView 只收排期非空的
            lines.append(
                f"  · {day:%m-%d} 周{_WEEKDAY_CN[day.weekday()]}"
                f"  {_role_label(task, names)}{task.title}"
            )
    else:
        lines.append("下周承诺：下周还没有排期。")

    lines.append("")
    if view.unscheduled:
        lines.append(f"没排期 {len(view.unscheduled)} 件（记了但没进任何一天）")
        for task in view.unscheduled:
            lines.append(f"  · {_role_label(task, names)}{task.title}")
    else:
        lines.append("没排期：没有悬空的事务。")

    if view.waiting:
        lines.append("")
        lines.append(f"在等 {len(view.waiting)} 件")
        for task in view.waiting:
            waiting = task.waiting_on
            assert waiting is not None        # WeekView 只收在等的
            lines.append(f"  · {task.title} —— {waiting.describe()}")

    # V1.71 成长目标（有意提示 / 20.1.12）：独立于四个事务分区，不与 care 的责任
    # 提醒并。只原样列出、不催促不评价 —— 每周回顾时把「你承诺的自我提高」带回视野。
    lines.append("")
    if view.growth_goals:
        lines.append("成长目标（记下的自我期许）:")
        for _kind, text in view.growth_goals:
            lines.append(f"  · {text}")
    else:
        lines.append("成长目标：还没记过 —— 用 /remember 记一条（如「今年把英语练到能开会」）。")

    # V1.133：拍板 / 承诺 / 工作台痕迹。三项都是「本周做过的事」，
    # 刻意放在事务分区之后 —— 它们回答的不是「还有什么没做」，而是
    # 「这一周我实际做了什么决定、许了什么诺、动过几次手」。
    # 拍板只摆**逐字冻结**的 conflict_text / reason_text，不解析 options_json
    # （解析结构化快照 = 把字段名变成契约，且会随格式变动而漂）。
    lines.append("")
    if view.decisions:
        lines.append(f"本周拍板 {len(view.decisions)} 件（当时记下的原话）:")
        for d in view.decisions:
            why = f" —— 因为：{d.reason_text}" if d.reason_text else ""
            lines.append(f"  · {d.conflict_text}{why}")
    else:
        lines.append("本周拍板：没有拍过板。")

    lines.append("")
    if view.promises:
        lines.append(f"本周许下的承诺 {len(view.promises)} 件（仍未兑现）:")
        for promise in view.promises:
            due = f" —— 承诺 {promise.promised_for}" if promise.promised_for else ""
            lines.append(f"  · {promise.statement}{due}")
    else:
        lines.append("本周承诺：没有新许下的承诺。")

    lines.append("")
    lines.append(
        f"本周交给引擎 {view.turn_count} 轮"
        if view.turn_count else "本周没有交给引擎的轮次。"
    )

    lines.append("")
    lines.append("这些是事实，不是建议 —— 要改什么，由你决定。")
    return "\n".join(lines)


def _hour_span_label(peak: tuple[int, int, int]) -> str:
    """(21, 23, 13) → 「21-23 点」；(21, 21, 3) → 「21 点」。"""
    start, end, _count = peak
    if end > start:
        return f"{start:02d}-{end:02d} 点"
    return f"{start:02d} 点"


def _dow_label(peak: tuple[int, int]) -> str:
    """(1, 14) → 「周二」。"""
    return f"周{_WEEKDAY_CN[peak[0]]}"


def _peak_lines(peak, done: int, label) -> list[str]:
    """时段/星期分布下面那一行凝练（V1.101）。

    出峰：**带计数与占比**（「其中 13 次（35%）集中在 21-23 点」）——
    V1.96「没有计数的规律不可反驳」在呈现层的落地。
    未达判定线：**说清判定线本身**，不静默跳过 —— 认不出而一声不响，
    看的人只会以为漏印了一行。
    """
    if peak is not None:
        count = peak[-1]
        share = count * 100 // done
        text = label(peak)
        # 数字与汉字之间留一个空格，汉字之间不留：「集中在 21-23 点」/
        # 「集中在周三」。同一个模板，靠标签首字符定，不写死两个模板。
        gap = " " if text[:1].isdigit() else ""
        return [f"其中 {count} 次（{share}%）集中在{gap}{text}"]
    return [
        "（没有达到「集中」的判定线：至少 3 次、且占全部完成的三分之一"
        " —— 上面是原始分布）"
    ]


def render_model(view, names: dict[str, str]) -> str:
    """连续性个人模型（11.17 / 20.1.1）。

    刻意**不排序打分、不给建议**：只把从记录里现推的规律摆出来
    （第七章「摆得更清楚 ≠ 替你决定」）。与 /week、/feedback 同一条纪律。
    ``names`` 保留签名一致，角色名已在服务层翻好。

    V1.101：时段/星期分布下各带一行**凝练结论**（「集中在哪」+
    计数与占比）。它是统计描述，不是评价 —— 「37 次里 13 次在 21-23 点」
    可以说，「你总在深夜干活」不可以说。
    """
    days = (view.until - view.since).days + 1
    lines = [f"连续性个人模型 · 近 {days} 天（{DISCLAIMER}）"]
    if view.is_empty:
        lines.append("这段时间还没有可归纳的记录 —— 先记几件事、完成几件，规律才会显现。")
        return "\n".join(lines)

    lines.append("")
    if view.done_in_window:
        lines.append(f"完成 {view.done_in_window} 件，按时段分布：")
        for hour, count in view.completion_by_hour:
            lines.append(f"  {hour:02d}:00  {count:>3}  {'▏' * count}")
        # V1.101 凝练结论：分布摆事实，「集中在哪」也要能一句话说出 ——
        # 每条都带计数（V1.96：没有计数的规律不可反驳）。判定不出时
        # 说清判定线，不静默（认不出要自报，同 engine_errors 的 unknown）。
        lines.extend(_peak_lines(view.hour_peak, view.done_in_window, _hour_span_label))
        lines.append("按星期分布：")
        for dow, count in view.completion_by_dow:
            lines.append(f"  周{_WEEKDAY_CN[dow]}   {count:>3}  {'▏' * count}")
        lines.extend(_peak_lines(view.dow_peak, view.done_in_window, _dow_label))
    else:
        lines.append("这段时间没有完成的记录。")

    lines.append("")
    if view.rollover_total:
        extra = f"（其中反复顺延 {len(view.tasks_rolled_over)} 件）" if view.tasks_rolled_over else ""
        lines.append(f"顺延 {view.rollover_total} 次{extra}")
        for tid, title, n in view.tasks_rolled_over:
            lines.append(f"  · {title}（{tid}） —— 顺延 {n} 次")
    else:
        lines.append("顺延：这段时间没有顺延记录。")

    lines.append("")
    if view.stuck_waiting:
        lines.append(f"卡住的等候 {len(view.stuck_waiting)} 件（已等 ≥7 天）：")
        for tid, title, age in view.stuck_waiting:
            lines.append(f"  · {title}（{tid}） —— 已等 {age} 天")
    else:
        lines.append("卡住的等候：没有悬在别人手里超过 7 天的事。")

    lines.append("")
    if view.role_activity:
        lines.append("角色活跃度（按挂接的事务数）：")
        for name, count in view.role_activity:
            lines.append(f"  · {name}  {count}")
    else:
        lines.append("角色活跃度：还没有任何角色。")

    # V1.60：关系账 + 承诺与目标两分面（只读聚合，只摆事实）。
    lines.append("")
    if view.relationships:
        lines.append("关系账（还跟谁有未结清的账）：")
        for _cid, name, promises, waitings in view.relationships:
            bits = []
            if promises:
                bits.append(f"我欠他 {promises} 条承诺")
            if waitings:
                bits.append(f"在等他 {waitings} 笔")
            lines.append(f"  · {name} —— " + "、".join(bits))
    else:
        lines.append("关系账：目前没有挂到具体人的未结清承诺或等候。")

    lines.append("")
    if view.commitments:
        lines.append("承诺与目标：")
        for kind, text, overdue in view.commitments:
            tag = "（已逾期）" if overdue else ""
            lines.append(f"  · [{kind}]{tag} {text}")
    else:
        lines.append("承诺与目标：没有未兑现的对外承诺，也没记过「约定 / 目标」。")

    # V1.69：滋养兴趣分面（记录优先，20.1.11）——「偏好」是我是谁，不是义务。
    # 只原样列出，不催促不评价；与「承诺与目标」分开，避免把爱好读成该兑现的事。
    lines.append("")
    if view.preferences:
        lines.append("滋养兴趣（记下的偏好）：")
        for kind, text in view.preferences:
            lines.append(f"  · {text}")
    else:
        lines.append("滋养兴趣：还没记过「偏好」—— 用 /remember 记一条（如「我最近在学赛艇」）。")

    lines.append("")
    lines.append("这些是从你的记录里现推的事实，不是评价 —— 要不要顺着它调整，由你决定。")
    return "\n".join(lines)


#: 捕获类型 → 人话。与桥接 ``_capture_unsupported`` 的 label 口径一致
#: （那边是「图片 / 文件」的二选一，这里同款）。
_CAPTURE_KIND_LABELS = {"image": "图片", "file": "文件"}


def _fmt_size(size: int) -> str:
    """字节数 → 人话。下载失败时 ``size`` 是消息里声称的大小，照实显示。"""
    if size <= 0:
        return "大小未知"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def render_pending(items, now=None) -> str:
    """待处理收集器（11.18）。

    **只摆事实，不猜内容**：每条捕获显示类型、收下时间、大小、本地路径 ——
    「它是什么」留给用户。序号即澄清时的编号（``/pending N``），
    先进先出，与仓储排序一致（最早收下的排最前）。
    """
    count = len(items)
    lines = [f"待处理 · {count} 条（最早收下的排最前；只摆事实，不猜它们是什么）"]
    if count == 0:
        lines.append("收集器是空的。飞书里发图片/文件会被收下并放到这里，")
        lines.append("等你显式说一声才变成事务。")
        return "\n".join(lines)

    lines.append("")
    for index, item in enumerate(items, start=1):
        label = _CAPTURE_KIND_LABELS.get(item.kind, item.kind or "捕获")
        head = f"  {index}. {label}"
        if item.name and item.name != item.kind:
            head += f"「{item.name}」"
        head += f" · {item.created_at:%m-%d %H:%M} · {_fmt_size(item.size)}"
        lines.append(head)
        if item.path:
            lines.append(f"     {item.path}")
        else:
            lines.append(f"     （未下载，飞书引用 {item.media_key}）")
    lines.append("")
    lines.append("澄清成事务：/pending <序号> [标题]")
    lines.append("明确不要了：/pending drop <序号>")
    return "\n".join(lines)


def render_promises(items, contacts=None, now=None) -> str:
    """对外承诺列表（设计文档 11.19）。

    **只摆事实，不判违约**：逾期的写「已逾期 N 天」，不写「你违约了」——
    判违约是用户的事（可能延期了、可能对方改主意了，见 11.19.5）。

    ``contacts`` 是 ``{contact_id: 名字}``，用来把关系显示成
    「答应了张总」而不是一串 id。
    """
    names = contacts or {}
    count = len(items)
    lines = [
        f"对外承诺 · {count} 条"
        "（我答应别人的；快到期的在前；只摆事实，违不违约你说）"
    ]
    if count == 0:
        lines.append("没有未兑现的承诺。")
        return "\n".join(lines)

    lines.append("")
    for index, promise in enumerate(items, start=1):
        head = f"  {index}. {promise.statement}"
        who = names.get(promise.contact_id or "")
        if who:
            head += f"（答应{who}）"
        if promise.promised_for is not None:
            head += f" · 期限 {promise.promised_for:%m-%d %H:%M}"
            if now is not None and promise.promised_for < now:
                overdue = now - promise.promised_for
                days = overdue.days
                head += (
                    f" · **已逾期 {days} 天**"
                    if days >= 1
                    else " · **今天刚过期限**"
                )
        lines.append(head)
    lines.append("")
    lines.append("兑现了：/promise keep <序号>")
    lines.append("违约了：/promise break <序号>")
    lines.append("收回了：/promise withdraw <序号>")
    lines.append("延期到：/promise postpone <序号> <日期>")
    return "\n".join(lines)


def render_contacts(items, promise_counts=None, waiting_counts=None) -> str:
    """关系对象列表（设计文档 11.19 / 20.1.5）。

    ``promise_counts`` 是 ``{contact_id: 未兑现承诺数}``，让用户一眼看到
    「我跟谁之间还有账」—— 那是关系一等化的全部意义。

    ``waiting_counts``（V1.52）是 ``{contact_id: 未结束等候数}``：关系不只
    挂在承诺上（我答应对方），也挂在等候上（我在等对方）。两数并排，
    「我跟张总之间有三笔没结清的」这个答案才是完整的 —— 少一半就还得
    靠人眼翻事务列表。
    """
    counts = promise_counts or {}
    waits = waiting_counts or {}
    count = len(items)
    lines = [f"关系对象 · {count} 个（事务背后的人；与角色脉络是两个维度）"]
    if count == 0:
        lines.append("还没有记录过关系对象。")
        return "\n".join(lines)

    lines.append("")
    for index, contact in enumerate(items, start=1):
        head = f"  {index}. {contact.name}"
        kind = CONTACT_KIND_LABELS.get(contact.kind, contact.kind.value)
        head += f" · {kind}"
        parts = []
        pending = counts.get(contact.id, 0)
        if pending:
            parts.append(f"{pending} 条承诺未兑现")
        waiting = waits.get(contact.id, 0)
        if waiting:
            parts.append(f"{waiting} 笔在等他")
        if parts:
            head += " · " + "、".join(parts)
        lines.append(head)
        if contact.note:
            lines.append(f"     {contact.note}")
    return "\n".join(lines)


def render_contact_ledger(ledger, now: datetime | None = None) -> str:
    """关系对象名下的「一页账」（设计文档 20.1.5 的最后一步）。

    见这个人之前最该先看的一页：**我欠他的**（未兑现承诺）与
    **我在等他的**（未结束等候）。只摆事实 —— 逾期的写「已逾期 N 天」、
    「已过 N 天」，不写「你违约了」，也不催。判违约、催不催，都是
    用户的事（11.19.5）。
    """
    contact = ledger.contact
    lines = [
        f"{contact.name} · {CONTACT_KIND_LABELS.get(contact.kind, contact.kind.value)}",
    ]
    if contact.note:
        lines.append(f"  备注：{contact.note}")

    promises = ledger.promises
    lines.append("")
    if promises:
        lines.append(f"我欠他的（未兑现承诺 · {len(promises)} 条）：")
        for index, item in enumerate(promises, start=1):
            head = f"  {index}. {item.promise.statement}"
            when = item.promise.promised_for
            if when is not None:
                head += f" · 期限 {when:%m-%d %H:%M}"
                if now is not None and when < now:
                    head += _elapsed_tag(now - when)
            head += f" → {item.task_title} [{item.task_id[:8]}]"
            lines.append(head)
    else:
        lines.append("我欠他的：没有未兑现的承诺。")

    waitings = ledger.waitings
    lines.append("")
    if waitings:
        lines.append(f"我在等他的（未结束等候 · {len(waitings)} 条）：")
        for index, item in enumerate(waitings, start=1):
            w = item.waiting
            head = f"  {index}. 等 {w.who_or_what}（从 {w.since:%m-%d} 起）"
            if w.follow_up_at is not None:
                head += f" · 原定跟进 {w.follow_up_at:%m-%d %H:%M}"
                if now is not None and w.follow_up_at < now:
                    head += _elapsed_tag(now - w.follow_up_at)
            head += f" → {item.task.title} [{item.task.id[:8]}]"
            lines.append(head)
    else:
        lines.append("我在等他的：没有未结束的等候。")

    lines.append("")
    if ledger.is_empty:
        lines.append("账已结清 —— 既不欠他，也不在等他。")
    lines.append("结账：/promise keep|break|withdraw <序号>（见 /promise 列表）")
    lines.append(f"记新的：/promise 记 <事务id> <承诺> 关系={contact.name}")
    lines.append(f"         /wait <事务id> <等谁> 关系={contact.name}")
    return "\n".join(lines)


def _elapsed_tag(delta: timedelta) -> str:
    """「已逾期 N 天」/「已过 N 天」/「今天刚过」—— 只陈述时间，不判违约。"""
    if delta.days >= 1:
        return f" · 已过 {delta.days} 天"
    return " · 今天刚过"


def render_nudge_targets(items) -> str:
    """该催的事务（设计文档 20.1.4）。

    **只列事实与方向，不写该怎么说。** 输出里没有一句建议语气的话 ——
    催办的急迫感由用户自己决定（``services/nudge.py`` 纪律 3）。
    方向写清「等我方动静」还是「等我方交付」，是因为这两者语气相反，
    看错方向就会催反（该催的人没被催，该道歉的事被写成质问）。
    """
    count = len(items)
    lines = [
        f"该催的事务 · {count} 条"
        "（系统只铺事实，正文要你自己敲；**它不会代发**）"
    ]
    if count == 0:
        lines.append(
            "现在没有到点的催办：等候件的跟进日没过、承诺也没逾期。"
        )
        return "\n".join(lines)

    lines.append("")
    for index, target in enumerate(items, start=1):
        head = f"  {index}. {target.task.title}"
        who = target.contact_name
        head += f"（{who}）" if who else "（未挂关系）"
        if target.direction == "promise":
            head += " · 我欠对方"
        else:
            head += " · 等对方"
        lines.append(f"{head}　{target.elapsed}")
    lines.append("")
    lines.append("起草稿（事实已铺好，正文留白）：/nudge <序号>")
    return "\n".join(lines)


def _strip_html_comments(text: str) -> str:
    """剥掉 HTML 注释行。

    产物正文里的 ``<!-- ... -->`` 在浏览器里看不见，在飞书纯文本里却
    清清楚楚——用户看到的是 ``<!-- opencode session: ses_... -->`` 这种
    内部标记，只会困惑。库里保留原文（程序可追溯），渲染时剥掉。
    """
    return "\n".join(
        line for line in text.splitlines()
        if not (line.strip().startswith("<!--") and line.strip().endswith("-->"))
    )


def render_artifact(artifact) -> str:
    """单个草稿版本。"""
    label = _ARTIFACT_LABELS.get(artifact.status.value, artifact.status.value)
    lines = [f"version {artifact.version}（{label}）· {artifact.title}"]
    if artifact.supersedes:
        lines.append(f"  上一版：{artifact.supersedes[:8]}")
    lines.append("")
    lines += [f"  {line}" for line in _strip_html_comments(artifact.content).splitlines()]
    return "\n".join(lines)


def render_artifact_list(artifacts) -> str:
    """版本一览。旧版永不删除，所以这里能看到完整链条。"""
    lines = [f"草稿版本（{len(artifacts)} 个，旧版全部保留）："]
    for artifact in artifacts:
        label = _ARTIFACT_LABELS.get(artifact.status.value, artifact.status.value)
        first_line = artifact.content.splitlines()[0] if artifact.content else ""
        lines.append(
            f"  v{artifact.version}  [{label}]  {artifact.created_at:%m-%d %H:%M}"
            f"  {first_line[:40]}"
        )
    lines.append("")
    lines.append("  /artifact <id> <版本> 看全文，/accept <id> <版本> 采纳")
    return "\n".join(lines)


def render_brief(text: str, quadrant, task_id: str = "") -> str:
    """简报预览 + 协作象限（V1.103 / 12.10.12）。

    两个刻意的排版决定：

    * **只按分隔符折行，不改一个字**。简报的硬约束是单行（换行会让
      opencode 升级到付费模型），所以预览必须先说清「实发的是单行」，
      否则人会以为执行器收到的是这份带缩进的东西。
    * **正文放最后**。人打开这个命令是为了判「我还该说什么」，那答案在
      象限三行里，不在几百字的正文里。
    """
    human = "、".join(quadrant.human_side) or "无"
    machine = "、".join(quadrant.machine_side) or "无"
    lines = [
        "简报预览 —— 执行器实际收到的原文（单行；下面只按「；」折行，内容一字未改）",
        "",
        f"  协作象限：{quadrant.label}",
        f"  你给过的：{human}",
        f"  机器记下的：{machine}",
        f"  该说多少：{quadrant.says_how_much}",
    ]
    if quadrant.what_to_add:
        add = quadrant.what_to_add
        if task_id:
            add = add.replace("<id>", task_id[:8])
        lines.append(f"  可补：{add}")
    lines.append("")
    lines.append("  正文：")
    for part in text.split("；"):
        if part.strip():
            lines.append(f"    · {part.strip()}")
    return "\n".join(lines)


def render_boundary(boundary) -> str:
    """一条答复的凭据档位（V1.104 / 11.10.9）。

    抬头那句**不是客套**：分档说的是「查没查」，不是「对不对」。少了它，
    「确定」会被读成「这句话是对的」—— 那正好是这套标注最坏的误用，
    比不标注更坏（人会把一个读错文件的结论当成已核事实）。
    """
    return "\n".join([
        "—— 凭据（说的是「查没查」，不是「对不对」）——",
        f"  {boundary.label} · {boundary.basis}",
    ])


def render_online_leg(leg) -> str:
    """在线腿的一块展示（V1.105 / 3.7.9）。**没启用时返回空串**。

    三条纪律都在这个函数里：

    1. **提交原文去哪了**：真发出去就写明主机 —— 查询离开本机是一次不可
       撤回的披露，人有权在结果旁边看到这件事。
    2. **单列，不混排**：抬头写「外部材料，未核实」，与本地条目分开摆。
       本地条目是「你记过的」，外部材料是「网上有个人这么说」。
    3. **没拿到就说为什么**：`reason` 原样呈现，不翻译成「未找到」——
       「端点配错了」与「网上也没有」要人做的事完全不同。
    """
    if not leg.enabled:
        return ""
    lines = ["—— 在线检索（外部材料，未核实）——"]
    if not leg.snippets:
        lines.append(f"  没取到：{leg.reason}")
        return "\n".join(lines)
    if leg.endpoint_host:
        lines.append(f"  已发往 {leg.endpoint_host}（查询原文离开了本机）")
    for index, item in enumerate(leg.snippets, start=1):
        lines.append(f"  {index}. {item.headline}")
        if item.url and item.url != item.headline:
            lines.append(f"     {item.url}")
        if item.snippet:
            lines.append(f"     {item.snippet}")
    return "\n".join(lines)


def render_roles(roles: list[Role], names: dict[str, str] | None = None) -> str:
    lines = ["角色脉络："]
    if not roles:
        lines.append("（还没有角色。直接说一句话，我会问你是哪个脉络）")
        return "\n".join(lines)
    for role in roles:
        mark = "" if role.active else "  [静置]"
        dod = (
            f"｜默认完成标准：{role.default_definition_of_done}"
            if role.default_definition_of_done
            else ""
        )
        note = f"｜{role.note}" if role.note else ""
        merged = f"｜已合并进 {names[role.merged_into]}" if role.merged_into and names else ""
        lines.append(f"  · {role.name}{mark}{note}{dod}{merged}")
        lines.append(f"    id={role.id}")
    return "\n".join(lines)


def render_role_detail(role: Role, tasks: list[Task], names: dict[str, str]) -> str:
    lines = [f"角色：{role.name}"]
    if role.note:
        lines.append(f"  沉淀：{role.note}")
    if role.default_definition_of_done:
        lines.append(f"  默认完成标准：{role.default_definition_of_done}")
    if role.merged_into:
        lines.append(f"  已合并进：{names.get(role.merged_into, role.merged_into)}")
    lines.append("")
    if not tasks:
        lines.append("（这个角色下还没有事务）")
    else:
        for task in tasks:
            lines.append(
                f"  · {task.title}  [{task.kind.label} · {task.state.label}]  id={task.id}"
            )
    return "\n".join(lines)


def render_task(view: RestoreView, names: dict[str, str]) -> str:
    """恢复契约的人读版本。"""
    task = view.task
    lines = [f"{task.title}  ·  {task.kind.label} · {task.state.label}"]
    lines.append(f"角色：{'、'.join(names.get(rid, '?') for rid in task.role_ids)}")
    lines.append(f"意图：{task.intent or '（未澄清）'}")
    lines.append(f"生效完成标准：{view.effective_definition_of_done or '（未声明）'}")
    if view.waiting_on is not None:
        lines.append(f"等候：{view.waiting_on.describe()}")
    if view.current_artifact is not None:
        art = view.current_artifact
        label = _ARTIFACT_LABELS.get(art.status.value, art.status.value)
        lines.append(f"当前稿：version {art.version}（{label}）")
        for line in _strip_html_comments(art.content).splitlines()[:12]:
            lines.append(f"    {line}")
    if view.progress_note:
        lines.append(f"进度：{view.progress_note}")
    if view.recent_records:
        # 「恢复上下文」是每轮例行动作，不是进展——6 条记录里 4 条是它，
        # 把真正的产出（草稿、失败原因）淹没了。过滤掉，只留有信息量的。
        meaningful = [r for r in view.recent_records if r.type is not RecordType.RESUME]
        if meaningful:
            lines.append("最近记录：")
            for record in meaningful[-8:]:
                lines.append(f"  · {record.ts:%m-%d %H:%M} {record.content}")
    lines.append("下一步：")
    for action in view.next_actions:
        lines.append(f"  · {action}")
    return "\n".join(lines)


def render_digest(digest: ReminderDigest) -> str:
    return digest.text() or ""


def render_daily_digest(
    day: str,
    *,
    today_items: Iterable[ScoredTask],
    today_count: int,
    pending_count: int,
    nudge_count: int,
    overdue_count: int,
    cares: Iterable[CareDue] = (),
) -> str:
    """渲染每日「该看总览」（V1.60）。

    **只摆事实、不替用户决定**：报数 + 关键标题，具体详情去 Web 看。
    刻意克制 —— 今天排期最多列前 3 条标题，其余用计数兜住；总览是
    「知道今天有没有事」的入口，不是让用户在这里把一天过完。
    ``cares``（V1.68）是今天到期的**人生角色关注面** —— 只报「该看看」，
    是持续责任不是待办（看完推到下周期，不因做了而消失）。
    """
    lines = [f"该看 · {day}（今天）"]
    if today_count:
        titles = [s.task.title for s in list(today_items)[:3]]
        shown = "、".join(titles)
        extra = f" …共 {today_count} 件" if today_count > len(titles) else ""
        lines.append(f"今天排了 {today_count} 件：{shown}{extra}")
    if pending_count:
        lines.append(f"待处理 {pending_count} 条（未澄清的收件，/pending 或 Web 待处理看）")
    if nudge_count:
        lines.append(f"该催 {nudge_count} 件（跟人 follow_up 已过或承诺逾期，/nudge 或 Web 催办看）")
    if overdue_count:
        lines.append(f"承诺逾期 {overdue_count} 条（我欠对方，/promise 或 Web 承诺看）")
    cares = list(cares)
    if cares:
        lines.append(
            "该看看（人生角色关注面）："
            + "；".join(f"{a.area.text}（{a.role_name}）" for a in cares)
        )
    lines.append("系统只摆事实，不替你决定先做哪个。详情点开 Web 看。")
    return "\n".join(lines)


_CARE_WEEKDAY_LABELS = {
    "1": "一", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "日",
}


def _cadence_label(cadence: str) -> str:
    """把 ``weekly:5`` / ``monthly:1`` 翻回人话（列表给用户看）。

    ``weekly:6`` → 「每周六」（每 + 周 + 六，不是「每周周六」）。
    """
    kind, _, value = cadence.partition(":")
    if kind == "weekly":
        return f"每周{_CARE_WEEKDAY_LABELS.get(value, value)}"
    if kind == "monthly":
        return f"每月{int(value)}号"
    return cadence


def render_cares(items: Iterable[CareDue], now: datetime | None = None) -> str:
    """人生角色关注面列表（11.24 / V1.68）。

    每个角色名下「我该周期性关心的东西」。刻意不催、不判 —— 只摆
    「角色 · 关注面 · 节奏 · 下次该看」，看完由用户自己 ``/care done``。
    """
    if not items:
        return "还没有角色关注面 —— 给某个角色记一个「该周期性关心的东西」，如：/care 爸爸 孩子的作业和状态 每周六"
    lines = ["人生角色关注面（该周期性关心的东西）："]
    for i, due in enumerate(items, 1):
        a = due.area
        lines.append(f"{i}. 每周照看 · {_cadence_label(a.cadence)} 该看：{a.text}（{due.role_name}）")
    lines.append("/care done <序号> 记「看过了」，会推到下个周期。关注面不是待办，不会做完就消失。")
    return "\n".join(lines)


def render_suggestion(text: str) -> str:
    return f"排期参考（{DISCLAIMER}）：{text}"


def render_weigh(result: WeighResult | None) -> str:
    """代价对照（设计文档 7.5 / 20.1.14）。

    **只摆，不建议** —— 三条纪律都在措辞里：

    * **陈述句**：不出现「你应该」「建议」「最好」。方案是「一件事 + 它的代价」，
      不是「推荐你做 X」（``delegate.py:551``：指令类语句是自动决策的前一步）。
    * **代价带出处**：每项代价后面标它的来源列，主人可以自己核对
      （``engine_errors.py:39``：宁可给不出，也不给错）。
    * **不给排序**：两件事并列，**不标哪个更该做** —— 那是筛选层（决定
      看不看得到），有损。摆出来的两件都还在队列里。

    ``result`` 为 ``None`` = 没有真取舍。这**必须如实说**，
    不能降格成「那就先做 X」（那是把没有取舍说成有建议）。
    """
    if result is None:
        return (
            "现在没有需要你拍板的取舍 —— "
            "没有两件事落在同一天（一天只有一份力气，争用才成立）。"
        )

    lines = [
        "需要你拍板（只摆代价，不替你决定）：",
        "",
        f"争的是同一件事：{result.conflict_text}。",
        f"代价的性质：{result.because}。",
        "",
    ]
    for i, opt in enumerate(result.options, 1):
        lines.append(f"{i}. {opt.task.title}")
        for facet in opt.facets:
            lines.append(f"   · {facet.text}（来源：{facet.source}）")
        lines.append("")
    lines.append("这几件都还在队列里 —— 先动哪件由你定。")
    lines.append(f"选定了就记下来：{_WEIGH_PICK_HINT}")
    return "\n".join(lines)


#: ``/weigh`` 之后怎么拍板的提示语。
#:
#: **刻意用「记下来」而不是「选 X」** —— 它要表达的是「这一板会被记住，
#: 到期会拿出来跟现在对质」，而不只是「标记一下做过了」。
#: 后者听起来像归档，而归档没人回看。
_WEIGH_PICK_HINT = "/weigh <序号> 把这一板记下来（7 天后会拿出来跟现在对质）"


def render_weigh_picked(
    result: WeighResult, chosen_index: int, *, is_new: bool = True
) -> str:
    """记完一板之后的回执（设计文档 7.6）。

    **只报事实**：选了哪个、当时为什么是取舍、什么时候会拿出来对质。
    **不说「好选择」也不说「记下了，以后会更有针对性」** ——
    那是评价与承诺，本仓的呈现层不做这两件事。

    ``chosen_index`` 是 **0 基**（与 :meth:`DecisionService.record` 一致）。

    ``is_new=False`` 表示**今天已经拍过同一块板、选了同一个** ——
    库里没有多出一行（幂等规则见 7.6.6）。

    **回执必须把这两种情况说成不同的话**：都说成「记下了」，
    就把「今天已经记过，还是那一条」说成了「刚记了一条」——
    库里其实没有新行，人却以为多了一条。那是把事实说错。
    """
    if is_new:
        header = "这一板记下了（只摆事实，不评价）："
    else:
        header = "这一板今天已经记过 —— 还是那一条，日志里没有多出一行："
    lines = [
        header,
        "",
        f"争的是同一件事：{result.conflict_text}。",
        f"代价的性质：{result.because}。",
        "",
    ]
    for i, opt in enumerate(result.options, 1):
        mark = " ← 你选的" if i == chosen_index + 1 else ""
        lines.append(f"{i}. {opt.task.title}{mark}")
        for facet in opt.facets:
            lines.append(f"   · {facet.text}（来源：{facet.source}）")
        lines.append("")
    lines.append("记的是当时那一板 —— 7 天后会拿出来，跟这几件事现在的状态并排摆给你看。")
    return "\n".join(lines)


def render_decisions(
    confrontations: Sequence[Confrontation],
    *,
    today: date,
) -> str:
    """到期对峙：当时的快照 + 现在的状态，并排（设计文档 7.6）。

    **只摆事实，不评价** —— 它**不**说「你选对了」也不说「你选错了」。
    判断留给主人自己：系统能摆清「当时怎么想的、现在各自什么状态」，
    但「这个判断好不好」是人的事（7.1 / 〇.8.4）。

    空列表时如实说「没有到期的取舍记录」，**不降格成「那先做 X」**。
    """
    if not confrontations:
        return (
            "没有到期的取舍记录 —— "
            "拍过板、且到对质日的那几条才会出现在这里。"
        )

    lines = [f"到期的取舍 · {today}（当时那板 vs 现在）", ""]
    for c in confrontations:
        decided = c.decision.decided_at
        lines.append(
            f"· {decided:%Y-%m-%d} 拍板：{c.decision.conflict_text}"
        )
        lines.append(f"  当时的理由：{c.decision.reason_text}")
        if c.chosen is not None:
            lines.append(f"  当时你选的是：{c.chosen.title}")
            for text, source in c.chosen.facets:
                lines.append(f"    · {text}（来源：{source}）")
        lines.append("  现在：")
        for line in c.current_lines:
            lines.append(f"    {line}")
        for title in c.vanished_titles:
            lines.append(f"    {title} —— 已经不在库里了")
        lines.append("")
    lines.append("只摆事实 —— 当初怎么想的、现在各自什么状态。你自己看。")
    return "\n".join(lines)


def render_decision_history(decisions: Sequence[DecisionLog]) -> str:
    """决策日志一览（只列，不展开对峙）。

    **刻意只列一行一条** —— 这是「我拍过哪些板」的索引，
    展开对峙是 :func:`render_decisions` 的活。
    """
    if not decisions:
        return "还没有拍过板 —— /weigh 摆出取舍后，用 /weigh <序号> 记下来。"
    lines = [f"拍过板 {len(decisions)} 次", ""]
    for d in decisions:
        lines.append(
            f"· {d.decided_at:%Y-%m-%d %H:%M} —— {d.conflict_text}"
            f"（{d.reason_text}）"
        )
    return "\n".join(lines)


def to_task_ref(task: Task) -> TaskRef:
    return TaskRef(
        id=task.id,
        title=task.title,
        intent=task.intent,
        kind=task.kind.value,
        definition_of_done=task.definition_of_done,
    )


def to_signal_refs(scored: ScoredTask) -> list[SignalRef]:
    return [
        SignalRef(code=s.code.value, weight=s.weight, reason=s.reason)
        for s in scored.signals
    ]
