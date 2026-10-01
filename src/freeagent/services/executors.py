"""执行器接线：把后端特有的知识关进一个带版本号的接缝。

## 为什么要这一层

委派要启动一个外部执行器（当前只有 opencode），而**那个东西的协议会变**。

V1 → V2 时 permission 模型的字段名全变了（``permission`` 对象 / ``bash`` /
``task`` → ``permissions`` 数组 / ``shell`` / ``subagent``），而**旧字段被静默
忽略** —— 不报错、不警告，闸门随之失效，而失效方向恰好是最危险的那侧
（默认回到 allow）。

在有这层之前，那些字段名散在 :mod:`freeagent.services.opencode_server` 的三个
函数里（权限配置、事件解析、答复载荷）。改一次协议要动三个地方，而漏掉一个
的后果是**闸门静默失效**而不是「跑不起来」。

有这层之后：协议变更收敛到**一个文件**，且改错了会**响**。

## 两条不做

1. **不猜未知的字段。** V2 的 ``permissions`` **数组元素是什么形状**，我们
   不知道（只知道键名从 ``permission`` 变到了 ``permissions``）。所以 V2 适配器
   把它**记下来但拒绝使用**，而不是照着一个「看起来很像对的」形状写下去 ——
   见 :mod:`docs.postmortem.0001`，写反的东西看起来总很像对的。
2. **不放行未验证的版本。** 版本闸门只放 ``verified=True`` 的适配器。所以
   这次改动**不改变任何运行时行为**：V2 仍然拒绝派发。

## 版本闸门在哪

:func:`freeagent.delegate.version_mismatch_reason` 拿探测到的主版本来这里问。
**探测**而不是配置 —— 能力声明一旦手写就会陈旧，而陈旧的方向是危险的那侧
（同上，V1 的教训）。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Sequence

__all__ = [
    "ToolPermission",
    "ToolQuestion",
    "ExecutorAdapter",
    "UnverifiedExecutorError",
    "adapter_for",
    "all_adapters",
    "known_majors",
    "dispatchable_majors",
    "V1_FIELD_NAMES",
    "V2_FIELD_RENAMES",
]


class UnverifiedExecutorError(RuntimeError):
    """这个适配器是对着**文档**写的，还没对着真实实例验过。

    刻意与 :class:`~freeagent.domain.errors.ServerError` 区分：
    那个是「服务起不来」，这个是「我们还没确认自己能正确地跟它说话」。
    两者都不该被当成功。
    """


@dataclasses.dataclass(frozen=True, slots=True)
class ToolPermission:
    """一次「执行器想动手」的请求，**归一化后**的形状。

    归一化是这层的核心价值：卡片渲染、审批落库、审计日志都只认这个形状，
    所以换后端时它们一行都不用改。执行器特有的坐标（``tool.messageID`` /
    ``callID`` 之类）在适配器里丢掉 —— 留着容易让人误以为该拿它们做点什么。
    """

    request_id: str
    permission: str
    paths: tuple[str, ...] = ()
    diff: str | None = None
    suggested_always: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """一行摘要，给日志用。"""
        where = self.paths[0] if self.paths else "(无路径)"
        return f"{self.permission} → {where}"


@dataclasses.dataclass(frozen=True, slots=True)
class ToolQuestion:
    """一次「agent 在提问」的请求，**归一化后**的形状。

    与 :class:`ToolPermission` 同一套理由：卡片渲染、落库、审计只认这个
    形状，所以换后端时它们一行都不用改。

    ## ``options`` 的粒度**尚未验证**

    实测载荷里 ``questions`` 与 ``options`` 都是数组，但**没有验证过
    ``options`` 是「全部问题的选项池」还是「每个问题各一组」**。所以这里
    **原样保留成一个扁平的字符串元组**，不猜嵌套。

    代价要说清：因为本项目一次只问一个问题，如果 ``options`` 实际是按问题
    分组的，那么发第 1 张卡时会把第 2 个问题的选项也列出来。所以卡片上
    **必须**写明这是「可选项」，而不能声称「这是这题的选项」。
    哪天验明了按问题分组，改这一处即可 —— 归一化层存在的意义就是这个改动
    只落在**一个文件**里。
    """

    request_id: str
    questions: tuple[str, ...]
    options: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """一行摘要，给日志用。"""
        n = len(self.questions)
        return f"{n} 个问题" + (f"，{len(self.options)} 个可选项" if self.options else "")


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutorAdapter:
    """一个执行器 + 一个主版本的接线。

    ``verified`` 是这里最重要的字段：它为 ``False`` 时 :func:`adapter_for`
    仍然返回适配器（好让调用方能给出**具体**的拒绝理由），但版本闸门不放行。
    """

    executor: str
    major: int
    verified: bool
    #: 该版本的键名。**单独拎出来**是为了让「V2 改了什么」这件事
    #: 可以被测试断言，而不是埋在拼 dict 的代码里。
    field_names: dict[str, str]
    build_permission_config: Callable[[], dict[str, Any]]
    parse_request: Callable[[Any], ToolPermission | None]
    build_reply: Callable[[str], dict[str, str]]
    #: 提问链路。与上面两个**同样**把版本差异关在这里：
    #: ``None`` 表示「不是一条我能回应的提问」，绝不返回半截形状。
    parse_question: Callable[[Any], ToolQuestion | None]
    #: 答复载荷。**必须能表达嵌套数组** —— V1 实测只接受 ``[["..."]]``，
    #: 扁平字符串会被拒（400）。
    build_question_reply: Callable[[Sequence[Sequence[str]]], dict[str, Any]]


# ── opencode V1（本仓实测并据以接线）─────────────────────────────────────

#: V1 的 permission 键名。**键序有意义**，见 :func:`_v1_permission_config`。
V1_FIELD_NAMES = {
    "container": "permission",
    "edit": "edit",
    "shell": "bash",
    "web_fetch": "webfetch",
    "web_search": "websearch",
    "subagent": "task",
    "external_dir": "external_directory",
}


def _v1_permission_config() -> dict[str, Any]:
    """V1 委派用的 permission 配置。

    ⚠️ **键序有意义**：V1 是 ``last matching rule wins``，所以通配 ``*``
    必须**放最前**，具体规则放后面。deny 放最后才不会被 ``*: allow`` 覆盖。
    （Python 的 ``dict`` 保序，``json.dumps`` 也保序，所以这个形状能原样落盘。）

    逐条的理由：

    - ``*: allow`` —— 底子。``read``/``grep``/``glob`` 是干活必需的，
      全设 ask 会让 agent 一直问、卡片刷屏，**问到最后就是无脑点**。
    - ``edit: ask`` —— 改文件。这是执行期闸门**真正要拦的东西**。
    - ``bash: ask`` —— 跑命令。与 edit 同级；官方 V1 文档明说 shell
      带宿主机的文件/进程/网络权限。
    - ``webfetch`` / ``websearch: ask`` —— 出网。委派的需求通常不需要。
    - ``task: deny`` —— 不让 agent 拉子代理。与 FreeAgent 的分层一致
      （「派什么由代码决定，不由模型决定」）。
    - ``external_directory: deny`` —— **永不越界**。这是 11.8 第 1 道闸门在
      执行器侧的落点；FreeAgent 侧只拒相对路径，两侧都拒才算闸门。

    为什么不给 ``bash`` 配窄白名单：官方文档明说目录推断是 best effort，
    **不要试图用规则枚举所有危险命令**。所以这里走「全 ask + 人判断」，
    靠闸门而不是靠正则。
    """
    f = V1_FIELD_NAMES
    return {
        "$schema": "https://opencode.ai/config.json",
        f["container"]: {
            "*": "allow",
            f["edit"]: "ask",
            f["shell"]: "ask",
            f["web_fetch"]: "ask",
            f["web_search"]: "ask",
            f["subagent"]: "deny",
            f["external_dir"]: "deny",
        },
    }


def _v1_parse_request(properties: Any) -> ToolPermission | None:
    """从 ``permission.asked`` 事件的 properties 里取出请求（V1 实测载荷）。

    **返回 ``None`` 表示这不是一条可回应的请求**（缺 id 或缺动作名），
    绝不用空字符串凑一个 —— 那会让上层把「无法回应」当成「已拒绝」，
    两种错误的处理方式完全不同。
    """
    if not isinstance(properties, dict):
        return None
    rid = properties.get("id")
    perm = properties.get("permission")
    if not isinstance(rid, str) or not rid:
        return None
    if not isinstance(perm, str) or not perm:
        return None
    meta = properties.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    raw_paths = properties.get("patterns")
    paths = tuple(p for p in raw_paths if isinstance(p, str)) \
        if isinstance(raw_paths, list) else ()
    filepath = meta.get("filepath")
    if isinstance(filepath, str) and filepath and filepath not in paths:
        paths = paths + (filepath,)
    diff = meta.get("diff")
    raw_always = properties.get("always")
    always = tuple(a for a in raw_always if isinstance(a, str)) \
        if isinstance(raw_always, list) else ()
    return ToolPermission(
        request_id=rid,
        permission=perm,
        paths=paths,
        diff=diff if isinstance(diff, str) and diff else None,
        suggested_always=always,
    )


def _v1_build_reply(decision: str) -> dict[str, str]:
    """把本地结论翻成 V1 的 ``reply`` 载荷。

    ⚠️ **必须带 ``reply`` 键**（实测：裸字符串 → ``400 Expected object``；
    ``{"action": ...}`` → ``400 Missing key ["reply"]``）。

    映射：本地 ``allow`` → ``once``。**刻意不映射到 ``always``** —— V1 的
    ``always`` 是会话级授权，而本项目明确不做永久授权（设计文档 11.9.7）；
    而且每次都问一次本来就是这个闸门的意义。
    """
    if decision not in ("allow", "deny"):
        raise ValueError(f"未知结论：{decision!r}")
    return {"reply": "once" if decision == "allow" else "reject"}


def _v1_parse_question(properties: Any) -> ToolQuestion | None:
    """从 ``question.asked`` 事件的 properties 里取出提问（V1 实测载荷）。

    **返回 ``None`` 表示这不是一条能回应的提问。**

    与 :func:`_v1_parse_request` 的一处**刻意不同**：那里的 ``patterns``
    遇到非字符串会被丢掉（少列一个路径只是少显示点东西），而这里的
    ``questions`` 遇到非字符串**整条放弃**。

    因为丢掉一题的后果不是「少显示点东西」，而是**人答了 N-1 题、
    agent 拿着残缺的答案继续干活，且哪儿都不报错**。宁可整条不认 ——
    那样至少是一次能看见的失败，而不是一次安静的错答案。
    """
    if not isinstance(properties, dict):
        return None
    rid = properties.get("id")
    if not isinstance(rid, str) or not rid:
        return None
    raw_q = properties.get("questions")
    if not isinstance(raw_q, list) or not raw_q:
        return None
    if not all(isinstance(q, str) and q.strip() for q in raw_q):
        # 有非字符串元素 = 形状与我们知道的不一样。不猜。
        return None
    raw_o = properties.get("options")
    if raw_o is None:
        options: tuple[str, ...] = ()
    elif isinstance(raw_o, list) and all(
        isinstance(o, str) for o in raw_o
    ):
        options = tuple(o for o in raw_o if o.strip())
    else:
        # 同理：选项的形状不认识就不认这条，别渲染出一堆没意义的空行。
        return None
    return ToolQuestion(
        request_id=rid,
        questions=tuple(q.strip() for q in raw_q),
        options=options,
    )


def _v1_build_question_reply(answers: Sequence[Sequence[str]]) -> dict[str, Any]:
    """把逐轮积累的答复翻成 V1 的提问答复载荷。

    ⚠️ **必须是嵌套数组**（实测：扁平字符串 → ``400``）：

        {"answers": [["第一题答案"], ["第二题答案"]]}

    所以本函数**不接受** ``["答案"]`` 那种形状 —— 与其让一个扁平列表悄悄
    变成 ``[["答案"]]``（于是「两题只答一题」被当成答完了），不如让它
    在这里就炸掉。

    空答复也拒：没人答就**不该**造一个答复发回去。超时的处置在调用方
    （记日志、不给答案），不在这里伪造一个空数组。
    """
    rows = [list(a) for a in answers]
    if not rows:
        raise ValueError("提问答复不能是空的：没人答就不该发答复")
    for i, row in enumerate(rows):
        if not row:
            raise ValueError(f"第 {i + 1} 题的答复是空的")
        for cell in row:
            if not isinstance(cell, str) or not cell.strip():
                raise ValueError(f"第 {i + 1} 题的答复里有空白")
    return {"answers": rows}


_V1 = ExecutorAdapter(
    executor="opencode",
    major=1,
    verified=True,
    field_names=V1_FIELD_NAMES,
    build_permission_config=_v1_permission_config,
    parse_request=_v1_parse_request,
    build_reply=_v1_build_reply,
    parse_question=_v1_parse_question,
    build_question_reply=_v1_build_question_reply,
)


# ── opencode V2（**未验证**）─────────────────────────────────────────────

#: V2 的改名。**只记我们确实知道的**。
#:
#: 来自官方 V2 文档的字段改名（README 已记）。刻意**不含** ``permissions``
#: 数组的元素形状 —— 那个我们不知道，而猜一个「看起来很像对的」形状比
#: 拒绝更危险：它会静默地不生效。
V2_FIELD_RENAMES = {
    "container": "permissions",   # 对象 → 数组
    "shell": "shell",             # bash → shell
    "subagent": "subagent",       # task → subagent
    # 未确认是否也改：edit / webfetch / websearch / external_directory
}

#: V2 适配器**故意**不实现这三个 —— 每一个都会抛。
#:
#: 写下来的价值有两个：把「我们不知道什么」变成代码里可执行的事实，
#: 以及让 :func:`adapter_for` 能对 V2 给出**具体**的拒绝理由，
#: 而不是笼统的「版本不对」。
def _v2_unverified(what: str) -> Callable[..., Any]:
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise UnverifiedExecutorError(
            f"opencode V2 的 {what} 尚未验证：V2 把 permission 从 "
            f"{V1_FIELD_NAMES['container']!r} 对象改成 "
            f"{V2_FIELD_RENAMES['container']!r} 数组，但**数组元素的形状**"
            "官方文档未给出可据以接线的定义。需要先重读官方文档、对着一个"
            "真实 V2 实例实测，再改这一处。"
        )

    return _raise


_V2 = ExecutorAdapter(
    executor="opencode",
    major=2,
    verified=False,
    field_names=V2_FIELD_RENAMES,
    build_permission_config=_v2_unverified("permission 配置形状"),
    parse_request=_v2_unverified("permission.asked 事件形状"),
    build_reply=_v2_unverified("reply 载荷形状"),
    # 提问链路一样：V2 有没有换路径或换了字段名，都未验证。
    # 猜一个「看起来很像对」的形状比拒绝更危险：
    # 错的形状会让 agent 拿到一个自身的回答，而不是人的。
    parse_question=_v2_unverified("question.asked 事件形状"),
    build_question_reply=_v2_unverified("提问答复载荷形状"),
)


# ── 注册表 ──────────────────────────────────────────────────────────────

_ADAPTERS: tuple[ExecutorAdapter, ...] = (_V1, _V2)


def all_adapters(executor: str = "opencode") -> tuple[ExecutorAdapter, ...]:
    """某个执行器的全部适配器，**含未验证的**，按主版本升序。

    给「哪些版本我知道、哪些能用」这类查询用。刻意返回未验证的那些 ——
    藏起来的话，版本闸门只能说「不认识」，给不出「认识但没验过」这种
    更有用的拒绝理由。
    """
    return tuple(sorted(
        (a for a in _ADAPTERS if a.executor == executor),
        key=lambda a: a.major,
    ))


def known_majors(executor: str = "opencode") -> tuple[int, ...]:
    """本仓知道的（不是**支持的**）主版本，升序。

    「知道」与「支持」是两个概念：V2 我们知道它存在、也知道它改了什么，
    但正因为只知道一半，它**不能**被派发。
    """
    return tuple(a.major for a in all_adapters(executor))


def dispatchable_majors(executor: str = "opencode") -> tuple[int, ...]:
    """可派发的（``verified=True``）主版本，升序。版本闸门只放行这些。"""
    return tuple(a.major for a in all_adapters(executor) if a.verified)


def adapter_for(major: int | None, executor: str = "opencode") -> ExecutorAdapter | None:
    """按主版本取适配器。**取不到返回 ``None``，不回落。**

    刻意**不**在取不到时回落到 V1 —— 那正是当前这个 bug 的形状：
    字段名对不上，配置被静默忽略，闸门失效，而外面看起来一切正常。
    """
    if major is None:
        return None
    for a in _ADAPTERS:
        if a.executor == executor and a.major == major:
            return a
    return None
