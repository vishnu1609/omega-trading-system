"""
SQLite Database — single-file persistent storage for all trading data.

Tables: wallet, trades, positions, signals, daily_stats
Thread-safe via WAL mode + Python threading lock.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DB_PATH = Path(__file__).parent.parent / "data" / "omega.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL DEFAULT 100000,
    in_trade REAL NOT NULL DEFAULT 0,
    today_pnl REAL NOT NULL DEFAULT 0,
    today_trades INTEGER NOT NULL DEFAULT 0,
    starting_capital REAL NOT NULL DEFAULT 100000,
    trade_date TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    option_type TEXT,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    pnl REAL NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT NOT NULL,
    exit_reason TEXT,
    strategy TEXT,
    balance_after REAL,
    signal_score REAL DEFAULT 0,
    signal_confidence TEXT DEFAULT 'LOW',
    volatility_regime TEXT DEFAULT 'NORMAL',
    adx_value REAL DEFAULT 0,
    rsi_value REAL DEFAULT 50,
    atr_value REAL DEFAULT 0,
    volume_ratio REAL DEFAULT 1.0,
    supertrend_dir TEXT DEFAULT '',
    vwap_position TEXT DEFAULT '',
    hold_minutes REAL DEFAULT 0,
    r_multiple REAL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    security_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange_segment TEXT NOT NULL,
    option_type TEXT,
    direction TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL DEFAULT 0,
    sl_price REAL DEFAULT 0,
    trail_sl REAL DEFAULT 0,
    tp_price REAL DEFAULT 0,
    peak_price REAL DEFAULT 0,
    entry_order_id TEXT,
    strategy TEXT,
    entry_time TEXT NOT NULL,
    dynamic_level INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT,
    score REAL DEFAULT 0,
    confidence TEXT,
    asset TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    total_pnl REAL DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    starting_balance REAL,
    ending_balance REAL,
    updated_at TEXT NOT NULL
);
"""


