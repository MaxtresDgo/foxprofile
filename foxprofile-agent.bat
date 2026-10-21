@echo off
rem FoxProfile agent: opens the hub's profiles on this PC.
rem First run installs everything (needs Python 3.10+ from python.org). The agent then
rem runs in the tray and signs in through your browser; this window closes by itself.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv 2>nul || python -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :fail
if not exist ".venv\camoufox-fetched" (
    ".venv\Scripts\python.exe" -m camoufox fetch || goto :fail
    type nul > ".venv\camoufox-fetched"
)
start "" ".venv\Scripts\pythonw.exe" -m src.agent
exit /b
:fail
echo Setup failed, see the messages above.
pause
