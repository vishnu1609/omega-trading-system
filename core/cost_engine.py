"""
Transaction Cost Engine — computes all-in cost for Indian index options.

Covers: brokerage, STT (0.15% sell-side Apr 2026+), exchange txn charges,
SEBI turnover fee, stamp duty, GST on brokerage+exchange.

Used by ML engine for cost-adjusted EV gate and by strategy for
realistic P&L tracking.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeCosts:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi_fee: float
    stamp_duty: float
    gst: float
    slippage_est: float
    total: float
    cost_pct: float  # total as % of turnover


# Apr 2026 SEBI rates for equity options
STT_SELL_PCT = 0.15 / 100       # 0.15% on sell-side premium
EXCHANGE_TXN_PCT = 0.03553 / 100  # NSE transaction charges
SEBI_PER_CRORE = 10.0            # ₹10 per crore turnover
STAMP_DUTY_PCT = 0.003 / 100     # 0.003% on buy side
GST_PCT = 18.0 / 100             # 18% GST on (brokerage + exchange charges)
BROKERAGE_PER_ORDER = 20.0       # flat ₹20 per executed order (Dhan)
DEFAULT_SLIPPAGE_TICKS = 2
DEFAULT_TICK_SIZE = 0.05


def compute_trade_cost(
    entry_price: float,
    exit_price: float,
    quantity: int,
    tick_size: float = DEFAULT_TICK_SIZE,
    slippage_ticks: int = DEFAULT_SLIPPAGE_TICKS,
) -> TradeCosts:
    buy_turnover = entry_price * quantity
    sell_turnover = exit_price * quantity
    total_turnover = buy_turnover + sell_turnover

    brokerage = BROKERAGE_PER_ORDER * 2  # entry + exit

    stt = sell_turnover * STT_SELL_PCT

    exchange_txn = total_turnover * EXCHANGE_TXN_PCT

    sebi_fee = total_turnover / 1_00_00_000 * SEBI_PER_CRORE

    stamp_duty = buy_turnover * STAMP_DUTY_PCT

    gst = (brokerage + exchange_txn) * GST_PCT

    slippage_est = slippage_ticks * tick_size * quantity * 2  # both legs

    total = brokerage + stt + exchange_txn + sebi_fee + stamp_duty + gst + slippage_est
    cost_pct = (total / total_turnover * 100) if total_turnover > 0 else 0

    return TradeCosts(
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange_txn=round(exchange_txn, 2),
        sebi_fee=round(sebi_fee, 2),
        stamp_duty=round(stamp_duty, 2),
        gst=round(gst, 2),
        slippage_est=round(slippage_est, 2),
        total=round(total, 2),
        cost_pct=round(cost_pct, 4),
    )


def estimate_round_trip_cost(premium: float, quantity: int) -> float:
    """Quick estimate: total cost of buying then selling at same price."""
    if premium <= 0 or quantity <= 0:
        return 0.0
    costs = compute_trade_cost(premium, premium, quantity)
    return costs.total


def cost_adjusted_ev(
    p_win: float,
    avg_win: float,
    avg_loss: float,
    premium: float,
    quantity: int,
) -> float:
    """Net expected value per trade after all transaction costs."""
    cost = estimate_round_trip_cost(premium, quantity)
    gross_ev = p_win * avg_win - (1 - p_win) * abs(avg_loss)
    return gross_ev - cost


def breakeven_win_rate(avg_win: float, avg_loss: float,
                       premium: float, quantity: int) -> float:
    """Minimum win rate for positive EV after costs."""
    cost = estimate_round_trip_cost(premium, quantity)
    denom = avg_win + abs(avg_loss)
    if denom <= 0:
        return 1.0
    return (abs(avg_loss) + cost) / denom
