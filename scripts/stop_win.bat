@echo off
REM Stop the Bank Nifty Trade Assistant.
setlocal
set "HERE=%~dp0"
set "ROOT=%HERE%.."

if exist "%ROOT%\data\engine.pid" (
  set /p ENGINEPID=<"%ROOT%\data\engine.pid"
  taskkill /PID !ENGINEPID! /F >nul 2>&1 && echo stopped engine ^(PID !ENGINEPID!^)
) else (
  taskkill /FI "WINDOWTITLE eq OneLot*" /F >nul 2>&1
  echo engine pid file not found; closed any OneLot windows
)
exit /b 0
