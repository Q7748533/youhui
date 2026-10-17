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
    # Whole-cent amounts keep the familiar two decimals; per-GB style unit
    # prices like $0.015 must not be rounded into a wrong price.
    if abs(amount * 100 - round(amount * 100)) < 1e-9:
        return f"{symbol}{amount:,.2f}"
    return f"{symbol}{amount:,.4f}".rstrip("0").rstrip(".")


def display_time(value) -> str:
    """Render stored ISO-8601 UTC timestamps in a human-readable form."""
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
        suffix = " UTC"
    else:
        suffix = ""
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}, {parsed.strftime('%H:%M')}{suffix}"


def display_date(value) -> str:
    """Render a stored timestamp as a concise date for cards and summaries."""
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10]
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


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


def pricing_basis(offer: dict) -> str:
    value = str(offer.get("pricing_unit") or "unknown").strip().lower()
    return value or "unknown"


def pricing_basis_label(value: str) -> str:
    value = pricing_basis({"pricing_unit": value})
    if value == "unknown":
        return "billing basis not stated"
    return "per " + value.replace("/", " / ")


def observed_family(plan: str) -> str:
    """Classify only vocabulary visible in an official plan label."""
    low = plan.lower()
    if "microsoft" in low or "google workspace" in low or "office 365" in low:
        return "Cloud application backup"
    if "server" in low:
        return "Server backup"
    if "backup" in low:
        return "Backup"
    if "team" in low or "business" in low:
        return "Team / business"
    if "personal" in low or "home" in low:
        return "Personal / home"
    if "storage" in low or "drive" in low or "tb" in low or "gb" in low:
        return "Storage plans"
    return "Other plan labels"


def provider_insight(provider: dict) -> str:
    """Build a small source-derived profile instead of invented editorial copy."""
    offers = provider.get("offers", [])
    if not offers:
        note = provider.get("note") or "No plan labels or prices were readable on the latest public fetch."
        return (f'<div class="card"><strong>No public price record for {esc(provider.get("name", "this provider"))}</strong>'
                f'<p class="note" style="margin:8px 0 0">{esc(note)}</p></div>')
    families: dict[str, list[str]] = {}
    for offer in offers:
        family = observed_family(str(offer.get("plan", "")))
        families.setdefault(family, []).append(str(offer.get("plan", "")))
    family_items = "".join(
        f'<li><strong>{esc(family)}</strong>: {esc(", ".join(names))}</li>'
        for family, names in families.items()
    )
    bases = sorted({pricing_basis(offer) for offer in offers})
    basis_items = "".join(f"<li>{esc(pricing_basis_label(value))}</li>" for value in bases)
    return (
        '<div class="grid">'
        '<div class="card"><h3>Observed plan families</h3><ul class="clean">'
        f"{family_items}</ul></div>"
        '<div class="card"><h3>Billing bases found</h3><ul class="clean">'
        f'{basis_items}</ul><p class="note" style="margin:10px 0 0">These labels are grouped from the official page; different bases are not ranked together.</p></div>'
        "</div>"
    )


