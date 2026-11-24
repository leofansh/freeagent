"""V1.160 窄屏互斥的浏览器探针（非交付物，不登记 §12.2）。

跑法：先在另一终端起隔离库 8813，再：
    .venv_browser\\Scripts\\python.exe tests\\manual\\v160_probe.py

钉住四件事：窄屏开一侧自动收另一侧 / 不横向溢出 / 宽屏仍可同时开两侧 /
菜单文案随状态刷新。静态守卫看不出「切换后该走没走」（V1.145 的教训），
必须真点一遍。
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


def open_from_menu(page, label: str) -> None:
    # 幂等：菜单已开就不重复点 more-btn（否则 toggle 又关掉），兼容上一步 Esc 是否生效。
    if page.locator("#chat-tool-menu").is_hidden():
        page.locator("#chat-more-btn").click()
    page.locator("#chat-tool-menu button", has_text=label).click()


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=not os.environ.get("HEADED"),
            args=["--no-proxy-server"],
        )
        # 窄屏：视口 761px，触发互斥（< CHAT_NARROW=1000）。
        page = browser.new_page(viewport={"width": 761, "height": 900})
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
        check("窄屏默认两侧收起", rail.is_hidden() and side.is_hidden(),
              f"rail={rail.is_hidden()} side={side.is_hidden()}")

        def overflow() -> tuple[int, int]:
            return page.evaluate(
                "() => [document.documentElement.scrollWidth, "
                "document.documentElement.clientWidth]")

        sw, cw = overflow()
        check("窄屏初始不横向溢出", sw <= cw, f"scrollWidth={sw} clientWidth={cw}")

        open_from_menu(page, "打开工作区")
        check("窄屏开工作区后工作区可见", rail.is_visible(),
              f"rail={rail.is_hidden()}")
        sw, cw = overflow()
        check("窄屏单开工作区不横向溢出", sw <= cw,
              f"scrollWidth={sw} clientWidth={cw}")

        open_from_menu(page, "打开产物")
        check("窄屏开产物自动收工作区（互斥）", side.is_visible() and rail.is_hidden(),
              f"side={side.is_hidden()} rail={rail.is_hidden()}")
        sw, cw = overflow()
        check("窄屏单开产物不横向溢出", sw <= cw,
              f"scrollWidth={sw} clientWidth={cw}")

        # 菜单文案按当前状态刷新：产物已开 → 显示"收起产物"，工作区已收 → "打开工作区"。
        page.locator("#chat-more-btn").click()
        menu = page.locator("#chat-tool-menu")
        check("菜单文案随互斥状态刷新",
              menu.locator("button", has_text="收起产物").count() == 1
              and menu.locator("button", has_text="打开工作区").count() == 1,
              menu.inner_text().replace("\n", " / "))
        page.keyboard.press("Escape")
        check("Esc 关闭工具菜单", page.locator("#chat-tool-menu").is_hidden())

        # 宽屏：视口 1001px（>= 1000），仍可同时开两侧。
        page.set_viewport_size({"width": 1001, "height": 900})
        open_from_menu(page, "打开工作区")
        check("宽屏可同时开两侧（不互斥）", rail.is_visible() and side.is_visible(),
              f"rail={rail.is_hidden()} side={side.is_hidden()}")

        check("0 console error / pageerror / 4xx",
              not errors and not bad, f"errors={errors[:2]} bad={bad[:2]}")

        browser.close()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
