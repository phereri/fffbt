"""5sim probe #2 (USER-AUTHORIZED): does raising maxPrice unlock high-rate operators?

Probe #1 showed operator=any picks the cheapest junk operator (england->virtual59,
0%) and several countries fail with 'no free phones, max price' (stock exists above
the price cap). This tests buying the HIGH-RATE operators with an explicit maxPrice.
Every bought order is cancelled immediately (refund before SMS, ~$0).
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

# (country, operator, maxPrice) — high-rate targets from guest/prices.
TARGETS = [
    ("croatia", "virtual4", 0.40),   # 81%
    ("slovenia", "virtual4", 0.40),  # 54%
    ("oman", "virtual4", 0.40),      # 52%
    ("czech", "virtual34", 0.30),    # 43%
    ("croatia", "any", 0.40),
    ("oman", "any", 0.40),
    ("usa", "any", 0.60),
]


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


def main() -> int:
    rates = _call("/guest/prices?product=instagram")[1]
    rates = rates.get("instagram", {}) if isinstance(rates, dict) else {}
    print(f"{'country':9s} {'req_op':9s} {'maxP':>5} {'buys':4s} {'got_op':11s} {'rate%':>6} {'price$':>7} phone")
    winners = []
    for country, op, maxp in TARGETS:
        code, data = _call(f"/user/buy/activation/{country}/{op}/instagram?maxPrice={maxp}")
        oid = data.get("id") if isinstance(data, dict) else None
        if not oid:
            msg = data if isinstance(data, str) else json.dumps(data)
            print(f"{country:9s} {op:9s} {maxp:5.2f} {'NO':4s} {'-':11s} {'-':>6} {'-':>7} {msg[:50]}")
            continue
        try:
            got = data.get("operator", "?")
            phone = data.get("phone", "?")
            price = data.get("price")
            rate = rates.get(country, {}).get(got, {}).get("rate")
            rate_s = f"{rate:.1f}" if isinstance(rate, (int, float)) else "?"
            print(f"{country:9s} {op:9s} {maxp:5.2f} {'YES':4s} {got:11s} {rate_s:>6} {str(price):>7} {phone}")
            winners.append((rate or 0, country, got, price))
        finally:
            _call(f"/user/cancel/{oid}")
            time.sleep(0.4)
    if winners:
        winners.sort(reverse=True)
        rate, country, op, price = winners[0]
        print(f"\nBEST buyable high-rate: {country} -> {op}, rate {rate}%, ${price}")
        print(f"=> run: --country {country} --operator {op} --sms-timeout 150  (add maxPrice support)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
