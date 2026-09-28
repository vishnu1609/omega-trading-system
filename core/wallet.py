"""
Persistent Wallet — tracks capital across sessions.

Backed by SQLite (via OmegaDB). Updates on every trade entry/exit.
Carries forward across days automatically.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from core.database import OmegaDB

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_CAPITAL = 200_000.0


class Wallet:
    def __init__(self, starting_capital: float = DEFAULT_CAPITAL):
        self._lock = threading.Lock()
        self._db = OmegaDB.get()
        self._starting_capital = starting_capital
        w = self._db.init_wallet(starting_capital)
        self._balance = w.get("balance", starting_capital)
        self._in_trade = w.get("in_trade", 0.0)
        self._today_pnl = w.get("today_pnl", 0.0)
        self._today_trades = w.get("today_trades", 0)
        self._today_date = w.get("trade_date", datetime.now(IST).strftime("%Y-%m-%d"))
        log.info("Wallet loaded: balance=%.2f in_trade=%.2f today_pnl=%.2f",
                 self._balance, self._in_trade, self._today_pnl)

    def _sync_to_db(self):
        self._db.update_wallet(
            balance=round(self._balance, 2),
            in_trade=round(self._in_trade, 2),
            today_pnl=round(self._today_pnl, 2),
            today_trades=self._today_trades,
            trade_date=self._today_date,
        )

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def in_trade(self) -> float:
        return self._in_trade

    @property
    def available(self) -> float:
        return self._balance - self._in_trade

    @property
    def today_pnl(self) -> float:
        return self._today_pnl

    @property
    def today_trades(self) -> int:
        return self._today_trades

    def deduct_margin(self, amount: float, symbol: str = ""):
        with self._lock:
            self._in_trade += amount
            log.info("WALLET: margin locked ₹%.2f for %s | balance=%.2f in_trade=%.2f avail=%.2f",
                     amount, symbol, self._balance, self._in_trade, self.available)
            self._sync_to_db()

    def release_margin(self, amount: float, pnl: float, symbol: str = "",
                       reason: str = ""):
        with self._lock:
            self._in_trade = max(0, self._in_trade - amount)
            self._balance += pnl
            self._today_pnl += pnl
            self._today_trades += 1
            log.info("WALLET: margin released ₹%.2f pnl=₹%+.2f for %s | "
                     "balance=%.2f in_trade=%.2f avail=%.2f",
                     amount, pnl, symbol, self._balance, self._in_trade,
                     self.available)
            self._sync_to_db()

    def snapshot(self) -> dict:
        return {
            "balance": round(self._balance, 2),
            "in_trade": round(self._in_trade, 2),
            "available": round(self.available, 2),
            "today_pnl": round(self._today_pnl, 2),
            "today_trades": self._today_trades,
            "starting_capital": self._starting_capital,
            "date": self._today_date,
        }
