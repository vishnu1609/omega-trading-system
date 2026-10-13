"""
Market Data Collector — Builds Training Datasets for ML Models

Collects 5-minute OHLCV candles from Yahoo Finance, computes 44 features
per candle, and stores them alongside trade outcomes for supervised learning.

Usage:
    collector = DataCollector()
    collector.collect_daily("NIFTY")   # stores to data/ml/NIFTY_features.csv
    collector.label_with_trades()       # labels candles near trade entries with win/loss
"""
from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from ml.features import Candle, extract_features, FEATURE_NAMES

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = Path(__file__).parent.parent / "data" / "ml"
MEMORY_FILE = Path(__file__).parent.parent / "data" / "omega_memory.json"


class DataCollector:

    def __init__(self, data_dir: str | Path = DATA_DIR):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def collect_daily(self, symbol: str, days: int = 30) -> int:
        try:
            import yfinance as yf
        except ImportError:
            log.error("yfinance not installed — run: pip install yfinance")
            return 0

        yahoo_map = {
            "NIFTY": "^NSEI",
            "BANKNIFTY": "^NSEBANK",
            "SENSEX": "^BSESN",
        }
        ticker = yahoo_map.get(symbol.upper(), f"^{symbol.upper()}")

        log.info("Collecting %d days of 5-min candles for %s (%s)", days, symbol, ticker)

        end = datetime.now(IST)
        start = end - timedelta(days=days)

        try:
            df = yf.download(ticker, start=start, end=end, interval="5m", progress=False)
        except Exception as e:
            log.error("Yahoo download failed: %s", e)
            return 0

        if df.empty:
            log.warning("No data returned for %s", symbol)
            return 0

        if hasattr(df.columns, 'levels') and len(df.columns.levels) > 1:
            df.columns = df.columns.get_level_values(0)

        candles: list[Candle] = []
        for idx, row in df.iterrows():
            ts = idx.timestamp() if hasattr(idx, 'timestamp') else time.time()
            candles.append(Candle(
                timestamp=ts,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row.get("Volume", 0)),
            ))

        log.info("Downloaded %d candles, extracting features...", len(candles))

        out_path = self._dir / f"{symbol.upper()}_features.csv"
        existing_timestamps = set()

        if out_path.exists():
            with open(out_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    existing_timestamps.add(row.get("timestamp", ""))

        header = ["timestamp", "date", "open", "high", "low", "close", "volume"] + FEATURE_NAMES + ["label"]
        is_new = not out_path.exists() or out_path.stat().st_size == 0
        wrote = 0

        with open(out_path, "a", newline="") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(header)

            for i in range(50, len(candles)):
                c = candles[i]
                ts_str = str(c.timestamp)
                if ts_str in existing_timestamps:
                    continue

                features = extract_features(candles[:i + 1], lookback=50)
                if not features:
                    continue

                dt = datetime.fromtimestamp(c.timestamp, tz=IST)
                row = [
                    ts_str,
                    dt.strftime("%Y-%m-%d %H:%M"),
                    f"{c.open:.2f}",
                    f"{c.high:.2f}",
                    f"{c.low:.2f}",
                    f"{c.close:.2f}",
                    f"{c.volume:.0f}",
                ]
                for feat in FEATURE_NAMES:
                    val = features.get(feat, 0.0)
                    row.append(f"{val:.6f}" if isinstance(val, float) else str(val))
                row.append("")  # empty label, filled later
                writer.writerow(row)
                wrote += 1

        log.info("Wrote %d feature rows to %s (total file rows: %d)",
                 wrote, out_path, len(existing_timestamps) + wrote)
        return wrote

    def label_with_trades(self, symbol: str = "") -> int:
        if not MEMORY_FILE.exists():
            log.warning("No trade memory file found")
            return 0

        with open(MEMORY_FILE) as f:
            memory = json.load(f)

        trades = memory.get("trades", [])
        if not trades:
            log.warning("No trades in memory")
            return 0

        trade_labels: dict[str, list[dict]] = {}
        for t in trades:
            base = ""
            for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
                if sym in t.get("symbol", "").upper():
                    base = sym
                    break
            if not base:
                continue
            if symbol and base != symbol.upper():
                continue
            trade_labels.setdefault(base, []).append(t)

        labeled = 0
        for sym, sym_trades in trade_labels.items():
            csv_path = self._dir / f"{sym}_features.csv"
            if not csv_path.exists():
                continue

            rows = []
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                rows = list(reader)

            entry_times = {}
            for t in sym_trades:
                entry_str = t.get("entry_time", "")
                if entry_str:
                    try:
                        dt = datetime.fromisoformat(entry_str)
                        entry_key = dt.strftime("%Y-%m-%d %H:%M")
                        window_keys = []
                        for offset in range(-2, 3):
                            adj = dt + timedelta(minutes=offset * 5)
                            window_keys.append(adj.strftime("%Y-%m-%d %H:%M"))
                        label = "WIN" if t.get("is_win", False) else "LOSS"
                        for key in window_keys:
                            entry_times[key] = label
                    except (ValueError, TypeError):
                        continue

            for row in rows:
                date_str = row.get("date", "")
                if date_str in entry_times and not row.get("label"):
                    row["label"] = entry_times[date_str]
                    labeled += 1

            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

        log.info("Labeled %d candle rows with trade outcomes", labeled)
        return labeled

    def get_training_data(self, symbol: str = "") -> tuple[np.ndarray, np.ndarray]:
        X_list, y_list = [], []

        pattern = f"{symbol.upper()}_features.csv" if symbol else "*_features.csv"
        for csv_path in self._dir.glob(pattern):
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    label = row.get("label", "").strip()
                    if label not in ("WIN", "LOSS"):
                        continue
                    features = []
                    for feat in FEATURE_NAMES:
                        try:
                            features.append(float(row.get(feat, 0)))
                        except (ValueError, TypeError):
                            features.append(0.0)
                    X_list.append(features)
                    y_list.append(1.0 if label == "WIN" else 0.0)

        if not X_list:
            return np.array([]), np.array([])

        return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=np.float32)

    def get_stats(self) -> dict:
        stats = {"files": [], "total_rows": 0, "labeled_rows": 0, "win": 0, "loss": 0}
        for csv_path in self._dir.glob("*_features.csv"):
            total = 0
            labeled = 0
            wins = 0
            losses = 0
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    total += 1
                    label = row.get("label", "").strip()
                    if label == "WIN":
                        labeled += 1
                        wins += 1
                    elif label == "LOSS":
                        labeled += 1
                        losses += 1
            stats["files"].append({
                "name": csv_path.name,
                "total": total,
                "labeled": labeled,
                "wins": wins,
                "losses": losses,
            })
            stats["total_rows"] += total
            stats["labeled_rows"] += labeled
            stats["win"] += wins
            stats["loss"] += losses
        return stats
