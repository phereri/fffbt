# GenRouter — proxy assignment research

- Status: research notes (FFF-22)
- Owner: Research Agent
- Last updated: 2026-05-17
- Scope: how to assign a SOCKS5 proxy to a specific physical Android device, programmatically, using GenRouter

This document covers what is publicly documented by the vendor (GenFarmer / GenRouter / fast-router-proxy), separates observations from conclusions, and lists what still needs to be verified on real hardware before we commit to a design.

Confidence labels used below: **confirmed** (quoted from vendor docs we read), **likely** (consistent across multiple vendor pages but not directly quoted), **assumption** (our inference), **unknown** (we could not find authoritative information).

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

## 4. Device ↔ proxy mapping — fit with our invariants

Our invariants (from `docs/architecture.md` §2):

- I3: one account owns exactly one proxy, one device profile, one GPS, one app/session.
- I4: physical phones are interchangeable executors; environment is loaded per-job.

This produces a clear constraint: **the proxy follows the account, not the phone.** So any GenRouter binding we settle on has to be re-pointable at job start: when account `A` is loaded onto whichever free phone `P` we just claimed, `A`'s SOCKS5 must end up applied to `P`'s traffic.

Three plausible architectures (none are confirmed; each needs a hardware PoC):

| Option | How it would work | Pros | Cons / risks |
|---|---|---|---|
| **A. UI-scripted per-job rewrite** | At job start, talk to GenRouter (HTTP or scripted UI) to set the proxy on the row matching phone `P`'s MAC/IP. Restore at job end. | Maps cleanly onto MVP; one source of truth per phone at any moment. | Requires a real API, or scripting the web UI — the latter is brittle and slow. |
| **B. Per-account SSID** | Pre-create one SSID per account (up to 32 per router); each SSID already has the account's SOCKS5 attached. At job start, make phone `P` connect to account `A`'s SSID. | No live rewrite; isolation by network is strong. | Only works if SSIDs can carry a per-SSID proxy (unconfirmed §2.2). Caps at ~32 accounts per router. Switching SSIDs from ADB needs verification. |
| **C. Skip GenRouter, set proxy on device** | Configure SOCKS5 on the Android side (Wi-Fi proxy settings, or `iptables` via root, or per-app via Appium proxy capability). | No GenRouter coupling. | Wi-Fi proxy on Android is HTTP-only; SOCKS5 typically needs root or a per-app helper. Loses GenRouter's "no app on device" detectability win. |

**Tentative preference (assumption, not a decision):** Option A if the API turns out to exist; Option B if it doesn't and SSID-bound proxies are confirmed; Option C only as last resort.

## 5. Suggested next steps (safe PoCs)

These are scoped to the device-environment-layer project and avoid the "no destructive device commands" rule. Each PoC should land its commands and outputs under `scripts/research/` with secrets redacted.

1. **Capture the web UI's network traffic.** Connect to a real GenRouter, log into `http://192.168.5.1:9000/`, perform one "Update proxy" action, and capture the request the SPA makes (browser devtools → Network → copy as `curl`). This is the only way to learn the real endpoint, auth scheme, and keying (MAC vs IP).
2. **List the device table via the same capture.** When the UI lists devices, observe the GET it issues. That tells us how device rows are identified.
3. **Check whether SSID config carries a proxy field.** Inspect the "WiFi Manager → Add WiFi" form payload to see if a SOCKS5 URL is part of the SSID record or stored against the device row.
4. **Confirm proxy egress.** From an Android device on GenRouter, hit `https://api.ipify.org` before and after the change to confirm the public IP actually flips.
5. **Ask the vendor.** Vendor pages list `info@genrouter.com` / support links. One direct email asking "is there a documented HTTP API for proxy assignment, and what is its stability guarantee?" would cost nothing and resolve §3 definitively.

## 6. Unresolved questions

These map back to the architecture's open-questions list (`docs/architecture.md` §7, esp. #2 "Proxy lifecycle" and #3 "Device profile fingerprint").

1. **Does a stable, documented REST API exist?** Or is the UI the only contract? If the latter, we either reverse-engineer the SPA's calls (acceptable for internal tooling, but no stability guarantee across firmware updates) or move to Option B/C above.
2. **What is the device identifier?** MAC address, DHCP IP, internal port/slot ID, or something the operator labels manually? This determines whether "swap account onto new phone" requires re-binding the proxy at job start.
3. **Is the proxy property of a device row or of an SSID?** This determines whether Option B is viable.
4. **What auth does the local web UI require?** Pages we read mention a login but not its mechanism. Cookie? Basic auth? Session token? Is it bypassable on the LAN side?
5. **Is the "Apply for Router" step required after each change?** I.e., is the API call atomic, or is there a separate commit step we must trigger?
6. **Idempotency and error model.** What happens if we POST the same proxy twice? What does the response look like when the upstream SOCKS5 itself is dead?
7. **Firmware version pinning.** Release notes show meaningful changes every 1–2 months in 2025. If we depend on an undocumented endpoint, we need a fingerprint check at startup so a firmware bump can't silently break jobs.
8. **Concurrent writers.** If two workers try to bind a proxy to the same row simultaneously (e.g. race on a freed phone), what does GenRouter do — last-writer-wins, reject, queue?
9. **Cap on simultaneous bindings.** Vendor lists "up to 50 devices" — does that include idle/unassigned rows, or only ones with active proxies? Affects how many accounts a single router can hold warm.
10. **Mini PC Router parity.** If we later move to the 200–300 device Mini PC variant, does it expose the same UI/API surface, or is it a different stack we'd have to research separately?

## 7. Recommendation (low confidence)

- **Evidence:** vendor docs describe only UI workflows; "Ask AI" answers are inconsistent and not credible; GenFarmer's documented API at `127.0.0.1:55554` (separate research, FFF-21 area) covers automation tasks and devices but **does not document any proxy-assignment endpoint** ([GenFarmer API](https://genfarmer-support.gitbook.io/genfarmer-eng/main-menu-bar/api)).
- **Risk:** committing to "GenRouter HTTP API" in the architecture before we have hardware-verified endpoint(s) means designing around an interface that may not exist as documented.
- **Next step:** before any code lands, do PoC step §5.1 (capture the real network calls) on one router. This converts the unknowns in §3 into either "confirmed endpoint" or "decision to use Option B/C".
- **Confidence:** low. The whole §3 conclusion can flip the moment we plug in real hardware.

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
- [GenFarmer API reference (separate product, no proxy endpoint)](https://genfarmer-support.gitbook.io/genfarmer-eng/main-menu-bar/api)
