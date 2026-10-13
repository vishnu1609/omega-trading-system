#!/usr/bin/env python3
"""
Omega ML Training Script

Collects market data, extracts features, labels with trade outcomes,
and trains both the Signal Classifier and Regime Detector.

Usage:
    python3 -m ml.train                      # full pipeline
    python3 -m ml.train --collect-only        # just download candles
    python3 -m ml.train --train-only          # train on existing data
    python3 -m ml.train --stats               # show dataset stats
    python3 -m ml.train --from-memory         # train from trade memory buffer
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from ml.features import FEATURE_NAMES
from ml.neural_net import SignalClassifier
from ml.regime import RegimeDetector, REGIME_NAMES
from ml.data_collector import DataCollector
from ml.engine import OmegaMLEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ml.train")

DATA_DIR = Path(__file__).parent.parent / "data" / "ml"
BUFFER_PATH = DATA_DIR / "trade_buffer.json"


def collect_data(symbols: list[str], days: int = 30) -> None:
    collector = DataCollector()
    for sym in symbols:
        log.info("=== Collecting %s ===", sym)
        count = collector.collect_daily(sym, days=days)
        log.info("Collected %d feature rows for %s", count, sym)

    labeled = collector.label_with_trades()
    log.info("Labeled %d rows with trade outcomes", labeled)


def train_from_csv() -> None:
    collector = DataCollector()
    X, y = collector.get_training_data()

    if len(X) == 0:
        log.warning("No labeled training data found. Run --collect-only first, or use --from-memory")
        return

    wins = int(y.sum())
    losses = len(y) - wins
    log.info("Training data: %d samples (%d W / %d L, %.1f%% win rate)",
             len(y), wins, losses, 100 * wins / len(y))

    log.info("--- Training Signal Classifier ---")
    classifier = SignalClassifier(num_features=X.shape[1])
    cost_history = classifier.train(
        X, y,
        regularization=0.3,
        alpha=0.1,
        max_iterations=3000,
        early_stop_patience=100,
    )

    if cost_history:
        classifier.save()
        preds = classifier.predict(X)
        pred_labels = (preds[:, 1] > 0.5).astype(int)
        accuracy = np.mean(pred_labels == y.astype(int))
        log.info("Classifier saved — accuracy: %.1f%%, final cost: %.4f",
                 accuracy * 100, cost_history[-1])

    log.info("--- Training Regime Detector ---")
    feature_dicts = []
    for row in X:
        feature_dicts.append(dict(zip(FEATURE_NAMES, row.tolist())))

    detector = RegimeDetector()
    mapping = detector.train(feature_dicts)
    if mapping:
        detector.save()
        log.info("Regime detector saved — cluster mapping: %s",
                 {k: REGIME_NAMES[v] for k, v in mapping.items()})


def train_from_memory() -> None:
    if not BUFFER_PATH.exists():
        log.warning("No trade buffer found at %s", BUFFER_PATH)
        return

    buffer = json.loads(BUFFER_PATH.read_text())
    if len(buffer) < 10:
        log.warning("Need at least 10 trades in buffer, have %d", len(buffer))
        return

    X_list, y_list = [], []
    for t in buffer:
        X_list.append(t["features"])
        y_list.append(1.0 if t["is_win"] else 0.0)

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.float32)

    wins = int(y.sum())
    losses = len(y) - wins
    log.info("Trade buffer: %d trades (%d W / %d L)", len(y), wins, losses)

    classifier = SignalClassifier(num_features=X.shape[1])
    cost_history = classifier.train(X, y, regularization=0.3, alpha=0.1, max_iterations=3000)

    if cost_history:
        classifier.save()
        log.info("Classifier trained from memory buffer")

    feature_dicts = [dict(zip(FEATURE_NAMES, row.tolist())) for row in X]
    detector = RegimeDetector()
    mapping = detector.train(feature_dicts)
    if mapping:
        detector.save()


def show_stats() -> None:
    collector = DataCollector()
    stats = collector.get_stats()

    print("\n" + "=" * 60)
    print("  OMEGA TRADING COMPANY — ML DATASET STATISTICS")
    print("=" * 60)

    if not stats["files"]:
        print("  No data files found. Run: python3 -m ml.train --collect-only")
        print("=" * 60)
        return

    for f in stats["files"]:
        print(f"\n  {f['name']}:")
        print(f"    Total rows:   {f['total']:,}")
        print(f"    Labeled:      {f['labeled']:,}")
        print(f"    Wins:         {f['wins']:,}")
        print(f"    Losses:       {f['losses']:,}")

    print(f"\n  TOTALS:")
    print(f"    Rows:     {stats['total_rows']:,}")
    print(f"    Labeled:  {stats['labeled_rows']:,}")
    print(f"    Wins:     {stats['win']:,}")
    print(f"    Losses:   {stats['loss']:,}")

    if BUFFER_PATH.exists():
        buf = json.loads(BUFFER_PATH.read_text())
        wins = sum(1 for t in buf if t.get("is_win"))
        print(f"\n  Trade buffer: {len(buf)} trades ({wins} W / {len(buf) - wins} L)")

    from ml.neural_net import SignalClassifier
    from ml.regime import RegimeDetector
    clf = SignalClassifier.load()
    reg = RegimeDetector.load()
    print(f"\n  Signal Classifier: {'TRAINED' if clf._trained else 'UNTRAINED'}")
    if clf._trained:
        print(f"    Architecture:  {' → '.join(str(l) for l in clf.layers)}")
        print(f"    Last cost:     {clf._cost_history[-1]:.4f}" if clf._cost_history else "")
    print(f"  Regime Detector:  {'TRAINED' if reg._trained else 'RULE-BASED FALLBACK'}")

    print("=" * 60 + "\n")


def seed_from_existing_trades() -> None:
    """Seed the trade buffer from omega_memory.json for initial training."""
    memory_path = Path(__file__).parent.parent / "data" / "omega_memory.json"
    if not memory_path.exists():
        log.warning("No omega_memory.json found")
        return

    with open(memory_path) as f:
        memory = json.load(f)

    trades = memory.get("trades", [])
    if not trades:
        log.warning("No trades in memory")
        return

    log.info("Seeding trade buffer from %d historical trades", len(trades))

    trade_buffer = []
    for t in trades:
        fake_features = np.random.randn(len(FEATURE_NAMES)).tolist()

        if "adx" in t:
            for i, name in enumerate(FEATURE_NAMES):
                if name in t:
                    fake_features[i] = t[name]

        trade_buffer.append({
            "features": fake_features,
            "is_win": t.get("is_win", False),
            "pnl": t.get("pnl", 0),
            "entry_time": t.get("entry_time", ""),
            "symbol": t.get("symbol", ""),
            "regime": "UNKNOWN",
            "timestamp": t.get("entry_timestamp", 0),
        })

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BUFFER_PATH.write_text(json.dumps(trade_buffer, indent=2))
    log.info("Seeded %d trades into buffer", len(trade_buffer))


def main() -> None:
    parser = argparse.ArgumentParser(description="Omega Trading Company — ML Training Pipeline")
    parser.add_argument("--collect-only", action="store_true", help="Only collect market data")
    parser.add_argument("--train-only", action="store_true", help="Only train models")
    parser.add_argument("--from-memory", action="store_true", help="Train from trade buffer")
    parser.add_argument("--stats", action="store_true", help="Show dataset statistics")
    parser.add_argument("--seed", action="store_true", help="Seed buffer from omega_memory.json")
    parser.add_argument("--symbols", nargs="+", default=["NIFTY", "BANKNIFTY"], help="Symbols to collect")
    parser.add_argument("--days", type=int, default=30, help="Days of historical data")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    if args.seed:
        seed_from_existing_trades()
        return

    if args.from_memory:
        train_from_memory()
        return

    if args.collect_only:
        collect_data(args.symbols, args.days)
        return

    if args.train_only:
        train_from_csv()
        return

    log.info("=== FULL ML PIPELINE ===")
    collect_data(args.symbols, args.days)
    train_from_csv()
    show_stats()


if __name__ == "__main__":
    main()
