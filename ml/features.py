"""
Feature Engineering for Omega ML Models

Extracts 40+ features from raw OHLCV candles for:
  1. Signal Quality Classifier (will this signal win or lose?)
  2. Regime Detection Model (trending/ranging/choppy/volatile)
  3. Entry Timing Model (is NOW a good time to enter?)

Features are grouped by category:
  - Price action (10 features)
  - Trend indicators (8 features)
  - Momentum indicators (6 features)
  - Volatility indicators (6 features)
  - Volume features (4 features)
  - Time features (6 features)
  - Market microstructure (4 features)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass
class Candle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float


def extract_features(candles: list[Candle], lookback: int = 50) -> dict[str, float]:
    if len(candles) < lookback:
        return {}

    window = candles[-lookback:]
    c = window[-1]
    closes = np.array([x.close for x in window])
    highs = np.array([x.high for x in window])
    lows = np.array([x.low for x in window])
    opens = np.array([x.open for x in window])
    volumes = np.array([x.volume for x in window], dtype=float)

    features: dict[str, float] = {}

    # === PRICE ACTION (10) ===
    features["body_pct"] = (c.close - c.open) / c.open * 100 if c.open > 0 else 0
    features["upper_wick_pct"] = (c.high - max(c.open, c.close)) / c.close * 100 if c.close > 0 else 0
    features["lower_wick_pct"] = (min(c.open, c.close) - c.low) / c.close * 100 if c.close > 0 else 0
    features["range_pct"] = (c.high - c.low) / c.close * 100 if c.close > 0 else 0
    features["close_position"] = (c.close - c.low) / (c.high - c.low) if (c.high - c.low) > 0 else 0.5
    features["gap_pct"] = (c.open - closes[-2]) / closes[-2] * 100 if len(closes) > 1 and closes[-2] > 0 else 0
    features["higher_high"] = 1.0 if c.high > highs[-2] else 0.0
    features["lower_low"] = 1.0 if c.low < lows[-2] else 0.0
    features["close_vs_prev_high"] = (c.close - highs[-2]) / highs[-2] * 100 if highs[-2] > 0 else 0
    features["avg_bar_range_5"] = float(np.mean((highs[-5:] - lows[-5:]) / closes[-5:] * 100))

    # === TREND INDICATORS (8) ===
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    ema50 = _ema(closes, min(50, len(closes)))
    sma200 = float(np.mean(closes[-min(200, len(closes)):]))

    features["ema9"] = ema9
    features["ema21"] = ema21
    features["ema_cross"] = 1.0 if ema9 > ema21 else -1.0
    features["ema_spread_pct"] = (ema9 - ema21) / ema21 * 100 if ema21 > 0 else 0
    features["price_vs_ema50_pct"] = (c.close - ema50) / ema50 * 100 if ema50 > 0 else 0
    features["price_vs_sma200_pct"] = (c.close - sma200) / sma200 * 100 if sma200 > 0 else 0
    features["trend_strength"] = _linear_regression_slope(closes[-20:])
    features["supertrend_dir"] = _supertrend_direction(highs, lows, closes, period=10, mult=3.0)

    # === MOMENTUM INDICATORS (6) ===
    rsi = _rsi(closes, 14)
    features["rsi_14"] = rsi
    features["rsi_zone"] = 1.0 if rsi > 70 else (-1.0 if rsi < 30 else 0.0)

    adx, plus_di, minus_di = _adx(highs, lows, closes, 14)
    features["adx_14"] = adx
    features["di_spread"] = plus_di - minus_di
    features["adx_slope_5"] = _slope_of_last_n(_adx_series(highs, lows, closes, 14), 5)

    macd_line, signal_line = _macd(closes)
    features["macd_histogram"] = macd_line - signal_line

    # === VOLATILITY INDICATORS (6) ===
    atr = _atr(highs, lows, closes, 14)
    features["atr_14"] = atr
    features["atr_pct"] = atr / c.close * 100 if c.close > 0 else 0
    features["atr_ratio_5_20"] = _atr(highs[-5:], lows[-5:], closes[-5:], 5) / atr if atr > 0 else 1.0

    bb_upper, bb_middle, bb_lower = _bollinger(closes, 20, 2.0)
    bb_width = (bb_upper - bb_lower) / bb_middle * 100 if bb_middle > 0 else 0
    features["bb_width"] = bb_width
    features["bb_position"] = (c.close - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5
    features["bb_squeeze"] = 1.0 if bb_width < _bollinger_squeeze_threshold(closes, 20) else 0.0

    # === VOLUME FEATURES (4) ===
    avg_vol = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
    features["volume_ratio"] = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
    features["volume_trend_5"] = _slope_of_last_n(volumes[-5:], 5)
    features["volume_price_corr"] = float(np.corrcoef(closes[-10:], volumes[-10:])[0, 1]) if len(closes) >= 10 and np.std(volumes[-10:]) > 0 else 0
    features["obv_slope"] = _obv_slope(closes[-20:], volumes[-20:])

    # === TIME FEATURES (6) ===
    import datetime as dt
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    ts = dt.datetime.fromtimestamp(c.timestamp, tz=IST) if c.timestamp > 1e9 else dt.datetime.now(IST)
    minutes_from_open = (ts.hour - 9) * 60 + (ts.minute - 15)
    features["minutes_from_open"] = max(0, minutes_from_open)
    features["hour_sin"] = math.sin(2 * math.pi * ts.hour / 24)
    features["hour_cos"] = math.cos(2 * math.pi * ts.hour / 24)
    features["day_of_week"] = float(ts.weekday())
    features["is_first_hour"] = 1.0 if minutes_from_open < 60 else 0.0
    features["is_last_hour"] = 1.0 if minutes_from_open > 300 else 0.0

    # === MARKET MICROSTRUCTURE (4) ===
    returns = np.diff(closes) / closes[:-1]
    features["return_skew"] = float(_skewness(returns[-20:])) if len(returns) >= 20 else 0
    features["return_kurtosis"] = float(_kurtosis(returns[-20:])) if len(returns) >= 20 else 0
    features["hurst_exponent"] = _hurst(closes[-min(100, len(closes)):])
    chop = _choppiness(highs[-14:], lows[-14:], closes[-14:], 14)
    features["choppiness"] = chop

    return features


def features_to_array(features: dict[str, float], feature_names: list[str] | None = None) -> np.ndarray:
    if feature_names is None:
        feature_names = sorted(features.keys())
    return np.array([features.get(name, 0.0) for name in feature_names], dtype=np.float32)


FEATURE_NAMES = [
    "adx_14", "adx_slope_5", "atr_14", "atr_pct", "atr_ratio_5_20",
    "avg_bar_range_5", "bb_position", "bb_squeeze", "bb_width",
    "body_pct", "choppiness", "close_position", "close_vs_prev_high",
    "day_of_week", "di_spread", "ema9", "ema21", "ema_cross",
    "ema_spread_pct", "gap_pct", "higher_high", "hour_cos", "hour_sin",
    "hurst_exponent", "is_first_hour", "is_last_hour", "lower_low",
    "lower_wick_pct", "macd_histogram", "minutes_from_open", "obv_slope",
    "price_vs_ema50_pct", "price_vs_sma200_pct", "range_pct",
    "return_kurtosis", "return_skew", "rsi_14", "rsi_zone",
    "supertrend_dir", "trend_strength", "upper_wick_pct",
    "volume_price_corr", "volume_ratio", "volume_trend_5",
]


# === INDICATOR CALCULATIONS ===

def _ema(values: np.ndarray, period: int) -> float:
    if len(values) < period:
        return float(np.mean(values))
    mult = 2 / (period + 1)
    ema = float(values[0])
    for v in values[1:]:
        ema = (float(v) - ema) * mult + ema
    return ema


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(highs) < 2:
        return 0.0
    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]),
                               np.abs(lows[1:] - closes[:-1])))
    if len(tr) < period:
        return float(np.mean(tr))
    atr = float(np.mean(tr[:period]))
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
    return atr


def _adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> tuple[float, float, float]:
    if len(highs) < period + 1:
        return 25.0, 12.5, 12.5
    plus_dm = np.maximum(np.diff(highs), 0)
    minus_dm = np.maximum(-np.diff(lows), 0)
    mask = plus_dm > minus_dm
    plus_dm = np.where(mask, plus_dm, 0)
    minus_dm = np.where(~mask, minus_dm, 0)
    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]),
                               np.abs(lows[1:] - closes[:-1])))
    atr_s = _smooth(tr, period)
    plus_di_s = _smooth(plus_dm, period)
    minus_di_s = _smooth(minus_dm, period)
    if atr_s == 0:
        return 25.0, 12.5, 12.5
    plus_di = plus_di_s / atr_s * 100
    minus_di = minus_di_s / atr_s * 100
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 0 else 0
    return dx, plus_di, minus_di


def _adx_series(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    if len(highs) < period + 5:
        return np.array([25.0] * 5)
    results = []
    for i in range(5):
        end = len(highs) - 4 + i
        if end < period + 1:
            results.append(25.0)
        else:
            adx, _, _ = _adx(highs[:end], lows[:end], closes[:end], period)
            results.append(adx)
    return np.array(results)


def _smooth(values: np.ndarray, period: int) -> float:
    if len(values) < period:
        return float(np.mean(values))
    s = float(np.sum(values[:period]))
    for i in range(period, len(values)):
        s = s - s / period + values[i]
    return s / period


def _macd(closes: np.ndarray) -> tuple[float, float]:
    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    macd_line = fast - slow
    signal_line = macd_line * 0.15  # simplified
    return macd_line, signal_line


def _bollinger(closes: np.ndarray, period: int = 20, std_mult: float = 2.0) -> tuple[float, float, float]:
    if len(closes) < period:
        period = len(closes)
    window = closes[-period:]
    middle = float(np.mean(window))
    std = float(np.std(window))
    return middle + std_mult * std, middle, middle - std_mult * std


def _bollinger_squeeze_threshold(closes: np.ndarray, period: int) -> float:
    if len(closes) < period * 3:
        return 5.0
    widths = []
    for i in range(period, len(closes)):
        w = closes[i - period:i]
        m = float(np.mean(w))
        s = float(np.std(w))
        if m > 0:
            widths.append((2 * 2.0 * s) / m * 100)
    return float(np.percentile(widths, 20)) if widths else 5.0


def _supertrend_direction(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                           period: int = 10, mult: float = 3.0) -> float:
    atr = _atr(highs, lows, closes, period)
    hl2 = (highs[-1] + lows[-1]) / 2
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    return 1.0 if closes[-1] > lower else -1.0


def _linear_regression_slope(values: np.ndarray) -> float:
    if len(values) < 2:
        return 0.0
    x = np.arange(len(values), dtype=float)
    mean_x = np.mean(x)
    mean_y = np.mean(values)
    ss_xy = np.sum((x - mean_x) * (values - mean_y))
    ss_xx = np.sum((x - mean_x) ** 2)
    if ss_xx == 0:
        return 0.0
    slope = ss_xy / ss_xx
    return float(slope / mean_y * 100) if mean_y != 0 else 0.0


def _slope_of_last_n(values, n: int) -> float:
    arr = np.array(values[-n:], dtype=float)
    if len(arr) < 2:
        return 0.0
    return _linear_regression_slope(arr)


def _obv_slope(closes: np.ndarray, volumes: np.ndarray) -> float:
    if len(closes) < 2:
        return 0.0
    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return _linear_regression_slope(np.array(obv[-10:]))


def _skewness(arr: np.ndarray) -> float:
    n = len(arr)
    if n < 3:
        return 0.0
    mean = np.mean(arr)
    std = np.std(arr)
    if std == 0:
        return 0.0
    return float(np.mean(((arr - mean) / std) ** 3))


def _kurtosis(arr: np.ndarray) -> float:
    n = len(arr)
    if n < 4:
        return 0.0
    mean = np.mean(arr)
    std = np.std(arr)
    if std == 0:
        return 0.0
    return float(np.mean(((arr - mean) / std) ** 4) - 3.0)


def _hurst(prices: np.ndarray) -> float:
    if len(prices) < 20:
        return 0.5
    try:
        lags = range(2, min(20, len(prices) // 2))
        tau = []
        for lag in lags:
            diffs = prices[lag:] - prices[:-lag]
            std = np.std(diffs)
            if std > 0:
                tau.append(np.sqrt(std))
        if len(tau) < 3:
            return 0.5
        log_lags = np.log(list(lags)[:len(tau)])
        log_tau = np.log(tau)
        slope = np.polyfit(log_lags, log_tau, 1)[0]
        return max(0.0, min(1.0, float(slope)))
    except Exception:
        return 0.5


def _choppiness(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
    if len(highs) < period:
        return 50.0
    atr_sum = 0.0
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        atr_sum += tr
    highest = float(np.max(highs))
    lowest = float(np.min(lows))
    if highest == lowest:
        return 50.0
    return 100 * math.log10(atr_sum / (highest - lowest)) / math.log10(period)
