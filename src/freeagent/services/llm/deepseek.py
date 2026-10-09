"""DeepSeek（OpenAI 兼容接口）实现。

设计约束
--------
* **只用标准库** ``urllib``：文档 12.1 承诺「唯一第三方依赖是 pytest」。
* **API Key 只从环境变量读**，不落盘、不进日志、不进错误信息。
* **传输层可注入**（``transport``）—— 测试全程离线，一次网络请求都不发。
* **输出必须过校验**：模型会编造角色名、编造事实，这两件事必须由代码拦住
  （文档第十五章「不伪造事实」）。
* **失败可降级**：有 ``fallback`` 时退回规则层，并把原因记在
  ``last_degradation`` 里由 CLI 如实告知用户，而不是静默劣化。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable

from ...domain import LLMError
from .provider import (
    DelegationIntent,
    KIND_ACTION,
    KIND_REMINDER,
    KIND_WAIT,
    ClassificationResult,
    LLMProvider,
    RoleGuess,
    RoleHint,
    SignalRef,
    TaskRef,
)
from .rules import (
    ROLE_KEEP_MIN,
    ROLE_MATCH_THRESHOLD,
    TITLE_MAX_LEN,
    _MIN_PREFIX,
    _WS_RE,
    strip_title_edges,
)

__all__ = [
    "DeepSeekConfig",
    "DeepSeekProvider",
    "urllib_transport",
    "ALLOWED_KINDS",
    "PROVIDER_NAME",
]

PROVIDER_NAME = "deepseek"

ALLOWED_KINDS = frozenset({KIND_ACTION, KIND_WAIT, KIND_REMINDER})

#: ``(url, payload, headers, timeout) -> 响应体文本``
#: key 走 headers 而不是塞进 payload —— payload 更容易被日志意外打印。
Transport = Callable[[str, Mapping[str, object], Mapping[str, str], float], str]

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_MAX_ERROR_BODY = 200


# --------------------------------------------------------------------------- #
# 传输层
# --------------------------------------------------------------------------- #
def urllib_transport(
    url: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str],
    timeout: float,
) -> str:
    """标准库 HTTP POST。出错时抛 :class:`LLMError`，**不含 key**。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **dict(headers)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:_MAX_ERROR_BODY]
        except OSError:  # 读错误体失败不该掩盖原始错误
            detail = ""
        raise LLMError(
            f"HTTP {exc.code} {exc.reason}" + (f"：{detail}" if detail else ""),
            provider=PROVIDER_NAME,
        ) from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"网络不可达：{exc.reason}", provider=PROVIDER_NAME) from exc
    except TimeoutError as exc:
        raise LLMError("请求超时", provider=PROVIDER_NAME) from exc
    except OSError as exc:
        raise LLMError(f"连接失败：{exc}", provider=PROVIDER_NAME) from exc


# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    timeout: float = 20.0
    temperature: float = 0.0
    #: **显示用的**服务商名。默认保持旧行为（"deepseek"），但界面上换了
    #: provider 就会传真名进来 —— 否则用 Kimi 出错时用户看到的是
    #: 「deepseek 调用失败」，而这正是「界面说一套、实际做另一套」：
    #: 用户会去查 DeepSeek 的账单和限流，根本查不到问题上。
    #:
    #: 只用于**给用户看的文案**，不参与任何协议判断。
    name: str = PROVIDER_NAME

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


