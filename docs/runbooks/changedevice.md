# ChangeDevice — per-account device identity (rotate / save / restore)

GenFarmer **ChangeDevice** rewrites a phone's device identity (model, brand,
build fingerprint, serial, android_id, …). We use it as a reusable building block
for **both** autoreg and posting: every account lives on its own device identity,
and we can **return** to an account later by restoring its exact saved identity.

- Module: [`src/genfarmer/changedevice.py`](../../src/genfarmer/changedevice.py)
  (`DeviceProfile`, `ChangeDeviceClient`, `default_client`)
- CLI: [`scripts/changedevice.py`](../../scripts/changedevice.py)
- Rotator integration: `GenFarmerAutoRotator` in
  [`src/registration/rotator.py`](../../src/registration/rotator.py)
- Validated end-to-end on the real fleet **2026-06-11** (round-trip below).

> The older [`genfarmer-change-device.md`](./genfarmer-change-device.md) documents
> an alternative path (driving the same change via the GenFarmer **Automation REST
> app**). Both work; the adb-props path here is preferred because it can apply a
> **specific** profile (needed for restore) and needs no automation-app graph.

---

## Mechanism

A device identity is a set of `dmMIN.*` properties. To apply one:

1. Write the `dmMIN.key=value` lines to **`/data/local/tmp/.genfarmer_props`** on the phone (`adb push`).
2. `adb shell setprop genfarmer.command change_device` (or `wipe_data_change` to also wipe app data).

The on-device GenFarmer ROM helper **`/system/bin/genfarmer`** (setuid root)
consumes the staged props, applies the new identity and **reboots (~90 s)**.
Preconditions on the device (checked by `ChangeDeviceClient.ready`):
`getprop genfarmer.activated == 1`, `getprop init.svc.genfarmer_command == running`,
and `/system/bin/genfarmer` present.

Profiles come from GenFarmer's local API: `GET http://127.0.0.1:55554/devices/random`
returns one random real-device profile (with `dmMIN.version.release` + `dmMIN.sdk`).
The endpoint is **unauthenticated on localhost** (do not send a token — a stale
one 401s) and **ignores server-side version filters**, so we filter client-side.

---

## The two flows

### 1. New account — rotate to a fresh, guaranteed Android-12+ identity
```bash
# on the GenFarmer host (ADB_PATH / ADB_BIN must point at adb)
scripts/changedevice.py apply --serial <serial> --random --min-android 12
# then immediately save what it became, tied to the account:
scripts/changedevice.py capture --serial <serial> --save accounts/<acct>.props
```
`--random` generates a **new** `serialno` (each account = a unique device). The
pool is ~1/6 twelve-plus, so `--min-android 12` loops `GET /devices/random`
(cheap, no reboot) until it draws a ≥12 profile, then applies that one — **one
reboot, no blind retry**.

### 2. Return to an existing account — restore its exact saved identity
```bash
scripts/changedevice.py apply --serial <serial> --profile accounts/<acct>.props
```
`--profile` restores the **exact** saved identity, **including the same
`serialno`** (`keep_serial` is on by default). This is required so Instagram sees
the same device the account was created on. Use `--no-keep-serial` only if you
deliberately want a new serial.

> **Why serial behaviour differs:** new account → new serial (accounts must not
> share a serial); return to account → same serial (the account is bound to its
> original device fingerprint). The CLI picks the right mode automatically
> (`--random` ⇒ new, `--profile` ⇒ keep).

### Programmatic (autoreg / posting)
```python
from src.genfarmer import default_client

client = default_client()
# registration: rotate then persist
profile = await client.fetch_random(min_android=12)
await client.apply(serial, profile, keep_serial=False)
await client.wait_reconnect(serial)
saved = await client.capture(serial)          # store saved.to_props() per account
# later, returning to the account:
await client.apply(serial, DeviceProfile.load("accounts/acct.props"))  # keep_serial=True
await client.wait_reconnect(serial)
```
`GenFarmerAutoRotator(min_android=12)` wraps the rotate+reconnect half behind the
`DeviceIdentityRotator` interface used by registration.

---

## Validation (round-trip, 2026-06-11)

On SM-G781B (`100.91.90.9`): `capture` (baseline) → `apply --random` → `apply
--profile <baseline>`:

| field | baseline | after `--random` | after restore |
|---|---|---|---|
| build fingerprint | `…r8q:12/…G781BXXU4DVC1` | `…r8q:11/…G781BXXU1BUA5` | `…r8q:12/…G781BXXU4DVC1` ✅ |
| serialno | `ce8fb4b49e27b7c763ed` | `ce7b0d55b7f29e5d4d75` | `ce8fb4b49e27b7c763ed` ✅ |
| android_id | `cffc97e303863479` | (unchanged) | `cffc97e303863479` ✅ |

**23/23 identity fields restored exactly**; only `ro.boot_id` differs (a per-boot
UUID, not part of the identity). Apps (Instagram, agents, IME) survived the change.

---

## Caveats & gotchas

- **`--profile` applies the exact profile and PRESERVES apps.** An earlier
  "it removed Instagram / produced a Frankenstein identity" conclusion was a
  **wrong-phone check** — always confirm you're reading the same serial you
  changed (a changed phone may reappear on a different Tailscale IP / LAN IP).
- **Tailscale auto-reconnect after the reboot is flaky on the REMOTE path**
  (always-on VPN helps but isn't guaranteed). On the **LAN** it's a non-issue:
  the phone returns on its local IP and GenFarmer re-finds it by serial. Run the
  fleet orchestration on the LAN. Never `am force-stop`/`pm disable` Tailscale on
  a phone reached **over** the tunnel — it kills the link.
- **`android_id` does not change with the default `change_device`** (only
  model/device/fingerprint/serialno do). For maximal isolation of a NEW account
  use `--clear-data` (`wipe_data_change`), which rotates the real `android_id`
  but also wipes app data. For **restore**, the default (no clear-data) is
  correct — you want the account's app state intact.
- **The change reboots the phone**; it is destructive/hazardous on a phone you
  cannot physically recover. See the hazard notes in
  [`genfarmer-change-device.md`](./genfarmer-change-device.md).
- Reference team tooling lives in the GenBR archive
  (`genfarmer_change_device.py`, `genfarmer_app_backup.py`,
  `genfarmer_scheduler.py` = `backup → change_device → restore`); this module is
  the clean, tested re-implementation of its adb-props core.
