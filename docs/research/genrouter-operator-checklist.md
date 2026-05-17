# GenRouter — manual operator checklist

- Status: research checklist (FFF-22 follow-up)
- Owner: Research Agent (prepared for human operator)
- Last updated: 2026-05-17
- Companion to: [`docs/research/genrouter-api.md`](./genrouter-api.md)

## Why this document exists

Public GenRouter documentation describes only UI workflows. The GitBook "Ask AI" answerer returned two mutually inconsistent REST specs (different base IPs, different paths, MAC vs IP keying, different body shapes). Those answers are **not evidence** and must not be coded against.

The only safe way forward is to capture real traffic from a real router. This checklist tells one human operator exactly what to click, what to copy, and what to redact, so the agent can decide between three architectures (A/B/C) on evidence rather than guesses.

## Safety rules for this run

- **Do not** change anything you cannot revert in the same session.
- **Do not** paste real cookies, tokens, passwords, or live proxy credentials into the issue. Redact before sharing — see §3.
- **Do not** click any "Apply for Router" / "Save" button on tabs you did not intend to touch.
- **Do** pick exactly **one** test device row. If possible, pick a device that is currently idle and not running a job.
- **Do** record the device's original proxy value before changing anything, and restore it at the end (§2 step 6).
- If anything looks destructive (reboot, factory reset, firmware update prompts), stop and report rather than confirming.

---

## 1. Browser DevTools checklist

Goal: capture the exact HTTP request the SPA sends when you change one device's proxy.

1. **Open the GenRouter admin UI** in a fresh Chrome / Edge / Firefox window.
   - Expected URL: `http://192.168.5.1:9000/` (from vendor docs — **confirm** what your router actually serves; if it redirects to a different host:port, write down the real one).
   - Use a fresh window so the Network tab is not polluted by other tabs.
