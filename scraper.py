# ::ILANG
# [TYPE:module][PROJECT:cloud-storage-deals]
# ::STATE{@SELF, role:scraper, reads:[.ilang/site.ilang], writes:data/offers.json}
# ::RULE{only_public_official_pages⇒先查 robots.txt 不绕反爬 不抓登录后内容}
# ::RULE{一条优惠一个真实方案⇒去重键 厂商+方案名+币种 抓不到方案名或当期售价就丢弃}
# ::BOUNDARY{never:编价格 编优惠 编 valid_until|scope:permanent}
"""Fetch official public pricing pages listed in .ilang/site.ilang and write data/offers.json.

Standard library only. No API keys, no LLM, no headless browser.
Everything on the site comes from this file; if a price cannot be verified
on the official page, the field is left out rather than guessed.
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from datetime import datetime, timezone

import siteilang

UA = "promo-radar-bot/1.0"
TIMEOUT = 25
DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "offers.json")

# Contexts that are NOT the current selling price: renewal, struck-through
# original price, "after the first term", fine print.
EXCLUDE_CONTEXT = (
    "renew", "renewal", "regular price", "regularly", "original", "originally",
    "was ", "msrp", "rrp", "list price", "strikethrough", "strike-through",
    "after 12", "after the first", "second year", "2nd year", "year 2",
    "then ", "thereafter", "from month", "billed at", "renews at", "usually",
    "save", "cashback", "discount", "you keep", "refund",
)
PLAN_STOPWORDS = (
    "pricing", "plans", "plan", "compare", "storage", "buy now", "get started",
    "sign up", "learn more", "free", "try free", "contact", "features",
    "personal", "business", "home", "menu", "login", "log in",
    "monthly", "annual", "annually", "yearly", "billed monthly", "billed annually",
    "best value", "per month", "per year", "most popular", "popular",
)
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
MONEY_RE = re.compile(
    r"(?:[$€£¥]\s?\d[\d.,]{0,9})|(?:\d[\d.,]{0,9}\s?[$€£¥])|"
    r"(?:\b(?:USD|EUR|GBP|JPY)\s?\d[\d.,]{0,9})"
)
SCRIPT_RE = re.compile(r"<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", re.S | re.I)

# A numeric price is not comparable until its billing basis is known. Keep the
# vocabulary deliberately small and conservative; an unknown basis is safer
# than silently comparing unlike units such as seat/year and server/month.
PERIODS = r"month|months|mo|year|years|yr|week|weeks|day|days"
SCOPES = r"server|servers|seat|seats|user|users|account|accounts|device|devices|license|licenses|tb|gb"
TAG_RE = re.compile(r"<[^>]+>")
HEADING_RE = re.compile(r"<h([1-4])[^>]*>(.*?)</h\1>", re.S | re.I)
# Only title/name-like containers are treated as plan names. Generic
# `pricing__plans` wrappers and feature lists must not become plan names.
# `li` is deliberately excluded: feature bullet lists are <li> and their items
# are not plans. A miss costs a row, a false positive invents one.
NAMED_RE = re.compile(
    r"<(?:div|section|article|span|td|th|p|strong|h[1-6])[^>]*class=\"[^\"]*"
    r"(?:title|name|heading|tier|packag)[^\"]*\"[^>]*>",
    re.I,
)
NAME_REJECT = (
    "frequently asked", "faq", "questions", "contact us", "help center",
    "privacy policy", "terms of service", "cookie", "newsletter", "blog",
)
# Encryption/compliance jargon belongs to feature blurbs, not to plan names.
TECH_REJECT = (
    "aes-", "rsa-", "ssl", "tls ", "256-bit", "128-bit", "gdpr", "hipaa",
    "soc 2", "iso 27001", "two-factor", "end-to-end", "zero-knowledge",
    "backup your", "unlimited devices", "file versioning",
)
LD_JSON_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I
)
IDRIVE_ADDONS = (
    ("Server Backup", "server-cloud-backup"),
    ("Microsoft Office 365 Backup", "microsoft-office-365-backup"),
    ("Google Workspace Backup", "google-workspace-backup"),
)
SPECIAL_PRICING_UNITS = {
    "sync-com": "user/month",
    "jottacloud": "month",
    "proton-drive": "month",
}
SPECIAL_PLAN_UNITS = {
    ("storj", "standard"): "month/tb",
    ("storj", "advanced"): "license/tb",
    ("storj", "teams"): "license/month",
    ("storj", "bring-your-own-cloud"): "license/month",
}


# ---------------------------------------------------------------- dedicated parsers
# Pages whose static structure confuses the generic heuristic (marketing
# headlines near prices, calculators, usage-based tables) get a URL-keyed
# parser. A dedicated parser only emits rows it can bind to a named plan and
# an explicit billing basis; when the page markup changes and the patterns
# stop matching, it returns rows=[] and the safe "no verified price" path
# applies instead of a wrong binding.

def _flat_text(html: str) -> str:
    return re.sub(r"\s+", " ", _clean_text(SCRIPT_RE.sub(" ", html)))


def _dedicated_row(plan: str, price: float, currency: str, page_url: str,
                   unit: str, method: str) -> dict:
    return {
        "plan": plan,
        "price": price,
        "currency": currency,
        "offer_url": page_url,
        "valid_until": None,
        "pricing_unit": unit,
        "method": method,
    }


def from_bunny(html: str, page_url: str, default_currency: str) -> list[dict]:
    flat = _flat_text(html)
    out = []
    edge_at = flat.find("Edge tier SSD")
    standard_part = flat[:edge_at] if edge_at >= 0 else flat
    edge_part = flat[edge_at:] if edge_at >= 0 else ""
    for m in re.finditer(r"(Single region|Two regions|Three regions)\s+\$([\d.]+)/GB", standard_part):
        got = parse_money(f"${m.group(2)}", default_currency)
        if got:
            out.append(_dedicated_row(
                f"Standard tier HDD ({m.group(1).lower()})", got[0], got[1],
                page_url, "month/gb", "dedicated"))
    if edge_part:
        m = re.search(r"Single region\s+\$([\d.]+)/GB", edge_part)
        if m:
            got = parse_money(f"${m.group(1)}", default_currency)
            if got:
                out.append(_dedicated_row(
                    "Edge tier SSD (single region)", got[0], got[1],
                    page_url, "month/gb", "dedicated"))
    return out


def from_r2(html: str, page_url: str, default_currency: str) -> list[dict]:
    flat = _flat_text(html)
    out = []
    # The R2 table is column-per-plan but row-major when flattened:
    # "Storage $X / GB-month $Y / GB-month" = Standard then Infrequent Access.
    m = re.search(r"Storage\s+\$([\d.]+)\s*/\s*GB-month\s+\$([\d.]+)\s*/\s*GB-month", flat)
    if m:
        for name, token in (("Standard storage", m.group(1)),
                            ("Infrequent Access storage", m.group(2))):
            got = parse_money(f"${token}", default_currency)
            if got:
                out.append(_dedicated_row(name, got[0], got[1],
                                          page_url, "month/gb", "dedicated"))
    return out


def from_backblaze_b2(html: str, page_url: str, default_currency: str) -> list[dict]:
    flat = _flat_text(html)
    out = []
    # The page is served in two variants: a newer card layout
    # ("Starts at $ 6.95 / TB / mo") and an older prose copy
    # ("on-demand cloud storage at $5/TB/month"). Handle both; when neither
    # pattern is found the safe empty result applies.
    cards = re.findall(r"Starts at \$\s?([\d.]+)\s*/\s*TB\s*/\s*mo\b", flat)
    if len(cards) >= 2:
        for name, token in (("B2 Cloud Storage", cards[0]), ("B2 Overdrive", cards[1])):
            got = parse_money(f"${token}", default_currency)
            if got:
                out.append(_dedicated_row(name, got[0], got[1],
                                          page_url, "month/tb", "dedicated"))
        return out
    m = re.search(r"on-demand cloud storage at \$([\d.]+)\s*/TB/month", flat)
    if m:
        got = parse_money(f"${m.group(1)}", default_currency)
        if got:
            out.append(_dedicated_row("B2 Cloud Storage", got[0], got[1],
                                      page_url, "month/tb", "dedicated"))
    m = re.search(r"Built on top of Backblaze B2, pricing starts at \$([\d.]+) per TB/month", flat)
    if m:
        got = parse_money(f"${m.group(1)}", default_currency)
        if got:
            out.append(_dedicated_row("B2 Overdrive", got[0], got[1],
                                      page_url, "month/tb", "dedicated"))
    return out


def from_backblaze_backup(html: str, page_url: str, default_currency: str) -> list[dict]:
    flat = _flat_text(html)
    out = []
    for m in re.finditer(r"(Personal|Business) backup\s+[^\$]{0,30}?\$([\d.]+)\s*/\s*year", flat):
        got = parse_money(f"${m.group(2)}", default_currency)
        if got:
            out.append(_dedicated_row(f"{m.group(1)} backup", got[0], got[1],
                                      page_url, "year", "dedicated"))
    return out


def from_wasabi(html: str, page_url: str, default_currency: str) -> list[dict]:
    flat = _flat_text(html)
    m = re.search(r"Starting at \$?([\d.]+)\s*(?:per\s+TB/month|TB/month)", flat)
    if not m:
        return []
    got = parse_money(f"${m.group(1)}", default_currency)
    if not got:
        return []
    return [_dedicated_row("Pay as you go", got[0], got[1], page_url, "month/tb", "dedicated")]


def from_idrive_e2(html: str, page_url: str, default_currency: str) -> list[dict]:
    flat = _flat_text(html)
    matches = list(re.finditer(r"Starts at \$([\d.]+)\s*/\s*year", flat))
    if not matches:
        return []
    veeam_at = flat.find("For Veeam Users")
    out = []
    standard = next((m for m in matches if veeam_at < 0 or m.start() < veeam_at), None)
    if standard:
        got = parse_money(f"${standard.group(1)}", default_currency)
        if got:
            out.append(_dedicated_row("1 TB", got[0], got[1], page_url, "year/tb", "dedicated"))
    veeam = next((m for m in matches if veeam_at >= 0 and m.start() > veeam_at), None)
    if veeam:
        got = parse_money(f"${veeam.group(1)}", default_currency)
        if got:
            out.append(_dedicated_row(
                "1 TB (Veeam/MSP/Resellers)", got[0], got[1], page_url, "year/tb", "dedicated"))
    return out


def from_dropbox(html: str, page_url: str, default_currency: str) -> list[dict]:
    flat = _flat_text(html)
    m = re.search(
        r"Price\s+\$([\d.]+)\s*/\s*month\s+\$([\d.]+)\s*/\s*user\s*/\s*month"
        r"\s+\$([\d.]+)\s*/\s*user\s*/\s*month", flat)
    if not m:
        return []
    plans = (("Plus", m.group(1), "month"),
             ("Standard", m.group(2), "month/user"),
             ("Advanced", m.group(3), "month/user"))
    out = []
    for name, token, unit in plans:
        got = parse_money(f"${token}", default_currency)
        if got:
            out.append(_dedicated_row(name, got[0], got[1], page_url, unit, "dedicated"))
    return out


def from_jottacloud(html: str, page_url: str, default_currency: str) -> list[dict]:
    flat = _flat_text(html)
    out = []
    # The pricing page serves currency variants (NOK/USD/EUR) with one stable
    # structure: "<plan> <CUR> X / month <CUR> Y / year". Both billing options
    # are current published prices, so each becomes its own record.
    cur = r"(?:NOK|USD|EUR)"
    pattern = re.compile(
        rf"((?:Unlimited\*?|Home \d+ TB|Pro \d+ TB|Business [A-Z][a-z]+))\s+"
        rf"({cur})\s?([\d.]+)\s*/\s*month\s+({cur})\s?([\d.]+)\s*/\s*year")
    for m in pattern.finditer(flat):
        plan = m.group(1).rstrip("*").strip()
        for token, unit, cur_group in ((m.group(3), "month", 2), (m.group(5), "year", 4)):
            got = parse_money(f"{m.group(cur_group)} {token}", default_currency)
            if got:
                out.append(_dedicated_row(plan, got[0], got[1], page_url, unit, "dedicated"))
    return out


def from_zoolz(html: str, page_url: str, default_currency: str) -> list[dict]:
    """Zoolz pricing cards (WordPress/ElementsKit markup).

    Only Monthly cards are recorded. The same page ships two conflicting sets
    of Yearly figures — static markup says one thing while an inline script
    rewrites every ``.yearly-plan`` wrapper with a different array at load —
    so Yearly cannot be verified as a current price and is skipped. The
    stale ``og:description`` mentions yet another Monthly figure; it is a
    cached summary, not the rendered card, and is not trusted.
    """
    out = []
    price_re = re.compile(
        r'elementskit-pricing-price">\s*'
        r'<sup[^>]*>\s*\$\s*</sup>\s*'
        r'<span>\s*([\d,]+(?:\.\d+)?)\s*</span>\s*'
        r'<sub[^>]*>\s*(Monthly|Yearly)\s*</sub>', re.S | re.I)
    title_re = re.compile(r'>\s*([^<]{3,80}?)\s*<')
    for m in price_re.finditer(html):
        period = m.group(2)
        if period.lower() != "monthly":
            continue
        i = html.rfind("elementskit-pricing-title", 0, m.start())
        if i < 0:
            continue
        tm = title_re.search(html[i:i + 300])
        title = tm.group(1).strip() if tm else ""
        if not title:
            continue
        # Normalize spacing so the same plan repeated across page sections
        # ("24TB" vs "24 TB") collapses to one dedupe key.
        title = re.sub(r"(\d)\s*(TB|GB)\b", r"\1 \2", title, flags=re.I)
        got = parse_money(f"${m.group(1).replace(',', '')}", default_currency)
        if got:
            out.append(_dedicated_row(title, got[0], got[1], page_url, "month", "dedicated"))
    return out


def from_icloud(html: str, page_url: str, default_currency: str) -> list[dict]:
    """Apple iCloud+ tiles on apple.com/icloud.

    The page repeats the same five paid tiers in two layouts (a plain
    "50GB" block and an "iCloud+ 50GB" block). Both are normalized to the
    "iCloud+ <size>" name so dedupe collapses them. The free tier has no
    currency amount and is not recorded.
    """
    out = []
    tile_re = re.compile(
        r'tile-eyebrow[^>]*>\s*([^<]{1,30}?)\s*</h3>\s*'
        r'<p class="tile-headline[^>]*>\s*(\$[\d.]+)/month\s*</p>')
    for m in tile_re.finditer(html):
        name = m.group(1).strip()
        if not name.lower().startswith("icloud+"):
            name = "iCloud+ " + name
        got = parse_money(m.group(2), default_currency)
        if got:
            out.append(_dedicated_row(name, got[0], got[1], page_url, "month", "dedicated"))
    return out


def from_crashplan(html: str, page_url: str, default_currency: str) -> list[dict]:
    """CrashPlan small-business cards, one per page section.

    Every card carries the same family name, so the section comment
    (ENDPOINTS / SERVERS / MICROSOFT 365 / GOOGLE WORKSPACE) is turned
    into a distinguishing suffix. The Enterprise card quotes "Custom"
    and is skipped; prices billed annually are the displayed monthly
    equivalent and are recorded as such.
    """
    out = []
    labels = {
        "Endpoints": "Endpoint backup",
        "Servers": "Server backup",
        "Microsoft 365": "Microsoft 365",
        "Google Workspace": "Google Workspace",
    }
    card_re = re.compile(
        r'<h3 class="tt">([^<]{1,50})</h3>.{0,800}?'
        r'<span class="am">(\$[0-9.]+)</span><span class="pe">([^<]{1,30})</span>', re.S)
    marks = [(m.start(), m.group(1).strip().title())
             for m in re.finditer(r'<!-- ([A-Za-z0-9 ]{3,40}) -->', html)]
    marks.append((len(html), ""))
    for (s, section), (e, _) in zip(marks, marks[1:]):
        label = labels.get(section, section)
        for m in card_re.finditer(html[s:e]):
            unit_raw = m.group(3).strip().strip("/")
            unit = {"user/month": "month/user", "server/month": "month/server"}.get(unit_raw)
            if unit is None:
                continue
            got = parse_money(m.group(2), default_currency)
            if got:
                out.append(_dedicated_row(
                    f"{m.group(1).strip()} {label}", got[0], got[1], page_url, unit, "dedicated"))
    return out


def from_mediafire(html: str, page_url: str, default_currency: str) -> list[dict]:
    """MediaFire upgrade page (discount layout).

    Only current selling prices are recorded. The Pro card renders an
    "Original price" block next to the discounted live price and the
    originals are excluded by site policy. The Business plan is driven
    by JavaScript with $0.00 placeholders and is skipped entirely.
    """
    out = []

    def grab(element_id: str, plan: str) -> None:
        # Two renderings exist: "$X/month" (annual-equivalent) and
        # "or $X month-to-month" (plain monthly). Accept both.
        m = re.search(r'<p id="' + element_id + r'">\s*(?:\$[\d.]+/month|or\s+(\$[\d.]+)\s*month-to-month)\s*</p>', html)
        if m:
            amount = m.group(1) or re.search(r'\$([\d.]+)', m.group(0)).group(0)
            got = parse_money(amount, default_currency)
            if got and got[0] > 0:
                out.append(_dedicated_row(plan, got[0], got[1], page_url, "month", "dedicated"))

    grab("yearly-monthly-price-promo", "Pro (billed annually)")
    grab("monthly-price-promo", "Pro (month-to-month)")
    m = re.search(r'id="planUltraColumn".{0,4000}?<p[^>]*>\s*(\$[0-9.]+)/month\s*</p>', html, re.S)
    if m:
        got = parse_money(m.group(1), default_currency)
        if got and got[0] > 0:
            out.append(_dedicated_row("Ultra", got[0], got[1], page_url, "month", "dedicated"))
    return out


AWS_REGION = "us-east-1"
AWS_CLASSES = (
    # usage type (exact), volume type (exact), plan label
    ("TimedStorage-ByteHrs", "Standard", "S3 Standard"),
    ("TimedStorage-SIA-ByteHrs", "Standard - Infrequent Access", "S3 Standard-IA"),
    ("TimedStorage-GIR-ByteHrs", "Glacier Instant Retrieval", "S3 Glacier Instant Retrieval"),
    ("TimedStorage-GlacierByteHrs", "Amazon Glacier", "S3 Glacier Flexible Retrieval"),
)


def from_aws_s3(index_text: str, page_url: str, default_currency: str) -> list[dict]:
    """AWS S3 prices from the official Price List bulk JSON (no scraping).

    The provider source is the region index document; this parser resolves
    the current us-east-1 offer file, then takes the first-tier (lowest
    beginRange) on-demand GB-Month price for four general-purpose storage
    classes. Tiered classes spell the tier out in the plan name, mirroring
    the headline figure on aws.amazon.com. Any structural surprise yields
    an empty result, never a guessed price.
    """
    try:
        index = json.loads(index_text)
        current = index["regions"][AWS_REGION]["currentVersionUrl"]
    except (KeyError, TypeError, ValueError):
        return []
    if not isinstance(current, str) or not current.startswith("/offers/"):
        return []
    try:
        status, body = fetch("https://pricing.us-east-1.amazonaws.com" + current)
        if status != 200:
            return []
        offer = json.loads(body)
    except (RuntimeError, ValueError):
        return []

    wanted = {usage: (vtype, label) for usage, vtype, label in AWS_CLASSES}
    out = []
    for sku, product in offer.get("products", {}).items():
        attrs = product.get("attributes", {})
        if product.get("productFamily") != "Storage" or attrs.get("regionCode") != AWS_REGION:
            continue
        hit = wanted.get(attrs.get("usagetype", ""))
        if hit is None or attrs.get("volumeType") != hit[0]:
            continue
        label = hit[1]
        dims = []
        for term in offer.get("terms", {}).get("OnDemand", {}).get(sku, {}).values():
            for pd in term.get("priceDimensions", {}).values():
                if pd.get("unit") == "GB-Mo" and "USD" in pd.get("pricePerUnit", {}):
                    dims.append(pd)
        if not dims:
            continue
        first = min(dims, key=lambda pd: float(pd.get("beginRange") or 0))
        try:
            price = float(first["pricePerUnit"]["USD"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        end = str(first.get("endRange", "Inf"))
        if end.lower() != "inf":
            try:
                label += f" (first {int(float(end)) // 1024} TB)"
            except (ValueError, OverflowError):
                pass
        out.append(_dedicated_row(label, price, "USD", page_url, "month/gb", "dedicated"))
    return out


def from_digitalocean_spaces(html: str, page_url: str, default_currency: str) -> list[dict]:
    """DigitalOcean Spaces product page (pricing block inside marketing cards).

    Three rows, all USD, all static: the entry bundle named in prose
    ("starting at $5 per month for 250GiB with 1TiB of outbound
    transfer"), the storage-card overage unit price, and the cold storage
    class. Outbound transfer pricing is deliberately skipped — it is
    bandwidth, not storage, and a bandwidth rate must never be compared
    against a storage rate under the same unit.
    """
    flat = _flat_text(html)
    out = []
    m = re.search(
        r"starting at \$([\d.]+) per month for (\d+)GiB with (\d+)TiB of outbound transfer",
        flat)
    if m:
        got = parse_money(f"${m.group(1)}", default_currency)
        if got:
            out.append(_dedicated_row(
                f"{m.group(2)}GiB included ({m.group(3)}TiB transfer)",
                got[0], got[1], page_url, "month", "dedicated"))
    m = re.search(r"\$([\d.]+)/GiB additional storage", flat)
    if m:
        got = parse_money(f"${m.group(1)}", default_currency)
        if got:
            out.append(_dedicated_row(
                "Additional storage", got[0], got[1], page_url, "month/gb", "dedicated"))
    m = re.search(r"\$([\d.]+)/GiB cold storage", flat)
    if m:
        got = parse_money(f"${m.group(1)}", default_currency)
        if got:
            out.append(_dedicated_row(
                "Cold storage", got[0], got[1], page_url, "month/gb", "dedicated"))
    return out


def from_impossible_cloud(html: str, page_url: str, default_currency: str) -> list[dict]:
    """Impossible Cloud pricing page (Webflow marketing layout).

    The pay-as-you-go card reads "Pay-as-you-go €7.99 Per TB/month".
    Note the page server-renders EUR regardless of visitor region, so the
    recorded currency is the one actually on the page — never assumed to
    be USD. The reserved-capacity card is "Partners only" with no public
    number and is skipped.
    """
    flat = _flat_text(html)
    m = re.search(r"Pay-as-you-go\s+([€£$¥])\s?([\d.]+)\s+Per TB/month", flat, re.I)
    if not m:
        return []
    got = parse_money(f"{m.group(1)}{m.group(2)}", default_currency)
    if not got:
        return []
    return [_dedicated_row("Pay-as-you-go", got[0], got[1], page_url, "month/tb", "dedicated")]


def from_filebase(html: str, page_url: str, default_currency: str) -> list[dict]:
    """Filebase pricing page (Next.js/static cards).

    The Pro card price and the comparison table both render the same
    "$7.50 /month" figure, so the two occurrences collapse into one
    dedupe key. The Free tier ($0/month) carries no selling price and is
    skipped — a zero price is not a comparable offer. The additional
    storage unit price is anchored on the "Additional Storage $X/GB"
    label so it cannot be confused with the separately-labelled
    "Additional IPFS bandwidth" rate that shares the same number.
    """
    flat = _flat_text(html)
    out = []
    for m in re.finditer(r"\$([\d.]+)\s*/\s*month\b", flat):
        got = parse_money(f"${m.group(1)}", default_currency)
        if got and got[0] > 0:
            out.append(_dedicated_row("Pro", got[0], got[1], page_url, "month", "dedicated"))
    m = re.search(r"Additional Storage\s+\$([\d.]+)/GB", flat)
    if m:
        got = parse_money(f"${m.group(1)}", default_currency)
        if got:
            out.append(_dedicated_row(
                "Additional storage", got[0], got[1], page_url, "month/gb", "dedicated"))
    return out


def from_upcloud(html: str, page_url: str, default_currency: str) -> list[dict]:
    """UpCloud Managed Object Storage product page.

    Prices in the server-rendered capacity table are monthly and may be
    localized: the page exposes "$5/mo" or "€5/mo" for 250 GB and the
    corresponding same-currency +5/month step for each additional 250 GB.
    The prose also mentions a separate
    "€0.02/GB" figure without a billing period; because the page currently
    mixes currencies and does not state that period beside the unit price,
    that figure is deliberately omitted instead of assigning month/GB.
    Hourly equivalents are not duplicated.
    """
    flat = _flat_text(html)
    out = []
    currency = r"([$€£¥])\s*([\d.]+)"
    m = re.search(rf"(\d+)\s*GB Included\s+{currency}/mo", flat)
    if m:
        got = parse_money(f"{m.group(2)}{m.group(3)}", default_currency)
        if got:
            out.append(_dedicated_row(
                f"{m.group(1)} GB", got[0], got[1], page_url, "month", "dedicated"))
    m = re.search(rf"\+\s*(\d+)\s*GB for each upscale\s+\+\s*{currency}/mo", flat)
    if m:
        got = parse_money(f"{m.group(2)}{m.group(3)}", default_currency)
        if got:
            out.append(_dedicated_row(
                f"Each additional {m.group(1)} GB", got[0], got[1],
                page_url, "month", "dedicated"))
    return out


def from_livedrive(html: str, page_url: str, default_currency: str) -> list[dict]:
    """LiveDrive homepage plan cards (public, static WordPress markup).

    The dedicated pricing page currently returns 404; the homepage contains
    the current plan selector and its static price nodes. Only the six
    explicitly named price elements (three plans × monthly/annual) are read.
    The GBP symbol is taken from the rendered page, not inferred from locale.
    """
    out = []
    plans = {
        "backup": ("Backup (unlimited storage)", "month"),
        "briefcase": ("Briefcase (2 TB)", "month"),
        "pro": ("Pro Suite (5 TB)", "month"),
    }
    pattern = re.compile(
        r'<p[^>]*class="[^"]*\bzd-price-(backup|briefcase|pro)-(monthly|annual)\b'
        r'[^"]*"[^>]*>(.*?)</p>', re.S | re.I)
    for m in pattern.finditer(html):
        key, period, fragment = m.groups()
        plan, _ = plans[key.lower()]
        token = re.sub(r"\s+", " ", _clean_text(fragment)).strip()
        got = parse_money(token, default_currency)
        if not got or got[0] <= 0:
            continue
        unit = "month" if period.lower() == "monthly" else "year"
        out.append(_dedicated_row(plan, got[0], got[1], page_url, unit, "dedicated"))
    return out


# SpiderOak is excluded: spideroak.com/robots.txt is a blanket
# "User-agent: * / Disallow: /" — the site explicitly disallows crawlers, and
# that robots rule outranks the fact that its prices are static and readable.

def from_sevalla(html: str, page_url: str, default_currency: str) -> list[dict]:
    """Sevalla object storage (Kinsta; single flat unit price).

    The page quotes exactly one number — "Pay $ 0.02 /GB" with no egress
    fees — so a single month/gb row is emitted. Any other figure on the
    page belongs to application hosting products and must not leak into
    the storage record.
    """
    flat = _flat_text(html)
    m = re.search(r"Pay\s+\$\s*([\d.]+)\s*/GB", flat)
    if not m:
        return []
    got = parse_money(f"${m.group(1)}", default_currency)
    if not got:
        return []
    return [_dedicated_row("Object storage", got[0], got[1],
                           page_url, "month/gb", "dedicated")]


def from_opendrive(html: str, page_url: str, default_currency: str) -> list[dict]:
    """OpenDrive pricing page (server-rendered plan cards).

    Personal Unlimited is the only fixed-price tier ("$12.95" monthly,
    "$129 /year" annual — the dollar sign lives in a separate tag, so the
    flattened text reads "Personal 12.95 Users"). The Free tier is $0 and
    the Custom plan is a per-seat ladder ($1-998/month by user count);
    neither is a comparable offer and both are skipped.
    """
    flat = _flat_text(html)
    out = []
    m = re.search(r"Personal\s+\$?\s*([\d.]+)\s+Users", flat)
    if m:
        got = parse_money(f"${m.group(1)}", default_currency)
        if got:
            out.append(_dedicated_row(
                "Personal Unlimited", got[0], got[1], page_url, "month", "dedicated"))
    m = re.search(r"Personal\s+\$?\s*[\d.]+\s+Users.{0,300}?([\d,]+)\s*/\s*year", flat)
    if m:
        got = parse_money(f"${m.group(1).replace(',', '')}", default_currency)
        if got:
            out.append(_dedicated_row(
                "Personal Unlimited", got[0], got[1], page_url, "year", "dedicated"))
    return out


DEDICATED_PARSERS = {
    "https://bunny.net/storage": from_bunny,
    "https://developers.cloudflare.com/r2/pricing": from_r2,
    "https://www.backblaze.com/cloud-storage/pricing": from_backblaze_b2,
    "https://www.backblaze.com/backup-pricing.html": from_backblaze_backup,
    "https://wasabi.com/pricing": from_wasabi,
    "https://www.idrive.com/e2": from_idrive_e2,
    "https://www.dropbox.com/plans": from_dropbox,
    "https://www.jottacloud.com/en/pricing": from_jottacloud,
    "https://www.zoolz.com/pricing": from_zoolz,
    "https://www.apple.com/icloud": from_icloud,
    "https://www.crashplan.com/pricing": from_crashplan,
    "https://www.mediafire.com/upgrade": from_mediafire,
    "https://www.digitalocean.com/products/spaces": from_digitalocean_spaces,
    "https://www.impossiblecloud.com/pricing": from_impossible_cloud,
    "https://filebase.com/pricing": from_filebase,
    "https://upcloud.com/products/object-storage": from_upcloud,
    "https://sevalla.com/object-storage": from_sevalla,
    "https://www.opendrive.com/pricing": from_opendrive,
    "https://www.livedrive.com": from_livedrive,
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/region_index.json": from_aws_s3,
}


# ---------------------------------------------------------------- http

def _decode(raw: bytes, encoding: str) -> str:
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "ignore")


def fetch(url: str, attempts: int = 2) -> tuple[int, str]:
    """Strict TLS first; fall back to unverified only when explicitly allowed.

    Transient blocks and resets are common on CDNs protecting pricing pages
    (Backblaze in particular fails intermittently), so each fetch is retried
    once after a short pause before the caller sees a failure.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, identity",
        },
    )
    contexts = [None]
    if os.environ.get("PROMO_RADAR_INSECURE_SSL"):
        contexts.append(ssl._create_unverified_context())
    last = None
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(8)
        for ctx in contexts:
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
                    return resp.status, _decode(resp.read(), resp.headers.get("Content-Encoding", ""))
            except Exception as exc:  # noqa: BLE001 - network is best-effort by design
                last = exc
    raise RuntimeError(f"{type(last).__name__}: {last}")


