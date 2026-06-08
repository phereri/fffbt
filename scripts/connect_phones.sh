#!/usr/bin/env bash
#
# connect_phones.sh — adb-connect the tailscale-attached Android phones and
# verify they are agent-ready, then probe GenFarmer's REST from WSL.
#
# Usage:
#   ./scripts/connect_phones.sh 100.101.1.2 100.101.1.3          # IPs as args
#   ./scripts/connect_phones.sh -f phones.txt                    # one IP per line
#   PORT=5555 GENFARMER_PORT=55554 ./scripts/connect_phones.sh ... # override ports
#
# Notes:
#   - Phones must expose adb over TCP on :5555 (adb tcpip 5555 once, while USB).
#   - In WSL mirrored mode the tailnet IPs are reachable directly. In NAT mode,
#     run tailscale INSIDE WSL first (see win_vps_setup.ps1 NAT note).
#
set -euo pipefail

PORT="${PORT:-5555}"
GENFARMER_PORT="${GENFARMER_PORT:-55554}"
ADB="${ADB_PATH:-adb}"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# --- collect IPs -----------------------------------------------------------
IPS=()
if [ "${1:-}" = "-f" ]; then
  [ -n "${2:-}" ] || die "usage: $0 -f <file-with-one-ip-per-line>"
  while IFS= read -r line; do
    line="$(echo "$line" | tr -d '[:space:]')"
    [ -n "$line" ] && [ "${line:0:1}" != "#" ] && IPS+=("$line")
  done < "$2"
else
  IPS=("$@")
fi
[ "${#IPS[@]}" -gt 0 ] || die "no phone IPs given. usage: $0 <ip> [ip...]  |  $0 -f phones.txt"

command -v "$ADB" >/dev/null 2>&1 || die "adb not found (set ADB_PATH or apt install android-tools-adb)"

# --- connect ---------------------------------------------------------------
say "Starting adb server"
"$ADB" start-server >/dev/null 2>&1 || true

for ip in "${IPS[@]}"; do
  target="${ip}:${PORT}"
  printf '  connecting %s ... ' "$target"
  if out="$("$ADB" connect "$target" 2>&1)"; then
    echo "$out"
  else
    warn "connect failed: $out"
  fi
done

# --- verify ----------------------------------------------------------------
say "adb devices"
"$ADB" devices

say "Per-device readiness (model / android / username probe)"
ok=0; total=0
for ip in "${IPS[@]}"; do
  target="${ip}:${PORT}"; total=$((total+1))
  if model="$("$ADB" -s "$target" shell getprop ro.product.model 2>/dev/null | tr -d '\r')" && [ -n "$model" ]; then
    rel="$("$ADB" -s "$target" shell getprop ro.build.version.release 2>/dev/null | tr -d '\r')"
    printf '  \033[1;32mOK\033[0m   %s  -> %s (Android %s)\n' "$target" "$model" "$rel"
    ok=$((ok+1))
  else
    printf '  \033[1;31mDOWN\033[0m %s  (no shell response)\n' "$target"
  fi
done
echo "  ready: ${ok}/${total}"

# --- GenFarmer REST probe (optional; needed later for rotation) ------------
say "GenFarmer REST probe (localhost:${GENFARMER_PORT})"
if curl -fsS -m 4 "http://localhost:${GENFARMER_PORT}/" >/dev/null 2>&1; then
  echo "  GenFarmer REST reachable on localhost:${GENFARMER_PORT}"
elif curl -fsS -m 4 "http://127.0.0.1:${GENFARMER_PORT}/" >/dev/null 2>&1; then
  echo "  GenFarmer REST reachable on 127.0.0.1:${GENFARMER_PORT}"
else
  warn "GenFarmer REST not reachable from WSL on :${GENFARMER_PORT}."
  warn "  mirrored mode: ensure GenFarmer is running on the Windows host."
  warn "  NAT mode: bind GenFarmer to 0.0.0.0 or add a netsh portproxy (see win_vps_setup.ps1)."
  warn "  (Not required for first registration runs — only for device rotation.)"
fi

say "Done. Use a phone serial like ${IPS[0]}:${PORT} as --device-serial."
