"""执行器**能力探针**（设计文档 11.10.2/ 11.10.3）。

## 它解决什么

11.8.1 把协议知识收进了版本化适配器，但**用户没有任何入口**能问
「我机器上有哪些执行器、装了没、什么版本、能不能派」。
只能去翻代码或跑 ``--version`` 自己猜。

## 与 11.8.1 的``ExecutorAdapter`` 是**两件事**

那个注册表是**协议**注册表：它回答「这个版本的字段名叫什么」。
本模块回答「**装没装、能不能用**」。两者不是一回事——
所以刻意**不**往 ``executors.py`` 里加，而是另立一个模块，
免得协议知识与能力探测耦在一起。

## 三条纪律

1. **探针不静默回落。** ``adapter_for()`` 取不到就返回 ``None``
   （11.8.1 规范 3）。同理，探针探不到执行器就报「没装 / 探不到版本」，
   **绝不**默认挑一个装着的 —— 静默挑一个的后果是「用户以为在用
   Claude，其实在用别的」，而那与本项目 V1.16 记的「字段名对不上时不报错」
   是同一类病。
2. **「不认识」与「认识但没验过」必须给不同的理由。** 该做的事不同
   （升级本仓 vs. 对着真机实测），合成一句「不可用」等于让人自己猜。
3. **``detected_major`` 探不到是 ``None``，不是 ``0``。** 两者都能比较，
   但 ``0`` 会被当成「版本 0」而进入别的分支，然后给出一句莫名其妙的话。
"""

from __future__ import annotations

import dataclasses
import shutil
from typing import Callable, Literal, Sequence

__all__ = [
    "ExecutorProbe",
    "Bucket",
    "probe_opencode",
    "probe_hermes",
    "probe_all",
    "render_probe",
    "render_probes",
    "READ",
    "WRITE",
    "APPROVE",
]

#: 11.10.3 的三个桶。**穷尽且互斥有序**：``read ⊂ write``，
#: 而 ``approve`` 覆盖 ``write``。
#:
#: 为什么是三桶而不是照抄工具面（``read``/``write``/``bash``/``webfetch``…）：
#: flag 多到没人能读就等于没有闸门 —— 用户会一路点「同意」，
#: 而 11.8.1 已实测过这个后果（"问到最后就是无脑点"，那比不设更糟）。
Bucket = Literal["read", "write", "approve"]
READ: Bucket = "read"
WRITE: Bucket = "write"
APPROVE: Bucket = "approve"


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutorProbe:
    """一个执行器的探测结果。**纯数据**，不做呈现。"""

    name: str
    #: 探到了可执行文件吗。与 ``detected_major`` **分开**：
    #: 「装了但跑不起来」（缺依赖、PATHEXT 那个坑）是真实存在的状态，
    #: 而「装了」不等于「探得到版本」。
    installed: bool
    #: 探到的主版本；**探不到是 ``None``，不是 ``0``**。
    detected_major: int | None
    #: 满足 11.8.1 的版本闸门吗（= 有适配器 **且** ``verified``）。
    dispatchable: bool
    #: 对着真实实例验过接线吗。
    verified: bool
    #: 能力集合（11.10.3 的三桶）。**穷尽且互斥有序**：
    #: ``read ⊂ write``，而 ``approve`` 覆盖 ``write``。
    capabilities: tuple[Bucket, ...]
    #: 不可派发时，**给一句能照着做的话**；可派发时为空串。
    unavailable_reason: str = ""


def _buckets_for(*, verified: bool) -> tuple[Bucket, ...]:
    """已验证的接线给三桶；未验证的**一桶都不给**。

    刻意不给「至少能读」：未验证意味着字段名可能对不上，
    而 ``read`` 桶在 opencode 侧对应的是 ``allow`` 规则 ——
    声称「能读」就是在声称「权限配置生效了」，而那正是未验证时**不能声称**的。
    """
    return (READ, WRITE, APPROVE) if verified else ()


