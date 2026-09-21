from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

_CALENDAR_FILE = "config/market_holidays.json"

NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26): "Republic Day",
    date(2026, 2, 17): "Maha Shivaratri",
    date(2026, 3, 14): "Holi",
    date(2026, 3, 31): "Id-Ul-Fitr (Ramadan)",
    date(2026, 4, 2): "Ram Navami",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 6, 7): "Bakri Id (Eid ul-Adha)",
    date(2026, 7, 7): "Muharram",
    date(2026, 8, 15): "Independence Day",
    date(2026, 8, 21): "Janmashtami",
    date(2026, 9, 7): "Milad-un-Nabi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 9): "Diwali (Lakshmi Puja)",
    date(2026, 11, 10): "Diwali (Balipratipada)",
    date(2026, 11, 30): "Guru Nanak Jayanti",
    date(2026, 12, 25): "Christmas",
}

NSE_SESSION = (dt_time(9, 15), dt_time(15, 30))
MCX_SESSION = (dt_time(9, 0), dt_time(23, 30))


class MarketCalendar:

    def __init__(self, base_path: str = "."):
        self._base = Path(base_path)
        self._holidays: dict[date, str] = dict(NSE_HOLIDAYS_2026)
        self._load_custom_holidays()

    def _load_custom_holidays(self) -> None:
        path = self._base / _CALENDAR_FILE
        if not path.exists():
            self._save_holidays()
            return
        try:
            with open(path) as f:
                data = json.load(f)
            for d_str, name in data.get("holidays", {}).items():
                self._holidays[date.fromisoformat(d_str)] = name
            log.info("Market calendar loaded: %d holidays", len(self._holidays))
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to load market calendar: %s", e)

    def _save_holidays(self) -> None:
        path = self._base / _CALENDAR_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "holidays": {d.isoformat(): name for d, name in sorted(self._holidays.items())},
            "nse_session": {"open": "09:15", "close": "15:30"},
            "mcx_session": {"open": "09:00", "close": "23:30"},
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def is_trading_day(self, d: date | None = None) -> bool:
        d = d or date.today()
        if d.weekday() >= 5:
            return False
        return d not in self._holidays

    def holiday_name(self, d: date | None = None) -> str:
        d = d or date.today()
        if d.weekday() == 5:
            return "Saturday"
        if d.weekday() == 6:
            return "Sunday"
        return self._holidays.get(d, "")

    def is_nse_open(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(IST)
        if not self.is_trading_day(now.date()):
            return False
        t = now.timetz()
        return NSE_SESSION[0] <= t <= NSE_SESSION[1]

    def is_mcx_open(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(IST)
        if not self.is_trading_day(now.date()):
            return False
        t = now.timetz()
        return MCX_SESSION[0] <= t <= MCX_SESSION[1]

    def next_trading_day(self, d: date | None = None) -> date:
        from datetime import timedelta
        d = d or date.today()
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def market_status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(IST)
        today = now.date()
        trading_day = self.is_trading_day(today)
        holiday = self.holiday_name(today)
        nse_open = self.is_nse_open(now)
        mcx_open = self.is_mcx_open(now)
        next_day = self.next_trading_day(today)

        if not trading_day:
            status = f"CLOSED — {holiday}" if holiday else "CLOSED"
        elif nse_open and mcx_open:
            status = "NSE + MCX OPEN"
        elif mcx_open:
            status = "MCX OPEN (NSE closed)"
        elif nse_open:
            status = "NSE OPEN (MCX closed)"
        else:
            status = "PRE/POST MARKET"

        return {
            "status": status,
            "trading_day": trading_day,
            "holiday": holiday,
            "nse_open": nse_open,
            "mcx_open": mcx_open,
            "next_trading_day": next_day.isoformat(),
            "time": now.strftime("%H:%M:%S IST"),
        }

    def add_holiday(self, d: date, name: str) -> None:
        self._holidays[d] = name
        self._save_holidays()
        log.info("Holiday added: %s — %s", d.isoformat(), name)
