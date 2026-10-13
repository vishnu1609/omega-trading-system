"""
Pre-Trade Guardrails — 15 Scenario Stress-Test Framework

Every signal passes through these checks BEFORE reaching the risk engine.
Any guardrail failure = signal rejected with a logged reason.

Categories:
  1. Capital Allocation & Concurrency (Scenarios 1-4)
  2. Data Infrastructure (Scenarios 5-6)
  3. Volatility & Indicator Regimes (Scenarios 7-9)
  4. Derivative & Instrument Mechanics (Scenarios 10-12)
  5. Risk Architecture (Scenarios 13-15)
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

EXPIRY_DAYS = {
    "NIFTY": 3,       # Thursday = weekday 3 (weekly expiry — only NSE weekly left post-SEBI Nov 2024)
    "BANKNIFTY": 3,   # Monthly expiry (last Thursday) — weekly discontinued Nov 2024
    "FINNIFTY": 3,    # Monthly expiry (last Tuesday→Thursday) — weekly discontinued Nov 2024
    "SENSEX": 4,      # Friday = weekday 4 (weekly expiry — only BSE weekly left post-SEBI Nov 2024)
    "BANKEX": 0,      # Monthly expiry — weekly discontinued Nov 2024
}

SECTOR_NSE = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX",
              "NIFTY_FUT", "BANKNIFTY_FUT"}

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"}


class GuardrailResult:
    __slots__ = ("passed", "reason", "guardrail")

    def __init__(self, passed: bool, reason: str = "", guardrail: str = ""):
        self.passed = passed
        self.reason = reason
        self.guardrail = guardrail

    def __bool__(self) -> bool:
        return self.passed


PASS = GuardrailResult(True)


class Guardrails:
    def __init__(
        self,
        capital: float = 100_000,
        max_risk_per_trade_pct: float = 2.0,
        nse_allocation_cap: float = 0.70,
        adx_min_threshold: float = 22.0,
        vwap_warmup_time: str = "09:30",
        candle_max_staleness_sec: float = 330,
        expiry_day_cutoff: str = "12:30",
        event_blackout_minutes_before: int = 15,
        event_blackout_minutes_after: int = 30,
        step_trail_breakeven_pct: float = 20.0,
        step_trail_lock_pct: float = 40.0,
        step_trail_tight_pct: float = 60.0,
    ):
        self.capital = capital
        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.nse_cap = nse_allocation_cap
        self.adx_min = adx_min_threshold

        h, m = map(int, vwap_warmup_time.split(":"))
        self.vwap_warmup = dt_time(h, m, tzinfo=IST)

        self.candle_max_stale = candle_max_staleness_sec

        eh, em = map(int, expiry_day_cutoff.split(":"))
        self.expiry_cutoff = dt_time(eh, em, tzinfo=IST)

        self.event_before = event_blackout_minutes_before
        self.event_after = event_blackout_minutes_after

        self.step_breakeven = step_trail_breakeven_pct
        self.step_lock = step_trail_lock_pct
        self.step_tight = step_trail_tight_pct

        self._event_blackouts: list[datetime] = []
        self._allocated_nse: float = 0.0
        self._recent_index_signals: dict[str, str] = {}

    # ── Scenario 1: Capital Allocation Caps (time-aware) ────────────────

    def check_sector_allocation(self, symbol: str,
                                estimated_margin: float) -> GuardrailResult:
        cap = self.capital * self.nse_cap
        if self._allocated_nse + estimated_margin > cap:
            return GuardrailResult(
                False,
                f"Allocation cap ₹{cap:,.0f} ({self.nse_cap:.0%}) would be exceeded "
                f"(current: ₹{self._allocated_nse:,.0f} + ₹{estimated_margin:,.0f})",
                "sector_allocation",
            )
        return PASS

    def record_allocation(self, symbol: str, margin: float) -> None:
        self._allocated_nse += margin

    def release_allocation(self, symbol: str, margin: float) -> None:
        self._allocated_nse = max(0, self._allocated_nse - margin)

    # ── Scenario 2: Per-Trade Risk Cap ───────────────────────────────────

    def check_per_trade_risk(self, entry_price: float, sl_price: float,
                             quantity: int) -> GuardrailResult:
        if entry_price <= 0 or sl_price <= 0:
            return PASS
        risk = abs(entry_price - sl_price) * quantity
        max_risk = self.capital * self.max_risk_per_trade_pct / 100
        if risk > max_risk:
            return GuardrailResult(
                False,
                f"Trade risk ₹{risk:,.0f} exceeds {self.max_risk_per_trade_pct}% "
                f"cap of ₹{max_risk:,.0f} on ₹{self.capital:,.0f} capital",
                "per_trade_risk",
            )
        return PASS

    # ── Scenario 3: Index Conflict Rule ──────────────────────────────────

    def check_index_conflict(self, cycle_signals: list[dict]) -> GuardrailResult:
        index_sides: dict[str, str] = {}
        for sig in cycle_signals:
            sym = sig.get("symbol", "").upper()
            if sym not in INDEX_SYMBOLS:
                continue
            side = sig.get("side", "")
            if side in ("BUY_CE", "SELL_PE", "BUY"):
                direction = "BULLISH"
            elif side in ("BUY_PE", "SELL_CE", "SELL"):
                direction = "BEARISH"
            else:
                continue
            index_sides[sym] = direction

        if len(index_sides) < 2:
            return PASS

        directions = set(index_sides.values())
        if len(directions) > 1:
            return GuardrailResult(
                False,
                f"Index conflict: {index_sides} — indices disagree on direction, "
                f"freezing all index trades until alignment",
                "index_conflict",
            )
        return PASS

    # ── Scenario 5: Candle Freshness Check ───────────────────────────────

    def check_candle_freshness(self, candle_time: datetime,
                               symbol: str) -> GuardrailResult:
        now = datetime.now(IST)
        if candle_time.tzinfo is None:
            candle_time = candle_time.replace(tzinfo=IST)
        staleness = (now - candle_time).total_seconds()
        if staleness > self.candle_max_stale:
            return GuardrailResult(
                False,
                f"Stale data for {symbol}: candle is {staleness:.0f}s old "
                f"(max {self.candle_max_stale:.0f}s). Yahoo may be delayed.",
                "candle_freshness",
            )
        return PASS

    # ── Scenario 7: ADX Regime Filter ────────────────────────────────────

    def check_adx_regime(self, adx_value: float,
                         symbol: str) -> GuardrailResult:
        if adx_value < self.adx_min:
            return GuardrailResult(
                False,
                f"ADX regime filter: {symbol} ADX={adx_value:.1f} < {self.adx_min} "
                f"(sideways/noise — signals suppressed)",
                "adx_regime",
            )
        return PASS

    # ── Scenario 8: VWAP Warm-Up Period ──────────────────────────────────

    def check_vwap_warmup(self, symbol: str) -> GuardrailResult:
        now = datetime.now(IST)
        if now.timetz() < self.vwap_warmup:
            return GuardrailResult(
                False,
                f"VWAP warm-up: {symbol} — before {self.vwap_warmup.strftime('%H:%M')}, "
                f"waiting for 3 candles to establish intraday VWAP",
                "vwap_warmup",
            )
        return PASS

    # ── Scenario 9: Economic Calendar Blackout ───────────────────────────

    def add_event_blackout(self, event_time: datetime) -> None:
        self._event_blackouts.append(event_time)

    def check_event_blackout(self) -> GuardrailResult:
        now = datetime.now(IST)
        for evt in self._event_blackouts:
            from datetime import timedelta
            before = evt - timedelta(minutes=self.event_before)
            after = evt + timedelta(minutes=self.event_after)
            if before <= now <= after:
                return GuardrailResult(
                    False,
                    f"Event blackout active: scheduled event at "
                    f"{evt.strftime('%H:%M')} IST "
                    f"({self.event_before}min before / {self.event_after}min after)",
                    "event_blackout",
                )
        return PASS

    # ── Scenario 10: 0-DTE Expiry Day Rules ──────────────────────────────

    def check_expiry_day(self, symbol: str) -> GuardrailResult:
        upper = symbol.upper()
        now = datetime.now(IST)

        expiry_weekday = None
        for name, wd in EXPIRY_DAYS.items():
            if name in upper:
                expiry_weekday = wd
                break
        if expiry_weekday is None:
            return PASS

        if now.weekday() != expiry_weekday:
            return PASS

        if now.timetz() >= self.expiry_cutoff:
            return GuardrailResult(
                False,
                f"0-DTE filter: {symbol} expiry today, past {self.expiry_cutoff.strftime('%H:%M')} — "
                f"extreme theta decay, new entries blocked. "
                f"Use ITM (delta >= 0.70) and max 15-min holding only.",
                "expiry_day",
            )
        return PASS

    # ── Scenario 12: Session Kill-Switch (hardcoded times) ───────────────

    def check_session_killswitch(self, symbol: str) -> GuardrailResult:
        now = datetime.now(IST)
        kill_time = dt_time(15, 10, tzinfo=IST)
        if now.timetz() >= kill_time:
            return GuardrailResult(
                False,
                f"Session kill-switch: past 15:10 — "
                f"no new entries (square-off imminent)",
                "session_killswitch",
            )
        return PASS

    # ── Scenario 15: Step-Ladder Trailing Stop ───────────────────────────

    def compute_step_trail_sl(
        self,
        entry_price: float,
        current_price: float,
        existing_sl: float,
        direction: str,
        prev_candle_low: float = 0,
        brokerage: float = 40,
        trail_pct: float = 0.15,
        atr_value: float = 0.0,
    ) -> float:
        if entry_price <= 0 or current_price <= 0:
            return existing_sl

        return existing_sl

    # ── Scenario 19: Time-of-Day Filter ────────────────────────────────────

    def check_time_of_day(self, symbol: str) -> GuardrailResult:
        now = datetime.now(IST)
        t = now.timetz()

        opening_end = dt_time(9, 30, tzinfo=IST)
        if t < opening_end:
            return GuardrailResult(
                False,
                f"Opening chaos filter: {symbol} — first 15min (9:15-9:30) "
                f"is erratic with gap reactions, waiting for direction",
                "time_of_day",
            )

        return PASS

    # ── Combined Pre-Signal Check ────────────────────────────────────────

    def check_spread_risk(self, max_loss: float, quantity: int) -> GuardrailResult:
        risk = max_loss * quantity
        max_risk = self.capital * self.max_risk_per_trade_pct / 100
        if risk > max_risk:
            return GuardrailResult(
                False,
                f"Spread risk ₹{risk:,.0f} (max_loss={max_loss:.0f} × qty={quantity}) "
                f"exceeds {self.max_risk_per_trade_pct}% cap of ₹{max_risk:,.0f}",
                "spread_risk",
            )
        return PASS

    def pre_signal_check(self, symbol: str, adx_value: float = 0,
                         candle_time: datetime | None = None,
                         is_spread: bool = False) -> GuardrailResult:
        checks = [
            self.check_time_of_day(symbol),
            self.check_vwap_warmup(symbol),
            self.check_session_killswitch(symbol),
        ]
        if not is_spread:
            checks.append(self.check_adx_regime(adx_value, symbol))
        if candle_time:
            checks.append(self.check_candle_freshness(candle_time, symbol))

        for c in checks:
            if not c:
                log.warning("GUARDRAIL [%s] %s: %s", c.guardrail, symbol, c.reason)
                return c
        return PASS

    def pre_trade_check(self, symbol: str, entry_price: float,
                        sl_price: float, quantity: int,
                        estimated_margin: float = 0) -> GuardrailResult:
        checks = [
            self.check_per_trade_risk(entry_price, sl_price, quantity),
            self.check_sector_allocation(symbol, estimated_margin),
        ]
        for c in checks:
            if not c:
                log.warning("GUARDRAIL [%s] %s: %s", c.guardrail, symbol, c.reason)
                return c
        return PASS

    # ── Scenario 16: Broker Outage Defcon Alert ────────────────────────────

    def check_broker_health(self, broker, telegram=None) -> bool:
        try:
            broker.get_available_margin()
            return True
        except Exception as exc:
            log.critical("DEFCON 1: Broker API unreachable — %s", exc)
            if telegram:
                try:
                    telegram.error("DEFCON1_BROKER", Exception(
                        "Broker API is DOWN while positions may be open. "
                        "Call Dhan RMS desk immediately to square off manually."))
                except Exception:
                    pass
            return False

    # ── Scenario 18: Contract Rollover ──────────────────────────────────

    def check_contract_rollover(self, symbol: str,
                                expiry_date: datetime | None = None,
                                days_before: int = 3) -> GuardrailResult:
        if not expiry_date:
            return PASS

        now = datetime.now(IST)
        from datetime import timedelta
        rollover_date = expiry_date - timedelta(days=days_before)

        if now.date() >= rollover_date.date():
            days_to_expiry = (expiry_date.date() - now.date()).days
            return GuardrailResult(
                False,
                f"Contract rollover warning: {symbol} expires in {days_to_expiry} day(s). "
                f"Liquidity drying up, spreads widening. Switch to next month contract.",
                "contract_rollover",
            )
        return PASS

    # ── Scenario 17: Freak Tick Sanity (price-level check) ───────────────

    def check_price_sanity(self, symbol: str, current_price: float,
                           prev_price: float,
                           max_move_pct: float = 5.0) -> GuardrailResult:
        if prev_price <= 0 or current_price <= 0:
            return PASS
        move_pct = abs(current_price - prev_price) / prev_price * 100
        if move_pct > max_move_pct:
            return GuardrailResult(
                False,
                f"Possible freak tick on {symbol}: {move_pct:.1f}% move "
                f"(₹{prev_price:.2f}→₹{current_price:.2f}) exceeds {max_move_pct}% threshold",
                "freak_tick",
            )
        return PASS

    def reset_daily(self) -> None:
        self._allocated_nse = 0.0
        self._recent_index_signals.clear()
        self._event_blackouts.clear()
        log.info("Guardrails reset for new trading day")
