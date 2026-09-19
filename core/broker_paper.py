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
    def __init__(self, initial_capital: float = 1_000_000,
                 slippage_bps: float = 5.0):
        self._capital = initial_capital
        self._slippage_bps = slippage_bps
        self._orders: list[dict] = []
        self._positions: dict[str, dict] = {}
        self._counter = 0

    def place_order(self, order: Order) -> OrderResponse:
        self._counter += 1
        oid = f"PAPER-{self._counter:06d}"

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
        log.info("[PAPER] Modify order %s: %s", order_id, kwargs)
        return OrderResponse(order_id=order_id, status="MODIFIED")

    def cancel_order(self, order_id: str) -> OrderResponse:
        log.info("[PAPER] Cancel order %s", order_id)
        return OrderResponse(order_id=order_id, status="CANCELLED")

    def get_order_status(self, order_id: str) -> dict:
        for o in reversed(self._orders):
            if o["orderId"] == order_id:
                return o
        return {"orderId": order_id, "status": "UNKNOWN"}

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
        return 0.0

    def get_option_chain(self, underlying_id: str, expiry: str,
                         exchange_segment: str = "NSE_FNO") -> list[dict]:
        return []
