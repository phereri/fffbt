#requires -Version 5
<#
  Merged fleet supervisor — keeps BOTH the dashboard and the Telegram status bot
  alive under a SINGLE scheduled task ("fffbt-fleet", At-logon autostart, mirrors
  fffbt-status-bot / fffbt-ngrok-ssh).

  Every 30s it health-checks each service and (re)launches it via its own
  keep-alive wrapper if it's down:

     dashboard   up == something is LISTENING on the dashboard port (8765)
                 launch -> scripts\start_dashboard.ps1   (self-guards on the port)
     status bot  up == a python process is running status_bot.py
                 launch -> scripts\run_status_bot.ps1     (self-restarts its child)

  The wrappers each keep their own python child alive; this supervisor is the
  outer net that relaunches a wrapper that died entirely (e.g. powershell crash,
  or a fresh boot). Both checks are idempotent — a service that is already up is
  never double-launched — so running this alongside a still-running service is
  safe. All output is appended to data\fleet_supervisor.log (trimmed at ~5 MB).

  Run by hand to test the autostart path:
     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_fleet.ps1
#>
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot          # repo root (this file is in scripts/)
Set-Location $root
$log      = Join-Path $root 'data\fleet_supervisor.log'
$dashPort = if ($env:FLEET_DASH_PORT) { [int]$env:FLEET_DASH_PORT } else { 8765 }

function Write-Sup($msg) {
    Add-Content -Path $log -Encoding utf8 -Value "[$(Get-Date -Format o)] $msg"
}

function Test-DashboardUp {
    [bool](Get-NetTCPConnection -State Listen -LocalPort $dashPort -ErrorAction SilentlyContinue)
}

function Test-StatusBotUp {
    [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -match 'status_bot\.py' })
}

function Start-Service-Wrapper($relPath) {
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
        '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $root $relPath)
    ) | Out-Null
}

# Keep the supervisor log bounded across long-lived runs.
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
    Get-Content $log -Tail 500 | Set-Content $log -Encoding utf8
}
Write-Sup "supervisor starting (dashPort=$dashPort, root=$root)"

while ($true) {
    try {
        if (-not (Test-DashboardUp)) {
            Write-Sup 'dashboard DOWN -> launching scripts\start_dashboard.ps1'
            Start-Service-Wrapper 'scripts\start_dashboard.ps1'
        }
        if (-not (Test-StatusBotUp)) {
            Write-Sup 'status bot DOWN -> launching scripts\run_status_bot.ps1'
            Start-Service-Wrapper 'scripts\run_status_bot.ps1'
        }
    } catch {
        Write-Sup "supervisor loop error: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 30
}
