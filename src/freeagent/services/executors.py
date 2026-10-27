"""执行器接线：把后端特有的知识关进一个带版本号的接缝。

## 为什么要这一层

委派要启动一个外部执行器，而**那个东西的协议会变**。

注册表里现有两个执行器：**opencode**（V1 与 V2 都已实测接线）与
**hermes**（/ask 那条线已接线，但不能委派）。后者**只能问、不能派**，
而且它与 opencode **不是同一种接法**（HTTP+SSE 走常驻 gateway，不是
起隔离进程）—— 见下面 hermes 那节。

V1 → V2 时 permission 模型的字段名全变了（``permission`` 对象 / ``bash`` /
``task`` → ``permissions`` 数组 / ``shell`` / ``subagent``），而**旧字段被静默
忽略** —— 不报错、不警告，闸门随之失效，而失效方向恰好是最危险的那侧
（默认回到 allow）。

在有这层之前，那些字段名散在 :mod:`freeagent.services.opencode_server` 的三个
函数里（权限配置、事件解析、答复载荷）。改一次协议要动三个地方，而漏掉一个
的后果是**闸门静默失效**而不是「跑不起来」。

有这层之后：协议变更收敛到**一个文件**，且改错了会**响**。

## 两条不做

1. **不猜未知的字段。** 一个版本要是还没对着真机验过，它的协议函数就**全部
   拒绝调用**，而不是照着一个「看起来很像对的」形状写下去 ——
   见 :mod:`docs.postmortem.0001`，写反的东西看起来总很像对的。
2. **不放行未验证的版本。** 版本闸门只放 ``verified=True`` 的适配器。
   （hermes 已 ``verified`` 但 ``dispatchable=False``：它只验过 /ask 那条线，
   不参与委派闸门 —— 见 :class:`ExecutorAdapter` 的字段说明。）

## 版本闸门在哪

:func:`freeagent.delegate.version_mismatch_reason` 拿探测到的主版本来这里问。
**探测**而不是配置 —— 能力声明一旦手写就会陈旧，而陈旧的方向是危险的那侧
（同上，V1 的教训）。

## V2 是**实测之后**才转正的（2026-10-15）

V2 的六个协议函数从 ``_v2_unverified`` 换成实测实现，同时补上 :class:`ExecutorApi`
（路径与事件翻译）。转正的依据是**真机 2.0.29 上逐项验过**：

- ``permission.asked`` 的 ``action`` 回的是 ``shell``，答复走
  ``POST /api/session/{sid}/permission/{pid}/reply`` ``{"decision": "once"}``；
- 提问换成了 Form（``form.created`` + ``POST …/form/{fid}/reply`` ``{"answer": {...}}``）；
- 事件载荷从 ``properties`` 挪到了 ``data``，事件名也换了（见
  :data:`_V2_EVENT_ALIAS`）。

**没验过的东西一个都没编**：V2 的动作名里只有 ``shell``/``*`` 是实测过的，
其余落到兜底 ``*: ask``（fail-closed）；``/api/provider`` 没有 models 字段，
所以模型列表由 ``/api/model`` 现推（见 :func:`_v2_normalize_models`）。
"""

from __future__ import annotations

import dataclasses
import urllib.parse
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "HttpCall",
    "ExecutorApi",
    "ToolPermission",
    "ToolQuestion",
    "ExecutorAdapter",
    "UnverifiedExecutorError",
    "adapter_for",
    "all_adapters",
    "known_majors",
    "dispatchable_majors",
    "known_executors",
    "V1_FIELD_NAMES",
    "V2_FIELD_RENAMES",
    "HERMES_FIELD_NAMES",
    "HERMES_API",
]


#: 一次 HTTP 调用的三要素。
HttpCall = tuple[str, str, Any]  # (method, path, body)


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
    #: 每个问题各一组选项（V2 实测按 field 分组）。空 = 没有分组信息。
    option_groups: tuple[tuple[str, ...], ...] = ()

    @property
    def summary(self) -> str:
        """一行摘要，给日志用。"""
        n = len(self.questions)
        pool = len(self.options) or sum(len(g) for g in self.option_groups)
        return f"{n} 个问题" + (f"，{pool} 个可选项" if pool else "")


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutorApi:
    """一个执行器版本的 HTTP 面（路径与事件翻译），与载荷字段分开。

    与 :attr:`ExecutorAdapter.parse_request` 那组并列：那组管「载荷里的
    字段叫什么」，这组管「打到哪个路径、事件名怎么翻译」。两者一起才构成
    一次可用的接线 —— 只对一半的表现是「HTTP 200 但什么都没发生」。

    ## 为什么 ``parse_event`` 是**函数**而不是一张别名表

    V2 不只改了事件名，还把载荷挪了位置（``properties`` → ``data``）、
    并给提问换了个载体（``question.asked`` → ``form.created``，载荷多包
    一层 ``form``）。别名表只解决名字，解决不了形状 —— 而形状认错的症状
    是**静默的**：事件流照常到达、``permission_from_event`` 每一条都返回
    ``None``、授权卡永远不出现，然后会话一直跑到时长上限。那是本项目最恨的
    一类失败。所以翻译与改写必须是同一个函数里的两步。
    """

    #: 就绪探针的路径。``_wait_ready`` 轮询它。
    ready_path: str
    #: 建会话的路径。
    session_create_path: str
    #: 从建会话的响应里取出 session id（V1 裸字符串，V2 在 ``data.id``）。
    session_id_of: Callable[[Any], str | None]
    #: ``prompt_async`` 要发的一串调用（V1 一条，V2 可能多条）。
    prompt_calls: Callable[[str, str, str, str, str], list[HttpCall]]
    #: 事件订阅路径。
    events_path: str
    #: 授权答复。``(session_id, request_id, decision)`` → ``(method, path, body)``。
    permission_reply: Callable[[str, str, str], HttpCall]
    #: 提问答复。``(session_id, form_id, answers)`` → ``(method, path, body)``。
    question_reply: Callable[
        [str, str, str, Sequence[Sequence[str]]], HttpCall
    ]
    #: 中止会话的路径。
    abort_path: Callable[[str], str]
    #: 「限目录」查询串。``directory`` → ``?directory=…``（V1）或
    #: ``?location[directory]=…``（V2）—— 两个版本的**查询形状不同**，
    #: 不是只换了路径。见 :func:`_v2_directory_query`。
    directory_query: Callable[[str], str]
    #: 挂起授权请求列表的路径（V1 ``/permission``，V2 ``/api/permission/request``）。
    permission_pending_path: str
    #: 挂起提问列表的路径（V1 ``/question``，V2 ``/api/form``）。
    question_pending_path: str
    #: 项目列表要取的路径（可能不止一个，V2 要拼两条）。
    projects_paths: tuple[str, ...]
    #: ``{路径: 原始响应}`` → 归一化后的列表。
    normalize_projects: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    agents_paths: tuple[str, ...]
    normalize_agents: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    models_paths: tuple[str, ...]
    normalize_models: Callable[[Mapping[str, Any]], dict[str, Any]]
    #: 一条 SSE 帧 → ``(内部事件名, 载荷)``；不认得返回 ``None``。
    parse_event: Callable[[Any], tuple[str, Any] | None]
    #: 答复路径是否必须带 session id（V2 是，V1 不是）。
    reply_needs_session: bool
    #: 目录/列表**尚未加载完**时的重试窗口（秒）。V1 没有这个问题。
    catalog_lag_seconds: float


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutorAdapter:
    """一个执行器 + 一个主版本的接线。

    ``verified`` 是这里最重要的字段：它为 ``False`` 时 :func:`adapter_for`
    仍然返回适配器（好让调用方能给出**具体**的拒绝理由），但版本闸门不放行。
    """

    executor: str
    major: int
    verified: bool
    #: 能不能走 11.8 **委派**。与 :attr:`verified` **是两个概念**：
    #:
    #: - ``verified`` —— 对着真实实例验过**接线**（11.10.2）。一个执行器
    #:   可能只验过「/ask（只读问答）」这一条线，就**不等于**它能委派。
    #: - ``dispatchable`` —— 能走 11.8 那条「建会话 → 执行期逐次授权 → 产出
    #:   artifact」的完整委派链路。只有这条线验过的执行器才有资格。
    #:
    #: 曾用的例子是 hermes：``verified`` 转正（/ask 线对着真机验过）后，
    #: ``dispatchable`` **仍为 False** —— V1.81 真机确认它的审批闸是「按模式
    #: 标记」而非「每动作确认」：API 面与飞书面都会直接执行未标记的 file/
    #: terminal 操作而不弹审批（manual + 重启也拦不住），**做不了执行期逐次
    #: 授权**。**V1.83 更因它连「只读改不了」都保证不了而把 ``disabled`` 置
    #: True 停用**（见下方 ``disabled`` 字段说明）；这里保留这段历史是为了说清
    #: 为什么 ``dispatchable`` 必须与 ``verified`` 分开。
    #:
    #: 为什么必须拆开而不是让 ``verified`` 兼管「可委派」：探针（``/agents``）
    #: 拿 ``dispatchable`` 说「能不能派」，拿 ``verified`` 说「验没验过」——
    #: 合在一起会让 hermes 验过 /ask 后就被说成「可以派发」，而那与
    #: 11.10.8 的边界直接冲突（诱导用户去委派一个做不了委派的执行器）。
    #:
    #: **必须显式填写、无默认值**：加一个新执行器时「能不能委派」是一个
    #: 需要人来定的决定，不该悄悄落到 False（那会「看起来能派其实不能派」），
    #: 也不该悄悄落到 True（那会让未验证委派的执行器进了闸门）。
    dispatchable: bool
    #: 该版本的键名。**单独拎出来**是为了让「V2 改了什么」这件事
    #: 可以被测试断言，而不是埋在拼 dict 的代码里。
    field_names: dict[str, str]
    build_permission_config: Callable[[], dict[str, Any]]
    #: ``/ask`` 用的**只读**权限配置（11.10.3 的 ``read`` 桶）。
    #:
    #: 刻意与 :attr:`build_permission_config` 分开：委派要「改了文件问人」，
    #: 而 ``/ask`` 必须「**改不了**」。合成一份的话，其中一条路必然拿到
    #: 错误的权限 —— 而错的那一侧是「``/ask`` 变成了无人监督的代码执行」。
    build_read_only_config: Callable[[], dict[str, Any]]
    parse_request: Callable[[Any], ToolPermission | None]
    build_reply: Callable[[str], dict[str, str]]
    #: 提问链路。与上面两个**同样**把版本差异关在这里：
    #: ``None`` 表示「不是一条我能回应的提问」，绝不返回半截形状。
    parse_question: Callable[[Any], ToolQuestion | None]
    #: 答复载荷。**必须能表达嵌套数组** —— V1 实测只接受 ``[["..."]]``，
    #: 扁平字符串会被拒（400）。
    build_question_reply: Callable[[Sequence[Sequence[str]]], dict[str, Any]]
    #: HTTP 面。**``None`` = 这个执行器不是 HTTP+SSE 那一种接法**（hermes）。
    api: ExecutorApi | None = None
    #: 停用（V1.83）：登记过、但**不作为可用执行器**。默认 ``False``
    #: （安全方向是「不静默停用」—— 新适配器默认仍可用，只有人决定不用
    #: 它时才显式置 True）。
    #:
    #: 与 :attr:`verified` / :attr:`dispatchable` 是两个方向：停用不是因为
    #: 「没验过」或「不能委派」，而是「**决定不用它了，代码保留作接缝**」。
    #: 现行的例子是 hermes —— V1.82 真机证伪它的审批闸（「按模式标记」而非
    #: 「每动作确认」，拦不住未标记的 file/terminal，API 面与飞书面都直接
    #: 执行不弹审批），FreeAgent 不再把它当执行器；适配器与 helper 全部保留，
    #: 将来若 hermes 修好审批闸，把 ``disabled`` 改回 ``False`` 并恢复
    #: ``/ask hermes`` 分派即可重新启用。
    disabled: bool = False


