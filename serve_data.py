"""
serve_data.py
=============
Three-tier refresh loop that writes data.json on every fast cycle.

Architecture
------------
                        ┌─────────────────────────────────────────┐
  Main thread           │  collect_fast()   every FAST_INTERVAL_SEC│
  (fast loop)    ──────►│  merge market / supply / defi caches     │──► data.json
                        │  atomic write (tmp → rename)             │
                        └─────────────────────────────────────────┘

  Background thread     ┌─────────────────────────────────────────┐
  (market loop)  ──────►│  collect_coingecko() every CG_INTERVAL  │──► _market_cache
                        └─────────────────────────────────────────┘
  CG_INTERVAL_SEC = 30  (CoinGecko public API: ~10-30 req/min limit;
                         polling faster causes HTTP 429 and null prices)

  Background thread     ┌─────────────────────────────────────────┐
  (supply loop)  ──────►│  collect_supply()  every SLOW_INTERVAL  │──► _supply_cache
                        └─────────────────────────────────────────┘

  Background thread     ┌─────────────────────────────────────────┐
  (defi loop)    ──────►│  collect_defillama() every SLOW_INTERVAL │──► _defi_cache
                        └─────────────────────────────────────────┘

All three background threads run as daemons and pre-populate their caches
before the fast loop starts.  On any failure the previous cached value is
retained — so the dashboard never sees a previously-good field go null just
because a single upstream call failed.

Usage
-----
    python3 serve_data.py                     # defaults: fast=5s, slow=60s, cg=30s
    python3 serve_data.py --fast 5 --slow 30  # custom cadences
    python3 serve_data.py --once              # single snapshot then exit

Output
------
    data.json   — written atomically on every fast cycle
    stderr      — timestamped log lines with tier and timing

Log format
----------
    [HH:MM:SS UTC] [FAST ] cycle=1.97s  sleep=3.03s  ✓ data.json
    [HH:MM:SS UTC] [MKTD ] collect_coingecko() → 0.41s  ✓ market cached ($173.21)
    [HH:MM:SS UTC] [MKTD ] collect_coingecko() → 0.12s  ✗ ERROR — retaining previous cache
    [HH:MM:SS UTC] [SLOW ] collect_supply()  → 7.82s  ✓ supply cached
    [HH:MM:SS UTC] [SLOW ] collect_supply()  → ERROR  ✗ retaining previous cache
"""

import argparse
import json
import pathlib
import sys
import threading
import time

import concurrent.futures

from analysis.anomaly_detector import detect_anomalies
from collectors.coingecko_collector import get_sol_market_data
from collectors.defillama_collector import collect_all as collect_defillama
from collectors.economics_collector import get_median_tx_fee, get_rev, get_rwa_volume
from collectors.jupiter_collector import collect_all as collect_jupiter
from collectors.news_collector import collect_all as collect_news
from collectors.rpc_collector import collect_fast, collect_supply
from storage.history import append_snapshot, get_recent
from report.json_generator import generate_json_report
from report.markdown_generator import generate_markdown_report

# ─── Configuration ────────────────────────────────────────────────────────────

FAST_INTERVAL_SEC: int = 5     # collect_fast()   cadence in seconds
                               # collect_fast() measured at ~1.3–2 s, safely under this limit.
CG_INTERVAL_SEC:   int = 30   # collect_coingecko() cadence — CoinGecko public API allows
                               # ~10-30 req/min; polling faster triggers HTTP 429 errors.
SLOW_INTERVAL_SEC: int = 60    # collect_supply() & collect_defillama() cadence in seconds
RWA_INTERVAL_SEC:  int = 300   # get_rwa_volume() cadence in seconds (5 minutes)
OUTPUT_PATH = pathlib.Path("data.json")

# ─── Slow-tier caches (shared between main thread and background daemon threads) ─

_market_cache: dict | None = None
_market_lock = threading.Lock()
_market_updated_at: float = 0.0   # monotonic timestamp of last successful update

_supply_cache: dict | None = None
_supply_lock = threading.Lock()
_supply_updated_at: float = 0.0   # monotonic timestamp of last successful update

_defi_cache: dict | None = None
_defi_lock = threading.Lock()
_defi_updated_at: float = 0.0     # monotonic timestamp of last successful update

