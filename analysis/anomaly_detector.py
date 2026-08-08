"""
analysis/anomaly_detector.py
============================
Statistical anomaly detection for Solana network & market metrics.
Stdlib only — math, statistics, time, json, sys.

Rule checks:
  1. TPS: Flag if throughput drops > 1.5 stddev below rolling mean.
  2. Slot Time: Flag if avg slot time rises > 1.5 stddev above rolling mean.
  3. Delinquent Validators: Flag if count > 2x rolling mean OR > 2.0% of active validators.
  4. Price USD: Flag if % change from previous snapshot exceeds 5.0%.
  5. TVL USD: Flag if % deviation from rolling mean exceeds 10.0%.

Features:
  - Minimum history size required (default: 5 snapshots). If fewer exist, logs INFO and returns [].
  - Near-zero stddev / low variance handling: stddev is floored against a minimum noise threshold
    (2% of mean) to prevent division by near-zero and absurdly high sigma values.
  - Reported sigma is clamped to a sane max (5.0σ) with a clear display string.
"""

import math
import sys
import time
from typing import Any

# ─── Configuration Thresholds ──────────────────────────────────────────────────

THRESHOLDS: dict[str, Any] = {
    "min_history_count": 5,            # Minimum historical samples required to compute baseline
    "tps_stddev_below": 1.5,           # Flag if current TPS < mean - 1.5 * stddev
    "slot_time_stddev_above": 1.5,      # Flag if current slot time > mean + 1.5 * stddev
    "delinquent_multiplier": 2.0,       # Flag if delinquent count > 2x rolling mean
    "delinquent_hard_pct": 2.0,         # Flag if delinquent count > 2% of active set
    "price_interval_change_pct": 5.0,   # Flag if price moves > 5% in a single update
    "tvl_mean_change_pct": 10.0,        # Flag if TVL deviates > 10% from rolling mean
    "max_display_sigma": 5.0,          # Cap display sigma string at >5.0σ to avoid absurd figures
    "min_stddev_pct_floor": 0.02,       # Floor stddev at 2% of mean to prevent near-zero stddev spikes
}


# ─── Statistical Helpers ───────────────────────────────────────────────────────


def _calc_stats(values: list[float]) -> tuple[float, float]:
    """Calculate (mean, stddev) for a list of floats using sample stddev."""
    valid = [v for v in values if v is not None and not math.isnan(v)]
    if not valid:
        return 0.0, 0.0
    n = len(valid)
    mean = sum(valid) / n
    if n < 2:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in valid) / (n - 1)
    return mean, math.sqrt(variance)


def _calc_sigma(diff: float, stddev: float, mean: float, min_stddev_pct: float = 0.02) -> tuple[float, str, bool]:
    """
    Calculate safe sigma multiplier with near-zero stddev protection and display clamping.

    Args:
        diff: Absolute difference between current value and mean (|current - mean|).
        stddev: Raw computed standard deviation.
        mean: Computed mean baseline.
        min_stddev_pct: Floor stddev at this fraction of mean to avoid near-zero division.

    Returns:
        (clamped_sigma_val, display_str, is_low_variance)
    """
    floor = max(0.01, abs(mean) * min_stddev_pct)
    effective_std = max(stddev, floor)
    is_low_var = stddev < floor and mean != 0

    if effective_std <= 0:
        return 0.0, "0.0σ", True

    raw_sigma = diff / effective_std
    if raw_sigma >= 5.0:
        display_str = ">5.0σ"
        clamped_sigma = 5.0
    else:
        display_str = f"{raw_sigma:.1f}σ"
        clamped_sigma = raw_sigma

    return clamped_sigma, display_str, is_low_var


