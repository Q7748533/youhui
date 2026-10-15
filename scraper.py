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
                        "method": "jsonld",
                    }
                )
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
                    "method": "heuristic",
                }
            )
            break
    return out


# ---------------------------------------------------------------- pipeline

def dedupe(rows: list[dict]) -> list[dict]:
    seen, kept = {}, []
    for row in rows:
        key = (row["provider_slug"], siteilang._slug(row["plan"]), row["currency"].upper())
        if key in seen:
            continue
        seen[key] = True
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
    rows += from_heuristics(html, provider["source"], default_currency)

    offers = []
    for row in rows:
        if row["price"] is None:
            continue
        offers.append(
            {
                "id": f"{provider['slug']}-{siteilang._slug(row['plan'])}-{row['currency'].lower()}",
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


def main(argv: list[str]) -> int:
    cfg = siteilang.load(argv[1] if len(argv) > 1 else siteilang.DEFAULT_PATH)
    conf = siteilang.settings(cfg)
    default_currency = conf.get("currency", "USD")
    providers = siteilang.providers(cfg)

    results = []
    for provider in providers:
        res = scrape_provider(provider, default_currency)
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
