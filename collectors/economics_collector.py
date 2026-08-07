"""
collectors/economics_collector.py
==================================
Collects economic metrics for the Solana network:
  1. Median Transaction Fee: Fetches prioritization fees from Solana RPC,
     calculates the median, adds the 5000 lamports base fee, and converts to SOL.
  2. REV (Real Economic Value): Fetches daily transaction fee revenue from DeFiLlama.
  3. Tokenized RWA Assets (TVL): Sums Solana-allocated TVL for RWA category protocols on DeFiLlama.

Pure Python stdlib only. Graceful error handling (5s timeout, 2 retries).
"""

import json
import time
import urllib.error
import urllib.request
import sys
from typing import Any

# ─── Configuration ────────────────────────────────────────────────────────────

RPC_URL = "https://api.mainnet-beta.solana.com"
DEFILLAMA_FEES_URL = "https://api.llama.fi/summary/fees/solana"
DEFILLAMA_PROTOCOLS_URL = "https://api.llama.fi/protocols"

TIMEOUT_SEC = 5
MAX_RETRIES = 2
RETRY_DELAY = 1.0

# ─── HTTP Helper with Retry logic ─────────────────────────────────────────────

def _http_post(url: str, payload: dict) -> Any:
    """Perform a POST request with retry and timeout."""
    headers = {"Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"POST {url} failed: {last_exc}")

def _http_get(url: str, timeout: int = TIMEOUT_SEC) -> Any:
    """Perform a GET request with retry and timeout."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "solana-ecosystem-report/0.1 (python-stdlib)"}
    )
    
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"GET {url} failed: {last_exc}")

# ─── Collectors ───────────────────────────────────────────────────────────────

def get_median_tx_fee(rpc_endpoint: str = RPC_URL) -> float | None:
    """
    Fetch priority fees for the last 150 slots, calculate the median,
    add the 5,000 lamport base signature fee, and return total fee in SOL.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getRecentPrioritizationFees",
        "params": [[]]
    }
    
    try:
        res = _http_post(rpc_endpoint, payload)
        result = res.get("result")
        if not isinstance(result, list) or not result:
            return 0.000005  # Fallback to base fee if empty
            
        fees = [item.get("prioritizationFee", 0) for item in result]
        fees.sort()
        n = len(fees)
        
        if n % 2 == 1:
            median_micro = fees[n // 2]
        else:
            median_micro = (fees[n // 2 - 1] + fees[n // 2]) / 2.0
            
        # Conversion: micro-lamports per Compute Unit (CU).
        # We assume a standard compute limit of 200,000 CUs.
        # Priority Fee (lamports) = (micro-lamports * 200,000) / 1,000,000 = micro-lamports * 0.2
        priority_fee_lamports = median_micro * 0.2
        base_fee_lamports = 5000.0
        total_fee_lamports = base_fee_lamports + priority_fee_lamports
        
        return total_fee_lamports / 1e9
        
    except Exception as exc:
        print(f"WARNING: economics/get_median_tx_fee failed - {exc!r}", file=sys.stderr)
        return None

def get_rev() -> float | None:
    """Fetch daily user fees on Solana from DeFiLlama summary API."""
    try:
        res = _http_get(DEFILLAMA_FEES_URL)
        total24h = res.get("total24h")
        if total24h is not None:
            return float(total24h)
        return None
    except Exception as exc:
        print(f"WARNING: economics/get_rev failed - {exc!r}", file=sys.stderr)
        return None

def get_rwa_volume() -> float | None:
    """
    Calculate Solana RWA TVL by querying DeFiLlama protocols, filtering for RWA
    category protocols, and summing up their Solana chain TVL allocations.
    """
    try:
        protocols = _http_get(DEFILLAMA_PROTOCOLS_URL, timeout=20)
        if not isinstance(protocols, list):
            return None
            
        total_rwa_tvl = 0.0
        rwa_count = 0
        
        for p in protocols:
            if not isinstance(p, dict):
                continue
            if p.get("category") == "RWA":
                chain_tvls = p.get("chainTvls") or {}
                # Match "Solana" (case-insensitive keys)
                sol_tvl = 0.0
                for chain_name, tvl_val in chain_tvls.items():
                    if chain_name.lower() == "solana":
                        sol_tvl = float(tvl_val) if tvl_val is not None else 0.0
                        break
                if sol_tvl > 0:
                    total_rwa_tvl += sol_tvl
                    rwa_count += 1
                    
        return total_rwa_tvl
    except Exception as exc:
        print(f"WARNING: economics/get_rwa_volume failed - {exc!r}", file=sys.stderr)
        return None

# ─── Combined Collector ───────────────────────────────────────────────────────

def collect_all(rpc_endpoint: str = RPC_URL) -> dict:
    """Collect all economic metrics and package into a standardized metadata envelope."""
    collected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    median_fee = get_median_tx_fee(rpc_endpoint)
    rev = get_rev()
    rwa_vol = get_rwa_volume()
    
    return {
        "meta": {
            "collected_at": collected_at,
            "source": "economics-collector",
            "rpc_endpoint": rpc_endpoint
        },
        "economics_extra": {
            "medianFeeSol": median_fee,
            "revUsd24h": rev,
            "rwaVolumeUsd": rwa_vol
        }
    }

# ─── Standalone Runner ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Collecting Solana economic stats...", file=sys.stderr)
    t0 = time.perf_counter()
    res = collect_all()
    elapsed = time.perf_counter() - t0
    
    print(json.dumps(res, indent=2))
    print(f"\n# collect_all() completed in {elapsed:.2f}s", file=sys.stderr)
