from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

EXPIRY_URL = "https://api.dhan.co/v2/optionchain/expirylist"
CHAIN_URL = "https://api.dhan.co/v2/optionchain"
LTP_URL = "https://api.dhan.co/v2/marketfeed/ltp"

UNDERLYING_MAP = {
    "NIFTY": ("13", "IDX_I"),
    "BANKNIFTY": ("25", "IDX_I"),
    "SENSEX": ("51", "IDX_I"),
    "FINNIFTY": ("27", "IDX_I"),
    "BANKEX": ("12", "IDX_I"),
}

EXCHANGE_FNO = {
    "NIFTY": "NSE_FNO",
    "BANKNIFTY": "NSE_FNO",
    "FINNIFTY": "NSE_FNO",
    "SENSEX": "BSE_FNO",
    "BANKEX": "BSE_FNO",
}


@dataclass
class OptionContract:
    underlying: str
    strike: int
    option_type: str
    expiry: str
    security_id: str
    exchange_segment: str
    ltp: float
    bid: float
    ask: float
    oi: int
    iv: float
    lot_size: int
    trade_symbol: str

    @property
    def mid_price(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2, 2)
        return self.ltp


class OptionChainResolver:

    def __init__(self, access_token: str = "", client_id: str = ""):
        self._token = access_token or os.environ.get("DHAN_ACCESS_TOKEN", "")
        self._client_id = client_id or os.environ.get("DHAN_CLIENT_ID", "")
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "access-token": self._token,
            "client-id": self._client_id,
        })
        self._expiry_cache: dict[str, list[str]] = {}
        self._chain_cache: dict[str, dict] = {}
        self._cache_ts: dict[str, float] = {}

    def update_token(self, token: str) -> None:
        self._token = token
        self._session.headers["access-token"] = token

    def get_expiry_list(self, underlying: str) -> list[str]:
        info = UNDERLYING_MAP.get(underlying.upper())
        if not info:
            return []

        cache_key = underlying.upper()
        if cache_key in self._expiry_cache:
            age = datetime.now().timestamp() - self._cache_ts.get(f"exp_{cache_key}", 0)
            if age < 3600:
                return self._expiry_cache[cache_key]

        sec_id, seg = info
        try:
            resp = self._session.post(EXPIRY_URL, json={
                "UnderlyingScrip": int(sec_id),
                "UnderlyingSeg": seg,
            }, timeout=10)
            if resp.status_code != 200:
                log.warning("Expiry list fetch failed for %s: %d %s",
                            underlying, resp.status_code, resp.text[:200])
                return []
            data = resp.json()
            expiries = sorted(data.get("data", []))
            self._expiry_cache[cache_key] = expiries
            self._cache_ts[f"exp_{cache_key}"] = datetime.now().timestamp()
            return expiries
        except Exception as e:
            log.error("Expiry list error for %s: %s", underlying, e)
            return []

    def get_nearest_expiry(self, underlying: str) -> str:
        expiries = self.get_expiry_list(underlying)
        if not expiries:
            return ""
        today = datetime.now(IST).strftime("%Y-%m-%d")
        for exp in expiries:
            if exp >= today:
                return exp
        return expiries[-1] if expiries else ""

    def get_weekly_expiry(self, underlying: str) -> str:
        return self.get_nearest_expiry(underlying)

    def get_next_expiry(self, underlying: str) -> str:
        expiries = self.get_expiry_list(underlying)
        if not expiries:
            return ""
        today = datetime.now(IST).strftime("%Y-%m-%d")
        future = [e for e in expiries if e >= today]
        if len(future) >= 2:
            return future[1]
        return future[0] if future else ""

    def fetch_chain(self, underlying: str, expiry: str) -> dict:
        info = UNDERLYING_MAP.get(underlying.upper())
        if not info:
            return {}

        cache_key = f"{underlying}_{expiry}"
        if cache_key in self._chain_cache:
            age = datetime.now().timestamp() - self._cache_ts.get(f"chain_{cache_key}", 0)
            if age < 30:
                return self._chain_cache[cache_key]

        sec_id, seg = info
        try:
            resp = self._session.post(CHAIN_URL, json={
                "UnderlyingScrip": int(sec_id),
                "UnderlyingSeg": seg,
                "Expiry": expiry,
            }, timeout=10)
            if resp.status_code != 200:
                log.warning("Option chain fetch failed for %s %s: %d",
                            underlying, expiry, resp.status_code)
                return {}
            data = resp.json()
            self._chain_cache[cache_key] = data
            self._cache_ts[f"chain_{cache_key}"] = datetime.now().timestamp()
            return data
        except Exception as e:
            log.error("Option chain error for %s: %s", underlying, e)
            return {}

    def resolve_atm_option(
        self,
        underlying: str,
        underlying_price: float,
        option_type: str = "CE",
        expiry: str = "",
        lot_size: int = 75,
        strike_step: int = 50,
    ) -> OptionContract | None:
        if not expiry:
            expiry = self.get_nearest_expiry(underlying)
        if not expiry:
            log.warning("No expiry found for %s", underlying)
            return None

        atm_strike = round(underlying_price / strike_step) * strike_step

        chain_data = self.fetch_chain(underlying, expiry)
        if not chain_data:
            return self._fallback_contract(
                underlying, atm_strike, option_type, expiry, lot_size)

        inner = chain_data.get("data", chain_data)
        underlying_ltp = inner.get("last_price", underlying_price)
        oc = inner.get("oc", {})
        if not oc:
            return self._fallback_contract(
                underlying, atm_strike, option_type, expiry, lot_size)

        best = None
        best_dist = float("inf")
        exch_seg = EXCHANGE_FNO.get(underlying.upper(), "NSE_FNO")

        for strike_str, sides in oc.items():
            try:
                strike_val = float(strike_str)
            except (ValueError, TypeError):
                continue
            dist = abs(strike_val - underlying_ltp)

            opt_key = "ce" if option_type == "CE" else "pe"
            opt_data = sides.get(opt_key, {})
            if not opt_data:
                continue

            ltp = opt_data.get("last_price", 0)
            if ltp <= 0:
                continue

            if dist < best_dist:
                best_dist = dist
                trade_sym = f"{underlying}{int(strike_val)}{option_type}"
                best = OptionContract(
                    underlying=underlying,
                    strike=int(strike_val),
                    option_type=option_type,
                    expiry=expiry,
                    security_id=str(opt_data.get("security_id", "")),
                    exchange_segment=exch_seg,
                    ltp=float(ltp),
                    bid=float(opt_data.get("top_bid_price", 0)),
                    ask=float(opt_data.get("top_ask_price", 0)),
                    oi=int(opt_data.get("oi", 0)),
                    iv=float(opt_data.get("implied_volatility", 0)),
                    lot_size=lot_size,
                    trade_symbol=trade_sym,
                )

        if not best:
            return self._fallback_contract(
                underlying, atm_strike, option_type, expiry, lot_size)

        log.info("Resolved: %s → strike=%d, LTP=₹%.2f, IV=%.1f%%, OI=%s, sid=%s",
                 best.trade_symbol, best.strike, best.ltp,
                 best.iv, f"{best.oi:,}", best.security_id)
        return best

    def resolve_otm_option(
        self,
        underlying: str,
        underlying_price: float,
        option_type: str = "CE",
        otm_steps: int = 2,
        expiry: str = "",
        lot_size: int = 75,
        strike_step: int = 50,
    ) -> OptionContract | None:
        if not expiry:
            expiry = self.get_nearest_expiry(underlying)
        if not expiry:
            return None

        atm_strike = round(underlying_price / strike_step) * strike_step
        if option_type == "CE":
            target_strike = atm_strike + (otm_steps * strike_step)
        else:
            target_strike = atm_strike - (otm_steps * strike_step)

        chain_data = self.fetch_chain(underlying, expiry)
        if not chain_data:
            return self._fallback_contract(
                underlying, target_strike, option_type, expiry, lot_size)

        inner = chain_data.get("data", chain_data)
        oc = inner.get("oc", {})
        if not oc:
            return self._fallback_contract(
                underlying, target_strike, option_type, expiry, lot_size)

        exch_seg = EXCHANGE_FNO.get(underlying.upper(), "NSE_FNO")
        opt_key = "ce" if option_type == "CE" else "pe"

        best = None
        best_dist = float("inf")
        for strike_str, sides in oc.items():
            try:
                strike_val = float(strike_str)
            except (ValueError, TypeError):
                continue
            dist = abs(strike_val - target_strike)
            opt_data = sides.get(opt_key, {})
            if not opt_data:
                continue
            ltp = opt_data.get("last_price", 0)
            if ltp <= 0:
                continue
            if dist < best_dist:
                best_dist = dist
                trade_sym = f"{underlying}{int(strike_val)}{option_type}"
                best = OptionContract(
                    underlying=underlying,
                    strike=int(strike_val),
                    option_type=option_type,
                    expiry=expiry,
                    security_id=str(opt_data.get("security_id", "")),
                    exchange_segment=exch_seg,
                    ltp=float(ltp),
                    bid=float(opt_data.get("top_bid_price", 0)),
                    ask=float(opt_data.get("top_ask_price", 0)),
                    oi=int(opt_data.get("oi", 0)),
                    iv=float(opt_data.get("implied_volatility", 0)),
                    lot_size=lot_size,
                    trade_symbol=trade_sym,
                )

        if not best:
            return self._fallback_contract(
                underlying, target_strike, option_type, expiry, lot_size)

        log.info("OTM resolved: %s → strike=%d (ATM=%d, %d steps OTM), LTP=₹%.2f, OI=%s",
                 best.trade_symbol, best.strike, atm_strike, otm_steps,
                 best.ltp, f"{best.oi:,}")
        return best

    def resolve_spread_pair(
        self,
        underlying: str,
        underlying_price: float,
        option_type: str = "CE",
        sell_otm_steps: int = 2,
        hedge_otm_steps: int = 6,
        expiry: str = "",
        lot_size: int = 75,
        strike_step: int = 50,
    ) -> tuple[OptionContract, OptionContract] | None:
        if not expiry:
            expiry = self.get_nearest_expiry(underlying)
        if not expiry:
            return None

        sell_leg = self.resolve_otm_option(
            underlying, underlying_price, option_type,
            otm_steps=sell_otm_steps, expiry=expiry,
            lot_size=lot_size, strike_step=strike_step)
        hedge_leg = self.resolve_otm_option(
            underlying, underlying_price, option_type,
            otm_steps=hedge_otm_steps, expiry=expiry,
            lot_size=lot_size, strike_step=strike_step)

        if not sell_leg or not hedge_leg:
            log.warning("Spread pair resolution failed for %s %s", underlying, option_type)
            return None

        if sell_leg.bid <= 0 and sell_leg.ltp <= 0:
            log.warning("Sell leg %s has no bid — spread not viable", sell_leg.trade_symbol)
            return None
        if sell_leg.oi < 100:
            log.warning("Sell leg %s OI=%d too low — spread not viable", sell_leg.trade_symbol, sell_leg.oi)
            return None

        net_credit = sell_leg.mid_price - hedge_leg.mid_price
        if net_credit <= 0:
            log.warning("Spread %s/%s has no net credit (%.2f - %.2f = %.2f)",
                        sell_leg.trade_symbol, hedge_leg.trade_symbol,
                        sell_leg.mid_price, hedge_leg.mid_price, net_credit)
            return None

        spread_width = abs(sell_leg.strike - hedge_leg.strike)
        max_loss = spread_width - net_credit
        log.info("SPREAD PAIR: SELL %s @ ₹%.2f / BUY %s @ ₹%.2f | credit=₹%.2f, max_loss=₹%.2f, width=%d",
                 sell_leg.trade_symbol, sell_leg.mid_price,
                 hedge_leg.trade_symbol, hedge_leg.mid_price,
                 net_credit, max_loss, spread_width)
        return (sell_leg, hedge_leg)

    def _fallback_contract(
        self,
        underlying: str,
        strike: int,
        option_type: str,
        expiry: str,
        lot_size: int,
    ) -> OptionContract:
        exch_seg = EXCHANGE_FNO.get(underlying.upper(), "NSE_FNO")
        trade_sym = f"{underlying}{strike}{option_type}"
        est_premium = max(strike * 0.008, 50.0)
        log.warning("Using estimated premium ₹%.2f for %s (chain unavailable)",
                    est_premium, trade_sym)
        return OptionContract(
            underlying=underlying,
            strike=strike,
            option_type=option_type,
            expiry=expiry,
            security_id=trade_sym,
            exchange_segment=exch_seg,
            ltp=est_premium,
            bid=0.0, ask=0.0, oi=0, iv=0.0,
            lot_size=lot_size,
            trade_symbol=trade_sym,
        )
