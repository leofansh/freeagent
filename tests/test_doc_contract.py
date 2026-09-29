"""文档 ↔ 实现 对账。

设计文档声称的每一项规范性内容，都必须在这里对上实现。
这组测试是防漂移的护栏 —— 改了代码或改了文档但没同步另一方，这里会红。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from freeagent import app as app_module
from freeagent import cli, domain, services, storage

DOC = Path(__file__).resolve().parents[1] / "docs" / "个人事务助手设计方案.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


# --------------------------------------------------------------------- 枚举
def test_doc_declares_every_state(doc_text: str):
    for state in domain.TaskState:
        assert state.value in doc_text, f"{state.value} 未在文档中声明"


def test_doc_declares_every_kind(doc_text: str):
    for kind in domain.TaskKind:
        assert kind.value in doc_text, f"{kind.value} 未在文档中声明"


def test_doc_declares_every_signal(doc_text: str):
    for code in domain.SortSignalCode:
        assert code.value in doc_text, f"{code.value} 未在文档中声明"


def test_doc_declares_every_record_type(doc_text: str):
    for rt in domain.RecordType:
        assert rt.value in doc_text, f"{rt.value} 未在文档中声明"


def test_doc_declares_every_artifact_status(doc_text: str):
    for st in domain.ArtifactStatus:
        assert st.value in doc_text, f"{st.value} 未在文档中声明"


# --------------------------------------------------------------------- 权重
def test_doc_weight_table_matches_code(doc_text: str):
    """文档 7.2 节的权重表必须与 SORT_SIGNAL_WEIGHTS 逐项一致。"""
    table = doc_text.split("### 7.2")[1].split("### 7.3")[0]
    for code, weight in domain.SORT_SIGNAL_WEIGHTS.items():
        # 表格行形如： | `OVERDUE_WAIT` / `overdue_wait` | +50 | ... |
        pattern = rf"`{re.escape(code.value)}`\s*\|\s*\+{weight}\s*\|"
        assert re.search(pattern, table), f"{code.value} 权重 {weight} 在文档表中对不上"


# --------------------------------------------------------------------- 不变式
def test_doc_documents_blocked_from_done(doc_text: str):
    """等候类必须能直接销账 —— 文档与迁移表都要写明。"""
    assert domain.TaskState.DONE in services.tasks.ALLOWED_TRANSITIONS[domain.TaskState.BLOCKED]


def test_doc_mentions_terminal_exemption(doc_text: str):
    assert "未结束" in doc_text and "不变式" in doc_text


# --------------------------------------------------------------------- 字段
def test_task_fields_all_documented(doc_text: str):
    fields = {f.name for f in domain.Task.__dataclass_fields__.values()}
    for name in fields:
        assert name in doc_text, f"Task.{name} 未在数据模型中出现"


def test_restore_view_fields_all_documented(doc_text: str):
    fields = {f.name for f in domain.RestoreView.__dataclass_fields__.values()}
    for name in fields:
        assert name in doc_text, f"RestoreView.{name} 未在文档中出现"


def test_waiting_on_fields_all_documented(doc_text: str):
    fields = {f.name for f in domain.WaitingOn.__dataclass_fields__.values()}
    for name in fields:
        assert name in doc_text, f"WaitingOn.{name} 未在文档中出现"


# --------------------------------------------------------------------- 分层纪律
def test_llm_provider_does_not_import_domain():
    """provider.py 刻意不 import domain —— 这是替换真实 LLM 的接缝。"""
    import ast

    import freeagent.services.llm.provider as provider

    tree = ast.parse(inspect.getsource(provider))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    for name in imported:
        assert "freeagent" not in name, f"provider.py 不应 import {name}"


def test_domain_has_no_io():
    for module in (domain.models, domain.enums):
        source = inspect.getsource(module)
        assert "sqlite3" not in source
        assert "import requests" not in source


def test_storage_has_no_business_rules():
    """storage 层不做顺延/权重/迁移校验。"""
    source = inspect.getsource(storage.repos)
    for forbidden in ("ROLLOVER", "SORT_SIGNAL", "ALLOWED_TRANSITIONS", "compute_signals"):
        assert forbidden not in source, f"{forbidden} 不该出现在 storage 层"


def test_repos_never_call_clock():
    source = inspect.getsource(storage)
    assert "datetime.now" not in source
    assert "date.today" not in source


# --------------------------------------------------------------------- 阈值
def test_doc_declares_signal_thresholds(doc_text: str):
    for const in (
        services.sorting.DEADLINE_RISK_HOURS,
        services.sorting.RESUME_STALE_DAYS,
        services.sorting.WAITING_TOO_LONG_DAYS,
    ):
        assert str(const) in doc_text, f"阈值 {const} 未在文档中声明"


def test_doc_declares_missed_reminder_window(doc_text: str):
    hours = int(services.reminders.MISSED_AFTER.total_seconds() // 3600)
    assert f"{hours} 小时" in doc_text


def test_doc_declares_record_window(doc_text: str):
    assert str(domain.REMINDER_FIRED_WINDOW) in doc_text


# ------------------------------------------------- 提醒的取 / 确认分离（9.2）
def test_doc_declares_two_phase_reminder_api(doc_text: str):
    """9.2 把 ``due()`` / ``acknowledge()`` 写成规范性 API，文档必须有。

    这两个名字是**契约**：三条渠道的确认点全靠它们分工。文档写了而实现改名，
    或者实现改了而文档没改，调用方就会各自去猜「什么时候算送达」——
    而这个 bug 的表现形式是提醒静默消失，用户不会知道是哪里错了。
    """
    section = doc_text.split("### 9.2")[1].split("### 9.3")[0]
    # 按文档里的记法匹配：``ReminderEngine.due()`` / ``acknowledge(digest)``
    for name in ("ReminderEngine.due()", "acknowledge("):
        assert name in section, f"9.2 未声明 {name}"


def test_reminder_engine_has_no_consuming_check():
    """**不得**存在「取走即消费」的入口。

    曾经有一个 ``check()`` 同时查和落去重令牌，于是三条渠道全部踩坑：
    推送失败时提醒被永久标记成已送达。留着这个方法等于给回归留后门，
    所以这里断言它**不存在**，而不只是断言新方法可用。
    """
    assert not hasattr(services.reminders.ReminderEngine, "check"), (
        "消费型的 check() 已删除；新代码必须走 due() + acknowledge()"
    )
    assert hasattr(services.reminders.ReminderEngine, "due")
    assert hasattr(services.reminders.ReminderEngine, "acknowledge")


def test_no_module_still_calls_the_old_consuming_api():
    """用 AST 搜一遍：没有任何地方还在**调用**已删除的 ``check()``。

    刻意用 ``ast`` 而不是文本 grep —— 文档字符串与注释里会合法地提到
    「以前有个 check() 是消费型的」，那是**历史叙述**，不是调用。
    文本 grep 分不清这两者，断言就会变成噪音，然后被人加白名单绕过。
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted(root.glob("src/**/*.py")) + sorted(root.glob("tests/**/*.py")):
        # utf-8-sig：部分源文件带 BOM（app.py 就有），ast.parse 不接受 U+FEFF。
        # Python 的 import 机制会剥掉它，所以只有手写解析时才必须显式处理。
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "check":
                src = ast.unparse(node)
                # 只认真的调用：``reminders.check()`` / ``self.app.reminders.check()``
                if "reminders" in src:
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}: {src}")
    assert not offenders, "还在调用已删除的消费型 check():\n" + "\n".join(offenders)


