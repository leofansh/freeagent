@echo off
REM Start the demo Web UI.
REM NOTE: keep this file pure ASCII. cmd.exe mis-reads UTF-8 batch files,
REM and Chinese comments corrupt the line boundaries (setlocal gets eaten).
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONPATH=src
echo.
echo   Web UI:  http://127.0.0.1:8770/
echo   Demo db: data\demo.db   (safe to delete and re-seed)
echo   Stop:    close this window, or press Ctrl+C
echo.
python -m freeagent.web --db data\demo.db --port 8770
