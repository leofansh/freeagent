"""目录访问走飞书确认 —— 最小闭环。

这是**只读**的那一档（读目录）。但闸门必须是同一条：凡是要动本地文件系统的
动作，**先问、过时才做**。所以这里刻意不提供 ``skip_confirmation`` 参数 ——
有那个参数就等于「默认允许、偶尔确认」，而默认必须是拒绝。

三段式，与设计文档 11.9.4 一致：

    ask（落库，带凭据）→ 发卡（凭据嵌进按钮 value）→ 轮询等答复

**轮询而不是挂起**：等待方和桥接是两个进程，内存里的同步原语跨不过去；
落盘 + 轮询天然跨进程、跨重启，且桥接挂掉时请求方读到的是 deny 而不是无限等。
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .approval import DEFAULT_TTL_SECONDS, ApprovalStore

__all__ = ["Confirmation", "DirectoryAccess", "DirectoryRefused", "DEFAULT_TTL_SECONDS"]


class DirectoryRefused(RuntimeError):
    """用户拒绝、或没在有效期内答。**两者都走这一条**，不区分。

    刻意合并：调用方不该靠 ``timeout=True/False`` 去做决策 ——
    那等于让它有机会把「没答」当「答了」。要看区别就去查 ``pending_approvals``。
    """


class CardSender(Protocol):
    """能发确认卡的东西。刻意**只要求发卡**。

    不要求「更新卡片」：那需要额外 API 权限，而失败会拖慢应答。
    卡上留着旧按钮无害 —— 凭据过期后再点会被丢弃（有测试）。
    """

    def send_approval_card(
        self, *, open_id: str, subject: str, detail: str,
        credential: str, ttl_seconds: int,
    ) -> str:
        """发卡，返回 ``message_id``（可为空字符串）。"""
        ...


@dataclass(frozen=True, slots=True)
class Confirmation:
    """一次确认的结果。"""

    granted: bool
    credential: str
    decided_by: str | None
    reason: str          # 允许 / 拒绝 / 过期未答 / 凭据丢失


@dataclass(frozen=True, slots=True)
class DirectoryAccess:
    """需要确认才能碰本地目录的闸门。

    **只做只读列举。** 写入/删除走别的路径（那属于委派，见 docs 11.8），
    这里刻意不做 —— 一次只加一条能力，加完实测通了再加下一条。
    """

    store: ApprovalStore
    sender: CardSender
    approver_id: str                 # 该问谁（open_id）
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    clock: Callable[[], datetime.datetime] = datetime.datetime.now
    sleep: Callable[[float], None] = __import__("time").sleep
    log: Callable[[str], None] = lambda _m: None

    def request(self, directory: Path) -> Confirmation:
        """就「只读列出这个目录」请求一次确认。**先问、过时才做。**"""
        subject = f"只读列出目录 {directory}"
        detail = (
            f"目录：`{directory}`\n"
            f"动作：**只读列出**（不写、不删）\n"
            f"授权范围：**仅这一次**"
        )
        item = self.store.ask(subject, detail=detail, ttl_seconds=self.ttl_seconds)
        self.log(f"【确认】已登记 {item.credential}：{subject}")

        try:
            msg_id = self.sender.send_approval_card(
                open_id=self.approver_id,
                subject=subject,
                detail=detail,
                credential=item.credential,
                ttl_seconds=self.ttl_seconds,
            )
        except Exception as exc:
            # 发卡失败 → 立刻当拒绝。**不能「没问成就当允许」**。
            self.log(f"【确认】发卡失败（{exc}）→ 当作拒绝")
            return Confirmation(False, item.credential, None, "发卡失败")

        if msg_id:
            self.store.record_card(item.credential, msg_id)

        self.log(f"【确认】卡片已发 {msg_id or '(无 id)'}，等答复…")
        # 轮询的超时**由 clock 决定**，不用挂钟。
        #
        # 踩过的坑（实测）：原先直接 ``store.wait(...)``，而它内部用
        # ``time.monotonic()`` 判超时 —— 注入的 FakeClock 怎么推进都不影响它，
        # 于是「超时当拒绝」那条测试**真的去等满 600 秒**，套件卡死十分钟。
        # 结论：凡是要被测的时限，**判据必须走注入的那个钟**。
        decision = self._await(item.credential, self.ttl_seconds)
        got = self.store.get(item.credential)
        who = got.decided_by if got is not None else None

        if decision == "allow":
            self.log(f"【确认】已允许（{who}）")
            return Confirmation(True, item.credential, who, "允许")
        reason = "拒绝" if (got is not None and got.decision == "deny"
                            and who and who != "expired") else "过期未答"
        self.log(f"【确认】{reason}")
        return Confirmation(False, item.credential, who, reason)

    def _await(self, credential: str, timeout_seconds: int) -> str:
        """等答复。**超时用注入的钟判**，不用 ``time.monotonic``。

        为什么不用现成的 :meth:`ApprovalStore.wait`：它按挂钟算超时，
        而测试注入的是 ``FakeClock`` —— 两者各走各的，于是「超时当拒绝」
        那条用例会真的睡满整个 ttl。这里改成按同一个 clock 判，
        于是测试推进时钟就等于推进等待。
        """
        deadline = self.clock() + datetime.timedelta(seconds=timeout_seconds)
        while True:
            got = self.store.decide(credential)
            if got is not None:
                return got
            if self.clock() >= deadline:
                # 边界上可能恰好答了，最后再问一次。
                got = self.store.decide(credential)
                return got if got is not None else "deny"
            self.sleep(0.5)

    def list_dir(self, directory: Path) -> list[str]:
        """确认通过才列目录。**不通过就抛，不静默返回空列表。**

        抛而不是返回 ``[]``：返回空列表的话，「被拒绝」和「目录真是空的」
        长得一模一样 —— 而这两个后果完全相反。
        """
        conf = self.request(Path(directory))
        if not conf.granted:
            raise DirectoryRefused(
                f"未获授权，未访问 {directory}（{conf.reason}"
                + (f"，凭据 {conf.credential}" if conf.credential else "")
                + "）"
            )
        try:
            names = sorted(p.name for p in Path(directory).iterdir())
        except OSError as exc:
            raise DirectoryRefused(f"允许了但读不了 {directory}：{exc}") from exc
        self.log(f"【确认】已列出 {directory}：{len(names)} 项")
        return names