# --------------------------------------------------------------------------- #
class DeepSeekProvider(LLMProvider):
    """真实模型实现。协议与 :mod:`provider` 一一对应。"""

    def __init__(
        self,
        config: DeepSeekConfig,
        api_key: str,
        *,
        transport: Transport | None = None,
        fallback: LLMProvider | None = None,
    ) -> None:
        if not api_key.strip():
            raise LLMError("API Key 为空", provider=config.name)
        self._config = config
        self._api_key = api_key
        self._transport: Transport = transport or urllib_transport
        self._fallback = fallback
        #: 最近一次降级的原因，供 CLI 如实告知用户
        self.last_degradation: str | None = None

    # -- 降级 --------------------------------------------------------------- #
    @property
    def degraded_reason(self) -> str | None:
        return self.last_degradation

    def _degrade(self, exc: Exception) -> LLMProvider:
        if self._fallback is None:
            raise exc
        # 在**唯一**对用户可见的那一处补上真实服务商名。模块级的解析函数
        # （_extract_text 等）拿不到实例，所以它们的 LLMError 里 provider
        # 字段还是 "deepseek" —— 但那些字符串没人直接看，CLI 只显示这里
        # 记下的这句。改这一处就够，不必给二十个调用点穿参数。
        self.last_degradation = f"[{self._config.name}] {exc}"
        return self._fallback

    # -- 底层调用 ----------------------------------------------------------- #
    def _chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_tokens: int = 800,
    ) -> str:
        payload: dict[str, object] = {
            "model": self._config.model,
            "temperature": self._config.temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            raw = self._transport(
                self._config.endpoint, payload, headers, self._config.timeout
            )
            return _extract_text(raw)
        except LLMError:
            raise
        except Exception as exc:  # 传输层实现千差万别，统一归一为 LLMError
            raise LLMError(f"调用失败：{exc}", provider=PROVIDER_NAME) from exc

    def _chat_json(
        self, system: str, user: str, *, max_tokens: int = 800
    ) -> object:
        return _parse_json(self._chat(system, user, json_mode=True,
                                      max_tokens=max_tokens))

    # -- 1. classify -------------------------------------------------------- #
    def classify(
        self, text: str, role_hints: Sequence[RoleHint]
    ) -> ClassificationResult:
        known = [h.name for h in role_hints]
        catalogue = "、".join(known) if known else "（还没有任何角色）"
        system = (
            "你在给一个个人事务助手做分类。只输出 JSON，不要任何解释。\n"
            "输出格式："
            '{"kind":"action|wait|reminder",'
            '"role_guesses":[{"role_name":"<必须原样取自给定角色列表>","confidence":0.0},...],'
            '"need_clarification":true或false,"clarifying_question":"<仅在需要追问时给，否则null>"}\n'
            f"可选角色列表（**只能从这里选，不得自创**）：{catalogue}\n"
            "kind 判定：动作类=要产出某个东西；等候类=等别人/系统/时间；提醒类=到点通知一下。\n"
            "角色置信度低于 0.34 就置 need_clarification=true 并给出一句中文追问。"
        )
        user = f"待分类：{text}"
        try:
            payload = self._chat_json(system, user)
        except LLMError as exc:
            return self._degrade(exc).classify(text, role_hints)

        return _validate_classification(payload, known)

    # -- 1c. parse_delegation ------------------------------------------------ #
    def parse_delegation(
        self, text: str, known_projects: Sequence[str]
    ) -> DelegationIntent:
        """抽「改哪个项目 + 改什么」。**只抽，不猜。**

        ## 为什么不给它「默认项目」

        猜错项目的代价是**在错误的仓库里动手**。所以抽不出项目就是空串，
        交给上层追问 —— 一次交互 vs 改错目录，不成比例。

        ## brief 要保留动词

        「把 greet 函数改成返回你好」抽出来必须还是这句话（可执行），
        而不是「greet 函数返回你好」（丢了动作，变成名词短语）。
        所以提示里明确写了：``brief`` 是**可以直接转述给执行器的祈使句**。
        """
        if not known_projects:
            return DelegationIntent()
        catalogue = "\n".join(f"- {p}" for p in known_projects)
        system = (
            "你在给一个个人事务助手抽「要改代码的那句话」。"
            "只输出 JSON，不要解释。\n"
            '输出格式：{"is_delegation":true或false,"project":"<项目名或空串>",'
            '"brief":"<要做的事，一句话>或空串"}\n'
            f"可选项目（**只能原样取这些名字，不得自创**）：\n{catalogue}\n"
            "规则：\n"
            "1. 这句话**没有**要求改动代码时，is_delegation=false，"
            "另两项填空串。这包括：只是记事、只是提问、只是打招呼。\n"
            "2. 项目名抽不出就填空串，**不要挑一个最像的**。\n"
            "3. brief 必须是**能直接转述给执行者的祈使句**，"
            "保留动词与对象（「把 greet 改成返回你好」），"
            "不要压成名词短语（「greet 返回你好」—— 那丢了动作）。\n"
            "4. 只说了要改哪个项目、没说改什么时，brief 填空串，上层会追问。\n"
            "5. brief 里**不要**写项目名（已在 project 字段里），"
            "也不要写「用 opencode」「帮我」这类客套。"
        )
        try:
            payload = self._chat_json(system, f"用户说：{text}", max_tokens=160)
        except LLMError as exc:
            return self._degrade(exc).parse_delegation(text, known_projects)
        return _validate_delegation(payload, tuple(known_projects))

    # -- 1b. select_view ---------------------------------------------------- #
    @property
    def can_select_view(self) -> bool:
        """真模型 —— 它的 ``select_view`` 返回的是**真意见**（含 ``None``）。"""
        return True

    def select_view(
        self, text: str, views: Sequence[tuple[str, str]]
    ) -> str | None:
        """在封闭视图集里选一个。**只选名字，不写内容。**

        提示里刻意强调「挑不出就 null」，因为不给出这个出口，模型在
        犹豫时仍会挑一个 —— 于是「不确定」被渲染成「自信地答错」。
        实测过：不给出口时，模型对「我这周有什么安排」这类问句从不返回空。
        """
        allowed = [name for name, _ in views]
        if not allowed:
            return None
        catalogue = "\n".join(f"- {name}：{desc}" for name, desc in views)
        system = (
            "你在给一个个人事务助手判断：用户这句话想看**哪一张清单**？"
            "只输出 JSON，不要解释。\n"
            '输出格式：{"view": "<下面某个名字>" 或 null}\n'
            f"可选清单（**只能原样选这些名字，不得自创**）：\n{catalogue}\n"
            "规则：\n"
            "1. 挑不出对应清单时必须返回 null —— 这是允许且常见的答案，"
            "不要为了给答案而硬选。\n"
            "2. 区分时间范围：问「本周/这周」不等于「今天」。\n"
            "3. 用户在描述自己的状态、心情、感受时选 null —— "
            "助手只有事务清单，答不了那个。"
        )
        try:
            payload = self._chat_json(system, f"用户问：{text}", max_tokens=64)
        except LLMError as exc:
            return self._degrade(exc).select_view(text, views)
        if not isinstance(payload, Mapping):
            return None
        picked = payload.get("view")
        # 封闭集校验：自创的名字一律丢弃。模型编一个视图名出来时，
        # 照着它渲染等于去调一个不存在的表。
        if not isinstance(picked, str) or picked not in allowed:
            return None
        return picked

    # -- 2. refine_title ---------------------------------------------------- #
    def refine_title(self, text: str) -> str:
        system = (
            "把一段口语化输入收敛成一句话标题。只输出标题本身，不要引号、句号、解释。\n"
            "规则：剥掉「帮我/记一下/麻烦」等开场白；保留「下周二」这类时间信息；"
            "末尾的补充说明删掉；不超过 30 字。绝不能返回空串。"
        )
        try:
            title = self._chat(system, f"输入：{text}", max_tokens=64).strip()
        except LLMError as exc:
            return self._degrade(exc).refine_title(text)
        # 剥两端时**必须连标点一起剥**：模型照提示词剥掉了「记一下」，
        # 但常把后面的冒号留在标题头上（实测「记一下：X」→「：X」）。
        # 只 strip 空白和引号挡不住这种 —— 见 rules.TITLE_EDGE_CHARS。
        cleaned = strip_title_edges(title)
        if not cleaned:
            return self._degrade(
                LLMError("模型返回了空标题", provider=PROVIDER_NAME)
            ).refine_title(text)
        # 提示词里的字数约束对模型只是建议，代码层必须兜住上限
        return _cap_title(cleaned)

    # -- 3. split_steps ----------------------------------------------------- #
    def split_steps(
        self, task: TaskRef, instruction: str | None = None
    ) -> tuple[str, ...]:
        system = (
            "把一件事拆成可执行的步骤。**只拆步骤，不要排序、不要给优先级。**\n"
            "每行一步，格式为 `- <步骤>`，不要编号、不要解释。"
        )
        detail = f"\n用户额外要求：{instruction}" if instruction else ""
        user = (
            f"事务：{task.title}\n"
            f"意图：{task.intent or '（未说明）'}\n"
            f"完成标准：{task.definition_of_done or '（未声明）'}{detail}"
        )
        try:
            text = self._chat(system, user, max_tokens=400)
        except LLMError as exc:
            return self._degrade(exc).split_steps(task, instruction)

        steps = _parse_steps(text)
        if not steps:
            return self._degrade(
                LLMError("模型没给出可解析的步骤", provider=PROVIDER_NAME)
            ).split_steps(task, instruction)
        return steps

    # -- 4. draft ----------------------------------------------------------- #
    def draft(self, task: TaskRef, instruction: str) -> str:
        system = (
            "你在帮用户起草内容。**绝对不许编造事实。**\n"
            "硬规则：\n"
            "1. 只允许使用我给你的字段里的信息；任何你不知道的事实一律写 `[TODO]`，"
            "不要用「通常」「一般来说」补全。\n"
            "2. 必须保留下列小标题，一个都不能少，顺序不变："
            "`## 目标`、`## 完成标准`、`## 材料`、`## 待确认`。\n"
            "3. 输出 Markdown 正文，不要包在代码块里，不要额外解说。"
        )
        user = (
            f"标题：{task.title}\n"
            f"意图：{task.intent or '（未说明，写 [TODO]）'}\n"
            f"完成标准：{task.definition_of_done or '（未声明，写 [TODO]）'}\n"
            f"本次要求：{instruction or '（无）'}"
        )
        try:
            text = self._chat(system, user, max_tokens=900)
        except LLMError as exc:
            return self._degrade(exc).draft(task, instruction)
        return _repair_skeleton(text, task, instruction)

    # -- 5. suggest_schedule ------------------------------------------------- #
    def suggest_schedule(
        self, task: TaskRef, signals: Sequence[SignalRef]
    ) -> str:
        system = (
            "根据给定的透明提示信号，说明它们各自意味着什么。\n"
            "**不得给出优先级结论、不得说「你应该先做 X」。**"
            "两三句话即可，中文。"
        )
        listed = "；".join(f"{s.reason}（权重 {s.weight}）" for s in signals) or "（无）"
        user = f"事务：{task.title}\n命中信号：{listed}"
        try:
            text = self._chat(system, user, max_tokens=200).strip()
        except LLMError as exc:
            return self._degrade(exc).suggest_schedule(task, signals)
        return text or self._degrade(
            LLMError("模型返回了空建议", provider=PROVIDER_NAME)
        ).suggest_schedule(task, signals)


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #
def _extract_text(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"响应不是合法 JSON：{exc.msg}", provider=PROVIDER_NAME) from exc
    if not isinstance(data, dict):
        raise LLMError("响应结构异常", provider=PROVIDER_NAME)
    if "error" in data:
        detail = data.get("error")
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        raise LLMError(f"接口报错：{message}", provider=PROVIDER_NAME)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("响应里没有 choices", provider=PROVIDER_NAME)
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMError("choices 结构异常", provider=PROVIDER_NAME)
    message_obj = first.get("message")
    if not isinstance(message_obj, dict):
        raise LLMError("响应里没有 message", provider=PROVIDER_NAME)
    content = message_obj.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMError("模型返回了空内容", provider=PROVIDER_NAME)
    return content


def _parse_json(text: str) -> object:
    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 有些模型会在 JSON 前后带解说，抓第一段完整对象
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match is None:
            raise LLMError(
                "模型没有返回可解析的 JSON", provider=PROVIDER_NAME
            ) from None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"模型返回的 JSON 不合法：{exc.msg}", provider=PROVIDER_NAME
            ) from exc


