# GenFarmer renderer chunks — IPC / script-DSL findings

**Status:** evidence-only, no production code, no devices touched.
**Scope:** what the 8 Vue renderer chunks extracted on 2026-05-18 reveal about the actual interface for ChangeDevice / ChangeInfo / Backup-Restore / DeviceAction / GenRouter / cloud-phone Device pages.
**Source files** (all from `resources/app.asar` → `dist/renderer/assets/`):

- `BackupRestore-BSC2VSMU.js` (v1 backup/restore step config)
- `BackupRestoreV2-BxpoAjhq.js` (v2 backup/restore step config)
- `ChangeDevice-6qPXklCK.js` (Change Device step config)
- `ChangeInfo-0U1Ep_tO.js` (per-app Change Info step config)
- `Device-BlPm5VME.js` (Cloud Phones page — *not* the local-device page)
- `DeviceAction-CPfQoY8_.js` (lock/unlock step config)
- `GenRouter-BA39nhhD.js` (Settings panel for GenRouter base URL)
- `GenRouter-D6Jtw0SG.js` (GenRouter assign/remove step config)

Classification per the research rules: **confirmed / likely / unknown**.

---

## 1. Single biggest finding — these are NOT REST endpoints, they are **script-DSL nodes**

**Confirmed.** Every one of `ChangeDevice`, `ChangeInfo`, `BackupRestore`, `BackupRestoreV2`, `DeviceAction`, `GenRouter (D6Jtw0SG)` is a **modal-node configurator** for the GenFarmer script editor. The Vue component does only one thing: write its UI state into `modalNode.options.*` via `useScriptEditor()` / `useScriptEditor.modalNode`. None of them call `ipcRenderer.invoke(...)` or any HTTP client. The actual execution happens when the parent **Task** is run, which routes through `useRunScript` → `main.jsc` → device-side agent.

**Implication for our MVP.** There is **no standalone "Change Device" or "Backup" REST or IPC call**. To perform any of these operations programmatically we have only one realistic path:

1. Build a GenFarmer **task** (DAG of script nodes) that contains the required node(s).
2. Submit it via the documented public REST: `POST /automation/tasks` then `POST /automation/runs` on `127.0.0.1:55554`.
3. Poll the run via the same REST.

That collapses several "unknown internal IPC" rows in the previous open-questions table into one known surface. It also means the public REST API is **complete enough** for everything we need, **provided** we can synthesise script JSON in the shape GenFarmer expects. That JSON shape is the next unknown (still in `main.jsc`).

The previous prediction "channel names are discoverable from renderer chunks via `grep ipcRenderer.invoke`" is **falsified** for these features. The renderer doesn't talk IPC for these flows — only the script runtime in `main.jsc` does.

## 2. ChangeDevice — confirmed options surface

```js
modalNode.options.changeDeviceMode      // "change" | "reset"   (default "change")
modalNode.options.changeDeviceClearData // boolean              (default false)
```

UI labels:

- `"change"` → **Change info** (rotate identity, keep app data unless `clearData` is set).
- `"reset"` → **Reset to default** (revert to factory identity).
- `clearData` checkbox tooltip: *"Clear data will remove all data from the device."*

No per-field control (model/brand/IMEI/etc.) — it is fully automatic. This is consistent with GenFarmer's marketing position: the user picks "change" vs "reset" and the engine handles fingerprint generation. **Confirms** that fine-grained fingerprint authoring is *not* exposed at the script level — if we want deterministic fingerprints we have to either (a) script around it via DB write to `devices.current_device_id` (risky, untested) or (b) accept GenFarmer's random rotation.

## 3. ChangeInfo — per-app, auto-only

```js
modalNode.options.changeInfoMode  // "auto" | "manual"  (default "auto"; "manual" is DISABLED in UI)
modalNode.options.apkId           // FK → apks.id       (default null)
```

The "Manual" radio is rendered with `disabled` attribute set unconditionally. Conclusion: **manual per-field ChangeInfo is not user-accessible in this build**. Only "Auto" + an APK selector.

