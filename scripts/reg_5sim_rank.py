"""Rank 5sim instagram operators by delivery rate, then probe buyability (with
maxPrice) of the top ones via buy+immediate-cancel (refunded). USER-AUTHORIZED
(empirical operator cycling). Prints a ranked, buyable shortlist to try."""
from __future__ import annotations
import json, os, sys, time, urllib.error, urllib.request

API = "https://5sim.net/v1"
KEY = os.environ["FIVESIM_API_KEY"]
MAXP = 0.60
MIN_COUNT = 80
TOP_N = 12


def call(path):
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + KEY, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.read().decode()[:120]
    except Exception as e:  # noqa: BLE001
        return str(e)[:120]


# guest/prices via urllib is flaky/blocked; read the curl'd snapshot if present,
# else fall back to the API.
try:
    prices = json.load(open("/tmp/p.json"))["instagram"]
except Exception:
    prices = call("/guest/prices?product=instagram")["instagram"]
ranked = []
for country, ops in prices.items():
    for op, i in ops.items():
        if isinstance(i, dict) and (i.get("count") or 0) >= MIN_COUNT and (i.get("rate") or 0) > 0:
            ranked.append(((i.get("rate") or 0), (i.get("count") or 0), country, op, i.get("cost")))
ranked.sort(reverse=True)

print(f"Top {TOP_N} operators by rate (count>={MIN_COUNT}); probing buyability @maxPrice={MAXP}\n")
print(f"{'rate%':>6} {'count':>7} {'country':14s} {'operator':10s} {'cost$':>7} {'buyable':8s} got_op")
buyable = []
for rate, cnt, country, op, cost in ranked[:TOP_N]:
    d = call(f"/user/buy/activation/{country}/{op}/instagram?maxPrice={MAXP}")
    oid = d.get("id") if isinstance(d, dict) else None
    got = d.get("operator") if isinstance(d, dict) else "-"
    buy = "YES" if oid else "no"
    print(f"{rate:6.1f} {cnt:7d} {country:14s} {op:10s} {str(cost):>7} {buy:8s} {got}")
    if oid:
        buyable.append((rate, country, op, cost))
        call(f"/user/cancel/{oid}")
        time.sleep(0.4)

print("\n=== BUYABLE high-rate shortlist (try in this order) ===")
for rate, country, op, cost in buyable:
    print(f"  --country {country} --operator {op},any --max-price {MAXP}   # rate {rate}% ${cost}")
