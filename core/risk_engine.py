"""
Risk Engine — Non-negotiable safety layer.

Every trade must pass ALL gates before execution. No exceptions.
If any gate fails, the trade is rejected with a reason logged.

Gates (in order):
  1. Bot not killed for the day
  2. Daily loss limit not breached
  3. Within trading session (after start, before square-off)
  4. Not in cooldown period
  5. Concurrent position limit
  6. Daily trade count limit
  7. Sufficient margin (with buffer)
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.broker_base import BrokerBase
from core.position_manager import PositionManager

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class RiskEngine:
    def __init__(
        self,
        position_manager: PositionManager,
        broker: BrokerBase | None = None,
        max_daily_loss: float = 5000,
        max_concurrent_positions: int = 2,
        max_trades_per_day: int = 10,
        sl_buffer_pct: float = 1.5,
        cooldown_after_losses: int = 2,
        cooldown_minutes: int = 30,
        margin_buffer_pct: float = 10,
        session_start: str = "09:16",
        square_off_time: str = "15:15",
    ):
        self.pm = position_manager
        self.broker = broker
        self.max_daily_loss = max_daily_loss
        self.max_concurrent = max_concurrent_positions
        self.max_trades = max_trades_per_day
        self.sl_buffer_pct = sl_buffer_pct
        self.cooldown_after_losses = cooldown_after_losses
        self.cooldown_minutes = cooldown_minutes
        self.margin_buffer_pct = margin_buffer_pct

        sh, sm = map(int, session_start.split(":"))
        self.session_start = dt_time(sh, sm, tzinfo=IST)
        eh, em = map(int, square_off_time.split(":"))
        self.square_off_time = dt_time(eh, em, tzinfo=IST)

        # Per-instrument session overrides (populated from instruments.yaml)
        self._instrument_sessions: dict[str, tuple[dt_time, dt_time]] = {}

        self.last_reason: str = ""
        self._cooldown_until: datetime | None = None
        self._bot_killed = False

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
                        instrument: str = "") -> bool:
        """Run every gate. Returns True only if ALL pass."""
        now = datetime.now(IST)
        return self._run_gates(now, estimated_margin, instrument)

    def _run_gates(self, now: datetime, estimated_margin: float,
                    instrument: str = "") -> bool:
        # Gate 1: Bot kill switch
        if self._bot_killed:
            self.last_reason = "Bot killed for the day (daily loss limit breached)"
            return False

        # Gate 2: Daily loss check
        if self.pm.daily_pnl <= -self.max_daily_loss:
            self._bot_killed = True
            self.last_reason = f"Daily loss limit breached: ₹{self.pm.daily_pnl:,.2f}"
            log.critical(self.last_reason)
            return False

        # Gate 3a/3b: Session check (per-instrument if available)
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
            self.pm._closed[-1]  # ensure there IS a loss (defensive)
            return False

        # Gate 5: Max concurrent positions
        if self.pm.open_count >= self.max_concurrent:
            self.last_reason = f"Max concurrent positions ({self.max_concurrent}) reached"
            return False

        # Gate 6: Max trades per day
        if self.pm.trade_count_today >= self.max_trades:
            self.last_reason = f"Max trades/day ({self.max_trades}) reached"
            return False

        # Gate 7: Margin check (if broker available and margin estimate provided)
        if self.broker and estimated_margin > 0:
            available = self.broker.get_available_margin()
            required = estimated_margin * (1 + self.margin_buffer_pct / 100)
            if available < required:
                self.last_reason = (
                    f"Insufficient margin: need ₹{required:,.0f} "
                    f"(incl {self.margin_buffer_pct}% buffer), have ₹{available:,.0f}"
                )
                log.warning(self.last_reason)
                return False

        self.last_reason = ""
        return True

    def should_square_off(self, instrument: str = "") -> bool:
        now = datetime.now(IST)
        _, sess_end = self.get_session_times(instrument)
        return now.timetz() >= sess_end

    def all_sessions_done(self) -> bool:
        """True only when every registered session (and the default) is past square-off."""
        now = datetime.now(IST)
        if now.timetz() < self.square_off_time:
            return False
        for _, (_, sess_end) in self._instrument_sessions.items():
            if now.timetz() < sess_end:
                return False
        return True

    def is_daily_loss_breached(self) -> bool:
        return self._bot_killed or self.pm.daily_pnl <= -self.max_daily_loss

    def compute_sl_price(self, entry_price: float, direction: str) -> float:
        """SL for option premium."""
        buffer = entry_price * self.sl_buffer_pct / 100
        if direction == "LONG":
            return round(max(entry_price - buffer, 0.05), 2)
        # SHORT: SL is above entry (premium rising = loss)
        return round(entry_price + buffer, 2)

    def compute_trail_sl(self, current_price: float, entry_price: float,
                         existing_sl: float, direction: str,
                         trail_pct: float = 0.8) -> float:
        """Ratcheting trailing stop. Only moves in the profitable direction."""
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

    def reset_daily(self) -> None:
        self._bot_killed = False
        self._cooldown_until = None
        self.last_reason = ""
        log.info("Risk engine reset for new trading day")
