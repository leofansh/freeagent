"""桌面入口与便携包构建的守卫（V1.144）。

守三件事，都是「错了但没人报红」的那类：

1. **窗口必须能等**：``--user-data-dir`` 一旦漏掉，``--app`` 只是把 URL 交给
   已运行的浏览器，父进程立刻退出 —— 服务变成没人停得掉的幽灵。这条只能靠
   断言命令行形状守住。
2. **核心零依赖**：``desktop`` 与构建脚本都只许标准库（构建脚本另需本仓自己）。
   AST 扫 import，而不是靠自觉。
3. **构建 fail-closed**：运行时缺 ``pythonw.exe``、``_pth`` 没加 ``..\\app``，
   都要在构建期炸，不能等到用户双击。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest

from freeagent import desktop

REPO = Path(__file__).resolve().parent.parent
BUILD_PATH = REPO / "tools" / "pack" / "build_portable.py"


def _load_build():
    spec = importlib.util.spec_from_file_location("build_portable", BUILD_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_module():
    return _load_build()


class _Response:
    def __init__(self, body: bytes):
        self._body = body
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class TestBrowserDiscovery:
    def test_candidates_cover_edge_and_chrome(self, monkeypatch):
        monkeypatch.setenv("PROGRAMFILES", r"C:\Program Files")
        monkeypatch.setenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
        names = [p.name for p in desktop._candidates()]
        assert names.count("msedge.exe") == 3
        assert names.count("chrome.exe") == 3

    def test_no_env_means_no_candidates(self, monkeypatch):
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            monkeypatch.delenv(key, raising=False)
        assert desktop._candidates() == []
        assert desktop.find_browser() is None


class TestWindowCommand:
    def test_app_mode_and_isolated_profile(self, tmp_path):
        cmd = desktop.window_command(Path("msedge.exe"), "http://127.0.0.1:8770/", tmp_path)
        assert cmd[0] == "msedge.exe"
        assert "--app=http://127.0.0.1:8770/" in cmd
        # 少了这一条就等不到「人把窗口关了」—— 见模块 docstring 第 2 条。
        assert any(arg.startswith("--user-data-dir=") for arg in cmd)
        assert f"--user-data-dir={tmp_path}" in cmd

    def test_no_new_window_flag(self, tmp_path):
        """``--app`` 已经决定了窗口形态；再加 ``--new-window`` 是两套说法。"""
        cmd = desktop.window_command(Path("chrome.exe"), "http://x/", tmp_path)
        assert "--new-window" not in cmd


class TestProbe:
    def test_accepts_our_health_payload(self):
        seen = []

        def opener(url, timeout=0):
            seen.append(url)
            return _Response(b'{"ok": true, "llm": "RuleBasedProvider"}')

        assert desktop.probe_running("http://127.0.0.1:8770/", opener=opener) is True
        assert seen == ["http://127.0.0.1:8770/api/health"]

    def test_foreign_service_is_not_ours(self):
        def opener(url, timeout=0):
            return _Response(b'{"hello": "world"}')

        assert desktop.probe_running("http://127.0.0.1:8770/", opener=opener) is False

    def test_ok_false_is_not_ours(self):
        def opener(url, timeout=0):
            return _Response(b'{"ok": false}')

        assert desktop.probe_running("http://127.0.0.1:8770/", opener=opener) is False

    def test_failure_means_not_running(self):
        def opener(url, timeout=0):
            raise ConnectionError("refused")

        assert desktop.probe_running("http://127.0.0.1:8770/", opener=opener) is False

    def test_response_is_closed(self):
        resp = _Response(b'{"ok": true}')

        def opener(url, timeout=0):
            return resp

        desktop.probe_running("http://127.0.0.1:8770/", opener=opener)
        assert resp.closed is True, "socket 不关，反复探测会漏 fd"


class TestLogging:
    def test_log_lands_in_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
        desktop._log("窗口没起来：测试")

        text = (tmp_path / desktop.LOG_NAME).read_text(encoding="utf-8")
        assert "窗口没起来：测试" in text

    def test_log_failure_does_not_raise(self, tmp_path, monkeypatch):
        """日志写不进去也不能反过来挡住服务。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")   # 同名文件 → mkdir 会失败
        monkeypatch.setenv("STEWARD_HOME", str(blocker))
        desktop._log("x")  # 不抛即通过


