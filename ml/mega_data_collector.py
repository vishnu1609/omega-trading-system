"""
Mega Data Collector — Pull 10K+ records daily for ML training.

Sources:
  1. Yahoo Finance: NIFTY, BANKNIFTY, SENSEX 5-min candles (60 days)
  2. Dhan API: Intraday candles for all 3 indices (5 days)
  3. Synthetic augmentation: time-shifted, noise-injected variations

Generates labeled training samples using forward-return analysis:
  - WIN: price moves +X% within N bars
  - LOSS: price moves -Y% within N bars
  - Multiple timeframes (5-bar, 10-bar, 20-bar) for richer labels

Output: 10K+ feature-label pairs for training.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from ml.features import Candle, extract_features, features_to_array, FEATURE_NAMES

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = Path(__file__).parent.parent / "data" / "ml"

# Yahoo Finance tickers for Indian indices
YAHOO_TICKERS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCAP": "^NSEMDCP50",
}

DHAN_INDEX_MAP = {
    "NIFTY": ("13", "IDX_I", "INDEX"),
    "BANKNIFTY": ("25", "IDX_I", "INDEX"),
    "SENSEX": ("51", "IDX_I", "INDEX"),
}

DHAN_CHART_URL = "https://api.dhan.co/v2/charts/intraday"
DHAN_HISTORICAL_URL = "https://api.dhan.co/v2/charts/historical"


def load_env() -> dict:
    env_path = Path(__file__).parent.parent / ".env"
    vals = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def fetch_yahoo_candles(ticker: str, period: str = "60d",
                        interval: str = "5m") -> list[Candle]:
    """Fetch candles from Yahoo Finance."""
    try:
        import yfinance as yf
        data = yf.download(ticker, period=period, interval=interval,
                           progress=False, auto_adjust=True)
        if data.empty:
            return []

        candles = []
        for idx, row in data.iterrows():
            ts = idx.timestamp() if hasattr(idx, 'timestamp') else time.time()
            o = float(row["Open"].iloc[0]) if hasattr(row["Open"], 'iloc') else float(row["Open"])
            h = float(row["High"].iloc[0]) if hasattr(row["High"], 'iloc') else float(row["High"])
            l = float(row["Low"].iloc[0]) if hasattr(row["Low"], 'iloc') else float(row["Low"])
            c = float(row["Close"].iloc[0]) if hasattr(row["Close"], 'iloc') else float(row["Close"])
            v = float(row["Volume"].iloc[0]) if hasattr(row["Volume"], 'iloc') else float(row["Volume"])
            if o > 0 and h > 0 and l > 0 and c > 0:
                candles.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v))
        return candles
    except Exception as e:
        log.warning("Yahoo fetch failed for %s: %s", ticker, e)
        return []


def fetch_dhan_candles(security_id: str, exchange_segment: str,
                       instrument: str, access_token: str,
                       days_back: int = 5) -> list[Candle]:
    """Fetch intraday candles from Dhan API."""
    import requests
    headers = {"access-token": access_token, "Content-Type": "application/json"}
    to_date = datetime.now(IST)
    from_date = to_date - timedelta(days=days_back)
    body = {
        "securityId": security_id,
        "exchangeSegment": exchange_segment,
        "instrument": instrument,
        "fromDate": from_date.strftime("%Y-%m-%d"),
        "toDate": to_date.strftime("%Y-%m-%d"),
    }
    try:
        resp = requests.post(DHAN_CHART_URL, json=body, headers=headers, timeout=30)
        if resp.status_code != 200:
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
                timestamp=timestamps[i], open=opens[i], high=highs[i],
                low=lows[i], close=closes[i],
                volume=volumes[i] if i < len(volumes) else 0,
            ))
        return candles
    except Exception as e:
        log.warning("Dhan fetch failed: %s", e)
        return []


def generate_multi_timeframe_labels(
    candles: list[Candle],
    forward_windows: list[int] = [5, 10, 20],
    win_thresholds: list[float] = [0.5, 0.7, 0.9],
    loss_threshold: float = 0.4,
) -> list[tuple[dict, bool]]:
    """Generate labels using multiple forward-looking windows.

    For each candle position, checks all timeframes:
    - If price hits win threshold in ANY timeframe → WIN
    - If price hits loss threshold FIRST → LOSS
    - Ambiguous cases skipped for cleaner labels.
    """
    samples = []
    max_forward = max(forward_windows)

    for i in range(50, len(candles) - max_forward):
        window = candles[max(0, i - 49):i + 1]
        features = extract_features(window, lookback=50)
        if not features:
            continue

        entry = candles[i].close
        if entry <= 0:
            continue

        # Check each forward window
        wins = 0
        losses = 0
        best_upside = 0
        worst_downside = 0

        for fw, wt in zip(forward_windows, win_thresholds):
            future = candles[i + 1:i + 1 + fw]
            if not future:
                continue

            max_up = max((c.high - entry) / entry * 100 for c in future)
            max_down = max((entry - c.low) / entry * 100 for c in future)
            best_upside = max(best_upside, max_up)
            worst_downside = max(worst_downside, max_down)

            if max_up >= wt and max_up > max_down * 1.2:
                wins += 1
            elif max_down >= loss_threshold:
                losses += 1

        # Majority voting across timeframes
        if wins >= 2:
            samples.append((features, True))
        elif losses >= 2:
            samples.append((features, False))
        elif wins == 1 and losses == 0 and best_upside > 0.3:
            samples.append((features, True))
        elif losses == 1 and wins == 0 and worst_downside > 0.3:
            samples.append((features, False))

    return samples


def augment_samples(
    samples: list[tuple[dict, bool]],
    noise_factor: float = 0.02,
    augment_count: int = 2,
) -> list[tuple[dict, bool]]:
    """Create augmented samples with small noise perturbations."""
    augmented = []
    for item in samples:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        features, label = item
        if not isinstance(features, dict):
            continue
        for _ in range(augment_count):
            noisy = {}
            for k, v in features.items():
                if isinstance(v, (int, float)) and abs(v) > 0.001:
                    noise = random.gauss(0, abs(v) * noise_factor)
                    noisy[k] = v + noise
                else:
                    noisy[k] = v
            augmented.append((noisy, label))
    return augmented


def generate_regime_aware_labels(
    candles: list[Candle],
) -> list[tuple[dict, bool]]:
    """Generate labels that account for market regime.

    Uses different win/loss thresholds for trending vs ranging markets:
    - Trending: larger win targets, wider SL
    - Ranging: tighter win targets, tighter SL
    """
    samples = []
    for i in range(50, len(candles) - 25):
        window = candles[max(0, i - 49):i + 1]
        features = extract_features(window, lookback=50)
        if not features:
            continue

        entry = candles[i].close
        if entry <= 0:
            continue

        adx = features.get("adx_14", 20)
        chop = features.get("choppiness", 50)

        # Regime-adaptive thresholds
        if adx > 25 and chop < 50:
            # Trending — larger targets
            win_pct, loss_pct, fw = 1.0, 0.6, 20
        elif adx < 18 and chop > 60:
            # Choppy — tight targets
            win_pct, loss_pct, fw = 0.4, 0.3, 8
        else:
            # Normal
            win_pct, loss_pct, fw = 0.7, 0.5, 15

        future = candles[i + 1:i + 1 + fw]
        if len(future) < fw // 2:
            continue

        max_up = max((c.high - entry) / entry * 100 for c in future)
        max_down = max((entry - c.low) / entry * 100 for c in future)

        if max_up >= win_pct and max_up > max_down:
            samples.append((features, True))
        elif max_down >= loss_pct and max_down > max_up:
            samples.append((features, False))

    return samples


def collect_all_data(target_samples: int = 10000) -> list[tuple[dict, bool]]:
    """Main collection pipeline — pulls from all sources, targets 10K+ samples."""
    env = load_env()
    token = env.get("DHAN_ACCESS_TOKEN", os.environ.get("DHAN_ACCESS_TOKEN", ""))

    all_samples: list[tuple[dict, bool]] = []

    # === Source 1: Yahoo Finance (bulk historical) ===
    print("\n=== Yahoo Finance Historical Data ===")
    for name, ticker in YAHOO_TICKERS.items():
        print(f"\nFetching {name} ({ticker})...")
        candles = fetch_yahoo_candles(ticker, period="60d", interval="5m")
        if not candles:
            # Try 1-hour for longer history
            candles = fetch_yahoo_candles(ticker, period="2y", interval="1h")
        if not candles:
            print(f"  No data for {name}")
            continue
        print(f"  Got {len(candles)} candles")

        # Multi-timeframe labels
        mtf_samples = generate_multi_timeframe_labels(candles)
        print(f"  Multi-TF samples: {len(mtf_samples)}")
        all_samples.extend(mtf_samples)

        # Regime-aware labels
        regime_samples = generate_regime_aware_labels(candles)
        print(f"  Regime-aware samples: {len(regime_samples)}")
        all_samples.extend(regime_samples)

    # === Source 2: Dhan API (recent high-res data) ===
    if token:
        print("\n=== Dhan API Intraday Data ===")
        for name, (sec_id, exch, inst) in DHAN_INDEX_MAP.items():
            print(f"\nFetching {name} from Dhan...")
            candles = fetch_dhan_candles(sec_id, exch, inst, token, days_back=5)
            if not candles:
                print(f"  No data for {name}")
                continue
            print(f"  Got {len(candles)} candles")

            samples = generate_multi_timeframe_labels(candles)
            print(f"  Samples: {len(samples)}")
            # Weight recent Dhan data 2x (more relevant)
            all_samples.extend(samples)
            all_samples.extend(samples)

    # === Source 3: Trade memory (real trades, highest value) ===
    mem_path = Path(__file__).parent.parent / "data" / "omega_memory.json"
    if mem_path.exists():
        print("\n=== Trade Memory ===")
        with open(mem_path) as f:
            mem = json.load(f)
        trades = mem.get("trades", [])
        mem_samples = []
        for t in trades:
            features = {name: 0.0 for name in FEATURE_NAMES}
            features["adx_14"] = t.get("adx_value", 20)
            features["rsi_14"] = t.get("rsi_value", 50)
            features["volume_ratio"] = t.get("volume_ratio", 1.0)
            features["atr_pct"] = t.get("atr_pct", 1.0)
            features["bb_position"] = 0.5
            features["choppiness"] = 50
            features["minutes_from_open"] = max(0, (t.get("entry_hour", 10) - 9) * 60)
            features["is_first_hour"] = 1.0 if t.get("entry_hour", 10) == 9 else 0.0
            features["is_last_hour"] = 1.0 if t.get("entry_hour", 10) >= 15 else 0.0
            features["day_of_week"] = float(t.get("entry_day", 1))
            features["ema_cross"] = 1.0 if t.get("supertrend_dir") == "BULLISH" else -1.0
            features["supertrend_dir"] = 1.0 if t.get("supertrend_dir") == "BULLISH" else -1.0
            mem_samples.append((features, t.get("is_win", False)))
        # Real trades weighted 5x
        for _ in range(5):
            all_samples.extend(mem_samples)
        print(f"  Trade memory: {len(trades)} trades (weighted 5x = {len(trades) * 5})")

    # === Source 4: Existing trade buffer ===
    buffer_path = DATA_DIR / "trade_buffer.json"
    if buffer_path.exists():
        print("\n=== Existing Trade Buffer ===")
        with open(buffer_path) as f:
            buffer = json.load(f)
        buf_count = 0
        for entry in buffer:
            features = entry.get("features", {})
            is_win = entry.get("is_win", entry.get("label", 0))
            if isinstance(is_win, (int, float)):
                is_win = bool(is_win)
            # Convert list features to dict — old buffer may have 44 features, current has 50
            if isinstance(features, list):
                feat_dict = {}
                for i, val in enumerate(features):
                    if i < len(FEATURE_NAMES):
                        feat_dict[FEATURE_NAMES[i]] = float(val)
                # Fill any missing features with 0
                for name in FEATURE_NAMES:
                    if name not in feat_dict:
                        feat_dict[name] = 0.0
                features = feat_dict
            if isinstance(features, dict) and features:
                all_samples.append((features, is_win))
                buf_count += 1
        print(f"  Buffer entries: {buf_count}")

    print(f"\n=== Raw samples collected: {len(all_samples)} ===")

    # === Augmentation to reach target ===
    if len(all_samples) < target_samples:
        shortfall = target_samples - len(all_samples)
        aug_count = max(1, shortfall // len(all_samples)) if all_samples else 0
        if aug_count > 0:
            print(f"\nAugmenting {aug_count}x to reach {target_samples} target...")
            augmented = augment_samples(all_samples, noise_factor=0.015, augment_count=aug_count)
            all_samples.extend(augmented)

    # Balance classes
    win_samples = [(f, l) for f, l in all_samples if l]
    loss_samples = [(f, l) for f, l in all_samples if not l]
    print(f"\nBefore balancing: {len(win_samples)} wins, {len(loss_samples)} losses")

    # Oversample minority class
    if len(win_samples) > 0 and len(loss_samples) > 0:
        if len(win_samples) < len(loss_samples):
            ratio = len(loss_samples) / len(win_samples)
            extra = augment_samples(win_samples, noise_factor=0.02, augment_count=max(1, int(ratio) - 1))
            all_samples.extend(extra)
        elif len(loss_samples) < len(win_samples):
            ratio = len(win_samples) / len(loss_samples)
            extra = augment_samples(loss_samples, noise_factor=0.02, augment_count=max(1, int(ratio) - 1))
            all_samples.extend(extra)

    # Shuffle
    random.shuffle(all_samples)

    wins = sum(1 for _, w in all_samples if w)
    losses = len(all_samples) - wins
    print(f"Final dataset: {len(all_samples)} samples ({wins}W / {losses}L)")

    return all_samples


def save_dataset(samples: list[tuple[dict, bool]], path: Path | None = None) -> Path:
    """Save collected dataset for training."""
    path = path or DATA_DIR / "mega_dataset.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    data = []
    for features, is_win in samples:
        data.append({
            "features": features,
            "label": 1 if is_win else 0,
        })

    path.write_text(json.dumps(data))
    print(f"Dataset saved: {path} ({len(data)} samples, {path.stat().st_size / 1024:.0f} KB)")
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    samples = collect_all_data(target_samples=10000)
    save_dataset(samples)
