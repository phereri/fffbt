# GenFarmer asar extraction — structural findings

**Status:** evidence-only, no production code, no devices touched.
**Scope:** what the 10 files extracted from `app.asar` on 2026-05-18 confirm or rule out.
**Source files (all from `C:\Users\Administrator\AppData\Local\Programs\GenFarmer\resources\app.asar`):**

- `dist/preload/index.js`
- `dist/preload/index.d.js`
- `dist/main/index.js`
- `build/migrations/0000_glamorous_titania.sql`
- `build/migrations/0001_short_puck.sql`
- `build/migrations/0002_strong_sentry.sql`
- `build/migrations/0003_smooth_khan.sql`
- `build/common/genfarmer_install.sh`
- `build/common/main.py`
- `build/common/run.py`

Classification per the research rules: **confirmed / likely / unknown**.

---

## 1. IPC contract — **NOT in the preload** (correcting prior prediction)

**Confirmed.** The preload is a 10-line passthrough — it does not declare any named IPC channels:

```js
// dist/preload/index.js — full file
const { contextBridge, ipcRenderer, webUtils } = require('electron');
contextBridge.exposeInMainWorld("ipcRenderer", {
  invoke: ipcRenderer.invoke.bind(ipcRenderer),
  on: ipcRenderer.on.bind(ipcRenderer),
  off: ipcRenderer.off.bind(ipcRenderer),
  removeAllListeners: ipcRenderer.removeAllListeners.bind(ipcRenderer),
  webUtils,
});
contextBridge.exposeInMainWorld("webUtils", webUtils);
```

`dist/preload/index.d.js` is a CJS exports stub with no declarations — there is no centralized type contract.

**Implication.** Every IPC call from the renderer is `window.ipcRenderer.invoke("<channel-name>", arg)`. The channel names — including any for `ChangeDevice`, `BackupRestoreV2`, `MockGPS`, per-device proxy — are **only** discoverable from:

1. The Vue renderer chunks (`dist/renderer/assets/<Page>-<hash>.js`) — plain JS, greppable for `ipcRenderer.invoke(`.
2. The bytenode-compiled `dist/main/main.jsc` — V8 bytecode, recoverable but harder.

The previous hypothesis ("preload tells us the IPC surface") is **wrong**. We need to pivot to renderer chunks for the IPC enumeration.

## 2. Main entry — **confirms bytenode** (no usable info on its own)

`dist/main/index.js` is three lines:

```js
require('bytenode')
module.exports = require('./main.jsc')
```

So everything real — Express route table, socket.io setup, port bindings, IPC handler registration, the call into the `:58211` Python service, ADB orchestration — lives in `main.jsc`. The bootstrap reveals nothing about ports or handlers.

## 3. Local DB schema — **confirmed**, decodes most REST errors

The 4 migration files give a complete SQLite schema. Tables and the columns we care about:

