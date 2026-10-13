"""
Paper Trading Broker

Simulates order fills with realistic slippage and latency.
All state is in-memory — resets each session. Logs every action to disk.
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from typing import Any

from core.broker_base import (
    BrokerBase,
    Order,
    OrderResponse,
    PositionData,
)

log = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")


@dataclass
class SlippageContext:
    price: float
    transaction_type: str
    bid: float = 0.0
    ask: float = 0.0
    oi: int = 0
    moneyness_pct: float = 0.0
    dte_days: float = 30.0
    vix: float = 14.0


class SlippageModel:
    """
    Realistic slippage for Indian index options.

    Sources of slippage:
    1. Half the bid-ask spread (crossing to fill)
    2. Time-of-day widening (open/close auctions)
    3. Moneyness — far OTM has wider spreads
    4. Low OI / illiquid strikes
    5. VIX-scaled volatility surcharge
    6. Random jitter (market microstructure noise)
    """

    MIN_TICK = 0.05

    def estimate(self, ctx: SlippageContext) -> float:
        if ctx.price <= 0:
            return 0.0

        slip = 0.0

        if ctx.bid > 0 and ctx.ask > 0 and ctx.ask > ctx.bid:
            half_spread = (ctx.ask - ctx.bid) / 2
            slip += half_spread
        else:
            slip += max(ctx.price * 0.003, self.MIN_TICK)

        now = datetime.now(_IST)
        h, m = now.hour, now.minute
        mins = h * 60 + m
        if mins < 9 * 60 + 20:
            slip += ctx.price * 0.005
        elif mins < 9 * 60 + 35:
            slip += ctx.price * 0.002
        elif mins > 15 * 60 + 15:
            slip += ctx.price * 0.003

        if ctx.moneyness_pct > 3.0:
            otm_factor = min((ctx.moneyness_pct - 3.0) / 5.0, 1.0)
            slip += ctx.price * 0.004 * otm_factor

        if 0 < ctx.oi < 5000:
            liquidity_penalty = (1 - ctx.oi / 5000) * ctx.price * 0.005
            slip += liquidity_penalty

        if ctx.vix > 18:
            vix_extra = min((ctx.vix - 18) / 20, 1.0) * ctx.price * 0.004
            slip += vix_extra

        jitter = random.gauss(0, ctx.price * 0.0008)
        slip += abs(jitter)

        slip = max(slip, self.MIN_TICK)
        slip = math.ceil(slip / self.MIN_TICK) * self.MIN_TICK

        if ctx.transaction_type == "BUY":
            return round(slip, 2)
        return round(-slip, 2)


_SLIPPAGE = SlippageModel()


class PaperBroker(BrokerBase):
    def __init__(self, initial_capital: float = 200_000,
                 slippage_bps: float = 0.0):
        self._capital = initial_capital
        self._slippage_bps = slippage_bps
        self.slippage_model = _SLIPPAGE
        self._contract_cache: dict[str, dict] = {}
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
            ctx = self._build_slip_ctx(order)
            slip = self.slippage_model.estimate(ctx)
            fill_price = round(fill_price + slip, 2)
            fill_price = max(fill_price, SlippageModel.MIN_TICK)

        record = {
            "orderId": oid,
            "securityId": order.security_id,
            "exchangeSegment": order.exchange_segment,
            "transactionType": order.transaction_type,
            "quantity": order.quantity,
            "orderPrice": order.price,
            "fillPrice": fill_price,
            "slippage": round(fill_price - order.price, 2),
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
                ctx = self._build_slip_ctx(order, ltp_override=ltp)
                slip = self.slippage_model.estimate(ctx)
                fill_price = round(fill_price + slip, 2)
                fill_price = max(fill_price, SlippageModel.MIN_TICK)

                record = {
                    "orderId": oid,
                    "securityId": order.security_id,
                    "exchangeSegment": order.exchange_segment,
                    "transactionType": order.transaction_type,
                    "quantity": order.quantity,
                    "orderPrice": pending["limit_price"],
                    "fillPrice": fill_price,
                    "slippage": round(fill_price - ltp, 2),
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

    def cache_contract(self, security_id: str, *,
                       bid: float = 0, ask: float = 0, oi: int = 0,
                       moneyness_pct: float = 0, dte_days: float = 30) -> None:
        self._contract_cache[security_id] = {
            "bid": bid, "ask": ask, "oi": oi,
            "moneyness_pct": moneyness_pct, "dte_days": dte_days,
        }

    def _build_slip_ctx(self, order: Order,
                        ltp_override: float = 0) -> SlippageContext:
        price = ltp_override if ltp_override > 0 else order.price
        cached = self._contract_cache.get(order.security_id, {})
        vix = 14.0
        if self.live_feed:
            v = self.live_feed.get_ltp("26017", "IDX_I")
            if v > 0:
                vix = v
        return SlippageContext(
            price=price,
            transaction_type=order.transaction_type,
            bid=cached.get("bid", 0),
            ask=cached.get("ask", 0),
            oi=cached.get("oi", 0),
            moneyness_pct=cached.get("moneyness_pct", 0),
            dte_days=cached.get("dte_days", 30),
            vix=vix,
        )
