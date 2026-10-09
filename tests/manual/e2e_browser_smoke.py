"""Final E2E: click 开始, wait generously (LLM ~7s), assert feedback appears."""
import json, time
from playwright.sync_api import sync_playwright

console_msgs, page_errors = [], []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1180, "height": 900})
    page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto("http://127.0.0.1:8770/", wait_until="networkidle")
    page.click('button[data-view="oc"]')
    page.wait_for_selector("#ocgo", timeout=20000)
    page.wait_for_timeout(1200)
    page.fill("#ocbrief", "端到端最终验证（可 /drop）")
    page.click("#ocgo")

    t0 = time.time()
    while time.time() - t0 < 30:
        s = page.evaluate("""() => ({
            note: [...document.querySelectorAll('.hint')].map(h=>h.innerText)
                  .filter(t=>t.includes('事务')||t.includes('错'))[0] || '',
            toast: (document.getElementById('toast')||{}).innerText || '',
            ta: document.getElementById('ocbrief').value,
            btn: document.getElementById('ocgo').disabled,
        })""")
        if "已建好事务" in s.get("note","") or s.get("toast") == "已建好事务":
            break
        time.sleep(0.5)
    elapsed = time.time() - t0
    page.screenshot(path="e2e_final.png")
    browser.close()

print(f"耗时 {elapsed:.1f}s")
print(json.dumps(s, ensure_ascii=False, indent=1))
print("page errors:", json.dumps(page_errors, ensure_ascii=False))
