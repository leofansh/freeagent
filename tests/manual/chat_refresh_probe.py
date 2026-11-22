"""复现：点「刷新」后，退役的建议行（chips）被历史重放插回消息中间。

跑法（浏览器 venv）：

    .venv_browser\\Scripts\\python.exe tests\\manual\\chat_refresh_probe.py

前提：另一个终端已起隔离库服务（--port 8811）。
输出：发送后的历史数组、刷新前 DOM、刷新后 DOM 的逐行类别与文本。
"""

from __future__ import annotations

import json
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8811/"
KEY = "freeagent.chat.v1"


def dump_rows(page, label: str) -> None:
    rows = page.eval_on_selector_all(
        "#chatlog > *",
        "els => els.map(e => ({cls: e.className, text: e.innerText.replace(/\\s+/g, ' ').trim()}))",
    )
    print(f"\n=== {label}：{len(rows)} 行 ===")
    for i, r in enumerate(rows):
        print(f"  {i:2d} [{r['cls']}] {r['text'][:70]}")


def dump_history(page, label: str) -> None:
    raw = page.evaluate(f"localStorage.getItem('{KEY}')")
    hist = json.loads(raw) if raw else []
    chips = [m for m in hist if m.get("who") == "chips"]
    print(f"\n--- {label}：history {len(hist)} 条，chips {len(chips)} 条 ---")
    for i, m in enumerate(hist):
        who = m.get("who")
        text = m.get("text", "")
        if isinstance(text, list):
            text = "/".join(text)
        print(f"  {i:2d} [{who}] {str(text)[:60]}")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True, args=["--no-proxy-server"]
        )
        page = browser.new_page(viewport={"width": 1180, "height": 1000})
        page.on("pageerror", lambda e: print("PAGEERROR:", e))
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector("#chatlog", timeout=5000)

        for q in ["我上周完成了什么", "你好", "今天"]:
            page.fill("#chatinput", q)
            page.keyboard.press("Enter")
            try:
                with page.expect_response(
                    lambda r: r.url.endswith("/api/chat") and r.request.method == "POST",
                    timeout=10000,
                ):
                    pass
            except Exception as e:
                print(f"等 {q!r} 回包超时：{e}")
            time.sleep(0.4)

        dump_history(page, "发送后（未刷新）")
        dump_rows(page, "刷新前 DOM")

        page.click("#refresh")
        time.sleep(1.2)
        dump_history(page, "刷新后（history_）")
        dump_rows(page, "刷新后 DOM")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