def _validate_classification(payload: object, known: Sequence[str]) -> ClassificationResult:
    """把模型输出**收敛**成合法结果。

    两道防线：kind 必须在白名单内；角色名必须在已知列表里。
    后者是必须的 —— 否则模型编一个角色名，CLI 会真的建出这个角色
    （这正是之前修过的「垃圾角色」问题的模型版）。
    """
    if not isinstance(payload, dict):
        raise LLMError("分类结果不是对象", provider=PROVIDER_NAME)

    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in ALLOWED_KINDS:
        raise LLMError(f"未知的 kind：{kind!r}", provider=PROVIDER_NAME)

    guesses: list[RoleGuess] = []
    raw_guesses = payload.get("role_guesses")
    if isinstance(raw_guesses, list):
        allowed = set(known)
        for item in raw_guesses:
            if not isinstance(item, dict):
                continue
            name = item.get("role_name")
            if not isinstance(name, str):
                continue
            if allowed and name not in allowed:
                continue  # 模型自创角色名 -> 直接丢弃
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            confidence = min(1.0, max(0.0, confidence))
            if confidence >= ROLE_KEEP_MIN:
                guesses.append(RoleGuess(role_name=name, confidence=confidence))

    guesses.sort(key=lambda g: (-g.confidence, g.role_name))
    guesses = guesses[:3]

    need = bool(payload.get("need_clarification"))
    question = payload.get("clarifying_question")
    if not isinstance(question, str) or not question.strip():
        question = None
    if guesses and guesses[0].confidence >= ROLE_MATCH_THRESHOLD:
        need = False
        question = None
    elif not guesses:
        # 零个合法候选 —— 模型的角色名可能全被过滤掉了（或角色列表本来就是空的）。
        # 这种情况下**必须追问**，否则事务会被塞进一个猜出来的角色。
        need = True
    if need and question is None:
        question = (
            "这是放到哪个脉络里？"
            if not guesses
            else f"这是放到「{guesses[0].role_name}」还是「{guesses[1].role_name}」里面？"
            if len(guesses) > 1
            else f"这是放到「{guesses[0].role_name}」里面吗？"
        )
    return ClassificationResult(
        kind=kind,
        role_guesses=tuple(guesses),
        need_clarification=need,
        clarifying_question=question,
    )


