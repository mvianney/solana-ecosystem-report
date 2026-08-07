"""
collectors/news_collector.py
============================
Pull recent Solana ecosystem news from RSS/Atom feeds using stdlib only.
No API key required.

Sources (both verified reachable and parseable as of 2026-08-06):

    PRIMARY — Solana Foundation official blog
        https://solana.com/news/rss.xml
        RSS 2.0, ~20 items, 100 % Solana-native content.
        No keyword filter needed.

    SECONDARY — Cointelegraph Solana tag feed
        https://cointelegraph.com/rss/tag/solana
        RSS 2.0, ~30 items, tag-based so a small fraction of items are
        tangentially related.  Items are keyword-filtered (title + description
        must contain "solana", case-insensitive) before returning.

Rejected candidates (tested same date):
    CoinDesk Solana tag  — heavy bleed of non-Solana stories
    Decrypt Solana tag   — large bleed; many items lack "Solana" in body
    The Block            — 404 / connection reset

Architecture
------------
    fetch_feed(url)        Parse a full RSS/Atom feed, return raw item list.
    get_solana_news(n)     Aggregate both sources, de-duplicate by link,
                           sort newest-first, return top-n.
    collect_all()          Wrap in meta envelope, same shape as other
                           collectors in this project.

Usage (standalone):
    python3 -m collectors.news_collector
    # or
    python3 collectors/news_collector.py
"""

import email.utils
import gzip
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

# ─── Configuration ────────────────────────────────────────────────────────────

TIMEOUT_SEC = 5     # per-request socket timeout
MAX_RETRIES = 2     # extra attempts after first failure
RETRY_DELAY = 1.0   # base back-off (doubles each retry)

# Feeds: (label, url, keyword_filter)
# keyword_filter=True  → keep only items mentioning "solana" in title/desc
# keyword_filter=False → accept all items (source is already Solana-only)
FEEDS: list[tuple[str, str, bool]] = [
    (
        "Solana Blog",
        "https://solana.com/news/rss.xml",
        False,   # official blog — all items are Solana content
    ),
    (
        "Cointelegraph Solana",
        "https://cointelegraph.com/rss/tag/solana",
        True,    # tag feed — filter by keyword to reduce bleed
    ),
]

# Atom namespace shorthand
_ATOM = "http://www.w3.org/2005/Atom"

# ─── HTTP helper (matches pattern in rpc_collector / coingecko_collector) ─────


