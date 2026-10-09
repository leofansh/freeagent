"""配对流程的**终端侧**：核销一个配对码，把对方写进白名单。

## 分层：为什么这个文件在 ``feishu/`` 而不在 ``services/``

``services/pairing.py`` 里的 :class:`~freeagent.services.pairing.PairingStore`
是**纯库操作**（签发 / 核销 / 清扫），不碰任何传输层的东西，所以放在
``services``。而「把 id 写进 ``feishu.env``」是**飞书配置**的知识 ——
键名、白名单上限、优先级规则都在这一侧，所以这一半必须住在这里。

反过来 ``services`` 就不该import 本包：那会让「核销一个码」这种纯库动作
依赖飞书通道，而 ``lark-oapi`` 是**可选依赖**。

⚠️ 本模块**不import** ``lark_oapi``（只碰 :mod:`secret_store` 与
:mod:`config`，两者都是纯 stdlib），所以终端 CLI 能在没装飞书 SDK 的
机器上使用它 —— 而这正是它的主要使用场景。

## 三条不能违反的

1. **它只是「怎么拿到 open_id」，不是「拿不到也放行」。**
   白名单为空仍然拒绝启动（:func:`freeagent.feishu.config.check_ready`），
   配对不碰那条判定。
2. **码一次性 + 短 TTL** —— 核销在 :class:`PairingStore` 里做，
   这里不重复实现过期判定。
3. **成功之后必须说清「还要重启」** —— 白名单是构造期定死的
   ``frozenset``，写进文件不会让**已经在跑的**桥接立刻认这个人。
   说不清的话，用户会以为配对失败了，去反复重配。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..services.pairing import PairingError, PairingStore
from .config import ENV_ALLOWED, MAX_ALLOWED
from .secret_store import read_env, write_env

__all__ = ["pair_command"]


def pair_command(
    conn,
    code: str,
    *,
    home: str | Path | None = None,
    clock=None,
) -> str:
    """核销 ``code``，把对应的 ``open_id`` 追加进白名单。返回给用户的一句话。

    **成功之后仍然要重启桥接** —— 见模块说明第3 条。

    ``clock`` 与 :class:`PairingStore` 同义（一个返回 ``datetime`` 的可调用）。
    暴露它不是为了「方便测试」，而是**因为不暴露就没法测**：内部自建
    ``PairingStore`` 时若不传时钟就会用真实时间，于是「10 分钟内有效」
    这条规则在测试里变成「签发即过期」。
    """
    raw = (code or "").strip()
    if not raw:
        return (
            "用法：/pair <配对码>。"
            "码从哪儿来：给 bot 发一条**私聊**，它会回一张带配对码的卡。"
            "（群里不会发 —— 群里发卡等于向全群确认「这里有个 bot」。）"
        )

    try:
        open_id = PairingStore(conn, clock=clock).consume(raw)
    except PairingError as exc:
        return f"配对没成：{exc}"

    existing = read_env(home).get(ENV_ALLOWED, "")
    entries = [part.strip() for part in existing.split(",") if part.strip()]

    if open_id in entries:
        # 不报错，也不重复写。理由：重复写会让白名单悄悄变长，
        # 而它有个上限（本模块末尾那条检查）—— 上限撞了之后加不进新人，
        # 症状是「配对说成功了但还是进不来」，极难查。
        return (
            f"{open_id} 已经在白名单里了，不用重配。"
            "（如果还是进不来，看下面这条：桥接要重启才认新人。）"
        )

    if len(entries) >= MAX_ALLOWED:
        # 刻意**拒绝**而不是静默挤掉一个。白名单是「谁能指挥本机」的
        # 信任边界，超上限时让人自己清理，比自动删一个安全。
        return (
            f"白名单已经有 {len(entries)} 个（上限 {MAX_ALLOWED}），不加了。"
            f"请先手工删掉不再用的：{ENV_ALLOWED}（在 {home or '默认目录'} 的 "
            "feishu.env 里）"
        )

    entries.append(open_id)
    write_env({ENV_ALLOWED: ",".join(entries)}, home)

    # ⚠️ 环境变量**优先于**文件（见 config.merged_env）。若它设着，
    # 这次写盘不会生效 —— 必须在成功话术里说，否则用户会以为配对成了、
    # 却始终被拒，然后反复重配。
    env_wins = bool(os.environ.get(ENV_ALLOWED, "").strip())
    lines = [f"已把 {open_id} 写进白名单（{ENV_ALLOWED}）。"]
    if env_wins:
        lines.append(
            f"⚠️ 但**环境变量 {ENV_ALLOWED} 也设着，而它优先于文件** —— "
            "所以文件这次不生效。要么去掉那个环境变量，要么把它也加上这个 id。"
        )
    lines.append("桥接是**另一个进程**，要重启它才认新人（白名单是启动时读的）。")
    return "\n".join(lines)