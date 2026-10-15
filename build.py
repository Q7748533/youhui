# ::ILANG
# [TYPE:module][PROJECT:cloud-storage-deals]
# ::STATE{@SELF, role:builder, reads:[.ilang/site.ilang, data/offers.json, templates/], writes:site/}
# ::RULE{一切由数据生成⇒title description canonical sitemap JSON-LD 都来自 offers.json 不许写字面量}
# ::RULE{结构化数据不许造⇒抓不到 price 或 priceValidUntil 就不写那个字段}
# ::BOUNDARY{never:编价格 编优惠 编佣金|scope:permanent}
"""Render the static site from .ilang/site.ilang + data/offers.json into site/."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import struct
import sys
import zlib
from datetime import datetime, timezone
from string import Template

import siteilang

SITE_DIR = "site"
TEMPLATE_DIR = "templates"
DATA_FILE = os.path.join("data", "offers.json")
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "CHF": "CHF "}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def money(amount, currency: str) -> str:
    symbol = SYMBOLS.get(currency, currency + " ")
    return f"{symbol}{amount:,.2f}"


def jsonld_block(obj) -> str:
    payload = json.dumps(obj, ensure_ascii=False, indent=None, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return '<script type="application/ld+json">' + payload + "</script>"


def read_template(name: str) -> Template:
    with open(os.path.join(TEMPLATE_DIR, name), "r", encoding="utf-8") as fh:
        return Template(fh.read())


def clean_output() -> None:
    """site/ is 100% generated. Wipe it so renamed or dropped providers do not
    leave orphan pages behind (they would stay in sitemap.xml forever)."""
    if SITE_DIR == "site" and os.path.isdir(SITE_DIR):
        shutil.rmtree(SITE_DIR)


def write(path: str, content: str) -> None:
    full = os.path.join(SITE_DIR, path)
    os.makedirs(os.path.dirname(full) or SITE_DIR, exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def is_expired(offer: dict) -> bool:
    raw = offer.get("valid_until")
    if not raw:
        return False
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except ValueError:
        return False


def dominant_currency(offers: list[dict], fallback: str) -> str:
    if not offers:
        return fallback
    counts: dict[str, int] = {}
    for o in offers:
        counts[o["currency"]] = counts.get(o["currency"], 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def offer_node(offer: dict, page_url: str) -> dict:
    node = {
        "@type": "Offer",
        "name": offer["title"],
        "url": page_url,
        "availability": "https://schema.org/OutOfStock" if is_expired(offer) else "https://schema.org/InStock",
        "seller": {"@type": "Organization", "name": offer["provider"]},
    }
    if offer.get("price") is not None:
        node["price"] = offer["price"]
        node["priceCurrency"] = offer["currency"]
    if offer.get("valid_until"):
        node["priceValidUntil"] = offer["valid_until"]
    return node


def write_og_image(path: str) -> None:
    """1200x630 brand gradient, pure stdlib (no image library available/desired)."""
    w, h = 1200, 630
    rows = []
    for y in range(h):
        t = y / (h - 1)
        r = int(31 + (11 - 31) * t)
        g = int(111 + (26 - 111) * t)
        b = int(235 + (51 - 235) * t)
        if 300 <= y < 306:  # accent rule
            r, g, b = 255, 255, 255
        row = bytes((r, g, b)) * w
        rows.append(b"\x00" + row)
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(os.path.join(SITE_DIR, path), "wb") as fh:
        fh.write(png)


def main(argv: list[str]) -> int:
    cfg = siteilang.load(argv[1] if len(argv) > 1 else siteilang.DEFAULT_PATH)
    conf = siteilang.settings(cfg)
    clean_output()
    clean_output()
    if not os.path.exists(DATA_FILE):
        print("data/offers.json missing — run `python scraper.py` first", file=sys.stderr)
        return 1
    with open(DATA_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    brand = cfg["site"].get("brand", "Deals Radar")
    niche_raw = cfg["site"].get("niche", "deals")
    niche = niche_raw.replace("-", " ")
    lang = conf.get("locale", "en-US")
    base = conf.get("base_url", "").rstrip("/")
    has_base = base.startswith("http")
    if not has_base:
        print("WARNING: base_url in .ilang/site.ilang is not a real URL — "
              "canonical/sitemap/robots will use root-relative paths until it is set.", file=sys.stderr)
    root = (base + "/") if has_base else "/"
    now = datetime.now(timezone.utc)
    month = now.strftime("%B %Y")
    updated_at = data["meta"].get("generated_at", now.replace(microsecond=0).isoformat())

    base_tpl = read_template("base.html")
    providers = data["providers"]
    offers = data["offers"]
    live_offers = [o for o in offers if not is_expired(o)]
    priced = [p for p in providers if p["status"] == "ok"]

    def url_for(path: str) -> str:
        """Absolute URL — used by sitemap.xml and robots.txt, which require it."""
        return (base + path) if has_base else path

    # Some hosts rotate their public hostname on every publish. With
    # canonical_relative the per-page canonical/OG/JSON-LD stay correct because
    # they resolve against whatever host is actually serving; only the sitemap
    # needs the absolute one, and that is what a fixed domain is for.
    def page_url(path: str) -> str:
        return path if conf.get("canonical_relative", "false").lower() == "true" else url_for(path)

    def page(title: str, description: str, path: str, body: str, extra_ld=None) -> str:
        graph = []
        if extra_ld:
            graph.append(extra_ld)
        graph.append({
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": brand, "item": page_url("/")},
                {"@type": "ListItem", "position": 2, "name": title, "item": page_url(path)},
            ],
        })
        ld = {"@context": "https://schema.org", "@graph": [g for g in graph if g]}
        return base_tpl.safe_substitute(
            lang=lang,
            title=esc(title),
            description=esc(description),
            canonical=esc(page_url(path)),
            brand=esc(brand),
            tagline=esc(conf.get("tagline", "")),
            root=root,
            body=body,
            updated_at=esc(updated_at),
            jsonld=jsonld_block(ld),
        )

    urls: list[str] = []

    # ------------------------------------------------------------ index
    cards = []
    for p in providers:
        badge = {"ok": "ok", "no_price": "warn", "unreachable": "warn", "robots_disallow": "warn"}.get(p["status"], "warn")
        label = {"ok": f'{len(p["offers"])} plans', "no_price": "no verified price",
                 "unreachable": "unreachable", "robots_disallow": "robots disallow"}.get(p["status"], p["status"])
        cards.append(
            f'<div class="card"><h3><a href="{root}providers/{esc(p["slug"])}/">{esc(p["name"])}</a></h3>'
            f'<div class="sub"><span class="tag {badge}">{esc(label)}</span></div>'
            f'<div class="sub">{esc(p["site"])}</div></div>'
        )
    rows = []
    for o in live_offers[:60]:
        price_cell = money(o["price"], o["currency"]) if o.get("price") is not None else "—"
        rows.append(
            f'<tr><td><a href="{root}deals/{esc(o["id"])}/">{esc(o["title"])}</a></td>'
            f'<td>{esc(price_cell)}</td>'
            f'<td><a href="{esc(o["source_url"])}" rel="nofollow noopener" target="_blank">official page</a></td></tr>'
        )
    if not rows:
        rows.append('<tr><td colspan="3" class="note">No verified price on the last run. '
                    'The pipeline leaves the table empty rather than filling it with estimates.</td></tr>')

    index_body = read_template("index.html").safe_substitute(
        headline=esc(f"{brand} — {niche} offers, {month}"),
        lede=esc(f"Official {niche} pricing, read straight from each provider's own public page. "
                 f"{len(priced)} of {len(providers)} providers currently expose a verifiable price."),
        providers_total=len(providers),
        offers_total=len(live_offers),
        providers_priced=len(priced),
        updated_at=esc(updated_at),
        cron_note=esc(conf.get("refresh_human", "6 hours")),
        provider_cards="\n".join(cards),
        deal_rows="\n".join(rows),
    )
    items = [{"@type": "ListItem", "position": i + 1, "name": o["title"], "url": page_url(f'/deals/{o["id"]}/')}
             for i, o in enumerate(live_offers[:60])]
    write("index.html", page(
        f"{brand} — {niche} offers, {month}",
        f"Verified {niche} prices from {len(providers)} official provider pages, refreshed {conf.get('refresh_human', 'every 6 hours')}. {len(live_offers)} offers.",
        "/", index_body,
        {"@type": "ItemList", "name": f"{niche} offers {month}", "itemListElement": items} if items else None,
    ))
    urls.append("/")

    # ------------------------------------------------------------ providers
    for p in providers:
        if p["offers"]:
            block = "<table><thead><tr><th>Plan</th><th>Price</th><th>Page</th></tr></thead><tbody>"
            for o in p["offers"]:
                expired = is_expired(o)
                price_cell = money(o["price"], o["currency"]) if o.get("price") is not None else "—"
                block += (f'<tr><td><a href="{root}deals/{esc(o["id"])}/">{esc(o["plan"])}</a></td>'
                          f'<td>{esc(price_cell)}{" <span class=\'tag warn\'>expired</span>" if expired else ""}</td>'
                          f'<td><a href="{esc(o["source_url"])}" rel="nofollow noopener" target="_blank">source</a></td></tr>')
            block += "</tbody></table>"
        else:
            block = ('<div class="card"><div class="sub">This provider has no publicly verifiable price '
                     'on the last run, so no price is shown. Nothing is estimated.</div></div>')

        currency = dominant_currency(p["offers"], conf.get("currency", "USD"))
        same = [o for o in p["offers"] if o["currency"] == currency]
        if len(same) >= 2:
            offers_node = {
                "@type": "AggregateOffer",
                "priceCurrency": currency,
                "lowPrice": min(o["price"] for o in same),
                "highPrice": max(o["price"] for o in same),
                "offerCount": len(same),
                "offers": [offer_node(o, page_url(f'/deals/{o["id"]}/')) for o in same],
            }
        elif same:
            offers_node = offer_node(same[0], page_url(f'/deals/{same[0]["id"]}/'))
        else:
            offers_node = None

        product = {
            "@type": "Product",
            "name": f'{p["name"]} {niche} plans',
            "brand": {"@type": "Brand", "name": p["name"]},
            "url": p["site"],
        }
        if offers_node:
            product["offers"] = offers_node

        body = read_template("provider.html").safe_substitute(
            breadcrumb=f'<a href="{root}">{esc(brand)}</a> / {esc(p["name"])}',
            provider_name=esc(p["name"]),
            provider_lede=esc(f'{p["name"]} {niche} pricing read from the official page on {updated_at[:10]}.'),
            site_url=esc(p["site"]),
            source_url=esc(p["source"]),
            offer_block=block,
            note=esc(p.get("note", "")),
        )
        write(f'providers/{p["slug"]}/index.html', page(
            f"{p['name']} {niche} plans and prices — {month}",
            f'{p["name"]}: {len(p["offers"])} plan(s) verified from the official pricing page. {p.get("note", "")}',
            f'/providers/{p["slug"]}/', body, product,
        ))
        urls.append(f'/providers/{p["slug"]}/')

    # ------------------------------------------------------------ deals
    for o in offers:
        expired = is_expired(o)
        price_display = money(o["price"], o["currency"]) if o.get("price") is not None else "Price not verifiable"
        body = read_template("deal.html").safe_substitute(
            breadcrumb=(f'<a href="{root}">{esc(brand)}</a> / '
                        f'<a href="{root}providers/{esc(o["provider_slug"])}/">{esc(o["provider"])}</a> / {esc(o["plan"])}'),
            deal_title=esc(o["title"]),
            deal_lede=esc(f'{o["provider"]} — {o["plan"]}. Price read from the official public pricing page, not estimated.'),
            price_display=esc(price_display),
            status_line=esc("Marked expired: the official validity date has passed." if expired
                            else f'Verified on {o["fetched_at"][:10]} from the official page.'),
            offer_url=esc(o["offer_url"]),
            provider_url=f'{root}providers/{esc(o["provider_slug"])}/',
            provider_name=esc(o["provider"]),
            plan=esc(o["plan"]),
            price_line=esc(price_display),
            valid_until=esc(o.get("valid_until") or "not published on the source page"),
            source_url=esc(o["source_url"]),
            fetched_at=esc(o["fetched_at"]),
            note=esc("This page carries only what the source page published. Missing fields are missing on purpose."),
        )
        write(f'deals/{o["id"]}/index.html', page(
            f'{o["title"]} — {price_display} ({month})',
            f'{o["title"]} at {price_display} {o["currency"]}. Read from the official {o["provider"]} pricing page.',
            f'/deals/{o["id"]}/', body, offer_node(o, page_url(f'/deals/{o["id"]}/')),
        ))
        urls.append(f'/deals/{o["id"]}/')

    # ------------------------------------------------------------ compare
    trows = []
    for p in sorted(providers, key=lambda x: (x["status"] != "ok", x["name"].lower())):
        prices = [o["price"] for o in p["offers"] if o.get("price") is not None]
        if prices:
            currency = dominant_currency(p["offers"], conf.get("currency", "USD"))
            low = money(min(prices), currency)
        else:
            low = "—"
        trows.append(
            f'<tr><td><a href="{root}providers/{esc(p["slug"])}/">{esc(p["name"])}</a></td>'
            f'<td>{esc(low)}</td><td>{len(p["offers"])}</td>'
            f'<td><span class="tag {"ok" if p["status"] == "ok" else "warn"}">{esc(p["status"])}</span></td>'
            f'<td><a href="{esc(p["source"])}" rel="nofollow noopener" target="_blank">source</a></td></tr>'
        )
    cbody = read_template("compare.html").safe_substitute(
        breadcrumb=f'<a href="{root}">{esc(brand)}</a> / Compare',
        headline=esc(f"Compare {niche} providers — {month}"),
        lede=esc(f"Lowest verified price per provider, taken from each official public pricing page on {updated_at[:10]}."),
        table_rows="\n".join(trows),
        note=esc("Empty cells mean the price could not be verified publicly. They are never filled with estimates."),
    )
    write("compare/index.html", page(
        f"Compare {niche} providers — {month}",
        f"Side-by-side {niche} pricing for {len(providers)} providers, refreshed {conf.get('refresh_human', 'every 6 hours')}.",
        "/compare/", cbody,
        {"@type": "ItemList", "name": f"{niche} providers",
         "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": p["name"],
                              "url": page_url(f'/providers/{p["slug"]}/')} for i, p in enumerate(providers)]},
    ))
    urls.append("/compare/")

    # ------------------------------------------------------------ index files
    lastmod = updated_at
    locs = "\n".join(
        f"  <url><loc>{esc(url_for(u))}</loc><lastmod>{esc(lastmod)}</lastmod></url>" for u in urls
    )
    write("sitemap.xml",
          '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + locs + "\n</urlset>\n")
    write("robots.txt",
          "User-agent: *\nAllow: /\n\nSitemap: " + url_for("/sitemap.xml") + "\n")
    write_og_image("og.png")

    print(f"built {len(urls)} pages -> {SITE_DIR}/  (offers: {len(offers)}, live: {len(live_offers)})")
    if not has_base:
        print("REMINDER: set base_url in .ilang/site.ilang to the real URL and rebuild (STEP 5b).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
