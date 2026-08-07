"""
collectors/rpc_collector.py
===========================
Collects on-chain network data directly from a public Solana JSON-RPC endpoint.
No API keys, no external dependencies — pure stdlib (urllib, json, time).

RPC reference: https://docs.solana.com/api/http
Public endpoint: https://api.mainnet-beta.solana.com

Usage (standalone sanity-check):
    python3 -m collectors.rpc_collector
    # or
    python3 collectors/rpc_collector.py
"""

import concurrent.futures
import json
import time
import urllib.error
import urllib.request
from typing import Any

# ─── Configuration ────────────────────────────────────────────────────────────

RPC_ENDPOINT      = "https://api.mainnet-beta.solana.com"
TIMEOUT_SEC       = 5   # default per-request socket timeout
SUPPLY_TIMEOUT_SEC = 8  # getSupply is a heavier call; give it extra headroom
MAX_RETRIES       = 2   # total extra attempts after the first failure
RETRY_DELAY       = 1.5 # base retry delay (seconds); doubles each attempt
LAMPORTS_PER_SOL  = 1_000_000_000

# ─── Low-level RPC helper ────────────────────────────────────────────────────


def _rpc_call(method: str, params: list | None = None,
              timeout: int | None = None) -> Any:
    """
    Send a JSON-RPC 2.0 POST request to the Solana RPC endpoint.

    Returns the ``result`` field from the response on success, or raises
    a ``RuntimeError`` describing the failure (after MAX_RETRIES attempts).

    Retries are attempted for transient network errors (timeouts, connection
    resets, HTTP 429/503). JSON-RPC application-level errors (``"error"`` in
    the response body) are not retried — they are raised immediately.

    Args:
        method:  Solana RPC method name (e.g. ``"getSlot"``).
        params:  Optional list of positional parameters for the method.
        timeout: Socket timeout in seconds.  Defaults to ``TIMEOUT_SEC``.

    Returns:
        The parsed ``result`` value from the RPC response.

    Raises:
        RuntimeError: If all retry attempts fail or the server returns an
                      application-level error.
    """
    timeout = timeout if timeout is not None else TIMEOUT_SEC
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params if params is not None else [],
    }).encode("utf-8")

    req = urllib.request.Request(
        RPC_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "solana-ecosystem-report/0.1 (python-stdlib)",
        },
        method="POST",
    )

    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            delay = RETRY_DELAY * (2 ** (attempt - 1))
            time.sleep(delay)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            if "error" in body:
                err = body["error"]
                raise RuntimeError(
                    f"RPC error [{err.get('code')}]: {err.get('message', err)}"
                )

            return body["result"]

        except RuntimeError:
            # Application-level errors — don't retry
            raise

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            # Retry on transient network errors

        except (json.JSONDecodeError, KeyError) as exc:
            last_exc = exc
            # Malformed response — retry in case it was a truncated packet

    raise RuntimeError(
        f"RPC call '{method}' failed after {MAX_RETRIES + 1} attempts: {last_exc}"
    )


def _safe_call(method: str, params: list | None = None,
               default: Any = None,
               timeout: int | None = None) -> Any:
    """
    Wrapper around ``_rpc_call`` that swallows all exceptions and returns
    ``default`` (``None`` by default) instead of propagating.

    Use this at the collector level so a single failing RPC method never
    crashes the whole collection run.

    Args:
        method:  Solana RPC method name.
        params:  Optional RPC parameters.
        default: Value to return on any failure (default: ``None``).
        timeout: Override the per-call socket timeout (seconds).
    """
    try:
        return _rpc_call(method, params, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)} if default is None else default


# ─── Per-method collector functions ─────────────────────────────────────────


def get_health() -> str:
    """
    Check whether the RPC node considers itself healthy.

    Calls ``getHealth`` — the node returns the string ``"ok"`` when healthy,
    or an error object when it is behind by too many slots.

    Returns:
        ``"ok"`` if the node is healthy, otherwise a string describing
        the problem (or ``"error"`` if the request itself failed).

    RPC docs: https://docs.solana.com/api/http#gethealth
    """
    result = _safe_call("getHealth")
    if result is None or isinstance(result, dict) and "error" in result:
        return "error"
    return str(result)


