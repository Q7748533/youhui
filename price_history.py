# ::ILANG
# [TYPE:module][PROJECT:cloud-storage-deals]
# ::STATE{@SELF, role:price-history, reads:[data/history/*], writes:[]}
# ::RULE{观测到才算数⇒stale 两侧任一侧未观测 一律不算调价}
# ::RULE{口径变了不等于价格变了⇒币种或计价单位变动单独归为 basis change}
# ::BOUNDARY{never:把新增或下架的方案算成调价 编造变更日期|scope:permanent}
"""Price change history, derived from the daily snapshots.

The archive stores what was published each day; this module reads two adjacent
days and reports only what actually moved. Three things are deliberately *not*
reported as price changes, because calling them that would be a lie:

  * a quote the scraper failed to read on either day (the stale flag) — the
    price was never observed, so its movement is unknown, not zero;
  * a change of currency or billing basis — $5/TB becoming $0.005/GB is a
    re-expression, not a price move;
  * a plan appearing or disappearing — that is a catalogue change.

With a single snapshot on record there is nothing to compare, and the page says
so rather than rendering an empty table that reads like "no changes happened".
"""

from __future__ import annotations

import archive

# A provider page should stay readable. Changes beyond this are summarised by
# count rather than listed, so the section never turns into an endless log.
MAX_ROWS = 12


def _pct(before: float, after: float) -> float:
    if not before:
        return 0.0
    return round((after / before - 1.0) * 100.0, 2)


def compute(history: list[dict], offers_by_id: dict) -> dict:
    """Walk consecutive snapshots and record what moved.

    `offers_by_id` supplies plan names and provider slugs for display; offers
    that no longer exist still get a record, keyed by id, because a retired
    plan's last change is still history.
    """
    days = [h for h in history if isinstance(h.get("prices"), dict)]
    empty = {"days": len(days), "first_date": "", "latest_date": "",
             "by_provider": {}, "changes": 0, "basis_changes": 0,
             "new_offers": 0, "retired_offers": 0, "compared_pairs": 0}
    if not days:
        return empty

    by_offer: dict[str, dict] = {}
    changes = basis_changes = new_offers = retired = compared = 0

    def bucket(oid: str) -> dict:
        rec = by_offer.get(oid)
        if rec is None:
            meta = offers_by_id.get(oid, {}) or {}
            rec = {
                "id": oid,
                "provider_slug": meta.get("provider_slug", ""),
                "provider": meta.get("provider", ""),
                "plan": meta.get("plan", ""),
                "first": None,
                "changes": [],
                "basis_changes": [],
            }
            by_offer[oid] = rec
        return rec

    for snap in days:
        for oid, row in snap["prices"].items():
            price, currency, unit, stale = archive.price_row(row)
            if price is None or stale:
                continue
            rec = bucket(oid)
            if rec["first"] is None:
                rec["first"] = {"date": snap["date"], "price": price,
                                "currency": currency, "unit": unit}

    for prev, cur in zip(days, days[1:]):
        p_prices, c_prices = prev["prices"], cur["prices"]
        for oid, c_row in c_prices.items():
            c_price, c_cur, c_unit, c_stale = archive.price_row(c_row)
            if c_price is None:
                continue
            p_row = p_prices.get(oid)
            if p_row is None:
                new_offers += 1
                continue
            p_price, p_cur, p_unit, p_stale = archive.price_row(p_row)
            if p_price is None:
                continue
            # Either side unobserved: the movement is unknown. Counting it as
            # "no change" would understate, counting it as a move would invent.
            if p_stale or c_stale:
                continue
            rec = bucket(oid)
            if c_cur != p_cur or c_unit != p_unit:
                basis_changes += 1
                rec["basis_changes"].append({
                    "date": cur["date"], "from": p_price, "to": c_price,
                    "from_currency": p_cur, "to_currency": c_cur,
                    "from_unit": p_unit, "to_unit": c_unit,
                })
                continue
            compared += 1
            if c_price != p_price:
                changes += 1
                rec["changes"].append({
                    "date": cur["date"], "from": p_price, "to": c_price,
                    "currency": c_cur, "unit": c_unit,
                    "pct": _pct(p_price, c_price),
                })
        for oid in p_prices:
            if oid not in c_prices:
                retired += 1

    by_provider: dict[str, list[dict]] = {}
    for rec in by_offer.values():
        if rec["changes"] or rec["basis_changes"] or rec["first"]:
            slug = rec["provider_slug"] or rec["provider"] or ""
            by_provider.setdefault(slug, []).append(rec)

    return {
        "days": len(days),
        "first_date": days[0]["date"],
        "latest_date": days[-1]["date"],
        "by_provider": by_provider,
        "changes": changes,
        "basis_changes": basis_changes,
        "new_offers": new_offers,
        "retired_offers": retired,
        "compared_pairs": compared,
    }


def provider_summary(report: dict, slug: str) -> dict:
    """Everything a provider page needs to state its own price history."""
    records = report.get("by_provider", {}).get(slug, [])
    rows = []
    for rec in records:
        for ch in rec["changes"]:
            rows.append({"plan": rec["plan"] or rec["id"], "id": rec["id"], **ch})
    rows.sort(key=lambda r: (r["date"], r["plan"]), reverse=True)

    return {
        "days": report["days"],
        "first_date": report["first_date"],
        "latest_date": report["latest_date"],
        "rows": rows[:MAX_ROWS],
        "hidden": max(0, len(rows) - MAX_ROWS),
        "change_count": len(rows),
        "tracked_plans": len([r for r in records if r["first"]]),
        "basis_changes": sum(len(rec["basis_changes"]) for rec in records),
    }
