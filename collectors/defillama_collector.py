"""
collectors/defillama_collector.py
==================================
Fetches Solana TVL, stablecoin supply, and 24h DEX volume from DeFiLlama's free public API.
No API key required. Pure stdlib — urllib, json, time.

API references:
- TVL:                https://api.llama.fi/v2/historicalChainTvl/Solana
- Stablecoin Supply:  https://stablecoins.llama.fi/stablecoins?includePrices=true
- DEX Volume:         https://api.llama.fi/overview/dexs/solana?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true

Usage (standalone sanity-check):
    python3 -m collectors.defillama_collector
    # or
    python3 collectors/defillama_collector.py
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_URL             = "https://api.llama.fi"
STABLECOINS_URL      = "https://stablecoins.llama.fi/stablecoins?includePrices=true"
TIMEOUT_SEC          = 5    # per-request socket timeout
MAX_RETRIES          = 2    # extra attempts after first failure
RETRY_DELAY          = 1.0  # base backoff (doubles each retry)

# ─── Shared HTTP helper ───────────────────────────────────────────────────────


def _http_get(url: str) -> Any:
    """
    Perform a GET request to ``url`` and return the parsed JSON body.

    Args:
        url: Fully-qualified URL to fetch.

    Returns:
        Parsed JSON value from the response body.

    Raises:
        RuntimeError: If all retry attempts fail or response cannot be parsed as JSON.
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

        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc

        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_exc = exc

    raise RuntimeError(
        f"GET {url} failed after {MAX_RETRIES + 1} attempts: {last_exc}"
    )


def _safe_get(url: str, label: str) -> Any | None:
    """
    Wrapper around ``_http_get`` that swallows exceptions, logs a ``WARNING``
    to stderr, and returns ``None``.
    """
    import sys as _sys

    try:
        return _http_get(url)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: defillama/{label} failed — {exc!r}",
            file=_sys.stderr,
        )
        return None


# ─── Per-endpoint collector functions ─────────────────────────────────────────


def get_solana_tvl() -> dict | None:
    """
    Return the most recent Solana Total Value Locked (TVL) in USD.

    Endpoint: GET /v2/historicalChainTvl/Solana

    Returns:
        Dict with key ``tvl_usd`` (float), or ``None`` on failure.
    """
    url = f"{BASE_URL}/v2/historicalChainTvl/Solana"
    data = _safe_get(url, "v2/historicalChainTvl/Solana")
    if not data or not isinstance(data, list):
        return None

    # Select entry with the highest Unix timestamp
    latest = max(data, key=lambda entry: entry.get("date", 0))
    tvl = latest.get("tvl")

    if tvl is None:
        return None

    return {"tvl_usd": float(tvl)}


def get_stablecoin_supply() -> dict | None:
    """
    Return the total circulating supply of all stablecoins on Solana in USD.

    Endpoint: GET https://stablecoins.llama.fi/stablecoins?includePrices=true

    Returns:
        Dict with key ``total_stablecoin_supply_usd`` (float), or ``None`` on failure.
    """
    data = _safe_get(STABLECOINS_URL, "stablecoins")
    if not data or not isinstance(data, dict):
        return None

    pegged_assets = data.get("peggedAssets", [])
    total_usd = 0.0

    for asset in pegged_assets:
        chain_circulating = asset.get("chainCirculating", {})
        solana_data = chain_circulating.get("Solana", {})
        
        current_amount = solana_data.get("current", {})
        peg_type = asset.get("pegType", "")

        # Only sum USD-pegged stablecoins (or default to current amount if pegType is pegUSD)
        if isinstance(current_amount, dict):
            usd_val = current_amount.get("peggedUSD")
            if usd_val is not None:
                total_usd += float(usd_val)
            elif "usd" in current_amount:
                total_usd += float(current_amount["usd"])
        elif isinstance(current_amount, (int, float)):
            # If current_amount is direct numeric value
            price = asset.get("price", 1.0) or 1.0
            total_usd += float(current_amount) * float(price)

    return {"total_stablecoin_supply_usd": round(total_usd, 2)}


def get_dex_volume() -> dict | None:
    """
    Return 24h DEX swap volume on Solana in USD.

    Endpoint: GET /overview/dexs/solana?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true

    Returns:
        Dict with key ``dex_volume_24h_usd`` (float), or ``None`` on failure.
    """
    url = (
        f"{BASE_URL}/overview/dexs/solana"
        "?excludeTotalDataChart=true"
        "&excludeTotalDataChartBreakdown=true"
    )
    data = _safe_get(url, "overview/dexs/solana")
    if not data or not isinstance(data, dict):
        return None

    total_24h = data.get("total24h")
    if total_24h is None:
        return None

    return {"dex_volume_24h_usd": float(total_24h)}


# ─── Combined collector ───────────────────────────────────────────────────────


def collect_all() -> dict:
    """
    Run all DeFiLlama collectors concurrently and return a normalised dict.

    Returns:
        Dict with keys:
        - meta: { collected_at, source }
        - defi: { tvlUsd, stablecoinSupplyUsd, dexVolume24hUsd }
    """
    import concurrent.futures

    collected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_tvl     = pool.submit(get_solana_tvl)
        f_stables = pool.submit(get_stablecoin_supply)
        f_dex     = pool.submit(get_dex_volume)

        try:
            tvl_res = f_tvl.result(timeout=TIMEOUT_SEC + 4)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"WARNING: get_solana_tvl future failed — {exc!r}", file=sys.stderr)
            tvl_res = None

        try:
            stables_res = f_stables.result(timeout=TIMEOUT_SEC + 4)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"WARNING: get_stablecoin_supply future failed — {exc!r}", file=sys.stderr)
            stables_res = None

        try:
            dex_res = f_dex.result(timeout=TIMEOUT_SEC + 4)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"WARNING: get_dex_volume future failed — {exc!r}", file=sys.stderr)
            dex_res = None

    return {
        "meta": {
            "collected_at": collected_at,
            "source":       "defillama-collector",
            "base_url":     BASE_URL,
        },
        "defi": {
            "tvlUsd":                tvl_res["tvl_usd"]                         if tvl_res     else None,
            "stablecoinSupplyUsd":   stables_res["total_stablecoin_supply_usd"] if stables_res else None,
            "dexVolume24hUsd":       dex_res["dex_volume_24h_usd"]             if dex_res     else None,
        },
    }


# ─── Standalone runner ────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys

    print("Fetching Solana DeFi data from DeFiLlama …", file=sys.stderr)
    print(
        f"  timeout={TIMEOUT_SEC}s per call   max_retries={MAX_RETRIES}\n",
        file=sys.stderr,
    )

    t0      = time.perf_counter()
    data    = collect_all()
    elapsed = time.perf_counter() - t0

    print(json.dumps(data, indent=2))
    print(
        f"\n# collect_all() completed in {elapsed:.2f}s  (target: <3.0s)",
        file=sys.stderr,
    )
