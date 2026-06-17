#!/usr/bin/env python3
"""Read-only inspection of fffbt.videos via the Supabase Management API (PAT).

No DB driver required; uses urllib + SUPABASE_PAT, matching scheduler.cli.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


def q(project_ref: str, pat: str, sql: str):
    url = f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
    req = urllib.request.Request(
        url,
        data=json.dumps({"query": sql}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "User-Agent": "fffbt-cli/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Management API query failed ({e.code}): {detail}") from None


def main() -> int:
    load_env()
    ref = os.environ.get("SUPABASE_PROJECT_REF", "")
    pat = os.environ.get("SUPABASE_PAT", "")
    if not ref or not pat:
        print("missing SUPABASE_PROJECT_REF or SUPABASE_PAT", file=sys.stderr)
        return 1

    print("=== columns of fffbt.videos ===")
    cols = q(ref, pat, """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='fffbt' AND table_name='videos'
        ORDER BY ordinal_position
    """)
    for c in cols:
        print(f"  {c['column_name']:<28} {c['data_type']:<28} "
              f"null={c['is_nullable']} default={c['column_default']}")

    print("\n=== status distribution ===")
    for r in q(ref, pat, "SELECT status, count(*) AS n FROM fffbt.videos GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']!r:<24} {r['n']}")

    print("\n=== category distribution ===")
    for r in q(ref, pat, "SELECT category, count(*) AS n FROM fffbt.videos GROUP BY category ORDER BY n DESC"):
        print(f"  {r['category']!r:<24} {r['n']}")

    print("\n=== one sample new/trend row (if any) ===")
    sample = q(ref, pat, """
        SELECT * FROM fffbt.videos
        WHERE status='new' AND category='trend'
        ORDER BY 1 LIMIT 1
    """)
    print(json.dumps(sample, indent=2, ensure_ascii=False, default=str))

    print("\n=== any CHECK constraints on fffbt.videos ===")
    cons = q(ref, pat, """
        SELECT con.conname, pg_get_constraintdef(con.oid) AS def
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        WHERE ns.nspname='fffbt' AND rel.relname='videos'
    """)
    for c in cons:
        print(f"  {c['conname']}: {c['def']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
