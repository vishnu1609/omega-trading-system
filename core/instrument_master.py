"""
Dhan Instrument Master Loader

Downloads the daily instrument CSV from Dhan and provides fast lookups
by symbol, expiry, strike, and option type. This is essential for resolving
human-readable option symbols (e.g., "BANKNIFTY25MAY50000CE") to Dhan's
numeric security IDs.

Usage:
    im = InstrumentMaster()
    im.download_and_parse()
    sec_id = im.get_security_id("BANKNIFTY", "2025-05-29", 50000, "CE")
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

import requests

log = logging.getLogger(__name__)

INSTRUMENT_CSV_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE_DIR = Path("data/instruments")


@dataclass
class Instrument:
    security_id: str
    exchange_segment: str
    instrument_type: str
    trading_symbol: str
    underlying_symbol: str
    expiry_date: date | None
    strike_price: float
    option_type: str        # CE, PE, or "" for futures/index
    lot_size: int
    tick_size: float


class InstrumentMaster:
    def __init__(self) -> None:
        self._instruments: list[Instrument] = []
        self._by_id: dict[str, Instrument] = {}
        self._cache_path = CACHE_DIR / f"scrip_master_{date.today().isoformat()}.csv"

    @property
    def count(self) -> int:
        return len(self._instruments)

    def download_and_parse(self, force: bool = False) -> int:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if self._cache_path.exists() and not force:
            log.info("Using cached instrument master: %s", self._cache_path)
            return self._parse_csv(self._cache_path)

        log.info("Downloading instrument master from Dhan...")
        try:
            resp = requests.get(INSTRUMENT_CSV_URL, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            log.error("Download failed: %s", exc)
            if self._cache_path.exists():
                return self._parse_csv(self._cache_path)
            return 0

        self._cache_path.write_bytes(resp.content)
        log.info("Saved instrument master → %s (%.1f MB)",
                 self._cache_path, len(resp.content) / 1_000_000)
        return self._parse_csv(self._cache_path)

    def _parse_csv(self, path: Path) -> int:
        self._instruments.clear()
        self._by_id.clear()

        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    expiry_str = row.get("SEM_EXPIRY_DATE", "").strip()
                    expiry = None
                    if expiry_str and expiry_str != "NA":
                        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
                            try:
                                expiry = datetime.strptime(expiry_str.split(" ")[0], fmt.split(" ")[0]).date()
                                break
                            except ValueError:
                                continue

                    strike = float(row.get("SEM_STRIKE_PRICE", "0") or "0")
                    opt_type = row.get("SEM_OPTION_TYPE", "").strip().upper()
                    if opt_type not in ("CE", "PE"):
                        opt_type = ""

                    inst = Instrument(
                        security_id=row.get("SEM_SMST_SECURITY_ID", "").strip(),
                        exchange_segment=row.get("SEM_EXM_EXCH_ID", "").strip(),
                        instrument_type=row.get("SEM_INSTRUMENT_NAME", "").strip(),
                        trading_symbol=row.get("SEM_TRADING_SYMBOL", "").strip(),
                        underlying_symbol=row.get("SEM_CUSTOM_SYMBOL", "").strip(),
                        expiry_date=expiry,
                        strike_price=strike,
                        option_type=opt_type,
                        lot_size=int(float(row.get("SEM_LOT_UNITS", "1") or "1")),
                        tick_size=float(row.get("SEM_TICK_SIZE", "0.05") or "0.05"),
                    )
                    self._instruments.append(inst)
                    self._by_id[inst.security_id] = inst
                except Exception:
                    continue

        log.info("Parsed %d instruments from %s", len(self._instruments), path.name)
        return len(self._instruments)

    def get_security_id(self, underlying: str, expiry: str | date,
                        strike: float, option_type: str) -> str | None:
        if isinstance(expiry, str):
            expiry = datetime.strptime(expiry, "%Y-%m-%d").date()

        for inst in self._instruments:
            if (inst.underlying_symbol.upper().startswith(underlying.upper())
                    and inst.expiry_date == expiry
                    and abs(inst.strike_price - strike) < 0.01
                    and inst.option_type == option_type.upper()):
                return inst.security_id
        return None

    def get_by_id(self, security_id: str) -> Instrument | None:
        return self._by_id.get(security_id)

    def search(self, underlying: str, segment: str = "",
               instrument_type: str = "", expiry: date | None = None,
               option_type: str = "") -> list[Instrument]:
        results = []
        for inst in self._instruments:
            if underlying.upper() not in inst.underlying_symbol.upper():
                continue
            if segment and segment.upper() not in inst.exchange_segment.upper():
                continue
            if instrument_type and instrument_type.upper() != inst.instrument_type.upper():
                continue
            if expiry and inst.expiry_date != expiry:
                continue
            if option_type and inst.option_type != option_type.upper():
                continue
            results.append(inst)
        return results

    def get_nearest_expiry(self, underlying: str, option_type: str = "CE",
                           segment: str = "NSE_FNO") -> date | None:
        today = date.today()
        candidates = self.search(underlying, segment=segment, option_type=option_type)
        future = [i.expiry_date for i in candidates
                  if i.expiry_date and i.expiry_date >= today]
        return min(future) if future else None

    def get_option_chain_strikes(self, underlying: str, expiry: date,
                                 segment: str = "NSE_FNO") -> list[float]:
        instruments = self.search(underlying, segment=segment, expiry=expiry,
                                  option_type="CE")
        strikes = sorted(set(i.strike_price for i in instruments))
        return strikes