def robots_allows(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        status, body = fetch(robots_url)
    except Exception:  # noqa: BLE001
        return True  # no robots.txt to read = nothing disallowed
    if status != 200:
        return True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines())
    return parser.can_fetch(UA, url)


# ---------------------------------------------------------------- extraction

def flatten(html: str) -> str:
    """HTML -> text. Tags and script bodies are replaced by spaces of the SAME
    length, so every offset in the result is still a valid offset in `html`."""
    blanked = SCRIPT_RE.sub(lambda m: " " * len(m.group(0)), html)
    return TAG_RE.sub(lambda m: " " * len(m.group(0)), blanked)


def _clean_text(fragment: str) -> str:
    text = TAG_RE.sub(" ", fragment)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_money(token: str, default_currency: str) -> tuple[float, str] | None:
    raw = token.strip()
    currency = None
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in raw:
            currency = code
            raw = raw.replace(sym, "")
            break
    for code in ("USD", "EUR", "GBP", "JPY", "NOK", "SEK", "DKK", "CHF", "CAD", "AUD"):
        if code in raw:
            currency = code
            raw = raw.replace(code, "")
            break
    raw = raw.strip().replace(" ", "")
    # 1.234,56 / 1,234.56 / 49 / 49.90
    if "," in raw and "." in raw:
        raw = raw.replace(",", "") if raw.rfind(".") > raw.rfind(",") else raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        tail = raw.split(",")[-1]
        raw = raw.replace(",", ".") if len(tail) in (1, 2) else raw.replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    if not (0 < value < 20000):
        return None
    # Four decimals keep per-GB unit prices ($0.015/GB-month) exact; normal
    # prices are unaffected.
    return round(value, 4), currency or default_currency


