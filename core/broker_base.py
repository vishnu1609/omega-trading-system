"""
Broker Abstraction Layer

Every broker implements this interface. The rest of the system talks to
BrokerBase, never to a specific broker. Swap brokers by changing one line
in config.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    SL = "STOP_LOSS"
    SLM = "STOP_LOSS_MARKET"


class ProductType(str, Enum):
    INTRADAY = "INTRADAY"
    CNC = "CNC"
    MARGIN = "MARGIN"


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExchangeSegment(str, Enum):
    NSE_FNO = "NSE_FNO"
    BSE_FNO = "BSE_FNO"
    NSE_EQ = "NSE_EQ"
    BSE_EQ = "BSE_EQ"


@dataclass
class Order:
    security_id: str
    exchange_segment: str
    transaction_type: str
    quantity: int
    order_type: str = OrderType.LIMIT
    product_type: str = ProductType.INTRADAY
    price: float = 0.0
    trigger_price: float = 0.0
    tag: str = ""


@dataclass
class OrderResponse:
    order_id: str
    status: str
    fill_price: float = 0.0
    fill_quantity: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class PositionData:
    security_id: str
    symbol: str
    exchange_segment: str
    buy_quantity: int = 0
    sell_quantity: int = 0
    net_quantity: int = 0
    buy_avg: float = 0.0
    sell_avg: float = 0.0
    pnl: float = 0.0


class BrokerError(Exception):
    pass


class BrokerAuthError(BrokerError):
    pass


class BrokerRateLimit(BrokerError):
    pass


class BrokerBase(ABC):
    """Every broker adapter must implement these methods."""

    @abstractmethod
    def place_order(self, order: Order) -> OrderResponse:
        ...

    @abstractmethod
    def modify_order(self, order_id: str, **kwargs: Any) -> OrderResponse:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderResponse:
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> dict:
        ...

    @abstractmethod
    def get_positions(self) -> list[PositionData]:
        ...

    @abstractmethod
    def get_funds(self) -> dict:
        ...

    @abstractmethod
    def get_ltp(self, security_id: str, exchange_segment: str) -> float:
        ...

    @abstractmethod
    def get_option_chain(self, underlying_id: str, expiry: str,
                         exchange_segment: str) -> list[dict]:
        ...

    def get_available_margin(self) -> float:
        funds = self.get_funds()
        return float(funds.get("availabelBalance", 0) or
                     funds.get("available_balance", 0) or
                     funds.get("net", 0) or 0)
