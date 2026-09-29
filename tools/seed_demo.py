"""演示数据：用**真实服务层**灌一份，隔离在 data/demo.db。

为什么用真实服务层
------------------
手工往 SQLite 里塞 JSON 会绕过所有不变式，造出服务层本身造不出来的数据。
那样演示的就不是真东西了。

日期全部**相对当前时间**
----------------------
之前这个脚本写死了 2026-09-26，于是过了一天数据就过期：排期变成昨天、
提醒变成「已错过」，而那两个一次性的效果（顺延标记、🔔 提醒条）
早就被验证过程消耗掉了 —— 打开界面啥也看不到。
改成相对时间后，任何时候重跑都是新鲜的。

只用 ``--db`` 指向这个库，不会碰你的真实数据。
"""

import argparse
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, "src")

from freeagent.app import build_app
from freeagent.domain import TaskKind, WaitingKind, WaitingOn
from freeagent.services.clock import FrozenClock


def seed(db_path: Path) -> tuple[int, int]:
    now = datetime.now().replace(second=0, microsecond=0)
    today = now.date()
    # 用冻clock 灌数据，让「相对今天」的语义确定；写完就恢复真实时间。
    app = build_app(db_path, clock=FrozenClock(now))
    try:
        _fill(app, now, today)
        n_roles = len(app.roles.list_roles(include_inactive=True))
        n_tasks = len(app.tasks.list_all())
    finally:
        app.close()
    return n_roles, n_tasks


