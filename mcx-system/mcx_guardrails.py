"""
MCX-specific Guardrails — extends base Guardrails with commodity rules.

Additional checks:
  - NatGas Thursday EIA blackout (19:50-20:15 IST)
  - MCX session kill-switch at 23:10 (before Dhan auto-square-off)
  - MCX allocation caps
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.guardrails import Guardrails, GuardrailResult, PASS, IST

log = logging.getLogger(__name__)


class MCXGuardrails(Guardrails):
    def __init__(
        self,
        natgas_thursday_close: str = "19:50",
        natgas_thursday_reopen: str = "20:15",
        **kwargs,
    ):
        super().__init__(**kwargs)

        nch, ncm = map(int, natgas_thursday_close.split(":"))
        self.ng_close = dt_time(nch, ncm, tzinfo=IST)
        nrh, nrm = map(int, natgas_thursday_reopen.split(":"))
        self.ng_reopen = dt_time(nrh, nrm, tzinfo=IST)

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
                f"no new entries",
                "natgas_thursday",
            )
        return PASS

    def should_close_natgas(self) -> bool:
        now = datetime.now(IST)
        if now.weekday() != 3:
            return False
        from datetime import timedelta
        close_check = (datetime.combine(now.date(), self.ng_close,
                                        tzinfo=IST) - timedelta(minutes=2)).timetz()
        return close_check <= now.timetz() <= self.ng_close

    def check_session_killswitch(self, symbol: str) -> GuardrailResult:
        now = datetime.now(IST)
        kill_time = dt_time(23, 10, tzinfo=IST)
        if now.timetz() >= kill_time:
            return GuardrailResult(
                False,
                f"MCX session kill-switch: past 23:10 — "
                f"no new entries (Dhan auto-square-off at 23:15-23:25)",
                "session_killswitch",
            )
        return PASS

    def pre_signal_check(self, symbol: str, adx_value: float = 0,
                         candle_time: datetime | None = None) -> GuardrailResult:
        checks = [
            self.check_vwap_warmup(symbol),
            self.check_adx_regime(adx_value, symbol),
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
