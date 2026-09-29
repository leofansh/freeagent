"""重启桥接，让新注册的 card.action.trigger 处理器生效。

进程跑的是启动那一刻的字节码 —— 不重启就还是「processor not found」。

坑：桥接是我**在界面之外**启的（detached），所以 ``supervise("stop")``
拒绝停它（它只认自己启的子进程）。必须先按命令行找到 PID 结束掉。
"""
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from freeagent.feishu import supervisor          # noqa: E402
from freeagent.feishu.status import read_status, status_path  # noqa: E402

PS = (
    "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\";"
    "foreach ($x in $p) { if ($x.CommandLine -like '*feishu.bridge*')"
    " { $x.ProcessId } }"
)
pids = [l.strip() for l in subprocess.run(
    ["powershell", "-NoProfile", "-Command", PS],
    capture_output=True, text=True, timeout=60).stdout.splitlines() if l.strip()]

for pid in pids:
    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=30)
    print(f"已结束旧桥接 pid={pid}")
time.sleep(2.0)

try:
    print("stop：", supervisor.supervise("stop"))
except supervisor.SupervisorError as exc:
    print("无需 stop：", exc)
time.sleep(1.0)
print("start：", supervisor.supervise("start"))

for _ in range(20):
    time.sleep(1.5)
    st = read_status(status_path()) or {}
    if st.get("connected"):
        print(f"已连上 pid={st.get('pid')} bot={st.get('bot_name')}")
        break
else:
    st = read_status(status_path()) or {}
    print(f"没等到连接。state={st.get('state')} connected={st.get('connected')}")
    raise SystemExit(1)

