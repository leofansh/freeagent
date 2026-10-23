"""视图渲染。

两条硬性要求（设计文档第七章）：
* 每条排序信号必须把**理由**一起展示；
* 输出必须标注「启发式提示，不是评分」。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from ..domain import (
    CONTACT_KIND_LABELS,
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
        lines.append("今天没有排进来的事务。直接说一句话就能新建。")
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

    lines.append("")
    lines.append("这些是**事实**，不是建议 —— 要改什么，由你决定。")
    return "\n".join(lines)


def render_model(view, names: dict[str, str]) -> str:
    """连续性个人模型（11.17 / 20.1.1）。

    刻意**不排序打分、不给建议**：只把从记录里现推的规律摆出来
    （第七章「摆得更清楚 ≠ 替你决定」）。与 /week、/feedback 同一条纪律。
    ``names`` 保留签名一致，角色名已在服务层翻好。
    """
    days = (view.until - view.since).days + 1
    lines = [f"连续性个人模型 · 近 {days} 天（{DISCLAIMER}）"]
    if view.is_empty:
        lines.append("这段时间还没有可归纳的记录 —— 先记几件事、完成几件，规律才会显现。")
        return "\n".join(lines)

    lines.append("")
    if view.done_in_window:
        lines.append(f"完成 {view.done_in_window} 件，按**时段**分布：")
        for hour, count in view.completion_by_hour:
            lines.append(f"  {hour:02d}:00  {count:>3}  {'▏' * count}")
        lines.append("按**星期**分布：")
        for dow, count in view.completion_by_dow:
            lines.append(f"  周{_WEEKDAY_CN[dow]}   {count:>3}  {'▏' * count}")
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

    lines.append("")
    lines.append("这些是**从你的记录里现推的事实**，不是评价 —— 要不要顺着它调整，由你决定。")
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
) -> str:
    """渲染每日「该看总览」（V1.60）。

    **只摆事实、不替用户决定**：报数 + 关键标题，具体详情去 Web 看。
    刻意克制 —— 今天排期最多列前 3 条标题，其余用计数兜住；总览是
    「知道今天有没有事」的入口，不是让用户在这里把一天过完。
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
    lines.append("系统只摆事实，不替你决定先做哪个。详情点开 Web 看。")
    return "\n".join(lines)


def render_suggestion(text: str) -> str:
    return f"排期参考（{DISCLAIMER}）：{text}"


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
