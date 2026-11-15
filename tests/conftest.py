"""测试夹具：临时库 + 固定时钟。

时间必须可注入，否则顺延、提醒、排序全都无法确定性测试。

## 为什么要全局隔离 ``FREEAGENT_HOME``（回归）

配置现在有**文件兜底**（``merged_env()``：环境变量优先，``feishu.env`` 补缺）。
在那之前配置只来自环境变量，所以测试天然封闭。现在不隔离就会读到开发者
真实的 ``~/.freeagent/feishu.env`` —— 症状是「本机配过之后，『缺配置应报错』
的测试反而通过了」，而且**要等真的配过飞书才会发作**：这正是它当初能混过去
的原因。

同理 ``FEISHU_LOCK_PORT``：测试若用默认 8771，而本机正跑着真桥接，
就会撞成「端口已被占用」—— 于是「点启动应报配置不足」这条测试报出的是
「端口被占」，断言全歪。
"""

from __future__ import annotations

import socket
from datetime import date, datetime
from pathlib import Path

import pytest

from freeagent.app import App, build_app
from freeagent.services import opencode_server
from freeagent.services.clock import FrozenClock
from freeagent.services.executors import adapter_for
from freeagent.services.sorting import EnergyWindows


def pytest_addoption(parser: pytest.Parser) -> None:
    """注册 ``--run-mutants``。

    **为什么要有这个开关**：变异探针会**改源码文件**（改完立刻还原）。
    它证明守卫真的会咬，但代价是「跑全量的时候源码在变」——
    而本仓有一条硬纪律：``test_doc_contract.py`` 用
    ``inspect.getsource()`` **从磁盘现读**，一边改一边跑会读到别的函数
    （实测报出 ``_cmd_llm`` 的源码是 ``_cmd_role``）。
    所以默认跳过，要跑得显式开。
    """
    parser.addoption(
        "--run-mutants",
        action="store_true",
        default=False,
        help="跑变异探针（会临时改源码，跑完还原）",
    )


