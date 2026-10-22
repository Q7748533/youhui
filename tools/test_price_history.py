"""Unit tests for price_history.py.

The rules under test are all about what *not* to call a price change. A history
feed that reports noise is worse than one that reports nothing, so each
exclusion is pinned down here.

Run:  python tools/test_price_history.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import price_history  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def snap(date, prices):
    return {"date": date, "prices": prices}


def row(price, currency="USD", unit="month/tb", stale=False):
    return [price, currency, unit, stale]


META = {
    "wasabi-pay-as-you-go-usd": {"provider": "Wasabi", "plan": "Pay as you go",
                                 "provider_slug": "wasabi"},
    "backblaze-b2-cloud-storage-usd": {"provider": "Backblaze", "plan": "B2",
                                       "provider_slug": "backblaze"},
    "storj-standard-usd": {"provider": "Storj", "plan": "Standard",
                           "provider_slug": "storj"},
}


def main():
    print("=== T1: one snapshot has nothing to compare ===")
    r = price_history.compute([snap("2026-10-22", {"storj-standard-usd": row(7.0)})], META)
    check("no changes", r["changes"] == 0, str(r["changes"]))
    check("days is 1", r["days"] == 1)
    check("first date recorded", r["by_provider"]["storj"][0]["first"]["date"] == "2026-10-22")

    print("=== T2: an increase is recorded with the right percentage ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": row(7.0)}),
        snap("2026-10-23", {"storj-standard-usd": row(8.0)}),
    ], META)
    ch = r["by_provider"]["storj"][0]["changes"]
    check("one change", len(ch) == 1, str(len(ch)))
    check("from 7 to 8", ch[0]["from"] == 7.0 and ch[0]["to"] == 8.0)
    check("pct is +14.29", abs(ch[0]["pct"] - 14.29) < 0.01, str(ch[0]["pct"]))
    check("date is the later snapshot", ch[0]["date"] == "2026-10-23")

    print("=== T3: a stale quote is not a price change ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": row(7.0)}),
        snap("2026-10-23", {"storj-standard-usd": row(9.0, stale=True)}),
    ], META)
    check("no change recorded", r["changes"] == 0, str(r["changes"]))
    check("pair not counted as compared", r["compared_pairs"] == 0, str(r["compared_pairs"]))

    print("=== T3b: stale on the earlier side is also excluded ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": row(7.0, stale=True)}),
        snap("2026-10-23", {"storj-standard-usd": row(9.0)}),
    ], META)
    check("no change recorded", r["changes"] == 0, str(r["changes"]))

    print("=== T4: a currency change is a basis change, not a price move ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": row(7.0, currency="USD")}),
        snap("2026-10-23", {"storj-standard-usd": row(6.5, currency="EUR")}),
    ], META)
    check("not counted as a price change", r["changes"] == 0, str(r["changes"]))
    check("counted as a basis change", r["basis_changes"] == 1, str(r["basis_changes"]))

    print("=== T5: a unit change is a basis change ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": row(7.0, unit="month/tb")}),
        snap("2026-10-23", {"storj-standard-usd": row(0.007, unit="month/gb")}),
    ], META)
    check("not counted as a price change", r["changes"] == 0, str(r["changes"]))
    check("counted as a basis change", r["basis_changes"] == 1, str(r["basis_changes"]))
    check("both prices preserved", r["by_provider"]["storj"][0]["basis_changes"][0]["to"] == 0.007)

    print("=== T6: a new plan is not a price change ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": row(7.0)}),
        snap("2026-10-23", {"storj-standard-usd": row(7.0),
                            "backblaze-b2-cloud-storage-usd": row(6.95)}),
    ], META)
    check("no price change", r["changes"] == 0, str(r["changes"]))
    check("new offer counted", r["new_offers"] == 1, str(r["new_offers"]))
    check("new plan gets a first date", r["by_provider"]["backblaze"][0]["first"]["date"] == "2026-10-23")

    print("=== T7: a retired plan is not a price change ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": row(7.0),
                            "backblaze-b2-cloud-storage-usd": row(6.95)}),
        snap("2026-10-23", {"storj-standard-usd": row(7.0)}),
    ], META)
    check("no price change", r["changes"] == 0, str(r["changes"]))
    check("retirement counted", r["retired_offers"] == 1, str(r["retired_offers"]))

    print("=== T8: an unchanged pair is still a measurement ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": row(7.0)}),
        snap("2026-10-23", {"storj-standard-usd": row(7.0)}),
    ], META)
    check("zero changes", r["changes"] == 0)
    check("but the pair was compared", r["compared_pairs"] == 1, str(r["compared_pairs"]))

    print("=== T9: a change and a revert are two changes ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": row(7.0)}),
        snap("2026-10-23", {"storj-standard-usd": row(9.0)}),
        snap("2026-10-24", {"storj-standard-usd": row(7.0)}),
    ], META)
    ch = r["by_provider"]["storj"][0]["changes"]
    check("two changes", len(ch) == 2, str(len(ch)))
    check("second is negative", ch[1]["pct"] < 0, str(ch[1]["pct"]))

    print("=== T10: malformed rows do not crash ===")
    r = price_history.compute([
        snap("2026-10-22", {"storj-standard-usd": [7.0], "x": None, "y": "bad"}),
        snap("2026-10-23", {"storj-standard-usd": row(8.0)}),
    ], META)
    check("survives bad rows", r["changes"] >= 0)

    print("=== T11: empty history ===")
    r = price_history.compute([], META)
    check("no crash", r["days"] == 0 and r["by_provider"] == {})

    print("=== T12: provider_summary caps rows and reports the overflow ===")
    prices = {}
    days = []
    for i in range(20):
        prices = {"storj-standard-usd": row(7.0 + i)}
        days.append(snap(f"2026-10-{i + 1:02d}", dict(prices)))
    r = price_history.compute(days, META)
    ps = price_history.provider_summary(r, "storj")
    check("capped at MAX_ROWS", len(ps["rows"]) == price_history.MAX_ROWS, str(len(ps["rows"])))
    check("overflow reported", ps["hidden"] == 19 - price_history.MAX_ROWS, str(ps["hidden"]))
    check("change_count is the true total", ps["change_count"] == 19, str(ps["change_count"]))
    check("newest first", ps["rows"][0]["date"] == "2026-10-20", ps["rows"][0]["date"])

    print("=== T13: provider_summary on an unknown provider ===")
    ps = price_history.provider_summary(r, "nope")
    check("empty but well formed", ps["rows"] == [] and ps["tracked_plans"] == 0)

    print()
    if FAILS:
        print(f"FAILURES ({len(FAILS)}): {', '.join(FAILS)}")
        return 1
    print("FAILURES: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
