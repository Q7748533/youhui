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


# ---------------------------------------------------------------- http

def _decode(raw: bytes, encoding: str) -> str:
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "ignore")


def fetch(url: str) -> tuple[int, str]:
    """Strict TLS first; fall back to unverified only when explicitly allowed."""
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
    for code in ("USD", "EUR", "GBP", "JPY"):
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
    return round(value, 2), currency or default_currency


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


def scrape_provider(provider: dict, default_currency: str) -> dict:
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
    rows = from_jsonld(html, provider["source"], default_currency)
    if provider["slug"] == "idrive":
        rows += from_idrive_addons(html, provider["source"], default_currency)
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
    Preserve the records but mark them stale so generated pages can be honest.
    A successful no-price result is intentionally not preserved.
    """
    if result.get("status") != "unreachable" or not previous or not previous.get("offers"):
        return result
    result = dict(result)
    result["offers"] = [{**offer, "stale": True} for offer in previous["offers"]]
    result["status"] = "stale"
    detail = result.get("note") or "latest fetch failed"
    result["note"] = f"{detail}; retained {len(result['offers'])} previously verified record(s) until the source is reachable again."
    return result


def main(argv: list[str]) -> int:
    cfg = siteilang.load(argv[1] if len(argv) > 1 else siteilang.DEFAULT_PATH)
    conf = siteilang.settings(cfg)
    default_currency = conf.get("currency", "USD")
    providers = siteilang.providers(cfg)

    previous_by_slug = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as fh:
                old_data = json.load(fh)
            previous_by_slug = {p["slug"]: p for p in old_data.get("providers", []) if p.get("slug")}
        except (OSError, ValueError, TypeError):
            previous_by_slug = {}

    results = []
    for provider in providers:
        res = scrape_provider(provider, default_currency)
        res = retain_previous_on_failure(res, previous_by_slug.get(provider["slug"]))
        results.append(res)

    all_offers = sorted(
        [o for r in results for o in r["offers"]],
        key=lambda o: (o["provider_slug"], o["price"]),
    )
    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
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

    print(f"providers: {payload['meta']['providers_total']}  "
          f"with_price: {payload['meta']['providers_with_price']}  "
          f"offers: {payload['meta']['offers_total']}")
    for r in results:
        print(f"  [{r['status']:<15}] {r['name']:<10} {len(r['offers'])} offer(s) {r['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
