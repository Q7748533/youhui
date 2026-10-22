# ::ILANG
# [TYPE:module][PROJECT:cloud-storage-deals]
# ::STATE{@SELF, role:archivist, reads:[data/offers.json], writes:[data/history/]}
# ::RULE{一天一份⇒同一天多次运行只覆盖当天 不追加 保证一天一个数据点}
# ::BOUNDARY{never:改写 offers.json 编造历史|scope:permanent}
"""Daily price archive — one immutable snapshot per day.

A price index needs a time series, and the scraper overwrites offers.json on
every run, so the history has to be captured deliberately. One file per UTC
day: the last run of the day wins, so the snapshot reflects the day's closing
state rather than whichever refresh happened to be first.

Kept deliberately dumb: no aggregation, no derived values. Just what was
published, so the index can be recomputed later without trusting an old
formula.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "offers.json")
HISTORY_DIR = os.path.join(BASE_DIR, "data", "history")


def snapshot(payload: dict, base_dir: str = BASE_DIR) -> str:
    """Write one day's prices. Same-day runs overwrite; nothing is appended."""
    meta = payload.get("meta", {})
    generated = meta.get("generated_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        day = datetime.fromisoformat(str(generated).replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        day = datetime.now(timezone.utc).date().isoformat()

    # Compact rows: id -> [price, currency, pricing_unit, stale]. The id already
    # encodes provider and plan, so the snapshot stays small enough to commit
    # every day without bloating the repository.
    #
    # The stale flag matters: when a fetch fails the scraper keeps yesterday's
    # price and marks it, so archiving it as today's price would make the index
    # report "no change" on a day when the price was never actually observed.
    prices: dict[str, list] = {}
    offers = payload.get("offers", [])
    for o in offers:
        oid = o.get("id")
        if not oid or o.get("price") is None:
            continue
        prices[oid] = [o.get("price"), o.get("currency"), o.get("pricing_unit"),
                       bool(o.get("stale"))]

    providers = payload.get("providers", [])
    record = {
        "date": day,
        "generated_at": generated,
        "providers": len(providers),
        "providers_priced": sum(1 for p in providers if p.get("offers")),
        "offers_total": len(offers),
        "offers_priced": len(prices),
        "offers_stale": sum(1 for row in prices.values() if len(row) > 3 and row[3]),
        "prices": prices,
    }
    out_dir = os.path.join(base_dir, "data", "history")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{day}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    return path


def load_history(base_dir: str = BASE_DIR) -> list[dict]:
    """All snapshots, oldest first. Used by the index page later."""
    out = []
    history_dir = os.path.join(base_dir, "data", "history")
    if not os.path.isdir(history_dir):
        return out
    for name in sorted(os.listdir(history_dir)):
        if not name.endswith(".json"):
            continue
        try:
            out.append(json.load(open(os.path.join(history_dir, name), encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return out


def price_row(row) -> tuple:
    """Normalise one snapshot row to (price, currency, unit, stale).

    Snapshots written before the stale flag existed carry three elements;
    they are treated as observed prices, which is what they were.
    """
    if not isinstance(row, list) or len(row) < 3:
        return None, "", "", False
    price, currency, unit = row[0], row[1], row[2]
    stale = bool(row[3]) if len(row) > 3 else False
    return price, currency, unit, stale


def main(argv: list[str]) -> int:
    if not os.path.exists(DATA_FILE):
        print("data/offers.json missing — run scraper.py first", file=sys.stderr)
        return 1
    payload = json.load(open(DATA_FILE, encoding="utf-8"))
    path = snapshot(payload)
    record = json.load(open(path, encoding="utf-8"))
    print(f"archived {record['date']}: {record['offers_priced']} priced offers, "
          f"{record['providers_priced']}/{record['providers']} providers -> {os.path.relpath(path, BASE_DIR)}")
    days = load_history()
    print(f"history: {len(days)} day(s) on record"
          + (f", {days[0]['date']} .. {days[-1]['date']}" if days else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
