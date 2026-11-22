"""组装 Windows 便携包 ``dist/Steward/``（纯标准库，构建期工具）。

跑法（本机开发机上）：

    python tools\\pack\\build_portable.py
    python tools\\pack\\build_portable.py --python-zip D:\\cache\\python-3.11.9-embed-amd64.zip

产物形态（不是 MSI，是一个目录 + 一个 install.cmd）：

    dist\\Steward\\
    ├── runtime\\           官方 embeddable Python（自带 pythonw.exe，无黑窗）
    ├── app\\freeagent\\    本仓 src 的副本
    ├── desktop_main.pyw  启动器：只做「把 app 放进 sys.path，交给 desktop.main」
    ├── install.cmd       装到 %LOCALAPPDATA%\\Steward + 建桌面快捷方式
    ├── uninstall.cmd     删程序，**不动** %USERPROFILE%\\.steward 里的数据
    └── VERSION.txt       版本号与来源

## 为什么是 embbeddable 而不是 PyInstaller

PyInstaller 单文件 exe 是杀软误报的重灾区（每次改代码重新打包，哈希一变就可能
被拦一次），且它把「运行时」变成不可见的一坨。官方 embeddable 是**官方发行版**，
目录里能看见 python.exe / python311.zip，签名与来源都可查。

## 为什么 _pth 里写 ``..\\app``

embeddable 存在 ``python311._pth`` 时**不加载 site、不读 PYTHONPATH**，
sys.path 只认那个文件里列的路径。把 ``..\\app``（相对 ``_pth`` 所在目录，即
``runtime\\``）写进去，启动器就不必自己拼路径 —— 少一处能拼错的地方。

## 什么必须由构建期保证（fail-closed）

- ``runtime\\pythonw.exe`` 必须在 —— 没有它就没有「无黑窗」这件事；
- ``app\\freeagent\\app.py`` 必须在 —— 少一个文件，双击后是 import 错误；
- ``desktop_main.pyw`` 内容必须指向 ``freeagent.desktop.main``（由
  ``tests/test_desktop.py`` 守着）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SRC = REPO / "src" / "freeagent"
CACHE = Path(__file__).resolve().parent / ".cache"
DIST = REPO / "dist"

PY_VERSION = "3.11.9"
# 官方源在前；国内直连 python.org 会卡死（V1.144 实测：.part 一直 0 字节），
# 所以带一条镜像回退。两个文件必须字节一致，sha 不校验，构建期只做 size 下限。
PY_URLS = (
    f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip",
    f"https://mirrors.huaweicloud.com/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip",
)

LAUNCHER = '''"""Steward 桌面启动器：无控制台窗口。

