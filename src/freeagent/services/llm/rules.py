"""规则驱动的默认实现。

无网络、无 API key、完全确定性：相同输入必定得到相同输出。
本模块不做任何 I/O，也不读环境变量 —— 阈值全部是具名模块常量，
便于测试替换。
"""

from __future__ import annotations

import re
from typing import Sequence

from .provider import (
    DelegationIntent,
    KIND_ACTION,
    KIND_REMINDER,
    KIND_WAIT,
    ClassificationResult,
    RoleGuess,
    RoleHint,
    SignalRef,
    TaskRef,
)

__all__ = [
    "RuleBasedProvider",
    "ROLE_MATCH_THRESHOLD",
    "ROLE_KEEP_MIN",
    "TITLE_MAX_LEN",
    "TITLE_EDGE_CHARS",
    "strip_title_edges",
]

#: 标题两端要剥掉的字符：空白、引号、**以及残留的标点**。
#:
#: 为什么必须含标点：开场白表 :data:`_LEADING_FILLERS` 里只有「记一下」，
#: 不含它后面那个冒号。于是「记一下：给客户A发季度报价单」剥完开场白，
#: 冒号就留在标题头上 —— 实测建出的标题是「：给客户A发季度报价单」。
#: 只剥空白和引号是不够的。
#:
#: 放在这里而不是各 provider 各写一份：两份必然漂移，而漂移的症状是
#: 「联网时标题干净、离线降级时带冒号」—— 没人会注意到。
TITLE_EDGE_CHARS = " \t\r\n\"'「」《》`:：,，、；;"


def strip_title_edges(text: str) -> str:
    """剥标题两端。两个 provider 共用，理由见 :data:`TITLE_EDGE_CHARS`。"""
    return text.strip(TITLE_EDGE_CHARS)

#: 低于此置信度就必须追问用户，不允许静默猜测。
ROLE_MATCH_THRESHOLD = 0.34

#: 低于此置信度的候选不返回（避免噪声）。
ROLE_KEEP_MIN = 0.15

#: 标题最大长度。
TITLE_MAX_LEN = 40

#: 输入无法收敛出标题时的占位（保证标题非空）。
UNTITLED_PLACEHOLDER = "未命名事务"

#: 标题截断时允许保留的最小前缀长度。
_MIN_PREFIX = 6

# 等候类特征。优先级高于提醒类：``别忘了等对方回复`` 本质是「在等」。
_WAIT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"等[^，。；、]{0,8}?(回复|回覆|答复|回信|回消息|结果|审批|确认|通知|反馈|消息|答复|回复意见)",
        r"盯着[^，。；、]{0,8}?(回复|结果|消息|通知|审批|确认|反馈|意见)",
        r"等[^，。；、]{0,8}?(对方|客户|老板|同事|律师|学校|对方回复)",
        r"待[^，。；、]{0,6}?(回复|结果|审批|确认|通知|反馈|处理|批)",
        r"等谁",
        r"\bpending\b",
        r"\bawait\b",
    )
)

# 提醒类特征。
_REMINDER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"提醒我",
        r"别忘了",
        r"别忘",
        r"记得",
        r"remind\s*me",
        r"到点",
        r"响铃",
        r"叫我",
        r"记得提醒",
    )
)

# 标题里要剥掉的开场白。
_LEADING_FILLERS: tuple[str, ...] = (
    "帮我记一下",
    "帮我记下",
    "帮我记",
    "记一下",
    "记下来",
    "记下",
    "麻烦你",
    "麻烦",
    "帮我",
    "请你",
    "请",
    "我要",
    "我想",
    "需要",
    "我想着",
)

# 标题截断用的句子终止符。
_TERMINATORS = "。！？!?；;\n"

# 拆步骤用的分隔符。
_STEP_SPLIT_RE = re.compile(
    r"(?:\d+[.、)）]|[（(]?\d+[)）]?第?[一二三四五六七八九十\d]+[、.）)]|"
    r"第[一二三四五六七八九十]+步|[-•·*]|\n)+"
)
_CONNECTIVE_RE = re.compile(r"(?:然后|接着|之后|随后|最后|再然后|再|先)")

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_WS_RE = re.compile(r"\s+")