2. **Log in manually.** Type credentials by hand. Do not let the browser autofill a saved password into a shared screen recording.
3. **Open DevTools → Network tab** *before* doing anything else.
   - Shortcut: F12, then click **Network**.
   - Check **Preserve log** (so navigations don't clear the table).
   - Check **Disable cache** while DevTools is open.
4. **Filter to API traffic only.**
   - Click the **Fetch/XHR** filter button (Chrome) or type `xhr` in the filter box.
   - This hides image / CSS / JS noise and leaves only the calls we care about.
5. **Navigate to the device list / proxy assignment screen** (GenFarmer → Devices Manager → Setup Router, or the GenRouter admin equivalent).
   - The list-devices request will appear in Network — note its URL and method (this answers "how is the device table keyed").
6. **Pick ONE test device row** (see §2 for which one).
7. **Change its SOCKS5 proxy** to a known harmless test value (see §2 step 4).
8. **Click Update** (or whatever the per-row commit button is called).
9. **Click "Apply for Router"** *only if* it appears after Update — note whether it was required for the change to take effect.
10. **In the Network tab**, locate the request triggered by step 8.
    - It will usually be highlighted briefly. Sort by **Time** column descending if you lose it.
11. **Right-click the request → Copy → Copy as cURL (bash)**. Save it to a scratch text file.
12. **Repeat steps 7–11 a second time** with the *same* device and *same* proxy value, to confirm the endpoint is stable across one refresh (see §4).

### Redact before sharing

Open the copied curl in a text editor and replace these with placeholders **before** pasting it into the issue:

| Field | Replace with |
|---|---|
| `Cookie: ...` header values | `Cookie: <REDACTED-SESSION>` |
| `Authorization: Bearer <token>` | `Authorization: Bearer <REDACTED-TOKEN>` |
| `Authorization: Basic <b64>` | `Authorization: Basic <REDACTED-BASIC>` |
| Any `X-CSRF-Token: ...` / `X-Auth-Token: ...` | `<REDACTED>` (but keep the **header name** — we need to know it existed) |
| SOCKS5 username `user` in `socks5://user:pass@ip:port` | `USER` |
| SOCKS5 password `pass` | `PASS` |
| SOCKS5 upstream `ip:port` if it identifies a real paid proxy | `PROXY_IP:PORT` |
| Router admin login password if it appears anywhere | `<REDACTED>` |

### Keep these fields verbatim (do NOT redact)

- HTTP method (`POST`, `PUT`, `PATCH`, …)
- Full URL path and query string (e.g. `/api/devices/update_proxy?id=…`) — this is the core finding
- All **header names** (even when the value is redacted)
- `Content-Type` header value
- Request body **structure** — keys and value *shapes*, but replace any device MAC / IP / hostname with `DEVICE_MAC` / `DEVICE_IP` / `DEVICE_HOSTNAME` placeholders so we can see which one is the key
- Response status code

---

## 2. UI actions to perform

### Step 1 — pick a test device row

Choose a device that is:

- Powered on and visible in the device list.
- **Not** currently running a job (or, if all phones are busy, the least important one).
- Easy for you to physically identify if you need to check it later.

### Step 2 — record visible fields BEFORE any change

For the chosen row, write down (or screenshot — see §3) every field the UI shows for it:

- IP address
- MAC address
- Hostname / device name / friendly label
- SSID it is connected to
- Current SOCKS5 proxy string (full URL — keep this in your private notes; do **not** paste the live password into the issue)
- Any other column the UI shows (signal, online/offline, model, …)

This is the "BEFORE" snapshot. We need it to restore in step 6 and to compare in §4.

### Step 3 — choose a harmless test proxy

Two acceptable forms:

- **Preferred:** a SOCKS5 you control, e.g. a localhost stub or a spare test proxy from your pool. Use a unique username so you can grep router logs for it later.
- **Acceptable:** an obviously fake value like `socks5://test:test@192.0.2.1:1080` (RFC 5737 documentation address, never routable). The router should accept the form even though traffic will fail — that is fine; we only care about the API call.

**Do not** use a live customer/account proxy as the test value.

### Step 4 — change the proxy on that one row

- Paste the test SOCKS5 into the proxy field on the chosen row.
- Click **Update**.
- If an "Apply for Router" / global save step appears, note it (and click it only if needed to make the change persist).

### Step 5 — observe success / failure

- Note the on-screen result: success toast? error toast? silent? row turned green?
- Note the response in DevTools (status code + body).
- If the router shows a per-device test/ping button, click it and note whether it reports proxy reachable.

### Step 6 — restore the previous proxy

- Paste the original SOCKS5 value (from §2 step 2) back into the row.
- Click **Update** (and **Apply for Router** if it appeared in step 4).
- Confirm the row shows the original value again.

### Step 7 — log out of the admin UI

Close the tab. Clear the curl scratch file once you have transferred the redacted version into the issue.

---

## 3. Data to send back

Post a single comment on issue **FFF-22** containing:

1. **Redacted curl** for the Update call (from §1 step 11, redacted per §1). Use a fenced code block.
2. **Request URL** (path + query string, after redaction).
3. **HTTP method**.
4. **Request headers** as a list, with auth values redacted but header names intact.
5. **Request body shape** — for example:
   ```json
   { "id": "DEVICE_MAC_OR_IP", "proxy": "socks5://USER:PASS@PROXY_IP:PORT" }
   ```
   Tell us **which field is the device key** and what kind of value it holds (looks like MAC? IP? UUID? row index?).
6. **Response body** — copy the JSON / text the server returned, redact if it echoes credentials.
7. **Two screenshots** of the device row, **before** (§2 step 2) and **after** (§2 step 5). In both:
   - Hide / blur the proxy password column.
   - Hide / blur any other customer-identifying field.
   - PNG is fine. Drag-drop attaches them to the issue.
8. **Router firmware / version** if visible in the admin UI (often under Settings → About / System → Info). Copy the exact string.
9. **Admin UI URL and port** as you actually used them (not the doc default), e.g. `http://192.168.5.1:9000/`.
10. **Auth scheme** — one of:
    - `cookie/session` (the only auth header is `Cookie: …`)
    - `basic` (`Authorization: Basic <b64>`)
    - `bearer` (`Authorization: Bearer <token>`)
    - `custom header` (e.g. `X-Auth-Token: …`) — name the header
    - `none` (no auth header on API calls)

A copy-paste template for all of the above is in §7.

---

## 4. API stability checks

After completing §2 once, do this short second pass so we know the endpoint is stable, not a session-scoped hash.

1. **Hard-refresh the admin UI** (Ctrl+Shift+R / Cmd+Shift+R). Log in again if it boots you out.
2. **Repeat §2 steps 4 and 5** on the same device (change → Update → restore).
3. Compare the second Update call to the first and answer these in your reply:

| Question | Answer (yes / no / value) |
|---|---|
| Does the endpoint URL stay the same across the two attempts? | |
| Does the row identifier in the request body stay the same? | |
| Is the device identified by MAC, IP, hostname, or an opaque internal ID? | |
| Does the proxy attach to the **device row** or to the **SSID record**? (Check the WiFi Manager tab — does adding an SSID also expose a proxy field?) | |
| Does the response carry a numeric / hashed row ID we'd need to fetch separately before each write? | |
| Is there a **bulk** endpoint? (Watch the Network tab when the UI loads — is there a single GET that returns the entire device table? Is there a Save-all button that POSTs many rows at once?) | |
| Does an explicit "Apply for Router" / commit step exist, separate from per-row Update? | |
| Did the change survive a router-side refresh, or only a UI refresh? (If you have console access, skip — do not reboot the router for this test.) | |

If any answer is "no" or "changes between attempts", the endpoint is **not stable enough to code against** and we should treat Option A (per-job UI/API rewrite) as blocked.

---

## 5. Architecture decision matrix

These are the same three options from [`genrouter-api.md`](./genrouter-api.md) §4, restated here so the operator can fill them in after testing. Update the rating column based on what §1–§4 actually showed.

Rating scale: ✅ good · ⚠️ caveat · ❌ blocker · ❓ still unknown after testing.

### Option A — per-job UI/API rewrite of the device row

At job start, call GenRouter (HTTP or scripted UI) to set the proxy on the row matching the phone that just got the job. Restore at job end.

| Criterion | Pre-test expectation | After-test rating | Notes |
|---|---|---|---|
| Compatible with "account owns proxy" (I3) | ✅ — proxy follows account because we re-bind per job | | Fill after §1–§4 |
| Compatible with "phones interchangeable" (I4) | ✅ — re-binding is per-row, so any free phone works | | |
| Speed (worst-case latency added to job start) | ⚠️ — 1 HTTP call expected, but unknown if commit step needed | | Measure response time in DevTools |
| Reliability | ❓ — depends on whether endpoint is stable across firmware | | Use §4 results |
| Risk | ⚠️ — undocumented endpoint can break on firmware bump | | Did §4 confirm stability across refresh? |
| Implementation complexity | Low if real API; Medium if we must script the SPA via headless browser | | |

### Option B — persistent SSID per account

Pre-create one SSID per account (cap ~32 per router). Each SSID carries that account's SOCKS5 as a fixed property. At job start, make the chosen phone connect to that SSID via ADB.

| Criterion | Pre-test expectation | After-test rating | Notes |
|---|---|---|---|
| Compatible with "account owns proxy" (I3) | ✅ — proxy is bound to SSID forever | | |
| Compatible with "phones interchangeable" (I4) | ⚠️ — phone must be able to switch SSIDs reliably from ADB | | |
| Speed | ⚠️ — SSID switch + DHCP can take several seconds | | |
| Reliability | ✅ — no per-job admin call; SSID config is set once | | |
| Risk | Blocker if SSID record does **not** expose a proxy field | | Confirmed in §4 row "device or SSID?" |
| Implementation complexity | Medium — capped at ~32 accounts per router; need pre-provisioning flow | | |

### Option C — skip GenRouter, configure proxy on Android side

Set SOCKS5 on the device itself (per-app via Appium proxy capability, or system-wide via root + `iptables`).

| Criterion | Pre-test expectation | After-test rating | Notes |
|---|---|---|---|
| Compatible with "account owns proxy" (I3) | ✅ — proxy travels with the account profile loaded on the phone | | |
| Compatible with "phones interchangeable" (I4) | ✅ — every phone applies the proxy at job-start regardless | | |
| Speed | ✅ — local change, no network round-trip | | |
| Reliability | ⚠️ — Android Wi-Fi proxy field is HTTP-only; SOCKS5 needs root or per-app helper | | |
| Risk | Loses GenRouter's "no software on device" detectability win | | |
| Implementation complexity | Medium-High — root requirement, or per-app proxy wrapper for the Instagram app | | |

### After-test verdict

Fill after running §1–§4:

- Recommended option (A / B / C): ____
- Why this option won, in one sentence: ____
- What still needs to be checked before committing code: ____

---

## 6. Vendor support message

Send this to GenRouter support (vendor pages list `info@genrouter.com` and a contact form). Adjust greeting only.

```
Subject: GenRouter — programmatic proxy assignment, API stability questions

Hello,

We are integrating GenRouter into an internal automation that assigns one
SOCKS5 proxy per device at job start. Before we commit to an implementation,
we would like authoritative answers to the following:

1. Does GenRouter expose an official local REST (or other) API for
   proxy assignment? If yes, where is its documentation, and what is
   the stability guarantee across firmware versions?

2. How can we programmatically assign a SOCKS5 proxy to a specific
   device? Please share the exact endpoint, request method, body
   shape, auth scheme, and an example.

3. In the device table, what is the canonical device key — MAC address,
   DHCP-assigned IP, hostname, or an internal/opaque ID? Which of these
   is safe to persist on our side as the long-term identifier of a
   physical phone?

4. Can a SOCKS5 proxy be assigned per SSID (so any device joining that
   SSID inherits the proxy), in addition to per-device assignment?
   If yes, where in the API/UI is this configured?

5. Is there an export/import endpoint for the device-to-proxy mapping,
   so we can back up and restore the full table programmatically?

6. Is there a webhook, event stream, or log API we can subscribe to,
   to be notified when a device goes online/offline, when a proxy
   assignment changes, or when an upstream proxy fails health checks?

For context: current firmware on our unit is <FILL IN AFTER §3 ITEM 8>,
admin UI at <FILL IN AFTER §3 ITEM 9>. Happy to share more on a call.

Thank you,
<Your name>
```

---

## 7. Result template — copy-paste into the issue after testing

Open the issue (FFF-22) and paste this filled-in block as a single comment. Attach the two screenshots after posting.

```
## GenRouter operator-checklist result (FFF-22)

Tester: <name>
Date / time: <YYYY-MM-DD HH:MM TZ>
Router firmware / version: <string from admin UI About page, or "not shown">
Admin UI URL: <e.g. http://192.168.5.1:9000/>
Login required: <yes / no>
Auth scheme: <cookie/session | basic | bearer | custom-header NAME | none>

### Device row used for test (BEFORE)
- IP:           <e.g. 192.168.5.42>
- MAC:          <e.g. AA:BB:CC:DD:EE:FF>
- Hostname:     <e.g. Pixel-3a-test>
- SSID:         <e.g. GenRouter-WiFi-2G>
- Current proxy: <REDACTED-CREDS - kept locally>

### Test proxy used
<e.g. socks5://test:test@192.0.2.1:1080  (RFC 5737, harmless)>

### Update — first attempt
- Method:           <POST | PUT | PATCH | ...>
- URL (path+query): <e.g. /api/devices/update_proxy>
- Headers (names only where redacted):
    Content-Type: <value>
    Cookie: <REDACTED-SESSION>
    X-CSRF-Token: <REDACTED>  # if present
    <other headers>
- Body shape:
    <paste JSON with DEVICE_MAC / DEVICE_IP placeholders and REDACTED creds>
- Response status: <e.g. 200>
- Response body:
    <paste, redacted>
- UI feedback:    <success toast / error / silent>
- "Apply for Router" step required?  <yes / no>

### Redacted curl (verbatim, redacted)
```
<paste here>
```

### Update — second attempt (after hard-refresh)
- Same endpoint URL?      <yes / no — if no, paste the new URL>
- Same row identifier?    <yes / no — if no, paste both>
- Identifier appears to be: <MAC | IP | hostname | opaque ID>

### Stability matrix (from §4)
- Endpoint stable across refresh:        <yes / no>
- Row identifier stable across refresh:  <yes / no>
- Device key type:                       <MAC | IP | hostname | opaque ID>
- Proxy attaches to:                     <device row | SSID | both>
- Bulk endpoint exists:                  <yes / no — paste URL if yes>
- Separate "commit" step needed:         <yes / no>

### Restore step
Original proxy restored at <HH:MM TZ>. Device row reverted: <yes / no>.

### Recommendation (from §5 matrix)
- Option chosen: <A | B | C>
- One-line reason: <...>
- Blockers still open: <list, or "none">

### Screenshots
[attached: device-row-before.png — proxy password blurred]
[attached: device-row-after.png  — proxy password blurred]
```

---

## What happens after you post the result

The Research Agent will:

1. Replace `genrouter-api.md` §3.2 ("Ask AI answers — do not trust") with the verified endpoint, or mark Option A blocked and pivot to B/C.
2. Update the Option A/B/C ratings in §5 above from the After-test column.
3. Add a closing recommendation to the issue with a concrete next ticket (e.g. "implement GenRouter HTTP client" or "implement per-account SSID provisioning").

No production code will be written from this checklist alone — its only output is evidence.
