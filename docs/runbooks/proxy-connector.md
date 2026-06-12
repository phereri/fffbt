# Proxy on a device — GenFarmer ProxyConnector (ADB) vs GenRouter (router)

Two ways to put a device's traffic behind a SOCKS5 proxy. **Which one you can use
depends on how you control the phone (USB adb vs network/tailscale adb).**

## 1. GenFarmer on-device ProxyConnector — ADB broadcasts

App `com.genfarmer.proxyconnector` (system priv-app, `/system/priv-app/GenfarmerProxy/`).
It is a **full-tunnel `VpnService`**. Drive it with broadcasts to `.ProxyReceiver`:

```bash
# CONNECT  (keys: protocol / address / port[int] / username / password)
adb shell am broadcast -a com.genfarmer.proxyconnector.CONNECT_PROXY \
  -n com.genfarmer.proxyconnector/.ProxyReceiver \
  --es protocol socks5 --es address <IP> --ei port <PORT> \
  --es username <USER> --es password <PASS>

# STOP
adb shell am broadcast -a com.genfarmer.proxyconnector.STOP_PROXY \
  -n com.genfarmer.proxyconnector/.ProxyReceiver

# CHECK  -> "Broadcast completed: result=200, data=\"{\"status\":true|false}\""
adb shell am broadcast -a com.genfarmer.proxyconnector.CHECK_PROXY_CONNECTED \
  -n com.genfarmer.proxyconnector/.ProxyReceiver
```

Notes:
- Drive `am broadcast` via a **subprocess arg-list**, never a shell string —
  spaces/quotes get mangled through ssh→PowerShell→cmd→adb-shell and a malformed
  config blackholes the device.
- On-device check of egress: `adb shell curl -s https://api.ipify.org`
  (`/system/bin/curl` exists; toybox has **no** wget).

### ⚠️ Incompatible with network/tailscale adb control (verified 2026-06-09)
Because it is a **full tunnel**, enabling it captures the tailscale path we use for
`adb connect <tailscale-ip>:5555`, so the device **drops off adb and does not come
back** — even with a correct command and a verified-live proxy. It does **not**
self-heal (a bad config blackholes everything); recovery needs a **reboot** or a
**GenFarmer-desktop stop-proxy**. This severed two phones during testing.

➡️ Use this ONLY when the phone is controlled over **USB** (the VPN doesn't touch
the USB adb channel — this is how the production farm uses it). For our remote
VPS + tailscale-adb setup, use GenRouter instead.

## 2. GenRouter — router-level proxy (preferred for tailscale-adb)

Proxy is applied at the router (`http://<gateway>:9000`), so there is **no on-device
VPN** and the adb/tailscale control path is not torn down. Reachable from the device
side (the orchestrator host usually cannot reach the phone LAN directly):

```bash
# list devices (read-only)
adb shell curl -s http://192.168.5.1:9000/api/devices

# assign a SOCKS5 proxy to a device row (keyed by the device LAN ip)
adb shell curl -s -X POST http://192.168.5.1:9000/api/update_proxy \
  -d '{"<device_lan_ip>":{"type":"socks5","server":"<IP>","port":<PORT>,"username":"<USER>","password":"<PASS>"}}'
```

Verify egress after: `adb shell curl -s https://api.ipify.org` should show the proxy IP.
(Tailscale must survive the egress change — gentler than the full-tunnel VPN, but
still verify on a spare device before fleet use.)

### ⚠️ `update_proxy` alone does NOT activate (verified 2026-06-11, router build 20260511)
On the live farm router, `POST /api/update_proxy` returns `{"success":true}` and is
**adb-safe** (Tailscale survives — verified on spare SM-G781B 192.168.4.161), BUT:
- the device row from `GET /api/devices` shows **no `proxy` object** afterwards, and
- egress (`curl api.ipify.org`) **stays on the bare fleet IP** (`14.245.75.171`),
  even after 45s — the assignment does not take effect.

`GET /api/system/config` on this router shows the gate:
```json
{"data":{"webrtcMode":0,"globalProxyMode":0,"globalProxy":null,"isolatedMode":0,"whiteListDevice":null},"success":true}
```
`globalProxyMode:0` = proxy mode OFF. The official GenRouter doc
(`fast-router-proxy.gitbook.io/genrouter/how-to-use/integrations`) lists ONLY
`update_proxy` and read-only `system/config`/`system/info`/`router/info`/`devices`
+ `router/create_wifi`; it documents **no** endpoint to flip `globalProxyMode`/
`globalProxy` or to "apply" a proxy. So activation must be done via the **GenRouter
web UI** (or an undocumented POST). Do NOT blind-POST config to this router — it is
SHARED by the whole farm and a bad write can drop everyone's networking.
Other read-only endpoints found: `/api/system/info` (build/version/license),
`/api/system/config`, `/api/system/network`, `/api/router/info`.

### `update_proxy` is a NO-OP on this build — use the panel's Proxy Distribute (2026-06-11)
Reverse-engineering the GenRouter web UI (`/assets/*.js`) shows it does **NOT** call
`/api/update_proxy` anywhere. The documented `update_proxy` is an external-integration
endpoint that this firmware accepts (`{"success":true}`) but does not wire to the
actual proxy distribution — assignments never appear on the device row and egress
never changes, regardless of pool membership or payload shape. So **do not rely on
`update_proxy`** for per-device proxying here.
What the UI actually uses:
- Proxy POOL CRUD: `GET/POST /api/db-proxy`, `/api/db-proxy/{id}`; test one with
  `/api/check_proxy`. (Pool already holds many VN socks5 entries, e.g. id127
  `14.235.129.92:40833`.)
- Per-device assignment is the **"Proxy Distribute"** UI feature: it stages device→
  proxy mappings in a client-side IndexedDB (Dexie, `devices: ++id,name,ip,mac,
  status,type,proxy`, `is_change` flag; types http/socks5/ovpn/dpn/pppoe) and pushes
  on "Apply". The push endpoint is built dynamically (not a literal in the bundle).
- Devices already proxied (verified live) DO show a `proxy` object in `/api/devices`
  with `dns_server`/`interface` fields — so that row IS the source of truth for
  whether a device is proxied. The Pixel 6 Pro (`192.168.4.169`, blank hostname,
  MAC `04:d6:aa:18:eb:52`) had none despite UI attempts → assignment must target
  that exact IP. ChangeDevice spoofs the model (getprop=Pixel 6 Pro, device_name=
  SM-N950F, DHCP hostname blank) so pick by IP/MAC, not name.

## Proxy list
Saved (gitignored) at `.secrets/proxies.json`: `vietnam_socks5`,
`america_datacenter_socks5`, `america_isp_socks5`; format `ip:port:user:pass`.
Device tz = `Asia/Ho_Chi_Minh` and the fleet's bare egress is a VN ISP IP
(`14.245.75.171`) → Vietnam proxies match. All 5 VN proxies verified live 2026-06-09.
