@echo off
REM Start agent-gateway on Windows (double-click or run from a terminal).
REM First run: make sure gateway.json exists and you already ran: python login.py
setlocal
cd /d "%~dp0"
if not exist gateway.json (
  echo [x] gateway.json not found. Copy gateway.example.json to gateway.json and edit it.
  pause
  exit /b 1
)
python -m agent_gateway --config gateway.json
echo.
echo [gateway exited with code %errorlevel%]
pause