def get_slot() -> int | None:
    """
    Return the current highest confirmed slot number.

    Calls ``getSlot`` with default commitment (``"finalized"``).  This is
    used as a proxy for block height in the dashboard.

    Returns:
        Current slot as an integer, or ``None`` on failure.

    RPC docs: https://docs.solana.com/api/http#getslot
    """
    result = _safe_call("getSlot")
    if isinstance(result, dict) and "error" in result:
        return None
    return int(result) if result is not None else None


def get_block_time(slot: int) -> int | None:
    """
    Return the estimated production time of a given slot as a Unix timestamp.

    Calls ``getBlockTime`` for the specified slot number.  Useful for
    calculating actual slot durations from historical performance data.

    Args:
        slot: Slot number to query.

    Returns:
        Unix timestamp (seconds since epoch) as an integer, or ``None``
        if the slot has no block or the request failed.

    RPC docs: https://docs.solana.com/api/http#getblocktime
    """
    result = _safe_call("getBlockTime", [slot])
    if isinstance(result, dict) and "error" in result:
        return None
    return int(result) if result is not None else None


def get_epoch_info() -> dict | None:
    """
    Return current epoch metadata including progress through the epoch.

    Calls ``getEpochInfo``.  In addition to the raw RPC fields the function
    computes ``progress_pct`` so callers don't have to repeat the arithmetic.

    Returns:
        Dict with keys:
            - ``epoch`` (int): Current epoch number.
            - ``slotIndex`` (int): Current slot's position within the epoch.
            - ``slotsInEpoch`` (int): Total slots in the current epoch.
            - ``absoluteSlot`` (int): Current absolute slot.
            - ``progress_pct`` (float): ``slotIndex / slotsInEpoch * 100``.
        Returns ``None`` on failure.

    RPC docs: https://docs.solana.com/api/http#getepochinfo
    """
    result = _safe_call("getEpochInfo")
    if not result or isinstance(result, dict) and "error" in result:
        return None

    slot_index    = result.get("slotIndex", 0)
    slots_in_epoch = result.get("slotsInEpoch", 1)  # guard div-by-zero

    return {
        "epoch":         result.get("epoch"),
        "slotIndex":     slot_index,
        "slotsInEpoch":  slots_in_epoch,
        "absoluteSlot":  result.get("absoluteSlot"),
        "progress_pct":  round(slot_index / slots_in_epoch * 100, 2),
    }


def get_recent_performance() -> dict | None:
    """
    Derive current TPS and average slot time from recent performance samples.

    Calls ``getRecentPerformanceSamples`` with a limit of 20 samples (each
    sample covers a ~60-second window).  The most recent sample gives the
    current TPS; the average slot time is computed across all samples.

    .. note::
        **TPS and slot time are Solana rolling-average values**, not
        instantaneous readings.  Each performance sample spans a ~60-second
        window on the validator side, so these figures reflect recent history
        rather than the current moment.  This is a Solana RPC characteristic
        (``getRecentPerformanceSamples``) — faster polling does not eliminate
        this latency; it only reduces the time until the next sample arrives.

    Returns:
        Dict with keys:
            - ``tps`` (float): Transactions per second from the latest sample.
            - ``avg_slot_time_ms`` (float): Mean slot duration in milliseconds
              across all returned samples.
            - ``sample_count`` (int): Number of samples used.
        Returns ``None`` on failure or empty result.

    RPC docs: https://docs.solana.com/api/http#getrecentperformancesamples
    """
    result = _safe_call("getRecentPerformanceSamples", [20])
    if not result or isinstance(result, dict) and "error" in result:
        return None

    samples = [
        s for s in result
        if s.get("samplePeriodSecs", 0) > 0 and s.get("numSlots", 0) > 0
    ]
    if not samples:
        return None

    # Most recent sample (index 0 = newest)
    latest          = samples[0]
    tps             = latest["numTransactions"] / latest["samplePeriodSecs"]

    # Avg slot time: for each sample, ms-per-slot = (samplePeriodSecs*1000) / numSlots
    slot_times_ms   = [
        (s["samplePeriodSecs"] * 1000) / s["numSlots"]
        for s in samples
    ]
    avg_slot_time_ms = sum(slot_times_ms) / len(slot_times_ms)

    return {
        "tps":              round(tps, 1),
        "avg_slot_time_ms": round(avg_slot_time_ms, 1),
        "sample_count":     len(samples),
    }


