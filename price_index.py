# ::ILANG
# [TYPE:module][PROJECT:cloud-storage-deals]
# ::STATE{@SELF, role:indexer, reads:[data/history/], writes:[]}
# ::RULE{只在配对样本上计算⇒基期与当期都存在 且币种与口径一致 才计入指数}
# ::RULE{抓取失败的报价不算价格变动⇒任一侧 stale 的配对直接排除}
# ::BOUNDARY{never:补数据点 平滑走势 隐藏样本流失|scope:permanent}
"""Price index over the daily snapshots.

Two decisions shape everything here:

1. **Paired samples only.** A quote entering or leaving the dataset is not a
   price movement, so the index only looks at offers present on both sides of a
   comparison with the same currency and the same billing basis. The count of
   paired offers is published alongside the index, because an index computed on
   four surviving quotes is not the same claim as one computed on ninety.

2. **Chained, not fixed-base.** Each day is compared with the day before and the
   factors are multiplied together, so the index survives providers being added
   or dropped. A fixed-base index would have to silently redefine its basket
   every time the provider list changed.

The averaging is Jevons (geometric mean of price relatives), which is what
statistical agencies use for homogeneous goods: it is not dragged upward by a
single large absolute price, and it is symmetric under time reversal.
"""

from __future__ import annotations

import math
import os
import sys

import archive

# A relative change larger than this is far more likely to be a parser misread
# (or a unit change we failed to notice) than a real price move. Such pairs are
# excluded from the index but reported by count, never silently dropped.
OUTLIER_RATIO = 5.0


def _pairs(prev: dict, cur: dict, keep=None) -> tuple[list[tuple], list[tuple]]:
    """Price relatives for offers observed on both days.

    Returns (pairs, outliers); each pair is (offer_id, ratio, prev_price,
    cur_price, currency, unit).
    """
    pairs, outliers = [], []
    for oid, cur_row in cur.items():
        prev_row = prev.get(oid)
        if prev_row is None:
            continue
        p0, c0, u0, s0 = archive.price_row(prev_row)
        p1, c1, u1, s1 = archive.price_row(cur_row)
        # A stale row carries yesterday's number, so it is not an observation.
        if s0 or s1:
            continue
        if p0 is None or p1 is None or p0 <= 0 or p1 <= 0:
            continue
        # Same currency and same billing basis or the ratio is meaningless.
        if c0 != c1 or u0 != u1:
            continue
        if keep is not None and not keep(oid, c0, u0):
            continue
        ratio = p1 / p0
        row = (oid, ratio, p0, p1, c0, u0)
        # Inclusive bounds so a ratio sitting exactly on the threshold is
        # decided the same way every run instead of flipping on float noise.
        if ratio >= OUTLIER_RATIO or ratio <= 1.0 / OUTLIER_RATIO:
            outliers.append(row)
        else:
            pairs.append(row)
    return pairs, outliers


def _factor(pairs: list[tuple]) -> float:
    """Jevons index factor: geometric mean of price relatives."""
    if not pairs:
        return 1.0
    return math.exp(sum(math.log(r) for _, r, *_ in pairs) / len(pairs))


def _chain(history: list[dict], keep=None) -> list[dict]:
    """Walk the snapshots forward, multiplying each day's factor."""
    series = []
    value = 100.0
    for i, snap in enumerate(history):
        prices = snap.get("prices", {}) or {}
        if i == 0:
            # Coverage is undefined on the base day: there is no previous
            # snapshot to have covered, and claiming 100% would read as a
            # measurement that was never taken.
            series.append({"date": snap.get("date", ""), "value": 100.0,
                           "paired": 0, "universe": len(prices), "coverage": None,
                           "note": "base"})
            continue
        prev = history[i - 1].get("prices", {}) or {}
        pairs, outliers = _pairs(prev, prices, keep)
        if pairs:
            value *= _factor(pairs)
            note = ""
        else:
            # Flat, but flagged: a flat line because nothing changed and a flat
            # line because nothing was comparable are different facts.
            note = "no comparable pairs"
        series.append({
            "date": snap.get("date", ""),
            "value": round(value, 2),
            "paired": len(pairs),
            "outliers": len(outliers),
            "universe": len(prices),
            "coverage": round(len(pairs) / len(prev), 3) if prev else 0.0,
            "note": note,
        })
    return series


