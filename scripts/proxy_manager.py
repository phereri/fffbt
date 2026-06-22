#!/usr/bin/env python3
"""Proxy orchestration: status, sync, and renewal over proxy.vn + the LAN router +
fffbt.proxies.

Source-of-truth model (the local table was stale, so we don't trust it for matching):
  * proxy.vn ``listproxy`` = the live INVENTORY: which idproxy exists, its endpoint
    (host:port:user:pass) and its expiry (``time`` epoch). Keyed by (port, username)
    because the same username spans providers.
  * the LAN router (/api/devices) = which proxy endpoint is bound to which phone, plus
    live health.
  * fffbt.proxies = a persisted mirror (``sync`` upserts the inventory into it).

  * status()        -> per-device view (account, bound proxy, idproxy, expiry, health,
                       blocked) by matching router endpoints to the inventory
  * sync()          -> upsert the proxy.vn inventory into fffbt.proxies
  * record_bought() -> INSERT freshly purchased proxies
  * renew(idproxy)  -> renew one proxy 2 days (SPENDS MONEY) + refresh expiry
  * renew_due()     -> auto-renew proxies of IN-WORK (not Blocked) devices expiring in
                       < 1 day. Blocked accounts are skipped.
"""
from __future__ import annotations

import json
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
sys.path.insert(0, str(ROOT))                  # repo root -> scripts.*, src.*

from scripts.post_trial import _lit, _load_env, _mgmt_query  # noqa: E402
from scripts import proxy_vn, router_proxy  # noqa: E402
from src.runner import fleet_events  # noqa: E402

RENEW_DAYS = 1                       # auto-renew extends by this many days (< 24h left)
BUY_DAYS = 2                         # fresh replacement proxies are bought for this long
RENEW_THRESHOLD_H = 24               # renew when < this many hours remain
DEFAULT_PROVIDER = "Viettel"

_INV_TTL = 300                       # inventory cache (proxy.vn list) seconds
_ROUTER_TTL = 20                     # router /api/devices cache seconds
_cache: dict = {"inv": None, "inv_ts": 0.0, "dev": None, "dev_ts": 0.0}


