"""
Option Chain Snapshot Collector

Periodically saves option chain data to SQLite for building a
historical option premium dataset for accurate backtesting.

Runs as a background thread during market hours, capturing
snapshots every N minutes.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "chain_history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    underlying TEXT NOT NULL,
    expiry TEXT NOT NULL,
    spot_price REAL NOT NULL,
    atm_strike REAL NOT NULL,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strike_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    underlying TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL,
    ltp REAL,
    bid REAL,
    ask REAL,
    oi INTEGER,
    volume INTEGER,
    iv REAL,
    spot_price REAL
);

CREATE INDEX IF NOT EXISTS idx_strike_ts ON strike_snapshots(underlying, expiry, strike, timestamp);
CREATE INDEX IF NOT EXISTS idx_chain_ts ON chain_snapshots(underlying, timestamp);
"""


class ChainCollector:

    def __init__(self, option_chain_resolver, interval_minutes: int = 5):
        self._ocr = option_chain_resolver
        self._interval = interval_minutes * 60
        self._db_path = str(DB_PATH)
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.executescript(_SCHEMA)
        conn.close()
        log.info("Chain collector DB ready: %s", self._db_path)

    def _is_market_hours(self) -> bool:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            return False
        t = now.hour * 100 + now.minute
        return 915 <= t <= 1530

    def _save_snapshot(self, underlying: str):
        expiry = self._ocr.get_nearest_expiry(underlying)
        if not expiry:
            return

        chain_data = self._ocr.fetch_chain(underlying, expiry)
        if not chain_data:
            return

        inner = chain_data.get("data", chain_data)
        spot = inner.get("last_price", 0)
        oc = inner.get("oc", {})
        if not oc or spot <= 0:
            return

        now_str = datetime.now(IST).isoformat()
        strike_step = 50 if underlying in ("NIFTY", "FINNIFTY") else 100
        atm = round(spot / strike_step) * strike_step

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO chain_snapshots (timestamp, underlying, expiry, spot_price, atm_strike, snapshot_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now_str, underlying, expiry, spot, atm, json.dumps(oc)))

            rows = []
            for strike_str, sides in oc.items():
                try:
                    strike_val = float(strike_str)
                except (ValueError, TypeError):
                    continue

                if abs(strike_val - atm) > strike_step * 15:
                    continue

                for otype, key in [("CE", "ce"), ("PE", "pe")]:
                    opt = sides.get(key, {})
                    if not opt:
                        continue
                    rows.append((
                        now_str, underlying, expiry, strike_val, otype,
                        opt.get("last_price", 0),
                        opt.get("top_bid_price", 0),
                        opt.get("top_ask_price", 0),
                        opt.get("oi", 0),
                        opt.get("volume", 0),
                        opt.get("implied_volatility", 0),
                        spot,
                    ))

            if rows:
                conn.executemany(
                    "INSERT INTO strike_snapshots "
                    "(timestamp, underlying, expiry, strike, option_type, "
                    "ltp, bid, ask, oi, volume, iv, spot_price) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows)

            conn.commit()
            log.debug("Saved %d strike snapshots for %s (spot=%.0f)", len(rows), underlying, spot)
        except Exception as e:
            log.error("Chain snapshot save error: %s", e)
        finally:
            conn.close()

    def _loop(self):
        underlyings = ["NIFTY", "BANKNIFTY", "SENSEX"]
        while self._running:
            if self._is_market_hours():
                for ul in underlyings:
                    try:
                        self._save_snapshot(ul)
                    except Exception as e:
                        log.error("Snapshot error for %s: %s", ul, e)
                    time.sleep(2)
            time.sleep(self._interval)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="chain-collector")
        self._thread.start()
        log.info("Chain collector started (every %d min)", self._interval // 60)

    def stop(self):
        self._running = False

    def get_stats(self) -> dict:
        conn = sqlite3.connect(self._db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM strike_snapshots").fetchone()[0]
            days = conn.execute(
                "SELECT COUNT(DISTINCT date(timestamp)) FROM chain_snapshots"
            ).fetchone()[0]
            latest = conn.execute(
                "SELECT timestamp FROM chain_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "total_strike_records": total,
                "days_collected": days,
                "last_snapshot": latest[0] if latest else None,
            }
        finally:
            conn.close()
