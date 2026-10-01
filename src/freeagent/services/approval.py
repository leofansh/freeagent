"""待确认的本地操作 —— 落盘存储。

为什么落 SQLite 而不是内存
--------------------------
**两个进程**：桥接进程收飞书的点击并**写**答复，等待方进程**读**答复。
放内存的话等待方和桥接不是同一个进程，读不到；就算同进程，进程一挂
用户点的那一下也白点了。落盘是唯一能让「用户已同意」活过进程边界的东西。

沉默即拒绝
----------
:func:`decide` 是**唯一**读出结论的入口，它只返回三种：允许 / 拒绝 /
**过期**。没有「还没答就当允许」这条路径 —— 飞书卡片可能躺一小时没人点，
把那种沉默读成同意，等于把「没人管」当成「有人批准」。

过期即拒绝
----------
过期**不是**「还没答」，而是明确的**拒绝**。理由：请求方是拿着一个可能已经
过期的授权去动本机的东西，此时若按「未决」处理，它会一直等下去；按允许处理，
就是把一个失效的授权当成永久的。所以超时的唯一安全解释是拒绝。

委派闸门（设计文档 11.9.7）
--------------------------
本模块同时是**委派**那条路的唯一闸门。委派是唯一终点在「本机执行代码」的
能力，所以它比本地只读操作更需要闸门。以下四条都在这里，不在 ``delegate.py``：
审批策略、绕过态查询、拒绝地板、命令整条匹配 —— 它们的**判定点必须唯一**，
分散到调用方就必然按代码路径漂移（Hermes 为此有 219 个判定函数，
它自己也不得不专门写 ``approvals_test.py`` 来测这些规则）。
"""

from __future__ import annotations

import datetime
import os
import re
import shlex
import uuid
from dataclasses import dataclass
from typing import Literal, Sequence

__all__ = [
    "Decision",
    "PendingApproval",
    "ApprovalStore",
    "ApprovalContext",
    "ApprovalPolicy",
    "new_credential",
    "unattended_deny",
    "is_bypass_active",
    "command_is_blocked",
    "DEFAULT_TTL_SECONDS",
]

Decision = Literal["allow", "deny"]

#: 多久算过期。10 分钟不是拍脑袋 —— 够看完一张卡片并决定，
#: 又不至于让一个「忘了点」的请求在半小时后还能被批准。
DEFAULT_TTL_SECONDS = 600

#: 委派这类动作要人看清楚「哪个项目、做什么」，给得比只读列举久。
DELEGATE_TTL_SECONDS = 1800


