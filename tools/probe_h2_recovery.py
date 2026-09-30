"""量一量：H2 到底成立吗？—— 从打开事务到拿到下一步动作要几轮交互。

这不是测试。跑法：

    set PYTHONIOENCODING=utf-8
    python tools\\probe_h2_recovery.py

**它量的是产品假设，不是代码对不对。** 代码有测试管着；这里要回答的是
「设计文档 14.2 写的那个数字，现状够不够」。

## 场景 S5 的定义是本探针给的

设计文档 14.2 反复引用「场景 S5（跨角色中断恢复）」，但**全文没有 S5 的
定义** —— 14.1 只列了 H1/H2 两句假设，8.3 只重复「交互轮数 ≤ 2」这个判据。
一份规范引用了一个不存在的场景编号却没人发现，这本身就是个问题。

所以这里**定义**它，并把定义写进输出，让读的人知道我在测什么：

    S5 = 跨角色 + 中断（有历史、不是新建的空壳）+ 靠 /task 单条命令就能恢复

三条同时成立才算一个 S5 样本。

## 「一轮」怎么数

一轮 = **一次用户输入 → 系统回应**。`/task <id>` 是一轮，之后如果还需要
再问一次别的（`/artifact` 看全文、`/roles` 认角色），那是第二轮。

判「拿到可执行的下一步」而不是「看到点文字」：`next_actions` 非空、
且**不是**「没有待处理的下步骤」那种敷衍话、且给出的建议**指向一个真实存在
的下一步**（有具体对象或具体动作，不是泛泛而谈）。
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from freeagent.app import build_app                       # noqa: E402
from freeagent.cli.app import Repl                        # noqa: E402
from freeagent.cli import render                          # noqa: E402
from freeagent.domain import (                            # noqa: E402
    TaskKind,
    TaskState,
    WaitingKind,
    WaitingOn,
)
from freeagent.services.clock import FrozenClock          # noqa: E402

NOW = datetime(2026, 9, 30, 14, 0)
LONG_DRAFT = 20          # 故意超过 render_task 印的 12 行
THRESHOLD = 2            # 设计文档 8.3 / 14.2 定的判据


def env(tag: str):
    """一套干净但有内容的库：3 个角色 + 若干有历史的事务。"""
    home = Path(tempfile.mkdtemp())
    app = build_app(home / f"{tag}.db", clock=FrozenClock(NOW))
    work = app.roles.create("工作项目A", note="销售 周报 客户")
    family = app.roles.create("家庭", note="孩子 学校 材料")
    return app, work, family


def s1_wait_followup_due():
    """跨角色 · 等候中，跟进时间已过。"""
    app, work, family = env("s1")
    # WAIT 类在 create 时就被置为 BLOCKED 且**当场**要求 waiting_on
    # （validate_task：等候类事务必须提供 waiting_on），所以不能建完再补。
    t = app.tasks.create(
        "等客户法务回复合同", [work.id, family.id],
        kind=TaskKind.WAIT,
        waiting_on=WaitingOn(WaitingKind.PERSON, "客户法务", NOW - timedelta(days=9),
                             follow_up_at=NOW - timedelta(days=2)),
    )
    app.tasks.note(t.id, "上周三催过一次，对方说在走内部流程")
    return app, t.id, "等客户法务回复合同"


def s2_draft_not_started():
    """跨角色 · 草稿已起但事务还在 inbox（没开始做）。"""
    app, work, family = env("s2")
    t = app.tasks.create("整理客户名单", [work.id, family.id])
    app.artifacts.create_draft(t.id, "客户名单", "1. 老客户\n2. 新客户\n3. 待跟进")
    return app, t.id, "整理客户名单"


def s3_long_draft_started():
    """跨角色 · 已开始 + **长草稿**（20 行 > 渲染的 12 行）。

    这个是最容易超标的：下一步会说「继续改还是重写」，但用户得先看到全文
    才知道改哪 —— 而 render_task 只印前 12 行。
    """
    app, work, family = env("s3")
    t = app.tasks.create("销售周报初稿", [work.id, family.id])
    app.artifacts.create_draft(
        t.id, "销售周报",
        "\n".join(f"{i}. 第{i}周进展：客户反馈与下周计划" for i in range(1, LONG_DRAFT + 1)),
    )
    app.tasks.start(t.id)
    app.tasks.note(t.id, "已跟王工确认过口径，按「结论先行」写")
    return app, t.id, "销售周报初稿"


def s4_no_draft_started():
    """跨角色 · 已开始但没有草稿。"""
    app, work, family = env("s4")
    t = app.tasks.create("季度述职材料", [work.id, family.id])
    app.tasks.start(t.id)
    app.tasks.note(t.id, "提纲列了三条，还没动笔")
    return app, t.id, "季度述职材料"


def s5_overdue_not_rolled():
    """跨角色 · 原定日期已过。

    顺带验一件事：``next_actions`` 说「已顺延」，但 ``/task`` **不触发
    rollover**（只有 ``today.view()`` 触发）。所以这句话可能是假话。
    """
    app, work, family = env("s5")
    t = app.tasks.create("交物业费", [work.id, family.id])
    app.tasks.schedule(t.id, NOW.date() - timedelta(days=3))
    app.tasks.start(t.id)
    return app, t.id, "交物业费"


def s6_no_dod():
    """跨角色 · 缺完成标准（两个角色都没设默认，事务自己也没设）。"""
    app, work, family = env("s6")
    t = app.tasks.create("修窗户螺丝", [work.id, family.id])
    app.tasks.start(t.id)
    app.tasks.note(t.id, "螺丝买了，工具没有")
    return app, t.id, "修窗户螺丝"


SCENARIOS = [
    ("S5-1", "等候 · 跟进已过", s1_wait_followup_due),
    ("S5-2", "inbox · 有草稿没开始", s2_draft_not_started),
    ("S5-3", "进行中 · 长草稿 20 行", s3_long_draft_started),
    ("S5-4", "进行中 · 无草稿", s4_no_draft_started),
    ("S5-5", "原定日期已过", s5_overdue_not_rolled),
    ("S5-6", "缺完成标准", s6_no_dod),
]


def ask(app, line: str) -> str:
    """跑一条命令，返回它的输出（走真实 Repl 渲染，不是直接调 service）。

    刻意走 CLI 而不是直接读 ``RestoreView`` —— 判据是**用户看到几轮**，
    看到的必须是渲染后的东西。直接读 dataclass 会把「渲染时丢了信息」
    这类问题算成通过。
    """
    out = io.StringIO()
    repl = Repl(app, out=out)
    repl.handle(line, check_reminders=False)
    return out.getvalue()


#: 「没有待处理的下一步」不算拿到动作 —— 那是没给出下一步。
NULL_NEXT = "按你自己的节奏"


def count_rounds(text: str) -> tuple[int, list[str]]:
    """数从这份输出到「可执行的下一步」要几轮。

    返回 (轮数, 依据)。这里只做**能机械判定**的部分：

    - 轮 1 = ``/task`` 本身
    - 若 ``/task`` 的输出里**没有**足够信息、需要再问一条命令（``/artifact``
      看全文、``/roles`` 对角色），则 +1

    「够不够」由 :func:`next_actions` 的质量判定，不靠猜。
    """
    m = re.search(r"下一步：\n((?:\s+·.*\n?)+)", text)
    actions = re.findall(r"·\s*(.+)", m.group(1)) if m else []
    real = [a for a in actions if NULL_NEXT not in a]
    if not real:
        return 1, actions           # 1 轮：看到了，只是它说「没有下一步」

    # 「继续改还是重写」需要看到全文才改得动 —— 草稿超 12 行时 /task 只印前 12 行
    need_more = any("继续改" in a or "重写" in a for a in real)
    if need_more and _draft_truncated(text):
        return 2, actions
    return 1, actions


def _draft_truncated(text: str) -> bool:
    """渲染出来的稿子是不是被截断了（没印出「还有更多」）。"""
    return "当前稿" in text and not re.search(r"还有 \d+ 行|共 \d+ 行", text)


def main() -> int:
    print("=" * 78)
    print("H2 探针 · 场景 S5「跨角色中断恢复」")
    print("  判据（设计文档 8.3 / 14.2）：从打开事务到拿到可执行的下一步 ≤ "
          f"{THRESHOLD} 轮")
    print("  S5 的定义：**本探针给的** —— 文档引用了 S5 但从未定义它")
    print("=" * 78)

    rows = []
    for key, name, factory in SCENARIOS:
        app, task_id, title = factory()
        text = ask(app, f"/task {task_id}")
        rounds, actions = count_rounds(text)
        rows.append((key, name, title, rounds, actions, text, app, task_id))
        flag = "OK  " if rounds <= THRESHOLD else "OVER"
        print(f"\n{flag} {key} {name}  →  **{rounds} 轮**")
        print(f"     事务：{title}")
        for a in actions:
            print(f"     · {a}")
        if not actions:
            print("     （!! 没解析出「下一步」段 —— 渲染变了？）")

    # ---- 汇总 ----------------------------------------------------------- #
    over = [r for r in rows if r[3] > THRESHOLD]
    print()
    print("=" * 78)
    print(f"共 {len(rows)} 个场景，{len(over)} 个超过 {THRESHOLD} 轮")
    if over:
        for key, name, _, rounds, _, _, _, _ in over:
            print(f"  · {key} {name}：{rounds} 轮")
        print()
        print(f"**H2 未达标。** 按 14.3，这属于「恢复契约信息量不足」，")
        print("对策是补上下文投影 —— 而不是把阈值改大。")
    else:
        print(f"**H2 在这 {len(rows)} 个场景下达标。**")
        print("但注意：S5 是本探针定义的，场景数也只有 "
              f"{len(rows)} —— 这是「不反对」，不是「证明」。")
    print("=" * 78)

    # ---- 顺带验一件真话：那句「已顺延」是不是假话 ---------------------- #
    print()
    print("--- 附带检查：next_actions 说「已顺延」时，真的顺延了吗 ---")
    app, task_id, _ = s5_overdue_not_rolled()
    text = ask(app, f"/task {task_id}")
    claimed = "已顺延" in text
    today = app.clock.today().isoformat()
    actual = next((t.scheduled_for for t in app.tasks.list_all()
                   if t.id == task_id), None)
    really = str(actual) == today
    print(f"  文案说已顺延：{claimed}")
    print(f"  库里 scheduled_for={actual}，今天={today} → 实际顺延：{really}")
    if claimed and not really:
        print("  !! 文案说「已顺延」，但库里日期还是旧的 ——")
        print("     `/task` 不触发 rollover（只有 /today 触发），所以这句是**假话**。")
        print("     这与设计文档 3.6「截断必须报」、第 10 项「tail 回落标成 tail "
              "而不是 bm25」是同一类毛病：断言比事实强。")
    else:
        print("  ok 文案与库一致")

    return 1 if over else 0


if __name__ == "__main__":
    raise SystemExit(main())
