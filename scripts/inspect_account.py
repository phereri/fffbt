#!/usr/bin/env python3
"""Reconstruct the full action history (every tap / type / screen / deviation) for
an account or device, from the per-run trajectories the scripts write.

Each scripted run logs to trajectories/scripted/<ts>_<ip>_<account>/trajectory.jsonl:
every step_tap (with target + x,y), caption typing, screen transitions, blocker
dismissals, DEVIATIONs (with a screen-dump file), and the final result. This tool
replays them as a readable timeline so you can see exactly what was done on an
account — e.g. what preceded a login challenge.

Usage:
  python scripts/inspect_account.py <account-or-serial>      # e.g. anducphamkt013  or  192.168.5.50
  python scripts/inspect_account.py <q> --runs 3             # only the newest 3 runs
  python scripts/inspect_account.py <q> --raw                # raw JSONL events
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRAJ = ROOT / "trajectories" / "scripted"


def _runs_for(query: str) -> list[Path]:
    q = query.lower().replace(".", "_").replace(":", "_")
    out = []
    for d in sorted(TRAJ.glob("*/"), reverse=True):
        jf = d / "trajectory.jsonl"
        if not jf.exists():
            continue
        # match on the dir name (has serial_account) or on any event's account/serial
        hay = d.name.lower()
        if q in hay:
            out.append(jf); continue
        try:
            for line in jf.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                if query.lower() in str(e.get("account", "")).lower() \
                        or query.lower() in str(e.get("serial", "")).lower():
                    out.append(jf); break
        except Exception:
            pass
    return out


def _t(rel: float) -> str:
    rel = int(rel)
    return f"{rel // 60:02d}:{rel % 60:02d}"


def _line(e: dict, t0: float) -> str | None:
    ev = e.get("event", "")
    ts = _t((e.get("ts", t0) or t0) - t0)
    scr = e.get("screen", "")
    step = e.get("step", "")
    if ev == "step_tap":
        xy = e.get("xy"); on = e.get("on", "")
        return f"[{ts}] TAP   {step:22} “{on}” @{tuple(xy) if xy else '?'}   ({scr})"
    if ev == "step_ok":
        return f"[{ts}] ✓     reached {step:16} → {scr}"
    if ev == "step_wait":
        return f"[{ts}] …     waiting at {step:14} ({scr})"
    if ev == "step_scroll":
        return f"[{ts}] ↕     scroll {step:18} ({scr})"
    if ev in ("type_per_char",):
        return f"[{ts}] TYPE  caption — {e.get('units','?')} graphemes, {e.get('emoji','?')} emoji, base {e.get('base_ms','?')}ms/char"
    if ev == "caption":
        return f"[{ts}] TYPE  caption landed={e.get('landed')}  ({e.get('info','')})"
    if ev == "caption_fallback_oneshot":
        return f"[{ts}] TYPE  per-char incomplete → one-shot insert (field {e.get('field_len')}/{e.get('want_len')})"
    if ev == "permission_deny":
        return f"[{ts}] DISMISS permission “{e.get('message','')}” → Don't allow"
    if ev == "nux_dismiss":
        return f"[{ts}] DISMISS NUX → Continue"
    if ev == "interstitial_dismiss":
        return f"[{ts}] DISMISS interstitial “{e.get('headline','')}”"
    if ev == "ime_set":
        return f"[{ts}] IME   → {e.get('to')} (was {e.get('was')})"
    if ev == "trial_check":
        return f"[{ts}] CHECK trial composer={e.get('is_trial')}"
    if ev == "publish_result":
        return f"[{ts}] SHARE published={e.get('ok')} screen={scr}"
    if ev == "DEVIATION":
        return f"[{ts}] ⚠ DEVIATION {step} on {scr} — {e.get('note','')}\n          dump: {e.get('dump','')}"
    if ev in ("hard_stop", "login_challenge"):
        return f"[{ts}] 🛑 STOP {e.get('reason','')} ({e.get('marker','')})"
    if ev == "publish_fail":
        return f"[{ts}] ✗ publish failed at stage={e.get('stage')} {e.get('detail','')}"
    if ev == "prepare_done":
        return f"[{ts}] PREP  ok={e.get('ok')} {e.get('seconds','')}s"
    if ev.startswith("capture_route"):
        return f"[{ts}] LINK  {ev} {e.get('route','')} {e.get('url','')}"
    if ev == "run_start":
        return None  # used for the header
    if ev == "run_result":
        return f"[{ts}] ════ RESULT {e.get('verdict')} rc={e.get('rc')} url={e.get('post_url') or '—'} deviations={e.get('deviations')}"
    if ev == "publish_start":
        return f"[{ts}] START publish (caption {e.get('caption_len')} chars, humanize={e.get('humanize')})"
    return f"[{ts}] {ev} {json.dumps({k:v for k,v in e.items() if k not in ('event','ts','serial','seq')}, ensure_ascii=False)}"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: inspect_account.py <account-or-serial> [--runs N] [--raw]")
        return 2
    query = args[0]
    raw = "--raw" in sys.argv
    nruns = None
    if "--runs" in sys.argv:
        try:
            nruns = int(sys.argv[sys.argv.index("--runs") + 1])
        except Exception:
            nruns = None

    runs = _runs_for(query)
    if nruns:
        runs = runs[:nruns]
    if not runs:
        print(f"no scripted trajectories match {query!r} under {TRAJ}")
        print("(our scripts log every run here; if a device has none, no scripted run touched it)")
        return 1

    print(f"{len(runs)} run(s) match {query!r}\n")
    for jf in runs:
        events = []
        for line in jf.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except Exception:
                pass
        if not events:
            continue
        t0 = events[0].get("ts", 0) or 0
        start = next((e for e in events if e.get("event") == "run_start"), events[0])
        acct = start.get("account", "?")
        serial = events[0].get("serial", "?")
        print(f"══════ {jf.parent.name}")
        print(f"       account={acct}  serial={serial}  events={len(events)}")
        if raw:
            for e in events:
                print("   " + json.dumps(e, ensure_ascii=False))
        else:
            for e in events:
                ln = _line(e, t0)
                if ln:
                    print("  " + ln)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