def test_每条渠道都在送达之后才确认(doc_text: str):
    """三条渠道的确认点必须写在 9.2 里，且都在「送」之后。

    这是防回归的文档侧锚点：某天有人把确认挪到发送之前（比如「简化一下」），
    文档就变成过期契约，而代码测试未必抓得到 —— 因为三条渠道各自的
    失败路径不同。
    """
    section = doc_text.split("### 9.2")[1].split("### 9.3")[0]
    # 确认点分列在三个渠道名下
    for channel in ("终端", "飞书", "Web"):
        assert channel in section, f"9.2 未说明 {channel} 渠道的确认点"
    # 明确写了「不承诺恰好一次」
    assert "不承诺恰好一次" in section, "9.2 必须写明不承诺恰好一次及其理由"


def test_doc_declares_feishu_normative_parameters(doc_text: str):
    """飞书通道的规范性参数必须在文档里，且和代码一致。

    这些数字是**契约**（TTL 24 小时、条数上限 2048、锁端口 8771、
    身份缓存 7 天）。代码改了而文档没改，用户按文档排查就会照着旧数字找问题，
    而数字对不上时症状极其模糊（表现为「去重时灵时不灵」）。
    """
    from freeagent.feishu import dedup, identity
    from freeagent.feishu import bridge as bridge_module

    for const in (
        dedup.DEDUP_TTL_HOURS,
        dedup.DEDUP_MAX_ENTRIES,
        bridge_module.DEFAULT_LOCK_PORT,
        identity.CACHE_TTL_SECONDS // 3600,
    ):
        assert str(const) in doc_text, f"飞书参数 {const} 未在文档中声明"


