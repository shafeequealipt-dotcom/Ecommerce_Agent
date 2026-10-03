#!/usr/bin/env python3
"""Keyword research — pulls live shopper search suggestions from Amazon.in, Flipkart and Google India.

Autocomplete is what shoppers are actually typing right now, ordered by popularity.
It gives demand *ranking*, not search volume (that needs Helium 10 / Keepa data).

Usage: keyword_research.py "shoe wipes" "sneaker cleaner"
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Seed expansions that surface long-tail intent ("... for white shoes", "... best")
MODIFIERS = ["", " for", " best", " with"]

# Other marketplaces/retailers leaking into Google suggestions — useless in a listing
NOISE = {"amazon", "flipkart", "meesho", "zepto", "blinkit", "zudio", "myntra", "ajio",
         "nykaa", "dmart", "jiomart", "instamart", "near me", "hsn code", "instant delivery"}


def _fetch(url, data=None, headers=None, timeout=12):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def amazon_suggestions(query):
    url = ("https://www.amazon.in/suggestions?mid=A21TJRUUN4KGV&alias=aps&limit=11"
           "&suggestion-type=KEYWORD&prefix=" + urllib.parse.quote(query))
    result = _fetch(url) or {}
    return [s.get("value", "") for s in result.get("suggestions", []) if s.get("value")]


def _collect_queries(node, out):
    """Flipkart nests each suggestion's text under a 'query' key at varying depth."""
    if isinstance(node, dict):
        q = node.get("query")
        if isinstance(q, str) and q:
            out.append(q)
        for v in node.values():
            _collect_queries(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_queries(v, out)


def flipkart_suggestions(query):
    result = _fetch(
        "https://1.rome.api.flipkart.com/api/4/discover/autosuggest",
        data={"query": query, "marketPlaceId": "FLIPKART", "types": ["QUERY", "QUERY_STORE"], "rows": 10},
        headers={
            "Content-Type": "application/json",
            "X-User-Agent": UA + " FKUA/website/42/website/Desktop",
            "Origin": "https://www.flipkart.com",
            "Referer": "https://www.flipkart.com/",
        },
    ) or {}
    out = []
    _collect_queries(result.get("RESPONSE", {}).get("suggestions", []), out)
    return out


def google_suggestions(query):
    url = ("https://suggestqueries.google.com/complete/search?client=firefox&gl=in&hl=en&q="
           + urllib.parse.quote(query))
    result = _fetch(url)
    return result[1] if isinstance(result, list) and len(result) > 1 else []


SOURCES = {"amazon": amazon_suggestions, "flipkart": flipkart_suggestions, "google": google_suggestions}


def _clean(term):
    t = re.sub(r"\s+", " ", urllib.parse.unquote_plus(str(term)).lower()).strip()
    if not t or any(re.search(r"\b" + re.escape(n) + r"\b", t) for n in NOISE):
        return None
    return t


def research(seeds, max_seeds=4):
    """Query every source for each seed. Returns per-source lists plus a combined ranking.

    Ranking score: higher autocomplete position + appearing on more platforms = more demand.
    """
    seeds = [s.strip().lower() for s in seeds if s and s.strip()][:max_seeds]
    per_source = {name: [] for name in SOURCES}
    scores, found_on = {}, {}

    for seed in seeds:
        for mod in MODIFIERS:
            query = seed + mod
            for name, fn in SOURCES.items():
                # Google is only a cross-check — the bare seed is enough
                if name == "google" and mod:
                    continue
                for pos, raw in enumerate(fn(query)):
                    term = _clean(raw)
                    if not term:
                        continue
                    if term not in per_source[name]:
                        per_source[name].append(term)
                    weight = 2 if name in ("amazon", "flipkart") else 1
                    scores[term] = scores.get(term, 0) + weight * max(1, 10 - pos)
                    found_on.setdefault(term, set()).add(name)
                time.sleep(0.15)

    ranked = sorted(scores, key=lambda t: (-len(found_on[t]), -scores[t]))
    return {
        "seeds": seeds,
        "amazon": per_source["amazon"][:40],
        "flipkart": per_source["flipkart"][:40],
        "google": per_source["google"][:20],
        "ranked": [{"keyword": t, "score": scores[t], "platforms": sorted(found_on[t])} for t in ranked[:50]],
    }


def coverage(research_data, listing):
    """Which researched keywords made it into each platform's listing copy."""
    fields = listing.get("listing_fields", {})
    plat = listing.get("platform_specific", {})

    def text(*parts):
        flat = []
        for p in parts:
            flat.append(" ".join(map(str, p)) if isinstance(p, list) else str(p or ""))
        return " ".join(flat).lower()

    copy = {
        "amazon": text(plat.get("amazon", {}).get("title"), plat.get("amazon", {}).get("bullets"),
                       plat.get("amazon", {}).get("description"), plat.get("amazon", {}).get("backend_keywords"),
                       fields.get("backend_search_terms")),
        "flipkart": text(plat.get("flipkart", {}).get("title"), plat.get("flipkart", {}).get("description"),
                         plat.get("flipkart", {}).get("search_keywords")),
    }
    report = {}
    for name, body in copy.items():
        words = set(re.findall(r"[a-z0-9]+", body))
        kws = research_data.get(name, [])[:20]
        # A phrase counts as covered when every word of it is indexed somewhere in the copy
        hit = [k for k in kws if all(w in words for w in re.findall(r"[a-z0-9]+", k))]
        report[name] = {
            "covered": hit,
            "missing": [k for k in kws if k not in hit],
            "coverage_pct": round(100 * len(hit) / len(kws)) if kws else 0,
        }
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    print(json.dumps(research(sys.argv[1:]), indent=2, ensure_ascii=False))
