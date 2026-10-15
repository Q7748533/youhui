# Cloud Storage Deals

A zero-server, zero-API-key coupon vertical site for consumer cloud storage.
Prices are read straight from each provider's own public pricing page, every
6 hours, by a GitHub Actions cron. The site is static; Cloudflare Pages serves it.

**The rule this project follows: a price that cannot be verified on the official
page is left out. It is never estimated, never carried over from the last run.**

Repo: https://github.com/Q7748533/youhui
Live: https://53213a96be244b908bd518f9961dc360.sg.agentos-app.run

## What it does

- `scraper.py` reads `.ilang/site.ilang`, checks `robots.txt` for every source,
  fetches the official pricing pages, and extracts plan name + current price +
  currency. Struck-through originals, renewal prices and "after the first term"
  numbers are discarded on purpose.
- `build.py` renders `site/` from `data/offers.json` and `templates/`, including
  JSON-LD (`Offer`, `Product` + `AggregateOffer`, `ItemList`, `BreadcrumbList`),
  canonical, hreflang, Open Graph, `sitemap.xml` and `robots.txt`.
- `.github/workflows/update.yml` runs both on a cron and commits the result. Every
  commit is both a freshness signal for search engines and one more day of age
  on the repo.

## Run it

```bash
python scraper.py          # fetch official pages -> data/offers.json
python build.py            # render site/
```

Standard library only. No pip install, no keys, no browser.
Behind a TLS-intercepting proxy, set `PROMO_RADAR_INSECURE_SSL=1` to allow the
unverified fallback; the default is strict.

## Change the providers

Edit `.ilang/site.ilang` — one row per provider:

```
Provider | https://site.example | https://site.example/pricing | <affiliate url or empty>
```

Then re-run `scraper.py && build.py`. Nothing is hardcoded in the Python: both
scripts read that file. If editing it does not change the site, the config has
become decoration and that is a bug.

## Deploy

1. Push this repo to GitHub (public — the free Actions minutes and the backlink
   both depend on it).
2. Cloudflare Pages → connect repo → build command `python build.py`,
   output directory `site/`. Set `PYTHON_VERSION=3.12` if the default differs.
3. **Copy the real `pages.dev` address** (it gets a suffix like `-2r6` when the
   project name is taken) into `base_url` in `.ilang/site.ilang`, then rebuild
   and push. Skipping this points every canonical and the whole sitemap at
   somebody else's site.

### About `canonical_relative`

Some preview hosts (including the WorkBuddy share link this repo was first
published on) hand out a **new hostname on every publish**. Pinning 42 canonical
tags to such a host means every redeploy silently points them at a dead name.

`canonical_relative: true` in `.ilang/site.ilang` makes per-page canonical,
`og:url` and JSON-LD URLs root-relative, so they resolve against whatever host is
actually serving. `sitemap.xml` and the `Sitemap:` line still need an absolute
host and therefore still follow `base_url`.

Once you have a fixed domain, set `canonical_relative: false` — absolute
canonicals are the better form when the host does not move.

Rebase command:

```bash
python - <<'EOF'
p = ".ilang/site.ilang"
s = open(p, encoding="utf-8").read().replace("OLD-HOST", "NEW-HOST")
open(p, "w", encoding="utf-8", newline="\n").write(s)
EOF
python build.py
```
4. Register your own domain and bind it — the `pages.dev` subdomain ages under
   Cloudflare's name, not yours, and in this category age is the asset.

## Honest limits

- 12 providers are tracked; at the last run 5 exposed a machine-readable price.
  The other 7 render prices client-side, so their pages show "no verified price"
  instead of a number. That is the intended behaviour, not a bug to patch with
  guesses.
- This is an auto-updating pipeline plus an asset that accumulates age. It is not
  an income machine. Whether it earns depends on the niche and on keeping it running.

## Files

| path | role |
|---|---|
| `.ilang/site.ilang` | single source of truth: brand, niche, providers, fields, rules |
| `siteilang.py` | parser for that file |
| `scraper.py` | robots-aware fetch + extraction |
| `build.py` | static render + structured data |
| `templates/` | page layouts |
| `AGENTS.md` | project boundaries for any AI that picks this repo up |
| `.github/workflows/update.yml` | the cron that keeps it alive |

Site rules are described with the I-Lang protocol — see `.ilang/site.ilang` (protocol: ilang.ai).