def compute(history: list[dict], labels: dict | None = None,
            categories: dict | None = None) -> dict:
    """Build the full index report.

    `labels` maps offer_id -> {provider, plan, url} for readable movers.
    `categories` maps provider_slug -> category (object / consumer / backup).
    """
    history = [h for h in history if h.get("prices")]
    if not history:
        return {"status": "empty", "days": 0, "series": [], "movers": {"up": [], "down": []}}

    labels = labels or {}
    categories = categories or {}
    series = _chain(history)
    last = series[-1]

    def slug_of(oid: str) -> str:
        return (labels.get(oid, {}) or {}).get("provider_slug", "")

    def keep_currency(code):
        return lambda oid, cur, unit: cur == code

    def keep_basis(basis):
        return lambda oid, cur, unit: unit == basis

    def keep_category(cat):
        return lambda oid, cur, unit: categories.get(slug_of(oid)) == cat

    def group_summary(keys, keep_for) -> list[dict]:
        out = []
        for key in keys:
            sub = _chain(history, keep_for(key))
            paired = sum(s["paired"] for s in sub)
            out.append({"key": key, "value": sub[-1]["value"], "paired": paired,
                        "universe": sub[-1]["universe"]})
        return out

    currencies = sorted({archive.price_row(r)[1] for h in history for r in h["prices"].values()} - {""})
    bases = sorted({archive.price_row(r)[2] for h in history for r in h["prices"].values()} - {""})
    cats = sorted({categories.get(slug_of(oid), "") for h in history for oid in h["prices"]} - {""})

    # Movers come from the most recent comparison only.
    movers = {"up": [], "down": []}
    changed = flat = 0
    outliers_total = 0
    if len(history) >= 2:
        pairs, outliers = _pairs(history[-2].get("prices", {}), history[-1].get("prices", {}))
        outliers_total = len(outliers)
        for oid, ratio, p0, p1, cur, unit in pairs:
            if abs(ratio - 1.0) < 1e-9:
                flat += 1
            else:
                changed += 1
        ranked = sorted(pairs, key=lambda p: abs(math.log(p[1])), reverse=True)
        for direction in ("up", "down"):
            # Strict inequalities: a ratio of exactly 1 is unchanged, and
            # listing it under "decreases" would be a lie told by a zero.
            if direction == "up":
                picked = [p for p in ranked if p[1] > 1.0][:5]
            else:
                picked = [p for p in ranked if p[1] < 1.0][:5]
            for oid, ratio, p0, p1, cur, unit in picked:
                meta = labels.get(oid, {})
                movers[direction].append({
                    "id": oid,
                    "provider": meta.get("provider", oid),
                    "plan": meta.get("plan", ""),
                    "from": p0, "to": p1,
                    "currency": cur, "unit": unit,
                    "pct": round((ratio - 1) * 100, 2),
                })

    return {
        "status": "ok" if len(history) >= 2 else "base_only",
        "days": len(history),
        "base_date": series[0]["date"],
        "latest_date": last["date"],
        "value": last["value"],
        "span_pct": round(last["value"] - 100.0, 2),
        "series": series,
        "by_currency": group_summary(currencies, keep_currency),
        "by_basis": group_summary(bases, keep_basis),
        "by_category": group_summary(cats, keep_category),
        "movers": movers,
        "changed": changed,
        "flat": flat,
        "paired_latest": last["paired"],
        "universe_latest": last["universe"],
        "coverage_latest": last["coverage"],
        "outliers_latest": outliers_total,
        "categories": cats,
    }


def main(argv: list[str]) -> int:
    history = archive.load_history()
    if not history:
        print("no snapshots in data/history/ — run scraper.py first", file=sys.stderr)
        return 1
    report = compute(history)
    print(f"{report['status']}: {report['days']} day(s) "
          f"{report['base_date']} .. {report['latest_date']}, index {report['value']}")
    for point in report["series"]:
        cov = "—" if point["coverage"] is None else f'{point["coverage"]:.0%}'
        print(f"  {point['date']}  {point['value']:>7.2f}  paired={point['paired']:>3} "
              f"coverage={cov:>4}  {point['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
