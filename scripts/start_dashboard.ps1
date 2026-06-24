# start_dashboard.ps1 - launch the Fleet Dashboard (Windows).
#
# Single source of truth for starting the dashboard, used both manually and by
# the "FleetDashboard" scheduled task (autostart at logon). It:
#   * cd's to the repo root,
#   * loads UTF-8 I/O (PYTHONUTF8 / PYTHONIOENCODING),
#   * refuses to start a second instance if the port is already listening,
#   * runs the repo venv python on scripts/fleet_dashboard.py,
#   * appends stdout/stderr to dashboard.log at the repo root.
#
# Manual use (from anywhere):
#   powershell -ExecutionPolicy Bypass -File C:\Users\Admin\projects\fffbt\scripts\start_dashboard.ps1
#
# The dashboard reads .env itself (SUPABASE_*, ADB_PATH, ...), so no secrets
# live here. It binds 127.0.0.1:8765 by default (override via FLEET_DASH_PORT).

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$port = if ($env:FLEET_DASH_PORT) { [int]$env:FLEET_DASH_PORT } else { 8765 }

# Already up? Don't start a second instance (would fail to bind the port).
$listening = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "dashboard already listening on port $port - not starting another instance"
    exit 0
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

$log = Join-Path $root 'dashboard.log'
$stamp = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
Add-Content -Path $log -Value "==== $stamp start_dashboard.ps1 launching on port $port ====" -Encoding utf8

# Run in the foreground of THIS process so the scheduled task (hidden window)
# stays alive as long as the dashboard does. The redirect is routed through cmd
# so Python's raw UTF-8 bytes land in the log as-is (PowerShell's own `*>>`
# would re-encode them as UTF-16). PYTHONUTF8/PYTHONIOENCODING are inherited.
& cmd.exe /c "`"$py`" scripts\fleet_dashboard.py >> `"$log`" 2>&1"
exit $LASTEXITCODE