# ── opencode V1（本仓实测并据以接线）─────────────────────────────────────

#: V1 的 permission 键名。**键序有意义**，见 :func:`_v1_permission_config`。
#:
#: 为什么把**每个工具**都登记进来，而不只是「会改文件的那几个」：
#: permission 是 **last matching rule wins** 的通配匹配，而**没登记 = 落到兜底
#: 规则**。所以「漏登记一个工具」不是「这个工具没权限」，而是「这个工具拿到
#: 兜底规则给的动作」—— 兜底是 ``ask`` 还是 ``allow``，决定了它是安全还是漏闸门。
#:
#: 工具名取自 V1 源码 ``src/tool/*.ts`` 的 ``Tool.define("<id>", …)``，那是
#: 权限系统认的**全部**工具（实测清单，2026-10-14）。两个必须记的坑：
#:
#: - ``ls.ts`` 的 id 是 ``list``，``plan.ts`` 的是 ``plan_exit``（文件名 ≠ id）。
#: - **写文件是三个独立工具**：``edit`` / ``write`` / ``multiedit``，加上
#:   ``apply_patch``。只登记 ``edit`` 等于没登记另外三个（见 :func:`_v1_permission_config`）。
V1_FIELD_NAMES = {
    "container": "permission",
    "edit": "edit",
    "shell": "bash",
    "web_fetch": "webfetch",
    "web_search": "websearch",
    "subagent": "task",
    "external_dir": "external_directory",
    # 写类：另外三个写文件的工具（V1 实测是独立工具，不是 edit 的别名）
    "write": "write",
    "multiedit": "multiedit",
    "apply_patch": "apply_patch",
    # 读类：显式放行，靠它们把「干活必需的噪声」挡在闸门外面
    "read": "read",
    "list": "list",
    "glob": "glob",
    "grep": "grep",
    "codesearch": "codesearch",
    "lsp": "lsp",
    "todo": "todo",
    "question": "question",
    "plan_exit": "plan_exit",
    # 技能：会照着技能说明动手，**后果同写类**，所以按写类问
    "skill": "skill",
}


