#!/usr/bin/env python3
"""proxy.vn API v2 client: buy / renew / list SOCKS5 proxies.

Docs: https://proxy.vn/?home=apiv2. Only Viettel / VNPT / FPT are used. The API key
comes from PROXY_VN_KEY (falls back to the shared account key). Every call returns
parsed JSON; helpers filter to successful rows (status == 100).

Money note: buy_proxies and renew_proxy SPEND money. list_proxies is free.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

KEY = os.environ.get("PROXY_VN_KEY", "ALpWJKagrDNcAZTdtvWISI")
BASE = "https://proxy.vn/apiv2"
PROVIDERS = ("Viettel", "VNPT", "FPT")          # the only allowed loaiproxy values


def _call(endpoint: str, **params) -> object:
    params = {"key": KEY, **params}
    url = f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "fffbt-proxy/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _as_list(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def buy_proxies(loaiproxy: str = "Viettel", count: int = 10, days: int = 30,
                proxy_type: str = "SOCKS5") -> list[dict]:
    """Buy `count` proxies (SPENDS MONEY). Returns the rows with status == 100, each
    holding idproxy / ip / port / user / password / proxy / time (expiry epoch)."""
    if loaiproxy not in PROVIDERS:
        raise ValueError(f"loaiproxy must be one of {PROVIDERS}, got {loaiproxy!r}")
    data = _call("muaproxy.php", loaiproxy=loaiproxy, soluong=int(count),
                 ngay=int(days), type=proxy_type, user="random", password="random")
    return [x for x in _as_list(data) if x.get("status") == 100]


def renew_proxy(idproxy: int, loaiproxy: str = "Viettel", days: int = 2) -> dict:
    """Renew one proxy by idproxy for `days` (SPENDS MONEY). Returns the raw response
    (status == 100 on success)."""
    if loaiproxy not in PROVIDERS:
        raise ValueError(f"loaiproxy must be one of {PROVIDERS}, got {loaiproxy!r}")
    data = _call("giahanproxy.php", loaiproxy=loaiproxy, idproxy=int(idproxy), ngay=int(days))
    return data if isinstance(data, dict) else (_as_list(data)[0] if _as_list(data) else {})


def list_proxies(loaiproxy: str = "Viettel", idproxy: str = "all") -> list[dict]:
    """List active proxies for a provider (FREE). Each row has idproxy / ip / proxy
    ('host:port:user:pass') / type / time (expiry epoch)."""
    if loaiproxy not in PROVIDERS:
        raise ValueError(f"loaiproxy must be one of {PROVIDERS}, got {loaiproxy!r}")
    return [x for x in _as_list(_call("listproxy.php", loaiproxy=loaiproxy, idproxy=idproxy))
            if x.get("status", 100) == 100]


def parse_proxy_string(s: str) -> dict:
    """'host:port:user:pass' -> {host, port, username, password} (best-effort)."""
    parts = (s or "").split(":")
    if len(parts) < 4:
        return {}
    return {"host": parts[0], "port": int(parts[1]) if parts[1].isdigit() else None,
            "username": parts[2], "password": parts[3]}


if __name__ == "__main__":   # quick manual check (free): list active proxies
    import sys
    prov = sys.argv[1] if len(sys.argv) > 1 else "Viettel"
    rows = list_proxies(prov)
    print(f"{prov}: {len(rows)} active")
    for r in rows[:10]:
        print(" ", r.get("idproxy"), r.get("ip"), r.get("proxy"), "exp_epoch=", r.get("time"))