def _probe_executor(
    *,
    name: str,
    command: str,
    detect: Callable[[str], int | None],
    expected_major: int,
    unverified_reason: Callable[[int], str],
) -> ExecutorProbe:
    """探测一个执行器的**共同骨架**。

    三段判据**各自独立**，缺一段就答不上来：

    1. ``installed`` —— 在 PATH 里找得到吗
    2. ``detected_major`` —— 跑一次 ``--version``（由 ``detect`` 注入，
       不在这里重写跑进程的逻辑）
    3. ``dispatchable`` / ``verified`` —— 查适配器注册表

    ``unverified_reason`` 是**函数**而不是字符串：那段话要带上探到的版本，
    而且两个执行器「为什么没验过」的原因**根本不同**
    （opencode 是字段名改名，hermes 是传输层都不一样），
    合成一句通用的「未验证」等于让人自己猜下一步 —— 那正是纪律 2 要防的。
    """
    from .executors import adapter_for, dispatchable_majors

    installed = shutil.which(command) is not None
    detected = detect(command) if installed else None

    adapter = adapter_for(detected, name)
    # ``verified`` 只在**认得这个版本**时才有意义：
    # 不认识的版本没有适配器，也就无从谈起「验过」。
    verified = bool(adapter and adapter.verified)
    dispatchable = detected in dispatchable_majors(name)

    reason = ""
    if not installed:
        reason = (
            f"PATH 里找不到 `{command}`。装好之后本仓就能派发；"
            f"先跑 `{command} --version` 确认它能起来。"
        )
    elif detected is None:
        reason = (
            f"装了 `{command}`，但**探不到版本**（`{command} --version` "
            f"没输出可解析的主版本，期望形如 {expected_major}.x.x）。"
            "本仓按「不知道在跟什么说话」处理—— 不猜。"
        )
    elif detected in dispatchable_majors(name):
        reason = ""
    elif adapter is None:
        reason = (
            f"{name} 主版本是 {detected}，本仓**不认识**它。"
            "请升级本仓，或把执行器降回已知版本 —— "
            "未知版本的 permission 字段名会被**静默忽略**，闸门随之失效。"
        )
    else:
        reason = unverified_reason(detected)

    return ExecutorProbe(
        name=name,
        installed=installed,
        detected_major=detected,
        dispatchable=dispatchable,
        verified=verified,
        capabilities=_buckets_for(verified=verified),
        unavailable_reason=reason,
    )


def probe_opencode(command: str = "opencode") -> ExecutorProbe:
    """探测 opencode 这个执行器。

    ⚠️ 函数内import :mod:`..delegate`：那个模块顶层会拉起委派相关的重物，
    而本模块是**只读探针**，两者不该在导入期就绑在一起。
    """
    from ..delegate import SUPPORTED_OPENCODE_MAJOR, detect_opencode_version

    return _probe_executor(
        name="opencode",
        command=command,
        detect=detect_opencode_version,
        expected_major=SUPPORTED_OPENCODE_MAJOR,
        unverified_reason=lambda detected: (
            f"opencode 主版本是 {detected}，本仓**认识**它但**未验证**接线。"
            "要接它得先对着一个真实实例实测适配器的字段形状 —— "
            "猜一个「看起来很像对的」形状比拒绝更危险：它会静默地不生效。"
        ),
    )


def probe_hermes(command: str = "hermes") -> ExecutorProbe:
    """探测 hermes 这个执行器。

    ## 它有两条面，而**走对的那条与 opencode 同构**

    - **ACP**（``hermes acp``）—— stdio 上的 JSON-RPC
    - **API Server**（``hermes gateway``，HTTP 默认 8642）—— ``POST /v1/runs``
      + ``GET /v1/runs/{id}/events``（SSE）+ ``/stop`` + ``/approval``

    走下面那条时，形状与 opencode **是同构的**（同为 HTTP + SSE，
    授权事件 ``approval.request`` 对得上 opencode 的 ``permission.asked``），
    **不必换传输层，也不必引 ACP SDK**。

    ## 那为什么还是不可派发

    因为上面这些是**读文档读来的，一次真机都没验过** —— 而 11.8.1 的教训
    正是「照文档接线、字段名对不上时不报错、闸门静默失效」。

    ## 有一条捷径**绝不能走**

    把它当 OpenAI 兼容后端直接问 ``/v1/chat/completions`` 是最省事的，
    但那条面上**工具已在服务端执行完毕**（回放的 ``function_call`` 一律
    ``"status": "completed"``）—— agent 已经动过 terminal 和文件系统，
    本仓看不到也拦不住。那是绕过 11.8 全部闸门，而症状只是「能用」。

    ## 那为什么还要登记它

    因为 11.10.2 要的正是「**有哪些执行器**」这份清单。先让它**可见**且
    **诚实地不可派发**，比让 ``/agents`` 只列 opencode 要好 ——
    后者会让「多执行器」看起来已经做完了，而实际一个都没接上。

    ⚠️ 探到版本**不等于**能派发。hermes 在注册表里 ``verified=False``，
    所以 :func:`_buckets_for` 一桶都不给它。
    """
    from ..delegate import detect_hermes_version

    return _probe_executor(
        name="hermes",
        command=command,
        detect=detect_hermes_version,
        expected_major=0,
        unverified_reason=lambda detected: (
            f"hermes 主版本是 {detected}，本仓**认识**它但**未验证**接线。"
            "它的 API Server（HTTP + SSE，默认 8642）照文档看与 opencode "
            "**同构** —— 建 run、读事件流、`/stop` 中止、`/approval` 回应授权，"
            "所以接它**不必**换传输层。但那都是读文档读来的，"
            "一次都没对着真机验过，而 11.8.1 的教训正是"
            "「照文档接线、字段名对不上时不报错、闸门静默失效」。"
            "要接它：先起 `hermes gateway`，按设计文档 11.10.8 的验收判据逐项实测。"
            "（另有一条捷径**不能走**：把它当 OpenAI 后端直接问 —— 那条面上"
            "工具已在服务端执行完，本仓拦不住。）"
        ),
    )