def _normalize_snapshot(data: dict) -> dict[str, Any]:
    """
    Extract flat key-value metrics whether `data` is a `data.json`-shaped dict
    or a flat row from `storage/history.py`.
    """
    if "network" in data or "validators" in data or "market" in data or "defi" in data:
        net = data.get("network") or {}
        val = data.get("validators") or {}
        mkt = data.get("market") or {}
        defi = data.get("defi") or {}

        return {
            "timestamp": data.get("meta", {}).get("collected_at") or int(time.time()),
            "tps": net.get("tps"),
            "avg_slot_time_ms": net.get("avgSlotTimeMs"),
            "active_validators": val.get("activeCount"),
            "delinquent_validators": val.get("delinquentCount"),
            "price_usd": mkt.get("priceUsd"),
            "tvl_usd": defi.get("tvlUsd"),
        }

    return {
        "timestamp": data.get("timestamp") or int(time.time()),
        "tps": data.get("tps"),
        "avg_slot_time_ms": data.get("avg_slot_time_ms"),
        "active_validators": data.get("active_validators"),
        "delinquent_validators": data.get("delinquent_validators"),
        "price_usd": data.get("price_usd"),
        "tvl_usd": data.get("tvl_usd"),
    }


# ─── Anomaly Detector ─────────────────────────────────────────────────────────


