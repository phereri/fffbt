#!/usr/bin/env python3
"""Bind proxies to ACCOUNTS in the automation schema (not to device IPs).

The LAN router stays the SOURCE OF TRUTH for which proxy is actually applied to a
device. This records the durable account<->proxy binding in automation.* so a
proxy follows its ACCOUNT across device / IP changes, instead of being pinned to
an Android device address.

  * automation.proxies            <- the proxy inventory (host/port/user/pass/idproxy)
  * automation.accounts.proxy_id  <- each account's bound proxy (FK -> automation.proxies)

The binding is derived from: router (device->proxy) + roster (device->account) +
fffbt.proxies (the full proxy record incl. password, matched by idproxy).

CLI:
  python scripts/account_proxy_store.py --migrate   # add columns/indexes + sync
  python scripts/account_proxy_store.py --sync
  python scripts/account_proxy_store.py --show
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from account_store import _load_env, _mgmt_query  # noqa: E402
import proxy_manager  # noqa: E402
import router_proxy  # noqa: E402


def ensure_schema() -> None:
    """Add idproxy/source to automation.proxies + proxy_id to automation.accounts
    (idempotent). proxy_id FKs the durable account<->proxy link."""
    _mgmt_query(
        "ALTER TABLE automation.proxies ADD COLUMN IF NOT EXISTS idproxy integer;"
        "ALTER TABLE automation.proxies ADD COLUMN IF NOT EXISTS source text;"
        "CREATE UNIQUE INDEX IF NOT EXISTS proxies_idproxy_uq "
        "  ON automation.proxies (idproxy) WHERE idproxy IS NOT NULL;"
        "ALTER TABLE automation.accounts ADD COLUMN IF NOT EXISTS proxy_id uuid "
        "  REFERENCES automation.proxies(id);")


def _account_proxy_rows() -> list[dict]:
    """[{username, idproxy, host, port, puser, ppass, protocol, country}] for every
    account whose CURRENT device (per the router) has a managed proxy. The full proxy
    record (incl. password) comes from the LIVE proxy.vn inventory, matched to the
    router endpoint by (port, username) — independent of the fffbt.proxies mirror's
    freshness."""
    st = proxy_manager.status()
    inv = proxy_manager._inventory()           # keyed by (port, username); has password + host + idproxy
    rows = []
    for i in st["items"]:
        acct, px = i.get("account"), i.get("proxy")
        if not acct or not px or not i.get("idproxy"):
            continue
        m = inv.get((px.get("port"), px.get("username")))
        if not m or not m.get("idproxy"):
            continue
        rows.append({"username": acct, "idproxy": int(m["idproxy"]),
                     "host": m.get("host") or px.get("server"),
                     "port": int(m["port"]), "puser": m.get("username"),
                     "ppass": m.get("password"), "protocol": "socks5",
                     "country": "vietnam"})
    return rows


def sync() -> dict:
    """Upsert the bound proxies into automation.proxies (by idproxy) and set
    automation.accounts.proxy_id for each account. One atomic statement: the proxy
    rows exist before the FK update. Best-effort caller wraps in try/except."""
    rows = _account_proxy_rows()
    if not rows:
        return {"proxies_upserted": 0, "accounts_bound": 0, "skipped": "no router pairs"}
    payload = json.dumps(rows).replace("'", "''")
    sql = f"""
    WITH input AS (
      SELECT * FROM jsonb_to_recordset('{payload}'::jsonb)
        AS t(username text, idproxy int, host text, port int,
             puser text, ppass text, protocol text, country text)
    ),
    ups AS (
      INSERT INTO automation.proxies
        (host, port, protocol, username, password, country_code, status, idproxy, source)
      SELECT DISTINCT ON (i.idproxy)
             i.host, i.port, COALESCE(i.protocol, 'socks5'), i.puser, i.ppass,
             i.country, 'active', i.idproxy, 'provy.vn'
      FROM input i
      ORDER BY i.idproxy
      ON CONFLICT (idproxy) WHERE idproxy IS NOT NULL DO UPDATE
        SET host = EXCLUDED.host, port = EXCLUDED.port, protocol = EXCLUDED.protocol,
            username = EXCLUDED.username, password = EXCLUDED.password,
            country_code = EXCLUDED.country_code, source = EXCLUDED.source,
            updated_at = now()
      RETURNING id, idproxy
    ),
    bound AS (
      UPDATE automation.accounts a
      SET proxy_id = ups.id, updated_at = now()
      FROM input i JOIN ups ON ups.idproxy = i.idproxy
      WHERE a.platform = 'instagram' AND a.username = i.username
      RETURNING a.id
    )
    SELECT (SELECT count(*) FROM ups)   AS proxies_upserted,
           (SELECT count(*) FROM bound) AS accounts_bound;
    """
    r = _mgmt_query(sql)
    return r[0] if r and isinstance(r[0], dict) else {}


def apply_account_proxies(*, serials=None, dry_run: bool = False) -> list:
    """Reconcile devices to their accounts' RECORDED proxy (DB -> router): for every
    account with a bound device + recorded proxy whose CURRENT router proxy differs,
    assign the recorded proxy via the router. This is how a proxy follows its account
    across IP changes / device moves. ``serials`` limits the scope (e.g. the devices a
    bind just touched); None = all bound. Assigning spends nothing (only renew does).
    Returns [(serial, account, 'host:port')] of the (would-be) changes."""
    rows = _mgmt_query(
        "SELECT a.bound_serial AS serial, a.username, p.host, p.port, "
        "p.username AS puser, p.password AS ppass, p.protocol "
        "FROM automation.accounts a JOIN automation.proxies p ON p.id = a.proxy_id "
        "WHERE a.platform = 'instagram' AND a.bound_serial IS NOT NULL "
        "AND p.host IS NOT NULL AND p.port IS NOT NULL")
    want_set = set(serials) if serials else None
    rows = [r for r in rows if want_set is None or r.get("serial") in want_set]
    if not rows:
        return []
    cur: dict = {}
    try:
        for d in router_proxy.list_devices(timeout=25):
            px = d.get("proxy") or {}
            cur[d.get("ip")] = (px.get("port"), px.get("username"))
    except Exception as e:
        print(f"[apply] router unreachable, nothing applied: {e}")
        return []
    applied = []
    for r in rows:
        ip = router_proxy.serial_to_ip(r["serial"])
        want = (int(r["port"]), r.get("puser"))
        if cur.get(ip) == want:
            continue                                   # device already has the recorded proxy
        applied.append((r["serial"], r["username"], f"{r['host']}:{r['port']}"))
        if not dry_run:
            try:
                router_proxy.assign_proxy(ip, r["host"], int(r["port"]), r.get("puser"),
                                          r.get("ppass"), proxy_type=(r.get("protocol") or "socks5"))
            except Exception as e:
                print(f"[apply] {r['serial']} ({r['username']}): assign failed: {e}")
    return applied


def show() -> None:
    rows = _mgmt_query(
        "SELECT a.username, p.host, p.port, p.idproxy, p.status "
        "FROM automation.accounts a JOIN automation.proxies p ON p.id = a.proxy_id "
        "WHERE a.platform = 'instagram' ORDER BY a.username LIMIT 200")
    print(f"account<->proxy bindings in automation: {len(rows)}")
    for r in rows[:25]:
        print(f"  {r.get('username'):26} -> {r.get('host')}:{r.get('port')} "
              f"idproxy={r.get('idproxy')} [{r.get('status')}]")


def main(argv=None) -> int:
    _load_env()
    args = set(argv if argv is not None else sys.argv[1:])
    if "--migrate" in args:
        ensure_schema()
        print("schema: automation.proxies.idproxy/source + automation.accounts.proxy_id ensured")
    if "--migrate" in args or "--sync" in args:
        res = sync()
        print(f"sync: proxies_upserted={res.get('proxies_upserted')} "
              f"accounts_bound={res.get('accounts_bound')}")
    if "--apply" in args or "--apply-dry-run" in args:
        dry = "--apply-dry-run" in args
        changes = apply_account_proxies(dry_run=dry)
        print(f"apply{' (dry-run)' if dry else ''}: {len(changes)} device(s) "
              f"{'would be' if dry else ''} re-pointed to their account's recorded proxy")
        for serial, acct, hp in changes[:30]:
            print(f"  {serial:24} {acct:24} -> {hp}")
    if "--show" in args or not args:
        show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
