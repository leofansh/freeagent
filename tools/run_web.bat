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
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=src"

REM Locate an interpreter before any import. Order: PATH `python`, the
REM `py` launcher, then the standard per-user and Program Files install
REM layouts. The old failure was cmd's own "'python' is not recognized"
REM line, which says nothing about what to install. A Microsoft Store
REM placeholder entry also lands here -- the version probe below fails on
REM it, and the bad_python message covers that case.
set "PYEXE="
set "PYARG="
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARG=-3"
    )
)
if not defined PYEXE for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try_py "%%D\python.exe"
if not defined PYEXE for /d %%D in ("%ProgramFiles%\Python3*") do call :try_py "%%D\python.exe"
if not defined PYEXE goto no_python

REM Reject interpreters older than 3.11 up front. Letting it reach import
REM would produce a syntax/encoding failure that blames the wrong thing.
"%PYEXE%" %PYARG% -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)"
if errorlevel 1 goto bad_python

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
"%PYEXE%" %PYARG% -m freeagent.web
goto end

:try_py
REM Called from the for loop: first candidate that actually runs 3.11+
REM wins. A label call (not an inline block) on purpose: without delayed
REM expansion `set` inside a `( )` block would not be visible to the next
REM iteration's guard. An older interpreter is skipped, not fatal -- another
REM folder may hold a newer one.
if defined PYEXE goto :eof
if not exist %1 goto :eof
%1 -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)"
if not errorlevel 1 set "PYEXE=%~1"
goto :eof

:no_python
echo.
echo   FreeAgent needs Python 3.11 or newer, but no interpreter was found
echo   on PATH, in the py launcher, or in the standard install folders.
echo   Install Python 3.11+ from https://www.python.org/downloads/, then run
echo   this script again. The Web UI needs no extra pip install.
echo.
pause
exit /b 1

:bad_python
echo.
echo   The interpreter found could not run FreeAgent: it is either older
echo   than Python 3.11, or it is the Microsoft Store placeholder that opens
echo   the store instead of starting Python.
echo   Install Python 3.11+ from https://www.python.org/downloads/, make sure
echo   the real interpreter is on PATH, then run this script again.
echo.
pause
exit /b 1

:end
endlocal
