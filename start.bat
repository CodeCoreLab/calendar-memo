@echo off
REM Calendar Memo - local launcher (Windows)
REM Web + REST API + MCP on one port
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] First run: creating virtual environment...
  python -m venv .venv || goto :err
  echo [2/4] Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :err
)

echo [3/4] Seeding data...
".venv\Scripts\python.exe" seed.py

if "%MEMO_TOKEN%"=="" (
  echo [4/4] Tip: set MEMO_TOKEN to bind MCP identity, e.g.
  echo        ".venv\Scripts\python.exe" mcp_server.py --issue-token ^<user^> ^<password^>
)

echo [4/4] Starting server...
start "" http://127.0.0.1:8000/memo_login.html
".venv\Scripts\python.exe" serve.py --port 8000
goto :eof

:err
echo Startup failed. Please install Python 3.9+ and make sure it is in PATH.
pause
