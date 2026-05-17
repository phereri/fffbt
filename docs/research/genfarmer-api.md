# GenFarmer API surface — research notes

- Status: research draft (no production code touched)
- Owner: Research Agent (FFF-18)
- Last updated: 2026-05-17
- Issue: FFF-18 — *Research and document current GenFarmer API surface*
- Scope: MVP — Instagram Reels Trial posting on physical Android devices via
  GenFarmer / GenRouter / ADB / MockGPS / Appium.

This document describes **what is publicly documented about GenFarmer's API**,
**what is observed in product behavior**, and **what is unknown**. Every claim
is tagged `confirmed`, `likely`, `assumption`, or `unknown`. No GenFarmer
binary was executed and no traffic was captured for this pass — findings come
from the vendor's English support docs and the GenFarmer / GenRouter product
pages. Direct verification on a running GenFarmer install is listed under
"Safe next steps".

## 1. Surfaces at a glance

GenFarmer is a desktop "phone farm" controller for physical Android devices
(also targets BoxPhone hardware). The vendor docs describe one programmable
surface plus several UI-only surfaces. The issue brief asks for four
categories; they map as follows:

| Surface | Status here | One-line summary |
|---|---|---|
| Public local REST API | **confirmed** (docs published) | Localhost-only REST on `127.0.0.1:55554`, covers Automation Apps / Tasks / Runs and `auth/me`. |
| Internal Electron IPC | **unknown** | GenFarmer's process model is not described in the public docs; "Electron" is our hypothesis, not vendor-confirmed. |
| Device-side privileged flow | **likely** | Product features (Change Device, Backup, Stream, Install APK, ADB Command) are UI-only and clearly do privileged work on the phone; the exact mechanism (ADB shell, on-device agent APK, root, accessibility) is not documented. |
| JSON-RPC / mobile control path | **unknown — likely absent** | The published API is REST. Vendor docs explicitly mention no JSON-RPC or websocket channel. |

The rest of this document expands each row.

## 2. Public local REST API

### 2.1 Base URL and transport — confirmed

- Base URL: `http://127.0.0.1:55554/` — *confirmed* (vendor docs).
- Plain HTTP, JSON request bodies, `Content-Type: application/json`.
- Examples in the docs are `curl` calls with **no `Authorization` header** and
  no API key — *confirmed* in the published examples.
- The server runs alongside the GenFarmer desktop app and is reachable only on
  loopback — *likely* (the docs only show `127.0.0.1`; a bind-address option
  is not documented either way).

### 2.2 Documented endpoints — confirmed

