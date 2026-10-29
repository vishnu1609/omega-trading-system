"""
Omega ML Engine — Self-Healing Intelligence Layer

v2: SQLite-backed buffer + LightGBM + SGD online adapter.
Replaces the JSON buffer + MLP neural net that was stuck at 0/20.

Key changes from v1:
  1. SQLite durable storage — survives crashes, no more lost training data
  2. Counterfactual shadow logging — blocked signals tracked for what-if learning
  3. LightGBM (max_depth=3, num_leaves=7) — works with 15+ samples vs MLP needing 100+
  4. SGDClassifier online adapter — incremental updates between full retrains
  5. E[R] >= +0.15R gate — expected R-multiple instead of raw p_win > 0.50
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from ml.features import Candle, extract_features, features_to_array, FEATURE_NAMES, REDUCED_FEATURE_NAMES
from ml.sqlite_buffer import MLBuffer
from ml.regime import RegimeDetector, Regime, REGIME_NAMES
from ml.regime_ensemble import EnsembleRegimeClassifier
from ml.counterfactual import CounterfactualResolver
from ml.validation import validate_from_buffer

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = Path(__file__).parent.parent / "data" / "ml"
MODEL_DIR = DATA_DIR / "models"

try:
    import lightgbm as lgb
    HAS_LGBM = True
except ImportError:
    lgb = None
    HAS_LGBM = False
    log.warning("LightGBM not installed — ML engine will use SGD-only mode")

try:
    from sklearn.linear_model import SGDClassifier
    from sklearn.preprocessing import StandardScaler
    import pickle
    HAS_SKLEARN = True
except ImportError:
    SGDClassifier = None
    StandardScaler = None
    HAS_SKLEARN = False
    log.warning("scikit-learn not installed — online learning disabled")


class OmegaMLEngine:
    """Central ML engine with self-healing SQLite buffer."""

    def __init__(self, min_confidence: float = 0.55, auto_train: bool = True):
        self.min_confidence = min_confidence
        self.auto_train = auto_train

        self.buffer = MLBuffer()
        self._migrate_json_buffer()

        self.regime_detector = RegimeDetector.load()
        self.regime_ensemble = EnsembleRegimeClassifier.load()
        self._last_regime: Regime = Regime.CHOPPY
        self._regime_history: list[dict] = []

        self.counterfactual = CounterfactualResolver(self.buffer)

        # LightGBM primary model
        self._lgbm_model = None
        self._lgbm_trained = False
        self._load_lgbm()

        # SGD online adapter
        self._sgd_model = None
        self._sgd_scaler = None
        self._sgd_trained = False
        self._load_sgd()

        self._trades_since_retrain = 0
        self._retrain_threshold = 15  # full retrain every 15 new trades
        self._last_validation: dict = {}

        # Track recent avg win/loss for E[R] calculation
        self._avg_win_r = 1.5   # default assumption
        self._avg_loss_r = -1.0

        log.info(
            "ML Engine v2 initialized — lgbm=%s, sgd=%s, buffer=%d signals/%d trades",
            "TRAINED" if self._lgbm_trained else "UNTRAINED",
            "TRAINED" if self._sgd_trained else "UNTRAINED",
            self.buffer.signal_count(),
            self.buffer.trade_count(),
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

        # Primary: ensemble classifier (deterministic, no training needed)
        regime, regime_probs = self.regime_ensemble.predict(features)
        # Fallback cross-check with K-Means if trained
        if self.regime_detector._trained:
            kmeans_regime, _ = self.regime_detector.predict(features)
            if kmeans_regime != regime:
                regime_probs[f"kmeans_{REGIME_NAMES[kmeans_regime]}"] = 0.3
        self._last_regime = regime
        self._regime_history.append({
            "time": datetime.now(IST).isoformat(),
            "regime": REGIME_NAMES[regime],
            "probs": regime_probs,
        })
        if len(self._regime_history) > 100:
            self._regime_history = self._regime_history[-100:]

        feat_array_full = features_to_array(features, FEATURE_NAMES)
        feat_array_reduced = features_to_array(features, REDUCED_FEATURE_NAMES)
        p_win, confidence_source = self._predict_win_prob(feat_array_reduced)

        # E[R] gate: E[R] = p_win * avg_win_R + (1-p_win) * avg_loss_R - cost_R
        expected_r = p_win * self._avg_win_r + (1 - p_win) * abs(self._avg_loss_r)
        # Cost-adjust: ~₹65 round-trip cost on ₹200 premium × 65 lot ≈ 0.05R
        cost_r = 0.05
        expected_r -= cost_r
        min_expected_r = 0.15  # require E[R] >= +0.15R after costs

        approved = True
        reason = "approved"

        if self._lgbm_trained or self._sgd_trained:
            if expected_r < min_expected_r:
                approved = False
                reason = f"low_expected_R ({expected_r:.2f} < {min_expected_r}, p_win={p_win:.2f})"

            # Hard block: p_win < 0.35 regardless of E[R]
            if p_win < 0.35:
                approved = False
                reason = f"ml_strong_loss_prediction (p_win={p_win:.2f})"

        regime_adjustments = self._get_regime_adjustments(regime, signal_direction)

        choppy_min = 55 if self.min_confidence <= 0.60 else 65
        if regime == Regime.CHOPPY and signal_score < choppy_min:
            approved = False
            reason = f"choppy_regime + weak_signal (need {choppy_min})"

        return {
            "approved": approved,
            "reason": reason,
            "confidence": p_win,
            "is_win_prediction": p_win > 0.5,
            "regime": REGIME_NAMES[regime],
            "regime_probs": regime_probs,
            "regime_adjustments": regime_adjustments,
            "ml_score": p_win * 100,
            "expected_r": round(expected_r, 3),
            "model_source": confidence_source,
            "features_snapshot": {
                "adx": features.get("adx_14", 0),
                "rsi": features.get("rsi_14", 0),
                "atr_pct": features.get("atr_pct", 0),
                "volume_ratio": features.get("volume_ratio", 0),
            },
        }

    def _predict_win_prob(self, feat_array: np.ndarray) -> tuple[float, str]:
        """Get win probability from best available model."""
        if feat_array.ndim == 1:
            feat_array = feat_array.reshape(1, -1)

        # Primary: LightGBM
        if self._lgbm_trained and self._lgbm_model is not None:
            try:
                probs = self._lgbm_model.predict(feat_array)
                return float(probs[0]), "lgbm"
            except Exception as e:
                log.warning("LGBM predict failed: %s", e)

        # Fallback: SGD online adapter
        if self._sgd_trained and self._sgd_model is not None and self._sgd_scaler is not None:
            try:
                X_scaled = self._sgd_scaler.transform(feat_array)
                probs = self._sgd_model.predict_proba(X_scaled)
                return float(probs[0, 1]), "sgd"
            except Exception as e:
                log.warning("SGD predict failed: %s", e)

        return 0.5, "default"

    def record_signal(
        self,
        payload: dict,
        status: str,
        candles: list[Candle] | None = None,
    ) -> int | None:
        """Record every signal (executed or blocked) to SQLite."""
        features = None
        feature_names = None
        if candles and len(candles) >= 50:
            feat_dict = extract_features(candles, lookback=50)
            if feat_dict:
                features = features_to_array(feat_dict, FEATURE_NAMES).tolist()
                feature_names = FEATURE_NAMES

        signal_id = self.buffer.log_signal(
            symbol=payload.get("symbol", ""),
            side=payload.get("side", ""),
            score=float(payload.get("score", 0)),
            confidence=payload.get("confidence", "LOW"),
            regime=payload.get("volatility_regime", "NORMAL"),
            status=status,
            features=features,
            feature_names=feature_names,
            adx=payload.get("adx", 0),
            rsi=payload.get("rsi", 50),
            choppiness=payload.get("choppiness", 50),
            atr_pct=payload.get("atr_pct", 0),
            bb_position=payload.get("bb_position", 0.5),
            volume_ratio=payload.get("volume_ratio", 1.0),
        )

        log.info("Signal logged for ML: %s %s %s score=%.0f (signals=%d)",
                 status, payload.get("side", ""), payload.get("symbol", ""),
                 float(payload.get("score", 0)), self.buffer.signal_count())

        return signal_id

    def record_trade_outcome(
        self,
        candles: list[Candle],
        is_win: bool,
        pnl: float = 0.0,
        entry_time: str = "",
        symbol: str = "",
        exit_reason: str = "",
        hold_seconds: float = 0,
        r_multiple: float = 0,
        signal_id: int | None = None,
    ) -> None:
        features = None
        if candles and len(candles) >= 50:
            feat_dict = extract_features(candles, lookback=50)
            if feat_dict:
                features = features_to_array(feat_dict, FEATURE_NAMES).tolist()

        trade_id = self.buffer.log_trade(
            symbol=symbol,
            direction="LONG",
            entry_price=0,
            regime=REGIME_NAMES[self._last_regime],
            features=features,
            signal_id=signal_id,
        )
        self.buffer.close_trade(
            trade_id=trade_id,
            exit_price=0,
            pnl=pnl,
            is_win=is_win,
            exit_reason=exit_reason,
            hold_seconds=hold_seconds,
            r_multiple=r_multiple,
        )

        log.info(
            "Trade recorded for ML: %s, pnl=%.2f, regime=%s (buffer=%d)",
            "WIN" if is_win else "LOSS",
            pnl,
            REGIME_NAMES[self._last_regime],
            self.buffer.trade_count(),
        )

        # Update running E[R] estimates
        self._update_r_estimates(is_win, r_multiple)

        # SGD online update (single sample)
        if features and HAS_SKLEARN:
            self._sgd_partial_fit(features, is_win)

        self._trades_since_retrain += 1
        if self.auto_train and self._trades_since_retrain >= self._retrain_threshold:
            self._full_retrain()

    def _update_r_estimates(self, is_win: bool, r_multiple: float) -> None:
        """Exponential moving average of win/loss R-multiples."""
        alpha = 0.1  # slow decay
        if is_win and r_multiple > 0:
            self._avg_win_r = self._avg_win_r * (1 - alpha) + r_multiple * alpha
        elif not is_win:
            loss_r = abs(r_multiple) if r_multiple != 0 else 1.0
            self._avg_loss_r = self._avg_loss_r * (1 - alpha) + loss_r * alpha

    def _sgd_partial_fit(self, features: list[float], is_win: bool) -> None:
        """Online SGD update — single sample incremental learning (reduced features)."""
        if not HAS_SKLEARN:
            return

        # Extract reduced features from full feature vector
        if len(features) == len(FEATURE_NAMES):
            reduced = [features[FEATURE_NAMES.index(n)] if n in FEATURE_NAMES else 0.0
                       for n in REDUCED_FEATURE_NAMES]
        else:
            reduced = features[:len(REDUCED_FEATURE_NAMES)]
        X = np.array([reduced], dtype=np.float32)
        y = np.array([1 if is_win else 0])

        if self._sgd_scaler is None:
            self._sgd_scaler = StandardScaler()
            self._sgd_scaler.fit(X)
        try:
            X_scaled = self._sgd_scaler.transform(X)
        except Exception:
            self._sgd_scaler.fit(X)
            X_scaled = self._sgd_scaler.transform(X)

        if self._sgd_model is None:
            self._sgd_model = SGDClassifier(
                loss="log_loss", penalty="l2", alpha=0.001,
                max_iter=1, warm_start=True, random_state=42,
            )
            self._sgd_model.partial_fit(X_scaled, y, classes=[0, 1])
        else:
            self._sgd_model.partial_fit(X_scaled, y)

        self._sgd_trained = True

    def _full_retrain(self) -> None:
        """Full retrain of LightGBM on all available data."""
        X_list, y_list = self.buffer.get_training_data(min_samples=15)
        if not X_list:
            log.info("Skipping retrain: insufficient training data")
            return

        X_full = np.array(X_list, dtype=np.float32)
        y = np.array(y_list, dtype=np.int32)

        # Extract reduced features from full feature vectors for LightGBM
        reduced_indices = []
        for rname in REDUCED_FEATURE_NAMES:
            if rname in FEATURE_NAMES:
                reduced_indices.append(FEATURE_NAMES.index(rname))
            else:
                reduced_indices.append(0)
        X_reduced = X_full[:, reduced_indices] if X_full.shape[1] > max(reduced_indices) else X_full

        wins = int(y.sum())
        losses = len(y) - wins
        log.info("Full retrain: %d samples (%d W / %d L), using %d/%d features",
                 len(y), wins, losses, len(REDUCED_FEATURE_NAMES), len(FEATURE_NAMES))

        # LightGBM training on reduced feature set
        if HAS_LGBM:
            try:
                train_data = lgb.Dataset(X_reduced, label=y, free_raw_data=False,
                                         feature_name=REDUCED_FEATURE_NAMES)

                params = {
                    "objective": "binary",
                    "metric": "binary_logloss",
                    "max_depth": 3,
                    "num_leaves": 7,
                    "learning_rate": 0.1,
                    "min_child_samples": max(3, len(y) // 10),
                    "subsample": 0.8,
                    "colsample_bytree": 0.8,
                    "reg_alpha": 0.1,
                    "reg_lambda": 1.0,
                    "verbose": -1,
                    "seed": 42,
                }

                n_rounds = min(100, max(20, len(y) * 2))
                self._lgbm_model = lgb.train(
                    params, train_data, num_boost_round=n_rounds,
                )
                self._lgbm_trained = True
                self._save_lgbm()

                preds = self._lgbm_model.predict(X_reduced)
                accuracy = np.mean((preds > 0.5).astype(int) == y)
                log.info("LightGBM retrained: %d rounds, %d features, accuracy=%.1f%%",
                         n_rounds, len(REDUCED_FEATURE_NAMES), accuracy * 100)
            except Exception as e:
                log.warning("LightGBM retrain failed: %s", e)

        # Also update SGD scaler with reduced features
        if HAS_SKLEARN:
            try:
                self._sgd_scaler = StandardScaler()
                self._sgd_scaler.fit(X_reduced)
                self._sgd_model = SGDClassifier(
                    loss="log_loss", penalty="l2", alpha=0.001,
                    max_iter=100, warm_start=True, random_state=42,
                )
                X_scaled = self._sgd_scaler.transform(X_reduced)
                self._sgd_model.fit(X_scaled, y)
                self._sgd_trained = True
                self._save_sgd()
            except Exception as e:
                log.warning("SGD retrain failed: %s", e)

        # Retrain K-Means regime detector (kept as secondary cross-check)
        try:
            feature_dicts = []
            for feat_list in X_list:
                fd = dict(zip(FEATURE_NAMES, feat_list))
                feature_dicts.append(fd)
            self.regime_detector = RegimeDetector()
            self.regime_detector.train(feature_dicts)
            self.regime_detector.save()
        except Exception as e:
            log.warning("Regime retrain failed: %s", e)

        self._trades_since_retrain = 0

        # Purged walk-forward validation on every retrain
        try:
            report = validate_from_buffer(self.buffer)
            if report.folds:
                self._last_validation = {
                    "folds": len(report.folds),
                    "oos_accuracy": report.avg_accuracy,
                    "oos_f1": report.avg_f1,
                    "overfit_ratio": report.overfit_ratio,
                    "summary": report.summary(),
                }
                if report.overfit_ratio > 1.5:
                    log.warning("OVERFIT ALERT: %.2fx — consider increasing regularisation", report.overfit_ratio)
        except Exception as e:
            log.warning("Walk-forward validation failed: %s", e)

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

    def get_current_regime(self) -> dict:
        return {
            "regime": REGIME_NAMES[self._last_regime],
            "adjustments": self._get_regime_adjustments(self._last_regime, "LONG"),
            "history": self._regime_history[-10:],
        }

    def get_model_stats(self) -> dict:
        stats = self.buffer.get_trade_stats()
        return {
            "lgbm": {
                "trained": self._lgbm_trained,
                "model_type": "LightGBM(depth=3, leaves=7)",
            },
            "sgd": {
                "trained": self._sgd_trained,
                "model_type": "SGDClassifier(log_loss)",
            },
            "regime_detector": {
                "trained": self.regime_detector._trained,
                "current_regime": REGIME_NAMES[self._last_regime],
            },
            "buffer": {
                "signals": self.buffer.signal_count(),
                "trades": self.buffer.trade_count(),
                "trade_stats": stats,
                "retrain_threshold": self._retrain_threshold,
                "trades_since_retrain": self._trades_since_retrain,
            },
            "expected_r": {
                "avg_win_r": round(self._avg_win_r, 2),
                "avg_loss_r": round(self._avg_loss_r, 2),
                "min_required": 0.15,
            },
            "confidence_threshold": self.min_confidence,
            "validation": self._last_validation if self._last_validation else "not yet run",
        }

    def force_retrain(self) -> dict:
        self._full_retrain()
        return self.get_model_stats()

    # --- Model persistence ---

    def _save_lgbm(self) -> None:
        if not HAS_LGBM or self._lgbm_model is None:
            return
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        path = MODEL_DIR / "lgbm_signal.txt"
        self._lgbm_model.save_model(str(path))
        log.info("LightGBM saved: %s", path)

    def _load_lgbm(self) -> None:
        if not HAS_LGBM:
            return
        path = MODEL_DIR / "lgbm_signal.txt"
        if path.exists():
            try:
                self._lgbm_model = lgb.Booster(model_file=str(path))
                self._lgbm_trained = True
                log.info("LightGBM loaded: %s", path)
            except Exception as e:
                log.warning("LightGBM load failed: %s", e)

    def _save_sgd(self) -> None:
        if not HAS_SKLEARN or self._sgd_model is None:
            return
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with open(MODEL_DIR / "sgd_signal.pkl", "wb") as f:
            pickle.dump({"model": self._sgd_model, "scaler": self._sgd_scaler}, f)
        log.info("SGD model saved")

    def _load_sgd(self) -> None:
        if not HAS_SKLEARN:
            return
        path = MODEL_DIR / "sgd_signal.pkl"
        if path.exists():
            try:
                with open(path, "rb") as f:
                    data = pickle.load(f)
                self._sgd_model = data["model"]
                self._sgd_scaler = data["scaler"]
                self._sgd_trained = True
                log.info("SGD model loaded")
            except Exception as e:
                log.warning("SGD load failed: %s", e)

    def _migrate_json_buffer(self) -> None:
        """One-time migration from old JSON trade buffer."""
        old_path = DATA_DIR / "trade_buffer.json"
        if old_path.exists() and self.buffer.trade_count() == 0:
            count = self.buffer.migrate_from_json(old_path)
            if count > 0:
                backup = old_path.with_suffix(".json.bak")
                old_path.rename(backup)
                log.info("Migrated %d trades, old file backed up to %s", count, backup)

    def resolve_counterfactuals(self, underlying_prices: dict[str, float] | None = None) -> int:
        """Run counterfactual resolution on blocked signals. Call from main loop."""
        return self.counterfactual.maybe_resolve(underlying_prices)

    def save_all(self) -> None:
        self._save_lgbm()
        self._save_sgd()
        self.regime_detector.save()
        self.regime_ensemble.save()
        log.info("All ML models saved")

    @classmethod
    def create_fresh(cls, min_confidence: float = 0.55) -> OmegaMLEngine:
        engine = cls(min_confidence=min_confidence)
        engine.regime_detector = RegimeDetector()
        return engine
