# Programmatic "Change Device" (GenFarmer Automation API)

Run GenFarmer's **ChangeDevice** (per-app device-identity rotation) headlessly via
the local REST API, and read its run logs — no desktop clicking. Verified against
GenFarmer **2.6.0** on this VPS (2026-06-09).

## ⚠️ HAZARD — read first
ChangeDevice (`changeInfo`) **bricked a Tailscale-only phone** in a prior test
(`docs/research/genfarmer-changeinfo-hazard.md`, 2026-06-08): the phone lost adb
(`:5555`) **and** atx-agent (`:7912`) **and** ICMP — fully offline at the Tailscale
level, with **no remote recovery** (needs physical USB/power-cycle). The node waits
`timeoutAdbReconnect: 60s` for adb to return; if it doesn't, the phone is gone
remotely. **Only run when the phone can be physically recovered.** SM-G973F
(100.91.90.9) is currently the only online phone.

## The pieces (live values on this install)
- Local REST API: `http://127.0.0.1:55554` (no auth header; localhost only). Reachable
  from the Windows host, not from WSL/VPS directly.
- SQLite DB (read-only source of IDs): `C:\Users\Administrator\.genfarmer\db.sqlite`
- **ChangeInfo app** (a flow `Start → ChangeDevice`): appId **`UowE7zCgq_uHhrVMLA64X`**
  (name `"[TEST] ChangeInfo"`). The `ChangeDevice` node options: `changeDeviceMode:"change"`,
  `nodeTimeout:120`, `timeoutAdbReconnect:60`.
- userId: `GET /backend/auth/me` → `data.id` (currently **28188**; note older DB rows
  use `21237` — if a create fails on ownership, try 21237).
- Device triple comes from the `devices` row: `{id: current_device_id, serialNo: serial_no, name}`.
  e.g. SM-G973F → `{"id":"100.91.90.9:5555","serialNo":"ce08171875e2dc580d7e","name":"SM-G973F"}`.

## Steps (App → Task → Run → logs)
All bodies are JSON; `Content-Type: application/json`.

1. **userId**: `GET /backend/auth/me` → `data.id`.
2. **Create a Task** binding the ChangeInfo app to the device:
   ```
   POST /automation/tasks
   {"appId":"UowE7zCgq_uHhrVMLA64X","input":[],"userId":28188,
    "name":"ChangeInfo-SM-G973F",
    "devices":{"enable":true,"list":[{"id":"100.91.90.9:5555","serialNo":"ce08171875e2dc580d7e","name":"SM-G973F"}]}}
   ```
   → `data.id` = taskId. (You can also reuse an existing task and repoint it with
   `PUT /automation/tasks/:id/add-devices`, body `{"devices":{"enabled":true,"list":[...]}}`.)
3. **Create a Run**: `POST /automation/runs`
   `{"userId":28188,"taskId":"<taskId>","appId":"UowE7zCgq_uHhrVMLA64X","status":0}` → `data.id` = runId.
4. **Execute**: `PUT /automation/runs/<runId>/run`.
5. **Read status + logs**:
   - `GET /automation/runs/<runId>` → run status (terminal status seen in DB: `status:4`).
   - `GET /automation/runs/<runId>/storages` → the **output logs** (`task_run_device_storages.data`
     blob). This is the "Change device | start/end" log the operation writes.
   Equivalent in DB: `task_run_device_status` (per-device status) + `task_run_device_storages` (logs).
6. **SAFETY CHECK (critical)**: immediately `adb connect <serial>` + `adb -s <serial> get-state`.
   If it does not return `device` within ~60-90s, ChangeDevice bricked the link → physical recovery.
   On success, re-read the fingerprint (`getprop`, `settings get secure android_id`) to confirm
   which identity fields changed (and capture them for the account's CSV row).

## Ready-to-run
`scripts/gf_change_device.py <serial>` does steps 1-6 (run it ON the Windows host via
`run_with_env.ps1`). It refuses to proceed unless `--yes-i-can-physically-recover` is passed.
This is the basis for the `GenFarmerAutoRotator` in `src/registration/rotator.py`.
