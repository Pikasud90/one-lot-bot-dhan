@echo off
REM One Lot Desk - Windows launcher.
REM All paths are quoted: this project's directory can contain spaces.
setlocal EnableDelayedExpansion

set "HERE=%~dp0"
set "ROOT=%HERE%.."
pushd "%ROOT%"

set "VENV=%ROOT%\.venv"
set "PORT=8787"
set "EXTRA="

:parse
if "%~1"=="" goto endparse
if /I "%~1"=="--mock"      ( set "EXTRA=!EXTRA! --mock" & shift & goto parse )
if /I "%~1"=="--lan"       ( set "EXTRA=!EXTRA! --lan"  & shift & goto parse )
if /I "%~1"=="--port"      ( set "PORT=%~2"              & shift & shift & goto parse )
if /I "%~1"=="--reinstall" ( rmdir /S /Q "%VENV%" 2>nul  & shift & goto parse )
if /I "%~1"=="-h"          ( goto usage )
if /I "%~1"=="--help"      ( goto usage )
set "EXTRA=!EXTRA! %~1"
shift
goto parse
:endparse

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: python not found on PATH. Install Python 3.10+ from python.org
  echo        and tick "Add Python to PATH" during setup.
  popd & exit /b 1
)

for /f "delims=" %%V in ('python -c "import sys;print(1 if sys.version_info>=(3,10) else 0)"') do set "PYOK=%%V"
if not "!PYOK!"=="1" (
  echo ERROR: Python 3.10 or newer is required.
  python --version
  popd & exit /b 1
)

if not exist "%VENV%\Scripts\python.exe" (
  echo ==^> creating virtual environment at "%VENV%"
  python -m venv "%VENV%"
  "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  echo ==^> installing dependencies ^(first run only, this takes a minute^)
  "%VENV%\Scripts\python.exe" -m pip install --quiet -r "%ROOT%\requirements.txt"
  "%VENV%\Scripts\python.exe" -m pip install --quiet -e "%ROOT%"
)

if not exist "%ROOT%\.env" (
  echo NOTE: no .env found. Paste your Dhan credentials in the Settings tab of the web app,
  echo       or copy .env.example to .env. Running on the synthetic market until then.
)

if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
if not exist "%ROOT%\data" mkdir "%ROOT%\data"

echo ==^> starting One Lot Desk  ^(http://127.0.0.1:!PORT!/^)
echo     press Ctrl-C to stop
"%VENV%\Scripts\python.exe" -m onelot run --port !PORT! !EXTRA!
popd
exit /b %errorlevel%

:usage
echo Usage: scripts\start_win.bat [options]
echo   --mock         run against the synthetic market ^(no credentials needed^)
echo   --lan          also listen on your LAN ^(0.0.0.0^) for other devices
echo   --port N       web app port ^(default 8787^)
echo   --reinstall    rebuild the virtual environment from scratch
echo.
echo Other commands ^(after the first run^):
echo   .venv\Scripts\python -m onelot backfill
echo   .venv\Scripts\python -m onelot backtest --strategy ema_cross --instrument BANKNIFTY_OPT --timeframe 12m
echo   .venv\Scripts\python -m onelot research --instruments BANKNIFTY_OPT --timeframes 5m,15m
echo   .venv\Scripts\python -m onelot preflight
popd
exit /b 0
