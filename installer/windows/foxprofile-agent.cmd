@echo off
rem FoxProfile agent, started at logon by the MSI. Leave this window open.
rem Code lives next to this file (read-only); each Windows user's data, .env and
rem logs live in %LOCALAPPDATA%\FoxProfile.
title FoxProfile agent
set "PY=%~dp0python\python.exe"
if not exist "%PY%" (
    echo %PY% is missing. Restart Windows if an install asked for it, otherwise
    echo uninstall FoxProfile Agent from Settings, Apps and install it again.
    pause
    exit /b 1
)
if not exist "%LOCALAPPDATA%\FoxProfile" mkdir "%LOCALAPPDATA%\FoxProfile"
cd /d "%LOCALAPPDATA%\FoxProfile"
if not exist camoufox-fetched (
    "%PY%" -m camoufox fetch || goto :fail
    type nul > camoufox-fetched
)
:run
rem The agent exits non-zero when the hub is unreachable (network not up yet at
rem logon) or refuses the login; try again instead of leaving a dead window.
"%PY%" -m src.agent && exit /b
echo Retrying in 10 seconds...
timeout /t 10 /nobreak >nul
goto :run
:fail
echo Downloading the browser failed, see the messages above. It is retried at the next logon.
pause
