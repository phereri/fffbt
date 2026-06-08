#!/usr/bin/env bash
#
# vps_bootstrap.sh — one-shot dev environment setup for the fffbt Instagram
# auto-registration work, intended for a fresh WSL2 (Ubuntu) checkout on the
# Windows VPS that hosts GenFarmer + tailscale-connected Android phones.
#
# What it does (idempotent):
#   1. Verifies Python 3.10+ and adb are present (installs adb hint if missing).
#   2. Creates/refreshes the .venv and installs dev deps (requirements-dev.txt).
#   3. Optionally installs `mobilerun` from a path you provide (MOBILERUN_SRC).
#   4. Runs the registration + agent_runner unit tests (no network, no spend).
#   5. If FIVESIM_API_KEY is set, does a read-only 5sim balance smoke check.
#
# What it does NOT do:
#   - Install Hermes (see docs: https://claude-code.nousresearch.com/docs).
#   - Configure WSL mirrored networking (.wslconfig on the Windows host).
#   - adb connect to phones (their tailscale IPs are site-specific).
#
# Usage:
#   ./scripts/vps_bootstrap.sh
#   MOBILERUN_SRC=/home/you/src/mobilerun ./scripts/vps_bootstrap.sh
#
set -euo pipefail

# Resolve repo root from this script's location, so it works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. Prerequisites ------------------------------------------------------
say "Checking prerequisites"

PYTHON="${PYTHON:-python3}"
command -v "${PYTHON}" >/dev/null 2>&1 || die "python3 not found. apt install python3 python3-venv"

PYVER="$(${PYTHON} -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PYMAJOR="${PYVER%%.*}"; PYMINOR="${PYVER##*.}"
if [ "${PYMAJOR}" -lt 3 ] || { [ "${PYMAJOR}" -eq 3 ] && [ "${PYMINOR}" -lt 10 ]; }; then
  die "Python ${PYVER} too old; need 3.10+."
fi
echo "  python ${PYVER} OK"

if command -v adb >/dev/null 2>&1; then
  echo "  adb $(adb --version 2>/dev/null | head -1 | awk '{print $NF}') OK"
else
  warn "adb not found. Install with: sudo apt install -y android-tools-adb"
fi

# --- 2. venv + dev deps ----------------------------------------------------
say "Creating/refreshing virtualenv (.venv)"
if [ ! -d .venv ]; then
  "${PYTHON}" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip >/dev/null
if [ -f requirements-dev.txt ]; then
  pip install -r requirements-dev.txt
else
  warn "requirements-dev.txt missing; installing minimal set"
  pip install pytest pytest-asyncio pydantic
fi

# --- 3. Optional: mobilerun (the M0 blocker for LIVE runs) -----------------
if [ -n "${MOBILERUN_SRC:-}" ]; then
  say "Installing mobilerun from ${MOBILERUN_SRC}"
  [ -e "${MOBILERUN_SRC}" ] || die "MOBILERUN_SRC path does not exist: ${MOBILERUN_SRC}"
  pip install -e "${MOBILERUN_SRC}"
fi
if python -c "import mobilerun" 2>/dev/null; then
  echo "  mobilerun import OK (live agent runs enabled)"
else
  warn "mobilerun NOT installed — unit tests still pass; live agent runs will raise."
  warn "  Install later with: MOBILERUN_SRC=/path/to/mobilerun ./scripts/vps_bootstrap.sh"
fi

# --- 4. Unit tests (no network, no spend) ----------------------------------
say "Running registration + agent_runner unit tests"
python -m pytest tests/registration tests/worker/agent_runner -q

# --- 5. Optional 5sim read-only smoke (balance) ----------------------------
if [ -n "${FIVESIM_API_KEY:-}" ]; then
  say "5sim read-only smoke check (balance — no spend)"
  python - <<'PY' || warn "5sim balance check failed (key/network?) — non-fatal"
import asyncio
from src.registration.five_sim import FiveSimClient
print("  5sim balance:", asyncio.run(FiveSimClient().balance()))
PY
else
  warn "FIVESIM_API_KEY not set — skipping 5sim smoke. export it to enable."
fi

say "Bootstrap complete."
echo "Next:"
echo "  - adb connect <phone-tailscale-ip>:5555   (then: adb devices)"
echo "  - curl -s http://localhost:55554/ | head  (GenFarmer REST from WSL)"
echo "  - start Hermes in this repo; .hermes.md auto-loads as project context."
