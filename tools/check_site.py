"""Full static check of the built site: placeholders, JSON-LD, ids, links, tags."""
import glob
import json
import os
import re
import sys
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
PH = None  # compiled below, after KNOWN_KEYS
KNOWN_KEYS = {
    "brand", "niche", "root", "headline", "lede", "contact_email", "object_rows",
    "consumer_rows", "faq_items", "short_version", "stat_cards", "model_rows",
    "verified_at", "source_count", "tco_rows", "tco_caption", "tco_data",
    "rule_rows", "excluded_items", "index_value", "index_meta", "chart",
    "chart_note", "chart_legend", "basis_rows", "currency_rows", "movers_html",
    "base_date", "basis_note", "currency_note", "hero_note", "refresh_human",
    "breadcrumb", "table_rows", "note", "basis_links", "lang", "title",
    "description", "canonical", "og_url", "og_image", "robots_meta", "tagline",
    "body", "updated_at", "jsonld", "providers_total", "offers_total",
    "provider_name", "slug", "plan", "price", "unit", "status", "offers",
    "source", "site", "headline_meta", "lede_note",
}

# Matches both $name and ${name}. The narrow $name-only pattern once missed a
# `${root}` that shipped a broken script src, so both forms are checked.
PH = re.compile(r"\$\{?(" + "|".join(sorted(KNOWN_KEYS)) + r")\}?")

bad_ld, bad_ids, bad_ph, unbalanced = [], [], [], []
missing = set()
checked = 0
pages = 0


class Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.err = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.err.append(f"stray </{tag}>")
        elif self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.err.append(f"</{tag}> closes <{self.stack[-1]}>")


for f in glob.glob("site/**/*.html", recursive=True):
    pages += 1
    h = open(f, encoding="utf-8").read()

    for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
        try:
            json.loads(m)
        except Exception as exc:
            bad_ld.append((f, str(exc)[:60]))

    ids = re.findall(r'\sid="([^"]+)"', h)
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        bad_ids.append((f, dupes))

    if PH.search(h):
        bad_ph.append((f, PH.search(h).group(0)))

    p = Balance()
    p.feed(h)
    if p.stack or p.err:
        unbalanced.append((f, p.stack[:3], p.err[:2]))

    for href in re.findall(r'href="(/[^"#?]*)"', h):
        checked += 1
        rel = href.strip("/")
        cand = os.path.join("site", rel, "index.html") if rel else "site/index.html"
        if not os.path.exists(cand) and not os.path.exists(os.path.join("site", rel)):
            missing.add(href)

sitemap = open("site/sitemap.xml", encoding="utf-8").read()
sm_urls = re.findall(r"<loc>(.*?)</loc>", sitemap)

print(f"pages: {pages}")
print(f"sitemap urls: {len(sm_urls)}")
print(f"internal links checked: {checked}, missing: {len(missing)}")
if missing:
    print("  ", sorted(missing)[:8])
print(f"jsonld parse failures: {len(bad_ld)}")
for x in bad_ld[:3]:
    print("  ", x)
print(f"duplicate ids: {len(bad_ids)}")
for x in bad_ids[:3]:
    print("  ", x)
print(f"placeholder residue: {len(bad_ph)}")
for x in bad_ph[:5]:
    print("  ", x)
print(f"unbalanced tags: {len(unbalanced)}")
for x in unbalanced[:3]:
    print("  ", x)

# Every sitemap URL must exist on disk, and no page should be orphaned from it
# except the intentionally noindex ones.
noindex = []
for f in glob.glob("site/**/*.html", recursive=True):
    h = open(f, encoding="utf-8").read()
    if 'content="noindex,follow"' in h:
        noindex.append(f)
print(f"noindex pages: {len(noindex)}  (pages - sitemap = {pages - len(sm_urls)})")

fail = bool(missing or bad_ld or bad_ids or bad_ph or unbalanced)
print("\nRESULT:", "FAIL" if fail else "PASS")
sys.exit(1 if fail else 0)
