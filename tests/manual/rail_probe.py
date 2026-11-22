"""工作区左栏 + 累积块的人工模拟走查（V1.139，非交付物）。

跑法（浏览器 venv）：

    .venv_browser\\Scripts\\python.exe tests\\manual\\rail_probe.py

前提：隔离库 Web 服务已在 8811 起（见 ui_walkthrough 的说明）。

只做一件事：把「左栏常驻」「三栏顺序」「选中即切目录」「备注就地改」
「读不到不说成 0」这几条在真 DOM 上点一遍，并留一张截图。
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
SHOT_DIR = REPO_ROOT / "tests" / "manual" / "shots" / "rail"

errors: list[str] = []
checks: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        errors.append(name)


def main() -> int:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=not os.environ.get("HEADED"),
            args=["--no-proxy-server"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(600)

        page.click('button[data-view="workbench"]')
        page.wait_for_selector("#wb-rail", timeout=5000)
        page.wait_for_timeout(400)

        # 1. 左栏在最左，三栏横向递增。
        rail = page.locator("#wb-rail").bounding_box()
        main = page.locator(".wb-col-main").bounding_box()
        side = page.locator("#wb-side").bounding_box()
        check(
            "三栏顺序 工作区 / 正文 / 产物",
            rail["x"] < main["x"] < side["x"],
            f"rail={rail['x']:.0f} main={main['x']:.0f} side={side['x']:.0f}",
        )
        check("左栏宽度约 220px", 200 <= rail["width"] <= 240,
              f"{rail['width']:.0f}px")

        # 2. 用页面自己的 post 建第二个工作区（页面内请求自带令牌）。
        page.evaluate(
            """async () => {
              await post("/api/agent/project_save", {
                directory: "D:\\\\PycharmProjects\\\\freeagent",
                name: "freeagent 本体", note: "",
              });
              await wbLoadProjects();
            }"""
        )
        page.wait_for_timeout(500)
        rows = page.locator("#wb-rail-list .wb-rail-row")
        check("左栏列出两个工作区", rows.count() >= 2, f"{rows.count()} 行")
        rail_text = page.locator("#wb-rail").inner_text()
        check("轮次数在位（还没跑过 / N 轮）", "还没跑过" in rail_text or "轮" in rail_text)

        # 3. 点行即选中：按钮文案、is-current、隐藏目录框三处同步。
        page.click('#wb-rail-list .wb-rail-row:has-text("freeagent 本体")')
        page.wait_for_timeout(500)
        btn = page.locator("#wb-project-btn").inner_text()
        check("点行选中后按钮文案跟着变", "freeagent 本体" in btn, btn)
        check(
            "选中行带 is-current",
            page.locator('#wb-rail-list .wb-rail-row.is-current:has-text("freeagent 本体")').count() == 1,
        )
        dir_val = page.input_value("#wb-dir")
        check("#wb-dir 同步为该项目目录",
              dir_val.replace("\\", "/").endswith("PycharmProjects/freeagent"),
              dir_val)

        # 4. 搜索只筛不删。
        page.fill("#wb-rail-filter", "free")
        page.wait_for_timeout(300)
        check("搜索后只剩匹配行",
              page.locator("#wb-rail-list .wb-rail-row").count() == 1,
              f"{page.locator('#wb-rail-list .wb-rail-row').count()} 行")
        page.fill("#wb-rail-filter", "")
        page.wait_for_timeout(300)
        check("清空搜索恢复全部行",
              page.locator("#wb-rail-list .wb-rail-row").count() >= 2)

        # 5. 备注就地改：走 project_update，落库后回到「备注」那行。
        page.click("#wb-harness button:has-text(\"改\")")
        page.wait_for_selector("#wb-rail-note", timeout=3000)
        page.fill("#wb-rail-note", "主仓，改代码在这里做")
        page.click("#wb-harness button:has-text(\"存下\")")
        page.wait_for_timeout(800)
        harness = page.locator("#wb-harness").inner_text()
        check("备注存下后回到累积块", "主仓，改代码在这里做" in harness,
              " ".join(harness.split())[:120])
        saved = page.evaluate(
            """async () => {
              const d = await api("/api/agent/projects");
              return (d.projects.find((p) => p.name === "freeagent 本体") || {}).note || "";
            }"""
        )
        check("备注确实落库", saved == "主仓，改代码在这里做", saved)

        # 6. 背景勾选只回显，不放第二个开关。
        check("左栏没有第二个注入勾选框",
              page.locator("#wb-rail input[type=checkbox]").count() == 0)
        page.uncheck("#wb-inject")
        page.wait_for_timeout(300)
        check("关掉背景后累积块当场改口",
              "只带这句话" in page.locator("#wb-harness").inner_text())
        page.check("#wb-inject")

        page.screenshot(path=str(SHOT_DIR / "rail.png"), full_page=True)
        browser.close()

    print("\n=== 左栏走查汇总 ===")
    ok_n = len([c for c in checks if c["ok"]])
    print(f"检查 {ok_n}/{len(checks)} 通过；pageerror {len(errors)}")
    (SHOT_DIR / "report.json").write_text(
        json.dumps({"checks": checks, "errors": errors}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
