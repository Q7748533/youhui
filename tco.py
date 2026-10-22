# ::ILANG
# [TYPE:module][PROJECT:cloud-storage-deals]
# ::STATE{@SELF, role:calculator, reads:[data/offers.json, data/egress.json, data/tco.json], writes:[]}
# ::RULE{一份公式两处用⇒Python 算 SSR 兜底 JS 算交互 两者必须逐位一致}
# ::BOUNDARY{never:估算费率 假设套餐可线性缩放|scope:permanent}
"""Total-cost-of-ownership maths.

Kept out of build.py so the same functions can be exercised directly by tests
and compared against the JavaScript copy in templates/tco.js. Two
implementations of one formula is a smell, but the alternative — shipping no
server-rendered fallback — means the page shows an empty table with JavaScript
off, and this site is meant to work as a plain document.

`tools/test_tco_parity.py` compares this module with the JS across scenarios;
if they ever disagree, one of them is wrong and the test says which scenario.
"""

from __future__ import annotations

GB_PER_TB = 1024.0
LINEAR_BASES = ("month/gb", "month/tb", "year/tb")


def per_tb_month(offer: dict):
    """Normalise a published rate to USD per TB per month.

    Returns None for plans that cannot be scaled linearly (flat bundles,
    per-seat, per-month), which the caller is expected to skip rather than
    approximate.
    """
    price, unit = offer.get("price"), offer.get("pricing_unit")
    if price is None:
        return None
    if unit == "month/gb":
        return price * 1024
    if unit == "month/tb":
        return price
    if unit == "year/tb":
        return price / 12
    return None


def egress_charge(calc: dict, storage_tb: float, egress_tb: float) -> float:
    """Apply one provider's egress policy to a monthly volume."""
    calc = calc or {}
    kind = calc.get("kind", "free")
    if kind in ("free", "bundled"):
        return 0.0
    if kind == "multiple":
        free_tb = storage_tb * float(calc.get("free_multiple") or 0)
        return max(0.0, egress_tb - free_tb) * GB_PER_TB * float(calc.get("rate_per_gb") or 0)
    if kind == "allowance":
        return (max(0.0, egress_tb - float(calc.get("allowance_tb") or 0))
                * GB_PER_TB * float(calc.get("rate_per_gb") or 0))
    if kind == "per_tb":
        return egress_tb * float(calc.get("rate_per_tb") or 0)
    if kind == "per_gb":
        return egress_tb * GB_PER_TB * float(calc.get("rate_per_gb") or 0)
    if kind == "tiered":
        remaining, prev, total = egress_tb, 0.0, 0.0
        for tier in calc.get("tiers", []):
            cap = tier.get("upto_tb")
            cap = float("inf") if cap is None else float(cap)
            span = min(remaining, cap - prev)
            if span <= 0:
                break
            total += span * GB_PER_TB * float(tier.get("rate_per_gb") or 0)
            remaining -= span
            prev = cap
            if remaining <= 0:
                break
        return total
    return 0.0


def monthly(row: dict, storage_tb: float, egress_tb: float) -> dict:
    """Monthly bill for one catalog row: storage + egress, floors applied."""
    calc = row.get("egress") or {}
    # A storage minimum (Wasabi's 1 TB) means you are billed for it even when
    # you store less, and the egress allowance is computed on the billed volume.
    billable = max(storage_tb, float(calc.get("min_storage_tb") or 0))
    storage = billable * row["storagePerTb"]
    egress = egress_charge(calc, billable, egress_tb)
    total = storage + egress
    floor = float(calc.get("min_monthly") or 0)
    if floor:
        total = max(total, floor)
    return {"storage": storage, "egress": egress, "total": total}


def build_catalog(tco_cfg: dict, offers_by_id: dict, egress_by_slug: dict,
                  root: str = "/", warn=None) -> list[dict]:
    """Resolve data/tco.json against the live offer set."""
    catalog = []
    for row in tco_cfg.get("providers", []):
        slug = row.get("slug", "")
        offer = offers_by_id.get(row.get("offer_id", ""))
        if not offer:
            if warn:
                warn(f"tco.json points at a missing offer ({row.get('offer_id')}) — skipped")
            continue
        rate = per_tb_month(offer)
        if rate is None:
            if warn:
                warn(f"{row.get('offer_id')} is not a linear basis — skipped")
            continue
        eg = egress_by_slug.get(slug, {})
        catalog.append({
            "slug": slug,
            "name": offer.get("provider") or slug,
            "tier": row.get("tier", ""),
            "note": row.get("note", ""),
            "storagePerTb": round(rate, 6),
            "egress": eg.get("calc") or {"kind": "free"},
            "egressNote": eg.get("overage", ""),
            "pageUrl": f"{root}providers/{slug}/",
        })
    return catalog


def describe_rule(calc: dict) -> str:
    """One-line human summary of an egress policy, for the rules table."""
    calc = calc or {}
    kind = calc.get("kind", "free")
    if kind in ("free", "bundled"):
        rule = "No egress charge"
        if calc.get("min_storage_tb"):
            rule += f' (but {calc["min_storage_tb"]:g} TB minimum storage)'
    elif kind == "multiple":
        rule = (f'Free up to {calc.get("free_multiple", 0):g}x stored volume, '
                f'then ${calc.get("rate_per_gb", 0):g}/GB')
    elif kind == "allowance":
        rule = (f'{calc.get("allowance_tb", 0):g} TB/month included, '
                f'then ${calc.get("rate_per_gb", 0):g}/GB')
    elif kind == "per_tb":
        rule = f'${calc.get("rate_per_tb", 0):g}/TB, no free allowance'
    elif kind == "per_gb":
        rule = f'${calc.get("rate_per_gb", 0):g}/GB delivered'
    elif kind == "tiered":
        rule = "Volume tiers: " + ", ".join(
            f'{"up to " + format(t["upto_tb"], "g") + " TB" if t.get("upto_tb") else "above"} '
            f'${t["rate_per_gb"]:g}/GB'
            for t in calc.get("tiers", [])
        )
    else:
        rule = str(kind)
    if calc.get("min_monthly"):
        rule += f' · ${calc["min_monthly"]:g} monthly minimum'
    return rule
