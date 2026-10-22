"""Synthetic-history tests for price_index.compute.

Builds controlled snapshots in memory so every branch (rise, fall, entry,
exit, stale, unit change, currency change, outlier) is exercised without
touching data/history.
"""
import copy
import json
import math

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import price_index

import archive

# The real snapshot used for the sanity checks. Picked at run time rather than
# by filename: hardcoding a date made the test fail the moment that day's file
# was rotated out or history started accumulating.
_HISTORY = archive.load_history()
BASE = _HISTORY[0] if _HISTORY else {"date": "", "prices": {}}


def snap(date, prices):
    return {"date": date, "prices": prices}


def row(price, cur="USD", unit="month", stale=False):
    return [price, cur, unit, stale]


fail = []


def check(name, got, want, tol=0.01):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got={got} want={want}")
    if not ok:
        fail.append(name)


print("=== T1: no change, 2 days ===")
d1 = snap("2026-01-01", {"a": row(10.0), "b": row(20.0)})
d2 = snap("2026-01-02", {"a": row(10.0), "b": row(20.0)})
r = price_index.compute([d1, d2])
check("index flat", r["value"], 100.0)
check("paired", r["paired_latest"], 2)
check("flat count", r["flat"], 2)
check("changed count", r["changed"], 0)

print("=== T2: one doubles, one halves -> geometric mean ===")
d2 = snap("2026-01-02", {"a": row(20.0), "b": row(10.0)})
r = price_index.compute([d1, d2])
check("geometric mean = 100", r["value"], 100.0)
check("changed count", r["changed"], 2)
check("mover up pct", r["movers"]["up"][0]["pct"], 100.0)
check("mover down pct", r["movers"]["down"][0]["pct"], -50.0)

print("=== T3: both double -> +100% ===")
d2 = snap("2026-01-02", {"a": row(20.0), "b": row(40.0)})
r = price_index.compute([d1, d2])
check("index 200", r["value"], 200.0)

print("=== T4: entry and exit are not movements ===")
d2 = snap("2026-01-02", {"b": row(20.0), "c": row(99.0)})
r = price_index.compute([d1, d2])
check("index flat on churn", r["value"], 100.0)
check("paired only b", r["paired_latest"], 1)
check("coverage 50%", r["coverage_latest"], 0.5)

print("=== T5: stale on either side is excluded ===")
d2 = snap("2026-01-02", {"a": row(99.0, stale=True), "b": row(20.0)})
r = price_index.compute([d1, d2])
check("stale excluded from index", r["value"], 100.0)
check("paired only b", r["paired_latest"], 1)
d1s = snap("2026-01-01", {"a": row(10.0, stale=True), "b": row(20.0)})
d2s = snap("2026-01-02", {"a": row(99.0), "b": row(20.0)})
r = price_index.compute([d1s, d2s])
check("stale on base side excluded", r["paired_latest"], 1)

print("=== T6: unit or currency change breaks the pair ===")
d2 = snap("2026-01-02", {"a": row(10.0, unit="year"), "b": row(20.0, cur="EUR")})
r = price_index.compute([d1, d2])
check("no pairs", r["paired_latest"], 0)
check("index flat", r["value"], 100.0)
check("flagged no comparable pairs", r["series"][-1]["note"], "no comparable pairs")

print("=== T7: outlier (10x) excluded but counted ===")
d2 = snap("2026-01-02", {"a": row(100.0), "b": row(20.0)})
r = price_index.compute([d1, d2])
check("outlier excluded", r["value"], 100.0)
check("outlier counted", r["outliers_latest"], 1)
check("paired excludes outlier", r["paired_latest"], 1)

print("=== T8: chain across 3 days ===")
d3 = snap("2026-01-03", {"a": row(100.0), "b": row(40.0)})
r = price_index.compute([d1, d2, d3])
# day2: a 10->100 is a 10x outlier (excluded), b flat -> factor 1.0
# day3: a 100->100 flat, b 20->40 doubles -> factor 2.0^(1/2) = 1.4142
check("chained value", r["value"], 141.42, tol=0.02)
check("3 points", len(r["series"]), 3)
check("day2 outlier recorded", r["series"][1]["outliers"], 1)

print("=== T9: empty / missing ===")
check("empty history", price_index.compute([])["status"], "empty")
check("empty series", len(price_index.compute([])["series"]), 0)
check("single day status", price_index.compute([d1])["status"], "base_only")

print("=== T10: bad rows do not crash ===")
d2 = snap("2026-01-02", {"a": [None, "USD", "month"], "b": row(0.0),
                         "c": "garbage", "d": row(-5.0), "e": row(20.0)})
try:
    r = price_index.compute([snap("2026-01-01", {"a": row(10.0), "b": row(10.0),
                                                "c": row(10.0), "d": row(10.0),
                                                "e": row(10.0)}), d2])
    check("survives bad rows", r["paired_latest"], 1)
except Exception as exc:
    print(f"  FAIL  crashed: {exc}")
    fail.append("bad rows crash")

print("=== T11: zero-egress-free check on real snapshot (sanity) ===")
r = price_index.compute([BASE])
check("real single day is base_only", r["status"], "base_only")
check("real universe", r["universe_latest"], len(BASE["prices"]))

print()
print("FAILURES:", fail if fail else "none")
sys.exit(1 if fail else 0)
