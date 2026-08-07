"""
collectors/jupiter_collector.py
================================
Fetches the SOL/USD spot price from Jupiter's Lite Price API (v3).
No API key required.  Pure stdlib — urllib, json, time.

API reference: https://dev.jup.ag/docs/apis/price-api-v3
Endpoint:      https://lite-api.jup.ag/price/v3?ids=<MINT>

Live response (verified 2026-08-06):

    {
      "So11111111111111111111111111111111111111112": {
        "createdAt":     "2024-06-05T08:55:25.527Z",
        "liquidity":     655217181.38,
        "usdPrice":      73.35632911699197,
        "blockId":       437610343,
        "decimals":      9,
        "priceChange24h": -1.0629090964513888
      }
    }

Fields used:
    usdPrice       — SOL/USD spot price derived from Jupiter aggregator routes.
    priceChange24h — 24-hour price change percentage (also present in this API,
                     unlike some Jupiter v2 endpoints).

Comparison with CoinGecko:
    Both sources provide a USD spot price and 24h change %.  Jupiter derives
    its price from on-chain swap liquidity across its aggregated routes;
    CoinGecko aggregates CEX order-book data.  The two prices will differ
    slightly at any given moment.  serve_data.py can use either or both for
    cross-validation or fallback.

Usage (standalone sanity-check):
    python3 -m collectors.jupiter_collector
    # or
    python3 collectors/jupiter_collector.py
"""

import json
import time
import urllib.error
import urllib.request

# ─── Configuration ────────────────────────────────────────────────────────────

SOL_MINT    = "So11111111111111111111111111111111111111112"
BASE_URL    = "https://lite-api.jup.ag/price/v3"
TIMEOUT_SEC = 5    # per-request socket timeout
MAX_RETRIES = 2    # extra attempts after first failure
RETRY_DELAY = 1.0  # base back-off (doubles each retry)

# ─── HTTP helper (matches pattern in rpc_collector / coingecko_collector) ─────


