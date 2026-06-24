#requires -Version 5
<#
  Launcher for the Telegram status bot (scripts/status_bot.py), used by the
  "fffbt-status-bot" scheduled task (At-logon autostart, mirrors fffbt-ngrok-ssh).

  Keeps the bot alive: runs the venv python on the bot and, if it ever exits,
  restarts it after a short delay. All output is appended to data/status_bot.log
  (trimmed when it grows past ~5 MB). Run by hand to test the autostart path:

      powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_status_bot.ps1
#>
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot      # repo root (this file lives in scripts/)
Set-Location $root
$py  = Join-Path $root '.venv\Scripts\python.exe'
$bot = Join-Path $root 'scripts\status_bot.py'
$log = Join-Path $root 'data\status_bot.log'

# The bot emits UTF-8 (it reconfigures its own stdout). Force UTF-8 end to end so
# the log stays readable; PYTHONUTF8 covers the interpreter, and the python output
# is appended through cmd's raw ">>" so PowerShell never re-encodes it to UTF-16.
$env:PYTHONUTF8 = '1'

while ($true) {
    # Keep the log from growing without bound across restarts.
    if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
        Get-Content $log -Tail 500 | Set-Content $log -Encoding utf8
    }
    Add-Content -Path $log -Encoding utf8 -Value "[$(Get-Date -Format o)] launching status_bot.py ($py)"
    & cmd.exe /c "`"$py`" `"$bot`" >> `"$log`" 2>&1"
    Add-Content -Path $log -Encoding utf8 -Value "[$(Get-Date -Format o)] status_bot.py exited (code $LASTEXITCODE); restarting in 10s"
    Start-Sleep -Seconds 10
}
