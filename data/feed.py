"""
Historical data loader + live feed placeholder.

Supports:
  - CSV files (most common for backtesting)
  - Dhan historical API (for fresh pulls)
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)


def load_csv(filepath: str, date_col: str = "datetime",
             date_format: str = "%Y-%m-%d %H:%M:%S") -> list[dict]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {filepath}")

    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["_timestamp"] = datetime.strptime(row[date_col], date_format)
            rows.append(row)

    log.info("Loaded %d rows from %s", len(rows), filepath)
    return rows


def generate_sample_data(symbol: str = "BANKNIFTY", days: int = 30,
                         timeframe_min: int = 5, base_price: float = 50000.0,
                         output_path: str | None = None) -> str:
    """Generate synthetic OHLCV data for testing. NOT for strategy validation."""
    import random
    random.seed(42)

    rows = []
    price = base_price
    dt = datetime(2025, 1, 2, 9, 15)

    for day in range(days):
        current = dt + timedelta(days=day)
        if current.weekday() >= 5:
            continue

        t = current.replace(hour=9, minute=15)
        end = current.replace(hour=15, minute=30)
        daily_vol = random.uniform(0.005, 0.015)

        while t <= end:
            move = random.gauss(0, daily_vol * price / 100)
            o = round(price, 2)
            c = round(price + move, 2)
            h = round(max(o, c) + abs(random.gauss(0, price * 0.001)), 2)
            l = round(min(o, c) - abs(random.gauss(0, price * 0.001)), 2)
            v = random.randint(5000, 50000)

            rows.append({
                "datetime": t.strftime("%Y-%m-%d %H:%M:%S"),
                "open": o, "high": h, "low": l, "close": c,
                "volume": v,
            })
            price = c
            t += timedelta(minutes=timeframe_min)

    out = output_path or f"data/{symbol.lower()}_sample_{timeframe_min}min.csv"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["datetime", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)

    log.info("Generated %d sample candles → %s", len(rows), out)
    return out


class DhanHistorical:
    """Pull historical candle data from Dhan API."""

    BASE = "https://api.dhan.co/v2"

    def __init__(self, access_token: str, client_id: str = ""):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "access-token": access_token,
            "client-id": client_id or os.environ.get("DHAN_CLIENT_ID", ""),
        })

    def get_candles(self, security_id: str, exchange_segment: str,
                    instrument_type: str, from_date: str, to_date: str,
                    interval: str = "5") -> list[dict]:
        """
        interval: "1", "5", "15", "25", "60" (minutes) or "D" (daily)
        from_date / to_date: "YYYY-MM-DD"
        """
        payload = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument_type,
            "fromDate": from_date,
            "toDate": to_date,
        }

        endpoint = f"/charts/intraday" if interval != "D" else "/charts/historical"
        try:
            resp = self.session.post(f"{self.BASE}{endpoint}", json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("Dhan historical fetch failed: %s", exc)
            return []

        candles = []
        timestamps = data.get("timestamp", [])
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        volumes = data.get("volume", [])

        for i in range(len(timestamps)):
            candles.append({
                "datetime": datetime.fromtimestamp(timestamps[i]).strftime("%Y-%m-%d %H:%M:%S"),
                "open": opens[i],
                "high": highs[i],
                "low": lows[i],
                "close": closes[i],
                "volume": volumes[i],
            })

        log.info("Fetched %d candles for %s", len(candles), security_id)
        return candles

    def save_to_csv(self, candles: list[dict], output_path: str) -> None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["datetime", "open", "high", "low", "close", "volume"])
            writer.writeheader()
            writer.writerows(candles)
        log.info("Saved %d candles → %s", len(candles), output_path)
