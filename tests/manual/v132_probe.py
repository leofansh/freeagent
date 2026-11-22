"""V1.132 易用性五件套浏览器人工模拟走查（隔离库 8811，不依赖真机引擎）。

跑法（浏览器 venv）：

    .venv_browser\\Scripts\\python.exe tests\\manual\\v132_probe.py

前提：另起隔离库服务
    set PYTHONPATH=src
    python -m freeagent.web --db .smoke/ui/agent.db --port 8811 --no-browser

本探针是人工验证物，非交付物，不登记 §12.2。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8811/"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHOT_DIR = REPO_ROOT / "tests" / "manual" / "shots" / "v132"

console_errors: list[str] = []
page_errors: list[str] = []
bad_responses: list[str] = []
results: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    results.append(f"[{tag}] {label}" + (f" — {detail}" if detail else ""))
    print(results[-1])


def settle(page, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    prev = ""
    while time.time() < deadline:
        text = page.locator("#root").inner_text()
        if text.strip() and "载入中" not in text and text == prev:
            return text
        prev = text
        time.sleep(0.3)
    return page.locator("#root").inner_text()


def main() -> int:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True, args=["--no-proxy-server"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r: bad_responses.append(f"{r.status} {r.url}") if r.status >= 400 else None)

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(500)

        # --- 15 视图全可达 ---
        views = page.eval_on_selector_all("#nav button[data-view]", "bs => bs.map(b => b.dataset.view)")
        primary = {"chat", "workbench", "today", "all"}
        for view in views:
            if view not in primary:
                page.click("#more-btn")
            page.click(f'button[data-view="{view}"]')
            settle(page)
        ok(f"15 视图全可达（实际 {len(views)}）", len(views) == 15)

        # --- ① chips 填框不发送 ---
        page.click('button[data-view="chat"]')
        page.wait_for_selector("#chatlog", timeout=5000)
        chip = page.locator("#chatlog .chip").first
        chip_text = chip.inner_text()
        log_before = page.locator("#chatlog").inner_text()
        chip.click()
        page.wait_for_timeout(200)
        input_val = page.locator("#chatinput").input_value()
        log_after = page.locator("#chatlog").inner_text()
        ok("chip 点一下填进输入框", input_val == chip_text, f"chip={chip_text!r} input={input_val!r}")
        ok("chip 点击不直接发送（对话正文未变）", log_after == log_before)

        # --- ② 简洁模式切换 ---
        btn = page.locator("#compact-btn")
        ok("简洁按钮已挂进导航", btn.count() == 1)
        init_compact = page.evaluate("document.body.classList.contains('compact')")
        txt0 = btn.inner_text()
        btn.click()
        page.wait_for_timeout(150)
        after_compact = page.evaluate("document.body.classList.contains('compact')")
        txt1 = btn.inner_text()
        ok("简洁按钮切换 body.compact", after_compact != init_compact, f"{init_compact}->{after_compact}")
        ok("简洁按钮文案随状态变", txt0 != txt1, f"{txt0!r}->{txt1!r}")
        btn.click()  # 切回
        page.wait_for_timeout(150)
        ok("再点切回原状态", page.evaluate("document.body.classList.contains('compact')") == init_compact)

        # --- ③ 对话内搜索 ---
        page.fill("#chatinput", "今天我还有什么事")
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        page.keyboard.press("Control+f")
        page.wait_for_timeout(200)
        ok("Ctrl+F 打开搜索条", page.locator(".fa-search").count() == 1)
        if page.locator("#fa-search-inp").count():
            page.fill("#fa-search-inp", "今天")
            page.wait_for_timeout(200)
            hit_count = page.locator(".fa-hit").count()
            ok("搜索命中被 <mark> 高亮", hit_count > 0, f"命中 {hit_count} 处")
            cnt = page.locator("#fa-search-cnt").inner_text() if page.locator("#fa-search-cnt").count() else ""
            print(f"    搜索计数文案：{cnt}")
            page.screenshot(path=str(SHOT_DIR / "chat_search.png"), full_page=True)
        # 关闭搜索条
        page.keyboard.press("Escape")
        if page.locator(".fa-search button:has-text('关闭')").count():
            page.locator(".fa-search button:has-text('关闭')").click()

        # --- ④ 设置分组 ---
        page.click("#more-btn")
        page.click('button[data-view="settings"]')
        settle(page)
        labels = page.eval_on_selector_all(".section-label", "bs => bs.map(b => b.textContent.trim())")
        for want in ("智能层与模型", "运行与降级", "排序偏好"):
            ok(f"设置分组标题「{want}」在", want in labels, f"实际: {labels}")

        # --- ⑤ 工作台产物侧栏 ---
        page.click('button[data-view="workbench"]')
        settle(page)
        ok("工作台两栏容器 .wb-cols 在", page.locator(".wb-cols").count() == 1)
        side = page.locator("#wb-side")
        ok("产物侧栏 #wb-side 在", side.count() == 1)
        side_txt = side.inner_text() if side.count() else ""
        ok("产物侧栏有空态文案", "产物" in side_txt or "还没有产物" in side_txt, f"文案: {' '.join(side_txt.split())[:80]}")
        page.screenshot(path=str(SHOT_DIR / "workbench_side.png"), full_page=True)

        browser.close()

    bad_unexpected = [b for b in bad_responses if not ("400" in b and "/api/agent/start" in b)]
    ok("0 pageerror", not page_errors, "; ".join(page_errors[:3]))
    ok("0 控制台 error", not console_errors, "; ".join(console_errors[:3]))
    ok("0 非预期坏响应", not bad_unexpected, "; ".join(bad_unexpected[:3]))

    print("\n=== 汇总 ===")
    fails = [r for r in results if r.startswith("[FAIL]")]
    print(f"检查项 {len(results)}，失败 {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
