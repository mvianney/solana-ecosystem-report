"""
report/json_generator.py
========================
Generates a pretty-printed JSON report from the current Solana ecosystem snapshot.
"""

import json
import os
import pathlib
import sys
import time

def generate_json_report(snapshot: dict, output_path: str = "reports/latest.json") -> None:
    """
    Writes the snapshot as clean, pretty-printed JSON.
    Ensures that the output directory exists.
    """
    try:
        path = pathlib.Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write to temporary file first and replace atomically
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        tmp_path.replace(path)
    except Exception as exc:
        print(f"ERROR: json_generator failed to write to {output_path} - {exc!r}", file=sys.stderr)
        raise

if __name__ == "__main__":
    # Standalone verification using a dummy sample snapshot
    sample_snapshot = {
        "meta": {
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "json-generator-test"
        },
        "network": {
            "tps": 2845.2,
            "avgSlotTimeMs": 412.5,
            "blockHeight": 284100000,
            "epochNumber": 1013,
            "epochProgressPct": 67.3,
            "healthStatus": "healthy"
        },
        "validators": {
            "activeCount": 1521,
            "delinquentCount": 23,
            "avgCommissionPct": 7.2,
            "topValidators": [
                {"name": "Helius", "activated_stake_sol": 24000000.0, "stake_pct": 8.2, "commission": 5},
                {"name": "Jito Labs", "activated_stake_sol": 21000000.0, "stake_pct": 7.6, "commission": 8}
            ]
        },
        "market": {
            "priceUsd": 74.5,
            "change24hPct": 2.5
        },
        "defi": {
            "tvlUsd": 4730000000.0
        },
        "economics_extra": {
            "medianFeeSol": 0.000005,
            "revUsd24h": 516121.0,
            "rwaVolumeUsd": 1809139005.65
        },
        "news": [
            {"title": "Solana blog post", "link": "https://solana.com/news/1", "source": "Solana Blog"}
        ],
        "alerts": []
    }
    
    print("Generating sample JSON report...", file=sys.stderr)
    generate_json_report(sample_snapshot, "reports/sample_latest.json")
    print(f"Sample JSON report generated at: {os.path.abspath('reports/sample_latest.json')}", file=sys.stderr)
