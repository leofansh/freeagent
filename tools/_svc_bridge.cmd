@echo off
REM Auto-generated. FreeAgent Feishu bridge (long connection).
REM Pure ASCII on purpose: cmd.exe mis-reads UTF-8 batch files.
setlocal
cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=src"
if not exist data mkdir data
python -u -m freeagent.feishu.bridge --verbose >> "data\bridge.log" 2>&1
endlocal