"""
Risk Engine — Non-negotiable safety layer.

Every trade must pass ALL 7 gates before execution. No exceptions.

Gates (in order):
  1. Bot not killed for the day (daily loss breached)
  2. Daily loss limit not breached
  3. Within trading session
  4. Not in cooldown period
  5. Concurrent position limit
  6. Stop-loss cap (8-10% of trade amount, scaled by confluence score)
  7. Category toggle (Index Futures disabled in live mode) + margin check

Trailing stop logic adapted from OpenAlgo's services/risk/position.py:
- Peak-based trail arming (uses highest/lowest excursion, not current price)
- Stop-beyond-peak guard (trail can never place SL past the best traded price)
- NaN/zero/missing price protection (zero is not a price on Indian exchanges)
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.broker_base import BrokerBase
from core.position_manager import PositionManager

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

def _is_index_future(symbol: str) -> bool:
    upper = symbol.upper()
    return upper.endswith("FUT") or "FUT" in upper.split("_")[-1:]


class RiskEngine:
    def __init__(
        self,
        position_manager: PositionManager,
        broker: BrokerBase | None = None,
        max_daily_loss: float = 3000,
        max_concurrent_positions: int = 2,
        max_daily_trades: int = 6,
        sl_buffer_pct: float = 1.5,
        trail_pct: float = 0.15,
        cooldown_after_losses: int = 2,
        cooldown_minutes: int = 30,
        margin_buffer_pct: float = 10,
        session_start: str = "09:16",
        square_off_time: str = "15:10",
        mode: str = "paper",
        allow_index_futures_live: bool = False,
    ):
        self.pm = position_manager
        self.broker = broker
        self.max_daily_loss = max_daily_loss
        self.max_concurrent = max_concurrent_positions
        self.max_daily_trades = max_daily_trades
        self.sl_buffer_pct = sl_buffer_pct
        self.trail_pct = trail_pct
        self.cooldown_after_losses = cooldown_after_losses
        self.cooldown_minutes = cooldown_minutes
        self.margin_buffer_pct = margin_buffer_pct
        self.mode = mode
        self.allow_index_futures_live = allow_index_futures_live

        sh, sm = map(int, session_start.split(":"))
        self.session_start = dt_time(sh, sm, tzinfo=IST)
        eh, em = map(int, square_off_time.split(":"))
        self.square_off_time = dt_time(eh, em, tzinfo=IST)

        self._instrument_sessions: dict[str, tuple[dt_time, dt_time]] = {}

        self.max_exposure_pct: float = 80.0
        self.symbol_cooldown_minutes: int = 15

        self.last_reason: str = ""
        self._cooldown_until: datetime | None = None
        self._bot_killed = False
        self._daily_trade_count: int = 0
        self._daily_pnl_history: list[float] = []
        self._peak_capital: float = 0
        self._gate_lock = threading.Lock()
        self._symbol_cooldowns: dict[str, datetime] = {}

    def register_instrument_session(self, name: str, start: str, square_off: str) -> None:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, square_off.split(":"))
        self._instrument_sessions[name.upper()] = (
            dt_time(sh, sm, tzinfo=IST),
            dt_time(eh, em, tzinfo=IST),
        )

    def get_session_times(self, instrument: str = "") -> tuple[dt_time, dt_time]:
        if instrument and instrument.upper() in self._instrument_sessions:
            return self._instrument_sessions[instrument.upper()]
        return self.session_start, self.square_off_time

    def allow_new_trade(self, estimated_margin: float = 0,
                        instrument: str = "", symbol: str = "",
                        entry_price: float = 0, sl_price: float = 0,
                        confluence_score: float = 0) -> bool:
        with self._gate_lock:
            now = datetime.now(IST)
            return self._run_gates(now, estimated_margin, instrument, symbol,
                                   entry_price, sl_price, confluence_score)

    def _run_gates(self, now: datetime, estimated_margin: float,
                   instrument: str, symbol: str,
                   entry_price: float, sl_price: float,
                   confluence_score: float) -> bool:
        # Gate 1: Bot kill switch
        if self._bot_killed:
            self.last_reason = "Bot killed for the day (daily loss limit breached)"
            return False

        # Gate 2: Daily loss check
        if self.pm.daily_pnl <= -self.max_daily_loss:
            self._bot_killed = True
            self.last_reason = f"Daily loss limit breached: Rs.{self.pm.daily_pnl:,.2f}"
            log.critical(self.last_reason)
            return False

        # Gate 3: Session check (per-instrument if available)
        sess_start, sess_end = self.get_session_times(instrument)
        if now.timetz() < sess_start:
            self.last_reason = f"Before session start ({sess_start}) for {instrument or 'default'}"
            return False
        if now.timetz() >= sess_end:
            self.last_reason = f"Past square-off time ({sess_end}) for {instrument or 'default'}"
            return False

        # Gate 4: Cooldown after consecutive losses
        if self._cooldown_until and now < self._cooldown_until:
            remaining = (self._cooldown_until - now).total_seconds() / 60
            self.last_reason = f"Cooldown active ({remaining:.0f} min remaining)"
            return False
        if self.pm.consecutive_losses >= self.cooldown_after_losses:
            self._cooldown_until = now + timedelta(minutes=self.cooldown_minutes)
            self.last_reason = (
                f"{self.pm.consecutive_losses} consecutive losses — "
                f"cooldown until {self._cooldown_until.strftime('%H:%M')}"
            )
            log.warning(self.last_reason)
            return False

        # Gate 5: Max concurrent positions
        if self.pm.open_count >= self.max_concurrent:
            self.last_reason = f"Max concurrent positions ({self.max_concurrent}) reached"
            return False

        # Gate 5b: Max daily trades (prevent overtrading)
        if self._daily_trade_count >= self.max_daily_trades:
            self.last_reason = (
                f"Max daily trades ({self.max_daily_trades}) reached — "
                f"quality over quantity, done for the day"
            )
            return False

        # Gate 5c: Progressive loss reduction — halve after 50% daily loss used
        half_loss = self.max_daily_loss / 2
        if self.pm.daily_pnl <= -half_loss and self.pm.daily_pnl > -self.max_daily_loss:
            if self.pm.open_count >= 1:
                self.last_reason = (
                    f"Half daily loss used (₹{abs(self.pm.daily_pnl):,.0f}/₹{self.max_daily_loss:,.0f}) "
                    f"— max 1 concurrent position until recovery"
                )
                return False

        # Gate 6: Stop-loss cap (8-10% of trade amount by confluence score)
        if entry_price > 0 and sl_price > 0:
            sl_pct = abs(entry_price - sl_price) / entry_price * 100
            max_sl_pct = 8.0 if confluence_score >= 75 else 10.0
            if sl_pct > max_sl_pct:
                self.last_reason = (
                    f"SL cap breached: SL is {sl_pct:.1f}% of entry "
                    f"(max {max_sl_pct:.0f}% for score {confluence_score:.0f})"
                )
                return False

        # Gate 7: Category toggle (Index Futures in live mode)
        check_symbol = symbol or instrument
        if check_symbol and self.mode == "live":
            if _is_index_future(check_symbol) and not self.allow_index_futures_live:
                self.last_reason = f"Index Futures disabled in live mode ({check_symbol})"
                return False

        # Gate 7b: Margin check (if broker available and margin estimate provided)
        if self.broker and estimated_margin > 0:
            available = self.broker.get_available_margin()
            required = estimated_margin * (1 + self.margin_buffer_pct / 100)
            if available < required:
                self.last_reason = (
                    f"Insufficient margin: need Rs.{required:,.0f} "
                    f"(incl {self.margin_buffer_pct}% buffer), have Rs.{available:,.0f}"
                )
                log.warning(self.last_reason)
                return False

        # Gate 8: Portfolio exposure cap
        if estimated_margin > 0 and self.broker:
            try:
                total_margin = self.broker.get_available_margin()
                open_exposure = sum(
                    p.entry_price * p.quantity for p in self.pm.open_positions)
                if total_margin > 0:
                    exposure_pct = (open_exposure + estimated_margin) / total_margin * 100
                    if exposure_pct > self.max_exposure_pct:
                        self.last_reason = (
                            f"Portfolio exposure cap: {exposure_pct:.0f}% "
                            f"(max {self.max_exposure_pct:.0f}%)"
                        )
                        return False
            except Exception:
                pass

        # Gate 9: Per-symbol post-SL cooldown
        check_sym = symbol or instrument
        if check_sym and check_sym in self._symbol_cooldowns:
            cooldown_end = self._symbol_cooldowns[check_sym]
            if now < cooldown_end:
                remaining = (cooldown_end - now).total_seconds() / 60
                self.last_reason = (
                    f"Symbol cooldown: {check_sym} had SL hit — "
                    f"blocked for {remaining:.0f} more min"
                )
                return False
            else:
                del self._symbol_cooldowns[check_sym]

        self.last_reason = ""
        return True

    def record_symbol_sl(self, symbol: str) -> None:
        self._symbol_cooldowns[symbol] = (
            datetime.now(IST) + timedelta(minutes=self.symbol_cooldown_minutes)
        )

    def clamp_sl_price(self, entry_price: float, sl_price: float,
                       direction: str, confluence_score: float = 0) -> float:
        if entry_price <= 0 or sl_price <= 0:
            return sl_price
        max_sl_pct = 8.0 if confluence_score >= 75 else 10.0
        max_distance = entry_price * max_sl_pct / 100
        if direction == "LONG":
            clamped = max(entry_price - max_distance, 0.05)
            return round(max(sl_price, clamped), 2)
        else:
            clamped = entry_price + max_distance
            return round(min(sl_price, clamped), 2)

    def should_square_off(self, instrument: str = "") -> bool:
        now = datetime.now(IST)
        _, sess_end = self.get_session_times(instrument)
        return now.timetz() >= sess_end

    def all_sessions_done(self) -> bool:
        now = datetime.now(IST)
        if now.timetz() < self.square_off_time:
            return False
        for _, (_, sess_end) in self._instrument_sessions.items():
            if now.timetz() < sess_end:
                return False
        return True

    def is_daily_loss_breached(self) -> bool:
        return self._bot_killed or self.pm.daily_pnl <= -self.max_daily_loss

    def compute_sl_price(self, entry_price: float, direction: str,
                         confluence_score: float = 0) -> float:
        buffer = entry_price * self.sl_buffer_pct / 100
        if direction == "LONG":
            raw_sl = round(max(entry_price - buffer, 0.05), 2)
        else:
            raw_sl = round(entry_price + buffer, 2)
        return self.clamp_sl_price(entry_price, raw_sl, direction, confluence_score)

    def compute_trail_sl(self, current_price: float, entry_price: float,
                         existing_sl: float, direction: str,
                         trail_pct: float | None = None,
                         peak_price: float = 0.0) -> float:
        """Compute trailing SL with peak-based arming (OpenAlgo pattern).

        Args:
            peak_price: Highest price seen (LONG) or lowest (SHORT). When
                provided, the trail is calculated from the peak — not the
                current price — so it can never silently widen after a pullback.
        """
        if trail_pct is None:
            trail_pct = self.trail_pct
        if not _is_valid_price(current_price) or not _is_valid_price(entry_price):
            return existing_sl

        is_long = direction == "LONG"
        best = peak_price if _is_valid_price(peak_price) else current_price

        if is_long:
            best = max(best, current_price)
            favourable = best - entry_price
            if favourable <= 0:
                return existing_sl
            new_sl = best - (best * trail_pct / 100)
            new_sl = min(new_sl, best)
            return max(round(new_sl, 2), existing_sl)
        else:
            best = min(best, current_price) if _is_valid_price(peak_price) else current_price
            favourable = entry_price - best
            if favourable <= 0:
                return existing_sl
            new_sl = best + (best * trail_pct / 100)
            new_sl = max(new_sl, best)
            if not _is_valid_price(existing_sl):
                return round(new_sl, 2)
            return min(round(new_sl, 2), existing_sl)

    def compute_tp_price(self, entry_price: float, direction: str,
                         rr_ratio: float = 2.0) -> float:
        risk = entry_price * self.sl_buffer_pct / 100
        reward = risk * rr_ratio
        if direction == "LONG":
            return round(entry_price + reward, 2)
        return round(max(entry_price - reward, 0.05), 2)

    def record_trade_taken(self) -> None:
        self._daily_trade_count += 1

    def get_risk_metrics(self, capital: float = 100000) -> dict:
        daily_pnl = self.pm.daily_pnl
        pnl_pct = (daily_pnl / capital * 100) if capital > 0 else 0
        loss_used_pct = (abs(min(0, daily_pnl)) / self.max_daily_loss * 100) if self.max_daily_loss > 0 else 0
        drawdown_recovery_needed = 0
        if daily_pnl < 0:
            remaining = capital + daily_pnl
            drawdown_recovery_needed = ((capital / remaining) - 1) * 100 if remaining > 0 else 999
        risk_of_ruin_zone = "SAFE"
        if loss_used_pct > 80:
            risk_of_ruin_zone = "CRITICAL"
        elif loss_used_pct > 50:
            risk_of_ruin_zone = "WARNING"
        elif loss_used_pct > 30:
            risk_of_ruin_zone = "ELEVATED"
        return {
            "daily_pnl": round(daily_pnl, 2),
            "daily_pnl_pct": round(pnl_pct, 2),
            "daily_loss_used_pct": round(loss_used_pct, 1),
            "drawdown_recovery_pct": round(drawdown_recovery_needed, 2),
            "trades_today": self._daily_trade_count,
            "trades_remaining": max(0, self.max_daily_trades - self._daily_trade_count),
            "consecutive_losses": self.pm.consecutive_losses,
            "risk_zone": risk_of_ruin_zone,
            "bot_killed": self._bot_killed,
        }

    def compute_r_multiple(self, entry_price: float, exit_price: float,
                           sl_price: float, direction: str) -> float:
        if entry_price <= 0 or sl_price <= 0:
            return 0.0
        initial_risk = abs(entry_price - sl_price)
        if initial_risk == 0:
            return 0.0
        if direction == "LONG":
            pnl = exit_price - entry_price
        else:
            pnl = entry_price - exit_price
        return round(pnl / initial_risk, 2)

    def kelly_position_size(self, win_rate: float, avg_win_r: float,
                            avg_loss_r: float, capital: float,
                            fraction: float = 0.5) -> float:
        if avg_loss_r == 0 or win_rate <= 0 or win_rate >= 1:
            return capital * 0.02
        rr_ratio = abs(avg_win_r / avg_loss_r)
        kelly_pct = (win_rate * rr_ratio - (1 - win_rate)) / rr_ratio
        kelly_pct = max(0, min(kelly_pct, 0.25))
        position_pct = kelly_pct * fraction
        return round(capital * position_pct, 2)

    def compute_atr_sl(self, entry_price: float, direction: str,
                       atr_value: float, multiplier: float = 1.5) -> float:
        if atr_value <= 0:
            return self.compute_sl_price(entry_price, direction)
        atr_distance = atr_value * multiplier
        if direction == "LONG":
            return round(max(entry_price - atr_distance, 0.05), 2)
        return round(entry_price + atr_distance, 2)

    def compute_atr_tp(self, entry_price: float, direction: str,
                       atr_value: float, multiplier: float = 3.0) -> float:
        if atr_value <= 0:
            return self.compute_tp_price(entry_price, direction)
        atr_distance = atr_value * multiplier
        if direction == "LONG":
            return round(entry_price + atr_distance, 2)
        return round(max(entry_price - atr_distance, 0.05), 2)

    def atr_position_size(self, capital: float, risk_pct: float,
                          entry_price: float, atr_value: float,
                          sl_multiplier: float = 1.5,
                          lot_size: int = 75,
                          fee_rate: float = 0.0005) -> int:
        if atr_value <= 0 or entry_price <= 0:
            return lot_size
        risk_amount = capital * risk_pct / 100
        risk_amount *= (1 - fee_rate * 3)
        atr_distance = atr_value * sl_multiplier
        raw_qty = risk_amount / atr_distance
        lots = max(1, int(raw_qty / lot_size))
        return lots * lot_size

    def time_decay_tp(self, original_tp: float, entry_price: float,
                      direction: str, hold_minutes: float) -> float:
        """Reduce TP target as hold time increases (Freqtrade ROI table pattern).

        Options lose value over time due to theta, so waiting for
        a large TP becomes increasingly unlikely.
        """
        if hold_minutes < 15 or original_tp <= 0 or entry_price <= 0:
            return original_tp

        tp_distance = abs(original_tp - entry_price)

        if hold_minutes < 30:
            decay = 0.75
        elif hold_minutes < 60:
            decay = 0.50
        elif hold_minutes < 120:
            decay = 0.25
        else:
            decay = 0.10

        reduced_distance = tp_distance * decay
        if direction == "LONG":
            return round(entry_price + reduced_distance, 2)
        return round(max(entry_price - reduced_distance, 0.05), 2)

    def evaluate_trail(self, current_price: float, entry_price: float,
                       existing_sl: float, direction: str,
                       peak_price: float = 0.0,
                       trail_step: float = 0.0,
                       trail_trigger: float = 1.0,
                       atr_value: float = 0.0) -> dict:
        """ATR-based trailing stop with phased progression.

        Phases (based on profit in ATR multiples):
          < 1.0× ATR: no trail change (let trade develop)
          1.0× ATR: move SL to breakeven
          2.0× ATR: lock 1.0× ATR profit
          3.0× ATR+: tight trail at 1.5× ATR from peak
        """
        if not _is_valid_price(current_price) or not _is_valid_price(entry_price):
            return {"sl": existing_sl, "peak": peak_price,
                    "trail_armed": False, "stop_hit": False,
                    "detail": "invalid price"}

        is_long = direction == "LONG"
        atr = atr_value if atr_value > 0 else entry_price * 0.02

        if is_long:
            peak = max(peak_price, current_price) if _is_valid_price(peak_price) else current_price
            favourable = max(0, peak - entry_price)
        else:
            peak = min(peak_price, current_price) if _is_valid_price(peak_price) else current_price
            favourable = max(0, entry_price - peak)

        atr_multiples = favourable / atr if atr > 0 else 0
        sl = existing_sl
        detail = ""

        if atr_multiples >= 3.0:
            if is_long:
                candidate = peak - (1.5 * atr)
            else:
                candidate = peak + (1.5 * atr)
            detail = f"tight trail (3×ATR): {sl:.2f} → {candidate:.2f}"
        elif atr_multiples >= 2.0:
            if is_long:
                candidate = entry_price + (1.0 * atr)
            else:
                candidate = entry_price - (1.0 * atr)
            detail = f"lock profit (2×ATR): {sl:.2f} → {candidate:.2f}"
        elif atr_multiples >= 1.0:
            candidate = entry_price
            detail = f"breakeven (1×ATR): {sl:.2f} → {candidate:.2f}"
        else:
            candidate = sl

        candidate = round(candidate, 2)
        if is_long:
            if _is_valid_price(candidate) and candidate > sl:
                sl = candidate
            else:
                detail = ""
        else:
            if _is_valid_price(candidate) and (sl <= 0 or candidate < sl):
                sl = candidate
            else:
                detail = ""

        stop_hit = False
        if _is_valid_price(sl):
            stop_hit = (current_price <= sl) if is_long else (current_price >= sl)

        return {"sl": sl, "peak": peak, "trail_armed": atr_multiples >= 1.0,
                "stop_hit": stop_hit, "detail": detail}

    def reset_daily(self) -> None:
        self._bot_killed = False
        self._cooldown_until = None
        self._daily_trade_count = 0
        self._symbol_cooldowns.clear()
        self.last_reason = ""
        log.info("Risk engine reset for new trading day")


def _is_valid_price(value: Any) -> bool:
    """True when value is a usable, strictly positive, finite price.
    Adapted from OpenAlgo: zero is not a price on Indian exchanges."""
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0
