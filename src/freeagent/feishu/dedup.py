"""跨重启的事件去重 —— 落盘，带 TTL 与条数上限。

## 为什么必须有它

飞书长连接**重连时会重投近期事件**。原实现是进程内 ``OrderedDict``
（上限 512、无 TTL），并在注释里说「重启后忘掉是已知且可接受的，
飞书重投只发生在短时间内」。

**这个权衡恰好在重连场景下不成立**：重启 → 立刻重连 → 收到重投，
正落在「只会短时间内重投」的窗口内，而后果是重复建事务 ——
也就是去重本身要防的那件事。所以去重必须跨重启（设计方案 11.9.2）。

## 两个原本没有的东西

- **TTL**：原来只有「上限 512 条」这一个约束，对低频的私人 bot 而言
  512 条可能是好几个月 —— 窗口既不明确也不可解释。改成 24 小时之后，
  语义是「24 小时内不重复处理」，与飞书实际重投窗口对齐。
- **落盘**：见上。

## 损坏处理

坏文件**改名留现场**（``.corrupt``）后以空表启动，不抛异常。
理由：去重表只是优化的缓存，**不该成为启动失败的理由** —— 而静默删掉
又会让「为什么去重没生效」永远查不出来。两者兼顾就是留证据 + 继续跑。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from .config import state_home

__all__ = [
    "SeenEventStore",
    "DEDUP_TTL_HOURS",
    "DEDUP_MAX_ENTRIES",
    "CACHE_FILE_NAME",
    "default_path",
]

#: 超过这个小时数的 event_id 不再视为可能重投。24 小时是对齐飞书
#: 重投窗口取的保守值。
DEDUP_TTL_HOURS = 24

#: 最多记多少条。超了按时间从旧到新剪 —— 私人 bot 的正常量远小于这个数，
#: 上限只为「有人拿它灌垃圾」兜底。
DEDUP_MAX_ENTRIES = 2048

CACHE_FILE_NAME = "feishu_seen_events.json"


def default_path(home: str | Path | None = None) -> Path:
    """落盘路径。目录不存在则返回预期路径（不创建）。"""
    return state_home(home) / CACHE_FILE_NAME


class SeenEventStore:
    """记住见过的 ``event_id``。**进程重启后仍然记得**。

    每个方法都不抛异常 —— 这是个优化缓存，不该把调用方拖垮。
    写盘失败只记到 :attr:`last_error`，由调用方（doctor / 启动日志）决定
    要不要告诉用户。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: float = DEDUP_TTL_HOURS * 3600,
        max_entries: int = DEDUP_MAX_ENTRIES,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        self.ttl = float(ttl_seconds)
        self.max_entries = int(max_entries)
        # ``now`` 是**可调用对象**而不是时间戳。踩过的坑：原先标注写成
        # ``float | None``，而这里当函数用（``self._now()``）—— 标注和用法
        # 对不上，且所有既有测试都传 lambda，于是没人发现。按标注传
        # ``now=0.0`` 会在第一次 ``self._now()`` 处抛 ``TypeError``。
        # 判 ``is None`` 而不是 ``or``：与可调用对象无关，但语义更准。
        self._now = time.time if now is None else now
        self._seen: dict[str, float] = {}
        self.last_error: str = ""
        self._load()

    # -- 查询 ---------------------------------------------------------------- #
    def is_duplicate(self, event_id: str) -> bool:
        """见过就返回 ``True``，并把「见过」这件事记下（含这次的）。"""
        if not event_id:
            return False
        now = self._now()
        self._evict(now)
        if event_id in self._seen:
            # 重复投递：刷新时间戳，让反复重投的那条不会因为一次旧时间戳
            # 被剪掉、然后下一轮又被当成新事件。
            #
            # 踩过的坑（这个bug 恰好是本模块存在的理由被自己破坏）：一开始只在
            # 内存里刷新、**不落盘**。于是「同一进程里连续重投、跑满 TTL 之后
            # 重启」这条路径上，磁盘上仍是最初那个旧时间戳，重启后一读就被
            # TTL 剪掉 —— 那条重投事件被当成新事件重复处理，正是 11.9.2 要
            # 防的事。重复投递只在重连时发生、不常有，所以这里的额外写盘可以
            # 接受：拿一次罕见的小写入，换掉一整类重复建事务。
            self._seen[event_id] = now
            self._flush()
            return True
        self._seen[event_id] = now
        self._trim()
        self._flush()
        return False

    def __len__(self) -> int:
        return len(self._seen)

    # -- 剪枝 ---------------------------------------------------------------- #
    def _evict(self, now: float) -> None:
        cutoff = now - self.ttl
        stale = [k for k, t in self._seen.items() if t < cutoff]
        for key in stale:
            del self._seen[key]

    def _trim(self) -> int:
        """超量时**删最旧的**，返回丢了几条。

        踩过的坑：原先写的是 ``key=self._seen.get``。运行时没毛病（键必然
        存在，``get`` 返回 ``float``），但 ``dict.get`` 有一堆重载，类型检查
        匹配不出唯一签名。改成显式下标，顺带把「按时间升序」写明白。

        **返回丢弃条数**，是为了让 :meth:`_load` 能留证据。
        """
        overflow = len(self._seen) - self.max_entries
        if overflow <= 0:
            return 0
        oldest = sorted(self._seen, key=lambda k: self._seen[k])
        for key in oldest[:overflow]:
            del self._seen[key]
        return overflow

    # -- 落盘 ---------------------------------------------------------------- #
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            self.last_error = f"{self.path.name} 读不了（{exc.strerror}），以空表启动"
            return
        if not raw.strip():
            # 空文件当空表，**不**算损坏。写盘走的是原子替换，本来不该出现
            # 空文件；但用户 ``touch`` 一个、或编辑器建了个空壳都很常见。
            # 此时**没有内容值得留现场**，改名反而白吓一跳。返回即可。
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._quarantine()
            return
        if not isinstance(payload, dict):
            self._quarantine()
            return
        entries = payload.get("seen")
        if not isinstance(entries, dict):
            self._quarantine()
            return
        now = self._now()
        for key, stamp in entries.items():
            if isinstance(key, str) and isinstance(stamp, (int, float)):
                self._seen[key] = float(stamp)
        self._evict(now)                  # 读进来就先按 TTL 剪一次
        dropped = self._trim()
        if dropped:
            # 留证据。**超量被裁与文件损坏同属「内容没按预期读到」** ——
            # 损坏会 quarantine 并写 last_error，裁剪原先却一声不吭。
            # 两者不一致的后果是：表被裁过的用户完全无从知道，症状是
            # 「去重时灵时不灵」，与 11.9.2 当初要防的正是同一件事。
            # 上限本身只为兜底灌垃圾，正常的量远小于它，所以裁掉不影响
            # 正确性 —— 但「不影响」不等于「不用讲」。
            self.last_error = (
                f"{self.path.name} 里有 {dropped} 条超出上限 {self.max_entries}，"
                "已裁掉最旧的（上限只为兜底灌垃圾，正常量远小于它）"
            )

    def _quarantine(self) -> None:
        try:
            self.path.replace(self.path.with_suffix(".json.corrupt"))
            self.last_error = f"{self.path.name} 格式不对，已改名留现场并以空表启动"
        except OSError as exc:
            self.last_error = f"{self.path.name} 读不了（{exc.strerror}），以空表启动"

    def _flush(self) -> None:
        """原子写。断电或崩溃不会留下半截文件。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    {"seen": self._seen},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            # 写不下去不抛：去重退化成「只在本进程内有效」，正是改造前的
            # 行为，可以接受。下一条消息还会再试。
            self.last_error = f"写不了 {self.path.name}（{exc.strerror}），本次去重只在本进程有效"
