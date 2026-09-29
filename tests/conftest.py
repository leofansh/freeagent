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
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows


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
