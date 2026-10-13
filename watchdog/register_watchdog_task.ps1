# Registers the Unigateway_Watchdog scheduled task (every 5 min, hidden via task_launcher.vbs).
# Needs an elevated PowerShell: a non-elevated `schtasks /Create` fails with "拒绝访问".
# Result is appended to data\watchdog-task-register.log.
$ErrorActionPreference = 'Continue'
$log = 'E:\claudeWork\WeChat-agent-unigateway\data\watchdog-task-register.log'
try {
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' `
        -Argument '//B //Nologo "E:\claudeWork\WeChat-agent-unigateway\watchdog\task_launcher.vbs"'
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -Hidden
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'Unigateway_Watchdog' -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force -ErrorAction Stop | Out-Null
    $msg = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') TASK REGISTERED OK (wscript launcher)"
} catch {
    $msg = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') REGISTER FAILED: $($_.Exception.Message)"
}
Add-Content -Path $log -Value $msg
Write-Output $msg