def render_probe(probe: ExecutorProbe) -> str:
    """探针结果 → 给用户看的一段中文。

    刻意**先说能不能派**，再说细节：用户问「能不能用」，
    答「装在D:/...」是答非所问。
    """
    if probe.dispatchable:
        head = f"{probe.name}：可以派发（主版本 {probe.detected_major}，接线已验证）"
    elif probe.installed:
        head = f"{probe.name}：装上了，但**现在不能派发**"
    else:
        head = f"{probe.name}：**没装**"

    lines = [head]
    lines.append(f"  可执行文件：{'有' if probe.installed else '无'}")
    lines.append(
        f"  探到的主版本：{probe.detected_major}"
        if probe.detected_major is not None
        else "  探到的主版本：**探不到**（不是 0，是「不知道」）"
    )
    caps = "、".join(probe.capabilities) if probe.capabilities else "**一桶都不给**"
    lines.append(f"  能力（read/write/approve）：{caps}")
    if probe.unavailable_reason:
        lines.append("")
        lines.append(f"  为什么不可派发：{probe.unavailable_reason}")
    return "\n".join(lines)


# ── 全部执行器（11.10.2 那份「有哪些执行器」的清单）────────────────────────

#: 执行器名 → 对应的探针。**显式列出**，不靠 ``globals()`` 去拼名字 ——
#: 拼名字在改名时会静默地探不到（于是执行器从清单里消失），
#: 而「消失」比「报错」难发现得多。
_PROBES: dict[str, Callable[[], ExecutorProbe]] = {
    "opencode": lambda: probe_opencode(),
    "hermes": lambda: probe_hermes(),
}


def _unprobed(name: str) -> ExecutorProbe:
    """注册表里有、但**没写探针**的执行器。

    刻意**不静默跳过**：注册表多登记了一个执行器而探针没跟上时，
    ``/agents`` 少列它比列一个「没探针」危险得多 ——
    「少列」会被读成「这个执行器不存在」，于是没人去补。
    """
    return ExecutorProbe(
        name=name,
        installed=False,
        detected_major=None,
        dispatchable=False,
        verified=False,
        capabilities=(),
        unavailable_reason=(
            f"本仓的注册表里有 `{name}`，但**没写它的探针** —— "
            "这不是「它没装」，而是本仓还不知道该怎么探它。"
            f"要接它，先在 `services/executor_probe.py` 的 ``_PROBES`` "
            f"里补一个 `probe_{name}()`。"
        ),
    )


def probe_all() -> tuple[ExecutorProbe, ...]:
    """探测**注册表里登记的全部**执行器。

    顺序取自 :func:`~freeagent.services.executors.known_executors`，
    也就是注册表自身 —— 刻意**不从** ``_PROBES`` 取：那份字典是「怎么探」，
    而「有哪些」应当由注册表回答。两份清单一旦漂移，
    这里会以「登记了但没探针」的形式**响出来**，而不是悄悄少一个。
    """
    from .executors import known_executors

    return tuple(
        _PROBES[name]() if name in _PROBES else _unprobed(name)
        for name in known_executors()
    )


def render_probes(probes: Sequence[ExecutorProbe]) -> str:
    """把多个探针结果排成一段给用户看的中文。

    先给一句**总览**（几个可派发），再逐个展开。用户问「有哪些执行器」，
    第一眼要的是「能用几个」，而不是第一台机器的版本号。
    """
    probes = list(probes)
    if not probes:
        return "注册表里没有登记任何执行器 —— 这不是好消息，是本仓坏了。"

    n_ok = sum(1 for p in probes if p.dispatchable)
    head = f"共 {len(probes)} 个执行器登记在册，其中 **{n_ok} 个**现在可以派发。"
    return "\n".join([head, ""] + [render_probe(p) for p in probes])