def _http_get(url: str) -> dict:
    """
    Perform a GET request to ``url`` and return the parsed JSON body.

    Implements the same retry / exponential back-off pattern used by the other
    collectors in this project.

    Args:
        url: Fully-qualified URL to fetch.

    Returns:
        Parsed JSON dict from the response body.

    Raises:
        RuntimeError: If all retry attempts fail or the response cannot be
                      parsed as JSON.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept":     "application/json",
            "User-Agent": "solana-ecosystem-report/0.1 (python-stdlib)",
        },
    )

    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            last_exc = exc   # 4xx / 5xx — retry

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc   # network error — retry

        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_exc = exc   # malformed response — retry

    raise RuntimeError(
        f"GET {url} failed after {MAX_RETRIES + 1} attempts: {last_exc}"
    )


def _safe_get(url: str, label: str) -> dict | None:
    """
    Wrapper around ``_http_get`` that swallows exceptions, logs a WARNING to
    stderr, and returns ``None`` so callers can degrade gracefully.
    """
    import sys as _sys

    try:
        return _http_get(url)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: jupiter/{label} failed — {exc!r}",
            file=_sys.stderr,
        )
        return None


# ─── Collector ────────────────────────────────────────────────────────────────


def get_sol_price() -> dict | None:
    """
    Return the current SOL/USD spot price and 24-hour change from Jupiter.

    Calls the ``/price/v3`` endpoint with the SOL mint address and extracts
    ``usdPrice`` and ``priceChange24h`` from the response.

    Endpoint::

        GET https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112

    Returns:
        Dict with keys:
            - ``price_usd``      (float): SOL spot price in USD (from on-chain
              liquidity across Jupiter aggregated routes).
            - ``change_24h_pct`` (float | None): 24-hour price change %.
            - ``block_id``       (int   | None): On-chain block the price was
              observed at.
            - ``liquidity_usd``  (float | None): Aggregated liquidity used to
              derive the price.
        Returns ``None`` on any failure.

    Note on price source:
        Jupiter derives ``usdPrice`` from on-chain swap routes and pooled
        liquidity.  CoinGecko aggregates CEX order-book prices.  The two will
        differ slightly; neither is "wrong" — they measure different markets.
    """
    url  = f"{BASE_URL}?ids={SOL_MINT}"
    data = _safe_get(url, "price/v3")
    if not data:
        return None

    sol = data.get(SOL_MINT)
    if not sol:
        import sys
        print(
            f"WARNING: jupiter/price/v3 — SOL mint key missing from response: {data!r}",
            file=sys.stderr,
        )
        return None

    price = sol.get("usdPrice")
    if price is None:
        import sys
        print(
            "WARNING: jupiter/price/v3 — 'usdPrice' field missing from SOL entry",
            file=sys.stderr,
        )
        return None

    return {
        "price_usd":     float(price),
        "change_24h_pct": float(sol["priceChange24h"]) if sol.get("priceChange24h") is not None else None,
        "block_id":       int(sol["blockId"])           if sol.get("blockId")       is not None else None,
        "liquidity_usd":  float(sol["liquidity"])       if sol.get("liquidity")     is not None else None,
    }


# ─── Combined collector ───────────────────────────────────────────────────────


def collect_all() -> dict:
    """
    Fetch the Jupiter SOL price and return a normalised collector envelope.

    Shape mirrors ``coingecko_collector.collect_all()`` so either can be used
    as a drop-in price source in ``serve_data.py``::

        {
            "meta": {
                "collected_at": "<ISO-8601 UTC>",
                "source":       "jupiter-collector",
                "base_url":     "https://lite-api.jup.ag/price/v3",
                "sol_mint":     "So11111111111111111111111111111111111111112",
            },
            "market": {
                "priceUsd":     73.36,        # from Jupiter on-chain liquidity
                "change24hPct": -1.06,        # 24h % (Jupiter does include this)
                # Fields absent from Jupiter v3 — always None from this source:
                "marketCapUsd":      None,
                "volume24hUsd":      None,    # Jupiter = DEX only; see note below
                "circulatingSupply": None,
            },
        }

    Note on volume:
        Jupiter *could* report DEX swap volume, but ``/price/v3`` does not
        include it.  CEX volume (``volume24hUsd``) is not available from
        Jupiter at all — use CoinGecko's ``/coins/solana`` for that field.

    Returns:
        Combined dict.  ``market.priceUsd`` is ``None`` on failure; all other
        fields remain populated so callers can safely check individual fields.
    """
    collected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    price = get_sol_price()

    return {
        "meta": {
            "collected_at": collected_at,
            "source":       "jupiter-collector",
            "base_url":     BASE_URL,
            "sol_mint":     SOL_MINT,
        },
        "market": {
            "priceUsd":          price["price_usd"]      if price else None,
            "change24hPct":      price["change_24h_pct"] if price else None,
            # Not available from Jupiter /price/v3 — source from CoinGecko instead
            "marketCapUsd":      None,
            "volume24hUsd":      None,
            "circulatingSupply": None,
            # Extra Jupiter-specific fields (informational)
            "_blockId":          price["block_id"]        if price else None,
            "_liquidityUsd":     price["liquidity_usd"]   if price else None,
        },
    }


# ─── Standalone runner ────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys

    print("Fetching SOL price from Jupiter Lite API v3 …", file=sys.stderr)
    print(
        f"  endpoint={BASE_URL}?ids={SOL_MINT}\n"
        f"  timeout={TIMEOUT_SEC}s per call   max_retries={MAX_RETRIES}\n",
        file=sys.stderr,
    )

    t0      = time.perf_counter()
    data    = collect_all()
    elapsed = time.perf_counter() - t0

    print(json.dumps(data, indent=2))
    print(
        f"\n# collect_all() completed in {elapsed:.2f}s  (target: <1.0s)",
        file=sys.stderr,
    )
