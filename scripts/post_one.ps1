# post_one.ps1 — standalone Trial Reel poster (Windows).
#
# Loads runner.env into the process env, sets PYTHONPATH=src, then runs
#   python -m runner post-one <your args...>
#
# Usage (from the repo root):
#   ./scripts/post_one.ps1 --device 100.100.57.41:5555 --video C:\clips\a.mp4 --caption "hi"
#   ./scripts/post_one.ps1 --device 100.100.57.41:5555 --video "https://bucket.s3...mp4?sig=.." --caption "hi" --hashtags trial,reels
#
# Prereqs: venv created and deps installed; runner.env filled in; phone prepared
# (IG logged in, Mobilerun Portal bound). This NEVER touches the database.

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Args
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$envFile = Join-Path $root 'runner.env'
if (-not (Test-Path $envFile)) {
    Write-Error "runner.env not found at $envFile. Copy config/runner.env.example to runner.env and fill it in."
    exit 2
}

# Parse runner.env (KEY=VALUE), strip surrounding quotes, set into process env.
Get-Content $envFile | ForEach-Object {
    $t = $_.Trim()
    if ($t -eq '' -or $t.StartsWith('#') -or ($t -notmatch '=')) { return }
    $idx = $t.IndexOf('=')
    $key = $t.Substring(0, $idx).Trim()
    $val = $t.Substring($idx + 1).Trim()
    if ($val.Length -ge 2 -and (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'")))) {
        $val = $val.Substring(1, $val.Length - 2)
    }
    [Environment]::SetEnvironmentVariable($key, $val, 'Process')
}

[Environment]::SetEnvironmentVariable('PYTHONPATH', 'src', 'Process')

# Prefer the repo venv python; fall back to PATH python.
$py = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

& $py -m runner post-one @Args
exit $LASTEXITCODE
