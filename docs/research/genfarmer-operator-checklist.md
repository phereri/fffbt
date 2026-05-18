# GenFarmer — manual operator checklist

- Status: research checklist (FFF-18 follow-up)
- Owner: Research Agent (prepared for human operator)
- Last updated: 2026-05-17
- Companion to: [`docs/research/genfarmer-api.md`](./genfarmer-api.md)
- Sibling checklist: [`docs/research/genrouter-operator-checklist.md`](./genrouter-operator-checklist.md)

## Why this document exists

The GenFarmer research note (`genfarmer-api.md`) is built from vendor docs only. Several invariant-critical questions — does Change Device actually move the fingerprint? does Backup/Restore cross phones? can the REST server bind off-loopback? — cannot be answered without touching a real install. This checklist tells one human operator exactly what to run, what to capture, and what to send back, so the agent can convert §2-§4 of the research note from *likely / unknown* to *confirmed*.

## Safety rules for this run

- **Read-only first.** Do not run any state-changing step in §4 or §5 until the read-only checks in §1-§3 are done and reviewed.
- **Sandbox / test phone only** for anything that mutates device state. Never run Change Device, Backup, or Restore against a phone that holds a production Instagram session.
- **No production Instagram accounts.** If you must log into an Android app to test Backup/Restore, use a throwaway calculator or notes app (§5), not Instagram, Facebook, Telegram, or any account you care about.
- **No destructive actions.** Do not wipe, factory-reset, re-flash, or root any phone. Do not uninstall GenFarmer. Do not click "Apply for Router" or anything that changes proxy state on a production device.
- **No credentials in the issue.** Redact tokens, cookies, session IDs, real proxy passwords, GenFarmer login passwords, and real ADB device serials *before* pasting anything into a comment. See §3.4 for the redaction table.
- **Restore-on-exit.** If you change anything (a setting, a proxy, a test app's state), write down the original value first and put it back at the end of the session.
- **Stop on surprise.** If a button you do not recognize asks for confirmation, stop and report instead of clicking through.

## Prerequisites

Before starting, have the following ready. If anything is missing, stop and report — do not improvise.

- Access to the GenFarmer desktop application (the VPS / workstation it runs on).
- One **test Android phone** that is:
  - Powered on and visible in GenFarmer's device list.
  - **Not** carrying any production Instagram session or any account you care about.
  - Reachable via USB or ADB-over-TCP from the GenFarmer host.
- ADB installed locally (or accessible inside the GenFarmer host).
- A scratch directory on your machine — e.g. `~/ff-research-2026-05-17/` — to dump command output and screenshots into. Do not commit this directory.
- The Result Template in §7 open in a text editor, ready to fill in.

---

## 1. GenFarmer install directory inspection (read-only)

Goal: confirm what framework GenFarmer is built on and where its code/data live, without executing any of it.

### 1.1 Locate the install directory

Expected paths (vendor does not publish these — confirm yours, do not assume):

| OS | Likely install root | Likely user-data root |
|---|---|---|
| Windows | `C:\Program Files\GenFarmer\` or `%LOCALAPPDATA%\Programs\GenFarmer\` | `%APPDATA%\GenFarmer\` or `%LOCALAPPDATA%\GenFarmer\` |
| Linux | `/opt/GenFarmer/` or `/usr/lib/genfarmer/` | `~/.config/GenFarmer/` |
| macOS | `/Applications/GenFarmer.app/` | `~/Library/Application Support/GenFarmer/` |

To find the real one (Windows, in PowerShell):

```powershell
Get-Process | Where-Object { $_.ProcessName -like "*Gen*" } | Select-Object ProcessName, Path
```

On Linux:

```bash
pgrep -af -i genfarmer
ps -ef | grep -i genfarmer | grep -v grep
```

Record the **process binary path** and the **current working directory** of the running GenFarmer process. Send both back.

### 1.2 Confirm whether GenFarmer is Electron

Look for these signals inside the install directory. Any one is enough:

- A file named `resources/app.asar` (or `app.asar.unpacked/`).
- A file named `chrome_100_percent.pak`, `icudtl.dat`, `snapshot_blob.bin`, `v8_context_snapshot.bin`.
- A directory `resources/` next to the main binary, with `electron.asar` or `node_modules` inside.
- A binary named `GenFarmer.exe` whose `Get-Item .\GenFarmer.exe | Select-Object VersionInfo` mentions `Electron`.

What to send back (text, no binaries):

- Output of `dir /S /B` (Windows) or `ls -la` of the install root, **first two levels only**:
  - Windows: `cmd /c "dir /B /AD"` in the install root, plus `dir /B *.asar *.pak *.dat *.bin 2>nul`.
  - Linux: `ls -la /opt/GenFarmer/ | head -50 ; find /opt/GenFarmer/ -maxdepth 2 -name "*.asar" -o -name "*.pak" -o -name "icudtl.dat"`.
- Whether `app.asar` exists, and its size (in bytes).
- Whether a `package.json` is visible anywhere in the tree (do **not** open `app.asar` yet — that is §1.4 and we may decide it isn't necessary).

If none of the Electron signals are present, also note: is there a `Qt*.dll`, `libQt*.so`, or a `.NET` runtime nearby? That tells us what framework it actually is.

### 1.3 Locate user-data, logs, and config (read-only)

These directories typically hold the local DB GenFarmer keeps for Apps / Tasks / Runs / Devices. We need to know they exist and roughly what's in them. **Do not open or copy any file that might contain credentials.**

- Windows: list `%APPDATA%\GenFarmer\` and `%LOCALAPPDATA%\GenFarmer\` (one level).
- Linux: list `~/.config/GenFarmer/` and `~/.local/share/GenFarmer/` (one level).
- macOS: list `~/Library/Application Support/GenFarmer/` and `~/Library/Logs/GenFarmer/`.

What to send back:

- Directory listing (top level only) with file sizes.
- Names of subdirectories (e.g. `IndexedDB`, `Local Storage`, `Cache`, `Logs`, `db`).
- Names of any `.sqlite` / `.db` / `.json` config files you can see. **Do not** send the contents — only the names.

### 1.4 Optional, only if §2 fails and we ask for it

Do **not** do this until the agent explicitly asks. Listed here only so you know what it would entail.

- If GenFarmer is Electron, the renderer/main bundles live inside `resources/app.asar`. Extracting that file shows the API server's source — but it's a redistribution of vendor code, so we treat it as a last resort. If we get to that point we'll add a separate step.

### 1.5 What to send back from §1

Fill in the Result Template (§7) sections **GenFarmer version**, **OS**, **install path**, **user-data path**, **framework signal**. Attach the directory listings as a single text file `install-tree.txt`.

---

## 2. Public / local REST API behavior (read-only)

Goal: confirm the REST API in `genfarmer-api.md` §2 actually exists on **your** install, find out how `userId` is obtained, and find out whether the server accepts unauthenticated calls.

These commands are all **read-only** (`GET` only). They will not create, modify, or delete anything.

### 2.1 Confirm the server is up on loopback

On the GenFarmer host:

```bash
# Linux / macOS
curl -sS -o /dev/null -w "HTTP %{http_code} from %{url_effective}\n" http://127.0.0.1:55554/
```

```powershell
# Windows PowerShell
Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:55554/ | Select-Object StatusCode, Headers
```

If it returns a 4xx / 5xx / connection refused, **stop §2 and report**. The server may run on a different port on your build, in which case we need the real one before doing anything else.

To find the real port if 55554 is wrong:

```bash
# Linux: list listening sockets owned by the GenFarmer process
sudo ss -ltnp | grep -i genfarmer
sudo lsof -iTCP -sTCP:LISTEN -P | grep -i genfarmer
```

```powershell
# Windows
Get-NetTCPConnection -State Listen | Where-Object { $_.OwningProcess -in (Get-Process *Gen*).Id } |
  Select-Object LocalAddress, LocalPort, OwningProcess
```

Record the **real port** and the **bind address** (e.g. `127.0.0.1:55554` vs `0.0.0.0:55554`). The bind address is critical — see §2.5.

### 2.2 Test `GET /backend/auth/me`

This tells us what `userId` to use in any later Task/Run payload, and whether the API requires a session.

```bash
curl -sS -i http://127.0.0.1:55554/backend/auth/me
```

What to capture:

- Full response status line and headers.
- Body — but **redact the `id` / `userId` value** before pasting, replace with `USER_ID`. Keep the field name and the value's *type* (integer vs UUID string) visible.
- Any `Set-Cookie` header — redact the cookie value, keep the cookie *name*.

### 2.3 Test `GET /automation/apps`

List Apps without authenticating (just curl, no cookies). This answers "does the API require an authenticated session, or is anything-on-loopback allowed?".

```bash
curl -sS -i "http://127.0.0.1:55554/automation/apps?page=1&pageSize=5"
```

What to capture:

- HTTP status code.
- If 200, the **first item only** of the array — but redact any `name` that looks like a real account/customer label and any `userId`. We just want to confirm the response shape.
- If 401/403, the body verbatim — that tells us the auth contract.

### 2.4 Test `GET /automation/runs`

Same idea, for Runs:

```bash
curl -sS -i "http://127.0.0.1:55554/automation/runs?page=1&pageSize=5"
```

If there is at least one Run, also try:

```bash
# Replace RUN_ID with the id from the previous response
curl -sS -i "http://127.0.0.1:55554/automation/runs/RUN_ID/storages"
```

What to capture:

- The shape of one Run object (keys only, redact values that look like account names).
- The shape of one storages response (keys only).
- Whether `status` is numeric or string, and which values you actually see.

### 2.5 Bind-address check

Can the API be reached from another host on the same LAN / Tailnet? **Do not** attempt to actually call it remotely yet — just look at where the socket is bound.

- From §2.1 you already have the bind address.
- If the bind is `127.0.0.1`, the server is loopback-only on this build. Do **not** try to change a config file or env var to move it — that's a vendor-support question (§6), not an operator one.
- If the bind is `0.0.0.0` or a LAN IP, write that down and **stop** — that's a security finding we need to triage together before testing it from outside.

### 2.6 What NOT to do in §2

- Do **not** call any `POST`, `PUT`, or `DELETE` endpoint in this section. Even creating a "test task" mutates the local DB.
- Do **not** call `/automation/tasks/:id/add-devices` against a real device, even read-only — there is no documented `GET` for that path and any wrong call might mutate state.
- Do **not** paste your real `userId` or any device serial into the issue. Replace with `USER_ID`, `DEVICE_SERIAL`, etc.

### 2.7 What to send back from §2

Fill the Result Template (§7) **API endpoints found** section. Attach raw responses as `api-responses.txt`, redacted per §3.4.

---

## 3. Capture desktop app network calls (read-only observation)

Goal: enumerate any endpoints the GenFarmer UI uses that **are not** on the public API page — most likely a device-list endpoint, the screenshot endpoint, the ADB-command endpoint, and whatever Change Device / Backup actually call.

Pick the **preferred** method (3.1) if it works on your build; only fall back to 3.2 / 3.3 if it doesn't.

### 3.1 Preferred — Electron DevTools (only if §1.2 confirmed Electron)

Many Electron apps accept a flag that opens DevTools on the renderer. Try, in order:

1. **Keyboard shortcut while GenFarmer window is focused:** `Ctrl+Shift+I` (Win/Linux) or `Cmd+Opt+I` (macOS). If a DevTools panel opens, go to step 4.
2. **F12.**
3. **Start with remote debugging.** Close GenFarmer first.
   - Windows: `"C:\Path\To\GenFarmer.exe" --remote-debugging-port=9222`
   - Linux: `/opt/GenFarmer/genfarmer --remote-debugging-port=9222`
   - Then open `http://127.0.0.1:9222/` in Chrome and pick the renderer target.
4. In DevTools → **Network** tab:
   - Tick **Preserve log** and **Disable cache**.
   - Filter to **Fetch/XHR**.
   - Perform each UI action below one at a time, and for each one **right-click the new row → Copy → Copy as cURL (bash)** into a scratch file:
     - Open the Devices page (look for the device-list request).
     - Click on one test phone row to open Stream Device (look for any handshake / "start stream" call).
     - Take a screenshot via the Screenshot button.
     - Click **Install APK** but then **cancel** the file picker — we only want to see if there's a pre-flight call.
     - Open Change Device on the test phone, but **do not click Apply** yet (that's §4). Just open the dialog — we want to see what call populates it.
     - Open Backup on a benign sandbox app, but **do not click the final Backup button**. Same reason.
5. Save the scratch file as `desktop-net-calls.txt`.

If DevTools simply will not open on your build, move on to 3.2.

### 3.2 Fallback — `mitmproxy` on loopback (Electron or non-Electron)

Use this only if 3.1 didn't work and you have a quiet workstation. It needs you to install a CA cert into the GenFarmer host's trust store, which is a real change — **do not do this on the production VPS**. Use a copy on a workstation.

1. Install `mitmproxy` (`brew install mitmproxy` / `pip install mitmproxy` / Windows installer).
2. Run `mitmweb --listen-host 127.0.0.1 --listen-port 8080`.
3. Install the mitmproxy CA cert into the host trust store (per OS docs). **Record that you did this** so we can roll it back at the end.
4. Configure GenFarmer to use `http://127.0.0.1:8080` as an HTTP proxy if possible. If GenFarmer doesn't expose proxy settings, set system-wide HTTP proxy to `127.0.0.1:8080`.
5. Repeat the same UI actions as 3.1 step 4. Export the flow list (`File → Save flows`) as `mitm-flows.dump`.
6. **Uninstall the mitm CA cert** when done. Note this in your handback.

### 3.3 Last resort — `tcpdump` / `netstat` / `lsof` on loopback

Use only if 3.1 and 3.2 both fail. This gives much less detail — it'll tell us *which* hosts/ports GenFarmer talks to, not the request bodies.

```bash
# Linux: who is GenFarmer connected to right now
sudo lsof -p $(pgrep -f -i genfarmer | head -1) -i -n -P 2>/dev/null

# Linux: 30-second packet capture on loopback while you click through the same UI actions
sudo timeout 30 tcpdump -i lo -A -s 0 'tcp port 55554 or tcp port 9222' -w /tmp/genfarmer-loop.pcap
```

```powershell
# Windows
Get-NetTCPConnection | Where-Object { $_.OwningProcess -in (Get-Process *Gen*).Id } |
  Select-Object LocalAddress, LocalPort, RemoteAddress, RemotePort, State
```

Send back the connection table and the `.pcap` (if small — otherwise just `tshark -r ... -T fields -e http.request.method -e http.request.uri | sort -u`).

### 3.4 Redaction table — apply to everything in §2 and §3 before sending

Open every captured curl / response / pcap-derived text in an editor and replace these **before pasting into the issue**:

| Field | Replace with |
|---|---|
| `Cookie: ...` header values | `Cookie: <REDACTED-SESSION>` |
| `Authorization: Bearer <token>` | `Authorization: Bearer <REDACTED-TOKEN>` |
| `Authorization: Basic <b64>` | `Authorization: Basic <REDACTED-BASIC>` |
| Any `X-CSRF-Token:` / `X-Auth-Token:` / `X-Api-Key:` | `<REDACTED>` (but keep the **header name**) |
| `userId` numeric/string values | `USER_ID` |
| ADB device serials (e.g. `00f65a5d`, `R5CW...`) | `DEVICE_SERIAL_1`, `DEVICE_SERIAL_2`, … |
| Device `name` if it identifies a real account (e.g. `acct_alice_2024`) | `DEVICE_NAME` |
| Real Android model names that map 1:1 to a single owned phone | `MODEL` |
| GenFarmer login email / username if it appears | `<REDACTED-EMAIL>` |
| Real proxy `user:pass@host:port` if any | `socks5://USER:PASS@PROXY_IP:PORT` |

Keep verbatim: HTTP method, full URL **path** and query keys (replace query *values* per the table), all header **names**, `Content-Type`, response status code, JSON body **keys** and value *shapes*.

### 3.5 What to send back from §3

`desktop-net-calls.txt` (curl exports, redacted) **or** `mitm-flows.dump` summary **or** the `lsof` / `tcpdump` summary, plus a one-line note saying which method was used.

---

## 4. Change Device verification (test phone only)

⚠️ **State-changing.** Do not run §4 until §1-§3 are returned. Run only on the test phone from "Prerequisites". Do **not** run on a phone holding any real account.

Goal: confirm whether GenFarmer's Change Device button actually moves the fingerprint surfaces we care about, and whether it does so per-app or per-device.

### 4.1 BEFORE snapshot (read-only via ADB)

Connect the test phone via ADB (USB or TCP — note which):

```bash
adb devices -l
# Confirm exactly ONE device is listed; if more, set ANDROID_SERIAL.
export ANDROID_SERIAL=<the test serial>
```

Take the BEFORE snapshot. Each command below is read-only:

```bash
adb -s "$ANDROID_SERIAL" shell getprop > before-getprop.txt
adb -s "$ANDROID_SERIAL" shell settings get secure android_id > before-android_id.txt
adb -s "$ANDROID_SERIAL" shell settings get global advertising_id 2>/dev/null > before-advertising_id.txt
adb -s "$ANDROID_SERIAL" shell cat /proc/sys/kernel/random/boot_id 2>/dev/null > before-boot_id.txt
adb -s "$ANDROID_SERIAL" shell pm list packages -3 > before-third-party-packages.txt
# A benign sandbox app's data dir mtime (we'll use this later for backup/restore §5)
adb -s "$ANDROID_SERIAL" shell pm dump com.android.calculator2 2>/dev/null | head -50 > before-calc-dump.txt
```

Verify each file is non-empty before continuing.

### 4.2 Pick the target sandbox app

Pick **one** Android package that:

- Is installed on the test phone.
- Is **not** Instagram, Facebook, Telegram, Twitter/X, or anything tied to a real account.
- Has no user data you care about. Good candidates: the stock Calculator (`com.android.calculator2`), the stock Notes app, or a freshly-installed sandbox app you don't mind losing.

Record the package name in the result template under **Change Device target app**.

### 4.3 Run Change Device — sandbox app only

In the GenFarmer UI:

1. Select the test phone.
2. Open Change Device.
3. Apply it to **only the sandbox app from §4.2**. Do not select Instagram or any other app from any account.
4. Pick the default "randomize" / "new device" option (whatever the UI calls it). Don't manually set values — we want to see what GenFarmer chooses on its own.
5. Click Apply. Wait for the UI to report success.

If at any point the dialog asks to also change MAC, WiFi, IMEI, or anything that says "permanent" or "device-wide", **stop and report**. We want the per-app-only path; the permanent-or-device-wide path is too risky for this PoC.

### 4.4 AFTER snapshot (same ADB commands)

```bash
adb -s "$ANDROID_SERIAL" shell getprop > after-getprop.txt
adb -s "$ANDROID_SERIAL" shell settings get secure android_id > after-android_id.txt
adb -s "$ANDROID_SERIAL" shell settings get global advertising_id 2>/dev/null > after-advertising_id.txt
adb -s "$ANDROID_SERIAL" shell cat /proc/sys/kernel/random/boot_id 2>/dev/null > after-boot_id.txt
adb -s "$ANDROID_SERIAL" shell pm list packages -3 > after-third-party-packages.txt
adb -s "$ANDROID_SERIAL" shell pm dump com.android.calculator2 2>/dev/null | head -50 > after-calc-dump.txt
```

### 4.5 Compare

```bash
diff before-getprop.txt after-getprop.txt > diff-getprop.txt
diff before-android_id.txt after-android_id.txt > diff-android_id.txt
diff before-advertising_id.txt after-advertising_id.txt > diff-advertising_id.txt
diff before-boot_id.txt after-boot_id.txt > diff-boot_id.txt
diff before-third-party-packages.txt after-third-party-packages.txt > diff-packages.txt
```

### 4.6 Decision rules

This is the evidence the agent will use to classify Change Device:

- **Per-device global change** — if `getprop` system-wide differs and `Settings.Secure.android_id` differs. This is unexpected and means Change Device touches the whole phone (not what the marketing implies).
- **Per-app sandboxed change** — if `getprop` is unchanged but the sandbox app's *seen* identifiers differ when queried from inside that app. The ADB-side snapshots above will look identical in this case — that's the expected outcome if Change Device is a per-app hook. We will need a separate test (Appium inside the sandbox app reading `Build.MODEL` etc.) to confirm — that's a follow-up, not this round.
- **No effective change** — both snapshots identical and the app also reports the same values. Tells us Change Device is mostly UI-cosmetic for this app type.

### 4.7 What to send back from §4

All six `before-*.txt`, all six `after-*.txt`, all `diff-*.txt`, plus the result template's **Change Device before/after results** filled in. Do not redact ADB serials *inside files you keep locally*, but when pasting **any of this into the issue**, redact serials per §3.4.

### 4.8 What NOT to do in §4

- Don't run Change Device on Instagram, Facebook, Telegram, or any package holding a real account.
- Don't pick "device-wide" / "permanent" / "factory reset" options if the dialog offers them.
- Don't reboot the phone between BEFORE and AFTER snapshots — that adds noise to `boot_id`.

---

## 5. Backup / Restore verification (sandbox app, test phone only)

⚠️ **State-changing.** Same rules as §4. Sandbox app only. Two test phones if you have them — see §5.5 for the cross-phone test.

Goal: confirm whether GenFarmer Backup actually persists app state, and — most importantly — whether that backup can be restored on a **different** physical phone. The cross-phone case is the one that decides invariant I4 ("phones are interchangeable").

### 5.1 Pick a sandbox app with observable state

Use the **same** sandbox package as §4.2 if possible (e.g. `com.android.calculator2`). The app needs *some* observable state that we can verify after restore:

- Notes app — type one note: `ff-research-2026-05-17 sandbox marker`.
- Calculator — open it, leave the display showing some specific number, then close (a lot of calculator apps persist the last display value).
- Freshly-installed sandbox app — any settings toggle inside it that survives a close.

Record what state you set, in the result template under **Backup invariant under test**.

### 5.2 Backup on phone A

1. In GenFarmer, select the test phone (call it phone A).
2. Open the Backup feature for the sandbox app from §5.1.
3. Use the default backup destination. Note where GenFarmer says the backup file is written — capture the path.
4. Wait for "complete".
5. Record:
   - Backup file path on the GenFarmer host.
   - Backup file size.
   - Whether the backup is one file or a directory (`ls -la <path>` / `dir`).

Do **not** open the backup file to inspect contents — we don't yet know if it has secrets we should not look at.

### 5.3 Same-phone restore (sanity check)

1. Inside the sandbox app, change its state to something *different* from §5.1 (e.g. type a second note `should-be-overwritten`, or clear the calculator).
2. Run Restore for the sandbox app from the backup file from §5.2.
3. Open the sandbox app. Did the state from §5.1 come back? Yes / no.

Record under **Backup/restore result → Same-phone restore**.

### 5.4 Negative case — restore against the wrong package

Skip this section unless §5.3 worked. The goal is to make sure Backup is actually package-scoped (and not "restore wipes the whole phone").

- Set a marker state in an **unrelated** sandbox app (e.g. Notes with `unrelated-marker`).
- Restore the sandbox-app backup again.
- Confirm the unrelated app's state is **unchanged**.

If the unrelated app's state changed, **stop the entire backup test** and report — that would mean Restore is broader than per-app, and we need to redesign before doing anything else.

### 5.5 Cross-phone restore (this is the real question)

Skip this section if you only have one test phone. Otherwise — and only if §5.3 and §5.4 both succeeded:

1. Take the backup file from §5.2.
2. Restore it onto a **second** test phone (phone B), targeting the same sandbox package.
3. Open the sandbox app on phone B. Is the §5.1 marker state present?
4. Record: same fingerprint? same app identity? did Android prompt about "different device" or "package mismatch"?

### 5.6 Decision rules

This is the evidence the agent will use to classify Backup/Restore:

- **Cross-phone restore works and state appears intact** → invariant I4 is plausibly supported by GenFarmer Backup directly. We can architect the session-migration story around it (with one more round of testing on an app that uses signed-in state).
- **Cross-phone restore "works" but app rejects state** (e.g. login expired, re-authentication required) → Backup is local-data only, not full session-portable. Treat I4 as unsupported by GenFarmer Backup; raise a separate research issue.
- **Cross-phone restore fails** → I4 unsupported by GenFarmer; needs alternative path (ADB `run-as`, re-login flow, vendor support question §6).
- **Same-phone restore fails** → bigger problem; report immediately, do not continue.

### 5.7 What to send back from §5

The result-template **Backup/restore result** block filled in, plus:

- Backup file path and size (paths only — do not send the file itself).
- Screenshots of the sandbox app's state at each step (§5.1 marker / §5.3 result / §5.5 result), with anything identifying redacted out.

### 5.8 What NOT to do in §5

- Do **not** test Backup against Instagram or any account-bearing app.
- Do **not** send the backup file itself to the issue — it likely contains app-private data.
- Do **not** delete a backup file from §5.2 until we've decided whether to keep it for further work.
- Do **not** restore a backup taken from a non-sandbox phone onto a sandbox phone "just to see what happens" — we're testing the controlled direction only.

---

## 6. Vendor support questions

Goal: get vendor-side ground truth on the six biggest unknowns from `genfarmer-api.md`. Send this as a single short email/ticket to GenFarmer support. Keep it concise; vendor support replies are more thorough when the question is sharp.

**Recommended subject line:** `GenFarmer API: programmatic access to Change Device / Backup / off-loopback bind / webhooks`

**Recommended body (copy-paste, fill in version and OS):**

```text
Hi GenFarmer team,

We are integrating GenFarmer with our own orchestration layer and have a
few questions that aren't covered in the public API docs. Could you
confirm any of the following?

GenFarmer version: <fill in>
OS: <Windows / Linux / macOS, fill in>

1. Change Device — Is there any API, CLI, or scriptable way to trigger
   Change Device on a selected app + device, or is it strictly UI-driven?
   If API: what is the endpoint or command?

2. Backup / Restore — Is there an API or CLI for triggering Backup and
   Restore? Does Restore support restoring a backup taken on phone A
   onto a different phone B (same model / same Android version)? If so,
   are there constraints we should know about (e.g. same vendor build,
   same Android version, root required)?

3. Off-loopback bind — Can the local REST server on 127.0.0.1:55554 be
   bound to a non-loopback address (e.g. 0.0.0.0 or a LAN IP) via
   config, CLI flag, or environment variable? We would like to drive
   GenFarmer from a separate orchestration host over a private network.
   If "no" today, is it on the roadmap?

4. Run webhooks — Does GenFarmer support a webhook (or any push
   notification) when an Automation Run completes or fails? If not, what
   is the recommended polling strategy and interval for
   GET /automation/runs/:id/storages?

5. Remote Automation API — Is the public REST API (/automation/apps,
   /automation/tasks, /automation/runs) intended to be callable
   remotely, or is it explicitly localhost-only by design?

6. Device profile programmatic apply — Is there a way to apply a
   previously-captured device profile (the values Change Device sets)
   to a target device programmatically, so we can pin a fingerprint per
   account across phones?

Bonus question (low priority): is the Postman collection linked from
the API docs the full surface, or a curated subset?

Thanks — happy to share more about our use case if helpful.
```

Do **not** include your real account names, real device serials, real customer information, or anything about your specific Instagram automation goals in this message. Generic "orchestration layer" is enough.

Send back: vendor's reply, verbatim, redacted only for anything they include about your own account (e.g. license keys).

---

## 7. Result template — copy-paste, then fill in

Copy this block, paste it into a fresh comment on FFF-18, fill in the blanks, and attach the files referenced in §1.5 / §2.7 / §3.5 / §4.7 / §5.7.

````text
## GenFarmer operator checklist — results

**GenFarmer version:** <fill in, e.g. 3.4.1>
**OS:** <Windows 11 23H2 / Ubuntu 22.04 / macOS 14.x — fill in>
**Install path:** <e.g. C:\Program Files\GenFarmer\>
**User-data path:** <e.g. %APPDATA%\GenFarmer\>
**Framework signal:** <Electron / not-Electron / unsure — and what made you decide>

### §1 — install inspection
- app.asar present? <yes / no>, size: <bytes if yes>
- Electron signals seen: <list e.g. icudtl.dat, snapshot_blob.bin, package.json>
- Other notable framework signs: <e.g. Qt5*.dll, .NET runtime>
- Attached file: install-tree.txt

### §2 — API endpoints found
- REST base URL actually serving on this install: <e.g. http://127.0.0.1:55554/>
- Bind address from §2.1: <127.0.0.1 / 0.0.0.0 / other>
- GET /backend/auth/me — status: <code>, userId type: <int / uuid / other>
- GET /automation/apps — status: <code>, requires auth? <yes / no / unsure>
- GET /automation/runs — status: <code>, run-status field type: <int / string>
- GET /automation/runs/:id/storages — status: <code>, body shape: <one-line summary>
- Attached file: api-responses.txt (redacted per §3.4)

### §3 — desktop network capture
- Method used: <DevTools / mitmproxy / lsof+tcpdump>
- Endpoints seen that are NOT in the public API page (path + method only):
  - <e.g. POST /internal/devices/list>
  - <…>
- Attached file: desktop-net-calls.txt (or mitm-flows.dump)

### §4 — Change Device before/after results
- Test phone serial: DEVICE_SERIAL_1 (real value redacted)
- Target sandbox app: <e.g. com.android.calculator2>
- getprop diff: <empty / non-empty, count of differing lines>
- android_id changed? <yes / no, old vs new redacted>
- advertising_id changed? <yes / no / not present>
- boot_id changed? <yes / no — should be "no" if no reboot>
- third-party packages list changed? <yes / no>
- Operator's read of the result: <per-device global / per-app sandboxed / no effective change / inconclusive>
- Attached files: before-*.txt, after-*.txt, diff-*.txt

### §5 — backup/restore result
- Backup invariant under test: <e.g. "calculator's last-display value persists">
- Backup file path: <local path on GenFarmer host>
- Backup file size: <bytes>
- Same-phone restore (§5.3): <state recovered / state not recovered / failed>
- Wrong-package negative case (§5.4): <unrelated app unchanged / unrelated app changed — STOP>
- Cross-phone restore (§5.5): <not attempted (only one phone) / recovered / app rejected state / failed>
- Android prompts about device mismatch on cross-phone restore: <none / quoted prompt>
- Operator's read of the result: <I4 plausible / I4 unsupported by GenFarmer / inconclusive>

### §6 — vendor support
- Ticket opened? <yes / no, date>
- Reply received? <yes / no, summary if yes — verbatim attached as vendor-reply.txt>

### Open questions / surprises during the run
- <free-form: anything that did not match this checklist, anything that asked for confirmation, anything that didn't work>
````

---

## 8. Out of scope for this run

These are intentionally **not** in the checklist and should not be attempted:

- VPS access changes, firewall changes, Tailscale routing changes.
- Any operation against the real production GenFarmer machine, router, or phones.
- Touching production Instagram sessions in any way.
- Wipe / factory-reset / re-flash on any phone.
- Mutating REST calls (`POST` / `PUT` / `DELETE`) — only the explicit GenFarmer UI actions in §4 and §5 mutate state, and only on the sandbox app on a test phone.
- Anything that would commit a backup file, an `app.asar`, or a vendor-redistributable artifact to git. Keep all of those local to your scratch directory.

If the operator finds themselves about to do any of the above to complete a step, **stop and report** instead.