| Table | Key columns | Notes |
|---|---|---|
| `apps` | `id text PK`, `user_id int`, `name`, `version`, `created_at`, `updated_at`, `expired_at`, `locale_input` (0003) | What `/automation/apps` returns. `id` is **text**, not int — explains "datatype mismatch" when sort/filter binds expect int. |
| `tasks` | `id text PK`, `user_id int`, `app_id text FK→apps`, `task_source int`, `task_table_account_id int`, `name`, `input`, `variables`, `enable_input`, `config text DEFAULT [object Object]`, `devices text DEFAULT [object Object]` | The `[object Object]` default is a bug in their migration — it's a literal SQLite default string `"[object Object]"`, not JSON. Worth flagging if we ever read these defaults. |
| `task_runs` | `id text PK`, `user_id int`, `task_id FK`, `app_id FK`, `input`, `variables`, `enable_input`, `config`, `devices`, `status int DEFAULT 0`, timestamps | What `/automation/runs` returns. |
| `task_run_device_status` | `id text PK`, `device_id`, `status int`, `run_id FK→task_runs ON DELETE CASCADE`, timestamps | Per-device status inside a run. |
| `task_run_device_storages` | `id text PK`, `device_id`, `run_id FK CASCADE`, `data text`, `created_at` | This is what `runs/:id/storages` returns. `data` is a free-form text blob — probably JSON. |
| `devices` | `id int PK AUTOINC`, `index int`, `name`, `current_device_id`, `serial_no UNIQUE text`, `width`, `height`, `type`, `connection_type`, `connection_interfaces text`, `created_at` | Confirms the device triple. `current_device_id` distinct from `serial_no` strongly suggests it tracks the result of Change Device (the "new" identity), while `serial_no` is the stable adb serial. |
| `device_groups` | `id int PK`, `user_id`, `name`, `list_serial_no text` | Comma-or-JSON list of serials per group. |
| `router_proxy` | `id text PK`, `keyValue`, `device_id`, `user_id`, `socks5`, `rotate_time int`, `proxy_type text DEFAULT 'socks5Proxy'`, `is_custom_rotate int DEFAULT false`, `expired_at`, `rotate_at`, `started_at`, timestamps | **Per-device proxy assignment IS persisted locally.** GenRouter integration writes here. Whether mutating this row reconfigures the proxy live or only on next rotate is the open question. |
| `apks` | `id text PK`, `parent_path`, `path`, `name`, `icon`, `package_name`, `min_sdk_version int`, `target_sdk_version int`, `version`, `size real`, `created_at` | APK cache on disk. Useful for our split-APK install needs. |
| `accounts` | `id text PK`, `user_id`, `table_id`, `device_id`, `data`, `status int`, `log`, timestamps, `platform_status text` (0002) | Tracked accounts (out of MVP scope). |
| `schedules`, `task_schedules`, `table_account`, `table_tree` | — | Cron scheduling and table-source data. |

**Not in the schema** (and therefore filesystem-managed):

- **No `backups` / `device_backups` table.** Backup/Restore is filesystem-based — we can't enumerate backups via the local DB.
- **No `change_device_history`.** Either ephemeral or stored as JSON in `devices.connection_interfaces`.
- **No `mock_gps` table.** Likely a per-device IPC command, not persisted state.

## 4. `:58211` — **a SECOND local API surface, not the same as `:55554`**

`build/common/run.py` and `main.py` together stand up a separate FastAPI service:

```python
# run.py
uvicorn.run("main:app", workers=args.workers, host="127.0.0.1", port=58211)
```

```python
# main.py — single WebSocket endpoint at /ws
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await handle_device(websocket)
```

The handler:

1. Reads a device serial from the first WS message.
2. Creates an `Android(serialno, CAP_METHOD.MINICAP, TOUCH_METHOD.MINITOUCH, ORI_METHOD.MINICAP)` device via the bundled `gentest` library (image-recognition automation; `gentest.core.android`, `gentest.core.cv.Template`, `try_log_screen` — pattern matches Airtest).
3. In a loop, receives JSON tasks `{ id, task: { file_path, click, timeout, index_pos, click_delay, threshold, method } }`.
4. For each task, captures the screen via MINICAP, runs `query.match_in(screen)` (CV template match on the **desktop**), and if found, sends MINITOUCH events to click.

**This is a third control plane** for image-template based touch automation. Operationally:

- Frames flow **device → desktop** (minicap).
- Template matching runs on **desktop CPU**.
- Touches flow **desktop → device** (minitouch).
- Coordinator on the GenFarmer side (in `main.jsc`) opens the WS and feeds tasks.

This was previously listed in `genfarmer-api.md` as `unknown / JSON-RPC mobile control path`. It is now **confirmed: FastAPI WebSocket on `127.0.0.1:58211`, single endpoint `/ws`, CV-template task protocol**.

**Implications for MVP:**

