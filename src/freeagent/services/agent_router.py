"""这句话要不要交给引擎动手（设计文档 21.2 / V1.114）。

## 它只回答一个问题

不是「这句话是什么意思」，而是「**要不要动文件 / 跑命令**」。
其余语义（记账、查今天、澄清、闲聊）归既有的白话入口，不在这里重做一遍
—— 一份能力一份实现（12.1.1）。

## 为什么规则优先，而且宁可漏判

把不该动手的话交出去，代价是**动了不该动的文件**；把该动手的话留在本地，
代价是**用户再说一遍**。两者的失效方向不对称，所以门槛设在「有明确动手
证据」这一侧。

它也不会假装自信：判断依据（命中了哪些词、为什么不动手）原样回给界面，
人可以一句话推翻它 —— 工作台上有一个「仍交给引擎」的按钮，那才是这条
规则真正的安全阀。**规则可被反驳，比规则更聪明重要。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["EngineDecision", "decide", "one_line"]

#: 动作词：出现就说明「有事要做」，而不是「有话要说」。
_VERBS: tuple[str, ...] = (
    "改", "修改", "修复", "实现", "重构", "新增", "添加", "删掉", "删除",
    "移除", "补上", "跑一下", "跑一遍", "运行", "执行", "测试", "编译",
    "构建", "安装", "提交", "推送", "排查", "检查", "优化", "迁移", "升级",
    "接入", "生成", "写个", "写一个", "看下", "看一眼", "打开", "对比",
)

#: 「让你直接做」的说法。它比动作词更明确：动作词可能是他在描述别人做的事。
_IMPERATIVE: tuple[str, ...] = (
    "你来", "帮我做", "帮我改", "帮我写", "动手", "直接做", "交给你", "按这个改",
)

#: 目标物：说了「代码 / 仓库 / 脚本」这类东西，指的多半是工作目录里的实体。
_TARGETS: tuple[str, ...] = (
    "代码", "仓库", "脚本", "程序", "项目里", "这个项目", "工程",
)

#: 问自己记录的说法。这些是 FreeAgent 本地能答的，不该麻烦引擎。
_VIEW_WORDS: tuple[str, ...] = (
    "今天", "日程", "待办", "提醒", "承诺", "关系", "角色", "记忆", "画像",
    "催办", "这周", "本周", "我在等", "该看",
)

#: 英文动作词。只认**小写整词**，避免 "test" 出现在别的词里被误命中。
_EN_VERBS: tuple[str, ...] = (
    "run ", "test", "build", "fix", "refactor", "implement", "install",
    "commit", "deploy", "debug", "rename", "migrate",
)

#: 路径 / 文件名。三种形状：Windows 盘符、带扩展名、常见源码目录前缀。
#: 刻意**不**把裸 ``/`` 当路径信号 —— 中文里「A / B 两件事」太多。
_PATH_RE = re.compile(
    r"([A-Za-z]:\\[^\s，。；]+"              # D:\PycharmProjects\x
    r"|[\w\-./]+\.(?:py|ts|tsx|js|json|jsonc|md|ya?ml|toml|txt|csv|sql|sh|ps1|bat)\b"
    r"|\b(?:src|tests?|docs|scripts|plugins)/[\w\-./]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EngineDecision:
    """一次判断的结论 + 依据。``reason`` 是**给人看的**，不是日志。"""

    needs_engine: bool
    reason: str
    hits: tuple[str, ...] = ()
    brief: str = ""


def one_line(text: str) -> str:
    """折成单行并压缩空白。**不丢内容**，只去掉换行。"""
    return " ".join((text or "").split())


def decide(text: str, *, directory: str, engine_available: bool) -> EngineDecision:
    """判「要不要交给引擎」。**纯函数**：不碰 IO、不看时间、不看库。"""
    raw = (text or "").strip()
    brief = one_line(raw)
    if not brief:
        return EngineDecision(False, "空消息，不交给引擎", brief="")
    if raw.startswith("/"):
        return EngineDecision(
            False, "以 / 开头，按命令处理（命令归命令层，不交给引擎）",
            brief=brief,
        )
    if not directory:
        return EngineDecision(
            False, "还没选工作目录 —— 没有目录就不知道该在哪儿动手",
            brief=brief,
        )
    if not engine_available:
        return EngineDecision(
            False, "引擎不可用（没装 / 起不来）—— 先让它能答话再说",
            brief=brief,
        )

    lowered = brief.lower()
    paths = [m.group(0) for m in _PATH_RE.finditer(brief)]
    verbs = [w for w in _VERBS if w in brief]
    imperative = [w for w in _IMPERATIVE if w in brief]
    targets = [w for w in _TARGETS if w in brief]
    english = [w for w in _EN_VERBS if w in lowered]

    hits: list[str] = []
    if paths:
        hits.append("路径/文件名：" + "、".join(paths[:3]))
    if verbs:
        hits.append("动作：" + "、".join(verbs[:4]))
    if english:
        hits.append("动作（英文）：" + "、".join(english[:3]))
    if imperative:
        hits.append("让你直接做：" + "、".join(imperative[:3]))
    if targets:
        hits.append("说的是：" + "、".join(targets[:3]))

    # 判据：**任何一条强信号**就交出去。
    #
    # 为什么不加权求和：加权会把「今天提醒我改一下 X.py」判成干活（权重够），
    # 也会把「帮我看看这个项目」判成闲聊（只有弱信号）。这两种错一个代价高、
    # 一个体验差，而加权的阈值调不动它们 —— 只能靠「强信号各自成立」。
    if paths or verbs or imperative:
        return EngineDecision(
            True, "有动手证据：" + "；".join(hits[:3]), tuple(hits), brief,
        )
    if targets or english:
        # 只有目标物/英文动作、没有明确动作词：**也交出去**，但理由写清
        # 「这只是弱证据」。台面上人能看到这句话，按一下就能改主意。
        return EngineDecision(
            True, "有弱证据（没有明确的动作词）：" + "；".join(hits[:3]),
            tuple(hits), brief,
        )

    view = [w for w in _VIEW_WORDS if w in brief]
    if view:
        return EngineDecision(
            False,
            "问的是你自己的记录（" + "、".join(view[:3]) + "），本地答，不动文件",
            brief=brief,
        )
    return EngineDecision(
        False, "没有动手证据（没有路径、没有动作词、没让你直接做），留在本地作答",
        brief=brief,
    )
