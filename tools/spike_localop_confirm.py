"""端到端：飞书确认 → 真的列出那个目录。

用**真的** FeishuSender（真凭据、真网络）和**真的** ApprovalStore，
只把「等答复」这一步换成轮询真库 —— 因为那一步要人来点，点不了。

跑法::

    set PYTHONIOENCODING=utf-8
    python tools\\spike_localop_confirm.py

流程：
    1. 登记待确认（落库，带凭据）
    2. 发真卡片到你的飞书
    3. 轮询真库等你点
    4. 点了「允许」→ 列出目录；「拒绝」/超时 → 明确拒绝

超时默认拒绝，且**不点也算拒绝** —— 这条在代码里有守卫，这里再走一遍真链路。
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from freeagent.app import build_app                                    # noqa: E402
from freeagent.feishu.config import load_config                        # noqa: E402
from freeagent.feishu.sender import FeishuSender                        # noqa: E402
from freeagent.services.approval import ApprovalStore, DEFAULT_TTL_SECONDS  # noqa: E402
from freeagent.services.localops import DirectoryAccess, DirectoryRefused   # noqa: E402

#: 故意用一个**和委派白名单无关**的目录 —— 这样「要确认才访问」是真的，
#: 不是靠「反正它在白名单里所以不用问」蒙混过去。
TARGET = pathlib.Path(r"D:\PycharmProjects\freeagent\docs")

TTL = 300


def main() -> int:
    # 不传 env —— merged_env 显式收到 env 就不读 feishu.env（config.py:165）
    cfg = load_config()
    if not cfg.app_id:
        print("!! 没读到凭据，先配好 feishu.env")
        return 2
    approver = next(iter(sorted(cfg.allowed_users)), "")
    if not approver:
        print("!! 白名单为空，没人能批准")
        return 2
    print(f"批准人={approver}  目标={TARGET}  超时={TTL}s")

    app = build_app()
    store = ApprovalStore(app.conn)
    sender = FeishuSender(cfg)
    said: list[str] = []

    access = DirectoryAccess(
        store=store, sender=sender, approver_id=approver,
        ttl_seconds=TTL, sleep=time.sleep,
        log=lambda m: (print("   " + m), said.append(m)),
    )

    print(f"\n目标目录里现在有 {len(list(TARGET.iterdir()))} 项（这一步没经过确认，"
          f"只是告诉你待会儿会看到什么）")

    try:
        names = access.list_dir(TARGET)
    except DirectoryRefused as exc:
        print(f"\n❌ 被拒绝：{exc}")
        print("\n这是**正确**行为：不点 / 点拒绝 / 超时，都是拒绝。")
        return 1

    print(f"\n✅ 已列出 {len(names)} 项：")
    for n in names[:20]:
        print(f"   - {n}")
    if len(names) > 20:
        print(f"   …（还有 {len(names) - 20} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
