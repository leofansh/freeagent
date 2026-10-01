"""CLI 对话循环。

形态：斜杠显式命令 + 自然语言混合。
* ``/today`` ``/roles`` ``/task <id>`` 等确定动作走命令；
* 建事务、补充意图走自然语言，交由 ``LLMProvider`` 分类。

每轮循环都会先检查提醒（合并成一条摘要），这也是 V1「系统不运行就不通知」
这一局限的补偿方式。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from ..app import App, build_app
from ..domain import (
    DEFAULT_ROLE_NAME,
    FreeAgentError,
    TaskKind,
    WaitingKind,
    WaitingOn,
)
from ..services.chat import ChatReplyKind
from ..services.llm import (
    CANNOT_MUTATE_HINT,
    NOT_A_TASK_HINT,
    InputIntent,
    RoleHint,
    read_intent,
)
from ..services.llm.deepseek import DeepSeekProvider
from ..services.sorting import DISCLAIMER_MARKER, score_and_sort
from . import parse, render

__all__ = ["Repl", "main", "DEFAULT_ROLE_NAME", "KIND_ALIASES"]

#: ``DEFAULT_ROLE_NAME`` 现在**从 domain 导入**（见上面的 import），不再
#: 在这里定义。原先它只住在本文件，而 ``services/chat.py`` 也需要同一个名字
#: —— 那里去 ``from ..domain import DEFAULT_ROLE_NAME`` 拿不到，飞书里发第一
#: 条事务就 ImportError。仍然列进 ``__all__``：这是本模块原先的公开名，
#: 去掉等于破坏别人的 import。
#
#: 不能反过来让 ``chat`` 从 ``cli`` 拿 —— 那是 services 依赖 cli，分层就倒了。

#: ``/kind`` 接受的中英文写法。
KIND_ALIASES: dict[str, TaskKind] = {
    "action": TaskKind.ACTION,
    "动作": TaskKind.ACTION,
    "wait": TaskKind.WAIT,
    "等候": TaskKind.WAIT,
    "reminder": TaskKind.REMINDER,
    "提醒": TaskKind.REMINDER,
}

_USAGE: dict[str, str] = {
    "/today": "/today",
    "/all": "/all [open|closed|all]   默认 open",
    "/roles": "/roles [all]",
    "/role": "/role <名字|id>",
    "/role-add": "/role-add <名字> [备注]",
    "/role-rename": "/role-rename <名字> <新名字>",
    "/role-silence": "/role-silence <名字> [on|off]   省略=静置，on=恢复活跃",
    "/role-dod": "/role-dod <名字> <完成标准|off>",
    "/role-del": "/role-del <名字>",
    "/merge": "/merge <源角色> <目标角色>",
    "/new": "/new <角色> | <描述>",
    "/delegate": "/delegate <项目绝对路径> | <角色> | <需求>",
    "/task": "/task <id>",
    "/start": "/start <id>",
    "/pause": "/pause <id>",
    "/done": "/done <id>",
    "/drop": "/drop <id>",
    "/kind": "/kind <id> <action|wait|reminder> [等谁]",
    "/wait": "/wait <id> <等谁>",
    "/followup": "/followup <id> <日期|off>",
    "/move": "/move <id> <角色>",
    "/unrole": "/unrole <id> <角色>",
    "/dod": "/dod <id> <完成标准|off>",
    "/today-pin": "/today-pin <id> [YYYY-MM-DD|off]",
    "/note": "/note <id> <内容>",
    "/draft": "/draft <id> [要求]",
    "/revise": "/revise <id> <新内容>",
    "/artifact": "/artifact <id> [版本号]",
    "/accept": "/accept <id> [版本号]",
    "/steps": "/steps <id>",
    "/plan": "/plan <id>",
    "/tick": "/tick",
    "/llm": "/llm            查看当前智能层与配置（不显示 Key）",
    "/help": "/help",
    "/quit": "/quit",
}

_WAIT_TARGET_RE = re.compile(r"等([^，。；、]{1,10}?)(?:回复|回|答复|结果|确认|通知|消息)")

_HELP = """\
个人事务助手 · 命令
  ── 视图 ──
  /today              今天视图（含顺延标记与每条信号的理由）
  /all [open|closed|all]  全部事务（默认只列未结束）
  /roles [all]        角色脉络（默认只列活跃；all 含静置）
  /role <名字|id>      某个角色下的事务
  /task <id>          打开事务，返回恢复契约

  ── 建事务 ──
  /new <角色> | <描述> 显式创建（跳过角色推断）
  /draft <id> [要求]   让助手起一个结构化草稿骨架
  /revise <id> <内容>  局部重做（产生新版本，旧版保留）
  /artifact <id> [版本] 查看草稿版本
  /accept <id> [版本]  采纳某一版
  /steps <id>          拆成几步（只拆，不排序）
  /plan <id>           按命中信号给排期参考

  ── 状态与调度 ──
  /start <id>         开始做
  /pause <id>         等会再做
  /done <id>          做完啦
  /drop <id>          算了
  /kind <id> <形状> [等谁]  改形状：action / wait / reminder
  /wait <id> <等谁>   放一放（记下在等什么）
  /followup <id> <日期>  设跟进时间（驱动「该催了」提示）
  /today-pin <id> [日期|off]  排进今天 / 指定日期 / 移出排期
  /note <id> <内容>   记一笔进度
  /dod <id> <标准|off> 改这条事务的完成标准

  ── 委派（交给 opencode 改代码）──
  /delegate <项目绝对路径> | <角色> | <需求>
        建一条委派事务。**必须 /start 之后**执行器才会动它；
        项目路径要在 config.json 的 delegate.projects 白名单里。
        另开终端跑：python -m freeagent.delegate --dry-run

  ── 角色归属 ──
  /move <id> <角色>   只挂到这个角色
  /unrole <id> <角色> 摘掉这个角色标签
  /role-add <名字> [备注]              新建/更新角色（备注用于角色推断）
  /role-rename <名字> <新名字>
  /role-silence <名字> [on|off]        折叠/静置（数据保留）；on 恢复
  /role-dod <名字> <完成标准|off>      设该脉络默认完成标准
  /role-del <名字>                     删除（要求该角色下无事务）
  /merge <源> <目标>    合并角色（原子重定向，不丢历史）

  ── 其它 ──
  /tick               手动检查提醒
  /llm                当前智能层与配置（不显示 Key）
  /help               这份帮助
  /quit               退出

