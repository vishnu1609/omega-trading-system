"""
Dhan Broker Adapter

Implements BrokerBase for the Dhan API (https://api.dhan.co/v2).
Handles DH-901 (auth expired), DH-904 (rate limit), and DH-906 (market closed).
Thread-safe with API mutex and 100ms throttle.

Uses httpx with connection pooling (adapted from OpenAlgo) for lower-latency
order execution compared to requests.Session.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

from core.broker_base import (
    BrokerAuthError,
    BrokerBase,
    BrokerError,
    BrokerRateLimit,
    Order,
    OrderResponse,
    PositionData,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.dhan.co/v2"

DHAN_ERROR_CODES = {
    "DH-901": "Invalid or expired authentication token",
    "DH-904": "API rate limit exceeded",
    "DH-906": "Market closed or order rejected by RMS",
}


def _build_httpx_client(access_token: str, client_id: str) -> httpx.Client:
    """Create a pooled httpx client (connection reuse across requests)."""
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": access_token,
            "client-id": client_id,
        },
        timeout=10.0,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30,
        ),
    )


class DhanBroker(BrokerBase):
    def __init__(self, client_id: str = "", access_token: str = ""):
        self.client_id = client_id or os.environ.get("DHAN_CLIENT_ID", "")
        self.access_token = access_token or os.environ.get("DHAN_ACCESS_TOKEN", "")
        if not self.client_id or not self.access_token:
            raise BrokerError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set")
        self._client = _build_httpx_client(self.access_token, self.client_id)
        self._last_request_ts: float = 0
        self._min_interval: float = 0.1
        self._api_lock = threading.Lock()
        self._auth_valid = True
        self._request_count: int = 0
        self._error_count: int = 0

    def validate_token(self) -> bool:
        try:
            self.get_funds()
            self._auth_valid = True
            log.info("Dhan token validated successfully")
            return True
        except BrokerAuthError:
            self._auth_valid = False
            log.critical("DH-901: Invalid or expired token — bot cannot trade")
            return False
        except BrokerError as exc:
            log.warning("Token validation inconclusive: %s", exc)
            return True

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.monotonic()

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        with self._api_lock:
            self._throttle()
            self._request_count += 1
            try:
                if method == "GET":
                    resp = self._client.get(path)
                elif method == "POST":
                    resp = self._client.post(path, json=payload)
                elif method == "PUT":
                    resp = self._client.put(path, json=payload)
                elif method == "DELETE":
                    resp = self._client.delete(path)
                else:
                    resp = self._client.request(method, path, json=payload)
            except httpx.HTTPError as exc:
                self._error_count += 1
                raise BrokerError(f"Network error: {exc}") from exc

            if resp.status_code == 401:
                self._auth_valid = False
                self._error_count += 1
                raise BrokerAuthError("DH-901: Token expired or invalid")

            if resp.status_code == 429:
                self._error_count += 1
                raise BrokerRateLimit("DH-904: API rate limit exceeded")

            if resp.status_code >= 400:
                self._error_count += 1
                body = resp.text
                for code, desc in DHAN_ERROR_CODES.items():
                    if code in body:
                        if code == "DH-901":
                            self._auth_valid = False
                            raise BrokerAuthError(f"{code}: {desc}")
                        if code == "DH-904":
                            raise BrokerRateLimit(f"{code}: {desc}")
                raise BrokerError(f"HTTP {resp.status_code}: {body}")

            return resp.json() if resp.text else {}

    @property
    def is_auth_valid(self) -> bool:
        return self._auth_valid

    def place_order(self, order: Order) -> OrderResponse:
        if not self._auth_valid:
            raise BrokerAuthError("DH-901: Token invalid — cannot place orders")

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
        for key in ("price", "quantity", "triggerPrice", "orderType"):
            if key in kwargs:
                payload[key] = kwargs[key]
        log.info("DHAN MODIFY: order=%s %s", order_id,
                 {k: v for k, v in kwargs.items() if k != "dhanClientId"})
        data = self._request("PUT", f"/orders/{order_id}", payload)
        return OrderResponse(
            order_id=order_id,
            status=data.get("orderStatus", "UNKNOWN"),
            raw=data,
        )

    def cancel_order(self, order_id: str) -> OrderResponse:
        log.info("DHAN CANCEL: order=%s", order_id)
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
        payload = {exchange_segment: [int(security_id)]}
        data = self._request("POST", "/marketfeed/ltp", payload)
        seg_data = data.get("data", {}).get(exchange_segment, {})
        sid_data = seg_data.get(str(security_id), seg_data.get(security_id, {}))
        return float(sid_data.get("last_price", 0))

    def get_option_chain(self, underlying_id: str, expiry: str,
                         exchange_segment: str = "IDX_I") -> list[dict]:
        payload = {
            "UnderlyingScrip": int(underlying_id),
            "UnderlyingSeg": exchange_segment,
            "Expiry": expiry,
        }
        data = self._request("POST", "/optionchain", payload)
        return data.get("data", [])

    def get_broker_metrics(self) -> dict:
        """Return broker API health for dashboard."""
        return {
            "total_requests": self._request_count,
            "error_count": self._error_count,
            "auth_valid": self._auth_valid,
            "error_rate_pct": round(
                self._error_count / max(1, self._request_count) * 100, 1),
        }

    def close(self) -> None:
        """Clean up the httpx client."""
        try:
            self._client.close()
        except Exception:
            pass
