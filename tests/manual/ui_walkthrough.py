"""FreeAgent Web 界面逐页人工模拟走查（不依赖真机引擎）。

跑法（浏览器 venv，不跑 pytest）：

    .venv_browser\\Scripts\\python.exe tests\\manual\\ui_walkthrough.py
    HEADED=1 .venv_browser\\Scripts\\python.exe tests\\manual\\ui_walkthrough.py

前提：先在另一个终端起隔离库的 Web 服务，例如：

    set PYTHONPATH=src
    python -m freeagent.web --db .smoke/ui/agent.db --port 8811 --no-browser

范围：点遍导航里的全部视图，记录每个视图的正文长度 / 首段文案、控制台
error、pageerror、4xx/5xx 响应；对话页与工作台各发一句本地问题（规则层
可答，不起真引擎）。截图落 tests/manual/shots/walk/。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8811/"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHOT_DIR = REPO_ROOT / "tests" / "manual" / "shots" / "walk"
SMOKE_WORK = REPO_ROOT / ".smoke" / "ui" / "work"

console_msgs: list[str] = []
page_errors: list[str] = []
bad_responses: list[str] = []
view_report: list[dict] = []


def record_view(name: str, text: str) -> None:
    view_report.append({
        "view": name,
        "chars": len(text),
        "head": " ".join(text.split())[:140],
    })
    print(f"[视图] {name}: {len(text)} 字 — {' '.join(text.split())[:80]}")


def settle(page, timeout: float = 10.0) -> str:
    """等异步视图把「载入中…」换掉，且正文两轮稳定。"""
    deadline = time.time() + timeout
    prev = ""
    while time.time() < deadline:
        text = page.locator("#root").inner_text()
        # 空正文说明视图还在装配（根被清空、节点没挂上），不能当成空态定稿。
        if text.strip() and "载入中" not in text and text == prev:
            return text
        prev = text
        time.sleep(0.3)
    return page.locator("#root").inner_text()


def main() -> int:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    SMOKE_WORK.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        # HEADED=1 时弹出真实 Edge 窗口便于人工观察；默认无头。
        browser = p.chromium.launch(
            channel="msedge", headless=not os.environ.get("HEADED"),
            args=["--no-proxy-server"],
        )
        page = browser.new_page(viewport={"width": 1180, "height": 1000})
        page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on(
            "response",
            lambda r: bad_responses.append(f"{r.status} {r.url}")
            if r.status >= 400 else None,
        )

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(600)

        header = page.locator("#hdr").inner_text()
        print(f"[头部] {header}")

        views = page.eval_on_selector_all(
            "#nav button[data-view]",
            "bs => bs.map(b => b.dataset.view)",
        )
        print(f"[导航] {len(views)} 个视图：{', '.join(views)}")

        # V1.119 起低频视图收进「更多」下拉：下拉项默认隐藏，点之前先展开。
        primary = {"chat", "workbench", "today", "all"}
        for view in views:
            if view not in primary:
                page.click("#more-btn")
            page.click(f'button[data-view="{view}"]')
            text = settle(page)
            record_view(view, text)
            page.screenshot(path=str(SHOT_DIR / f"{view}.png"), full_page=True)

        # 对话页：先打一句招呼（问候类句子过去没测过 —— 曾把授权项目清单
        # 甩在「你好」的回复里），再发本地问题，等 POST 回包。
        page.click('button[data-view="chat"]')
        page.wait_for_selector("#chatlog", timeout=5000)
        page.fill("#chatinput", "你好")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        greet = ""
        try:
            greet = page.locator("#chatlog").inner_text()
        except Exception as e:
            print(f"[对话·问候] 读不到 chatlog：{e}")
        print(f"[对话·问候] 回复片段：{' '.join(greet.split())[:160]}")
        if "能改这" in greet:
            page_errors.append("问候语里又出现授权项目清单")
        page.screenshot(path=str(SHOT_DIR / "chat_greeting.png"), full_page=True)
        try:
            with page.expect_response(
                lambda r: r.url.endswith("/api/chat")
                and r.request.method == "POST",
                timeout=10000,
            ):
                page.fill("#chatinput", "今天我还有什么事")
                page.keyboard.press("Enter")
        except Exception as e:  # 回包没等到也要把现场留下
            print(f"[对话] 等回复超时：{e}")
        time.sleep(0.5)
        reply = ""
        try:
            reply = page.locator("#chatlog").inner_text()
        except Exception as e:
            print(f"[对话] 读不到 chatlog：{e}")
        page.screenshot(path=str(SHOT_DIR / "chat_reply.png"), full_page=True)
        print(f"[对话] 回复片段：{' '.join(reply.split())[:160]}")

        # 工作台：控件齐全 + 项目选择器 + 本地问题路由（不起真引擎）。
        page.click('button[data-view="workbench"]')
        page.wait_for_selector("#wb-project-btn", timeout=5000)
        ctrl = {
            sel: page.locator(sel).count()
            for sel in ["#wb-dir", "#wb-project-btn", "#wb-project-menu",
                        "#wb-model", "#wb-mode", "#wb-text",
                        "#wb-send", "#wb-stop", "#wb-route", "#wb-history"]
        }
        print(f"[工作台] 控件：{ctrl}")

        # 项目下拉：搜索框 / 已有项目 / 三个动作都在；浏览面板是二级兜底。
        page.click("#wb-project-btn")
        page.wait_for_selector("#wb-project-menu:not([hidden])", timeout=3000)
        for label in ("不使用项目", "新建项目", "选择本地文件夹"):
            assert page.locator(f'#wb-project-menu button:has-text("{label}")').count() == 1, label
        page.click('#wb-project-menu button:has-text("选择本地文件夹")')
        page.wait_for_selector("#wb-dirs:not([hidden])", timeout=3000)
        # 选用当前浏览层目录 = 直接导入建档（名取末段、备注空）。
        page.click('#wb-dirs button:has-text("选用此目录")')
        page.wait_for_timeout(600)
        btn_label = page.locator("#wb-project-btn").inner_text()
        print(f"[工作台·项目] 导入后按钮：{btn_label}")
        assert "未选择" not in btn_label, "导入后仍显示未选择"

        # 再打开菜单：已建档项目应在列表里；点「不使用项目」清空选中。
        page.click("#wb-project-btn")
        page.wait_for_selector("#wb-project-menu:not([hidden])", timeout=3000)
        menu_text = page.locator("#wb-project-list").inner_text()
        print(f"[工作台·项目] 清单：{' '.join(menu_text.split())[:120]}")

        # 新建项目表单：名称 / 备注 / 目录行；浏览选定回填目录，取消不建档。
        page.click('#wb-project-menu button:has-text("新建项目")')
        page.wait_for_selector("#wb-project-form:not([hidden])", timeout=3000)
        page.fill("#wb-form-name", "走查项目")
        page.click('#wb-project-form button:has-text("浏览选目录")')
        page.wait_for_selector("#wb-dirs:not([hidden])", timeout=3000)
        page.click('#wb-dirs button:has-text("选用此目录")')
        page.wait_for_timeout(300)
        dir_line = page.locator("#wb-form-dir").inner_text()
        print(f"[工作台·项目] 表单目录行：{dir_line}")
        assert dir_line.startswith("目录：")
        page.click('#wb-project-form button:has-text("取消")')
        page.wait_for_timeout(300)
        assert page.locator("#wb-project-form").is_hidden(), "取消后表单应收起"
        page.click('#wb-dirs button:has-text("收起浏览")')

        page.click("#wb-project-btn")
        page.wait_for_selector("#wb-project-menu:not([hidden])", timeout=3000)
        page.click('#wb-project-menu button:has-text("不使用项目")')
        page.wait_for_timeout(400)
        cleared = page.locator("#wb-project-btn").inner_text()
        print(f"[工作台·项目] 清空后按钮：{cleared}")
        # 没有项目目录时发不出交给引擎的轮次；重新选中刚导入的项目再测路由。
        page.click("#wb-project-btn")
        page.wait_for_selector("#wb-project-menu:not([hidden])", timeout=3000)
        page.click('#wb-project-list button:first-child')
        page.wait_for_timeout(400)

        page.fill("#wb-text", "今天我还有什么事")
        try:
            with page.expect_response(
                lambda r: "/api/agent/start" in r.url, timeout=10000
            ):
                page.focus("#wb-text")
                page.keyboard.press("Enter")
        except Exception as e:
            print(f"[工作台] 等 start 回包超时：{e}")
        time.sleep(0.8)
        route = ""
        try:
            route = page.locator("#wb-route").inner_text()
        except Exception as e:
            print(f"[工作台] 读不到路由区：{e}")
        print(f"[工作台] 路由：{' '.join(route.split())[:160]}")
        page.screenshot(path=str(SHOT_DIR / "workbench.png"), full_page=True)

        browser.close()

    unexpected_bad = [
        b for b in bad_responses
        if not (b.startswith("400 ") and "/api/agent/start" in b)
    ]
    other_errors = [
        m for m in console_msgs
        if m.startswith("[error]") and "400 (Bad Request)" not in m
    ]

    print("\n=== 走查汇总 ===")
    print(f"视图数：{len(view_report)}")
    print(f"pageerror：{len(page_errors)}")
    print(f"控制台 error：{len(other_errors)}")
    print(f"非预期 4xx/5xx：{len(unexpected_bad)}")
    if page_errors:
        print("pageerror 明细：")
        for e in page_errors:
            print(f"  - {e}")
    if unexpected_bad:
        print("坏响应明细：")
        for b in unexpected_bad:
            print(f"  - {b}")

    (SHOT_DIR / "report.json").write_text(
        json.dumps({
            "header": header,
            "views": view_report,
            "page_errors": page_errors,
            "console_errors": other_errors,
            "bad_responses": unexpected_bad,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return 1 if (page_errors or unexpected_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