_news_cache: list | None = None
_news_lock = threading.Lock()
_news_updated_at: float = 0.0     # monotonic timestamp of last successful update

_economics_cache: dict | None = None
_economics_lock = threading.Lock()
_economics_updated_at: float = 0.0 # monotonic timestamp of last successful update

_rwa_cache: float | None = None
_rwa_lock = threading.Lock()
_rwa_updated_at: float = 0.0       # monotonic timestamp of last successful update

# ─── Logging ─────────────────────────────────────────────────────────────────


def _ts() -> str:
    """Current time as HH:MM:SS UTC, e.g. '22:14:37 UTC'."""
    return time.strftime("%H:%M:%S UTC", time.gmtime())


def _log(tier: str, msg: str) -> None:
    """Print a timestamped log line to stderr."""
    print(f"[{_ts()}] [{tier:<5}] {msg}", file=sys.stderr, flush=True)


# ─── Slow background threads ──────────────────────────────────────────────────


def _market_loop(interval: int) -> None:
    """
    Background daemon thread: refresh market data every ``interval`` seconds (default: 30 s).

    Sources:
      - Jupiter (/price/v3) → priceUsd, change24hPct (on-chain DEX liquidity, fast & keyless)
      - CoinGecko (/coins/solana) → marketCapUsd, volume24hUsd, circulatingSupply (CEX metrics)

    Independent fail-safety:
      - If Jupiter fails, retained cached priceUsd and change24hPct.
      - If CoinGecko fails, retained cached marketCapUsd and volume24hUsd.
    """
    global _market_cache, _market_updated_at

    while True:
        t0 = time.perf_counter()
        jup_data = None
        cg_data = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_jup = pool.submit(collect_jupiter)
            f_cg = pool.submit(get_sol_market_data)

            try:
                jup_res = f_jup.result(timeout=10)
                jup_data = jup_res.get("market") if jup_res else None
            except Exception as exc:  # noqa: BLE001
                _log("MKTD", f"collect_jupiter() → EXCEPTION {exc!r}")

            try:
                cg_data = f_cg.result(timeout=10)
            except Exception as exc:  # noqa: BLE001
                _log("MKTD", f"get_sol_market_data() → EXCEPTION {exc!r}")

        elapsed = time.perf_counter() - t0

        jup_ok = jup_data is not None and jup_data.get("priceUsd") is not None
        cg_ok = cg_data is not None and cg_data.get("market_cap_usd") is not None

        with _market_lock:
            if _market_cache is None:
                _market_cache = {
                    "priceUsd": None,
                    "change24hPct": None,
                    "marketCapUsd": None,
                    "volume24hUsd": None,
                    "circulatingSupply": None,
                }

            if jup_ok:
                _market_cache["priceUsd"] = jup_data.get("priceUsd")
                _market_cache["change24hPct"] = jup_data.get("change24hPct")
                if jup_data.get("_blockId") is not None:
                    _market_cache["_blockId"] = jup_data.get("_blockId")
                if jup_data.get("_liquidityUsd") is not None:
                    _market_cache["_liquidityUsd"] = jup_data.get("_liquidityUsd")

            if cg_ok:
                _market_cache["marketCapUsd"] = cg_data.get("market_cap_usd")
                _market_cache["volume24hUsd"] = cg_data.get("volume_24h_usd")
                _market_cache["circulatingSupply"] = cg_data.get("circulating_supply")

            if jup_ok or cg_ok:
                _market_updated_at = time.monotonic()

        price_val = _market_cache.get("priceUsd") if _market_cache else None
        mcap_val = _market_cache.get("marketCapUsd") if _market_cache else None
        price_str = f"${price_val:.2f}" if price_val is not None else "?"
        mcap_str = f"${mcap_val/1e9:.2f}B" if mcap_val is not None else "?"

        status_jup = "✓ Jup" if jup_ok else "✗ Jup (cached)"
        status_cg = "✓ CG" if cg_ok else "✗ CG (cached)"

        _log("MKTD", f"market fetch → {elapsed:.2f}s  [{status_jup}, {status_cg}]  price={price_str} mcap={mcap_str}")

        time.sleep(interval)


