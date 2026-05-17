# GenRouter — proxy assignment research

- Status: research notes (FFF-22)
- Owner: Research Agent
- Last updated: 2026-05-17 (revision 3 — `/api/devices` confirmed against real hardware; cleanup-on-job-end dropped from scope)
- Scope: how to assign a SOCKS5 proxy to a specific physical Android device, programmatically, using GenRouter

This document covers what is publicly documented by the vendor (GenFarmer / GenRouter / fast-router-proxy), separates observations from conclusions, and lists what still needs to be verified on real hardware before we commit to a design.

Confidence labels used below: **confirmed** (quoted from vendor docs we read), **likely** (consistent across multiple vendor pages but not directly quoted), **assumption** (our inference), **unknown** (we could not find authoritative information).

> **TL;DR (2026-05-17 revision 3):** the vendor publishes a REST API at [`/genrouter/how-to-use/integrations`](https://fast-router-proxy.gitbook.io/genrouter/how-to-use/integrations) — see §3.4. Device-row is keyed by **IP**, no auth, bulk endpoint exists, base is `http://192.168.5.1:9000`. `GET /api/devices` has now been **hardware-verified** from the operator's phone (§3.4.1) — shape matches the doc with one wrapping `{"data": [...]}` envelope and one extra field `is_current_device`. The remaining hardware checks (`POST /api/update_proxy` round-trip, egress change, IP stability) are gated on the in-flight VPS-side router tunnel. **Scope simplification:** the operator confirmed that end-of-job proxy cleanup is not required — orchestrator only needs to set the right proxy at job start, so question #11 ("how do we clear a proxy?") is **closed (not needed)**.

---

## 1. Product context

- **confirmed** GenRouter is a hardware proxy router sold by GenFarmer; it sits between the upstream network and the phone farm and applies per-device proxy settings without installing anything on the phones. Source: [GenRouter — How it works](https://genrouter.com/pages/how-it-work) and [GenRouter — product page](https://genrouter.com/).
- **confirmed** The vendor positions three integrated products: GenLogin (anti-detect browser profiles), GenFarmer (no-code phone-farm automation, screen streaming), GenRouter (network/proxy layer). Source: [The Gen Ecosystem](https://genrouter.com/blogs/news/the-gen-ecosystem-genlogin-genfarmer-genrouter).
- **confirmed** The "Proxy Distribute" feature is reached through the device manager UI in GenFarmer ("Devices Manager → Setup Router → Proxy Distribute"). Source: [Proxy Distribution feature](https://fast-router-proxy.gitbook.io/genrouter/how-to-use/proxy-distribution-feature).
- **confirmed** A SOCKS5 proxy is configured with the standard URL form `socks5://user:pass@ip:port`. Source: [Hướng dẫn sử dụng Gen-router](https://fast-router-proxy.gitbook.io/fast-router-api-document/genrouter/huong-dan-su-dung-gen-router).
- **likely** Up to ~50 devices per single GenRouter unit; a "Mini PC Router" SKU scales to 200–300 devices. Source: [GenRouter product page (50 devices)](https://genfarmer.com/shop/san-pham/genrouter-proxy-ios-android-50-thiet-bi/) and [Mini PC Router (200–300 devices)](https://genfarmer.com/shop/san-pham/mini-pc-router-en/).

## 2. What the official docs actually describe (UI workflow)

All vendor pages we were able to read describe **UI workflows**, not REST endpoints. Cited verbatim where possible.

### 2.1 Proxy entry per device

- **confirmed** Admin UI is reached at `http://192.168.5.1:9000/` after connecting to the router, or via the GenFarmer client in its "Router" section. Source: [User guide](https://fast-router-proxy.gitbook.io/fast-router-api-document/genrouter/huong-dan-su-dung-gen-router).
- **confirmed** Per the user guide, connected devices are listed in the GenRouter screen; the operator pastes a SOCKS5 string into the device row and clicks **Update** to apply.
- **unknown** Whether the row identifier shown in the UI is keyed by MAC address, by DHCP-assigned IP, by hostname, or by some internal port/slot ID — the page does not specify, and we have no hardware in hand.

### 2.2 Wi-Fi Manager (multi-SSID broadcast)

- **confirmed** GenRouter can broadcast multiple SSIDs simultaneously and "assign different IPs or VPNs to separate WiFi networks for managing devices more efficiently". Per-SSID fields: Band (2G/5G), SSID, Brand (Viettel / TP-Link / Cisco / Huawei / Xiaomi / Asus / …), MAC address (custom or auto), Password (optional, min 8 chars), Hidden toggle. Workflow: Add WiFi → fill fields → Add → toggle in Action column → "Apply for Router". Source: [WiFi Manager](https://fast-router-proxy.gitbook.io/genrouter/how-to-use/wifi-manager).
- **likely** A single GenRouter can broadcast up to 32 SSIDs simultaneously (vendor marketing claim). Source: [GenRouter product page](https://genfarmer.com/shop/san-pham/genrouter-proxy-ios-android-50-thiet-bi/).
- **unknown** Whether the SSID itself can be configured to carry a fixed SOCKS5 upstream (so any phone joining that SSID inherits that proxy), or whether SSID is only a network-layer grouping with proxy still pinned per device row. Vendor copy uses both framings.

### 2.3 Proxy Distribution modes

From [Proxy Distribution feature](https://fast-router-proxy.gitbook.io/genrouter/how-to-use/proxy-distribution-feature):

- **confirmed (UI)** "Rotate Proxy" mode rotates a pool of N proxies across M devices on a timer (`Rotate Time` in seconds). Example given: 10 proxies × 5 devices, rotate every 10 s.
- **confirmed (UI)** "Allow Duplicate" mode lets the same proxy be assigned to multiple devices simultaneously. With this mode **off**, each proxy is bound to a single device, and any device without an assignment falls through to the router's original upstream IP.
- **assumption** "Off + non-duplicate" is the mode we want for MVP: one account = one proxy = pinned to whichever phone currently holds that account's session. Rotation breaks our invariant I3 ("One account owns exactly one proxy").

## 3. REST/HTTP API — what is and is not confirmed

Short version: **no public REST API for GenRouter is documented in the pages we could read.** This is the most important finding for this issue.

### 3.1 What we looked at

- `fast-router-proxy.gitbook.io/fast-router-api-document/genrouter/huong-dan-su-dung-gen-router` — UI guide only; no endpoints.
- `fast-router-proxy.gitbook.io/genrouter/how-to-use/wifi-manager` — UI fields only; "no mention of REST APIs, JSON interfaces, command-line tools, or scripting hooks".
- `fast-router-proxy.gitbook.io/genrouter/how-to-use/proxy-distribution-feature` — UI workflow only.
- `fast-router-proxy.gitbook.io/genrouter/release-note` — release notes through 2025-06-23 mention "VPN and proxy distribution" and "proxy management tools" improvements; no endpoint-level entries.

### 3.2 GitBook "Ask AI" answers — DO NOT TRUST AS EVIDENCE

The GitBook space exposes an `?ask=…` URL parameter that returns generated answers. We queried it twice with similar questions and got **mutually inconsistent** answers:

- Query A returned: `POST /api/devices`, `POST /api/v1/update_proxy`, keyed by **MAC**, base URL `http://192.168.5.1:9000`, body shape `{ "<mac>": "socks5://…" }`.
- Query B returned: `POST /api/update_proxy`, `GET /api/devices`, `GET /api/system/info`, keyed by **IP**, base URL `http://192.168.8.1:9000`, body shape `{ "<ip>": { type, server, port, username, password } }`.

Different paths, different base IPs (5.1 vs 8.1), different keying (MAC vs IP), different body schemas — this is consistent with the assistant inventing plausible answers, not quoting source pages. **These endpoints are not evidence and must not be coded against without first-hand verification on a real GenRouter.**

### 3.3 What the project already assumed

- The architecture baseline already lists `GENROUTER_BASE_URL` as a required env var (see `docs/contracts/environment.md`, "Device / proxy backends"). That commits us to *some* HTTP interface, but does not specify what it is.

### 3.4 Vendor integrations page (added 2026-05-17) — **confirmed (vendor doc) / unverified (hardware)**

The vendor publishes a dedicated integrations page that **does** document a REST API: [Integrations — fast-router-proxy.gitbook.io](https://fast-router-proxy.gitbook.io/genrouter/how-to-use/integrations). It was not linked from the user-guide page we read first, which is why §3.1 missed it. The endpoints below are quoted verbatim from that page.

**Base URL:** `http://192.168.5.1:9000` (examples on the page also use `http://192.168.8.1:9000` — the LAN-side router address, which depends on the unit's DHCP config).

**Auth:** none documented. No cookie, token, basic, or bearer scheme appears on the page.

**Endpoints:**

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/update_proxy` | Assign SOCKS5 proxy to one or more device rows (bulk by design — body is an IP-keyed map). |
| `GET` | `/api/devices` | List connected devices: `[{ip, mac, hostname, connected}]`. |
| `GET` | `/api/system/info` | Firmware build/version, `need_reboot` flag. |
| `GET` | `/api/router/info` | Router info. |
| `GET` | `/api/system/config` | Global proxy / webRTC config. |
| `GET` | `/api/system/network` | Network config. |
| `GET` | `/api/system/check_for_update` | Firmware update check. |
| `POST` | `/api/router/create_wifi` | Create / configure SSIDs (radio + ssid array). |

**`POST /api/update_proxy` — request body shape (verbatim):**

```json
{
    "192.168.4.253": {
        "type": "socks5",
        "server": "179.60.183.234",
        "port": 50101,
        "username": "genrouter",
        "password": "MDoFXVw5s8"
    }
}
```

Response: `{"success": true}`. Multiple IPs in a single map = one bulk call.

**`GET /api/devices` — response shape (verbatim from vendor doc):**

```json
{
    "data": [
        {
            "ip": "192.168.8.101",
            "mac": "40:c2:ba:89:c1:51",
            "hostname": "akatsuki",
            "connected": true
        }
    ]
}
```

**Implications for our design:**

- **Device key = IP**, not MAC. This is the load-bearing fact. A phone that gets a new DHCP lease becomes a different row for `update_proxy` purposes. The orchestrator must therefore resolve `phone → current IP` at job start by querying `/api/devices` and matching on MAC (the stable identifier). We cannot cache device-row IDs across jobs.
- **No auth** simplifies the call site but also means anyone on the LAN can rebind proxies — keep GenRouter on the trusted network only (Tailscale subnet, not the public WAN).
- **Bulk endpoint** lets the orchestrator do an atomic batch rebind if we ever need it (e.g. shift N accounts onto M phones in one shot).
- ~~**No documented "clear proxy" verb** — open question: do we send a body without that IP key, or send a null/empty value? Needs one hardware test.~~ — **dropped (rev 3):** operator confirmed end-of-job cleanup is not required. Orchestrator overwrites at job start; whatever proxy is left on a row between jobs is irrelevant. See §4.
- The page does **not** document GenFarmer ↔ GenRouter relay, webhooks, events, or external/cloud access. External reach (VPS → GenRouter) is an operator-side topology problem (port-forward / Tailscale subnet route / GenFarmer co-located in same LAN), not an API problem — see §3.4.2.

### 3.4.1 Hardware verification of `GET /api/devices` (added 2026-05-17, rev 3) — **confirmed (hardware)**

The operator opened `http://192.168.5.1:9000/api/devices` from a phone on the GenRouter LAN. Real response (excerpt, hostnames redacted where they were identifying):

```json
{
  "data": [
    { "ip": "192.168.5.25", "mac": "dc:04:5a:20:b1:da", "hostname": "*",          "connected": true, "is_current_device": false },
    { "ip": "192.168.5.35", "mac": "dc:04:5a:20:b1:e4", "hostname": "*",          "connected": true, "is_current_device": false },
    { "ip": "192.168.5.33", "mac": "dc:04:5a:20:b1:e2", "hostname": "SM-G781B",   "connected": true, "is_current_device": false }
  ]
}
```

Three deltas vs. the vendor doc, all benign:

1. **Wrapper.** Response is `{"data": [...]}`, not a bare array. The doc actually showed this too (re-read §3.4 sample), so no change — note here for clarity because hardware confirmed it.
2. **Extra field `is_current_device: bool`.** Not in the doc. Probably marks whichever row corresponds to the caller's own MAC/IP. Operator's call returned `false` for every row, consistent with the request originating from a separate client (not from one of the listed devices' own browsers). Orchestrator can **ignore this field** — selection still happens by MAC.
3. **`hostname` is often `"*"`.** Two of three rows use the placeholder, one carries a real device model (`SM-G781B`, Samsung Galaxy S20 FE 5G). Treat `hostname` as **decorative / unreliable** — never as a stable key. MAC is the only safe anchor.

Also confirmed by inspection of this response: this unit's LAN segment is `192.168.5.0/24` (not `192.168.8.0/24` — both are documented as possible bases). So for this specific deployment, base URL is `http://192.168.5.1:9000`.

Status of §3.4 endpoint table — `GET /api/devices` row is now **confirmed (hardware)**. The other rows remain **confirmed (vendor doc) / unverified (hardware)** until §5 steps 3–4 run from the VPS.

### 3.4.2 GenFarmer ↔ GenRouter binding — clarified (2026-05-17, rev 3)

Operator clarified that GenFarmer's "Setup Router" linkage is **not** an API relay or proxy. It just lets the GenFarmer desktop client drive the GenRouter web UI from inside its own window — both processes must be on the same LAN as the router to use it. So:

- It does **not** give a VPS-side caller a path through GenFarmer to reach GenRouter's API.
- It is **not relevant** for the FFF-22 design as long as the planned "forward router-UI onto VPS" topology lands separately (which is the actual gate for VPS reach).
- Deferred until / unless we find a use case beyond what direct HTTP to `/api/...` already covers.

This closes the "is GenFarmer a relay?" speculation from comment 6 above (`6bb85b65`).

**Why we still don't blindly trust this:** the same product publishes the `?ask=…` AI answerer that produced the contradictory answers in §3.2, *and* the integrations page does not show timestamps or version pinning, *and* there is no statement of API stability across firmware updates. One first-hand verification run from the real router (§5) converts these claims to **confirmed (hardware)**.

## 4. Device ↔ proxy mapping — fit with our invariants

Our invariants (from `docs/architecture.md` §2):

- I3: one account owns exactly one proxy, one device profile, one GPS, one app/session.
- I4: physical phones are interchangeable executors; environment is loaded per-job.

This produces a clear constraint: **the proxy follows the account, not the phone.** So any GenRouter binding we settle on has to be re-pointable at job start: when account `A` is loaded onto whichever free phone `P` we just claimed, `A`'s SOCKS5 must end up applied to `P`'s traffic.

**Lifecycle simplification (rev 3, operator confirmed):** the orchestrator only needs to guarantee **state at job *start***. End-of-job cleanup is *not* a requirement. Whatever proxy is left on a device row between jobs is irrelevant, because the next job that uses that phone will overwrite it before doing anything observable. This eliminates one whole class of "we crashed before cleanup ran, now the next account leaks egress through the previous account's proxy" failure modes — the start-of-job overwrite is itself the safety barrier.

Three plausible architectures (none are confirmed; each needs a hardware PoC):

| Option | How it would work | Pros | Cons / risks |
|---|---|---|---|
| **A. UI-scripted per-job rewrite** | At job start, talk to GenRouter (HTTP or scripted UI) to set the proxy on the row matching phone `P`'s MAC/IP. No restore at job end (operator-confirmed: not required). | Maps cleanly onto MVP; one source of truth per phone at job start; no cleanup path to fail. | Requires a real API, or scripting the web UI — the latter is brittle and slow. |
| **B. Per-account SSID** | Pre-create one SSID per account (up to 32 per router); each SSID already has the account's SOCKS5 attached. At job start, make phone `P` connect to account `A`'s SSID. | No live rewrite; isolation by network is strong. | Only works if SSIDs can carry a per-SSID proxy (unconfirmed §2.2). Caps at ~32 accounts per router. Switching SSIDs from ADB needs verification. |
| **C. Skip GenRouter, set proxy on device** | Configure SOCKS5 on the Android side (Wi-Fi proxy settings, or `iptables` via root, or per-app via Appium proxy capability). | No GenRouter coupling. | Wi-Fi proxy on Android is HTTP-only; SOCKS5 typically needs root or a per-app helper. Loses GenRouter's "no app on device" detectability win. |

**Tentative preference (assumption, not a decision):** Option A if the API turns out to exist; Option B if it doesn't and SSID-bound proxies are confirmed; Option C only as last resort.

## 5. Suggested next steps (safe PoCs)

These are scoped to the device-environment-layer project and avoid the "no destructive device commands" rule. Each PoC should land its commands and outputs under `scripts/research/` with secrets redacted.

> **Revision 2026-05-17 (rev 3):** §3.4 located a documented API; step 1 below (`GET /api/devices` from phone) is now **done** — see §3.4.1. The remaining critical path is **verify the rest of the endpoints from VPS** once the operator's router-tunnel-onto-VPS work lands (steps 2–4). Browser-traffic capture (step 6) is fallback only.

1. ~~**GET /api/devices from the phone.**~~ **Done 2026-05-17.** Operator pulled `http://192.168.5.1:9000/api/devices` from a phone browser; response matches §3.4 shape with a wrapping `{"data": [...]}` envelope and one extra ignorable `is_current_device` field — see §3.4.1.
2. **GET /api/devices from VPS (once the operator's pending router-on-VPS forward is in place).** Same call, run from the VPS shell. If responses match the phone-side call, VPS reach works. If the base URL from VPS is something other than `http://192.168.5.1:9000` (e.g. `localhost:9000` if it's a TCP forward, or the phone's Tailscale 100.x.y.z IP if subnet routing was used), record the actual URL — this is what `GENROUTER_BASE_URL` needs to be set to in the VPS env.
3. **POST /api/update_proxy round-trip on one idle device.** From VPS shell, on a phone that is **not** currently running a job, send the `update_proxy` body from §3.4 with a known-harmless test SOCKS5 (e.g. a localhost stub or an unroutable RFC 5737 address like `socks5://test:test@192.0.2.1:1080`). Expect `{"success": true}`. No restore step required (cleanup is not in scope — see §4); just leave the test value on the row, or overwrite it with a second POST if you want to leave the device usable.
4. **Verify egress actually changes.** With the test proxy active on the device, ADB `shell curl https://api.ipify.org` from the device. The public IP should reflect the SOCKS5 server, not the router's WAN.
5. **IP-stability check (one-time).** Toggle airplane mode on the test phone (or reboot it), wait for it to rejoin the GenRouter SSID, call `/api/devices` again, and confirm whether the same MAC still has the same IP. If IP drifts: orchestrator must lookup `MAC → current IP` at job start (one extra GET per job, no blocker). If IP is stable: simpler. — This step is independent of the VPS tunnel and can be run from the phone right now.
6. **Fallback only if step 2–3 fail:** capture the admin UI's actual XHRs (the existing `docs/research/genrouter-operator-checklist.md` covers this). Useful only if the documented endpoints in §3.4 turn out to disagree with the live firmware.
7. ~~**One vendor question worth asking** (lower priority now): how to *clear* a per-device proxy via `/api/update_proxy`…~~ — **dropped (rev 3):** cleanup not required.

## 6. Unresolved questions

These map back to the architecture's open-questions list (`docs/architecture.md` §7, esp. #2 "Proxy lifecycle" and #3 "Device profile fingerprint"). Status as of the 2026-05-17 revision is in **bold** after each question.

1. **Does a stable, documented REST API exist?** — **answered by §3.4** (documented). Stability across firmware updates is still unverified; add a `GET /api/system/info` startup fingerprint check before we depend on this.
2. **What is the device identifier?** — **answered by §3.4: IP** (with MAC also reported by `/api/devices`). Practical consequence: orchestrator does `MAC → current IP` lookup at job start.
3. **Is the proxy property of a device row or of an SSID?** — **answered: device row** (`/api/update_proxy` is IP-keyed; `/api/router/create_wifi` body does not carry a proxy field). Architecture Option B (per-account SSID with attached proxy) is therefore **off the table** unless undocumented.
4. **What auth does the local web UI require?** — **answered: none documented**, and the operator confirms no login screen on `192.168.5.1:9000`. Treat the router as trusted-LAN-only.
5. **Is there a separate "Apply" step after `/api/update_proxy`?** — still **unknown** at the API layer. Verify in §5 step 3 by checking whether egress changes immediately after the POST or only after some second call.
6. **Idempotency and error model.** Still **unknown** — verify by POST'ing the same body twice and by POST'ing a SOCKS5 that points to a dead upstream.
7. **Firmware version pinning.** Still relevant — depend on `/api/system/info` `build_version` and fail loudly if it changes.
8. **Concurrent writers.** Still **unknown** — bulk-call form in §3.4 suggests the orchestrator should serialize writes through one process anyway.
9. **Cap on simultaneous bindings.** Still **unknown** at the API layer; vendor marketing says "up to 50 devices" per unit.
10. **Mini PC Router parity.** Still **unknown** — assume the documented API is the same surface, re-verify when/if we move to that SKU.
11. ~~**(new) How do we clear a per-device proxy?**~~ — **closed (rev 3): not needed.** Operator confirmed cleanup-at-job-end is out of scope; start-of-job overwrite is the only state guarantee we need.
12. **(new) Reach from VPS.** Operator request to forward router-UI onto VPS is in-flight. Once landed, confirm whether the base URL the VPS hits is `http://192.168.5.1:9000` (subnet route), `localhost:9000` (port-forward), or a Tailscale 100.x address. This is the value that goes into `GENROUTER_BASE_URL`.
13. **(new) `is_current_device` semantics.** §3.4.1 found this field; the orchestrator can ignore it for proxy assignment, but it's worth one line in the eventual integration doc to note that the field exists and is keyed off the caller's identity, not the row's role.

## 7. Recommendation (revised 2026-05-17, rev 3 — medium/high confidence)

- **Evidence:** vendor publishes a REST API at `/genrouter/how-to-use/integrations` (§3.4). `GET /api/devices` now **confirmed against real hardware** (§3.4.1) — same shape as the doc plus a wrapping `{"data": [...]}` envelope and an ignorable `is_current_device` field; for this deployment LAN is `192.168.5.0/24` so base URL is `http://192.168.5.1:9000`. `POST /api/update_proxy` and the rest remain **confirmed (vendor doc)** until §5 steps 3–4 run from the VPS.
- **Architecture pick:** Option **A (per-job rewrite via `POST /api/update_proxy`)** is the working design. Option B (per-SSID proxy) is off the table because `/api/router/create_wifi` does not carry a proxy field. Option C (proxy on the Android side) stays as fallback only if §5 verification fails. **Lifecycle: set-on-start only, no cleanup-on-end** (operator-confirmed, rev 3) — the start-of-next-job overwrite is itself the safety barrier.
- **Resolution strategy at job start:** (1) `GET /api/devices` → match `mac == phone.mac` to find current `ip`; (2) `POST /api/update_proxy {"<ip>": {type, server, port, user, pass}}`; (3) optionally ADB `curl https://api.ipify.org` to verify egress before handing the phone to the worker. `hostname` and `is_current_device` are decorative — do not key off them.
- **Risk:** depending on a documented-but-not-fully-hardware-verified API. Mitigate with (a) `GET /api/system/info` build-version fingerprint check at orchestrator startup, (b) the remaining hardware verification (§5 steps 3–4) once the VPS-side router tunnel is in place.
- **Next step:** the in-flight operator action — forward GenRouter's web/API onto the VPS — is the only remaining gate. The moment that lands, run §5 steps 2–4 from the VPS shell. §5 step 5 (IP stability) can be run from the phone right now, independently. GenFarmer↔GenRouter binding is **not** that gate (§3.4.2): it's an in-LAN UI bridge, not an API relay.
- **Confidence:** medium-high. Up from "medium" because (i) one endpoint is now hardware-confirmed and (ii) the design simplification (no cleanup) eliminates the most fragile failure mode.

---

## Sources

- [GenRouter — How it works](https://genrouter.com/pages/how-it-work)
- [GenRouter — product overview](https://genrouter.com/)
- [The Gen Ecosystem (GenLogin / GenFarmer / GenRouter)](https://genrouter.com/blogs/news/the-gen-ecosystem-genlogin-genfarmer-genrouter)
- [GenRouter — 50-device SKU](https://genfarmer.com/shop/san-pham/genrouter-proxy-ios-android-50-thiet-bi/)
- [Mini PC Router — 200–300 device SKU](https://genfarmer.com/shop/san-pham/mini-pc-router-en/)
- [Gen-router user guide (UI workflow)](https://fast-router-proxy.gitbook.io/fast-router-api-document/genrouter/huong-dan-su-dung-gen-router)
- [GenRouter Wi-Fi Manager](https://fast-router-proxy.gitbook.io/genrouter/how-to-use/wifi-manager)
- [GenRouter Proxy Distribution feature](https://fast-router-proxy.gitbook.io/genrouter/how-to-use/proxy-distribution-feature)
- [GenRouter release notes](https://fast-router-proxy.gitbook.io/genrouter/release-note)
- [GenRouter Integrations — REST API spec](https://fast-router-proxy.gitbook.io/genrouter/how-to-use/integrations) — load-bearing for §3.4
- [GenFarmer API reference (separate product, no proxy endpoint)](https://genfarmer-support.gitbook.io/genfarmer-eng/main-menu-bar/api)
