"""桥接运行状态：写一个 JSON 文件，给控制面读。

**为什么是文件而不是 HTTP 端点**（设计方案 12.7）：起一个 HTTP 端点就得处理
端口冲突与 CSRF，为「看一眼状态」这点便利引入攻击面不划算。文件在
``state_home`` 里，零网络暴露面。

**为什么必须有 ``updated_at``**：进程被强杀时这个文件会**留在磁盘上**。
界面若照着它显示「已连接」，就是对着一个几小时前死掉的进程报健康 ——
那比没有状态更坏，因为它给出虚假的安心。所以 :func:`is_stale` 是本模块
存在的理由之一，不是附加功能。

**为什么状态判定只有一处**：桥接写它、界面读它、`doctor` 也要查它。
三处各判一次必然漂移，而漂移的表现是「界面说连着、doctor 说没连」，
用户无从判断该信谁 —— 这种分裂比没有界面更糟（见 11.9.1 的同源教训）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "STATUS_FILE_NAME",
    "STALE_AFTER_SECONDS",
    "status_path",
    "write_status",
    "read_status",
    "is_stale",
    "config_revision",
    "restart_required",
    "CONFIG_REVISION_FIELD",
    "SEEN_SENDERS_KEY",
    "record_sender",
    "StatusReporter",
]

#: 状态文件名。放在 state_home，与身份缓存、去重表同目录。
STATUS_FILE_NAME = "bridge_status.json"

#: 超过这个秒数没心跳就视为「陈旧」。
#:
#: 取 90 秒的依据：桥接的 ``--verbose`` 日志里心跳/重连是分钟级的，
#: 而界面需要「秒级」才能反映出问题。90 秒既能容忍一次重连，又不至于
#: 让界面在进程真死之后还显示几分钟「已连接」。
STALE_AFTER_SECONDS = 90.0


def status_path(home: str | Path | None = None) -> Path:
    """状态文件放哪。

    复用 :func:`freeagent.feishu.config.state_home`，与身份缓存、去重表
    **同一套路径解析** —— 复制粘贴的路径解析迟早漂移，而漂移的表现是
    「桥接写 A 目录、控制面读 B 目录」，于是状态永远显示「未运行」。
    """
    from .config import state_home

    return state_home(home) / STATUS_FILE_NAME


def write_status(path: Path, payload: dict[str, Any]) -> None:
    """原子写状态文件。写失败**只记日志**，绝不让它掀翻桥接。

    踩过的坑会很直觉：这里抛异常的话，状态上报这种「纯观测」的功能
    就能把主流程搞崩。所以宁可不写，也必须不炸。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)          # 原子：要么旧的完整内容，要么新的
    except OSError:
        # 清理半截文件，别留下垃圾
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_status(path: Path) -> dict[str, Any] | None:
    """读状态。**读不到不是错误** —— 桥接可能根本没起过。

    返回 ``None`` 而不抛异常：控制面要区分「桥接没跑」和「桥接出问题了」，
    而这两者都不该让页面 500。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 半截文件（写盘途中被强杀）不该让界面报错 —— 当「陈旧」处理即可。
        return None
    if not isinstance(data, dict):
        return None
    return data


def is_stale(status: dict[str, Any] | None, *, now: float, after: float = STALE_AFTER_SECONDS) -> bool:
    """状态是否已陈旧（即：桥接很可能已经不在了）。

    ``status`` 为 ``None``（文件不存在/读不了）也算陈旧 —— 「没写状态」
    和「状态很旧」对用户是同一件事：桥接没在跑。
    """
    if status is None:
        return True
    updated = status.get("updated_at")
    if not isinstance(updated, (int, float)):
        # 没有心跳时间戳 = 不可信。宁可说「陈旧」也不要假装健康。
        return True
    return (now - float(updated)) > after


#: 心跳间隔。必须**明显小于** :data:`STALE_AFTER_SECONDS`，否则正常运行时
#: 界面也会间歇性显示「已陈旧」，而那会立刻被当成 bug。
HEARTBEAT_SECONDS = 15.0

#: 状态文件里记录「这个进程启动时看到的配置」的那个字段。
CONFIG_REVISION_FIELD = "config_revision"

#: 状态文件里「最近见过的发送者」那个字段。
SEEN_SENDERS_KEY = "seen_senders"


def record_sender(
    seen: dict[str, dict[str, Any]], msg: Any, *, keep: int = 20
) -> None:
    """记下「这个发送者带着哪些 ID」，供控制面显示。

    ## 为什么要记

    白名单最大的坑是**「不知道该填什么」**。飞书同一个人有三层 id
    （``open_id`` 应用级 / ``user_id`` 租户级 / ``union_id``），换应用
    ``open_id`` 就变、``user_id`` 不变。填错的表现是「bot 一句话都不回」，
    而用户连自己的 id 是多少都不知道。

    所以把**实际从事件里读到的**标识原样记下来，界面上照着填。
    这比文档里写「去日志里取 open_id」有用得多 —— 那条路径要求用户
    会看日志、会在多条日志里认出哪条是自己。

    刻意**只记身份、不记内容**。消息正文有隐私，而这个表只回答「你是谁」。

    刻意**记满三层**，不只记能匹配上的那层：用户看到自己
    ``ou_xxx`` / ``user_id=yyy`` 都在，才理解「填哪个都行」以及
    「哪个跨应用不变」。

    刻意**不记录群成员**：桥接只在群聊门控通过后才调它（见
    ``FeishuBridge.on_message``），所以这张表是「主动找过 bot 的人」，
    不是「bot 所在群的所有人」。

    :data:`SEEN_SENDERS_KEY` 之外的一切都不写 —— 状态文件是纯观测产物。
    """
    ids = {
        "open_id": getattr(msg, "sender_open_id", "") or "",
        "user_id": getattr(msg, "sender_user_id", "") or "",
        "union_id": getattr(msg, "sender_union_id", "") or "",
    }
    key = next((v for v in ids.values() if v), "")
    if not key:
        return                              # 没有任何身份，记它没有意义
    seen[key] = {
        "open_id": ids["open_id"],
        "user_id": ids["user_id"],
        "union_id": ids["union_id"],
        "is_group": bool(getattr(msg, "is_group", False)),
        "seen_at": time.time(),
    }
    if len(seen) > keep:
        # 丢最久没见到的。显式取 float：``seen_at`` 的类型是 ``object``，
        # 交给 sorted 排会静默退化到比较 str —— 那样「最久」就成了「最小字符串」，
        # 于是丢掉的是**最早出现的**而不是最久没见的。两者在真实数据里不同。
        def last_seen(key: str) -> float:
            value = seen[key].get("seen_at")
            return float(value) if isinstance(value, (int, float)) else 0.0

        for stale in sorted(seen, key=last_seen)[: len(seen) - keep]:
            seen.pop(stale, None)


def config_revision(home: str | Path | None = None) -> int:
    """当前 ``feishu.env`` 的修订号：它的 ``mtime_ns``；没文件就是 0。

    **为什么是 mtime 而不是内容哈希**：哈希（哪怕 sha256）本身就是一个
    **验证器** —— 拿到状态文件的人可以拿它离线试候选 Secret。飞书 Secret
    是 32 位随机串，暴力不现实，但白送一个验证器没有任何好处。mtime 只说明
    「这文件什么时候被写过」，不泄露值的任何信息。

    代价是「碰一下文件就提示要重启」。这个方向的错是**安全**的：多提醒一次
    总比让人以为新配置已经生效要好。
    """
    from .secret_store import env_path

    try:
        return env_path(home).stat().st_mtime_ns
    except OSError:
        return 0            # 没有文件 = 从没配过，修订号 0


def restart_required(
    status: dict[str, Any] | None, *, home: str | Path | None = None
) -> bool:
    """盘上的配置是否已经比正在跑的进程所依据的更新。

    桥接启动时把 :func:`config_revision` 记进状态文件，这里拿它跟盘上
    现在的值比。不同 = 你刚改的配置**还没生效**，界面必须说出来 ——
    12.7 要防的「以为保存了就是生效了」正是这一条。

    两种情况判「不需要重启」：

    - **拿不到状态**（没跑过 / 文件坏了）：界面本来就显示「未运行」，
      再挂一句「需要重启」是纯噪音。
    - **状态里没有这个字段**（旧版桥接）：不知道就是不知道，不猜。
    """
    if not isinstance(status, dict):
        return False
    recorded = status.get(CONFIG_REVISION_FIELD)
    if not isinstance(recorded, int):
        return False
    return recorded != config_revision(home)


class StatusReporter:
    """周期性把桥接状态写进文件。

    用法::

        reporter = StatusReporter(status_path(home))
        reporter.update(state="starting", connected=False)
        reporter.start()
        ...
        reporter.stop()

    **写失败绝不抛**：状态上报是纯观测功能，它掀翻主流程是本末倒置。
    """

    def __init__(
        self,
        path: Path,
        *,
        interval: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.interval = interval
        self._clock = clock
        self._fields: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, **fields: Any) -> None:
        """更新字段并**立刻**写一次。

        立刻写而不是等下个心跳：启动阶段那些状态（身份探到没有、锁占着没有）
        正是用户最急着看的，等 15 秒才出现会让人以为界面坏了。
        """
        with self._lock:
            self._fields.update(fields)
            snapshot = dict(self._fields)
        self._write(snapshot)

    def start(self) -> None:
        """起心跳线程。已在跑时**什么都不做**（不重复起线程）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="feishu-status", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """停心跳。

        刻意**不**把状态改写成 ``down``：进程被杀时根本来不及写，那种情况下
        文件会留在盘上。界面靠 :func:`is_stale` 判活，而不是靠这里有没有
        写终态 —— 所以「陈旧判定」是必须的，这个方法只负责不留下孤儿线程。
        """
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._fields)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                snapshot = dict(self._fields)
            self._write(snapshot)

    def _write(self, snapshot: dict[str, Any]) -> None:
        payload = dict(snapshot)
        payload["updated_at"] = self._clock()
        payload.setdefault("pid", os.getpid())
        try:
            write_status(self.path, payload)
        except Exception:              # noqa: BLE001 - 观测代码不该掀翻主流程
            # 静默失败会和「没写状态」一样让人困惑，所以留一条日志。
            logging.getLogger("freeagent.feishu").warning(
                "状态文件写失败：%s（控制面会显示「未运行」）", self.path,
                exc_info=True,
            )
