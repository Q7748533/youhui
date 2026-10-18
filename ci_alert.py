#!/usr/bin/env python3
"""CI health gate for the scraper.

Reads data/scrape_report.json (written by scraper.py) and opens a GitHub
issue the first time a provider's fetch fails or its verified-price count
collapses to zero. A single open issue acts as the latch: while it stays
open, later runs stay silent. Exit code is always 0 — alerting must never
break the data pipeline.
"""
import json
import os
import subprocess
import sys

REPORT = os.path.join("data", "scrape_report.json")
ISSUE_TITLE = "Scraper health alert: parser output dropped"
LABEL = "scraper-health"


def gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)


def main() -> int:
    try:
        with open(REPORT, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return 0  # no report = nothing to alert about; never fail CI

    flagged = [p for p in report.get("providers", []) if p.get("alert")]
    if not flagged:
        return 0

    lines = [f"- **{p['name']}** (`{p['slug']}`): status `{p['status']}`, "
             f"verified prices {p['priced_offers_before']} -> {p['priced_offers_now']}."
             + (f" Note: {p['note']}" if p.get("note") else "")
             for p in flagged]
    body = (
        "The scheduled scrape produced no verified prices for the providers "
        "below. Their previous records are retained as stale on the site — "
        "nothing was overwritten — but the parser for these official pages "
        "probably no longer matches a changed markup (or the fetch is being "
        "blocked).\n\n"
        + "\n".join(lines)
        + "\n\n_Run details: `data/scrape_report.json` at commit "
        + os.environ.get("GITHUB_SHA", "")[:12] + "._"
    )

    if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
        print("Scraper health alert (no token, printing instead):\n" + body)
        return 0

    existing = gh(["issue", "list", "--state", "open", "--search",
                   f'"{ISSUE_TITLE}" in:title', "--json", "number"])
    if existing.returncode == 0 and existing.stdout.strip() not in ("", "[]"):
        print("Open scraper-health issue already exists; not duplicating.")
        return 0

    created = gh(["issue", "create", "--title", ISSUE_TITLE,
                  "--body", body, "--label", LABEL])
    if created.returncode == 0:
        print("Opened scraper-health issue.")
    else:
        # Label may not exist; retry without it.
        retry = gh(["issue", "create", "--title", ISSUE_TITLE, "--body", body])
        print("Opened scraper-health issue." if retry.returncode == 0
              else f"Could not open issue: {retry.stderr.strip()[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
