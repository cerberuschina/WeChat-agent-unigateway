' Launcher for the Unigateway_Watchdog scheduled task.
' Runs gateway_watchdog.ps1 hidden (window mode 0) so no console window flashes every 5 minutes.
' The task Action must point at wscript.exe + this file — a powershell.exe -WindowStyle Hidden
' action still flashes a console, because Task Scheduler starts it as a console app.
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""E:\claudeWork\WeChat-agent-unigateway\watchdog\gateway_watchdog.ps1""", 0, False
