from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

EXPIRY_URL = "https://api.dhan.co/v2/optionchain/expirylist"
CHAIN_URL = "https://api.dhan.co/v2/optionchain"
LTP_URL = "https://api.dhan.co/v2/marketfeed/ltp"

CHAIN_CACHE_TTL = 300
EXPIRY_CACHE_TTL = 3600
SID_CACHE_TTL = 86400
MAX_RETRIES = 3
BASE_BACKOFF = 2.0

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

    def __init__(self, access_token: str = "", client_id: str = "",
                 instrument_master=None):
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
        self._sid_cache: dict[str, str] = {}
        self._instrument_master = instrument_master
        self._consecutive_429s: int = 0
        self._last_429_time: float = 0.0
        self._chain_hits: int = 0
        self._chain_misses: int = 0

    def update_token(self, token: str) -> None:
        self._token = token
        self._session.headers["access-token"] = token

    def resolve_lot_size(self, underlying: str, fallback: int = 65) -> int:
        """Get lot size from instrument master (daily Dhan CSV), falling back to config."""
        if not self._instrument_master:
            return fallback
        try:
            results = self._instrument_master.search(underlying, option_type="CE")
            if results:
                lot = results[0].lot_size
                if lot > 0:
                    return lot
        except Exception as e:
            log.debug("Instrument master lot_size lookup failed for %s: %s", underlying, e)
        return fallback

    def cache_stats(self) -> dict:
        return {
            "chain_hits": self._chain_hits,
            "chain_misses": self._chain_misses,
            "cached_chains": len(self._chain_cache),
            "cached_sids": len(self._sid_cache),
            "consecutive_429s": self._consecutive_429s,
        }

    def _is_rate_limited(self) -> bool:
        if self._consecutive_429s < 3:
            return False
        cooldown = min(60.0, BASE_BACKOFF ** self._consecutive_429s)
        elapsed = time.time() - self._last_429_time
        if elapsed < cooldown:
            log.debug("Rate limit cooldown: %.0fs remaining (429 streak=%d)",
                      cooldown - elapsed, self._consecutive_429s)
            return True
        return False

    def _api_post(self, url: str, payload: dict, label: str) -> dict | None:
        if self._is_rate_limited():
            log.warning("API THROTTLED: skipping %s (429 streak=%d)",
                        label, self._consecutive_429s)
            return None

        for attempt in range(MAX_RETRIES):
            try:
                resp = self._session.post(url, json=payload, timeout=10)

                if resp.status_code == 200:
                    self._consecutive_429s = max(0, self._consecutive_429s - 1)
                    return resp.json()

                if resp.status_code == 429:
                    self._consecutive_429s += 1
                    self._last_429_time = time.time()
                    wait = min(30.0, BASE_BACKOFF * (2 ** attempt))
                    log.warning("429 RATE LIMITED on %s (streak=%d) — waiting %.1fs before retry %d/%d",
                                label, self._consecutive_429s, wait, attempt + 1, MAX_RETRIES)
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(wait)
                    continue

                log.warning("%s failed: HTTP %d %s", label, resp.status_code, resp.text[:200])
                return None

            except requests.Timeout:
                log.warning("%s timeout (attempt %d/%d)", label, attempt + 1, MAX_RETRIES)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1.0)
            except Exception as e:
                log.error("%s error: %s", label, e)
                return None

        log.warning("%s failed after %d retries", label, MAX_RETRIES)
        return None

    def get_expiry_list(self, underlying: str) -> list[str]:
        info = UNDERLYING_MAP.get(underlying.upper())
        if not info:
            return []

        cache_key = underlying.upper()
        if cache_key in self._expiry_cache:
            age = time.time() - self._cache_ts.get(f"exp_{cache_key}", 0)
            if age < EXPIRY_CACHE_TTL:
                return self._expiry_cache[cache_key]

        sec_id, seg = info
        data = self._api_post(EXPIRY_URL, {
            "UnderlyingScrip": int(sec_id),
            "UnderlyingSeg": seg,
        }, f"expiry_list({underlying})")

        if not data:
            if cache_key in self._expiry_cache:
                log.info("Using stale expiry cache for %s", underlying)
                return self._expiry_cache[cache_key]
            return []

        expiries = sorted(data.get("data", []))
        self._expiry_cache[cache_key] = expiries
        self._cache_ts[f"exp_{cache_key}"] = time.time()
        return expiries

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
        now = time.time()
        if cache_key in self._chain_cache:
            age = now - self._cache_ts.get(f"chain_{cache_key}", 0)
            if age < CHAIN_CACHE_TTL:
                self._chain_hits += 1
                return self._chain_cache[cache_key]

        self._chain_misses += 1
        sec_id, seg = info
        data = self._api_post(CHAIN_URL, {
            "UnderlyingScrip": int(sec_id),
            "UnderlyingSeg": seg,
            "Expiry": expiry,
        }, f"chain({underlying},{expiry})")

        if data:
            self._chain_cache[cache_key] = data
            self._cache_ts[f"chain_{cache_key}"] = now
            self._index_security_ids(underlying, expiry, data)
            return data

        if cache_key in self._chain_cache:
            log.info("Using stale chain cache for %s %s (age=%.0fs)",
                     underlying, expiry,
                     now - self._cache_ts.get(f"chain_{cache_key}", 0))
            return self._chain_cache[cache_key]

        return {}

    def _index_security_ids(self, underlying: str, expiry: str, chain_data: dict) -> None:
        inner = chain_data.get("data", chain_data)
        oc = inner.get("oc", {})
        for strike_str, sides in oc.items():
            try:
                strike_val = int(float(strike_str))
            except (ValueError, TypeError):
                continue
            for opt_key, opt_type in [("ce", "CE"), ("pe", "PE")]:
                opt_data = sides.get(opt_key, {})
                if not opt_data:
                    continue
                sid = opt_data.get("security_id")
                if sid and str(sid).isdigit():
                    cache_key = f"{underlying}|{expiry}|{strike_val}|{opt_type}"
                    self._sid_cache[cache_key] = str(sid)

    def _lookup_cached_sid(self, underlying: str, expiry: str,
                           strike: int, option_type: str) -> str:
        cache_key = f"{underlying}|{expiry}|{strike}|{option_type}"
        return self._sid_cache.get(cache_key, "")

    def resolve_atm_option(
        self,
        underlying: str,
        underlying_price: float,
        option_type: str = "CE",
        expiry: str = "",
        lot_size: int = 65,
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

    def resolve_delta_option(
        self,
        underlying: str,
        underlying_price: float,
        option_type: str = "CE",
        target_delta: float = 0.55,
        delta_band: tuple[float, float] = (0.45, 0.65),
        expiry: str = "",
        lot_size: int = 65,
        strike_step: int = 50,
    ) -> OptionContract | None:
        """Pick the strike closest to target_delta within the delta band.

        Delta approximation (no Black-Scholes dependency):
          CE delta ≈ N(d1) where d1 = ln(S/K) / (σ√T) + 0.5σ√T
          Simplified: moneyness_pct / (IV * sqrt(DTE/365))
          ATM ≈ 0.50, 1-step ITM ≈ 0.55-0.60, 1-step OTM ≈ 0.40-0.45
        """
        if not expiry:
            expiry = self.get_nearest_expiry(underlying)
        if not expiry:
            return self.resolve_atm_option(
                underlying, underlying_price, option_type, expiry, lot_size, strike_step)

        chain_data = self.fetch_chain(underlying, expiry)
        if not chain_data:
            return self.resolve_atm_option(
                underlying, underlying_price, option_type, expiry, lot_size, strike_step)

        inner = chain_data.get("data", chain_data)
        underlying_ltp = inner.get("last_price", underlying_price)
        oc = inner.get("oc", {})
        if not oc:
            return self.resolve_atm_option(
                underlying, underlying_price, option_type, expiry, lot_size, strike_step)

        # Estimate DTE
        import math
        try:
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            dte = max(0.01, (exp_date - datetime.now(IST).date()).days)
        except (ValueError, TypeError):
            dte = 1

        exch_seg = EXCHANGE_FNO.get(underlying.upper(), "NSE_FNO")
        opt_key = "ce" if option_type == "CE" else "pe"
        candidates = []

        for strike_str, sides in oc.items():
            try:
                strike_val = float(strike_str)
            except (ValueError, TypeError):
                continue

            opt_data = sides.get(opt_key, {})
            if not opt_data:
                continue

            ltp = opt_data.get("last_price", 0)
            if ltp <= 0:
                continue
            oi = int(opt_data.get("oi", 0))
            if oi <= 0:
                continue

            iv = float(opt_data.get("implied_volatility", 0)) / 100
            if iv <= 0:
                iv = 0.20  # fallback 20% IV

            # Approximate delta
            moneyness = (underlying_ltp - strike_val) / underlying_ltp
            if option_type == "PE":
                moneyness = -moneyness
            sigma_sqrt_t = iv * math.sqrt(dte / 365)
            if sigma_sqrt_t > 0:
                d1_approx = moneyness / sigma_sqrt_t + 0.5 * sigma_sqrt_t
                # Approximate N(d1) with logistic sigmoid
                delta_est = 1.0 / (1.0 + math.exp(-1.7 * d1_approx))
            else:
                delta_est = 0.50

            if delta_band[0] <= delta_est <= delta_band[1]:
                trade_sym = f"{underlying}{int(strike_val)}{option_type}"
                contract = OptionContract(
                    underlying=underlying,
                    strike=int(strike_val),
                    option_type=option_type,
                    expiry=expiry,
                    security_id=str(opt_data.get("security_id", "")),
                    exchange_segment=exch_seg,
                    ltp=float(ltp),
                    bid=float(opt_data.get("top_bid_price", 0)),
                    ask=float(opt_data.get("top_ask_price", 0)),
                    oi=oi,
                    iv=float(opt_data.get("implied_volatility", 0)),
                    lot_size=lot_size,
                    trade_symbol=trade_sym,
                )
                candidates.append((abs(delta_est - target_delta), delta_est, contract))

        if not candidates:
            log.info("DELTA SELECT: no strikes in %.2f-%.2f band for %s %s, falling back to ATM",
                     delta_band[0], delta_band[1], underlying, option_type)
            return self.resolve_atm_option(
                underlying, underlying_price, option_type, expiry, lot_size, strike_step)

        candidates.sort(key=lambda x: x[0])
        _, best_delta, best = candidates[0]
        log.info("DELTA SELECT: %s → strike=%d, delta≈%.2f (target=%.2f), LTP=₹%.2f, IV=%.1f%%, OI=%s",
                 best.trade_symbol, best.strike, best_delta, target_delta,
                 best.ltp, best.iv, f"{best.oi:,}")
        return best

    def resolve_otm_option(
        self,
        underlying: str,
        underlying_price: float,
        option_type: str = "CE",
        otm_steps: int = 2,
        expiry: str = "",
        lot_size: int = 65,
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
        lot_size: int = 65,
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

        sid = self._lookup_cached_sid(underlying, expiry, strike, option_type)
        if sid:
            log.info("Fallback using cached security_id=%s for %s", sid, trade_sym)
        else:
            sid = ""
            log.warning("No cached security_id for %s — trade will be blocked", trade_sym)

        est_premium = max(strike * 0.008, 50.0)
        log.warning("Using estimated premium ₹%.2f for %s (chain unavailable)",
                    est_premium, trade_sym)
        return OptionContract(
            underlying=underlying,
            strike=strike,
            option_type=option_type,
            expiry=expiry,
            security_id=sid,
            exchange_segment=exch_seg,
            ltp=est_premium,
            bid=0.0, ask=0.0, oi=0, iv=0.0,
            lot_size=lot_size,
            trade_symbol=trade_sym,
        )
