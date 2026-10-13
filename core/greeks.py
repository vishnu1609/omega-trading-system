"""
Black-Scholes Greeks Calculator

Computes delta, gamma, theta, vega, and theoretical price for
European-style index options. Uses IV from Dhan chain data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

RISK_FREE_RATE = 0.07
TRADING_DAYS_PER_YEAR = 252


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


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
