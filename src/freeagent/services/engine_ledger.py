"""执行器的改动账 —— 「这一次它到底动了什么」（12.10.13）。

## 为什么要有这个模块

委派收尾时 ``ok=True`` 的判据是**排除法**：没有会话错误、没有未答提问、
不是用户叫停、没超步数、没超时长。五条**全是「没出坏消息」**，没有一条
问「它到底动了什么」。于是一个 agent 读一圈文件、回一段话、一个字没改，
正常退出，也会被记成「完成」—— 人据此去验收，验收的是空气。

## 证据从哪来：走执行器的公开接口，不读它的库

早期版本直接读 ``opencode.db`` 的 ``part`` 表。**这条路在 V2 上是错的**，
2026-11-03 对着真机 2.0.29 实测：

- 同一个会话，``part`` 表 **0 行**、``session_message`` 表 **333 行**。
  V2 把消息搬了家，按老表名读的代码不会报错 —— 它会安静地给**每一次**
  委派报「零改动」。这是本模块最坏的失效形态：看起来在工作。
- 官方 OpenAPI（``/v2/docs/api``）有 ``session.message.list``，返回的
  每条 assistant 消息里带着 ``content[].type == "tool"``，其中
  ``state.input.filePath`` 是改过的文件、``state.metadata.exit`` 是退出码。
  实测一个会话 431 次工具调用（``shell`` 105 / ``edit`` 16 / ``write`` 10
  / ``read`` 40 / ``grep`` 23），全部可读。
- **收尾那一刻服务还在**：调用点在 ``with make_server(...) as oc`` 之内，
  ``oc.close()`` 在退出 ``with`` 时才发生。所以「服务可能已经关了」不成立。
- 跨目录可查：在 ``freeagent`` 目录查到了 ``openmos`` 的会话，所以
  「会话有项目作用域」也不构成障碍。

接口是**有版本承诺的那一层**（表名不是）。读库唯一的优势是「服务挂了也能读」，
而服务挂了这种情况本身就该报 ``unreadable`` —— 不是读库的理由，是失败可见
的理由。

**未采用**：``session.get`` 回的 ``outcome``（``succeeded`` / ``failed``）。
它是执行器自己的判定，与本仓那五条判据并列会变成「两个完成」；工具账只报
证据，判定仍由本仓一处说。

## 三值，不是布尔

``changed`` / ``none`` / ``unreadable`` 必须分开：

- ``none``（零改动）现在被报成「完成」—— 这是本模块要修的那一类错判。
  但它**不是失败**：有些委派本来就不该改文件（查一件事、答一个问题）。
  判成失败会让人去排查一个不存在的 bug。
- ``unreadable``（读不到）**不等于**零改动。连不上服务、会话查不到、
  返回形状不认识 —— 都必须说出来，不许落成「没改」。同
  ``executor_credentials._read_providers`` 的 ``None`` 与 ``()``。

## 只报观察，不报结论

账里只有「哪个文件被写/编辑过几次」「几条命令非 0 退出」。
**「改了 160 个文件」不等于「改对了」** —— 实测一个 session 改 160 个
文件、同时 96 条命令非 0 退出。批不批、改得对不对，是人的事（11.9.12）：
标注说的是证据，不是判断的正确性。
"""

from __future__ import annotations

import dataclasses
import json
import urllib.parse
from typing import Any, Literal, Protocol

__all__ = [
    "ChangeState",
    "CHANGED",
    "NO_CHANGE",
    "UNREADABLE",
    "EngineLedger",
    "LedgerSource",
    "read_session_ledger",
    "render_ledger",
]

#: 这次委派的改动账有几种读法。**三者不可合并**，理由见模块文档。
ChangeState = Literal["changed", "none", "unreadable"]

CHANGED: ChangeState = "changed"
#: 查到了会话、但里面一次写操作都没有。**不是失败**，只是不等于做完。
NO_CHANGE: ChangeState = "none"
#: 读不到（没会话号 / 连不上 / 会话不存在 / 形状不认识）。**不等于没改**。
UNREADABLE: ChangeState = "unreadable"

