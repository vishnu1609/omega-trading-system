"""
Black-Scholes Greeks Calculator

Computes delta, gamma, theta, vega, and theoretical price for
European-style index options. Uses IV from Dhan chain data.

Includes vectorized NumPy path for sub-ms batch computation on
WebSocket ticks (compute_greeks_vectorized).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

RISK_FREE_RATE = 0.07
TRADING_DAYS_PER_YEAR = 252

_SQRT2 = math.sqrt(2)
_SQRT2PI = math.sqrt(2 * math.pi)
_INV_SQRT2 = 1.0 / _SQRT2
_INV_SQRT2PI = 1.0 / _SQRT2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / _SQRT2))


def _np_norm_cdf(x: np.ndarray) -> np.ndarray:
    """Vectorized normal CDF — Abramowitz & Stegun rational approx, max err ~1.5e-7."""
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = np.sign(x)
    x_abs = np.abs(x)
    t = 1.0 / (1.0 + p * x_abs)
    t2 = t * t
    y = 1.0 - (a1 * t + a2 * t2 + a3 * t2 * t + a4 * t2 * t2 + a5 * t2 * t2 * t) * np.exp(-0.5 * x_abs * x_abs)
    return np.where(sign >= 0, 0.5 * (1.0 + y), 0.5 * (1.0 - y))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) * _INV_SQRT2PI


@dataclass
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    iv: float = 0.0
    theoretical_price: float = 0.0


def compute_greeks(
    spot: float,
    strike: float,
    dte_days: float,
    iv: float,
    option_type: str = "CE",
    r: float = RISK_FREE_RATE,
) -> Greeks:
    if iv <= 0 or dte_days <= 0 or spot <= 0 or strike <= 0:
        return Greeks(iv=iv)

    T = dte_days / 365.0
    sigma = iv / 100.0

    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    nd1 = _norm_cdf(d1)
    nd2 = _norm_cdf(d2)
    npd1 = _norm_pdf(d1)

    if option_type == "CE":
        delta = nd1
        theo = spot * nd1 - strike * math.exp(-r * T) * nd2
    else:
        delta = nd1 - 1
        theo = strike * math.exp(-r * T) * (1 - nd2) - spot * (1 - nd1)

    gamma = npd1 / (spot * sigma * math.sqrt(T))

    theta_annual = (
        -(spot * npd1 * sigma) / (2 * math.sqrt(T))
        - r * strike * math.exp(-r * T) * nd2
    ) if option_type == "CE" else (
        -(spot * npd1 * sigma) / (2 * math.sqrt(T))
        + r * strike * math.exp(-r * T) * (1 - nd2)
    )
    theta_daily = theta_annual / 365.0

    vega_val = spot * npd1 * math.sqrt(T) / 100.0

    return Greeks(
        delta=round(delta, 4),
        gamma=round(gamma, 6),
        theta=round(theta_daily, 2),
        vega=round(vega_val, 2),
        iv=iv,
        theoretical_price=round(max(theo, 0), 2),
    )


@dataclass
class GreeksArray:
    """Vectorized Greeks for N options at once."""
    delta: np.ndarray
    gamma: np.ndarray
    theta: np.ndarray
    vega: np.ndarray
    iv: np.ndarray
    theoretical_price: np.ndarray

    def __len__(self) -> int:
        return len(self.delta)

    def to_greeks_list(self) -> list[Greeks]:
        return [
            Greeks(
                delta=round(float(self.delta[i]), 4),
                gamma=round(float(self.gamma[i]), 6),
                theta=round(float(self.theta[i]), 2),
                vega=round(float(self.vega[i]), 2),
                iv=round(float(self.iv[i]), 2),
                theoretical_price=round(float(self.theoretical_price[i]), 2),
            )
            for i in range(len(self))
        ]


def compute_greeks_vectorized(
    spots: np.ndarray,
    strikes: np.ndarray,
    dte_days: np.ndarray,
    ivs: np.ndarray,
    is_call: np.ndarray,
    r: float = RISK_FREE_RATE,
) -> GreeksArray:
    """Sub-ms vectorized Black-Scholes for batch WebSocket ticks.

    All inputs are 1-D float64 arrays of equal length.
    is_call: boolean array (True=CE, False=PE).
    """
    n = len(spots)
    valid = (ivs > 0) & (dte_days > 0) & (spots > 0) & (strikes > 0)

    delta = np.zeros(n)
    gamma = np.zeros(n)
    theta = np.zeros(n)
    vega = np.zeros(n)
    theo = np.zeros(n)

    if not np.any(valid):
        return GreeksArray(delta, gamma, theta, vega, ivs.copy(), theo)

    s = spots[valid]
    k = strikes[valid]
    t = dte_days[valid] / 365.0
    sigma = ivs[valid] / 100.0
    call = is_call[valid]

    sqrt_t = np.sqrt(t)
    d1 = (np.log(s / k) + (r + 0.5 * sigma**2) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    nd1 = _np_norm_cdf(d1)
    nd2 = _np_norm_cdf(d2)
    npd1 = np.exp(-0.5 * d1**2) * _INV_SQRT2PI

    exp_rt = np.exp(-r * t)

    d_call = nd1
    d_put = nd1 - 1.0
    delta[valid] = np.where(call, d_call, d_put)

    theo_call = s * nd1 - k * exp_rt * nd2
    theo_put = k * exp_rt * (1.0 - nd2) - s * (1.0 - nd1)
    theo[valid] = np.maximum(np.where(call, theo_call, theo_put), 0.0)

    gamma[valid] = npd1 / (s * sigma * sqrt_t)

    theta_common = -(s * npd1 * sigma) / (2.0 * sqrt_t)
    theta_call = (theta_common - r * k * exp_rt * nd2) / 365.0
    theta_put = (theta_common + r * k * exp_rt * (1.0 - nd2)) / 365.0
    theta[valid] = np.where(call, theta_call, theta_put)

    vega[valid] = s * npd1 * sqrt_t / 100.0

    return GreeksArray(delta, gamma, theta, vega, ivs.copy(), theo)


def dte_from_expiry(expiry: str) -> float:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)
    exp_date = datetime.strptime(expiry, "%Y-%m-%d").replace(
        hour=15, minute=30, tzinfo=IST)
    diff = (exp_date - now).total_seconds()
    return max(diff / 86400, 0.01)


def should_exit_by_theta(
    theta: float,
    current_price: float,
    entry_price: float,
    dte_days: float,
) -> tuple[bool, str]:
    if dte_days <= 1:
        return True, "EXPIRY_DAY_EXIT"

    if dte_days <= 2 and current_price < entry_price * 0.3:
        return True, "THETA_DECAY_NEAR_EXPIRY"

    if theta < 0 and abs(theta) > current_price * 0.15:
        return True, "RAPID_THETA_DECAY"

    return False, ""


GAMMA_RISK_THRESHOLD = 0.15


def should_exit_spread_by_greeks(
    sell_greeks: Greeks,
    hedge_greeks: Greeks,
    net_credit: float,
    current_net_premium: float,
    dte_days: float,
    spot: float = 0.0,
) -> tuple[bool, str]:
    if dte_days <= 1:
        return True, "EXPIRY_DAY_EXIT"

    profit_captured = net_credit - current_net_premium
    if profit_captured >= net_credit * 0.70:
        return True, "THETA_TARGET_70PCT"

    net_theta = abs(sell_greeks.theta) - abs(hedge_greeks.theta)
    if dte_days <= 3 and net_theta < 0:
        return True, "NEGATIVE_THETA_SPREAD"

    # Raw gamma scales as 1/spot, so normalise to "delta change per 1% move"
    # to get a threshold that means the same thing on NIFTY and BANKNIFTY.
    if spot > 0 and dte_days <= 2:
        net_gamma = (abs(sell_greeks.gamma) - abs(hedge_greeks.gamma)) * spot * 0.01
        if net_gamma > GAMMA_RISK_THRESHOLD:
            return True, "GAMMA_RISK_NEAR_EXPIRY"

    return False, ""
