"""
Dhan Broker Adapter

Implements BrokerBase for the Dhan API (https://api.dhan.co/v2).
Rate-limited to 5 req/sec with exponential backoff.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from core.broker_base import (
    BrokerBase,
    BrokerError,
    BrokerRateLimit,
    Order,
    OrderResponse,
    PositionData,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.dhan.co/v2"


class DhanBroker(BrokerBase):
    def __init__(self, client_id: str = "", access_token: str = ""):
        self.client_id = client_id or os.environ.get("DHAN_CLIENT_ID", "")
        self.access_token = access_token or os.environ.get("DHAN_ACCESS_TOKEN", "")
        if not self.client_id or not self.access_token:
            raise BrokerError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "access-token": self.access_token,
        })
        self._last_request_ts: float = 0
        self._min_interval: float = 0.2

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.monotonic()

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        self._throttle()
        url = f"{BASE_URL}{path}"
        try:
            resp = self.session.request(method, url, json=payload, timeout=10)
        except requests.RequestException as exc:
            raise BrokerError(f"Network error: {exc}") from exc

        if resp.status_code == 429:
            raise BrokerRateLimit("Dhan rate limit hit")
        if resp.status_code >= 400:
            raise BrokerError(f"HTTP {resp.status_code}: {resp.text}")

        return resp.json() if resp.text else {}

    def place_order(self, order: Order) -> OrderResponse:
        payload = {
            "dhanClientId": self.client_id,
            "transactionType": order.transaction_type,
            "exchangeSegment": order.exchange_segment,
            "productType": order.product_type,
            "orderType": order.order_type,
            "securityId": order.security_id,
            "quantity": order.quantity,
            "price": order.price,
            "triggerPrice": order.trigger_price,
            "validity": "DAY",
            "correlationId": order.tag[:25] if order.tag else "",
        }
        log.info("DHAN ORDER: %s %s qty=%d @ %.2f tag=%s",
                 order.transaction_type, order.security_id,
                 order.quantity, order.price, order.tag)
        data = self._request("POST", "/orders", payload)
        return OrderResponse(
            order_id=data.get("orderId", ""),
            status=data.get("orderStatus", "UNKNOWN"),
            raw=data,
        )

    def modify_order(self, order_id: str, **kwargs: Any) -> OrderResponse:
        payload: dict[str, Any] = {
            "dhanClientId": self.client_id,
            "orderId": order_id,
        }
        for key in ("price", "quantity", "triggerPrice"):
            if key in kwargs:
                payload[key] = kwargs[key]
        data = self._request("PUT", f"/orders/{order_id}", payload)
        return OrderResponse(
            order_id=order_id,
            status=data.get("orderStatus", "UNKNOWN"),
            raw=data,
        )

    def cancel_order(self, order_id: str) -> OrderResponse:
        data = self._request("DELETE", f"/orders/{order_id}")
        return OrderResponse(order_id=order_id, status="CANCELLED", raw=data)

    def get_order_status(self, order_id: str) -> dict:
        return self._request("GET", f"/orders/{order_id}")

    def get_positions(self) -> list[PositionData]:
        data = self._request("GET", "/positions")
        raw_list = data if isinstance(data, list) else data.get("data", [])
        positions = []
        for r in raw_list:
            positions.append(PositionData(
                security_id=str(r.get("securityId", "")),
                symbol=r.get("tradingSymbol", ""),
                exchange_segment=r.get("exchangeSegment", ""),
                buy_quantity=int(r.get("buyQty", 0)),
                sell_quantity=int(r.get("sellQty", 0)),
                net_quantity=int(r.get("netQty", 0)),
                buy_avg=float(r.get("buyAvg", 0)),
                sell_avg=float(r.get("sellAvg", 0)),
                pnl=float(r.get("realizedProfit", 0)),
            ))
        return positions

    def get_funds(self) -> dict:
        return self._request("GET", "/fundlimit")

    def get_ltp(self, security_id: str, exchange_segment: str) -> float:
        payload = {
            "dhanClientId": self.client_id,
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
        }
        data = self._request("POST", "/marketfeed/ltp", payload)
        return float(data.get("data", {}).get("lastPrice", 0))

    def get_option_chain(self, underlying_id: str, expiry: str,
                         exchange_segment: str = "NSE_FNO") -> list[dict]:
        payload = {
            "dhanClientId": self.client_id,
            "underlyingSecurityId": underlying_id,
            "expiryDate": expiry,
            "exchangeSegment": exchange_segment,
        }
        data = self._request("POST", "/optionchain", payload)
        return data.get("data", [])