#: 会改动磁盘的工具名。只认这几个 —— 认不出的一律不计，
#: 漏一个的代价是少报一个文件，多认一个的代价是把「读过」说成「改过」。
#:
#: V2 把权限里的 ``bash`` 改名成 ``shell``（见 ``executors.V2_FIELD_RENAMES``），
#: 但**工具名本身两版都出现过**，所以两个都收。
_WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "patch", "apply_patch"})
_SHELL_TOOLS = frozenset({"bash", "shell"})

#: 一页拉多少条消息。实测 200 条 ≈ 950KB，100 条是传输量与页数的折中。
_PAGE_LIMIT = 100

#: 最多翻几页。翻满了就标 ``truncated`` —— **不许**把半本账报成整本。
_MAX_PAGES = 30


class LedgerSource(Protocol):
    """能从执行器读消息的东西。``OpenCodeServer`` 满足它（多余参数都有默认值）。

    做成协议而不是直接收 ``OpenCodeServer``：测试要能塞一个假服务，
    否则每条用例都会去碰开发者的真服务（同 ``executor_credentials`` 的注入理由）。
    """

    def call(
        self,
        path: str,
        method: str = "GET",
        body: Any = None,
        *,
        timeout: float = 30.0,
    ) -> tuple[int, bytes]: ...


@dataclasses.dataclass(frozen=True, slots=True)
class EngineLedger:
    """一次委派的改动账。**纯数据**，不含任何判断。"""

    state: ChangeState
    #: ``(路径, 次数)``，按次数降序（同次数按路径）。只对 ``changed`` 非空。
    files: tuple[tuple[str, int], ...] = ()
    #: 非 0 退出的命令条数。三种状态下都可能是 0 —— 别把它当判据。
    bash_failed: int = 0
    #: ``truncated`` 时上面两个数是**下界**（只翻了前 ``_MAX_PAGES`` 页）。
    truncated: bool = False
    #: ``unreadable`` 时：为什么读不到。另外两种状态为空串。
    reason: str = ""

    @property
    def file_count(self) -> int:
        return len(self.files)