- Host CPU load scales with the number of concurrently-running CV tasks (one frame match per device per task per ~500 ms). Worth measuring before we plan many parallel phones.
- `:58211` is loopback-only by code (no env override visible). Off-loopback control of this service would require a code patch — unlike `:55554` which is dual-stack `::` and just firewall-gated.
- If GenFarmer Reels-Trial scripts use mostly image clicks rather than UI-Automator selectors, our timing and reliability hinge on this stack rather than on `atx-agent`. Worth confirming when we look at an actual `app.script` for Reels Trial.

## 5. `genfarmer_install.sh` — **generic split-APK installer, not GenFarmer-specific**

```sh
#!/system/bin/sh
# Usage: $0 packageName apk [[[apk] apk] ...]
# pm install-create -S TOTAL -i pkg -r  →  pm install-write per split  →  pm install-commit
```

The whole script is a standard Android split-APK installer using `pm install-create` / `pm install-write` / `pm install-commit`. It is **not GenFarmer-specific**. Useful as a reference for our own Instagram split-APK install flow, but tells us nothing about the GenFarmer privileged-device-side flow we hoped to learn from.

Per the previous prediction ("install/launch scripts tell us how atx-agent and genauto-agent start"), this script is **not the one we want**. The actual atx-agent/genauto-agent launch must live elsewhere — likely embedded in `main.jsc` as a string of `adb shell` commands, since the asar listing showed no other `.sh` or launch wrapper under `build/common/`.

## 6. No secrets in the extract

All 10 files reviewed. No credentials, tokens, bearer headers, or vendor backend hostnames were exposed.

---

## Updated open questions

| Topic | Was | Now |
|---|---|---|
| IPC contract enumeration | "Read preload to enumerate" | **Read renderer Vue chunks for `ipcRenderer.invoke(` strings** |
| `:55554` `datatype mismatch` mystery | unknown | likely: `apps.id` is text, sort/filter param-bind expects int |
| Per-device proxy assignment | unknown | likely: write to `router_proxy` table; need to confirm whether the daemon picks up changes or caches |
| `:58211` Python service | unknown | **confirmed: FastAPI WS `/ws` for CV-template clicks** |
| Backup/Restore inventory | unknown | confirmed not in DB → must be filesystem-discovered |
| atx-agent / genauto-agent launch on device | unknown | unknown (not in `build/common/`; embedded in `main.jsc`) |
| Off-loopback bind for `:55554` | likely-yes | still likely-yes; auth-on-the-port still unknown |
| MockGPS surface | unknown | unknown (not in DB; likely IPC-only) |
| Change Device mechanism | unknown | likely: alters `devices.current_device_id`; per-device propagation unknown |

---

## Next safe step — single, small, no app restart

To unlock IPC channel names without touching `main.jsc`, extract the renderer chunks we already know exist from the asar listing. Three target features cover the MVP unknowns:

```powershell
$asar = "C:\Users\Administrator\AppData\Local\Programs\GenFarmer\resources\app.asar"
$out  = "$env:USERPROFILE\Desktop\gf-extract-r2"
mkdir $out; cd $out

# Find the actual hashed filenames first (printed in the previous asar-listing.txt under dist/renderer/assets/)
# then extract just those, e.g.:
asar extract-file $asar dist/renderer/assets/ChangeDevice-<hash>.js
asar extract-file $asar dist/renderer/assets/BackupRestoreV2-<hash>.js
asar extract-file $asar dist/renderer/assets/Device-<hash>.js
asar extract-file $asar dist/renderer/assets/DeviceAction-<hash>.js
asar extract-file $asar dist/renderer/assets/GenRouter-<hash>.js
```

Then zip and attach. These chunks are minified but readable JS; a single `grep ipcRenderer.invoke` will enumerate every channel name used by those pages, giving us the entire IPC surface for Change Device / Backup-Restore / per-device proxy.

**Do not yet** extract `main.jsc`, run any state-changing GenFarmer action, or open `:55554` over Tailscale.
