"""一次性真机 SSE 抓包探针（诊断 V2.0.29 工具生命周期 / 授权事件形状）。

不落库、不写闸门覆盖层；起一个临时目录的非隔离 opencode 服务，先订阅事件流，
再发一条只读指令，把每帧原始 JSON 落 JSONL，最后按事件类型汇总各一条样本。

用法（仓库根目录）：
    set PYTHONPATH=src
    python tests/manual/sse_capture.py
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading
import time
import urllib.request

# 探针前置 PATH：PowerShell / npm 入口在默认 PATH 时可能解析不到。
os.environ["PATH"] = (
    r"C:\Windows\System32\WindowsPowerShell\v1.0;"
    r"C:\Users\fanli\AppData\Roaming\npm;" + os.environ["PATH"]
)

from freeagent.services.opencode_server import OpenCodeServer  # noqa: E402
from freeagent.services.opencode_server import current_adapter  # noqa: E402

BRIEF = (
    "在当前目录读一下 README.md 的第一行；然后运行一次 `python --version` "
    "把版本号告诉我。不要修改任何文件。"
)

TERMINAL = {
    "session.execution.succeeded",
    "session.execution.failed",
    "session.idle",
    "session.error",
}


def main() -> None:
    work = pathlib.Path(tempfile.mkdtemp(prefix="fa-sse-"))
    (work / "README.md").write_text("# probe\nhello sse\n", encoding="utf-8")

    frames: list[dict] = []
    done = threading.Event()

    server = OpenCodeServer(
        project=work, isolate=False, apply_gate=False,
        executable="opencode", startup_timeout=60.0,
    )
    try:
        server.start()
        sid = server.create_session()
        api = current_adapter().api
        url = f"{server.base_url}{api.events_path}"
        print(f"[probe] sid={sid} events={url}")

        req = urllib.request.Request(url)
        req.add_header("Authorization", server._auth)

        def reader() -> None:
            with urllib.request.urlopen(req, timeout=180.0) as resp:
                for raw in resp:
                    if done.is_set():
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        frames.append({"_parse_error": payload[:200]})
                        continue
                    etype = obj.get("type", "")
                    frames.append({"type": etype, "data": obj.get("data")})
                    if etype in TERMINAL:
                        done.set()
                        break

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        time.sleep(1.0)

        server.prompt_async(sid, BRIEF, model="opencode/big-pickle")

        deadline = time.monotonic() + 160.0
        while not done.is_set() and time.monotonic() < deadline:
            time.sleep(0.5)
        done.set()
        th.join(timeout=5.0)

        out = pathlib.Path(__file__).with_name("sse_capture.jsonl")
        out.write_text(
            "\n".join(json.dumps(f, ensure_ascii=False) for f in frames),
            encoding="utf-8",
        )

        # 汇总：每种事件一条样本 + 计数。
        samples: dict[str, dict] = {}
        counts: dict[str, int] = {}
        for f in frames:
            t = f.get("type", "_parse_error")
            counts[t] = counts.get(t, 0) + 1
            if t not in samples:
                samples[t] = f.get("data")

        print("\n=== counts ===")
        for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{n:4d}  {t}")
        print("\n=== samples ===")
        for t, data in samples.items():
            print(f"\n## {t}")
            print(json.dumps(data, ensure_ascii=False, indent=1)[:1200])
    finally:
        server.close()
        print(f"\n[probe] workdir={work}")


if __name__ == "__main__":
    main()
