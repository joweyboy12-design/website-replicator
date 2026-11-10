@echo off
setlocal
cd /d "%~dp0"
title Website Replicator

rem The virtual environment lives outside OneDrive so thousands of files are not synced.
set "VENV=%LOCALAPPDATA%\WebsiteReplicator\venv"
set "VPY=%VENV%\Scripts\python.exe"

if exist "%VPY%" goto deps

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.9+ was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH".
    goto fail
)
echo [setup] Creating virtual environment in %VENV% ...
%PY% -m venv "%VENV%" || goto fail

:deps
"%VPY%" -c "import playwright" >nul 2>nul
if errorlevel 1 (
    echo [setup] Installing Python packages...
    "%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt || goto fail
)
if not exist "%VENV%\.chromium-installed" (
    echo [setup] Downloading Playwright Chromium - one time only, about 150 MB...
    "%VPY%" -m playwright install chromium || goto fail
    echo ok>"%VENV%\.chromium-installed"
)

start "" "%VENV%\Scripts\pythonw.exe" "%~dp0app.py"
exit /b 0

:fail
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
