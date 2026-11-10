"""
Ensemble Regime Detector — HMM + BOCPD + KER

Combines multiple regime detection methods into a single composite score
that replaces static ADX/choppiness threshold gates.

Components:
  1. 3-state Gaussian HMM (trending-up, trending-down, ranging)
  2. Bayesian Online Changepoint Detection (Adams & MacKay)
  3. Kaufman Efficiency Ratio with hysteresis
  4. ADX velocity + Choppiness Index (legacy, reweighted)

All implemented with numpy only — no sklearn/hmmlearn.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class RegimeState:
    regime: str = "TRANSITIONAL"        # TRENDING_UP / TRENDING_DOWN / RANGING / TRANSITIONAL
    confidence: float = 0.0             # 0-1
    composite_score: float = 0.5        # 0-1, >0.6 = trending, <0.4 = ranging
    hmm_probs: tuple[float, float, float] = (0.33, 0.33, 0.34)  # (up, down, ranging)
    bocpd_cp_prob: float = 0.0          # P(changepoint)
    ker_value: float = 0.5              # Kaufman Efficiency Ratio
    adx_trend_score: float = 0.5        # normalized ADX velocity component
    chop_score: float = 0.5             # normalized choppiness component
    forced_reassess: bool = False        # True when BOCPD forced a regime flip


# ═══════════════════════════════════════════════════════════════════════
# 1. Simple 3-state Gaussian HMM
# ═══════════════════════════════════════════════════════════════════════

# States: 0=TRENDING_UP, 1=TRENDING_DOWN, 2=RANGING
_N_STATES = 3

# Pre-set transition matrix (no EM needed)
_TRANS = np.array([
    [0.92, 0.02, 0.06],   # trending-up: sticky, rare direct flip to down
    [0.02, 0.92, 0.06],   # trending-down: sticky, rare direct flip to up
    [0.04, 0.04, 0.92],   # ranging: stickier, equal chance of either trend
], dtype=np.float64)

# Emission parameters per state: (mean, std) for each of 3 features
# Features: [log_return, realized_vol_20bar, volume_ratio]
_EMISSION_PARAMS = {
    0: {  # TRENDING_UP
        "means": np.array([0.0008, 0.012, 1.15]),
        "stds":  np.array([0.004,  0.006, 0.30]),
    },
    1: {  # TRENDING_DOWN
        "means": np.array([-0.0010, 0.018, 1.25]),
        "stds":  np.array([0.005,   0.008, 0.35]),
    },
    2: {  # RANGING
        "means": np.array([0.0001, 0.008, 0.90]),
        "stds":  np.array([0.003,  0.004, 0.25]),
    },
}


def _gaussian_pdf(x: np.ndarray, mean: np.ndarray, std: np.ndarray) -> float:
    """Multivariate diagonal Gaussian log-likelihood, exponentiated."""
    std_safe = np.maximum(std, 1e-8)
    log_p = -0.5 * np.sum(((x - mean) / std_safe) ** 2 + np.log(2 * np.pi * std_safe ** 2))
    return math.exp(max(log_p, -500))


class SimpleHMM:
    """Online forward-algorithm HMM with fixed parameters."""

    def __init__(self):
        self._alpha: np.ndarray = np.array([0.33, 0.33, 0.34])  # state probs
        self._n_updates: int = 0

    def update(self, log_return: float, realized_vol: float, volume_ratio: float) -> np.ndarray:
        """Run one forward step. Returns state probabilities [P(up), P(down), P(ranging)]."""
        obs = np.array([log_return, realized_vol, volume_ratio])

        # Emission likelihoods
        emit = np.zeros(_N_STATES)
        for s in range(_N_STATES):
            emit[s] = _gaussian_pdf(obs, _EMISSION_PARAMS[s]["means"], _EMISSION_PARAMS[s]["stds"])

        # Forward step: alpha_new[j] = emit[j] * sum_i(alpha[i] * trans[i,j])
        alpha_pred = self._alpha @ _TRANS
        alpha_new = emit * alpha_pred

        total = alpha_new.sum()
        if total > 0:
            alpha_new /= total
        else:
            alpha_new = np.array([0.33, 0.33, 0.34])

        self._alpha = alpha_new
        self._n_updates += 1
        return self._alpha.copy()

    @property
    def state_probs(self) -> np.ndarray:
        return self._alpha.copy()

    @property
    def most_likely_state(self) -> int:
        return int(np.argmax(self._alpha))

    def reset(self):
        self._alpha = np.array([0.33, 0.33, 0.34])
        self._n_updates = 0


# ═══════════════════════════════════════════════════════════════════════
# 2. Bayesian Online Changepoint Detection (BOCPD)
# ═══════════════════════════════════════════════════════════════════════

class BOCPD:
    """Adams & MacKay online changepoint detector with Gaussian sufficient stats."""

    def __init__(self, hazard_lambda: float = 200.0, max_run_length: int = 500):
        self._hazard = 1.0 / hazard_lambda
        self._max_rl = max_run_length
        # Run length probabilities: P(r_t = k) for k in [0, max_rl]
        self._rl_probs = np.zeros(self._max_rl + 1)
        self._rl_probs[0] = 1.0
        # Sufficient statistics for Gaussian predictive (mean, var) per run length
        self._sum_x = np.zeros(self._max_rl + 1)
        self._sum_x2 = np.zeros(self._max_rl + 1)
        self._counts = np.zeros(self._max_rl + 1)
        self._n_updates = 0

    def update(self, x: float) -> float:
        """Process one observation. Returns P(changepoint at this step)."""
        n = min(self._n_updates + 1, self._max_rl)

        # Prior predictive for changepoint (broad, uninformative)
        prior_pred = _norm_pdf(x, 0.0, 0.02)

        # Predictive probability under each run length
        pred_probs = np.ones(n + 1) * 1e-10
        for k in range(n):
            if self._rl_probs[k] < 1e-15:
                continue
            cnt = self._counts[k]
            if cnt < 2:
                pred_probs[k] = prior_pred
            else:
                mu = self._sum_x[k] / cnt
                var = max((self._sum_x2[k] / cnt - mu * mu), 1e-10)
                sigma = math.sqrt(var) * math.sqrt(1 + 1.0 / cnt)
                pred_probs[k] = _norm_pdf(x, mu, sigma)

        # Growth probabilities: P(r_{t+1} = k+1) = P(r_t=k) * pi(x|r_t=k) * (1-H)
        growth = self._rl_probs[:n] * pred_probs[:n] * (1 - self._hazard)

        # Changepoint mass (Adams & MacKay eq. 3):
        # P(r_{t+1}=0) = prior_pred(x) * sum_k[ P(r_t=k) * H ]
        # The new run uses the prior predictive, not the old run's learned model
        cp_mass = prior_pred * self._hazard * np.sum(self._rl_probs[:n])

        # Shift run lengths up and set r=0
        new_rl = np.zeros(self._max_rl + 1)
        new_rl[0] = cp_mass
        limit = min(n, self._max_rl - 1)
        new_rl[1:limit + 1] = growth[:limit]

        total = new_rl.sum()
        if total > 0:
            new_rl /= total

        # Update sufficient stats: shift existing, reset r=0
        new_sum = np.zeros(self._max_rl + 1)
        new_sum2 = np.zeros(self._max_rl + 1)
        new_cnt = np.zeros(self._max_rl + 1)

        new_sum[1:limit + 1] = self._sum_x[:limit] + x
        new_sum2[1:limit + 1] = self._sum_x2[:limit] + x * x
        new_cnt[1:limit + 1] = self._counts[:limit] + 1
        # r=0 starts fresh with this observation
        new_sum[0] = x
        new_sum2[0] = x * x
        new_cnt[0] = 1

        self._rl_probs = new_rl
        self._sum_x = new_sum
        self._sum_x2 = new_sum2
        self._counts = new_cnt
        self._n_updates += 1

        return float(new_rl[0])  # normalized P(r=0) = P(changepoint)

    @property
    def changepoint_prob(self) -> float:
        return float(self._rl_probs[0]) if self._n_updates > 0 else 0.0

    def reset(self):
        self._rl_probs = np.zeros(self._max_rl + 1)
        self._rl_probs[0] = 1.0
        self._sum_x = np.zeros(self._max_rl + 1)
        self._sum_x2 = np.zeros(self._max_rl + 1)
        self._counts = np.zeros(self._max_rl + 1)
        self._n_updates = 0


def _norm_pdf(x: float, mu: float, sigma: float) -> float:
    sigma = max(sigma, 1e-8)
    return math.exp(-0.5 * ((x - mu) / sigma) ** 2) / (sigma * math.sqrt(2 * math.pi))


# ═══════════════════════════════════════════════════════════════════════
# 3. Kaufman Efficiency Ratio with hysteresis
# ═══════════════════════════════════════════════════════════════════════

class KaufmanER:
    """Efficiency Ratio with hysteresis for regime gating."""

    def __init__(self, period: int = 20,
                 trending_threshold: float = 0.45,
                 ranging_threshold: float = 0.35,
                 confirm_bars: int = 3):
        self._period = period
        self._trending_th = trending_threshold
        self._ranging_th = ranging_threshold
        self._confirm_bars = confirm_bars
        self._prices: list[float] = []
        self._er: float = 0.5
        self._state: str = "TRANSITIONAL"  # TRENDING / RANGING / TRANSITIONAL
        self._bars_above: int = 0
        self._bars_below: int = 0

    def update(self, close: float) -> float:
        self._prices.append(close)
        if len(self._prices) > self._period + 50:
            self._prices = self._prices[-(self._period + 50):]

        if len(self._prices) < self._period + 1:
            return self._er

        window = self._prices[-(self._period + 1):]
        direction = abs(window[-1] - window[0])
        volatility = sum(abs(window[k + 1] - window[k]) for k in range(len(window) - 1))
        self._er = direction / volatility if volatility > 0 else 0.0

        # Hysteresis state machine
        if self._er > self._trending_th:
            self._bars_above += 1
            self._bars_below = 0
        elif self._er < self._ranging_th:
            self._bars_below += 1
            self._bars_above = 0
        else:
            self._bars_above = max(0, self._bars_above - 1)
            self._bars_below = max(0, self._bars_below - 1)

        if self._bars_above >= self._confirm_bars:
            self._state = "TRENDING"
        elif self._bars_below >= self._confirm_bars:
            self._state = "RANGING"
        elif self._bars_above == 0 and self._bars_below == 0:
            self._state = "TRANSITIONAL"

        return self._er

    @property
    def value(self) -> float:
        return self._er

    @property
    def state(self) -> str:
        return self._state

    def reset(self):
        self._prices.clear()
        self._er = 0.5
        self._state = "TRANSITIONAL"
        self._bars_above = 0
        self._bars_below = 0


# ═══════════════════════════════════════════════════════════════════════
# 4. Ensemble Regime Detector
# ═══════════════════════════════════════════════════════════════════════

# Ensemble weights
W_ADX = 0.20
W_CHOP = 0.15
W_HMM = 0.25
W_BOCPD = 0.25
W_KER = 0.15

# Composite score thresholds (with 2-bar confirmation)
TRENDING_THRESHOLD = 0.60
RANGING_THRESHOLD = 0.40
CONFIRM_BARS = 2
BOCPD_FORCE_THRESHOLD = 0.8


class RegimeDetector:
    """Combines HMM, BOCPD, KER, ADX, and choppiness into a single regime call."""

    def __init__(self):
        self._hmm = SimpleHMM()
        self._bocpd = BOCPD(hazard_lambda=200.0)
        self._ker = KaufmanER(period=20)
        self._prev_closes: list[float] = []
        self._prev_volumes: list[float] = []
        self._state = RegimeState()
        self._trending_bars: int = 0
        self._ranging_bars: int = 0
        self._last_forced_reset: int = 0
        self._tick_count: int = 0

    def update(
        self,
        candles: list,
        adx_value: float = 25.0,
        choppiness: float = 50.0,
    ) -> RegimeState:
        """
        Update regime state from latest candles + pre-computed ADX/choppiness.

        Args:
            candles: list of Candle objects (need .close, .volume, .high, .low)
            adx_value: current ADX value (already computed by signal_generator)
            choppiness: current choppiness index value
        """
        if len(candles) < 25:
            return self._state

        self._tick_count += 1

        # Extract features from latest candles
        closes = [c.close for c in candles[-25:]]
        volumes = [c.volume for c in candles[-25:]]

        # Log return
        if closes[-2] > 0:
            log_ret = math.log(closes[-1] / closes[-2])
        else:
            log_ret = 0.0

        # Realized volatility (20-bar)
        if len(closes) >= 21:
            rets = [math.log(closes[k] / closes[k - 1]) if closes[k - 1] > 0 else 0
                    for k in range(-20, 0)]
            realized_vol = float(np.std(rets)) if rets else 0.01
        else:
            realized_vol = 0.01

        # Volume ratio (current / 20-bar avg)
        avg_vol = np.mean(volumes[-21:-1]) if len(volumes) >= 21 else np.mean(volumes[:-1])
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

        # ── Component updates ──

        # 1. HMM
        hmm_probs = self._hmm.update(log_ret, realized_vol, vol_ratio)
        # Convert HMM to trending score: P(up) + P(down) = trending
        hmm_trending = float(hmm_probs[0] + hmm_probs[1])

        # 2. BOCPD
        cp_prob = self._bocpd.update(log_ret)

        # 3. KER
        self._ker.update(closes[-1])
        ker_val = self._ker.value
        ker_trending = min(ker_val / 0.6, 1.0)  # normalize: 0.6+ = fully trending

        # 4. ADX velocity score (normalize: 15=ranging, 30+=trending)
        adx_score = max(0.0, min(1.0, (adx_value - 15) / 20))

        # 5. Choppiness score (invert: high chop = ranging, clamped 0-1)
        chop_score = max(0.0, min(1.0, (68 - choppiness) / 30))

        # ── Composite ──
        composite = (
            W_ADX * adx_score
            + W_CHOP * chop_score
            + W_HMM * hmm_trending
            + W_BOCPD * (1.0 - cp_prob)  # high cp_prob means unstable → not clearly trending
            + W_KER * ker_trending
        )

        # BOCPD forced reassessment
        forced = cp_prob > BOCPD_FORCE_THRESHOLD
        if forced and (self._tick_count - self._last_forced_reset) > 10:
            self._trending_bars = 0
            self._ranging_bars = 0
            self._last_forced_reset = self._tick_count
            log.info("BOCPD CHANGEPOINT: P=%.3f — forced regime reassessment", cp_prob)

        # Hysteresis confirmation
        if composite > TRENDING_THRESHOLD:
            self._trending_bars += 1
            self._ranging_bars = 0
        elif composite < RANGING_THRESHOLD:
            self._ranging_bars += 1
            self._trending_bars = 0
        else:
            self._trending_bars = max(0, self._trending_bars - 1)
            self._ranging_bars = max(0, self._ranging_bars - 1)

        # Determine regime
        if self._trending_bars >= CONFIRM_BARS:
            hmm_state = self._hmm.most_likely_state
            if hmm_state == 0:
                regime = "TRENDING_UP"
            elif hmm_state == 1:
                regime = "TRENDING_DOWN"
            else:
                regime = "TRENDING_UP" if log_ret > 0 else "TRENDING_DOWN"
            confidence = min(composite, 1.0)
        elif self._ranging_bars >= CONFIRM_BARS:
            regime = "RANGING"
            confidence = min(1.0 - composite, 1.0)
        else:
            regime = "TRANSITIONAL"
            confidence = 1.0 - 2 * abs(composite - 0.5)  # highest at 0.5

        prev_regime = self._state.regime
        self._state = RegimeState(
            regime=regime,
            confidence=round(confidence, 3),
            composite_score=round(composite, 4),
            hmm_probs=(round(float(hmm_probs[0]), 3),
                       round(float(hmm_probs[1]), 3),
                       round(float(hmm_probs[2]), 3)),
            bocpd_cp_prob=round(cp_prob, 4),
            ker_value=round(ker_val, 4),
            adx_trend_score=round(adx_score, 3),
            chop_score=round(chop_score, 3),
            forced_reassess=forced,
        )

        if regime != prev_regime:
            log.info(
                "ENSEMBLE REGIME: %s → %s (score=%.3f, conf=%.2f) "
                "HMM=[%.2f,%.2f,%.2f] BOCPD=%.3f KER=%.3f ADX=%.2f CHOP=%.2f",
                prev_regime, regime, composite, confidence,
                hmm_probs[0], hmm_probs[1], hmm_probs[2],
                cp_prob, ker_val, adx_score, chop_score,
            )

        return self._state

    @property
    def state(self) -> RegimeState:
        return self._state

    def reset(self):
        self._hmm.reset()
        self._bocpd.reset()
        self._ker.reset()
        self._state = RegimeState()
        self._trending_bars = 0
        self._ranging_bars = 0
        self._tick_count = 0

    def regime_to_vol_label(self) -> str:
        """Map ensemble regime to legacy volatility regime labels for backward compat."""
        r = self._state.regime
        if r == "RANGING":
            return "CHOPPY"
        elif r in ("TRENDING_UP", "TRENDING_DOWN"):
            return "TRENDING"
        elif r == "TRANSITIONAL":
            return "NORMAL"
        return "NORMAL"