def normalize_pricing_unit(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip().lower())
    value = re.sub(r"^per\s+", "", value)
    value = value.replace("per user,", "per user/").replace("per license,", "per license/")
    value = re.sub(r"^monthly\s+", "month/", value)
    value = re.sub(r"^license pricing\s*", "license/", value)
    value = value.replace("/mo", "/month").replace("/yr", "/year")
    value = value.replace("mo/", "month/").replace("yr/", "year/")
    value = {"mo": "month", "months": "month", "monthly": "month",
             "yr": "year", "years": "year", "annual": "year", "annually": "year"}.get(value, value)
    return value or "unknown"


def pricing_unit(flat: str, start: int, end: int) -> str:
    """Return a conservative normalized billing basis near a price token.

    The basis is intentionally taken from visible text immediately around the
    price. If the source does not state it clearly, return ``unknown`` rather
    than inventing a period or scope.
    """
    period = rf"(?:{PERIODS})"
    scope = rf"(?:{SCOPES})"
    patterns = (
        (rf"/(?:\s*)({period})(?:\s*)/(?:\s*)({scope})", lambda m: f"{m.group(1)}/{m.group(2)}"),
        (rf"/(?:\s*)({scope})(?:\s*)/(?:\s*)({period})", lambda m: f"{m.group(2)}/{m.group(1)}"),
        (rf"\bper\s+({period})\s*(?:per|/)\s*({scope})", lambda m: f"{m.group(1)}/{m.group(2)}"),
        (rf"\bper\s+({scope})\s*(?:per|/)\s*({period})", lambda m: f"{m.group(2)}/{m.group(1)}"),
        (rf"\bmonthly\s+({scope})\b", lambda m: f"month/{m.group(1)}"),
        (rf"\bmonthly\s+({scope})\s*\$", lambda m: f"month/{m.group(1)}"),
        (rf"\bmonthly\s+storage\b", lambda m: "month/tb"),
        (rf"\blicense\s+pricing\b", lambda m: "license/tb"),
        (rf"/(?:\s*)({period})\b", lambda m: m.group(1)),
        (rf"\bper\s+({period})\b", lambda m: m.group(1)),
    )
    # Prefer text after the token. Looking too far backwards can accidentally
    # borrow the billing label belonging to the previous plan card.
    contexts = (
        flat[end:end + 120],
        flat[max(0, start - 40):start],
    )
    for raw_context in contexts:
        context = re.sub(r"\s+", " ", raw_context).strip().lower()
        for pattern, normalize in patterns:
            match = re.search(pattern, context)
            if match:
                return normalize_pricing_unit(normalize(match))
    return "unknown"