class OmegaDB:
    _instance: OmegaDB | None = None
    _lock = threading.Lock()

    def __init__(self, db_path: str | Path = DB_PATH):
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self._conn()
        conn.executescript(_SCHEMA)
        for alter in [
            "ALTER TABLE positions ADD COLUMN dynamic_level INTEGER DEFAULT 1",
            "ALTER TABLE trades ADD COLUMN signal_score REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN signal_confidence TEXT DEFAULT 'LOW'",
            "ALTER TABLE trades ADD COLUMN volatility_regime TEXT DEFAULT 'NORMAL'",
            "ALTER TABLE trades ADD COLUMN adx_value REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN rsi_value REAL DEFAULT 50",
            "ALTER TABLE trades ADD COLUMN atr_value REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN volume_ratio REAL DEFAULT 1.0",
            "ALTER TABLE trades ADD COLUMN supertrend_dir TEXT DEFAULT ''",
            "ALTER TABLE trades ADD COLUMN vwap_position TEXT DEFAULT ''",
            "ALTER TABLE trades ADD COLUMN hold_minutes REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN r_multiple REAL DEFAULT 0",
        ]:
            try:
                conn.execute(alter)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        log.info("Database ready: %s", self._db_path)

    @classmethod
    def get(cls, db_path: str | Path = DB_PATH) -> OmegaDB:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db_path)
        return cls._instance

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    # ── Wallet ──────────────────────────────────────────────────────────

    def get_wallet(self) -> dict:
        row = self._conn().execute("SELECT * FROM wallet WHERE id=1").fetchone()
        if not row:
            return {}
        return dict(row)

    def init_wallet(self, starting_capital: float = 100_000) -> dict:
        now = datetime.now(IST)
        conn = self._conn()
        existing = conn.execute("SELECT * FROM wallet WHERE id=1").fetchone()
        if existing:
            w = dict(existing)
            if w["trade_date"] != now.strftime("%Y-%m-%d"):
                conn.execute(
                    "UPDATE wallet SET today_pnl=0, today_trades=0, "
                    "trade_date=?, updated_at=? WHERE id=1",
                    (now.strftime("%Y-%m-%d"), now.isoformat()))
                conn.commit()
            return self.get_wallet()

        conn.execute(
            "INSERT INTO wallet (id, balance, in_trade, today_pnl, today_trades, "
            "starting_capital, trade_date, updated_at) VALUES (1,?,0,0,0,?,?,?)",
            (starting_capital, starting_capital,
             now.strftime("%Y-%m-%d"), now.isoformat()))
        conn.commit()
        return self.get_wallet()

    _WALLET_COLUMNS = frozenset({
        "balance", "in_trade", "today_pnl", "today_trades",
        "starting_capital", "trade_date", "updated_at",
    })

    def update_wallet(self, **kwargs) -> None:
        if not kwargs:
            return
        kwargs["updated_at"] = datetime.now(IST).isoformat()
        safe = {k: v for k, v in kwargs.items() if k in self._WALLET_COLUMNS}
        if not safe:
            return
        sets = ", ".join(f"{k}=?" for k in safe)
        vals = list(safe.values())
        self._conn().execute(f"UPDATE wallet SET {sets} WHERE id=1", vals)
        self._conn().commit()

    # ── Trades ──────────────────────────────────────────────────────────

    def record_trade(self, symbol: str, direction: str, option_type: str,
                     quantity: int, entry_price: float, exit_price: float,
                     pnl: float, entry_time: str, exit_time: str,
                     exit_reason: str, strategy: str,
                     balance_after: float, **kwargs) -> int:
        cur = self._conn().execute(
            "INSERT INTO trades (symbol, direction, option_type, quantity, "
            "entry_price, exit_price, pnl, entry_time, exit_time, exit_reason, "
            "strategy, balance_after, signal_score, signal_confidence, "
            "volatility_regime, adx_value, rsi_value, atr_value, volume_ratio, "
            "supertrend_dir, vwap_position, hold_minutes, r_multiple, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, direction, option_type, quantity, entry_price, exit_price,
             pnl, entry_time, exit_time, exit_reason, strategy, balance_after,
             kwargs.get("signal_score", 0), kwargs.get("signal_confidence", "LOW"),
             kwargs.get("volatility_regime", "NORMAL"), kwargs.get("adx_value", 0),
             kwargs.get("rsi_value", 50), kwargs.get("atr_value", 0),
             kwargs.get("volume_ratio", 1.0), kwargs.get("supertrend_dir", ""),
             kwargs.get("vwap_position", ""), kwargs.get("hold_minutes", 0),
             kwargs.get("r_multiple", 0),
             datetime.now(IST).isoformat()))
        self._conn().commit()
        return cur.lastrowid

    def get_trades(self, limit: int = 50, date: str = "") -> list[dict]:
        q = "SELECT * FROM trades"
        params: list = []
        if date:
            q += " WHERE date(created_at)=?"
            params.append(date)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_trade_stats(self, date: str = "") -> dict:
        q = """SELECT COUNT(*) as total,
                   SUM(CASE WHEN pnl >= 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                   COALESCE(SUM(pnl), 0) as total_pnl,
                   COALESCE(AVG(CASE WHEN pnl >= 0 THEN pnl END), 0) as avg_win,
                   COALESCE(AVG(CASE WHEN pnl < 0 THEN ABS(pnl) END), 0) as avg_loss
            FROM trades"""
        params: list = []
        if date:
            q += " WHERE date(created_at)=?"
            params.append(date)
        row = self._conn().execute(q, params).fetchone()
        return dict(row) if row else {}

    # ── Positions ──────────────────────────────────────────────────────

    _POSITION_COLUMNS = frozenset({
        "security_id", "symbol", "exchange_segment", "option_type",
        "direction", "quantity", "entry_price", "current_price",
        "sl_price", "trail_sl", "tp_price", "peak_price",
        "entry_order_id", "strategy", "entry_time", "dynamic_level",
    })

    def upsert_position(self, security_id: str, **data) -> None:
        data["security_id"] = security_id
        data = {k: v for k, v in data.items() if k in self._POSITION_COLUMNS}
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != "security_id")
        self._conn().execute(
            f"INSERT INTO positions ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(security_id) DO UPDATE SET {updates}",
            list(data.values()))
        self._conn().commit()

    def get_positions(self) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM positions").fetchall()
        return [dict(r) for r in rows]

    def delete_position(self, security_id: str) -> None:
        self._conn().execute(
            "DELETE FROM positions WHERE security_id=?", (security_id,))
        self._conn().commit()

    def clear_positions(self) -> None:
        self._conn().execute("DELETE FROM positions")
        self._conn().commit()

    def update_position_price(self, security_id: str,
                              current_price: float) -> None:
        self._conn().execute(
            "UPDATE positions SET current_price=? WHERE security_id=?",
            (current_price, security_id))
        self._conn().commit()

    # ── Signals ────────────────────────────────────────────────────────

    def record_signal(self, symbol: str, side: str, strategy: str = "",
                      score: float = 0, confidence: str = "",
                      asset: str = "", details: dict | None = None) -> int:
        cur = self._conn().execute(
            "INSERT INTO signals (symbol, side, strategy, score, confidence, "
            "asset, details, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (symbol, side, strategy, score, confidence, asset,
             json.dumps(details or {}),
             datetime.now(IST).isoformat()))
        self._conn().commit()
        return cur.lastrowid

    def get_signals(self, limit: int = 50) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── Daily Stats ────────────────────────────────────────────────────

    def update_daily_stats(self, date: str, total_pnl: float,
                           total_trades: int, wins: int, losses: int,
                           starting_balance: float,
                           ending_balance: float) -> None:
        self._conn().execute(
            "INSERT INTO daily_stats (date, total_pnl, total_trades, wins, "
            "losses, starting_balance, ending_balance, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET total_pnl=?, total_trades=?, "
            "wins=?, losses=?, ending_balance=?, updated_at=?",
            (date, total_pnl, total_trades, wins, losses, starting_balance,
             ending_balance, datetime.now(IST).isoformat(),
             total_pnl, total_trades, wins, losses, ending_balance,
             datetime.now(IST).isoformat()))
        self._conn().commit()

    def get_daily_stats(self, limit: int = 30) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM daily_stats ORDER BY date DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── API endpoints (for dashboard) ──────────────────────────────────

    def get_dashboard_data(self) -> dict:
        wallet = self.get_wallet()
        positions = self.get_positions()
        today = datetime.now(IST).strftime("%Y-%m-%d")
        trades = self.get_trades(limit=20, date=today)
        all_trades = self.get_trades(limit=50)
        signals = self.get_signals(limit=15)
        stats = self.get_trade_stats()
        daily = self.get_daily_stats(limit=7)
        return {
            "wallet": wallet,
            "positions": positions,
            "trades_today": trades,
            "trades_all": all_trades,
            "signals": signals,
            "stats": stats,
            "daily_stats": daily,
        }
