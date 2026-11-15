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
    ORIGIN_SELF,
    FreeAgentError,
    NotFoundError,
    STATE_LABELS,
    TaskKind,
    ValidationError,
    WaitingKind,
    WaitingOn,
)
from ..services.chat import ChatReplyKind
from ..services.memory import (
    DEFAULT_REVIEW_DAYS,
    DEFAULT_USAGE_DAYS,
    GLOBAL_SCOPE,
    MemoryMinimalPair,
    role_scope,
)
from ..services.llm import (
    CANNOT_MUTATE_HINT,
    NOT_A_TASK_HINT,
    InputIntent,
    RoleHint,
    read_intent,
)
from ..services.llm.deepseek import DeepSeekProvider
from ..services.sorting import DISCLAIMER_MARKER, score_and_sort
from ..services.weigh import weigh
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
    "/weigh": "/weigh     代价对照：两件事撞在同一处时，把各自的代价摆出来供你拍板（只摆事实，不替你决定）\n"
    "           /weigh <序号> 记下这一板（当时那板会被冻结，7 天后与现在并排对质）",
    "/week": "/week      本周回顾：完成了什么、卡在哪、下周承诺了什么",
    "/decisions": "/decisions  决策日志：拍过哪些板，以及今天到期的那些（当时那板 vs 现在，只摆事实）",
    "/feedback": "/feedback   留痕：审批分账、委派履历、改版差异、以及没派出去的卡在哪",
    "/model": "/model      连续性个人模型：从你的记录现推的画像（节奏/关系账/承诺与目标），只读聚合，只摆事实不评价",
    "/pending": "/pending [N [标题]|drop N]   待处理收集器：收下的图片/文件；N 澄清成事务，drop N 明确忽略",
    "/capture": "/capture   批量倾倒：粘贴一大坨文本，空行结束；每行切成一条进待处理队列",
    "/mail": "/mail    邮件入口：粘贴邮件正文，空行结束；整段一条进待处理队列（首行当标题）",
    "/all": "/all [open|closed|all]   默认 open",
    "/roles": "/roles [all]",
    "/role": "/role <名字|id>",
    "/role-add": "/role-add <名字> [备注]",
    "/role-rename": "/role-rename <名字> <新名字>",
    "/role-silence": "/role-silence <名字> [on|off]   省略=静置，on=恢复活跃",
    "/role-dod": "/role-dod <名字> <完成标准|off>",
    "/role-del": "/role-del <名字>",
    "/task-del": "/task-del <id> confirm   删事务（不可恢复；先输一次只看后果）",
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
    # 记忆（设计文档 3.7）。用法文本的值**必须以命令名开头**（test_doc_contract 会查）。
    "/remember": "/remember <内容>    记一条全局偏好（= /memory add 偏好）",
    "/memory": (
        "/memory [list [global|角色]|add <偏好|约定|修正|目标> <内容>|"
        "add-role <角色> <类别> <内容>|search <关键词>|stats [天数]|"
        "review [天数]|review confirm <id> [<id>...]|"
        "edit <id> <新内容>|del <id>]"
    ),
    # 刻意与 /note 分开成两条：/note 无校验，而这条要挡三种不许重派的情形。
    # 合成一条就等于把「用错入口静默无效」又请回来（见 _cmd_redispatch）。
    "/redispatch": "/redispatch <id> [原因]",
    # 简报是派发给执行器的唯一输入，此前人看不到它（/brief 预览，V1.103）。
    "/brief": "/brief <id>    看这条委派实发的简报与协作象限",
    # 配对码**只能由桥接签发**（它看得见发消息的人），终端只负责核销。
    "/pair": "/pair <配对码>",
    "/draft": "/draft <id> [要求]",
    "/revise": "/revise <id> <新内容>",
    "/artifact": "/artifact <id> [版本号]",
    "/accept": "/accept <id> [版本号]",
    # 恢复出厂：整仓唯一真正删数据的功能，口令必须逐字对上。
    # 与 /drop 分开成两条，而且**刻意不提供无口令快捷方式** ——
    # 「慢一点」是这个功能该有的摩擦。
    "/reset-data": "/reset-data <确认口令>",
    "/steps": "/steps <id>",
    "/plan": "/plan <id>",
    # 交互模式（设计文档 12.7.2）。**不叫 /plan** —— 那个名字已经是
    # 「拆步骤/排期参考」了，撞名的后果是静默错绑（详见 _cmd_mode_plan 的注释）。
    # 用法文本的值**必须以命令名开头**（test_doc_contract 会查）——
    # 它被拿去做「你敲了 X，正确的用法是 X …」这种回话，缺了前缀就成了
    # 一句没有主语的说明。
    "/mode-plan": "/mode-plan —— 进入规划：只沟通、不落库、可多轮",
    "/mode-build": (
        "/mode-build —— 退出规划并发确认卡（还不执行）；"
        "/mode-build ok 确认建事务、cancel 先留着、"
        "discard 计划丢掉、回正常模式"
    ),
    "/tick": "/tick",
    "/llm": "/llm            查看当前智能层与配置（不显示 Key）",
    # 对外承诺（11.19）。用法文本的值**必须以命令名开头**（test_doc_contract 会查）。
    "/promise": (
        "/promise [记 <事务id> <承诺> [关系=名字] [期限=时间]"
        "|keep <序号>|break <序号>|withdraw <序号>|postpone <序号> <日期>]"
        "  对外承诺：我答应别人的"
    ),
    "/contact": "/contact [关系名 [备注]]  关系对象：事务背后的人（可检索）",
    # 人生角色关注面（11.24 / V1.68）。用法文本的值**必须以命令名开头**
    # （test_doc_contract 会查）。关注面是持续责任，不是待办。
    "/care": (
        "/care [<角色> <关注面> [每周X|每月X号]|done <序号>]"
        "  人生角色关注面：这个角色该周期性关心的东西；到点进每日总览"
    ),
    # 催办草稿（20.1.4）。用法文本的值**必须以命令名开头**（test_doc_contract 会查）。
    "/nudge": (
        "/nudge [序号]  该催的事务；给序号就出草稿"
        "（**只铺事实、正文留白，绝不代发**）"
    ),
    # 用法文本的值**必须以命令名开头**（test_doc_contract 会查）。
    "/agents": "/agents       有哪些执行器可用、装了没、能不能派（只看不派）",
    "/ask": "/ask <问题>    问执行器一句（只读：不改文件、不建事务、不问审批；会带上你记下的记忆）",
    "/help": "/help",
    "/quit": "/quit",
}

_WAIT_TARGET_RE = re.compile(r"等([^，。；、]{1,10}?)(?:回复|回|答复|结果|确认|通知|消息)")

_RELATION_MARK = "关系="


def _split_relation(tokens: list[str]) -> tuple[list[str], str | None]:
    """把 ``关系=名字`` 从其余 token 里摘出来。

    **刻意用显式标记，不用位置猜**（与 ``/promise 记`` 同一条纪律）：
    「周三」这种既是日期又可能是人名的词，位置猜会误判；误判的代价是
    把「在等张总」记成「在等一个叫周三的关系」。

    返回 ``(其余 token, 关系名或 None)``。关系名可以为空（``关系=``），
    那表示「我要挂关系，但名字还没给」—— 调用方据此报错而不是静默忽略，
    因为静默忽略会让用户以为挂上了。
    """
    rest: list[str] = []
    name: str | None = None
    for token in tokens:
        if token.startswith(_RELATION_MARK):
            name = token[len(_RELATION_MARK):].strip() or ""
        else:
            rest.append(token)
    if name == "":
        raise FreeAgentError(
            f"{_RELATION_MARK} 后面要写关系名（例：{_RELATION_MARK}张总）"
        )
    return rest, (name or None)


def _readable_span(seconds: int) -> str:
    """把秒数说成人话。**只用于展示，不参与任何判断。**

    审批留痕里「等了多久」是个关键事实（秒回 = 他正盯着，躺满 TTL =
    根本没看见），但 ``7432`` 这种数字读不出这个差别。
    """
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


