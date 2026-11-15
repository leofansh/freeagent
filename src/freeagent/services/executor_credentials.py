"""执行器**凭据来源**探针（设计文档 11.10.2 的「凭据」一列）。

## 它解决什么

``/agents`` 答得出「装了没、什么版本、能不能派」，答不出一问：
**派出去之后，它拿什么身份去调模型？**

这一问在实际故障里排第一个。401 / 402 / 认证失败时，人的真实路径是翻配置、
翻文档、翻环境变量，最后才想起来「它到底用哪个 key」—— 而这一问有两个
**完全可观察**的答案，一眼能看完。

## 一条纪律：报**看到什么**，不报**能不能用**

这一行只描述「在哪看到什么」，**不说**「所以它跑得起来」。理由与 11.10.4
「能力声明一旦手写就会陈旧」同源：**存放位置是执行器的内部实现，会随版本改**。
写成「有凭据 → 能调通」，下一版换了位置，本仓就开始说谎。

而「``auth.json`` 里有 3 个提供方」这句话**永远是真的** —— 它说的是文件内容。
这也是 11.9.12 那条「标注说证据，不说结论」：人拿它去核对配置，
而不是拿它当结论。

## 绝不打印密钥值

只打印**提供方名**与**环境变量名**。两者泄露面完全不同：名字能让人去核对
配置，值一旦落到日志 / 截图 / 工单里就收不回来。测试拿一个假 key 断言它
不出现在输出里（两个状态各断言一次）。

## 为什么环境变量按**名字形状**扫，不列白名单

白名单一定会陈旧：opencode 新增一个提供方，白名单不知道，于是本仓报
「没有凭据」而其实有 —— 那是**错判**，它把人引去查错方向（11.9.11 那条
「宁可漏判，不要错判」的同一个判断）。

按 ``*_API_KEY`` 结尾扫则是**观察**：说的是「叫这个名字的变量一个都没有」，
不是断言「所以一定没有凭据」。代价是认不出用别的命名习惯喂 key 的人 ——
那是漏判，代价只是「这一行没帮上忙」。
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Literal, Mapping

__all__ = [
    "CredentialProbe",
    "CredentialState",
    "LOGIN",
    "ENV_ONLY",
    "BOTH",
    "NONE",
    "UNKNOWN",
    "UNTESTED",
    "probe_opencode_credentials",
    "render_credential",
]

#: 凭据从哪来的六种情形。**``unknown`` 与 ``none`` 必须分开**：前者是
#: 「文件在、读不懂」（可能新版换了结构），后者是「两处都没有」。
#: 合成一个，人就会照着「没登录」去重登，而问题在别处。
CredentialState = Literal["login", "env", "both", "none", "unknown", "untested"]

LOGIN: CredentialState = "login"
ENV_ONLY: CredentialState = "env"
BOTH: CredentialState = "both"
NONE: CredentialState = "none"
UNKNOWN: CredentialState = "unknown"
#: 本仓**没有**这个执行器的凭据探针（如 hermes）。刻意与 ``unknown`` 分开：
#: ``unknown`` 是「查了但认不出」，这个是「没查」。渲染时**不打这一行** ——
#: 编一行「不知道」不如不说（11.10.2 那句「合成一句话等于让人自己猜」）。
UNTESTED: CredentialState = "untested"


@dataclasses.dataclass(frozen=True, slots=True)
class CredentialProbe:
    """一次凭据观察的结果。**纯数据**，不含任何密钥值。"""

    state: CredentialState
    #: 一句话说清查了哪里、看到什么。``untested`` 时为空串。
    detail: str = ""


def _shorten(path: Path, home: Path) -> str:
    """把家目录缩成 ``~`` —— 输出里出现 ``C:\\Users\\<我的用户名>`` 没必要。"""
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return path.as_posix()


def _candidate_auth_files(home: Path, env: Mapping[str, str]) -> tuple[Path, ...]:
    """``auth.json`` 的候选位置。

    **两个都试、取第一个存在的**：只试一个的风险是「``XDG_DATA_HOME`` 设了但
    opencode 没认」，那本仓会报「没看到凭据」而其实有 —— 错判方向。
    反过来（两个都没有）时，报告里把**试过的路径都列出来**，人能自己核对。
    """
    out: list[Path] = []
    xdg = env.get("XDG_DATA_HOME", "").strip()
    if xdg:
        out.append(Path(xdg) / "opencode" / "auth.json")
    out.append(home / ".local" / "share" / "opencode" / "auth.json")
    return tuple(out)


def _read_providers(path: Path) -> tuple[str, ...] | None:
    """读 ``auth.json`` → 配好的提供方名。

    **读不懂返回 ``None``，不是空元组。** 两者都会变成空结果，但该说的话不同：
    空元组是「文件在、里面没东西」（人没登录），``None`` 是「文件在、本仓看不懂」
    （可能新版换了结构）。合成一个，人就会去重登，而问题在别处。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    names: list[str] = []
    for name, item in raw.items():
        if not isinstance(item, dict):
            continue
        # ``key`` 是 api 型；``access``/``refresh`` 是 oauth 型。两者都算「配了」。
        if item.get("key") or item.get("access") or item.get("refresh"):
            names.append(str(name))
    return tuple(sorted(names))


