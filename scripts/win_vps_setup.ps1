<#
.SYNOPSIS
  One-shot Windows-host setup for the fffbt Instagram auto-registration VPS.
  Installs WSL2 + Ubuntu, picks the right networking mode for this Windows
  build, writes ~/.wslconfig, and stages the Linux-side bootstrap.

.DESCRIPTION
  Run in an ELEVATED PowerShell (Run as Administrator). Because the repo is
  public, the fastest invocation needs no git on Windows:

      irm https://raw.githubusercontent.com/phereri/fffbt/feat/instagram-autoreg/scripts/win_vps_setup.ps1 | iex

  The script is idempotent and reboot-aware. WSL feature enablement requires a
  reboot; after rebooting, run this same one-liner again — it detects that WSL
  is ready and proceeds to the Ubuntu + repo + venv + tests phase.

  NETWORKING DECISION (critical for a VPS):
    * WSL "mirrored" networking (localhost + tailnet reachable directly from
      WSL) requires Windows build 22621+ (Win11 22H2 / Windows Server 2025).
    * On older builds (Server 2019/2022, Win10) mirrored is UNAVAILABLE; the
      script writes a NAT .wslconfig and prints the tailscale-in-WSL fallback
      so the phones stay reachable by their tailnet IPs.

.NOTES
  Branch/repo are parameterized at the top so this works from a fork too.
#>

[CmdletBinding()]
param(
  [string]$Repo   = "https://github.com/phereri/fffbt.git",
  [string]$Branch = "feat/instagram-autoreg",
  [string]$Distro = "Ubuntu",
  # Where the repo gets cloned INSIDE WSL (root's home unless you pass a user).
  [string]$WslClonePath = "/root/code/fffbt"
)

$ErrorActionPreference = "Stop"

function Say  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Warn ($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "[fail] $m" -ForegroundColor Red; exit 1 }

# --- 0. Elevation check ----------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal(
  [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Die "Run this in an ELEVATED PowerShell (Run as Administrator)."
}

$build = [int][System.Environment]::OSVersion.Version.Build
$mirroredOk = $build -ge 22621
Say "Windows build $build  (mirrored networking supported: $mirroredOk)"

# --- 1. Ensure WSL is installed --------------------------------------------
$wslReady = $false
try {
  wsl.exe --status *> $null
  if ($LASTEXITCODE -eq 0) { $wslReady = $true }
} catch { $wslReady = $false }

if (-not $wslReady) {
  Say "Enabling WSL + Virtual Machine Platform features"
  # Prefer the modern installer; fall back to DISM on older images.
  $installed = $false
  try {
    wsl.exe --install --no-distribution
    if ($LASTEXITCODE -eq 0) { $installed = $true }
  } catch { $installed = $false }

  if (-not $installed) {
    dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart | Out-Null
    dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart | Out-Null
  }

  Warn "WSL features enabled. A REBOOT IS REQUIRED."
  Warn "After reboot, re-run the same one-liner to continue with Ubuntu + repo + tests."
  Say  "Rebooting in 20s — press Ctrl+C to cancel and reboot manually."
  Start-Sleep -Seconds 20
  Restart-Computer -Force
  exit 0
}

Say "WSL is ready"
wsl.exe --update *> $null
wsl.exe --set-default-version 2 *> $null

# --- 2. Write ~/.wslconfig with the correct networking mode -----------------
$wslConfig = Join-Path $env:USERPROFILE ".wslconfig"
if ($mirroredOk) {
  Say "Writing mirrored-networking .wslconfig -> $wslConfig"
  @"
[wsl2]
networkingMode=mirrored
dnsTunneling=true
firewall=true
"@ | Set-Content -Path $wslConfig -Encoding ASCII
  $netNote = "mirrored: GenFarmer 127.0.0.1:55554 and the phones' tailnet IPs are reachable directly from WSL."
} else {
  Say "Writing NAT .wslconfig (mirrored unsupported on build $build) -> $wslConfig"
  @"
[wsl2]
networkingMode=NAT
"@ | Set-Content -Path $wslConfig -Encoding ASCII
  $netNote = @"
NAT mode (build < 22621). The phones are on tailscale, so the robust path is to
run tailscale INSIDE WSL as its own tailnet node:
    wsl -d $Distro -u root -- bash -lc 'curl -fsSL https://tailscale.com/install.sh | sh'
    wsl -d $Distro -u root -- tailscale up
Then 'adb connect <phone-tailnet-ip>:5555' works from WSL.
For GenFarmer REST from WSL under NAT, either bind GenFarmer to 0.0.0.0 or add a
portproxy on the Windows host:
    netsh interface portproxy add v4tov4 listenport=55554 listenaddress=0.0.0.0 connectport=55554 connectaddress=127.0.0.1
(GenFarmer REST is only needed later for device rotation — not for first runs.)
"@
}

# Networking changes need a WSL restart to take effect.
Say "Restarting WSL to apply networking config"
wsl.exe --shutdown

# --- 3. Ensure the Ubuntu distro is installed ------------------------------
$haveDistro = (wsl.exe -l -q) -match "^$([regex]::Escape($Distro))$"
if (-not $haveDistro) {
  Say "Installing distro: $Distro (no interactive user; we operate as root)"
  wsl.exe --install -d $Distro --no-launch
  if ($LASTEXITCODE -ne 0) {
    Die "Could not install $Distro. Try: wsl --list --online  then re-run with -Distro <name>."
  }
}

# --- 4. Clone the repo + run the Linux bootstrap inside WSL -----------------
Say "Preparing Ubuntu: packages, repo clone, venv, tests (this runs in WSL as root)"

$linux = @"
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip android-tools-adb ripgrep curl ca-certificates
mkdir -p "`$(dirname $WslClonePath)"
if [ ! -d "$WslClonePath/.git" ]; then
  git clone "$Repo" "$WslClonePath"
fi
cd "$WslClonePath"
git fetch origin "$Branch"
git checkout "$Branch"
git pull --ff-only origin "$Branch" || true
chmod +x scripts/*.sh || true
./scripts/vps_bootstrap.sh
"@

# Pass the script via stdin to avoid quoting hell across the PS->bash boundary.
$linux | wsl.exe -d $Distro -u root -- bash -s
if ($LASTEXITCODE -ne 0) {
  Warn "Linux bootstrap returned non-zero. Inspect with: wsl -d $Distro -u root"
}

# --- 5. Final guidance -----------------------------------------------------
Say "Windows-side setup complete."
Write-Host ""
Write-Host "Networking: $netNote"
Write-Host ""
Write-Host "Next steps (inside WSL):" -ForegroundColor Green
Write-Host "  wsl -d $Distro -u root"
Write-Host "  cd $WslClonePath && source .venv/bin/activate"
Write-Host "  export FIVESIM_API_KEY=...           # for live 5sim + smoke"
Write-Host "  ./scripts/connect_phones.sh <phone-tailnet-ip> [more ips...]"
Write-Host "  # then install Hermes: https://claude-code.nousresearch.com/docs"
