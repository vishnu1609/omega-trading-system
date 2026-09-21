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
    "NIFTY": 3,       # Thursday = weekday 3
    "BANKNIFTY": 2,   # Wednesday = weekday 2
    "FINNIFTY": 1,    # Tuesday = weekday 1
    "SENSEX": 4,      # Friday = weekday 4
    "BANKEX": 0,      # Monday = weekday 0
}

SECTOR_NSE = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX",
              "NIFTY_FUT", "BANKNIFTY_FUT"}
SECTOR_MCX = {"GOLD", "GOLDM", "SILVER", "SILVERM",
              "CRUDEOIL", "CRUDEOILM", "NATURALGAS"}

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
        nse_allocation_cap: float = 0.60,
        mcx_allocation_cap: float = 0.40,
        adx_min_threshold: float = 22.0,
        vwap_warmup_time: str = "09:30",
        candle_max_staleness_sec: float = 330,
        expiry_day_cutoff: str = "12:30",
        natgas_thursday_close: str = "19:50",
        natgas_thursday_reopen: str = "20:15",
        event_blackout_minutes_before: int = 15,
        event_blackout_minutes_after: int = 30,
        step_trail_breakeven_pct: float = 20.0,
        step_trail_lock_pct: float = 40.0,
        step_trail_tight_pct: float = 60.0,
    ):
        self.capital = capital
        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.nse_cap = nse_allocation_cap
        self.mcx_cap = mcx_allocation_cap
        self.adx_min = adx_min_threshold

        h, m = map(int, vwap_warmup_time.split(":"))
        self.vwap_warmup = dt_time(h, m, tzinfo=IST)

        self.candle_max_stale = candle_max_staleness_sec

        eh, em = map(int, expiry_day_cutoff.split(":"))
        self.expiry_cutoff = dt_time(eh, em, tzinfo=IST)

        nch, ncm = map(int, natgas_thursday_close.split(":"))
        self.ng_close = dt_time(nch, ncm, tzinfo=IST)
        nrh, nrm = map(int, natgas_thursday_reopen.split(":"))
        self.ng_reopen = dt_time(nrh, nrm, tzinfo=IST)

        self.event_before = event_blackout_minutes_before
        self.event_after = event_blackout_minutes_after

        self.step_breakeven = step_trail_breakeven_pct
        self.step_lock = step_trail_lock_pct
        self.step_tight = step_trail_tight_pct

        self._event_blackouts: list[datetime] = []
        self._allocated_nse: float = 0.0
        self._allocated_mcx: float = 0.0
        self._recent_index_signals: dict[str, str] = {}

    # ── Scenario 1: Capital Allocation Caps (time-aware) ────────────────

    def _get_allocation_caps(self) -> tuple[float, float]:
        now = datetime.now(IST).timetz()
        nse_close = dt_time(15, 30, tzinfo=IST)
        if now < nse_close:
            return 0.80, 0.20
        return 0.50, 0.50

    def check_sector_allocation(self, symbol: str,
                                estimated_margin: float) -> GuardrailResult:
        upper = symbol.upper()
        nse_pct, mcx_pct = self._get_allocation_caps()
        if upper in SECTOR_NSE or any(s in upper for s in SECTOR_NSE):
            cap = self.capital * nse_pct
            if self._allocated_nse + estimated_margin > cap:
                return GuardrailResult(
                    False,
                    f"NSE allocation cap ₹{cap:,.0f} ({nse_pct:.0%}) would be exceeded "
                    f"(current: ₹{self._allocated_nse:,.0f} + ₹{estimated_margin:,.0f})",
                    "sector_allocation",
                )
        elif upper in SECTOR_MCX or any(s in upper for s in SECTOR_MCX):
            cap = self.capital * mcx_pct
            if self._allocated_mcx + estimated_margin > cap:
                return GuardrailResult(
                    False,
                    f"MCX allocation cap ₹{cap:,.0f} ({mcx_pct:.0%}) would be exceeded "
                    f"(current: ₹{self._allocated_mcx:,.0f} + ₹{estimated_margin:,.0f})",
                    "sector_allocation",
                )
        return PASS

    def record_allocation(self, symbol: str, margin: float) -> None:
        upper = symbol.upper()
        if upper in SECTOR_NSE or any(s in upper for s in SECTOR_NSE):
            self._allocated_nse += margin
        elif upper in SECTOR_MCX or any(s in upper for s in SECTOR_MCX):
            self._allocated_mcx += margin

    def release_allocation(self, symbol: str, margin: float) -> None:
        upper = symbol.upper()
        if upper in SECTOR_NSE or any(s in upper for s in SECTOR_NSE):
            self._allocated_nse = max(0, self._allocated_nse - margin)
        elif upper in SECTOR_MCX or any(s in upper for s in SECTOR_MCX):
            self._allocated_mcx = max(0, self._allocated_mcx - margin)

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

    # ── Scenario 11: Natural Gas Thursday Blackout ───────────────────────

    def check_natgas_thursday(self, symbol: str) -> GuardrailResult:
        if "NATURALGAS" not in symbol.upper():
            return PASS
        now = datetime.now(IST)
        if now.weekday() != 3:  # Thursday
            return PASS
        if self.ng_close <= now.timetz() <= self.ng_reopen:
            return GuardrailResult(
                False,
                f"NatGas Thursday blackout: EIA inventory report window "
                f"{self.ng_close.strftime('%H:%M')}-{self.ng_reopen.strftime('%H:%M')} — "
                f"no new entries, existing positions auto-closed at {self.ng_close.strftime('%H:%M')}",
                "natgas_thursday",
            )
        return PASS

    def should_close_natgas(self) -> bool:
        now = datetime.now(IST)
        if now.weekday() != 3:
            return False
        close_window_start = self.ng_close
        from datetime import timedelta
        close_check = (datetime.combine(now.date(), self.ng_close,
                                        tzinfo=IST) - timedelta(minutes=2)).timetz()
        return close_check <= now.timetz() <= self.ng_close

    # ── Scenario 12: Session Kill-Switch (hardcoded times) ───────────────

    def check_session_killswitch(self, symbol: str) -> GuardrailResult:
        now = datetime.now(IST)
        upper = symbol.upper()

        if upper in SECTOR_MCX or any(s in upper for s in SECTOR_MCX):
            kill_time = dt_time(23, 10, tzinfo=IST)
            if now.timetz() >= kill_time:
                return GuardrailResult(
                    False,
                    f"MCX session kill-switch: past 23:10 — "
                    f"no new entries (Dhan MIS auto-square-off at 23:15-23:25)",
                    "session_killswitch",
                )
        else:
            kill_time = dt_time(15, 10, tzinfo=IST)
            if now.timetz() >= kill_time:
                return GuardrailResult(
                    False,
                    f"NSE session kill-switch: past 15:10 — "
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
    ) -> float:
        if entry_price <= 0 or current_price <= 0:
            return existing_sl

        if direction == "LONG":
            pnl_pct = (current_price - entry_price) / entry_price * 100
            breakeven = entry_price + (brokerage / max(1, entry_price))

            if pnl_pct >= self.step_tight:
                # +60%: trail behind previous candle low
                if prev_candle_low > 0:
                    new_sl = prev_candle_low
                else:
                    lock_amount = entry_price * (self.step_tight - 20) / 100
                    new_sl = entry_price + lock_amount
            elif pnl_pct >= self.step_lock:
                # +40%: lock in 20% profit
                lock_amount = entry_price * (self.step_breakeven) / 100
                new_sl = entry_price + lock_amount
            elif pnl_pct >= self.step_breakeven:
                # +20%: move to breakeven
                new_sl = breakeven
            else:
                return existing_sl

            return max(new_sl, existing_sl)

        else:  # SHORT
            pnl_pct = (entry_price - current_price) / entry_price * 100
            breakeven = entry_price - (brokerage / max(1, entry_price))

            if pnl_pct >= self.step_tight:
                if prev_candle_low > 0:
                    new_sl = prev_candle_low
                else:
                    lock_amount = entry_price * (self.step_tight - 20) / 100
                    new_sl = entry_price - lock_amount
            elif pnl_pct >= self.step_lock:
                lock_amount = entry_price * self.step_breakeven / 100
                new_sl = entry_price - lock_amount
            elif pnl_pct >= self.step_breakeven:
                new_sl = breakeven
            else:
                return existing_sl

            if existing_sl <= 0:
                return round(new_sl, 2)
            return min(new_sl, existing_sl)

    # ── Combined Pre-Signal Check ────────────────────────────────────────

    def pre_signal_check(self, symbol: str, adx_value: float = 0,
                         candle_time: datetime | None = None) -> GuardrailResult:
        checks = [
            self.check_vwap_warmup(symbol),
            self.check_adx_regime(adx_value, symbol),
            self.check_expiry_day(symbol),
            self.check_natgas_thursday(symbol),
            self.check_session_killswitch(symbol),
            self.check_event_blackout(),
        ]
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

    # ── Scenario 18: MCX Contract Rollover ───────────────────────────────

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
        self._allocated_mcx = 0.0
        self._recent_index_signals.clear()
        self._event_blackouts.clear()
        log.info("Guardrails reset for new trading day")