def get_vote_accounts() -> dict | None:
    """
    Summarise the validator set from the current vote accounts.

    Calls ``getVoteAccounts`` (no params, uses default commitment).  Stake
    values are converted from lamports to SOL.

    Returns:
        Dict with keys:
            - ``active_count`` (int): Number of validators in the active set.
            - ``delinquent_count`` (int): Number of delinquent validators.
            - ``avg_commission`` (float): Mean commission % across active set.
            - ``top_5_by_stake`` (list[dict]): Top 5 active validators sorted
              by ``activatedStake`` descending.  Each entry has:
                  - ``votePubkey`` (str)
                  - ``activated_stake_sol`` (float): Stake in SOL.
                  - ``commission`` (int): Commission percentage (0-100).
        Returns ``None`` on failure.

    RPC docs: https://docs.solana.com/api/http#getvoteaccounts
    """
    result = _safe_call("getVoteAccounts")
    if not result or isinstance(result, dict) and "error" in result:
        return None

    current    = result.get("current", [])
    delinquent = result.get("delinquent", [])

    if not current:
        return {
            "active_count":    0,
            "delinquent_count": len(delinquent),
            "avg_commission":  0.0,
            "top_5_by_stake":  [],
        }

    # Sort active validators by stake descending
    sorted_current = sorted(
        current,
        key=lambda v: v.get("activatedStake", 0),
        reverse=True,
    )

    top_5 = [
        {
            "votePubkey":          v["votePubkey"],
            "activated_stake_sol": round(v["activatedStake"] / LAMPORTS_PER_SOL, 2),
            "commission":          v.get("commission", 0),
        }
        for v in sorted_current[:5]
    ]

    commissions    = [v.get("commission", 0) for v in current]
    avg_commission = sum(commissions) / len(commissions) if commissions else 0.0

    return {
        "active_count":     len(current),
        "delinquent_count": len(delinquent),
        "avg_commission":   round(avg_commission, 2),
        "top_5_by_stake":   top_5,
    }


def get_supply() -> dict | None:
    """
    Return the current SOL supply breakdown.

    Calls ``getSupply`` with ``excludeNonCirculatingAccountsList=True`` to
    reduce response size (we don't need the full non-circulating address list).
    All values are converted from lamports to whole SOL.

    Returns:
        Dict with keys:
            - ``total`` (float): Total SOL supply.
            - ``circulating`` (float): Circulating SOL supply.
            - ``non_circulating`` (float): Non-circulating SOL supply.
        Returns ``None`` on failure.

    RPC docs: https://docs.solana.com/api/http#getsupply
    """
    result = _safe_call(
        "getSupply",
        [{"excludeNonCirculatingAccountsList": True}],
        timeout=SUPPLY_TIMEOUT_SEC,   # heavier call — use extended timeout
    )
    if not result or isinstance(result, dict) and "error" in result:
        return None

    value = result.get("value", {})

    def to_sol(lamports: int | None) -> float | None:
        return round(lamports / LAMPORTS_PER_SOL, 2) if lamports is not None else None

    return {
        "total":           to_sol(value.get("total")),
        "circulating":     to_sol(value.get("circulating")),
        "non_circulating": to_sol(value.get("nonCirculating")),
    }


# ─── Future resolver (shared by all collector tiers) ─────────────────────────


def _resolve_future(
    future: "concurrent.futures.Future[Any]",
    label: str,
    future_timeout: float,
) -> Any:
    """
    Resolve a ``concurrent.futures.Future``, logging a warning to stderr and
    returning ``None`` on timeout or any other exception.

    Using this instead of a bare ``.result()`` call means one flaky RPC
    method can never crash the caller — the failed field simply comes back
    as ``None`` and the rest of the snapshot is still written.
    """
    import sys as _sys

    try:
        return future.result(timeout=future_timeout)
    except concurrent.futures.TimeoutError:
        print(
            f"WARNING: {label} timed out after {future_timeout}s — "
            "field will be null in this snapshot.",
            file=_sys.stderr,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: {label} failed ({exc!r}) — "
            "field will be null in this snapshot.",
            file=_sys.stderr,
        )
        return None