def _v1_permission_config() -> dict[str, Any]:
    """V1 委派用的 permission 配置。**失败关闭**：未登记的一律 ``ask``。

    ⚠️ **键序有意义**：V1 是 ``last matching rule wins``（``evaluate.ts`` 用
    ``rules.findLast``），所以通配 ``*`` 必须**放最前**，具体规则放后面 ——
    反过来的话 ``*: ask`` 会把每一条具体规则都盖掉。
    （Python 的 ``dict`` 保序，``json.dumps`` 也保序，所以这个形状能原样落盘。）

    ## 为什么兜底从 ``allow`` 改成 ``ask``（2026-10-14 实测改的）

    原来是 ``*: allow`` + 只登记 6 个键。**实测证明那等于半个闸门**：

    1. ``*`` 匹配一切，所以**任何没登记的工具都拿到 allow**。本机 opencode
       由插件注入了 5 个 MCP 服务器（codegraph / context7 / grep_app / lsp /
       websearch），实测在委派里**静默执行完、零授权事件**。
    2. **写文件在 V1 是四个工具**：``edit`` / ``write`` / ``multiedit`` /
       ``apply_patch``。原来只登记了 ``edit`` —— 另外三个落到 ``*: allow``，
       agent 可以不打招呼就新建/覆盖文件。
    3. 最狠的一条：``tool/registry.ts`` 按模型能力**二选一** ——
       模型 id 含 ``gpt-``（非 oss / 非 gpt-4）时给 ``apply_patch``，
       **并且把 ``edit`` 与 ``write`` 整个过滤掉**。所以拿 GPT-5 跑委派时，
       ``edit: ask`` 是**一条永远用不上的死配置**，模型改文件走的是没登记的
       ``apply_patch`` ⇒ 静默放行。第三道闸门（执行期逐次授权）被整体绕过。

    opencode 自己的兜底方向是对的：``permission/evaluate.ts`` 未命中任何规则
    时返回 ``ask``（不是 ``allow``）。是**我们的** ``*: allow`` 把这个方向
    掰反了。复盘 0001 早就写过「默认值要往拒绝的方向倒」，这里就是那句话
    漏掉的一处。

    ## 逐条

    - ``*: ask`` —— **兜底**。未登记的（含全部 MCP 工具、将来 opencode 新增的
      工具、以及任何第三方插件注入的工具）都要人点头。这条是本函数的核心。
    - 读类 ``allow`` —— ``read`` / ``list`` / ``glob`` / ``grep`` /
      ``codesearch`` / ``lsp`` / ``todo`` / ``question`` / ``plan_exit``。
      干活必需，全设 ask 会让 agent 一直问、卡片刷屏，**问到最后就是无脑点**
      —— 那才是真的不安全（复盘 0001 第 2 条）。
    - 写类 ``ask`` —— ``edit`` / ``write`` / ``multiedit`` / ``apply_patch``。
      这才是执行期闸门**真正要拦的东西**，四个都必须登记（理由见上）。
    - ``bash: ask`` —— 跑命令。与写文件同级；官方 V1 文档明说 shell
      带宿主机的文件/进程/网络权限。
    - ``webfetch`` / ``websearch: ask`` —— 出网。委派的需求通常不需要。
    - ``skill: ask`` —— 技能会照着说明动手，后果与写类同级。
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
            # 兜底 —— 必须第一项（last matching rule wins，见函数 docstring）
            "*": "ask",
            # 读类：干活必需，闸门只该拦「有后果的动作」
            f["read"]: "allow",
            f["list"]: "allow",
            f["glob"]: "allow",
            f["grep"]: "allow",
            f["codesearch"]: "allow",
            f["lsp"]: "allow",
            f["todo"]: "allow",
            f["question"]: "allow",
            f["plan_exit"]: "allow",
            # 写类：执行期闸门真正要拦的东西
            f["edit"]: "ask",
            f["write"]: "ask",
            f["multiedit"]: "ask",
            f["apply_patch"]: "ask",
            f["shell"]: "ask",
            f["web_fetch"]: "ask",
            f["web_search"]: "ask",
            f["skill"]: "ask",
            # 永不
            f["subagent"]: "deny",
            f["external_dir"]: "deny",
        },
    }


def _v1_read_only_config() -> dict[str, Any]:
    """V1 的**只读**权限配置 —— ``/ask`` 用的那一份（设计文档 11.10.3 的 ``read`` 桶）。

    ## 为什么必须单独一份，而不能复用 :func:`_v1_permission_config`

    11.10.6 说 ``/ask`` 「不落库、不问任何审批」，理由是「它不改任何东西，
    所以没有『有副作用』可拦」。**那句话是个假设**：真正的原因是**配置必须
    自己保证「不改任何东西」**，不能指望执行器自觉。

    ## 兜底是 ``deny``，不是 ``allow``（2026-10-14 实测改的）

    原来这份是 ``*: allow`` + 只 deny 六个键。**那等于没实现「只读」**：
    ``write`` / ``apply_patch`` / ``multiedit`` 三个写文件的工具没被登记，
    于是从 ``*: allow`` 拿到 allow —— ``/ask`` 能改文件、能打补丁，
    而它的 docstring 恰恰写着「这就是『不改任何东西』的实现」。
    ``/ask`` 没有审批界面，``ask`` 等于挂死，所以这里只能**显式 deny**，
    靠兜底 ``*: deny`` 兜住所有没登记的（含全部 MCP 工具与插件注入的工具）。

    顺带纠正一句旧注释：opencode V1 **未命中规则时默认是 ``ask``**
    （``permission/evaluate.ts`` 的 ``match ?? { action: "ask" }``），
    不是 ``allow``。11.8.1 的版本陷阱表据此。

    ## 逐条

    - ``*: deny`` —— 兜底。**未登记的一律不许**，这才是「只读」的实现。
    - 读类 ``allow`` —— 「问一句」必需的：``read`` / ``list`` / ``glob`` /
      ``grep`` / ``codesearch`` / ``lsp`` / ``todo`` / ``question`` /
      ``plan_exit``。
    - 写类不写规则 —— 落到兜底 ``deny``（``edit`` / ``write`` / ``multiedit`` /
      ``apply_patch`` / ``skill``）。
    - ``webfetch`` / ``websearch``: ``deny`` —— 出网等于把问题送到别处；
      11.10.3 的 ``read`` 桶只含「只读地看这个项目的文件」
    - ``task``: ``deny`` —— 不让执行器拉子代理。11.10.4 明确「不因为
      『预算还够』而放行」
    - ``external_directory``: ``deny`` —— 永不越界（与委派同一道）

    ⚠️ **键序仍然有意义**：V1 是 ``last matching rule wins``，
    ``*`` 必须**放最前**，放最后会把每一条具体规则都盖成 deny。
    """
    f = V1_FIELD_NAMES
    return {
        "$schema": "https://opencode.ai/config.json",
        f["container"]: {
            # 兜底 —— 必须第一项（last matching rule wins）
            "*": "deny",
            # 只读地看这个项目：这一组之外全都不许
            f["read"]: "allow",
            f["list"]: "allow",
            f["glob"]: "allow",
            f["grep"]: "allow",
            f["codesearch"]: "allow",
            f["lsp"]: "allow",
            f["todo"]: "allow",
            f["question"]: "allow",
            f["plan_exit"]: "allow",
            # 显式 deny（写类交给兜底，但列出来是为了「读代码就能看出边界」）
            f["edit"]: "deny",
            f["write"]: "deny",
            f["multiedit"]: "deny",
            f["apply_patch"]: "deny",
            f["skill"]: "deny",
            f["shell"]: "deny",
            f["web_fetch"]: "deny",
            f["web_search"]: "deny",
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
    rows = _rows_or_raise(answers)
    return {"answers": rows}


def _rows_or_raise(answers: Sequence[Sequence[str]]) -> list[list[str]]:
    """把逐轮答复摊平成行。V1/V2 共用，错误文案也共用。"""
    rows: list[list[str]] = []
    for a in answers:
        # **先挡字符串，再 list()。**
        #
        # 踩过的坑（红测抓到的）：原先写 ``rows = [list(a) for a in answers]``，
        # 而 ``list("答案") == ['答', '案']`` —— 扁平列表于是被**按字符拆开**，
        # 一个两字答复变成「第一题答了『答』、第二题答了『案』」。
        # 后果是两种静默错误：单题被记成两题（看起来答完了，其实没有），
        # 或者把一个答复塞进错误的问题槽位。
        #
        # 放在 ``list()`` **之后**判就晚了：那会儿字符串早已变成字符列表，
        # 每一个元素都是合法 str，再也认不出它本来是个字符串。
        #
        # 线上路径不受影响：:meth:`ApprovalStore.answers_of` 返回的已经是
        # ``[[...], [...]]``，顶层元素是 list 而非 str。
        if isinstance(a, str):
            raise ValueError(
                "提问答复必须是嵌套数组（形如 [[第一题], [第二题]]），"
                f"却收到扁平字符串 {a!r}：扁平形状会被按字符拆开，"
                "把一问变成多答案。"
            )
        rows.append(list(a))
    if not rows:
        raise ValueError("提问答复不能是空的：没人答就不该发答复")
    for i, row in enumerate(rows):
        if not row:
            raise ValueError(f"第 {i + 1} 题的答复是空的")
        for cell in row:
            if not isinstance(cell, str) or not cell.strip():
                raise ValueError(f"第 {i + 1} 题的答复里有空白")
    return rows


def _v1_event_type(obj: Any) -> str | None:
    """V1 帧里取事件名。**类型不对就返回 ``None``**，由调用方丢掉。

    与 V2 共用同一个判断：``type`` 是**唯一**可靠的事件名来源
    （V2 的帧里 ``event`` 字段压根不存在，实测）。
    """
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    return kind if isinstance(kind, str) and kind else None


def _v1_parse_event(obj: Any) -> tuple[str, Any] | None:
    """V1 的一帧：``data: {"type": ..., "properties": {...}}``。"""
    kind = _v1_event_type(obj)
    if kind is None:
        return None
    return kind, obj.get("properties")


def _v1_prompt_payload(brief: str, model: str, agent: str, variant: str) -> dict[str, Any]:
    """V1 的 ``prompt_async`` 载荷。

    ## variant 在**顶层**，不在 model 成员里（踩过的坑）

    原来写成 ``payload["model"]["variant"]``，实测 opencode 完全不看 ——
    症状是「选了 high 却按模型默认档跑」，而没有任何报错。V2 才把 variant
    收进 ``Model.Ref``，那是**另一个版本的另一件事**。
    """
    payload: dict[str, Any] = {"parts": [{"type": "text", "text": brief}]}
    if model:
        provider, _, name = model.partition("/")
        payload["model"] = {"providerID": provider, "modelID": name or provider}
    if variant:
        payload["variant"] = variant
    if agent:
        payload["agent"] = agent
    return payload


def _v1_prompt_calls(
    session_id: str, brief: str, model: str, agent: str, variant: str
) -> list[HttpCall]:
    return [(
        "POST",
        f"/session/{session_id}/prompt_async",
        _v1_prompt_payload(brief, model, agent, variant),
    )]


def _v1_permission_reply(_sid: str, request_id: str, decision: str) -> HttpCall:
    """V1 的授权答复：路径不带你（会话 id），载荷是 ``{"reply": …}``。

    ⚠️ **``request_id`` 必须转义**。id 由 opencode 发来，正常情况下只是
    ``per_…`` 这样的安全串，但它**不是我们生成的** —— 一个含 ``/`` 或 ``..``
    的 id 会把路径拐到别处（``/permission/{id}/reply`` 变成打到任意端点）。
    转义之后最坏是一个 404，不是一次打歪的 POST。这段转义原来住在
    :mod:`opencode_server`，收进这里是为了让「怎么拼路径」只有一个来源。
    """
    rid = urllib.parse.quote(request_id, safe="")
    return ("POST", f"/permission/{rid}/reply", _v1_build_reply(decision))


def _v1_question_reply(
    _sid: str, request_id: str, answers: Sequence[Sequence[str]]
) -> HttpCall:
    """V1 的提问答复：路径不带你，载荷是 ``{"answers": …}``。转义理由同授权。"""
    rid = urllib.parse.quote(request_id, safe="")
    return (
        "POST",
        f"/question/{rid}/reply",
        _v1_build_question_reply(answers),
    )


def _v1_directory_query(directory: str) -> str:
    """V1 的「限目录」查询串：``?directory=<quoted>``（实测形状）。"""
    return f"?directory={urllib.parse.quote(directory, safe='')}"


def _v1_abort_path(session_id: str) -> str:
    return f"/session/{session_id}/abort"


def _v1_normalize_projects(responses: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = next(iter(responses.values()))
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def _v1_normalize_agents(responses: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = next(iter(responses.values()))
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def _v1_normalize_models(responses: Mapping[str, Any]) -> dict[str, Any]:
    raw = next(iter(responses.values()))
    if isinstance(raw, dict) and isinstance(raw.get("all"), list):
        return raw
    raise UnverifiedExecutorError("V1 的 /provider 返回了预期外的形状")


def _v1_session_id_of(raw: Any) -> str | None:
    """V1 ``POST /session`` → 会话 id。

    实测形状是 ``{"id": "ses_…"}``（本仓据以接线的实现即读 ``body["id"]``，
    见 git 历史里的 :meth:`OpenCodeServer.create_session`）。也**容忍裸字符串**
    ——V1 的历史实现两种形状都出现过。认错的症状是**静默拿到空会话**：之后
    每一次答复都发不出去（而报错在很远的地方才浮现），所以宁可两种都收。
    """
    if isinstance(raw, str):
        cand: Any = raw
    elif isinstance(raw, dict):
        cand = raw.get("id")
    else:
        cand = None
    return cand if isinstance(cand, str) and cand else None


def _v2_session_id_of(raw: Any) -> str | None:
    """V2 ``POST /api/session`` → ``{"data": {"id": "ses_…"}}``（真机实测）。

    与 V1 的关键差别：V2 把会话包在 ``data`` 信封里（列表端点也一样，
    见 :func:`_unwrap_list`）。读错这一层会拿到 ``None``，于是
    :meth:`OpenCodeServer.create_session` 报「建会话没拿到 sessionID」——
    这是一次能看见的失败，而不是静默错会话。
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    cand = data.get("id") if isinstance(data, dict) else None
    return cand if isinstance(cand, str) and cand else None


_V1_API = ExecutorApi(
    ready_path="/config",
    session_create_path="/session",
    session_id_of=_v1_session_id_of,
    prompt_calls=_v1_prompt_calls,
    events_path="/event",
    permission_reply=_v1_permission_reply,
    question_reply=_v1_question_reply,
    abort_path=_v1_abort_path,
    directory_query=_v1_directory_query,
    permission_pending_path="/permission",
    question_pending_path="/question",
    projects_paths=("/project",),
    normalize_projects=_v1_normalize_projects,
    agents_paths=("/agent",),
    normalize_agents=_v1_normalize_agents,
    models_paths=("/provider",),
    normalize_models=_v1_normalize_models,
    parse_event=_v1_parse_event,
    reply_needs_session=False,
    catalog_lag_seconds=0.0,
)


_V1 = ExecutorAdapter(
    executor="opencode",
    major=1,
    verified=True,
    dispatchable=True,
    field_names=V1_FIELD_NAMES,
    build_permission_config=_v1_permission_config,
    build_read_only_config=_v1_read_only_config,
    parse_request=_v1_parse_request,
    build_reply=_v1_build_reply,
    parse_question=_v1_parse_question,
    build_question_reply=_v1_build_question_reply,
    api=_V1_API,
)