def _first_plan_text(fragment: str) -> str | None:
    """First text node inside a title-like element that looks like a plan name.

    Plan names are usually buried a level or two down
    (`<div class=plan__title><img><span>Teams 1TB</span>...`), so every text
    node in the element is tried, not just the one right after the open tag.
    """
    for chunk in TAG_RE.split(fragment):
        name = usable_plan_name(_clean_text(chunk))
        if name:
            return name
    return None


def plan_candidates(html: str) -> list[tuple[int, str]]:
    """(offset_after_anchor, candidate_plan_name) pairs, in document order.

    Offsets are absolute positions in `html`, which are valid in `flatten(html)`
    because that function blanks tags in place instead of removing them.
    """
    found: list[tuple[int, str]] = []

    for m in HEADING_RE.finditer(html):
        name = usable_plan_name(_clean_text(m.group(2)))
        if name:
            found.append((m.end(), name))

    for m in NAMED_RE.finditer(html):
        name = _first_plan_text(html[m.end(): m.end() + 600])
        if name:
            found.append((m.end(), name))

    found.sort(key=lambda pair: pair[0])
    return found


def usable_plan_name(name: str) -> str | None:
    n = name.strip(" \u00b7|-–—").strip()
    if not (2 <= len(n) <= 42):
        return None
    if not re.search(r"[A-Za-z]", n):
        return None
    if re.search(r"[{}<>]|https?://", n):
        return None
    low = n.lower()
    if low in PLAN_STOPWORDS or low.startswith(("cookie", "we use", "all rights", "save", "get ", "up to")):
        return None
    if any(phrase in low for phrase in NAME_REJECT):
        return None
    if any(phrase in low for phrase in TECH_REJECT):
        return None
    if "%" in n or len(low.split()) > 6:
        return None
    if not n[0].isalnum():  # fragments like "/month" are not plan names
        return None
    return n