def detect_anomalies(current: dict, history: list[dict], thresholds: dict[str, Any] | None = None, verbose: bool = True) -> list[dict[str, Any]]:
    """
    Detect statistical anomalies in `current` metrics relative to `history`.

    Args:
        current: Dict containing current metrics (either data.json shape or history row shape).
        history: List of historical metric dicts (chronological order).
        thresholds: Optional custom thresholds dictionary overriding defaults.
        verbose: Print baseline mean/stddev to stderr for debugging.

    Returns:
        List of alert dicts:
            {
                "id": str,
                "severity": "warning" | "critical",
                "metric": str,
                "title": str,
                "message": str,
                "timestamp": str (ISO UTC),
            }
    """
    cfg = {**THRESHOLDS, **(thresholds or {})}
    min_count = cfg["min_history_count"]

    if len(history) < min_count:
        print(
            f"INFO: [anomaly_detector] History size {len(history)} < min required {min_count}; skipping anomaly detection.",
            file=sys.stderr,
        )
        return []

    curr_norm = _normalize_snapshot(current)
    hist_norm = [_normalize_snapshot(h) for h in history]

    alerts: list[dict[str, Any]] = []
    iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 1. TPS Check
    tps_vals = [h["tps"] for h in hist_norm if h.get("tps") is not None]
    curr_tps = curr_norm.get("tps")
    if curr_tps is not None and len(tps_vals) >= min_count:
        tps_mean, tps_std = _calc_stats(tps_vals)
        if verbose:
            print(f"[DEBUG anomaly_detector] TPS: current={curr_tps:.1f}, mean={tps_mean:.1f}, stddev={tps_std:.2f}", file=sys.stderr)

        tps_drop = tps_mean - curr_tps
        _, tps_sigma_str, low_var = _calc_sigma(max(0.0, tps_drop), tps_std, tps_mean, cfg["min_stddev_pct_floor"])
        tps_thresh = tps_mean - (cfg["tps_stddev_below"] * max(tps_std, tps_mean * cfg["min_stddev_pct_floor"]))

        if curr_tps < tps_thresh:
            pct_drop = (tps_drop / tps_mean * 100) if tps_mean > 0 else 0.0
            sev = "critical" if pct_drop >= 30.0 or tps_sigma_str == ">5.0σ" else "warning"
            var_note = " (low baseline variance)" if low_var else ""
            alerts.append({
                "id": "anomaly-tps",
                "severity": sev,
                "metric": "tps",
                "title": "TPS Anomaly Detected",
                "message": f"Transaction throughput dropped ~{pct_drop:.1f}% below rolling mean "
                           f"({curr_tps:,.0f} vs {tps_mean:,.0f} TPS, {tps_sigma_str} below baseline{var_note}). "
                           f"Possible congestion or leader instability.",
                "timestamp": iso_now,
            })

    # 2. Avg Slot Time Check
    st_vals = [h["avg_slot_time_ms"] for h in hist_norm if h.get("avg_slot_time_ms") is not None]
    curr_st = curr_norm.get("avg_slot_time_ms")
    if curr_st is not None and len(st_vals) >= min_count:
        st_mean, st_std = _calc_stats(st_vals)
        if verbose:
            print(f"[DEBUG anomaly_detector] Slot Time: current={curr_st:.1f}ms, mean={st_mean:.1f}ms, stddev={st_std:.2f}ms", file=sys.stderr)

        st_rise = curr_st - st_mean
        _, st_sigma_str, low_var = _calc_sigma(max(0.0, st_rise), st_std, st_mean, cfg["min_stddev_pct_floor"])
        st_thresh = st_mean + (cfg["slot_time_stddev_above"] * max(st_std, st_mean * cfg["min_stddev_pct_floor"]))

        if curr_st > st_thresh:
            pct_rise = (st_rise / st_mean * 100) if st_mean > 0 else 0.0
            sev = "critical" if curr_st > 550 or st_sigma_str == ">5.0σ" else "warning"
            var_note = " (low baseline variance)" if low_var else ""
            alerts.append({
                "id": "anomaly-slot-time",
                "severity": sev,
                "metric": "avg_slot_time_ms",
                "title": "Slow Slot Time Warning",
                "message": f"Average slot time rose ~{pct_rise:.1f}% to {curr_st:.0f} ms "
                           f"({st_sigma_str} above rolling mean {st_mean:.0f} ms{var_note}).",
                "timestamp": iso_now,
            })

    # 3. Delinquent Validators Check
    delinq_vals = [h["delinquent_validators"] for h in hist_norm if h.get("delinquent_validators") is not None]
    curr_delinq = curr_norm.get("delinquent_validators")
    curr_active = curr_norm.get("active_validators") or 0

    if curr_delinq is not None and len(delinq_vals) >= min_count:
        delinq_mean, delinq_std = _calc_stats(delinq_vals)
        if verbose:
            print(f"[DEBUG anomaly_detector] Delinquent: current={curr_delinq}, mean={delinq_mean:.1f}, stddev={delinq_std:.2f}", file=sys.stderr)

        total_val = curr_active + curr_delinq
        delinq_pct = (curr_delinq / total_val * 100) if total_val > 0 else 0.0

        is_mult_spike = (delinq_mean > 0) and (curr_delinq >= delinq_mean * cfg["delinquent_multiplier"])
        is_hard_spike = delinq_pct >= cfg["delinquent_hard_pct"]

        if is_mult_spike or is_hard_spike:
            sev = "critical" if delinq_pct >= 3.0 or (delinq_mean > 0 and curr_delinq >= delinq_mean * 3.0) else "warning"
            mult_str = f"{curr_delinq / delinq_mean:.1f}x above baseline" if delinq_mean > 0 else "elevated"
            alerts.append({
                "id": "anomaly-delinquent",
                "severity": sev,
                "metric": "delinquent_validators",
                "title": "Elevated Validator Delinquency",
                "message": f"{curr_delinq} validators currently delinquent ({delinq_pct:.2f}% of active set) — "
                           f"{mult_str} (rolling mean: {delinq_mean:.1f}).",
                "timestamp": iso_now,
            })

    # 4. Price Shift Check (against immediately preceding snapshot)
    curr_price = curr_norm.get("price_usd")
    prev_price = hist_norm[-1].get("price_usd") if hist_norm else None
    if curr_price is not None and prev_price is not None and prev_price > 0:
        price_change_pct = ((curr_price - prev_price) / prev_price) * 100
        if verbose:
            print(f"[DEBUG anomaly_detector] Price: current=${curr_price:.2f}, prev=${prev_price:.2f}, change={price_change_pct:+.2f}%", file=sys.stderr)

        if abs(price_change_pct) >= cfg["price_interval_change_pct"]:
            sev = "critical" if abs(price_change_pct) >= 10.0 else "warning"
            alerts.append({
                "id": "anomaly-price",
                "severity": sev,
                "metric": "price_usd",
                "title": "Rapid SOL Price Shift",
                "message": f"SOL price shifted {price_change_pct:+.1f}% in a single update "
                           f"(${prev_price:.2f} -> ${curr_price:.2f}).",
                "timestamp": iso_now,
            })

    # 5. TVL Deviation Check (against rolling mean)
    tvl_vals = [h["tvl_usd"] for h in hist_norm if h.get("tvl_usd") is not None]
    curr_tvl = curr_norm.get("tvl_usd")
    if curr_tvl is not None and len(tvl_vals) >= min_count:
        tvl_mean, tvl_std = _calc_stats(tvl_vals)
        if verbose:
            print(f"[DEBUG anomaly_detector] TVL: current=${curr_tvl/1e9:.2f}B, mean=${tvl_mean/1e9:.2f}B, stddev=${tvl_std/1e9:.3f}B", file=sys.stderr)

        if tvl_mean > 0:
            tvl_diff_pct = ((curr_tvl - tvl_mean) / tvl_mean) * 100
            if abs(tvl_diff_pct) >= cfg["tvl_mean_change_pct"]:
                sev = "critical" if abs(tvl_diff_pct) >= 20.0 else "warning"
                alerts.append({
                    "id": "anomaly-tvl",
                    "severity": sev,
                    "metric": "tvl_usd",
                    "title": "TVL Anomaly Detected",
                    "message": f"Solana TVL shifted {tvl_diff_pct:+.1f}% from rolling baseline "
                               f"(${curr_tvl/1e9:.2f}B vs mean ${tvl_mean/1e9:.2f}B).",
                    "timestamp": iso_now,
                })

    return alerts