_HELP = """\
个人事务助手 · 命令
  ── 视图 ──
  /today              今天视图（含顺延标记与每条信号的理由）
  /weigh              代价对照：两件以上的事争同一天时，把各自的代价（带出处）摆出来供你拍板
                      （只摆事实不替你决定，不排序；没有真取舍时如实说没有）
  /weigh <序号>       记下这一板：冻结「当时那板」，7 天后拿出来跟现在并排对质
  /decisions          决策日志：拍过哪些板，以及今天到期的那些（当时那板 vs 现在，只摆事实）
  /week               本周回顾（完成了什么 / 卡在哪 / 下周承诺了什么，只摆事实）
  /feedback           留痕：审批分账（把「没人看」从「拒绝」里拆开）、委派履历、
                      改版差异、以及没派出去的各自卡在哪一步
  /model              连续性个人模型：从记录现推的规律（完成时段 / 常顺延 / 卡住的等候 / 角色活跃度，只摆事实）
  /pending [N|drop N]  待处理收集器：飞书收下的图片/文件，列出来、一键澄清成事务
  /capture            批量倾倒：粘贴一大坨文本，空行结束，每行进待处理队列
  /mail               邮件入口：粘贴邮件正文，空行结束，整段一条进待处理队列（首行当标题）
  /promise [记|keep|break|withdraw|postpone]  对外承诺：我答应别人的（只显式记录，不自动推断）
  /contact [名字 [备注]]  关系对象：事务背后的人（与角色脉络是两个维度）
  /nudge [序号]       该催的事务；给序号出草稿（只铺事实、正文留白，绝不代发）
  /care [<角色> <关注面> [每周X|每月X号]|done <序号>]  人生角色关注面：该周期性关心的东西，到点进每日总览
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

  ── 交互模式（设计文档 12.7.2）──
  /mode-plan           进入规划：只沟通、不落库、可多轮
  /mode-build          退出规划并出确认卡（还不执行）：
                       ok 确认建事务｜cancel 先留着｜discard 计划丢掉、回正常模式

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
        建一条委派事务。必须 /start 之后执行器才会动它；
        项目路径要在 config.json 的 delegate.projects 白名单里。
        另开终端跑：python -m freeagent.delegate --dry-run
  /redispatch <id> [原因]
        让这条委派可以「重新派发」。「派发前就被拒」（白名单不符 / 没闸门）
        不会自动重试 —— 改配置前重试多少次都是同一个结果。
        改好配置后跑这条，执行器下次扫到就会再派一次。
  /brief <id>
        看这条委派**实发的那份简报**（逐字，含协作象限）：
        象限告诉你「还说少了什么」，正文告诉你「它已经知道什么」。

  ── 记忆（设计文档 3.7）──
  /remember <内容>          记一条全局偏好（= /memory add 偏好 <内容>）
  /memory list [global|角色] 列出记过什么（全貌）
  /memory add <类别> <内容>  记一条：类别是 偏好 / 约定 / 修正 / 目标
  /memory add-role <角色> <类别> <内容>   记到某个角色名下
  /memory search <关键词>    检索记忆（返回「怎么找到的」，可反驳）
  /memory stats [天数]       哪些记忆好久没用了（默认近 90 天，只摆事实不建议删）
  /memory review [天数]       哪些记忆好久没确认了（默认近 90 天；列候选，不自动删）
  /memory review confirm <id>...  确认这条还留着（可一次多条；删仍用 /memory del）
  /memory edit <id> <新内容> 改内容（类别与范围不可改）
  /memory del <id>          删掉这条

  ── 角色归属 ──
  /move <id> <角色>   只挂到这个角色  /unrole <id> <角色> 摘掉这个角色标签
  /role-add <名字> [备注]              新建/更新角色（备注用于角色推断）
  /role-rename <名字> <新名字>
  /role-silence <名字> [on|off]        折叠/静置（数据保留）；on 恢复
  /role-dod <名字> <完成标准|off>      设该脉络默认完成标准
  /role-del <名字>                     删除（要求该角色下无事务）
  /task-del <id> confirm               删除事务（不可恢复；先输一次只看后果）
  /merge <源> <目标>    合并角色（原子重定向，不丢历史）

  ── 其它 ──
  /tick               手动检查提醒
  /llm                当前智能层与配置（不显示 Key）
  /agents             执行器探针：装了没、什么版本、能不能派（只看不派）
  /ask <问题>         问执行器一句就回来（只读：不改文件、不建事务、不问审批；会带上你记下的记忆）
  /pair <配对码>      把飞书某个人加进白名单（码来自 bot 私聊发的那张卡）
  /reset-data <确认口令>
                      「恢复出厂」：清空全部事务数据（角色脉络/配置/Key 保留），
                      执行前强制自动备份。口令不带时只打印说明、不执行。
                      执行器或桥接在跑时会拒绝。慎用。
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


#: 交互模式（设计文档 12.7.2）。
#:
#: ``plan`` = 只沟通、零副作用、可多轮累积；``build`` = 正常执行。
#:
#: 刻意只用两个字面量、**不引入第三个模式**：本项目反复吃过「多加一个状态就
#: 多一处会漂移」的亏（状态文件假绿灯、待批队列、Plan 状态三层各说各话）。
MODE_PLAN = "plan"
MODE_BUILD = "build"


#: 通道标识。**不同的通道有不同的送达方式**，所以它是一个维度而不是装饰
#: （设计文档 11.11）。
#:
#: 为什么必须显式区分：``delegate_chat_id`` 是「回推目的地」，而它的值来自
#: ``channel_ctx.chat_id``。在只有飞书一个通道时，那个字段里装的**总是**
#: 一个真会话 ID，于是「能不能推」这个问题不需要问。多一个 WEB 通道之后，
#: 往同一个列里塞进去的是常量 ``"web"`` —— 它**不是会话 ID**，库里于是多了
#: 一条假地址，而执行器会拿着它去发消息（实测症状：一条指向错误方向的
#: 「缺少 FEISHU_APP_ID」警告）。
CHANNEL_TERMINAL = "terminal"
CHANNEL_WEB = "web"
CHANNEL_FEISHU = "feishu"

#: **能主动推送**的通道。终端的 ``None`` 与 WEB 的请求-响应都不在其中 ——
#: 它们的结果靠「落库 + 下次拉取」送达，没有「推」这回事（11.11.2）。
#:
#: 刻意做成集合而不是 ``if ctx.channel == CHANNEL_FEISHU``：将来加微信时
#: 要改的是**一处名单**，而不是散落在各处的等值比较。
PUSH_CHANNELS = frozenset({CHANNEL_FEISHU})


@dataclass(frozen=True, slots=True)
class _ChannelCtx:
    """这条命令来自哪个通道的哪个会话（飞书 / WEB / 终端）。

    终端发起的为 ``None``。有了它，「在飞书里派一件事、结果自己飞回来」
    才成立 —— 委派得知道往哪儿回。

    :attr:`channel` 是 11.11 补的**维度**。在此之前这个类只有 ``chat_id``，
    而 ``chat_id`` 的**语义随通道变**：飞书的是一个真会话 ID
    （``oc_xxx``），WEB 的是一个常量（``"web"``）。两件事装在同一个字段里，
    调用方就无法回答「这个地址能不能推」—— 于是唯一的写法是猜。
    """

    chat_id: str
    sender_open_id: str = ""
    #: 通道标识，取 :data:`CHANNEL_TERMINAL` / :data:`CHANNEL_WEB` /
    #: :data:`CHANNEL_FEISHU`。
    #:
    #: 默认 ``terminal``：这个类**多数**构造点是终端。给默认值而不是必填，
    #: 是因为「不知道自己是哪个通道」时的正确姿态是**最保守的那个** ——
    #: 终端不推送，于是漏判的后果是「少推一次」，而不是「推错地方」。
    channel: str = CHANNEL_TERMINAL


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

        #: **委派要件**（项目名, 要做的事），半截状态。
        #:
        #: 与 ``_pending`` 并列，因为它们是同一类东西：「这个会话现在处于
        #: 什么状态」。分开存是因为**回答的问题不同**：``_pending`` 是「这条
        #: 该归哪条脉络」，本字段是「这件事到底是不是你要的」。
        self._pending_delegate: tuple[str | None, str] | None = None
        #: 当前交互模式（设计文档 12.7.2）。``build`` = 正常，``plan`` = 只沟通、
        #: **零副作用**。
        #:
        #: 刻意与 ``_pending`` 并列放在同一个对象上：它们是同一类东西——
        #: 「这个会话现在处于什么状态」。**本字段有落盘副本**：进出 Plan 时
        #: 经 :class:`~freeagent.services.plan_state.PlanStore` 写到
        #: ``plan_sessions`` 表，所以 ``ChannelService._repls`` 这个 **LRU**
        #: （``channel.py:292``）淘汰掉本对象之后，:meth:`restore_plan` 能把
        #: 它读回来。
        #:
        #: 之前这里是纯内存的，实测症状是**静默丢失**：A 进入 plan 说了两句
        #: → B 插进来挤掉 A → A 再说一句拿到的是空 ``_plan``、模式退回
        #: ``build``，且没有任何提示。报错他会重来，静默丢失他只会以为
        #: 系统记错了。
        self._mode: str = MODE_BUILD
        #: Plan 模式下累积的计划行。**只进不出，直到退出 Plan**。
        self._plan: list[str] = []
        #: 从盘上恢复 Plan 后、**还没说给用户听**的那句话（12.7.2 恢复时要说清）。
        #:
        #: 为什么不在 ``restore_plan`` 里直接 ``_say`` 完事：恢复发生在**新建
        #: Repl 的那一刻**，而那一刻的输出目标（``out``）要么还没绑到本条
        #: 消息的 buffer（飞书：``channel.py`` 在建好之后才重绑 ``repl.out``），
        #: 要么是一个**用完就丢**的临时 buffer（Web：``_web_repl`` 里那个
        #: ``io.StringIO()``）。写进去=写给了没人看的地方，与静默丢失同效。
        #: 所以改成**先记下、等真有话要回时随第一句讲出来**。
        self._resume_notice: str | None = None
        #: 已发出、**等待用户点确认**的计划快照（设计文档 12.7.2）。
        #:
        #: 非空 = 有一张确认卡在飞书里等着。刻意存**快照**而不是直接用
        #: ``_plan``：卡片上印的是发出时的计划，用户确认期间可能又说了
        #: 几句 —— 那时若无条件执行，就是批准了他**没看过的东西**，
        #: 而确认卡的全部意义就是「他核对过」。
        self._last_plan_confirm: tuple[str, ...] = ()
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
        #: 上一条回复附带的**事务动作卡**（``(task_id, title)``），供通道层发卡。
        #: 终端不读它 —— 终端能直接敲 ``/start``，加按钮没有意义。
        #:
        #: 归属与 :attr:`_last_choices` **同类**（「那一条回复的卡片」），
        #: 所以每条消息都要清（见 ``ChannelService._run``）——
        #: 粘住的后果是「下一句无关的话也长出一张动作卡」。
        self._last_task_action: tuple[str, str] | None = None
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
            # 安静窗口内 due() 返回空摘要但带 quiet_until —— 这里**也说出来**：
            # 否则终端每轮都「什么都没发生」，用户无从知道窗口把它压后了（9.8）。
            elif digest.is_quiet:
                self._say(f"[提醒] {digest.quiet_note()}")
        if line in ("/quit", "/exit", "/q"):
            self._say("再见。")
            return False
        # 恢复告知排在本条回复之前，且**只说一次**（12.7.2 恢复时要说清）。
        # 放这里而不是 handle 开头：/quit 不该附带这句话 —— 用户都要走了，
        # 告诉他「上次的规划还在」只是噪声。
        self._flush_resume_notice()
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
            if self._mode == MODE_PLAN:
                # Plan 模式下**自由文本一律不落库**（设计文档 12.7.2）。
                # 刻意在这里拦、而不是在 _natural 里判：_natural 是「记事」的正路，
                # 塞一个模式判断进去，它就同时负责两件事，而 Plan 的「零副作用」
                # 一旦漏判，用户会在规划阶段发现库里多了一条没要过的事务。
                return self._plan_say(line)
            return self._natural(line)
        except FreeAgentError as exc:
            self._say(f"[出错] {exc}")
            return True
        finally:
            self._report_degradation()

    # -- Plan 模式（设计文档 12.7.2）------------------------------------ #
    def _plan_say(self, text: str) -> bool:
        """Plan 模式下的一句话：**只沟通，不落库**。

        刻意**不**复用 :meth:`_natural`：那条路的职责是「判定后建事务」，
        混进模式判断会让 Plan 的零副作用依赖分支正确 —— 而 Plan 期多出一条
        没要过的事务，正是这个模式要杜绝的那件事。

        这里**不假装能理解**：本切片还没有 action 维度，所以只回「记下了」并
        原样复述。**说清边界比假装听懂重要** —— 08:01 那次它就是满口答应
        「我去查」，执行时才被拒。

        ## 确认卡挂着的时候不接受新内容

        ``_last_plan_confirm`` 非空 = 有一张卡在等用户点。这时候他打的**任何**
        自由文本都追加进计划，于是卡片上印的旧版与他以为要批准的内容**不一致**
        —— 而他看到的回复恰恰是「记下了」，像是成功了。

        所以这里**明确拒绝追加**并说明为什么。实测这个坑是我自己的测试撞出来的：
        「/mode-build 之后、点确认之前，随便说一句」把那句话吸进了计划。
        """
        if self._last_plan_confirm:
            self._say(
                f"（确认卡还挂着，计划仍是那 {len(self._last_plan_confirm)} 条。）\n"
                f"  你说的这句**没有**加进去 —— 否则卡上印的和实际会执行的对不上，"
                f"而你看到的回复却像成功了。\n"
                f"  先点「确认执行」或「再想想」，或者先 /mode-plan 重新规划。"
            )
            return True
        self._plan.append(text)
        self._persist_plan()
        self._say(
            f"（Plan {len(self._plan)}）记下了：{text}\n"
            f"规划中不建事务、不改文件。要动手时说 /mode-build。"
        )
        return True

    # -- Plan 状态的落盘 --------------------------------------------------- #
    def _plan_store(self):
        """Plan 状态存储。**每次现取**，不要缓存到 ``__init__``。

        缓存会在「换库」之后留下一个指向旧连接的 store，而写进旧库的
        Plan 读回来永远是空的 —— 症状是「落盘了但重启后还是丢」。
        """
        from ..services.plan_state import PlanStore

        return PlanStore(self.app.conn)

    def _persist_plan(self) -> None:
        """把当前 Plan 状态写盘。**静默失败**：存不上不该打断对话。

        刻意吞掉异常：写盘是**锦上添花**，而这一刀的全部承诺是「零副作用」——
        为存状态而报错，会让用户以为自己的话被弄丢了。存不上退化成
        「这次会话内仍然有效」，那本来就是本功能的上限。
        """
        chat_id = self.channel_ctx.chat_id if self.channel_ctx else ""
        if not chat_id:
            return  # 终端会话：没有 chat_id，无处可存
        try:
            self._plan_store().save(chat_id, self._mode, tuple(self._plan))
        except Exception:  # noqa: BLE001
            pass

    def _drop_plan_state(self) -> None:
        """把盘上的 Plan 状态删掉。**静默失败**，理由同 :meth:`_persist_plan`。

        调用方**必须先写一次终态再调它**（``_discard_plan`` / ``_confirm_plan``
        都这么做）：drop 失败时盘上留着的是「刚写的那个终态」，而不是旧计划。
        只 drop 不写的后果是「丢弃/确认之后一重启，计划又回来了」—— 那等于
        系统没照做他要求的动作，却什么也没说。
        """
        chat_id = self.channel_ctx.chat_id if self.channel_ctx else ""
        if not chat_id:
            return
        try:
            self._plan_store().drop(chat_id)
        except Exception:  # noqa: BLE001
            pass

    def restore_plan(self) -> bool:
        """从磁盘恢复本会话的 Plan。**有得恢复返回 True。**

        由 :class:`~freeagent.services.channel.ChannelService` 在**新建**
        Repl 时调用 —— 那正是 LRU 淘汰掉旧实例之后、新会话进来的时候。
        没有这一步，淘汰即静默丢失（实测过，见 ``_mode`` 的注释）。

        恢复成功时**记下**一句待说的话（``_resume_notice``），而不是在这里
        直接输出 —— 此刻的 ``out`` 还没绑到本条消息（见 ``_resume_notice``
        的注释）。真正说出来由 :meth:`handle` 在下一句回复前完成。
        """
        chat_id = self.channel_ctx.chat_id if self.channel_ctx else ""
        if not chat_id:
            return False
        try:
            saved = self._plan_store().load(chat_id)
        except Exception:  # noqa: BLE001
            return False
        if saved is None:
            return False
        if saved.mode != MODE_PLAN and not saved.lines:
            # 「build + 空计划」= 什么都值得恢复的都没有（典型来源：
            # discard/确认之后落下的终态，而删行没删掉）。这种情况**要说
            # 一句「上次的会话状态已恢复」就是噪声**，且会让用户以为有别的东西。
            return False
        self._mode = saved.mode
        self._plan = list(saved.lines)
        self._resume_notice = self._resume_text(saved)
        return True

    def _resume_text(self, saved) -> str:
        """恢复 Plan 时的那句告知（12.7.2「恢复时要说清」）。

        措辞刻意**不做假设**：只陈述盘上有什么、还处于什么模式，把「要不要
        接着规划」交回用户。说不清就等于又静默接续了一次。
        """
        if saved.mode == MODE_PLAN:
            n = len(saved.lines)
            tail = f"，已记下 {n} 条" if n else "，还没记下任何内容"
            # 「不想继续」这句必须给**真能走的**那条路（V1.91）：旧版写的是
            # 「/mode-build 或直接重说」，而无参 /mode-build 只是重新出一张
            # 确认卡 —— 照做会得到一张卡，用户更懵。
            return (f"（上次的规划还在{tail}。现在是 Plan 模式：只沟通、不落库；"
                    f"要动手时说 /mode-build；不想继续就说 /mode-build discard"
                    f"（计划丢掉、回正常模式）。）")
        # 落在盘上的也可能是 build 模式（例如写盘后用户又退出了规划）。
        # 这时**不谎称**「在规划」——照样说清、让用户知道系统记得他。
        return "（上次的会话状态已恢复。）"

    def _flush_resume_notice(self) -> None:
        """把待说的恢复告知说出去，**只说一次**。"""
        if self._resume_notice:
            self._say(self._resume_notice)
            self._resume_notice = None

    # ⚠️ 刻意**不叫** ``_cmd_plan``：本文件第 1169 行已有那个名字（拆步骤，
    # ``/plan <id>``）。Python 类体里**后定义覆盖先定义**，而我在上文先写了
    # 同名方法，于是 ``self._cmd_plan`` 解析到的是**它** —— 后果是 Plan 模式
    # 入口静默接到「拆步骤」上：不报错、不建任何东西、只回一句用法。
    # **静默的错绑比报错难查得多**，所以这里必须用不同的名字。
    def _cmd_mode_plan(self, args: list[str]) -> bool:
        self._mode = MODE_PLAN
        self._plan = []
        self._persist_plan()
        self._say("（Plan）只沟通、不落库。说完说 /mode-build 再动手。")
        return True

    def _cmd_mode_build(self, args: list[str]) -> bool:
        """退出规划。**四个形态，不新增命令**：

        - ``/mode-build`` → **发确认卡，不动手**（Plan 仍在，可继续补）
        - ``/mode-build ok`` → 确认：把计划落成事务
        - ``/mode-build cancel`` → 取消：计划留着，回到 Plan 继续沟通
        - ``/mode-build discard`` → **丢弃**：计划清空、回正常模式（V1.91）

        刻意不加 ``/confirm`` / ``/cancel`` 这两个新命令 —— 命令表每多一条，
        说明书与设计文档的**计数**就跟着漂移一次（上次加 ``/mode-*`` 时刚
        付过这个代价）。而给已有命令加参数，代价是零。

        ## 为什么必须有 ``discard``（V1.91 补的出边）

        这不是「多一个方便的参数」，是**状态机少了一条出边**。此前退出 Plan
        的唯一路径是 ``ok``，而 ``cancel`` 的语义是「计划留着、继续规划」：

        - 进了 ``/mode-plan`` 又改主意「算了不弄了」→ **没有一条命令能回到
          正常模式**，只能确认一份不想要的计划；
        - Plan 是**落盘**的（``_persist_plan`` + 启动时 ``restore_plan``），
          所以「关掉重开」也逃不掉 —— 重启后照样被恢复回 Plan；
        - 更糟的是当时恢复提示写的是「不想继续就 /mode-build 或直接重说」，
          而 ``/mode-build`` 无参只是**重新出一张确认卡**，等于提示在指错路。

        对照生态里的 ``opencode-feishu-plugin``：它每条临时态命令（``/new``
        ``/form``）都配了一条 ``/cancel`` 收场 —— 这是对的，进得去就得出得来。
        我们借它的**结论**（每个临时态必须有零副作用出口），但按本仓纪律
        **不借它的形态**（不新增命令，加参数）。
        """
        chat_id = self.channel_ctx.chat_id if self.channel_ctx else ""
        choice = args[0].strip().lower() if args else ""

        # 「丢掉计划、回正常模式」——**必须排在「没有计划」的早退之前**。
        # 否则进了 Plan 但一句没说（计划为空）时，用户连唯一的出边都走不了，
        # 那正是这个 bug 最容易被撞到的形态（进错模式、什么都没说、想退出）。
        if choice in ("discard", "abandon", "drop", "放弃", "算了"):
            return self._discard_plan(chat_id)

        if not self._plan:
            # 分不清「在 Plan 里但空手」和「本来就在 build」是旧版的两个谎之一：
            # 前者说「已经在 build 模式」是假话，而用户正卡在这里找出路。
            if self._mode == MODE_PLAN:
                self._say(
                    "（现在是 Plan 模式，还没记下任何内容 —— 你说的每句话都只"
                    "进计划、不落库。）\n"
                    "  要开始规划就说一句话；要回正常模式就说 /mode-build discard。"
                )
            else:
                self._say("（已经是正常模式，没有待确认的计划。）")
            return True

        if choice in ("cancel", "no"):
            # 计划**留着** —— 他可能只是想再想想，不是想丢掉。
            self._last_plan_confirm = ()
            self._say("（好，先留着。想继续补就说 /mode-plan "
                      "或直接再说一句。）")
            return True

        if choice in ("ok", "yes", "确认"):
            return self._confirm_plan()

        # 无参数：**发确认卡**。
        #
        # 这里**不切模式、也不清计划**：确认可能不来，也可能来「再想想」。
        # 提前切走的话，取消就回不来了。
        #
        # 快照存一份：卡片上印的是**发出时**的计划，而用户确认期间可能又
        # 说了几句 —— 那时若无条件执行，就是**批准了他没看过的东西**。
        # ``_confirm_plan`` 会拿快照与当前计划比对，不一致就要求重新确认。
        self._last_plan_confirm = tuple(self._plan)
        self._say(
            f"（build）{len(self._plan)} 条待确认。"
            "**还没有执行任何东西** —— 确认之后才动手。"
        )
        for line in self._plan:
            self._say(f"· {line}")
        return True

    def _discard_plan(self, chat_id: str) -> bool:
        """丢掉计划、退回正常模式。**这是 Plan 状态机唯一的无副作用出边。**

        ## 三条都要一起做，少一条就留个「幽灵计划」

        1. 内存：``_plan`` 清空、``_last_plan_confirm`` 清空（挂着的确认卡
           随之作废，否则用户点「确认执行」会把已经丢掉的计划建出来）；
        2. 模式：``_mode = MODE_BUILD``；
        3. 盘上：**先写**「build + 空计划」再 ``drop``。顺序不能反 ——
           写盘是幂等的、drop 可能失败，而 ``_persist_plan`` 本身**静默失败**
           （它的注释写了理由：不为存状态打断对话）。若只 drop 不写，
           drop 失败时盘上还留着旧计划，用户重启后会被「恢复」回 Plan
           模式 —— 他会以为自己那句话说错了，而系统其实**没执行他要求的
           丢弃**。先写一次空状态，最坏情况也是「盘上是真的：build、无计划」。
        """
        had = len(self._plan)
        self._plan = []
        self._last_plan_confirm = ()
        self._mode = MODE_BUILD
        self._persist_plan()
        self._drop_plan_state()
        if had:
            self._say(
                f"（已退出规划，{had} 条计划**丢掉**了 —— 一条都没建。）\n"
                f"  现在是正常模式，直接说要做什么就行。"
            )
        else:
            self._say("（已退出规划，现在是正常模式。）")
        return True

    def _confirm_plan(self) -> bool:
        """真的动手：把计划里的每一行按**正常路径**建成事务。

        ## 为什么复用 ``_create`` 而不是另写一套

        这一刀要落库、要推断角色、要生成标题 —— 那些都在 :meth:`_create`
        里（含 ``refine_title``）。另写一套就会漂移，而漂移的方向必然是
        「建得比正常路径少」—— 那正是 Plan→Build 最不能坏的地方。

        ## 为什么比对快照

        卡片上印的是发出时的计划。若用户确认期间又说了几句，无条件执行
        就是**批准了他没看过的东西** —— 而确认卡的全部意义就是「他核对过」。
        所以不一致就**要求重新确认**，不猜他想批准哪一版。
        """
        shown = self._last_plan_confirm
        if not shown:
            self._say("（没有待确认的计划 —— 先说 /mode-build。）")
            return True
        if tuple(self._plan) != shown:
            self._last_plan_confirm = ()
            self._say(
                f"（计划在你确认期间变了：确认卡上是 {len(shown)} 条，"
                f"现在 {len(self._plan)} 条。）\n"
                f"  已作废那张卡 —— 请重新说 /mode-build 看最新的一版。"
            )
            return True

        plan = list(self._plan)
        self._plan = []
        self._last_plan_confirm = ()
        self._mode = MODE_BUILD
        # 先写终态（build + 空计划）再删盘上状态 —— 顺序与理由见
        # ``_drop_plan_state``：drop 失败时盘上必须是「真的：已确认且无待办」，
        # 否则重启会把已确认的计划恢复回来，再点一次 ok 就是**重复建事务**。
        self._persist_plan()
        self._drop_plan_state()

        made: list[str] = []
        for line in plan:
            # **走正常路径**，而不是直接调 `_create(line, ACTION, [])`。
            #
            # 后者会让每一条都掉进「未分类」—— 那比用户自己在飞书里说
            # 这句话**差**：`_natural` 里有角色推断、有 kind 判定、有
            # 日期解析。复用它的意思就是字面上的「当你说这句话时会发生什么」。
            self._natural(line)
            if self._pending is not None:
                # 触发追问就必须**停在这里**：追问会吃掉下一条输入，
                # 而剩下的计划行会被当成「回答那个追问」——
                # 于是计划内容被静默改写，而用户以为他批准的是原来那几条。
                self._say(
                    f"（{len(made) + 1} 条需要你先回答一个问题，已停下。"
                    f"剩下的 {len(plan) - len(made) - 1} 条还没建。）"
                )
                break
            made.append(line)
        else:
            self._say(f"（已建 {len(made)} 条事务。）")
        return True

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

        # 委派意图**先于**记事判断 —— 「帮我改 OpenMOS 的登录」既有动作
        # 动词又像一句话，若先走记事分支就会被建成普通事务，**看着成功、
        # 实际什么都不会发生**（执行器只认 project_path 非空的行）。
        if self._pending_delegate is not None:
            if text.strip() in ("确认", "对", "是", "确认执行"):
                return self._confirm_delegation()
            return self._resume_delegation(text)

        if self._maybe_delegation(text):
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
            with self.app.lock:
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
        # 读-改-写：两个线程都「没找到」就会各建一个同名角色。
        # ``app.lock`` 是 **RLock**，所以 :meth:`_create` 里嵌套调用本方法
        # 不会自锁。
        with self.app.lock:
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

    # -- 委派：对话 → 确定要件 → 审批后执行 ------------------------------- #
    def _maybe_delegation(self, text: str) -> bool:
        """这句话是在要求改代码吗？是就接进委派三段。

        ## 为什么不在能力通道里做

        能力通道（:mod:`services.capability`）**只读**。而建委派要写库，
        所以这一步只能在 Repl 里 —— 这也是 ``/api/chat`` 需要把这类句子
        转过来的原因（见 ``web/endpoints_read.py``）。

        ## 判断用 LLM，不用关键词

        「让 opencode 改 X」「帮我改 X 项目」「X 那个 bug 修一下」——
        关键词表必漏。所以这里问模型一次**动作**（``describe``/``howto``/
        ``authorize``/``none``，见 :class:`CapabilityRouter`），复用同一套
        闭合动作集，不另造词表。
        """
        router = self.app.chat.capability_router
        caps = self.app.chat.capabilities
        if router is None or caps is None or not caps.enabled:
            return False
        project, brief = self._delegation_needs(text)
        if project is None and not brief:
            # 模型说「这不是委派」或抽不出任何东西 —— 走它自己那条路
            # （记事 / 提问 / 能力问答）。
            return False
        if project is not None and brief:
            # 两样都有 —— 也**先复述**，不直接建。
            self._pending_delegate = (project, brief)
            self._say("我理解你要的是：")
            self._say(f"  项目：{project}")
            self._say(f"  做的事：{brief}")
            self._say("对的话回「确认」，要改说法就直接说新的。")
            return True
        return self._ask_delegation_parts(brief)


    def _delegation_needs(self, text: str) -> tuple[str | None, str]:
        """这句话里能抽出 ``(项目, 需求)`` 吗？抽不出就 ``(None, ...)``。

        **交给模型抽结构**（``LLMProvider.parse_delegation``），不用规则剥。

        ## 为什么不用规则剥（第一版就是这么写的，错了）

        第一版是「剥掉项目名与一串动词，剩下的就是要做的事」。实测两处出错：

        - 「帮我改一下 README」剥出**空串** —— 需求整个丢了
        - 「把 greet 函数改成返回你好」剥成「greet 函数返回你好」——
          **动作被当成噪声删掉了**，而动词正是要执行的那部分

        规则只在写表的人和用户说同一句话时成立。结构化抽取交给模型，
        它保留了动词（实测 brief 原样是「把 greet 函数改成返回你好」）。

        ## 项目名必须原样取自授权清单

        ``parse_delegation`` 内部按闭集校验，自创名字被丢弃。而抽取不出
        项目时**不挑一个最像的** —— 挑错的代价是**在错误的仓库里动手**。
        """
        caps = self.app.chat.capabilities
        if caps is None or not caps.enabled:
            return None, ""
        try:
            got = self.app.llm.parse_delegation(text, caps.labels())
        except Exception:
            return None, ""
        if not got or not got.is_delegation:
            return None, ""
        project = got.project or None
        return project, (got.brief or "")

    def _ask_delegation_parts(self, brief: str) -> bool:
        """缺要件 —— 问清楚，**不建**。"""
        caps = self.app.chat.capabilities
        assert caps is not None
        if brief:
            self._pending_delegate = (None, brief)
            self._say(f"要改什么？「{brief}」是要做的事，还是项目名？")
            self._say("一句话说完就行，例如「FreeAgent 的 README 加一行说明」。")
            return True
        self._pending_delegate = (None, "")
        self._say(
            "要用 opencode 改哪个项目？现在授权的是："
            + "、".join(caps.labels())
        )
        self._say("说一句「改 <项目名> “要做的事”」就行。")
        return True

    def _resume_delegation(self, line: str) -> bool:
        """用户补上了缺的那半句 —— 复述一遍，**要他确认**。

        ## 确认卡在这里，不在后面

        闸门（授权卡）管的是「执行器每动一次要不要批」。而**这一步**管的是
        「这件事到底是不是你要的」。两道是不同的东西：前者在链路上，后者
        在链路上**之前**。少了它，一张张授权卡批下去，批的是一份猜出来的
        简报。
        """
        prior_project, prior_brief = self._pending_delegate
        self._pending_delegate = None

        project, brief = self._delegation_needs(line)
        project = project or prior_project
        brief = brief or prior_brief

        if project is None:
            caps = self.app.chat.capabilities
            self._say("项目还是不知道叫哪个。现在授权的是："
                      + "、".join(caps.labels() if caps else ()))
            return True
        if not brief:
            self._pending_delegate = (project, "")
            self._say(f"「{project}」——要改什么？一句话说清就行。")
            return True

        # **复述，不是新建。** 用户确认后才落库。
        self._pending_delegate = (project, brief)
        self._say("我理解你要的是：")
        self._say(f"  项目：{project}")
        self._say(f"  做的事：{brief}")
        self._say("对的话回「确认」，要改说法就直接说新的。")
        return True

    def _confirm_delegation(self) -> bool:
        """确认落库 —— 走 :meth:`_create_delegation`，与命令同一条路。"""
        project, brief = self._pending_delegate or (None, "")
        self._pending_delegate = None
        if not project or not brief:
            self._say("要确认的东西不完整，重新说一句「改 <项目> “要做的事”」。")
            return True

        role_name = self._default_role_name()
        task, err, role, by_default = self._create_delegation(
            project, role_name, brief
        )
        if err:
            self._say(f"[拒绝] {err}")
            return True

        self._say(f"已建委派事务 {task.id[:8]}")
        self._say(f"  项目：{task.project_path}")
        self._say(f"  脉络：{role.name}" + ("（默认第一条）" if by_default else ""))
        self._say("")
        self._say("它现在是「待办」，执行器不会碰它 —— 这是第三道闸门。")
        self._say(f"确认要派：/start {task.id[:8]}")
        if not by_default:
            self._say("（之后每一步它还会发卡问你批不批 —— 那是第四道闸门。）")
        return True

    def _default_role_name(self) -> str:
        roles = self.app.roles.list_roles()
        return roles[0].name if roles else ""

    # -- 自然语言建事务 ----------------------------------------------------- #
    def _create(self, text: str, kind: str, role_names: list[str]) -> None:
        # ── 锁外：网络调用 ────────────────────────────────────────────────
        #
        # 标题保留「下周二」这类时间信息 —— 那是标题的一部分，不是噪声。
        #
        # **刻意放在锁外**：这是网络往返（配置 20s），而下面那个写入是三条
        # 语句的序列。锁只该盖住后者（设计文档 12.7.2 的 R1）。
        title = self.app.llm.refine_title(text)

        today = self.app.clock.today()
        scheduled = parse.parse_date_expr(text, today)

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

        # ── 锁内：角色解析 + 建事务的**三条语句** ─────────────────────────
        #
        # ``TaskService.create`` 是 ``INSERT tasks`` → ``task_roles`` →
        # ``task_records``。而 ``sqlite3.threadsafety == 3`` 只保证**语句级**
        # 串行，不保证**序列**原子 —— 两个线程交错时，一个线程可能在自己的
        # ``task_roles`` 插入那一刻另一个线程已推进到下一条，于是外键找不到
        # 父行。实测过：``sqlite3.IntegrityError: FOREIGN KEY constraint
        # failed``。
        #
        # 所以锁必须盖住**整个序列**，而不是「大概包一下」。
        with self.app.lock:
            role_ids: list[str] = []
            for name in role_names:
                role = self._match_role(name) or self._ensure_role(name)
                role_ids.append(role.id)
            if not role_ids:
                role_ids = [self._ensure_role("未分类").id]
            task = self.app.tasks.create(
                title,
                role_ids,
                kind=task_kind,
                intent=intent,
                scheduled_for=scheduled,
                due_time=due,
                reminder_time=reminder,
                waiting_on=waiting_on,
                origin=ORIGIN_SELF,
            )
        self._say()
        self._say(f"已记下：{task.title}")
        self._say(
            f"  形状 {task.kind.label}｜状态 {task.state.label}"
            f"｜角色 {'、'.join(self._names().get(r, '?') for r in task.role_ids)}"
        )
        # V1.84：不再自动追问「意图还没说清楚」—— 快速记一笔不该被盘问。
        # intent 是可选项，缺了有 [TODO] 兜底（rules.py / deepseek.py），
        # 用户真要补自会用 /steps、/draft 或再补一句。**默认安静记下**。
        if task_kind is TaskKind.WAIT and waiting_on is not None:
            self._say(f"  在等：{waiting_on.who_or_what}")
        if reminder is not None:
            self._say(f"  提醒时间：{reminder:%Y-%m-%d %H:%M}")
        if due is not None:
            self._say(f"  截止时间：{due:%Y-%m-%d %H:%M}")
        if scheduled is not None:
            self._say(f"  已排到 {scheduled}")
        # V1.84：提到已登记的关系对象（张总这种）就顺势提一句可挂账 ——
        # **只提示不静默挂**（守 11.19「只显式触发」）：用户知道能挂，但
        # 挂不挂、挂到谁由他拍板。零误判 —— 只认已登记的名字（mentioned_
        # contacts 里那套逻辑，见 promise.py）。
        for contact in self.app.promise.mentioned_contacts(task.title):
            self._say(f"  提到 {contact.name}，可挂关系账（/contact {contact.name}）")
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
            # 刻意**不叫** ``/plan``：那个名字已经被「拆步骤」占用了
            # （``/plan <id>``）。撞名的后果很难看——实测它既不报「未知
            # 命令」、也不建任何东西，界面上只剩一句「用法：/plan <id>」，
            # 用户只会以为这个功能坏了。**沉默的降级比报错更难查。**
            #
            # 签名与其它命令一致（都收 ``args``）：``_command`` 无条件调
            # ``handler(args)``，写成零参lambda 会在运行时 TypeError。
            "/mode-plan": self._cmd_mode_plan,
            "/mode-build": self._cmd_mode_build,
            "/today": self._cmd_today,
            "/weigh": self._cmd_weigh,
            # 决策日志（7.6 / V1.112）。跟 /weigh 挨着 —— 那是拍板的地方，
            # 这是回头看拍过哪些板的地方。
            "/decisions": self._cmd_decisions,
            "/week": self._cmd_week,
            "/feedback": self._cmd_feedback,
            "/model": self._cmd_model,
            "/pending": self._cmd_pending,
            "/capture": self._cmd_capture,
            "/mail": self._cmd_mail,
            "/all": self._cmd_all,
            "/roles": self._cmd_roles,
            "/role": self._cmd_role,
            "/role-add": self._cmd_role_add,
            "/role-rename": self._cmd_role_rename,
            "/role-silence": self._cmd_role_silence,
            "/role-dod": self._cmd_role_dod,
            "/role-del": self._cmd_role_del,
            "/task-del": self._cmd_task_del,
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
            # 记忆（设计文档 3.7）。/remember 是 /memory add 偏好 的快捷方式。
            "/remember": self._cmd_remember,
            "/memory": self._cmd_memory,
            # 委派恢复入口。与 /note 分开：那条无校验，这条要挡三种不许重派。
            "/redispatch": self._cmd_redispatch,
            "/brief": self._cmd_brief,
            # 飞书配对（设计文档 11.9.8）。码由桥接侧签发，终端只核销。
            "/pair": self._cmd_pair,
            "/draft": self._cmd_draft,
            "/revise": self._cmd_revise,
            "/artifact": self._cmd_artifact,
            "/accept": self._cmd_accept,
            "/steps": self._cmd_steps,
            # 这里是「拆步骤」的 /plan（``/plan <id>``），**不是** Plan 模式。
            # 我曾经把模式入口的 ``/plan`` 写进这一行，于是**静默覆盖**了它：
            # 症状是 ``/plan <id>`` 报「用法：/plan <id>」之外什么也不做，
            # 而``/plan`` 自己也不进模式。已挪到 ``/mode-plan``（上方）。
            "/plan": self._cmd_plan,
            # 恢复出厂。**不带口令就跑 = 打印将要删什么**，不执行 ——
            # 一次无口令调用应当得到「说明」而不是「事故」。
            "/reset-data": self._cmd_reset_data,
            "/tick": self._cmd_tick,
            "/llm": self._cmd_llm,
            # 能力探针（设计文档 11.10.2）。只看不派 —— 见 _cmd_agents。
            "/agents": self._cmd_agents,
            # 问一句（设计文档 11.10.6）。刻意放在委派**旁边**而不是混进
            # 委派那张表：它不建事务、不落库，混进去会让人以为它能存东西。
            "/ask": self._cmd_ask,
            # 对外承诺与关系对象（设计文档 11.19）。放在「脉络」那张表**外面**，
            # 因为它不是脉络 —— 关系是「事务背后的人」，与角色正交（11.19.2）。
            "/promise": self._cmd_promise,
            "/contact": self._cmd_contact,
            "/nudge": self._cmd_nudge,
            # 人生角色关注面（11.24 / V1.68）：给角色定义「该周期性关心的东西」。
            "/care": self._cmd_care,
        }

    # ---- 视图 ---- #
    def _cmd_help(self, args: list[str]) -> None:
        """帮助。**先报当前模式**（V1.91）。

        进 Plan 之后用户唯一的困惑是「我现在说的话落不落库」，而这件事必须
        在 ``/help`` 第一行看得到 —— 否则他得靠回忆「我刚才是不是敲过
        /mode-plan」。这也顺手替掉了原本缺一条命令才有的「我在哪」查询：
        不新增 ``/mode``，把状态挂在本来就该看的那一页上。
        """
        if self._mode == MODE_PLAN:
            n = len(self._plan)
            tail = f"，已记下 {n} 条" if n else "，还没记下任何内容"
            self._say(
                f"（当前：Plan 模式{tail} —— 只沟通、不落库。"
                f"要动手说 /mode-build；不干了说 /mode-build discard。）"
            )
        self._say(_HELP)

    def _cmd_today(self, args: list[str]) -> None:
        view = self.app.today.view()
        # 待处理条数一并传给今天视图（11.18）：捕获了没澄清的事，
        # 应该在你每天看的那块屏幕上露个面，而不是躺在目录里等你想起。
        self._say(
            render.render_today(
                view, self._names(), pending_count=self.app.pending.count()
            )
        )
        # 飞书侧：今天**最高优先那条**附一张动作卡（开始 / 做完 / 放一放）。
        # 终端不读 ``_last_task_action``，设了无副作用；飞书 bridge 读到就发卡。
        # 刻意**只对第一条**（全局排序最高优先）发：卡片是「省一次交互」用的，
        # 每条都发会把飞书变成每句话一张卡（V1.61 起，查询命令可带操作卡；
        # 普通问答仍纯文本，见 test_card_button_command 的边界测试）。
        if view.items:
            top = view.items[0].task
            self._last_task_action = (
                top.id, top.title, STATE_LABELS[top.state],
            )

    def _cmd_weigh(self, args: list[str]) -> None:
        """代价对照（设计文档 7.5 / 20.1.14）。

        **只摆代价，不替用户做取舍** —— 这是 19.2 第 2 条壁垒兑现的地方：
        通用产品倾向黑箱打分「显得更智能」，这里反着来 —— 把两件事各自的
        代价摆出来，让主人自己拍板。

        三条纪律落在实现里（``services/weigh.py`` 有详述）：

        * **代价带出处**：每项代价标它的来源列，主人可核对
        * **事实不足就不给**：没有真取舍时如实说「没有需要拍板的取舍」，
          不降格成「那就先做 X」
        * **陈述句**：不出现「你应该」「建议」

        **不读人的任何决策** —— 所以不碰 11.9.11 的自我进化边界：
        本命令的输出与「人点过什么」无关。

        两种形态（**给已有命令加参数，不新增命令** —— 同 V1.91
        ``/mode-build`` 的那条纪律：命令表每多一条，说明书与设计文档的
        计数就跟着漂移一次）：

        * ``/weigh`` —— 只摆取舍
        * ``/weigh <序号>`` —— **记下这一板**（设计文档 7.6）

        **序号指向「此刻重算出来的那组取舍」** —— 与 ``/pending N``
        同一条路：列表会随事务状态变动，超界时说清当前有几位，
        不让用户对空气拍板。
        """
        now = self.app.clock.now()
        today = self.app.clock.today()
        # 全部未结束事务都参与争用 —— 冲突不只发生在今天排的那几条里
        # （一条没排期但今天到期的事，同样抢今天）。
        candidates = self.app.tasks.list_open()
        scored = score_and_sort(
            candidates,
            now,
            today,
            dependents_of=self.app.task_repo.count_dependents,
            energy_windows=self.app.energy_windows,
            promise_of=self.app.promise.promise_of,
        )
        promises_by_task = {
            p.task_id: p for p in self.app.promise.open_promises()
        }
        result = weigh(
            scored,
            now,
            today=today,
            promises_by_task=promises_by_task,
        )

        if not args:
            self._say(render.render_weigh(result))
            return

        # /weigh <序号>：记下这一板
        if len(args) != 1 or not args[0].isdigit():
            self._usage("/weigh")
            return
        index = int(args[0])
        if result is None:
            # 没有取舍就没有可拍板的东西 —— 如实说，不降级成「那就先做 X」
            self._say(render.render_weigh(None))
            return
        if not 1 <= index <= len(result.options):
            self._say(
                f"没有第 {index} 个 —— 此刻摆出来的是 {len(result.options)} 个。"
                "（列表会随事务状态变动，先 /weigh 看现在有哪几个）"
            )
            return
        recorded = self.app.decision.record(result, index - 1)
        self._say(
            render.render_weigh_picked(result, index - 1, is_new=recorded.is_new)
        )

    def _cmd_decisions(self, args: list[str]) -> None:
        """决策日志（设计文档 7.6 / 19.5.4 候选 D）。

        两块：**今天到期的那些**（当时那板 vs 现在，并排对质），
        以及**拍过哪些板**的索引。

        **只摆事实，不评价** —— 它不说「你选对了」也不说「你选错了」。
        对峙的价值在于把「当时怎么想的」和「现在各自什么状态」摆到
        一起让人自己看，不是替人下结论（7.1 / 〇.8.4）。

        **不读人的审批决策** —— 本命令的输出与「人点过什么」无关。
        """
        if args:
            self._usage("/decisions")
            return
        today = self.app.clock.today()
        due = self.app.decision.due(today)
        confrontations = [self.app.decision.confront(d) for d in due]
        self._say(render.render_decisions(confrontations, today=today))
        self._say("")
        self._say(render.render_decision_history(self.app.decision.history()))

    def _cmd_week(self, args: list[str]) -> None:
        """周回顾（设计文档 6.5）。

        **刻意只摆事实**：不按分数排序，也不给「下周该怎么排」的建议 ——
        与 ``/feedback`` 同一条纪律，与第七章「不替用户做取舍」同源。
        """
        view = self.app.week.view()
        self._say(render.render_week(view, self._names()))

    def _cmd_model(self, args: list[str]) -> None:
        """连续性个人模型（设计文档 11.17 / 20.1.1）。

        纯只读聚合：从 ``TaskRecord`` 与 ``Task`` 现推关于你自己的轻量规律，
        **不写库、不排序打分、不给建议**（与 /week、/feedback 同一条纪律），
        也正好契合「不自带 RAG」的定位——规则统计即可，零新依赖。
        """
        view = self.app.personal_model.view()
        self._say(render.render_model(view, self._names()))

    def _cmd_pending(self, args: list[str]) -> None:
        """待处理收集器（设计文档 11.18）。

        三种形态：无参列出；``/pending N [标题]`` 澄清成事务；
        ``/pending drop N`` 明确忽略。

        **澄清是用户显式动作**：捕获侧（飞书 ``_capture_unsupported``）只收
        不建（11.9.10），变成事务的扳机只在这里。所以序号指向的永远是
        「当前待处理列表」里那一条 —— 列表会随澄清/忽略变动，超界时
        说清当前有几位，不让用户对空气操作。
        """
        items = self.app.pending.pending()
        if args and args[0] == "drop":
            rest = args[1:]
            if len(rest) != 1 or not rest[0].isdigit():
                self._usage("/pending")
                return
            index = int(rest[0])
            if not 1 <= index <= len(items):
                self._say(
                    f"没有第 {index} 条待处理项（当前 {len(items)} 条）。"
                    "/pending 看列表。"
                )
                return
            item = items[index - 1]
            try:
                self.app.pending.dismiss(item.id)
            except FreeAgentError as exc:
                self._say(f"忽略失败：{exc}")
                return
            self._say(
                f"已忽略第 {index} 条。待处理还剩 {self.app.pending.count()} 条。"
            )
            return
        if args and args[0].isdigit():
            index = int(args[0])
            title = " ".join(args[1:]).strip() or None
            if not 1 <= index <= len(items):
                self._say(
                    f"没有第 {index} 条待处理项（当前 {len(items)} 条）。"
                    "/pending 看列表。"
                )
                return
            item = items[index - 1]
            try:
                task = self.app.pending.clarify(item.id, title=title)
            except FreeAgentError as exc:
                self._say(f"澄清失败：{exc}")
                return
            self._say(
                f"已澄清成事务 {task.id[:8]}：{task.title}\n"
                f"归在「{DEFAULT_ROLE_NAME}」—— 要归位用 "
                f"/move {task.id[:8]} <角色>。\n"
                f"待处理还剩 {self.app.pending.count()} 条。"
            )
            return
        if args:
            self._usage("/pending")
            return
        self._say(render.render_pending(items))

    def _cmd_capture(self, args: list[str]) -> None:
        """批量倾倒（录入口三件之一）：粘贴一大坨文本，按行切成多条进待处理队列。

        多行收集**停在空行**：空行在 REPL 里通常表示「我没话说了」，这里借它
        表示「倒完了」。入队**不建事务** —— 澄清是 ``/pending N``，纪律 11.18。
        """
        self._say("批量倾倒：粘贴文本（可多行），空行结束 —— 每行将进待处理队列。")
        lines = self._collect_multiline()
        if not lines:
            self._say("没有内容，未入队。")
            return
        text = "\n".join(lines)
        try:
            items = self.app.pending.capture_batch(source="cli", text=text)
        except FreeAgentError as exc:
            self._say(f"入队失败：{exc}")
            return
        self._say(f"已入待处理队列 {len(items)} 条 → /pending 查看并澄清。")

    def _cmd_mail(self, args: list[str]) -> None:
        """邮件入口（录入口三件之一）：粘贴邮件正文，整段一条进待处理队列。

        ``source='mail'`` 标记「这来自一封邮件」。整段一条（**不是按行切分**）——
        邮件是一条承诺 / 等候的载体，不该被拆碎。首行当作标题。不建事务，
        ``/pending`` 澄清。
        """
        self._say("邮件：粘贴邮件正文，空行结束 —— 整段一条进待处理队列，首行当作标题。")
        lines = self._collect_multiline()
        if not lines:
            self._say("没有内容，未入队。")
            return
        title = lines[0]
        body = "\n".join(lines)
        try:
            item = self.app.pending.capture_text(source="mail", text=body, name=title)
        except FreeAgentError as exc:
            self._say(f"入队失败：{exc}")
            return
        self._say(f"已收进待处理队列 1 条（{item.name}）→ /pending 澄清。")

    def _collect_multiline(self) -> list[str]:
        """从 stdin 收集多行，停在空行或 EOF。``/capture`` 与 ``/mail`` 共用。"""
        lines: list[str] = []
        while True:
            try:
                line = input("").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            lines.append(line)
        return lines

    def _cmd_promise(self, args: list[str]) -> None:
        """对外承诺（设计文档 11.19）。

        **只由用户显式触发，绝不从对话里静默推断**（11.19.1）——
        抽取「你答应了什么」是弱信号，与 11.10.5「绝不自动触发委派」同型。
        """
        items = self.app.promise.open_promises()
        if not args:
            self._say(
                render.render_promises(
                    items, self._contact_names(), now=self.app.clock.now()
                )
            )
            return

        head = args[0]

        # 记一笔：/promise 记 <事务id> <承诺内容> [关系] [期限]
        if head == "记":
            self._promise_record(args[1:])
            return

        # 落终态：/promise keep|break|withdraw <序号>
        if head in ("keep", "break", "withdraw"):
            if len(args) < 2 or not args[1].isdigit():
                self._usage("/promise")
                return
            index = int(args[1])
            if not 1 <= index <= len(items):
                self._say(
                    f"没有第 {index} 条承诺（当前共 {len(items)} 条）。"
                    "/promise 看列表。"
                )
                return
            promise = items[index - 1]
            action = {
                "keep": ("已兑现", self.app.promise.keep),
                "break": ("已违约", self.app.promise.break_),
                "withdraw": ("已收回", self.app.promise.withdraw),
            }[head]
            label, call = action
            try:
                call(promise.id)
            except FreeAgentError as exc:
                self._say(f"记不上：{exc}")
                return
            self._say(
                f"第 {index} 条承诺记为「{label}」：{promise.statement}\n"
                f"未兑现的还剩 {self.app.promise.open_count()} 条。"
            )
            return

        # 延期：/promise postpone <序号> <日期>
        if head == "postpone":
            if len(args) < 3 or not args[1].isdigit():
                self._usage("/promise")
                return
            index = int(args[1])
            if not 1 <= index <= len(items):
                self._say(
                    f"没有第 {index} 条承诺（当前共 {len(items)} 条）。"
                    "/promise 看列表。"
                )
                return
            try:
                when = self._parse_moment(args[2])
            except FreeAgentError as exc:
                self._say(f"日期看不懂：{exc}")
                return
            try:
                self.app.promise.postpone(items[index - 1].id, when)
            except FreeAgentError as exc:
                self._say(f"延期不上去：{exc}")
                return
            self._say(
                f"第 {index} 条承诺的期限改到 {when:%Y-%m-%d %H:%M}。"
                "（延期不算违约。）"
            )
            return

        self._usage("/promise")

    def _promise_record(self, args: list[str]) -> None:
        """``/promise 记 <事务id> <承诺内容> [关系] [期限]``。

        承诺**必须有兑现物**（11.19.2）：没有兑现物的承诺是空头承诺，
        记下来也无处可查、无从兑现。所以第一个参数是事务 id。

        **关系与期限用显式标记分开**（``关系=张总`` / ``期限=周三``），
        不用位置猜：位置猜的话「周三」这种既是日期又可能是人名的
        东西会被误判 —— 而误判的代价是把「我答应张总」记成「我答应
        陌生人，期限周三」。
        """
        if len(args) < 2:
            self._usage("/promise")
            return
        try:
            task_id = self._resolve_id(args[0])
        except FreeAgentError as exc:
            self._say(f"记不上承诺：{exc}")
            return
        statement = args[1]
        rest = args[2:]

        contact_name: str | None = None
        promised_for: datetime | None = None
        for token in rest:
            if token.startswith("关系="):
                contact_name = token[len("关系="):].strip() or None
            elif token.startswith("期限="):
                raw = token[len("期限="):].strip()
                try:
                    promised_for = self._parse_moment(raw)
                except FreeAgentError as exc:
                    self._say(f"期限看不懂：{exc}")
                    return
            else:
                self._say(
                    f"看不懂「{token}」—— 关系写「关系=张总」，"
                    "期限写「期限=明天下午三点」。"
                )
                return

        try:
            promise = self.app.promise.promise(
                statement,
                contact_name=contact_name,
                promised_for=promised_for,
                task_id=task_id,
            )
        except FreeAgentError as exc:
            self._say(f"记不上承诺：{exc}")
            return
        who = self._contact_names().get(promise.contact_id or "")
        tail = f"（答应{who}）" if who else ""
        when = f"，期限 {promised_for:%Y-%m-%d %H:%M}" if promised_for else ""
        self._say(
            f"记下了这条承诺{tail}：{promise.statement}{when}\n"
            f"挂在事务 {task_id[:8]} 上。\n"
            f"未兑现的承诺共 {self.app.promise.open_count()} 条。"
        )

    def _cmd_contact(self, args: list[str]) -> None:
        """关系对象（设计文档 11.19 / 20.1.5）。

        与角色脉络**正交**：角色回答「这件事属于哪一块」，关系回答
        「我跟谁之间有这笔账」。
        """
        if not args:
            counts: dict[str, int] = {}
            for promise in self.app.promise.open_promises():
                if promise.contact_id:
                    counts[promise.contact_id] = counts.get(promise.contact_id, 0) + 1
            waits = self._waiting_counts_by_contact()
            self._say(
                render.render_contacts(
                    self.app.promise.contacts(), counts, waiting_counts=waits
                )
            )
            return
        name = args[0]
        note = " ".join(args[1:]).strip() or None
        # 单参数、且这个关系**已经存在** → 查账（「见人前一页账」）。
        # 只有「记备注」（多参数）或「还不存在」才落到创建 ——
        # 关系对象是记账层，查账必须只读、不脏写；为查账去建一个
        # 不存在的人，会把「我有没有这笔账」变成「我造了一笔账」。
        if note is None:
            existing = self.app.promise.find_contact(name)
            if existing is not None:
                self._say(
                    render.render_contact_ledger(
                        self.app.promise.ledger(existing),
                        now=self.app.clock.now(),
                    )
                )
                return
        try:
            contact = self.app.promise.contact(name, note=note)
        except FreeAgentError as exc:
            self._say(f"记不下关系：{exc}")
            return
        self._say(
            f"关系对象「{contact.name}」（{contact.id[:8]}）已记下。\n"
            "承诺挂到它名下就能按人聚合了：/promise 记 <事务id> <承诺> "
            f"{contact.name}"
        )

    def _contact_names(self) -> dict[str, str]:
        """``{contact_id: 名字}``，给渲染层用。"""
        return {c.id: c.name for c in self.app.promise.contacts()}

    def _waiting_counts_by_contact(self) -> dict[str, int]:
        """每个关系对象名下的**未结束等候**条数。

        与未兑现承诺并排显示，两数一读就是「我跟这个人之间有多少笔
        没结清的账」—— 关系一等化的意义（20.1.5）全在这两行数字上。
        """
        counts: dict[str, int] = {}
        for task in self.app.tasks.list_open():
            waiting = task.waiting_on
            if waiting is not None and waiting.contact_id:
                counts[waiting.contact_id] = counts.get(waiting.contact_id, 0) + 1
        return counts

    def _cmd_nudge(self, args: list[str]) -> None:
        """该催的事务 / 出催办草稿（设计文档 20.1.4）。

        **起草不发送**：这一命令只产出文本，系统没有「顺手发出去」
        这条路。理由见 :mod:`freeagent.services.nudge` 的纪律 1 ——
        不可逆的动作不该只靠一条纪律守着，得让那条路径不存在。
        """
        targets = self.app.nudge.targets()
        if not args:
            self._say(render.render_nudge_targets(targets))
            return
        if not targets:
            self._say("现在没有到点的催办（等候件的跟进日没过、承诺也没逾期）。")
            return
        raw = args[0]
        picked: object | None = None
        # **先试序号，再试 id 前缀**，而不是靠 ``isdigit()`` 分流。
        # 踩过的坑：`id` 是 ``new_id()`` 的纯 hex，**可能是全数字**
        # （实测 id 前缀 "61316741" 就是），而 ``isdigit()`` 判真后
        # 会被当成序号 61316741，于是「没有第 61316741 条」——
        # 用户明明敲了一个合法的 id，却被告越界。
        # 判据不是「像不像数字」，而是**试过之后哪种解释成立**：
        # 序号在范围内就按序号，超界了再按 id 试一次，两边都不中才报错。
        if raw.isdigit():
            number = int(raw)
            if 1 <= number <= len(targets):
                picked = targets[number - 1]
        if picked is None:
            prefix = raw.lower()
            picked = next(
                (t for t in targets if t.task.id.startswith(prefix)), None
            )
        if picked is None:
            if raw.isdigit():
                self._say(f"没有第 {raw} 条（现有 1..{len(targets)}），"
                          f"也不匹配任何事务 id。")
            else:
                self._say(f"没有匹配 {raw} 的催办项（先 /nudge 不带参数看列表）。")
            return
        task = picked.task  # type: ignore[union-attr]
        self._say(f"催办草稿 · {task.title}")
        self._say("")
        for line in self.app.nudge.draft(picked).splitlines():  # type: ignore[arg-type]
            self._say(f"  {line}")

    def _cmd_feedback(self, args: list[str]) -> None:
        """执行层与治理层的**事实留痕**：我批得怎么样、派得怎么样、改了什么。

        **刻意只摆事实，不给建议。** 与第七章「摆得更清楚 ≠ 替你决定」
        同构，也与 11.10.5「绝不自动触发委派」同源 —— 一个「建议重派」
        的按钮就是自动触发的前一步。所以输出里没有「建议你…」这种句子。

        三段分开算，任何一段没数据就跳过那一段，而不是整条命令早退：
        「一次都没派过」不该让「我批了 12 次、其中 9 次没看见」也看不见 ——
        恰恰是后者才说明问题出在**打扰方式**上，而不是派发质量上。
        """
        self._say_audit_section()
        self._say_delegation_section()
        self._say_dispatch_gap_section()
        self._say_revision_section()
        self._say(
            "\n这些是事实，不是建议 —— 要不要重派、怎么改，由你决定。"
        )

    def _say_audit_section(self) -> None:
        """审批留痕：每条等你批的请求最后怎么了（V1.92）。

        这一段的全部意义在于**把「没人看」从「拒绝」里拆出来**。拆之前
        两者都是 ``decision='deny'``，而它们该导致的后果完全相反：
        前者要改的是提议内容，后者要改的是打扰方式。
        """
        from ..services.approval import ApprovalStore

        rep = ApprovalStore(
            self.app.conn, clock=self.app.clock.now
        ).evidence()
        if rep.total == 0:
            self._say("审批留痕：这段时间没有等你批的请求。")
            return
        self._say(
            f"审批留痕（近 {rep.since_days} 天共 {rep.total} 条）\n"
            f"  允许 {rep.allow}　明确拒绝 {rep.deny}　叫停 {rep.stop}\n"
            f"  点了但已过期 {rep.late}　没人看 {rep.timeout}　还挂着 {rep.open}"
        )
        if rep.timeout:
            self._say(
                "  ↑「没人看」不是拒绝 —— 卡放到过期，你根本没看见它。"
            )
        recent = [e for e in rep.entries if e.outcome != "open"][:5]
        if recent:
            label = {
                "allow": "允许", "deny": "拒绝", "stop": "叫停",
                "late": "过期后才点", "timeout": "没看", "open": "挂着",
            }
            self._say("\n  最近几条：")
            for e in recent:
                span = (
                    f"　等了 {_readable_span(e.waited_seconds)}"
                    if e.waited_seconds is not None else ""
                )
                self._say(
                    f"    {e.asked_at.strftime('%m-%d %H:%M')}　"
                    f"{label.get(e.outcome, e.outcome)}　{e.subject}{span}"
                )

    def _say_delegation_section(self) -> None:
        """执行层履历：派了多少、成了多少、反复失败在哪。"""
        from ..services.feedback import feedback_report

        tasks = [t for t in self.app.task_repo.list_all() if t.is_open]
        if not tasks:
            self._say("\n还没有未结束的事务 —— 没有可统计的委派。")
            return
        ids = [t.id for t in tasks]
        titles = {t.id: t.title for t in tasks}
        rep = feedback_report(
            ids,
            lambda tid: self.app.record_repo.list_for_task(tid),
            lambda tid: titles.get(tid, ""),
        )
        rate = rep.success_rate
        rate_text = "没真跑过" if rate is None else f"{rate:.0%}"
        self._say(
            f"\n未结束事务 {rep.total} 条 —— 其中派过的 "
            f"{rep.total - rep.never} 条\n"
            f"  成功 {rep.succeeded}　执行期失败 {rep.failed}　"
            f"派发前被拒 {rep.refused}　没派过 {rep.never}\n"
            f"  成功率 {rate_text}"
            + (
                "（分母只算真跑过的：派发前被拒不算失败）"
                if rate is not None else ""
            )
        )
        if rep.top_reasons:
            self._say("\n反复出现的错因（原样引用，不归类）：")
            for reason, n in rep.top_reasons:
                self._say(f"  {n} 次　{reason}")
        if rep.error_kinds:
            # 上面那段是**原话**，按文本聚类 —— 同一类错误因细节不同会聚成
            # N 条，计数因此失去意义。这一段是**类别**（从
            # ``task_records.error_kind`` 列读，schema v29），回答的是
            # 「这类错我犯过几次」。
            #
            # 两段并存：原话给人判断「引擎到底说了什么」，类别给人看
            # 「哪一类在反复发生」。只留前者就看不出模式，只留后者就
            # 没法自己核对。
            self._say("\n引擎错误的类别（按类别计次）：")
            for kind, n in rep.error_kinds:
                label = {
                    "quota": "额度耗尽",
                    "auth": "凭据问题",
                    "model_gone": "模型不可用",
                    "context": "上下文超限",
                    "tool": "工具失败",
                    "upstream": "上游故障",
                }.get(kind, kind)
                self._say(f"  {n} 次　{label}")
        if rep.verdicts:
            self._say("\n产出验收：")
            for verdict, n in rep.verdicts:
                label = {
                    "accepted": "已验收",
                    "revised": "圈改重做过（>=2 版）",
                }.get(verdict, verdict)
                self._say(f"  {label}　{n} 条")

    def _say_dispatch_gap_section(self) -> None:
        """没派出去的委派，**各自卡在哪一步**（12.11.3 / V1.94）。

        为什么需要这一段：资格判定是三个条件的合取（有项目路径 / 是 active /
        没派过或上次失败），不满足就**静默跳过**。于是「我建了委派它怎么
        不动」的答案只存在于代码里 —— 这与 v1.36 那次「文档承诺了重派、
        守卫禁止它」是同一类毛病：系统知道原因，但不说。

        全部都在正常轨道上时**一个字不占**：没有卡点就没有版面。
        措辞里不带「建议」，与本章其余各段同源。
        """
        from ..services.delegate import dispatch_blocker
        from ..storage.repos import LeaseRepo

        leases = LeaseRepo(self.app.conn).all()
        now = self.app.clock.now()
        rows: list[tuple[str, str]] = []
        ready = 0
        for task in self.app.task_repo.list_all():
            if not task.project_path or not task.is_open:
                continue
            why = dispatch_blocker(
                task,
                self.app.record_repo.list_for_task(task.id),
                lease=leases.get(task.id),
                now=now,
            )
            if why:
                rows.append((task.title, why))
            else:
                # 够格 = 下一次扫派会派它。**不列进「卡住」**（它没卡住），
                # 但也不该彻底沉默：执行器没在跑时，它同样永远不会开始 ——
                # 而「我建了委派它怎么不动」问的正是这一格。
                ready += 1
        if not rows and not ready:
            return
        if rows:
            self._say(f"\n没派出去的 {len(rows)} 条（各自卡在哪一步）：")
            for title, why in rows[:8]:
                self._say(f"  {title} —— {why}")
            if len(rows) > 8:
                self._say(f"  …还有 {len(rows) - 8} 条")
        if ready:
            lead = "　" if rows else "\n"
            self._say(
                f"{lead}够格待派 {ready} 条 —— 执行器下一次扫到就会派；"
                "它没在跑就不会自己开始。"
            )

    def _say_revision_section(self) -> None:
        """改版差异：圈改的时候**实际动了哪几行**（V1.92）。

        这是「人改了什么」唯一能算出来的地方 —— 审批卡上只有同意/拒绝，
        没有「改一下」；真正的改动发生在 :meth:`revise`，而它**永远产生
        新版本、旧版不删**，于是两版摆在一起就能算差。

        只报增删行数与几行样本，**不做语义解释**（「他大概想更简洁」是
        替他讲动机）。样本裁到 60 字，避免一条长段落把整屏吃掉。
        """
        from ..services.feedback import revision_drift

        rows: list[tuple[str, str, int, int, object]] = []
        for task in self.app.task_repo.list_all():
            versions = self.app.artifacts.list_for_task(task.id)
            for prev, cur in zip(versions, versions[1:]):
                drift = revision_drift(prev.content, cur.content)
                if drift.added == 0 and drift.removed == 0:
                    continue
                rows.append(
                    (task.id, task.title, prev.version, cur.version, drift)
                )
        if not rows:
            return
        self._say("\n改版差异（圈改时实际动了什么）：")
        for task_id, title, v_from, v_to, drift in rows[-5:]:
            self._say(
                f"  {title}（{task_id[:8]}）v{v_from}→v{v_to}："
                f"+{drift.added} 行 / -{drift.removed} 行"
            )
            for sign, line in drift.samples:
                text = line.strip()
                if not text:
                    continue
                self._say(f"    {sign} {text[:60]}")

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

    def _cmd_agents(self, args: list[str]) -> None:
        """``/agents`` —— 有哪些执行器可用、装了没、能不能派（设计文档 11.10.2）。

        **只看不派。** 刻意不给「派一个」的参数：本命令是探针，
        而探针一旦能顺手派出去，就有了「点错就跑」的形状
        （11.10.5：绝不自动触发委派）。

        列的是**注册表里登记的全部**执行器，不是「装了的那几个」——
        没装的也要列出来并说清为什么不可用，否则用户会以为
        「没列出 = 本仓不支持」，而那与「装了但没验过」是两件不同的事。

        函数内导入：探针要跑一次 ``--version``（起进程），
        那是**有代价**的操作，不该在模块导入期就发生。
        """
        from ..services.executor_probe import probe_all, render_probes

        self._say(render_probes(probe_all()))

    def _cmd_ask(self, args: list[str]) -> None:
        """``/ask [hermes] <问题>`` —— 把一句话交给执行器，把答复拿回来（设计文档 11.10.6）。

        **和 ``/delegate`` 的区别**：那是「把一件事做掉」，这是「问一句」。
        所以不落库、不进今天视图、不产出 artifact、**不问任何审批**。

        可选的首个 token ``hermes`` 保留但**已停用**（V1.83）：``/ask hermes``
        会直接报「hermes 执行器已停用」，不再分派 —— 因为 hermes 的审批闸是
        「按模式标记」而非「每动作确认」，拦不住未标记的 file/terminal，不能当
        受控执行器（见 11.10.8）。知识问答统一走默认的 opencode。

        「不问审批」的前提是「**改不了**」：opencode 由 ``services/ask.py``
        传入的**只读权限配置**强制（不是靠执行器自觉）。

        函数内导入：``ask_executor`` 会起一个**真进程**，那是本文件导入期
        绝不该发生的事。

        V1.97 起会**带上用户记下的 core 记忆**（global 作用域 —— 3.7.6
        注入点①的落地）：执行器不再对你的约定一无所知。块自带「可能过期，
        以当前为准」的定性；一条都没记时不注入、也不打印。

        V1.99 起注入集**按问题的相关度选**（不再一律取最新 N 条）：
        相关度命中就说带了几条相关的；落空就说「相关度没命中，带了最近的
        几条」；相关度那条腿本身坏了也明说（按时间倒序带，不装作按相关度）。
        三种说法都在 :class:`CoreBlock` 的 ``leg`` 里，日志同值可复核。
        """
        executor = "opencode"
        if args and args[0] == "hermes":
            # V1.83 停用：hermes 不再当执行器用。不悄悄改走 opencode ——
            # 用户点了名字就是想要 hermes，报清「已停用」并给出路，比
            # 换到别的执行器更诚实（fail-closed + 说得清原因）。
            self._say("hermes 执行器已停用（V1.83）。它的审批闸是「按模式标记」"
                      "而非「每动作确认」，拦不住未标记的文件/终端操作，不能当"
                      "受控执行器。知识问答请用 `/ask`（默认 opencode，FreeAgent "
                      "全控）。代码保留作接缝，若 hermes 修好审批可重新启用。")
            return
        question = " ".join(args).strip()
        if not question:
            self._say("用法：/ask [hermes] <问题>。例如：/ask 这个项目是干什么的？")
            return

        import pathlib

        from ..services.ask import ask_executor, knowledge_boundary
        from ..services.opencode_server import OpenCodeServer

        def make_server(p, c, **kw):
            # 只 opcode 一个执行器（hermes 已停用，V1.83）。``command`` 参数
            # （ask_executor 传入的 ``executor``）就是这里的 ``c``。
            return OpenCodeServer(project=p, executable=c, apply_gate=True, **kw)

        project = pathlib.Path.cwd()
        self._say(f"（问的是这个目录：{project}，执行器 {executor}）")
        # V1.97：把用户记下的 core 记忆带给执行器（3.7.6 注入点①的落地）。
        # 只取 global 作用域 —— /ask 没有事务上下文，判不出「命中角色」；
        # 角色作用域的记忆走 12.10.10 的委派简报（conventions(role_ids)）。
        # 定性（「可能过期，以当前为准」）由 core_block 自带，这里不另拼。
        # 空记忆不注入、也不打印：用户问的是问题，不是记忆状态，而
        # 「什么都没记」不存在被误当作「注入了」的余地。
        # V1.99：把问题本身当查询词 —— 注入集按相关度选，不一律取最新。
        block = self.app.memory.core_block(consumer="/ask", query=question)
        if block.text:
            # 三种 leg 三种说法：选法不同，告诉用户的话就不同 ——
            # 「按相关度选的」与「只是最近写的」混成一句，等于让自己猜。
            if block.leg == "bm25":
                note = (
                    f"（带上了你记下的记忆里与这个问题相关的 {block.selected} 条，"
                    "可能过期，以当前为准"
                )
            elif block.leg == "tail":
                note = (
                    f"（带上了你记下的记忆里最近的 {block.selected} 条"
                    "——相关度没命中，可能过期，以当前为准"
                )
            else:
                note = "（带上了你记下的记忆，可能过期，以当前为准"
            if block.degraded_reason:
                # 相关度没走成必须说：否则「按相关度选的」会被当成真的。
                note += f"；{block.degraded_reason}"
            if block.truncated:
                note += f"；还有 {block.extra} 条未带上，用 /memory list 看全部"
            self._say(note + "）")
        outcome = ask_executor(
            question,
            project=project,
            command=executor,
            make_server=make_server,
            model=(self.app.config.model if self.app.config else None),
            # 只把文本交给执行器 —— CoreBlock 的 leg/selected 是给我们自己
            # 看的（措辞与日志），执行器不需要知道这批是怎么选出来的。
            context=block.text,
        )
        if outcome.ok:
            self._say("")
            self._say(outcome.text or "（它没给出文字答复。）")
            # V1.104：答复之后立刻给凭据档（11.10.9）。放在**后面**是有意的 ——
            # 先看答案再看「这个答案站在什么上」，否则人会拿一行标注代替阅读。
            self._say("")
            self._say(
                render.render_boundary(
                    knowledge_boundary(outcome.evidence, project)
                )
            )
            return
        self._say("")
        self._say(f"没答成：{outcome.reason}")
        if outcome.partial:
            self._say("")
            self._say("已拿到的部分：")
            self._say(outcome.partial)
        for note in outcome.notes:
            self._say(note)
        # 失败也给：**半截答复照样有凭据问题** —— 它读没读过东西，与
        # 「答没答完」是两件事。没有证据就是「猜想」，这个结论不会因为
        # 中途失败而变。
        self._say("")
        self._say(
            render.render_boundary(knowledge_boundary(outcome.evidence, project))
        )

    def _cmd_tick(self, args: list[str]) -> None:
        digest = self.app.reminders.due()
        text = render.render_digest(digest)
        if not text:
            # 安静窗口内「有提醒但不推」与「真没有提醒」必须分得开（9.8）：
            # 都回一句「没有到点的提醒」会让用户以为窗口没生效。
            note = digest.quiet_note()
            self._say(note if note else "没有到点的提醒")
            return
        self._say(text)
        # 确认放在 _say 之后 —— 先说再认领。反过来会先改状态后播报，
        # 播报失败就「状态改了但用户什么都没看到」（8.2 的同一条纪律）。
        self.app.reminders.acknowledge(digest)

    # ---- 恢复出厂 ---------------------------------------------------- #
    def _cmd_reset_data(self, args: list[str]) -> None:
        """清空全部事务数据。角色脉络与配置保留；强制先备份。

        口令是**位置参数**而不是第二问 input()：一行命令自己带着凭据，
        比「先问一句再问一句」更不容易被顺手敲过；而口令不对时打印
        将要删什么（说明）而不是执行 —— 无口令调用的正确结果是说明书。
        """
        from .services.reset import (
            RESET_PHRASE,
            ResetRefused,
            db_path_of,
            reset_factory,
        )

        if not args or "".join(args) != RESET_PHRASE:
            n = len(self.app.task_repo.list_all())
            self._say(
                f"这将清空全部 {n} 条事务及其记录/产物（角色脉络、配置、"
                f"Key 全部保留），并**先自动备份**。\n"
                f"确认请输入：/reset-data {RESET_PHRASE}"
            )
            return
        try:
            result = reset_factory(
                db_path_of(self.app.conn), confirmed=True
            )
        except ResetRefused as exc:
            self._say(f"没有执行：{exc}")
            return
        self._say(
            "已清空。删除：" +
            "，".join(f"{k} {v} 条" for k, v in result.deleted.items() if v) +
            f"\n备份在：{result.backup}\n{result.restore_hint}"
        )

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

    #: 「每周X」里 X 的中文数字 → ISO 星期（1=周一 … 7=周日）。
    _CARE_WEEKDAY = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
    }

    def _parse_cadence(self, tokens: list[str]) -> str:
        """把「每周六 / 每月1号」翻成规范串（``weekly:6`` / ``monthly:1``）。

        缺省 = **每周一**（weekly:1）——「周期性关心」总得有个周期，默认
        每周一次是低频不扰的起点；用户可随时显式改。解析失败抛错，
        不静默猜（fail-closed）。
        """
        s = "".join(tokens).strip()
        if not s:
            return "weekly:1"
        if s.startswith("每周"):
            v = s[2:]
            if v in self._CARE_WEEKDAY:
                return f"weekly:{self._CARE_WEEKDAY[v]}"
            if v.isdigit() and 1 <= int(v) <= 7:
                return f"weekly:{int(v)}"
            raise FreeAgentError(f"「每周」后请给 一~日 或 1-7（收到：{s}）")
        if s.startswith("每月"):
            v = s[2:].replace("号", "")
            if v.isdigit() and 1 <= int(v) <= 31:
                return f"monthly:{int(v)}"
            raise FreeAgentError(f"「每月」后请给 1-31 号（收到：{s}）")
        raise FreeAgentError(f"节奏只支持「每周X」或「每月X号」（收到：{s}）")

    def _cmd_care(self, args: list[str]) -> None:
        """人生角色关注面（11.24 / V1.68）。

        无参 → 列出所有角色的关注面；``/care done <序号>`` → 记「看过了」
        （推到下周期）；``/care <角色> <关注面> [每周X|每月X号]`` → 记一条。
        """
        if not args:
            cares = [
                render.CareDue(area=a, role_name=self.app.roles.get(a.role_id).name)
                for a in self.app.care.list()
            ]
            self._say(render.render_cares(cares))
            return
        if args[0] == "done":
            if len(args) < 2:
                self._usage("/care")
                return
            try:
                index = int(args[1])
            except ValueError:
                self._usage("/care")
                return
            try:
                area = self.app.care.done(index)
            except NotFoundError:
                self._say(
                    f"没有第 {index} 条关注面（当前共 {self.app.care.count()} 条）"
                )
                return
            self._say(
                f"已记「{area.text}」看过了，推到下个周期 —— "
                f"{area.next_due.isoformat()} 再看。关注面不是待办，不会做完就消失。"
            )
            return
        if len(args) < 2:
            self._usage("/care")
            return
        role = self._require_role(args[0])
        text = args[1]
        cadence = self._parse_cadence(args[2:])
        try:
            area = self.app.care.add(role.id, text, cadence)
        except (ValidationError, NotFoundError) as exc:
            self._say(str(exc))
            return
        self._say(
            f"已给「{role.name}」记关注面：{area.text}（"
            f"{render._cadence_label(area.cadence)} 起，{area.next_due.isoformat()} 该看）"
        )

    def _cmd_task_del(self, args: list[str]) -> None:
        """硬删一条事务（记录/产物/角色归属一起没，找不回来）。

        **两道确认做成「同一命令敲两次」而不是 input() 再问一句**：命令层
        有三个通道（终端、WEB 对话、飞书），只有终端有 stdin——在命令里
        嵌 input() 会把 WEB 的请求线程挂死在等输入上。第一次调用只说后果
        与出路、不碰库，任何通道都跑得动；而「手滑连敲两次同样带 confirm
        的命令」比「顺手点掉一个弹窗」难得多。

        第一调就解析 id：id 都不存在时，说明书话术没有意义——直接说找不到。
        """
        if not args:
            self._usage("/task-del")
            return
        task_id = self._resolve_id(args[0])
        if len(args) >= 2 and args[1] == "confirm":
            task = self.app.tasks.delete_task(task_id)
            self._say(f"已删除事务「{task.title}」（记录与产物一并消失）")
            return
        task = self.app.tasks.get(task_id)
        self._say(
            f"这将**彻底删除**「{task.title}」：事务、记录、产物、角色归属"
            "一起消失，找不回来。\n"
            f"确认请再输一次：/task-del {args[0]} confirm"
        )

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

    def _create_delegation(
        self, raw_project: str, role_name: str, requirement: str
    ):
        """委派事务的**唯一**创建入口。命令与自然语言都走它。

        ## 为什么不写两份

        白名单闸门、脉络归属、``delegate_chat_id`` / ``delegate_requested_by``
        的来源——每一条都是**安全或闭环**的一部分。抄第二份就等于给其中
        一条漏掉的路径开了个口子。所以 :meth:`_cmd_delegate` 与自然语言那条
        都在这里汇合。

        返回 **4 元组** ``(task, err, role, by_default)``：成功时后两项有值，
        失败时前两项有值。**四个位置在两条路上都要填** —— 少填一个，
        走那条路的调用方就会在解包时炸，而那恰恰是最该被测的错误路径
        （实测：「白名单外的项目被拒」那条测试就是这么红的）。

        **不抛异常** —— 调用点是「说一句话」，抛异常会变成一句没人懂的栈。
        """
        from ..services.delegate import check_project_allowed
        from ..services.opencode_projects import authorized_names

        caps = self.app.chat.capabilities
        policy = self.app.config.delegate_policy()
        try:
            project = check_project_allowed(
                policy, raw_project,
                # 只列**已授权**的项目名：列未授权的会让人去试、然后撞闸门，
                # 而撞闸门的感觉是「这功能坏了」。
                known_names=authorized_names(policy.projects),
                # 项目**名**也认 —— 译出来的路径照常过白名单，准入权不变
                # （见 services/delegate.py 里 aliases 的说明）。
                aliases=tuple(zip(
                    caps.labels() if caps else (),
                    caps.projects if caps else (),
                )),
            )
        except FreeAgentError as exc:
            return None, str(exc), None, False

        role = self._match_role(role_name)
        if role is None:
            # 自然语言那句「没说归属哪条脉络」不该逼用户去 /role-add ——
            # 有第一条就用它，并在回话里说清用了哪条。
            existing = self.app.roles.list_roles()
            if role_name and len(existing) > 1:
                return None, (
                    f"「{role_name}」不是现有脉络。你有：" +
                    "、".join(r.name for r in existing)
                ), None, False
            if not existing:
                return None, (
                    "还没有任何脉络，委派需要归属到一条 —— 先 /role-add 建一个。"
                ), None, False
            role = existing[0]
            chosen_by_default = True
        else:
            chosen_by_default = False

        task = self.app.tasks.create(
            requirement,
            [role.id],
            kind=TaskKind.ACTION,
            intent=f"在 {project} 里完成：{requirement}",
            project_path=str(project),
            origin=ORIGIN_SELF,
            # 从**能推送的**通道发起的，才记下是哪个会话 —— 执行器靠它把结果
            # 推回去，闭环才合得上。
            #
            # ⚠️ ``channel_ctx`` 存在**不等于**有地方可推（设计文档 11.11.2）。
            # WEB 是请求-响应通道，它的 ``chat_id`` 是个常量 ``"web"`` 而不是
            # 会话 ID —— 写进这个语义为「往哪儿发消息」的列，就是**在库里存了
            # 一条假地址**。执行器那侧会拿着它去发消息，症状是打出一条指向
            # 错误方向的「缺少 FEISHU_APP_ID」（实测，2026-10-09）。
            #
            # 所以判据是**通道能不能推**，而不是**有没有 channel_ctx**。
            # 终端（``None``）与 WEB 都落 ``None``：结果只进产物链，
            # 由各自的前端拉取 —— 那是它们的正确送达方式，不是降级。
            delegate_chat_id=(
                self.channel_ctx.chat_id
                if self.channel_ctx
                and self.channel_ctx.channel in PUSH_CHANNELS
                else None
            ),
            # 「谁发起的」—— 与上面那个「发到哪」取自**同一个** channel_ctx，
            # 所以两者天然一致：不会出现「A 发起的、
            # 结果却推给 B 的会话」。终端发起时 channel_ctx 是 None。
            #
            # 刻意**不**跟着上面那条一起收窄：那是「往哪儿推」（推送能力），
            # 这是「谁发起的」（身份，11.9.7 的「只有发起人能批」要用）。
            # WEB 侧的 ``sender_open_id`` 是空串，于是落库仍是 ``None`` ——
            # 结果相同，但**理由不同**，写下来免得将来有人以为这里漏了。
            delegate_requested_by=(
                (self.channel_ctx.sender_open_id or None)
                if self.channel_ctx else None
            ),
        )
        return task, None, role, chosen_by_default

    def _cmd_delegate(self, args: list[str]) -> None:
        """建一条**委派**事务：交给 opencode 在指定项目里完成。

        刻意放在终端而不是界面上。委派链路的终点是本地代码执行，
        所以「谁能创建委派」必须是显式动作，而不是表单里的一个字段。
        """
        from ..services.delegate import check_project_allowed
        from ..services.opencode_projects import authorized_names

        raw_project, role_name, requirement = parse_delegate_args(args)
        if not raw_project or not role_name or not requirement:
            self._usage("/delegate")
            self._say(
                "格式：/delegate <项目名或绝对路径> | <角色> | <需求>\n"
                "例：  /delegate myapp | 工作项目A | 加个邮箱登录\n"
                "（`|` 只是分隔符，敲不敲都行；也可用空格分隔）\n"
                "项目名指你在设置里授权过的那些（" +
                "、".join(self.app.chat.capabilities.labels()
                          if self.app.chat.capabilities else ()) + "）"
            )
            return

        task, err, role, by_default = self._create_delegation(
            raw_project, role_name, requirement
        )
        if err:
            self._say(f"[拒绝] {err}")
            return

        self._say(f"已建委派事务 {task.id[:8]}")
        self._say(f"  项目：{task.project_path}")
        self._say(f"  脉络：{role.name}" + ("（你没指定，用的第一条）" if by_default else ""))
        self._say("")
        self._say("它现在是「待办」，执行器不会碰它。")
        # ⑩ 委派隐私的正面声明（重写版附录 B·10）：委派**不隔离** —— 真跑时
        # 会在该项目里以你的执行器配置运行（默认 ``plugins=inherit``，加载你配的
        # 插件 / MCP / 技能，它们在项目里跑会读到项目内容）。这句话必须放在
        # 「确认要派的话」之前：用户是在看到它之后才决定要不要 /start 的。
        self._say("  ⚠️ 委派不隔离：真跑时会在该项目里以你的执行器配置运行"
                  "（默认加载你配的插件 / MCP / 技能）。只把可信项目交给它。")
        self._say(f"确认要派的话：/start {task.id[:8]}")
        self._say("然后另开一个终端跑：python -m freeagent.delegate --dry-run")
        # 让通道层（飞书）能发一张动作卡：否则用户看到「确认要派的话：
        # /start ab12」却还得**手打这条命令**—— 而 11.9.8 说的「点一下即可」
        # 就没兑现。终端不读这个字段，所以这里设了也无副作用。
        # 三元组 (id, title, state_label)：V1.61 起桥接用第三项做卡上
        # 「当前状态」文案，today 卡传事务实际状态，委派卡传这句。
        self._last_task_action = (task.id, task.title, "待办（执行器不会碰它）")

    def _cmd_task(self, args: list[str]) -> None:
        if not args:
            self._usage("/task")
            return
        view = self.app.restore.open_task(self._resolve_id(args[0]))
        self._say(render.render_task(view, self._names()))
        # V1.66：飞书点开单条事务，带它自己的动作卡（同 today 卡，见 11.23）。
        # 只对**未结束**的事务发：DONE / DROPPED 的按钮（start/done/pause）没有
        # 意义，发了就是噪音。open_task 可能 mark_resumed（改了状态），
        # 所以用返回的 view.task（最新那条）。
        if view.task.is_open:
            self._last_task_action = (
                view.task.id, view.task.title, STATE_LABELS[view.task.state],
            )

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
            target_tokens, contact_name = _split_relation(args[2:])
            target = " ".join(target_tokens).strip()
            if not target:
                current = self.app.tasks.get(task_id)
                if current.waiting_on is None:
                    raise FreeAgentError(
                        "改成等候类要说在等什么：/kind <id> wait 客户回复"
                    )
                # 保留原有的 contact_id：只是换形状，不该把人挂掉。
                target = current.waiting_on.who_or_what
                contact_id = current.waiting_on.contact_id
            else:
                contact_id = (
                    self.app.promise.contact(contact_name).id if contact_name else None
                )
            waiting = WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what=target,
                since=self.app.clock.now(),
                contact_id=contact_id,
            )
        task = self.app.tasks.set_kind(task_id, kind, waiting_on=waiting)
        self._say(f"{task.title} → {task.kind.label}｜{task.state.label}")

    def _cmd_wait(self, args: list[str]) -> None:
        """``/wait <id> <等谁> [关系=名字]``。

        ``关系=`` 是可选的（11.19）：给了就把这条等候**挂到关系对象上**，
        于是「我跟张总之间有几笔在等的事」能聚合出来（20.1.5 说的正是
        这件事）。不给也能记 —— 记一句「在等对方回话」不该先被要求
        建一个人。
        """
        if len(args) < 2:
            self._usage("/wait")
            return
        target_tokens, contact_name = _split_relation(args[1:])
        target = " ".join(target_tokens).strip()
        if not target:
            raise FreeAgentError(
                "要说在等什么：/wait <id> <等谁>  [关系=名字]"
            )
        contact_id = None
        if contact_name:
            contact_id = self.app.promise.contact(contact_name).id
        task = self.app.tasks.block(
            self._resolve_id(args[0]),
            WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what=target,
                since=self.app.clock.now(),
                contact_id=contact_id,
            ),
            reason="放一放",
        )
        tail = f"，已挂到关系对象「{contact_name}」" if contact_name else ""
        self._say(
            f"已放一放：{task.title}（等 {task.waiting_on.who_or_what}{tail}）"  # type: ignore[union-attr]
        )

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

    # ---- 记忆（设计文档 3.7）---- #
    def _cmd_remember(self, args: list[str]) -> None:
        """``/remember <内容>`` —— 记一条全局偏好。

        刻意是 ``/memory add 偏好`` 的**快捷方式**而不是第二套实现：
        两者都调同一个 :meth:`MemoryService.add`（12.1.1 一份能力一份实现）。
        """
        if not args:
            self._usage("/remember")
            return
        entry = self.app.memory.add(" ".join(args), kind="偏好", source_ref="user")
        self._say(f"记住了（偏好）：{entry.content}")

    def _cmd_memory(self, args: list[str]) -> None:
        """``/memory`` 命令族。子命令：list / add / add-role / search / stats /
        review / edit / del。

        与 ``/remember`` 共用同一个 :class:`MemoryService` —— 这里只做参数解析
        与呈现，不重复任何写入逻辑。
        """
        sub = args[0] if args else "list"
        rest = args[1:]

        if sub == "list":
            return self._memory_list(rest)
        if sub == "add":
            return self._memory_add(rest)
        if sub == "add-role":
            return self._memory_add_role(rest)
        if sub == "search":
            return self._memory_search(rest)
        if sub == "stats":
            return self._memory_stats(rest)
        if sub == "review":
            return self._memory_review(rest)
        if sub == "edit":
            return self._memory_edit(rest)
        if sub == "del":
            return self._memory_del(rest)
        self._usage("/memory")

    def _memory_list(self, args: list[str]) -> None:
        scope = None
        if args:
            want = args[0]
            if want == "global":
                scope = GLOBAL_SCOPE
            else:
                role = self.app.roles.get_by_name(want)
                scope = role_scope(role.id)
        entries = self.app.memory.list_all(scope=scope)
        if not entries:
            # 空也要明说 —— 与 /week 空分区同一条纪律。
            self._say("还没记过任何东西。用 /remember <内容> 记一条。")
            return
        self._say(f"记过 {len(entries)} 条：")
        for e in entries:
            self._say(f"  [{e.kind}·{self._memory_scope_name(e.scope)}] {e.content}"
                      f"  ({e.id[:8]})")

    def _memory_add(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/memory")
            return
        kind = args[0]
        entry = self.app.memory.add(
            " ".join(args[1:]), kind=kind, source_ref="user"
        )
        self._say(f"记住了（{entry.kind}）：{entry.content}")

    def _memory_add_role(self, args: list[str]) -> None:
        if len(args) < 3:
            self._usage("/memory")
            return
        role = self.app.roles.get_by_name(args[0])
        entry = self.app.memory.add(
            " ".join(args[2:]),
            kind=args[1],
            scope=role_scope(role.id),
            source_ref="user",
        )
        self._say(f"记住了（{role.name}·{entry.kind}）：{entry.content}")

    def _memory_search(self, args: list[str]) -> None:
        if not args:
            self._usage("/memory")
            return
        result = self.app.memory.retrieve(" ".join(args), consumer="/memory search")
        # 来源说明**永远**打出来（含空结果）—— 与知识检索同一条：不只列结果。
        self._say(f"检索：{result.describe_sources()}")
        for h in result.hits:
            e = h.entry
            self._say(f"  [{e.kind}·{self._memory_scope_name(e.scope)}] {e.content}"
                      f"  （{h.via_label}）")
        # 在线腿（V1.105）：本地关键词空手、且用户配了端点时才有话要说。
        # **单列一块**，不与上面的本地条目混排 —— 两类证据可信度不同。
        online = render.render_online_leg(result.online)
        if online:
            self._say(online)

    def _memory_stats(self, args: list[str]) -> None:
        """``/memory stats [天数]`` —— 记忆的**使用留痕**（V1.98 / 3.7）。

        回答的是「哪条记忆好久没用过了」：记过什么由 ``/memory list`` 说，
        用过什么只有留痕知道。没有留痕就没有框架凝练的原料 ——
        V1.96 的教训：没有计数的规律不可反驳。

        **不自动建议删除**：计数 0 只说明这段没人用，不说明它错了
        （3.7.5 的不自动 consolidation 在这里同样成立）。删不删是人决定。
        """
        days = DEFAULT_USAGE_DAYS
        if args:
            raw = args[0]
            if not raw.isdigit() or int(raw) <= 0:
                raise FreeAgentError(
                    f"天数要写正整数（比如 /memory stats 30），收到 {raw!r}"
                )
            days = int(raw)
        report = self.app.memory.usage_report(days=days)
        if not report.rows:
            self._say("还没记过任何记忆 —— 用 /remember 或 /memory add 记一条。")
            return
        head = f"记忆留痕（近 {report.days} 天）：检索 {report.retrievals} 次 · 注入 {report.injections} 次"
        if report.last_event_at is not None:
            head += f" · 最近一次 {report.last_event_at.strftime('%m-%d %H:%M')}"
        self._say(head)
        if report.degraded_reason:
            # 「读不到」与「没有」必须能区分 —— 否则 0 次会被当成真相。
            self._say(f"（{report.degraded_reason}，下面的计数不可信）")
        used, stale = report.used, report.stale
        if used:
            self._say(f"用过的（{len(used)} 条）：")
            for row in used:
                self._say(
                    f"  [{row.entry.kind}·{self._memory_scope_name(row.entry.scope)}]"
                    f" {row.entry.content} —— 检索 {row.retrievals} ·"
                    f" 注入 {row.injections} · 最近"
                    f" {row.last_event_at.strftime('%m-%d') if row.last_event_at else '—'}"
                )
        if stale:
            self._say(f"近 {report.days} 天没用过的（{len(stale)} 条）：")
            for row in stale:
                when = (
                    f"上次 {row.last_event_at.strftime('%m-%d')}"
                    if row.last_event_at else "从未被检索或注入"
                )
                self._say(
                    f"  [{row.entry.kind}·{self._memory_scope_name(row.entry.scope)}]"
                    f" {row.entry.content} —— {when}"
                )
        if not used:
            self._say(
                "（这段时间里没有任何记忆被检索或注入过 —— 要么没在用 /memory search"
                " 与 /ask，要么注入口没带上它。）"
            )
        # 在线腿开关放在最后：它是「我到底开了什么」的交代，不是这次统计的
        # 一部分。开着就说发往哪；关着就说关着 —— 否则人会以为「本地搜不到
        # 就是真没有」，而它默认是不开的。
        self._say(self._online_leg_status())

    def _online_leg_status(self) -> str:
        """在线腿现在是开是关（V1.105 / 3.7.9）。

        放在 ``/memory stats`` 里而不是检索输出里：检索时没开就说一句
        「未启用」是噪声（每搜一次一次），而这个地方本来就是「记账与诊断」
        —— 人会来这里查「我到底开了什么」。
        """
        from ..services.online_leg import ONLINE_URL_ENV, OnlineSearchConfig

        cfg = OnlineSearchConfig.from_env()
        if not cfg.enabled:
            return (
                "在线检索：未启用（默认关闭 —— 本仓默认不联网）。"
                f"要开就配 {ONLINE_URL_ENV}，见使用说明书。"
            )
        if cfg.error:
            return f"在线检索：配置有问题 —— {cfg.error}"
        return (
            f"在线检索：已启用，本地关键词空手时发往 {cfg.host}"
            "（查询原文会离开本机）"
        )

    def _memory_review(self, args: list[str]) -> None:
        """``/memory review [天数]`` / ``/memory review confirm <id>...``
        （V1.100 / 3.7.8）。

        回答的是「该整理了吗」：core 要小才有效，而记下来的东西只变多。
        V1.98 的留痕让「好久没用」成为事实，但**事实不产生动作** ——
        没有人会主动翻 ``/memory stats``，所以候选要自己找上门。

        候选两条口径：**好久没动静**（窗口内没被检索/注入/确认过，连写下
        来都超窗）与**内容相近**（bigram Jaccard ≥ 0.5 的对子 —— 只说相近，
        不说冲突：可能记重了，也可能一条改了另一条没改，分诊留给人）；
        V1.102 再加**只差一处**（首尾极像、中间岔开 —— 改口最可能长这样）。

        **只摆不动**：不自动删、不自动 consolidation（3.7.5 纪律在此原样
        成立）。动作只有两个且都由人发起：``/memory review confirm <id>``
        与既有的 ``/memory del``（一份能力一份实现，不另造删的口子）。
        """
        if args and args[0] == "confirm":
            return self._memory_review_confirm(args[1:])
        days = DEFAULT_REVIEW_DAYS
        if args:
            raw = args[0]
            if not raw.isdigit() or int(raw) <= 0:
                raise FreeAgentError(
                    f"天数要写正整数（比如 /memory review 30），收到 {raw!r}"
                )
            days = int(raw)
        report = self.app.memory.review_candidates(days=days)
        if report.degraded_reason:
            # 读不出来 ≠ 没有候选。说清楚**哪一组**判断不了 —— 两组字面关系
            # 与留痕无关，照摆在下面（V1.102）；一句「判断不了」把它们也
            # 藏起来，等于故障时二次惩罚。
            self._say(
                f"（{report.degraded_reason}，判断不了哪些好久没动静；"
                "「内容相近」「只差一处」两组只看正文，不受影响）"
            )
        elif not report.candidates and not report.near_duplicates \
                and not report.minimal_pairs:
            # 空也要明说 —— 与 /week 空分区、/memory list 空库同一条纪律。
            if report.total == 0:
                self._say(
                    "还没记过任何记忆 —— 用 /remember 或 /memory add 记一条。"
                )
            else:
                self._say(
                    f"近 {report.days} 天没有待整理的候选 —— {report.total} 条"
                    "记忆在窗口内都有过动静（记下 / 检索 / 注入 / 确认）。"
                )
            return
        parts = []
        if report.candidates:
            parts.append(f"{len(report.candidates)} 条好久没动静")
        if report.near_duplicates:
            parts.append(f"{len(report.near_duplicates)} 组内容相近")
        if report.minimal_pairs:
            parts.append(f"{len(report.minimal_pairs)} 对只差一处")
        if not parts:
            # 走到这里只可能是降级分支（非降级的空集合在上面已早退）：
            # 原因已经在上一行说过了，再打一行「（共 N 条）」的空摘要
            # 只是噪音。
            return
        self._say(
            f"记忆 review（近 {report.days} 天）：{' · '.join(parts)}"
            f"（共 {report.total} 条）"
        )
        if report.candidates:
            self._say(f"好久没动静的（{len(report.candidates)} 条，最旧在前）：")
            for row in report.candidates:
                e = row.entry
                # 给**完整 id**：confirm 与 del 都按全 id 认（不搞前缀匹配，
                # 那是任务侧的另一套规矩）。摆出来就要能照着敲。
                self._say(
                    f"  [{e.kind}·{self._memory_scope_name(e.scope)}] {e.content}"
                    f"  ({e.id})\n"
                    f"    —— 最后一次动静"
                    f" {row.last_attention_at.strftime('%m-%d')}，"
                    f"{report.days} 天没被检索/注入/确认"
                )
        if report.near_duplicates:
            self._say(
                f"内容相近的（{len(report.near_duplicates)} 组）—— 相近不一定"
                "是冲突：可能记重了，也可能一条改了另一条没改："
            )
            for pair in report.near_duplicates:
                for e in (pair.left, pair.right):
                    self._say(
                        f"  [{e.kind}·{self._memory_scope_name(e.scope)}]"
                        f" {e.content}  ({e.id})"
                    )
                self._say(
                    f"    —— 相近度 {pair.similarity:.0%}（bigram 重合），"
                    "留哪条、怎么改，你定"
                )
        if report.minimal_pairs:
            self._say(
                f"只差一处的（{len(report.minimal_pairs)} 对）—— 同一个框架、"
                "不同的取值，改口最可能长这样："
            )
            for pair in report.minimal_pairs:
                for e in (pair.left, pair.right):
                    self._say(
                        f"  [{e.kind}·{self._memory_scope_name(e.scope)}]"
                        f" {e.content}  ({e.id})"
                    )
                self._say(
                    f"    —— 只差{self._minimal_diff_label(pair)}，"
                    f"首尾重合 {pair.overlap:.0%}；哪条算数、要不要合，你定"
                )
        self._say(
            "确认还留着：/memory review confirm <id>（可一次多条）"
            "；删掉：/memory del <id>。候选只是摆出来 —— 绝不自动删。"
        )

    def _memory_review_confirm(self, args: list[str]) -> None:
        """``/memory review confirm <id> [<id>...]`` —— 确认还留着。

        一次确认多条：队列的意义就是成批 triage，一条一条敲会把
        「整理一下」变成十轮操作。某条没找到时报错并点名那一条
        （前面的已经生效 —— 与其它批量操作同一条语义）。
        """
        if not args:
            self._usage("/memory")
            return
        for entry_id in args:
            entry = self.app.memory.confirm(entry_id)
            if entry is None:
                raise FreeAgentError(f"没找到这条记忆：{entry_id}")
            self._say(
                f"确认留着：{entry.content}"
                "（退出本轮候选；窗口过后仍没被用过，它还会再出现）"
            )

    @staticmethod
    def _minimal_diff_label(pair: MemoryMinimalPair) -> str:
        """「只差一处」那处差在哪：两边都有就写成对照，一边空就说多出什么。

        一边为空不是数据错误 —— 它正是「一条是另一条的延长 / 截短」那种
        常见形状（「回复先给结论」vs「回复先给结论，再给细节」），
        写成「↔」会读成「这里换了个说法」，反而不准。
        """
        if pair.left_diff and pair.right_diff:
            return f"「{pair.left_diff}」↔「{pair.right_diff}」"
        if pair.right_diff:
            return f"右边多出「{pair.right_diff}」"
        return f"左边多出「{pair.left_diff}」"

    def _memory_edit(self, args: list[str]) -> None:
        if len(args) < 2:
            self._usage("/memory")
            return
        entry = self.app.memory.edit(args[0], " ".join(args[1:]))
        if entry is None:
            raise FreeAgentError(f"没找到这条记忆：{args[0]}")
        self._say(f"改好了：{entry.content}")

    def _memory_del(self, args: list[str]) -> None:
        if not args:
            self._usage("/memory")
            return
        if not self.app.memory.delete(args[0]):
            raise FreeAgentError(f"没找到这条记忆：{args[0]}")
        self._say("删掉了。")

    def _memory_scope_name(self, scope: str) -> str:
        """把 ``role:<id>`` 换成角色名，呈现时更好读。"""
        if scope == GLOBAL_SCOPE:
            return "全局"
        rid = scope.split(":", 1)[1]
        return self._names().get(rid, rid)


    def _cmd_redispatch(self, args: list[str]) -> None:
        """``/redispatch <id> [原因]`` —— 让一条委派可以重新派发。

        刻意**不**做成「``/note`` 的特殊写法」：``/note`` 没有任何校验，
        而这条要挡三种不许重派的情形（正在跑 / 已完成 / 还没派过）。
        混进 ``/note`` 的话，「用错入口就静默无效」又回来了 ——
        而那正是原来那句「可以 /note 记下原因后重派」的病根。
        """
        if not args:
            self._usage("/redispatch")
            return
        task = self.app.tasks.reset_delegation(
            self._resolve_id(args[0]), " ".join(args[1:])
        )
        self._say(f"已标记重派：{task.title}")
        self._say("执行器下次扫到就会再派一次（它是独立进程，不会立刻跑）。")

    def _cmd_brief(self, args: list[str]) -> None:
        """``/brief <id>`` —— 看这条委派**实发的那份**简报（V1.103）。

        为什么需要它：简报是执行器唯一收到的输入，而人此前看不到它、
        也不知道自己「说够没说够」。象限就是那个答案的依据 ——
        它只说「哪一侧手里有材料」，不评判该不该派。

        预览与实发共用 ``assemble_brief``（外层 delegate 模块）：解析刚拼好
        的文本去反推象限是拿文案当契约（11.9.12），而各收集一份零件则
        必然漂移。这里只排版，不收集。
        """
        if not args:
            self._usage("/brief")
            return
        # 局部导入：外层 ``freeagent.delegate`` 会拖进 subprocess / 审批链，
        # 终端启动不该为一条预览付这份加载成本（同 ``_cmd_delegate`` 的写法）。
        from ..delegate import assemble_brief

        task_id = self._resolve_id(args[0])
        task = self.app.tasks.get(task_id)
        assembled = assemble_brief(self.app, task)
        self._say(render.render_brief(assembled.text, assembled.quadrant, task.id))

    def _cmd_pair(self, args: list[str]) -> None:
        """``/pair <配对码>`` —— 核销码，把对方写进飞书白名单。

        码从哪儿来：给 bot 发一条**私聊**，它会回一张带配对码的卡。

        刻意**不在终端生成码** —— 终端不知道对方的 ``open_id``，生成不出
        能用的码。能生成码的地方是桥接（它看得见发消息的人）。

        它与「白名单失败关闭」**不冲突**：配对解决的是「怎么拿到
        open_id」，而白名单为空仍然拒绝启动飞书通道。
        """
        # 函数内导入：``feishu.pairing_cmd`` 属于通道侧，而本文件是核心 CLI
        # —— 顶层导入会让「不装飞书通道」这件事在**导入期**就变成硬依赖。
        # 该模块只依赖 stdlib（secret_store / config 都不 import lark_oapi），
        # 所以这里导入不会把 ``lark-oapi`` 拖进来。
        from ..feishu.pairing_cmd import pair_command

        if not args:
            self._say(
                "用法：/pair <配对码>。\n"
                "码从哪儿来：给 bot 发一条私聊，它会回一张带配对码的卡。\n"
                "（群里不会发 —— 群里发卡等于向全群确认「这里有个 bot」。）"
            )
            return
        # home 取 ``app.config.home``：它跟着 ``--db`` 走，所以配对写进的
        # feishu.env 与桥接读的是**同一份**。取错目录的症状是「配对说成功了，
        # 但还是进不来」—— 而那正是本次要治的病。
        home = self.app.config.home if self.app.config is not None else None
        self._say(pair_command(self.app.conn, args[0], home=home))

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
        # 验收是人看执行器产出的时刻 —— 该沉淀的「世界的事实」（11.9.11）
        # 在这里最清楚。只提示不代记：写库必须显式（EXPLICIT_ONLY），
        # 静默替人记一条约定和静默挂关系账是同一种越权。
        if accepted.model:  # type: ignore[union-attr]
            self._say(
                "验收里如果有该沉淀为约定的（如项目禁用某库、客户不接受某类改动），"
                "用 /memory add 约定 <内容> 记下来 —— 约定会进以后的委派简报。"
            )

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
    # 12.11.2 / V1.39：每日滚动备份。三个进程入口各调**一行**，逻辑只在
    # services 层一份（:func:`daily_backup`：当日一次、留 7 份、失败自己
    # 记日志不抛）。从**活连接**问路径（``db_path_of``）而不是重新解析 --
    # -- 显式 --db 时重新解析可能得到另一个文件，备份就备到别的库上了。
    from ..services.reset import daily_backup, db_path_of
    try:
        daily_backup(db_path_of(repl.app.conn))
    except Exception as exc:  # noqa: BLE001 - 备份失败绝不挡住主流程
        print(f"⚠ 今日备份没能执行（{exc}）；功能不受影响，明天会再试。")
    try:
        repl.run()
    finally:
        repl.app.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