Quoted from the API page on the GenFarmer GitBook:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/automation/apps` | List Automation Apps, paginated. |
| `GET` | `/automation/apps/:id` | Get one app. |
| `PUT` | `/automation/apps` | Update an app. |
| `DELETE` | `/automation/apps` | Delete an app. |
| `POST` | `/automation/tasks` | Create a Task (bind an App to a name + devices + input). |
| `PUT` | `/automation/tasks/:id` | Update a Task. |
| `PUT` | `/automation/tasks/:id/add-devices` | Attach devices to a Task. |
| `PUT` | `/automation/tasks/:id/remove-devices` | Detach devices (no payload example in docs). |
| `DELETE` | `/automation/tasks` | Delete a Task. |
| `POST` | `/automation/runs` | Create a Run for a Task. |
| `PUT` | `/automation/runs/:id/run` | Execute a Run. |
| `GET` | `/automation/runs` | List Runs. |
| `GET` | `/automation/runs/:id/storages` | Retrieve a Run's output ("storages"). |
| `GET` | `/backend/auth/me` | Current user info. |

A Postman collection is referenced from the docs page. Not downloaded for
this pass — see next steps.

### 2.3 Sample payloads — confirmed (verbatim from docs)

`POST /automation/tasks`:

```json
{
  "appId": "adoFNVNN6Jwl8FlbZCfni",
  "input": [],
  "userId": 3,
  "name": "test",
  "devices": {
    "enable": true,
    "list": []
  }
}
```

`POST /automation/runs`:

```json
{
  "userId": 3,
  "taskId": "uNDVKT_VwhEDuXT3PGUUv",
  "appId": "adoFNVNN6Jwl8FlbZCfni",
  "status": 0
}
```

`PUT /automation/tasks/:id/add-devices`:

```json
{
  "devices": {
    "enabled": true,
    "list": [
      { "id": "emulator-5554", "serialNo": "00f65a5d", "name": "SM-G960N" }
    ]
  }
}
```

Notes:

- The task-create payload uses `"enable"`; `add-devices` uses `"enabled"`.
  That inconsistency is in the vendor docs verbatim — *confirmed*. Any client
  we write must send the field exactly as the endpoint expects.
- Device identifier triple is `{ id, serialNo, name }`, where `id` looks like
  an ADB serial (`emulator-5554`) — *confirmed* from the docs example.
- `userId` is numeric and required on Tasks and Runs — *confirmed*. The
  source of that number for our setup is *unknown*; presumably from
  `GET /backend/auth/me`.

### 2.4 Authentication and exposure — likely / unknown

- The API is **localhost-only** in published examples — *likely*.
- No auth header is shown — *confirmed* in the docs, but this is **not** a
  vendor guarantee that the server accepts arbitrary calls; behavior on a
  real install must be verified.
- `GET /backend/auth/me` exists — *confirmed*. Whether it gates the rest of
  the API (session cookie from the desktop login) or is purely informational
  is *unknown*.
- Whether the server can be bound to a non-loopback address (e.g. to drive
  GenFarmer from a different host over Tailscale) is *unknown* — undocumented.
  **Do not assume yes.**

### 2.5 What is **not** in the REST surface — confirmed (by omission)

The published API does not include endpoints for:

- ADB `shell`, `push`, `pull`.
- APK install.
- Screenshot capture.
- Live screen streaming (scrcpy-style).
- Direct "Change Device" / Backup / Restore.
- MockGPS / location.
- Proxy assignment per device.
- Per-device start/stop/connect (the device list itself is referenced as a UI
  page; no list endpoint is in the API page).

These all exist as **product features in the desktop UI** (see §4) — they
are simply not exposed in the documented REST surface.

## 3. Internal Electron IPC — unknown

The issue brief assumed GenFarmer is an Electron desktop app with an internal
IPC channel between the renderer (UI) and the main process (privileged work).
The public docs **do not confirm or deny this**:

- *Unknown:* whether GenFarmer is Electron, Tauri, native (CEF/WebView2), or
  Qt. The product is described only as a "desktop application" with a
  "no-coding" UI.
- *Unknown:* the IPC mechanism between UI and backend, channel names, or any
  way to invoke IPC handlers from outside the app's own renderer.
- *Likely (cross-product):* if the desktop app is Electron, the renderer
  almost certainly talks to a Node main process via `ipcRenderer` /
  `ipcMain`, and the localhost REST server in §2 is probably a `koa` / `express`
  / `fastify` server running in that same main process. This is a hypothesis
  consistent with what the REST surface looks like, **not** a vendor claim.

**Do not design any component against an "IPC handler" assumption.** If we
need to reach GenFarmer programmatically, the only documented contract is
the REST API in §2.

## 4. Device-side privileged flow — likely (mechanism unknown)

These features exist and are documented in the UI guide, so we know
*something* on the phone is doing the work — but the docs describe the user
journey, not the protocol. We list each, what we know, and what is unknown.

### 4.1 Stream Device / Control Center — confirmed (exists)

- Real-time screen stream of the phone inside the desktop UI.
- Sidebar controls for screen size (40–100%), quality (Low / Medium / High /
  Extra), and multi-select via Ctrl+Click — *confirmed*.
- Quality/size knobs are *consistent with* an scrcpy-style H.264 stream over
  ADB. That GenFarmer wraps `scrcpy` is *assumption*, not confirmed.

### 4.2 ADB Command, Screenshot, Install APK — confirmed (exists), API-invocable: confirmed-no

- These are buttons inside the Stream Device panel — *confirmed*.
- The vendor docs explicitly note that the REST API does **not** expose ADB
  shell, file push, screenshot, or APK install — *confirmed*.
- The buttons therefore go through whatever internal channel the desktop
  uses (see §3, *unknown*).

### 4.3 Change Device — confirmed (exists), mechanism unknown

- Vendor description: "allows you to easily change the device information
  for each application … run two Telegram applications on the same phone,
  but each application will have different device information."
- *Likely:* operates per-app (not per-device), so the "device fingerprint"
  is scoped to a single Android package, not the whole phone.
- *Unknown:* whether this is done via Xposed/LSPosed-style hooking, an
  accessibility-driven flow, a Magisk/root module, an injected agent APK,
  or a custom on-device service. No public docs name the mechanism.
- *Unknown:* which exact fingerprint surfaces it touches (Build.* props,
  IMEI/IMSI, MAC, Settings.Secure.ANDROID_ID, advertising ID, hardware
  sensors). Marketing language only says "device information".
- *Unknown:* whether a non-rooted stock Android device is supported, or
  whether it requires root / a custom firmware / a specific GenFarmer
  "BoxPhone" model.

### 4.4 Backup / Restore — confirmed (exists), scope unknown

- Vendor description: "back up and restore your data automatically and
  flexibly."
- *Unknown:* whether this is per-app, per-account, per-device.
- *Unknown:* what is captured — app private data, accounts, Instagram
  session cookies, keystore? — and where the backup file lives.
- *Unknown:* whether backups can be moved between physical phones (which is
  what invariant **I3 / I4** in `docs/architecture.md` would require).
- This is the single highest-value unknown for the MVP: invariant I4
  ("phones are interchangeable") presumes app/session state can be
  re-loaded on any free phone. If GenFarmer Backup doesn't move between
  phones, we need an alternative (re-login, ADB run-as, native backup).

### 4.5 ADB / TCP — assumption

- GenFarmer almost certainly speaks ADB to the phone (USB and/or TCP). The
  device-identifier shape (`emulator-5554`) and the existence of an "ADB
  Command" button make this *likely*.
- Whether GenFarmer reuses the system `adb` server on port 5037 or spawns
  its own is *unknown*. This matters: if it grabs the only `adb` server,
  parallel ADB-TCP reconnects from our own worker (FFF research topic 7)
  could fight it.

### 4.6 Proxy / GenRouter integration — confirmed (no GenFarmer API)

- GenFarmer has no documented endpoint for assigning a proxy to a device.
- The vendor positions GenRouter (separate product) as the proxy plane:
  GenRouter's own admin UI at `http://192.168.5.1:9000/` lets an operator
  enter a `socks5://user:pass@ip:port` per connected device and click
  Update — *confirmed* from GenRouter's docs.
