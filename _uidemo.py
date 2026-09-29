"""起一个真的 Web 服务，用真实 HTTP 请求看「飞书通道」页返回什么。

刻意用真 socket 而不是直接调端点函数：路由表、CSP 头、JSON 编码
这些都只在真请求里才走到。
"""

import json
import pathlib
import sys
import threading
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from freeagent.app import build_app  # noqa: E402
from freeagent.feishu.status import StatusReporter, status_path  # noqa: E402
from freeagent.web import server as web_server  # noqa: E402

tmp = pathlib.Path("data/_uidemo")
tmp.mkdir(parents=True, exist_ok=True)
app = build_app(tmp / "a.db")
srv = web_server.create_server(app, "127.0.0.1", 8791)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)
base = "http://127.0.0.1:8791"


def get(p):
    with urllib.request.urlopen(base + p, timeout=10) as r:
        return r.status, r.read().decode("utf-8"), dict(r.headers)


def show(title, payload):
    print(f"\n=== {title} ===")
    for k, v in payload.items():
        if k in ("notice",):
            continue
        print(f"  {k:22} {v}")


st, _b, _h = get("/")
print(f"GET /  -> {st}, 长度 {len(_b)}")
print("  导航含飞书页签:", 'data-view="feishu"' in _b)
print("  页面含 renderFeishu:", "function renderFeishu" in _b)
print("  页面含状态端点:", "/api/feishu/status" in _b)
print("  无外部资源(http 引用):", "http://" not in _b.replace("http:// 或 https://", ""))

_s, body, _h = get("/api/feishu/status")
show("没有桥接时 /api/feishu/status", json.loads(body))

# 造一个「已连接」的桥接状态
r = StatusReporter(status_path(app.config.home), clock=time.time)
r.update(
    state="ready", connected=True, bot_open_id="ou_1d32_bot", bot_name="FreeAgent",
    allowed_users_count=1, lock_port=8771, dedup_entries=6, last_error="",
)
_s, body, _h = get("/api/feishu/status")
show("桥接就绪时", json.loads(body))

# 降级：身份没探到 -> 界面必须能分开说
r.update(state="degraded", connected=True, bot_open_id="", bot_name="")
_s, body, _h = get("/api/feishu/status")
show("降级时（连接在、身份没）", json.loads(body))

# 陈旧：进程被强杀后文件留在盘上
r2 = StatusReporter(status_path(app.config.home), clock=lambda: time.time() - 9999)
r2.update(state="ready", connected=True, bot_open_id="ou_1d32_bot")
_s, body, _h = get("/api/feishu/status")
show("陈旧时（文件在、进程没了）", json.loads(body))

# 写一份日志
(tmp / "feishu.log").write_text(
    "2026-09-28 10:02:01,009 INFO freeagent.feishu 收到 'image' 消息\n"
    "2026-09-28 10:02:06,270 INFO gateway 收到 'image' 消息，回一句「只处理文字」而不建事务\n"
    "2026-09-28 10:03:00,000 WARNING freeagent.feishu 发送者 ou_xxx 不在白名单\n"
    "2026-09-28 10:04:00,000 ERROR freeagent.feishu 发送失败\n",
    encoding="utf-8",
)
_s, body, _h = get("/api/feishu/log?lines=10")
lg = json.loads(body)
print(f"\n=== 日志 === {lg['total_lines']} 行，返回 {len(lg['lines'])} 行")
for line in lg["lines"]:
    print("  " + line)

srv.shutdown()
app.close()
import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print("\n临时目录已清理")
