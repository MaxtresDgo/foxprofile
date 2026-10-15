@echo off
rem FoxProfile agent: opens the hub's profiles on this PC. Leave this window open.
rem First run installs everything (needs Python 3.10+ from python.org) and asks for the hub and your user.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv 2>nul || python -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :fail
if not exist ".venv\camoufox-fetched" (
    ".venv\Scripts\python.exe" -m camoufox fetch || goto :fail
    type nul > ".venv\camoufox-fetched"
)
".venv\Scripts\python.exe" -m src.agent
pause
exit /b
:fail
echo Setup failed, see the messages above.
pause