# ── opencode V2（2026-10-15 对着真机 2.0.29 逐项实测后接线）─────────────

#: V2 的改名。**只记我们确实知道的**。
#:
#: 来源是实测，不是文档：``permission.asked`` 事件的 ``action`` 字段在 V2 上
#: 回的是 ``shell``（V1 是 ``bash``），授权答复路径也从 ``/permission`` 挪到了
#: ``/api/session/{sid}/permission``。
V2_FIELD_RENAMES = {
    "container": "permissions",   # 对象 → 数组
    "shell": "shell",             # bash → shell
    "subagent": "subagent",       # task → subagent
}


def _rule(action: str, effect: str) -> dict[str, str]:
    """一条 V2 的 permission 规则。**顺序有意义**（last matching rule wins）。"""
    return {"action": action, "resource": "*", "effect": effect}


def _v2_permission_config() -> dict[str, Any]:
    """V2 委派用的 permission 配置。**失败关闭**：没登记的动作一律 ``ask``。

    ## 形状与 V1 不是同一回事

    V1 是一个**对象**（键 = 工具名，值 = 动作），V2 是一个**数组**（每条
    规则带 action/resource/effect）。差别不止是「换了容器」：

    - V1 靠 **dict 键序 + last matching rule wins** 决定命中哪条；
      V2 靠 **数组顺序**，同样是 last matching rule wins。
      所以两者的通配兜底都必须**放最前**，否则它会盖掉后面每一条具体规则。
    - V2 的未命中默认**不是 allow**：官方语义与 V1 一致，未命中落到 ``ask``。
      但**这一条不能当安全边界用** —— 我们自己那份 ``*: ask`` 才是。
    - ``edit`` 在 V2 覆盖 edit / write / patch（V1 是四个独立工具）。

    ## 为什么兜底是 ``ask``，而不是 allow

    同 :func:`_v1_permission_config`：``*: allow`` 会把任何**没登记**的动作
    （插件注入的、将来新增的）一律放行，而失效方向恰好是最危险的那侧
    （见 :mod:`docs.postmortem.0001`）。

    ## 动作名哪些**验过**、哪些没有

    实测过：``shell``（``permission.asked`` 回的就是它）、``*``（通配）。
    ``read`` / ``glob`` / ``grep`` / ``edit`` / ``patch`` / ``webfetch`` /
    ``websearch`` / ``subagent`` 来自 V2 的命名约定，**没有逐个对着真机验过**。

    所以这里刻意**只登记我们能解释的动作**，其余全部落到 ``*: ask`` ——
    记错一个名字的后果是「那条规则不生效、退回兜底 ask」（卡片多问一次），
    而**不是**「静默放行」。fail-closed 的方向就是这样保住的。
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        V2_FIELD_RENAMES["container"]: [
            # 兜底 —— 必须第一项（last matching rule wins，见函数 docstring）
            _rule("*", "ask"),
            # 读类：干活必需，闸门只该拦「有后果的动作」
            _rule("read", "allow"),
            _rule("glob", "allow"),
            _rule("grep", "allow"),
            # 写类：执行期闸门真正要拦的东西
            _rule("edit", "ask"),
            _rule("patch", "ask"),
            _rule(V2_FIELD_RENAMES["shell"], "ask"),
            _rule("webfetch", "ask"),
            _rule("websearch", "ask"),
            # 永不
            _rule(V2_FIELD_RENAMES["subagent"], "deny"),
            # **必须显式 deny**：兜底是 ``*: ask``，所以不写这句
            # ``external_directory`` 会退化成「问一次」—— 也就是「人可以放它
            # 越界」。V1 那份一直是 ``deny``（11.8 第 1 道闸门：「永不越界」），
            # V2 不能比它松。
            _rule("external_directory", "deny"),
        ],
    }


def _v2_read_only_config() -> dict[str, Any]:
    """V2 的**只读**权限配置 —— ``/ask`` 用的那一份。

    与 :func:`_v1_read_only_config` 同一条纪律：``/ask`` 没有审批界面，
    ``ask`` 等于挂死，所以兜底必须是 ``deny`` 而不是 ``ask``。
    **漏登记一个动作 = ``/ask`` 多一条能改东西的路**，而它的全部安全声明
    就建立在「改不了」上面。

    同样地，动作名没逐个验过的都落到 ``*: deny`` —— 记错名字在这里的代价
    是「问一句用不上这个工具」，不是「悄悄改了文件」。
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        V2_FIELD_RENAMES["container"]: [
            # 兜底 —— 必须第一项
            _rule("*", "deny"),
            # 只读地看这个项目：这一组之外全都不许
            _rule("read", "allow"),
            _rule("glob", "allow"),
            _rule("grep", "allow"),
            # 显式 deny（写类交给兜底，但列出来是为了「读代码就能看出边界」）
            _rule("edit", "deny"),
            _rule("patch", "deny"),
            _rule(V2_FIELD_RENAMES["shell"], "deny"),
            _rule("webfetch", "deny"),
            _rule("websearch", "deny"),
            _rule(V2_FIELD_RENAMES["subagent"], "deny"),
            _rule("external_directory", "deny"),
        ],
    }


def _v2_parse_request(properties: Any) -> ToolPermission | None:
    """从 ``permission.asked`` 事件的载荷里取出请求（V2 实测载荷）。

    V2 实测::

        {"id": "per_...", "sessionID": "ses_...", "action": "shell",
         "resources": ["echo hello-v2"], "save": ["echo *"],
         "source": {"type": "tool", "messageID": "msg_...", "id": "call-..."}}

    与 V1 的三处差别：``permission`` → ``action``、``patterns`` → ``resources``、
    ``always`` → ``save``。

    ## ``suggested_always`` **刻意不映射**

    V2 的 ``save`` 等价于 V1 的 ``always``（「下次别再问」）。本仓的闸门
    **永不**授予永久授权（:func:`_v1_build_reply` 只回 ``once``/``reject``）。
    把 ``save`` 塞进 ``suggested_always`` 会让卡片显示成「可以永久允许」——
    而那个选项根本不存在，用户点了会以为生效了。
    """
    if not isinstance(properties, dict):
        return None
    rid = properties.get("id")
    action = properties.get("action")
    if not isinstance(rid, str) or not rid:
        return None
    if not isinstance(action, str) or not action:
        return None
    raw_resources = properties.get("resources")
    paths = tuple(
        p for p in raw_resources if isinstance(p, str)
    ) if isinstance(raw_resources, list) else ()
    return ToolPermission(request_id=rid, permission=action, paths=paths)


def _v2_build_reply(decision: str) -> dict[str, str]:
    """把本地结论翻成 V2 的 ``decision`` 载荷（实测：``{"decision":"once"}``）。

    与 V1 唯一的差别是键名。**同样不映射到 ``always``**，理由同 V1。
    """
    if decision not in ("allow", "deny"):
        raise ValueError(f"未知结论：{decision!r}")
    return {"decision": "once" if decision == "allow" else "reject"}


#: V2 的提问字段键。实测 opencode 生成 ``q0`` / ``q1`` …
#:
#: 答复载荷要按**这个**键回，而 ``Form.Reply`` 是 ``{字段键: 答案}``。
#: 万一真机用了别的键名，V2 会回 ``400 FormInvalidAnswerError``，
#: 而那会**响亮地**传回用户（「答案已收到，但送回 opencode 失败」），
#: 不是静默变成一个错答案 —— 这是敢在这里推导键名的前提。
_V2_FIELD_KEY = "q"


def _v2_parse_question(properties: Any) -> ToolQuestion | None:
    """从 ``form.created`` 的表单里取出提问（V2 实测载荷）。

    V2 **没有** ``question.asked``：提问变成了一个 Form。实测::

        {"id": "frm_...", "sessionID": "ses_...", "title": "Questions",
         "metadata": {"kind": "question", "tool": {...}},
         "fields": [{"key": "q0", "title": "Color preference",
                     "description": "Which color do you prefer?",
                     "type": "string",
                     "options": [{"value": "red", "label": "red",
                                  "description": "Prefer red"}, ...],
                     "custom": true}]}

    ## V2 的 ``options`` 是**每个问题各一组**

    V1 那个「扁平选项池」的语义在这条线上被证伪了：V2 的 options 挂在
    field 上，一题一组。所以这里填 :attr:`ToolQuestion.option_groups`
    （按题分组），而 ``options`` 留空 —— 那样卡片会**只列当前这一题的选项**，
    不会把第 2 题的选项混到第 1 张卡上。
    """
    if not isinstance(properties, dict):
        return None
    rid = properties.get("id")
    if not isinstance(rid, str) or not rid:
        return None
    raw_fields = properties.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        return None
    questions: list[str] = []
    groups: list[tuple[str, ...]] = []
    for i, field in enumerate(raw_fields):
        if not isinstance(field, dict):
            return None
        key = field.get("key")
        if key != f"{_V2_FIELD_KEY}{i}":
            # 键名与我们能回的对不上 —— 整条不认。半认会让人答了题、
            # 而答复发到一个错的字段上，症状是「答了但 agent 说没答」。
            return None
        title = field.get("title")
        desc = field.get("description")
        text = (desc if isinstance(desc, str) and desc.strip() else "") \
            or (title if isinstance(title, str) and title.strip() else "")
        if not text:
            return None
        questions.append(text.strip())
        raw_o = field.get("options")
        if raw_o is None:
            groups.append(())
        elif isinstance(raw_o, list) and all(
            isinstance(o, dict) for o in raw_o
        ):
            values = []
            for o in raw_o:
                value = o.get("value")
                values.append(value.strip() if isinstance(value, str) else "")
            groups.append(tuple(v for v in values if v))
        else:
            # 选项形状不认识就不认这条，别渲染出一堆没意义的空行。
            return None
    return ToolQuestion(
        request_id=rid,
        questions=tuple(questions),
        option_groups=tuple(groups),
    )