def detect_correlated_anomalies(current: dict, history: list[dict], single_metric_alerts: list[dict]) -> list[dict[str, Any]]:
    """
    Examine the single_metric_alerts list for co-occurring anomalies.
    Returns any correlated alert dictionaries representing multi-source anomalies.
    """
    correlated_alerts = []
    iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Build a lookup of active alert metrics
    active_metrics = {a.get("metric") for a in single_metric_alerts if a.get("metric")}

    # 1. Congestion Check: TPS anomaly AND slot_time anomaly co-occurrence
    if "tps" in active_metrics and "avg_slot_time_ms" in active_metrics:
        correlated_alerts.append({
            "id": "correlation-congestion",
            "type": "correlated",
            "severity": "critical",
            "metric": "congestion",
            "title": "Network Stress Detected (Multi-Source)",
            "message": "Network stress detected: TPS drop + slot time increase occurring together — consistent with congestion, not isolated noise.",
            "timestamp": iso_now,
            "related_metrics": ["tps", "avg_slot_time_ms"]
        })

    # 2. Market Event Check: price_usd AND tvl_usd co-occurrence in the SAME direction
    if "price_usd" in active_metrics and "tvl_usd" in active_metrics:
        curr_norm = _normalize_snapshot(current)
        
        # Price direction
        price_dir = 0
        curr_price = curr_norm.get("price_usd")
        prev_price = None
        if history:
            prev_price = _normalize_snapshot(history[-1]).get("price_usd")
        if curr_price is not None and prev_price is not None and prev_price > 0:
            price_dir = 1 if curr_price > prev_price else -1

        # TVL direction
        tvl_dir = 0
        curr_tvl = curr_norm.get("tvl_usd")
        hist_tvls = [h.get("tvl_usd") for h in [_normalize_snapshot(x) for x in history] if h.get("tvl_usd") is not None]
        if curr_tvl is not None and hist_tvls:
            tvl_mean = sum(hist_tvls) / len(hist_tvls)
            tvl_dir = 1 if curr_tvl > tvl_mean else -1

        if price_dir != 0 and tvl_dir != 0 and price_dir == tvl_dir:
            direction_str = "upward" if price_dir > 0 else "downward"
            correlated_alerts.append({
                "id": "correlation-market",
                "type": "correlated",
                "severity": "critical",
                "metric": "market",
                "title": "Correlated Market Shift (Multi-Source)",
                "message": f"Correlated market movement: SOL price and TVL moved together in a {direction_str} direction — broader market event likely, not an isolated data anomaly.",
                "timestamp": iso_now,
                "related_metrics": ["price_usd", "tvl_usd"]
            })

    # 3. Isolated Validator Delinquency Check
    if "delinquent_validators" in active_metrics:
        if "tps" not in active_metrics and "avg_slot_time_ms" not in active_metrics:
            correlated_alerts.append({
                "id": "correlation-isolated-delinquency",
                "type": "correlated",
                "severity": "warning",
                "metric": "delinquent_validators",
                "title": "Isolated Delinquency (Multi-Source)",
                "message": "Isolated validator delinquency — network performance and transaction throughput are otherwise normal.",
                "timestamp": iso_now,
                "related_metrics": ["delinquent_validators"]
            })

    return correlated_alerts


