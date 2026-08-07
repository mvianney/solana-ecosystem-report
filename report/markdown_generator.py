"""
report/markdown_generator.py
============================
Generates a human-readable Markdown report from the current Solana ecosystem snapshot.
"""

import os
import pathlib
import sys
import time

# ─── Formatting Helpers ────────────────────────────────────────────────────────

def _fmt_usd(val: float | None) -> str:
    """Format float as USD currency ($XX.XX)."""
    if val is None:
        return "N/A"
    return f"${val:,.2f}"

def _fmt_usd_compact(val: float | None) -> str:
    """Format raw USD values into compact readable format (B, M, K)."""
    if val is None:
        return "N/A"
    abs_val = abs(val)
    if abs_val >= 1e9:
        return f"${val / 1e9:.2f}B"
    if abs_val >= 1e6:
        return f"${val / 1e6:.2f}M"
    if abs_val >= 1e3:
        return f"${val / 1e3:.2f}K"
    return f"${val:.2f}"

def _fmt_pct(val: float | None, show_sign: bool = False) -> str:
    """Format float percentage, optionally with +/- prefix for changes."""
    if val is None:
        return "N/A"
    if show_sign:
        prefix = "+" if val > 0 else ""
        return f"{prefix}{val:.2f}%"
    return f"{val:.2f}%"

def _fmt_num(val: int | float | None) -> str:
    """Format integers or floats with comma separators."""
    if val is None:
        return "N/A"
    if isinstance(val, float):
        return f"{val:,.2f}"
    return f"{val:,}"

def _fmt_sol(val: float | None, precision: int = 6) -> str:
    """Format SOL value."""
    if val is None:
        return "N/A"
    return f"{val:.{precision}f} SOL"

# ─── Report Generator ──────────────────────────────────────────────────────────

