"""
Market Regime Detection Model

Classifies market into 4 regimes using K-Means clustering + neural network:
  1. TRENDING   — strong directional move (ADX > 25, clear SuperTrend)
  2. RANGING    — sideways chop (ADX < 20, price near VWAP)
  3. VOLATILE   — high ATR expansion, wide candles, breakout potential
  4. CHOPPY     — erratic price action, many false signals

Uses unsupervised K-Means to discover natural clusters in feature space,
then maps clusters to regime labels using domain rules.
Adapted from trekhleb/homemade-machine-learning k_means + neural network.
"""
from __future__ import annotations

import json
import logging
from enum import IntEnum
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).parent.parent / "data" / "ml" / "models"


class Regime(IntEnum):
    TRENDING = 0
    RANGING = 1
    VOLATILE = 2
    CHOPPY = 3


REGIME_NAMES = {
    Regime.TRENDING: "TRENDING",
    Regime.RANGING: "RANGING",
    Regime.VOLATILE: "VOLATILE",
    Regime.CHOPPY: "CHOPPY",
}

REGIME_FEATURES = [
    "adx",
    "atr_pct",
    "bollinger_squeeze",
    "choppiness_index",
    "rsi",
    "return_kurtosis",
    "return_skewness",
    "supertrend_dir",
    "volume_ratio",
    "hurst_exponent",
]