def _v2_build_question_reply(answers: Sequence[Sequence[str]]) -> dict[str, Any]:
    """把逐轮积累的答复翻成 V2 的 Form 答复载荷（实测：``{"answer": {...}}``）。

    字段键按 :data:`_V2_FIELD_KEY` 从**位置**推导（``q0`` / ``q1`` …），
    而答案的顺序就是 :func:`_v2_parse_question` 建出来的 ``questions`` 顺序
    —— 两者同源，所以位置对得上。键名对不上时 V2 会 400，
    见 :data:`_V2_FIELD_KEY` 的说明。
    """
    rows = _rows_or_raise(answers)
    answer: dict[str, str] = {}
    for i, row in enumerate(rows):
        # 取第一格：V2 的一个 field 是**一个**答案槽，不是多选集合。
        answer[f"{_V2_FIELD_KEY}{i}"] = row[0]
    return {"answer": answer}


#: V2 的事件别名。**只翻译我们真正处理的那些**，其余原样透传。
#:
#: 为什么必须翻译而不是让调用方按版本分支：``delegate.run_once`` 与
#: :mod:`ask` 里的判据是**内部词汇**（``session.idle`` / ``question.asked`` /
#: ``message.part.delta`` …），它们由 V1 的实测分布定下来、且有一批测试
#: 直接喂 V1 形状。让每个调用点都变成 ``if major == 2`` 会把协议知识
#: 重新撒回三个文件 —— 那正是这层要消灭的东西。
_V2_EVENT_ALIAS: dict[str, str] = {
    "form.created": "question.asked",
    "session.execution.succeeded": "session.idle",
    "session.execution.failed": "session.error",
    "session.text.delta": "message.part.delta",
    "session.text.ended": "message.part.updated",
    # ``tool.started`` 是**只给进度卡片**的（delegate 认得任意 kind）；
    # ``tool`` 才是 ask.py 数轮次的那一个 —— 一次工具调用只能计一次，
    # 所以这里刻意只把 ``session.tool.called`` 映射成 ``tool``。
    "session.tool.input.started": "tool.started",
    "session.tool.called": "tool",
}


def _v2_parse_event(obj: Any) -> tuple[str, Any] | None:
    """V2 的一帧 → ``(内部事件名, 归一化后的载荷)``。

    ## 载荷在 ``data``，不是 ``properties``

    实测帧形如 ``{"id":"evt_...","type":"session.tool.called","data":{...}}``。
    **认错这个键不会报错**：每一条事件都变成「不认识」，于是授权卡永不出现、
    会话一直跑到时长上限。这正是 :mod:`executors` 那层要挡住的失败。

    ## 为什么还要改写载荷，而不只是改名

    调用方（``delegate`` / ``ask`` / ``tool_event_label``）读的是 V1 形状：
    ``session.text.ended`` 要读 ``part.text``、工具事件要读 ``tool`` 与
    ``state.input``、提问要读表单本身。**只改名不改形状，症状是
    「事件到了、内容读不到」** —— 比没有事件更难查。
    """
    kind = _v1_event_type(obj)
    if kind is None:
        return None
    data = obj.get("data")
    mapped = _V2_EVENT_ALIAS.get(kind, kind)

    if mapped == "question.asked":
        # 多包了一层 form：提问的本体在 data.form 里。
        form = data.get("form") if isinstance(data, dict) else None
        return (mapped, form) if isinstance(form, dict) else None

    if mapped == "message.part.updated":
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            return None
        return mapped, {"part": {"type": "text", "text": text}}

    if mapped in ("tool", "tool.started"):
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name:
            return None
        if mapped == "tool":
            payload: dict[str, Any] = {"tool": name}
            args = data.get("input")
            if isinstance(args, dict):
                payload["state"] = {"input": args}
            return mapped, payload
        return mapped, {"tool": name}

    # 其余原样透传（含 permission.asked：它的载荷形状由
    # :func:`_v2_parse_request` 自己判，不在这里猜）。
    return mapped, data


def _v2_prompt_calls(
    session_id: str, brief: str, model: str, agent: str, variant: str
) -> list[HttpCall]:
    """V2 的提示链路：先切模型/工作模式，再发正文。

    V2 的 ``POST /session/{sid}/prompt`` 只收 ``{"text": ...}``
    （``additionalProperties: false``）—— V1 那套 ``parts`` / ``model`` /
    ``agent`` / ``variant`` 全被挪到了**会话级**端点上。

    ## variant 只能跟 model 一起给

    V2 把 ``variant`` 收进了 ``Model.Ref``（``{id, providerID, variant}``），
    所以**没有 model 就没有地方放 variant**。只给 variant 时这里直接抛 ——
    让调用方看到一句能读懂的话，而不是「选了 high 却按默认档跑」这种
    无报错的退化（V1 踩过同型的坑）。
    """
    if variant and not model:
        raise ValueError(
            "V2 的推理档只能与模型一起指定（它在 Model.Ref 里）—— "
            "收到了 variant 却没有 model。先选模型再选推理档。"
        )
    calls: list[HttpCall] = []
    if model:
        provider, _, name = model.partition("/")
        ref: dict[str, Any] = {
            "id": name or provider,
            "providerID": provider,
        }
        if variant:
            ref["variant"] = variant
        calls.append(("POST", f"/api/session/{session_id}/model", {"model": ref}))
    if agent:
        calls.append((
            "POST", f"/api/session/{session_id}/agent", {"agent": agent}
        ))
    calls.append(("POST", f"/api/session/{session_id}/prompt", {"text": brief}))
    return calls


def _v2_permission_reply(
    session_id: str, request_id: str, decision: str
) -> HttpCall:
    """V2 的授权答复：路径**带会话**，载荷是 ``decision``。

    ``request_id`` 转义理由同 V1（id 是对方发来的，不可信）；
    ``session_id`` 是我们自己建的，保持原样。
    """
    if not session_id:
        raise ValueError(
            "V2 的授权答复必须带会话 id，但本服务器还没建过会话 —— "
            "这是内部顺序错误，不是环境问题。"
        )
    rid = urllib.parse.quote(request_id, safe="")
    return (
        "POST",
        f"/api/session/{session_id}/permission/{rid}/reply",
        _v2_build_reply(decision),
    )


def _v2_question_reply(
    session_id: str, request_id: str, answers: Sequence[Sequence[str]]
) -> HttpCall:
    """V2 的提问答复：路径是 ``/api/session/{sid}/form/{fid}/reply``。"""
    if not session_id:
        raise ValueError(
            "V2 的提问答复必须带会话 id，但本服务器还没建过会话 —— "
            "这是内部顺序错误，不是环境问题。"
        )
    rid = urllib.parse.quote(request_id, safe="")
    return (
        "POST",
        f"/api/session/{session_id}/form/{rid}/reply",
        _v2_build_question_reply(answers),
    )


def _v2_directory_query(directory: str) -> str:
    """V2 的「限目录」查询串：``?location[directory]=<quoted>``。

    ## 为什么与 V1 不是同一个形状

    V2 的 ``location`` 是个 **deepObject**（``style=deepObject`` /
    ``explode=true``），序列化出来是 ``location[directory]=<值>``，
    而 V1 是普通的 ``?directory=<值>``。**这是从 OpenAPI 规格推出来的，
    没有对着真机验过** —— 所以哪天 V2 这里回 4xx，第一处该看的是这个形状，
    而不是怀疑会话本身。
    """
    return f"?location[directory]={urllib.parse.quote(directory, safe='')}"


def _v2_abort_path(session_id: str) -> str:
    """V2 用 ``interrupt`` 取代 V1 的 ``abort``（实测 200）。"""
    return f"/api/session/{session_id}/interrupt"


def _unwrap_list(raw: Any) -> list[dict[str, Any]]:
    """V2 的列表响应是 ``{location, data: [...]}``；V1 是裸数组。"""
    if isinstance(raw, dict):
        raw = raw.get("data")
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return []


