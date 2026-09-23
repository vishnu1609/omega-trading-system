"""
Live Signal Generator — Replaces TradingView webhooks.

Fetches intraday candles (Yahoo Finance free, Dhan paid fallback),
computes the 4-factor Omega confluence (SuperTrend + VWAP + ADX +
Chandelier Exit), and emits BUY/SELL/EXIT signals into the signal queue.

Runs in its own thread alongside the main loop.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event, Lock
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

from core.signal_scorer import (
    ScoredSignal, score_signal, select_best_signals, format_score_report,
    _get_correlation_group,
)

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DHAN_CHART_URL = "https://api.dhan.co/v2/charts/intraday"

DHAN_INDEX_MAP = {
    "NIFTY": ("13", "IDX_I", "INDEX"),
    "BANKNIFTY": ("25", "IDX_I", "INDEX"),
    "FINNIFTY": ("27", "IDX_I", "INDEX"),
    "SENSEX": ("51", "IDX_I", "INDEX"),
    "BANKEX": ("12", "IDX_I", "INDEX"),
}

YAHOO_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
}

_get_corr_group = _get_correlation_group


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ── Indicators (streaming-friendly, operate on list of Candle) ───────────

def _ema(values: list[float], period: int) -> list[float]:
    result = [0.0] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def calc_atr(candles: list[Candle], period: int) -> list[float]:
    trs: list[float] = [0.0]
    for i in range(1, len(candles)):
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        trs.append(tr)
    return _ema(trs, period)


def calc_supertrend(candles: list[Candle], period: int = 10,
                    multiplier: float = 3.0) -> tuple[list[float], list[int]]:
    atr_vals = calc_atr(candles, period)
    n = len(candles)
    upper = [0.0] * n
    lower = [0.0] * n
    st_val = [0.0] * n
    st_dir = [0] * n  # -1 = bullish, +1 = bearish (Pine convention)

    for i in range(n):
        hl2 = (candles[i].high + candles[i].low) / 2
        upper[i] = hl2 + multiplier * atr_vals[i]
        lower[i] = hl2 - multiplier * atr_vals[i]

    for i in range(1, n):
        if lower[i] < lower[i - 1] and candles[i - 1].close > lower[i - 1]:
            lower[i] = lower[i - 1]
        if upper[i] > upper[i - 1] and candles[i - 1].close < upper[i - 1]:
            upper[i] = upper[i - 1]

        if st_dir[i - 1] <= 0:
            if candles[i].close < lower[i]:
                st_dir[i] = 1
                st_val[i] = upper[i]
            else:
                st_dir[i] = -1
                st_val[i] = lower[i]
        else:
            if candles[i].close > upper[i]:
                st_dir[i] = -1
                st_val[i] = lower[i]
            else:
                st_dir[i] = 1
                st_val[i] = upper[i]

    return st_val, st_dir


def calc_adx(candles: list[Candle], period: int = 14) -> list[float]:
    n = len(candles)
    if n < period + 1:
        return [0.0] * n

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr_list = [0.0] * n

    for i in range(1, n):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm[i] = up if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0
        tr_list[i] = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )

    smooth_tr = _ema(tr_list, period)
    smooth_pdm = _ema(plus_dm, period)
    smooth_mdm = _ema(minus_dm, period)

    dx = [0.0] * n
    for i in range(n):
        if smooth_tr[i] == 0:
            continue
        pdi = smooth_pdm[i] / smooth_tr[i] * 100
        mdi = smooth_mdm[i] / smooth_tr[i] * 100
        denom = pdi + mdi
        if denom > 0:
            dx[i] = abs(pdi - mdi) / denom * 100

    return _ema(dx, period)


def calc_vwap(candles: list[Candle]) -> list[float]:
    result = [0.0] * len(candles)
    cum_vol = 0.0
    cum_tp_vol = 0.0
    current_date = None

    for i, c in enumerate(candles):
        day = c.timestamp.date()
        if day != current_date:
            cum_vol = 0.0
            cum_tp_vol = 0.0
            current_date = day

        tp = (c.high + c.low + c.close) / 3
        vol = c.volume if c.volume > 0 else 1
        cum_vol += vol
        cum_tp_vol += tp * vol
        result[i] = cum_tp_vol / cum_vol if cum_vol > 0 else tp

    return result


def calc_chandelier(candles: list[Candle], period: int = 22,
                    multiplier: float = 3.0) -> tuple[list[float], list[float]]:
    atr_vals = calc_atr(candles, period)
    n = len(candles)
    long_stop = [0.0] * n
    short_stop = [0.0] * n

    for i in range(period, n):
        highest = max(c.high for c in candles[i - period + 1:i + 1])
        lowest = min(c.low for c in candles[i - period + 1:i + 1])
        long_stop[i] = highest - multiplier * atr_vals[i]
        short_stop[i] = lowest + multiplier * atr_vals[i]

        if i > period and long_stop[i - 1] > 0:
            long_stop[i] = max(long_stop[i], long_stop[i - 1])
        if i > period and short_stop[i - 1] > 0:
            short_stop[i] = min(short_stop[i], short_stop[i - 1])

    return long_stop, short_stop


# ── Signal Generator ─────────────────────────────────────────────────────

class SignalGenerator:
    """Fetches candles (Yahoo Finance / Dhan) and generates Omega confluence signals."""

    def __init__(
        self,
        instruments: dict,
        access_token: str = "",
        poll_interval: int = 60,
        st_period: int = 10,
        st_mult: float = 3.0,
        adx_len: int = 14,
        adx_threshold: int = 25,
        chand_len: int = 22,
        chand_mult: float = 3.0,
        signal_cooldown_sec: int = 600,
    ):
        self.instruments = instruments
        self.access_token = access_token or os.environ.get("DHAN_ACCESS_TOKEN", "")
        self.poll_interval = poll_interval
        self.signal_cooldown_sec = signal_cooldown_sec
        self.st_period = st_period
        self.st_mult = st_mult
        self.adx_len = adx_len
        self.adx_threshold = adx_threshold
        self.chand_len = chand_len
        self.chand_mult = chand_mult

        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "access-token": self.access_token,
        })

        self._last_signal: dict[str, str] = {}
        self._signal_time: dict[str, float] = {}
        self._trade_dir: dict[str, int] = {}
        self._scan_count: int = 0
        self._cycle_lock = Lock()
        self.guardrails = None  # set externally via set_guardrails()
        self.calendar = None    # set externally via set_calendar()

    def set_guardrails(self, guardrails) -> None:
        from core.guardrails import Guardrails
        self.guardrails: Guardrails = guardrails

    def set_calendar(self, calendar) -> None:
        from utils.market_calendar import MarketCalendar
        self.calendar: MarketCalendar = calendar

    def fetch_candles(self, inst_name: str, security_id: str = "",
                      exchange_segment: str = "",
                      instrument_type: str = "INDEX") -> list[Candle]:
        dhan_info = DHAN_INDEX_MAP.get(inst_name.upper())
        if not security_id and dhan_info:
            security_id, exchange_segment, instrument_type = dhan_info

        if security_id and self.access_token:
            raw = self._fetch_dhan(security_id, exchange_segment, instrument_type)
            if raw:
                raw = self._filter_ghost_candles(raw, inst_name)
                raw = self._sanitize_outliers(raw, inst_name)
                return raw[-500:] if len(raw) > 500 else raw
            log.warning("Dhan fetch empty for %s — falling back to Yahoo", inst_name)

        candles = self._fetch_yahoo(inst_name)
        if candles:
            candles = self._filter_ghost_candles(candles, inst_name)
            candles = self._sanitize_outliers(candles, inst_name)
            return candles[-500:] if len(candles) > 500 else candles

        return []

    @staticmethod
    def _filter_ghost_candles(candles: list[Candle], symbol: str) -> list[Candle]:
        if not candles:
            return candles
        cleaned: list[Candle] = []
        ffill_count = 0
        for c in candles:
            is_ghost = (c.open == c.high == c.low == c.close and c.open > 0)
            if is_ghost and cleaned:
                prev = cleaned[-1]
                cleaned.append(Candle(
                    timestamp=c.timestamp,
                    open=prev.close, high=prev.close,
                    low=prev.close, close=prev.close,
                    volume=prev.volume * 0.1,
                ))
                ffill_count += 1
            else:
                cleaned.append(c)
        if ffill_count:
            log.warning("Forward-filled %d ghost candles for %s (flat OHLC)",
                        ffill_count, symbol)
        return cleaned

    @staticmethod
    def _sanitize_outliers(candles: list[Candle], symbol: str,
                           max_deviation_pct: float = 5.0) -> list[Candle]:
        if len(candles) < 3:
            return candles
        cleaned: list[Candle] = [candles[0]]
        repairs = 0
        for j in range(1, len(candles)):
            c = candles[j]
            prev = cleaned[-1]
            if prev.close <= 0:
                cleaned.append(c)
                continue
            high_dev = abs(c.high - prev.close) / prev.close * 100
            low_dev = abs(c.low - prev.close) / prev.close * 100
            close_dev = abs(c.close - prev.close) / prev.close * 100
            if high_dev > max_deviation_pct or low_dev > max_deviation_pct or close_dev > max_deviation_pct:
                cleaned.append(Candle(
                    timestamp=c.timestamp,
                    open=prev.close, high=prev.close,
                    low=prev.close, close=prev.close,
                    volume=c.volume,
                ))
                repairs += 1
            else:
                cleaned.append(c)
        if repairs:
            log.warning("DIRTY DATA: Repaired %d outlier candles for %s "
                        "(>%.0f%% deviation → forward-filled)", repairs, symbol,
                        max_deviation_pct)
        return cleaned

    @staticmethod
    def _is_freak_tick(candles: list[Candle], threshold_pct: float = 5.0) -> bool:
        if len(candles) < 3:
            return False
        curr = candles[-1]
        prev = candles[-2]
        if prev.close <= 0:
            return False
        pct_change = abs(curr.close - prev.close) / prev.close * 100
        if pct_change > threshold_pct:
            intra_range = (curr.high - curr.low) / curr.close * 100 if curr.close > 0 else 0
            if intra_range > threshold_pct * 1.5:
                log.warning("FREAK TICK detected: %s moved %.1f%% in one candle "
                            "(%.2f→%.2f, range=%.1f%%)",
                            "?", pct_change, prev.close, curr.close, intra_range)
                return True
        return False

    def _fetch_yahoo(self, inst_name: str) -> list[Candle]:
        yahoo_sym = YAHOO_SYMBOLS.get(inst_name.upper())
        if not yahoo_sym:
            return []

        try:
            df = yf.download(yahoo_sym, period="5d", interval="5m",
                             progress=False, timeout=10)
            if df.empty:
                return []

            if hasattr(df.columns, 'droplevel'):
                df.columns = df.columns.droplevel(1)

            candles = []
            for ts, row in df.iterrows():
                candles.append(Candle(
                    timestamp=ts.to_pydatetime(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0)),
                ))
            return candles

        except Exception as exc:
            log.warning("Yahoo fetch failed for %s: %s", inst_name, exc)
            return []

    def _fetch_dhan(self, security_id: str, exchange_segment: str,
                    instrument_type: str) -> list[Candle]:
        today = datetime.now(IST)
        from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        payload = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument_type,
            "fromDate": from_date,
            "toDate": today.strftime("%Y-%m-%d"),
        }

        try:
            resp = self._session.post(DHAN_CHART_URL, json=payload, timeout=10)
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception as exc:
            log.error("Dhan candle fetch failed: %s", exc)
            return []

        timestamps = data.get("timestamp", [])
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        volumes = data.get("volume", [])

        candles = []
        for i in range(len(timestamps)):
            candles.append(Candle(
                timestamp=datetime.fromtimestamp(timestamps[i], tz=IST),
                open=opens[i],
                high=highs[i],
                low=lows[i],
                close=closes[i],
                volume=volumes[i] if i < len(volumes) else 0,
            ))

        return candles

    def generate_signals(self, inst_name: str, candles: list[Candle],
                         asset: str = "options") -> dict | None:
        if len(candles) < max(self.st_period, self.adx_len, self.chand_len) + 5:
            return None

        if self._is_freak_tick(candles):
            log.warning("Freak tick on %s — skipping signal generation", inst_name)
            return None

        if self.guardrails:
            freshness = self.guardrails.check_candle_freshness(
                candles[-1].timestamp, inst_name)
            if not freshness:
                return None

        st_val, st_dir = calc_supertrend(candles, self.st_period, self.st_mult)
        adx_vals = calc_adx(candles, self.adx_len)
        vwap_vals = calc_vwap(candles)
        chand_long, chand_short = calc_chandelier(
            candles, self.chand_len, self.chand_mult)

        i = len(candles) - 1
        prev = i - 1

        close = candles[i].close

        if self.guardrails:
            gr = self.guardrails.pre_signal_check(
                inst_name, adx_value=adx_vals[i],
                candle_time=candles[i].timestamp)
            if not gr:
                return None

        st_bullish = st_dir[i] < 0
        st_bearish = st_dir[i] > 0
        adx_ok = adx_vals[i] > self.adx_threshold

        bull_entry = st_bullish and close > vwap_vals[i] and adx_ok
        bear_entry = st_bearish and close < vwap_vals[i] and adx_ok

        prev_bull = (st_dir[prev] < 0 and candles[prev].close > vwap_vals[prev]
                     and adx_vals[prev] > self.adx_threshold) if prev >= 0 else False
        prev_bear = (st_dir[prev] > 0 and candles[prev].close < vwap_vals[prev]
                     and adx_vals[prev] > self.adx_threshold) if prev >= 0 else False

        first_scan = inst_name not in self._last_signal
        buy_signal = bull_entry and (not prev_bull or first_scan)
        sell_signal = bear_entry and (not prev_bear or first_scan)

        trade_dir = self._trade_dir.get(inst_name, 0)

        if buy_signal:
            trade_dir = 1
        if sell_signal:
            trade_dir = -1

        trail_exit_long = trade_dir == 1 and close < chand_long[i] and chand_long[i] > 0
        trail_exit_short = trade_dir == -1 and close > chand_short[i] and chand_short[i] > 0

        if trail_exit_long or trail_exit_short:
            trade_dir = 0

        self._trade_dir[inst_name] = trade_dir

        strategy = "omega_futures" if asset == "futures" else "omega_complete"
        last = self._last_signal.get(inst_name, "")

        atr_vals = calc_atr(candles, self.st_period)
        avg_vol = sum(c.volume for c in candles[-20:]) / 20 if len(candles) >= 20 else 1

        def _build_signal(side: str, sl: float, chand_stop: float) -> dict:
            payload = {
                "strategy": strategy,
                "symbol": inst_name,
                "side": side,
                "sl": sl,
                "close": close,
                "asset": asset,
                "strike": 0,
            }
            scored = score_signal(
                symbol=inst_name, side=side, strategy=strategy, asset=asset,
                raw_signal=payload,
                adx_value=adx_vals[i], adx_threshold=self.adx_threshold,
                close_price=close, vwap_price=vwap_vals[i],
                st_value=st_val[i], chandelier_stop=chand_stop,
                avg_volume=avg_vol, current_volume=candles[i].volume,
                atr_value=atr_vals[i],
            )
            payload["score"] = round(scored.total_score, 1)
            payload["confidence"] = scored.confidence
            payload["score_details"] = {
                "adx": round(scored.adx_score, 1),
                "vwap": round(scored.vwap_score, 1),
                "rr": round(scored.rr_score, 1),
                "st": round(scored.st_score, 1),
                "vol": round(scored.volume_score, 1),
            }
            log.info("SIGNAL: %s %s — %s",
                     side, inst_name, format_score_report(scored))
            return payload

        now_ts = time.time()
        last_sig_ts = self._signal_time.get(inst_name, 0)
        in_cooldown = (now_ts - last_sig_ts) < self.signal_cooldown_sec

        if buy_signal and last != "BUY" and not in_cooldown:
            self._last_signal[inst_name] = "BUY"
            self._signal_time[inst_name] = now_ts
            side = "BUY" if asset == "futures" else "BUY_CE"
            sl = round(chand_long[i], 2)
            return _build_signal(side, sl, chand_long[i])

        if sell_signal and last != "SELL" and not in_cooldown:
            self._last_signal[inst_name] = "SELL"
            self._signal_time[inst_name] = now_ts
            side = "SELL" if asset == "futures" else "BUY_PE"
            sl = round(chand_short[i], 2)
            return _build_signal(side, sl, chand_short[i])

        if (trail_exit_long or trail_exit_short) and last != "EXIT":
            self._last_signal[inst_name] = "EXIT"
            self._signal_time[inst_name] = now_ts
            log.info("SIGNAL: EXIT %s — trail stop hit", inst_name)
            return {
                "strategy": strategy,
                "symbol": inst_name,
                "side": "EXIT",
                "sl": 0,
                "asset": asset,
                "strike": 0,
                "score": 100,
                "confidence": "HIGH",
            }

        return None

    def run(self, signal_queue, shutdown_event: Event,
            watch_instruments: list[str] | None = None) -> None:
        """Main polling loop — runs in a thread."""
        log.info("Signal generator started (poll every %ds)", self.poll_interval)

        if not watch_instruments:
            watch_instruments = list(self.instruments.keys())[:3]

        cycle = 0
        _holiday_logged = None
        while not shutdown_event.is_set():
            if self.calendar:
                now = datetime.now(IST)
                if not self.calendar.is_trading_day(now.date()):
                    if _holiday_logged != now.date():
                        status = self.calendar.market_status(now)
                        log.info("Market closed: %s — next trading day: %s",
                                 status["status"], status["next_trading_day"])
                        _holiday_logged = now.date()
                    shutdown_event.wait(timeout=300)
                    continue
                if not self.calendar.is_nse_open(now):
                    if cycle == 0:
                        log.info("Pre/post market — waiting for session open")
                    shutdown_event.wait(timeout=60)
                    continue

            if not self._cycle_lock.acquire(blocking=False):
                log.warning("Scan cycle overlap — previous cycle still running, skipping")
                shutdown_event.wait(timeout=self.poll_interval)
                continue

            try:
                cycle += 1
                self._scan_count = cycle
                cycle_signals: list[dict] = []

                for inst_name in watch_instruments:
                    cfg = self.instruments.get(inst_name, {})
                    asset_class = cfg.get("asset_class", "index_options")
                    asset = "futures" if "futures" in asset_class else "options"

                    try:
                        candles = self.fetch_candles(inst_name)
                        if not candles:
                            log.warning("No candle data for %s — skipping", inst_name)
                            continue

                        signal = self.generate_signals(inst_name, candles, asset)
                        if signal:
                            cycle_signals.append(signal)
                        elif cycle == 1 or cycle % 10 == 0:
                            log.info("Scan #%d %s: %d candles, close=%.2f — no signal",
                                     cycle, inst_name, len(candles), candles[-1].close)

                    except Exception as exc:
                        log.error("Signal gen error for %s: %s", inst_name, exc)

                    time.sleep(0.5)

                if cycle_signals:
                    exits = [s for s in cycle_signals if s["side"] == "EXIT"]
                    entries = [s for s in cycle_signals if s["side"] != "EXIT"]

                    for ex in exits:
                        signal_queue.put(ex)
                        log.info("EXIT queued: %s", ex["symbol"])

                    if entries and self.guardrails:
                        conflict = self.guardrails.check_index_conflict(entries)
                        if not conflict:
                            entries = [s for s in entries
                                       if s["symbol"].upper() not in
                                       {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"}]
                            log.warning("Index entries removed due to conflict — "
                                        "%d non-index entries remain", len(entries))

                    if entries:
                        scored = []
                        for s in entries:
                            sc = ScoredSignal(
                                symbol=s["symbol"], side=s["side"],
                                strategy=s["strategy"], asset=s["asset"],
                                raw_signal=s, total_score=s.get("score", 50),
                                confidence=s.get("confidence", "LOW"),
                                correlation_group=_get_corr_group(s["symbol"]),
                            )
                            scored.append(sc)

                        best = select_best_signals(scored, max_positions=3, min_score=35)

                        for pick in best:
                            signal_queue.put(pick.raw_signal)
                            log.info("QUEUED [%s] %s %s score=%.0f (best of %d)",
                                     pick.confidence, pick.side, pick.symbol,
                                     pick.total_score, len(entries))

            finally:
                self._cycle_lock.release()

            shutdown_event.wait(timeout=self.poll_interval)

        log.info("Signal generator stopped")
