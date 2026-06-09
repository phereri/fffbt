"""Bounded 5sim buyability + operator-resolution probe (USER-AUTHORIZED).

For each candidate country: buy ONE instagram number via operator=any, read which
operator 5sim actually assigned and that operator's published delivery rate, then
CANCEL IMMEDIATELY (refunded before any SMS -> ~$0). This does NOT measure live
SMS delivery (impossible on an idle number — only Instagram signup triggers an
SMS); it finds which country's `any` lands on a high-delivery operator and
actually buys. Safety: every bought order is cancelled in a finally block.

Run (WSL): FIVESIM_API_KEY=... python scripts/reg_5sim_probe.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://5sim.net/v1"
KEY = os.environ["FIVESIM_API_KEY"]
CANDIDATES = ["croatia", "slovenia", "oman", "czech", "namibia", "england", "usa"]


def _call(path: str) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        API + path, headers={"Authorization": "Bearer " + KEY, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:160]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)[:160]
    try:
        return 200, json.loads(body)
    except Exception:
        return 200, body


def _rates() -> dict:
    code, data = _call("/guest/prices?product=instagram")
    return data.get("instagram", {}) if isinstance(data, dict) else {}


def main() -> int:
    rates = _rates()
    print(f"{'country':10s} {'buys?':5s} {'assigned_op':12s} {'rate%':>6} {'cost$':>7} phone")
    winners = []
    for country in CANDIDATES:
        code, data = _call(f"/user/buy/activation/{country}/any/instagram")
        oid = data.get("id") if isinstance(data, dict) else None
        if not oid:
            msg = data if isinstance(data, str) else json.dumps(data)
            print(f"{country:10s} {'NO':5s} {'-':12s} {'-':>6} {'-':>7} {msg[:60]}")
            continue
        try:
            op = data.get("operator", "?")
            phone = data.get("phone", "?")
            info = rates.get(country, {}).get(op, {})
            rate = info.get("rate")
            cost = data.get("price") or info.get("cost")
            rate_s = f"{rate:.1f}" if isinstance(rate, (int, float)) else "?"
            print(f"{country:10s} {'YES':5s} {op:12s} {rate_s:>6} {str(cost):>7} {phone}")
            winners.append((rate or 0, country, op, cost))
        finally:
            c2, _ = _call(f"/user/cancel/{oid}")
            # tiny spacing so we never hammer the API
            time.sleep(0.4)
    if winners:
        winners.sort(reverse=True)
        rate, country, op, cost = winners[0]
        print(f"\nBEST buyable: {country} (any -> {op}), published rate {rate}%, ${cost}")
        print(f"=> run: --country {country} --operator any --sms-timeout 150")
    else:
        print("\nNo country bought via operator=any. Investigate balance/account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
