"""
Position & P&L Tracker

Tracks open positions with correct P&L for both LONG (buy options)
and SHORT (sell options) positions.

LONG CE:  profit = (current - entry) * qty    (price goes up = win)
LONG PE:  profit = (current - entry) * qty    (premium goes up = win)
SHORT CE: profit = (entry - current) * qty    (premium decays = win)
SHORT PE: profit = (entry - current) * qty    (premium decays = win)

For options, we always trade the PREMIUM. Entry/exit prices are option
premium prices, not the underlying index price.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

POSITIONS_FILE = "data/paper_positions.json"


@dataclass
class Position:
    symbol: str
    security_id: str
    exchange_segment: str
    option_type: str            # CE or PE
    direction: str              # LONG or SHORT
    quantity: int
    entry_price: float
    current_price: float = 0.0
    sl_price: float = 0.0
    trail_sl: float = 0.0
    tp_price: float = 0.0
    entry_order_id: str = ""
    sl_order_id: str = ""
    peak_price: float = 0.0
    entry_time: datetime = field(default_factory=datetime.now)
    strategy: str = ""

    @property
    def is_long(self) -> bool:
        return self.direction == "LONG"

    @property
    def unrealised_pnl(self) -> float:
        if self.current_price <= 0:
            return 0.0
        if self.is_long:
            return (self.current_price - self.entry_price) * self.quantity
        return (self.entry_price - self.current_price) * self.quantity

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return self.unrealised_pnl / (self.entry_price * self.quantity) * 100

    @property
    def is_sl_hit(self) -> bool:
        active_sl = self.trail_sl if self.trail_sl > 0 else self.sl_price
        if active_sl <= 0 or self.current_price <= 0:
            return False
        if self.is_long:
            return self.current_price <= active_sl
        return self.current_price >= active_sl

    @property
    def is_tp_hit(self) -> bool:
        if self.tp_price <= 0 or self.current_price <= 0:
            return False
        if self.is_long:
            return self.current_price >= self.tp_price
        return self.current_price <= self.tp_price


@dataclass
class ClosedTrade:
    symbol: str
    option_type: str
    direction: str
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    entry_time: datetime
    exit_time: datetime = field(default_factory=datetime.now)
    exit_reason: str = ""


class PositionManager:
    def __init__(self) -> None:
        self._open: dict[str, Position] = {}
        self._closed: list[ClosedTrade] = []
        self._lock = threading.Lock()

    @property
    def open_positions(self) -> list[Position]:
        return list(self._open.values())

    @property
    def open_count(self) -> int:
        return len(self._open)

    @property
    def closed_trades(self) -> list[ClosedTrade]:
        return list(self._closed)

    @property
    def daily_pnl(self) -> float:
        realised = sum(t.pnl for t in self._closed)
        unrealised = sum(p.unrealised_pnl for p in self._open.values())
        return realised + unrealised

    @property
    def realised_pnl(self) -> float:
        return sum(t.pnl for t in self._closed)

    @property
    def trade_count_today(self) -> int:
        return len(self._closed) + len(self._open)

    @property
    def consecutive_losses(self) -> int:
        count = 0
        for trade in reversed(self._closed):
            if trade.pnl < 0:
                count += 1
            else:
                break
        return count

    def add_position(self, pos: Position) -> None:
        with self._lock:
            key = pos.security_id
            if key in self._open:
                log.warning("Position already open for %s — overwriting", key)
            self._open[key] = pos
        log.info("POSITION OPEN: %s %s %s qty=%d entry=%.2f SL=%.2f TP=%.2f",
                 pos.direction, pos.option_type, pos.symbol,
                 pos.quantity, pos.entry_price, pos.sl_price, pos.tp_price)

    def update_price(self, security_id: str, price: float) -> Position | None:
        pos = self._open.get(security_id)
        if pos:
            pos.current_price = price
        return pos

    def update_trail_sl(self, security_id: str, new_sl: float) -> None:
        pos = self._open.get(security_id)
        if not pos:
            return
        if pos.is_long:
            if new_sl > pos.trail_sl:
                log.info("Trail SL ratcheted UP: %s %.2f → %.2f",
                         pos.symbol, pos.trail_sl, new_sl)
                pos.trail_sl = new_sl
        else:
            if pos.trail_sl <= 0 or new_sl < pos.trail_sl:
                log.info("Trail SL ratcheted DOWN: %s %.2f → %.2f",
                         pos.symbol, pos.trail_sl, new_sl)
                pos.trail_sl = new_sl

    def close_position(self, security_id: str, exit_price: float,
                       reason: str = "SIGNAL_EXIT") -> ClosedTrade | None:
        with self._lock:
            pos = self._open.pop(security_id, None)
        if not pos:
            log.warning("No open position for %s to close", security_id)
            return None

        if pos.is_long:
            pnl = (exit_price - pos.entry_price) * pos.quantity
        else:
            pnl = (pos.entry_price - exit_price) * pos.quantity

        trade = ClosedTrade(
            symbol=pos.symbol,
            option_type=pos.option_type,
            direction=pos.direction,
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl=round(pnl, 2),
            entry_time=pos.entry_time,
            exit_reason=reason,
        )
        self._closed.append(trade)
        log.info("POSITION CLOSED: %s %s %s pnl=%.2f reason=%s",
                 pos.direction, pos.option_type, pos.symbol, pnl, reason)
        return trade

    def close_all(self, reason: str = "SQUARE_OFF") -> list[ClosedTrade]:
        trades = []
        for sid in list(self._open.keys()):
            pos = self._open[sid]
            price = pos.current_price if pos.current_price > 0 else pos.entry_price
            trade = self.close_position(sid, price, reason)
            if trade:
                trades.append(trade)
        return trades

    def get_position_by_symbol(self, symbol_fragment: str) -> Position | None:
        for pos in self._open.values():
            if symbol_fragment in pos.symbol:
                return pos
        return None

    def get_win_loss_stats(self) -> tuple[int, int]:
        wins = sum(1 for t in self._closed if t.pnl >= 0)
        losses = sum(1 for t in self._closed if t.pnl < 0)
        return wins, losses

    def save_state(self) -> None:
        try:
            state = {
                "open": {
                    sid: {
                        "symbol": p.symbol, "security_id": p.security_id,
                        "exchange_segment": p.exchange_segment, "option_type": p.option_type,
                        "direction": p.direction, "quantity": p.quantity,
                        "entry_price": p.entry_price, "current_price": p.current_price,
                        "sl_price": p.sl_price, "trail_sl": p.trail_sl,
                        "tp_price": p.tp_price, "entry_order_id": p.entry_order_id,
                        "sl_order_id": p.sl_order_id, "peak_price": p.peak_price,
                        "strategy": p.strategy,
                        "entry_time": p.entry_time.isoformat(),
                    } for sid, p in self._open.items()
                },
                "closed_count": len(self._closed),
            }
            Path(POSITIONS_FILE).parent.mkdir(parents=True, exist_ok=True)
            Path(POSITIONS_FILE).write_text(json.dumps(state, indent=2))
        except Exception as e:
            log.error("Failed to save position state: %s", e)

    def load_state(self) -> int:
        try:
            path = Path(POSITIONS_FILE)
            if not path.exists():
                return 0
            state = json.loads(path.read_text())
            loaded = 0
            for sid, data in state.get("open", {}).items():
                pos = Position(
                    symbol=data["symbol"], security_id=data["security_id"],
                    exchange_segment=data["exchange_segment"],
                    option_type=data["option_type"], direction=data["direction"],
                    quantity=data["quantity"], entry_price=data["entry_price"],
                    current_price=data.get("current_price", 0),
                    sl_price=data["sl_price"], trail_sl=data.get("trail_sl", 0),
                    tp_price=data.get("tp_price", 0),
                    entry_order_id=data.get("entry_order_id", ""),
                    sl_order_id=data.get("sl_order_id", ""),
                    peak_price=data.get("peak_price", 0),
                    strategy=data.get("strategy", ""),
                    entry_time=datetime.fromisoformat(data["entry_time"]),
                )
                self._open[sid] = pos
                loaded += 1
            if loaded:
                log.info("Restored %d position(s) from disk", loaded)
            return loaded
        except Exception as e:
            log.error("Failed to load position state: %s", e)
            return 0

    def reset_daily(self) -> None:
        self._open.clear()
        self._closed.clear()
        try:
            Path(POSITIONS_FILE).unlink(missing_ok=True)
        except Exception:
            pass