def offer_node(offer: dict, page_url: str) -> dict:
    node = {
        "@type": "Offer",
        "name": offer["title"],
        "url": page_url,
        "seller": {"@type": "Organization", "name": offer["provider"]},
    }
    if not offer.get("stale"):
        node["availability"] = "https://schema.org/OutOfStock" if is_expired(offer) else "https://schema.org/InStock"
    if offer.get("price") is not None and not offer.get("stale"):
        node["price"] = offer["price"]
        node["priceCurrency"] = offer["currency"]
        basis = pricing_basis(offer)
        if basis != "unknown":
            node["priceSpecification"] = {
                "@type": "UnitPriceSpecification",
                "price": offer["price"],
                "priceCurrency": offer["currency"],
                "unitText": basis,
            }
    if offer.get("valid_until") and not offer.get("stale"):
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
    updated_at_display = display_time(updated_at)

    base_tpl = read_template("base.html")
    providers = data["providers"]
    # Provider records carry the latest extracted fields; merge them into the
    # flat list so new fields such as billing basis cannot drift between views.
    provider_offer_index = {
        offer["id"]: offer
        for provider in providers
        for offer in provider.get("offers", [])
        if offer.get("id")
    }
    offers = [{**offer, **provider_offer_index.get(offer.get("id"), {})} for offer in data["offers"]]
    live_offers = [o for o in offers if not is_expired(o) and not o.get("stale")]
    priced = [p for p in providers if p["status"] in ("ok", "stale")]

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
        graph = [
            {
                "@type": "Organization",
                "@id": page_url("/") + "#organization",
                "name": brand,
                "url": page_url("/"),
                "logo": page_url("/og.png"),
                "description": conf.get("tagline", ""),
            },
            {
                "@type": "WebSite",
                "@id": page_url("/") + "#website",
                "name": brand,
                "url": page_url("/"),
                "publisher": {"@id": page_url("/") + "#organization"},
                "inLanguage": lang,
            },
        ]
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
            updated_at=esc(updated_at_display),
            jsonld=jsonld_block(ld),
        )

    urls: list[str] = []

    # ------------------------------------------------------------ index
    cards = []
    for p in providers:
        badge = {"ok": "ok", "stale": "warn", "no_price": "warn", "unreachable": "bad", "robots_disallow": "warn"}.get(p["status"], "warn")
        label = {"ok": f'{len(p["offers"])} plans', "stale": f'{len(p["offers"])} stale plans', "no_price": "no verified price",
                 "unreachable": "source unavailable", "robots_disallow": "robots disallow"}.get(p["status"], p["status"])
        cards.append(
            f'<article class="card"><div class="card-top"><span class="provider">{esc(p["name"])}</span>'
            f'<span class="tag {badge}">{esc(label)}</span></div>'
            f'<div class="plan">Official pricing source</div>'
            f'<div class="meta">{esc(p["site"])}</div>'
            f'<div class="card-actions"><a href="{root}providers/{esc(p["slug"])}/">View provider</a>'
            f'<a href="{esc(p["site"])}" rel="nofollow noopener" target="_blank">Official page ↗</a></div></article>'
        )

    offer_cards = []
    for o in sorted(live_offers, key=lambda item: (item.get("price") is None, item.get("price") or 0, item["provider"]))[:12]:
        if o.get("price") is not None:
            price_display = money(o["price"], o["currency"])
            price_class = "price"
            status = '<span class="tag warn">Previously verified</span>' if o.get("stale") else '<span class="tag ok">Verified</span>'
        else:
            price_display = "Price unavailable"
            price_class = "price muted"
            status = '<span class="tag warn">No verified price</span>'
        currency_label = f' <span class="cur">{esc(o["currency"])}</span>' if o.get("price") is not None else ""
        offer_cards.append(
            f'<article class="card"><div class="card-top"><span class="provider">{esc(o["provider"])}</span>{status}</div>'
            f'<div class="plan">{esc(o["plan"])}</div>'
            f'<div class="{price_class}">{esc(price_display)}{currency_label}</div>'
            f'<div class="meta">{"Last verified " if o.get("stale") else "Checked "}{esc(display_date(o["fetched_at"]))} · official source</div>'
            f'<div class="card-actions"><a href="{root}deals/{esc(o["id"])}/">View details</a>'
            f'<a href="{esc(o["offer_url"])}" rel="nofollow noopener" target="_blank">Official page ↗</a></div></article>'
        )
    if not offer_cards:
        offer_cards.append('<div class="card"><div class="plan">No verified price was available on the last run. Missing data stays missing.</div></div>')

    index_body = read_template("index.html").safe_substitute(
        headline=esc(f"Compare {niche} prices from official pages."),
        lede=esc(f"Verified plans, clear sources and conservative data. {len(priced)} of {len(providers)} providers currently expose a verifiable price."),
        providers_total=len(providers),
        offers_total=len(live_offers),
        providers_priced=len(priced),
        updated_at=esc(updated_at_display),
        cron_note=esc(conf.get("refresh_human", "6 hours")),
        provider_cards="\n".join(cards),
        offer_cards="\n".join(offer_cards),
        root=root,
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
    provider_rows = []
    for p in sorted(providers, key=lambda item: item["name"].lower()):
        badge = {"ok": "ok", "stale": "warn", "no_price": "warn", "unreachable": "warn", "robots_disallow": "warn"}.get(p["status"], "warn")
        label = {"ok": f'{len(p["offers"])} plans', "stale": f'{len(p["offers"])} stale plans', "no_price": "no verified price",
                 "unreachable": "unreachable", "robots_disallow": "robots disallow"}.get(p["status"], p["status"])
        provider_rows.append(
            f'<tr><td><a href="{root}providers/{esc(p["slug"])}/">{esc(p["name"])}</a></td>'
            f'<td>{len(p["offers"])}</td>'
            f'<td><span class="tag {badge}">{esc(label)}</span></td>'
            f'<td><a href="{esc(p["site"])}" rel="nofollow noopener" target="_blank">official site</a></td></tr>'
        )
    providers_body = read_template("providers.html").safe_substitute(
        breadcrumb=f'<a href="{root}">{esc(brand)}</a> / Providers',
        headline=esc(f"{brand} providers — {month}"),
        lede=esc(f"Official {niche} pricing sources tracked by this site. Provider pages show only plans and prices verifiable from public official pages."),
        provider_rows="\n".join(provider_rows),
        note=esc(f"{len(priced)} of {len(providers)} providers currently expose a verifiable price. Missing prices are left empty and never estimated."),
    )
    provider_items = [{"@type": "ListItem", "position": i + 1, "name": p["name"],
                       "url": page_url(f'/providers/{p["slug"]}/')} for i, p in enumerate(sorted(providers, key=lambda item: item["name"].lower()))]
    write("providers/index.html", page(
        f"{brand} providers — {month}",
        f"Official {niche} providers tracked by {brand}, with verified public pricing where available.",
        "/providers/", providers_body,
        {"@type": "ItemList", "name": f"{niche} providers", "itemListElement": provider_items},
    ))
    urls.append("/providers/")

    for p in providers:
        if p["offers"]:
            block = "<table><thead><tr><th>Plan</th><th>Price</th><th>Billing basis</th><th>Page</th></tr></thead><tbody>"
            for o in p["offers"]:
                expired = is_expired(o)
                price_cell = money(o["price"], o["currency"]) if o.get("price") is not None else "—"
                basis_cell = pricing_basis_label(pricing_basis(o))
                state_tag = " <span class='tag warn'>stale</span>" if o.get("stale") else " <span class='tag warn'>expired</span>" if expired else ""
                block += (f'<tr><td><a href="{root}deals/{esc(o["id"])}/">{esc(o["plan"])}</a></td>'
                          f'<td>{esc(price_cell)}{state_tag}</td>'
                          f'<td>{esc(basis_cell)}</td>'
                          f'<td><a href="{esc(o["source_url"])}" rel="nofollow noopener" target="_blank">source</a></td></tr>')
            block += "</tbody></table>"
        else:
            block = ('<div class="card"><div class="sub">This provider has no publicly verifiable price '
                     'on the last run, so no price is shown. Nothing is estimated.</div></div>')

        priced_offers = [o for o in p["offers"] if o.get("price") is not None and not o.get("stale")]
        groups: dict[tuple[str, str], list[dict]] = {}
        for offer in priced_offers:
            groups.setdefault((offer["currency"], pricing_basis(offer)), []).append(offer)
        if p.get("status") == "stale":
            comparison_summary = (f'Latest fetch unavailable. {len(p["offers"])} previously verified plan(s) are retained '
                                  "for reference and are not treated as current prices or ranked.")
        elif priced_offers:
            group_summaries = []
            for (currency, basis), group in sorted(groups.items()):
                low = money(min(o["price"] for o in group), currency)
                high = money(max(o["price"] for o in group), currency)
                range_text = low if low == high else f"{low} to {high}"
                group_summaries.append(f"{len(group)} at {range_text} {pricing_basis_label(basis)}")
            if len(group_summaries) == 1:
                comparison_summary = (f'{len(priced_offers)} verified plan(s) are listed at {group_summaries[0]}. '
                                      "Only plans with this same billing basis can be compared.")
            else:
                comparison_summary = (f'{len(priced_offers)} verified plan(s) use different billing bases: '
                                      f'{"; ".join(group_summaries)}. Prices are not combined into one ranking or range.')
        else:
            comparison_summary = "No current public price could be verified on this provider page during the latest fetch."
        comparable_groups = [group for (currency, basis), group in groups.items() if basis != "unknown"]
        if len(groups) == 1 and len(comparable_groups) == 1:
            only_group = comparable_groups[0]
            offers_node = {
                "@type": "AggregateOffer",
                "priceCurrency": only_group[0]["currency"],
                "lowPrice": min(o["price"] for o in only_group),
                "highPrice": max(o["price"] for o in only_group),
                "offerCount": len(only_group),
                "offers": [offer_node(o, page_url(f'/deals/{o["id"]}/')) for o in only_group],
            } if len(only_group) >= 2 else offer_node(only_group[0], page_url(f'/deals/{only_group[0]["id"]}/'))
        else:
            # Keep individually verified offers in structured data, but do not
            # claim one aggregate range when their billing bases differ/are unknown.
            offers_node = [offer_node(o, page_url(f'/deals/{o["id"]}/')) for o in priced_offers] or None

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
            comparison_summary=esc(comparison_summary),
            provider_insight=provider_insight(p),
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
        basis = pricing_basis(o)
        peer_offers = sorted(
            [peer for peer in offers
             if peer["provider_slug"] == o["provider_slug"]
             and peer.get("currency") == o.get("currency")
             and pricing_basis(peer) == basis
             and basis != "unknown"
             and peer.get("price") is not None
             and not peer.get("stale")],
            key=lambda peer: peer["price"],
        )
        if o.get("price") is not None and not o.get("stale") and len(peer_offers) >= 2:
            rank = next((i + 1 for i, peer in enumerate(peer_offers) if peer["id"] == o["id"]), None)
            comparison_summary = (f'This plan is {money(o["price"], o["currency"])} {pricing_basis_label(basis)} and ranks '
                                  f'{rank} of {len(peer_offers)} verified {o["provider"]} plan(s) with the same billing basis. '
                                  f'The comparable range is {money(peer_offers[0]["price"], o["currency"])} to '
                                  f'{money(peer_offers[-1]["price"], o["currency"])}.')
        elif o.get("price") is not None and o.get("stale"):
            comparison_summary = (f'This plan was previously verified at {money(o["price"], o["currency"])} '
                                  f'{pricing_basis_label(basis)}. The latest source fetch was unavailable, so no current ranking is shown.')
        elif o.get("price") is not None:
            comparison_summary = (f'This plan is {money(o["price"], o["currency"])} {pricing_basis_label(basis)}. '
                                  "No same-basis peer prices were found, so no ranking is shown.")
        else:
            comparison_summary = "A price comparison is not available because a current price could not be verified."
        body = read_template("deal.html").safe_substitute(
            breadcrumb=(f'<a href="{root}">{esc(brand)}</a> / '
                        f'<a href="{root}providers/{esc(o["provider_slug"])}/">{esc(o["provider"])}</a> / {esc(o["plan"])}'),
            deal_title=esc(o["title"]),
            deal_lede=esc(f'{o["provider"]} — {o["plan"]}. Price read from the official public pricing page, not estimated.'),
            price_display=esc(price_display),
            status_line=esc("Marked expired: the official validity date has passed." if expired
                            else (f'Previously verified on {display_date(o["fetched_at"])}; the latest source fetch was unavailable.'
                                  if o.get("stale") else f'Verified on {display_date(o["fetched_at"])} from the official page.')),
            offer_url=esc(o["offer_url"]),
            provider_url=f'{root}providers/{esc(o["provider_slug"])}/',
            provider_name=esc(o["provider"]),
            plan=esc(o["plan"]),
            price_line=esc(price_display),
            pricing_basis=esc(pricing_basis_label(pricing_basis(o))),
            valid_until=esc(o.get("valid_until") or "not published on the source page"),
            source_url=esc(o["source_url"]),
            fetched_at=esc(display_time(o["fetched_at"])),
            comparison_summary=esc(comparison_summary),
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
        priced_provider_offers = [o for o in p["offers"] if o.get("price") is not None]
        if priced_provider_offers:
            lowest = min(priced_provider_offers, key=lambda offer: offer["price"])
            low = money(lowest["price"], lowest["currency"])
            low_basis = pricing_basis_label(pricing_basis(lowest))
        else:
            low = "—"
            low_basis = "—"
        trows.append(
            f'<tr><td><a href="{root}providers/{esc(p["slug"])}/">{esc(p["name"])}</a></td>'
            f'<td>{esc(low)}</td><td>{esc(low_basis)}</td><td>{len(p["offers"])}</td>'
            f'<td><span class="tag {"ok" if p["status"] == "ok" else "warn"}">{esc(p["status"])}</span></td>'
            f'<td><a href="{esc(p["source"])}" rel="nofollow noopener" target="_blank">source</a></td></tr>'
        )
    cbody = read_template("compare.html").safe_substitute(
        breadcrumb=f'<a href="{root}">{esc(brand)}</a> / Compare',
        headline=esc(f"Compare {niche} providers — {month}"),
        lede=esc(f"Lowest listed price per provider, with its billing basis shown separately. Values come from official public pricing pages on {updated_at[:10]}."),
        table_rows="\n".join(trows),
        note=esc("The lowest listed values use different currencies, periods, and billing scopes across providers; they are not a normalized recommendation. Empty cells mean no public price was verified."),
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

    # ------------------------------------------------------------ about / contact
    about_body = read_template("about.html").safe_substitute(
        brand=esc(brand),
        niche=esc(niche),
        providers_total=len(providers),
        offers_total=len(live_offers),
        refresh_human=esc(conf.get("refresh_human", "6 hours")),
        updated_at=esc(updated_at_display),
    )
    write("about/index.html", page(
        f"About and methodology — {brand}",
        f"How {brand} collects and verifies public {niche} prices, including its update schedule and data limitations.",
        "/about/", about_body,
        {"@type": "AboutPage", "name": f"About {brand}", "description": f"Data sources and methodology for {brand}."},
    ))
    urls.append("/about/")

    contact_body = read_template("contact.html").safe_substitute()
    write("contact/index.html", page(
        f"Contact and site maintenance — {brand}",
        f"Site maintenance information for {brand}, an independent public {niche} pricing index.",
        "/contact/", contact_body,
        {"@type": "ContactPage", "name": f"Contact {brand}", "url": page_url("/contact/")},
    ))
    urls.append("/contact/")

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