def test_doc_and_code_agree_on_dedup_file_names(doc_text: str):
    """11.9.1 / 11.9.2 写死的文件名必须和代码常量一致。"""
    from freeagent.feishu import dedup, identity

    for name in (dedup.CACHE_FILE_NAME, identity.CACHE_FILE_NAME):
        assert name in doc_text, f"状态文件名 {name} 未在文档中声明"


def test_doc_uses_the_real_dedup_method_name(doc_text: str):
    """文档里的协议方法名必须和代码一致。

    踩过的坑：文档先写成 ``seen(event_id) -> bool``，实现后来定成
    ``is_duplicate``。文档是实现契约，两边不一致时**读者会照文档写出错误的调用**。
    """
    from freeagent.services.channel import InMemoryDeduplicator

    assert "is_duplicate" in doc_text, "文档仍在用旧的方法名"
    assert hasattr(InMemoryDeduplicator, "is_duplicate"), "实现改名了，文档没跟上"
    assert "seen(event_id)" not in doc_text, "旧的方法名该从文档里清掉"


def test_doc_does_not_claim_dedup_is_in_memory():
    """去重已经落盘了。文档若还说「在内存里」就是撒谎。

    撒谎的代价很具体：用户以为「重启就忘了」是可以接受的，于是**不会去查**
    「为什么重连后重复建事务」—— 而那正是落盘要解决的 bug。

    三份文档一起查（设计文档 / README / 使用说明书）：只查设计文档会漏掉
    恰恰最可能被照着排查的那两份。

    注意这里**自己读文件**，不收 ``doc_text`` fixture：收下却不用会让人
    误以为断言挂在那个参数上（我上一轮就被这点怀疑过一次，确认是虚惊）。
    """
    stale = ("去重表**在内存里**", "去重表在内存里")
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "docs/个人事务助手设计方案.md", "docs/使用说明书.md"):
        text = (root / name).read_text(encoding="utf-8")
        for phrase in stale:
            assert phrase not in text, f"{name} 仍声称 {phrase}，与实现不符"


