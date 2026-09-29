"""基本功能**跨入口**端到端验证。

为什么要这个
------------
单测各测各的，绿了也说明不了「同一个功能从三个入口进去是一回事」。
而今天已经栽过两次**路径分叉**：Repl 自己判意图、Web 走 ChatService，
两者对「问句」给出完全不同的答案；修好一个，另一个还坏着。

所以这里不测「函数对不对」，只测一件事：

    **同一句话，从终端 / Web / 飞书三个入口进去，数据结果与答复必须一致。**

数据结果一致是硬条件（那是真源）；答复文本允许飞书被裁剪（``_clip``），
所以按前缀比。

这不是测试，是**取证**。它输出的是矩阵，不是通过/失败。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from freeagent.app import build_app                      # noqa: E402
from freeagent.cli.app import Repl                       # noqa: E402
from freeagent.domain import TaskKind, WaitingKind, WaitingOn  # noqa: E402
from freeagent.services.channel import ChannelService    # noqa: E402
from freeagent.services.clock import FrozenClock         # noqa: E402
from freeagent.web.server import create_server           # noqa: E402
from freeagent.web.session import SESSION_HEADER, SESSION_TOKEN  # noqa: E402

NOW = datetime(2026, 9, 26, 14, 0)          # 周六
SENDER = "ou_tester"
CHAT = "oc_tester"


# =============================================================================
# 三个入口，各自语义不同但应当**结果一致**
# =============================================================================
def drive_cli(app, text: str) -> str:
    out = io.StringIO()
    Repl(app, out=out)._dispatch(text)
    return out.getvalue()


def drive_feishu(app, text: str) -> str:
    ch = ChannelService(app, allowed_senders=frozenset({SENDER}))
    reply = ch.handle(CHAT, SENDER, text, event_id=uuid.uuid4().hex)
    return reply.text


def serve(app):
    server = create_server(app, "127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"127.0.0.1:{server.server_address[1]}"


def drive_web(addr: str, text: str) -> tuple[str, list[str]]:
    """真 HTTP。返回 (text, item 标题列表)。

    **必须把 items 一起带回来**：Web 把结构化数据放在 ``items`` 里、由界面
    渲染，``text`` 只是个摘要。第一版只断言 ``text``，于是「Web 没回标题」
    被我误判成 bug —— 实际那是设计如此，界面会渲染出来。
    """
    conn = HTTPConnection(*addr.split(":"), timeout=10)
    try:
        body = json.dumps({"text": text, "last_items": []}).encode()
        conn.request("POST", "/api/chat", body=body, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            SESSION_HEADER: SESSION_TOKEN,
        })
        res = conn.getresponse()
        raw = res.read().decode("utf-8")
        if res.status != 200:
            return f"HTTP {res.status}: {raw[:200]}", []
        data = json.loads(raw)
        items = [str(i.get("title", "")) for i in (data.get("items") or [])]
        return str(data.get("text", raw)), items
    finally:
        conn.close()


#: 播种之后库里固定 4 条（今天 2、已结束 1、未排期提醒 1）。
#: 断言用**增量**，否则每加一条种子数据就要改一遍全部期望值。
BASELINE = 4

DRIVERS = {"CLI": drive_cli, "飞书": drive_feishu, "Web": None}   # Web 单独起服务


# =============================================================================
# 基本功能集。``expect_writes`` 是硬条件，``expect_in`` 查答复里该出现什么。
# =============================================================================
class Case:
    def __init__(self, name, text, *, expect_delta=0, expect_items=(),
                 expect_in=(), must_not=()):
        self.name = name
        self.text = text
        self.expect_delta = expect_delta        # 相对播种基线的**增量**
        self.expect_items = expect_items        # 答复里必须回来的事务标题
        self.expect_in = expect_in              # 答复里必须出现的原话（如自报提示）
        self.must_not = must_not


#: 播种基线：4 条（今天 2、已结束 1、提醒 1 未排期）
CASES = [
    Case("记事：白话建事务", "下周二要交的销售周报初稿",
         expect_delta=+1, expect_items=("销售周报",)),

    Case("提问：今天视图", "今天该做什么？",
         expect_items=("季度述职材料",)),

    Case("提问：全部（名词短语，今天修过的坑）", "全部未结束的事",
         expect_items=("季度述职材料", "等客户法务回复合同")),

    Case("提问：在等什么", "我在等什么",
         expect_items=("等客户法务回复合同",)),

    Case("提问：有什么提醒", "有什么提醒",
         expect_items=("吃药",)),

    Case("提问：这周做完了什么", "这周做完了什么",
         expect_items=("交完物业费",)),

    Case("提问：表外问句（须自报家门）", "接下来该关注什么",
         expect_items=("季度述职材料",), expect_in=("没听懂",)),

    Case("拒绝：改已有事务", "把周报改到下周三",
         expect_in=("改已有",)),

    Case("拒绝：追问时答非角色名", "qqqqzzz完全不相关的一件事",
         expect_in=("脉络",)),
]

#: 旧话术的指纹 —— 今天修掉的 bug 只要复发就必然出现
DEFLECTION = ("我只会做两件事", "查看用命令")


def fresh(tag: str, case: Case):
    """每个入口一份**干净但有内容**的库。

    刻意**先播种再问**。第一版没播种，于是「全部」「在等什么」这些视图
    全都只走**空态**分支 —— 而空态只能证明「没崩」，证不了「查对了东西」。
    空库下「没有未结束的事务」和「查错了表但也是空」输出**完全一样**。
    """
    home = Path(tempfile.mkdtemp())
    app = build_app(home / f"{tag}.db", clock=FrozenClock(NOW))
    work = app.roles.create("工作项目A", note="销售 周报 客户")
    app.roles.create("家庭", note="孩子 学校 材料")

    # 播下三件**能互相区分**的事：今天要做的 / 卡住的 / 上周做完的。
    # 视图答对答错，看它回的是不是对应的那一条就行。
    t_today = app.tasks.create("季度述职材料", [work.id])
    app.tasks.schedule(t_today.id, app.clock.today())

    t_wait = app.tasks.create(
        "等客户法务回复合同", [work.id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(WaitingKind.PERSON, "客户法务", NOW),
    )
    app.tasks.schedule(t_wait.id, app.clock.today())

    t_done = app.tasks.create("交完物业费", [work.id])
    app.tasks.schedule(t_done.id, app.clock.today())
    app.tasks.complete(t_done.id)          # 今天完成的 → 属「本周已结束」

    t_remind = app.tasks.create("吃药", [work.id], kind=TaskKind.REMINDER,
                                reminder_time=NOW.replace(hour=23))
    return app


def titles(app) -> list[str]:
    return sorted(t.title for t in app.tasks.list_all())


def run_case(case: Case) -> dict:
    """三个入口各建一份干净环境跑同一句，比结果。"""
    row = {"case": case.name, "text": case.text, "entries": {}}

    # Web 单独起一次真服务（端口 0，三个用例互不冲突）
    web_app = fresh("web", case)
    server, addr = serve(web_app)
    try:
        for label, drive in (("CLI", drive_cli), ("飞书", drive_feishu)):
            app = fresh(label, case)
            try:
                text, items = drive(app, case.text), []
                row["entries"][label] = {
                    "titles": titles(app), "reply": text,
                    "items": items, "error": None,
                }
            except Exception as exc:                      # 入口崩了也要记下来
                row["entries"][label] = {
                    "titles": titles(app), "reply": "", "items": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
        try:
            wtext, witems = drive_web(addr, case.text)
            row["entries"]["Web"] = {
                "titles": titles(web_app), "reply": wtext,
                "items": witems, "error": None,
            }
        except Exception as exc:
            row["entries"]["Web"] = {
                "titles": titles(web_app), "reply": "", "items": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
    finally:
        server.shutdown()
        server.server_close()

    row["ok"] = judge(row, case)
    return row


def blob(got: dict) -> str:
    """答复的**全部可见内容**：正文 + 结构化条目标题。

    Web 把事务标题放在 ``items`` 里（界面渲染），CLI/飞书直接印在正文里。
    只比正文会把「Web 没回标题」误判成 bug，所以合成一个 blob 再比。
    """
    return got["reply"] + " " + " ".join(got.get("items") or [])


def judge(row: dict, case: Case) -> list[str]:
    """返回问题清单；空 = 通过。"""
    bad: list[str] = []
    entries = row["entries"]
    for label, got in entries.items():
        if got["error"]:
            bad.append(f"{label} 抛异常：{got['error']}")
            continue
        # 增量：播种后是 BASELINE 条
        n = len(got["titles"])
        want = BASELINE + case.expect_delta
        if n != want:
            bad.append(f"{label} 事务数 {n}（期望 {want}）：{got['titles']}")
        seen = blob(got)
        for needle in case.expect_items:
            if needle not in seen:
                bad.append(f"{label} 答复里没有 {needle!r}：{seen[:70]!r}")
        for needle in case.expect_in:
            if needle not in seen:
                bad.append(f"{label} 答复缺 {needle!r}：{seen[:70]!r}")
        for needle in DEFLECTION + tuple(case.must_not):
            if needle in seen:
                bad.append(f"{label} 答复仍含 {needle!r}（旧话术没删干净）")

    # 三入口数据必须一致
    title_sets = {l: tuple(g["titles"]) for l, g in entries.items() if not g["error"]}
    if len(set(title_sets.values())) > 1:
        bad.append(f"三入口数据不一致：{title_sets}")
    return bad


def main() -> int:
    print("=" * 78)
    print("基本功能 · 跨入口端到端验证（终端 / Web 真 HTTP / 飞书 ChannelService）")
    print("=" * 78)

    rows = []
    for case in CASES:
        row = run_case(case)
        rows.append(row)
        mark = "OK  " if not row["ok"] else "FAIL"
        print(f"\n{mark} {case.name}   「{case.text}」")
        if row["ok"]:
            for label, got in row["entries"].items():
                print(f"       {label:<4} 事务{len(got['titles'])}条  "
                      f"答复首行={got['reply'].strip().splitlines()[0][:44] if got['reply'].strip() else '（空）'!r}")
        else:
            for label, got in row["entries"].items():
                print(f"       {label:<4} 事务={got['titles']}  "
                      f"答复={got['reply'].strip()[:60]!r}"
                      + (f"  [{got['error']}]" if got['error'] else ""))
            for b in row["ok"]:
                print(f"       → {b}")

    npass = sum(1 for r in rows if not r["ok"])
    print("\n" + "=" * 78)
    print(f"结果：{npass}/{len(rows)} 个用例三入口一致")
    print("=" * 78)

    if npass != len(rows):
        print("\n不一致明细（JSON）：")
        print(json.dumps([r for r in rows if r["ok"]], ensure_ascii=False, indent=2))
    return 0 if npass == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
