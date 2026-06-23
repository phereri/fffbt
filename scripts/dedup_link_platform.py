#!/usr/bin/env python3
"""De-duplicate fffbt.videos.link_platform and enforce uniqueness.

A capture bug stamped many distinct reels with one tracking URL (the "newest
trial tile" was not actually the just-posted reel). A duplicated link is
untrustworthy for EVERY row that carries it, so this migration:

  1. backs up every current (id, posted_by, name, link_platform, published_at)
     to data/link_dedup_backup.json (so nothing is lost and recovery has the
     full prior state);
  2. NULLs link_platform on every row whose link is shared by >1 row (empties
     are allowed; the rows become recovery targets for recover_links.py);
  3. creates a partial UNIQUE INDEX on link_platform (WHERE NOT NULL) so a
     newly captured link can never equal one already saved.

Dry-run by default (prints what it WOULD do). Pass --apply to mutate. Only ever
touches fffbt.videos.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "data" / "link_dedup_backup.json"
INDEX_NAME = "videos_link_platform_uniq"


def _load_env() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _q(sql: str):
    ref = os.environ["SUPABASE_PROJECT_REF"]
    pat = os.environ["SUPABASE_PAT"]
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": sql}).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json",
                 "User-Agent": "fffbt-dedup/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
        return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Management API ({e.code}): {detail}") from None


# group of links shared by >1 row (the bug fingerprint)
_DUP_LINKS = (
    "select link_platform from fffbt.videos "
    "where link_platform is not null group by link_platform having count(*) > 1"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually mutate (default: dry-run)")
    args = ap.parse_args()
    _load_env()

    dup_groups = _q(_DUP_LINKS)
    n_groups = len(dup_groups)
    rows_to_null = _q(
        f"select count(*) c from fffbt.videos where link_platform in ({_DUP_LINKS})")[0]["c"]
    keep = _q(
        "select count(*) c from fffbt.videos where link_platform is not null "
        f"and link_platform not in ({_DUP_LINKS})")[0]["c"]
    accounts = _q(
        "select count(distinct posted_by) c from fffbt.videos "
        f"where link_platform in ({_DUP_LINKS}) and posted_by is not null")[0]["c"]

    print(f"duplicate link groups        : {n_groups}")
    print(f"rows to NULL (untrustworthy) : {rows_to_null}  across {accounts} accounts")
    print(f"rows kept (unique links)     : {keep}")

    if not args.apply:
        print("\n[dry-run] no changes made. Re-run with --apply to back up, null dups, and add the unique index.")
        return 0

    # 1) backup EVERY current non-null link row
    snapshot = _q(
        "select id, posted_by, name, link_platform, published_at "
        "from fffbt.videos where link_platform is not null order by posted_by, published_at")
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nbacked up {len(snapshot)} rows -> {BACKUP}")

    # 2) null the duplicated links
    nulled = _q(
        f"update fffbt.videos set link_platform = null, updated_at = now() "
        f"where link_platform in ({_DUP_LINKS}) returning id")
    print(f"nulled link_platform on {len(nulled)} rows")

    # 3) enforce uniqueness going forward
    _q(f"create unique index if not exists {INDEX_NAME} "
       f"on fffbt.videos (link_platform) where link_platform is not null")
    idx = _q("select indexname from pg_indexes where schemaname='fffbt' "
             f"and tablename='videos' and indexname='{INDEX_NAME}'")
    print(f"unique index present: {bool(idx)} ({INDEX_NAME})")

    remaining_dups = _q(_DUP_LINKS)
    print(f"remaining duplicate groups   : {len(remaining_dups)} (expect 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
