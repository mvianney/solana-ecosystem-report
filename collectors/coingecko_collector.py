"""
collectors/coingecko_collector.py
==================================
Fetches SOL price and market data from CoinGecko's free public API.
No API key required.  Pure stdlib — urllib, json, time.

API reference: https://docs.coingecko.com/reference/introduction
Base URL:      https://api.coingecko.com/api/v3

Rate limits (public, no key):
    ~10-30 requests/minute.  At the designed call volume (2 GET requests
    per collect_all() run, called at most every 10-30 s) this is far within
    limits — no special throttling logic is needed.

Usage (standalone sanity-check):
    python3 -m collectors.coingecko_collector
    # or
    python3 collectors/coingecko_collector.py
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_URL    = "https://api.coingecko.com/api/v3"
TIMEOUT_SEC = 5    # per-request socket timeout
MAX_RETRIES = 2    # extra attempts after first failure
RETRY_DELAY = 1.0  # base backoff (doubles each retry)

# ─── Shared HTTP helper ───────────────────────────────────────────────────────


def _http_get(url: str) -> Any:
    """
    Perform a GET request to ``url`` and return the parsed JSON body.

    Implements the same retry/backoff pattern used in ``rpc_collector.py``
    so collector behaviour is consistent across the project.

    Args:
        url: Fully-qualified URL to fetch.

    Returns:
        Parsed JSON value (dict, list, etc.) from the response body.

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
            delay = RETRY_DELAY * (2 ** (attempt - 1))
            time.sleep(delay)

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            # 429 Too Many Requests — back off; other 4xx/5xx — retry too
            last_exc = exc

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc

        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_exc = exc
            # Malformed response — retry in case of transient truncation

    raise RuntimeError(
        f"GET {url} failed after {MAX_RETRIES + 1} attempts: {last_exc}"
    )


def _safe_get(url: str, label: str) -> Any | None:
    """
    Wrapper around ``_http_get`` that swallows exceptions, logs a ``WARNING``
    to stderr, and returns ``None`` so callers can degrade gracefully.

    Args:
        url:   URL to fetch.
        label: Human-readable endpoint name used in the warning message.

    Returns:
        Parsed JSON on success, ``None`` on any failure.
    """
    import sys as _sys

    try:
        return _http_get(url)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: coingecko/{label} failed — {exc!r}",
            file=_sys.stderr,
        )
        return None


# ─── Per-endpoint collector functions ─────────────────────────────────────────


def get_sol_price() -> dict | None:
    """
    Return the current SOL/USD spot price and 24-hour price change.

    Uses the ``/simple/price`` endpoint — the lightest CoinGecko call,
    designed for frequent polling.

    Endpoint::

        GET /simple/price?ids=solana&vs_currencies=usd&include_24hr_change=true

    Returns:
        Dict with keys:
            - ``price_usd``     (float): Current SOL price in USD.
            - ``change_24h_pct`` (float): 24-hour price change percentage
              (positive = up, negative = down).
        Returns ``None`` on failure.

    CoinGecko docs:
        https://docs.coingecko.com/reference/simple-price
    """
    url  = (
        f"{BASE_URL}/simple/price"
        "?ids=solana"
        "&vs_currencies=usd"
        "&include_24hr_change=true"
    )
    data = _safe_get(url, "simple/price")
    if not data:
        return None

    solana = data.get("solana", {})
    price  = solana.get("usd")
    change = solana.get("usd_24h_change")

    if price is None:
        import sys
        print(
            "WARNING: coingecko/simple/price — 'usd' field missing from response",
            file=sys.stderr,
        )
        return None

    return {
        "price_usd":      float(price),
        "change_24h_pct": float(change) if change is not None else None,
    }


