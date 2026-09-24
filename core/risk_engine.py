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
"""
from __future__ import annotations

import logging
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
        max_daily_loss: float = 5000,
        max_concurrent_positions: int = 2,
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

        self.last_reason: str = ""
        self._cooldown_until: datetime | None = None
        self._bot_killed = False
        self._gate_lock = threading.Lock()

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

        self.last_reason = ""
        return True

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
                         trail_pct: float | None = None) -> float:
        if trail_pct is None:
            trail_pct = self.trail_pct
        if direction == "LONG":
            move = current_price - entry_price
            if move <= 0:
                return existing_sl
            new_sl = current_price - (current_price * trail_pct / 100)
            return max(round(new_sl, 2), existing_sl)
        else:
            move = entry_price - current_price
            if move <= 0:
                return existing_sl
            new_sl = current_price + (current_price * trail_pct / 100)
            if existing_sl <= 0:
                return round(new_sl, 2)
            return min(round(new_sl, 2), existing_sl)

    def compute_tp_price(self, entry_price: float, direction: str,
                         rr_ratio: float = 2.0) -> float:
        risk = entry_price * self.sl_buffer_pct / 100
        reward = risk * rr_ratio
        if direction == "LONG":
            return round(entry_price + reward, 2)
        return round(max(entry_price - reward, 0.05), 2)

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

    def reset_daily(self) -> None:
        self._bot_killed = False
        self._cooldown_until = None
        self.last_reason = ""
        log.info("Risk engine reset for new trading day")
