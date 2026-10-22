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


def _tiered(tiers: list, volume_tb: float) -> float:
    """Cost of `volume_tb` under cumulative volume tiers.

    Tiers are cumulative, not incremental: the second tier's rate applies only
    to the TB above the first tier's ceiling, so the first 50 TB of a 1000 TB
    bill are still charged at the first-tier rate. A single flat rate here
    overstates large deployments by up to 7%.
    """
    remaining, prev, total = volume_tb, 0.0, 0.0
    for tier in tiers:
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


def storage_charge(calc: dict, storage_tb: float) -> float:
    """Cost of storing `storage_tb` under a provider's storage tiers, if any.

    Falls back to a flat rate when the provider publishes no storage tiers.
    """
    tiers = (calc or {}).get("storage_tiers")
    if tiers:
        return _tiered(tiers, storage_tb)
    return 0.0


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
        return _tiered(calc.get("tiers", []), egress_tb)
    return 0.0


def monthly(row: dict, storage_tb: float, egress_tb: float) -> dict:
    """Monthly bill for one catalog row: storage + egress, floors applied."""
    calc = row.get("egress") or {}
    # A storage minimum (Wasabi's 1 TB) means you are billed for it even when
    # you store less, and the egress allowance is computed on the billed volume.
    billable = max(storage_tb, float(calc.get("min_storage_tb") or 0))
    tiers = calc.get("storage_tiers")
    storage = storage_charge(calc, billable) if tiers else billable * row["storagePerTb"]
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


def cheapest(catalog: list, storage_tb: float, egress_tb: float):
    """Lowest-total row at one point. Ties break on name, so it is stable."""
    best = None
    for row in catalog:
        m = monthly(row, storage_tb, egress_tb)
        key = (m["total"], row.get("name", "").lower())
        if best is None or key < best[0]:
            best = (key, row, m)
    return (best[1], best[2]) if best else (None, None)


# How far past the visitor's own egress volume the scan looks for a change of
# leader. A crossover beyond 1,000 TB/month is not a decision anyone makes from
# a public price list, but the axis still stretches if the inputs go higher.
SCAN_FLOOR_TB = 1000.0
SCAN_SAMPLES = 400


def crossover(catalog: list, storage_tb: float, egress_tb: float,
              samples: int = SCAN_SAMPLES) -> dict:
    """Points where the cheapest provider changes hands as egress grows.

    Every provider's bill is piecewise linear in egress, so the leader can only
    change at a crossing. The scan samples the axis coarsely, then bisects each
    transition to the exact crossing — coarse sampling alone would report the
    sample boundary rather than the true break-even.

    The output is the decision aid a per-scenario price table cannot give: not
    "who is cheapest for this number" but "at what number does the answer change".
    """
    empty = {"storage": storage_tb, "egress_max": 0.0, "current": None,
             "switches": []}
    if not catalog:
        return empty
    egress_max = max(SCAN_FLOOR_TB, egress_tb * 2.0)
    step = egress_max / samples

    prev_row, prev_m = cheapest(catalog, storage_tb, 0.0)
    if prev_row is None:
        return empty

    switches = []
    for i in range(1, samples + 1):
        point = step * i
        row, _m = cheapest(catalog, storage_tb, point)
        if row["slug"] == prev_row["slug"]:
            continue
        lo, hi = step * (i - 1), point
        for _ in range(40):
            mid = (lo + hi) / 2.0
            if cheapest(catalog, storage_tb, mid)[0]["slug"] == prev_row["slug"]:
                lo = mid
            else:
                hi = mid
        row2, m2 = cheapest(catalog, storage_tb, hi)
        switches.append({
            "at": hi,
            "from": prev_row["name"],
            "to": row2["name"],
            "from_slug": prev_row["slug"],
            "to_slug": row2["slug"],
            "cost": m2["total"],
        })
        prev_row, prev_m = row2, m2

    current_row, current_m = cheapest(catalog, storage_tb, egress_tb)
    return {
        "storage": storage_tb,
        "egress_max": egress_max,
        "current": {"slug": current_row["slug"], "name": current_row["name"],
                    "total": current_m["total"]},
        "switches": switches,
    }


def fmt_tb(value: float) -> str:
    """Volume for display, matching fmtTb in tco.js byte for byte.

    One decimal below 10 TB, whole TB above it. Built from integer arithmetic
    on both sides so a float formatting difference cannot make the two
    implementations print different sentences.
    """
    tenths = int(value * 10 + 0.5)
    if tenths < 10:
        return f"{tenths // 10}.{tenths % 10}"
    return str(int(value + 0.5))


def _usd(amount: float) -> str:
    """Money as the JavaScript usd() renders it, so both sides agree."""
    return f"${amount:,.2f}"


def crossover_text(catalog: list, storage_tb: float, egress_tb: float) -> dict:
    """Sentence summary of the crossover; mirrored by crossoverText in tco.js."""
    # With nothing stored every provider bills $0, so "cheapest" is decided by
    # the tie-break rather than by price. Saying AWS S3 wins because "A" sorts
    # first would be worse than saying nothing.
    if storage_tb <= 0:
        return {
            "headline": "No storage volume entered.",
            "items": ["With nothing stored, every provider starts at $0, so there is "
                      "no cheapest option to compare. Enter a storage volume above "
                      "zero to see where the leader changes."],
        }
    report = crossover(catalog, storage_tb, egress_tb)
    if not report["current"]:
        return {"headline": "", "items": []}
    name = report["current"]["name"]
    headline = f"At {storage_tb:g} TB stored, {name} is the cheapest choice."
    items = [
        f"From {fmt_tb(s['at'])} TB/month of egress onward, {s['to']} "
        f"is the cheapest option ({_usd(s['cost'])}/month)."
        for s in report["switches"]
    ]
    if not items:
        items = [
            f"No provider overtakes {name} anywhere up to "
            f"{fmt_tb(report['egress_max'])} TB/month of egress."
        ]
    return {"headline": headline, "items": items}


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
