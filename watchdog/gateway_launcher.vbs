' Hidden + detached launcher for the unified gateway.
' Called by gateway_watchdog.ps1 (and usable by hand).
' Window mode 0 = no console window, so nobody can kill the gateway by closing a terminal.
' stdout/stderr are appended to data\gateway.out.log — that file is where an unhandled
' traceback shows up when the gateway dies (the gateway's own data\gateway.log may end
' mid-heartbeat with no traceback at all).
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "E:\claudeWork\WeChat-agent-unigateway"
sh.Run "cmd /c C:\Python314\python.exe -m agent_gateway --config gateway.json >> data\gateway.out.log 2>&1", 0, False