def _short(path: str, keep: int = 2) -> str:
    """把绝对路径压成最后 ``keep`` 段，正斜杠。

    收尾卡上写 ``src/freeagent/delegate.py`` 比 ``D:/PycharmProjects/...``
    好读，而且**不会漏出用户名**。只取最后一段不够：同名的 ``__init__.py``
    或 ``app.py`` 会混成一条。
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return path
    return "/".join(parts[-keep:])


def _messages_path(session_id: str, cursor: str | None) -> str:
    """一页消息的路径。**V2 的前缀是 ``/api``**（V1 是裸 ``/session``）。"""
    path = (
        f"/api/session/{urllib.parse.quote(session_id, safe='')}"
        f"/message?limit={_PAGE_LIMIT}"
    )
    if cursor:
        path += "&cursor=" + urllib.parse.quote(cursor, safe="")
    return path


def _fail(code: int, raw: bytes) -> EngineLedger:
    """把一次失败翻译成一句人能核对的话。**四种失败说法不同**。

    合成一句「读不到」会让人分不清该去查服务、查会话号，还是查版本 ——
    而这三件事的处置方式完全不同。
    """
    detail = raw.decode("utf-8", "replace").strip()[:200]
    if code < 0:
        return EngineLedger(UNREADABLE, reason=f"连不上执行器服务（{detail}）")
    if code == 404:
        return EngineLedger(
            UNREADABLE, reason="执行器里找不到这个会话（可能已被删除，或会话号不对）"
        )
    if code == 401:
        return EngineLedger(
            UNREADABLE, reason="执行器要求鉴权（本仓应带 Authorization 头，没带上）"
        )
    return EngineLedger(UNREADABLE, reason=f"执行器返回 HTTP {code}（{detail}）")


def read_session_ledger(
    session_id: str,
    *,
    server: LedgerSource | None = None,
    max_pages: int = _MAX_PAGES,
) -> EngineLedger:
    """按会话号读执行器的改动账。**只读，永不写盘。**

    ``server`` 缺省为 ``None`` 时返回 ``unreadable`` 而**不是**空账 ——
    「没给我服务」与「服务说没改动」是两句话。
    """
    if not session_id or not session_id.strip():
        return EngineLedger(UNREADABLE, reason="没有会话号，无从查账")
    if server is None:
        return EngineLedger(UNREADABLE, reason="没有可用的执行器服务，读不到工具账")

    counts: dict[str, int] = {}
    bash_failed = 0
    truncated = False
    cursor: str | None = None

    for _ in range(max(1, max_pages)):
        code, raw = server.call(_messages_path(session_id.strip(), cursor))
        if code != 200:
            return _fail(code, raw)
        try:
            body = json.loads(raw)
        except (TypeError, ValueError):
            return EngineLedger(
                UNREADABLE, reason="执行器的响应不是合法 JSON（版本不匹配？）"
            )
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            # 形状不认识就是读不到。**不许**当成「没有消息」—— 那会落成零改动。
            return EngineLedger(
                UNREADABLE, reason="执行器的响应里没有 data 列表（版本不匹配？）"
            )
        if not rows:
            # 实测：最后一页会给 0 条、但游标仍然非空。**空页才是终点**，
            # 只看游标会多翻一页（多一次无用请求），翻满则误标 truncated。
            break
        for message in rows:
            _tally(message, counts)
            bash_failed += _shell_failures(message)
        cursor = (body.get("cursor") or {}).get("next")
        if not cursor:
            break
    else:
        truncated = True

    if not counts:
        return EngineLedger(
            NO_CHANGE, bash_failed=bash_failed, truncated=truncated
        )
    ordered = tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return EngineLedger(
        CHANGED, files=ordered, bash_failed=bash_failed, truncated=truncated
    )


def _tool_parts(message: Any):
    """一条消息里的工具调用。形状不认识就一条都不给（跳过，不作废整本账）。"""
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool":
            yield part


def _tally(message: Any, counts: dict[str, int]) -> None:
    """把一条消息里「写过的文件」累进 ``counts``。"""
    for part in _tool_parts(message):
        if part.get("name") not in _WRITE_TOOLS:
            continue
        state = part.get("state")
        if not isinstance(state, dict):
            continue
        payload = state.get("input")
        if not isinstance(payload, dict):
            continue
        target = payload.get("filePath") or payload.get("path")
        if isinstance(target, str) and target.strip():
            counts[target] = counts.get(target, 0) + 1


def _shell_failures(message: Any) -> int:
    """一条消息里非 0 退出的命令条数。"""
    bad = 0
    for part in _tool_parts(message):
        if part.get("name") not in _SHELL_TOOLS:
            continue
        state = part.get("state")
        if not isinstance(state, dict):
            continue
        meta = state.get("metadata")
        if not isinstance(meta, dict):
            continue
        code = meta.get("exit")
        # 只认 int：``None`` 是「这条命令还在跑」或「没记退出码」，
        # 把它算成失败等于编一条不存在的错误。
        if isinstance(code, int) and code != 0:
            bad += 1
    return bad


def render_ledger(ledger: EngineLedger, *, limit: int = 3) -> str:
    """把账变成一句人话。**三句话的措辞刻意不同**，看一眼就知道是哪一类。

    ``limit`` 控制列几个文件名：收尾卡是聊天消息，不是报表。
    """
    if ledger.state == UNREADABLE:
        return (
            f"读不到这次的工具账（{ledger.reason}）"
            "—— 别把「没记录」当成「没改」"
        )
    tail = f"；{ledger.bash_failed} 条命令非 0 退出" if ledger.bash_failed else ""
    cut = "（只翻了前几页）" if ledger.truncated else ""
    if ledger.state == NO_CHANGE:
        return f"这次没有改动任何文件{tail}{cut} —— 这不等于做完"
    shown = "、".join(f"{_short(p)} {n} 次" for p, n in ledger.files[:limit])
    rest = (
        f" 等 {ledger.file_count} 个文件"
        if ledger.file_count > limit else ""
    )
    # 翻满了就是下界：写「至少」而不是把半本账当整本报出去。
    at_least = "至少" if ledger.truncated else ""
    return f"{at_least}动了 {ledger.file_count} 个文件（{shown}{rest}）{tail}{cut}"
