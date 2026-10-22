"""Parity test: tco.py vs site/tco.js.

The calculator has two implementations of the same formulas — Python for the
no-JS server render, JavaScript for the interactive one. They must agree
exactly, or a visitor sees one number before the script loads and a different
one after.

Three things are compared, because all three are visible to a visitor:
  * the per-provider bills (storage / egress / total) for every scenario
  * the crossover scan (where the cheapest provider changes hands)
  * the crossover sentences themselves, character for character

Run:  python tools/test_tco_parity.py
Exits non-zero on any mismatch.
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tco  # noqa: E402

SCENARIOS = [
    (0, 0), (1, 0), (1, 1), (1, 10), (10, 10), (10, 0),
    (0.5, 2), (100, 100), (1000, 1), (1, 1000), (5, 5), (0.1, 0.1),
    (0.34, 3.4), (2.5, 0.5), (50, 200),
    # Boundaries the spot-checks below depend on.
    (2, 6),    # exactly 3x stored volume -> still free on B2 / e2
    (1, 11),   # one TB past the first AWS tier
    (0.5, 0),  # below Wasabi's 1 TB storage floor
    (1, 2),    # one TB past the bundled allowance
    # Storage-tier boundaries for AWS S3 (50 TB / 500 TB) and either side.
    (49, 0), (50, 0), (51, 0), (499, 0), (500, 0), (501, 0), (2000, 0),
    (32.96, 33),  # sits on the IDrive -> Wasabi break-even
]


def load_catalog():
    path = os.path.join(ROOT, "site", "calculator", "index.html")
    if not os.path.exists(path):
        raise SystemExit("site/calculator/index.html missing — run `python build.py` first")
    html = open(path, encoding="utf-8").read()
    m = re.search(r'<script type="application/json" id="tco-data">(.*?)</script>', html, re.S)
    if not m:
        raise SystemExit("tco-data block not found in the built page")
    return json.loads(m.group(1))


def python_expected(catalog):
    out = {}
    for storage, egress in SCENARIOS:
        key = f"{storage}|{egress}"
        out[key] = {
            "rows": {row["slug"]: tco.monthly(row, storage, egress) for row in catalog},
            "xo": tco.crossover(catalog, storage, egress),
            "text": tco.crossover_text(catalog, storage, egress),
        }
    return out


def run_node(catalog):
    payload = {"catalog": catalog, "scenarios": SCENARIOS}
    script = r"""