class TestStdioBinding:
    """pythonw 下 stdout/stderr 是 None —— 访问日志一写就 AttributeError，
    客户端看到的是空回复（curl 报 52）。打包版第一次真跑撞上过。"""

    def test_none_streams_are_rebound_to_the_log(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
        out, err = sys.stdout, sys.stderr
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        desktop._bind_stdio()
        bound = sys.stdout

        assert bound is not None and sys.stderr is not None
        try:
            print("127.0.0.1 - GET /api/health 200")
            bound.flush()
            text = (tmp_path / desktop.LOG_NAME).read_text(encoding="utf-8")
            assert "GET /api/health 200" in text
        finally:
            # 句柄由测试自己关：留到 GC 时 tmp 目录已被清掉，close 会抛 unraisable。
            sys.stdout, sys.stderr = out, err
            bound.close()

    def test_does_nothing_when_streams_exist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
        before = sys.stdout

        desktop._bind_stdio()

        assert sys.stdout is before
        assert not (tmp_path / desktop.LOG_NAME).exists()


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # 相对导入 = 本仓内部
            if node.module:
                out.add(node.module.split(".")[0])
    return out


class TestZeroDependency:
    STDLIB = {
        "__future__", "argparse", "ast", "importlib", "io", "json", "os", "shutil",
        "subprocess", "sys", "threading", "time", "types", "urllib", "webbrowser",
        "zipfile", "pathlib", "collections", "dataclasses", "datetime", "contextlib", "re",
    }

    def test_desktop_imports_are_stdlib_or_local(self):
        foreign = _imports(REPO / "src" / "freeagent" / "desktop.py") - self.STDLIB
        assert not foreign, f"desktop 引入了第三方：{sorted(foreign)}"

    def test_build_script_imports_are_stdlib_or_local(self):
        foreign = _imports(BUILD_PATH) - self.STDLIB
        assert not foreign, f"构建脚本引入了第三方：{sorted(foreign)}"

    def test_desktop_declares_no_tkinter(self):
        """打包版是 embeddable 发行版 —— 它**不含 Tcl/Tk**，用 tkinter 会到用户机器上炸。"""
        text = (REPO / "src" / "freeagent" / "desktop.py").read_text(encoding="utf-8")
        code = text.split('"""', 2)[2]
        assert "tkinter" not in code


class TestBuildScript:
    def test_launcher_points_at_desktop_main(self, build_module):
        assert "from freeagent.desktop import main" in build_module.LAUNCHER
        assert "sys.exit(main())" in build_module.LAUNCHER

    def test_batch_files_are_ascii(self, build_module):
        """cmd.exe 会把 UTF-8 批处理读错行（run_web.bat 踩过）。"""
        build_module.INSTALL_CMD.encode("ascii")
        build_module.UNINSTALL_CMD.encode("ascii")

    def test_uninstall_keeps_user_data(self, build_module):
        text = build_module.UNINSTALL_CMD
        assert "untouched" in text
        assert ".steward" in text
        assert "rmdir /S /Q \"%USERPROFILE%\\.steward\"" not in text

    def test_install_creates_shortcut_with_pythonw(self, build_module):
        text = build_module.INSTALL_CMD
        assert "pythonw.exe" in text, "快捷方式指向 python.exe 就会闪一个黑窗"
        assert "CreateShortcut" in text
        assert "[char]34" in text, "嵌套引号必须用 [char]34，否则 cmd 会吃掉它们"

    def test_external_tools_use_full_paths(self, build_module):
        """PATH 被裁剪过的机器上，xcopy / powershell 都不在 PATH 里（V1.144 实测
        install.cmd 第一步就失败）。System32 里这两个必然在，所以走全路径。"""
        install = build_module.INSTALL_CMD.lower()
        uninstall = build_module.UNINSTALL_CMD.lower()
        assert "%systemroot%\\system32\\xcopy.exe" in install
        assert "%systemroot%\\system32\\windowspowershell\\v1.0\\powershell.exe" in install
        assert "%systemroot%\\system32\\windowspowershell\\v1.0\\powershell.exe" in uninstall

    def test_uninstall_hands_the_delete_to_a_later_process(self, build_module):
        """cmd.exe 边读 .bat 边执行：rmdir 掉自己所在的目录后，下一行读不出来，
        报「The system cannot find the path specified.」且收尾文案全丢（V1.144
        实测）。所以删除必须交给本进程退出之后才跑的那个 helper。"""
        low = build_module.UNINSTALL_CMD.lower()
        assert "rmdir /s /q" not in low, "不能在脚本里直接删自己所在的目录"
        assert "start " in low
        assert "start-sleep" in low


class TestPthPatch:
    def test_app_dir_is_added(self, tmp_path, build_module):
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        pth = runtime / "python311._pth"
        pth.write_text("python311.zip\n.\n", encoding="utf-8")

        build_module._patch_pth(runtime)

        assert "..\\app" in pth.read_text(encoding="utf-8")

    def test_idempotent(self, tmp_path, build_module):
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        pth = runtime / "python311._pth"
        pth.write_text("python311.zip\n.\n", encoding="utf-8")
        build_module._patch_pth(runtime)
        build_module._patch_pth(runtime)
        assert pth.read_text(encoding="utf-8").count("..\\app") == 1

    def test_missing_pth_is_an_error(self, tmp_path, build_module):
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        with pytest.raises(SystemExit):
            build_module._patch_pth(runtime)


def _fake_runtime_zip(path: Path, *, with_pythonw: bool = True) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("python.exe", b"stub")
        if with_pythonw:
            zf.writestr("pythonw.exe", b"stub")
        zf.writestr("python311.zip", b"stub")
        zf.writestr("python311._pth", "python311.zip\n.\n")
    path.write_bytes(buffer.getvalue())
    return path


class TestBuildAssembles:
    def test_layout_and_version(self, tmp_path, build_module, monkeypatch):
        zip_path = _fake_runtime_zip(tmp_path / "rt.zip")
        package = build_module.build(python_zip=zip_path, out=tmp_path / "dist")

        for name in ("runtime/pythonw.exe", "app/freeagent/app.py",
                     "desktop_main.pyw", "install.cmd", "uninstall.cmd",
                     "VERSION.txt"):
            assert (package / name).exists(), name
        assert "..\\app" in (package / "runtime" / "python311._pth").read_text(encoding="utf-8")
        assert "Steward portable package" in (package / "VERSION.txt").read_text(encoding="utf-8")

    def test_missing_pythonw_fails_the_build(self, tmp_path, build_module):
        """没有 pythonw.exe 就没有「无黑窗」—— 必须在构建期炸。"""
        zip_path = _fake_runtime_zip(tmp_path / "rt.zip", with_pythonw=False)
        with pytest.raises(SystemExit, match="pythonw.exe"):
            build_module.build(python_zip=zip_path, out=tmp_path / "dist")

    def test_rebuild_is_clean(self, tmp_path, build_module):
        zip_path = _fake_runtime_zip(tmp_path / "rt.zip")
        first = build_module.build(python_zip=zip_path, out=tmp_path / "dist")
        (first / "stale.txt").write_text("x", encoding="utf-8")
        second = build_module.build(python_zip=zip_path, out=tmp_path / "dist")
        assert not (second / "stale.txt").exists(), "残留文件会被一起发出去"