def is_struck_through(html: str, pos: int) -> bool:
    """True when the money at `pos` sits inside a tag carrying line-through.

    Struck-through prices are original prices. CSS renders them, so after tags
    are blanked they look identical to the current price — the raw tag is the
    only place the difference survives.
    """
    gt = html.rfind(">", 0, pos)          # end of the tag enclosing this text
    if gt < 0 or pos - gt > 2:            # money is not the first thing in it
        return False
    lt = html.rfind("<", 0, gt)
    if lt < 0:
        return False
    return "line-through" in html[lt:gt + 1].lower()


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _money_value(value, default_currency: str):
    if isinstance(value, (int, float)):
        return float(value), default_currency
    if isinstance(value, dict):
        price = value.get("price") or value.get("lowPrice") or value.get("value")
        return parse_money(str(price), value.get("priceCurrency", default_currency))
    if isinstance(value, str):
        return parse_money(value, default_currency)
    return None


def from_jsonld(html: str, page_url: str, default_currency: str) -> list[dict]:
    out = []
    for blob in LD_JSON_RE.findall(html):
        try:
            data = json.loads(blob.strip())
        except Exception:  # noqa: BLE001 - malformed ld+json is common
            continue
        for node in _walk(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            types = [str(t) for t in types if t]
            if not any(t in ("Offer", "Product", "Service") for t in types):
                continue
            offers = node.get("offers")
            if offers is None:
                continue
            offers = offers if isinstance(offers, list) else [offers]
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                otypes = offer.get("@type")
                otypes = otypes if isinstance(otypes, list) else [otypes]
                if "AggregateOffer" in [str(t) for t in otypes if t]:
                    continue  # a price range is not one real plan
                got = _money_value(offer.get("price") or offer.get("priceSpecification"), default_currency)
                if not got:
                    continue
                price, currency = got
                name = usable_plan_name(str(offer.get("name") or node.get("name") or ""))
                if not name:
                    continue
                url = offer.get("url") or node.get("url") or page_url
                out.append(
                    {
                        "plan": name,
                        "price": price,
                        "currency": str(offer.get("priceCurrency") or currency or default_currency).upper(),
                        "offer_url": urllib.parse.urljoin(page_url, str(url)),
                        "valid_until": offer.get("priceValidUntil"),
                        "pricing_unit": normalize_pricing_unit(offer.get("unitText") or offer.get("billingDuration") or "unknown"),
                        "method": "jsonld",
                    }
                )
    return out


def from_idrive_addons(html: str, page_url: str, default_currency: str) -> list[dict]:
    """Parse IDrive's stable add-on cards by their official link targets.

    These cards sit outside IDrive's tabbed plan tables, so a generic nearest
    heading/price heuristic can accidentally associate a tab price with them.
    Only emit a row when the named official card contains a price and an
    explicit billing basis.
    """
    out = []
    flat = flatten(html)
    for plan, slug in IDRIVE_ADDONS:
        anchor = re.search(
            rf'<a[^>]+href=["\'][^"\']*{re.escape(slug)}[^"\']*["\'][^>]*>'
            rf'\s*{re.escape(plan)}\s*</a>', html, re.I | re.S)
        if not anchor:
            continue
        body_start = anchor.end()
        body_end = min(len(html), body_start + 900)
        body = html[body_start:body_end]
        body_flat = flatten(body)
        price_match = MONEY_RE.search(body_flat)
        if not price_match:
            continue
        got = parse_money(price_match.group(0), default_currency)
        if not got:
            continue
        unit = pricing_unit(body_flat, price_match.start(), price_match.end())
        if unit == "unknown":
            continue
        price, currency = got
        out.append({
            "plan": plan,
            "price": price,
            "currency": currency,
            "offer_url": urllib.parse.urljoin(page_url, f"/{slug}"),
            "valid_until": None,
            "pricing_unit": unit,
            "method": "idrive_addon",
        })
    return out


def from_heuristics(html: str, page_url: str, default_currency: str) -> list[dict]:
    """Pair each plan name with the first real (non struck-through) price that
    appears after it and before the next plan name, so a plan can never inherit
    the price of the card below it."""
    flat = flatten(html)
    money = [m for m in MONEY_RE.finditer(flat) if not is_struck_through(html, m.start())]
    if not money:
        return []
    candidates = plan_candidates(html)
    out = []
    for index, (offset, name) in enumerate(candidates):
        limit = candidates[index + 1][0] if index + 1 < len(candidates) else len(flat)
        for m in money:
            if m.start() <= offset or m.start() >= limit:
                continue
            gap = flat[offset:m.start()]
            # measure visible characters only: blanked markup is whitespace
            if len(re.sub(r"\s+", "", gap)) > 250:
                break
            low = gap.lower()
            if any(k in low for k in EXCLUDE_CONTEXT):
                continue
            got = parse_money(m.group(0), default_currency)
            if not got:
                continue
            price, currency = got
            window = flat[max(0, m.start() - 120): m.end() + 120].lower()
            if any(k in window for k in ("renew", "regular price", "original", "was ")):
                continue
            out.append(
                {
                    "plan": name,
                    "price": price,
                    "currency": currency,
                    "offer_url": page_url,
                    "valid_until": None,
                    "pricing_unit": pricing_unit(flat, m.start(), m.end()),
                    "method": "heuristic",
                }
            )
            break
    return out


# ---------------------------------------------------------------- pipeline

def dedupe(rows: list[dict]) -> list[dict]:
    seen: dict[tuple, int] = {}
    kept: list[dict] = []
    for row in rows:
        base = (row["provider_slug"], siteilang._slug(row["plan"]), row["currency"].upper())
        unit = normalize_pricing_unit(row.get("pricing_unit"))
        key = (*base, unit)
        unknown_key = (*base, "unknown")
        # The same plan can appear in multiple tabs on a source page. Prefer a
        # clearly stated basis over an ambiguous duplicate, while retaining
        # genuinely distinct month/year or per-seat/per-server offers.
        if unit == "unknown" and any(k[:3] == base and k[3] != "unknown" for k in seen):
            continue
        if unit != "unknown" and unknown_key in seen:
            index = seen.pop(unknown_key)
            kept[index] = row
            seen[key] = index
            continue
        if key in seen:
            continue
        seen[key] = len(kept)
        kept.append(row)
    return kept


def scrape_provider(provider: dict, default_currency: str, previous: dict | None = None) -> dict:
    result = dict(provider)
    result["offers"] = []
    result["status"] = "unreachable"
    result["note"] = ""

    if not robots_allows(provider["source"]):
        result["status"] = "robots_disallow"
        result["note"] = "robots.txt disallows this path — skipped, not fetched."
        return result

    try:
        status, html = fetch(provider["source"])
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"fetch failed: {type(exc).__name__}"
        return result
    if status != 200:
        result["note"] = f"HTTP {status}"
        return result

    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows = list(from_jsonld(html, provider["source"], default_currency))
    if provider["slug"] == "idrive":
        rows += from_idrive_addons(html, provider["source"], default_currency)
    dedicated_fn = DEDICATED_PARSERS.get(provider["source"].rstrip("/"))
    if dedicated_fn is not None:
        dedicated_rows = dedicated_fn(html, provider["source"], default_currency)
        if dedicated_rows:
            # A dedicated parser only matches named plans with explicit
            # billing bases, so its rows replace whatever noise the generic
            # heuristic would pair on this page.
            rows = dedicated_rows
        else:
            rows += from_heuristics(html, provider["source"], default_currency)
    else:
        rows += from_heuristics(html, provider["source"], default_currency)

    offers = []
    used_ids = set()
    for row in rows:
        if row["price"] is None:
            continue
        pricing_unit_value = normalize_pricing_unit(row.get("pricing_unit"))
        base_id = f"{provider['slug']}-{siteilang._slug(row['plan'])}-{row['currency'].lower()}"
        offer_id = base_id
        if offer_id in used_ids:
            offer_id = f"{base_id}-{siteilang._slug(pricing_unit_value)}"
        used_ids.add(offer_id)
        offers.append(
            {
                "id": offer_id,
                "provider": provider["name"],
                "provider_slug": provider["slug"],
                "plan": row["plan"],
                "title": f"{provider['name']} {row['plan']}",
                "price": row["price"],
                "currency": row["currency"].upper(),
                "offer_url": row["offer_url"],
                "valid_until": row["valid_until"],
                "source_url": provider["source"],
                "fetched_at": fetched_at,
                "pricing_unit": SPECIAL_PLAN_UNITS.get(
                              (provider["slug"], siteilang._slug(row["plan"])),
                              (row.get("pricing_unit", "unknown")
                               if row.get("pricing_unit", "unknown") != "unknown"
                               else SPECIAL_PRICING_UNITS.get(provider["slug"], "unknown"))),
                "extraction": row["method"],
            }
        )

    offers = dedupe(offers)

    # Freshness without churn: an offer whose data is identical to the
    # previous live record keeps that record's fetched_at. Unchanged pages
    # then do not claim a new verification date on every run, which keeps
    # sitemap lastmod meaningful (Google only trusts lastmod that is
    # consistently and verifiably accurate).
    prev_by_id = {o["id"]: o for o in (previous or {}).get("offers", []) if not o.get("stale")}
    for offer in offers:
        prev = prev_by_id.get(offer["id"])
        if prev is not None and (
            {k: v for k, v in prev.items() if k not in ("fetched_at", "stale")}
            == {k: v for k, v in offer.items() if k not in ("fetched_at", "stale")}
        ):
            offer["fetched_at"] = prev["fetched_at"]

    result["offers"] = offers
    result["status"] = "ok" if offers else "no_price"
    if not offers:
        result["note"] = (
            "No price could be verified on the official page "
            "(client-rendered or no public price block). Left empty on purpose."
        )
    return result


def retain_previous_on_failure(result: dict, previous: dict | None) -> dict:
    """Keep last verified records when a source is temporarily unreachable.

    An unreachable source is not evidence that its previous prices vanished.
    Preserve the records so generated pages can be honest. A successful
    no-price result is intentionally not preserved.

    robots_disallow is handled the same way: official robots.txt can flip
    briefly during staging/rollouts, and the records were obtained while the
    path was allowed. The no-fetch rule still applies — we only stop fetching,
    we never bypass robots.

    Consecutive-failure hysteresis: a single failed run keeps the previous
    records live (their fetched_at still states exactly when they were
    verified, so nothing on the site becomes untrue), because one flaky
    fetch must not flip sitemap membership or noindex state back and forth.
    Two or more consecutive failures mark the records stale. A legacy stale
    record without a streak counter counts as already failing, so stale data
    is never silently promoted back to live.
    """
    if result.get("status") not in ("unreachable", "robots_disallow") or not previous or not previous.get("offers"):
        return result
    result = dict(result)
    detail = result.get("note") or "latest fetch blocked"
    prev_offers = previous["offers"]
    streak = int(previous.get("failure_streak") or 0)
    if not streak and any(o.get("stale") for o in prev_offers):
        streak = 2
    streak += 1
    result["failure_streak"] = streak
    if streak >= 2:
        result["offers"] = [{**offer, "stale": True} for offer in prev_offers]
        result["status"] = "stale"
        result["note"] = f"{detail}; retained {len(result['offers'])} previously verified record(s) until the source is reachable again."
    else:
        result["offers"] = [dict(offer) for offer in prev_offers]
        result["status"] = "ok" if any(o.get("price") is not None for o in prev_offers) else "no_price"
        result["note"] = f"{detail}; first consecutive failure — records stay live until the next run confirms."
    return result


def main(argv: list[str]) -> int:
    cfg = siteilang.load(argv[1] if len(argv) > 1 else siteilang.DEFAULT_PATH)
    conf = siteilang.settings(cfg)
    default_currency = conf.get("currency", "USD")
    providers = siteilang.providers(cfg)

    previous_by_slug = {}
    previous_meta: dict = {}
    previous_providers: list[dict] = []
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as fh:
                old_data = json.load(fh)
            previous_by_slug = {p["slug"]: p for p in old_data.get("providers", []) if p.get("slug")}
            previous_meta = old_data.get("meta", {})
            previous_providers = old_data.get("providers", [])
        except (OSError, ValueError, TypeError):
            previous_by_slug = {}

    results = []
    for provider in providers:
        res = scrape_provider(provider, default_currency, previous_by_slug.get(provider["slug"]))
        res = retain_previous_on_failure(res, previous_by_slug.get(provider["slug"]))
        results.append(res)

    all_offers = sorted(
        [o for r in results for o in r["offers"]],
        key=lambda o: (o["provider_slug"], o["price"]),
    )

    # Bump the freshness stamp only when the records actually changed (or the
    # calendar month rolled over, since month labels appear in page copy).
    # Unchanged data then rewrites a byte-identical payload: repeated runs and
    # CI commits become true no-ops and sitemap lastmod stays trustworthy.
    def _comparable(records: list[dict]) -> str:
        return json.dumps(records, sort_keys=True, ensure_ascii=False)

    data_month = datetime.now(timezone.utc).strftime("%Y-%m")
    data_changed = (
        _comparable(results) != _comparable(previous_providers)
        or data_month != previous_meta.get("data_month")
    )
    now_stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    generated_at = now_stamp if data_changed else (previous_meta.get("generated_at") or now_stamp)

    payload = {
        "meta": {
            "generated_at": generated_at,
            "data_month": data_month,
            "locale": conf.get("locale", "en-US"),
            "currency": default_currency,
            "brand": cfg["site"].get("brand"),
            "niche": cfg["site"].get("niche"),
            "providers_total": len(results),
            "providers_with_price": sum(1 for r in results if r["status"] == "ok"),
            "providers_with_stale_price": sum(1 for r in results if r["status"] == "stale"),
            "offers_total": len(all_offers),
            "rule": "prices come from official public pages only; unreadable prices are left out, never estimated",
        },
        "providers": results,
        "offers": all_offers,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)

    # Machine-readable health report for CI alerting: a provider whose
    # verified-price count collapses to zero, or whose fetch failed outright,
    # usually means an official page changed and the parser no longer matches.
    def priced(items: list[dict]) -> int:
        return sum(1 for o in items if o.get("price") is not None and not o.get("stale"))

    report = {
        "generated_at": payload["meta"]["generated_at"],
        "providers": [],
    }
    for r in results:
        prev = previous_by_slug.get(r["slug"], {})
        before = priced(prev.get("offers", []))
        now = priced(r["offers"])
        entry = {
            "slug": r["slug"],
            "name": r["name"],
            "status": r["status"],
            "note": r.get("note", ""),
            "failure_streak": r.get("failure_streak", 0),
            "priced_offers_before": before,
            "priced_offers_now": now,
            "alert": (
                r["status"] in ("unreachable", "robots_disallow", "stale")
                or r.get("failure_streak", 0) >= 1
                or (before > 0 and now == 0)
            ),
        }
        report["providers"].append(entry)
    with open(os.path.join(DATA_DIR, "scrape_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print(f"providers: {payload['meta']['providers_total']}  "
          f"with_price: {payload['meta']['providers_with_price']}  "
          f"offers: {payload['meta']['offers_total']}")
    for r in results:
        print(f"  [{r['status']:<15}] {r['name']:<10} {len(r['offers'])} offer(s) {r['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