def _supply_loop(interval: int) -> None:
    """
    Background daemon thread: refresh the supply cache every ``interval``
    seconds.  Runs its first fetch immediately on start so the fast loop
    has a supply value available from the very first write.

    On failure, the previous cached value is retained and a warning is logged.
    """
    global _supply_cache, _supply_updated_at

    while True:
        t0 = time.perf_counter()
        result = collect_supply()
        elapsed = time.perf_counter() - t0

        if result is not None:
            with _supply_lock:
                _supply_cache = result
                _supply_updated_at = time.monotonic()
            _log("SLOW", f"collect_supply() → {elapsed:.2f}s  ✓ supply cached"
                         f" (total={result.get('totalSOL', '?'):.0f} SOL)")
        else:
            age = time.monotonic() - _supply_updated_at if _supply_updated_at else None
            age_str = f", cache age {age:.0f}s" if age else ""
            _log("SLOW", f"collect_supply() → {elapsed:.2f}s  ✗ ERROR"
                         f" — retaining previous cache{age_str}")

        time.sleep(interval)


def _defi_loop(interval: int) -> None:
    """
    Background daemon thread: refresh the DeFiLlama cache every ``interval``
    seconds. Runs its first fetch immediately on start.

    On failure, the previous cached value is retained and a warning is logged.
    """
    global _defi_cache, _defi_updated_at

    while True:
        t0 = time.perf_counter()
        result = collect_defillama()
        elapsed = time.perf_counter() - t0

        defi_data = result.get("defi") if result else None

        if defi_data is not None:
            with _defi_lock:
                _defi_cache = defi_data
                _defi_updated_at = time.monotonic()
            tvl_val = defi_data.get("tvlUsd") or 0.0
            _log("SLOW", f"collect_defillama() → {elapsed:.2f}s  ✓ defi cached"
                         f" (tvl=${tvl_val/1e9:.2f}B)")
        else:
            age = time.monotonic() - _defi_updated_at if _defi_updated_at else None
            age_str = f", cache age {age:.0f}s" if age else ""
            _log("SLOW", f"collect_defillama() → {elapsed:.2f}s  ✗ ERROR"
                         f" — retaining previous cache{age_str}")

        time.sleep(interval)


def _news_loop(interval: int) -> None:
    """
    Background daemon thread: refresh the Solana news cache every ``interval``
    seconds (default: 60 s, same cadence as supply/defi).

    Fetches from the Solana Blog and Cointelegraph RSS feeds via
    ``collect_news()``.  On failure the previous cached list is retained and a
    warning is logged — the dashboard never reverts to an empty news section
    just because a single RSS poll fails.
    """
    global _news_cache, _news_updated_at

    while True:
        t0 = time.perf_counter()
        try:
            result = collect_news()
        except Exception as exc:  # noqa: BLE001
            result = None
            _log("NEWS", f"collect_news() → EXCEPTION {exc!r}")
        elapsed = time.perf_counter() - t0

        news_items = result.get("news") if result else None

        if news_items is not None:   # empty list [] is valid (no stories yet)
            with _news_lock:
                _news_cache = news_items
                _news_updated_at = time.monotonic()
            _log("NEWS", f"collect_news() → {elapsed:.2f}s  ✓ news cached"
                         f" ({len(news_items)} items)")
        else:
            age = time.monotonic() - _news_updated_at if _news_updated_at else None
            age_str = f", cache age {age:.0f}s" if age else ""
            _log("NEWS", f"collect_news() → {elapsed:.2f}s  ✗ ERROR"
                         f" — retaining previous cache{age_str}")

        time.sleep(interval)


def _economics_loop(interval: int) -> None:
    """
    Background daemon thread: refresh fast Solana economics metrics (median fee, REV)
    every ``interval`` seconds (default: 60 s).
    """
    global _economics_cache, _economics_updated_at

    while True:
        t0 = time.perf_counter()
        try:
            median_fee = get_median_tx_fee()
            rev = get_rev()
            result = {
                "medianFeeSol": median_fee,
                "revUsd24h": rev
            }
        except Exception as exc:  # noqa: BLE001
            result = None
            _log("ECON", f"economics fetch → EXCEPTION {exc!r}")
        elapsed = time.perf_counter() - t0

        if result is not None and result.get("medianFeeSol") is not None:
            with _economics_lock:
                _economics_cache = result
                _economics_updated_at = time.monotonic()
            _log("ECON", f"economics fetch → {elapsed:.2f}s  ✓ cached (fee={result.get('medianFeeSol'):.6f} SOL)")
        else:
            age = time.monotonic() - _economics_updated_at if _economics_updated_at else None
            age_str = f", cache age {age:.0f}s" if age else ""
            _log("ECON", f"economics fetch → {elapsed:.2f}s  ✗ ERROR — retaining previous cache{age_str}")

        time.sleep(interval)