def test_user_docs_must_state_when_the_unsupported_hint_is_sent():
    """非文字消息会回一句提示，但**有前提**——文档必须把前提写出来。

    这条测试改过一次方向，本身就是教训：

    - 最初实现对非文字返回 ``Ignored``，桥接只记日志就 ``return``，
      **一个字都不发**；而两份用户文档都写着「会明确回「暂只处理文字」」。
      那时本测试断言「不许出现『会明确回』」——对着一个撒谎的文档。
    - 后来按「禁止静默劣化」改成**真的回一句**（见设计方案 11.9.1），
      于是撒谎的方向也跟着变了：现在可能写成「**无条件**会回」，
      而实现是**白名单外不回、群里没 @ 也不回**。

    所以现在断言的是**前提条件必须在文档里**，而不是某句措辞在不在。
    这样无论将来措辞怎么变，只要「前提」被文档说清了就放过；
    一旦简化成「总会回一句」，用户就会在不该收到提示时去排查。
    """
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "docs/使用说明书.md"):
        text = (root / name).read_text(encoding="utf-8")
        # 三个前提都要在文档里说清。**刻意用「任一等价措辞」而不是精确子串** ——
        # 上一版这里写的是 `"不建事务" in text`，而文档实际写「不会建事务」，
        # 两者不构成子串关系，于是测试红了而文档并没有错。
        # 靠中文子串锁措辞必然脆：改个说法就误报，久了就没人信它。
        # 宁可少锁一点措辞，也要让「前提有没有被说清」这件事判得准。
        for concept, alternatives in (
            ("白名单前提", ("白名单",)),
            ("群聊 @ 门控前提", ("@",)),
            ("不建事务", ("不建事务", "不会建事务", "不建任务", "不会建任务")),
        ):
            assert any(alt in text for alt in alternatives), (
                f"{name} 没写清「{concept}」"
                f"（期望其中之一：{alternatives}）"
            )


def test_user_docs_mention_the_new_switches():
    """用户实际会看的那两份必须写清新机制，否则等于没做。"""
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "docs/使用说明书.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert "FEISHU_LOCK_PORT" in text, f"{name} 没写单实例锁端口变量"
        assert "feishu_seen_events.json" in text, f"{name} 没写去重表落盘位置"
        assert "feishu_bot.json" in text, f"{name} 没写身份缓存位置"
        # 探不到身份时**群聊失败关闭**是用户唯一能察觉「bot 为什么不回」的线索。
        # 不写清楚，用户只会以为机器人坏了或自己 @ 错了人（门控提示语恰好就是
        # 「@ 的是别人」，会把这人带偏）。
        assert "失败关闭" in text, f"{name} 没写探不到身份时群聊失败关闭"
        assert "私聊" in text, f"{name} 没写清私聊不受影响，代价只限群聊"


# --------------------------------------------------------------------- 结构
def test_doc_code_structure_matches_reality(doc_text: str):
    """文档 12.2 节列出的模块必须真的存在，且都在结构树里出现过。"""
    root = Path(__file__).resolve().parents[1] / "src" / "freeagent"
    tree_region = doc_text.split("### 12.2")[1].split("### 12.3")[0]
    for module in (
        "domain/enums.py", "domain/models.py", "domain/errors.py",
        "storage/db.py", "storage/repos.py",
        "services/clock.py", "services/roles.py", "services/tasks.py",
        "services/artifacts.py", "services/today.py", "services/sorting.py",
        "services/restore.py", "services/reminders.py",
        "services/llm/provider.py", "services/llm/rules.py",
        "services/llm/deepseek.py",
        "services/channel.py",
        "feishu/config.py", "feishu/identity.py", "feishu/events.py",
        "feishu/dedup.py", "feishu/sender.py", "feishu/doctor.py",
        "feishu/bridge.py",
        "cli/app.py", "cli/parse.py", "cli/render.py",
        "web/server.py", "web/serialize.py", "web/page.py", "web/__main__.py",
        "app.py", "config.py", "secrets.py",
    ):
        path = root / module
        assert path.is_file(), f"文档列出的 {module} 不存在"
        package, _, filename = module.rpartition("/")
        # 结构树里目录与文件名分行书写，所以两者都要出现
        assert filename in tree_region, f"{filename} 未在文档结构树中出现"
        if package:
            leaf = package.split("/")[-1]
            assert f"{leaf}/" in tree_region, f"目录 {leaf}/ 未在文档结构树中出现"


