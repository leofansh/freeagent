"""V1.133 参考三家 Agent 的易用性四件套浏览器走查（隔离库 8811）。

跑法（浏览器 venv）：

    .venv_browser\\Scripts\\python.exe tests\\manual\\v133_probe.py

前提：另起隔离库服务
    set PYTHONPATH=src
    python -m freeagent.web --db .smoke/ui/agent.db --port 8811 --no-browser

人工验证物，非交付物，不登记 §12.2。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8811/"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHOT_DIR = REPO_ROOT / "tests" / "manual" / "shots" / "v133"

console_errors: list[str] = []
page_errors: list[str] = []
bad_responses: list[str] = []
results: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    line = ("[PASS] " if cond else "[FAIL] ") + label + (f" — {detail}" if detail else "")
    results.append(line)
    print(line)


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
        browser = p.chromium.launch(channel="msedge", headless=True, args=["--no-proxy-server"])
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
        ok(f"16 视图全可达（实际 {len(views)}，V1.133 加了周回顾）", len(views) == 16)

        # --- ③ 对话首屏欢迎区（V1.145：空对话居中，取代 V1.133 可关闭引导卡）---
        page.click('button[data-view="chat"]')
        page.wait_for_selector("#chatlog", timeout=5000)
        page.wait_for_timeout(300)
        welcome = page.locator(".chat-welcome")
        ok("空对话出现欢迎区", welcome.count() == 1)
        ok("欢迎区不再有可关闭引导卡", page.locator("#fa-onboard").count() == 0)
        if welcome.count():
            wtext = welcome.inner_text()
            ok("欢迎区有招呼与「试试这样问」", "你好" in wtext and "试试这样问" in wtext,
               f"片段: {' '.join(wtext.split())[:80]}")
            cards = page.locator(".chat-welcome .cw-card")
            ok("欢迎区用例子卡片（来自 can_do）", cards.count() >= 3, f"卡片数={cards.count()}")
            if cards.count():
                ex = cards.first
                ex_text = ex.inner_text()
                log_before = page.locator("#chatlog").inner_text()
                ex.click()
                page.wait_for_timeout(150)
                input_val = page.locator("#chatinput").input_value()
                ok("点例子填进输入框（不发送）",
                   input_val and input_val == ex_text and page.locator("#chatlog").inner_text() == log_before,
                   f"例子={ex_text!r} input={input_val!r}")
            page.screenshot(path=str(SHOT_DIR / "chat_welcome.png"), full_page=True)

        # --- ① 周回顾页（更多 → 我的事 → 周回顾）---
        page.click("#more-btn")
        page.click('button[data-view="week"]')
        settle(page)
        week_text = page.locator("#root").inner_text()
        ok("周回顾页渲染出「周回顾」标题", "周回顾" in week_text)
        ok("周回顾页有本周范围与事实声明", "本周" in week_text and "事实" in week_text,
           f"片段: {' '.join(week_text.split())[:100]}")
        page.screenshot(path=str(SHOT_DIR / "week.png"), full_page=True)

        # --- ② 产物复制/导出（函数接线 + 侧栏真实渲染）---
        funcs = page.evaluate("""() => ({
            artifact: typeof artifactCopyBar === 'function',
            wbCopy: typeof wbCopyText === 'function',
            wbExport: typeof wbExportMd === 'function'
        })""")
        ok("产物复制/导出函数已进页面", funcs["artifact"] and funcs["wbCopy"] and funcs["wbExport"], str(funcs))
        # 用合成 turn 驱动侧栏真渲染，验证按钮上屏（隔离库无真实轮次）
        side_render = page.evaluate("""() => {
            const box = document.querySelector('#wb-side') || (() => {
                const d = document.createElement('div'); d.id = 'wb-side'; document.body.appendChild(d); return d;
            })();
            wbSidePaint({ text: '这是一段答复', files: ['a.py'], commands: ['pytest -q'], status: 'done' });
            const t = box.innerText;
            return { copy: t.includes('复制答复'), exp: t.includes('导出 .md'), hasPre: !!box.querySelector('pre') };
        }""")
        ok("产物侧栏渲染出复制/导出按钮 + 答复", side_render["copy"] and side_render["exp"] and side_render["hasPre"], str(side_render))

        # --- ④ 用量可见（读不到如实显示，不显示 0）---
        # 必须走应用的 api()：裸 fetch 不带会话令牌，会吃 401（那是探针的错，不是应用的）。
        state = page.evaluate("""async () => (await api('/api/agent/state')).usage || null""")
        ok("agent_state 暴露 usage", state is not None and state.get("state") in ("ok", "unreadable"), str(state))
        if state and state.get("state") == "unreadable":
            ok("用量读不到时带原因（不报 0）", bool(state.get("reason")) and state.get("total", 0) == 0, str(state))
        # 合成 usage 驱动 wbPaint 文字（真实引擎未起，验证呈现分支）。
        # 引擎行画在 #wb-engine，且必须人在工作台页它才存在。
        page.click('button[data-view="workbench"]')
        settle(page)
        usage_render = page.evaluate("""() => {
            const box = document.querySelector('#wb-engine');
            if (!box) return { readable: false, unreadable: false, why: 'no #wb-engine' };
            const probe = (u) => {
                wbPaint({ engine: { status: 'ready', pid: 1 }, usage: u, turn: null });
                return box.textContent;
            };
            const readable = probe({ state: 'ok', input: 12, output: 5, total: 17, reason: '' });
            const unreadable = probe({ state: 'unreadable', input: 0, output: 0, total: 0, reason: '连不上引擎' });
            return {
                readable: readable.includes('Token 输入 12'),
                unreadable: unreadable.includes('Token 读不到：连不上引擎'),
                detail: readable + ' || ' + unreadable
            };
        }""")
        ok("用量可读/读不到两种文案都能渲染", usage_render["readable"] and usage_render["unreadable"], str(usage_render))
        page.screenshot(path=str(SHOT_DIR / "workbench_usage.png"), full_page=True)

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
