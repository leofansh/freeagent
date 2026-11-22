"""工作台 V1.117 浏览器人工模拟走查（不开真引擎）。

跑法（用浏览器 venv，不跑 pytest）：

    .venv_browser\\Scripts\\python.exe tests\\manual\\wb_smoke.py

前提：先在另一个终端起隔离库的 Web 服务，例如：

    set PYTHONPATH=src
    python -m freeagent.web --db .smoke/agent.db --port 8811 --no-browser

走查范围（只覆盖不依赖真机 opencode 的路径）：

1. 工作台装配：目录 / 模型 / 授权档 / 输入框 / 发送 / 停止 控件齐全。
2. 初始态：停止禁用、发送可用；无 pageerror。
3. 相对路径目录在起轮次前被拦下（toast 说必须是绝对路径），没起一轮。
4. 不存在的绝对目录同样被拦下。
5. 合法目录下一句本地问题按回车提交，路由判「不用动手」，不起引擎；
   回车键位接线生效。
6. 渲染转义：把带 `<img>` / `<script>` 的合成一轮喂给 `wbPaint`，
   原文必须以转义形式进 DOM，裸标签不许存在于 innerHTML。
7. 目录名转义：浏览到含 `&` 的子目录，option 文本与转义后 HTML 分开核对。

真机引擎路径（真起 opencode、审批卡 / 提问卡 / 停止）不在本脚本范围，
真源只在 docs/真机验收矩阵.md 表 3（W1–W4）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8811/"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SMOKE_WORK = REPO_ROOT / ".smoke" / "work"
SHOT_DIR = REPO_ROOT / "tests" / "manual" / "shots"

failures: list[str] = []
console_msgs: list[str] = []
page_errors: list[str] = []
bad_responses: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def wait_toast(page, needle: str, timeout: float = 3.0) -> str:
    """轮询 toast 文案；返回最终读到的 toast 文本。"""
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        text = (page.locator("#toast").inner_text() or "").strip()
        if needle in text:
            return text
        time.sleep(0.2)
    return text


def main() -> int:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    # 目录名转义检查要用一个带 & 的子目录；脚本自建，不依赖手工准备。
    (SMOKE_WORK / "a&b").mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True, args=["--no-proxy-server"]
        )
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on(
            "response",
            lambda r: bad_responses.append(f"{r.status} {r.url}")
            if r.status >= 400 else None,
        )

        page.goto(BASE, wait_until="networkidle")
        page.click('button[data-view="workbench"]')
        page.wait_for_selector("#wb-dir", timeout=5000)

        # 1. 控件齐全
        for sel, label in [
            ("#wb-dir", "目录输入框"),
            ("#wb-model", "模型输入框"),
            ("#wb-mode", "授权档下拉"),
            ("#wb-text", "一句话输入框"),
            ("#wb-send", "交给它按钮"),
            ("#wb-stop", "停止按钮"),
            ("#wb-route", "路由理由区"),
            ("#wb-stream", "正文流"),
            ("#wb-history", "最近的活"),
        ]:
            check(f"控件存在：{label}", page.locator(sel).count() == 1)

        # 2. 初始态：停止禁用、发送可用
        check("初始：停止按钮禁用", page.locator("#wb-stop").is_disabled())
        check("初始：发送按钮可用", not page.locator("#wb-send").is_disabled())

        # 3. 相对路径被前置拦下
        page.fill("#wb-dir", "projects/foo")
        page.fill("#wb-text", "改一下这个文件")
        page.click("#wb-send")
        toast = wait_toast(page, "绝对路径")
        check("相对路径：toast 要求绝对路径", "绝对路径" in toast, toast)
        stream_after_rel = page.locator("#wb-stream").inner_text()
        check("相对路径：没起轮次（仍显示空态）", "还没有跑过" in stream_after_rel)

        # 4. 不存在的绝对目录被前置拦下
        page.fill("#wb-dir", "D:/no-such-dir-xyz-999")
        page.click("#wb-send")
        toast = wait_toast(page, "不存在或不是目录")
        check("不存在目录：toast 说明目录不存在", "不存在或不是目录" in toast, toast)
        stream_after_missing = page.locator("#wb-stream").inner_text()
        check("不存在目录：没起轮次（仍显示空态）", "还没有跑过" in stream_after_missing)

        # 5. 合法目录 + 本地问题，回车提交 → 路由判不用动手
        page.fill("#wb-dir", str(SMOKE_WORK).replace("\\", "/"))
        page.fill("#wb-text", "今天我还有什么事")
        page.focus("#wb-text")
        page.keyboard.press("Enter")
        page.wait_for_selector("#wb-route", timeout=3000)
        route_text = ""
        deadline = time.time() + 3.0
        while time.time() < deadline:
            route_text = page.locator("#wb-route").inner_text()
            if "没交给引擎" in route_text:
                break
            time.sleep(0.2)
        check("回车提交：路由显示「没交给引擎」", "没交给引擎" in route_text, route_text)
        check("回车提交：停止仍禁用", page.locator("#wb-stop").is_disabled())
        check("回车提交：发送仍可用", not page.locator("#wb-send").is_disabled())

        # 6. 渲染转义：合成一轮带 payload，wbPaint 后裸标签不许进 innerHTML
        payload_turn = {
            "id": "smoke", "status": "done", "brief": "<b>hi</b>",
            "context_note": "", "steps": [], "tools": [],
            "files": ["<img src=x onerror=alert(1)>"],
            "commands": ["<script>alert(1)</script>"],
            "reason": "", "notes": [], "text": "", "ok": True,
            "directory": "", "model": "", "mode": "ask",
            "started_at": "", "elapsed": 0, "tool_calls": 0, "ask": None,
        }
        page.evaluate("(t) => wbPaint({ turn: t })", payload_turn)
        inner_html = page.locator("#wb-stream").inner_html()
        text_content = page.locator("#wb-stream").inner_text()
        check("转义：innerHTML 无裸 img 标签", "<img src=" not in inner_html)
        check("转义：innerHTML 无裸 script 标签", "<script" not in inner_html)
        check("转义：img 原文保留在文本里", "<img src=x onerror=alert(1)>" in text_content)
        check("转义：script 原文保留在文本里", "<script>alert(1)</script>" in text_content)
        check("转义：brief 的 <b> 被转义", "&lt;b&gt;hi&lt;/b&gt;" in inner_html)

        # 7. 目录名转义：浏览含 & 的子目录
        page.evaluate("(d) => wbBrowse(d)", str(SMOKE_WORK).replace("\\", "/"))
        page.wait_for_selector("#wb-dir-pick", timeout=3000)
        option_html = page.locator("#wb-dir-pick option").last.inner_html()
        option_text = page.locator("#wb-dir-pick option").last.inner_text()
        check("目录名：& 在 HTML 里转义为 &amp;", "&amp;" in option_html, option_html)
        check("目录名：文本仍是原始 a&b", option_text == "a&b", option_text)

        page.screenshot(path=str(SHOT_DIR / "wb_smoke.png"), full_page=True)
        browser.close()

    check("无浏览器 pageerror", len(page_errors) == 0, "; ".join(page_errors))
    # 负路径用例（相对目录 / 不存在目录）本来就该吃 400，控制台把失败的
    # HTTP 资源也记成 [error]；只允许这两次 /api/agent/start 的 400，
    # 其余 4xx/5xx 或别的 error 都算红。
    unexpected_bad = [
        b for b in bad_responses
        if not (b.startswith("400 ") and "/api/agent/start" in b)
    ]
    check("无预期外的 4xx/5xx 响应", len(unexpected_bad) == 0,
          "; ".join(unexpected_bad))
    check("负路径恰好两次 400", len(bad_responses) == 2,
          f"实际 {len(bad_responses)} 次：{'; '.join(bad_responses)}")
    other_errors = [
        m for m in console_msgs
        if m.startswith("[error]") and "400 (Bad Request)" not in m
    ]
    check("无其他控制台 error", len(other_errors) == 0, "; ".join(other_errors))

    print("\n=== 汇总 ===")
    if failures:
        print(f"{len(failures)} 项失败：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
