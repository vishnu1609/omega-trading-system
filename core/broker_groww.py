"""
Groww Broker Adapter

Implements BrokerBase for the Groww Trade API (https://api.groww.in/v1).
Used for equity cash (CNC/MIS) trades alongside the Dhan adapter (which
handles index options).

Auth: Bearer token in Authorization header + X-API-VERSION: 1.0
Rate limits: Orders 10/s 250/min, Market data 10/s 300/min, Non-trading 20/s 500/min

Ref: https://groww.in/trade-api/docs/curl
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid
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

BASE_URL = "https://api.groww.in"

GROWW_ERROR_MAP = {
    "GA000": "Internal error",
    "GA001": "Bad request",
    "GA003": "Unable to serve request currently",
    "GA004": "Entity does not exist",
    "GA005": "Not authorised",
    "GA006": "Cannot process request",
    "GA007": "Duplicate order reference ID",
}

SEGMENT_MAP = {
    "NSE_EQ": "CASH",
    "BSE_EQ": "CASH",
    "NSE_FNO": "FNO",
    "BSE_FNO": "FNO",
    "CASH": "CASH",
    "FNO": "FNO",
}

EXCHANGE_MAP = {
    "NSE_EQ": "NSE",
    "BSE_EQ": "BSE",
    "NSE_FNO": "NSE",
    "BSE_FNO": "BSE",
    "NSE": "NSE",
    "BSE": "BSE",
}

ORDER_TYPE_MAP = {
    "LIMIT": "LIMIT",
    "MARKET": "MARKET",
    "STOP_LOSS": "SL",
    "STOP_LOSS_MARKET": "SL_M",
    "SL": "SL",
    "SLM": "SL_M",
    "SL_M": "SL_M",
}

PRODUCT_MAP = {
    "INTRADAY": "MIS",
    "CNC": "CNC",
    "MARGIN": "NRML",
    "MIS": "MIS",
    "NRML": "NRML",
}


def _gen_order_ref() -> str:
    """Generate a unique order reference ID (8-20 alphanumeric + hyphens)."""
    return f"OM-{uuid.uuid4().hex[:14]}"


def _build_httpx_client(access_token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-API-VERSION": "1.0",
        },
        timeout=10.0,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30,
        ),
    )


class GrowwBroker(BrokerBase):
    def __init__(self, access_token: str = ""):
        self.access_token = access_token or os.environ.get("GROWW_ACCESS_TOKEN", "")
        if not self.access_token:
            raise BrokerError("GROWW_ACCESS_TOKEN must be set")
        self._client = _build_httpx_client(self.access_token)
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
            log.info("Groww token validated successfully")
            return True
        except BrokerAuthError:
            self._auth_valid = False
            log.error("Groww token validation FAILED")
            return False
        except Exception as e:
            log.warning("Groww token validation error: %s", e)
            return False

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.monotonic()

    def _request(self, method: str, path: str, payload: dict | list | None = None,
                 params: dict | None = None) -> dict:
        with self._api_lock:
            self._throttle()
            self._request_count += 1
            try:
                if method == "GET":
                    resp = self._client.get(path, params=params)
                elif method == "POST":
                    resp = self._client.post(path, json=payload, params=params)
                elif method == "PUT":
                    resp = self._client.put(path, json=payload)
                elif method == "DELETE":
                    resp = self._client.delete(path, params=params)
                else:
                    raise BrokerError(f"Unsupported HTTP method: {method}")

                data = resp.json()

                if resp.status_code == 401 or (
                    isinstance(data, dict) and
                    data.get("error", {}).get("code") == "GA005"
                ):
                    self._auth_valid = False
                    self._error_count += 1
                    raise BrokerAuthError(
                        f"Groww auth failed: {data.get('error', {}).get('message', 'Unauthorized')}"
                    )

                if resp.status_code == 429:
                    self._error_count += 1
                    raise BrokerRateLimit("Groww rate limit exceeded")

                if resp.status_code >= 400:
                    self._error_count += 1
                    err = data.get("error", {}) if isinstance(data, dict) else {}
                    code = err.get("code", "")
                    msg = err.get("message", str(data))
                    friendly = GROWW_ERROR_MAP.get(code, "")
                    raise BrokerError(
                        f"Groww API error {code}: {msg}" +
                        (f" ({friendly})" if friendly else "")
                    )

                if isinstance(data, dict) and data.get("status") == "FAILURE":
                    self._error_count += 1
                    err = data.get("error", {})
                    raise BrokerError(
                        f"Groww FAILURE: {err.get('code', '')} — {err.get('message', '')}"
                    )

                if isinstance(data, dict):
                    return data.get("payload", data)
                return data

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                self._error_count += 1
                log.error("Groww network error: %s", e)
                raise BrokerError(f"Groww network error: {e}") from e

    def is_auth_valid(self) -> bool:
        return self._auth_valid

    def place_order(self, order: Order) -> OrderResponse:
        if not self._auth_valid:
            raise BrokerAuthError("Groww token invalid — cannot place orders")

        segment = SEGMENT_MAP.get(order.exchange_segment, "CASH")
        exchange = EXCHANGE_MAP.get(order.exchange_segment, "NSE")
        order_type = ORDER_TYPE_MAP.get(order.order_type, "MARKET")
        product = PRODUCT_MAP.get(order.product_type, "CNC")

        payload = {
            "trading_symbol": order.security_id,
            "quantity": order.quantity,
            "validity": "DAY",
            "exchange": exchange,
            "segment": segment,
            "product": product,
            "order_type": order_type,
            "transaction_type": order.transaction_type,
            "order_reference_id": order.tag[:20] if order.tag else _gen_order_ref(),
        }

        if order_type in ("LIMIT", "SL") and order.price > 0:
            payload["price"] = round(order.price, 2)
        if order_type in ("SL", "SL_M") and order.trigger_price > 0:
            payload["trigger_price"] = round(order.trigger_price, 2)

        log.info("GROWW ORDER: %s %s %s qty=%d @ %.2f product=%s",
                 order.transaction_type, order.security_id,
                 order_type, order.quantity, order.price, product)

        data = self._request("POST", "/v1/order/create", payload)
        return OrderResponse(
            order_id=data.get("groww_order_id", ""),
            status=data.get("order_status", "UNKNOWN"),
            raw=data,
        )

    def modify_order(self, order_id: str, **kwargs: Any) -> OrderResponse:
        segment = kwargs.pop("segment", "CASH")
        payload: dict[str, Any] = {
            "groww_order_id": order_id,
            "segment": segment,
            "order_type": ORDER_TYPE_MAP.get(
                kwargs.get("order_type", "LIMIT"), "LIMIT"
            ),
        }
        for key in ("quantity", "price", "trigger_price"):
            if key in kwargs:
                payload[key] = kwargs[key]

        log.info("GROWW MODIFY: order=%s %s", order_id, payload)
        data = self._request("POST", "/v1/order/modify", payload)
        return OrderResponse(
            order_id=order_id,
            status=data.get("order_status", "UNKNOWN"),
            raw=data,
        )

    def cancel_order(self, order_id: str, segment: str = "CASH") -> OrderResponse:
        payload = {
            "groww_order_id": order_id,
            "segment": segment,
        }
        log.info("GROWW CANCEL: order=%s segment=%s", order_id, segment)
        data = self._request("POST", "/v1/order/cancel", payload)
        return OrderResponse(
            order_id=order_id,
            status=data.get("order_status", "CANCELLED"),
            raw=data,
        )

    def get_order_status(self, order_id: str, segment: str = "CASH") -> dict:
        return self._request(
            "GET", f"/v1/order/status/{order_id}",
            params={"segment": segment},
        )

    def get_order_detail(self, order_id: str, segment: str = "CASH") -> dict:
        return self._request(
            "GET", f"/v1/order/detail/{order_id}",
            params={"segment": segment},
        )

    def get_positions(self, segment: str = "CASH") -> list[PositionData]:
        data = self._request(
            "GET", "/v1/positions/user",
            params={"segment": segment},
        )
        raw_list = data.get("positions", []) if isinstance(data, dict) else []
        positions = []
        for r in raw_list:
            net_qty = int(r.get("quantity", 0))
            credit_qty = int(r.get("credit_quantity", 0))
            debit_qty = int(r.get("debit_quantity", 0))
            positions.append(PositionData(
                security_id=r.get("trading_symbol", ""),
                symbol=r.get("trading_symbol", ""),
                exchange_segment=r.get("exchange", "NSE") + "_EQ",
                buy_quantity=debit_qty,
                sell_quantity=credit_qty,
                net_quantity=net_qty,
                buy_avg=float(r.get("debit_price", 0)),
                sell_avg=float(r.get("credit_price", 0)),
                pnl=float(r.get("realised_pnl", 0)),
            ))
        return positions

    def get_holdings(self) -> list[dict]:
        data = self._request("GET", "/v1/holdings/user")
        return data.get("holdings", []) if isinstance(data, dict) else []

    def get_funds(self) -> dict:
        return self._request("GET", "/v1/margins/detail/user")

    def get_available_margin(self) -> float:
        funds = self.get_funds()
        eq = funds.get("equity_margin_details", {})
        cnc_avail = float(eq.get("cnc_balance_available", 0))
        mis_avail = float(eq.get("mis_balance_available", 0))
        clear_cash = float(funds.get("clear_cash", 0))
        return max(cnc_avail, clear_cash)

    def get_ltp(self, security_id: str, exchange_segment: str = "NSE_EQ") -> float:
        """Get LTP for an equity symbol.

        security_id here is the trading_symbol (e.g. "RELIANCE").
        exchange_segment maps to exchange prefix (NSE/BSE) + segment (CASH).
        """
        exchange = EXCHANGE_MAP.get(exchange_segment, "NSE")
        segment = SEGMENT_MAP.get(exchange_segment, "CASH")
        exchange_symbol = f"{exchange}_{security_id}"

        data = self._request(
            "GET", "/v1/live-data/ltp",
            params={
                "segment": segment,
                "exchange_symbols": exchange_symbol,
            },
        )
        if isinstance(data, dict):
            ltp = data.get(exchange_symbol, 0)
            if isinstance(ltp, (int, float)):
                return float(ltp)
        return 0.0

    def get_quote(self, trading_symbol: str, exchange: str = "NSE",
                  segment: str = "CASH") -> dict:
        return self._request(
            "GET", "/v1/live-data/quote",
            params={
                "exchange": exchange,
                "segment": segment,
                "trading_symbol": trading_symbol,
            },
        )

    def get_option_chain(self, underlying_id: str, expiry: str,
                         exchange_segment: str = "NSE") -> list[dict]:
        exchange = EXCHANGE_MAP.get(exchange_segment, exchange_segment)
        data = self._request(
            "GET",
            f"/v1/option-chain/exchange/{exchange}/underlying/{underlying_id}",
            params={"expiry_date": expiry},
        )
        strikes_map = data.get("strikes", {}) if isinstance(data, dict) else {}
        result = []
        for strike_str, legs in strikes_map.items():
            for opt_type in ("CE", "PE"):
                if opt_type in legs:
                    leg = legs[opt_type]
                    result.append({
                        "strike": float(strike_str),
                        "option_type": opt_type,
                        "trading_symbol": leg.get("trading_symbol", ""),
                        "ltp": float(leg.get("ltp", 0)),
                        "oi": int(leg.get("open_interest", 0)),
                        "volume": int(leg.get("volume", 0)),
                        "greeks": leg.get("greeks", {}),
                    })
        return result

    def get_required_margin(self, orders: list[dict],
                            segment: str = "CASH") -> dict:
        return self._request(
            "POST", "/v1/margins/detail/orders",
            payload=orders,
            params={"segment": segment},
        )

    def get_broker_metrics(self) -> dict:
        return {
            "broker": "groww",
            "total_requests": self._request_count,
            "error_count": self._error_count,
            "auth_valid": self._auth_valid,
            "error_rate_pct": round(
                self._error_count / max(1, self._request_count) * 100, 1),
        }

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