def _key_env_names(env: Mapping[str, str]) -> tuple[str, ...]:
    """环境里**名字像密钥**的变量名（``*_API_KEY`` 且有值）。只取名字，不取值。

    ``CODEBUDDY_API_KEY_HELPER_DISABLED`` 这类开关名结尾是 ``_DISABLED``，
    不会被算进来 —— 这条在测试里有专门一行守着（形状匹配要按**结尾**判，
    11.9.11「错判的代价远大于漏判」）。
    """
    return tuple(
        sorted(
            name
            for name, value in env.items()
            if name.upper().endswith("_API_KEY") and value.strip()
        )
    )


def probe_opencode_credentials(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> CredentialProbe:
    """看 opencode 的凭据从哪来。**只读文件与进程环境**，不起进程、不落库。

    ``env`` / ``home`` 可注入：测试要造「有文件 / 没文件 / 文件坏了」三种局面，
    **不能靠开发者本机碰巧长什么样**（那种测试在别人机器上永远是绿的）。
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home

    env_names = _key_env_names(env)
    tried = _candidate_auth_files(home, env)
    # 列**目录**不列文件：文件路径本身就以 ``auth.json`` 结尾，再补一句
    # 「没有 auth.json」读起来是「没有 X/…/X」。人要去核对时看目录也够。
    dirs_text = "、".join(f"`{_shorten(p.parent, home)}`" for p in tried)
    env_text = (
        "环境变量里设了 " + "、".join(f"`{n}`" for n in env_names)
        if env_names
        else "环境里也没有以 `_API_KEY` 结尾的变量"
    )

    found: Path | None = None
    providers: tuple[str, ...] | None = ()
    for path in tried:
        if path.exists():
            found = path
            providers = _read_providers(path)
            break

    if found is None:
        if env_names:
            return CredentialProbe(
                ENV_ONLY,
                f"找过 {dirs_text} —— 没有 `auth.json`；但{env_text}。"
                "本仓**不判断** opencode 用不用它 —— 那是它的内部规则。",
            )
        return CredentialProbe(
            NONE,
            f"找过 {dirs_text} —— 没有 `auth.json`；{env_text}。"
            "**这是一次观察，不是断言**：换版本后它可能把凭据放到别处。"
            "真报认证错误时先跑 `opencode auth list` 核对；要补："
            "`opencode auth login`。",
        )

    where = f"`{_shorten(found, home)}`"
    if providers is None:
        # 文件在、读不懂。**说清「别按没登录处理」** —— 这是这一格唯一的用处。
        tail = f"；另外{env_text}" if env_names else ""
        return CredentialProbe(
            UNKNOWN,
            f"{where} 存在，但读不懂（不是合法 JSON，或顶层不是对象）{tail}。"
            "本仓不猜 —— 别按「没登录」处理；先 `opencode auth login` "
            "或直接看这个文件。",
        )
    if providers and env_names:
        return CredentialProbe(
            BOTH,
            f"{where} 里有 {len(providers)} 个提供方（"
            + "、".join(providers)
            + f"），{env_text}。"
            "**本仓不判断哪一个生效** —— 优先级是执行器的内部规则，写在这里会随它改版陈旧。",
        )
    if providers:
        return CredentialProbe(
            LOGIN,
            f"{where} 里有 {len(providers)} 个提供方（"
            + "、".join(providers)
            + f"），{env_text}。",
        )
    if env_names:
        return CredentialProbe(
            ENV_ONLY,
            f"{where} 在，但里面没有可用提供方；{env_text}。",
        )
    return CredentialProbe(
        NONE,
        f"{where} 在，但里面没有可用提供方；{env_text}。"
        "要补：`opencode auth login`。",
    )


_LABEL: dict[str, str] = {
    LOGIN: "登录态（`auth.json`）",
    ENV_ONLY: "只有环境变量",
    BOTH: "两处都有",
    NONE: "两处都没看到",
    UNKNOWN: "认不出",
}


def render_credential(state: CredentialState, detail: str = "") -> str:
    """一行：凭据从哪来。``untested`` 返回**空串**，调用方据此整行不打印。"""
    if state == UNTESTED:
        return ""
    label = _LABEL.get(state, "认不出")
    return f"凭据来源：{label}" + (f" —— {detail}" if detail else "")
