"""5sim probe #3 (USER-AUTHORIZED): buyability + assigned-operator rate for
IG-friendly EU/major countries via operator=any + maxPrice. Buy+cancel (refunded).
Goal: find a country that is (likely) IG-accepted AND has a decent 5sim delivery
operator AND is buyable right now."""
from __future__ import annotations
import json, os, sys, time, urllib.error, urllib.request

API = "https://5sim.net/v1"
KEY = os.environ["FIVESIM_API_KEY"]
MAXP = 0.55
# IG generally accepts these country formats; mix of mid-EU (better 5sim) + majors.
COUNTRIES = ["netherlands", "poland", "czech", "romania", "latvia", "lithuania",
             "bulgaria", "portugal", "spain", "italy", "germany", "france"]


def call(path):
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + KEY, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.read().decode()[:120]
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


rates = call("/guest/prices?product=instagram")
rates = rates.get("instagram", {}) if isinstance(rates, dict) else {}
print(f"{'country':12s} {'buys':4s} {'got_op':11s} {'rate%':>6} {'price$':>7} phone")
winners = []
for c in COUNTRIES:
    d = call(f"/user/buy/activation/{c}/any/instagram?maxPrice={MAXP}")
    oid = d.get("id") if isinstance(d, dict) else None
    if not oid:
        print(f"{c:12s} {'NO':4s} {'-':11s} {'-':>6} {'-':>7} {str(d)[:45]}")
        continue
    try:
        op = d.get("operator", "?"); phone = d.get("phone", "?"); price = d.get("price")
        rate = rates.get(c, {}).get(op, {}).get("rate")
        rate_s = f"{rate:.1f}" if isinstance(rate, (int, float)) else "?"
        print(f"{c:12s} {'YES':4s} {op:11s} {rate_s:>6} {str(price):>7} {phone}")
        winners.append((rate or 0, c, op, price))
    finally:
        call(f"/user/cancel/{oid}")
        time.sleep(0.4)
if winners:
    winners.sort(reverse=True)
    print("\nRanked buyable (by delivery rate):")
    for rate, c, op, price in winners:
        print(f"  {c:12s} {op:11s} rate {rate}%  ${price}")
    rate, c, op, price = winners[0]
    print(f"\n=> try first: --country {c} --operator any --max-price {MAXP} --sms-timeout 150")
