@echo off
REM Start the Feishu bridge, asking for the App Secret interactively.
REM
REM NOTE: keep this file pure ASCII. cmd.exe mis-reads UTF-8 batch files,
REM and non-ASCII comments corrupt the line boundaries (setlocal gets eaten).
REM
REM Why interactive: the App Secret must never be typed as a command line.
REM A value typed after `set X=` lands in shell history, and anything pasted
REM into chat is exposed forever. Here it is read by the machine instead.
REM
REM NOTE: delayed expansion is deliberately NOT enabled. This file uses no
REM !var! syntax, and with it turned on cmd would CONSUME any `!` in the
REM pasted secret -- silently corrupting the very value this script exists
REM to protect. Feishu secrets do contain characters like `!`.
setlocal

cd /d "%~dp0.."

set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=src"
if not exist data mkdir data
set "LOG=data\feishu.log"

REM The App ID / allowed users are deliberately NOT hardcoded here anymore.
REM They used to be baked into this file, which made it a second source of
REM truth: save new values in the UI, restart through this script, and the
REM old hardcoded ones would win with no warning. Config now comes from
REM <state_home>\feishu.env (written by the web UI) or from the environment,
REM and merged_env() is what decides the order. See docs 12.7.

echo.
echo   Feishu bridge  -- long connection, no public IP needed
echo   -----------------------------------------------
echo   Log file      : %CD%\%LOG%
echo.

REM Show the values that will ACTUALLY be used, and decide whether to ask for
REM a secret. Only Python knows the precedence order, because the environment
REM beats feishu.env. Asking it beats guessing here -- and guessing is exactly
REM what made this script silently override config saved from the web UI.
python -c "import sys; from freeagent.feishu.config import merged_env, ENV_APP_ID, ENV_ALLOWED, ENV_APP_SECRET; e=merged_env(); print('   App ID        : ' + (e.get(ENV_APP_ID) or '[NOT SET]')); print('   Allowed users : ' + (e.get(ENV_ALLOWED) or '[NOT SET - nobody may command this machine]')); sys.exit(0 if e.get(ENV_APP_SECRET) else 1)"
if errorlevel 1 goto ask_secret

echo   App Secret   : already configured -- not asking again.
echo.
goto after_secret

:ask_secret
REM A goto, NOT an if-block, on purpose. Delayed expansion is deliberately
REM off in this file because cmd would eat a `!` inside the pasted secret.
REM A `set` inside `( )` is invisible outside the block without it, so the
REM secret would silently read as empty right after being typed.
echo   Paste the App Secret from the Feishu developer console,
echo   under Credentials ^& Basic Info.
echo   It is NOT typed as a command, so it stays out of shell history.
echo.
set /p "FEISHU_APP_SECRET=   App Secret: "

:after_secret
REM Deliberately `if not defined`, NOT `if "%VAR%"==""`.
REM The quoted form expands the value into the command line, so a secret
REM containing & | < > or " corrupts this line and the test silently
REM misbehaves -- which is exactly what happened: a valid paste was
REM reported as "empty secret". `if not defined` never touches the value.
if not defined FEISHU_APP_SECRET (
    echo   Nothing was pasted. The field is still empty.
    >> "%LOG%" echo [run_feishu] secret field empty after paste, aborted
    echo.
    pause
    exit /b 2
)

REM Length is NOT checked here on purpose. Counting characters in a batch
REM file means fragile string slicing (`for /f` counts LINES, not chars),
REM and Feishu secrets do contain no spaces -- so the naive version just
REM breaks. doctor.py does it in Python, where it is safe and exact.
echo.

REM Verify before connecting, so a bad secret fails fast with a clear
REM reason instead of a silent reconnect loop.
echo   Checking credentials with Feishu...
echo.
python -m freeagent.feishu.doctor --live 2>&1 | tee /a "%LOG%"
if errorlevel 1 (
    echo.
    echo   ---------------------------------------------------------------
    echo   Doctor found a fatal problem, so the bridge was NOT started.
    echo   Most likely: the App Secret does not match this App ID.
    echo   Get a fresh one from the Feishu console and run this again.
    echo   Full details are in %LOG%
    echo   ---------------------------------------------------------------
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting bridge. Long connection, heartbeat every 30s.
echo   Leave this window OPEN. Ctrl+C to stop.
echo.
python -m freeagent.feishu.bridge --verbose 2>&1 | tee /a "%LOG%"
echo.
echo   Bridge exited. Log kept at %LOG%
pause
endlocal
