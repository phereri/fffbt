#!/usr/bin/env python3
"""Distil a MobileRun trajectory into a clean, script-friendly action log.

Every agent run already writes a full trajectory to ``trajectories/<ts>/``
(``trajectory.json`` = the agent's events incl. taps with coordinates,
``ui_states/`` = the a11y tree per planner cycle, ``screenshots/``). That format
is verbose. This tool flattens one run into an ordered ``actions.jsonl`` of just
the executed actions:

  {seq, action, tool, target_text, target_class, x, y, success, error, subgoal}

so the movement trajectory is easy to read and to turn into a deterministic
script (which screen → which tap by text/resource-id/coords reaches the next).

It also tags the run with the account/device by matching the trajectory's start
time to the nearest ``claim`` event in ``data/fleet_events.jsonl``.

Usage:
  python scripts/extract_actions.py                 # newest trajectory
  python scripts/extract_actions.py <traj_dir>      # a specific one
  python scripts/extract_actions.py --all           # every trajectory
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRAJ_DIR = ROOT / "trajectories"
EVENTS = ROOT / "data" / "fleet_events.jsonl"

# "Clicked on Text: 'Profile' | Class: FrameLayout | Type: unknown | Coordinates: (970, 1732)"
_SUMMARY = re.compile(
    r"Text:\s*'(?P<text>.*?)'.*?Class:\s*(?P<cls>[^|]*?)\s*(?:\|.*?)?Coordinates:\s*\((?P<x>-?\d+),\s*(?P<y>-?\d+)\)",
    re.DOTALL,
)


def _dir_ts(d: Path) -> str:
    # trajectory dir name like 20260619_175025_581e792d -> ISO-ish for matching
    m = re.match(r"(\d{8})_(\d{6})", d.name)
    if not m:
        return ""
    d8, t6 = m.group(1), m.group(2)
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}T{t6[:2]}:{t6[2:4]}:{t6[4:6]}"


def _tag_account(traj_ts: str) -> dict:
    """Nearest claim event (account/device) at/just-before the trajectory start."""
    if not traj_ts or not EVENTS.exists():
        return {}
    best = {}
    for line in EVENTS.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("type") in ("claim", "stage_start") and e.get("ts", "") <= traj_ts + "Z":
            best = {"account": e.get("account"), "device": e.get("device")}
    return best


def extract(d: Path) -> list[dict]:
    tj = d / "trajectory.json"
    if not tj.exists():
        return []
    try:
        arr = json.loads(tj.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    out: list[dict] = []
    subgoal = None
    seq = 0
    for e in arr:
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t in ("ManagerPlanDetailsEvent", "ExecutorActionEvent"):
            sg = e.get("subgoal") or e.get("thought")
            if sg:
                subgoal = str(sg)[:160]
        if t == "ExecutorActionResultEvent":
            seq += 1
            summary = str(e.get("summary") or "")
            act = e.get("action")
            row = {
                "seq": seq,
                "action": (act.get("action") if isinstance(act, dict) else act),
                "index": (act.get("index") if isinstance(act, dict) else None),
                "success": e.get("success"),
                "error": (str(e.get("error"))[:160] if e.get("error") else None),
                "subgoal": subgoal,
                "summary": summary[:160] or None,
            }
            m = _SUMMARY.search(summary)
            if m:
                row["target_text"] = m.group("text")
                row["target_class"] = m.group("cls").strip()
                row["x"] = int(m.group("x"))
                row["y"] = int(m.group("y"))
            out.append(row)
    return out


def main() -> int:
    args = sys.argv[1:]
    if "--all" in args:
        dirs = sorted([p for p in TRAJ_DIR.glob("*/") if (p / "trajectory.json").exists()])
    elif args:
        dirs = [Path(args[0])]
    else:
        dirs = sorted([p for p in TRAJ_DIR.glob("*/") if (p / "trajectory.json").exists()])[-1:]

    for d in dirs:
        actions = extract(d)
        if not actions:
            continue
        tag = _tag_account(_dir_ts(d))
        header = {"trajectory": d.name, **tag, "n_actions": len(actions)}
        (d / "actions.jsonl").write_text(
            "\n".join([json.dumps(header, ensure_ascii=False)]
                      + [json.dumps(a, ensure_ascii=False) for a in actions]) + "\n",
            encoding="utf-8",
        )
        print(f"\n=== {d.name}  {tag.get('account','?')}@{tag.get('device','?')}  ({len(actions)} actions) ===")
        for a in actions:
            tgt = a.get("target_text")
            coord = f"({a['x']},{a['y']})" if "x" in a else ""
            ok = "ok" if a.get("success") else f"FAIL {a.get('error') or ''}"
            print(f"  {a['seq']:>2}. {str(a.get('action')):<8} {('['+tgt+']') if tgt else '':<28} {coord:<12} {ok}")
            if a.get("subgoal"):
                print(f"       ↳ {a['subgoal']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
