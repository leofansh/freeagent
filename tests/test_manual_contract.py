"""《使用说明书》防漂移。

手册是最容易烂掉的文档：命令表抄错一个、阈值写错一个，用户就照着错的东西操作。
这里把手册里**可被机器验证**的事实钉死在实现上。

不可机器验证的散文（建议、比喻、限制说明）不在本文件职责内 ——
那是人写的，靠 review，不靠测试假装保证。
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from freeagent import config as cfg
from freeagent.cli.app import _USAGE
from freeagent.services.llm.providers import all_key_env_vars
from freeagent.services.reminders import MISSED_AFTER
from freeagent.storage.db import DEFAULT_HOME_ENV
from freeagent.web import server as web_server

ROOT = Path(__file__).resolve().parents[1]
MANUAL_PATH = ROOT / "docs" / "使用说明书.md"


@pytest.fixture(scope="module")
def manual() -> str:
    assert MANUAL_PATH.exists(), "使用说明书必须存在"
    return MANUAL_PATH.read_text(encoding="utf-8")


# --- 命令表 -----------------------------------------------------------------

#: ``/skip`` 是**追问状态下的会话内逃逸口**，不是注册命令：
#: 它只在 ``Repl._handle_pending`` 里被识别，故意不进 ``_USAGE``/``/help``，
#: 否则会污染命令表（大部分时候它没有意义）。
#: 手册必须提到它（用户在提示里会看到），但它不算漏文档。
CONVERSATIONAL_ESCAPES = {"/skip"}

#: **只在飞书通道存在**的命令：它们不在 ``_USAGE`` 里，因为终端没有
#: 交互卡片，敲了确实没反应 —— 但在飞书里是真实可用的，所以手册必须写。
#:
#: 与 :data:`CONVERSATIONAL_ESCAPES` 是两回事，区别要说清楚：
#: ``/skip`` 是「**有**注册命令表、但只在追问这个状态下才认」；
#: ``/menu`` 是「**任何时候都认，但只在飞书通道里」—— 它压根不进终端。
#:
#: 为什么不能把它塞进 ``_USAGE``：那会让 ``/help`` 和命令速查表声称
#: 终端有个 ``/menu``，而终端里按下去是静默无响应 —— 那恰好是本文件
#: 想防的「让用户敲一个永远没反应的东西」。
#:
#: 为什么不从手册里删掉：它是真实功能，删了等于让用户不知道它存在。
#: 手册同时覆盖终端与飞书通道（见飞书通道那几节），所以列它是诚实的。
CHANNEL_ONLY_COMMANDS = {"/menu"}


def _commands_in(text: str) -> set[str]:
    """抽出手册里所有反引号包裹的斜杠命令 token。"""
    found = set(re.findall(r"`(/[a-z][a-z-]*)`", text))
    found |= set(re.findall(r"`(/[a-z][a-z-]*)(?: [^`]*)`", text))
    # 反引号表格里成组出现的裸命令，例如 ```/start``` `/pause` `/done` `/drop` <id>
    found |= set(re.findall(r"^\s*(/start|/pause|/done|/drop)\b", text, re.M))
    return found


def test_manual_has_no_unknown_commands(manual: str) -> None:
    """手册不能提到不存在的命令 —— 那会让用户敲一个永远没反应的东西。"""
    known = set(_USAGE) | CONVERSATIONAL_ESCAPES | CHANNEL_ONLY_COMMANDS
    unknown = sorted(_commands_in(manual) - known)
    assert not unknown, f"手册提到不存在的命令：{unknown}"


def test_manual_documents_every_command(manual: str) -> None:
    """每个注册命令都必须在手册里出现 —— 不能有 undocumented 的隐藏功能。"""
    mentioned = _commands_in(manual)
    missing = sorted(set(_USAGE) - mentioned)
    assert not missing, f"这些命令没写进手册：{missing}"


def test_manual_command_count_is_accurate(manual: str) -> None:
    """手册声称的命令条数必须等于实际条数。"""
    claim = re.search(r"命令速查（(\d+) 条）", manual)
    assert claim, "手册应声明命令总条数，便于发现漂移"
    assert int(claim.group(1)) == len(_USAGE), (
        f"手册说 {claim.group(1)} 条，实际 {len(_USAGE)} 条"
    )


def test_manual_mentions_conversational_escape(manual: str) -> None:
    """``/skip`` 只在追问时有效，必须在手册里说明它不是普通命令。"""
    assert "/skip" in manual
    assert "追问" in manual or "跳过" in manual


# --- 环境变量 ---------------------------------------------------------------

#: 飞书通道的变量名**从代码里导入**，不在这里手抄一份 ——
#: 手抄的话改了代码名，手册契约测试还会绿着，而文档已经过期了。
from freeagent.feishu.config import (  # noqa: E402
    ENV_ALLOWED,
    ENV_APP_ID,
    ENV_APP_SECRET,
    ENV_DOMAIN,
    ENV_POLL,
)

EXPECTED_ENV_VARS = {
    cfg.API_KEY_ENV,
    cfg.MODEL_ENV,
    cfg.BASE_URL_ENV,
    cfg.TIMEOUT_ENV,
    cfg.DISCLAIM_PROVIDER,
    cfg._ALLOW_FALLBACK_ENV,
    cfg.PROVIDER_ENV,
    DEFAULT_HOME_ENV,
    # 飞书通道（可选能力，但它有环境变量，手册就得写）
    ENV_APP_ID,
    ENV_APP_SECRET,
    ENV_ALLOWED,
    ENV_DOMAIN,
    ENV_POLL,
    # 各服务商的 Key 变量名**从注册表推导**，不逐个抄。
    # 抄的那份会漂 —— 加了 provider 忘了同步这里，手册就漏写一个变量名，
    # 用户照着设了却毫无效果。推导的来源和 llm.env 白名单是同一个函数，
    # 所以「代码里有」和「手册写了」在结构上就不会分叉。
    *all_key_env_vars(),
}


def test_manual_documents_every_env_var(manual: str) -> None:
    mentioned = set(re.findall(r"`([A-Z][A-Z0-9_]{4,})(?:=[^`]*)?`", manual))
    missing = sorted(EXPECTED_ENV_VARS - mentioned)
    assert not missing, f"这些环境变量没写进手册：{missing}"


def test_manual_has_no_invented_env_var(manual: str) -> None:
    """手册不能提到代码里不存在（也不生效）的环境变量。"""
    known = EXPECTED_ENV_VARS | {"PYTHONPATH", "PYTHONIOENCODING"}
    mentioned = {
        v
        for v in re.findall(r"`([A-Z][A-Z0-9_]{4,})(?:=[^`]*)?`", manual)
        if v.endswith(("_KEY", "_MODEL", "_URL", "_TIMEOUT", "_HOME", "_ONLY", "_FALLBACK"))
    }
    invented = sorted(mentioned - known)
    assert not invented, f"手册提到不存在的环境变量：{invented}"


def test_manual_stress_tests_key_comes_from_env_only(manual: str) -> None:
    """Key 只能走环境变量 —— 手册必须把这条边界讲清楚。"""
    assert cfg.API_KEY_ENV in manual
    assert "api_key" in manual and ("不生效" in manual or "绝不" in manual)


def test_manual_documents_settings_page(manual: str) -> None:
    """设置页是新增能力，手册必须写清能改什么、不能改什么。"""
    assert "设置页" in manual
    # 不能改的那一栏必须包含 Key
    assert "API Key" in manual
    # 必须说清换 Key 绕不开重启（进程环境变量改不了）
    assert "重启" in manual
    # 必须提醒环境变量会覆盖设置页
    assert "DEEPSEEK_MODEL" in manual and "覆盖" in manual or "盖掉" in manual


def test_manual_does_not_claim_ui_can_set_key(manual: str) -> None:
    """手册不能给出「在界面填 Key」的错误指引。"""
    for bad in ("在设置页填 Key", "界面里填 Key", "设置页输入 Key"):
        assert bad not in manual, f"手册给出了错误的 Key 操作指引：{bad}"


def test_manual_documents_input_rejection(manual: str) -> None:
    """手册必须写明白白话入口**仍然**会拒绝哪几类输入。

    不写的话用户会以为「说人话」什么都能干，然后发现助手「不听话」——
    而实际发生的是助手在保护他的数据不被垃圾记录污染。

    原本是**三类**，其中「问句」那一类已于 2026-09-29 撤销：问句改成直接
    回答（设计文档 12.1.1）。所以现在只剩两类 —— 改已有事务、追问时答非
    角色名。这类断言必须跟着实现走，否则它自己就成了过时的文档。
    """
    assert "助手现在会拒绝的两类输入" in manual
    for needle in ("/role-add", "/skip", "/today-pin"):
        assert needle in manual, f"拒绝规则应提到 {needle}"


def test_manual_does_not_say_questions_are_rejected(manual: str) -> None:
    """手册**不许**再说「问句会被拒绝」—— 现在它会直接回答。

    与上一条互为镜像：文档漂移有两个方向，说多了（谎称能）会让人白敲命令，
    说少了（谎称不能）会让人以为坏了。两边都要锁。
    """
    for bad in ("这是问句 → 让你用", "问句 → 让你用", "助手会拒绝问句"):
        assert bad not in manual, f"手册仍在说问句被拒绝：{bad}"


def test_manual_says_questions_are_answered(manual: str) -> None:
    """反过来也要写明：问句是**答的**，且终端/飞书同样适用。

    漏这句的后果实测过 —— 用户以为只能敲命令，于是在飞书里问
    「今天该做什么」却被回一句「查看用命令：/today」。
    """
    assert "问句是答的，不是拒绝的" in manual
    assert "终端和飞书" in manual, "应说明终端/飞书同样可以直接问"


def test_manual_documents_chat_entry(manual: str) -> None:
    """手册必须写清对话能问什么、不能做什么。

    这是用户上手的第一个界面，能力边界不写清楚就会「试一下发现它啥也不会」，
    或者反过来「以为它能改，结果它拒了以为是坏了」。
    """
    assert "能问" in manual and "不能做" in manual
    for question in ("今天该做什么", "我在等什么", "有什么提醒"):
        assert question in manual, f"能问的清单应包含 {question}"
    assert "认不准就反问" in manual, "必须说明它的反问行为"
    # 边界要写清是刻意的，不是没做完
    assert "刻意的边界" in manual


def test_manual_documents_vision(manual: str) -> None:
    """拍照识物是新增能力，手册必须写清三件容易踩坑的事。

    1. 搜索只是**深链**，不是内置电商接口 —— 否则用户以为是后者
    2. 图片**不落盘** —— 隐私边界，必须说
    3. 识别不准时**宁可说读不出也不编型号** —— 买错件的真实后果
    """
    assert "拍照识物" in manual
    assert "深链" in manual and "内置搜索" in manual, "要说清搜索是深链"
    assert "不落盘" in manual, "必须说明图片不落盘"
    assert "没读出型号" in manual, "必须说明不会编造型号"
    assert "8 MB" in manual, "要给出体积上限"
    for target in ("京东", "淘宝"):
        assert target in manual, f"应说明能跳{target}"


def test_manual_keeps_chapter_structure(manual: str) -> None:
    """章节顺序不能被我插内容插乱。"""
    import re

    chapters = re.findall(r"^## ([一二三四五六七八九十]+)、", manual, re.M)
    assert chapters == list("一二三四五六七八九十"), f"章节乱了：{chapters}"


def test_manual_documents_multi_turn(manual: str) -> None:
    """多轮指代是「像豆包」的关键，手册必须写清怎么用、什么情况下不成立。"""
    assert "能接着问" in manual
    assert "第N个" in manual, "要给出指代的写法"
    assert "指代只在" in manual, "必须说明没有上文时不成立"


def test_manual_says_boundary_holds_across_turns(manual: str) -> None:
    """多轮下不改数据的边界要写明 —— 有上下文后最容易顺势下命令。"""
    assert "多轮下也不松" in manual
    assert "把它标完成" in manual


# --- 阈值 -------------------------------------------------------------------


def test_manual_missed_reminder_threshold_matches_code(manual: str) -> None:
    """手册写的「超过 6 小时标已错过」必须等于代码里的阈值。"""
    assert MISSED_AFTER == timedelta(hours=6)
    assert "6 小时" in manual


def test_manual_default_port_matches_code(manual: str) -> None:
    from freeagent.web.server import DEFAULT_PORT

    assert str(DEFAULT_PORT) in manual, f"手册应写对默认端口 {DEFAULT_PORT}"


# --- Web 能力边界 -----------------------------------------------------------


#: 界面上每个状态动作的**用户可见名字**。
#: 手册是给用户看的，不该逼他记 ``blocked`` 这种接口词 ——
#: 但必须能对上号，否则用户看到按钮却找不到手册里的说法。
_UI_LABEL_FOR_ACTION = {
    "start": "开始做",
    "done": "做完啦",
    "pause": "等会再做",
    "drop": "算了",
    "blocked": "放一放",
}


def test_manual_web_write_whitelist_matches_server(manual: str) -> None:
    """手册声称「界面能做 / 做不了」必须与 server 的白名单一致。"""
    from freeagent.web.actions import STATE_ACTIONS

    for action in STATE_ACTIONS:
        label = _UI_LABEL_FOR_ACTION[action]
        assert action in manual, f"手册没提界面的状态动作 {action}"
        assert label in manual, f"手册没给出 {action} 在界面上的名字「{label}」"
    assert "排期" in manual and "移出排期" in manual

    # 界面做不到的，必须在手册里被列为「只能去终端」
    for cmd in ("/move", "/dod", "/kind", "/accept", "/merge"):
        assert cmd in manual, f"手册没说明 {cmd} 只能去终端"


def test_manual_does_not_claim_web_does_everything(manual: str) -> None:
    """反面：手册必须承认界面有做不到的事，否则等于骗用户。"""
    assert "Web 做不到的" in manual or "只能去终端" in manual


# --- 安全 -------------------------------------------------------------------


def test_manual_contains_no_api_key(manual: str) -> None:
    """手册绝不能内嵌真实 Key。"""
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", manual), "手册里出现了疑似真实 API Key"
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", MANUAL_PATH.read_text(encoding="utf-8"))


def test_manual_documents_loopback_only(manual: str) -> None:
    """无鉴权的安全前提是只绑回环，手册必须说明，否则用户会内网暴露。"""
    assert "127.0.0.1" in manual
    assert "局域网" in manual


def test_manual_documents_backup(manual: str) -> None:
    """单文件数据库、无自动备份 —— 这是真实的运维风险，手册必须写。"""
    assert "备份" in manual
    assert ".db" in manual