def _build_snapshot(
    collected_at: str,
    health_raw: str | None,
    slot: int | None,
    epoch_info: dict | None,
    perf: dict | None,
    vote: dict | None,
    supply: dict | None,
) -> dict:
    """
    Assemble the normalised snapshot dict from raw collector outputs.

    Kept as a private helper so ``collect_fast()`` and ``collect_all()``
    share identical assembly logic without code duplication.  Supply may be
    ``None`` when called from the fast tier — stake_pct will be backfilled
    by the refresh loop once a cached supply value is available.
    """
    epoch_slot_start = epoch_eta_hours = epoch_slot_end = None
    if epoch_info and slot is not None:
        slot_index      = epoch_info.get("slotIndex", 0)
        slots_in_epoch  = epoch_info.get("slotsInEpoch", 1)
        avg_ms          = perf["avg_slot_time_ms"] if perf else 400.0
        epoch_slot_start = slot - slot_index
        epoch_slot_end   = epoch_slot_start + slots_in_epoch
        remaining_slots  = slots_in_epoch - slot_index
        epoch_eta_hours  = round(remaining_slots * avg_ms / 3_600_000, 2)

    health_status = "healthy" if health_raw == "ok" else "degraded"

    top_validators = []
    if vote and vote.get("top_5_by_stake"):
        total_supply_sol = supply["totalSOL"] if supply else None
        for v in vote["top_5_by_stake"]:
            stake_sol = v["activated_stake_sol"]
            stake_pct = (
                round(stake_sol / total_supply_sol * 100, 2)
                if total_supply_sol else None
            )
            top_validators.append({
                "votePubkey":          v["votePubkey"],
                "name":                v["votePubkey"][:8] + "…",
                "activated_stake_sol": stake_sol,
                "stake_pct":           stake_pct,
                "commission":          v["commission"],
            })

    return {
        "meta": {
            "collected_at": collected_at,
            "source":       "rpc-collector",
            "endpoint":     RPC_ENDPOINT,
        },
        "network": {
            "tps":               perf["tps"]               if perf else None,
            "avgSlotTimeMs":     perf["avg_slot_time_ms"]  if perf else None,
            "blockHeight":       slot,
            "epochNumber":       epoch_info["epoch"]        if epoch_info else None,
            "epochProgressPct":  epoch_info["progress_pct"] if epoch_info else None,
            "epochSlotIndex":    epoch_info["slotIndex"]    if epoch_info else None,
            "epochSlotsInEpoch": epoch_info["slotsInEpoch"] if epoch_info else None,
            "epochSlotStart":    epoch_slot_start,
            "epochSlotEnd":      epoch_slot_end,
            "epochEtaHours":     epoch_eta_hours,
            "healthStatus":      health_status,
        },
        "validators": {
            "activeCount":      vote["active_count"]      if vote else None,
            "delinquentCount":  vote["delinquent_count"]  if vote else None,
            "avgCommissionPct": vote["avg_commission"]    if vote else None,
            "topValidators":    top_validators,
        },
        "supply": supply,   # None when called from fast tier; merged later
    }


# ─── Two-tier collector API ───────────────────────────────────────────────────


def collect_fast() -> dict:
    """
    **Tier-1 collector** — all metrics *except* supply.  Target: <5 s
    (measured at ~1.3–2 s in practice).

    Runs ``get_health``, ``get_slot``, ``get_epoch_info``,
    ``get_recent_performance``, ``get_vote_accounts``, and (once the slot is
    known) ``get_block_time`` concurrently via a ``ThreadPoolExecutor``.

    ``getSupply`` is intentionally excluded — it takes 7–8 s on the public
    endpoint.  Supply data is fetched separately by ``collect_supply()`` on a
    60-second cadence and merged into each snapshot by ``serve_data.py``.

    .. note::
        **TPS and avgSlotTimeMs reflect Solana's own rolling performance-sample
        averages** (``getRecentPerformanceSamples``), not instantaneous values.
        Each sample covers a ~60-second validator window, so these fields
        represent recent history, not the current millisecond.  This is a
        Solana RPC characteristic — not something faster polling eliminates.

    Returns:
        Normalised snapshot dict with ``supply`` set to ``None``.
        Individual failed fields are also ``None`` rather than raising.
    """
    collected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        f_health = pool.submit(get_health)
        f_slot   = pool.submit(get_slot)
        f_epoch  = pool.submit(get_epoch_info)
        f_perf   = pool.submit(get_recent_performance)
        f_vote   = pool.submit(get_vote_accounts)

        # Slot resolves first → queue the dependent block-time call while
        # the other futures are still in flight.
        slot        = _resolve_future(f_slot, "get_slot", TIMEOUT_SEC + 2)
        f_blocktime = pool.submit(get_block_time, slot) if slot else None

        health_raw = _resolve_future(f_health, "get_health",            TIMEOUT_SEC + 2)
        epoch_info = _resolve_future(f_epoch,  "get_epoch_info",         TIMEOUT_SEC + 2)
        perf       = _resolve_future(f_perf,   "get_recent_performance", TIMEOUT_SEC + 2)
        vote       = _resolve_future(f_vote,   "get_vote_accounts",      TIMEOUT_SEC + 2)
        if f_blocktime:
            _resolve_future(f_blocktime, "get_block_time", TIMEOUT_SEC + 2)

    return _build_snapshot(collected_at, health_raw, slot, epoch_info, perf, vote, supply=None)


