#!/usr/bin/env python3
"""Recover broken ``link_platform`` values stamped by the capture bug.

The link-capture used to copy the top-left "newest trial tile" and trust it was
the just-posted reel; when the new reel lagged, many posts were saved with one
stale URL. dedup_link_platform.py NULLed every duplicated link, leaving the rows
as recovery targets. This script refills them:

  1. drive the account's device through its Trial-reels list, opening the reel
     player and walking it with swipe-ups, copying EACH reel's real link in
     order (newest -> oldest);
  2. order the account's posted/verify rows by published_at (newest -> oldest);
  3. VALIDATE the chronological assumption with anchors -- rows that still carry
     a (trusted, unique) link must line up 1:1 with the enumerated reels at the
     same positions. Only if every anchor matches do we trust the alignment;
  4. fill each NULL row with its position-matched reel link (dry-run by default).

Read-only on Instagram (opens reels, copies links; never posts). DB writes only
with --apply, only link_platform (status/published_at untouched), and only links
not already used (the unique index stays satisfied).

Usage:
  python scripts/recover_links.py --account quycohongbuiro828            # dry-run
  python scripts/recover_links.py --account quycohongbuiro828 --enum-only 8
  python scripts/recover_links.py --account quycohongbuiro828 --apply
  python scripts/recover_links.py --all                                  # dry-run all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "ADB_PATH",
    r"C:/Users/Admin/.genfarmer/python-3.9.0.amd64/Lib/site-packages/adbutils/binaries/adb.exe")

from scripts.post_trial import _lit, _load_env, _mgmt_query, link_exists  # noqa: E402
from scripts.publish_trial import (  # noqa: E402
    Traj, _by_rid, _by_text, _dismiss_blockers, _jxy, _navigate, _open_clean,
    _reach_trials_list, a11y_ok, parse_bounds, node_text, read_ui,
    recover_accessibility, shell, tap,
)

DEVICE_MAP = ROOT / "data" / "device_accounts.json"
MAX_REELS = 200


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def account_rows(account: str) -> list[dict]:
    """Posted/verify rows for an account, NEWEST first (the recovery targets +
    their anchors)."""
    return _mgmt_query(
        "SELECT id, name, link_platform, published_at, left(caption, 40) AS cap "
        "FROM fffbt.videos "
        f"WHERE posted_by = {_lit(account)} AND status IN ('posted', 'verify') "
        "ORDER BY published_at DESC NULLS LAST")


def affected_accounts() -> list[str]:
    rows = _mgmt_query(
        "SELECT DISTINCT posted_by FROM fffbt.videos "
        "WHERE status IN ('posted','verify') AND link_platform IS NULL "
        "AND posted_by IS NOT NULL ORDER BY posted_by")
    return [r["posted_by"] for r in rows if r.get("posted_by")]


def set_link(video_id: str, url: str) -> None:
    _mgmt_query(
        f"UPDATE fffbt.videos SET link_platform = {_lit(url)}, updated_at = now() "
        f"WHERE id = {_lit(video_id)};")


def resolve_serial(account: str) -> str | None:
    data = json.loads(DEVICE_MAP.read_text(encoding="utf-8"))
    for serial, acc in (data.get("devices") or {}).items():
        if acc == account:
            return serial
    return None


# ---------------------------------------------------------------------------
# Device: walk the Trial-reels player and copy every reel link in order
# ---------------------------------------------------------------------------
def _clean_reel_url(text: str) -> str | None:
    if not text:
        return None
    marker = "instagram.com/reel/"
    i = text.find(marker)
    if i == -1:
        return None
    tail = text[i + len(marker):]
    code = ""
    for ch in tail:
        if ch.isalnum() or ch in "-_":
            code += ch
        else:
            break
    return f"https://www.instagram.com/reel/{code}/" if code else None


async def _copy_current_reel_link(serial, traj) -> str | None:
    """From inside the reel player, open Share, Copy link, paste into the search
    box, read the URL back, then dismiss the share sheet. Returns the URL."""
    share = None
    for _ in range(8):                                     # wait for the player to settle
        nodes = await read_ui(serial)
        share = _by_rid(nodes, "direct_share_button") or _by_rid(nodes, "share_button")
        if share:
            break
        await asyncio.sleep(0.5)
    if not share:
        return None
    await tap(serial, _jxy(share), "reel Share", human=False)
    opened = False
    for _ in range(5):
        nodes = await read_ui(serial)
        if _by_text(nodes, "Copy link") or _by_rid(nodes, "search_edit_text"):
            opened = True
            break
        await asyncio.sleep(0.5)
    if not opened:
        return None
    copy = _by_text(nodes, "Copy link")
    url = None
    if copy:
        await tap(serial, _jxy(copy), "Copy link", human=False)
        # paste the fresh clipboard into the (freshly opened, empty) search box
        nodes = await read_ui(serial)
        sb = _by_rid(nodes, "search_edit_text")
        if sb:
            await tap(serial, _jxy(sb), "search box (focus)", human=False)
            await shell(serial, "input keyevent 279", timeout=10)          # PASTE
            await asyncio.sleep(0.5)
            nodes = await read_ui(serial)
            sf = _by_rid(nodes, "search_edit_text")
            url = _clean_reel_url(node_text(sf) if sf else "")
        if not url:
            for n in nodes:
                url = _clean_reel_url(node_text(n))
                if url:
                    break
    # Back out: the search box opened the keyboard ON TOP of the share sheet, so
    # one BACK only closes the keyboard. Keep pressing BACK until the reel player
    # (the ViewPager) is showing again -- otherwise the next swipe/copy lands on
    # the share sheet and the walk stalls.
    for _ in range(4):
        await shell(serial, "input keyevent 4", timeout=10)
        await asyncio.sleep(0.5)
        nodes = await read_ui(serial)
        if _by_rid(nodes, "clips_viewer_view_pager") or _by_rid(nodes, "direct_share_button"):
            break
    return url


async def _goto_trials_list(serial: str, traj: Traj):
    """Navigate to the Trial-reels grid; returns its bounds. Retries the whole
    open+navigate a few times (live nav occasionally lands on screen=unknown).
    Recovers the a11y service first if it has dropped (empty reads)."""
    if not await a11y_ok(serial):
        print(f"  [a11y] {serial} down -> recovering (toggle+reboot+reconnect)…")
        if not await recover_accessibility(serial, traj):
            raise RuntimeError("a11y down and recovery failed")
    for attempt in range(3):
        await _open_clean(serial, traj)
        ok, _ = await _navigate(
            serial, traj, step="rec/profile",
            find=lambda ns: _by_text(ns, "Profile", min_y=1550),
            target=lambda ns: bool(_by_text(ns, "Professional dashboard") or _by_rid(ns, "trials_list")),
            human=False)
        if ok:
            await _navigate(
                serial, traj, step="rec/dashboard",
                find=lambda ns: _by_text(ns, "Professional dashboard"),
                target=lambda ns: bool(_by_text(ns, "Your tools") or _by_rid(ns, "trials_list")),
                human=False)
        ok, _ = await _reach_trials_list(serial, traj, human=False)
        nodes = await read_ui(serial)
        for _ in range(3):
            if await _dismiss_blockers(serial, nodes, traj):
                nodes = await read_ui(serial)
            else:
                break
        tl = _by_rid(nodes, "trials_list")
        if tl:
            return parse_bounds(tl["bounds"])
        traj.log("goto_trials_retry", attempt=attempt)
    raise RuntimeError("could not reach trials_list")


async def _back_to_grid(serial: str, traj: Traj):
    """Press BACK until the trials grid is showing again; returns its bounds."""
    for _ in range(5):
        nodes = await read_ui(serial)
        tl = _by_rid(nodes, "trials_list")
        if tl:
            return parse_bounds(tl["bounds"])
        await shell(serial, "input keyevent 4", timeout=10)
        await asyncio.sleep(0.7)
    return await _goto_trials_list(serial, traj)


async def _tile_link(serial: str, traj: Traj, point) -> str | None:
    """Tap a grid tile, copy its reel link, and return to the grid."""
    await tap(serial, _jxy(point), "trial tile", human=False)
    await asyncio.sleep(1.6)
    url = await _copy_current_reel_link(serial, traj)
    await _back_to_grid(serial, traj)
    return url


async def enumerate_reels(serial: str, traj: Traj, max_reels: int = MAX_REELS) -> list[dict]:
    """Walk the Trial-reels GRID tile-by-tile (the viewer is single-item, so the
    grid is the only ordering), newest -> oldest, copying each reel's link. The
    grid is reverse-chronological, so the resulting order matches published_at.
    Scrolls down a row-pair at a time; dedups by URL; stops when a full screen
    yields nothing new."""
    box = await _goto_trials_list(serial, traj)
    col_w = (box[2] - box[0]) // 3
    row_h = int(col_w * 16 / 9)                            # trial tiles are ~9:16 portrait

    reels: list[dict] = []
    seen: set[str] = set()
    dry_screens = 0
    while len(reels) < max_reels:
        nodes = await read_ui(serial)
        tl = _by_rid(nodes, "trials_list")
        box = parse_bounds(tl["bounds"]) if tl else await _back_to_grid(serial, traj)
        rows_visible = max(1, (box[3] - box[1]) // row_h)
        new_this_screen = 0
        for r in range(rows_visible):
            cy = box[1] + r * row_h + row_h // 2
            if cy > box[3] - row_h // 3:                   # skip a cut-off bottom row
                continue
            for c in range(3):
                cx = box[0] + c * col_w + col_w // 2
                url = await _tile_link(serial, traj, (cx, cy))
                if not url or url in seen:
                    continue
                seen.add(url)
                reels.append({"url": url})
                new_this_screen += 1
                print(f"  reel[{len(reels)-1:>2}] {url}")
                if len(reels) >= max_reels:
                    break
            # the grid box can shift after re-entry; re-read before next row
            nodes = await read_ui(serial)
            tl = _by_rid(nodes, "trials_list")
            if tl:
                box = parse_bounds(tl["bounds"])
            if len(reels) >= max_reels:
                break
        if new_this_screen == 0:
            dry_screens += 1
            if dry_screens >= 2:
                traj.log("enum_end", reason="no new reels", total=len(reels))
                break
        else:
            dry_screens = 0
        # scroll down to reveal older reels (about two rows), keep position stable
        await shell(serial, f"input swipe 540 {box[1] + row_h + 200} 540 {box[1] + 100} 450", timeout=10)
        await asyncio.sleep(1.6)
    return reels


# ---------------------------------------------------------------------------
# Alignment (newest-first both sides) with anchor validation
# ---------------------------------------------------------------------------
def align(reels: list[dict], rows: list[dict]) -> dict:
    """Anchor-segmented restoration.

    Anchors are rows whose CURRENT link is present in the grid -- ground-truth
    (row <-> reel) pairs, since that row was demonstrably posted as that reel.
    They cut the timeline into segments. Inside a segment, the reels there must
    belong to the NULL rows there (both are strictly between the same two
    anchors, and the grid is reverse-chronological). When a segment has an EQUAL
    number of null rows and reels we fill them 1:1 in order (confident); when the
    counts differ we can't tell which reel is which, so those rows stay NULL.
    """
    reel_urls = [r["url"] for r in reels]
    reel_pos = {u: i for i, u in enumerate(reel_urls)}

    anchors = []                       # (row_index, reel_index)
    stale_link_rows = []               # rows whose link is NOT in the grid (aged out)
    for i, r in enumerate(rows):
        link = r.get("link_platform")
        if not link:
            continue
        if link in reel_pos:
            anchors.append((i, reel_pos[link]))
        else:
            stale_link_rows.append(i)
    # anchors must be monotonic in reel index (grid order == published order); a
    # drop means published_at and the grid disagree (e.g. a re-verify bumped
    # published_at) -- flag it so its neighbouring fills can be reviewed.
    inversions = [(anchors[k], anchors[k + 1]) for k in range(len(anchors) - 1)
                  if anchors[k + 1][1] <= anchors[k][1]]

    bounds = [(-1, -1)] + anchors + [(len(rows), len(reel_urls))]
    fills, ambiguous = [], []
    for k in range(len(bounds) - 1):
        r_lo, e_lo = bounds[k]
        r_hi, e_hi = bounds[k + 1]
        gap_rows = [i for i in range(r_lo + 1, r_hi) if not rows[i].get("link_platform")]
        gap_reels = list(range(e_lo + 1, e_hi))
        if not gap_rows:
            continue
        if len(gap_rows) == len(gap_reels):
            for ri, ei in zip(gap_rows, gap_reels):
                fills.append({"id": rows[ri]["id"], "row": ri, "name": rows[ri]["name"],
                              "published_at": rows[ri]["published_at"], "url": reel_urls[ei]})
        else:
            for ri in gap_rows:
                ambiguous.append({"id": rows[ri]["id"], "row": ri, "name": rows[ri]["name"],
                                  "published_at": rows[ri]["published_at"],
                                  "n_rows": len(gap_rows), "n_reels": len(gap_reels)})
    return {
        "n_reels": len(reels), "n_rows": len(rows), "anchors": len(anchors),
        "stale_link_rows": stale_link_rows, "inversions": inversions,
        "fills": fills, "ambiguous": ambiguous,
    }


# ---------------------------------------------------------------------------
def _cache_path(account: str) -> Path:
    return ROOT / "data" / f"reels_cache_{account}.json"


async def _get_reels(account, serial, traj, *, max_reels, use_cache) -> list[dict]:
    cache = _cache_path(account)
    if use_cache and cache.exists():
        reels = json.loads(cache.read_text(encoding="utf-8"))
        print(f"  loaded {len(reels)} reels from cache")
        return reels
    reels = await enumerate_reels(serial, traj, max_reels=max_reels)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(reels, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  enumerated {len(reels)} reels -> cached")
    return reels


async def recover_account(account: str, *, apply: bool, enum_only: int, max_reels: int,
                          use_cache: bool) -> None:
    serial = resolve_serial(account)
    print(f"\n=== {account}  device={serial} ===")
    if not serial:
        print("  [skip] no device bound in device_accounts.json")
        return
    traj = Traj(serial, tag=f"recover-{account}")
    reels = await _get_reels(account, serial, traj, max_reels=max_reels, use_cache=use_cache)
    if enum_only:
        return

    rows = account_rows(account)
    rep = align(reels, rows)
    print(f"  rows={rep['n_rows']} reels={rep['n_reels']} anchors={rep['anchors']} "
          f"stale_link_rows={len(rep['stale_link_rows'])} inversions={len(rep['inversions'])}")
    if rep["inversions"]:
        print("  [warn] anchor order vs grid order disagree near:", rep["inversions"][:5])
    print(f"  CONFIDENT fills: {len(rep['fills'])}   |   left NULL (ambiguous): {len(rep['ambiguous'])}")
    for f in rep["fills"]:
        print(f"    row {f['row']:>2} {f['name']:34} {str(f['published_at'])[:16]} -> {f['url']}")
    if rep["ambiguous"]:
        amb_groups = {}
        for a in rep["ambiguous"]:
            amb_groups.setdefault((a["n_rows"], a["n_reels"]), []).append(a["row"])
        print("  ambiguous gaps (rows!=reels), left NULL:")
        for (nr, ne), rws in sorted(amb_groups.items()):
            print(f"    {nr} rows vs {ne} reels -> rows {rws}")

    if not apply:
        print("  [dry-run] no writes. Re-run with --apply to write the CONFIDENT fills.")
        return
    written = 0
    for f in rep["fills"]:
        if link_exists(f["url"]):
            print(f"    [skip] {f['url']} already used -> not writing")
            continue
        set_link(f["id"], f["url"])
        written += 1
    print(f"  wrote {written} links ({len(rep['ambiguous'])} rows left NULL)")


async def main_async(args) -> None:
    _load_env()
    accounts = ([args.account] if args.account else affected_accounts())
    print(f"accounts to process: {accounts}")
    for acc in accounts:
        try:
            await recover_account(acc, apply=args.apply, enum_only=args.enum_only,
                                  max_reels=args.max_reels, use_cache=args.use_cache)
        except Exception as e:
            print(f"  [error] {acc}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--account", help="single account username")
    g.add_argument("--all", action="store_true", help="every affected account")
    ap.add_argument("--apply", action="store_true", help="write links (default: dry-run)")
    ap.add_argument("--use-cache", action="store_true",
                    help="reuse a saved reels_cache_<account>.json instead of re-driving the device")
    ap.add_argument("--enum-only", type=int, default=0, metavar="N",
                    help="just enumerate reels and cache them (read-only)")
    ap.add_argument("--max-reels", type=int, default=MAX_REELS)
    args = ap.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