直接说一句话即可新建事务，例如：
  下周二要交的销售周报初稿，先理一版
  别忘了提醒我下午三点修窗户螺丝
  等对方回复合同条款

`<id>` 都可以只输前几位。角色推断靠角色名 + 备注里的词。
"""


def parse_delegate_args(args: list[str]) -> tuple[str, str, str]:
    """解析 ``/delegate`` 的参数，返回 ``(项目, 角色, 需求)``。

    支持两种写法，因为用户会照着文档敲：

    - ``/delegate D:/proj/myapp | 工作项目A | 加个邮箱登录``  ← 文档里写的
    - ``/delegate D:/proj/myapp 工作项目A 加个邮箱登录``

    踩过的坑：按位置取 ``args[1]`` 当角色，可文档里的 ``|`` 会被切成
    独立的一个词，于是角色取到字面量 ``"|"``、需求吞掉角色名 ——
    **照文档敲必然错**。800 多个测试全绿，因为没有一个测过命令行解析。

    **分隔符的判定规则**：一个词等于 ``|``、或以 ``|`` 开头时才算分隔符。
    刻意**不**无差别地按 ``|`` 切字符串 —— 需求里常有 ``a|b`` 这种内容
    （比如表格），按字符切会把它变成 ``a b``，需求被悄悄改写。

    缺的段返回空串，由调用方给明确提示，不猜。
    """
    segments: list[list[str]] = [[]]
    saw_separator = False
    for token in args:
        if token == "|":
            segments.append([])
            saw_separator = True
        elif token.startswith("|"):
            # 贴着的写法：``|工作项目A``。第一个词作为新段，其余留在这段。
            segments.append([])
            saw_separator = True
            segments[-1].extend(token[1:].split())
        else:
            segments[-1].extend(token.split())

    if saw_separator:
        parts = [" ".join(seg) for seg in segments if seg]
    else:
        # 没写 ``|`` 就是纯位置参数：每个词自成一「段」，
        # 需求横跨第 3 段往后（这样 ``加个 邮箱 登录`` 不会被切碎）。
        # 踩过的坑：这里原本无条件走 ``segments``，于是无分隔符时整串
        # 挤进第一段 —— 角色和需求双双变空。
        parts = [token for token in args if token]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], " ".join(parts[2:])


@dataclass(slots=True)
class _Pending:
    """等待用户回答的一次追问。"""

    text: str
    kind: str
    role_guesses: tuple


@dataclass(frozen=True, slots=True)
class _ChannelCtx:
    """这条命令来自哪个远程会话（飞书等）。

    终端发起的为 ``None``。有了它，「在飞书里派一件事、结果自己飞回来」
    才成立 —— 委派得知道往哪儿回。
    """

    chat_id: str
    sender_open_id: str = ""


class Repl:
    def __init__(
        self,
        app: App,
        out=sys.stdout,
        channel_ctx: _ChannelCtx | None = None,
    ) -> None:
        self.app = app
        self.out = out
        self._pending: _Pending | None = None
        #: 上一轮回答里列出的事务 id，按出现顺序。
        #:
        #: 问句交给 :meth:`ChatService.respond` 后，它靠这个把「第二个」
        #: 接回具体那一条。Web 那边是由**客户端**回传 ``last_items`` 的，
        #: 而 Repl 是常驻进程、自己有地方放 —— 不存的话终端和飞书里
        #: 多轮就接不上（「今天该做什么」→「第二个为什么」会掉回兜底）。
        self._last_items: list[str] = []
        #: 上一条回复附带的**只读视图选项**，供通道层（飞书）渲染按钮卡。
        #: 终端不读它 —— 终端本来就能直接敲命令，给它加按钮没有意义。
        self._last_choices: tuple[str, ...] = ()
        #: 远程来源。**刻意做成构造参数而不是全局** —— 这样「谁发起的」
        #: 是显式传入的，不会因为某个 handler 忘了传递就静默变成「来自终端」。
        self.channel_ctx = channel_ctx

    # -- 输出 --------------------------------------------------------------- #
    def _say(self, text: str = "") -> None:
        print(text, file=self.out)

    def _names(self) -> dict[str, str]:
        return render.role_names(self.app.roles.list_roles(include_merged=True))

    def _usage(self, cmd: str) -> None:
        self._say(f"用法：{_USAGE.get(cmd, cmd)}")

    # -- 主循环 ------------------------------------------------------------- #
    def run(self) -> None:
        self._say("个人事务助手原型 · 输入 /help 看命令，直接说话也行。")
        if self.app.llm_name != "RuleBasedProvider":
            self._say(f"智能层：{self.app.llm_name}（模型失败会自动退回规则推断）")
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                self._say()
                break
            if not line:
                continue
            if not self.handle(line):
                break

    def handle(self, line: str, *, check_reminders: bool = True) -> bool:
        """处理一行输入。返回 ``False`` 表示应退出循环。

        ``check_reminders=False`` 供**远程通道**（飞书等）使用：
        提醒必须由通道自己按节奏推送，不能在这里被顺带消费掉。
        原因见 :mod:`freeagent.services.channel`。
        """
        if check_reminders:
            digest = self.app.reminders.due()
            digest_text = render.render_digest(digest)
            if digest_text:
                self._say(f"[提醒] {digest_text}")
                # 说出去才记账。_say 抛异常（管道断裂、编码失败）时令牌不落库，
                # 下一轮还会提醒 —— 宁可重复也不静默丢弃（设计文档 9.2）。
                self.app.reminders.acknowledge(digest)
        if line in ("/quit", "/exit", "/q"):
            self._say("再见。")
            return False
        return self._dispatch(line)

    # -- 降级告知 ----------------------------------------------------------- #
    def _report_degradation(self) -> None:
        """模型不可用时**如实告知**，不静默劣化。

        用户必须知道刚才那句判断是规则猜的，不是模型说的。
        """
        reason = getattr(self.app.llm, "degraded_reason", None)
        if not reason:
            return
        self.app.llm.last_degradation = None  # 只说一次
        self._say(f"（模型不可用，已退回规则推断：{reason}）")

    # -- 派发 --------------------------------------------------------------- #
    def _dispatch(self, line: str) -> bool:
        try:
            if self._pending is not None:
                # /skip 是这次追问的合法选项，其余斜杠命令 = 放弃追问。
                # 绝不能把 "/help" 当成角色名建出一个叫 /help 的角色。
                if line.startswith("/") and line != "/skip":
                    self._pending = None
                    self._say("（已取消这次追问）")
                    return self._command(line)
                return self._handle_pending(line)
            if line.startswith("/"):
                return self._command(line)
            return self._natural(line)
        except FreeAgentError as exc:
            self._say(f"[出错] {exc}")
            return True
        finally:
            self._report_degradation()

    # -- 追问状态 ----------------------------------------------------------- #
    def _natural(self, text: str) -> bool:
        # 一句话到底想干什么，必须先分清「记事 / 提问 / 改已有事务」。
        # 不分的后果很具体：问一句会建一条垃圾事务，追问之后下一句还会
        # 被当成角色名建出角色 —— 角色是组织结构，污染代价最大。
        # 只读视图**先**问 ChatService，与 :meth:`ChatService.respond`
        # 内部是同一个判断。
        #
        # 刻意不放成「先调 read_intent 再决定要不要问」—— 那样这条防线
        # 就被绕过了：「全部未结束的事」没有疑问词，read_intent 判成
        # RECORD，于是这里根本不会去问，直接建出一条垃圾事务。
        # 实测就是这么发现的（见 tests/test_routing_eval.py）。
        view = self.app.chat.try_view(text)
        if view is not None:
            self._say(view.text)
            self._last_items = [item.task_id for item in view.items]
            return True

        intent = read_intent(text)
        if intent is InputIntent.QUESTION:
            # 问句要**回答**，不是拒绝。
            #
            # 踩过的坑（真在飞书里撞出来的）：这里原先对任何问句一律回
            # NOT_A_TASK_HINT，于是飞书里问「今天该做什么？」得到的是
            # 「查看用命令：/today 今天要动的」—— 而 /today 就写在下一行。
            # 同一句话在 Web 对话框里能正常答上来。
            #
            # 能力早就有：``ChatService`` 里是一套完整问答路由（今天/全部/
            # 在等什么/提醒/为什么/进展/某条角色），Web 走的就是它。
            # 终端与飞书经 Repl 进来，这里却自己另写了一份「一律拒绝」。
            #
            # 所以刻意**不**再写一套问句到命令的映射 —— 直接委派给同一个
            # ChatService：一份逻辑服务两个入口，才不会再次分叉。顺带
            # 拿到 suggestions 之外的多轮指代（「第二个」）。
            reply = self.app.chat.respond(text, context_ids=self._last_items)
            self._say(reply.text)
            self._last_items = [item.task_id for item in reply.items]
            # 供通道层（飞书）渲染**按钮卡**用。终端不读它 —— 终端本来就
            # 能直接敲命令，给它加按钮没有意义。
            #
            # 只在 ``CLARIFY`` 时给：那是「我没把握，请选一个」的时刻，
            # 也就是**唯一**值得花一次交互成本去问的时刻。
            # 正常回答（ANSWER/RECORDED）不给，否则飞书每条消息都变成卡片。
            self._last_choices = (
                tuple(reply.suggestions)
                if reply.kind is ChatReplyKind.CLARIFY
                else ()
            )
            return True
        if intent is InputIntent.MUTATE:
            self._say("（这句是要改已有的一条，我没有建新事务）")
            self._say(CANNOT_MUTATE_HINT)
            return True
        existing = self.app.roles.list_roles()
        if not existing:
            # 还没有任何脉络时，问「是哪个脉络」毫无意义。
            # 先解释再创建 —— 首次运行的提示顺序很重要。
            self._say(
                f"（还没有角色，先归入「{DEFAULT_ROLE_NAME}」；"
                f"以后可以 /role-rename 改名或 /merge 合并）"
            )
            role = self.app.roles.create(DEFAULT_ROLE_NAME)
            self._say(f"  已建角色脉络「{role.name}」")
            self._create(text, self.app.llm.classify(text, []).kind, [role.name])
            return True

        hints = [RoleHint(role.name, role.note) for role in existing]
        result = self.app.llm.classify(text, hints)
        if result.need_clarification:
            self._pending = _Pending(text, result.kind, result.role_guesses)
            self._say(f"{result.clarifying_question}（直接回角色名，或 /skip 跳过）")
            return True
        self._create(text, result.kind, [g.role_name for g in result.role_guesses[:1]])
        return True

    def _handle_pending(self, line: str) -> bool:
        pending = self._pending
        self._pending = None
        if line in ("/skip", "跳过"):
            role = self._ensure_role("未分类")
            self._create(pending.text, pending.kind, [role.name])
            return True
        answer_intent = read_intent(line)
        if answer_intent is not InputIntent.QUESTION:
            # 关键：角色是**组织结构**，绝不能从一句完整的话里凭空变出来。
            # 早先的 bug 是「问一句 → 被追问 → 下一句被建成同名角色」。
            # 问句挡掉了，但「提醒我下午三点修窗户螺丝」这种**任务句**
            # 照样会被建角色 —— 所以这里改成：只认**已存在**的角色，
            # 新角色必须显式 /role-add。
            role = self._match_role(line)
            if role is None:
                self._say(f"没有叫「{line.strip()}」的脉络，我没有擅自新建。")
                self._say(
                    "想开一条新脉络请用 /role-add <名字> [备注]；"
                    "先跳过用 /skip（会归到「未分类」）。"
                )
                return True
            self._create(pending.text, pending.kind, [role.name])
            return True
        self._say("（这句不是角色名，这次追问已取消，什么也没建）")
        self._say(NOT_A_TASK_HINT)
        return True

    def _match_role(self, name: str):
        """按名字/id 前缀找**可操作**的角色：排除已合并的，包含静置的。

        已合并角色不在用户的列表里（文档 3.5），所以命令层也不该按名字捞到它。
        """
        for role in self.app.roles.list_roles(include_inactive=True):
            if role.name == name:
                return role
        for role in self.app.roles.list_roles(include_inactive=True):
            if name and (name in role.name or role.name in name):
                return role
        return None

    def _find_role_by_id(self, key: str):
        for role in self.app.roles.list_roles(include_inactive=True):
            if role.id == key or role.id.startswith(key):
                return role
        return None

    def _ensure_role(self, name: str):
        existing = self.app.roles.find_by_name(name)
        if existing is not None:
            return existing
        role = self.app.roles.create(name)
        self._say(f"  新建角色脉络「{name}」")
        return role

    def _require_role(self, key: str):
        role = self._match_role(key) or self._find_role_by_id(key)
        if role is None:
            raise FreeAgentError(f"找不到角色「{key}」。/roles 看现有角色。")
        return role

    def _resolve_id(self, partial: str) -> str:
        """允许只输 id 的前几位。"""
        for task in self.app.task_repo.list_all():
            if task.id == partial or task.id.startswith(partial):
                return task.id
        raise FreeAgentError(f"找不到事务 {partial}")

    # -- 自然语言建事务 ----------------------------------------------------- #
    def _create(self, text: str, kind: str, role_names: list[str]) -> None:
        today = self.app.clock.today()
        scheduled = parse.parse_date_expr(text, today)
        # 标题保留「下周二」这类时间信息 —— 那是标题的一部分，不是噪声
        title = self.app.llm.refine_title(text)

        role_ids: list[str] = []
        for name in role_names:
            role = self._match_role(name) or self._ensure_role(name)
            role_ids.append(role.id)
        if not role_ids:
            role_ids = [self._ensure_role("未分类").id]

        task_kind = TaskKind(kind)
        waiting_on = None
        intent = self._guess_intent(text)
        if task_kind is TaskKind.WAIT:
            waiting_on = WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what=self._guess_wait_target(text),
                since=self.app.clock.now(),
            )

        due = None
        reminder = None
        parsed_time = parse.parse_time_expr(text, scheduled or today)
        if parsed_time is not None:
            if task_kind is TaskKind.REMINDER:
                reminder = parsed_time
            else:
                due = parsed_time
        # 「下午三点提醒我」没说哪一天 —— 对提醒来说就是今天
        if scheduled is None and task_kind is TaskKind.REMINDER and reminder is not None:
            scheduled = reminder.date()

        task = self.app.tasks.create(
            title,
            role_ids,
            kind=task_kind,
            intent=intent,
            scheduled_for=scheduled,
            due_time=due,
            reminder_time=reminder,
            waiting_on=waiting_on,
        )
        self._say()
        self._say(f"已记下：{task.title}")
        self._say(
            f"  形状 {task.kind.label}｜状态 {task.state.label}"
            f"｜角色 {'、'.join(self._names().get(r, '?') for r in task.role_ids)}"
        )
        if intent is None:
            self._say("  意图还没说清楚 —— 说一句「想做到什么程度」我帮你补上。")
        if task_kind is TaskKind.WAIT and waiting_on is not None:
            self._say(f"  在等：{waiting_on.who_or_what}")
        if reminder is not None:
            self._say(f"  提醒时间：{reminder:%Y-%m-%d %H:%M}")
        if due is not None:
            self._say(f"  截止时间：{due:%Y-%m-%d %H:%M}")
        if scheduled is not None:
            self._say(f"  已排到 {scheduled}")
        self._say(f"  id={task.id}")
        self._say()
        self._say(f"  /draft {task.id[:8]} 出个骨架，/steps {task.id[:8]} 拆步骤。")

    def _guess_intent(self, text: str) -> str | None:
        for marker in ("先理一版", "先出个初稿", "先整一版"):
            if marker in text:
                return marker
        return None

    def _guess_wait_target(self, text: str) -> str:
        m = _WAIT_TARGET_RE.search(text)
        if m:
            return m.group(1).strip() or "对方"
        if "审批" in text:
            return "审批"
        return "对方"

    # -- 命令派发 ----------------------------------------------------------- #
    def _command(self, line: str) -> bool:
        parts = line.split()
        cmd = parts[0]
        args = parts[1:]
        handler = self._handlers().get(cmd)
        if handler is None:
            self._say(f"未知命令 {cmd}。/help 看命令列表。")
            return True
        handler(args)
        return True

    def _handlers(self) -> dict[str, Callable[[list[str]], None]]:
        return {
            "/help": self._cmd_help,
            "/today": self._cmd_today,
            "/all": self._cmd_all,
            "/roles": self._cmd_roles,
            "/role": self._cmd_role,
            "/role-add": self._cmd_role_add,
            "/role-rename": self._cmd_role_rename,
            "/role-silence": self._cmd_role_silence,
            "/role-dod": self._cmd_role_dod,
            "/role-del": self._cmd_role_del,
            "/merge": self._cmd_merge,
            "/new": self._cmd_new,
            "/delegate": self._cmd_delegate,
            "/task": self._cmd_task,
            "/start": self._cmd_start,
            "/pause": self._cmd_pause,
            "/done": self._cmd_done,
            "/drop": self._cmd_drop,
            "/kind": self._cmd_kind,
            "/wait": self._cmd_wait,
            "/followup": self._cmd_followup,
            "/move": self._cmd_move,
            "/unrole": self._cmd_unrole,
            "/dod": self._cmd_dod,
            "/today-pin": self._cmd_pin,
            "/note": self._cmd_note,
            "/draft": self._cmd_draft,
            "/revise": self._cmd_revise,
            "/artifact": self._cmd_artifact,
            "/accept": self._cmd_accept,
            "/steps": self._cmd_steps,
            "/plan": self._cmd_plan,
            "/tick": self._cmd_tick,
            "/llm": self._cmd_llm,
        }

    # ---- 视图 ---- #
    def _cmd_help(self, args: list[str]) -> None:
        self._say(_HELP)

    def _cmd_today(self, args: list[str]) -> None:
        view = self.app.today.view()
        self._say(render.render_today(view, self._names()))

    def _cmd_all(self, args: list[str]) -> None:
        mode = args[0] if args else "open"
        if mode not in ("open", "closed", "all"):
            raise FreeAgentError("用法：/all [open|closed|all]")
        tasks = self.app.task_repo.list_all()
        if mode == "open":
            tasks = [t for t in tasks if t.is_open]
        elif mode == "closed":
            tasks = [t for t in tasks if not t.is_open]
        now, today = self.app.clock.now(), self.app.clock.today()
        scored = score_and_sort(
            tasks,
            now,
            today,
            dependents_of=self.app.task_repo.count_dependents,
            energy_windows=self.app.energy_windows,
        )
        self._say(render.render_all(scored, self._names(), scope=mode))

    def _cmd_roles(self, args: list[str]) -> None:
        """默认只列活跃角色 —— 原则 5：静置的不该一直占屏。"""
        show_all = "all" in args
        roles = self.app.roles.list_roles(include_inactive=show_all)
        self._say(render.render_roles(roles, self._names()))
        if not show_all:
            hidden = len(self.app.roles.list_roles(include_inactive=True)) - len(roles)
            if hidden:
                self._say(f"（另有 {hidden} 个静置角色，/roles all 一并查看）")

    def _cmd_role(self, args: list[str]) -> None:
        if not args:
            self._usage("/role")
            return
        role = self._require_role(args[0])
        self._say(render.render_role_detail(role, self.app.roles.tasks_in(role.id), self._names()))

    # ---- 其它 ---- #
    def _cmd_llm(self, args: list[str]) -> None:
        """查看当前智能层。**绝不打印 API Key。**"""
        config = self.app.config
        self._say(f"当前智能层：{self.app.llm_name}")
        if config is None:
            self._say("（未加载配置）")
            return
        for key, value in config.describe().items():
            self._say(f"  {key}：{value}")
        if isinstance(self.app.llm, DeepSeekProvider):
            self._say("  兜底：规则层（模型失败时自动降级）")
        else:
            self._say("  说明：没配置 API Key，当前是纯规则推断。")
            self._say("       设 DEEPSEEK_API_KEY 环境变量后重启即可接真实模型。")
        degraded = getattr(self.app.llm, "degraded_reason", None)
        if degraded:
            self._say(f"  最近降级：{degraded}")

    def _cmd_tick(self, args: list[str]) -> None:
        digest = self.app.reminders.due()
        text = render.render_digest(digest)
        if not text:
            self._say("没有到点的提醒。")
            return
        self._say(text)
        # 确认放在 _say 之后：写不出去就别记账，否则这条提醒会被永久
        # 标记成已送达而用户从没看到（设计文档 9.2）。
        self.app.reminders.acknowledge(digest)

    # ---- 角色管理 ---- #
    def _cmd_role_add(self, args: list[str]) -> None:
        if not args:
            self._usage("/role-add")
            return
        name = args[0]
        note = " ".join(args[1:]).strip() or None
        existing = self._match_role(name)
        if existing is not None and existing.name == name:
            updated = self.app.roles.set_note(existing.id, note)
            self._say(f"已更新「{updated.name}」的备注：{note}")
        else:
            created = self.app.roles.create(name, note=note)
            self._say(
                f"已建角色「{created.name}」"
                + (f"，备注：{note}（用于角色推断）" if note else "")
            )

    def _cmd_role_rename(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/role-rename")
            return
        role = self._require_role(args[0])
        updated = self.app.roles.rename(role.id, " ".join(args[1:]))
        self._say(f"「{role.name}」已改名为「{updated.name}」")

    def _cmd_role_silence(self, args: list[str]) -> None:
        if not args:
            self._usage("/role-silence")
            return
        role = self._require_role(args[0])
        # 命令叫「静置」，所以默认是静置；on 表示恢复活跃。
        flag = args[1] if len(args) > 1 else "off"
        if flag not in ("on", "off"):
            raise FreeAgentError("用法：/role-silence <名字> [on|off]   省略=on 恢复，off=静置")
        updated = self.app.roles.set_active(role.id, flag == "on")
        state = (
            "活跃（重新出现在默认列表）"
            if updated.active
            else "静置（数据保留，不再出现在默认列表）"
        )
        self._say(f"「{updated.name}」现在是 {state}")

    def _cmd_role_dod(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/role-dod")
            return
        role = self._require_role(args[0])
        dod = " ".join(args[1:])
        updated = self.app.roles.set_default_definition_of_done(
            role.id, None if dod == "off" else dod
        )
        self._say(
            f"「{updated.name}」的默认完成标准："
            f"{updated.default_definition_of_done or '（已清空）'}"
        )

    def _cmd_role_del(self, args: list[str]) -> None:
        if not args:
            self._usage("/role-del")
            return
        role = self._require_role(args[0])
        count = len(self.app.roles.tasks_in(role.id))
        if count:
            raise FreeAgentError(
                f"「{role.name}」下还有 {count} 条事务，不能删。"
                f"先 /merge {role.name} <别的角色>，或用 /move 逐条改归属。"
            )
        self.app.roles.delete(role.id)
        self._say(f"已删除角色「{role.name}」")

    def _cmd_merge(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/merge")
            return
        src = self._require_role(args[0])
        dst = self._require_role(args[1])
        report = self.app.roles.merge(src.id, dst.id)
        if report.absorbed:
            self._say(f"「{report.source_name}」已经并入「{report.target_name}」了。")
            return
        self._say(
            f"「{report.source_name}」已并入「{report.target_name}」，"
            f"重指向 {len(report.moved_tasks)} 条事务（原数据保留，可回看）"
        )

    # ---- 建事务 ---- #
    def _cmd_new(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/new")
            return
        role_name, desc = args[0], " ".join(args[1:])
        if desc.startswith("|"):
            desc = desc[1:].strip()
        if not desc:
            self._say("描述不能为空：/new <角色> | <描述>")
            return
        self._create(desc, "action", [role_name])

    def _cmd_delegate(self, args: list[str]) -> None:
        """建一条**委派**事务：交给 opencode 在指定项目里完成。

        刻意放在终端而不是界面上。委派链路的终点是本地代码执行，
        所以「谁能创建委派」必须是显式动作，而不是表单里的一个字段。
        """
        from ..services.delegate import check_project_allowed

        raw_project, role_name, requirement = parse_delegate_args(args)
        if not raw_project or not role_name or not requirement:
            self._usage("/delegate")
            self._say(
                "格式：/delegate <项目绝对路径> | <角色> | <需求>\n"
                "例：  /delegate D:/proj/myapp | 工作项目A | 加个邮箱登录\n"
                "（`|` 只是分隔符，敲不敲都行；也可用空格分隔）"
            )
            return

        try:
            project = check_project_allowed(self.app.config.delegate_policy(), raw_project)
        except FreeAgentError as exc:
            self._say(f"[拒绝] {exc}")
            return
        role = self._match_role(role_name)
        if role is None:
            self._say(
                f"没有叫「{role_name}」的脉络。委派要归属到已有脉络，"
                "先 /role-add 建一个。"
            )
            return

        task = self.app.tasks.create(
            requirement,
            [role.id],
            kind=TaskKind.ACTION,
            intent=f"在 {project} 里完成：{requirement}",
            project_path=str(project),
            # 从飞书发起的，**记下是哪个会话** —— 执行器靠它把结果推回去，
            # 闭环才合得上。终端发起的为 None，结果只进产物链（终端本来就能看）。
            delegate_chat_id=(
                self.channel_ctx.chat_id if self.channel_ctx else None
            ),
            # 「谁发起的」—— 与上面那个「发到哪」取自**同一个** channel_ctx，
            # 所以两者天然一致：不会出现「A 发起的、
            # 结果却推给 B 的会话」。终端发起时 channel_ctx 是 None。
            delegate_requested_by=(
                (self.channel_ctx.sender_open_id or None)
                if self.channel_ctx else None
            ),
        )
        self._say(f"已建委派事务 {task.id[:8]}")
        self._say(f"  项目：{project}")
        self._say(f"  脉络：{role.name}")
        self._say("")
        self._say("它现在是「待办」，执行器不会碰它。")
        self._say(f"确认要派的话：/start {task.id[:8]}")
        self._say("然后另开一个终端跑：python -m freeagent.delegate --dry-run")

    def _cmd_task(self, args: list[str]) -> None:
        if not args:
            self._usage("/task")
            return
        view = self.app.restore.open_task(self._resolve_id(args[0]))
        self._say(render.render_task(view, self._names()))

    # ---- 状态与调度 ---- #
    def _cmd_start(self, args: list[str]) -> None:
        self._transition(args, "/start", lambda tid: self.app.tasks.start(tid))

    def _cmd_pause(self, args: list[str]) -> None:
        self._transition(args, "/pause", lambda tid: self.app.tasks.pause(tid))

    def _cmd_done(self, args: list[str]) -> None:
        self._transition(args, "/done", lambda tid: self.app.tasks.complete(tid))

    def _cmd_drop(self, args: list[str]) -> None:
        self._transition(args, "/drop", lambda tid: self.app.tasks.drop(tid))

    def _transition(
        self, args: list[str], cmd: str, action: Callable[[str], object]
    ) -> None:
        if not args:
            self._usage(cmd)
            return
        task = action(self._resolve_id(args[0]))  # type: ignore[arg-type]
        self._say(f"{task.title} → {task.state.label}")  # type: ignore[attr-defined]

    def _cmd_kind(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/kind")
            return
        task_id = self._resolve_id(args[0])
        key = args[1].lower()
        kind = KIND_ALIASES.get(key) or KIND_ALIASES.get(args[1])
        if kind is None:
            raise FreeAgentError("形状只能是 action/wait/reminder（或 动作/等候/提醒）")
        waiting = None
        if kind is TaskKind.WAIT:
            target = " ".join(args[2:]).strip()
            if not target:
                current = self.app.tasks.get(task_id)
                if current.waiting_on is None:
                    raise FreeAgentError(
                        "改成等候类要说在等什么：/kind <id> wait 客户回复"
                    )
                target = current.waiting_on.who_or_what
            waiting = WaitingOn(
                kind=WaitingKind.PERSON, who_or_what=target, since=self.app.clock.now()
            )
        task = self.app.tasks.set_kind(task_id, kind, waiting_on=waiting)
        self._say(f"{task.title} → {task.kind.label}｜{task.state.label}")

    def _cmd_wait(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/wait")
            return
        task = self.app.tasks.block(
            self._resolve_id(args[0]),
            WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what=" ".join(args[1:]),
                since=self.app.clock.now(),
            ),
            reason="放一放",
        )
        self._say(f"已放一放：{task.title}（等 {task.waiting_on.who_or_what}）")  # type: ignore[union-attr]

    def _cmd_followup(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/followup")
            return
        task_id = self._resolve_id(args[0])
        raw = " ".join(args[1:])
        if raw == "off":
            task = self.app.tasks.set_followup(task_id, None)
            self._say(f"{task.title}：已取消跟进时间")
            return
        moment = self._parse_moment(raw)
        task = self.app.tasks.set_followup(task_id, moment)
        self._say(f"{task.title}：{moment:%Y-%m-%d %H:%M} 该跟进")

    def _parse_moment(self, raw: str) -> datetime:
        """把「明天下午三点」「3天后」这类文本解析成时刻。"""
        today = self.app.clock.today()
        day = parse.parse_date_expr(raw, today)
        clock_time = parse.parse_time_expr(raw, day or today)
        if clock_time is not None:
            return clock_time if day is None else clock_time.replace(
                year=day.year, month=day.month, day=day.day
            )
        if day is not None:
            return datetime.combine(day, time(9, 0))
        raise FreeAgentError(f"看不懂的时间：{raw}")

    def _cmd_move(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/move")
            return
        task_id = self._resolve_id(args[0])
        role = self._require_role(" ".join(args[1:]))
        task = self.app.tasks.move_to_role(task_id, role.id)
        self._say(f"{task.title} 现在只属于「{role.name}」")

    def _cmd_unrole(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/unrole")
            return
        task_id = self._resolve_id(args[0])
        role = self._require_role(" ".join(args[1:]))
        task = self.app.tasks.remove_role(task_id, role.id)
        names = "、".join(self._names().get(r, "?") for r in task.role_ids)
        self._say(f"{task.title} 现在的角色：{names}")

    def _cmd_dod(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/dod")
            return
        task_id = self._resolve_id(args[0])
        dod = " ".join(args[1:])
        task = self.app.tasks.set_definition_of_done(
            task_id, None if dod == "off" else dod
        )
        self._say(f"{task.title} 的完成标准：{task.definition_of_done or '（清空）'}")

    def _cmd_pin(self, args: list[str]) -> None:
        if not args:
            self._usage("/today-pin")
            return
        task_id = self._resolve_id(args[0])
        if len(args) == 1:
            today = self.app.clock.today()
            task = self.app.tasks.schedule(task_id, today)
            self._say(f"{task.title} 已排到 {today}")
            return
        if args[1] in ("off", "none", "-"):
            task = self.app.tasks.unschedule(task_id)
            self._say(f"{task.title} 已移出排期（状态仍为 {task.state.label}）")
            return
        day = self._parse_day(" ".join(args[1:]))
        task = self.app.tasks.schedule(task_id, day)
        self._say(f"{task.title} 已排到 {day}")

    def _parse_day(self, raw: str) -> date:
        day = parse.parse_date_expr(raw, self.app.clock.today())
        if day is not None:
            return day
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise FreeAgentError(f"看不懂的日期：{raw}") from exc

    def _cmd_note(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/note")
            return
        task = self.app.tasks.note(self._resolve_id(args[0]), " ".join(args[1:]))
        self._say(f"已记：{task.title}")

    # ---- 草稿与助手产出 ---- #
    def _cmd_draft(self, args: list[str]) -> None:
        if not args:
            self._usage("/draft")
            return
        task_id = self._resolve_id(args[0])
        task = self.app.tasks.get(task_id)
        instruction = " ".join(args[1:]).strip()
        content = self.app.llm.draft(render.to_task_ref(task), instruction)
        artifact = self.app.artifacts.create_draft(task_id, task.title, content)
        self._say(f"已出草稿 version {artifact.version}（{task.title}）")
        self._say("")
        for line in content.splitlines():
            self._say(f"  {line}")
        self._say("")
        self._say(f"  圈改后 /revise {task_id[:8]} <新内容>，满意就 /accept {task_id[:8]}")

    def _cmd_revise(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/revise")
            return
        task_id = self._resolve_id(args[0])
        content = " ".join(args[1:])
        if self.app.artifacts.current(task_id) is None:
            raise FreeAgentError(
                f"还没有草稿可改，先 /draft {args[0]}"
            )
        artifact = self.app.artifacts.revise(task_id, content)
        self._say(f"已产生 version {artifact.version}（旧版保留，可回看）")
        for line in content.splitlines()[:20]:
            self._say(f"  {line}")

    def _cmd_artifact(self, args: list[str]) -> None:
        if not args:
            self._usage("/artifact")
            return
        task_id = self._resolve_id(args[0])
        versions = self.app.artifacts.list_for_task(task_id)
        if not versions:
            self._say("这条事务还没有草稿。/draft 起一个骨架。")
            return
        if len(args) > 1:
            try:
                wanted = int(args[1])
            except ValueError as exc:
                raise FreeAgentError("版本号必须是整数") from exc
            match = next((a for a in versions if a.version == wanted), None)
            if match is None:
                raise FreeAgentError(
                    f"没有 version {wanted}（现有 1..{versions[-1].version}）"
                )
            self._say(render.render_artifact(match))
            return
        self._say(render.render_artifact_list(versions))

    def _cmd_accept(self, args: list[str]) -> None:
        if not args:
            self._usage("/accept")
            return
        task_id = self._resolve_id(args[0])
        if len(args) > 1:
            try:
                wanted = int(args[1])
            except ValueError as exc:
                raise FreeAgentError("版本号必须是整数") from exc
            artifact = self.app.artifact_repo.get_by_version(task_id, wanted)
            if artifact is None:
                raise FreeAgentError(f"没有 version {wanted}")
        else:
            artifact = self.app.artifacts.current(task_id)
            if artifact is None:
                raise FreeAgentError("还没有草稿可采纳，先 /draft")
        accepted = self.app.artifacts.accept(artifact.id)  # type: ignore[union-attr]
        self._say(f"已采纳 version {accepted.version}")  # type: ignore[union-attr]

    def _cmd_steps(self, args: list[str]) -> None:
        if not args:
            self._usage("/steps")
            return
        task = self.app.tasks.get(self._resolve_id(args[0]))
        steps = self.app.llm.split_steps(render.to_task_ref(task))
        self._say(f"{task.title} 拆成 {len(steps)} 步（只拆，不排序）：")
        for index, step in enumerate(steps, start=1):
            self._say(f"  {index}. {step}")

    def _cmd_plan(self, args: list[str]) -> None:
        if not args:
            self._usage("/plan")
            return
        task = self.app.tasks.get(self._resolve_id(args[0]))
        now, today = self.app.clock.now(), self.app.clock.today()
        from freeagent.services.sorting import score_task

        scored = score_task(
            task,
            now,
            today,
            dependents=self.app.task_repo.count_dependents(task.id),
            energy_windows=self.app.energy_windows,
        )
        if not scored.signals:
            self._say(f"{task.title}：没有命中任何提示信号，何时做由你决定。")
            return
        for sig in scored.signals:
            self._say(f"  · {sig.reason}（权重 {sig.weight}）")
        text = self.app.llm.suggest_schedule(
            render.to_task_ref(task), render.to_signal_refs(scored)
        )
        self._say("")
        self._say(render.render_suggestion(text))


def _force_utf8_streams() -> None:
    """把标准输入输出切到 UTF-8。

    中文 Windows 的控制台默认 OEM 代码页（GBK）。交互式输入时 Python 走
    console API 通常没问题，但一旦 stdin 被**重定向**（管道、文件）就会按
    locale 解码而报 ``UnicodeDecodeError``。这里统一重配。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - 极少数不可重配的流
            pass


def main(argv: list[str] | None = None) -> int:
    """入口。``--db <path>`` 可指定数据库文件。"""
    _force_utf8_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    db_path: Path | None = None
    if "--db" in args:
        index = args.index("--db")
        if index + 1 >= len(args):
            print("用法：--db <path>")
            return 2
        db_path = Path(args[index + 1])
    repl = Repl(build_app(db_path))
    try:
        repl.run()
    finally:
        repl.app.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