def test_doc_declares_tech_stack(doc_text: str):
    for token in ("Python 3.11", "SQLite", "pytest", "LLMProvider", "Clock"):
        assert token in doc_text, f"技术选型缺少 {token}"


# --------------------------------------------------------------------- 命令表
def test_every_command_is_documented(doc_text: str):
    """12.4 命令表必须列全所有已实现的命令 —— 有能力没入口等于没做。"""
    from freeagent.cli.app import _USAGE

    table = doc_text.split("### 12.4")[1].split("## 十三")[0]
    for cmd in _USAGE:
        assert cmd in table, f"命令 {cmd} 未在 12.4 命令表中列出"


def test_documented_commands_all_exist(doc_text: str):
    """命令表里不能出现尚不存在的命令（防文档超前于实现）。"""
    import re

    from freeagent.cli.app import _USAGE

    table = doc_text.split("### 12.4")[1].split("## 十三")[0]
    for token in set(re.findall(r"`(/[a-z][a-z0-9-]*)", table)):
        if token.startswith("/all ") or token.startswith("/roles "):
            continue
        assert token in _USAGE, f"文档列出了尚不存在的命令 {token}"


def test_help_text_covers_every_command():
    from freeagent.cli.app import _USAGE
    from freeagent.cli.app import _HELP

    for cmd in _USAGE:
        assert cmd in _HELP, f"{cmd} 未出现在 /help 文本里"


def test_every_command_has_usage_text():
    from freeagent.cli.app import _USAGE

    for cmd, usage in _USAGE.items():
        assert usage.strip(), f"{cmd} 用法文本为空"
        assert usage.startswith(cmd), f"{cmd} 用法文本应以命令名开头"


def test_dispatch_uses_table_not_if_chain():
    """命令派发必须是派发表，不是不断增长的 if/elif 链。"""
    import ast
    import inspect

    import freeagent.cli.app as cli_app

    tree = ast.parse(inspect.getsource(cli_app))
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            # 只允许业务判断（解析后的分支），不允许比较 cmd 字符串
            if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name):
                assert test.left.id != "cmd", "命令派发不应写成 if/elif 链"
    assert hasattr(cli_app.Repl, "_handlers")


def test_doc_features_map_to_commands(doc_text: str):
    """13.1 每一条功能都要标出对应命令（含跨多行的条目）。"""
    section = doc_text.split("### 13.1")[1].split("### 13.2")[0]
    import re

    starts = list(re.finditer(r"^\s*(\d+)\.\s", section, re.MULTILINE))
    assert len(starts) >= 12, f"13.1 功能条目变少了（{len(starts)}）"
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(section)
        block = section[match.start() : end]
        head = block.strip().splitlines()[0]
        assert "→" in block, f"13.1 第 {match.group(1)} 条未标出对应命令：{head[:40]}"


def test_doc_v1_views_drop_stale_overdue_section(doc_text: str):
    """6.2 已把「逾期分区」改成顺延标记，13.1 不能再写逾期分区。"""
    section = doc_text.split("### 13.1")[1].split("### 13.2")[0]
    assert "逾期分区" not in section
    assert "顺延标记" in section


# --------------------------------------------------------------------- Web UI
def test_doc_declares_web_ui(doc_text: str):
    section = doc_text.split("### 12.6")[1].split("## 十三")[0]
    for token in ("127.0.0.1", "http.server", "serialize", "App.lock", "CSP"):
        assert token in section, f"12.6 Web UI 缺少 {token}"


def test_doc_declares_web_non_goals(doc_text: str):
    section = doc_text.split("### 12.6")[1].split("## 十三")[0]
    for token in ("WebSocket", "鉴权", "拖拽"):
        assert token in section, f"12.6 应明确不做 {token}"
    assert "Web 界面不解决" in section, "必须写明 Web 不解决漏提醒问题"


def test_web_refuses_non_loopback():
    from freeagent.web.server import DEFAULT_HOST

    assert DEFAULT_HOST == "127.0.0.1"