const path = require('path');
const calc = require(path.join(process.env.TCO_ROOT, 'site', 'tco.js'));
const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = {};
for (const [storage, egress] of payload.scenarios) {
  const key = storage + '|' + egress;
  const entry = { rows: {}, xo: null, text: null };
  for (const row of payload.catalog) {
    entry.rows[row.slug] = calc.monthly(row, storage, egress);
  }
  const xo = calc.crossover(payload.catalog, storage, egress);
  entry.xo = {
    storage: xo.storage, egress_max: xo.egress_max, current: xo.current,
    switches: xo.switches
  };
  entry.text = calc.crossoverText(payload.catalog, storage, egress);
  out[key] = entry;
}
process.stdout.write(JSON.stringify(out));
"""
    node = os.environ.get("NODE_BIN", "node")
    env = dict(os.environ, TCO_ROOT=ROOT)
    proc = subprocess.run([node, "-e", script], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"node failed: {proc.stderr[:500]}")
    return json.loads(proc.stdout)


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def compare_crossover(key, p, j, fails):
    """Same switches, same order, same break-even points, same tie prices."""
    if j is None:
        fails.append((key, "crossover", "missing in js"))
        return
    ps, js = p["switches"], j["switches"]
    if len(ps) != len(js):
        fails.append((key, "crossover", f"switch count py={len(ps)} js={len(js)}"))
        return
    for i, (a, b) in enumerate(zip(ps, js)):
        if a["from_slug"] != b["from_slug"] or a["to_slug"] != b["to_slug"]:
            fails.append((key, "crossover",
                          f"switch {i} py={a['from_slug']}->{a['to_slug']} "
                          f"js={b['from_slug']}->{b['to_slug']}"))
            continue
        if not close(a["at"], b["at"], 1e-9):
            fails.append((key, "crossover", f"switch {i} at py={a['at']!r} js={b['at']!r}"))
        if not close(a["cost"], b["cost"]):
            fails.append((key, "crossover", f"switch {i} cost py={a['cost']!r} js={b['cost']!r}"))
    if not close(p["egress_max"], j["egress_max"]):
        fails.append((key, "crossover", f"egress_max py={p['egress_max']} js={j['egress_max']}"))
    pc, jc = p["current"], j["current"]
    if (pc is None) != (jc is None):
        fails.append((key, "crossover", "current null mismatch"))
    elif pc and pc["slug"] != jc["slug"]:
        fails.append((key, "crossover", f"current py={pc['slug']} js={jc['slug']}"))


def main():
    catalog = load_catalog()
    print(f"catalog: {len(catalog)} providers, {len(SCENARIOS)} scenarios")

    py = python_expected(catalog)
    js = run_node(catalog)

    fails = []
    pairs = 0
    for key in py:
        entry = js.get(key) or {}
        for slug, p in py[key]["rows"].items():
            j = (entry.get("rows") or {}).get(slug)
            pairs += 1
            if j is None:
                fails.append((key, slug, "missing in js"))
                continue
            for field in ("storage", "egress", "total"):
                if not close(p[field], j[field]):
                    fails.append((key, slug, f"{field}: py={p[field]!r} js={j[field]!r}"))
        compare_crossover(key, py[key]["xo"], entry.get("xo"), fails)

        # The sentences are what the visitor actually reads, so compare them
        # verbatim — a formatting divergence is a visible bug even when the
        # underlying arithmetic agrees.
        pt, jt = py[key]["text"], entry.get("text")
        if jt is None:
            fails.append((key, "text", "missing in js"))
        else:
            if pt["headline"] != jt["headline"]:
                fails.append((key, "text", f"headline py={pt['headline']!r} js={jt['headline']!r}"))
            if pt["items"] != jt["items"]:
                fails.append((key, "text", f"items py={pt['items']!r} js={jt['items']!r}"))

    if fails:
        print(f"\nFAIL — {len(fails)} mismatches")
        for key, slug, detail in fails[:25]:
            print(f"  {key:>12}  {slug:<24} {detail}")
        return 1

    print(f"PASS — {pairs} provider-scenario bills identical")
    print(f"PASS — {len(py)} crossover scans and {len(py)} rendered sentences identical")

    # Spot-check hand-computed values so a shared bug in both copies cannot
    # pass unnoticed.
    checks = [
        ("1|1", "aws-s3", "egress", 92.16, "1 TB out at $0.09/GB"),
        ("1|1", "backblaze", "egress", 0.0, "1 TB out within 3x of 1 TB stored"),
        ("2|6", "backblaze", "egress", 0.0, "6 TB out = 3x of 2 TB stored"),
        ("1|11", "aws-s3", "egress", 10 * 1024 * 0.09 + 1 * 1024 * 0.085, "tier boundary at 10 TB"),
        ("1|0", "wasabi", "total", 7.99, "1 TB floor, no egress"),
        ("0.5|0", "wasabi", "total", 7.99, "0.5 TB still bills the 1 TB floor"),
        ("0.1|0", "storj", "total", 5.0, "$5 monthly minimum"),
        ("1|1", "digitalocean-spaces", "egress", 0.0, "1 TB inside the bundled 1 TiB"),
        ("1|2", "digitalocean-spaces", "egress", 1 * 1024 * 0.01, "1 TB past the allowance"),
        # AWS storage tiers, from the official Price List: 50 TB then 450 TB.
        ("50|0", "aws-s3", "storage", 50 * 1024 * 0.023, "exactly the first storage tier"),
        ("500|0", "aws-s3", "storage", 50 * 1024 * 0.023 + 450 * 1024 * 0.022,
         "exactly the second storage tier"),
        ("1000|0", "aws-s3", "storage",
         50 * 1024 * 0.023 + 450 * 1024 * 0.022 + 500 * 1024 * 0.021,
         "1,000 TB across all three storage tiers"),
        ("2000|0", "aws-s3", "storage",
         50 * 1024 * 0.023 + 450 * 1024 * 0.022 + 1500 * 1024 * 0.021,
         "2,000 TB in the top storage tier"),
    ]
    hard = []
    by_slug = {r["slug"]: r for r in catalog}
    for key, slug, field, want, why in checks:
        storage_s, egress_s = key.split("|")
        got = tco.monthly(by_slug[slug], float(storage_s), float(egress_s))[field]
        ok = close(got, want, 1e-6)
        print(f"  {'PASS' if ok else 'FAIL'}  {slug:<22} {key:>8} {field:<8} got={got:<11.4f} want={want:<11.4f} {why}")
        if not ok:
            hard.append(f"{slug} {key} {field}")

    # Crossover spot-checks: the break-even is derivable by hand.
    # IDrive e2 stores 10 TB at $4.9583/TB ($49.583) and B2-style free egress
    # up to 3x stored; Wasabi is a flat $79.90 at any egress. They meet where
    # 49.583 + (e - 30) * 1024 * 0.01 == 79.90.
    want_at = 30 + (79.90 - 49.583) / (1024 * 0.01)
    xo = tco.crossover(catalog, 10.0, 1.0)
    first = xo["switches"][0] if xo["switches"] else None
    ok = bool(first) and abs(first["at"] - want_at) < 0.05 and first["to_slug"] == "wasabi"
    print(f"  {'PASS' if ok else 'FAIL'}  crossover 10 TB stored  first switch at "
          f"{first['at'] if first else 'none':.2f} TB (hand {want_at:.2f}) -> "
          f"{first['to_slug'] if first else '-'}")
    if not ok:
        hard.append("crossover break-even")

    # The break-even should scale with storage: both providers are linear in TB.
    xo100 = tco.crossover(catalog, 100.0, 1.0)
    scaled = xo100["switches"][0]["at"] if xo100["switches"] else 0
    ok = abs(scaled - want_at * 10) < 0.5
    print(f"  {'PASS' if ok else 'FAIL'}  crossover 100 TB stored first switch at "
          f"{scaled:.2f} TB (expect ~{want_at * 10:.2f})")
    if not ok:
        hard.append("crossover scaling")

    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