- No documented programmatic API on GenRouter for "assign proxy X to
  serial Y" — *confirmed by absence*. We have only the web UI.
- This means proxy assignment is currently a **manual or UI-scrape**
  problem from our side, not an API call. See next steps.

### 4.7 MockGPS — confirmed (no GenFarmer API)

- Not documented as a GenFarmer-controlled feature in the public API or
  product pages.
- *Unknown:* whether GenFarmer ships a built-in MockGPS or relies on a
  third-party Android mock-location app (`com.devspark.mockgps`,
  `com.lexa.fakegps`, etc.) configured via the phone's developer options.
- For MVP, treat MockGPS as a **separate device-side concern** (set via
  ADB intent / settings, possibly an Appium-driven mock-location app),
  not as something GenFarmer hands us for free.

## 5. JSON-RPC / mobile control path — unknown, likely absent

The issue brief listed this as a separate surface. The public docs:

- Describe REST only — *confirmed*.
- Make no mention of JSON-RPC, gRPC, or websocket — *confirmed by absence*.
- Don't expose a websocket for the screen stream in the API docs (the
  stream is consumed inside the desktop UI only).

It is *likely* that internally GenFarmer uses some streaming protocol to
ship frames from device → app, but that protocol is **not a public control
surface** and we should not plan to call it.

If we need a JSON-RPC / `uiautomator2`-style channel on the phone, the
right tool is **`uiautomator2`** (`openatx`) or **Appium**, not GenFarmer.
GenFarmer remains the device-management plane (provision, fingerprint,
backup), not the per-tap automation plane.

## 6. How this maps onto the MVP plan

Cross-references to `docs/architecture.md` invariants and to the research
topics listed in `CLAUDE.md`.

| MVP need | Best GenFarmer hook | Confidence | Gap |
|---|---|---|---|
| Build a no-code "post a Reel" flow inside GenFarmer | Automation App → Task → Run via REST | likely | Reels Trial-specific nodes unknown; we may end up driving the flow via Appium instead, and use GenFarmer only as device manager. |
| Assign job to a specific phone | `PUT /automation/tasks/:id/add-devices` with `{ id, serialNo, name }` | confirmed | We need to learn the `userId` and confirm device-list source. |
| Trigger and poll a run | `POST /automation/runs` + `PUT .../runs/:id/run` + `GET .../runs/:id/storages` | likely | Polling interval, terminal statuses, error shape all *unknown*. |
| Per-account device fingerprint (invariant I3) | Change Device (UI-only) | likely / mechanism unknown | No API; would require UI scripting or a separate fingerprint plane. **Blocker for "interchangeable phones".** |
| App/session state moves between phones (invariant I4) | Backup / Restore (UI-only) | unknown | If backups don't move between phones we need an alternative path (re-login, ADB `run-as`). **Highest-value unknown.** |
| Per-device proxy (Environment Loader) | GenRouter admin UI | confirmed UI-only | No documented API; needs separate decision (UI automation vs. a different proxy plane). |
| MockGPS | not in GenFarmer | confirmed-no | Handle on the device side directly. |
| ADB-TCP reconnect through Tailscale | GenFarmer almost certainly uses ADB | assumption | Risk of `adb server` contention with our own workers; verify in PoC. |

## 7. Safe next steps

All next steps are read-only / sandboxed and avoid invariants I7
("`fffbt` schema read-only") and the safety rules in `CLAUDE.md`. None of
them write production data, install on real accounts' phones, or commit
secrets. Each yields an artifact we can attach to a follow-up issue.