# --------------------------------------------------------------------------- #
# 场景分档（规则 2）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """某个场景下「该问多久、无人应答怎么办」。

    为什么要分档而不是一条 TTL：同一件事在不同上下文里该占用的**人的注意力**
    不同。远程入口驱动的事，值得多等一会儿；本机交互的，用户就在屏幕前，
    慢一点只会烦。而**无人应答的处理只有一种：拒绝**（见 :func:`unattended_deny`）。
    """

    name: str
    ttl_seconds: int
    #: 这个场景是否允许「非交互」地跑完（定时任务等）。默认不允许。
    may_run_unattended: bool = False

    @staticmethod
    def for_context(context: str) -> "ApprovalPolicy":
        """按场景名取策略。**认不出的场景一律最严**，不回落成宽松档。

        回落成宽松档是这类 switch 最危险的写法：将来加一个场景忘了写分支，
        它会静默地按最松的处理 —— 而这条链路的失败模式是「本机执行了代码」。
        """
        table = {
            "remote": ApprovalPolicy("remote", DELEGATE_TTL_SECONDS),
            "unattended": ApprovalPolicy("unattended", 0, may_run_unattended=False),
            "local": ApprovalPolicy("local", DEFAULT_TTL_SECONDS),
        }
        got = table.get(context)
        if got is None:
            # 不认识 = 最严：立刻过期 ⇒ 一定走 deny
            return ApprovalPolicy(f"unknown:{context}", 0, may_run_unattended=False)
        return got


def unattended_deny(reason: str = "无人应答") -> Decision:
    """无人应答 → **拒绝**。

    抄自 Hermes 的同名函数。它把这条纪律**命名成了一个函数**，所以能被测。
    原先 FreeAgent 只在卡片 note 里写「不点就算拒绝」—— 那是给**用户**看的，
    给代码看的是另一回事：约定会漂移，函数不会。

    刻意不返回 ``None``（未决）：无人值守场景下没有「等下去」这个选项，
    等待方进程会一直睡到进程结束。
    """
    return "deny"


# --------------------------------------------------------------------------- #
# 绕过态（规则 4）
# --------------------------------------------------------------------------- #
#: 会让 agent 自己批准自己的环境变量 / argv 开关。
#:
#: Hermes 有 263 个 flag，其中就包括 ``--auto`` / ``--yes``。这类开关**必然**
#: 有人会传 —— 所以防线不能是「记得别传」（约定），而必须是
#: 「能查当前是否处于绕过态」（状态）。这是从 Hermes 的
#: ``is_approval_bypass_active()`` 学来的，它甚至有 ``_for_session()`` 变体，
#: 因为绕过是**会话级**的。
BYPASS_FLAGS = frozenset({"--auto", "--yes", "-y", "--dangerously-skip-permissions",
                          "--force", "--no-confirm", "--yolo"})

#: 会让本进程整体关掉批准的环境变量。
BYPASS_ENV = frozenset({"FREEAGENT_BYPASS_APPROVAL", "HERMES_APPROVAL_BYPASS"})


def is_bypass_active(
    argv: list[str] | None = None, env: dict[str, str] | None = None
) -> bool:
    """当前是否处于「批准被绕过」的状态。**这是唯一入口。**

    两条路都能绕：argv 里塞 ``--auto``，或者环境里设
    ``FREEAGENT_BYPASS_APPROVAL=1``。两者都返回 ``True``。

    为什么必须是**函数**而不是一个布尔常量：布尔量会在某次重构里被缓存、
    被改写、被某个 ``--verbose`` 分支顺手设掉。而这里每次都**重新读**，
    代价是几十纳秒 —— 拿这个代价换一个「不可能忘记检查」的属性，很划算。
    """
    if argv:
        for arg in argv:
            if arg in BYPASS_FLAGS:
                return True
            # 形如 ``--auto=true`` / ``--yes=1`` 也要认
            head = arg.split("=", 1)[0]
            if head in BYPASS_FLAGS:
                return True
    if env:
        for key in BYPASS_ENV:
            value = env.get(key)
            if value is not None and value.strip().lower() not in ("", "0", "false", "no"):
                return True
    return False


# --------------------------------------------------------------------------- #
# 拒绝地板 + 整条命令匹配（规则 5）
# --------------------------------------------------------------------------- #
#: shell 运算符。出现在 argv 的**参数**里就危险 ——
#: 因为 agent 可能把用户可控的文本塞进参数，而 :mod:`shlex` 只拆词、不管拼接。
SHELL_OPERATORS = (";", "&&", "||", "|", "`", "$(", "\n", "\r")

#: Windows 侧的等价物。``shell=True`` 时 ``&`` 能串命令，且在 cmd 上最常见。
_WINDOWS_OPERATORS = ("&", ">" , "<")


def command_is_blocked(command: str) -> tuple[bool, str]:
    """这条命令**能不能**走白名单。返回 ``(是否拦截, 原因)``。

    两条规则（抄自 Hermes 的 ``approval_floors`` 与
    ``_has_allowlist_shell_operator()``）：

    1. **含 shell 运算符一律拦。** ``git log; rm -rf ~`` 不能因为前半段
       ``git log`` 在白名单里就整体放行 —— 那样白名单就成了
       「前半段的通行证」，而这正是绕过闸门最短的路径。
    2. **不能靠前缀/子串匹配。** 必须整条命令一致：见 :func:`matches_exactly`。

    另有**显式 deny 优先于任何白名单**（地板）：见 :func:`check_command`。
    """
    if not command.strip():
        return True, "空命令"
    for op in SHELL_OPERATORS:
        if op in command:
            return True, f"含 shell 运算符 {op!r}（可拼接绕过白名单）"
    for op in _WINDOWS_OPERATORS:
        # 只在 Windows 上判，且要排除重定向箭头作为「普通字符」的情况 ——
        # 保守起见宁可多拦：这一层拦错的代价是「用户得输全命令」，
        # 漏拦的代价是「远程输入在本机执行了代码」。
        if os.name == "nt" and op in command:
            return True, f"含 Windows shell 运算符 {op!r}（可拼接绕过白名单）"
    return False, ""


def matches_exactly(allowed: set[str], command: str) -> bool:
    """命令是否**整条**在白名单里。

    刻意不用 ``startswith`` / ``in``：那两种匹配让「白名单里有 ``pytest``」
    变成「``pytest --tb=no -p no:cacheprovider; rm -rf ~`` 也算命中」。
    规范化到 token 序列再比，避免 ``pytest  tests`` 与 ``pytest tests``
    因空白差异漏判。
    """
    if command_is_blocked(command)[0]:
        return False
    try:
        want = shlex.split(command)
    except ValueError:
        # 解析不了（引号不闭合等）→ 视为不匹配。**解析失败必须往严的方向走**：
        # 一个解析不了的命令，我们无从知道它会做什么。
        return False
    if not want:
        return False
    for entry in allowed:
        try:
            if shlex.split(entry) == want:
                return True
        except ValueError:
            continue
    return False


def check_command(
    command: str, *, allowlist: set[str] | None = None, denied: set[str] | None = None
) -> tuple[bool, str]:
    """闸门对一条命令的最终判定。**deny 优先于白名单。**

    顺序是刻意的：**先查 deny 地板，再查白名单**。反过来写的话，
    将来有人往白名单里加一条 deny 过的命令，就会静默放行 ——
    而 deny 的全部意义就是「即使它在别处被允许，这里也不许」。

    抄自 Hermes 的 ``approval_floors``：``_match_user_deny_rule()`` 永远
    先于 ``_command_matches_permanent_allowlist()``。
    """
    if denied:
        for entry in denied:
            try:
                if shlex.split(entry) == shlex.split(command):
                    return False, f"被显式拒绝（地板规则，优先于白名单）"
            except ValueError:
                if entry == command:
                    return False, "被显式拒绝（地板规则，优先于白名单）"
    blocked, why = command_is_blocked(command)
    if blocked:
        return False, why
    if allowlist is not None and not matches_exactly(allowlist, command):
        return False, "不在白名单内（且不是整条精确匹配）"
    return True, ""


#: 本机交互式场景的显式拒绝清单。**刻意为空** ——
#: 永久拒绝将来要人工往里加（设计文档 11.9.7「明确不做的：永久授权」）。
#: 存在的意义是 :func:`check_command` 的 deny 优先逻辑有东西可测，
#: 而不是为了配置。
DEFAULT_DENIED_COMMANDS: frozenset[str] = frozenset()

#: 会让下层 agent 自己批准自己的 argv 片段。与 :data:`BYPASS_FLAGS` 同一份，
#: 导出两个名字只是为了让「读代码的人」和「查状态的人」看到同一个真相。
PASSED_THROUGH_FLAGS = BYPASS_FLAGS

#: 审批上下文 → 策略的别名。``ApprovalContext.remote()`` 等价于
#: ``ApprovalPolicy.for_context("remote")``，但更短、也不会打错字。
_ALIASES = {"remote", "unattended", "local"}


class ApprovalContext:
    """发起一次审批时的上下文（场景 + 谁问 + 环境快照）。

    存在的理由是让**判定所需的全部输入**集中在一个对象里：
    散成三个参数传下去，就会有某条路径忘了传环境 —— 而那正是
    :func:`is_bypass_active` 漏检的路径。
    """

    __slots__ = ("name", "who", "argv", "env")

    def __init__(
        self,
        name: str,
        *,
        who: str = "unknown",
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        # 认不出的场景名**不报错**，但会被 :meth:`policy` 降级成最严档 ——
        # 报错会让「加个新场景忘了登记」变成启动失败，那比静默降级更糟？
        # 不 —— 对这条链路，静默降级更糟。所以这里让 policy() 降级，
        # 而本构造器**拒绝空名字**，因为空名字一定是 bug。
        if not name:
            raise ValueError("审批上下文的场景名不能为空")
        self.name = name
        self.who = who
        self.argv = list(argv or [])
        self.env = dict(env or {})

    @property
    def policy(self) -> ApprovalPolicy:
        return ApprovalPolicy.for_context(self.name)

    @property
    def is_bypassing(self) -> bool:
        """**唯一**的绕过态读法。调用方不许自己去翻 argv/env。"""
        return is_bypass_active(self.argv, self.env)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ApprovalContext(name={self.name!r}, who={self.who!r})"


def new_credential(prefix: str = "ap") -> str:
    """生成一个**不可猜**的凭据。

    刻意用 ``uuid4().hex`` 而不是自增或时间戳：它会出现在飞书卡片里，
    拿到它就能批准一次本地操作。它是「谁点了」的凭据，必须防猜。

    前缀保留是为了让日志和卡片一眼能看出「这是个待确认凭据」，
    不然一串十六进制谁也认不出是什么。
    """
    return f"{prefix}-{uuid.uuid4().hex}"


def _same_person(a: str, b: str) -> bool:
    """判断两个标识**是不是同一个人**。

    飞书同一个人的 ``open_id``（``ou_xxx``）与租户级 ``user_id``
    （形如 ``2d1b7bec``）是**两个不同的串**，而白名单两种都收（实测踩过，
    见 :meth:`~freeagent.feishu.sender.FeishuSender.send_approval_card` 的
    ``_receive_id_type``）。所以只按单边比，会把「他自己点自己发起的卡」
    判成越权 —— 那种误拒比不拦更糟：它让人以为闸门坏了。

    做法：**双向后缀匹配** —— 两边各自的**尾部**相同就算同一人。
    租户级 ``user_id`` 是 ``open_id`` 的后缀，这是飞书 id 的实际构造。

    刻意**不做**前缀比对（``ou_`` vs 裸串）：那只能区分类型、
    分不出是不是同一个 —— 而那正是这里要判的。
    """
    left, right = (a or "").strip(), (b or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    # 要求短的那串长度够长，避免「某个短 id 恰好是另一个的后缀」这种巧合放行。
    return len(short) >= 8 and long.endswith(short)


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """一条待确认请求。

    ``decision is None`` = 还没人答。它**不表示允许** ——
    要结论请走 :meth:`ApprovalStore.decide`。

    ``requested_by`` = **发起人**。它与 ``decided_by`` 是两回事：
    前者是「谁要求做这件事」，后者是「谁点了按钮」。
    设计文档要求「只有发起人能批」——两者分开才能查那条规则。
    """

    credential: str
    subject: str
    detail: str | None
    asked_at: datetime.datetime
    expires_at: datetime.datetime
    decision: str | None
    decided_by: str | None
    decided_at: datetime.datetime | None
    open_message_id: str | None
    requested_by: str | None = None
    #: ``"approval"``（默认，兼容历史行）或 ``"question"``。
    #:
    #: 刻意**可空且给默认值**而不是 NOT NULL：加这一列时旧行没有值，
    #: 而迁移只加列不回填（回填要写 UPDATE，那又是一条迁移）；
    #: 读侧把「空」当 ``approval``，历史行的行为因此完全不变。
    kind: str = "approval"
    #: 提问的答复正文。
    #:
    #: **库里不会出现空串** —— :meth:`ApprovalStore.answer_text` 挡掉了
    #: 空白答复。所以这里用 ``is not None`` 判「已答」是安全的。
    answer_text: str | None = None
    #: 一次问一个、逐轮积累的**权威结构**（JSON 文本）：
    #: ``{"questions": ["问题1", ...], "answers": [[], ...]}``
    #:
    #: 问题数 = ``len(questions)``，**不另存**；下一个待答 = 第一个空槽；
    #: 发给 opencode 的载荷 = ``answers``，形状天然是 ``string[][]``。
    #:
    #: 为什么不拆成 questions_json + question_count + answers_json 三列：
    #: 那是把同一份事实存三处，任何一处漂移都不太好查。**只存一次。**
    question_spec: str | None = None

    @property
    def spec(self) -> dict:
        """``question_spec`` 解开后的结构。**坏 JSON 当空结构**。

        坏 JSON 不抛：这一列是**我们自己**写进去的，而一个写坏了的
        待答项不该把整条消息处理搞崩 —— 那等于让一条坏数据
        掀翻整个桥接。它退化成「没有问题」= 没人会答，等过期清理。
        """
        import json as _json

        if not self.question_spec:
            return {"questions": [], "answers": []}
        try:
            data = _json.loads(self.question_spec)
        except (ValueError, TypeError):
            return {"questions": [], "answers": []}
        if not isinstance(data, dict):
            return {"questions": [], "answers": []}
        questions = [str(q) for q in (data.get("questions") or [])
                     if isinstance(q, str)]
        answers = [list(a) if isinstance(a, list) else []
                   for a in (data.get("answers") or [])]
        return {"questions": questions, "answers": answers}

    @property
    def is_decided(self) -> bool:
        return self.decision is not None

    @property
    def is_question(self) -> bool:
        """这条待决项是不是「等一个文本答复」。

        **空 kind 当 approval** —— 加列前的历史行没有这个值，
        而它们全是授权。若反过来把空当 question，那些行会突然开始
        拦「打字」并期待文本答复。
        """
        return self.kind == "question"

    @property
    def is_answered(self) -> bool:
        """有没有拿到答复。**授权看 decision，提问看 answer_text。**"""
        return self.answer_text is not None if self.is_question else self.is_decided

    @property
    def is_expired(self) -> bool:
        return datetime.datetime.now() >= self.expires_at

    @property
    def may_be_answered_by(self) -> str | None:
        """``None`` = 任何人都能答；否则是**唯一**有资格的人。

        ``requested_by`` 为空（旧行）时返回 ``None``（不拦）——
        加列前的历史行不该因为新规则而永远点不动。
        """
        return self.requested_by or None


class ApprovalStore:
    """待确认的读写。**不缓存** —— 每次都读库，因为另一个进程随时会写。"""

    def __init__(self, conn, clock=None) -> None:
        self._conn = conn
        self._clock = clock or datetime.datetime.now

    # -- 写入 --------------------------------------------------------------- #
    def ask(
        self,
        subject: str,
        *,
        detail: str | None = None,
        credential: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        requested_by: str | None = None,
    ) -> PendingApproval:
        """登记一条待确认请求，返回它（含凭据）。

        **同一个 subject 重复 ask 会换一个新的凭据**，旧的那条留在库里
        但已经没人会答它。这样「重试一次」是安全的，而不会出现
        「两个有效凭据同时指向同一次授权」的歧义。
        """
        cred = credential or new_credential()
        now = self._clock()
        expires = now + datetime.timedelta(seconds=ttl_seconds)
        self._conn.execute(
            "INSERT INTO pending_approvals"
            " (credential, subject, detail, asked_at, expires_at, requested_by)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (cred, subject, detail, now.isoformat(), expires.isoformat(),
             requested_by),
        )
        self._conn.commit()
        got = self.get(cred)
        if got is None:  # pragma: no cover - 刚 INSERT 的行不可能读不回来
            # 不用 ``# type: ignore`` 蒙过去：那一行会掩盖「读库逻辑坏了」
            # 这类真问题。这里显式炸，且信息里带上凭据，便于对账。
            raise RuntimeError(f"刚写入的待确认行读不回来：{cred}")
        return got

    # -- 提问：等一个**文本**答复 ----------------------------------------- #

    def request_question(
        self,
        subject: str,
        *,
        detail: str | None = None,
        questions: Sequence[str] = (),
        credential: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        requested_by: str | None = None,
    ) -> PendingApproval:
        """登记一条「agent 在提问」，等用户给文本答复。

        ``questions`` 是这次要问的**全部**问题（实测载荷里那个数组）。
        存储策略是**一次问一个**：执行器只把 ``questions[0]`` 发给用户，
        用户答完再发下一个，攒齐了才调 opencode 的 reply 端点。

        为什么不做「一次全发出去」：一条消息无法可靠地对应到多个问题 ——
        序号？分屏？都没有好办法。opencode-feishu 就是在这上面做成了
        切分 + 序号匹配，然后被我实测的**绝对路径**答复打穿
        （``C:/Users/...`` 按空白切碎后匹配不上任何 label，整条被丢弃）。

        所以 N=1（实测到的常见情形）与 N>1 走**同一条路径**，
        只是轮数不同 —— 不写两套逻辑。

        刻意**复用同一张表**而不是另开 ``pending_questions``：
        凭据、TTL、只有发起人能答、过期处理、跨进程 wait 全部现成，
        另开一张表等于把迁移、清扫、purge、索引各复制一遍，
        只为了换一个字段存放答复。

        ``detail`` 放选项之类的补充说明 —— **不是**机器 id。
        路由目标（opencode 的 ``que_…``）只登记它的执行器需要，
        留在**进程内存**里即可；写进库反而暗示它需要被共享。
        """
        cred = credential or new_credential(prefix="q")
        now = self._clock()
        expires = now + datetime.timedelta(seconds=ttl_seconds)
        import json as _json

        asked = [str(q) for q in questions if str(q).strip()]
        spec = _json.dumps(
            {"questions": asked, "answers": [[] for _ in asked]},
            ensure_ascii=False,
        )
        self._conn.execute(
            "INSERT INTO pending_approvals"
            " (credential, subject, detail, asked_at, expires_at,"
            "  requested_by, kind, question_spec)"
            " VALUES (?, ?, ?, ?, ?, ?, 'question', ?)",
            (cred, subject, detail, now.isoformat(), expires.isoformat(),
             requested_by, spec),
        )
        self._conn.commit()
        got = self.get(cred)
        if got is None:  # pragma: no cover - 刚 INSERT 的行不可能读不回来
            raise RuntimeError(f"刚写入的提问行读不回来：{cred}")
        return got

    def put_answer(
        self, credential: str, text: str, *, answered_by: str | None = None
    ) -> int | None:
        """把一段答复填进**下一个空槽**。返回填的是第几题（从 0 起）。

        返回 ``None`` 表示没填上：不是问题、已过期、全部答完、
        空白答复、或**不是发起人**。五种原因给用户看的文案不同，
        所以调用方想知道具体是哪一种时用 :meth:`can_answer` 自己判断 ——
        判定本身在这里，和 :meth:`resolve` 同一个惯例。

        每轮都重新校验发起人：第 2 轮是**另一条消息**，
        不能因为第 1 轮验过就放行。
        """
        if not text or not text.strip():
            return None                     # 空白不是答复（见类文档）
        if not self.can_answer(credential, answered_by or ""):
            return None
        item = self.get(credential)
        if item is None or not item.is_question:
            return None
        if self._clock() >= item.expires_at:
            return None
        spec = item.spec
        answers = spec["answers"]
        slot = next((i for i, a in enumerate(answers) if not a), None)
        if slot is None:
            return None                     # 全部答完，不再收
        answers[slot] = [text.strip()]
        self._write_spec(credential, spec["questions"], answers)
        # 扁平渲染：给日志与执行器的完成摘要看，**不参与判定**。
        flat = "\n".join(a[0] for a in answers if a)
        #
        # decided_by / decided_at 一并写：对提问来说这就是「谁答的、
        # 什么时候答的」。没有它，一次挂起的会话被人冒名答复也查不出来
        # —— 而设计要求「谁批的必须事后查得到」。第一版重写时漏了这两列，
        # 回归测试 ``test_answered_by_recorded`` 当场抓住。
        self._conn.execute(
            "UPDATE pending_approvals"
            " SET answer_text = ?, decided_by = ?, decided_at = ?"
            " WHERE credential = ?",
            (flat, answered_by, self._clock().isoformat(), credential),
        )
        self._conn.commit()
        return slot

    def _write_spec(
        self, credential: str, questions: Sequence[str],
        answers: Sequence[Sequence[str]],
    ) -> None:
        import json as _json

        self._conn.execute(
            "UPDATE pending_approvals SET question_spec = ? WHERE credential = ?",
            (_json.dumps({"questions": list(questions),
                          "answers": [list(a) for a in answers]},
                         ensure_ascii=False), credential),
        )
        self._conn.commit()

    def answers_of(self, credential: str) -> list[list[str]]:
        """发给 opencode 的那个载荷。**没答完就是空槽。**

        刻意不「补齐」空槽：空槽是「这一问还没答」的诚实表示，
        补成 ``[""]`` 会被当成「用户答了空的」而让 agent 拿到一个空答案。
        """
        item = self.get(credential)
        if item is None or not item.is_question:
            return []
        return [list(a) for a in item.spec["answers"]]

    def next_question(self, credential: str) -> str | None:
        """下一个该问的问题。**全答完返回 None。**

        执行器靠它决定「下一轮该发什么」；返回 None 就该收尾了。
        """
        item = self.get(credential)
        if item is None or not item.is_question:
            return None
        spec = item.spec
        for i, answer in enumerate(spec["answers"]):
            if not answer:
                if i < len(spec["questions"]):
                    return spec["questions"][i]
                return None
        return None

    def is_complete(self, credential: str) -> bool:
        """全部问题都答完了吗。"""
        item = self.get(credential)
        if item is None or not item.is_question:
            return False
        answers = item.spec["answers"]
        return bool(answers) and all(a for a in answers)

    def wait_complete(
        self,
        credential: str,
        *,
        poll_seconds: float = 0.5,
        timeout_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> list[list[str]] | None:
        """轮询等**全部**答完。**超时或过期返回 None。**

        与 :meth:`wait` 同理用轮询而不是内存原语：等待方（执行器）
        与答复方（桥接）**是两个进程**。
        """
        import time

        deadline = time.monotonic() + timeout_seconds
        while True:
            if self.is_complete(credential):
                return self.answers_of(credential)
            item = self.get(credential)
            if item is None or self._clock() >= item.expires_at:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_seconds)

    # -- 兼容：单问场景的旧读法 -------------------------------------------- #
    def text_answer(self, credential: str) -> str | None:
        """已收到的答复**扁平文本**；一个字都没收到时返回 None。

        给「N=1 就够了」的场景留的便捷读法。**判「全部答完」用
        :meth:`is_complete`** —— 这个在 N>1 时会给出误导性的真。
        """
        item = self.get(credential)
        if item is None or not item.is_question:
            return None
        return item.answer_text

    def wait_text(
        self,
        credential: str,
        *,
        poll_seconds: float = 0.5,
        timeout_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str | None:
        """等**第一段**答复。超时返回 None。

        只适合 N=1。N>1 请用 :meth:`wait_complete` ——
        否则会在第一轮就返回，剩下的问题没人等。
        """
        import time

        deadline = time.monotonic() + timeout_seconds
        while True:
            got = self.text_answer(credential)
            if got is not None:
                return got
            item = self.get(credential)
            if item is None or self._clock() >= item.expires_at:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_seconds)

    def record_card(self, credential: str, open_message_id: str) -> None:
        """记下卡片 id —— 拿到答复后要把**那张卡**改成「已允许」。

        刻意单独一个方法而不是 ``ask`` 的参数：发卡是**发卡之后**才知道
        message_id 的，而 ``ask`` 必须先于发卡执行（凭据要嵌进按钮里）。
        """
        self._conn.execute(
            "UPDATE pending_approvals SET open_message_id = ? WHERE credential = ?",
            (open_message_id, credential),
        )
        self._conn.commit()

    def resolve(
        self, credential: str, decision: Decision, *, decided_by: str | None = None
    ) -> bool:
        """写入答复。**过期的一律拒绝**，无论调用方想写什么。

        刻意不提供「强制写入过期项」的口子 —— 那种口子迟早被用来
        「就这一次」绕过，而它绕过的正是「授权有时效」这件事本身。

        过期**只在写入这一刻**判定（下面 ``decide`` 就不再看时间了）。
        所以「答复落库后」结论是定的：用户点了就是点了，之后再等多久
        去读它，答案也一样。若在这里之后还要按时间翻掉，
        就会出现「点了允许、请求方读到时却变成拒绝」—— 那是最坏的组合：
        用户明明同意了，系统却说没有。
        """
        row = self._conn.execute(
            "SELECT expires_at, decision, requested_by"
            " FROM pending_approvals WHERE credential = ?",
            (credential,),
        ).fetchone()
        if row is None:
            return False
        # **只有发起人能批**（设计文档 11.9.7 规则 2；抄自 QM 的
        # "only the person who requested this command can approve or deny it"）。
        #
        # 放在这里而不是桥接的 ``_card_action`` 里：那是**唯一**的写入口，
        # 判定与写入同处一地，才不会出现「这条路径查了、那条路径忘了」。
        #
        # 身份比对刻意用**双向后缀**匹配：飞书同一个人的 ``open_id`` 与
        # ``user_id`` 是两个不同的串（实测踩过：白名单两种都收），
        # 只按单边比会把「他自己点自己发起的卡」判成越权。
        requester = row["requested_by"]
        if requester and decided_by:
            if not _same_person(requester, decided_by):
                # 返回 False = 「没写进去」。调用方据此显示「这次点击没生效」。
                return False
        if row["decision"] is not None:
            # **已决定过就不再改写。**
            #
            # 踩过的坑（实测）：这张卡片的按钮会一直留在那儿，于是用户可以
            # 点第二次、第三次。第一次「拒绝」第二次「允许」—— 决策被翻掉，
            # 一张用过的卡片变成长期有效的授权。
            #
            # 这与我给「过期」定的规则是同一条：结论在**落库那一刻**就该定死。
            # 过期不可翻，已决定更不可翻 —— 后者更严重，因为它不需要等时间。
            return False
        expires = datetime.datetime.fromisoformat(row["expires_at"])
        now = self._clock()
        if now >= expires:
            # 过期 → 落成明确的 deny，并留下「已过期」痕迹。
            # 不静默丢弃：否则调用方会以为「没写入 = 还在等」而一直等。
            # ``decided_by`` 记成 expired（**不记点击者**）——
            # 这次点击在时间上无效，记下是谁点的会让人误以为那次点击算数。
            self._conn.execute(
                "UPDATE pending_approvals SET decision = 'deny', decided_at = ?,"
                " decided_by = ? WHERE credential = ?",
                (now.isoformat(), "expired", credential),
            )
            self._conn.commit()
            return True
        self._conn.execute(
            "UPDATE pending_approvals SET decision = ?, decided_at = ?,"
            " decided_by = ? WHERE credential = ?",
            (decision, now.isoformat(), decided_by, credential),
        )
        self._conn.commit()
        return True

    # -- 读取 --------------------------------------------------------------- #
    def can_answer(self, credential: str, who: str) -> bool:
        """``who`` 是否有资格答这一条。**只用于给用户一句准确的话**。

        判定本身在 :meth:`resolve` 里（那是唯一写入口），这里是**同一判据的
        查询视图** —— 存在的理由是 :meth:`resolve` 对「不是发起人」与
        「已决定过」与「已过期」都只返回 ``False``，而这三件事要给用户
        显示**完全不同的**文案。把它们混成一句「先前已经 X 过了」
        会让「你没权限批这个」看起来像「这条卡坏了」。

        所以这里复算一遍（不查写路径），只为了让消息准确。
        """
        item = self.get(credential)
        if item is None:
            return False
        return item.may_be_answered_by is None or _same_person(
            item.may_be_answered_by, who
        )

    def get(self, credential: str) -> PendingApproval | None:
        row = self._conn.execute(
            "SELECT * FROM pending_approvals WHERE credential = ?", (credential,)
        ).fetchone()
        return _row_to_obj(row) if row is not None else None

    def decide(self, credential: str) -> Decision | None:
        """读结论。**这是唯一该被调用方使用的读法。**

        三种返回：

        - ``"allow"`` —— 用户点了允许
        - ``"deny"`` —— 用户点了拒绝，**或已过期**
        - ``None`` —— 还没答，仍然在有效期内
        """
        item = self.get(credential)
        if item is None:
            return None
        # 已有答复 → 直接用它。**不再按时间复核**（见 ``resolve`` 的说明）：
        # 落库那一刻判过有效期了，之后再翻掉就成了「用户点了却没生效」。
        if item.decision is not None:
            return "allow" if item.decision == "allow" else "deny"
        # 还没答 —— 现在过期了吗？过期即拒绝。
        if self._clock() >= item.expires_at:
            return "deny"
        return None

    def wait(
        self,
        credential: str,
        *,
        poll_seconds: float = 0.5,
        timeout_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> Decision:
        """轮询等答复。**超时返回 deny。**

        刻意用轮询而不是条件变量/管道：等待方和桥接**是两个进程**，
        内存里的同步原语根本跨不过去。而轮询的代价（每 0.5 秒一次
        本地 SQLite 读）可以忽略，且它天然跨进程、跨重启。
        """
        import time

        deadline = time.monotonic() + timeout_seconds
        while True:
            got = self.decide(credential)
            if got is not None:
                return got
            if time.monotonic() >= deadline:
                # 最后再问一次库：可能恰好在超时边界上答复到了。
                got = self.decide(credential)
                return got if got is not None else "deny"
            time.sleep(poll_seconds)

    # -- 委派闸门（设计文档 11.9.7，规则 1）-------------------------------- #
    def request_delegation(
        self,
        *,
        project: str,
        brief: str,
        role: str | None = None,
        context: ApprovalContext | None = None,
    ) -> PendingApproval:
        """**委派前**登记一条待确认，并返回它（含凭据）。

        这是 :mod:`freeagent.delegate` 唯一允许的入口 —— 规则 1 说
        「派发前必须有 pending 记录」，那就让**发卡之前必须先有这行记录**。
        `delegate.py` 拿到返回值里的 ``credential`` 才能去发卡。

        卡片上刻意写明三件事（项目 / 做什么 / 授权多久）：缺任何一项，
        用户就只能凭「信任机器人」点按钮 —— 那不叫确认。
        这是与 11.9.4 那张卡同一个理由，抄的是它的做法。

        ``brief`` **必须单行**：opencode 拿到带换行的提示会判定为「复杂任务」
        并升级到主 agent 的强模型、**无视** ``--model``（见 README 与 11.8）。
        所以这里**不静默改写**用户的 brief，而是原样记下、并在 detail 里
        提醒换行的后果 —— 静默截断会让「派出去的和我写的不一样」。
        """
        subject = f"委派到 {project}"
        lines = [
            f"项目：`{project}`",
            f"角色：{role or '（未指定）'}",
            f"要求：{brief}",
        ]
        if "\n" in brief.strip():
            lines.append(
                "⚠ **要求里有换行** —— opencode 会判定为复杂任务并升级到强模型、"
                "无视你指定的模型。"
            )
        policy = context.policy if context else ApprovalPolicy.for_context("remote")
        lines.append(f"授权范围：**仅这一次**（{policy.name} 档，{policy.ttl_seconds}s 内有效）")
        return self.ask(
            subject,
            detail="\n".join(lines),
            credential=new_credential("dp"),   # 前缀 dp = delegation pending
            ttl_seconds=policy.ttl_seconds,
            requested_by=context.who if context else None,
        )

    # -- 执行期闸门（设计文档 11.8.1）------------------------------------- #
    def request_tool_call(
        self,
        *,
        permission: str,
        paths: Sequence[str],
        diff: str | None = None,
        suggested_always: Sequence[str] = (),
        context: ApprovalContext | None = None,
    ) -> PendingApproval:
        """**执行期**登记一条待确认：agent 想动手了。

        与 :meth:`request_delegation` 的区别不是「早一步晚一步」，而是
        **粒度完全不同**：

        - 委派闸门问的是「**要不要**开始这件事」
        - 这一条问的是「**这一次**动作要不要做」

        所以它**一次动作一条记录**。用同一个 subject 反复 ask 会不断换凭据
        （见 :meth:`ask`），这正是我们要的 —— 每一次都重新问，
        一次批准不覆盖下一次。

        卡片上刻意写明四件事：动作类型 / 影响哪些路径 / **改了什么内容** /
        这次授权多久。**diff 是第四件里最关键的** —— 没有它，用户点的
        是「信任」而不是「确认」。

        ``diff`` 原样写入，**不截断、不改写**。截断会让「用户看到的」
        与「实际发生的」不一致，那比不显示更坏。
        """
        shown = list(paths) or ["(opencode 没给路径)"]
        lines = [
            f"动作：`{permission}`",
            "影响："
            + ("、".join(f"`{p}`" for p in shown[:5])
               + (f" 等 {len(shown)} 处" if len(shown) > 5 else "")),
        ]
        if diff:
            lines += ["", "**它想改成这样：**", "```", diff.rstrip(), "```"]
        else:
            lines += ["", "_（opencode 没给 diff —— 只能凭路径判断）_"]
        if suggested_always:
            lines += [
                "",
                f"_若选「本会话都允许」，opencode 建议的范围：_"
                f"`{ '`、`'.join(suggested_always) }`",
            ]
        policy = context.policy if context else ApprovalPolicy.for_context("remote")
        lines += [
            "",
            f"授权范围：**仅这一次动作**"
            f"（{policy.name} 档，{policy.ttl_seconds}s 内有效）。"
            "下一次动作会**再问一次**。",
        ]
        return self.ask(
            subject=f"opencode 想{permission}",
            detail="\n".join(lines),
            credential=new_credential("tp"),   # 前缀 tp = tool pending
            ttl_seconds=policy.ttl_seconds,
            requested_by=context.who if context else None,
        )

    def purge_expired(self, older_than_seconds: int = 86400) -> int:
        """清掉过期很久的行。返回删掉几条。

        不是为了正确性（过期的行读出来也是 deny），是为了不让表无限长。
        """
        cutoff = self._clock() - datetime.timedelta(seconds=older_than_seconds)
        cur = self._conn.execute(
            "DELETE FROM pending_approvals WHERE expires_at < ?", (cutoff.isoformat(),)
        )
        self._conn.commit()
        return cur.rowcount


def _row_to_obj(row) -> PendingApproval:
    return PendingApproval(
        credential=row["credential"],
        subject=row["subject"],
        detail=row["detail"],
        asked_at=datetime.datetime.fromisoformat(row["asked_at"]),
        expires_at=datetime.datetime.fromisoformat(row["expires_at"]),
        decision=row["decision"],
        decided_by=row["decided_by"],
        decided_at=(
            datetime.datetime.fromisoformat(row["decided_at"])
            if row["decided_at"]
            else None
        ),
        open_message_id=row["open_message_id"],
        # 旧库没有这一列时 row 里也没这个键 —— getattr 兜住，
        # 别让加列这件事反过来把老库读炸。
        requested_by=(
            row["requested_by"] if "requested_by" in row.keys() else None
        ),
        # 同上：旧库没有这两列时 row 里也没这些键。
        kind=(row["kind"] if "kind" in row.keys() else None) or "approval",
        answer_text=(
            row["answer_text"] if "answer_text" in row.keys() else None
        ),
        question_spec=(
            row["question_spec"] if "question_spec" in row.keys() else None
        ),
    )