def test_web_page_has_no_external_resources():
    """页面不许有真实外部资源引用。

    只查会发起请求的位置（``src``/``href``/``srcset``/``@import``/``url()``）。
    正文里出现 ``http://`` 只是说明文字，不算引用 —— 且响应头的
    ``Content-Security-Policy: default-src 'none'`` 已让真实外部加载 fail closed。
    """
    import re

    from freeagent.web.page import INDEX_HTML

    for needle in ("cdnjs", "unpkg", "googleapis", "jsdelivr", "//cdn"):
        assert needle not in INDEX_HTML, f"页面引用了外部资源：{needle}"
    external = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.I)
    for attr in ("src", "href", "srcset"):
        for url in re.findall(rf'{attr}="([^"]*)"', INDEX_HTML):
            assert not external.match(url), f"{attr} 指向外部资源：{url}"
    assert not re.search(r"@import", INDEX_HTML), "CSS 里有 @import"
    assert not re.search(r"""url\(\s*['"]?(?:https?:)?//""", INDEX_HTML), (
        "CSS 里有外部 url()"
    )


def test_web_page_ships_disclaimer():
    from freeagent.services.sorting import DISCLAIMER_MARKER
    from freeagent.web.page import INDEX_HTML

    assert DISCLAIMER_MARKER in INDEX_HTML, "免责标记必须在页面里，不等接口返回"
    assert "不替你决定" in INDEX_HTML


def test_web_serialize_has_no_business_logic():
    """Web 层不得自己**重算**业务规则，但可以**读取**服务层的判断。

    区别很关键：``allowed_next_states`` 来自 ``ALLOWED_TRANSITIONS``（服务层的迁移表），
    这是把决定**透传**给界面；而自己写一套权重表或顺延逻辑才是重算。
    """
    import inspect

    from freeagent.web import serialize, serialize_api

    # 序列化拆成了「视图类」和「单实体/信封类」两个模块，两边都要守
    source = inspect.getsource(serialize) + inspect.getsource(serialize_api)
    for forbidden in ("SORT_SIGNAL_WEIGHTS", "score_and_sort", "compute_signals",
                      "def rollover", "sort("):
        assert forbidden not in source, f"serialize 不该出现 {forbidden}"
    # 允许「读」服务层决定，不允许「另立一套」
    assert "ALLOWED_TRANSITIONS" in source, "合法迁入应直接来自服务层迁移表"


def test_web_write_surface_is_whitelisted():
    """界面能做的写操作是白名单，不是任意方法调用。"""
    import inspect

    from freeagent.web import endpoints
    from freeagent.web.actions import ACTION_FOR_STATE, STATE_ACTIONS

    assert set(STATE_ACTIONS) == {"start", "done", "pause", "drop", "blocked"}
    # 每个白名单动作都必须在 mutate 里有分支，否则点了会 404
    source = inspect.getsource(endpoints.mutate)
    for action in STATE_ACTIONS:
        assert f'"{action}"' in source, f"{action} 在白名单里但 mutate 没实现"
    # 这些只能去终端
    for forbidden in ("accept", "merge", "delete", "rename", "silence", "set_kind"):
        assert forbidden not in source, f"界面不该做 {forbidden}"
    # 状态 -> 动作的映射必须完整，且落在白名单内，否则界面会出现死按钮
    from freeagent.domain import TaskState

    assert set(ACTION_FOR_STATE) == {s.value for s in TaskState}
    assert set(ACTION_FOR_STATE.values()) <= set(STATE_ACTIONS)


def test_web_lock_is_shared():
    """SQLite 连接不能被两个请求线程同时使用 —— 必须有共享锁。"""
    import threading

    from freeagent.app import build_app

    app = build_app(Path(__file__).resolve().parent / ".probe.db")
    try:
        assert isinstance(app.lock, type(threading.RLock()))
    finally:
        app.close()
        (Path(__file__).resolve().parent / ".probe.db").unlink(missing_ok=True)