def _cap_title(title: str) -> str:
    """标题长度兜底。

    与规则层共用同一个上限 —— 提示词里的字数要求对模型只是建议，
    **代码层必须拦住超长标题**，否则数据库里会积攒一堆没法看的长句。
    """
    cleaned = _WS_RE.sub(" ", title).strip()
    if len(cleaned) <= TITLE_MAX_LEN:
        return cleaned
    truncated = cleaned[: TITLE_MAX_LEN - 1]
    # 别在词中间切：退到最后一个明显的分隔符
    for sep in ("，", ",", "、", "；", " "):
        cut = truncated.rfind(sep)
        if cut >= _MIN_PREFIX:
            return truncated[:cut].strip()
    return truncated + "…"


def _parse_steps(text: str) -> tuple[str, ...]:
    """解析步骤列表。

    **必须真的有列表标记** —— 否则模型的散文会被当成「一步」，
    那还不如退回规则层给出的脚手架。
    """
    steps: list[str] = []
    had_marker = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        stripped = re.sub(r"^(?:[-*•]\s*|\d+[.、)）]\s*)", "", line).strip()
        if stripped != line:
            had_marker = True
        if not stripped or stripped.startswith("#"):
            continue
        if stripped not in steps:
            steps.append(stripped)
    if not had_marker:
        return ()
    return tuple(steps)


