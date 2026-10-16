@echo off
REM Start the REAL Web UI: your own database, your own Feishu config.
REM
REM NOTE: keep this file pure ASCII. cmd.exe mis-reads UTF-8 batch files,
REM and Chinese comments corrupt the line boundaries (setlocal gets eaten).
REM
REM Why there is deliberately no --db here: build_app() makes the config
REM directory follow the database's parent directory, so passing --db
REM silently repoints the Feishu config page and the bridge status file at
REM that directory, while the bridge and the executor keep reading the
REM default home. The UI then reports an empty Feishu config even though
REM ~\.freeagent\feishu.env is complete. One flag, two different homes.
REM See the build_app docstring in src/freeagent/app.py.
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONPATH=src
echo.
echo   Web UI:  http://127.0.0.1:8770/
echo   Db:      %USERPROFILE%\.freeagent\agent.db   (your real data)
echo   Config:  %USERPROFILE%\.freeagent\config.json
echo   Feishu:  %USERPROFILE%\.freeagent\feishu.env
echo.
echo   Port 8770 is shared with tools\run_demo_web.bat.
echo   Close that one first, or this will fail to bind.
echo   Stop: close this window, or press Ctrl+C
echo.
python -m freeagent.web