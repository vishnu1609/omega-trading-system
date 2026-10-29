"""
Ensemble Regime Classifier with Hysteresis FSM

Replaces K-Means clustering (ml/regime.py) with a deterministic ensemble
of 5 complementary regime indicators + hysteresis state machine.

Indicators (each votes independently):
  1. ADX velocity  — slope of ADX(14) over 5 bars, not just level
  2. Kaufman Efficiency Ratio (KER) — directional efficiency 0-1
  3. Choppiness Index (CI) — range-bound detection 0-100
  4. Bollinger %B dispersion — squeeze detection
  5. Hurst exponent — mean-reversion vs persistence

Hysteresis FSM: requires 3/5 votes AND 2 consecutive bars to switch regime.
Prevents rapid flickering between regimes on noisy data.

Research basis: 6-agent deep research + ChatGPT blueprint (Oct 2026).
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


class EnsembleRegimeClassifier:
    """Deterministic ensemble regime detector with hysteresis."""

    def __init__(self):
        self._current_regime = Regime.CHOPPY
        self._pending_regime = Regime.CHOPPY
        self._consecutive_votes = 0
        self._hysteresis_bars = 2  # need 2 consecutive bars agreeing to switch
        self._history: list[dict] = []
        self._trained = True  # always ready — no training needed

    def predict(self, features: dict[str, float]) -> tuple[Regime, dict[str, float]]:
        """Classify regime from feature dict. Returns (regime, confidence_dict)."""
        votes = self._collect_votes(features)
        vote_counts = {}
        for regime in votes.values():
            vote_counts[regime] = vote_counts.get(regime, 0) + 1

        # Winner = regime with most votes (ties → CHOPPY as conservative default)
        winner = max(vote_counts, key=vote_counts.get, default=Regime.CHOPPY)
        winner_count = vote_counts.get(winner, 0)

        # Require majority (3/5) to propose a switch
        if winner_count >= 3:
            proposed = winner
        else:
            proposed = Regime.CHOPPY  # no consensus → choppy

        # Hysteresis FSM: need consecutive bars agreeing before switching
        if proposed == self._current_regime:
            self._pending_regime = proposed
            self._consecutive_votes = 0
        elif proposed == self._pending_regime:
            self._consecutive_votes += 1
            if self._consecutive_votes >= self._hysteresis_bars:
                old = self._current_regime
                self._current_regime = proposed
                self._consecutive_votes = 0
                log.info("REGIME SHIFT: %s → %s (votes: %s)",
                         REGIME_NAMES[old], REGIME_NAMES[proposed], vote_counts)
        else:
            self._pending_regime = proposed
            self._consecutive_votes = 1

        # Confidence: proportion of indicators that agree with current regime
        total = len(votes)
        agree = sum(1 for r in votes.values() if r == self._current_regime)
        confidence = agree / total if total > 0 else 0.25

        probs = {}
        for regime in Regime:
            count = vote_counts.get(regime, 0)
            probs[REGIME_NAMES[regime]] = count / total if total > 0 else 0.25

        self._history.append({
            "regime": REGIME_NAMES[self._current_regime],
            "votes": {k: REGIME_NAMES[v] for k, v in votes.items()},
            "confidence": confidence,
        })
        if len(self._history) > 50:
            self._history = self._history[-50:]

        return self._current_regime, probs

    def _collect_votes(self, f: dict[str, float]) -> dict[str, Regime]:
        """Each indicator casts a vote for a regime."""
        votes = {}

        # 1. ADX velocity (slope of ADX over 5 bars)
        adx = f.get("adx_14", f.get("adx", 20))
        adx_slope = f.get("adx_slope_5", 0)
        if adx > 25 and adx_slope > 0:
            votes["adx"] = Regime.TRENDING
        elif adx > 30 and adx_slope <= 0:
            votes["adx"] = Regime.VOLATILE  # strong but decaying → volatile
        elif adx < 20:
            votes["adx"] = Regime.RANGING
        else:
            votes["adx"] = Regime.CHOPPY

        # 2. Kaufman Efficiency Ratio (KER)
        # KER = |net direction| / sum(|bar-to-bar moves|), range 0-1
        # High KER (>0.6) = trending, Low KER (<0.3) = ranging
        hurst = f.get("hurst_exponent", 0.5)
        ker = f.get("ker", 0.5)
        if ker > 0.6:
            votes["ker"] = Regime.TRENDING
        elif ker < 0.2:
            votes["ker"] = Regime.RANGING
        elif ker < 0.35:
            votes["ker"] = Regime.CHOPPY
        else:
            votes["ker"] = Regime.VOLATILE if f.get("atr_pct", 0) > 1.0 else Regime.CHOPPY

        # 3. Choppiness Index
        chop = f.get("choppiness", f.get("choppiness_index", 50))
        if chop > 61.8:
            votes["chop"] = Regime.RANGING
        elif chop < 38.2:
            votes["chop"] = Regime.TRENDING
        elif chop > 55:
            votes["chop"] = Regime.CHOPPY
        else:
            votes["chop"] = Regime.VOLATILE if f.get("atr_pct", 0) > 0.8 else Regime.TRENDING

        # 4. Bollinger %B dispersion (squeeze detection)
        bb_width = f.get("bb_width", 5)
        bb_squeeze = f.get("bb_squeeze", 0)
        bb_pos = f.get("bb_position", 0.5)
        if bb_squeeze > 0.5:
            votes["bb"] = Regime.RANGING  # squeeze = consolidation
        elif bb_width > 8:
            votes["bb"] = Regime.VOLATILE  # wide bands = high vol
        elif bb_pos > 0.85 or bb_pos < 0.15:
            votes["bb"] = Regime.TRENDING  # at band extremes = directional
        else:
            votes["bb"] = Regime.CHOPPY

        # 5. Hurst exponent
        if hurst > 0.6:
            votes["hurst"] = Regime.TRENDING  # persistent
        elif hurst < 0.4:
            votes["hurst"] = Regime.RANGING  # mean-reverting
        else:
            votes["hurst"] = Regime.CHOPPY  # random walk

        return votes

    def get_current_regime(self) -> dict:
        return {
            "regime": REGIME_NAMES[self._current_regime],
            "pending": REGIME_NAMES[self._pending_regime],
            "consecutive_votes": self._consecutive_votes,
            "history": self._history[-10:],
        }

    def save(self, path: str | Path | None = None) -> Path:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        path = Path(path) if path else MODEL_DIR / "regime_ensemble.json"
        data = {
            "current_regime": int(self._current_regime),
            "pending_regime": int(self._pending_regime),
            "consecutive_votes": self._consecutive_votes,
        }
        path.write_text(json.dumps(data, indent=2))
        return path

    @classmethod
    def load(cls, path: str | Path | None = None) -> EnsembleRegimeClassifier:
        path = Path(path) if path else MODEL_DIR / "regime_ensemble.json"
        model = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                model._current_regime = Regime(data.get("current_regime", 3))
                model._pending_regime = Regime(data.get("pending_regime", 3))
                model._consecutive_votes = data.get("consecutive_votes", 0)
                log.info("Ensemble regime loaded: %s", REGIME_NAMES[model._current_regime])
            except Exception as e:
                log.warning("Ensemble regime load failed: %s", e)
        return model