不要在这里加逻辑：真正的东西在 freeagent.desktop。这个文件只被
install.cmd 生成的快捷方式用 pythonw.exe 调起。
"""

import sys

from freeagent.desktop import main

sys.exit(main())
'''

INSTALL_CMD = r'''@echo off
REM Keep this file pure ASCII: cmd.exe mis-reads UTF-8 batch files
REM (same lesson as tools\run_web.bat).
setlocal
set "SRC=%~dp0"
set "DST=%LOCALAPPDATA%\Steward"

if exist "%DST%\runtime\pythonw.exe" (
    echo Steward is already installed at "%DST%".
    echo Remove it first with uninstall.cmd in that folder, or just use the shortcut.
    pause
    exit /b 1
)

echo Installing Steward to "%DST%" ...
REM Full paths on purpose: on a machine whose PATH was trimmed, xcopy and
REM powershell are not on it and this script dies at step one. Both always
REM exist under System32, so we do not rely on PATH.
"%SystemRoot%\System32\xcopy.exe" "%SRC%runtime" "%DST%\runtime" /E /I /Y /Q >nul
"%SystemRoot%\System32\xcopy.exe" "%SRC%app" "%DST%\app" /E /I /Y /Q >nul
copy /Y "%SRC%desktop_main.pyw" "%DST%" >nul
copy /Y "%SRC%VERSION.txt" "%DST%" >nul
copy /Y "%SRC%uninstall.cmd" "%DST%" >nul
if not exist "%DST%\runtime\pythonw.exe" goto failed

REM Create the desktop shortcut from a PowerShell one-liner. Paths travel in
REM environment variables so the command line itself contains no paths; the
REM only quotes needed are built with [char]34, because nesting " inside
REM "-Command " through cmd.exe is where this normally breaks.
set "STEWARD_TARGET=%DST%\runtime\pythonw.exe"
set "STEWARD_ARGS=%DST%\desktop_main.pyw"
set "STEWARD_LNK=%USERPROFILE%\Desktop\Steward.lnk"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $l=$ws.CreateShortcut($env:STEWARD_LNK); $l.TargetPath=$env:STEWARD_TARGET; $l.Arguments=[char]34+$env:STEWARD_ARGS+[char]34; $l.WorkingDirectory=Split-Path $env:STEWARD_TARGET; $l.Save()"

echo.
echo Done. A shortcut named "Steward" is on your desktop.
echo Your data stays in %USERPROFILE%\.steward and is never touched by install.
echo.
pause
exit /b 0

:failed
echo.
echo Install did not produce %DST%\runtime\pythonw.exe.
echo The package looks incomplete: re-extract the zip and run install.cmd again.
echo.
pause
exit /b 1
'''

UNINSTALL_CMD = r'''@echo off
REM Pure ASCII, same reason as install.cmd.
setlocal
set "DST=%LOCALAPPDATA%\Steward"
if not exist "%DST%" (
    echo Steward is not installed at "%DST%".
    pause
    exit /b 1
)
echo Removing "%DST%" ...
echo Your data in %USERPROFILE%\.steward is untouched - delete it yourself if you want it gone.
pause
REM cmd.exe reads a .bat line by line, so it cannot survive the removal of the
REM directory this file lives in: right after rmdir, the next read fails with
REM "The system cannot find the path specified." and the closing lines never
REM print (V1.144 measured). So the delete goes to a helper process that runs
REM after this script has exited. Full paths because PATH may be trimmed.
start "" /b "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Milliseconds 900; Remove-Item -LiteralPath ($env:USERPROFILE + '\Desktop\Steward.lnk') -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath ($env:LOCALAPPDATA + '\Steward') -Recurse -Force"
exit /b 0
'''


def _download(target: Path) -> Path:
    """下载 embeddable zip 到缓存目录（已存在就复用）。"""
    if target.is_file() and target.stat().st_size > 5 * 1024 * 1024:
        return target
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    for url in PY_URLS:
        print(f"下载 {url}")
        try:
            urllib.request.urlretrieve(url, tmp)
        except Exception as exc:  # noqa: BLE001 —— 换源继续，最后一次失败才算失败
            print(f"  失败：{exc}")
            tmp.unlink(missing_ok=True)
            continue
        if tmp.stat().st_size > 5 * 1024 * 1024:
            tmp.replace(target)
            return target
        print(f"  文件不完整（{tmp.stat().st_size} 字节）")
        tmp.unlink(missing_ok=True)
    raise SystemExit(
        "下载失败：两个源都不通。手工下载 embeddable zip 后重跑：\n"
        f"  python tools\\pack\\build_portable.py --python-zip <zip 路径>\n"
        f"下载页：https://www.python.org/downloads/windows/（Windows embeddable package (64-bit) {PY_VERSION}）"
    )


def _unpack(zip_path: Path, runtime: Path) -> None:
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(runtime)


def _patch_pth(runtime: Path) -> Path:
    """把 ``..\\app`` 写进 ``python311._pth``。

    embeddable 有 ``_pth`` 时不加载 site、不读 PYTHONPATH —— 这是唯一能让
    ``import freeagent`` 生效的地方。
    """
    candidates = sorted(runtime.glob("python*._pth"))
    if not candidates:
        raise SystemExit(
            f"{runtime} 里没有 python*._pth —— 解压出来的不是官方 embeddable 包？"
        )
    pth = candidates[0]
    text = pth.read_text(encoding="utf-8")
    if "..\\app" not in text:
        pth.write_text(text.rstrip("\n") + "\n..\\app\n", encoding="utf-8")
    return pth


def build(*, python_zip: Path | None, out: Path) -> Path:
    package = out / "Steward"
    if package.exists():
        try:
            shutil.rmtree(package)
        except OSError as exc:
            raise SystemExit(
                f"删不掉旧产物 {package}：{exc}\n"
                "多半是包里的服务还在跑（窗口没关 / pythonw 没退），也可能是某个"
                "终端把工作目录停在里面。先关掉，再重跑构建。"
            ) from exc
    package.mkdir(parents=True)

    zip_path = python_zip or _download(CACHE / f"python-{PY_VERSION}-embed-amd64.zip")
    _unpack(zip_path, package / "runtime")
    _patch_pth(package / "runtime")

    shutil.copytree(SRC, package / "app" / "freeagent")

    (package / "desktop_main.pyw").write_text(LAUNCHER, encoding="utf-8")
    (package / "install.cmd").write_text(INSTALL_CMD, encoding="utf-8")
    (package / "uninstall.cmd").write_text(UNINSTALL_CMD, encoding="utf-8")
    (package / "VERSION.txt").write_text(
        "Steward portable package\n"
        f"Python runtime: {PY_VERSION} embeddable (python.org)\n"
        f"Source: {REPO}\n"
        "No extra pip install is needed.\n",
        encoding="utf-8",
    )

    # 构建期验收：少了任何一件，「双击没反应」就会变成用户的问题。
    for needed in ("runtime/pythonw.exe", "app/freeagent/app.py", "desktop_main.pyw"):
        if not (package / needed).exists():
            raise SystemExit(f"构建不完整：{package / needed} 不存在")
    return package


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_portable")
    parser.add_argument("--python-zip", default=None, help="已有的 embeddable zip（离线构建用）")
    parser.add_argument("--out", default=str(DIST), help="产物目录（默认 dist/）")
    args = parser.parse_args(argv)
    zip_path = Path(args.python_zip) if args.python_zip else None
    package = build(python_zip=zip_path, out=Path(args.out))
    size = sum(p.stat().st_size for p in package.rglob("*") if p.is_file())
    print(f"产物：{package}（{size / 1048576:.1f} MB）")
    print("分发：把整个 Steward 目录压成 zip 给人，对方解压后双击 install.cmd。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