# ─── Standalone Runner ────────────────────────────────────────────────────────


if __name__ == "__main__":
    import json
    from storage.history import get_recent

    print("Running anomaly detector against storage/history.py DB …", file=sys.stderr)

    recent = get_recent(20)
    print(f"Loaded {len(recent)} recent historical snapshots.", file=sys.stderr)

    if len(recent) >= 5:
        latest = recent[-1]
        historical_baseline = recent[:-1]
        results = detect_anomalies(latest, historical_baseline, verbose=True)
        print(json.dumps(results, indent=2))
    else:
        # Realistic synthetic baseline with natural realistic variance:
        # - TPS around ~3200 with ~250 TPS variance
        # - Slot time around ~420ms with ~25ms variance
        # - Active validators ~1520, delinquent ~12
        # - Price $73.40, TVL $4.75B
        print("Fewer than 5 DB snapshots found; testing with realistic synthetic baseline …", file=sys.stderr)
        import random
        random.seed(42)

        synth_history = []
        base_tps = 3200.0
        base_st = 420.0
        for i in range(10):
            synth_history.append({
                "timestamp": 1000 + i * 5,
                "tps": base_tps + (random.random() - 0.5) * 400,          # ~3000 to 3400
                "avg_slot_time_ms": base_st + (random.random() - 0.5) * 40, # ~400 to 440ms
                "active_validators": 1520,
                "delinquent_validators": 10 + (i % 3),                     # 10 to 12
                "price_usd": 73.40 + (random.random() - 0.5) * 1.5,       # $72.65 to $74.15
                "tvl_usd": 4.75e9 + (random.random() - 0.5) * 0.1e9,       # $4.70B to $4.80B
            })

        # Test anomaly 1: Realistic drop (TPS drops from ~3200 to 2400 — a 25% drop, ~3.1σ)
        synth_anomalous_tps = {
            "network": {"tps": 2400.0, "avgSlotTimeMs": 420.0},
            "validators": {"activeCount": 1520, "delinquentCount": 11},
            "market": {"priceUsd": 73.40},
            "defi": {"tvlUsd": 4.75e9},
        }

        # Test anomaly 2: Realistic slot time rise (Slot time rises from ~420ms to 495ms — ~4.2σ)
        synth_anomalous_st = {
            "network": {"tps": 3200.0, "avgSlotTimeMs": 495.0},
            "validators": {"activeCount": 1520, "delinquentCount": 11},
            "market": {"priceUsd": 73.40},
            "defi": {"tvlUsd": 4.75e9},
        }

        print("\n--- Test 1: Realistic TPS Drop (3200 -> 2400 TPS) ---", file=sys.stderr)
        res1 = detect_anomalies(synth_anomalous_tps, synth_history, verbose=True)
        corr1 = detect_correlated_anomalies(synth_anomalous_tps, synth_history, res1)
        print("Single alerts:", json.dumps(res1, indent=2))
        print("Correlated alerts:", json.dumps(corr1, indent=2))

        print("\n--- Test 2: Realistic Slot Time Rise (420ms -> 495ms) ---", file=sys.stderr)
        res2 = detect_anomalies(synth_anomalous_st, synth_history, verbose=True)
        corr2 = detect_correlated_anomalies(synth_anomalous_st, synth_history, res2)
        print("Single alerts:", json.dumps(res2, indent=2))
        print("Correlated alerts:", json.dumps(corr2, indent=2))

        # Test anomaly 3: Co-occurring TPS drop AND Slot Time rise (Congestion)
        synth_anomalous_both = {
            "network": {"tps": 2400.0, "avgSlotTimeMs": 495.0},
            "validators": {"activeCount": 1520, "delinquentCount": 11},
            "market": {"priceUsd": 73.40},
            "defi": {"tvlUsd": 4.75e9},
        }
        print("\n--- Test 3: Co-occurring TPS Drop & Slot Time Rise (Congestion) ---", file=sys.stderr)
        res3 = detect_anomalies(synth_anomalous_both, synth_history, verbose=True)
        corr3 = detect_correlated_anomalies(synth_anomalous_both, synth_history, res3)
        print("Single alerts:", json.dumps(res3, indent=2))
        print("Correlated alerts:", json.dumps(corr3, indent=2))
