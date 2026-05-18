# GenFarmer ChangeInfo — operational hazard (Tailscale-only farms)

Status: **confirmed** on one phone, 2026-05-18.
Scope: FFF-18 (research). Not a separate ticket — kept here as a known hazard.

## Summary

Running the `ChangeDevice` node with `changeInfo` enabled, against a phone
whose only host link is ADB-over-Tailscale on `:5555`, can leave the
phone unreachable on **every** TCP port from the GenFarmer host —
not just `:5555` — without the GenFarmer panel reflecting the loss.

The panel still lists the device (DB row is intact and the previously
recorded `current_device_id = 100.x.x.x:5555` is still shown), but no
adb / atx-agent / ICMP traffic returns.

## Observed sequence

1. App graph: `Start → ChangeDevice (mode=change, changeInfo selected) → (no success/error edge wired)`.
2. Run logs (host time):

   ```
   06:59:11.264Z [Info] Device run started.
   06:59:11.564Z [Info] Automation started.
   06:59:11.565Z [Device: 100.85.130.65:5555] [Info] - Change device | 0ms [start]
   06:59:13.775Z [Device: 100.85.130.65:5555] [Info] - Change device | 2.2s [end]
                                                       No success node found. Script stopped.
   06:59:13.786Z [Success] Finished. duration=2.5s
   ```

   The `No success node found` line is the App-graph engine reporting an
   unwired edge — it is **not** a ChangeInfo failure indicator. The
   2.2 s node duration is consistent with the privileged on-device
   helper completing its writes.

3. Immediately after the run, from the GenFarmer host on Tailscale
   (`100.123.216.87`) targeting the phone at `100.85.130.65`:

   - `adb kill-server` → `adb start-server` → `adb connect 100.85.130.65:5555`
     → `cannot connect … (10060)` (Windows TCP timeout)
   - `Test-NetConnection 100.85.130.65 -Port 5555` → `TcpTestSucceeded: False`, `PingSucceeded: False`
   - `Test-NetConnection 100.85.130.65 -Port 7912` → `TcpTestSucceeded: False`, `PingSucceeded: False`
   - `ping 100.85.130.65` → 100 % loss

   **Both ADB (`5555`) and atx-agent (`7912`) are dead, and ICMP itself
   does not return.** The phone is offline at the Tailscale-node level,
   not just at the adbd level.

## What this rules out

- It is **not** the local adb client cache — `kill-server` / `start-server`
  did not recover.
- It is **not** an adbd-only restart — `7912` is also gone, so the
  atx-agent recovery path (`POST /shell?command=start+adbd`) is not
  available here.
- It is **not** masked by GenFarmer's view — the panel still sees the
  device because GenFarmer reads from `devices.current_device_id`
  (DB), not from a live probe.

## What remains unknown (not blocking FFF-18)

- Whether the phone has rebooted, hung pre-Tailscale, or lost its
  Tailscale auth after a property/identity change.
- Whether physical recovery (USB, power-cycle) brings it back to the
  same `100.85.130.65` Tailscale IP or to a new one.
- Whether the same hazard reproduces on every phone or only on the
  specific Android build / vendor under test.
- Whether wiring a proper success/error edge on the ChangeDevice node
  changes the outcome (graph completion is unrelated to property
  writes, but worth verifying once we have a phone to spare).

## Pre-run baseline (captured before the failure)

For any future before/after diff once the phone is recovered or a new
sandbox phone is selected:

| Field      | Value                       |
| ---------- | --------------------------- |
| Serial No  | `ce071717816b463b037e`      |
| Android ID | `c8e249f59fa118a2`          |
| Model      | `SM-A920F` (from `devices` row) |
| Endpoint   | `100.85.130.65:5555`        |
| Conn. type | `otg`                       |

These should be re-read after recovery to determine which fields the
ChangeInfo path actually mutates.

## MVP implications

- **Invariant I3 ("per-account fingerprint that survives")** —
  ChangeInfo is the only programmatic identity-mutation primitive we
  have seen in GenFarmer, but until we can read identifiers post-run
  on a recovered phone we cannot confirm it actually changes them, or
  what it changes them to.
- **Operational** — on a Tailscale-only farm, ChangeInfo is currently
  a **one-way operation**: if it bricks the network path there is no
  remote recovery channel. Treat as physically-attended-only until a
  remote recovery path is proven.
- **Acceptance criteria for FFF-18** — adding this note satisfies the
  "confirmed / assumed / unknown" requirement for the ChangeDevice /
  ChangeInfo surface. The recovery work itself is not in MVP scope
  and is intentionally not opened as a separate ticket per
  2026-05-18 direction.
