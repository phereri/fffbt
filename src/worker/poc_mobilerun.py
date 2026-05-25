"""Mobilerun PoC — verify GenFarmer REST API connectivity and readiness.

Checks:
  1. GenFarmer REST API reachable (GET /backend/auth/me)
  2. Automation apps available (GET /automation/apps)
  3. Can resolve userId for task creation

Usage:
    python -m src.worker.poc_mobilerun \
        [--genfarmer-url http://127.0.0.1:55554] \
        [--artifacts-dir ./.artifacts]

No external dependencies required (stdlib only).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _log(check: str, passed: bool, detail: str = "") -> dict:
    status = "PASS" if passed else "FAIL"
    msg = f"[{status}] {check}"
    if detail:
        msg += f" — {detail}"
    print(msg, file=sys.stderr)
    return {"check": check, "passed": passed, "detail": detail}


def _get_json(url: str) -> dict:
    req = Request(url, headers={"Accept": "application/json"})
    resp = urlopen(req, timeout=10)
    return json.loads(resp.read())


def run_poc(
    genfarmer_url: str,
    artifacts_dir: str,
) -> list[dict]:
    results: list[dict] = []
    out = Path(artifacts_dir) / "poc_mobilerun" / _ts()
    out.mkdir(parents=True, exist_ok=True)

    # --- Check 1: GenFarmer reachable ---
    user_id = None
    try:
        data = _get_json(f"{genfarmer_url}/backend/auth/me")
        (out / "auth_me.json").write_text(json.dumps(data, indent=2))
        user_id = data.get("id") or data.get("userId")
        results.append(_log("genfarmer_reachable", True, f"user_id={user_id}"))
    except Exception as exc:
        results.append(_log("genfarmer_reachable", False, str(exc)))
        return results

    # --- Check 2: Automation apps listed ---
    try:
        data = _get_json(f"{genfarmer_url}/automation/apps")
        (out / "automation_apps.json").write_text(json.dumps(data, indent=2))
        apps = data if isinstance(data, list) else data.get("data", data.get("items", []))
        count = len(apps) if isinstance(apps, list) else "?"
        app_names = []
        if isinstance(apps, list):
            app_names = [a.get("name", "?") for a in apps[:5]]
        results.append(_log("automation_apps", True, f"count={count} names={app_names}"))
    except Exception as exc:
        results.append(_log("automation_apps", False, str(exc)))

    # --- Check 3: userId resolved ---
    if user_id is not None:
        results.append(_log("user_id_resolved", True, f"userId={user_id} (needed for task/run creation)"))
    else:
        results.append(_log("user_id_resolved", False, "could not extract userId from /backend/auth/me"))

    (out / "poc_summary.json").write_text(json.dumps(results, indent=2))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Mobilerun/GenFarmer PoC — FFF-27")
    parser.add_argument(
        "--genfarmer-url",
        default=os.environ.get("GENFARMER_BASE_URL", "http://127.0.0.1:55554"),
    )
    parser.add_argument(
        "--artifacts-dir",
        default=os.environ.get("ARTIFACTS_DIR", "./.artifacts"),
    )
    args = parser.parse_args()

    results = run_poc(args.genfarmer_url, args.artifacts_dir)

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"\n{'='*40}", file=sys.stderr)
    print(f"Mobilerun PoC: {passed}/{total} checks passed", file=sys.stderr)

    print(json.dumps({"checks": results, "passed": passed, "total": total}, indent=2))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
