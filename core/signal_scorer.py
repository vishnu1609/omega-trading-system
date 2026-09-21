"""
Signal Scoring & Selection Engine

Scores each signal 0-100 based on:
  1. ADX strength (trend momentum)
  2. VWAP proximity (entry quality)
  3. Risk/Reward ratio (Chandelier stop distance vs potential)
  4. SuperTrend confidence (how far price is from ST line)
  5. Volume surge (above-average volume = institutional interest)

When multiple signals fire, picks the best non-correlated set.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

CORRELATION_GROUPS = {
    "NSE_INDEX": ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"],
    "NSE_FUT": ["NIFTY_FUT", "BANKNIFTY_FUT"],
    "PRECIOUS": ["GOLD", "GOLDM", "SILVER", "SILVERM"],
    "ENERGY": ["CRUDEOIL", "CRUDEOILM", "NATURALGAS"],
}


@dataclass
class ScoredSignal:
    symbol: str
    side: str
    strategy: str
    asset: str
    raw_signal: dict
    adx_score: float = 0.0
    vwap_score: float = 0.0
    rr_score: float = 0.0
    st_score: float = 0.0
    volume_score: float = 0.0
    total_score: float = 0.0
    confidence: str = "LOW"
    category: str = ""
    correlation_group: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(IST))

    def compute_total(self):
        self.total_score = (
            self.adx_score * 0.25 +
            self.vwap_score * 0.20 +
            self.rr_score * 0.25 +
            self.st_score * 0.15 +
            self.volume_score * 0.15
        )
        if self.total_score >= 75:
            self.confidence = "HIGH"
        elif self.total_score >= 55:
            self.confidence = "MEDIUM"
        else:
            self.confidence = "LOW"


def _get_correlation_group(symbol: str) -> str:
    upper = symbol.upper()
    for group, members in CORRELATION_GROUPS.items():
        for m in members:
            if m in upper:
                return group
    return symbol


def _categorize(symbol: str) -> str:
    upper = symbol.upper()
    mcx = ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"]
    for m in mcx:
        if m in upper:
            return "MCX"
    if "FUT" in upper:
        return "INDEX_FUTURES"
    return "INDICES"


def score_signal(
    symbol: str,
    side: str,
    strategy: str,
    asset: str,
    raw_signal: dict,
    adx_value: float = 0.0,
    adx_threshold: float = 25.0,
    close_price: float = 0.0,
    vwap_price: float = 0.0,
    st_value: float = 0.0,
    chandelier_stop: float = 0.0,
    avg_volume: float = 0.0,
    current_volume: float = 0.0,
    atr_value: float = 0.0,
) -> ScoredSignal:

    scored = ScoredSignal(
        symbol=symbol,
        side=side,
        strategy=strategy,
        asset=asset,
        raw_signal=raw_signal,
        category=_categorize(symbol),
        correlation_group=_get_correlation_group(symbol),
    )

    # 1. ADX Score (0-100): higher ADX = stronger trend
    if adx_value > 0:
        if adx_value >= 50:
            scored.adx_score = 100
        elif adx_value >= 40:
            scored.adx_score = 85
        elif adx_value >= 30:
            scored.adx_score = 70
        elif adx_value >= adx_threshold:
            scored.adx_score = 55
        else:
            scored.adx_score = max(0, adx_value / adx_threshold * 40)

    # 2. VWAP Score (0-100): how well aligned price is with VWAP
    if close_price > 0 and vwap_price > 0:
        vwap_dist_pct = abs(close_price - vwap_price) / vwap_price * 100

        is_buy = "BUY" in side.upper()
        price_above_vwap = close_price > vwap_price

        if is_buy and price_above_vwap:
            if vwap_dist_pct < 0.3:
                scored.vwap_score = 95
            elif vwap_dist_pct < 0.6:
                scored.vwap_score = 80
            elif vwap_dist_pct < 1.0:
                scored.vwap_score = 60
            else:
                scored.vwap_score = max(20, 60 - vwap_dist_pct * 10)
        elif not is_buy and not price_above_vwap:
            if vwap_dist_pct < 0.3:
                scored.vwap_score = 95
            elif vwap_dist_pct < 0.6:
                scored.vwap_score = 80
            elif vwap_dist_pct < 1.0:
                scored.vwap_score = 60
            else:
                scored.vwap_score = max(20, 60 - vwap_dist_pct * 10)
        else:
            scored.vwap_score = 20

    # 3. Risk/Reward Score (0-100): chandelier stop distance vs 2x target
    if close_price > 0 and chandelier_stop > 0 and atr_value > 0:
        is_buy = "BUY" in side.upper()
        if is_buy:
            risk = abs(close_price - chandelier_stop)
            reward = atr_value * 2
        else:
            risk = abs(chandelier_stop - close_price)
            reward = atr_value * 2

        if risk > 0:
            rr_ratio = reward / risk
            if rr_ratio >= 3.0:
                scored.rr_score = 100
            elif rr_ratio >= 2.0:
                scored.rr_score = 80
            elif rr_ratio >= 1.5:
                scored.rr_score = 60
            elif rr_ratio >= 1.0:
                scored.rr_score = 40
            else:
                scored.rr_score = 15

    # 4. SuperTrend Score (0-100): distance from ST line = conviction
    if close_price > 0 and st_value > 0:
        st_dist_pct = abs(close_price - st_value) / close_price * 100
        if st_dist_pct > 2.0:
            scored.st_score = 90
        elif st_dist_pct > 1.0:
            scored.st_score = 70
        elif st_dist_pct > 0.5:
            scored.st_score = 55
        else:
            scored.st_score = 35

    # 5. Volume Score (0-100): current vs average
    if avg_volume > 0 and current_volume > 0:
        vol_ratio = current_volume / avg_volume
        if vol_ratio >= 2.0:
            scored.volume_score = 100
        elif vol_ratio >= 1.5:
            scored.volume_score = 80
        elif vol_ratio >= 1.0:
            scored.volume_score = 55
        elif vol_ratio >= 0.7:
            scored.volume_score = 30
        else:
            scored.volume_score = 10
    else:
        scored.volume_score = 55

    scored.compute_total()
    return scored


def select_best_signals(
    scored_signals: list[ScoredSignal],
    max_positions: int = 3,
    min_score: float = 40.0,
) -> list[ScoredSignal]:
    viable = [s for s in scored_signals if s.total_score >= min_score]
    viable.sort(key=lambda s: s.total_score, reverse=True)

    selected: list[ScoredSignal] = []
    used_groups: set[str] = set()

    for sig in viable:
        if len(selected) >= max_positions:
            break
        if sig.correlation_group in used_groups:
            log.info("Skipping %s (correlated with already selected %s group)",
                     sig.symbol, sig.correlation_group)
            continue
        selected.append(sig)
        used_groups.add(sig.correlation_group)

    return selected


def format_score_report(scored: ScoredSignal) -> str:
    return (
        f"[{scored.confidence}] {scored.side} {scored.symbol} "
        f"Score={scored.total_score:.0f}/100 "
        f"(ADX={scored.adx_score:.0f} VWAP={scored.vwap_score:.0f} "
        f"R:R={scored.rr_score:.0f} ST={scored.st_score:.0f} "
        f"Vol={scored.volume_score:.0f}) "
        f"[{scored.category}]"
    )
