# ::ILANG
# [TYPE:module][PROJECT:cloud-storage-deals]
# ::STATE{@SELF, role:parser, reads:.ilang/site.ilang, feeds:[scraper.py, build.py]}
# ::RULE{配置唯一真源⇒任何厂商清单或站点设置都从这个文件读 代码里不许再写一份}
# ::BOUNDARY{never:解析不出来就静默用默认值顶上 必须抛错|scope:permanent}
"""Minimal parser for .ilang/site.ilang — the single source of truth for this site."""

from __future__ import annotations

import os
import re

DEFAULT_PATH = os.path.join(".ilang", "site.ilang")


class IlangConfigError(RuntimeError):
    pass


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return s.strip("-") or "item"


def _split_fields(body: str) -> dict:
    """Parse `key: value` pairs separated by commas / newlines."""
    out = {}
    for chunk in re.split(r"[,\n]", body):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        k, v = chunk.split(":", 1)
        out[k.strip().strip('"').strip("'")] = v.strip().strip('"').strip("'")
    return out


def load(path: str = DEFAULT_PATH) -> dict:
    if not os.path.exists(path):
        raise IlangConfigError(f"config not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    cfg = {"site": {}, "modules": {}, "rules": [], "path": path}
    current = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        if line.startswith("::STATE{"):
            body = line[len("::STATE{"):].rstrip("}")
            if "@" in body:
                body = body.split(",", 1)[1] if "," in body else ""
            cfg["site"] = _split_fields(body)
            current = None
            continue

        m = re.match(r"^::MODULE\{([A-Z_]+)(?:\|(.*))?\}\s*$", line)
        if m:
            name = m.group(1)
            cfg["modules"][name] = {"title": (m.group(2) or "").strip(), "lines": []}
            current = name
            continue

        if line.startswith("::RULE{") or line.startswith("::BOUNDARY{"):
            cfg["rules"].append(line.strip())
            current = None
            continue

        if line.startswith("::") or line.startswith("[") or line.strip() == "ILANG":
            current = None
            continue

        if current:
            cfg["modules"][current]["lines"].append(line.strip())

    if "PROVIDERS" not in cfg["modules"]:
        raise IlangConfigError("::MODULE{PROVIDERS} missing — scraper has nothing to fetch")

    return cfg


def providers(cfg: dict) -> list[dict]:
    """::MODULE{PROVIDERS} → [{'name','slug','site','source','affiliate','outbound'}]"""
    out = []
    for line in cfg["modules"].get("PROVIDERS", {}).get("lines", []):
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        parts += [""] * (4 - len(parts))
        name, site, source, affiliate = parts[:4]
        if not name or not source:
            continue
        out.append(
            {
                "name": name,
                "slug": _slug(name),
                "site": site,
                "source": source,
                "affiliate": affiliate,
                "outbound": affiliate or source,
            }
        )
    if not out:
        raise IlangConfigError("::MODULE{PROVIDERS} parsed to zero usable rows")
    return out


def fields(cfg: dict) -> list[str]:
    """::MODULE{FIELDS} → ordered field names."""
    names = []
    for line in cfg["modules"].get("FIELDS", {}).get("lines", []):
        names += [t for t in re.split(r"[\s,]+", line) if t]
    return names


def settings(cfg: dict) -> dict:
    """::MODULE{SITE} key: value lines, merged over ::STATE{@SITE}."""
    out = dict(cfg["site"])
    for line in cfg["modules"].get("SITE", {}).get("lines", []):
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    out.setdefault("locale", "en-US")
    out.setdefault("currency", "USD")
    out.setdefault("base_url", "")
    return out


if __name__ == "__main__":
    c = load()
    s = settings(c)
    print("brand :", c["site"].get("brand"))
    print("niche :", c["site"].get("niche"))
    print("base  :", s["base_url"])
    print("fields:", fields(c))
    for p in providers(c):
        print(f"  - {p['name']:<10} {p['slug']:<10} {p['source']}")