# --------------------------------------------------------------------------- utils
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch_to_iso(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")
    except Exception:
        return None


def _roster() -> dict[str, str]:
    try:
        return json.loads(BINDING.read_text(encoding="utf-8")).get("devices", {}) or {}
    except Exception:
        return {}


def _blocked_accounts() -> set[str]:
    """Accounts whose most-recent run result is BLOCKED (login challenge)."""
    latest: dict[str, str] = {}
    for e in fleet_events.read_events():
        if e.get("type") == "result" and e.get("account"):
            latest[e["account"]] = e.get("verdict") or ""
    return {a for a, v in latest.items() if v == "BLOCKED"}


# ------------------------------------------------------------------ live inventory
def _inventory(force: bool = False) -> dict[tuple, dict]:
    """proxy.vn inventory across all providers, keyed by (port, username)."""
    if not force and _cache["inv"] is not None and (_time.monotonic() - _cache["inv_ts"]) < _INV_TTL:
        return _cache["inv"]
    inv: dict[tuple, dict] = {}
    for prov in proxy_vn.PROVIDERS:
        try:
            rows = proxy_vn.list_proxies(prov)
        except Exception:
            continue
        for r in rows:
            p = proxy_vn.parse_proxy_string(r.get("proxy", ""))
            if not p.get("port") or not p.get("username"):
                continue
            inv[(p["port"], p["username"])] = {
                "idproxy": r.get("idproxy"), "provider": prov, "time": r.get("time"),
                "ip": r.get("ip"), "host": p.get("host"), "port": p["port"],
                "username": p["username"], "password": p.get("password"),
            }
    _cache.update(inv=inv, inv_ts=_time.monotonic())
    return inv


def _router_devices(force: bool = False) -> list[dict]:
    if not force and _cache["dev"] is not None and (_time.monotonic() - _cache["dev_ts"]) < _ROUTER_TTL:
        return _cache["dev"]
    try:
        devs = router_proxy.list_devices(timeout=25)
        _cache.update(dev=devs, dev_ts=_time.monotonic())
    except Exception:
        devs = _cache["dev"] or []          # serve last-known on a flaky router
    return devs


# ----------------------------------------------------------------------- DB access
def record_bought(rows: list[dict], provider: str) -> int:
    """INSERT freshly purchased proxy.vn rows into fffbt.proxies. Returns the count."""
    vals = []
    for r in rows:
        parsed = proxy_vn.parse_proxy_string(r.get("proxy", ""))
        host = parsed.get("host") or r.get("ip")
        port = r.get("port") or parsed.get("port")
        vals.append("(" + ", ".join([
            _lit(r.get("ip")), _lit(host), str(int(port)) if port else "NULL",
            _lit(r.get("user") or parsed.get("username")),
            _lit(r.get("password") or parsed.get("password")),
            _lit("socks5"), _lit(provider),
            str(int(r["idproxy"])) if r.get("idproxy") else "NULL",
            _lit(_epoch_to_iso(r.get("time"))), "now()",
        ]) + ")")
    if not vals:
        return 0
    _mgmt_query("INSERT INTO fffbt.proxies (ip, host, port, username, password, type, "
                "provider, idproxy, expires_at, purchased_at) VALUES " + ", ".join(vals))
    return len(vals)


def sync() -> int:
    """Upsert the live proxy.vn inventory into fffbt.proxies (by idproxy). Returns the
    inventory size touched."""
    inv = _inventory(force=True)
    existing = {int(r["idproxy"]) for r in _mgmt_query(
        "SELECT idproxy FROM fffbt.proxies WHERE idproxy IS NOT NULL")}
    ins, upd = [], []
    for v in inv.values():
        if not v.get("idproxy"):
            continue
        iso = _epoch_to_iso(v.get("time"))
        if int(v["idproxy"]) in existing:
            upd.append(f"({int(v['idproxy'])}, {_lit(iso)}::timestamptz)")
        else:
            ins.append("(" + ", ".join([
                _lit(v.get("ip")), _lit(v.get("host")),
                str(int(v["port"])), _lit(v["username"]), _lit(v.get("password")),
                _lit("socks5"), _lit(v["provider"]), str(int(v["idproxy"])),
                _lit(iso), "now()"]) + ")")
    if ins:
        _mgmt_query("INSERT INTO fffbt.proxies (ip, host, port, username, password, type, "
                    "provider, idproxy, expires_at, purchased_at) VALUES " + ", ".join(ins))
    if upd:
        _mgmt_query("UPDATE fffbt.proxies p SET expires_at = v.exp FROM (VALUES "
                    + ", ".join(upd) + ") AS v(idproxy, exp) WHERE p.idproxy = v.idproxy")
    return len(inv)


# ------------------------------------------------------------------ status assembly
def status() -> dict:
    """Per-device proxy view for the dashboard (router endpoints matched to the live
    proxy.vn inventory)."""
    roster = _roster()
    blocked = _blocked_accounts()
    inv = _inventory()
    devices = _router_devices()
    now = _now()

    items = []
    for d in devices:
        ip = d.get("ip")
        serial = f"{ip}:5555"
        account = roster.get(serial)
        px = d.get("proxy") or {}
        health = d.get("proxy_health") or {}
        match = inv.get((px.get("port"), px.get("username"))) if px else None
        exp_iso = _epoch_to_iso(match.get("time")) if match else None
        hours_left = None
        if match and match.get("time"):
            hours_left = round((int(match["time"]) - now.timestamp()) / 3600, 1)
        is_blocked = account in blocked if account else False
        in_work = bool(account) and not is_blocked
        if hours_left is None:
            state = "external" if px else "none"     # not in our proxy.vn account / no proxy
        elif hours_left <= 0:
            state = "expired"
        elif hours_left < RENEW_THRESHOLD_H:
            state = "expiring"
        else:
            state = "ok"
        items.append({
            "device": serial, "ip": ip, "account": account, "online": d.get("online"),
            "blocked": is_blocked, "in_work": in_work,
            "proxy": {"server": px.get("server"), "port": px.get("port"),
                      "username": px.get("username")} if px else None,
            "idproxy": match.get("idproxy") if match else None,
            "provider": match.get("provider") if match else None,
            "expires_at": exp_iso, "hours_left": hours_left, "state": state,
            "health": {"working": health.get("working"), "latency_ms": health.get("latency_ms"),
                       "error": health.get("error")},
            "renewable": bool(match and match.get("idproxy")),
        })
    order = {"expired": 0, "expiring": 1, "ok": 2, "external": 3, "none": 4}
    items.sort(key=lambda x: (order.get(x["state"], 9),
                              x["hours_left"] if x["hours_left"] is not None else 1e9))
    summary = {
        "devices": len(items),
        "managed": sum(1 for i in items if i["renewable"]),
        "expired": sum(1 for i in items if i["state"] == "expired"),
        "expiring": sum(1 for i in items if i["state"] == "expiring"),
        "ok": sum(1 for i in items if i["state"] == "ok"),
        "external": sum(1 for i in items if i["state"] == "external"),
        "unhealthy": sum(1 for i in items if i["health"].get("working") is False),
        "blocked": sum(1 for i in items if i["blocked"]),
        "due": sum(1 for i in items if i["in_work"] and i["renewable"]
                   and i["state"] in ("expired", "expiring")),
    }
    return {"items": items, "summary": summary,
            "router_ok": bool(devices), "inventory": len(inv)}


# ------------------------------------------------------------------------- renewals
def renew(idproxy: int, provider: str = DEFAULT_PROVIDER, days: int = RENEW_DAYS) -> dict:
    """Renew ONE proxy by idproxy (SPENDS MONEY) then refresh its expiry."""
    resp = proxy_vn.renew_proxy(int(idproxy), provider, days)
    ok = resp.get("status") == 100
    if ok:
        _cache["inv_ts"] = 0.0          # force inventory refresh on next read
        try:
            for r in proxy_vn.list_proxies(provider):
                if r.get("idproxy") == int(idproxy) and _epoch_to_iso(r.get("time")):
                    _mgmt_query(f"UPDATE fffbt.proxies SET expires_at="
                                f"{_lit(_epoch_to_iso(r['time']))} WHERE idproxy={int(idproxy)}")
                    break
        except Exception:
            pass
    return {"idproxy": int(idproxy), "ok": ok, "response": resp}


def renew_many(items: list[dict]) -> list[dict]:
    """items: [{idproxy, provider}]."""
    return [renew(int(i["idproxy"]), i.get("provider") or DEFAULT_PROVIDER) for i in items if i.get("idproxy")]


def broken_inwork_devices() -> list[dict]:
    """In-work (bound + not Blocked) devices whose proxy is NOT working — candidates
    for a fresh proxy. Covers both unhealthy proxies and devices with none."""
    return [i for i in status()["items"]
            if i["in_work"] and (i["health"].get("working") is not True)]


def replace_proxy_for_device(device_ip: str, provider: str = DEFAULT_PROVIDER,
                             days: int = BUY_DAYS, verify_wait: int = 8) -> dict:
    """Buy ONE fresh proxy (SPENDS money), record it, assign it to the device via the
    router, then health-check it. Returns the outcome (with the live check result)."""
    bought = proxy_vn.buy_proxies(provider, 1, days)
    if not bought:
        return {"ok": False, "device_ip": device_ip, "error": "buy returned no proxy"}
    p = bought[0]
    try:
        record_bought([p], provider)
    except Exception:
        pass
    parsed = proxy_vn.parse_proxy_string(p.get("proxy", ""))
    server = p.get("ip") or parsed.get("host")
    port = p.get("port") or parsed.get("port")
    user = p.get("user") or parsed.get("username")
    pwd = p.get("password") or parsed.get("password")
    try:
        assign = router_proxy.assign_proxy(device_ip, server, int(port), user, pwd)
    except Exception as e:
        return {"ok": False, "device_ip": device_ip, "idproxy": p.get("idproxy"),
                "proxy": f"{server}:{port}", "error": f"assign failed: {e}"}
    _time.sleep(verify_wait)                         # let the router apply + route
    try:
        check = router_proxy.check_proxy(device_ip, timeout=40)
    except Exception as e:
        check = {"error": str(e)}
    _cache["inv_ts"] = 0.0                            # new proxy -> refresh inventory
    working = bool(check.get("working") if isinstance(check, dict) else False)
    return {"ok": True, "working": working, "device_ip": device_ip,
            "idproxy": p.get("idproxy"), "provider": provider,
            "proxy": f"{server}:{port}", "username": user,
            "expires": _epoch_to_iso(p.get("time")), "assign": assign, "check": check}


def replace_broken_inwork(provider: str = DEFAULT_PROVIDER, days: int = BUY_DAYS,
                          limit: int | None = None, dry_run: bool = False) -> dict:
    """Replace the proxy on every in-work device whose proxy isn't working (SPENDS
    money, one buy per device). `limit` caps how many; dry_run just lists candidates."""
    cands = broken_inwork_devices()
    if limit:
        cands = cands[:limit]
    out = {"candidates": [{"device": c["device"], "account": c["account"],
                           "health": c["health"].get("working")} for c in cands],
           "results": [], "dry_run": dry_run}
    if not dry_run:
        for c in cands:
            out["results"].append(replace_proxy_for_device(c["ip"], provider, days))
    return out


def renew_due(dry_run: bool = False) -> dict:
    """Auto-renew proxies of IN-WORK (not Blocked) devices expiring in < 1 day."""
    st = status()
    due = [i for i in st["items"]
           if i["in_work"] and i["renewable"] and i["state"] in ("expired", "expiring")]
    out = {"candidates": [{"device": i["device"], "account": i["account"],
                           "idproxy": i["idproxy"], "hours_left": i["hours_left"],
                           "provider": i["provider"]} for i in due],
           "renewed": [], "dry_run": dry_run}
    if not dry_run:
        out["renewed"] = renew_many([{"idproxy": i["idproxy"], "provider": i["provider"]} for i in due])
    return out


if __name__ == "__main__":
    _load_env(str(ROOT / ".env"))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        st = status()
        print("summary:", st["summary"], "router_ok:", st["router_ok"], "inv:", st["inventory"])
        for i in st["items"]:
            if i["renewable"] or i["account"]:
                print(f"  {i['device']:20} {str(i['account'])[:18]:18} id={i['idproxy']} "
                      f"{i['state']:8} left={i['hours_left']}h health={i['health']['working']} "
                      f"blk={i['blocked']} work={i['in_work']}")
    elif cmd == "sync":
        print("synced inventory:", sync())
    elif cmd == "due":
        print(json.dumps(renew_due(dry_run=True), indent=2, ensure_ascii=False))
