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

## Proxy list
Saved (gitignored) at `.secrets/proxies.json`: `vietnam_socks5`,
`america_datacenter_socks5`, `america_isp_socks5`; format `ip:port:user:pass`.
Device tz = `Asia/Ho_Chi_Minh` and the fleet's bare egress is a VN ISP IP
(`14.245.75.171`) → Vietnam proxies match. All 5 VN proxies verified live 2026-06-09.