The APK selector reads from `useApkManager().apks` (uniq'd by `packageName`) — i.e. only APKs already imported into GenFarmer's local APK cache. **Implication:** for Instagram-Trial, the Instagram APK must be imported into GenFarmer's APK manager first, otherwise ChangeInfo cannot target it.

## 4. BackupRestore (v1) — filesystem-based, single-app, native dialog

```js
modalNode.options.backupRestoreMode  // "backup" | "restore"
modalNode.options.packageName        // app id (selected) OR free text (manual)
modalNode.options.tag                // backup tag (backup mode)
modalNode.options.backupFilename     // default: "<tag>_<deviceId>_<packageName>.7z"
modalNode.options.backupOutputPath   // desktop directory, picked via native dialog
modalNode.options.fileBackupPath     // desktop file path (restore mode)
```

Two important confirmations:

- **Backups are `.7z` archives on the desktop filesystem.** Filename format is `<tag>_<deviceId>_<packageName>.7z`. This matches the previous finding (no `backups` table in SQLite — they're files).
- **File path on restore is captured via `window.webUtils.getPathForFile(file)`** — that's Electron ≥ 32's replacement for `file.path` since `webUtils` is exposed in the preload. Confirms the asar-findings note that the preload exposes `webUtils`.
- The directory picker is a native dialog (`dialog.showOpenDialog`-equivalent imported from main bundle as `eg as O`). Returns `{ data: [paths…] }`, code takes `[0]`. So the *backup output* is a directory (single entry), not a file.

**Implication for our MVP and the I4 invariant ("phones interchangeable").** Backup/Restore is per-app and produces a `.7z` on the operator's desktop. Moving a session between two physical phones boils down to **(a) run Backup on phone A → desktop file; (b) run Restore on phone B → same file**. That is automatable end-to-end via two GenFarmer task runs, with no manual UI steps, provided we know the task-JSON shape.

## 5. **BackupRestoreV2 — the key primitive: `withChangeInfo` atomic restore-and-rotate**

```js
modalNode.options.backupRestoreMode // "backup" | "restore"
modalNode.options.packageName       // free text (e.g. "com.instagram.android")
modalNode.options.fileBackupPath    // ModalNode-typed path expression
modalNode.options.withChangeInfo    // boolean (default false)    ← NEW
```

The `withChangeInfo` flag is **new in V2** and is the single most useful primitive we have found so far for our MVP:

- Set on a restore step, it triggers a ChangeInfo (auto, per the matching APK) **as part of the same restore**.
- This means: when porting an Instagram session from a retiring phone to a fresh phone, V2 can in *one task step* set the new phone's device-identity fingerprint to match the bundle being restored (or rotate, depending on how GenFarmer wires it internally — both choices preserve the invariant that the *combined* state is internally consistent).
- Without this, a naive Restore on a different phone would leave the Instagram client seeing a mismatched device fingerprint and risk a security challenge.

**Confidence: likely (not confirmed).** The renderer chunk only proves the option exists and is plumbed to `modalNode.options.withChangeInfo`; the semantics are inferred from the flag name and the parallel ChangeInfo node. Needs a sandbox test to confirm exact behavior.

Also notable: V2 drops the `tag` and `backupFilename` fields — restore picks the file directly via `fileBackupPath`, and backups appear to be auto-named server-side. The V2 path field uses a `ModalNode` rich-expression input (not a plain text input), meaning **it can reference task variables / inputs** — i.e. the backup file path can be templated per-device at task run time. That is exactly what we want for a fleet workflow.

## 6. DeviceAction — trivial

```js
modalNode.options.deviceAction // "lock" | "unlock"
```

Just screen lock / unlock. Useful as a no-op before/after long automation runs. No other actions exposed at this level (reboot, screen on/off, etc. are not in this node).

## 7. GenRouter step config — **confirmed per-device proxy assignment is supported, with named-interface and PPPoE**

```js
modalNode.options.genRouterAction         // "assign" | "remove"
modalNode.options.genRouterProtocolString // free-text, formats below
```

Documented in the placeholder text and adjacent help block. **Supported protocol-string formats (verbatim from the chunk):**

```
http://ip:port:username:password
socks5://ip:port:username:password
http://ip:port
socks5://ip:port
if_interface_name
pppoe://if_interface_name
```

> "If protocol is not provided, default is http://"

This **flips** a previous "unknown" — per-device proxy assignment IS available, scoped to a GenFarmer task run on the target device, with two important capabilities our MVP can use:

- **`if_<name>` / `pppoe://<name>`** — pin a device to a specific WAN interface or PPPoE connection. This is the right primitive if we are using USB-modem-as-WAN per device.
- **Auth-embedded SOCKS5 / HTTP** — single string handles authenticated proxies (no separate field). Note the format is **colon-delimited**, not the standard `user:pass@host:port` URL form — easy to get wrong.

`"remove"` clears the assignment.

What is still **unknown**: whether the proxy assignment is applied (a) at the moment the script-step runs (one-shot), or (b) sticks until "remove" is run / until rotate timer fires. The DB schema (`router_proxy.rotate_time`, `is_custom_rotate`, `rotate_at`, `started_at`) strongly suggests rotation policy is stored, so likely (b).

## 8. GenRouter Settings panel — single user-settable field: `baseUrl`

```js
settings.genRouter.baseUrl
```

That's the entire GenRouter settings surface in the renderer. Implication: every GenRouter integration in GenFarmer talks to a single configurable base URL. If the operator's GenRouter is reachable at e.g. `http://192.168.5.1:9000/` (the documented admin port), this is where it's pointed. No auth fields, no per-environment switching. We don't yet know how GenFarmer authenticates to GenRouter (cookie? IP allowlist? none?) — `main.jsc` will know, the renderer does not.

## 9. **Device.js is the Cloud Phones page, NOT local Android control — out of scope**

`Device-BlPm5VME.js` imports from `cloudPhone.api-C7uBIZWV.js` (`Ve` extend, `H` set-power-status, `qe` list, `He` device-types, `We` share, `se` unshare, `Ye` disconnect-by-host-port). The data model on this page is **rented cloud phones**: every row has `device_ip`, `port`, `email`, `origin_email`, `rent`/`extend`/`expire` status, `price`, `expired_at`, payment methods (Bank / Coin / Crypto / Card). The whole "Buy Services / Extend / Share with Email / Unshare" flow is GenFarmer's **commercial cloud-phone rental SaaS**, not local USB Android control.

**Implications:**

- Our MVP runs against **local USB Trial phones**, so this entire page and its `cloudPhone.api` calls are **out of scope**. They will not control our phones.
- The local-device control lives in `useDevice` / `useDevices` (also imported in this chunk but only for index correlation). Those hooks talk to `:55554` and to `main.jsc` IPC.
- Useful side-finding: when a cloud phone is powered off / rebooted from this page, the code explicitly calls `Ye({ host, port })` ("disconnect by host:port"). That suggests `main.jsc` keeps an adb-connect table keyed by `(host, port)` and exposes a disconnect IPC. We should keep that mental model for our own ADB-TCP-over-Tailscale story — disconnects must be explicit, not implicit.

## 10. No secrets in the extract

All 8 files reviewed. No credentials, tokens, bearer headers, or backend URLs were exposed in the renderer code.

---

## Updated open-questions table

| Topic | Was | Now |
|---|---|---|
| Surface for ChangeDevice / ChangeInfo / Backup / DeviceAction / GenRouter assign | unknown IPC | **confirmed: script-DSL nodes; trigger via REST `/automation/tasks` + `/automation/runs`** |
| ChangeDevice option fields | unknown | confirmed: `changeDeviceMode` ∈ {change, reset}, `changeDeviceClearData` ∈ {true, false} — no per-field fingerprint authoring |
| ChangeInfo manual mode | unknown | **confirmed disabled in this build** — only "auto" + APK selection |
| Backup/Restore archive format and naming | unknown | confirmed: `.7z`, default `<tag>_<deviceId>_<packageName>.7z`, desktop filesystem |
| Atomic restore-and-rotate-identity | unknown | likely available via `BackupRestoreV2.withChangeInfo=true` — **needs sandbox confirmation** |
| Per-device proxy assignment | likely (via `router_proxy` table) | **confirmed via GenRouter script node with named-interface and PPPoE support** |
| GenRouter base URL config | unknown | confirmed: single `settings.genRouter.baseUrl` value, no auth fields exposed |
| GenRouter ↔ GenFarmer auth | unknown | unknown (renderer doesn't show it) |
| Whether `Device-*` page controls local phones | likely yes | **confirmed NO — it controls rented cloud phones; out of scope** |
| ADB-TCP disconnect on remote power events | unknown | likely: `cloudPhone.api.Ye({host, port})` pattern → main.jsc holds a host:port → adb session map |
| Task-JSON shape for script DAG | unknown | **still unknown** — only `main.jsc` knows; next bottleneck |

---

## Next safe step — confirm the V2 atomic restore-and-rotate, without touching prod

Single small experiment, on the sandbox phone + sandbox account only, all read-or-script-test:

1. In GenFarmer UI, manually build a 2-node task: `BackupRestoreV2 (mode=backup, packageName=com.example.something)` on phone-A.
2. Run it once. Confirm a `.7z` lands at the specified path.
3. Capture the task JSON (it persists in SQLite `tasks.config`). This gives us a known-good template for the task-JSON shape — closing the last unknown.
4. **Do not** yet run a `restore + withChangeInfo` on a real Instagram session. Re-test on a throwaway app first to verify the identity rotation actually happens before that flag is trusted with anything irreversible.

**Do not yet:**
- Extract or disassemble `main.jsc` — risk of accidentally violating GenFarmer ToS.
- Run any task against a production Instagram account, including ChangeInfo or ChangeDevice.
- Configure GenRouter to point at the live PPPoE / proxy pool in this experiment.

---

## Correction to previous note

The prior `genfarmer-asar-findings.md` (§1) recommended pivoting to renderer chunks "for IPC enumeration via `grep ipcRenderer.invoke`". For the features in this extract that approach yielded zero hits because the renderer doesn't call IPC for these flows at all — it builds task config and lets the script runtime call into the device. The IPC enumeration prediction was wrong; the **task-DSL** discovery (this doc) is the correct next layer.