def _normalize(data: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = np.mean(data, axis=0)
    std = np.std(data, axis=0)
    std[std == 0] = 1.0
    return (data - mean) / std, mean, std


class RegimeDetector:
    """Detects market regime from feature vectors."""

    def __init__(self, n_clusters: int = 4, max_iter: int = 300):
        self.n_clusters = n_clusters
        self.max_iter = max_iter
        self.centroids: np.ndarray | None = None
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None
        self._cluster_to_regime: dict[int, Regime] = {}
        self._trained = False

    def train(self, feature_dicts: list[dict[str, float]]) -> dict[int, Regime]:
        X = self._extract_regime_features(feature_dicts)
        if len(X) < self.n_clusters * 5:
            log.warning("Need at least %d samples, got %d", self.n_clusters * 5, len(X))
            return {}

        X_norm, self._mean, self._std = _normalize(X)

        self.centroids = self._kmeans(X_norm)

        labels = self._assign_clusters(X_norm)

        self._cluster_to_regime = self._map_clusters_to_regimes(X, labels)
        self._trained = True

        regime_counts = {}
        for cluster_id, regime in self._cluster_to_regime.items():
            count = int(np.sum(labels == cluster_id))
            regime_counts[REGIME_NAMES[regime]] = regime_counts.get(REGIME_NAMES[regime], 0) + count

        log.info("Regime detection trained on %d samples: %s", len(X), regime_counts)
        return self._cluster_to_regime

    def predict(self, features: dict[str, float]) -> tuple[Regime, dict[str, float]]:
        if not self._trained or self.centroids is None:
            return self._rule_based_fallback(features)

        x = np.array([features.get(f, 0.0) for f in REGIME_FEATURES]).reshape(1, -1)
        x_norm = (x - self._mean) / self._std

        distances = np.linalg.norm(x_norm - self.centroids, axis=1)
        cluster_id = int(np.argmin(distances))

        regime = self._cluster_to_regime.get(cluster_id, Regime.CHOPPY)

        inv_dist = 1.0 / (distances + 1e-10)
        probs = inv_dist / inv_dist.sum()
        confidence = {
            REGIME_NAMES[self._cluster_to_regime.get(i, Regime.CHOPPY)]: float(probs[i])
            for i in range(self.n_clusters)
        }

        return regime, confidence

    def _rule_based_fallback(self, features: dict[str, float]) -> tuple[Regime, dict[str, float]]:
        adx = features.get("adx", 20)
        atr_pct = features.get("atr_pct", 0.5)
        choppiness = features.get("choppiness_index", 50)
        bb_squeeze = features.get("bollinger_squeeze", 0.5)

        if adx > 25 and choppiness < 45:
            regime = Regime.TRENDING
        elif atr_pct > 1.5 or bb_squeeze < 0.3:
            regime = Regime.VOLATILE
        elif adx < 20 and choppiness > 55:
            regime = Regime.RANGING
        else:
            regime = Regime.CHOPPY

        return regime, {REGIME_NAMES[regime]: 1.0}

    def _kmeans(self, X: np.ndarray) -> np.ndarray:
        n = X.shape[0]
        idx = np.random.choice(n, self.n_clusters, replace=False)
        centroids = X[idx].copy()

        for _ in range(self.max_iter):
            distances = np.array([
                np.linalg.norm(X - centroids[k], axis=1)
                for k in range(self.n_clusters)
            ])
            labels = np.argmin(distances, axis=0)

            new_centroids = np.zeros_like(centroids)
            for k in range(self.n_clusters):
                members = X[labels == k]
                if len(members) > 0:
                    new_centroids[k] = members.mean(axis=0)
                else:
                    new_centroids[k] = X[np.random.randint(n)]

            if np.allclose(centroids, new_centroids, atol=1e-6):
                break
            centroids = new_centroids

        return centroids

    def _assign_clusters(self, X_norm: np.ndarray) -> np.ndarray:
        distances = np.array([
            np.linalg.norm(X_norm - self.centroids[k], axis=1)
            for k in range(self.n_clusters)
        ])
        return np.argmin(distances, axis=0)

    def _map_clusters_to_regimes(
        self,
        X_raw: np.ndarray,
        labels: np.ndarray,
    ) -> dict[int, Regime]:
        adx_idx = REGIME_FEATURES.index("adx")
        atr_idx = REGIME_FEATURES.index("atr_pct")
        chop_idx = REGIME_FEATURES.index("choppiness_index")

        cluster_profiles = {}
        for k in range(self.n_clusters):
            members = X_raw[labels == k]
            if len(members) == 0:
                cluster_profiles[k] = {"adx": 20, "atr": 0.5, "chop": 50}
                continue
            cluster_profiles[k] = {
                "adx": float(np.mean(members[:, adx_idx])),
                "atr": float(np.mean(members[:, atr_idx])),
                "chop": float(np.mean(members[:, chop_idx])),
            }

        mapping: dict[int, Regime] = {}
        assigned: set[int] = set()

        scored = sorted(
            cluster_profiles.items(),
            key=lambda kv: kv[1]["adx"],
            reverse=True,
        )
        if scored[0][1]["adx"] > 22:
            mapping[scored[0][0]] = Regime.TRENDING
            assigned.add(scored[0][0])

        scored_atr = sorted(
            [(k, v) for k, v in cluster_profiles.items() if k not in assigned],
            key=lambda kv: kv[1]["atr"],
            reverse=True,
        )
        if scored_atr and scored_atr[0][1]["atr"] > 0.8:
            mapping[scored_atr[0][0]] = Regime.VOLATILE
            assigned.add(scored_atr[0][0])

        scored_chop = sorted(
            [(k, v) for k, v in cluster_profiles.items() if k not in assigned],
            key=lambda kv: kv[1]["chop"],
            reverse=True,
        )
        if scored_chop:
            if scored_chop[0][1]["chop"] > 50:
                mapping[scored_chop[0][0]] = Regime.RANGING
            else:
                mapping[scored_chop[0][0]] = Regime.CHOPPY
            assigned.add(scored_chop[0][0])

        for k in range(self.n_clusters):
            if k not in assigned:
                mapping[k] = Regime.CHOPPY

        return mapping

    def _extract_regime_features(self, feature_dicts: list[dict[str, float]]) -> np.ndarray:
        rows = []
        for fd in feature_dicts:
            rows.append([fd.get(f, 0.0) for f in REGIME_FEATURES])
        return np.array(rows, dtype=np.float64)

    def save(self, path: str | Path | None = None) -> Path:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        path = Path(path) if path else MODEL_DIR / "regime_detector.json"

        data = {
            "n_clusters": self.n_clusters,
            "trained": self._trained,
            "centroids": self.centroids.tolist() if self.centroids is not None else None,
            "mean": self._mean.tolist() if self._mean is not None else None,
            "std": self._std.tolist() if self._std is not None else None,
            "cluster_to_regime": {str(k): int(v) for k, v in self._cluster_to_regime.items()},
        }
        path.write_text(json.dumps(data, indent=2))
        log.info("Regime model saved to %s", path)
        return path

    @classmethod
    def load(cls, path: str | Path | None = None) -> RegimeDetector:
        path = Path(path) if path else MODEL_DIR / "regime_detector.json"
        if not path.exists():
            log.warning("No saved regime model at %s, returning untrained", path)
            return cls()

        data = json.loads(path.read_text())
        model = cls(n_clusters=data["n_clusters"])
        model._trained = data["trained"]
        model.centroids = np.array(data["centroids"]) if data["centroids"] else None
        model._mean = np.array(data["mean"]) if data["mean"] else None
        model._std = np.array(data["std"]) if data["std"] else None
        model._cluster_to_regime = {
            int(k): Regime(int(v)) for k, v in data["cluster_to_regime"].items()
        }
        log.info("Regime model loaded from %s (trained=%s)", path, model._trained)
        return model