def collect_supply() -> dict | None:
    """
    **Tier-2 collector** — just ``getSupply``.  Expected duration: 7–8 s.
    Call on a slow cadence (e.g. every 60 s); cache the result between runs.

    Supply figures (total/circulating SOL) change at most ~0.1 % per minute
    so a 60-second staleness window is perfectly acceptable for a dashboard.

    Returns:
        Dict with keys ``totalSOL``, ``circulatingSOL``,
        ``nonCirculatingSOL``, and ``collected_at`` (ISO-8601 UTC).
        Returns ``None`` on failure — the caller should retain its previous
        cached value rather than overwriting with ``None``.
    """
    import sys as _sys

    collected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result = get_supply()

    if result is None:
        print(
            "WARNING: collect_supply() returned None — "
            "retaining previous cached supply value.",
            file=_sys.stderr,
        )
        return None

    return {
        "collected_at":      collected_at,
        "totalSOL":          result.get("total"),
        "circulatingSOL":    result.get("circulating"),
        "nonCirculatingSOL": result.get("non_circulating"),
    }


def collect_all() -> dict:
    """
    **Convenience wrapper** — run both tiers concurrently and merge.

    Intended for one-shot invocations (CLI, tests, cron) where you want the
    complete snapshot in a single call and don't mind waiting ~8 s for supply.
    The long-running ``serve_data.py`` refresh loop calls ``collect_fast()``
    and ``collect_supply()`` independently on separate cadences instead.

    Returns:
        Merged snapshot dict.  ``supply`` will be ``None`` only if
        ``collect_supply()`` itself fails.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_fast   = pool.submit(collect_fast)
        f_supply = pool.submit(collect_supply)

        snapshot = _resolve_future(f_fast,   "collect_fast",   TIMEOUT_SEC + 5)
        supply   = _resolve_future(f_supply, "collect_supply", SUPPLY_TIMEOUT_SEC + 5)

    if snapshot is None:
        snapshot = {}
    if supply is not None:
        snapshot["supply"] = supply
    return snapshot


# ─── Standalone runner ───────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys

    print(f"Fetching Solana RPC data from {RPC_ENDPOINT} …", file=sys.stderr)
    print(
        f"  Fast tier timeout : {TIMEOUT_SEC}s per call\n"
        f"  Supply timeout    : {SUPPLY_TIMEOUT_SEC}s\n"
        f"  Max retries       : {MAX_RETRIES}\n",
        file=sys.stderr,
    )

    print("── collect_fast() ─────────────────────────────", file=sys.stderr)
    t0     = time.perf_counter()
    fast   = collect_fast()
    t_fast = time.perf_counter() - t0
    print(f"   → {t_fast:.2f}s  (target: <2.0s)\n", file=sys.stderr)

    print("── collect_supply() ───────────────────────────", file=sys.stderr)
    t0    = time.perf_counter()
    sup   = collect_supply()
    t_sup = time.perf_counter() - t0
    print(f"   → {t_sup:.2f}s  (expected: 7–8s)\n", file=sys.stderr)

    if sup:
        fast["supply"] = sup

    print(json.dumps(fast, indent=2))
    print(
        f"\n# Wall time — fast: {t_fast:.2f}s   supply: {t_sup:.2f}s"
        f"   total: {t_fast + t_sup:.2f}s",
        file=sys.stderr,
    )