def get_sol_market_data() -> dict | None:
    """
    Return SOL market-cap, CEX trading volume, and circulating supply.

    Uses the ``/coins/solana`` detail endpoint with only ``market_data=true``
    enabled to minimise response size and latency.

    **Important distinction — two types of volume:**

    - ``volume_24h_usd`` (this field): Total SOL trading volume across
      *centralised exchanges* (CEX) in the past 24 h.  This is a measure of
      overall market liquidity, *not* on-chain activity.
    - On-chain DEX volume (``dexVolume24hB`` in the dashboard): Swap volume
      settled on Solana DeFi protocols — fetched separately from DeFiLlama.
      Do not conflate these two numbers.

    Endpoint::

        GET /coins/solana
            ?localization=false
            &tickers=false
            &market_data=true
            &community_data=false
            &developer_data=false

    Returns:
        Dict with keys:
            - ``market_cap_usd``    (float): Total market capitalisation in USD.
            - ``volume_24h_usd``    (float): CEX trading volume (USD, 24 h).
            - ``circulating_supply`` (float): Circulating SOL supply reported
              by CoinGecko (cross-check against ``rpc_collector.get_supply()``).
        Returns ``None`` on failure.

    CoinGecko docs:
        https://docs.coingecko.com/reference/coins-id
    """
    url  = (
        f"{BASE_URL}/coins/solana"
        "?localization=false"
        "&tickers=false"
        "&market_data=true"
        "&community_data=false"
        "&developer_data=false"
    )
    data = _safe_get(url, "coins/solana")
    if not data:
        return None

    md = data.get("market_data", {})

    def _usd(field: str) -> float | None:
        val = md.get(field, {})
        if isinstance(val, dict):
            return float(val["usd"]) if "usd" in val else None
        return float(val) if val is not None else None

    market_cap  = _usd("market_cap")
    volume_24h  = _usd("total_volume")
    circ_supply = md.get("circulating_supply")

    return {
        "market_cap_usd":     market_cap,
        # CEX trading volume — NOT on-chain DEX volume (see docstring)
        "volume_24h_usd":     volume_24h,
        "circulating_supply": float(circ_supply) if circ_supply is not None else None,
    }


# ─── Combined collector ───────────────────────────────────────────────────────


def collect_all() -> dict:
    """
    Run both CoinGecko collectors concurrently and return a normalised dict.

    The two endpoints are independent so they are fetched in parallel via
    ``concurrent.futures.ThreadPoolExecutor`` — both calls finish in the
    time of the slower one rather than their sum.

    Field names follow the dashboard's ``DASHBOARD_DATA.economics`` shape:

    ``meta``
        - ``collected_at`` (str): ISO-8601 UTC timestamp.
        - ``source``       (str): Always ``"coingecko-collector"``.

    ``market``
        - ``priceUsd``          (float | None): SOL spot price in USD.
        - ``change24hPct``      (float | None): 24-h price change %.
        - ``marketCapUsd``      (float | None): Market cap in USD.
        - ``volume24hUsd``      (float | None): **CEX** trading volume, USD.
          *(separate from on-chain DEX volume — see ``get_sol_market_data``)*
        - ``circulatingSupply`` (float | None): Circulating SOL from CoinGecko.

    Returns:
        Combined dict.  Individual ``None`` values mean that endpoint failed;
        the rest of the report can still proceed.
    """
    import concurrent.futures

    collected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_price  = pool.submit(get_sol_price)
        f_market = pool.submit(get_sol_market_data)

        try:
            price = f_price.result(timeout=TIMEOUT_SEC + 4)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"WARNING: get_sol_price future failed — {exc!r}", file=sys.stderr)
            price = None

        try:
            market = f_market.result(timeout=TIMEOUT_SEC + 4)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"WARNING: get_sol_market_data future failed — {exc!r}", file=sys.stderr)
            market = None

    return {
        "meta": {
            "collected_at": collected_at,
            "source":       "coingecko-collector",
            "base_url":     BASE_URL,
        },
        "market": {
            # Price data — from /simple/price
            "priceUsd":          price["price_usd"]      if price  else None,
            "change24hPct":      price["change_24h_pct"] if price  else None,
            # Market data — from /coins/solana
            "marketCapUsd":      market["market_cap_usd"]     if market else None,
            "volume24hUsd":      market["volume_24h_usd"]     if market else None,  # CEX only
            "circulatingSupply": market["circulating_supply"] if market else None,
        },
    }


# ─── Standalone runner ────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys

    print(f"Fetching SOL market data from CoinGecko …", file=sys.stderr)
    print(
        f"  timeout={TIMEOUT_SEC}s per call   max_retries={MAX_RETRIES}\n",
        file=sys.stderr,
    )

    t0      = time.perf_counter()
    data    = collect_all()
    elapsed = time.perf_counter() - t0

    print(json.dumps(data, indent=2))
    print(
        f"\n# collect_all() completed in {elapsed:.2f}s  (target: <2.0s)",
        file=sys.stderr,
    )
