"""
Train ML model from historical candle data + trade memory.

Fetches NIFTY/BANKNIFTY candles via Dhan API, generates synthetic
training labels (profitable vs unprofitable entries), and trains
the signal classifier with expanded dataset.
"""
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from ml.features import Candle, extract_features, features_to_array, FEATURE_NAMES
from ml.neural_net import SignalClassifier
from ml.regime import RegimeDetector

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DHAN_CHART_URL = "https://api.dhan.co/v2/charts/intraday"
DHAN_INDEX_MAP = {
    "NIFTY": ("13", "IDX_I", "INDEX"),
    "BANKNIFTY": ("25", "IDX_I", "INDEX"),
    "SENSEX": ("51", "IDX_I", "INDEX"),
}

DATA_DIR = Path(__file__).parent.parent / "data"


def load_env():
    env_path = DATA_DIR.parent / ".env"
    if not env_path.exists():
        env_path = Path(__file__).parent.parent / ".env"
    vals = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def fetch_candles(security_id: str, exchange_segment: str, instrument: str,
                  access_token: str, days_back: int = 5) -> list[Candle]:
    headers = {
        "access-token": access_token,
        "Content-Type": "application/json",
    }
    to_date = datetime.now(IST)
    from_date = to_date - timedelta(days=days_back)
    body = {
        "securityId": security_id,
        "exchangeSegment": exchange_segment,
        "instrument": instrument,
        "fromDate": from_date.strftime("%Y-%m-%d"),
        "toDate": to_date.strftime("%Y-%m-%d"),
    }
    resp = requests.post(DHAN_CHART_URL, json=body, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"  API error {resp.status_code}: {resp.text[:200]}")
        return []

    data = resp.json()
    timestamps = data.get("timestamp", [])
    opens = data.get("open", [])
    highs = data.get("high", [])
    lows = data.get("low", [])
    closes = data.get("close", [])
    volumes = data.get("volume", [])

    candles = []
    for i in range(len(timestamps)):
        candles.append(Candle(
            timestamp=timestamps[i],
            open=opens[i],
            high=highs[i],
            low=lows[i],
            close=closes[i],
            volume=volumes[i] if i < len(volumes) else 0,
        ))
    return candles


def generate_labels(candles: list[Candle], forward_bars: int = 20,
                    win_pct: float = 0.8, loss_pct: float = 0.5) -> list[tuple[dict, bool]]:
    """Generate training samples with labels based on forward price movement."""
    samples = []
    for i in range(50, len(candles) - forward_bars):
        window = candles[max(0, i - 49):i + 1]
        features = extract_features(window, lookback=50)
        if not features:
            continue

        entry = candles[i].close
        future = candles[i + 1:i + 1 + forward_bars]

        max_up = max((c.high - entry) / entry * 100 for c in future) if future else 0
        max_down = max((entry - c.low) / entry * 100 for c in future) if future else 0

        if max_up >= win_pct and max_up > max_down:
            is_win = True
        elif max_down >= loss_pct:
            is_win = False
        else:
            continue

        samples.append((features, is_win))
    return samples


def load_trade_memory_samples() -> list[tuple[dict, bool]]:
    """Load the 56 trades from memory and convert to feature-label pairs."""
    mem_path = DATA_DIR / "omega_memory.json"
    if not mem_path.exists():
        return []

    with open(mem_path) as f:
        mem = json.load(f)

    trades = mem.get("trades", [])
    samples = []
    for t in trades:
        features = {
            "adx_14": t.get("adx_value", 20),
            "rsi_14": 50,
            "volume_ratio": t.get("volume_ratio", 1.0),
            "atr_pct": 1.0,
            "bb_position": 0.5,
            "choppiness": 50,
            "minutes_from_open": max(0, (t.get("entry_hour", 10) - 9) * 60),
            "is_first_hour": 1.0 if t.get("entry_hour", 10) == 9 else 0.0,
            "is_last_hour": 1.0 if t.get("entry_hour", 10) >= 15 else 0.0,
            "day_of_week": float(t.get("entry_day", 1)),
            "ema_cross": 1.0 if t.get("supertrend_dir") == "BULLISH" else -1.0,
            "trend_strength": 0.0,
            "supertrend_dir": 1.0 if t.get("supertrend_dir") == "BULLISH" else -1.0,
        }
        for name in FEATURE_NAMES:
            if name not in features:
                features[name] = 0.0

        samples.append((features, t.get("is_win", False)))
    return samples


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    env = load_env()
    token = env.get("DHAN_ACCESS_TOKEN", os.environ.get("DHAN_ACCESS_TOKEN", ""))
    if not token:
        print("ERROR: No DHAN_ACCESS_TOKEN found in .env")
        return

    all_samples = []

    # 1. Fetch candle data for each index
    for name, (sec_id, exch, inst) in DHAN_INDEX_MAP.items():
        print(f"\nFetching {name} candles...")
        candles = fetch_candles(sec_id, exch, inst, token)
        if not candles:
            print(f"  No candles for {name}")
            continue
        print(f"  Got {len(candles)} candles")

        # Generate synthetic labels
        samples = generate_labels(candles, forward_bars=15, win_pct=0.8, loss_pct=0.5)
        print(f"  Generated {len(samples)} labeled samples")
        wins = sum(1 for _, w in samples if w)
        print(f"  Wins: {wins}, Losses: {len(samples) - wins}")
        all_samples.extend(samples)

    # 2. Add trade memory samples (weighted 3x — real data is more valuable)
    mem_samples = load_trade_memory_samples()
    print(f"\nTrade memory: {len(mem_samples)} samples (weighted 3x)")
    for _ in range(3):
        all_samples.extend(mem_samples)

    if len(all_samples) < 50:
        print(f"\nOnly {len(all_samples)} samples — need at least 50. Aborting.")
        return

    # 3. Convert to arrays
    X_list = []
    y_list = []
    for features, is_win in all_samples:
        arr = features_to_array(features, FEATURE_NAMES)
        X_list.append(arr)
        y_list.append(1.0 if is_win else 0.0)

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.float32)

    wins = int(y.sum())
    losses = len(y) - wins
    print(f"\nTotal training set: {len(y)} samples ({wins}W / {losses}L)")

    # 4. Train classifier
    print("\nTraining signal classifier...")
    classifier = SignalClassifier(num_features=X.shape[1])
    cost_history = classifier.train(
        X, y,
        regularization=0.3,
        alpha=0.05,
        max_iterations=5000,
        early_stop_patience=200,
    )

    if cost_history:
        print(f"  Final cost: {cost_history[-1]:.4f} (iterations: {len(cost_history)})")

        # Accuracy
        predictions = classifier.predict(X)
        pred_labels = np.argmax(predictions, axis=1)
        accuracy = np.mean(pred_labels == y.astype(int)) * 100
        print(f"  Training accuracy: {accuracy:.1f}%")

        save_path = classifier.save()
        print(f"  Model saved to {save_path}")

    # 5. Train regime detector
    print("\nTraining regime detector...")
    feature_dicts = [s[0] for s in all_samples[:500]]
    regime = RegimeDetector()
    regime.train(feature_dicts)
    regime.save()
    print("  Regime detector saved")

    print("\n✓ ML training complete!")


if __name__ == "__main__":
    main()
