#!/usr/bin/env python3
"""Scale test: run N post_trial concurrently and report each outcome + any
agent exception, to find the host's safe concurrency.

Unlike the fleet (which discards post_trial output), this captures each child's
full stdout/stderr so a failing run's exact exception ("agent.run raised: …")
is visible. Uses threads + subprocess.run (robust on Windows; asyncio subprocess
needs the Proactor loop which conflicts with the adb selector loop).

Usage:
  python scripts/scale_test.py 5            # first 5 devices from the binding
  python scripts/scale_test.py 8 --stagger 15
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
VENV_PY = os.environ.get("LOOP_PY", str(ROOT / ".venv" / "Scripts" / "python.exe"))

_EXC_PAT = re.compile(r"(agent\.run raised|agent_exception|Traceback|GenFarmer connect failed|"
                      r"construction failed|TimeoutError|ConnectionError|RuntimeError|"
                      r"NotImplementedError|Errno|refused|timed out)")


def _one(serial: str, account: str, idx: int, stagger: int) -> dict:
    time.sleep(idx * stagger)  # staggered start
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    print(f"  [{idx}] start {account}@{serial}", flush=True)
    try:
        p = subprocess.run(
            [VENV_PY, str(ROOT / "scripts" / "post_trial.py"),
             "--device", serial, "--account", account, "--category", "trend"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=1800,
        )
        text = (p.stdout or "") + (p.stderr or "")
        rc = p.returncode
    except subprocess.TimeoutExpired as e:
        text = ((e.stdout or "") + (e.stderr or "")) if hasattr(e, "stdout") else ""
        rc = 124
    # Save each child's FULL output so an INFRA cause can be inspected afterwards.
    try:
        (ROOT / "data" / f"scale_out_{idx}_{serial.replace(':','_').replace('.','-')}.txt").write_text(
            text, encoding="utf-8")
    except Exception:
        pass
    m = re.search(r'"verdict":\s*"(\w+)"', text)
    verdict = m.group(1) if m else "?"
    m2 = re.search(r'"code":\s*("?\w+"?|null)', text)
    code = (m2.group(1).strip('"') if m2 else "?")
    exc_lines = [l.strip() for l in text.splitlines() if _EXC_PAT.search(l)]
    print(f"  [{idx}] done  {account}@{serial} rc={rc} verdict={verdict} code={code}", flush=True)
    return {"serial": serial, "account": account, "rc": rc, "verdict": verdict,
            "code": code, "exc": exc_lines[-4:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("count", type=int)
    ap.add_argument("--stagger", type=int, default=15, help="seconds between starts")
    args = ap.parse_args()

    devices = list(json.loads(BINDING.read_text(encoding="utf-8")).get("devices", {}).items())
    pick = devices[: args.count]
    print(f"scale test: {len(pick)} concurrent post_trial (stagger {args.stagger}s)")
    with ThreadPoolExecutor(max_workers=len(pick)) as ex:
        futs = [ex.submit(_one, s, a, i, args.stagger) for i, (s, a) in enumerate(pick)]
        results = [f.result() for f in futs]

    ok = [r for r in results if r["rc"] in (0, 2)]
    bad = [r for r in results if r["rc"] not in (0, 2)]
    print(f"\n===== SCALE TEST RESULT: {len(ok)}/{len(results)} ok =====")
    for r in results:
        tag = "OK " if r["rc"] in (0, 2) else "FAIL"
        print(f"  {tag} {r['account']:>20}@{r['serial']} rc={r['rc']} verdict={r['verdict']} code={r['code']}")
        for e in r["exc"]:
            print(f"        ! {e[:200]}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
