"""
Order Manager — Idempotent, retry-safe order execution.

Handles:
  - Duplicate signal detection (by tag)
  - Limit orders with slippage buffer (never blind market orders)
  - Retry with exponential backoff on rate limits
  - Correct exit direction (BUY to close shorts, SELL to close longs)
  - Partial fill tracking
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.broker_base import (
    BrokerAuthError,
    BrokerBase,
    BrokerError,
    BrokerRateLimit,
    Order,
    OrderResponse,
    OrderType,
    ProductType,
    TransactionType,
)
from core.risk_engine import RiskEngine

log = logging.getLogger(__name__)


@dataclass
class Signal:
    strategy: str
    symbol: str
    security_id: str
    exchange_segment: str
    side: str                   # BUY_CE, BUY_PE, SELL_CE, SELL_PE, EXIT
    quantity: int
    price: float                # limit price (0 = use LTP + buffer)
    sl: float = 0.0
    tp: float = 0.0
    strike: int = 0
    timestamp: float = 0.0
    tag: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.tag:
            self.tag = f"{self.strategy}_{self.symbol}_{int(self.timestamp)}"

    @property
    def direction(self) -> str:
        """LONG if buying options, SHORT if selling options."""
        if self.side.startswith("BUY"):
            return "LONG"
        if self.side.startswith("SELL"):
            return "SHORT"
        return "EXIT"

    @property
    def option_type(self) -> str:
        if self.side.endswith("CE"):
            return "CE"
        if self.side.endswith("PE"):
            return "PE"
        return ""


@dataclass
class ManagedOrder:
    signal: Signal
    order_id: str
    status: str
    fill_price: float = 0.0
    fill_quantity: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    raw: dict = field(default_factory=dict)


class OrderManager:
    def __init__(self, broker: BrokerBase, slippage_pct: float = 0.5,
                 telegram=None):
        self.broker = broker
        self.slippage_pct = slippage_pct
        self._orders: dict[str, ManagedOrder] = {}
        self._telegram = telegram

    def exists(self, tag: str) -> bool:
        return tag in self._orders

    def get(self, tag: str) -> ManagedOrder | None:
        return self._orders.get(tag)

    @property
    def active_orders(self) -> list[ManagedOrder]:
        return [o for o in self._orders.values()
                if o.status in ("PENDING", "TRANSIT", "TRADED")]

    def place_entry(self, signal: Signal, risk_engine: RiskEngine,
                    instrument: str = "") -> OrderResponse | None:
        """Place an entry order. Must pass all risk gates first."""
        if self.exists(signal.tag):
            log.info("Duplicate signal ignored: %s", signal.tag)
            return None

        if not risk_engine.allow_new_trade(instrument=instrument):
            log.warning("Risk gate blocked: %s — %s", signal.tag, risk_engine.last_reason)
            return None

        # Entry: BUY_CE/BUY_PE → broker BUY, SELL_CE/SELL_PE → broker SELL
        if signal.direction == "LONG":
            txn_type = TransactionType.BUY
        else:
            txn_type = TransactionType.SELL

        limit_price = signal.price
        if limit_price <= 0:
            limit_price = self._estimate_limit_price(signal)

        order = Order(
            security_id=signal.security_id,
            exchange_segment=signal.exchange_segment,
            transaction_type=txn_type,
            quantity=signal.quantity,
            order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=limit_price,
            tag=signal.tag[:25],
        )

        resp = self._place_with_retry(order)
        if resp:
            managed = ManagedOrder(
                signal=signal,
                order_id=resp.order_id,
                status=resp.status,
                fill_price=resp.fill_price,
                fill_quantity=resp.fill_quantity,
                raw=resp.raw,
            )
            self._orders[signal.tag] = managed
            log.info("Entry placed: %s %s → %s (id=%s, fill=%.2f)",
                     signal.side, signal.symbol, resp.status,
                     resp.order_id, resp.fill_price)
        return resp

    def place_exit(self, signal: Signal, position_direction: str) -> OrderResponse | None:
        """Place an exit order. Direction is opposite of position direction.

        Closing a LONG position → SELL
        Closing a SHORT position → BUY (buy back what you sold)
        """
        if self.exists(signal.tag):
            log.info("Duplicate exit ignored: %s", signal.tag)
            return None

        if position_direction == "LONG":
            txn_type = TransactionType.SELL
        else:
            txn_type = TransactionType.BUY

        limit_price = signal.price
        if limit_price <= 0:
            limit_price = self._estimate_exit_price(signal, position_direction)

        order = Order(
            security_id=signal.security_id,
            exchange_segment=signal.exchange_segment,
            transaction_type=txn_type,
            quantity=signal.quantity,
            order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=limit_price,
            tag=signal.tag[:25],
        )

        resp = self._place_with_retry(order)
        if resp:
            self._orders[signal.tag] = ManagedOrder(
                signal=signal,
                order_id=resp.order_id,
                status=resp.status,
                fill_price=resp.fill_price,
                fill_quantity=resp.fill_quantity,
                raw=resp.raw,
            )
            log.info("Exit placed: %s %s → %s (id=%s)",
                     txn_type, signal.symbol, resp.status, resp.order_id)
        return resp

    def place_sl_limit(self, security_id: str, exchange_segment: str,
                       direction: str, quantity: int,
                       trigger_price: float, buffer_pct: float = 2.0) -> OrderResponse | None:
        min_tick = 0.05
        if direction == "LONG":
            txn_type = TransactionType.SELL
            pct_buffer = trigger_price * buffer_pct / 100
            limit_price = round(max(trigger_price - max(pct_buffer, min_tick), 0), 2)
        else:
            txn_type = TransactionType.BUY
            pct_buffer = trigger_price * buffer_pct / 100
            limit_price = round(trigger_price + max(pct_buffer, min_tick), 2)

        order = Order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type=txn_type,
            quantity=quantity,
            order_type=OrderType.SL,
            product_type=ProductType.INTRADAY,
            price=limit_price,
            trigger_price=trigger_price,
            tag=f"SL_{security_id}_{int(time.time())}"[:25],
        )

        log.info("SL-LIMIT order: %s %s trigger=%.2f limit=%.2f (buffer=%.1f%%)",
                 txn_type, security_id, trigger_price, limit_price, buffer_pct)
        return self._place_with_retry(order)

    def cancel_all_open(self) -> int:
        cancelled = 0
        for managed in self._orders.values():
            if managed.status in ("PENDING", "TRANSIT"):
                try:
                    self.broker.cancel_order(managed.order_id)
                    managed.status = "CANCELLED"
                    cancelled += 1
                except BrokerError as exc:
                    log.error("Failed to cancel %s: %s", managed.order_id, exc)
        return cancelled

    def check_fill_status(self, tag: str) -> ManagedOrder | None:
        """Poll broker for updated fill status on a pending order."""
        managed = self._orders.get(tag)
        if not managed or managed.status not in ("PENDING", "TRANSIT"):
            return managed
        try:
            status = self.broker.get_order_status(managed.order_id)
            managed.status = status.get("orderStatus", managed.status)
            managed.fill_price = float(status.get("price", 0) or 0)
            managed.fill_quantity = int(status.get("filledQty", 0) or 0)
        except BrokerError as exc:
            log.warning("Status check failed for %s: %s", managed.order_id, exc)
        return managed

    def _place_with_retry(self, order: Order, max_retries: int = 4) -> OrderResponse | None:
        last_exc = None
        for attempt in range(max_retries):
            try:
                return self.broker.place_order(order)
            except BrokerAuthError as exc:
                log.critical("DH-901: Auth failed — cannot place order %s", order.tag)
                if self._telegram:
                    try:
                        self._telegram.error("DH-901_AUTH_EXPIRED",
                            Exception("CRITICAL: Token expired — manual exit required. "
                                      "Renew token at api.dhan.co and restart bot."))
                    except Exception:
                        pass
                return None
            except BrokerRateLimit:
                wait = max(1, 2 ** attempt)
                log.warning("DH-904: Rate limited, backing off %ds (attempt %d/%d)",
                            wait, attempt + 1, max_retries)
                time.sleep(wait)
            except BrokerError as exc:
                last_exc = exc
                wait = 2 ** attempt
                log.error("Order failed (attempt %d/%d): %s", attempt + 1, max_retries, exc)
                if attempt < max_retries - 1:
                    time.sleep(wait)

        ghost = self._check_ghost_fill(order)
        if ghost:
            return ghost

        log.critical("DEFCON 1: %d consecutive order failures for %s — "
                     "broker may be down", max_retries, order.tag)
        if self._telegram:
            try:
                self._telegram.error("DEFCON1_ORDER",
                    Exception(f"BROKER DOWN: {max_retries} retries exhausted for "
                              f"{order.tag}. If you have open positions, call Dhan "
                              f"RMS desk immediately to manually square off."))
            except Exception:
                pass
        return None

    def _check_ghost_fill(self, order: Order) -> OrderResponse | None:
        try:
            positions = self.broker.get_positions()
            for p in positions:
                if p.security_id == order.security_id and p.net_quantity != 0:
                    log.warning("GHOST FILL detected: order for %s timed out but "
                                "position exists on broker (qty=%d)",
                                order.security_id, p.net_quantity)
                    fill_price = p.buy_avg if order.transaction_type == "BUY" else p.sell_avg
                    return OrderResponse(
                        order_id=f"GHOST-{int(time.time())}",
                        status="TRADED",
                        fill_price=fill_price,
                        fill_quantity=abs(p.net_quantity),
                    )
        except Exception as exc:
            log.error("Ghost fill check failed: %s", exc)
        return None

    def _estimate_limit_price(self, signal: Signal) -> float:
        """For entries: add slippage buffer to LTP."""
        try:
            ltp = self.broker.get_ltp(signal.security_id, signal.exchange_segment)
            if ltp <= 0:
                return signal.price if signal.price > 0 else 0
        except BrokerError:
            return signal.price if signal.price > 0 else 0

        if signal.direction == "LONG":
            return round(ltp * (1 + self.slippage_pct / 100), 2)
        return round(ltp * (1 - self.slippage_pct / 100), 2)

    def _estimate_exit_price(self, signal: Signal, position_direction: str) -> float:
        """For exits: apply slippage in the unfavourable direction."""
        try:
            ltp = self.broker.get_ltp(signal.security_id, signal.exchange_segment)
            if ltp <= 0:
                return signal.price if signal.price > 0 else 0
        except BrokerError:
            return signal.price if signal.price > 0 else 0

        if position_direction == "LONG":
            return round(ltp * (1 - self.slippage_pct / 100), 2)
        return round(ltp * (1 + self.slippage_pct / 100), 2)
