#!/usr/bin/env python3
"""Client for the on-LAN Proxy Router API (http://192.168.5.1:9009).

The router is the source of truth for which proxy is bound to which phone and for
live proxy health. Devices are keyed by LAN IP (== the adb serial minus ':5555').

Endpoints used:
  GET  /api/devices              -> [{ip, proxy:{type,server,port,username}, proxy_health, online, ...}]
  GET  /api/proxy/{ip}/check     -> live health check for one device's proxy
  POST /api/proxy                -> assign/replace a device's proxy (ProxyUpsertRequest)
  POST /api/batch_assign         -> bulk assign
"""
from __future__ import annotations

import json
import os
import urllib.request

BASE = os.environ.get("PROXY_ROUTER_URL", "http://192.168.5.1:9009").rstrip("/")


def _req(method: str, path: str, body: dict | None = None, timeout: int = 20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "User-Agent": "fffbt-proxy/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def serial_to_ip(serial: str) -> str:
    """'192.168.5.11:5555' -> '192.168.5.11'."""
    return (serial or "").split(":", 1)[0]


def list_devices(timeout: int = 20) -> list[dict]:
    """Every device the router knows, each with its bound proxy + live health."""
    out = _req("GET", "/api/devices", timeout=timeout)
    if isinstance(out, dict):
        out = out.get("devices", out)
    return out if isinstance(out, list) else []


def check_proxy(device_ip: str, timeout: int = 30) -> dict:
    """Force a live health check of the proxy bound to one device."""
    return _req("GET", f"/api/proxy/{device_ip}/check", timeout=timeout)


def assign_proxy(device_ip: str, server: str, port: int, username: str, password: str,
                 *, proxy_type: str = "socks5") -> dict:
    """Bind (or replace) a device's proxy."""
    return _req("POST", "/api/proxy", {
        "ip": device_ip, "type": proxy_type, "protocol": proxy_type,
        "server": server, "port": int(port), "username": username,
        "password": password, "is_change": True, "remove": False})


def batch_assign(assignments: list[dict]) -> dict:
    """Bulk bind: each item = {ip(device), server, port, username, password}."""
    return _req("POST", "/api/batch_assign", {"assignments": assignments})


def remove_proxy(device_ip: str) -> dict:
    """Unbind (remove) the proxy from a device."""
    return _req("DELETE", f"/api/proxy/{device_ip}")


if __name__ == "__main__":
    devs = list_devices()
    print(f"router: {len(devs)} devices")
    for d in devs[:15]:
        px = d.get("proxy") or {}
        h = d.get("proxy_health") or {}
        print(f"  {d.get('ip'):16} proxy={px.get('server')}:{px.get('port')} "
              f"user={px.get('username')} health={h.get('working')} ({h.get('latency_ms')}ms)")
