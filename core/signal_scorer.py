"""
Signal Scoring & Selection Engine — v5 (15-factor)

Scores each signal 0-100 based on:
  1. ADX strength (trend momentum)                    — 9%
  2. VWAP proximity (entry quality)                   — 6%
  3. Risk/Reward ratio (Chandelier stop vs potential)  — 9%
  4. SuperTrend confidence (distance from ST line)     — 6%
  5. Volume surge (institutional interest)             — 8%
  6. RSI momentum (not overbought/oversold)            — 7%
  7. Bollinger Band squeeze (volatility breakout)      — 4%
  8. EMA alignment (9/21 crossover)                    — 4%
  9. ORB breakout (opening range)                      — 4%
  10. Confluence count (how many factors agree)         — 9%
  11. Time-of-day quality (ML book: minute-of-day)     — 8%
  12. Momentum spread (acceleration detection)         — 4%
  13. Previous day H/L breakout (institutional ref)    — 9%
  14. Volatility regime quality (trending vs choppy)   — 6%
  15. VWAP band entry quality (institutional levels)   — 7%

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
    rsi_score: float = 0.0
    bb_score: float = 0.0
    ema_score: float = 0.0
    orb_score: float = 0.0
    confluence_score: float = 0.0
    time_score: float = 0.0
    momentum_spread_score: float = 0.0
    pdhl_score: float = 0.0
    regime_score: float = 0.0
    vwap_band_score: float = 0.0
    total_score: float = 0.0
    confidence: str = "LOW"
    category: str = ""
    correlation_group: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(IST))

    def compute_total(self):
        self.total_score = (
            self.adx_score * 0.09 +
            self.vwap_score * 0.06 +
            self.rr_score * 0.09 +
            self.st_score * 0.06 +
            self.volume_score * 0.08 +
            self.rsi_score * 0.07 +
            self.bb_score * 0.04 +
            self.ema_score * 0.04 +
            self.orb_score * 0.04 +
            self.confluence_score * 0.09 +
            self.time_score * 0.08 +
            self.momentum_spread_score * 0.04 +
            self.pdhl_score * 0.09 +
            self.regime_score * 0.06 +
            self.vwap_band_score * 0.07
        )
        if self.total_score >= 65:
            self.confidence = "HIGH"
        elif self.total_score >= 52:
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
    if "FUT" in upper:
        return "INDEX_FUTURES"
    return "INDICES"


def _is_bullish(side: str) -> bool:
    """Determine market direction from signal side.
    BUY/BUY_CE/SELL_PE = bullish, SELL/BUY_PE/SELL_CE = bearish."""
    s = side.upper()
    if "PE" in s:
        return "SELL" in s
    if "CE" in s:
        return "BUY" in s
    return "BUY" in s


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
    rsi_value: float = 50.0,
    rsi_ob: float = 70.0,
    rsi_os: float = 30.0,
    bb_squeeze: bool = False,
    ema_aligned: bool = False,
    orb_breakout: bool = False,
    confluence_count: int = 0,
    rsi_divergence: bool = False,
    signal_hour: int = -1,
    signal_minute: int = -1,
    momentum_spread: float = 0.0,
    prev_day_high: float = 0.0,
    prev_day_low: float = 0.0,
    volatility_regime: str = "NORMAL",
    signal_repeat_count: int = 0,
    vwap_band_position: float = 50.0,
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

    # 2. VWAP Score (0-100): alignment with VWAP direction
    bullish = _is_bullish(side)
    if close_price > 0 and vwap_price > 0:
        vwap_dist_pct = abs(close_price - vwap_price) / vwap_price * 100
        price_above_vwap = close_price > vwap_price

        if (bullish and price_above_vwap) or (not bullish and not price_above_vwap):
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

    # 3. Risk/Reward Score (0-100): chandelier stop vs ATR target
    if close_price > 0 and chandelier_stop > 0 and atr_value > 0:
        risk = abs(close_price - chandelier_stop)
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

    # 4. SuperTrend Score (0-100): distance = conviction
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

    # 6. RSI Score (0-100): momentum quality
    if bullish:
        if 40 <= rsi_value <= 60:
            scored.rsi_score = 90
        elif 30 <= rsi_value < 40:
            scored.rsi_score = 75
        elif 60 < rsi_value <= 70:
            scored.rsi_score = 60
        elif rsi_value > rsi_ob:
            scored.rsi_score = 15
        elif rsi_value < rsi_os:
            scored.rsi_score = 50
        else:
            scored.rsi_score = 40
    else:
        if 40 <= rsi_value <= 60:
            scored.rsi_score = 90
        elif 60 < rsi_value <= 70:
            scored.rsi_score = 75
        elif 30 <= rsi_value < 40:
            scored.rsi_score = 60
        elif rsi_value < rsi_os:
            scored.rsi_score = 15
        elif rsi_value > rsi_ob:
            scored.rsi_score = 50
        else:
            scored.rsi_score = 40
    if rsi_divergence:
        scored.rsi_score = min(100, scored.rsi_score + 20)

    # 7. Bollinger Band Score (0-100): squeeze = breakout potential
    scored.bb_score = 85 if bb_squeeze else 45

    # 8. EMA Score (0-100): 9/21 alignment
    scored.ema_score = 90 if ema_aligned else 20

    # 9. ORB Score (0-100): opening range breakout
    scored.orb_score = 85 if orb_breakout else 40

    # 10. Confluence Score (0-100): how many factors agree
    if confluence_count >= 6:
        scored.confluence_score = 100
    elif confluence_count >= 5:
        scored.confluence_score = 85
    elif confluence_count >= 4:
        scored.confluence_score = 65
    elif confluence_count >= 3:
        scored.confluence_score = 40
    else:
        scored.confluence_score = 15

    # 11. Time-of-day Score (0-100): NSE intraday edge windows
    if signal_hour >= 0:
        t = signal_hour * 60 + (signal_minute if signal_minute >= 0 else 0)
        if 570 <= t < 630:       # 9:30-10:30 — trend establishment
            scored.time_score = 90
        elif 630 <= t < 690:     # 10:30-11:30 — still good
            scored.time_score = 75
        elif 690 <= t < 810:     # 11:30-13:30 — choppy lunch
            scored.time_score = 30
        elif 810 <= t < 870:     # 13:30-14:30 — afternoon revival
            scored.time_score = 65
        elif 870 <= t < 910:     # 14:30-15:10 — closing momentum
            scored.time_score = 80
        else:
            scored.time_score = 40
    else:
        scored.time_score = 50

    # 12. Momentum Spread Score (0-100): acceleration detection
    if momentum_spread != 0:
        if (bullish and momentum_spread > 0) or (not bullish and momentum_spread < 0):
            abs_spread = abs(momentum_spread)
            if abs_spread > 0.5:
                scored.momentum_spread_score = 95
            elif abs_spread > 0.3:
                scored.momentum_spread_score = 80
            elif abs_spread > 0.1:
                scored.momentum_spread_score = 60
            else:
                scored.momentum_spread_score = 45
        else:
            scored.momentum_spread_score = 20
    else:
        scored.momentum_spread_score = 50

    # 13. Previous Day High/Low Breakout (0-100): institutional reference levels
    if close_price > 0 and prev_day_high > 0 and prev_day_low > 0:
        if bullish:
            if close_price > prev_day_high:
                scored.pdhl_score = 95
            elif close_price > (prev_day_high + prev_day_low) / 2:
                scored.pdhl_score = 65
            else:
                scored.pdhl_score = 25
        else:
            if close_price < prev_day_low:
                scored.pdhl_score = 95
            elif close_price < (prev_day_high + prev_day_low) / 2:
                scored.pdhl_score = 65
            else:
                scored.pdhl_score = 25
    else:
        scored.pdhl_score = 50

    # 14. Volatility Regime Quality (0-100): trending markets score higher
    regime_scores = {"TRENDING": 90, "NORMAL": 60, "HIGH_VOL": 40, "CHOPPY": 15}
    scored.regime_score = regime_scores.get(volatility_regime, 60)

    # 15. VWAP Band Entry Quality (0-100): where price sits within VWAP bands
    # For buys: price near the lower VWAP band = institutional pullback entry
    # For sells: price near the upper VWAP band = institutional rejection entry
    if bullish:
        if vwap_band_position <= 20:
            scored.vwap_band_score = 95
        elif vwap_band_position <= 40:
            scored.vwap_band_score = 80
        elif vwap_band_position <= 60:
            scored.vwap_band_score = 60
        elif vwap_band_position <= 80:
            scored.vwap_band_score = 40
        else:
            scored.vwap_band_score = 20
    else:
        if vwap_band_position >= 80:
            scored.vwap_band_score = 95
        elif vwap_band_position >= 60:
            scored.vwap_band_score = 80
        elif vwap_band_position >= 40:
            scored.vwap_band_score = 60
        elif vwap_band_position >= 20:
            scored.vwap_band_score = 40
        else:
            scored.vwap_band_score = 20

    # Signal repeat penalty: decay score if same signal fires repeatedly
    if signal_repeat_count >= 5:
        scored.regime_score = max(0, scored.regime_score - 30)
    elif signal_repeat_count >= 3:
        scored.regime_score = max(0, scored.regime_score - 15)

    scored.compute_total()
    return scored


def select_best_signals(
    scored_signals: list[ScoredSignal],
    max_positions: int = 10,
    min_score: float = 35.0,
    skip_correlation: bool = False,
) -> list[ScoredSignal]:
    viable = [s for s in scored_signals if s.total_score >= min_score]
    viable.sort(key=lambda s: s.total_score, reverse=True)

    selected: list[ScoredSignal] = []
    used_groups: set[str] = set()

    for sig in viable:
        if len(selected) >= max_positions:
            break
        if not skip_correlation and sig.correlation_group in used_groups:
            log.info("Skipping %s (correlated with already selected %s group)",
                     sig.symbol, sig.correlation_group)
            continue
        selected.append(sig)
        if not skip_correlation:
            used_groups.add(sig.correlation_group)

    return selected


def format_score_report(scored: ScoredSignal) -> str:
    return (
        f"[{scored.confidence}] {scored.side} {scored.symbol} "
        f"Score={scored.total_score:.0f}/100 "
        f"(ADX={scored.adx_score:.0f} VWAP={scored.vwap_score:.0f} "
        f"R:R={scored.rr_score:.0f} ST={scored.st_score:.0f} "
        f"Vol={scored.volume_score:.0f} RSI={scored.rsi_score:.0f} "
        f"BB={scored.bb_score:.0f} EMA={scored.ema_score:.0f} "
        f"ORB={scored.orb_score:.0f} Conf={scored.confluence_score:.0f} "
        f"Time={scored.time_score:.0f} Mom={scored.momentum_spread_score:.0f} "
        f"PDH={scored.pdhl_score:.0f} Reg={scored.regime_score:.0f} "
        f"VB={scored.vwap_band_score:.0f}) "
        f"[{scored.category}]"
    )
