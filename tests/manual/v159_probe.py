"""V1.159 对话页布局的浏览器探针（非交付物，不登记 §12.2）。

跑法：先在另一终端起隔离库 8813，再：
    .venv_browser\\Scripts\\python.exe tests\\manual\\v159_probe.py

钉住四件事：默认只留对话 / 三栏撑满 / 「…」菜单两入口开合 / 0 console error。
静态守卫看不出「切换后该走没走」（V1.145 的教训），必须真点一遍。
"""

from __future__ import annotations

import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8813/"

errors: list[str] = []
bad: list[str] = []
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (" — " + detail if detail else ""))


def settle(page) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        if page.locator(".chat-shell").count():
            return
        time.sleep(0.3)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=not os.environ.get("HEADED"),
            args=["--no-proxy-server"],
        )
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        page.on("console", lambda m: errors.append(f"[{m.type}] {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: bad.append(f"{r.status} {r.url}")
                if r.status >= 400 else None)

        page.goto(BASE)
        page.locator('[data-view="chat"]').first.click()
        settle(page)

        rail = page.locator("#chat-rail")
        side = page.locator("#chat-side")
        check("两侧栏默认收起", rail.count() == 1 and rail.is_hidden()
              and side.count() == 1 and side.is_hidden(),
              f"rail hidden={rail.is_hidden() if rail.count() else 'N/A'}, "
              f"side hidden={side.is_hidden() if side.count() else 'N/A'}")

        shell_w = page.locator(".chat-shell").evaluate("e => e.getBoundingClientRect().width")
        check("三栏撑满窗口", shell_w > 1000, f"shell width={shell_w:.0f}px / viewport 1180px")

        page.locator("#chat-more-btn").click()
        menu = page.locator("#chat-tool-menu")
        check("「…」菜单有开工作区/产物两入口",
              menu.locator("button", has_text="打开工作区").count() == 1
              and menu.locator("button", has_text="打开产物").count() == 1,
              menu.inner_text().replace("\n", " / "))

        menu.locator("button", has_text="打开工作区").click()
        check("菜单入口能打开工作区", rail.is_visible(), f"rail hidden={rail.is_hidden()}")

        page.locator("#chat-more-btn").click()
        check("菜单文案随状态刷新（不说反话）",
              menu.locator("button", has_text="收起工作区").count() == 1
              and menu.locator("button", has_text="打开工作区").count() == 0,
              menu.inner_text().replace("\n", " / "))
        page.keyboard.press("Escape")

        page.locator("#chat-rail-toggle").click()
        check("栏内「收起 ×」能关工作区", rail.is_hidden(), f"rail hidden={rail.is_hidden()}")

        check("0 console error / pageerror / 4xx",
              not errors and not bad, f"errors={errors[:2]} bad={bad[:2]}")

        browser.close()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
