"""
Paper Trading Broker

Simulates order fills with realistic slippage and latency.
All state is in-memory — resets each session. Logs every action to disk.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from core.broker_base import (
    BrokerBase,
    Order,
    OrderResponse,
    PositionData,
)

log = logging.getLogger(__name__)


class PaperBroker(BrokerBase):
    def __init__(self, initial_capital: float = 200_000,
                 slippage_bps: float = 5.0):
        self._capital = initial_capital
        self._slippage_bps = slippage_bps
        self._orders: list[dict] = []
        self._positions: dict[str, dict] = {}
        self._pending_sl: dict[str, dict] = {}
        self._counter = 0
        self.live_feed = None
        self.data_broker = None
        self._ltp_cache: dict[str, float] = {}
        self._ltp_ts: dict[str, float] = {}
        self._rate_limited_until: float = 0

    def place_order(self, order: Order) -> OrderResponse:
        self._counter += 1
        oid = f"PAPER-{self._counter:06d}"

        if order.order_type == "STOP_LOSS" and order.trigger_price > 0:
            self._pending_sl[oid] = {
                "orderId": oid,
                "order": order,
                "trigger_price": order.trigger_price,
                "limit_price": order.price,
                "status": "PENDING",
            }
            log.info("[PAPER] SL order PENDING: %s %s trigger=%.2f limit=%.2f id=%s",
                     order.transaction_type, order.security_id,
                     order.trigger_price, order.price, oid)
            return OrderResponse(
                order_id=oid,
                status="PENDING",
                fill_price=0,
                fill_quantity=0,
                raw=self._pending_sl[oid],
            )

        fill_price = order.price
        if fill_price > 0:
            slip = fill_price * self._slippage_bps / 10_000
            if order.transaction_type == "BUY":
                fill_price = round(fill_price + slip, 2)
            else:
                fill_price = round(fill_price - slip, 2)

        record = {
            "orderId": oid,
            "securityId": order.security_id,
            "exchangeSegment": order.exchange_segment,
            "transactionType": order.transaction_type,
            "quantity": order.quantity,
            "orderPrice": order.price,
            "fillPrice": fill_price,
            "status": "TRADED",
            "tag": order.tag,
            "timestamp": datetime.now().isoformat(),
        }
        self._orders.append(record)

        sid = order.security_id
        pos = self._positions.get(sid, {
            "security_id": sid,
            "symbol": order.tag,
            "exchange_segment": order.exchange_segment,
            "buy_qty": 0, "sell_qty": 0,
            "buy_value": 0.0, "sell_value": 0.0,
        })
        if order.transaction_type == "BUY":
            pos["buy_qty"] += order.quantity
            pos["buy_value"] += fill_price * order.quantity
        else:
            pos["sell_qty"] += order.quantity
            pos["sell_value"] += fill_price * order.quantity
        self._positions[sid] = pos

        log.info("[PAPER] %s %s qty=%d price=%.2f→%.2f id=%s",
                 order.transaction_type, order.security_id,
                 order.quantity, order.price, fill_price, oid)

        return OrderResponse(
            order_id=oid,
            status="TRADED",
            fill_price=fill_price,
            fill_quantity=order.quantity,
            raw=record,
        )

    def modify_order(self, order_id: str, **kwargs: Any) -> OrderResponse:
        if order_id in self._pending_sl:
            pending = self._pending_sl[order_id]
            if "trigger_price" in kwargs:
                pending["trigger_price"] = kwargs["trigger_price"]
            if "price" in kwargs:
                pending["limit_price"] = kwargs["price"]
            log.info("[PAPER] SL order modified: %s trigger=%.2f limit=%.2f",
                     order_id, pending["trigger_price"], pending["limit_price"])
            return OrderResponse(order_id=order_id, status="MODIFIED")
        log.info("[PAPER] Modify order %s: %s", order_id, kwargs)
        return OrderResponse(order_id=order_id, status="MODIFIED")

    def cancel_order(self, order_id: str) -> OrderResponse:
        if order_id in self._pending_sl:
            del self._pending_sl[order_id]
            log.info("[PAPER] Pending SL cancelled: %s", order_id)
            return OrderResponse(order_id=order_id, status="CANCELLED")
        log.info("[PAPER] Cancel order %s", order_id)
        return OrderResponse(order_id=order_id, status="CANCELLED")

    def get_order_status(self, order_id: str) -> dict:
        if order_id in self._pending_sl:
            return {"orderId": order_id, "status": "PENDING"}
        for o in reversed(self._orders):
            if o["orderId"] == order_id:
                return o
        return {"orderId": order_id, "status": "UNKNOWN"}

    def check_pending_sl_orders(self) -> list[dict]:
        triggered = []
        for oid in list(self._pending_sl.keys()):
            pending = self._pending_sl[oid]
            order: Order = pending["order"]
            ltp = self.get_ltp(order.security_id, order.exchange_segment)
            if ltp <= 0:
                continue

            hit = False
            if order.transaction_type == "SELL" and ltp <= pending["trigger_price"]:
                hit = True
            elif order.transaction_type == "BUY" and ltp >= pending["trigger_price"]:
                hit = True

            if hit:
                del self._pending_sl[oid]
                fill_price = ltp
                slip = fill_price * self._slippage_bps / 10_000
                if order.transaction_type == "BUY":
                    fill_price = round(fill_price + slip, 2)
                else:
                    fill_price = round(fill_price - slip, 2)

                record = {
                    "orderId": oid,
                    "securityId": order.security_id,
                    "exchangeSegment": order.exchange_segment,
                    "transactionType": order.transaction_type,
                    "quantity": order.quantity,
                    "orderPrice": pending["limit_price"],
                    "fillPrice": fill_price,
                    "status": "TRADED",
                    "tag": order.tag,
                    "timestamp": datetime.now().isoformat(),
                }
                self._orders.append(record)

                sid = order.security_id
                pos = self._positions.get(sid, {
                    "security_id": sid,
                    "symbol": order.tag,
                    "exchange_segment": order.exchange_segment,
                    "buy_qty": 0, "sell_qty": 0,
                    "buy_value": 0.0, "sell_value": 0.0,
                })
                if order.transaction_type == "BUY":
                    pos["buy_qty"] += order.quantity
                    pos["buy_value"] += fill_price * order.quantity
                else:
                    pos["sell_qty"] += order.quantity
                    pos["sell_value"] += fill_price * order.quantity
                self._positions[sid] = pos

                log.info("[PAPER] SL TRIGGERED: %s %s @ %.2f (trigger=%.2f) id=%s",
                         order.transaction_type, order.security_id,
                         fill_price, pending["trigger_price"], oid)
                triggered.append(record)
        return triggered

    def get_positions(self) -> list[PositionData]:
        result = []
        for p in self._positions.values():
            bq = p["buy_qty"]
            sq = p["sell_qty"]
            result.append(PositionData(
                security_id=p["security_id"],
                symbol=p["symbol"],
                exchange_segment=p["exchange_segment"],
                buy_quantity=bq,
                sell_quantity=sq,
                net_quantity=bq - sq,
                buy_avg=p["buy_value"] / bq if bq else 0,
                sell_avg=p["sell_value"] / sq if sq else 0,
            ))
        return result

    def get_funds(self) -> dict:
        return {"availabelBalance": self._capital}

    def get_ltp(self, security_id: str, exchange_segment: str) -> float:
        cache_key = f"{exchange_segment}:{security_id}"
        now = time.time()
        cache_ttl = 30 if now < self._rate_limited_until else 5
        cache_age = now - self._ltp_ts.get(cache_key, 0)
        if cache_age < cache_ttl and cache_key in self._ltp_cache:
            return self._ltp_cache[cache_key]
        if self.live_feed:
            ltp = self.live_feed.get_ltp(security_id, exchange_segment)
            if ltp > 0:
                self._ltp_cache[cache_key] = ltp
                self._ltp_ts[cache_key] = now
                return ltp
        if now < self._rate_limited_until:
            return self._ltp_cache.get(cache_key, 0.0)
        if self.data_broker:
            try:
                ltp = self.data_broker.get_ltp(security_id, exchange_segment)
                if ltp > 0:
                    self._ltp_cache[cache_key] = ltp
                    self._ltp_ts[cache_key] = now
                    return ltp
            except Exception as e:
                err_str = str(e)
                if "rate limit" in err_str.lower() or "DH-904" in err_str:
                    self._rate_limited_until = now + 60
                    log.warning("[PAPER] Dhan API rate limited — backing off 60s")
                else:
                    log.warning("[PAPER] Dhan API LTP failed: %s", e)
                self._ltp_ts[cache_key] = now
        return self._ltp_cache.get(cache_key, 0.0)

    def get_option_chain(self, underlying_id: str, expiry: str,
                         exchange_segment: str = "NSE_FNO") -> list[dict]:
        if self.data_broker:
            return self.data_broker.get_option_chain(
                underlying_id, expiry, exchange_segment)
        return []
