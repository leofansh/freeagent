"""安静模式 + 右栏工作区文件清单的人工模拟走查（V1.142，非交付物）。

跑法（浏览器 venv）：

    .venv_browser\\Scripts\\python.exe tests\\manual\\zen_files_probe.py

前提：隔离库 Web 服务已在 8811 起（见 ui_walkthrough 的说明）。

只做一件事：把「安静收外壳 / 窄条不失明 / Esc 与刷新 / 文件清单进出一层 /
绝对路径与 `../` 被拒」这几条在真 DOM 上点一遍，并留两张截图。
**不点「定位」** —— 那会真的拉起系统文件管理器，走查不需要真开户。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8811/"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHOT_DIR = REPO_ROOT / "tests" / "manual" / "shots" / "zen"

errors: list[str] = []
checks: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        errors.append(name)


def enter_workbench(page) -> None:
    page.click('button[data-view="workbench"]')
    # 安静模式下左栏是 hidden（那是本版要的行为），所以只等挂上，不等可见。
    page.wait_for_selector(".wb-view", state="attached", timeout=5000)
    page.wait_for_timeout(500)


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
        enter_workbench(page)

        # 0. 五个新挂载点都在（页面重建后漏挂是这一类改动最常见的坏法）。
        for sel, label in [
            ("#wb-bar", "顶部表单有 id"),
            ("#wb-run-note", "后台运行那一句"),
            ("#wb-side-turn", "右栏上块（产物）"),
            ("#wb-files", "右栏下块（文件）"),
            ("#wb-zen", "安静按钮"),
            ("#wb-zen-strip", "安静窄条已插入"),
        ]:
            check(label, page.locator(sel).count() == 1, sel)

        run_note = page.locator("#wb-run-note").inner_text()
        check("那一句说的是「关掉这一页不会停」", "不会停" in run_note, run_note[:60])
        check("那一句指向停止口", "停止" in run_note)

        # 1. 先确保有一个带目录的工作区（隔离库可能已经建过）。
        page.evaluate(
            """async () => {
              const d = await api("/api/agent/projects");
              if (!d.projects.some((p) => p.name === "freeagent 本体")) {
                await post("/api/agent/project_save", {
                  directory: "D:\\\\PycharmProjects\\\\freeagent",
                  name: "freeagent 本体", note: "",
                });
                await wbLoadProjects();
              }
            }"""
        )
        page.wait_for_timeout(500)
        if page.locator('#wb-rail-list .wb-rail-row:has-text("freeagent 本体")').count():
            page.click('#wb-rail-list .wb-rail-row:has-text("freeagent 本体")')
            page.wait_for_timeout(800)

        # 2. 文件清单：根、一层、目录在前、有「进去」与「定位」。
        files = page.locator("#wb-files")
        text = files.inner_text()
        check("文件块写了「这个工作区有什么」", "这个工作区有什么" in text)
        rows = page.locator("#wb-files .wb-file-row")
        check("根目录列出条目", rows.count() > 0, f"{rows.count()} 行")
        check("根的位置写出来了", ("根：" in text) or ("位置：" in text))
        first_is_dir = page.locator("#wb-files .wb-file-row").first.inner_text()
        check("目录排在文件前面", "目录" in first_is_dir, first_is_dir[:40])
        check(
            "有 src/ 目录并带「进去」",
            page.locator('#wb-files .wb-file-row:has-text("src/") button.chip').count() == 1,
        )
        check("有「定位」按钮", page.locator("#wb-files button.wb-locate").count() > 0)
        loc_path = page.evaluate(
            """() => {
              const b = document.querySelector("#wb-files button.wb-locate");
              return b ? b.dataset.path : "";
            }"""
        )
        check("定位按钮带着绝对路径", str(loc_path).startswith("D:\\"), str(loc_path))
        page.screenshot(path=str(SHOT_DIR / "files.png"), full_page=True)

        # 3. 进子目录 / 上一层：路径回显与 parent 都对。
        page.click('#wb-files .wb-file-row:has-text("src/") button.chip')
        page.wait_for_timeout(700)
        tip = page.locator("#wb-files .note").first.inner_text()
        check("进去之后位置变成子目录", "位置：" in tip and "src" in tip, tip[:60])
        check("子目录里有「上一层」", page.locator('#wb-files button:has-text("上一层")').count() == 1)
        page.click('#wb-files button:has-text("上一层")')
        page.wait_for_timeout(700)
        check("上一层回到根（不再有「上一层」）",
              page.locator('#wb-files button:has-text("上一层")').count() == 0)

        # 4. 越界两种写法在真端点上被拒（不是前端藏起来了事）。
        bad = page.evaluate(
            """async () => {
              const out = {};
              const q = (p) => "/api/agent/files?project=" +
                encodeURIComponent(wbProjectId) + "&path=" + encodeURIComponent(p);
              for (const [k, v] of [["dotdot", "../"], ["abs", "C:/Windows"]]) {
                try { await api(q(v)); out[k] = "NO-BLOCK"; }
                catch (e) { out[k] = "BLOCKED"; }
              }
              return out;
            }"""
        )
        check("`../` 被拒", bad.get("dotdot") == "BLOCKED", str(bad))
        check("绝对路径被拒", bad.get("abs") == "BLOCKED", str(bad))

        # 5. 安静模式：进 / 窄条 / 按钮文案 / localStorage。
        page.click("#wb-zen")
        page.wait_for_timeout(400)
        check("进安静：.wb-view 带 is-zen",
              page.locator(".wb-view.is-zen").count() == 1)
        check("配置表单收起来了", not page.locator("#wb-bar").is_visible())
        check("左栏收起来了", not page.locator("#wb-rail").is_visible())
        check("右栏收起来了", not page.locator("#wb-side").is_visible())
        strip = page.locator("#wb-zen-strip")
        check("窄条可见", strip.is_visible())
        strip_text = strip.inner_text()
        check("窄条写着当前工作区", "安静模式 · " in strip_text and "freeagent 本体" in strip_text,
              strip_text[:60])
        check("退出按钮在位", page.locator('#wb-zen-strip button:has-text("退出（Esc）")').count() == 1)
        check("按钮文案变成退出安静", page.locator("#wb-zen").inner_text() == "退出安静")
        stored = page.evaluate("() => window.localStorage.getItem('wbZen')")
        check("状态写进 localStorage", stored == "1", str(stored))
        page.screenshot(path=str(SHOT_DIR / "zen.png"), full_page=True)

        # 6. 刷新后仍安静（这是「记在本机」的兑现，不是一次性动作）。
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(600)
        enter_workbench(page)
        page.wait_for_timeout(600)
        check("刷新后仍安静", page.locator(".wb-view.is-zen").count() == 1)
        check("刷新后窄条仍写着工作区",
              "安静模式 · " in page.locator("#wb-zen-strip").inner_text())

        # 7. Esc 退出：外壳回来、状态回 0。
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        check("Esc 退出安静", page.locator(".wb-view.is-zen").count() == 0)
        check("退出后配置表单回来", page.locator("#wb-bar").is_visible())
        check("退出后左栏回来", page.locator("#wb-rail").is_visible())
        check("退出后右栏回来", page.locator("#wb-side").is_visible())
        check("退出后窄条隐藏", not page.locator("#wb-zen-strip").is_visible())
        check("退出按钮文案还原", page.locator("#wb-zen").inner_text() == "安静")
        stored = page.evaluate("() => window.localStorage.getItem('wbZen')")
        check("localStorage 回到 0", stored == "0", str(stored))

        # 8. 安静期间正文与输入框还在（收的是外壳，不是工作台）。
        page.click("#wb-zen")
        page.wait_for_timeout(400)
        check("安静时正文仍在", page.locator(".wb-col-main").is_visible())
        check("安静时输入框仍在", page.locator("#wb-text").is_visible())
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        browser.close()

    print("\n=== 安静模式 / 文件清单走查汇总 ===")
    ok_n = len([c for c in checks if c["ok"]])
    print(f"检查 {ok_n}/{len(checks)} 通过；pageerror {len(errors)}")
    (SHOT_DIR / "report.json").write_text(
        json.dumps({"checks": checks, "errors": errors}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
