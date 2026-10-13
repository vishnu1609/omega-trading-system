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
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

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
    entry_time: datetime = field(default_factory=lambda: datetime.now(_IST))
    strategy: str = ""
    atr_value: float = 0.0
    partial_exit_done: bool = False
    original_quantity: int = 0
    confidence: str = "LOW"
    tp_extended: bool = False
    breakeven_locked: bool = False
    tranche_exits: int = 0
    peak_profit_pct: float = 0.0
    dynamic_level: int = 1
    max_dynamic_level: int = 0
    expiry: str = ""
    underlying: str = ""
    strike: int = 0
    iv: float = 0.0
    is_spread: bool = False
    hedge_security_id: str = ""
    hedge_trade_symbol: str = ""
    hedge_strike: int = 0
    spread_width: int = 0
    net_credit: float = 0.0
    max_loss: float = 0.0
    hedge_entry_price: float = 0.0
    hedge_current_price: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.direction == "LONG"

    @property
    def unrealised_pnl(self) -> float:
        if self.current_price <= 0:
            return 0.0
        if self.is_spread and self.hedge_current_price > 0:
            sell_pnl = (self.entry_price - self.current_price) * self.quantity
            hedge_pnl = (self.hedge_current_price - self.hedge_entry_price) * self.quantity
            return sell_pnl + hedge_pnl
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
    exit_time: datetime = field(default_factory=lambda: datetime.now(_IST))
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
                        "strategy": p.strategy, "atr_value": p.atr_value,
                        "entry_time": p.entry_time.isoformat(),
                        "confidence": p.confidence,
                        "tp_extended": p.tp_extended,
                        "breakeven_locked": p.breakeven_locked,
                        "tranche_exits": p.tranche_exits,
                        "peak_profit_pct": p.peak_profit_pct,
                    "dynamic_level": p.dynamic_level,
                    "max_dynamic_level": p.max_dynamic_level,
                    "expiry": p.expiry,
                    "underlying": p.underlying,
                    "strike": p.strike,
                    "iv": p.iv,
                    "is_spread": p.is_spread,
                    "hedge_security_id": p.hedge_security_id,
                    "hedge_trade_symbol": p.hedge_trade_symbol,
                    "hedge_strike": p.hedge_strike,
                    "spread_width": p.spread_width,
                    "net_credit": p.net_credit,
                    "max_loss": p.max_loss,
                    "hedge_entry_price": p.hedge_entry_price,
                    "hedge_current_price": p.hedge_current_price,
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
                    atr_value=data.get("atr_value", 0.0),
                    entry_time=datetime.fromisoformat(data["entry_time"]).replace(tzinfo=_IST)
                        if datetime.fromisoformat(data["entry_time"]).tzinfo is None
                        else datetime.fromisoformat(data["entry_time"]),
                    confidence=data.get("confidence", "LOW"),
                    tp_extended=data.get("tp_extended", False),
                    breakeven_locked=data.get("breakeven_locked", False),
                    tranche_exits=data.get("tranche_exits", 0),
                    peak_profit_pct=data.get("peak_profit_pct", 0.0),
                    dynamic_level=data.get("dynamic_level", 1),
                    max_dynamic_level=data.get("max_dynamic_level", 0),
                    expiry=data.get("expiry", ""),
                    underlying=data.get("underlying", ""),
                    strike=data.get("strike", 0),
                    iv=data.get("iv", 0.0),
                    is_spread=data.get("is_spread", False),
                    hedge_security_id=data.get("hedge_security_id", ""),
                    hedge_trade_symbol=data.get("hedge_trade_symbol", ""),
                    hedge_strike=data.get("hedge_strike", 0),
                    spread_width=data.get("spread_width", 0),
                    net_credit=data.get("net_credit", 0.0),
                    max_loss=data.get("max_loss", 0.0),
                    hedge_entry_price=data.get("hedge_entry_price", 0.0),
                    hedge_current_price=data.get("hedge_current_price", 0.0),
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
