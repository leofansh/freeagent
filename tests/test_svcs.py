"""后台服务控制：状态探测、启停、边界。

真机进程检测留一条**不 mock** 的（守扫描命令本身没写错），其余用
替身 —— 那些要造「在跑/没在跑」的确定状态，不能依赖开发机上恰好
有什么进程（与 test_reset 同一分工）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from freeagent.services import svcs


def _patch_scan(monkeypatch, table: dict[str, list[tuple[int, str]]]):
    monkeypatch.setattr("freeagent.services.svcs._scan", lambda: table)


_BRIDGE = [("4242", "python -m freeagent.feishu.bridge --verbose")]


class TestListStates:
    def test_running_service_is_reported(self, monkeypatch) -> None:
        _patch_scan(monkeypatch, {"bridge": [(4242, "python -m freeagent.feishu.bridge")],
                                  "executor": []})
        states = {s.key: s for s in svcs.list_states()}
        assert states["bridge"].running is True
        assert states["bridge"].pid == 4242
        assert states["executor"].running is False
        assert states["executor"].pid is None

    def test_state_comes_from_scan_not_registry(self, monkeypatch) -> None:
        """状态**只**来自扫描：Web 重启后不认识旧实例也要照样显示在跑。"""
        _patch_scan(monkeypatch, {
            "bridge": [(99, "python -m freeagent.feishu.bridge")],
            "executor": [(100, "python -m freeagent.delegate --tool-gate --watch 5")],
        })
        states = {s.key: s for s in svcs.list_states()}
        assert states["bridge"].running and states["bridge"].pid == 99
        assert states["executor"].running and states["executor"].pid == 100
        assert states["bridge"].started_by_page is False


class TestStartStop:
    def test_start_when_running_refuses(self, monkeypatch) -> None:
        _patch_scan(monkeypatch, {"bridge": [(7, "x freeagent.feishu.bridge x")],
                                  "executor": []})
        with pytest.raises(svcs.ServiceRefused) as ei:
            svcs.start("bridge")
        assert "已经在跑" in str(ei.value) and "7" in str(ei.value)

    def test_stop_when_stopped_refuses(self, monkeypatch) -> None:
        _patch_scan(monkeypatch, {"bridge": [], "executor": []})
        with pytest.raises(svcs.ServiceRefused, match="本来就没在跑"):
            svcs.stop("bridge")

    def test_stop_kills_only_scanned_pids(self, monkeypatch) -> None:
        """只杀扫描命中的 pid —— 绝不给「杀任意 pid」的口子。"""
        killed: list[int] = []
        _patch_scan(monkeypatch, {
            "bridge": [(11, "freeagent.feishu.bridge"), (12, "freeagent.feishu.bridge")],
            "executor": [],
        })
        monkeypatch.setattr("freeagent.services.svcs._kill",
                            lambda pid: killed.append(pid))
        svcs.stop("bridge")
        assert sorted(killed) == [11, 12]

    def test_unknown_key_refused(self) -> None:
        with pytest.raises(svcs.ServiceRefused, match="不认识的服务"):
            svcs.start("hermes")   # 登记表里没有：未接线就不给按钮


class TestRestart:
    def test_restart_when_running_kills_then_starts(self, monkeypatch) -> None:
        killed: list[int] = []
        started: list[str] = []
        _patch_scan(monkeypatch, {"bridge": [(21, "freeagent.feishu.bridge")],
                                  "executor": []})
        monkeypatch.setattr("freeagent.services.svcs._kill",
                            lambda pid: killed.append(pid))
        monkeypatch.setattr("freeagent.services.svcs._start_bridge",
                            lambda: started.append("bridge"))
        svcs.restart("bridge")
        assert killed == [21]
        assert started == ["bridge"]

    def test_restart_when_stopped_just_starts(self, monkeypatch) -> None:
        started: list[str] = []
        _patch_scan(monkeypatch, {"bridge": [], "executor": []})
        monkeypatch.setattr("freeagent.services.svcs._start_bridge",
                            lambda: started.append("bridge"))
        svcs.restart("bridge")
        assert started == ["bridge"]


class TestRealMachineScan:
    """**真机**扫描链路：WMI/POSIX 查询 → 解析 → 按特征分组。

    为什么不起一个带标记的真进程来测，而不是断言「开发机上服务在跑」：
    后者把 ambient 状态当契约 —— 2026-10-11 就这么裂过：用户从页面停了
    执行器（当时它正好有启动 bug），测试红成「应扫到执行器」，而扫描
    逻辑毫无问题。自包含的探针不依赖机器上恰好有什么。
    """

    def test_scan_finds_a_marked_process(self) -> None:
        import subprocess as sp
        import time

        proc = sp.Popen(
            [sys.executable, "-c",
             "import time; time.sleep(30)", "freeagent.delegate"],
        )
        try:
            found = svcs._scan()
            pids = [pid for pid, _cmd in found.get("executor", [])]
            assert proc.pid in pids, (
                f"扫描没认出刚起的带标记进程：{proc.pid} vs {found}"
            )
        finally:
            proc.kill()
            proc.wait(timeout=10)


class TestExecutorSpawnPaths:
    """执行器启动的路径推导。

    回归（2026-10-11 真机点出来的）：``_repo_root`` 写成 ``parents[2]``
    得到 ``src/``，于是日志指到不存在的目录（Errno 2）、PYTHONPATH 变
    ``src/src`` —— 页面启动执行器必失败，而桥接（走 supervisor）却是
    好的。以下三条把每个派生量钉住。
    """

    def test_repo_root_is_the_project_root(self) -> None:
        root = svcs._repo_root()
        assert (root / "pyproject.toml").is_file(), (
            f"_repo_root 指错了：{root}（应含 pyproject.toml）"
        )
        assert (root / "src" / "freeagent").is_dir()

    def test_spawn_argv_and_env(self, tmp_path, monkeypatch) -> None:
        """捕获 Popen 的实参：cwd 是仓库根、PYTHONPATH 指向真 src。"""
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kw):
                captured["argv"] = argv
                captured.update(kw)
                self.pid = 999

        monkeypatch.setattr("freeagent.services.svcs._scan",
                            lambda: {"bridge": [], "executor": []})
        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        svcs.start("executor")
        root = svcs._repo_root()
        assert captured["argv"][1:3] == ["-u", "-m"] or "-m" in captured["argv"]
        assert captured["argv"][-4:] == [
            "freeagent.delegate", "--tool-gate", "--watch", "5",
        ], "页面启动的命令必须与 tools/_svc_executor.cmd 一致"
        assert Path(captured["cwd"]) == root
        assert Path(captured["env"]["PYTHONPATH"]) == root / "src"
        assert (root / "src").is_dir(), "PYTHONPATH 指向的目录必须存在"
        assert Path(captured["stdout"].name).parent == root / "data"