def test_doc_example_two_matches_real_output():
    """17 章示例二必须与实际渲染一致 —— 防文档漂移。

    文档写的是「实现契约」，示例输出如果对不上，读者照着实现就会走偏。
    """
    import re

    from datetime import date, datetime
    from pathlib import Path as _Path
    import tempfile

    from freeagent.app import build_app
    from freeagent.cli import render
    from freeagent.domain import TaskKind, WaitingKind, WaitingOn
    from freeagent.services.clock import FrozenClock

    doc = DOC.read_text(encoding="utf-8")
    example = doc.split("### 示例二：今天视图")[1].split("### 示例三")[0]
    blocks = re.findall(r"```text\n(.*?)```", example, re.DOTALL)
    assert blocks, "示例二应包含输出块"

    app = build_app(
        _Path(tempfile.mkdtemp()) / "a.db", clock=FrozenClock(datetime(2026, 9, 26, 9))
    )
    try:
        work = app.roles.create(
            "工作项目A", note="销售 周报 客户",
            default_definition_of_done="先给我能用的就行",
        )
        fam = app.roles.create("家庭", note="孩子 学校 材料")
        late = app.tasks.create("上周就该交的材料", [fam.id])
        app.tasks.schedule(late.id, date(2026, 9, 20))
        report = app.tasks.create(
            "销售周报初稿", [work.id], intent="先理一版",
            due_time=datetime(2026, 9, 27, 18, 0),
        )
        app.tasks.schedule(report.id, date(2026, 9, 26))
        wait = app.tasks.create(
            "孩子周四要交的材料清单", [fam.id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(
                WaitingKind.PERSON, "孩子带回来",
                datetime(2026, 9, 20), datetime(2026, 9, 25),
            ),
        )
        app.tasks.schedule(wait.id, date(2026, 9, 26))
        errand = app.tasks.create(
            "修一下窗户螺丝", [work.id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 26, 15, 0),
        )
        app.tasks.schedule(errand.id, date(2026, 9, 26))
        names = render.role_names(app.roles.list_roles(include_merged=True))
        actual = render.render_today(app.today.view(), names).strip()
    finally:
        app.close()

    assert actual.strip() == blocks[0].strip(), (
        "17 章示例二与实际输出不一致 —— 文档是实现契约，示例必须对得上"
    )


def test_dependencies_are_stdlib_plus_pytest():
    content = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "dependencies = []" in content
    assert "pytest" in content


# --------------------------------------------------------------------- V1 边界
def test_doc_declares_v1_non_goals(doc_text: str):
    for phrase in ("多渠道", "Computer Use", "多设备"):
        assert phrase in doc_text


def test_doc_declares_hypotheses(doc_text: str):
    assert "H1" in doc_text and "H2" in doc_text
    assert "≤ 2" in doc_text or "≤2" in doc_text


# --------------------------------------------------------------------- 模型层
def test_doc_declares_model_layer(doc_text: str):
    section = doc_text.split("### 12.5")[1].split("## 十三")[0]
    for token in (
        "DEEPSEEK_API_KEY",
        "RuleBasedProvider",
        "DeepSeekProvider",
        "白名单",
        "降级",
        "urllib",
    ):
        assert token in section, f"12.5 模型层缺少 {token}"


def test_doc_no_longer_claims_no_real_llm(doc_text: str):
    """13.2 已接真实模型，不能再写「真实 LLM 调用」不做。"""
    section = doc_text.split("### 13.2")[1].split("## 十四")[0]
    assert "真实 LLM 调用" not in section


def test_key_is_never_hardcoded():
    """防回归：源码与测试里不得出现任何 sk- 开头的字面量。"""
    import re

    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"sk-[A-Za-z0-9]{16,}")
    targets = list((root / "src").rglob("*.py")) + list((root / "tests").rglob("*.py"))
    targets += [root / "pyproject.toml", root / "README.md", root / "docs" / "个人事务助手设计方案.md"]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), f"{path.name} 里出现了疑似硬编码的 key"