_REQUIRED_SECTIONS = ("## 目标", "## 完成标准", "## 材料", "## 待确认")


def _repair_skeleton(text: str, task: TaskRef, instruction: str) -> str:
    """保证骨架完整 —— 无论模型说什么，这四个小标题必须在。

    这是「不伪造事实」的最后一道防线：模型可能漏掉「待确认」，
    而那正是提醒用户「这里我没信息」的地方。
    """
    body = text.strip()
    if "```" in body:
        body = re.sub(r"^```[a-z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()

    if not body.startswith("# "):
        body = f"# {task.title}\n\n{body}"

    fillers = {
        "## 目标": f"## 目标\n{task.intent or '[TODO] 这次想做到什么程度？'}",
        "## 完成标准": f"## 完成标准\n"
        f"{task.definition_of_done or '[TODO] 做到什么程度算完？'}",
        "## 材料": "## 材料\n- [TODO] 需要哪些材料/数据？",
        "## 待确认": "## 待确认\n- [TODO] 有没有遗漏的约束条件？",
    }
    missing = [s for s in _REQUIRED_SECTIONS if s not in body]
    if missing:
        body = body.rstrip() + "\n\n" + "\n\n".join(fillers[s] for s in missing)

    if instruction.strip() and "本次要求" not in body:
        body = f"{body.rstrip()}\n\n## 本次要求\n{instruction.strip()}"

    if "[TODO]" not in body:
        body = (
            body.rstrip()
            + "\n\n## 待确认\n- [TODO] 以上内容里哪些是我没说清楚、你是猜的？"
        )
    return body


def _validate_delegation(payload: object, known: tuple[str, ...]) -> DelegationIntent:
    """把模型吐的东西**收窄**回闭集。坏形状一律当「不是委派」。

    与 :func:`_validate_classification` 同一套理由：模型会编造项目名，
    编造的名字必须被丢弃 —— 而丢弃之后就没有可信的项目，于是整条按
    「不是委派」处理（上层会走记事或追问，而不是拿一个假项目去动手）。
    """
    if not isinstance(payload, Mapping):
        return DelegationIntent()
    if payload.get("is_delegation") is not True:
        return DelegationIntent()

    project = payload.get("project")
    project = project.strip() if isinstance(project, str) else ""
    # **闭集**：自创名字丢弃
    if project and project not in known:
        project = ""

    brief = payload.get("brief")
    brief = brief.strip() if isinstance(brief, str) else ""

    return DelegationIntent(is_delegation=True, project=project, brief=brief)