def generate_markdown_report(snapshot: dict, output_path: str = "reports/latest.md") -> None:
    """
    Renders the Solana snapshot as a human-readable Markdown report.
    """
    try:
        meta = snapshot.get("meta") or {}
        net = snapshot.get("network") or {}
        val = snapshot.get("validators") or {}
        mkt = snapshot.get("market") or {}
        defi = snapshot.get("defi") or {}
        econ = snapshot.get("economics_extra") or {}
        news = snapshot.get("news") or []
        alerts = snapshot.get("alerts") or snapshot.get("anomalies") or []
        
        # Timestamp
        collected_at = meta.get("collected_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        
        md = []
        md.append("# Solana Ecosystem Health Report")
        md.append(f"*Generated at: `{collected_at}` (UTC)*\n")
        
        # 1. Anomaly Alerts (High Priority Banner)
        md.append("## Anomaly & Alert Summary")
        if alerts:
            for alert in alerts:
                severity_badge = "⚠️ WARNING" if alert.get("severity") == "warning" else "🚨 CRITICAL"
                md.append(f"- **{severity_badge}**: {alert.get('message')} *({alert.get('time', 'recent')})*")
        else:
            md.append("✅ **All systems nominal.** No anomalies detected in current network performance metrics.")
        md.append("")
        
        # 2. Network Performance
        md.append("## Network Performance")
        md.append("| Metric | Current Value | Details |")
        md.append("|---|---|---|")
        md.append(f"| **TPS** | {_fmt_num(net.get('tps'))} | Rolling 60s transaction throughput |")
        md.append(f"| **Slot Time** | {_fmt_num(net.get('avgSlotTimeMs'))} ms | Avg time to produce a block |")
        md.append(f"| **Block Height** | {_fmt_num(net.get('blockHeight'))} | Current finalized slot |")
        
        epoch_str = f"Epoch {net.get('epochNumber') or 'N/A'}"
        progress = net.get("epochProgressPct")
        progress_str = f"{progress:.1f}% complete" if progress is not None else "N/A"
        eta = net.get("epochEtaHours")
        eta_str = f" (~{eta}h remaining)" if eta is not None else ""
        md.append(f"| **Epoch Progress** | {progress_str} | {epoch_str}{eta_str} |")
        md.append(f"| **Health Status** | {str(net.get('healthStatus', 'unknown')).upper()} | RPC node status check |")
        md.append("")
        
        # 3. Validator Health
        md.append("## Validator Health")
        md.append(f"- **Active Validators**: {_fmt_num(val.get('activeCount'))}")
        md.append(f"- **Delinquent Validators**: {_fmt_num(val.get('delinquentCount'))}")
        md.append(f"- **Average Validator Commission**: {_fmt_pct(val.get('avgCommissionPct'))}")
        md.append("")
        
        md.append("### Top 5 Validators by Activated Stake")
        top_v = val.get("topValidators") or []
        if top_v:
            md.append("| Rank | Validator Name / Pubkey | Activated Stake | Stake Share | Commission |")
            md.append("|:---:|---|---|:---:|:---:|")
            for idx, v in enumerate(top_v, start=1):
                stake_sol = v.get("activated_stake_sol") or v.get("activatedStake")
                stake_sol_str = _fmt_num(stake_sol) + " SOL" if stake_sol else "N/A"
                md.append(
                    f"| {idx} | `{v.get('name') or v.get('votePubkey', 'Unknown')}` | "
                    f"{stake_sol_str} | "
                    f"{_fmt_pct(v.get('stake_pct') or v.get('stakePct'))} | "
                    f"{v.get('commission') or v.get('commissionPct', 0)}% |"
                )
        else:
            md.append("*Top validator stake distribution metrics are currently cached/unavailable.*")
        md.append("")
        
        # 4. Economics
        md.append("## Economic Indicators")
        md.append("| Metric | Value | Source |")
        md.append("|---|---|---|")
        md.append(f"| **SOL Price (USD)** | {_fmt_usd(mkt.get('priceUsd'))} ({_fmt_pct(mkt.get('change24hPct'), show_sign=True)}) | Jupiter Spot Price |")
        md.append(f"| **Market Cap** | {_fmt_usd_compact(mkt.get('marketCapUsd'))} | CoinGecko |")
        md.append(f"| **CEX Volume (24h)** | {_fmt_usd_compact(mkt.get('volume24hUsd'))} | CoinGecko |")
        md.append(f"| **DeFi TVL (USD)** | {_fmt_usd_compact(defi.get('tvlUsd'))} | DeFiLlama |")
        md.append(f"| **Stablecoin Supply** | {_fmt_usd_compact(defi.get('stablecoinSupplyUsd'))} | DeFiLlama |")
        md.append(f"| **DEX Volume (24h)** | {_fmt_usd_compact(defi.get('dexVolume24hUsd'))} | DeFiLlama |")
        md.append(f"| **Real Economic Value (REV)** | {_fmt_usd_compact(econ.get('revUsd24h'))} | DeFiLlama (User fees) |")
        md.append(f"| **Median Transaction Fee** | {_fmt_sol(econ.get('medianFeeSol'))} | Solana JSON-RPC (priority + base) |")
        md.append("")
        
        # 5. Ecosystem Growth
        md.append("## Ecosystem Growth")
        md.append(f"- **Tokenized RWA TVL**: {_fmt_usd_compact(econ.get('rwaVolumeUsd'))} *(Source: DeFiLlama RWA category)*")
        md.append("- **Daily Active Addresses**: *Not available — no free public API*")
        md.append("")
        
        # 6. News & Upgrades
        md.append("## News & Announcements")
        if news:
            for item in news[:5]: # Show top 5 news items
                pub_time = item.get("published")
                pub_str = f" ({pub_time})" if pub_time else ""
                md.append(f"- [{item.get('title')}]({item.get('link')}) - *Source: {item.get('source')}{pub_str}*")
        else:
            md.append("*No recent ecosystem news announcements fetched yet.*")
        md.append("")
        
        # Write to file
        path = pathlib.Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        tmp_path.replace(path)
        
    except Exception as exc:
        print(f"ERROR: markdown_generator failed to write to {output_path} - {exc!r}", file=sys.stderr)
        raise

if __name__ == "__main__":
    # Standalone verification using a dummy sample snapshot
    sample_snapshot = {
        "meta": {
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "markdown-generator-test"
        },
        "network": {
            "tps": 2845.2,
            "avgSlotTimeMs": 412.5,
            "blockHeight": 284100000,
            "epochNumber": 1013,
            "epochProgressPct": 67.3,
            "epochEtaHours": 4.2,
            "healthStatus": "healthy"
        },
        "validators": {
            "activeCount": 1521,
            "delinquentCount": 23,
            "avgCommissionPct": 7.2,
            "topValidators": [
                {"name": "Helius", "votePubkey": "HeliusVoteAddress11111111", "activated_stake_sol": 24000000000000000, "stake_pct": 8.2, "commission": 5},
                {"name": "Jito Labs", "votePubkey": "JitoVoteAddress222222222", "activated_stake_sol": 21000000000000000, "stake_pct": 7.6, "commission": 8}
            ]
        },
        "market": {
            "priceUsd": 74.5,
            "change24hPct": 2.5,
            "marketCapUsd": 43150000000.0,
            "volume24hUsd": 1500000000.0
        },
        "defi": {
            "tvlUsd": 4730000000.0,
            "stablecoinSupplyUsd": 9800000000.0,
            "dexVolume24hUsd": 4200000000.0
        },
        "economics_extra": {
            "medianFeeSol": 0.000005,
            "revUsd24h": 516121.0,
            "rwaVolumeUsd": 1809139005.65
        },
        "news": [
            {"title": "Solana Blog Post: SIMD-525 details", "link": "https://solana.com/news/1", "source": "Solana Blog", "published": "2 hours ago"},
            {"title": "Cointelegraph: Solana DEX Volume Surges", "link": "https://cointelegraph.com/news/2", "source": "Cointelegraph", "published": "5 hours ago"}
        ],
        "alerts": [
            {"severity": "warning", "message": "TPS dropped ~15% below baseline.", "time": "14:10 UTC"}
        ]
    }
    
    print("Generating sample Markdown report...", file=sys.stderr)
    generate_markdown_report(sample_snapshot, "reports/sample_latest.md")
    print(f"Sample Markdown report generated at: {os.path.abspath('reports/sample_latest.md')}", file=sys.stderr)
