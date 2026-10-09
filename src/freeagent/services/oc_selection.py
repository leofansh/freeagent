"""OpenCode 的**可选集合**：项目 / 工作模式 / 模型 / 推理档位。

## 这个模块解决什么

用户要「像 OpenCode Desktop 那样在飞书里选」，但 Desktop 是**下拉框**，
飞书**没有下拉框**。所以只能拆成四段按钮卡 —— 而拆开就带来三个真问题：

1. **给什么选项**（不能把 8475 个模型全端上卡）
2. **点了之后走到哪一步**（四段是有序的，且依赖上一步的结果）
3. **选完怎么落到委派上**（要能跨消息、跨进程重启地记住）

这里只做 **1 和 3 的数据部分**：查询、过滤、状态读写。
**不**发任何卡片、**不**解析任何点击 —— 那是
:mod:`freeagent.feishu.sender` 与 :mod:`freeagent.feishu.bridge` 的事。

## 为什么过滤规则住在服务层而不是卡片里

卡片里写过滤 = 每个入口（飞书卡、Web、将来的别的通道）各写一遍，
而它们会漂移。更具体的坏处：**飞书按钮文案有 40 字上限**，于是「先在卡片里
过滤再截断」和「先截断再过滤」会给出不同的集合，而用户看不出哪个是真规则。

所以：过滤在这里，卡片只负责把 :class:`Option` 渲染成按钮。

## 三个过滤规则，都是实测得来的（2026-10-07，opencode 1.18.34）

- **项目**：`GET /project` 返回 4 条，其中一条 worktree 是 `/`（``global``）。
  那不是真实目录，委派到它没有意义 → 过滤（:data:`DEFAULT_PROJECT_WORKTREE`）。
- **工作模式**：`GET /agent` 返回 17 条，但 ``compaction`` / ``summary`` /
  ``title`` 的 ``model`` 是 ``null`` —— 那是 OpenCode **内部件**，Desktop 的
  下拉框也不列它们 → 只要 ``mode == "primary"`` 且有 model 的。
- **模型**：``GET /provider`` 全量 **8475** 个模型，但 ``connected`` 只有
  ``['deepseek', 'opencode']``，其下**共 85 个**。没凭据的模型点了必然 401/402
  → **只给 connected 的**。

## 为什么「不指定推理档」是一个合法选项

实测 4300/8475 个模型带 ``variants``，且**没有一个带 ``default`` 档**。
所以「不选」不是「落到某个默认」，而是**不施加 variant**、用模型基线。
把它做成第一个选项（而不是留空），是因为省略与「选了一个不存在的档」在
OpenCode 侧**都是 204** —— 服务端不校验，所以**只能由我们**保证不选错。

## 状态为什么存 ``config.json`` 而不是 ``llm.env``

``llm.env`` 是**秘密**的存放处，而 agent 名 / 模型 id / 档位名**不是秘密**
（``/config/providers`` 那种明文 key 才是）。混进去会让「哪些文件碰不得」
这条规矩失效。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "Option",
    "Selection",
    "list_project_options",
    "list_agent_options",
    "current_agent_is_valid",
    "agent_is_executable",
    "list_model_options",
    "daily_model_options",
    "model_list_note",
    "connected_providers",
    "variant_options",
    "current_variant_is_valid",
    "load_selection",
    "save_selection",
    "load_curated_models",
    "save_curated_models",
    "curate",
    "daily_models_configured",
    "MODEL_PAGE_SIZE",
    "VARIANT_ORDER",
]

#: 模型卡每页几个。飞书一张卡放 85 个按钮会被截断，而每页 12 个是
#: 肉眼一屏能扫完、又不至于要点「下一页」点到手酸的数字。
MODEL_PAGE_SIZE = 12

#: 推理档的展示顺序。
#:
#: **为什么要有顺序**：实测 ``variants`` 返回的是 dict，键序不保证是
#: 「弱 → 强」。而 ``none`` / ``minimal`` / ``low`` / ``medium`` / ``high`` /
#: ``xhigh`` / ``max`` 之间**有强弱顺序**，用户预期「越高越靠后」。
#: 不排序的话同一组档位在不同模型上会以不同顺序出现，像 bug。
VARIANT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

#: 状态文件名。放在 ``~/.freeagent/`` 下，与 ``config.json`` 同级但独立 ——
#: 独立是为了**不与 ``save_config`` 抢同一个文件**（那个函数会整体重写
#: ``config.json``，会把这里的字段抹掉）。
_SELECTION_FILE = "oc_selection.json"


@dataclass(frozen=True, slots=True)
class Option:
    """一个可选项。**卡片只认这一个形状**，所以渲染永远一致。

    :attr:`value` 是**回传的稳定标识**（项目 worktree、模型
    ``provider/model``），:attr:`label` 是给人看的。刻意分开：
    显示名会变（``Fledge Alpha Free`` ↔ ``Space Bunny Free`` 就变过），
    而回传必须用不会变的那个，否则改个显示名就点不动了。
    """

    value: str
    label: str
    #: 副标题（如「免费」「provider 名」）。可空。
    hint: str = ""
    #: 灰掉但仍显示 —— 用于「这个模型没有推理档可选」这类**信息**，
    #: 而不是把整条藏起来（藏起来用户会以为漏了）。
    disabled: bool = False


@dataclass(slots=True)
class Selection:
    """当前选择。**字段全可空**：每一段都可能还没选。

    刻意做成可变 dataclass 而不是不可变：这个对象就是「当前对话上下文里
    选了啥」，它天生要被逐段改。
    """

    project: str = ""     # worktree（绝对路径，正斜杠）
    agent: str = ""       # ``Sisyphus - ultraworker`` 这类**精确名**
    model: str = ""       # ``provider/modelID``
    variant: str = ""     # 空 = 不施加 variant，用模型基线

    def is_empty(self) -> bool:
        return not (self.project or self.agent or self.model or self.variant)

    def clear_from(self, stage: str) -> None:
        """改了某一段之后，**把下游全部清掉**。

        为什么必须清：选了 GPT-5 再把模型换成 Big Pickle，而 Big Pickle
        **没有推理档**（实测 ``variants=[]``）—— 若不清，用户之前选的
        ``high`` 会留在状态里，于是我们对 OpenCode 发一个它不认识的
        ``variant: "high"``。而 OpenCode **不校验**（实测错误档也返回 204），
        症状是「推理档明明选的是 High，实际没生效」—— 极难排查。
        """
        order = ("project", "agent", "model", "variant")
        if stage not in order:
            return
        for name in order[order.index(stage) + 1:]:
            setattr(self, name, "")

    def describe(self) -> list[str]:
        """给人看的一行行摘要。空的那段显示成「（默认）」而不是消失。

        不消失是有意的：卡上少一行，用户会以为程序没听见他选了什么。
        """
        return [
            f"项目：{self.project or '（默认）'}",
            f"工作模式：{self.agent or '（默认）'}",
            f"模型：{self.model or '（默认）'}",
            f"推理档：{self.variant or '（默认 / 模型基线）'}",
        ]


# ── 项目 ──────────────────────────────────────────────────────────────── #

def list_project_options(rows: Iterable[dict[str, Any]]) -> list[Option]:
    """``GET /project`` → 可选项目。

    过滤掉 ``worktree == "/"``（``global``）。**其余一律保留** ——
    白名单交集由 :func:`~freeagent.services.opencode_projects.authorized_names`
    在**创建委派那一步**再判一次；这里先不滤，是为了「界面上看得见但会
    被闸门拒」比「看不见」更容易排查。

    刻意保留两条防线而不是只留一条：这里滤 ``global`` 是因为它**不是目录**
    （委派进去物理上不可能），白名单是安全边界。两者理由不同。
    """
    from .opencode_projects import DEFAULT_PROJECT_WORKTREE, _basename

    out: list[Option] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        worktree = str(row.get("worktree") or row.get("path") or "").strip()
        worktree = worktree.replace("\\", "/").rstrip("/")
        if not worktree or worktree == DEFAULT_PROJECT_WORKTREE:
            continue
        name = str(row.get("name") or "").strip() or _basename(worktree)
        if not name or worktree.casefold() in seen:
            continue
        seen.add(worktree.casefold())
        out.append(Option(value=worktree, label=name, hint=worktree))
    return out


# ── 工作模式 ──────────────────────────────────────────────────────────── #

#: OpenCode 内部件的名字。
#:
#: 真正的判据是 ``model is None``（实测这三个都这样），但**同时按名字挡一道**：
#: 万一哪天 OpenCode 给 ``summary`` 配了默认模型，``model is None`` 就漏了，
#: 而 Desktop 的下拉框里依然不会出现它们 —— 用户会问「为什么飞书里有
#: summary 这个选项，Desktop 里没有」。名字这道闸门让两者对齐。
_INTERNAL_AGENTS = frozenset({"compaction", "summary", "title", "internal"})


def list_agent_options(rows: Iterable[dict[str, Any]]) -> list[Option]:
    """``GET /agent`` → 可选工作模式。

    只要 ``mode == "primary"`` **且** ``model`` 非 null 的。
    ``mode`` 字段实测存在（primary / subagent / all）。
    """
    out: list[Option] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or name in _INTERNAL_AGENTS:
            continue
        if str(row.get("mode") or "") != "primary":
            continue
        model = row.get("model")
        if not isinstance(model, dict):
            continue
        provider = str(model.get("providerID") or "").strip()
        model_id = str(model.get("modelID") or "").strip()
        hint = f"{provider}/{model_id}" if provider and model_id else ""
        out.append(Option(value=name, label=name, hint=hint))
    return out


def current_agent_is_valid(rows: Iterable[dict[str, Any]], agent: str) -> bool:
    """当前选择里的工作模式**现在还在吗**？

    :meth:`Selection.clear_from` 只在**用户改了上游**时清下游，而 agent
    这一段的失效**不来自用户操作**：

    - agent 定义可以是**项目级**的（``.opencode/agent/*.md``），而
      ``Selection.agent`` 是**全局一份**（``~/.freeagent/oc_selection.json``），
      不随项目变 —— 在 A 项目里选的 agent，切到 B 项目可能压根不存在。
    - OpenCode 升级后内置 agent 会变；用户也可能自己删了某个 agent 文件。

    ## 为什么必须显式判：OpenCode 这边是**查表**，不是校验

    ``src/agent/agent.ts`` 里 ``get`` 的实现就一句：

    .. code-block:: js

        const get = Effect.fnUntraced(function* (agent) {
          return agents[agent]
        })

    取不到就返回 ``undefined``，**不抛错、不报「没有这个 agent」**。
    （对比：内置 ``default_agent`` 配错时那里**会** ``throw`` —— 但经
    ``--agent`` / ``payload["agent"]`` 传进来的**不走那道检查**。）

    所以「指定的工作模式已经不存在」这件事**没有任何报错可查**，症状表现为
    「派发出去了，但用的不是你选的那个模式」。这正是 11.9.9 要防的同型故障。

    ## 为什么复用 :func:`list_agent_options` 而不是再写一遍过滤

    过滤规则（``mode == "primary"``、model 非 null、排除内部件）只准有一份。
    在这里另写一遍，就会出现「列表里看得见、校验时说不认识」的错位。
    """
    if not agent:
        return True
    return any(opt.value == agent for opt in list_agent_options(rows))


def agent_is_executable(rows: Iterable[dict[str, Any]], agent: str) -> bool:
    """**执行期**校验：这个工作模式在「执行世界」里真的用得了吗？

    ## 为什么和 :func:`current_agent_is_valid` 是两个函数

    二者回答的是**不同世界**的问题（实测踩过，见设计文档 11.13.8 / 11.14）：

    - **选择世界**（Web 四段选择器，``discovery()`` 非隔离实例）：看得见
      真实配置 —— 登录态、云端 agent（如 ``Prometheus - Plan Builder``）、
      云端模型。展示过滤要求 ``model`` 非空，因为列表要连 hint 一起渲染。
    - **执行世界**（执行器，隔离实例）：``HOME`` 关进 jail、不继承 provider
      配置（V1.14 定下的安全属性），只看得到内置 agent，且**全部**
      ``model is None`` —— 模型由 ``prompt_async`` **显式**传，不靠 agent 自带。

    于是两条规则必然分叉：在执行世界拿「model 非空」当门槛会**错杀一切**
    （连 ``build`` / ``plan`` 都被判无效）。执行期真正要紧的只有三件事：

    1. 名字存在（OpenCode 对不存在的 agent 是查表取 ``undefined``，静默退化）；
    2. ``mode == "primary"``（``subagent`` 不能被 ``--agent`` 选中）；
    3. 不是内部件（``compaction`` / ``summary`` / ``title`` 不该当工作模式）。

    与 :func:`current_agent_is_valid` 的分叉是**有意的**，不是漂移：
    那边答「目录里还有没有它」，这边答「执行器用它跑不跑得起来」。
    """
    if not agent:
        return True
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or name in _INTERNAL_AGENTS:
            continue
        if name == agent:
            return str(row.get("mode") or "") == "primary"
    return False


# ── 模型 ──────────────────────────────────────────────────────────────── #

#: 免费模型的判据：``cost.input == 0 and cost.output == 0``。
#:
#: 刻意**不**用名字里有没有 "free"：实测列表里 ``space-bunny-free`` 叫 free，
#: 而 ``big-pickle`` / ``kimi-k3`` 同样 ``cost`` 为 0 却没在名字里带 free。
#: 按名字判会漏掉一大半，而「哪些真不要钱」只有 ``cost`` 说了算。
def _is_free(model: dict[str, Any]) -> bool:
    cost = model.get("cost")
    if not isinstance(cost, dict):
        return False
    return not cost.get("input") and not cost.get("output")


def list_model_options(
    payload: dict[str, Any], *, provider_filter: Sequence[str] | None = None
) -> list[Option]:
    """``GET /provider`` → 可选模型，**免费置顶**。

    :param provider_filter: 只保留这些 provider（通常传 ``connected``）。
        为 ``None`` 时**全给** —— 那会把 8475 个模型端上卡，所以生产路径
        必须传；但保留这个参数是因为测试要能直接喂小样本。

    排序：先按「免费」，再按 **provider 内**的名称，最后按 provider 名。
    刻意不给 provider 排序额外加权 —— 用户认的是模型名，
    而 DeepSeek 那 2 个模型排在 OpenCode 的 83 个里哪个位置他并不关心。
    """
    all_rows = payload.get("all") if isinstance(payload, dict) else None
    if not isinstance(all_rows, list):
        return []

    wanted = (
        {str(p).strip().casefold() for p in provider_filter}
        if provider_filter is not None else None
    )

    free: list[Option] = []
    paid: list[Option] = []
    for provider in all_rows:
        if not isinstance(provider, dict):
            continue
        pid = str(provider.get("id") or "").strip()
        pname = str(provider.get("name") or "").strip() or pid
        if not pid or (wanted is not None and pid.casefold() not in wanted):
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, model in models.items():
            if not isinstance(model, dict):
                continue
            label = str(model.get("name") or "").strip() or str(model_id)
            variants = model.get("variants")
            n_variants = len(variants) if isinstance(variants, dict) else 0
            hint = pname if not n_variants else f"{pname} · {n_variants} 档"
            opt = Option(
                value=f"{pid}/{model_id}",
                label=label,
                hint=hint,
            )
            (free if _is_free(model) else paid).append(opt)

    # 组内按展示名排 —— 模型 id 在变（「deepseek-v4-pro」→「deepseek-v4.1-flash」），
    # 名字是用户认的东西。
    free.sort(key=lambda o: (o.label.casefold(), o.value))
    paid.sort(key=lambda o: (o.label.casefold(), o.value))
    return free + paid


def connected_providers(payload: dict[str, Any]) -> list[str]:
    """``GET /provider`` 的 ``connected`` 字段。

    刻意**只信这个字段**而不是「配置里有 key 的 provider」：实测
    ``/config/providers`` 会返回明文 key，而「有 key」与「connected」
    并不总是同一个集合（过期 key 仍在配置里，但 connected 已经不含它）。
    """
    connected = payload.get("connected") if isinstance(payload, dict) else None
    if not isinstance(connected, list):
        return []
    return [str(x).strip() for x in connected if str(x or "").strip()]


def _variants_of(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """从 ``/provider`` 里挖出某个 ``provider/model`` 的 ``variants``。"""
    provider_id, _, model_id = model.partition("/")
    if not provider_id or not model_id:
        return {}
    for provider in payload.get("all") or ():
        if not isinstance(provider, dict):
            continue
        if str(provider.get("id") or "") != provider_id:
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            return {}
        entry = models.get(model_id)
        if not isinstance(entry, dict):
            return {}
        variants = entry.get("variants")
        return variants if isinstance(variants, dict) else {}
    return {}


# ── 推理档 ────────────────────────────────────────────────────────────── #

def variant_options(payload: dict[str, Any], model: str) -> list[Option]:
    """某个模型可用的推理档。**第一个恒是「不指定」**。

    第一个选项（:attr:`Selection.variant` 为空）代表「用模型基线」，
    见模块 docstring 里「为什么『不指定推理档』是一个合法选项」。
    它**永远存在**，哪怕模型一个档都没有 —— 那样这张卡就只有一个按钮，
    等于告诉用户「这个模型没有档可选」，而不是让他卡在这一步。
    """
    out = [Option(value="", label="不指定（用模型默认）", hint="基线")]
    variants = _variants_of(payload, model)
    known = [v for v in VARIANT_ORDER if v in variants]
    # 未知档位**也列出来**，但排最后 —— 不认识的名字不能静默丢掉
    # （丢掉的话「我明明有 high 却选不到」无从解释）。
    extra = sorted(v for v in variants if v not in VARIANT_ORDER)
    for name in [*known, *extra]:
        out.append(Option(value=name, label=name))
    return out


def current_variant_is_valid(payload: dict[str, Any], model: str, variant: str) -> bool:
    """当前选择里的档位**对当前模型**还成立吗？

    :meth:`Selection.clear_from` 已经会在正常路径上清掉它，但**状态是
    跨消息持久化的**：用户可能昨天选了 ``high``，今天在模型列表里换了模型
    却没走完四段流程。所以每次真正发指令前都要过这一关。
    """
    if not variant:
        return True
    return variant in _variants_of(payload, model)


# ── 日常可选清单（curation）──────────────────────────────────────────── #
#
# 为什么需要这一层：连了凭据的 provider 下**有 85 个模型**（实测
# 2026-10-07：deepseek 2 + opencode 83）。而「日常要用的」通常不到十个。
# 85 个按钮既放不进飞书一张卡（于是我写了分页），也超出「识别优于回忆」
# 能承载的量 —— 选项越多越没人选。
#
# 与 OpenCode Desktop「管理模型 → 自定义模型选择器中显示的模型」是同一个
# 意图（给每个模型一个开关），但**清单放在 FreeAgent 侧**：
#
# - Desktop 那个开关写进 OpenCode 自己的配置；FreeAgent 若也去写，就变成
#   **两个进程共同拥有同一个配置文件**，迟早互相覆盖
# - 飞书用不着管 OpenCode 的配置，它只需要知道「日常能用哪些」
#
# 刻意**不**提供「连接提供商」：``/config/providers`` 只有 GET，且实测返回
# **明文 API Key**。让凭据流经一个无鉴权的 Web 界面等于把密钥摊开。

#: 清单文件名。与 :data:`_SELECTION_FILE` 同级但独立 —— 理由同样是
#: ``save_config`` 会整体重写 ``config.json``。
_CURATED_FILE = "oc_models.json"


def _curated_path(home: str | Path | None = None) -> Path:
    base = Path(home) if home is not None else Path.home() / ".freeagent"
    return base / _CURATED_FILE


def load_curated_models(home: str | Path | None = None) -> list[str]:
    """日常可选的模型 id 列表。**读不出来就是空清单**（不抛）。

    空清单**不是错误**，它意味着「还没挑过」—— 那时 :func:`daily_model_options`
    退回给全部已连接模型（见那里的理由）。若这里抛异常，助手会因一个
    可选的配置起不来。
    """
    try:
        raw = json.loads(_curated_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x or "").strip()]
    if isinstance(raw, dict) and isinstance(raw.get("models"), list):
        return [str(x).strip() for x in raw["models"] if str(x or "").strip()]
    return []


def save_curated_models(models: Sequence[str], home: str | Path | None = None) -> Path:
    """写日常可选清单，返回写入路径。**去重并保序**。

    保序是有意的：用户勾选的顺序就是他的偏好顺序（常用的排前面），
    而界面上除「当前选中项高亮」外没有别的排序依据。
    """
    path = _curated_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    ordered: list[str] = []
    for item in models:
        key = str(item or "").strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    path.write_text(
        json.dumps({"models": ordered}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def curate(action: str, model: str, home: str | Path | None = None) -> list[str]:
    """增删一个模型，返回新的清单。

    :param action: ``"add"`` 或 ``"remove"``。
    :raises ValueError: 未知 action，或 ``model`` 为空 —— 调用方要把它
        变成 400，而不是默默加了个空串进清单。
    """
    key = str(model or "").strip()
    if not key:
        raise ValueError("模型 id 不能为空")
    current = load_curated_models(home)
    if action == "add":
        if key not in current:
            current.append(key)
    elif action == "remove":
        # 移除**全部**同名项：清单可能已被手改成有重复（它是纯 JSON 文件），
        # 只删一个会留下幽灵项，而界面会把同一个模型显示两次。
        current = [m for m in current if m != key]
    else:
        raise ValueError(f"未知操作：{action}")
    save_curated_models(current, home)
    return current


def daily_models_configured(home: str | Path | None = None) -> bool:
    """用户是否**挑过**。用于区分「没挑=全给」与「挑了=按清单」。"""
    return bool(load_curated_models(home))


def daily_model_options(
    payload: dict[str, Any], *, home: str | Path | None = None
) -> list[Option]:
    """日常可选的模型。**清单为空就退回全部已连接模型**。

    ## 退回而不是给空

    因为「没挑过」是**正常状态**（刚装好的人还没配），给一个空列表会让人
    以为「没模型可用」，而实际上有 85 个 —— 于是去查凭据、查网络，而真实
    原因只是「还没挑」。这与 :func:`list_project_options` 里「滤掉 global」
    是同类理由。

    ## 清单里的模型这一版**不在**了怎么办

    跳过（不报错）。那是「你卸载了它 / 它改名了」，而症状是「我明明勾了它
    却选不到」—— 说清比报错有用。
    """
    every = list_model_options(payload, provider_filter=connected_providers(payload))
    curated = load_curated_models(home)
    if not curated:
        return every
    allowed = set(curated)
    return [o for o in every if o.value in allowed]


def model_list_note(options: Sequence[Option]) -> str:
    """模型段的提示语。

    刻意**不提分页**：清单通常十来个，一屏就够。而 :data:`MODEL_PAGE_SIZE`
    仍然生效 —— 万一清单勾到几十个，那时它才有用武之地。
    """
    if not options:
        return "日常清单是空的。"
    return f"日常清单里的 {len(options)} 个模型（可在设置里增删）。"


# ── 状态持久化 ────────────────────────────────────────────────────────── #

def _selection_path(home: str | Path | None = None) -> Path:
    base = Path(home) if home is not None else Path.home() / ".freeagent"
    return base / _SELECTION_FILE


def load_selection(home: str | Path | None = None) -> Selection:
    """读当前选择。**读不出来就返回空选择**，绝不抛。

    读不出来（文件不存在 / 坏了 / 权限）是**正常状态**：用户还没选过。
    那个理由与 :func:`freeagent.config.load_config` 一致 ——
    配置坏了不该让整个助手起不来。
    """
    try:
        raw = json.loads(_selection_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Selection()
    if not isinstance(raw, dict):
        return Selection()
    return Selection(
        project=str(raw.get("project") or ""),
        agent=str(raw.get("agent") or ""),
        model=str(raw.get("model") or ""),
        variant=str(raw.get("variant") or ""),
    )


def save_selection(selection: Selection, home: str | Path | None = None) -> Path:
    """写当前选择，返回写入的路径。"""
    path = _selection_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": selection.project,
                "agent": selection.agent,
                "model": selection.model,
                "variant": selection.variant,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return path