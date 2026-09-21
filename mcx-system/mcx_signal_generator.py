"""
MCX Signal Generator — extends base SignalGenerator with commodity Yahoo symbols.

Uses Yahoo Finance commodity futures as data source until Dhan commodity access
is approved. Once approved, add MCX security IDs to DHAN_INDEX_MAP.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.signal_generator import SignalGenerator, YAHOO_SYMBOLS

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

MCX_YAHOO_SYMBOLS = {
    "GOLD": "GC=F",
    "GOLDM": "GC=F",
    "SILVER": "SI=F",
    "SILVERM": "SI=F",
    "CRUDEOIL": "CL=F",
    "CRUDEOILM": "CL=F",
    "NATURALGAS": "NG=F",
}

YAHOO_SYMBOLS.update(MCX_YAHOO_SYMBOLS)


class MCXSignalGenerator(SignalGenerator):
    """Signal generator with MCX session hours (09:00-23:30) instead of NSE."""

    def _is_market_open(self, now: datetime) -> bool:
        if self.calendar:
            if not self.calendar.is_trading_day(now.date()):
                return False
            return self.calendar.is_mcx_open(now)
        from datetime import time as dt_time
        t = now.timetz()
        return dt_time(9, 0, tzinfo=IST) <= t <= dt_time(23, 30, tzinfo=IST)

    def run(self, signal_queue, shutdown_event: Event,
            watch_instruments: list[str] | None = None) -> None:
        log.info("MCX signal generator started (poll every %ds)", self.poll_interval)

        if not watch_instruments:
            watch_instruments = list(self.instruments.keys())[:3]

        cycle = 0
        _holiday_logged = None
        while not shutdown_event.is_set():
            now = datetime.now(IST)

            if self.calendar and not self.calendar.is_trading_day(now.date()):
                if _holiday_logged != now.date():
                    log.info("Market holiday — MCX closed")
                    _holiday_logged = now.date()
                shutdown_event.wait(timeout=300)
                continue

            if not self._is_market_open(now):
                if cycle == 0:
                    log.info("MCX pre/post market — session 09:00-23:30")
                shutdown_event.wait(timeout=60)
                continue

            if not self._cycle_lock.acquire(blocking=False):
                shutdown_event.wait(timeout=self.poll_interval)
                continue

            try:
                cycle += 1
                self._scan_count = cycle
                cycle_signals: list[dict] = []

                for inst_name in watch_instruments:
                    cfg = self.instruments.get(inst_name, {})
                    asset_class = cfg.get("asset_class", "commodity_options")
                    asset = "options"

                    try:
                        candles = self.fetch_candles(inst_name)
                        if not candles:
                            log.warning("No candle data for %s — skipping", inst_name)
                            continue

                        sig = self.generate_signals(inst_name, candles, asset)
                        if sig:
                            cycle_signals.append(sig)
                    except Exception as exc:
                        log.error("Scan error for %s: %s", inst_name, exc)

                if cycle_signals:
                    if self.guardrails:
                        from core.guardrails import GuardrailResult
                        checked = self.guardrails.check_index_conflict(cycle_signals)
                        if not checked:
                            log.warning("Conflict detected: %s", checked.reason)
                            cycle_signals.clear()

                for sig in cycle_signals:
                    signal_queue.put(sig)
                    log.info("MCX SIGNAL → %s %s (score=%.0f, ADX=%.1f)",
                             sig.get("side"), sig.get("symbol"),
                             sig.get("score", 0), sig.get("adx", 0))

                if cycle % 10 == 0:
                    log.info("MCX scan #%d complete — %d signal(s), watching %s",
                             cycle, len(cycle_signals), ", ".join(watch_instruments))
            finally:
                self._cycle_lock.release()

            shutdown_event.wait(timeout=self.poll_interval)

        log.info("MCX signal generator stopped after %d cycles", cycle)
