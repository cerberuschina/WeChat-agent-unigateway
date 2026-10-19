@echo off
chcp 65001 >nul
cd /d K:\hermesWork
set PYW=
for /d %%D in ("%LOCALAPPDATA%\hermes\tools\python-*") do set PYW=%%D\python.exe
if "%PYW%"=="" (
  echo 找不到 Hermes 的 python，改用系统 python
  set PYW=python
)
echo 用 %PYW%
"%PYW%" start_agent_stack.py
echo.
echo （窗口别关，关了就看不到状态了；服务是在后台跑的）
pause