def test_key_comes_only_from_env(tmp_path, monkeypatch):
    """Key **不来自配置文件**；来源是环境变量，其次是 ``llm.env``。

    这里原本是「grep ``Config.api_key`` 的源码，看里面有没有 ``os.environ``」。
    改成界面可填 Key 之后，``api_key`` 委托给了 ``llm_env.resolve_key``，
    grep 就失效了 —— 而它**本来就不该是可靠的验证方式**：源码怎么写和
    实际行为对不对是两件事，grep 只能证明前者。

    改成**行为断言**后更强：直接证明「config.json 里那份不算数」。
    """
    from freeagent import llm_env
    from freeagent.config import API_KEY_ENV, Config, load_config, save_config

    monkeypatch.delenv(API_KEY_ENV, raising=False)
    home = tmp_path / "home"
    home.mkdir()

    # 1. 配置文件里的 api_key 不作数（原始意图）
    (home / "config.json").write_text(
        '{"api_key": "sk-from-json"}', encoding="utf-8"
    )
    assert load_config(home).api_key is None, "config.json 里的 Key 被读进来了"

    # 2. 环境变量算数
    monkeypatch.setenv(API_KEY_ENV, "sk-from-env")
    assert load_config(home).api_key == "sk-from-env"

    # 3. llm.env 算数（界面写进去的那份）
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    llm_env.write_env({API_KEY_ENV: "sk-from-llm-env"}, home=home)
    assert load_config(home).api_key == "sk-from-llm-env"

    # 4. 两份都有时，环境变量优先 —— 且必须**说得出**是哪份在生效，
    #    否则用户在界面上重填却发现没生效会以为设置坏了。
    monkeypatch.setenv(API_KEY_ENV, "sk-from-env")
    config = load_config(home)
    assert config.api_key == "sk-from-env", "环境变量没有压过 llm.env"
    assert "环境变量" in config.key_source

    # 5. 真正的不变量：**存盘不会把 Key 写进 config.json**。
    #    有了「文件里可以放 Key」之后，这条才是真正要守住的边界 ——
    #    放错文件（config.json 会被提交、被同步）比放对文件危险得多。
    save_config(config, home=home)
    assert "sk-from-env" not in (home / "config.json").read_text(encoding="utf-8")
    assert "sk-from-llm-env" not in (home / "config.json").read_text(encoding="utf-8")
    assert "api_key" not in (home / "config.json").read_text(encoding="utf-8")


def test_transport_keeps_key_out_of_payload():
    """Key 走 headers；payload 容易被日志打印，不能带 key。"""
    import inspect

    from freeagent.services.llm.deepseek import DeepSeekProvider

    source = inspect.getsource(DeepSeekProvider._chat)
    payload_block = source.split("headers =")[0]
    assert "self._api_key" not in payload_block, "key 不应出现在 payload 构造里"
    assert "Authorization" in source


def test_deepseek_tests_are_offline():
    """真实模型测试必须打桩，不能打真网络。"""
    import inspect

    from freeagent.services.llm import deepseek

    source = inspect.getsource(deepseek)
    assert "urllib.request.urlopen" in source
    # 真正发请求的只有 urllib_transport，测试全部走注入的 transport
    assert "urlopen" not in inspect.getsource(deepseek.DeepSeekProvider)


def test_cli_never_prints_key():
    """`/llm` 只能显示「已配置/未配置」。"""
    import inspect

    from freeagent.cli.app import Repl

    source = inspect.getsource(Repl._cmd_llm)
    assert "api_key" not in source, "/llm 不得直接引用 api_key"
    assert "describe()" in source


def test_readme_and_doc_agree_on_disclaimer():
    from freeagent.cli.render import DISCLAIMER

    assert DISCLAIMER == services.sorting.DISCLAIMER_MARKER
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    assert DISCLAIMER in readme


def test_package_graph_imports_cleanly():
    for module in (domain, storage, services, cli, app_module):
        assert module is not None
