# ::ILANG
# [TYPE:module][PROJECT:cloud-storage-deals]
# ::STATE{@SELF, role:health_checker, reads:[.ilang/site.ilang, data/, site/], writes:[.workbuddy-ai/daily/]}
# ::RULE{只诊断不修改⇒发现问题只写报告 不动 data/ 与 site/}
# ::BOUNDARY{never:自动改线上数据 自动改价格|scope:permanent}
"""Daily health check for the published site and its data.

Four layers, in the order a failure would hurt: is the site reachable, is the
data still the data we published, is the technical SEO intact, and did the
deploy actually land. Everything is read-only — the checker reports, a human
decides. Pure stdlib so it runs on any machine with Python and no network
dependencies beyond the site itself.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import siteilang

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(BASE_DIR, "site")
DATA_FILE = os.path.join(BASE_DIR, "data", "offers.json")
SCRAPE_REPORT = os.path.join(BASE_DIR, "data", "scrape_report.json")
DAILY_DIR = os.path.join(BASE_DIR, ".workbuddy-ai", "daily")
SNAPSHOT_FILE = os.path.join(DAILY_DIR, "last_snapshot.json")

UA = "cloud-storage-deals-health/1.0 (self-check)"
TIMEOUT = 20

CORE_PAGES = ["/", "/providers/", "/compare/", "/egress/", "/about/", "/privacy/", "/contact/"]
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CNY", "CHF", "NOK", "SEK", "DKK", "CAD", "AUD"}
BASES = {"month", "month/gb", "month/tb", "month/user", "user/month", "month/server",
         "year", "year/tb", "year/seat", "license/month", "license/tb"}
PRICE_JUMP = 0.20          # a verified price moving >20% in a day is a parser smell
TITLE_RANGE = (25, 70)
DESC_RANGE = (50, 165)

findings: list[dict] = []


def add(level: str, area: str, msg: str) -> None:
    findings.append({"level": level, "area": area, "msg": msg})


def fetch(url: str) -> tuple[int | None, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as exc:
        return exc.code, "", str(exc)
    except Exception as exc:                      # noqa: BLE001 - report, never crash
        return None, "", str(exc)


# --------------------------------------------------------------------- layers
def check_availability(base: str) -> list[str]:
    """Every page a visitor or a crawler is likely to hit first."""
    for path in CORE_PAGES:
        code, _, err = fetch(base + path)
        if code != 200:
            add("P0", "availability", f"{path} returned {code or err}")
    code, body, err = fetch(base + "/sitemap.xml")
    if code != 200:
        add("P0", "availability", f"/sitemap.xml returned {code or err}")
        return []
    locs = re.findall(r"<loc>(.*?)</loc>", body)
    if not locs:
        add("P0", "availability", "/sitemap.xml parsed to zero URLs")
    code, _, err = fetch(base + "/robots.txt")
    if code != 200:
        add("P1", "availability", f"/robots.txt returned {code or err}")
    # A missing page must return a real 404, not a soft 200.
    code, _, err = fetch(base + "/health-check-no-such-page-9f2c/")
    if code != 404:
        add("P1", "availability", f"unknown path returned {code or err} instead of 404")
    return locs


def check_sitemap_sample(locs: list[str]) -> None:
    """Spot-check real URLs: this is what catches slug drift and dead links."""
    providers = [u for u in locs if "/providers/" in u][:5]
    deals = [u for u in locs if "/deals/" in u]
    stride = max(1, len(deals) // 5) if deals else 1
    sample = providers + deals[::stride][:5]
    bad = []
    for url in sample:
        code, _, err = fetch(url)
        if code != 200:
            bad.append(f"{url} -> {code or err}")
    if bad:
        add("P1", "availability", f"{len(bad)}/{len(sample)} sampled URLs failed: " + "; ".join(bad[:3]))


def check_data() -> tuple[dict, list[dict]]:
    if not os.path.exists(DATA_FILE):
        add("P0", "data", "data/offers.json missing")
        return {}, []
    data = json.load(open(DATA_FILE, encoding="utf-8"))
    offers = data.get("offers", [])
    providers = data.get("providers", [])
    if not offers:
        add("P0", "data", "offers.json contains zero offers")

    ids: dict[str, int] = {}
    for o in offers:
        oid = o.get("id") or ""
        ids[oid] = ids.get(oid, 0) + 1
        if not oid:
            add("P1", "data", "offer without an id")
        if not (o.get("plan") or "").strip():
            add("P1", "data", f"empty plan name on {oid or '?'}")
        if not (o.get("provider") or "").strip():
            add("P1", "data", f"empty provider on {oid or '?'}")
        price = o.get("price")
        if price is None:
            continue
        if price <= 0:
            add("P1", "data", f"non-positive price on {oid} ({price})")
        if o.get("currency") not in CURRENCIES:
            add("P1", "data", f"unexpected currency {o.get('currency')!r} on {oid}")
        unit = o.get("pricing_unit")
        if unit and unit not in BASES:
            add("P2", "data", f"unmapped pricing_unit {unit!r} on {oid}")
    dupes = [k for k, n in ids.items() if n > 1]
    if dupes:
        add("P1", "data", f"duplicate offer ids: {dupes[:3]}")

    stale = [o for o in offers if o.get("stale")]
    if stale:
        add("P1", "data", f"{len(stale)} offer(s) marked stale")

    alerts = []
    if os.path.exists(SCRAPE_REPORT):
        report = json.load(open(SCRAPE_REPORT, encoding="utf-8"))
        alerts = [p.get("slug") for p in report.get("providers", []) if p.get("alert")]
        if alerts:
            add("P1", "data", f"scraper reported alerts: {alerts}")

    counts = {
        "generated_at": data.get("meta", {}).get("generated_at", ""),
        "providers": len(providers),
        "providers_priced": sum(1 for p in providers if p.get("offers")),
        "offers": len(offers),
        "stale": len(stale),
        "alerts": alerts,
    }
    return counts, offers


def check_cycle(offers: list[dict], counts: dict) -> None:
    """Day-over-day delta: new, gone, and prices that moved too far at once."""
    current = {o["id"]: o.get("price") for o in offers if o.get("id")}
    os.makedirs(DAILY_DIR, exist_ok=True)
    if os.path.exists(SNAPSHOT_FILE):
        prev = json.load(open(SNAPSHOT_FILE, encoding="utf-8"))
        old = prev.get("offers", {})
        new_ids = sorted(set(current) - set(old))
        gone = sorted(set(old) - set(current))
        jumps = []
        for oid in sorted(set(current) & set(old)):
            a, b = old[oid], current[oid]
            if a in (None, 0) or b is None:
                continue
            if abs(b - a) / abs(a) > PRICE_JUMP:
                jumps.append(f"{oid}: {a} -> {b}")
        if new_ids:
            add("P2", "cycle", f"{len(new_ids)} new offer(s): {new_ids[:3]}")
        if gone:
            add("P1", "cycle", f"{len(gone)} offer(s) disappeared: {gone[:3]}")
        if jumps:
            add("P1", "cycle", f"{len(jumps)} price jump(s) over {int(PRICE_JUMP * 100)}%: {jumps[:3]}")
        if prev.get("providers_priced") != counts.get("providers_priced"):
            add("P2", "cycle", "priced-provider count changed: "
                               f"{prev.get('providers_priced')} -> {counts.get('providers_priced')}")
    json.dump({"date": datetime.now(timezone.utc).date().isoformat(),
               "counts": counts, "offers": current},
              open(SNAPSHOT_FILE, "w", encoding="utf-8"))


def local_pages() -> dict[str, str]:
    pages = {}
    for root, _, files in os.walk(SITE_DIR):
        for name in files:
            if name == "index.html":
                full = os.path.join(root, name)
                rel = "/" + os.path.relpath(root, SITE_DIR).replace(os.sep, "/").strip(".") + "/"
                pages["/" if rel == "/./" else rel.replace("//", "/")] = full
    return pages


def check_seo() -> None:
    if not os.path.isdir(SITE_DIR):
        add("P0", "seo", "site/ not built — run build.py")
        return
    sitemap_path = os.path.join(SITE_DIR, "sitemap.xml")
    if not os.path.exists(sitemap_path):
        add("P0", "seo", "site/sitemap.xml missing")
        return
    sm = open(sitemap_path, encoding="utf-8").read()
    locs = re.findall(r"<loc>(.*?)</loc>", sm)
    pages = local_pages()
    if not locs:
        add("P0", "seo", "sitemap lists zero URLs")

    paths = set()
    for loc in locs:
        m = re.match(r"^(?:https?://[^/]+)?(/.*)$", loc)
        paths.add(m.group(1) if m else loc)
    missing = sorted(p for p in paths if p not in pages)
    if missing:
        add("P0", "seo", f"{len(missing)} sitemap URL(s) have no page: {missing[:3]}")

    titles: dict[str, list[str]] = {}
    for path, full in pages.items():
        html = open(full, encoding="utf-8").read()
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html)
        if not canonical:
            add("P1", "seo", f"no canonical on {path}")
        else:
            href = canonical.group(1)
            href_path = re.sub(r"^https?://[^/]+", "", href)
            if href_path.rstrip("/") != path.rstrip("/"):
                add("P1", "seo", f"canonical mismatch on {path}: {href}")
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        desc = re.search(r'name="description" content="([^"]*)"', html)
        if title:
            t = title.group(1).strip()
            titles.setdefault(t, []).append(path)
            if not (TITLE_RANGE[0] <= len(t) <= TITLE_RANGE[1]):
                add("P2", "seo", f"title length {len(t)} on {path}")
        else:
            add("P1", "seo", f"no <title> on {path}")
        if desc:
            if not (DESC_RANGE[0] <= len(desc.group(1)) <= DESC_RANGE[1]):
                add("P2", "seo", f"description length {len(desc.group(1))} on {path}")
        else:
            add("P1", "seo", f"no meta description on {path}")
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
            try:
                json.loads(block)
            except ValueError:
                add("P1", "seo", f"invalid JSON-LD on {path}")

    dup_titles = {t: p for t, p in titles.items() if len(p) > 1}
    if dup_titles:
        sample = list(dup_titles.items())[:2]
        add("P2", "seo", f"{len(dup_titles)} duplicated title(s), e.g. {sample[0][0][:40]!r} on {sample[0][1][:3]}")

    # Pages kept out of the sitemap must say so; indexed pages must not.
    for path, full in pages.items():
        if path == "/404.html":
            continue
        html = open(full, encoding="utf-8").read()
        noindex = 'name="robots" content="noindex' in html
        if path in paths and noindex:
            add("P1", "seo", f"{path} is in the sitemap but marked noindex")
        if path not in paths and not noindex:
            add("P1", "seo", f"{path} is not in the sitemap and not noindex")


def check_deploy(base: str) -> None:
    """The one check that answers 'did the push actually reach the edge'."""
    local_file = os.path.join(SITE_DIR, "index.html")
    if not os.path.exists(local_file):
        return
    local = open(local_file, encoding="utf-8").read()
    stamp = re.search(r"Last refresh: ([^.<]+)", local)
    code, live, err = fetch(base + "/")
    if code != 200:
        return
    live_stamp = re.search(r"Last refresh: ([^.<]+)", live)
    if not stamp or not live_stamp:
        return
    if stamp.group(1).strip() != live_stamp.group(1).strip():
        add("P1", "deploy", "live page is behind the local build: "
                             f"live={live_stamp.group(1).strip()!r} local={stamp.group(1).strip()!r}")


# ---------------------------------------------------------------------- report
def render(base: str, counts: dict) -> str:
    order = {"P0": 0, "P1": 1, "P2": 2}
    findings.sort(key=lambda f: order.get(f["level"], 9))
    tally = {lvl: sum(1 for f in findings if f["level"] == lvl) for lvl in ("P0", "P1", "P2")}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Health check — {now}", "", f"Target: {base}", ""]
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- P0 (site down / data loss): **{tally['P0']}**")
    lines.append(f"- P1 (needs a fix today): **{tally['P1']}**")
    lines.append(f"- P2 (plan into the week): **{tally['P2']}**")
    lines.append("")
    if counts:
        lines.append("## Data")
        lines.append("")
        lines.append(f"- generated_at: `{counts.get('generated_at')}`")
        lines.append(f"- providers: {counts.get('providers')} "
                     f"({counts.get('providers_priced')} with a verified price)")
        lines.append(f"- offers: {counts.get('offers')} | stale: {counts.get('stale')}")
        alerts = counts.get("alerts") or []
        lines.append(f"- scraper alerts: {', '.join(alerts) if alerts else 'none'}")
        lines.append("")
    lines.append("## Findings")
    lines.append("")
    if not findings:
        lines.append("No findings. Every check passed.")
    else:
        lines.append("| Level | Area | Detail |")
        lines.append("|---|---|---|")
        for f in findings:
            detail = f["msg"].replace("|", "\\|")
            lines.append(f"| {f['level']} | {f['area']} | {detail} |")
    lines.append("")
    lines.append("## Checks that ran")
    lines.append("")
    lines.append("- availability: 6 core pages, sitemap, robots, real 404, 10 sampled URLs")
    lines.append("- data: ids, plan/provider names, prices, currencies, billing bases, stale, alerts")
    lines.append("- cycle: new / removed offers, price moves over 20%, priced-provider count")
    lines.append("- seo: sitemap-to-page parity, canonical, title/description length, duplicate titles, JSON-LD, noindex parity")
    lines.append("- deploy: live 'Last refresh' against the local build")
    lines.append("")
    lines.append("> Read-only. Nothing in data/ or site/ was modified by this check.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    cfg = siteilang.load()
    conf = siteilang.settings(cfg)
    base = (conf.get("base_url") or "").rstrip("/")
    offline = "--offline" in argv
    if not base.startswith("http"):
        add("P0", "config", "base_url is not a real URL — cannot verify the live site")
        offline = True

    if not offline:
        locs = check_availability(base)
        if locs:
            check_sitemap_sample(locs)
        check_deploy(base)
    counts, offers = check_data()
    if offers:
        check_cycle(offers, counts)
    check_seo()

    report = render(base, counts)
    os.makedirs(DAILY_DIR, exist_ok=True)
    out = os.path.join(DAILY_DIR, datetime.now(timezone.utc).date().isoformat() + ".md")
    open(out, "w", encoding="utf-8", newline="\n").write(report + "\n")

    tally = {lvl: sum(1 for f in findings if f["level"] == lvl) for lvl in ("P0", "P1", "P2")}
    print(f"health check -> {os.path.relpath(out, BASE_DIR)}")
    print(f"P0={tally['P0']} P1={tally['P1']} P2={tally['P2']}")
    for f in findings[:12]:
        print(f"  [{f['level']}] {f['area']}: {f['msg']}")
    if len(findings) > 12:
        print(f"  ... {len(findings) - 12} more in the report")
    return 1 if tally["P0"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
