"""
storage/history.py
==================
SQLite-backed storage for Solana ecosystem snapshots.
Stdlib only — sqlite3, json, pathlib, time.

Database Schema:
    table: snapshots
        id                    INTEGER PRIMARY KEY AUTOINCREMENT
        timestamp             INTEGER (Unix epoch seconds)
        tps                   REAL
        avg_slot_time_ms      REAL
        active_validators     INTEGER
        delinquent_validators INTEGER
        price_usd             REAL
        tvl_usd               REAL

Features:
    - Automatic schema initialization (init_db)
    - Safe insertion from data.json-shaped snapshot dicts (append_snapshot)
    - Recent history retrieval ordered chronologically (get_recent)
    - Automatic retention pruning to keep the database lightweight (default 500 rows)
"""

import pathlib
import sqlite3
import sys
import time
from typing import Any

DEFAULT_DB_PATH = pathlib.Path(__file__).parent / "history.db"
RETENTION_LIMIT = 500


def init_db(db_path: str | pathlib.Path = DEFAULT_DB_PATH) -> None:
    """
    Initialize the SQLite database and create the `snapshots` table if not exists.
    """
    path = pathlib.Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(path) as conn:
        cursor = conn.execute("PRAGMA table_info(snapshots)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "id" not in columns:
            conn.execute("DROP TABLE snapshots")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                tps REAL,
                avg_slot_time_ms REAL,
                active_validators INTEGER,
                delinquent_validators INTEGER,
                price_usd REAL,
                tvl_usd REAL
            )
        """)
        conn.commit()


def prune_old_snapshots(db_path: str | pathlib.Path = DEFAULT_DB_PATH, limit: int = RETENTION_LIMIT) -> int:
    """
    Prune rows beyond `limit` retention count, keeping the `limit` newest entries by ID/timestamp.
    """
    path = pathlib.Path(db_path)
    if not path.exists():
        return 0

    with sqlite3.connect(path) as conn:
        cursor = conn.execute("""
            DELETE FROM snapshots
            WHERE id NOT IN (
                SELECT id FROM snapshots
                ORDER BY id DESC
                LIMIT ?
            )
        """, (limit,))
        conn.commit()
        return cursor.rowcount


def append_snapshot(data: dict, db_path: str | pathlib.Path = DEFAULT_DB_PATH) -> bool:
    """
    Extract metric values from a `data.json`-shaped snapshot dict and insert into DB.
    Also triggers pruning to enforce RETENTION_LIMIT.
    """
    init_db(db_path)

    # Extract metrics safely from data.json nested structure
    net = data.get("network") or {}
    val = data.get("validators") or {}
    mkt = data.get("market") or {}
    defi = data.get("defi") or {}

    tps = net.get("tps") if "network" in data else data.get("tps")
    avg_slot_time_ms = net.get("avgSlotTimeMs") if "network" in data else data.get("avg_slot_time_ms")
    active_validators = val.get("activeCount") if "validators" in data else data.get("active_validators")
    delinquent_validators = val.get("delinquentCount") if "validators" in data else data.get("delinquent_validators")
    price_usd = mkt.get("priceUsd") if "market" in data else data.get("price_usd")
    tvl_usd = defi.get("tvlUsd") if "defi" in data else data.get("tvl_usd")

    ts = int(time.time())

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO snapshots (
                timestamp, tps, avg_slot_time_ms, active_validators,
                delinquent_validators, price_usd, tvl_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ts, tps, avg_slot_time_ms, active_validators, delinquent_validators, price_usd, tvl_usd))
        conn.commit()

    prune_old_snapshots(db_path, RETENTION_LIMIT)
    return True


def get_recent(n: int = 20, db_path: str | pathlib.Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """
    Retrieve the last `n` snapshots from SQLite, returned in chronological order (oldest to newest).
    """
    path = pathlib.Path(db_path)
    if not path.exists():
        return []

    init_db(path)

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
            SELECT * FROM (
                SELECT id, timestamp, tps, avg_slot_time_ms, active_validators,
                       delinquent_validators, price_usd, tvl_usd
                FROM snapshots
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
        """, (n,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


if __name__ == "__main__":
    import json

    db_file = pathlib.Path(__file__).parent / "test_history.db"
    print(f"Testing storage/history.py using test DB: {db_file}", file=sys.stderr)

    init_db(db_file)

    sample_snapshot = {
        "network": {"tps": 2850.5, "avgSlotTimeMs": 415.2},
        "validators": {"activeCount": 1520, "delinquentCount": 18},
        "market": {"priceUsd": 73.42},
        "defi": {"tvlUsd": 4750000000.0},
    }

    append_snapshot(sample_snapshot, db_file)
    history = get_recent(5, db_file)
    print(json.dumps(history, indent=2))

    # Clean up test database
    if db_file.exists():
        db_file.unlink()