def _tokenize(text: str) -> set[str]:
    """CJK 感知的粗分词。

    ASCII 走 ``[a-z0-9]+``；中文按连续汉字串切 **相邻二元组**
    （「销售周报」→ 销售 / 售周 / 周报），长度为 1 的串保留单字。
    """
    lowered = text.lower()
    tokens: set[str] = set(_ASCII_TOKEN_RE.findall(lowered))
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
            continue
        for i in range(len(run) - 1):
            tokens.add(run[i : i + 2])
    return tokens


def _confidence(name_hits: int, note_hits: int) -> float:
    """把命中数折成 0..1 的置信度。

    名字命中比备注命中更强；命中越多越高，但有上限，避免长备注刷分。
    """
    best = 0.0
    if name_hits:
        best = max(best, min(0.9, 0.6 + 0.15 * (name_hits - 1)))
    if note_hits:
        best = max(best, min(0.6, 0.35 + 0.1 * (note_hits - 1)))
    return round(best, 3)


class RuleBasedProvider:
    """关键词 + 启发式实现。确定性、可测、离线可用。"""

    # -- 分类 --------------------------------------------------------------- #
    def classify(
        self, text: str, role_hints: Sequence[RoleHint]
    ) -> ClassificationResult:
        kind = self._infer_kind(text)
        guesses = self._guess_roles(text, role_hints)

        if guesses and guesses[0].confidence >= ROLE_MATCH_THRESHOLD:
            return ClassificationResult(
                kind=kind, role_guesses=guesses, need_clarification=False
            )
        return ClassificationResult(
            kind=kind,
            role_guesses=guesses,
            need_clarification=True,
            clarifying_question=self._clarifying_question(guesses),
        )

    @property
    def can_select_view(self) -> bool:
        """规则实现**没有选路意见** —— 见 :meth:`select_view`。"""
        return False

    def select_view(
        self, text: str, views: Sequence[tuple[str, str]]
    ) -> str | None:
        """规则实现**不做选路**，如实返回 ``None``。

        刻意不把它做成「关键词表选路」：那张表已经在
        :class:`~freeagent.services.chat.ChatService` 里了。在这里再写一份
        就是两份会漂移的词表 —— 而漂移的表现是「这里选对、那里选错」。

        返回 ``None`` 让上层走它自己那条路，所以离线/降级时行为完全不变。
        """
        return None

    def parse_delegation(
        self, text: str, known_projects: Sequence[str]
    ) -> DelegationIntent:
        """规则实现**不抽**，如实说「不是委派」。

        与 :meth:`select_view` 同一条理由：那份「剥动词猜需求」的实现
        实测必然出错（「帮我改一下 README」剥出空的，「把 greet 改成
        返回你好」剥丢了动作）。而**猜错项目的代价是在错误的仓库里动手**。

        返回 ``is_delegation=False`` 时上层照旧走它自己那条路
        （记事 / 追问），所以离线或降级时行为与从前逐字一致。
        """
        return DelegationIntent()

    def _infer_kind(self, text: str) -> str:
        lowered = text.lower()
        if any(p.search(lowered) for p in _WAIT_PATTERNS):
            return KIND_WAIT
        if any(p.search(lowered) for p in _REMINDER_PATTERNS):
            return KIND_REMINDER
        return KIND_ACTION

    def _guess_roles(
        self, text: str, role_hints: Sequence[RoleHint]
    ) -> tuple[RoleGuess, ...]:
        text_tokens = _tokenize(text)
        scored: list[RoleGuess] = []
        for hint in role_hints:
            name_hits = len(text_tokens & _tokenize(hint.name))
            note_hits = len(text_tokens & _tokenize(hint.note or ""))
            confidence = _confidence(name_hits, note_hits)
            if confidence >= ROLE_KEEP_MIN:
                scored.append(RoleGuess(role_name=hint.name, confidence=confidence))
        scored.sort(key=lambda g: (-g.confidence, g.role_name))
        return tuple(scored[:3])

    def _clarifying_question(self, guesses: Sequence[RoleGuess]) -> str:
        names = [g.role_name for g in guesses[:2]]
        if not names:
            return "这是放到哪个脉络里？"
        if len(names) == 1:
            return f"这是放到「{names[0]}」里面吗？"
        return f"这是放到「{names[0]}」还是「{names[1]}」里面？"

    # -- 标题 --------------------------------------------------------------- #
    def refine_title(self, text: str) -> str:
        """收敛成一句话标题。

        细节属于 ``intent``，不属于标题 —— 所以末尾的补充说明
        （「，先理一版」这类）会在逗号处收口掉。
        """
        cleaned = _WS_RE.sub(" ", text).strip()
        for filler in _LEADING_FILLERS:
            if cleaned.startswith(filler):
                # 用 strip_title_edges 而不是 lstrip()：后者只剥空白，
                # 于是「记一下：X」剥完变成「：X」（实测）。
                cleaned = strip_title_edges(cleaned[len(filler) :])
                break

        # 先按句子终止符切
        cut = len(cleaned)
        for idx, ch in enumerate(cleaned):
            if ch in _TERMINATORS:
                cut = idx
                break
        cleaned = strip_title_edges(cleaned[:cut])

        # 前段足够长时，在最后一个逗号/顿号处收口
        last_break = max(
            (idx for idx, ch in enumerate(cleaned) if ch in "，,、" and idx >= _MIN_PREFIX),
            default=-1,
        )
        if last_break > 0:
            cleaned = strip_title_edges(cleaned[:last_break])

        if len(cleaned) > TITLE_MAX_LEN:
            cleaned = cleaned[: TITLE_MAX_LEN - 1] + "…"

        if cleaned:
            return cleaned
        # 兜底：绝不返回空标题，否则事务无法进入系统
        fallback = _WS_RE.sub(" ", text).strip()
        return fallback[:TITLE_MAX_LEN] if fallback else UNTITLED_PLACEHOLDER

    # -- 拆步骤 ------------------------------------------------------------- #
    def split_steps(
        self, task: TaskRef, instruction: str | None = None
    ) -> tuple[str, ...]:
        """拆步骤。**不排序、不排优先级** —— 只把一件事切成几步。"""
        source = task.intent or task.definition_of_done or task.title
        pieces: list[str] = []
        for chunk in _STEP_SPLIT_RE.split(source):
            if not chunk:
                continue
            for part in _CONNECTIVE_RE.split(chunk):
                part = part.strip(" 　,，.。;；、-—")
                if part:
                    pieces.append(part)

        deduped: list[str] = []
        for piece in pieces:
            if piece not in deduped:
                deduped.append(piece)

        if len(deduped) >= 2:
            return tuple(deduped)
        if task.definition_of_done:
            return (f"明确「{task.definition_of_done}」的判定标准", "准备需要的材料", "产出并自检一遍")
        if task.intent:
            return (f"先想清楚：{task.intent}", "准备需要的材料", "产出并自检一遍")
        return ("明确目标", "准备材料", "产出并自检")

    # -- 草稿 --------------------------------------------------------------- #
    def draft(self, task: TaskRef, instruction: str) -> str:
        """生成结构化草稿。

        **只复述 ``TaskRef`` 里已有的信息，未知处一律标 ``[TODO]``，
        绝不编造事实。**
        """
        lines: list[str] = [f"# {task.title}"]

        if instruction.strip():
            lines += ["", "## 本次要求", instruction.strip()]

        lines += ["", "## 目标"]
        lines.append(task.intent.strip() if task.intent else "[TODO] 这次想做到什么程度？")

        lines += ["", "## 完成标准"]
        lines.append(
            task.definition_of_done.strip()
            if task.definition_of_done
            else "[TODO] 做到什么程度算完？"
        )

        lines += ["", "## 材料"]
        materials = [chunk.strip() for chunk in _STEP_SPLIT_RE.split(task.title) if chunk.strip()]
        if materials:
            lines += [f"- [TODO] {item}" for item in materials]
        else:
            lines.append("- [TODO] 需要哪些材料/数据？")

        lines += ["", "## 待确认"]
        lines.append("- [TODO] 有没有遗漏的约束条件？")
        if task.kind == "wait":
            lines.append("- [TODO] 在等谁？等到什么时候该跟进？")

        lines += ["", "（本内容由规则引擎生成的结构化草稿，非模型生成，请自行改写）"]
        return "\n".join(lines)

    # -- 排期建议 ----------------------------------------------------------- #
    def suggest_schedule(
        self, task: TaskRef, signals: Sequence[SignalRef]
    ) -> str:
        """把信号渲染成建议文本。**不给优先级结论。**"""
        if not signals:
            return "没有命中任何提示信号，由你决定何时做。"
        ordered = sorted(signals, key=lambda s: -s.weight)
        parts = "、".join(f"{s.reason}（权重 {s.weight}）" for s in ordered[:3])
        return f"命中 {len(signals)} 条提示信号：{parts}。是否今天做由你决定。"
