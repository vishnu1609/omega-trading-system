"""
Omega ML Engine — Central Intelligence Layer

Integrates all ML models into the trading pipeline:
  1. Signal Quality Classifier → filters bad signals before entry
  2. Regime Detection → adapts strategy parameters to market state
  3. Continuous learning → trains on each completed trade

Hooks into OmegaStrategy.process_signal() as a pre-trade filter
and into monitor_positions() for regime-aware trailing logic.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from ml.features import Candle, extract_features, features_to_array, FEATURE_NAMES
from ml.neural_net import SignalClassifier
from ml.regime import RegimeDetector, Regime, REGIME_NAMES

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = Path(__file__).parent.parent / "data" / "ml"


class OmegaMLEngine:
    """Central ML engine for Omega Trading Company."""

    def __init__(self, min_confidence: float = 0.55, auto_train: bool = True):
        self.min_confidence = min_confidence
        self.auto_train = auto_train

        self.signal_classifier = SignalClassifier.load()
        self.regime_detector = RegimeDetector.load()

        self._trade_buffer: list[dict] = []
        self._buffer_path = DATA_DIR / "trade_buffer.json"
        self._load_trade_buffer()

        self._last_regime: Regime = Regime.CHOPPY
        self._regime_history: list[dict] = []

        log.info(
            "ML Engine initialized — classifier=%s, regime=%s, confidence_threshold=%.2f",
            "TRAINED" if self.signal_classifier._trained else "UNTRAINED",
            "TRAINED" if self.regime_detector._trained else "RULE-BASED",
            self.min_confidence,
        )

    def evaluate_signal(
        self,
        candles: list[Candle],
        signal_direction: str = "LONG",
        signal_score: float = 0.0,
    ) -> dict:
        features = extract_features(candles, lookback=50)
        if not features:
            return {
                "approved": True,
                "reason": "no_features",
                "confidence": 0.5,
                "regime": REGIME_NAMES[self._last_regime],
                "ml_score": 0.0,
            }

        regime, regime_probs = self.regime_detector.predict(features)
        self._last_regime = regime
        self._regime_history.append({
            "time": datetime.now(IST).isoformat(),
            "regime": REGIME_NAMES[regime],
            "probs": regime_probs,
        })
        if len(self._regime_history) > 100:
            self._regime_history = self._regime_history[-100:]

        feat_array = features_to_array(features, FEATURE_NAMES)
        is_win, confidence = self.signal_classifier.predict_confidence(feat_array)

        approved = True
        reason = "approved"

        if self.signal_classifier._trained:
            if confidence < self.min_confidence:
                approved = False
                reason = f"low_ml_confidence ({confidence:.2f} < {self.min_confidence})"

            if not is_win and confidence > 0.65:
                approved = False
                reason = f"ml_predicts_loss (confidence={confidence:.2f})"

        regime_adjustments = self._get_regime_adjustments(regime, signal_direction)

        if regime == Regime.CHOPPY and signal_score < 65:
            approved = False
            reason = "choppy_regime + weak_signal"

        return {
            "approved": approved,
            "reason": reason,
            "confidence": confidence,
            "is_win_prediction": is_win,
            "regime": REGIME_NAMES[regime],
            "regime_probs": regime_probs,
            "regime_adjustments": regime_adjustments,
            "ml_score": confidence * 100,
            "features_snapshot": {
                "adx": features.get("adx", 0),
                "rsi": features.get("rsi", 0),
                "atr_pct": features.get("atr_pct", 0),
                "volume_ratio": features.get("volume_ratio", 0),
            },
        }

    def record_trade_outcome(
        self,
        candles: list[Candle],
        is_win: bool,
        pnl: float = 0.0,
        entry_time: str = "",
        symbol: str = "",
    ) -> None:
        features = extract_features(candles, lookback=50)
        if not features:
            return

        feat_array = features_to_array(features, FEATURE_NAMES)

        trade_record = {
            "features": feat_array.tolist(),
            "is_win": is_win,
            "pnl": pnl,
            "entry_time": entry_time or datetime.now(IST).isoformat(),
            "symbol": symbol,
            "regime": REGIME_NAMES[self._last_regime],
            "timestamp": time.time(),
        }

        self._trade_buffer.append(trade_record)
        self._save_trade_buffer()

        log.info(
            "Trade recorded for ML: %s, pnl=%.2f, regime=%s (buffer=%d)",
            "WIN" if is_win else "LOSS",
            pnl,
            REGIME_NAMES[self._last_regime],
            len(self._trade_buffer),
        )

        if self.auto_train and len(self._trade_buffer) >= 20:
            self._retrain()

    def _retrain(self) -> None:
        if len(self._trade_buffer) < 20:
            return

        X_list, y_list = [], []
        for t in self._trade_buffer:
            X_list.append(t["features"])
            y_list.append(1.0 if t["is_win"] else 0.0)

        X = np.array(X_list, dtype=np.float32)
        y = np.array(y_list, dtype=np.float32)

        wins = int(y.sum())
        losses = len(y) - wins
        log.info("Retraining signal classifier: %d trades (%d W / %d L)", len(y), wins, losses)

        self.signal_classifier = SignalClassifier(num_features=X.shape[1])
        cost_history = self.signal_classifier.train(
            X, y,
            regularization=0.3,
            alpha=0.1,
            max_iterations=3000,
            early_stop_patience=100,
        )

        if cost_history:
            self.signal_classifier.save()
            log.info("Classifier retrained: final_cost=%.4f", cost_history[-1])

        feature_dicts = []
        for t in self._trade_buffer:
            fd = dict(zip(FEATURE_NAMES, t["features"]))
            feature_dicts.append(fd)

        self.regime_detector = RegimeDetector()
        self.regime_detector.train(feature_dicts)
        self.regime_detector.save()

    def _get_regime_adjustments(self, regime: Regime, direction: str) -> dict:
        if regime == Regime.TRENDING:
            return {
                "sl_multiplier": 1.2,
                "tp_multiplier": 1.5,
                "trail_atr_factor": 2.5,
                "confidence_boost": 10,
                "hold_longer": True,
                "notes": "Strong trend — wider stops, bigger targets, let winners run",
            }
        elif regime == Regime.RANGING:
            return {
                "sl_multiplier": 0.8,
                "tp_multiplier": 0.7,
                "trail_atr_factor": 1.5,
                "confidence_boost": -5,
                "hold_longer": False,
                "notes": "Range-bound — tight stops, quick profits, mean-reversion bias",
            }
        elif regime == Regime.VOLATILE:
            return {
                "sl_multiplier": 1.5,
                "tp_multiplier": 1.3,
                "trail_atr_factor": 3.0,
                "confidence_boost": 0,
                "hold_longer": False,
                "notes": "High volatility — wide stops to avoid noise, cautious sizing",
            }
        else:
            return {
                "sl_multiplier": 0.7,
                "tp_multiplier": 0.5,
                "trail_atr_factor": 1.0,
                "confidence_boost": -15,
                "hold_longer": False,
                "notes": "Choppy — tight everything, reduce exposure, many false signals",
            }

    def record_signal(self, payload: dict, status: str, candles: list[Candle] | None = None) -> None:
        """Record every signal (blocked or executed) for ML learning."""
        signal_record = {
            "symbol": payload.get("symbol", ""),
            "side": payload.get("side", ""),
            "score": float(payload.get("score", 0)),
            "confidence": payload.get("confidence", "LOW"),
            "adx": payload.get("adx", 0),
            "rsi": payload.get("rsi", 50),
            "vwap_position": payload.get("vwap_position", ""),
            "supertrend_dir": payload.get("supertrend_dir", ""),
            "volatility_regime": payload.get("volatility_regime", "NORMAL"),
            "volume_ratio": payload.get("volume_ratio", 1.0),
            "status": status,
            "timestamp": time.time(),
            "entry_time": datetime.now(IST).isoformat(),
            "regime": REGIME_NAMES[self._last_regime],
        }
        if candles and len(candles) >= 50:
            features = extract_features(candles, lookback=50)
            if features:
                signal_record["features"] = features_to_array(features, FEATURE_NAMES).tolist()

        signal_log_path = DATA_DIR / "signal_log.json"
        try:
            existing = json.loads(signal_log_path.read_text()) if signal_log_path.exists() else []
        except Exception:
            existing = []
        existing.append(signal_record)
        if len(existing) > 5000:
            existing = existing[-5000:]
        signal_log_path.write_text(json.dumps(existing, indent=1))
        log.info("Signal logged for ML: %s %s %s score=%.0f (total=%d)",
                 status, payload.get("side", ""), payload.get("symbol", ""),
                 signal_record["score"], len(existing))

    def get_current_regime(self) -> dict:
        return {
            "regime": REGIME_NAMES[self._last_regime],
            "adjustments": self._get_regime_adjustments(self._last_regime, "LONG"),
            "history": self._regime_history[-10:],
        }

    def get_model_stats(self) -> dict:
        return {
            "signal_classifier": {
                "trained": self.signal_classifier._trained,
                "layers": self.signal_classifier.layers,
                "last_cost": self.signal_classifier._cost_history[-1] if self.signal_classifier._cost_history else None,
                "iterations_trained": len(self.signal_classifier._cost_history),
            },
            "regime_detector": {
                "trained": self.regime_detector._trained,
                "n_clusters": self.regime_detector.n_clusters,
                "current_regime": REGIME_NAMES[self._last_regime],
            },
            "trade_buffer": {
                "size": len(self._trade_buffer),
                "wins": sum(1 for t in self._trade_buffer if t["is_win"]),
                "losses": sum(1 for t in self._trade_buffer if not t["is_win"]),
                "auto_retrain_at": 20,
            },
            "confidence_threshold": self.min_confidence,
        }

    def force_retrain(self) -> dict:
        if len(self._trade_buffer) < 10:
            return {"error": f"Need at least 10 trades, have {len(self._trade_buffer)}"}

        self._retrain()
        return self.get_model_stats()

    def _load_trade_buffer(self) -> None:
        if self._buffer_path.exists():
            try:
                self._trade_buffer = json.loads(self._buffer_path.read_text())
            except (json.JSONDecodeError, KeyError):
                self._trade_buffer = []

    def _save_trade_buffer(self) -> None:
        self._buffer_path.parent.mkdir(parents=True, exist_ok=True)
        self._buffer_path.write_text(json.dumps(self._trade_buffer, indent=2))

    def save_all(self) -> None:
        self.signal_classifier.save()
        self.regime_detector.save()
        self._save_trade_buffer()
        log.info("All ML models saved")

    @classmethod
    def create_fresh(cls, min_confidence: float = 0.55) -> OmegaMLEngine:
        engine = cls(min_confidence=min_confidence)
        engine.signal_classifier = SignalClassifier()
        engine.regime_detector = RegimeDetector()
        return engine
