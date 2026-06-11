#!/usr/bin/env python3
"""Operator CLI for GenFarmer ChangeDevice (capture / random / apply / restore).

Run ON the GenFarmer host (needs adb on the fleet + the local REST API at
:55554). ADB binary from ``ADB_PATH`` (or ``ADB_BIN``). See
``docs/runbooks/changedevice.md``.

Examples
--------
  # save an account's device identity (do this right after registration)
  changedevice.py capture --serial 100.91.90.9:5555 --save accounts/alice.props

  # rotate to a fresh, GUARANTEED Android-12+ identity (a NEW account)
  changedevice.py apply --serial 100.91.90.9:5555 --random --min-android 12

  # return to an existing account: restore its EXACT saved device (serial incl.)
  changedevice.py apply --serial 100.91.90.9:5555 --profile accounts/alice.props

  # preview a random 12+ profile without touching the device
  changedevice.py random --min-android 12 --save /tmp/candidate.props
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# allow running as a loose script (python scripts/changedevice.py ...)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.genfarmer.changedevice import ChangeDeviceError, DeviceProfile, default_client  # noqa: E402


def _print_identity(label: str, ident: dict[str, str]) -> None:
    print(f"{label}: model={ident.get('ro.product.model')} "
          f"release={ident.get('ro.build.version.release')} "
          f"serial={ident.get('ro.serialno')} fp={ident.get('ro.build.fingerprint')}")


async def cmd_ready(client, args) -> int:
    ok = await client.ready(args.serial)
    print(f"{args.serial}: GenFarmer-ready = {ok}")
    return 0 if ok else 1


async def cmd_identity(client, args) -> int:
    _print_identity("identity", await client.identity(args.serial))
    return 0


async def cmd_capture(client, args) -> int:
    profile = await client.capture(args.serial)
    print("captured:", profile.summary())
    if args.save:
        Path(args.save).write_text(profile.to_props(), encoding="utf-8")
        print("saved ->", Path(args.save).resolve())
    else:
        print(profile.to_props())
    return 0


async def cmd_random(client, args) -> int:
    profile = await client.fetch_random(min_android=args.min_android)
    print("random:", profile.summary())
    if args.save:
        Path(args.save).write_text(profile.to_props(), encoding="utf-8")
        print("saved ->", Path(args.save).resolve())
    else:
        print(profile.to_props())
    return 0


async def cmd_apply(client, args) -> int:
    if args.random:
        profile = await client.fetch_random(min_android=args.min_android)
        keep_serial = False  # a new account => a fresh serial
    else:
        profile = DeviceProfile.load(args.profile)
        keep_serial = not args.no_keep_serial  # restore => keep saved serial
    print("applying:", profile.summary(), f"(clear_data={args.clear_data}, keep_serial={keep_serial})")

    _print_identity("before", await client.identity(args.serial))
    await client.apply(args.serial, profile, clear_data=args.clear_data, keep_serial=keep_serial)
    print("triggered change_device — phone is rebooting (Tailscale will drop on the remote path)")
    if args.no_wait:
        return 0

    print("waiting for reconnect (~90s)...")
    if not await client.wait_reconnect(args.serial, timeout=args.reconnect_timeout):
        print("⚠️ phone did not return on adb — reconnect it (on the LAN it auto-returns by serial)")
        return 2
    _print_identity("after ", await client.identity(args.serial))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="GenFarmer ChangeDevice operator CLI.")
    p.add_argument("--api-base", default=None, help="override local GenFarmer API base URL")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("ready", help="check the device has the GenFarmer ROM helper")
    pr.add_argument("--serial", required=True)

    pi = sub.add_parser("identity", help="print the live ro.* identity")
    pi.add_argument("--serial", required=True)

    pc = sub.add_parser("capture", help="save the device's current identity profile")
    pc.add_argument("--serial", required=True)
    pc.add_argument("--save", help="write a .props file (else print)")

    prd = sub.add_parser("random", help="preview a random profile (no device change)")
    prd.add_argument("--min-android", type=int, default=None)
    prd.add_argument("--save")

    pa = sub.add_parser("apply", help="apply a profile to the device (DESTRUCTIVE: reboots)")
    pa.add_argument("--serial", required=True)
    src = pa.add_mutually_exclusive_group(required=True)
    src.add_argument("--random", action="store_true", help="fresh random profile (new account)")
    src.add_argument("--profile", help="saved .props/.json to restore (existing account)")
    pa.add_argument("--min-android", type=int, default=12, help="with --random (default 12)")
    pa.add_argument("--clear-data", action="store_true", help="wipe app data + rotate android_id")
    pa.add_argument("--no-keep-serial", action="store_true", help="with --profile: regenerate serial")
    pa.add_argument("--no-wait", action="store_true", help="don't wait for reconnect")
    pa.add_argument("--reconnect-timeout", type=float, default=300.0)
    return p


def main() -> int:
    args = build_parser().parse_args()
    kwargs = {"api_base": args.api_base} if args.api_base else {}
    client = default_client(**kwargs)
    handler = {
        "ready": cmd_ready, "identity": cmd_identity, "capture": cmd_capture,
        "random": cmd_random, "apply": cmd_apply,
    }[args.command]
    try:
        return asyncio.run(handler(client, args))
    except ChangeDeviceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
