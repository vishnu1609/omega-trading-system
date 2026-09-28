from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

_CALENDAR_FILE = "config/market_holidays.json"
_REFRESH_DAYS = 7

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

_NSE_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
_NSE_BASE_URL = "https://www.nseindia.com"
_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays",
}


def _fetch_nse_holidays() -> dict[date, str] | None:
    """Fetch trading holidays from NSE India API. Returns None on failure."""
    import requests
    holidays: dict[date, str] = {}
    try:
        sess = requests.Session()
        sess.headers.update(_NSE_HEADERS)
        sess.get(_NSE_BASE_URL, timeout=10)

        resp = sess.get(_NSE_HOLIDAY_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        cm_holidays = data.get("CM", [])
        for entry in cm_holidays:
            try:
                raw_date = entry.get("tradingDate", "")
                name = entry.get("description", entry.get("Sr_no", "Holiday"))
                d = datetime.strptime(raw_date, "%d-%b-%Y").date()
                holidays[d] = name
            except (ValueError, KeyError):
                continue

        if holidays:
            log.info("Fetched %d holidays from NSE API", len(holidays))
            return holidays
        log.warning("NSE API returned empty holiday list")
        return None
    except Exception as e:
        log.warning("Failed to fetch NSE holidays: %s", e)
        return None


class MarketCalendar:

    def __init__(self, base_path: str = "."):
        self._base = Path(base_path)
        self._holidays: dict[date, str] = dict(NSE_HOLIDAYS_2026)
        self._last_fetch: date | None = None
        self._load_cached()
        self._try_refresh()

    def _load_cached(self) -> None:
        path = self._base / _CALENDAR_FILE
        if not path.exists():
            self._save_cache()
            return
        try:
            with open(path) as f:
                data = json.load(f)
            for d_str, name in data.get("holidays", {}).items():
                self._holidays[date.fromisoformat(d_str)] = name
            fetched = data.get("last_fetched")
            if fetched:
                self._last_fetch = date.fromisoformat(fetched)
            log.info("Calendar cache loaded: %d holidays (fetched: %s)",
                     len(self._holidays), self._last_fetch or "never")
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to load calendar cache: %s", e)

    def _save_cache(self) -> None:
        path = self._base / _CALENDAR_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "holidays": {d.isoformat(): name for d, name in sorted(self._holidays.items())},
            "last_fetched": self._last_fetch.isoformat() if self._last_fetch else None,
            "nse_session": {"open": "09:15", "close": "15:30"},
            "mcx_session": {"open": "09:00", "close": "23:30"},
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _needs_refresh(self) -> bool:
        if self._last_fetch is None:
            return True
        return (date.today() - self._last_fetch).days >= _REFRESH_DAYS

    def _try_refresh(self) -> None:
        if not self._needs_refresh():
            return
        t = threading.Thread(target=self._refresh_async, daemon=True)
        t.start()

    def _refresh_async(self) -> None:
        fetched = _fetch_nse_holidays()
        if fetched:
            self._holidays.update(fetched)
            self._last_fetch = date.today()
            self._save_cache()
            log.info("Calendar refreshed from NSE — %d total holidays", len(self._holidays))
        else:
            log.warning("Calendar refresh failed — using cached/hardcoded holidays")

    def refresh(self) -> bool:
        """Force a synchronous refresh. Returns True if new data was fetched."""
        fetched = _fetch_nse_holidays()
        if fetched:
            self._holidays.update(fetched)
            self._last_fetch = date.today()
            self._save_cache()
            log.info("Calendar force-refreshed — %d total holidays", len(self._holidays))
            return True
        return False

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
            "last_fetched": self._last_fetch.isoformat() if self._last_fetch else "never",
        }

    def add_holiday(self, d: date, name: str) -> None:
        self._holidays[d] = name
        self._save_cache()
        log.info("Holiday added: %s — %s", d.isoformat(), name)