1. **Pull the Postman collection** linked from the GenFarmer API GitBook
   page. Store under `scripts/research/genfarmer/postman/` (gitignored if
   it contains any per-user IDs). Cost: minutes. Value: ground truth for
   payload shapes including the undocumented `remove-devices`.

2. **Stand up a throwaway GenFarmer install on a dev workstation** with
   one disposable test phone. Verify:
   - whether the REST server actually accepts unauthenticated calls from
     localhost,
   - what `GET /backend/auth/me` returns and how `userId` is obtained,
   - whether the server binds beyond `127.0.0.1` (default and configurable),
   - what `GET /automation/runs/:id/storages` looks like after a real run
     (success and failure).
   Capture all responses verbatim into `docs/research/genfarmer-api-samples.md`.
   No production accounts, no real Instagram login.

3. **Capture an HAR of the desktop app during one full run** (browser
   devtools won't see it; use `mitmproxy` configured as the system proxy
   for the GenFarmer process, *or* tcpdump on loopback). Goal: enumerate
   any endpoints used by the UI that aren't on the API page (very likely
   includes a device-list endpoint, screenshot, ADB shell).

4. **Inspect the GenFarmer install on disk** (Electron vs. native,
   `app.asar` if Electron). Read-only. If Electron, the
   `package.json` + main entry will name the framework and likely the
   IPC channels. This converts §3 from "unknown" to either "confirmed" or
   "confirmed-not-Electron" with one afternoon of work.

5. **For Change Device**: run it once on a dev phone with `adb shell`
   logging open and compare Build.* props, Settings.Secure.ANDROID_ID,
   and `getprop` before/after. This tells us which fingerprint surfaces
   actually move, without committing to any production strategy.

6. **For Backup**: back up a benign sandbox app (e.g. a calculator with a
   counter), restore it on a *different* physical phone, and confirm
   whether state crosses. If yes, invariant I4 is supported by GenFarmer
   directly; if no, raise a separate research issue for session-state
   migration alternatives.

7. **GenRouter**: confirm whether the `192.168.5.1:9000/` admin page has
   any JSON endpoints (look at network calls from its own UI). If yes,
   that becomes the proxy plane; if no, the Environment Loader either
   automates the UI or replaces GenRouter with a different proxy layer.

8. **Open a vendor support ticket** asking, explicitly: (a) is there an
   API or CLI for Change Device and Backup, (b) is there a stable way to
   bind the REST server to a non-loopback address, (c) is there a webhook
   for Run completion, (d) is the Postman collection authoritative or a
   subset. Cost: a message; high value if they answer.

## 8. Open questions to raise as separate issues

Each of these is too large to resolve inside FFF-18 and should be its own
ticket once we have the PoC data from §7:

- **OQ-1.** What is the canonical way to provision a per-account device
  fingerprint that persists across phones? (Architecture open question 3.)
- **OQ-2.** How does Instagram session state migrate between phones, and
  is GenFarmer Backup the right tool? (Architecture open question 4.)
- **OQ-3.** Is there an authenticated remote-control path for GenFarmer
  over Tailscale, or do we keep one operator workstation per farm?
- **OQ-4.** Do we drive Reels Trial posting through a GenFarmer Automation
  App, or through Appium with GenFarmer only as the device manager? (This
  affects what we owe the Poster component vs. what GenFarmer owes us.)

## 9. Sources

Vendor documentation only. No code was executed.

- GenFarmer API page (GitBook):
  <https://genfarmer-support.gitbook.io/genfarmer-eng/main-menu-bar/api>
- GenFarmer home (GitBook):
  <https://genfarmer-support.gitbook.io/genfarmer-eng>
- GenFarmer Automation (My Apps / My Modules / Runs / Saved Tasks):
  <https://genfarmer-support.gitbook.io/genfarmer-eng/main-menu-bar/automation>
- GenFarmer Control Center (Stream Device / Inspector / ADB Command):
  <https://genfarmer-support.gitbook.io/genfarmer-eng/main-menu-bar/control-center>
- GenFarmer Change / Backup:
  <https://genfarmer-support.gitbook.io/genfarmer-eng/main-menu-bar/change-backup>
- GenFarmer Devices page:
  <https://genfarmer-support.gitbook.io/genfarmer-eng/main-menu-bar/devices>
- GenRouter usage guide (Vietnamese, summarized):
  <https://fast-router-proxy.gitbook.io/fast-router-api-document/genrouter/huong-dan-su-dung-gen-router>
- GenRouter product page:
  <https://genrouter.com/>
- Gen ecosystem overview (marketing, low technical content):
  <https://genrouter.com/blogs/news/the-gen-ecosystem-genlogin-genfarmer-genrouter>