def _http_get(url: str) -> bytes:
    """
    GET ``url`` and return the raw response bytes.

    Implements retry / exponential back-off identical to the other collectors
    in this project (``rpc_collector``, ``coingecko_collector``).

    Args:
        url: Fully-qualified URL to fetch.

    Returns:
        Raw response body as bytes (possibly gzip-compressed — callers should
        decompress if needed).

    Raises:
        RuntimeError: If all retry attempts fail.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept":          "application/rss+xml, application/atom+xml, "
                               "application/xml, text/xml, */*",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent":      "solana-ecosystem-report/0.1 (python-stdlib)",
        },
    )

    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                raw = resp.read()
                # Transparently decompress if the server gzip-encoded the body
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw

        except urllib.error.HTTPError as exc:
            last_exc = exc   # 4xx / 5xx — retry

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc   # network error — retry

    raise RuntimeError(
        f"GET {url} failed after {MAX_RETRIES + 1} attempts: {last_exc}"
    )


def _safe_get(url: str, label: str) -> bytes | None:
    """
    ``_http_get`` wrapper that logs a WARNING and returns ``None`` on failure
    so callers can degrade gracefully rather than crashing.
    """
    try:
        return _http_get(url)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: news/{label} fetch failed — {exc!r}",
            file=sys.stderr,
        )
        return None


# ─── RSS / Atom field helpers ─────────────────────────────────────────────────


def _item_text(item: ET.Element, *tags: str) -> str:
    """Return the text of the first matching child tag, or ''."""
    for tag in tags:
        val = item.findtext(tag)
        if val:
            return val.strip()
    return ""


def _item_link(item: ET.Element) -> str:
    """
    Extract the article URL from an RSS <item> or Atom <entry>.

    RSS items store the URL in <link> text; Atom entries store it as the
    ``href`` attribute of <link rel="alternate">.
    """
    # RSS 2.0: <link> text node
    link = item.findtext("link")
    if link and link.strip():
        return link.strip()

    # Atom: <link href="…" rel="alternate"/>
    for link_el in item.findall(f"{{{_ATOM}}}link"):
        href = link_el.get("href", "").strip()
        rel  = link_el.get("rel", "alternate")
        if href and rel in ("alternate", ""):
            return href

    return ""


# ─── Core fetch function ──────────────────────────────────────────────────────


def fetch_feed(url: str, label: str = "feed") -> list[dict[str, str]]:
    """
    Fetch and parse an RSS 2.0 or Atom 1.0 feed.

    Extracts ``title``, ``link``, and ``published`` (raw date string as
    returned by the feed) for each item/entry.  On any fetch or parse error,
    logs a WARNING to stderr and returns an empty list — never raises.

    Args:
        url:   Full feed URL.
        label: Human-readable name used in log messages.

    Returns:
        List of dicts with keys ``title``, ``link``, ``published``.
        Empty list on failure.
    """
    raw = _safe_get(url, label)
    if raw is None:
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        print(
            f"WARNING: news/{label} XML parse error — {exc!r}",
            file=sys.stderr,
        )
        return []

    # Detect RSS 2.0 vs Atom 1.0
    is_atom = root.tag == f"{{{_ATOM}}}feed" or root.tag == "feed"
    items = (
        root.findall(f"{{{_ATOM}}}entry") if is_atom
        else root.findall(".//item")
    )

    results: list[dict[str, str]] = []
    for item in items:
        if is_atom:
            title = _item_text(
                item,
                f"{{{_ATOM}}}title",
                "title",
            )
            published = _item_text(
                item,
                f"{{{_ATOM}}}published",
                f"{{{_ATOM}}}updated",
            )
        else:
            title = _item_text(item, "title")
            published = _item_text(
                item,
                "pubDate",
                "dc:date",
                "{http://purl.org/dc/elements/1.1/}date",
            )

        link = _item_link(item)

        if not title:
            continue   # skip structurally broken items

        results.append({
            "title":     title,
            "link":      link,
            "published": published,
        })

    return results


# ─── Date parsing ────────────────────────────────────────────────────────────


def _parse_date(date_str: str) -> float:
    """
    Parse an RSS/Atom date string into a UTC epoch float for reliable sorting.

    Handles both RFC-2822 (RSS 2.0 ``pubDate``) and ISO-8601 (Atom
    ``published``).  Returns 0.0 on failure so unparseable dates sort to the
    bottom rather than crashing.
    """
    if not date_str:
        return 0.0
    # RFC-2822: "Wed, 05 Aug 2026 18:55:00 GMT"
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return parsed.timestamp()
    except Exception:  # noqa: BLE001
        pass
    # ISO-8601: "2026-08-05T18:55:00Z" or "2026-08-05T18:55:00+00:00"
    try:
        s = date_str.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        pass
    return 0.0


# ─── Aggregator ───────────────────────────────────────────────────────────────


def _mentions_solana(item: dict[str, str]) -> bool:
    """Return True if the item title or description contains 'solana'."""
    haystack = (item.get("title", "") + " " + item.get("description", "")).lower()
    return "solana" in haystack


def get_solana_news(limit: int = 5) -> list[dict[str, str]]:
    """
    Aggregate Solana news from all configured feeds, de-duplicate by URL,
    and return the most recent ``limit`` items.

    Items from keyword-filtered feeds are kept only when their title or
    description contains "solana" (case-insensitive).  Items from the
    official Solana Blog are always included — the source is 100 % Solana
    content so no filtering is applied.

    Args:
        limit: Maximum number of items to return (default 5).

    Returns:
        List of dicts — ``{ "title", "link", "published", "source" }`` —
        sorted newest-first by ``published`` string (lexicographic; RFC-2822
        and ISO-8601 both sort correctly this way when formatted consistently).
        Empty list if all feeds fail.
    """
    seen_links: set[str] = set()
    merged: list[dict[str, str]] = []

    for label, url, do_filter in FEEDS:
        items = fetch_feed(url, label)
        kept = 0
        for item in items:
            link = item.get("link", "")

            # De-duplicate by URL
            if link and link in seen_links:
                continue

            # Keyword filter for general feeds
            if do_filter and not _mentions_solana(item):
                continue

            if link:
                seen_links.add(link)

            merged.append({**item, "source": label})
            kept += 1

        print(
            f"  news/{label}: fetched {len(items)} items, kept {kept}",
            file=sys.stderr,
        )

    # Sort newest-first using parsed timestamps (handles RFC-2822 month names
    # correctly — lexicographic sort on raw strings does NOT work for RFC-2822).
    merged.sort(key=lambda x: _parse_date(x.get("published", "")), reverse=True)

    return merged[:limit]


# ─── Combined collector (matches shape of other collectors) ───────────────────


def collect_all(limit: int = 5) -> dict:
    """
    Run ``get_solana_news()`` and wrap the result in a meta envelope
    consistent with the other collectors in this project.

    Returns::

        {
            "meta": {
                "collected_at": "<ISO-8601 UTC>",
                "source":       "news-collector",
                "feeds":        ["Solana Blog", "Cointelegraph Solana"],
                "limit":        5,
            },
            "news": [
                {
                    "title":     "…",
                    "link":      "https://…",
                    "published": "Wed, 05 Aug 2026 09:33:00 GMT",
                    "source":    "Solana Blog",
                },
                …
            ],
        }

    On complete failure, ``news`` is an empty list — never ``None``.
    """
    collected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    news = get_solana_news(limit=limit)

    return {
        "meta": {
            "collected_at": collected_at,
            "source":       "news-collector",
            "feeds":        [label for label, _, _ in FEEDS],
            "limit":        limit,
        },
        "news": news,
    }


# ─── Standalone runner ────────────────────────────────────────────────────────


if __name__ == "__main__":
    import json

    print("Fetching Solana news from RSS feeds …\n", file=sys.stderr)
    t0      = time.perf_counter()
    result  = collect_all()
    elapsed = time.perf_counter() - t0

    print(json.dumps(result, indent=2))
    print(
        f"\n# collect_all() completed in {elapsed:.2f}s  "
        f"({len(result['news'])} items returned)",
        file=sys.stderr,
    )