def _rwa_loop(interval: int) -> None:
    """
    Background daemon thread: refresh Solana RWA assets TVL every ``interval``
    seconds (default: 300 s / 5 minutes).
    """
    global _rwa_cache, _rwa_updated_at

    while True:
        t0 = time.perf_counter()
        try:
            rwa_vol = get_rwa_volume()
        except Exception as exc:  # noqa: BLE001
            rwa_vol = None
            _log("RWA", f"rwa fetch → EXCEPTION {exc!r}")
        elapsed = time.perf_counter() - t0

        if rwa_vol is not None:
            with _rwa_lock:
                _rwa_cache = rwa_vol
                _rwa_updated_at = time.monotonic()
            _log("RWA", f"rwa fetch → {elapsed:.2f}s  ✓ cached (tvl=${rwa_vol/1e9:.2f}B)")
        else:
            age = time.monotonic() - _rwa_updated_at if _rwa_updated_at else None
            age_str = f", cache age {age:.0f}s" if age else ""
            _log("RWA", f"rwa fetch → {elapsed:.2f}s  ✗ ERROR — retaining previous cache{age_str}")

        time.sleep(interval)


# ─── Atomic JSON write ────────────────────────────────────────────────────────


def _write_json(path: pathlib.Path, data: dict) -> None:
    """
    Write ``data`` to ``path`` atomically using a tmp-file + rename pattern.

    On POSIX systems ``os.rename`` is atomic, so readers never see a
    half-written file.  On Windows, ``Path.replace()`` handles this.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ─── Fast loop (main thread) ─────────────────────────────────────────────────


def _fast_loop(fast_interval: int, output: pathlib.Path) -> None:
    """
    Main refresh loop: call ``collect_fast()`` every ``fast_interval`` seconds,
    merge the latest cached market / supply / defi values, and write atomically
    to ``output``.

    Scheduling uses wall-clock elapsed time::

        sleep = max(0, fast_interval - elapsed_this_cycle)

    This means a slow cycle (e.g. 5–6 s RPC variance) shortens the *next*
    sleep rather than adding a full ``fast_interval`` on top, preventing the
    loop from drifting progressively late over a long-running session.

    CoinGecko market data is **not** fetched here — it is maintained by the
    ``_market_loop`` daemon thread (30 s cadence) and read from ``_market_cache``
    on each cycle.  This avoids HTTP 429 rate-limit errors that occurred when
    the CoinGecko API was called in-band on every 5 s fast-loop iteration.
    """
    cycle = 0

    while True:
        cycle += 1
        t0 = time.monotonic()   # wall-clock reference for this cycle

        try:
            snapshot = collect_fast()
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - t0
            _log("FAST", f"collect_fast() → EXCEPTION {exc!r} — skipping write")
            time.sleep(max(0.0, fast_interval - elapsed))
            continue

        # elapsed so far = collect_fast() duration; updated again after write below

        # ── Merge cached CoinGecko market data ───────────────────────────
        # Updated by _market_loop every CG_INTERVAL_SEC (30 s).  On failure
        # _market_cache retains the last good value — price never goes null.
        with _market_lock:
            cached_market = _market_cache
            market_age = (
                round(time.monotonic() - _market_updated_at)
                if _market_updated_at else None
            )

        snapshot["market"] = (
            {**cached_market, "_cache_age_sec": market_age}
            if cached_market is not None else None
        )

        # ── Merge cached supply ───────────────────────────────────────────
        with _supply_lock:
            cached_supply = _supply_cache
            supply_age = (
                round(time.monotonic() - _supply_updated_at)
                if _supply_updated_at else None
            )

        if cached_supply is not None:
            snapshot["supply"] = {
                **cached_supply,
                "_cache_age_sec": supply_age,   # surface staleness to the dashboard
            }
        # else supply remains None from collect_fast(); dashboard shows "—"

        # Backfill validator stake percentages using total supply
        total_supply = snapshot.get("supply", {}).get("totalSOL") if snapshot.get("supply") else None
        if total_supply and snapshot.get("validators", {}).get("topValidators"):
            for val_item in snapshot["validators"]["topValidators"]:
                stake_sol = val_item.get("activated_stake_sol")
                if stake_sol and total_supply:
                    val_item["stake_pct"] = round((stake_sol / total_supply) * 100, 2)

        # ── Merge cached DeFiLlama data ──────────────────────────────────
        with _defi_lock:
            cached_defi = _defi_cache
            defi_age = (
                round(time.monotonic() - _defi_updated_at)
                if _defi_updated_at else None
            )

        if cached_defi is not None:
            snapshot["defi"] = {
                **cached_defi,
                "_cache_age_sec": defi_age,
            }
        else:
            snapshot["defi"] = None

        # ── Merge cached news ─────────────────────────────────────────────
        with _news_lock:
            cached_news = _news_cache
            news_age = (
                round(time.monotonic() - _news_updated_at)
                if _news_updated_at else None
            )

        # Always write "news" key; empty list [] until first successful fetch.
        snapshot["news"] = cached_news if cached_news is not None else []

        # ── Merge cached economics ────────────────────────────────────────
        with _economics_lock:
            cached_econ = _economics_cache
            econ_age = (
                round(time.monotonic() - _economics_updated_at)
                if _economics_updated_at else None
            )

        with _rwa_lock:
            cached_rwa = _rwa_cache
            rwa_age = (
                round(time.monotonic() - _rwa_updated_at)
                if _rwa_updated_at else None
            )

        if cached_econ is not None:
            snapshot["economics_extra"] = {
                "medianFeeSol": cached_econ.get("medianFeeSol"),
                "revUsd24h": cached_econ.get("revUsd24h"),
                "rwaVolumeUsd": cached_rwa if cached_rwa is not None else None,
                "_cache_age_sec": econ_age,
                "_rwa_cache_age_sec": rwa_age,
            }
        else:
            snapshot["economics_extra"] = None

        # ── History & Anomaly Detection ──────────────────────────────────
        try:
            append_snapshot(snapshot)
            recent_history = get_recent(n=20)
            # Compare current snapshot against baseline history prior to this snapshot
            baseline_history = recent_history[:-1] if len(recent_history) > 1 else []
            alerts = detect_anomalies(current=snapshot, history=baseline_history, verbose=False)
        except Exception as exc:  # noqa: BLE001
            _log("FAST", f"WARNING: history/anomaly check failed ({exc!r})")
            alerts = []
            recent_history = []

        snapshot["alerts"] = alerts
        snapshot["anomalies"] = alerts

        # ── Annotate snapshot with refresh metadata ───────────────────────
        snapshot.setdefault("meta", {}).update({
            "cycle":                 cycle,
            "fast_interval_sec":     fast_interval,
            "cg_interval_sec":       CG_INTERVAL_SEC,
            "slow_interval_sec":     SLOW_INTERVAL_SEC,
            "market_cache_age_sec":  market_age,
            "supply_cache_age_sec":  supply_age,
            "defi_cache_age_sec":    defi_age,
            "news_cache_age_sec":    news_age,
            "econ_cache_age_sec":    econ_age,
            "rwa_cache_age_sec":     rwa_age,
            "history_count":         len(recent_history),
            "alerts_count":          len(alerts),
        })

        # ── Write atomically ──────────────────────────────────────────────
        try:
            _write_json(output, snapshot)
        except OSError as exc:
            _log("FAST", f"collect_fast() → ✗ write failed: {exc}")

        try:
            generate_json_report(snapshot, "reports/latest.json")
            generate_markdown_report(snapshot, "reports/latest.md")
        except Exception as exc:  # noqa: BLE001
            _log("FAST", f"WARNING: report generation failed ({exc!r})")

        # ── Wall-clock scheduling: sleep only the remaining interval ──────
        # Measure elapsed *after* the write so the full cycle time is counted.
        elapsed = time.monotonic() - t0
        sleep_sec = max(0.0, fast_interval - elapsed)

        tags = [
            f"market cached {market_age}s ago" if market_age is not None else "market=null",
            f"supply cached {supply_age}s ago" if supply_age is not None else "supply=null",
            f"econ cached {econ_age}s ago" if econ_age is not None else "econ=null",
            f"rwa cached {rwa_age}s ago" if rwa_age is not None else "rwa=null",
            f"history={len(recent_history)}" if 'recent_history' in locals() else "history=0",
            f"alerts={len(alerts)}" if 'alerts' in locals() else "alerts=0",
        ]
        _log("FAST", f"cycle={elapsed:.2f}s  sleep={sleep_sec:.2f}s  ✓ {output}  [{', '.join(tags)}]")

        time.sleep(sleep_sec)


# ─── Single-shot mode ────────────────────────────────────────────────────────


def _run_once(output: pathlib.Path) -> None:
    """Collect a single fast snapshot, merge available market, supply, defi & news, and write."""
    _log("ONCE", "Running single fast snapshot …")

    t0 = time.perf_counter()
    snapshot = collect_fast()

    jup_market = None
    try:
        jup_res = collect_jupiter()
        jup_market = jup_res.get("market") if jup_res else None
    except Exception as exc:  # noqa: BLE001
        _log("ONCE", f"WARNING: collect_jupiter() failed ({exc!r})")

    cg_market = None
    try:
        cg_market = get_sol_market_data()
    except Exception as exc:  # noqa: BLE001
        _log("ONCE", f"WARNING: get_sol_market_data() failed ({exc!r})")

    snapshot["market"] = {
        "priceUsd": jup_market.get("priceUsd") if jup_market else None,
        "change24hPct": jup_market.get("change24hPct") if jup_market else None,
        "marketCapUsd": cg_market.get("market_cap_usd") if cg_market else None,
        "volume24hUsd": cg_market.get("volume_24h_usd") if cg_market else None,
        "circulatingSupply": cg_market.get("circulating_supply") if cg_market else None,
    }

    try:
        defillama_res = collect_defillama()
        snapshot["defi"] = defillama_res.get("defi") if defillama_res else None
    except Exception as exc:  # noqa: BLE001
        _log("ONCE", f"WARNING: collect_defillama() failed ({exc!r})")
        snapshot["defi"] = None

    try:
        supply_res = collect_supply()
        snapshot["supply"] = supply_res
    except Exception as exc:  # noqa: BLE001
        _log("ONCE", f"WARNING: collect_supply() failed ({exc!r})")
        snapshot["supply"] = None

    try:
        news_res = collect_news()
        snapshot["news"] = news_res.get("news") if news_res else []
    except Exception as exc:  # noqa: BLE001
        _log("ONCE", f"WARNING: collect_news() failed ({exc!r})")
        snapshot["news"] = []

    # Backfill validator stake percentages using total supply
    total_supply = snapshot.get("supply", {}).get("totalSOL") if snapshot.get("supply") else None
    if total_supply and snapshot.get("validators", {}).get("topValidators"):
        for val_item in snapshot["validators"]["topValidators"]:
            stake_sol = val_item.get("activated_stake_sol")
            if stake_sol and total_supply:
                val_item["stake_pct"] = round((stake_sol / total_supply) * 100, 2)

    try:
        median_fee = get_median_tx_fee()
        rev = get_rev()
        rwa_vol = get_rwa_volume()
        snapshot["economics_extra"] = {
            "medianFeeSol": median_fee,
            "revUsd24h": rev,
            "rwaVolumeUsd": rwa_vol
        }
    except Exception as exc:  # noqa: BLE001
        _log("ONCE", f"WARNING: collect economics failed ({exc!r})")
        snapshot["economics_extra"] = None

    elapsed = time.perf_counter() - t0
    _log("ONCE", f"collect_fast() + market + defi → {elapsed:.2f}s")

    _write_json(output, snapshot)
    _log("ONCE", f"✓ Written to {output}")

    try:
        generate_json_report(snapshot, "reports/latest.json")
        generate_markdown_report(snapshot, "reports/latest.md")
        _log("ONCE", "✓ Reports generated inside reports/latest.json and reports/latest.md")
    except Exception as exc:  # noqa: BLE001
        _log("ONCE", f"WARNING: report generation failed ({exc!r})")


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solana ecosystem report — two-tier data refresh loop",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--fast", type=int, default=FAST_INTERVAL_SEC, metavar="SEC",
        help="Cadence for collect_fast() in seconds",
    )
    parser.add_argument(
        "--slow", type=int, default=SLOW_INTERVAL_SEC, metavar="SEC",
        help="Cadence for collect_supply() in seconds",
    )
    parser.add_argument(
        "--output", type=pathlib.Path, default=OUTPUT_PATH, metavar="PATH",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single fast snapshot and exit (no supply fetch)",
    )
    args = parser.parse_args()

    if args.once:
        _run_once(args.output)
        return

    print(
        f"\n{'─'*55}\n"
        f"  Solana Ecosystem Report — Data Refresh Loop\n"
        f"{'─'*55}\n"
        f"  Fast tier : collect_fast()      every {args.fast}s\n"
        f"  Market    : Jupiter (price) &   every {CG_INTERVAL_SEC}s\n"
        f"              CoinGecko (mcap)\n"
        f"  Slow tier : collect_supply(),   every {args.slow}s\n"
        f"              collect_defillama(),\n"
        f"              collect_news(),\n"
        f"              collect_economics() (fast metrics)\n"
        f"  RWA tier  : get_rwa_volume()    every {RWA_INTERVAL_SEC}s\n"
        f"  Output    : {args.output.resolve()}\n"
        f"{'─'*55}\n",
        file=sys.stderr,
    )

    # ── Start background daemon threads ───────────────────────────────────
    # Daemon=True so threads don't block process exit on Ctrl-C.
    # Each thread runs its first fetch immediately on start, so all caches
    # are pre-populated before the fast loop begins writing data.json.

    market_thread = threading.Thread(
        target=_market_loop,
        args=(CG_INTERVAL_SEC,),
        daemon=True,
        name="market-refresh",
    )
    market_thread.start()
    _log("INIT", f"Market thread (Jupiter + CoinGecko) started (every {CG_INTERVAL_SEC}s)")

    supply_thread = threading.Thread(
        target=_supply_loop,
        args=(args.slow,),
        daemon=True,
        name="supply-refresh",
    )
    supply_thread.start()
    _log("INIT", f"Supply thread started (every {args.slow}s)")

    defi_thread = threading.Thread(
        target=_defi_loop,
        args=(args.slow,),
        daemon=True,
        name="defi-refresh",
    )
    defi_thread.start()
    _log("INIT", f"DeFiLlama thread started (every {args.slow}s)")

    news_thread = threading.Thread(
        target=_news_loop,
        args=(args.slow,),
        daemon=True,
        name="news-refresh",
    )
    news_thread.start()
    _log("INIT", f"News thread started (every {args.slow}s)")

    econ_thread = threading.Thread(
        target=_economics_loop,
        args=(args.slow,),
        daemon=True,
        name="econ-refresh",
    )
    econ_thread.start()
    _log("INIT", f"Economics thread started (every {args.slow}s)")

    rwa_thread = threading.Thread(
        target=_rwa_loop,
        args=(RWA_INTERVAL_SEC,),
        daemon=True,
        name="rwa-refresh",
    )
    rwa_thread.start()
    _log("INIT", f"RWA thread started (every {RWA_INTERVAL_SEC}s)")

    from collectors.rpc_collector import SUPPLY_TIMEOUT_SEC
    _log("INIT", f"Waiting up to {SUPPLY_TIMEOUT_SEC // 2}s for initial slow-tier fetches …")
    market_thread.join(timeout=5.0)   # CoinGecko is fast; wait up to 5 s
    supply_thread.join(timeout=SUPPLY_TIMEOUT_SEC // 2)
    defi_thread.join(timeout=1.0)
    news_thread.join(timeout=8.0)     # RSS feeds typically respond in < 5 s
    econ_thread.join(timeout=2.0)     # Fast economics finishes quickly
    rwa_thread.join(timeout=1.0)      # Heavy scan completed in background

    _log("INIT", "History DB: storage/history.db (SQLite retention=500)")
    _log("INIT", "Anomaly Detector: initialized (baseline requires >= 5 historical snapshots)")

    # ── Start fast loop (blocks forever) ─────────────────────────────────
    _log("INIT", f"Fast loop starting (every {args.fast}s) — Ctrl-C to stop\n")
    try:
        _fast_loop(args.fast, args.output)
    except KeyboardInterrupt:
        print("\n[serve_data] Stopped by user.", file=sys.stderr)


if __name__ == "__main__":
    main()