def _free_port() -> int:
    """借操作系统给一个当前没人用的端口。

    bind(0) 让系统挑，然后把号记下来。**端口号是给测试用的**，所以不释放也
    无妨 —— 真正要防的是「和本机在跑的服务撞号」，而这个号此刻是空的。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@pytest.fixture(scope="session", autouse=True)
def _isolate_feishu_config(tmp_path_factory: pytest.TempPathFactory):
    """把飞书配置与锁端口**整体挪出**开发者的真实环境。

    session 级 + autouse：这是**默认**该有的状态，而不是每个用例各写一遍
    （那漏一个就是一个只在「本机配过」时才发作的坑）。
    需要真实默认路径的用例自己 ``monkeypatch.delenv``（见 test_config.py）。
    """
    import os

    home = tmp_path_factory.mktemp("feishu_home")
    old_home = os.environ.get("FREEAGENT_HOME")
    old_port = os.environ.get("FEISHU_LOCK_PORT")
    os.environ["FREEAGENT_HOME"] = str(home)
    os.environ["FEISHU_LOCK_PORT"] = str(_free_port())
    try:
        yield home
    finally:
        for key, value in (("FREEAGENT_HOME", old_home),
                           ("FEISHU_LOCK_PORT", old_port)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _pin_opencode_adapter_to_factory_default():
    """每个用例都从出厂适配器（V1）开始，结束时回到它。

    ``OpenCodeServer.start()`` 会把**进程级**协议适配器采纳为本机装的版本
    （``_adopt_installed_adapter``：本机装了 opencode V2，就翻成 V2）—— 那是
    长驻进程要的行为，但对测试是越界的全局副作用：一个用例触发过真实探测，
    后面拿 V1 假数据说话的用例就全对不上，症状是「单跑绿、全量红」
    （2026-10-16 排查全量 86 红时确认）。

    一个已知触发路径是 ``build_app() → _capability_view → list_projects``；
    那条空项目路径已不再空转探测（见 app.py），这里仍是兜底：凡是要换
    适配器的用例，自己 ``set_current_adapter`` 显式钉住，并在用例里写清
    它模拟的是哪个版本 —— 隐式依赖别的用例留下的全局状态，正是上面那个坑。
    """
    factory = adapter_for(1)
    assert factory is not None, "V1 适配器必须存在"
    opencode_server.set_current_adapter(factory)
    try:
        yield
    finally:
        opencode_server.set_current_adapter(factory)


@pytest.fixture(autouse=True)
def _no_online_search_leg(monkeypatch: pytest.MonkeyPatch):
    """默认把在线检索腿关掉（V1.105 / 3.7.9）。

    这条腿读**进程环境变量**（每次检索现读，故意不缓存）。于是开发者本机
    一旦配了 ``FREEAGENT_ONLINE_SEARCH_URL``，所有走 ``/memory search`` 的
    测试都会真的往外发一次查询 —— 症状是测试变慢、依赖网络，而在没配的
    机器上（CI）**永远看不出来**。与 ``FREEAGENT_HOME`` 那条同一个理由：
    要等真的配过才发作的坑，就该在默认状态里堵掉。

    要测这条腿的用例自己 ``setenv`` 并注入 transport（永不联网）。
    """
    for name in (
        "FREEAGENT_ONLINE_SEARCH_URL",
        "FREEAGENT_ONLINE_SEARCH_TIMEOUT",
        "FREEAGENT_ONLINE_SEARCH_MAX",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture()
def clock() -> FrozenClock:
    # 2026-09-26 是周六
    return FrozenClock(datetime(2026, 9, 26, 9, 0))


@pytest.fixture()
def app(tmp_path: Path, clock: FrozenClock) -> App:
    application = build_app(
        tmp_path / "agent.db", clock=clock, energy_windows=EnergyWindows()
    )
    yield application
    application.close()


@pytest.fixture()
def roles(app: App) -> dict:
    """一套贴近文档示例的角色。"""
    return {
        "work": app.roles.create(
            "工作项目A",
            note="销售 周报 客户",
            default_definition_of_done="先给我能用的就行",
        ),
        "family": app.roles.create("家庭", note="孩子 学校 材料"),
        "errand": app.roles.create("跑腿杂项", note="窗户 螺丝 修"),
    }


@pytest.fixture()
def today(clock: FrozenClock) -> date:
    return clock.today()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """跑完把「有多少条没验证」单独写一段，别让人把末行当全绿。

    为什么需要它（V1.106）：本仓的跳过**全部是环境缺失**（没装 node 或
    opencode），而 ``test_web_js_syntax.py`` 是唯一守 web 包 JS 语法的地方。
    这类跳过在输出里只有 ``N passed, M skipped`` 里的一个小 M ——
    人读到「passed」就收工，于是「这一路从没验证过」被记成了「通过」。

    不改退出码：缺 node 的机器本来就跑不了，判红只会让人关掉这个守卫。
    按 ``postmortem/lenses.md``「漏判代价远小于错判」——只让它看得见。

    放在 conftest 而不是某个测试文件里：它要覆盖**每一次**运行。单文件跑时
    也会打印，那时它说的是「这次只验证了这些」，仍然是事实。
    """
    skipped = list(terminalreporter.stats.get("skipped") or [])
    if not skipped:
        return

    reasons: dict[str, int] = {}
    for report in skipped:
        raw = report.longrepr
        if isinstance(raw, tuple) and len(raw) >= 3:
            text = str(raw[2])
        elif raw is not None:
            text = str(raw).strip().splitlines()[-1]
        else:
            text = ""
        text = text.removeprefix("Skipped: ").strip() or "（没有写明原因）"
        reasons[text] = reasons.get(text, 0) + 1

    terminalreporter.write_sep("!", f"本次有 {len(skipped)} 条测试没有验证")
    for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])):
        terminalreporter.write_line(f"  {count} 条 · {reason}")
    terminalreporter.write_line(
        "  这些条件说的是「这台机器上缺什么」，不是「这条不重要」。"
        "装上那个依赖再跑一次，才算验证过。"
    )
