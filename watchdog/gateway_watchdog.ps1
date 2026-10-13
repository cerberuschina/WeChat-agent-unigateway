# Unified gateway watchdog — runs every 5 min from Windows Task Scheduler (hidden, no console flash).
#
# Liveness is judged by an APPLICATION-level answer (dashboard /api/state == 200), never by
# "port 18500 is LISTENING": the gateway can hold the port while its poll loop is dead, which is
# exactly what happens when the real-iLink long poll raises RemoteDisconnected and the process exits.
#
# On DOWN: release 18500/18600 from stale holders, start via gateway_launcher.vbs (hidden + detached,
# so no console window exists that could be closed), re-check, append everything to data\watchdog.log.
$ErrorActionPreference = 'SilentlyContinue'
$root  = 'E:\claudeWork\WeChat-agent-unigateway'
$log   = Join-Path $root 'data\watchdog.log'
$vbs   = Join-Path $root 'watchdog\gateway_launcher.vbs'

function Add-Line([string]$text) {
    Add-Content -Path $log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $text)
}

function Test-GatewayAlive {
    # --noproxy: FlClash/TUN would otherwise swallow loopback probes
    $code = & curl.exe --noproxy '*' -s -o NUL -m 8 -w '%{http_code}' 'http://127.0.0.1:18600/api/state' 2>$null
    return ("$code" -eq '200')
}

if (Test-GatewayAlive) { Add-Line "running - no action"; exit 0 }

Add-Line "DOWN (18600/api/state != 200) - restarting"

foreach ($port in 18500, 18600) {
    $holders = @()
    try {
        $holders = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
                     Select-Object -ExpandProperty OwningProcess -Unique)
    } catch { }
    if (-not $holders) {
        $holders = @(netstat -ano | Select-String ":$port " | Select-String 'LISTENING' |
                     ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique)
    }
    foreach ($heldPid in $holders) {
        if ("$heldPid" -match '^\d+$') {
            & taskkill /F /PID $heldPid *> $null
            Start-Sleep -Milliseconds 300
            if (Get-Process -Id $heldPid -ErrorAction SilentlyContinue) {
                # taskkill is denied when the holder runs at a higher integrity than the caller
                # (a gateway started by Task Scheduler as the built-in Administrator does).
                # Winmgmt runs as SYSTEM, so Win32_Process.Terminate() still gets through.
                try {
                    Invoke-CimMethod -InputObject (Get-CimInstance Win32_Process -Filter "ProcessId=$heldPid") `
                        -MethodName Terminate | Out-Null
                    Add-Line "  taskkill denied for pid $heldPid - used CIM Terminate"
                } catch { Add-Line "  could not kill pid ${heldPid}: $($_.Exception.Message)" }
            } else {
                Add-Line "  released :$port from stale pid $heldPid"
            }
        }
    }
}

Start-Sleep -Seconds 2
Start-Process -FilePath 'wscript.exe' -ArgumentList '//B','//Nologo',$vbs -WindowStyle Hidden
Start-Sleep -Seconds 20

if (Test-GatewayAlive) { Add-Line "  restarted OK" }
else { Add-Line "  RESTART FAILED (see data\gateway.out.log)" }
exit 0
