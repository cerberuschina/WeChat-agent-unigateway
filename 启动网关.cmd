@echo off
rem Double-click entry: run start_agent_stack.py from THIS folder (no drive letters, any machine).
rem Optional args: --check / --gateway-only / --client-only / --claude-cwd D:/work
rem This file is ASCII-only on purpose: cmd.exe decodes .cmd in the OEM codepage, and UTF-8
rem Chinese here gets mis-decoded into stray command fragments. Chinese output comes from
rem the Python script instead (chcp 65001 keeps the console readable).
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE="
rem 0) explicit interpreter:  set PYTHON=C:\path\python.exe
if defined PYTHON set "PYEXE=%PYTHON%"

rem 1) a venv inside this repo, if one appears later
if not defined PYEXE if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"

rem 2) the python Hermes ships (version-pinned, safest)
if not defined PYEXE (
  for /d %%D in ("%LOCALAPPDATA%\hermes\tools\python-*") do set "PYEXE=%%D\python.exe"
)

rem 3) py launcher, then whatever python is on PATH
if not defined PYEXE (
  where py >nul 2>&1 && set "PYEXE=py -3"
)
if not defined PYEXE (
  where python >nul 2>&1 && set "PYEXE=python"
)

if not defined PYEXE (
  echo [x] python not found: install one, or  set "PYTHON=C:\path\python.exe"  and retry.
  pause
  exit /b 1
)

echo Using %PYEXE%
%PYEXE% "%~dp0start_agent_stack.py" %*
echo.
echo (keep this window open to watch the status; gateway and client run in the background)
pause
