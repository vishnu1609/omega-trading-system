"""
SQLite-backed ML buffer — durable signal + trade storage with counterfactual logging.

Replaces the JSON trade_buffer.json that lost data on crashes and only stored
executed trades (selective-label bias). Now stores:
  1. Every signal (executed AND blocked) with features
  2. Trade outcomes linked back to signals
  3. Counterfactual outcomes for blocked signals (what WOULD have happened)

Schema designed for fast LightGBM training queries.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

DB_DIR = Path(__file__).parent.parent / "data" / "ml"
DB_PATH = DB_DIR / "omega_ml.db"


class MLBuffer:
    """SQLite-backed ML training buffer."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()
        log.info("MLBuffer opened: %s (%d signals, %d trades)",
                 self._db_path, self.signal_count(), self.trade_count())

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                score REAL DEFAULT 0,
                confidence TEXT DEFAULT 'LOW',
                regime TEXT DEFAULT 'NORMAL',
                status TEXT NOT NULL,  -- EXECUTED, BLOCKED_SCORE, BLOCKED_CHOPPY, etc.
                features TEXT,         -- JSON array of feature values
                feature_names TEXT,    -- JSON array of feature names (for schema evolution)
                adx REAL DEFAULT 0,
                rsi REAL DEFAULT 50,
                choppiness REAL DEFAULT 50,
                atr_pct REAL DEFAULT 0,
                bb_position REAL DEFAULT 0.5,
                volume_ratio REAL DEFAULT 1.0,
                -- Outcome (filled later for EXECUTED signals, counterfactual for BLOCKED)
                outcome TEXT,          -- WIN, LOSS, PENDING, COUNTERFACTUAL_WIN, COUNTERFACTUAL_LOSS
                pnl REAL DEFAULT 0,
                r_multiple REAL DEFAULT 0,
                exit_reason TEXT,
                hold_seconds REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER REFERENCES signals(id),
                timestamp REAL NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                is_win INTEGER DEFAULT 0,
                regime TEXT DEFAULT 'NORMAL',
                features TEXT,
                exit_reason TEXT,
                hold_seconds REAL DEFAULT 0,
                r_multiple REAL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp);
            CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
            CREATE INDEX IF NOT EXISTS idx_signals_outcome ON signals(outcome);
            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
        """)
        self._conn.commit()

    def log_signal(
        self,
        symbol: str,
        side: str,
        score: float,
        confidence: str,
        regime: str,
        status: str,
        features: list[float] | None = None,
        feature_names: list[str] | None = None,
        adx: float = 0,
        rsi: float = 50,
        choppiness: float = 50,
        atr_pct: float = 0,
        bb_position: float = 0.5,
        volume_ratio: float = 1.0,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO signals
               (timestamp, symbol, side, score, confidence, regime, status,
                features, feature_names, adx, rsi, choppiness, atr_pct,
                bb_position, volume_ratio, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(), symbol, side, score, confidence, regime, status,
                json.dumps(features) if features else None,
                json.dumps(feature_names) if feature_names else None,
                adx, rsi, choppiness, atr_pct, bb_position, volume_ratio,
                "PENDING" if status == "EXECUTED" else None,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def log_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        regime: str,
        features: list[float] | None = None,
        signal_id: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO trades
               (signal_id, timestamp, symbol, direction, entry_price, regime, features)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id, time.time(), symbol, direction, entry_price,
                regime, json.dumps(features) if features else None,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        pnl: float,
        is_win: bool,
        exit_reason: str = "",
        hold_seconds: float = 0,
        r_multiple: float = 0,
    ) -> None:
        self._conn.execute(
            """UPDATE trades SET
               exit_price=?, pnl=?, is_win=?, exit_reason=?,
               hold_seconds=?, r_multiple=?
               WHERE id=?""",
            (exit_price, pnl, int(is_win), exit_reason, hold_seconds, r_multiple, trade_id),
        )
        # Also update the linked signal's outcome
        row = self._conn.execute(
            "SELECT signal_id FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        if row and row[0]:
            outcome = "WIN" if is_win else "LOSS"
            self._conn.execute(
                "UPDATE signals SET outcome=?, pnl=?, r_multiple=?, exit_reason=?, hold_seconds=? WHERE id=?",
                (outcome, pnl, r_multiple, exit_reason, hold_seconds, row[0]),
            )
        self._conn.commit()

    def log_counterfactual(
        self,
        signal_id: int,
        would_have_won: bool,
        counterfactual_pnl: float = 0,
        counterfactual_r: float = 0,
    ) -> None:
        """Update a blocked signal with what WOULD have happened."""
        outcome = "COUNTERFACTUAL_WIN" if would_have_won else "COUNTERFACTUAL_LOSS"
        self._conn.execute(
            "UPDATE signals SET outcome=?, pnl=?, r_multiple=? WHERE id=?",
            (outcome, counterfactual_pnl, counterfactual_r, signal_id),
        )
        self._conn.commit()

    def get_training_data(self, min_samples: int = 15, include_counterfactual: bool = True) -> tuple[list[list[float]], list[int]]:
        """Get feature vectors and labels for model training.

        Returns (X, y) where y=1 for WIN, y=0 for LOSS.
        Includes counterfactual outcomes if enabled (fixes selective-label bias).
        """
        outcomes = ["WIN", "LOSS"]
        if include_counterfactual:
            outcomes.extend(["COUNTERFACTUAL_WIN", "COUNTERFACTUAL_LOSS"])

        placeholders = ",".join("?" * len(outcomes))
        rows = self._conn.execute(
            f"""SELECT features, outcome FROM signals
                WHERE features IS NOT NULL AND outcome IN ({placeholders})
                ORDER BY timestamp DESC LIMIT 500""",
            outcomes,
        ).fetchall()

        if len(rows) < min_samples:
            return [], []

        X, y = [], []
        for feat_json, outcome in rows:
            try:
                feats = json.loads(feat_json)
                if feats and len(feats) > 0:
                    X.append(feats)
                    y.append(1 if outcome in ("WIN", "COUNTERFACTUAL_WIN") else 0)
            except (json.JSONDecodeError, TypeError):
                continue

        return X, y

    def get_trade_stats(self) -> dict:
        row = self._conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN is_win=1 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN is_win=0 AND exit_price>0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl) as total_pnl,
                AVG(CASE WHEN is_win=1 THEN pnl ELSE NULL END) as avg_win,
                AVG(CASE WHEN is_win=0 AND exit_price>0 THEN pnl ELSE NULL END) as avg_loss
            FROM trades WHERE exit_price > 0
        """).fetchone()
        return {
            "total": row[0] or 0,
            "wins": row[1] or 0,
            "losses": row[2] or 0,
            "total_pnl": round(row[3] or 0, 2),
            "avg_win": round(row[4] or 0, 2),
            "avg_loss": round(row[5] or 0, 2),
        }

    def get_pending_counterfactuals(self, max_age_seconds: float = 7200) -> list[dict]:
        """Get blocked signals that need counterfactual outcome resolution."""
        cutoff = time.time() - max_age_seconds
        rows = self._conn.execute(
            """SELECT id, symbol, side, score, timestamp, features
               FROM signals
               WHERE status != 'EXECUTED' AND outcome IS NULL
               AND timestamp > ? AND features IS NOT NULL
               ORDER BY timestamp DESC LIMIT 50""",
            (cutoff,),
        ).fetchall()
        return [
            {"id": r[0], "symbol": r[1], "side": r[2], "score": r[3],
             "timestamp": r[4], "features": json.loads(r[5]) if r[5] else None}
            for r in rows
        ]

    def signal_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    def trade_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    def recent_win_rate(self, n: int = 30) -> float:
        rows = self._conn.execute(
            "SELECT is_win FROM trades WHERE exit_price>0 ORDER BY timestamp DESC LIMIT ?",
            (n,),
        ).fetchall()
        if not rows:
            return 0.5
        return sum(r[0] for r in rows) / len(rows)

    def migrate_from_json(self, json_path: Path) -> int:
        """Import trades from the old JSON buffer."""
        if not json_path.exists():
            return 0
        try:
            trades = json.loads(json_path.read_text())
        except (json.JSONDecodeError, KeyError):
            return 0

        count = 0
        for t in trades:
            features = t.get("features")
            is_win = t.get("is_win", False)
            pnl = t.get("pnl", 0)
            symbol = t.get("symbol", "UNKNOWN")
            regime = t.get("regime", "NORMAL")
            ts = t.get("timestamp", time.time())

            sig_id = self._conn.execute(
                """INSERT INTO signals
                   (timestamp, symbol, side, score, confidence, regime, status,
                    features, outcome, pnl)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, symbol, "UNKNOWN", 0, "UNKNOWN", regime, "MIGRATED",
                 json.dumps(features) if features else None,
                 "WIN" if is_win else "LOSS", pnl),
            ).lastrowid

            self._conn.execute(
                """INSERT INTO trades
                   (signal_id, timestamp, symbol, direction, entry_price, pnl,
                    is_win, regime, features)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sig_id, ts, symbol, "LONG", 0, pnl, int(is_win), regime,
                 json.dumps(features) if features else None),
            )
            count += 1

        self._conn.commit()
        log.info("Migrated %d trades from JSON buffer", count)
        return count

    def close(self) -> None:
        self._conn.close()