def _v2_normalize_projects(responses: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``/api/project`` → 归一化后的项目行。

    ## 为什么这里要补 ``worktree``

    V2 的 ``Project`` 只有 ``canonical``（绝对路径），**没有 V1 的 ``worktree``
    也没有 ``name``**。而 :func:`~freeagent.services.oc_selection.list_project_options`
    读的是 ``worktree``。不补的话下拉里一个项目都没有 —— 那会被误读成
    「OpenCode 没起来」，而真实原因只是字段名换了。

    ``name`` 刻意**不编**：选择层已经会退回用目录名，编一个出来只会让
    界面上出现一个用户不认识的标签。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in responses.values():
        for row in _unwrap_list(raw):
            worktree = str(row.get("canonical") or row.get("worktree") or "").strip()
            if not worktree or worktree in seen:
                continue
            seen.add(worktree)
            copy = dict(row)
            copy["worktree"] = worktree
            out.append(copy)
    return out


def _v2_normalize_agents(responses: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``/api/agent`` → 工作模式行。字段名与 V1 兼容（``name`` / ``mode``）。

    实测 7 条：``build`` / ``plan`` 是 ``primary``，``general`` / ``explore``
    是 ``subagent``，``compaction`` / ``title`` / ``summary`` 是
    ``primary`` 且 ``hidden: true``（内部件）。

    ## ``model`` 在 V2 上**恒为 null**

    不是 bug：V2 把模型整个挪到了会话上，agent 不再自带模型。
    所以选择层**不能**再拿「model 非空」当「可选」的判据（那是 V1 的做法），
    要改用 ``hidden`` + 名单 —— 见 :mod:`oc_selection`。

    ## 为什么把 ``id`` 塞进 ``name``

    V2 的 agent 有**两个**字符串：``id``（``Agent.ID``，机器标识）与
    ``name``（给人看的显示名）。而**回传给执行器的必须是 ``id``**
    —— OpenAPI 里 ``POST /api/session/{sid}/agent`` 的请求体是
    ``{"agent": Agent.ID}``，``GET /api/agent/{agentID}`` 也按 ``id`` 取。
    传显示名的后果与「传一个不存在的 agent」完全一样：V2 **查表取
    ``undefined``、不报错**，于是派发用了一个不是用户选的模式（11.9.9 同型故障）。

    而 V1 只有一个 ``name``，选择层读的就是它。所以这里把 V2 的 ``id``
    挪进 ``name``（选择层当 value 用），把显示名放进额外的 ``label``
    （选择层只拿它渲染按钮，见 :func:`~freeagent.services.oc_selection.list_agent_options`）。
    这样两个版本喂给选择层的形状一致，选择层不必知道版本。
    """
    out: list[dict[str, Any]] = []
    for raw in responses.values():
        for row in _unwrap_list(raw):
            ident = str(row.get("id") or "").strip() or str(row.get("name") or "").strip()
            if not ident:
                continue
            copy = dict(row)
            pretty = str(row.get("name") or "").strip()
            copy["name"] = ident
            if pretty and pretty != ident:
                copy["label"] = pretty
            out.append(copy)
    return out


def _v2_normalize_models(responses: Mapping[str, Any]) -> dict[str, Any]:
    """``/api/model`` + ``/api/provider`` → V1 ``/provider`` 的形状。

    ## 为什么要做这次「折叠」

    :mod:`oc_selection` 读的形状是 V1 的::

        {"all": [{"id", "name", "models": {mid: {"name", "cost", "variants"}}}],
         "connected": [...]}

    而 V2 有两个**实质**差异，不只是换了个路径：

    1. ``cost`` 是**数组**（V1 是 ``{input, output}`` 对象）；
    2. ``variants`` 也是**数组**（V1 是 dict）；
    3. 模型**不再挂在 provider 下面** —— ``/api/provider`` 压根没有 models
       字段，模型在另一个端点 ``/api/model`` 上，靠 ``providerID`` 关联。

    所以这里必须：按 ``providerID`` 分组、把两个数组折成 dict、并从
    ``/api/model`` 里**推导** ``connected``（V2 没有这个字段了）。

    ## ``enabled: false`` 的模型不进列表

    V2 给每个模型一个 ``enabled`` 开关。关掉的模型点了必然用不了，
    而 V1 那边它们压根不会出现在 ``connected`` 里 —— 语义一致。
    """
    models_raw = next(
        (v for k, v in responses.items() if k.rstrip("/").endswith("model")), None
    )
    provider_raw = next(
        (v for k, v in responses.items() if k.rstrip("/").endswith("provider")), None
    )

    provider_names: dict[str, str] = {}
    provider_states: dict[str, str] = {}
    for row in _unwrap_list(provider_raw):
        pid = str(row.get("id") or "").strip()
        if not pid:
            continue
        provider_names[pid] = str(row.get("name") or "").strip() or pid
        provider_states[pid] = str(row.get("activation") or "").strip()

    grouped: dict[str, dict[str, Any]] = {}
    seen_providers: list[str] = []
    for row in _unwrap_list(models_raw):
        if row.get("enabled") is False:
            continue
        pid = str(row.get("providerID") or "").strip()
        mid = str(row.get("modelID") or row.get("id") or "").strip()
        if not pid or not mid:
            continue
        if pid not in grouped:
            grouped[pid] = {
                "id": pid,
                "name": provider_names.get(pid) or pid,
                "models": {},
            }
            seen_providers.append(pid)
        grouped[pid]["models"][mid] = {
            "name": str(row.get("name") or "").strip() or mid,
            "cost": _fold_cost(row.get("cost")),
            "variants": _fold_variants(row.get("variants")),
        }

    # connected = 出现在模型列表里、且 provider 没被显式关掉的那些。
    connected = [
        pid for pid in seen_providers if provider_states.get(pid) != "disabled"
    ]
    return {"all": [grouped[pid] for pid in seen_providers], "connected": connected}


def _fold_cost(raw: Any) -> dict[str, Any]:
    """V2 的 ``cost`` 数组 → V1 的 ``{input, output}``。

    取**第一档**（实测免费模型就是 ``[{input:0, output:0, cache:{...}}]``）。
    选择层只拿它判「要不要钱」，多档位时取基线那一档就够。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        first = raw[0]
        return {"input": first.get("input"), "output": first.get("output")}
    return {}


def _fold_variants(raw: Any) -> dict[str, Any]:
    """V2 的 ``variants`` 数组 → V1 的 ``{档位名: 档位}``。"""
    if isinstance(raw, dict):
        return raw
    out: dict[str, Any] = {}
    if isinstance(raw, list):
        for v in raw:
            if isinstance(v, dict) and isinstance(v.get("id"), str):
                out[v["id"]] = v
    return out


_V2_API = ExecutorApi(
    ready_path="/api/info",
    session_create_path="/api/session",
    session_id_of=_v2_session_id_of,
    prompt_calls=_v2_prompt_calls,
    events_path="/api/event",
    permission_reply=_v2_permission_reply,
    question_reply=_v2_question_reply,
    abort_path=_v2_abort_path,
    directory_query=_v2_directory_query,
    permission_pending_path="/api/permission/request",
    question_pending_path="/api/form",
    projects_paths=("/api/project",),
    normalize_projects=_v2_normalize_projects,
    agents_paths=("/api/agent",),
    normalize_agents=_v2_normalize_agents,
    models_paths=("/api/model", "/api/provider"),
    normalize_models=_v2_normalize_models,
    parse_event=_v2_parse_event,
    reply_needs_session=True,
    # 实测：就绪探针（/api/info）0.6s 就能 200，但目录要到 ~2.7s 才加载完，
    # 期间 /api/model 回 503、/api/{provider,agent} 回空数组。
    # 空的 agent 列表会让 delegate 的工作模式校验**误杀一次真实委派**，
    # 所以列表方法要按这个窗口重试。
    catalog_lag_seconds=4.0,
)


_V2 = ExecutorAdapter(
    executor="opencode",
    major=2,
    verified=True,
    dispatchable=True,
    field_names=V2_FIELD_RENAMES,
    build_permission_config=_v2_permission_config,
    build_read_only_config=_v2_read_only_config,
    parse_request=_v2_parse_request,
    build_reply=_v2_build_reply,
    parse_question=_v2_parse_question,
    build_question_reply=_v2_build_question_reply,
    api=_V2_API,
)


# ── hermes（**未验证**：照文档看形状很顺，但一次真机都没验过）────────────
#
# ## 它有**两个**接口面，别只看一个
#
# 1. **ACP**（``hermes acp`` / ``acp_adapter.entry``）—— stdio 上的 JSON-RPC
# 2. **API Server**（``hermes gateway`` 起的 HTTP 端点，默认 8642）
#    —— **OpenAI 兼容**的 ``POST /v1/chat/completions``，以及
#    一组**有状态**的 run 端点：``POST /v1/runs``、
#    ``GET /v1/runs/{id}/events``（SSE）、``POST /v1/runs/{id}/stop``、
#    ``POST /v1/runs/{id}/approval``，能力面见 ``GET /v1/capabilities``
#    （``run_submission`` / ``run_events_sse`` / ``run_stop`` / ``run_approval``）。
#
# ## 所以「接第二个执行器」比想象中容易，但仍然不能现在就派
#
# 值得高兴的那半：走 **API Server** 那条面，形状与 opencode **同构** ——
# 同为 HTTP + SSE，同样「建会话 → 流式读数 → 中止 → 回应授权」，
# 连授权事件都对得上（opencode ``permission.asked`` ↔ hermes
# ``approval.request`` + ``waiting_for_approval``）。**不需要引 ACP SDK，
# 也不需要换传输层**：本仓给 opencode 用的就是标准库 ``urllib``。
#
# 仍然不能派的那半：上面这些都是**读文档读来的，一次都没对着真机验过**。
# 而这正是 11.8.1 吃过的亏 —— 照文档接线、字段名对不上时**不报错**，
# 配置被静默忽略，闸门失效，且失效方向是回到 allow
# （见 :mod:`docs.postmortem.0001`）。
#
# 所以这里能做、且只做的是：把它**登记**成「本仓知道它存在」，
# 但**一个协议函数都不实现** —— 猜一个「看起来很像对的」形状比拒绝更危险。
#
# > ⚠️ 另一条**绝不能走**的捷径：直接把 hermes 当 OpenAI 兼容后端来问
# > （``/v1/chat/completions``）。那条面**工具已在服务端执行完毕**，
# > 回放出来的 ``function_call`` 一律是 ``"status": "completed"`` ——
# > 也就是说 agent 已经动过 terminal 和文件系统了，本仓**看不到也拦不住**。
# > 那等于绕开 11.8 的全部闸门，而症状只是「能用」。
#
# ## 一个反向结论：这条面**走不了 11.8 委派**
#
# 读 ``tools/approval_context.py``（**未实测**）：它把 **``api_server``
# 判为 unattended 平台**，注释原话是「there is no human who can resolve a
# pending approval … blocks the session for the full approval timeout」。
# 所以这条面上**做不了** 11.8 那条「执行期逐次授权」——
# pending 产生后没人能答，只会阻塞到超时再 fail-closed。
#
# 好消息是危险命令改由 ``approvals.unattended_mode`` 裁决，而它
# **默认 deny，未知值也 deny**（``_binary_approval_mode`` 只放行
# ``approve``/``off``/``allow``/``yes``）—— 连配错了都是 fail-closed。
#
# **结论**：hermes 在这条面上**只适合 ``/ask``（只读问答），不适合 11.8 委派**。
# 这与 19.4 的领域划分对得上 —— 编程走 opencode（有逐次授权、能委派），
# 办公走 hermes（问答）。不是妥协，是两个执行器的能力面本来就不一样。

#: hermes 侧**没有**与 V1 对位的「按工具名写 permission 配置」那一面。
#:
#: 它的闸门是**两套东西加起来的**：
#:
#: - **profile 级三档审批模式**（``manual`` / ``smart`` / ``off``）——
#:   回答「这类操作要不要**问人**」；
#: - **run 级授权端点** —— 命中策略时 run 发 ``approval.request`` 事件、
#:   停在 ``waiting_for_approval``，由客户端 ``POST /v1/runs/{id}/approval``
#:   决定 ``once``（放行这一次）或 ``deny``（拦下）。
#:
#: 而本仓 11.10.3 的三桶回答的是第三个问题：「这个执行器**能不能**做这类事」。
#: **三者不是同一语义** —— 尤其 ``manual`` **仍会执行**，只是先问一句；
#: 本仓要的是「``/ask`` 根本改不了」（11.10.6），靠三档模式是拿不到的。
#:
#: 所以这里**刻意留空**，不把 ``manual`` 之类塞进 ``field_names`` 假装是
#: 键名映射。留空以后，任何试图按 opencode 那套拼 permission 配置的代码
#: 都会立刻拿到空字典而炸掉，而不是拿到一份看起来合理、实则无效的映射。
HERMES_FIELD_NAMES: dict[str, str] = {}


#: hermes run 事件名 → 内部事件名。**只翻译我们真正处理的那些。**
#:
#: 来源是**本机 hermes 源码**（``gateway/platforms/api_server_runs.py``）：
#: 每个 SSE 帧是 ``_run_event(run_id, name, **fields)`` = ``{"event": name,
#: "run_id", "timestamp", **fields}``；事件名取 ``obj["event"]``。
#:
#: 翻译对齐 opencode 的分布（V1 实测定下、ask/delegate 读的就是这些内部名）：
#:
#: - ``message.delta``（载荷 ``delta=`` 增量）→ ``message.part.delta``，
#:   于是 :func:`~freeagent.services.ask._delta_from_part` 原样复用。
#: - ``tool.completed`` → ``tool``：ask 数轮次只数 ``kind == "tool"``
#:   （11.10.4），一次工具调用计一次。刻意**只**把完成事件映射成 ``tool``，
#:   ``tool.started`` 只给进度卡（delegate 认得任意 kind）—— 与 V2 同一条
#:   纪律：一次调用只能计一次。
#: - ``approval.request`` → ``permission.asked``：/ask 遇它即中止（只读配置
#:   下不该有任何授权请求）。⚠️ **V1.81 真机证伪了「unattended deny 让危险
#:   命令直接拒、approval.request 本不该出现」的假设**：hermes 的审批闸是
#:   「按模式标记」，``del`` / 简单 ``write_file`` 不在危险清单里，**根本不进
#:   这个闸**、也不发 approval.request，直接执行。所以 approval.request 真出现
#:   只是「连被标记的动作都没挡住」的更糟情况，ask 依然中止报错，但这道闸
#:   拦不住**未标记**的 file/terminal 操作（见模块 docstring 的 V1.81 处置）。
#: - ``run.completed`` → ``session.idle``（权威结束信号）；``run.failed`` →
#:   ``session.error``；``run.cancelled`` → ``session.idle``（被叫停不是失败，
#:   保留已拿到的部分；ask 主动中止时早已 return，正常不会走到它）。
_HERMES_EVENT_MAP: dict[str, str] = {
    "message.delta": "message.part.delta",
    "tool.completed": "tool",
    "tool.started": "tool.started",
    "approval.request": "permission.asked",
    "run.completed": "session.idle",
    "run.failed": "session.error",
    "run.cancelled": "session.idle",
}


def _hermes_event_name(obj: Any) -> str | None:
    """hermes 帧里取事件名。**类型不对就返回 ``None``**，由调用方丢掉。

    hermes 的事件名在帧 JSON 的 ``event`` 字段（``_run_event`` 的 name），
    **不在** ``type`` —— 与 opencode 两版都不一样，所以不能复用
    :func:`_v1_event_type`。
    """
    if not isinstance(obj, dict):
        return None
    kind = obj.get("event")
    return kind if isinstance(kind, str) and kind else None


def _hermes_parse_event(obj: Any) -> tuple[str, Any] | None:
    """hermes 的一帧 → ``(内部事件名, 归一化后的载荷)``。

    hermes 的载荷键与 opencode 不同（``delta`` / ``tool`` 在**顶层**，而
    opencode 的 ``message.part.delta`` 在 ``properties`` / ``data`` 里）。
    所以这里不仅改事件名，还要把载荷折成调用方认的形状 —— 只改名不改形状，
    症状是「事件到了、内容读不到」（与 V2 同一条纪律）。

    ``message.delta`` 的载荷折成 ``{"delta": ...}``：ask 的
    ``_delta_from_part`` 读的就是 ``props["delta"]``，这样 ask 一行不用改。
    ``tool.completed`` / ``tool.started`` 折成 ``{"tool": name}``：delegate 的
    ``tool_event_label`` 读 ``props["tool"]``。
    """
    kind = _hermes_event_name(obj)
    if kind is None:
        return None
    mapped = _HERMES_EVENT_MAP.get(kind)
    if mapped is None:
        # 不翻译的（reasoning.available / approval.responded / run.steered 等）
        # 原样透传也认不得 —— 直接丢掉。ask 只关心上面的子集。
        return None

    if mapped == "message.part.delta":
        delta = obj.get("delta")
        if not isinstance(delta, str):
            return None
        return mapped, {"delta": delta}

    if mapped in ("tool", "tool.started"):
        name = obj.get("tool")
        if not isinstance(name, str) or not name:
            return None
        return mapped, {"tool": name}

    if mapped == "session.error":
        # ``run.failed`` 带 ``error`` 字段（真机实测，如模型到期/余额不足的
        # HTTP 404）。折进 ``{"message": ...}`` 让 ask 能报出真话 ——
        # 笼统的「执行器报错」对用户没用（详见 ask 的 session.error 分支）。
        error = obj.get("error")
        if isinstance(error, str) and error:
            return mapped, {"message": error}
        return mapped, None

    # 其余（permission.asked / session.idle）载荷不解析 —— ask 要么中止要么
    # 结束，不读载荷。
    return mapped, None


def _hermes_build_read_only_config() -> dict[str, Any]:
    """hermes 的**只读**「配置」。

    hermes **没有** opencode 那种「按工具名写 permission 配置」的面 ——
    它的闸门是 profile 三档（manual/smart/off）+ run 级授权端点，而 /ask
    要的「根本改不了」**做不到**（V1.81 真机证伪初稿）：``approvals.mode``
    只决定**已被判为危险**的动作怎么处理，不会把更多动作标记为危险 ——
    ``del`` / 简单 ``write_file`` 不在危险清单里，API 面与飞书面都会直接执行
    而不弹审批（manual + 重启也拦不住）。所以 hermes 只能作知识问答、不能当
    可执行器开放，``/ask hermes`` 的回话常驻安全警示。这里返回空字典 ——
    任何试图按 opencode 那套拼配置的代码会立刻拿到空而炸掉，而不是拿到一份
    无效映射。

    为什么用「空」而不是拒绝：``ask_executor`` 无条件调
    :func:`~freeagent.services.opencode_server.read_only_permission_config`
    （= 当前适配器的 ``build_read_only_config()``）并把结果传给 make_server。
    对 hermes，那个值**不会被使用**（:class:`~freeagent.services.hermes_client.HermesClient`
    忽略它），所以返回空而不是抛，好让 /ask hermes 的正常路径能跑通。
    """
    return {}


def _hermes_build_permission_config() -> dict[str, Any]:
    """hermes 的委派 permission 配置：**没有**。

    hermes **不可委派**（11.10.8，unattended 面做不了执行期逐次授权），所以
    「委派用的 permission 配置」这个功能对她不存在。返回空 + 由调用方判断
    不可派发 —— 不要试图给一份「看起来合理」的配置（那会暗示能委派）。
    """
    return {}


def _hermes_parse_request(_props: Any) -> ToolPermission | None:
    """hermes 的授权请求解析：**不适用**。

    hermes 只走 /ask，而 /ask 对 ``permission.asked`` 一律中止（只读配置下
    不该有任何授权请求）—— 从**不**调 ``parse_request``。所以这里返回
    ``None``（不是能回应的请求）。不试图去读 hermes 的 ``approval.request``
    载荷：那是委派那条线的功能，hermes 没有。
    """
    return None


def _hermes_build_reply(decision: str) -> dict[str, str]:
    """hermes 的授权答复载荷：**不存在**（不委派就不回授权）。"""
    raise ValueError(
        f"hermes 不可委派，没有授权答复可发（收到 decision={decision!r}）"
    )


def _hermes_parse_question(_props: Any) -> ToolQuestion | None:
    """hermes 的提问解析：**不适用**（不委派就不回提问）。"""
    return None


def _hermes_build_question_reply(
    _answers: Sequence[Sequence[str]],
) -> dict[str, Any]:
    """hermes 的提问答复载荷：**不存在**（不委派就不回提问）。"""
    raise ValueError("hermes 不可委派，没有提问答复可发")


def _hermes_run_id_of(raw: Any) -> str | None:
    """POST /v1/runs → run id。

    真机实测（判据 5）：响应是 ``{"run_id": "run_…", "status": "started"}`` ——
    字段名是 ``run_id``，**不是** ``id``。别再猜成 opencode 的 ``session_id``。
    ``HermesClient`` 其实不用它（它直接拿 POST 的响应体取 run id），这里只是
    占住 ExecutorApi 的字段 —— hermes 不走 OpenCodeServer 的 ``create_session``。
    """
    if isinstance(raw, dict):
        cand = raw.get("run_id") or raw.get("id")
        return cand if isinstance(cand, str) and cand else None
    return None


def _hermes_prompt_calls(
    _session_id: str, _brief: str, _model: str, _agent: str, _variant: str
) -> list[HttpCall]:
    """hermes 的 prompt 链路：**不走 OpenCodeServer.prompt_async**。

    hermes 的 run 是「提交即运行」：prompt 在 ``POST /v1/runs`` 的请求体里，
    没有 opencode 那种「先建空会话、再发 prompt」的拆分。所以这个函数
    （给 OpenCodeServer 用）对 hermes **不会被执行** —— HermesClient 自己
    拼 ``/v1/runs``。这里返回空 + 抛，避免有人误以为能拿它发 prompt。
    """
    raise ValueError("hermes 的 prompt 随 /v1/runs 一起提交，不走 prompt_calls")


def _hermes_abort_path(run_id: str) -> str:
    """中止：``POST /v1/runs/{id}/stop``（源码确认的路径）。"""
    return f"/v1/runs/{urllib.parse.quote(run_id, safe='')}/stop"


def _hermes_directory_query(_directory: str) -> str:
    """hermes 无「限目录」概念（它的工作目录随 gateway 配置走）。"""
    return ""


def _hermes_normalize_projects(_responses: Mapping[str, Any]) -> list[dict[str, Any]]:
    """hermes 不委派，无项目目录（那是委派选目录用的）。"""
    return []


def _hermes_normalize_agents(_responses: Mapping[str, Any]) -> list[dict[str, Any]]:
    """hermes 不委派，无工作模式列表。"""
    return []


def _hermes_normalize_models(_responses: Mapping[str, Any]) -> dict[str, Any]:
    """hermes 不委派，无模型目录。"""
    return {}


def _hermes_deny_reply(_sid: str, _rid: str, _decision: str) -> HttpCall:
    raise ValueError("hermes 不可委派，没有授权答复端点")


def _hermes_deny_question(
    _sid: str, _rid: str, _answers: Sequence[Sequence[str]]
) -> HttpCall:
    raise ValueError("hermes 不可委派，没有提问答复端点")


#: hermes API Server 的 HTTP 面（路径与事件翻译）。
#:
#: ⚠️ **只有 ``ready_path`` 与 ``parse_event`` 会被真正使用** —— 二者分别
#: 供「就绪探针」与 ``HermesClient.events()`` 用。其余字段（session/目录/授权/
#: 提问）是 :class:`ExecutorApi` 为 opencode 那条「起进程 → 建会话 → 委派」的
#: 生命周期设计的，而 hermes **不走 OpenCodeServer**（它是**连一个已常驻的
#: gateway**，不是起一个隔离进程）。所以那些字段要么占位、要么抛，且都注明
#: 「不适用」。把空壳留在 dataclass 里而不是拆掉 ExecutorApi，是为了
#: 「parse_event 该住在哪」只有一个答案（适配器），换执行器时协议知识仍只
#: 在一处。
HERMES_API = ExecutorApi(
    ready_path="/v1/capabilities",
    session_create_path="/v1/runs",        # 不被 OpenCodeServer 使用
    session_id_of=_hermes_run_id_of,       # 不被 OpenCodeServer 使用
    prompt_calls=_hermes_prompt_calls,     # 不被 OpenCodeServer 使用
    events_path="/v1/runs/{run_id}/events",  # HermesClient 自己拼 run_id
    permission_reply=_hermes_deny_reply,
    question_reply=_hermes_deny_question,
    abort_path=_hermes_abort_path,         # HermesClient 用它
    directory_query=_hermes_directory_query,
    permission_pending_path="",
    question_pending_path="",
    projects_paths=(),
    normalize_projects=_hermes_normalize_projects,
    agents_paths=(),
    normalize_agents=_hermes_normalize_agents,
    models_paths=(),
    normalize_models=_hermes_normalize_models,
    parse_event=_hermes_parse_event,       # HermesClient.events() 用它
    reply_needs_session=False,
    catalog_lag_seconds=0.0,
)


# ── hermes（**已接线 /ask**：判据 1-3 真机验过，其余见文档 11.10.8）────────
#
# ## 它有**两个**接口面，别只看一个
#
# 1. **ACP**（``hermes acp`` / ``acp_adapter.entry``）—— stdio 上的 JSON-RPC
# 2. **API Server**（``hermes gateway`` 起的 HTTP 端点，默认 8642）
#    —— **OpenAI 兼容**的 ``POST /v1/chat/completions``，以及
#    一组**有状态**的 run 端点：``POST /v1/runs``、
#    ``GET /v1/runs/{id}/events``（SSE）、``POST /v1/runs/{id}/stop``、
#    ``POST /v1/runs/{id}/approval``，能力面见 ``GET /v1/capabilities``
#    （``run_submission`` / ``run_events_sse`` / ``run_stop`` / ``run_approval``）。
#
# ## 所以「接第二个执行器」比想象中容易，但仍然只能接 /ask
#
# 值得高兴的那半：走 **API Server** 那条面，形状与 opencode **同构** ——
# 同为 HTTP + SSE，同样「建会话 → 流式读数 → 中止 → 回应授权」，连授权事件
# 都对得上（opencode ``permission.asked`` ↔ hermes ``approval.request``）。
# **不需要引 ACP SDK，也不需要换传输层**：本仓给 opencode 用的就是标准库
# ``urllib``，给 hermes 也一样（见 :mod:`freeagent.services.hermes_client`）。
#
# 仍然只能接 /ask 的那半：V1.81 真机确认 hermes 的审批闸是**「按模式标记」
# 而非「每动作确认」** —— ``approvals.mode`` 只决定**已被判为危险**的动作
# 怎么处理，不会把更多动作标记为危险。实测 ``del`` / 简单 ``write_file`` 到
# 普通路径**不在危险清单里**，API 面与飞书面都会**直接执行而不弹审批**
# （``approvals.mode: manual`` + 重启 gateway 也一样，``file_created: True``、
# 无 ``approval.request``）。所以这条面**做不了 11.8 委派，连「只读改不了」都
# 保证不了** —— 只能当知识问答执行器（``/ask`` + 常驻安全警示），不能开放
# 执行。执行 + 飞书确认走 opencode（FreeAgent 全控）。
#
# ## 一个反向结论：这条面**走不了 11.8 委派**
#
# 所以 ``verified`` 与 ``dispatchable`` 在这里**分开了**：
#
# - ``verified = True`` —— /ask 这条线对着真机验过（判据 1 面自证、判据 3
#   unattended deny，及事件名从本机源码确认）。
# - ``dispatchable = False`` —— 不能进 11.8 委派闸门（11.10.8）。
#
# 这与 19.4 的领域划分对得上 —— 编程走 opencode（有逐次授权、能委派），
# 办公走 hermes（问答）。不是妥协，是两个执行器的能力面本来就不一样。
#
# > ⚠️ 另一条**绝不能走**的捷径：直接把 hermes 当 OpenAI 兼容后端来问
# > （``/v1/chat/completions``）。那条面**工具已在服务端执行完毕**，
# > 回放出来的 ``function_call`` 一律是 ``"status": "completed"`` ——
# > 也就是说 agent 已经动过 terminal 和文件系统了，本仓**看不到也拦不住**。
# > 那等于绕开 11.8 的全部闸门，而症状只是「能用」。``/ask hermes`` 走的是
# > 有状态的 ``/v1/runs``，不是这条捷径。

_HERMES_0 = ExecutorAdapter(
    executor="hermes",
    major=0,
    verified=True,
    dispatchable=False,     # 只 /ask，不委派（见上方 docstring）
    disabled=True,          # V1.83 停用：审批闸失效，FreeAgent 不再把它当执行器；
                            # 代码保留作接缝，修好审批可改回 False 重新启用。
    field_names=HERMES_FIELD_NAMES,
    build_permission_config=_hermes_build_permission_config,
    build_read_only_config=_hermes_build_read_only_config,
    parse_request=_hermes_parse_request,
    build_reply=_hermes_build_reply,
    parse_question=_hermes_parse_question,
    build_question_reply=_hermes_build_question_reply,
    # HTTP 面：走 API Server（``/v1/runs`` + SSE）。只读问答那条线
    # （ready_path / parse_event / abort_path）是给 HermesClient 用的；
    # 其余字段是不被使用的外壳（见 ``HERMES_API`` 的 docstring）。
    api=HERMES_API,
)


# ── 注册表 ──────────────────────────────────────────────────────────────
#
# 11.10.7：**不做动态加载**，注册表就是代码里这一份元组。加一个执行器是
# 加代码，不是加配置 —— 这样「有哪些执行器」永远是写代码时审过的，
# 而不是运行时从别处下载来的。

_ADAPTERS: tuple[ExecutorAdapter, ...] = (_V1, _V2, _HERMES_0)


def known_executors() -> tuple[str, ...]:
    """本仓**知道**的执行器名字，按注册表里的出现顺序去重。

    这就是 11.10.2 说的「有哪些执行器」那份清单 —— 刻意**由注册表导出**
    而不是另写一份常量：两份清单迟早会漂移，而漂移的方向是「探针说没有、
    注册表里其实有」。

    注意「知道」不等于「能派」：hermes 在列，但它 ``dispatchable=False``
    （只 /ask，不委派）。
    """
    seen: dict[str, None] = {}
    for a in _ADAPTERS:
        seen.setdefault(a.executor, None)
    return tuple(seen)


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

    「知道」与「支持」是两个概念。现行的例子是 hermes：它 /ask 那条线已
    验证接线，但 ``dispatchable=False`` —— 它不能**被派发**（11.10.8）。
    """
    return tuple(a.major for a in all_adapters(executor))


def dispatchable_majors(executor: str = "opencode") -> tuple[int, ...]:
    """可**委派**的（``dispatchable=True`` 且未停用）主版本，升序。版本闸门只放行这些。

    ``dispatchable`` 与 ``verified`` 分开（见 :class:`ExecutorAdapter` 的字段
    说明）：一个执行器可能只验过 /ask 这条线（hermes），那它 ``verified``
    为真、但**不可委派**，也就进不了这份清单。**停用的执行器（``disabled``）
    也绝不进这份清单** —— 即便有人把停用适配器的 ``dispatchable`` 误写成
    True，闸门也不该放行（V1.83 防御）。
    """
    return tuple(
        a.major for a in all_adapters(executor) if a.dispatchable and not a.disabled
    )


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