def _fill(app, now: datetime, today: date) -> None:
    # --- 角色 ---------------------------------------------------------------
    work = app.roles.create(
        "工作项目A", note="销售 周报 客户 合同",
        default_definition_of_done="先给我能用的就行",
    )
    family = app.roles.create("家庭", note="孩子 学校 材料 物业")
    app.roles.create("个人创作", note="写作 灵感 选题 播客")
    chores = app.roles.create("跑腿杂项", note="修 买 交")
    legacy = app.roles.create("旧公司交接", note="交接 离职 社保")
    app.roles.set_active(legacy.id, False)          # 静置：演示静置分组

    # --- 20 天前开的、之后再没碰过（RESUME_STALE）---------------------------
    # 必须先建好且之后**不再改动**，否则 updated_at 一新信号就不触发了。
    app.clock.set(now - timedelta(days=20))
    stale = app.tasks.create(
        "整理竞品资料", [work.id],
        intent="把三家的定价页摘成一页对比，拿去谈判用",
        definition_of_done="一页表，含各家阶梯价与最低起订量",
        scheduled_for=today,
    )
    app.tasks.start(stale.id)
    app.tasks.note(stale.id, "摘完 A 家，B 家卡在要登录")

    # --- 其余都在「现在」之前 -------------------------------------------------
    app.clock.set(now - timedelta(minutes=30))

    # 等候 + 跟进日已过 → OVERDUE_WAIT +50（今天视图排第一）
    contract = app.tasks.create(
        "等客户法务回复合同条款", [work.id], kind=TaskKind.WAIT,
        intent="等对方法务把条款改完，我这边才能签",
        scheduled_for=today,
        waiting_on=WaitingOn(
            WaitingKind.PERSON, "客户法务",
            since=now - timedelta(days=5),
            follow_up_at=now - timedelta(days=2),   # 已过 → 该催了
            note="上次说周五给，没给",
        ),
    )
    app.tasks.note(contract.id, "周一又发了一封提醒邮件")

    # 明天到期 + 进行中 → DEADLINE_RISK +40
    report = app.tasks.create(
        "销售周报初稿", [work.id],
        intent="给老板看的一页纸，重点讲增长",
        scheduled_for=today,
        due_time=now + timedelta(hours=19),
    )
    app.tasks.start(report.id)
    app.tasks.note(report.id, "数据拉完了，结论还没写")

    # 提醒：1 小时前就该响 → 首屏会出现 🔔 摘要条。
    # 刻意不用「现在 + N 小时」：那样还没到点，演示时看不到任何提醒。
    app.tasks.create(
        "修窗户螺丝", [chores.id], kind=TaskKind.REMINDER,
        intent="别再忘了，螺丝掉了一地",
        scheduled_for=today,
        reminder_time=now - timedelta(hours=1),
    )

    # 排期已过且未结束 → 首屏带 ⟲顺延 标记
    app.tasks.create(
        "孩子周四要交的材料清单", [family.id],
        intent="清单打印好放进书包，别又忘",
        scheduled_for=today - timedelta(days=6),
    )

    # 被另一条事务挡住 → DEPENDED_ON +30
    waiting_property = app.tasks.create(
        "等物业通知交费金额", [family.id], kind=TaskKind.WAIT,
        intent="等物业发本年度缴费通知",
        waiting_on=WaitingOn(
            WaitingKind.SYSTEM, "物业公众号", since=now - timedelta(days=2),
            follow_up_at=now + timedelta(days=1),    # 还没到 → 不算逾期
        ),
    )
    app.tasks.create(
        "交物业费", [family.id], kind=TaskKind.REMINDER,
        intent="收到通知就交，别拖到催缴",
        scheduled_for=today,
        blocked_by=[waiting_property.id],
    )

    # 无信号的：同分时验证「先进先出」
    app.tasks.create(
        "读《置身事内》第三章", [app.roles.get_by_name("个人创作").id],
        intent="通勤时读，不求记住",
        scheduled_for=today,
    )

    # 没排期 → 不进今天视图，但「全部未结束」里能看到（区分这两个视图）
    app.tasks.create(
        "想一个播客选题", [app.roles.get_by_name("个人创作").id],
        intent="这期想聊「工具怎么反过来改变人」",
    )

    # 已结束视图的两条（其中一条是「上周」完成的，用来演示时间范围）
    app.clock.set(now - timedelta(days=5))
    last_week = app.tasks.create(
        "报销上周打车费", [work.id], intent="贴票，走报销流程",
        scheduled_for=today - timedelta(days=6),
    )
    app.tasks.start(last_week.id)
    app.tasks.complete(last_week.id)

    app.clock.set(now - timedelta(hours=2))
    done = app.tasks.create("买猫粮", [chores.id], intent="猫粮快没了")
    app.tasks.complete(done.id)

    # 静置角色下的一条
    app.tasks.create("社保转移手续", [legacy.id], intent="离职后要办")

    # --- 草稿：让详情页的「恢复契约」有真东西 -------------------------------
    app.clock.set(now - timedelta(minutes=20))
    app.artifacts.create_draft(
        stale.id, "竞品定价对比 v1",
        "# 竞品定价对比\n\n"
        "| 厂商 | 起步价 | 最低起订 |\n|---|---|---|\n"
        "| A | [TODO] | [TODO] |\n| B | [TODO] | [TODO] |\n| C | [TODO] | [TODO] |",
    )
    app.artifacts.revise(
        stale.id,
        "# 竞品定价对比\n\n"
        "| 厂商 | 起步价 | 最低起订 | 备注 |\n|---|---|---|---|\n"
        "| A | 99/月 | 1 席 | 已摘完 |\n"
        "| B | [TODO] | [TODO] | 卡在要登录 |\n"
        "| C | [TODO] | [TODO] | |\n\n"
        "谈判用的一句话结论：[TODO]",
        title="竞品定价对比 v2",
    )
    accepted = app.artifacts.create_draft(
        report.id, "销售周报初稿 v1",
        "# 销售周报\n\n## 一、结论\n[TODO] 本周增长主要来自……\n\n"
        "## 二、数据\n| 指标 | 本周 | 上周 |\n|---|---|---|\n| [TODO] | | |\n\n"
        "## 三、下一步\n[TODO]",
    )
    app.artifacts.accept(accepted.id)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成演示数据（隔离在 demo.db）")
    parser.add_argument("--db", type=Path, default=Path("data/demo.db"))
    args = parser.parse_args()

    if args.db.exists():
        args.db.unlink()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    n_roles, n_tasks = seed(args.db)
    print("演示库已生成：%s" % args.db.resolve())
    print("  基准时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("  角色 %d 个（含 1 个静置）｜事务 %d 条" % (n_roles, n_tasks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
