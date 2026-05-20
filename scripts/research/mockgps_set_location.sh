#!/usr/bin/env bash
# FFF-24 research PoC — set / verify / stop a mock GPS location on one device
# via the io.appium.settings LocationService.
#
# Research only. Run against an IDLE lab phone, not a phone running a job.
# Nothing here is destructive: it grants an AppOp and starts/stops a service,
# all reversible. It does NOT touch production data or the Supabase schema.
#
# Usage:
#   scripts/research/mockgps_set_location.sh <serial> set   <lat> <lon> [accuracy]
#   scripts/research/mockgps_set_location.sh <serial> verify
#   scripts/research/mockgps_set_location.sh <serial> stop
#
# See docs/research/mockgps-integration.md for the full notes.
set -euo pipefail

PKG="io.appium.settings"
SVC="${PKG}/.LocationService"

serial="${1:?serial required (adb devices)}"
action="${2:?action required: set|verify|stop}"
adb() { command adb -s "$serial" "$@"; }

case "$action" in
  set)
    lat="${3:?lat required}"; lon="${4:?lon required}"; acc="${5:-10}"
    echo "[*] one-time grants for $PKG"
    adb shell appops set "$PKG" android:mock_location allow
    adb shell pm grant "$PKG" android.permission.ACCESS_FINE_LOCATION || true
    echo "[*] starting mock location $lat,$lon (accuracy ${acc}m)"
    adb shell am start-foreground-service --user 0 -n "$SVC" \
      --es latitude "$lat" --es longitude "$lon" --es accuracy "$acc"
    echo "[+] mocking started; verify with: $0 $serial verify"
    ;;
  verify)
    echo "[*] dumpsys location (look for a test provider + last location):"
    adb shell dumpsys location | grep -iE 'mock|test provider|last location' || true
    echo "[*] location providers (Android 12+):"
    adb shell cmd location providers 2>/dev/null || echo "  (cmd location not available)"
    ;;
  stop)
    echo "[*] stopping mock location service"
    adb shell am stopservice "$SVC"
    echo "[+] stopped (AppOp grant left in place; revoke with: adb -s $serial shell appops set $PKG android:mock_location deny)"
    ;;
  *)
    echo "unknown action: $action (expected set|verify|stop)" >&2
    exit 1
    ;;
esac
