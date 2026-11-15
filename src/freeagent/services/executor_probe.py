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

## 「派出去拿什么身份调模型」不在本模块

那是另一件事（另立 :mod:`.executor_credentials`）：本模块答「装没装、能不能派」，
那个答「凭据从哪来」。分开的理由不是洁癖 —— 凭据那一问的判据是**文件内容与
进程环境**，而它的纪律也不同（只报观察到的事实，不报「能不能调通」）。
这里只把结果装进 :attr:`ExecutorProbe.credential` 两个字段，呈现不在这儿写第二遍。
"""

from __future__ import annotations

import dataclasses
import shutil
from typing import Callable, Literal, Sequence

from .executor_credentials import (
    UNTESTED,
    CredentialProbe,
    CredentialState,
    probe_opencode_credentials,
    render_credential,
)

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
    #: 派出去之后拿什么身份调模型（:mod:`.executor_credentials`）。
    #: 默认 ``untested``（= 本仓没有它的凭据探针），**不是「没凭据」** ——
    #: 两者混起来，hermes 会被写成「两处都没看到」，那是在替它断言。
    credential: CredentialState = UNTESTED
    #: 凭据的**观察结果**（查了哪里、看到什么）。绝不含密钥值。
    credential_detail: str = ""


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
    nondispatchable_reason: Callable[[int], str] | None = None,
    disabled_reason: Callable[[int], str] | None = None,
    capabilities: Callable[[bool], tuple[Bucket, ...]] = lambda v: _buckets_for(
        verified=v
    ),
    credential: CredentialProbe | None = None,
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

    ``nondispatchable_reason`` 处理「**验过接线但不可委派**」的执行器
    （曾经是 hermes：``verified=True`` 但 ``dispatchable=False``）。此时不能
    说「未验证」（那是在说谎），要说清「为什么验过了还是不能派」。不传则沿用
    :param:`unverified_reason`（对 opencode 来说，verified 与 dispatchable
    总是同真同假，用不上）。

    ``disabled_reason`` 处理「**已停用**」的执行器（V1.83 的 hermes）：它不是
    没验过、也不是不能委派，而是**决定不用了、代码保留作接缝**。停用优先于
    其余：只要适配器 ``disabled``，就按停用报（verified 不给、一桶能力都不给）。

    ``capabilities`` 默认按 ``verified`` 给三桶；需要单独覆盖（曾经是 hermes
    只 /ask 知识问答，现在停用了）—— 见 :func:`probe_hermes`。

    ``credential`` 不传就是 ``untested``：本仓**没有**这个执行器的凭据探针。
    刻意不给默认的「没凭据」—— 那是替它断言，而断言的方向最好是「不知道」。
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
    elif adapter.disabled and disabled_reason is not None:
        # 停用优先于「验过但不能委派」：不是没验过，是决定不用了。
        # 能力一桶都不给（verified 保持 False，避免被读成「能读」）。
        verified = False
        reason = disabled_reason(detected)
    elif verified and nondispatchable_reason is not None:
        # 验过接线、但设计上不走委派（曾经是 hermes 只 /ask）。要跟「未验证」
        # 分开给理由 —— 前者是「能问不能派」，后者是「连问都还没验」。
        reason = nondispatchable_reason(detected)
    else:
        reason = unverified_reason(detected)

    cred = credential if credential is not None else CredentialProbe(UNTESTED)
    return ExecutorProbe(
        name=name,
        installed=installed,
        detected_major=detected,
        dispatchable=dispatchable,
        verified=verified,
        capabilities=capabilities(verified),
        unavailable_reason=reason,
        credential=cred.state,
        credential_detail=cred.detail,
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
        # 凭据是**文件 + 环境**的观察，不起进程、不落库，所以装没装都探。
        # 「装了但 PATH 没了」与「装了、版本对、但没登录」是两种完全不同的坑，
        # 后者正是 401/402 的来源 —— 那一行要在同一次 `/agents` 里就能看到。
        credential=probe_opencode_credentials(),
    )


def probe_hermes(command: str = "hermes") -> ExecutorProbe:
    """探测 hermes 这个执行器。

    ## V1.83 起：已停用

    hermes 从执行器注册表**停用**（适配器 ``disabled=True``，代码保留作接缝）。
    停用的原因见下 —— 它是「决定不用了」，不是「没验过」或「还没接上」。
    所以只要装了，本探针就报「已停用」，能力一桶都不给、verified 不给。

    ## 为什么停用（V1.82 真机证伪初稿的假设）

    hermes 的审批闸是**「按模式标记」而非「每动作确认」**：``approvals.mode``
    只决定**已被它判为危险**的动作怎么处理，不会把更多动作标记为危险 ——
    ``del`` / 简单 ``write_file`` 到普通路径不在危险清单里，API 面与飞书面
    都会**直接执行而不弹审批**（manual + 重启也拦不住）。所以它**做不了执行期
    逐次授权，连「只读改不了」都保证不了** —— 不能作为受控执行器。

    FreeAgent 的处置（方案 B）：执行 + 飞书确认走 **opencode**（全控）；
    hermes 不再当执行器用。你若真想用 hermes，直接用它的独立飞书 bot 聊天，
    但那不是 FreeAgent 的执行器。

    ## 一条捷径**绝不能走**（若将来重新启用）

    把它当 OpenAI 兼容后端直接问 ``/v1/chat/completions`` 是最省事的，
    但那条面上**工具已在服务端执行完毕**（回放的 ``function_call`` 一律
    ``"status": "completed"``）—— agent 已经动过 terminal 和文件系统，
    本仓看不到也拦不住。那是绕过 11.8 全部闸门，而症状只是「能用」。
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
        disabled_reason=lambda detected: (
            f"hermes 主版本是 {detected}，本仓**已停用**它（V1.82/83）。"
            "它的审批闸是「按模式标记」而非「每动作确认」——`approvals.mode`"
            "只决定**已被判为危险**的动作怎么处理，`del`/简单 `write_file` 到"
            "普通路径不在危险清单里，API 面与飞书面都会直接执行而不弹审批"
            "（manual + 重启也拦不住），所以它做不了执行期逐次授权、也不能"
            "当受控执行器。执行 + 飞书确认走 **opencode**（FreeAgent 全控）。"
            "你若真想用 hermes，直接用它的独立飞书 bot 聊天，但那不是 FreeAgent "
            "的执行器。代码保留作接缝 —— 若 hermes 将来修好审批，把适配器 "
            "`disabled` 改回 False 即可重新启用。"
            "（别把它当 OpenAI 兼容后端直接问 —— 那条面上工具已在服务端执行完，"
            "本仓看不到也拦不住。）"
        ),
        nondispatchable_reason=lambda detected: (
            f"hermes 主版本是 {detected}，本仓**验过接线**（/ask 只读问答那条线），"
            "但**不能委派** —— 它的 API Server 面是 unattended 平台，危险命令"
            "直接 deny、做不了 11.8 那种「执行期逐次授权」（设计文档 11.10.8）。"
            "它只适合 `/ask hermes` 只读问答。"
            "（别把它当 OpenAI 兼容后端直接问 —— 那条面上工具已在服务端执行完，"
            "本仓看不到也拦不住。）"
        ),
        capabilities=lambda verified: (READ,) if verified else (),
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
    # 凭据放在能力之后、理由之前：它答的是另一个问题（派出去拿谁的身份），
    # 而不是「为什么不能派」。放错位置会被读成「不能派的原因是没凭据」。
    cred_line = render_credential(probe.credential, probe.credential_detail)
    if cred_line:
        lines.append(f"  {cred_line}")
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