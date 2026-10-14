"""
Reverse Engineering Engine — Post-trade autopsy + pattern miner.

1. Autopsy: replays candle data around each trade, recalculates all indicators,
   scores which were right vs wrong, computes optimal SL/TP.
2. Miner: clusters trades by regime/time/direction/confidence, surfaces
   statistically significant rules, feeds them into TradeMemory.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.signal_generator import (
    Candle, SignalGenerator,
    calc_atr, calc_supertrend, calc_adx, calc_vwap, calc_vwap_bands,
    calc_rsi, calc_bollinger, calc_choppiness, calc_chandelier,
    calc_bb_width, _ema,
)

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DB_PATH = "data/omega.db"
MEMORY_PATH = "data/omega_memory.json"
AUTOPSY_PATH = "data/autopsy_results.json"

_INDEX_PATTERN = re.compile(r"^(NIFTY|BANKNIFTY|FINNIFTY|SENSEX|BANKEX)")


def _extract_index(symbol: str) -> str:
    m = _INDEX_PATTERN.match(symbol.split("/")[0])
    return m.group(1) if m else "UNKNOWN"


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


# ─── Autopsy dataclass ───────────────────────────────────────────────────

@dataclass
class TradeAutopsy:
    trade_id: int
    symbol: str
    direction: str
    index: str
    entry_price: float
    exit_price: float
    actual_pnl: float
    entry_time: str
    exit_time: str
    exit_reason: str
    hold_minutes: float

    # Indicator state at entry
    indicators_at_entry: dict = field(default_factory=dict)
    # Indicator state at exit
    indicators_at_exit: dict = field(default_factory=dict)

    # Which indicators agreed with trade direction
    indicators_correct: list = field(default_factory=list)
    indicators_wrong: list = field(default_factory=list)
    indicator_accuracy: float = 0.0

    # Optimal exit analysis
    max_favorable: float = 0.0       # max price move in trade direction
    max_adverse: float = 0.0         # max price move against trade
    optimal_exit_price: float = 0.0  # best exit within the trade window
    optimal_pnl: float = 0.0
    pnl_left_on_table: float = 0.0   # optimal - actual
    capture_ratio: float = 0.0       # actual / optimal (0-1)

    # Optimal SL/TP that would have maximized P&L
    optimal_sl_pct: float = 0.0
    optimal_tp_pct: float = 0.0

    # Signal component scores (each indicator's contribution)
    component_scores: dict = field(default_factory=dict)

    # Candle data quality
    candles_available: int = 0
    data_quality: str = "OK"


# ─── Pattern Miner dataclass ─────────────────────────────────────────────

@dataclass
class PatternRule:
    name: str
    conditions: dict
    win_rate: float
    sample_size: int
    avg_pnl: float
    avg_r_multiple: float
    z_score: float
    confidence: str       # LOW / MEDIUM / HIGH
    action: str           # BOOST / PENALIZE / BLOCK
    score_adjust: float
    description: str


# ═════════════════════════════════════════════════════════════════════════
#  POST-TRADE AUTOPSY ENGINE
# ═════════════════════════════════════════════════════════════════════════

class AutopsyEngine:

    def __init__(self, sig_gen: SignalGenerator | None = None):
        self.sig_gen = sig_gen
        self._results: list[TradeAutopsy] = []

    # ── Fetch index candles around trade time ─────────────────────────

    def _fetch_candles_for_trade(self, index: str,
                                 entry_time: datetime,
                                 exit_time: datetime) -> list[Candle]:
        if not self.sig_gen:
            return []
        candles = self.sig_gen.fetch_candles(index)
        if not candles:
            return []
        buffer = timedelta(minutes=30)
        start = entry_time - buffer
        end = exit_time + buffer
        window = [c for c in candles
                  if start <= (c.timestamp if c.timestamp.tzinfo else c.timestamp.replace(tzinfo=IST)) <= end]
        return window

    def _compute_indicators(self, candles: list[Candle], idx: int) -> dict:
        if idx < 25 or idx >= len(candles):
            return {}
        cfg = self.sig_gen if self.sig_gen else None
        st_period = cfg.st_period if cfg else 10
        st_mult = cfg.st_mult if cfg else 3.0
        adx_len = cfg.adx_len if cfg else 14
        rsi_period = cfg.rsi_period if cfg else 14
        bb_period = cfg.bb_period if cfg else 20
        bb_std = cfg.bb_std if cfg else 2.0
        ema_fast_p = cfg.ema_fast if cfg else 9
        ema_slow_p = cfg.ema_slow if cfg else 21
        chop_period = cfg.chop_period if cfg else 14
        chand_len = cfg.chand_len if cfg else 22
        chand_mult = cfg.chand_mult if cfg else 3.0

        subset = candles[:idx + 1]
        st_val, st_dir = calc_supertrend(subset, st_period, st_mult)
        adx_vals = calc_adx(subset, adx_len)
        vwap_vals = calc_vwap(subset)
        rsi_vals = calc_rsi(subset, rsi_period)
        bb_mid, bb_upper, bb_lower = calc_bollinger(subset, bb_period, bb_std)
        chop_vals = calc_choppiness(subset, chop_period)
        atr_vals = calc_atr(subset, st_period)
        chand_long, chand_short = calc_chandelier(subset, chand_len, chand_mult)
        closes = [c.close for c in subset]
        ema_f = _ema(closes, ema_fast_p)
        ema_s = _ema(closes, ema_slow_p)

        j = len(subset) - 1
        close = subset[j].close

        return {
            "close": close,
            "supertrend": st_val[j] if j < len(st_val) else 0,
            "supertrend_dir": "BULLISH" if (j < len(st_dir) and st_dir[j] == 1) else "BEARISH",
            "adx": round(adx_vals[j], 2) if j < len(adx_vals) else 0,
            "vwap": round(vwap_vals[j], 2) if j < len(vwap_vals) else 0,
            "vwap_position": "ABOVE" if close > vwap_vals[j] else "BELOW",
            "rsi": round(rsi_vals[j], 2) if j < len(rsi_vals) else 50,
            "bb_upper": round(bb_upper[j], 2) if j < len(bb_upper) else 0,
            "bb_lower": round(bb_lower[j], 2) if j < len(bb_lower) else 0,
            "bb_mid": round(bb_mid[j], 2) if j < len(bb_mid) else 0,
            "choppiness": round(chop_vals[j], 2) if j < len(chop_vals) else 50,
            "atr": round(atr_vals[j], 2) if j < len(atr_vals) else 0,
            "ema_fast": round(ema_f[j], 2),
            "ema_slow": round(ema_s[j], 2),
            "ema_cross": "BULLISH" if ema_f[j] > ema_s[j] else "BEARISH",
            "chandelier_long": round(chand_long[j], 2) if j < len(chand_long) else 0,
            "chandelier_short": round(chand_short[j], 2) if j < len(chand_short) else 0,
        }

    def _check_indicator_alignment(self, indicators: dict,
                                    direction: str) -> tuple[list, list]:
        is_long = direction == "LONG"
        correct, wrong = [], []

        checks = {
            "SuperTrend": indicators.get("supertrend_dir") == ("BULLISH" if is_long else "BEARISH"),
            "VWAP": indicators.get("vwap_position") == ("ABOVE" if is_long else "BELOW"),
            "EMA Cross": indicators.get("ema_cross") == ("BULLISH" if is_long else "BEARISH"),
            "RSI": (
                (30 < indicators.get("rsi", 50) < 70)
                if is_long else (30 < indicators.get("rsi", 50) < 70)
            ),
            "ADX Strength": indicators.get("adx", 0) > 20,
            "Choppiness": indicators.get("choppiness", 50) < 61.8,
        }

        rsi = indicators.get("rsi", 50)
        if is_long:
            checks["RSI"] = rsi < 70 and rsi > 30
        else:
            checks["RSI"] = rsi > 30 and rsi < 70

        for name, aligned in checks.items():
            if aligned:
                correct.append(name)
            else:
                wrong.append(name)

        return correct, wrong

    def _find_optimal_exit(self, candles: list[Candle],
                           entry_idx: int, exit_idx: int,
                           entry_price: float, exit_price: float,
                           actual_pnl: float,
                           direction: str, quantity: int) -> dict:
        is_long = direction == "LONG"

        # Candles are INDEX data, but trade is on OPTIONS.
        # Compute index % move range, then project onto option entry price.
        index_entry = candles[entry_idx].close if entry_idx < len(candles) else 0
        if index_entry <= 0:
            return {"best_price": exit_price, "worst_price": entry_price,
                    "max_favorable": 0, "max_adverse": 0, "optimal_pnl": 0,
                    "optimal_sl_pct": 0, "optimal_tp_pct": 0}

        best_move_pct = 0.0
        worst_move_pct = 0.0

        for i in range(entry_idx, min(exit_idx + 1, len(candles))):
            c = candles[i]
            if is_long:
                high_pct = (c.high - index_entry) / index_entry
                low_pct = (index_entry - c.low) / index_entry
                best_move_pct = max(best_move_pct, high_pct)
                worst_move_pct = max(worst_move_pct, low_pct)
            else:
                low_pct = (index_entry - c.low) / index_entry
                high_pct = (c.high - index_entry) / index_entry
                best_move_pct = max(best_move_pct, low_pct)
                worst_move_pct = max(worst_move_pct, high_pct)

        # Options have ~2-4x leverage on index moves (delta)
        delta_mult = 2.5
        max_favorable = entry_price * best_move_pct * delta_mult
        max_adverse = entry_price * worst_move_pct * delta_mult

        if is_long:
            optimal_exit = entry_price + max_favorable
            optimal_pnl = max_favorable * quantity
        else:
            optimal_exit = entry_price - max_favorable
            optimal_pnl = max_favorable * quantity

        optimal_sl_pct = round(worst_move_pct * delta_mult * 100 + 0.5, 2)
        optimal_tp_pct = round(best_move_pct * delta_mult * 100, 2)

        return {
            "best_price": round(optimal_exit, 2),
            "worst_price": round(entry_price - max_adverse if is_long else entry_price + max_adverse, 2),
            "max_favorable": round(max_favorable, 2),
            "max_adverse": round(max_adverse, 2),
            "optimal_pnl": round(optimal_pnl, 2),
            "optimal_sl_pct": min(optimal_sl_pct, 50.0),
            "optimal_tp_pct": min(optimal_tp_pct, 100.0),
        }

    def _score_components(self, indicators_entry: dict,
                          indicators_exit: dict,
                          direction: str,
                          actual_pnl: float) -> dict:
        is_long = direction == "LONG"
        scores = {}

        # SuperTrend: did it correctly predict direction?
        st_correct = (indicators_entry.get("supertrend_dir") ==
                      ("BULLISH" if is_long else "BEARISH"))
        scores["SuperTrend"] = {
            "signal": indicators_entry.get("supertrend_dir", "?"),
            "correct": st_correct,
            "score": 1.0 if st_correct else -1.0,
        }

        # ADX: was trend strong enough?
        adx = indicators_entry.get("adx", 0)
        adx_good = adx > 20
        scores["ADX"] = {
            "value": adx,
            "strong_enough": adx_good,
            "score": 1.0 if adx_good else -0.5,
        }

        # VWAP: correct side?
        vwap_correct = (indicators_entry.get("vwap_position") ==
                        ("ABOVE" if is_long else "BELOW"))
        scores["VWAP"] = {
            "position": indicators_entry.get("vwap_position", "?"),
            "correct": vwap_correct,
            "score": 1.0 if vwap_correct else -1.0,
        }

        # EMA Cross
        ema_correct = (indicators_entry.get("ema_cross") ==
                       ("BULLISH" if is_long else "BEARISH"))
        scores["EMA_Cross"] = {
            "signal": indicators_entry.get("ema_cross", "?"),
            "correct": ema_correct,
            "score": 1.0 if ema_correct else -0.5,
        }

        # RSI: not overbought/oversold against direction
        rsi = indicators_entry.get("rsi", 50)
        rsi_ok = (rsi < 70) if is_long else (rsi > 30)
        scores["RSI"] = {
            "value": rsi,
            "favorable": rsi_ok,
            "score": 0.5 if rsi_ok else -1.0,
        }

        # Choppiness: was market trending?
        chop = indicators_entry.get("choppiness", 50)
        trending = chop < 61.8
        scores["Choppiness"] = {
            "value": chop,
            "trending": trending,
            "score": 0.5 if trending else -1.0,
        }

        # BB position
        close = indicators_entry.get("close", 0)
        bb_upper = indicators_entry.get("bb_upper", 0)
        bb_lower = indicators_entry.get("bb_lower", 0)
        if bb_upper > 0 and bb_lower > 0:
            bb_pct = (close - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5
            bb_ok = (bb_pct < 0.8) if is_long else (bb_pct > 0.2)
            scores["Bollinger"] = {
                "pct_b": round(bb_pct, 3),
                "favorable": bb_ok,
                "score": 0.5 if bb_ok else -0.5,
            }

        return scores

    # ── Main autopsy runner ───────────────────────────────────────────

    def run_autopsy(self, limit: int = 0) -> list[TradeAutopsy]:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        query = """SELECT id, symbol, direction, quantity, entry_price, exit_price,
                          pnl, entry_time, exit_time, exit_reason, strategy
                   FROM trades WHERE exit_price IS NOT NULL AND exit_price > 0
                   ORDER BY id DESC"""
        if limit > 0:
            query += f" LIMIT {limit}"
        cur.execute(query)
        cols = [d[0] for d in cur.description]
        trades = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()

        if not trades:
            log.info("No closed trades to autopsy")
            return []

        log.info("Running autopsy on %d trades", len(trades))
        results = []

        for t in trades:
            try:
                result = self._autopsy_single(t)
                if result:
                    results.append(result)
            except Exception as exc:
                log.warning("Autopsy failed for trade %s: %s", t.get("id"), exc)

        self._results = results
        self._save_results(results)
        return results

    def _autopsy_single(self, trade: dict) -> TradeAutopsy | None:
        symbol = trade["symbol"]
        index = _extract_index(symbol)
        entry_time = _parse_dt(trade["entry_time"])
        exit_time = _parse_dt(trade["exit_time"])
        hold_minutes = (exit_time - entry_time).total_seconds() / 60
        direction = trade["direction"]
        quantity = trade.get("quantity", 1)

        candles = self._fetch_candles_for_trade(index, entry_time, exit_time)

        autopsy = TradeAutopsy(
            trade_id=trade["id"],
            symbol=symbol,
            direction=direction,
            index=index,
            entry_price=trade["entry_price"],
            exit_price=trade["exit_price"],
            actual_pnl=trade["pnl"],
            entry_time=trade["entry_time"],
            exit_time=trade["exit_time"],
            exit_reason=trade.get("exit_reason", ""),
            hold_minutes=round(hold_minutes, 1),
            candles_available=len(candles),
        )

        if len(candles) < 30:
            autopsy.data_quality = "INSUFFICIENT"
            return autopsy

        # Find entry and exit candle indices
        entry_idx = self._find_nearest_candle(candles, entry_time)
        exit_idx = self._find_nearest_candle(candles, exit_time)

        if entry_idx is None or exit_idx is None:
            autopsy.data_quality = "NO_MATCH"
            return autopsy

        # Compute indicators at entry and exit
        autopsy.indicators_at_entry = self._compute_indicators(candles, entry_idx)
        autopsy.indicators_at_exit = self._compute_indicators(candles, exit_idx)

        # Check which indicators were aligned
        correct, wrong = self._check_indicator_alignment(
            autopsy.indicators_at_entry, direction)
        autopsy.indicators_correct = correct
        autopsy.indicators_wrong = wrong
        total = len(correct) + len(wrong)
        autopsy.indicator_accuracy = round(len(correct) / total, 3) if total > 0 else 0

        # Optimal exit analysis
        opt = self._find_optimal_exit(
            candles, entry_idx, exit_idx,
            trade["entry_price"], trade["exit_price"],
            trade["pnl"], direction, quantity)
        autopsy.max_favorable = opt["max_favorable"]
        autopsy.max_adverse = opt["max_adverse"]
        autopsy.optimal_exit_price = opt["best_price"]
        autopsy.optimal_pnl = opt["optimal_pnl"]
        autopsy.pnl_left_on_table = round(
            max(0, opt["optimal_pnl"] - trade["pnl"]), 2)
        if opt["optimal_pnl"] > 0 and trade["pnl"] > 0:
            autopsy.capture_ratio = round(
                min(trade["pnl"] / opt["optimal_pnl"], 1.0), 3)
        elif trade["pnl"] >= 0:
            autopsy.capture_ratio = 1.0
        else:
            autopsy.capture_ratio = 0.0
        autopsy.optimal_sl_pct = opt["optimal_sl_pct"]
        autopsy.optimal_tp_pct = opt["optimal_tp_pct"]

        # Component scores
        autopsy.component_scores = self._score_components(
            autopsy.indicators_at_entry,
            autopsy.indicators_at_exit,
            direction, trade["pnl"])

        return autopsy

    def _find_nearest_candle(self, candles: list[Candle],
                              target: datetime) -> int | None:
        best_idx, best_diff = None, float("inf")
        for i, c in enumerate(candles):
            ts = c.timestamp if c.timestamp.tzinfo else c.timestamp.replace(tzinfo=IST)
            diff = abs((ts - target).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_idx = i
        return best_idx if best_diff < 600 else None  # within 10 min

    def _save_results(self, results: list[TradeAutopsy]) -> None:
        data = [asdict(r) for r in results]
        Path(AUTOPSY_PATH).write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8")
        log.info("Saved %d autopsy results to %s", len(results), AUTOPSY_PATH)

    # ── Summary report ────────────────────────────────────────────────

    def summary(self) -> dict:
        if not self._results:
            return {}

        valid = [r for r in self._results if r.data_quality == "OK"]
        if not valid:
            return {"total": len(self._results), "valid": 0}

        wins = [r for r in valid if r.actual_pnl >= 0]
        losses = [r for r in valid if r.actual_pnl < 0]

        # Aggregate indicator accuracy
        indicator_stats = defaultdict(lambda: {"correct": 0, "wrong": 0})
        for r in valid:
            for ind in r.indicators_correct:
                indicator_stats[ind]["correct"] += 1
            for ind in r.indicators_wrong:
                indicator_stats[ind]["wrong"] += 1

        indicator_accuracy = {}
        for ind, stats in indicator_stats.items():
            total = stats["correct"] + stats["wrong"]
            indicator_accuracy[ind] = {
                "accuracy": round(stats["correct"] / total, 3) if total > 0 else 0,
                "correct": stats["correct"],
                "total": total,
            }

        total_left = sum(r.pnl_left_on_table for r in valid if r.pnl_left_on_table > 0)
        avg_capture = (sum(r.capture_ratio for r in valid) / len(valid)) if valid else 0

        # Best/worst component scores
        component_agg = defaultdict(lambda: {"total_score": 0, "count": 0})
        for r in valid:
            for comp, data in r.component_scores.items():
                component_agg[comp]["total_score"] += data.get("score", 0)
                component_agg[comp]["count"] += 1

        component_avg = {
            comp: round(d["total_score"] / d["count"], 3)
            for comp, d in component_agg.items() if d["count"] > 0
        }

        return {
            "total_trades": len(self._results),
            "valid_trades": len(valid),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(valid), 3),
            "total_pnl_left_on_table": round(total_left, 2),
            "avg_capture_ratio": round(avg_capture, 3),
            "avg_optimal_sl_pct": round(
                sum(r.optimal_sl_pct for r in valid) / len(valid), 2),
            "avg_optimal_tp_pct": round(
                sum(r.optimal_tp_pct for r in valid) / len(valid), 2),
            "indicator_accuracy": dict(sorted(
                indicator_accuracy.items(),
                key=lambda x: x[1]["accuracy"], reverse=True)),
            "component_scores": dict(sorted(
                component_avg.items(), key=lambda x: x[1], reverse=True)),
            "exit_reason_breakdown": dict(
                sorted(
                    defaultdict(int,
                                {r.exit_reason: sum(1 for x in valid if x.exit_reason == r.exit_reason)
                                 for r in valid}).items(),
                    key=lambda x: x[1], reverse=True)),
        }


# ═════════════════════════════════════════════════════════════════════════
#  PATTERN MINER
# ═════════════════════════════════════════════════════════════════════════

class PatternMiner:

    def __init__(self):
        self._rules: list[PatternRule] = []

    def mine(self, min_sample: int = 5, z_threshold: float = 1.65) -> list[PatternRule]:
        mem = self._load_memory()
        trades = mem.get("trades", [])
        if len(trades) < min_sample * 2:
            log.info("Not enough trades (%d) for pattern mining", len(trades))
            return []

        log.info("Mining patterns from %d trades", len(trades))

        baseline_wr = sum(1 for t in trades if t.get("is_win")) / len(trades) if trades else 0.5
        all_rules: list[PatternRule] = []

        # Dimension 1: Time of day
        all_rules.extend(self._mine_dimension(
            trades, "entry_hour",
            lambda t: str(t.get("entry_hour", 0)),
            "hour", baseline_wr, min_sample, z_threshold))

        # Dimension 2: Volatility regime
        all_rules.extend(self._mine_dimension(
            trades, "volatility_regime",
            lambda t: t.get("volatility_regime", "NORMAL"),
            "regime", baseline_wr, min_sample, z_threshold))

        # Dimension 3: Index
        all_rules.extend(self._mine_dimension(
            trades, "index",
            lambda t: _extract_index(t.get("symbol", "")),
            "index", baseline_wr, min_sample, z_threshold))

        # Dimension 4: Direction
        all_rules.extend(self._mine_dimension(
            trades, "direction",
            lambda t: t.get("direction", ""),
            "direction", baseline_wr, min_sample, z_threshold))

        # Dimension 5: Signal confidence
        all_rules.extend(self._mine_dimension(
            trades, "confidence",
            lambda t: t.get("signal_confidence", ""),
            "confidence", baseline_wr, min_sample, z_threshold))

        # Dimension 6: ADX zone
        all_rules.extend(self._mine_dimension(
            trades, "adx_zone",
            lambda t: self._adx_zone(t.get("adx_value", 0)),
            "adx_zone", baseline_wr, min_sample, z_threshold))

        # Dimension 7: VWAP position
        all_rules.extend(self._mine_dimension(
            trades, "vwap_position",
            lambda t: t.get("vwap_position", ""),
            "vwap", baseline_wr, min_sample, z_threshold))

        # Dimension 8: SuperTrend direction
        all_rules.extend(self._mine_dimension(
            trades, "supertrend_dir",
            lambda t: t.get("supertrend_dir", ""),
            "supertrend", baseline_wr, min_sample, z_threshold))

        # Multi-factor combos (2-way)
        all_rules.extend(self._mine_combos(
            trades, baseline_wr, min_sample, z_threshold))

        # Sort by z-score descending
        all_rules.sort(key=lambda r: abs(r.z_score), reverse=True)
        self._rules = all_rules

        log.info("Mined %d significant patterns", len(all_rules))
        return all_rules

    def _mine_dimension(self, trades: list[dict], dim_name: str,
                         key_fn, prefix: str,
                         baseline_wr: float, min_sample: int,
                         z_threshold: float) -> list[PatternRule]:
        buckets: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            k = key_fn(t)
            if k:
                buckets[k].append(t)

        rules = []
        for bucket_key, bucket_trades in buckets.items():
            if len(bucket_trades) < min_sample:
                continue
            rule = self._test_bucket(
                f"{prefix}_{bucket_key}", {dim_name: bucket_key},
                bucket_trades, baseline_wr, z_threshold)
            if rule:
                rules.append(rule)
        return rules

    def _mine_combos(self, trades: list[dict], baseline_wr: float,
                      min_sample: int, z_threshold: float) -> list[PatternRule]:
        combos = [
            ("hour_regime", lambda t: (str(t.get("entry_hour", 0)),
                                       t.get("volatility_regime", "NORMAL"))),
            ("hour_direction", lambda t: (str(t.get("entry_hour", 0)),
                                          t.get("direction", ""))),
            ("index_direction", lambda t: (_extract_index(t.get("symbol", "")),
                                           t.get("direction", ""))),
            ("regime_confidence", lambda t: (t.get("volatility_regime", "NORMAL"),
                                             t.get("signal_confidence", ""))),
            ("adx_vwap", lambda t: (self._adx_zone(t.get("adx_value", 0)),
                                     t.get("vwap_position", ""))),
            ("direction_supertrend", lambda t: (t.get("direction", ""),
                                                 t.get("supertrend_dir", ""))),
        ]

        rules = []
        for combo_name, key_fn in combos:
            buckets: dict[tuple, list[dict]] = defaultdict(list)
            for t in trades:
                k = key_fn(t)
                if all(k):
                    buckets[k].append(t)

            for bucket_key, bucket_trades in buckets.items():
                if len(bucket_trades) < min_sample:
                    continue
                conditions = dict(zip(combo_name.split("_"), bucket_key))
                rule = self._test_bucket(
                    f"{combo_name}_{'_'.join(bucket_key)}",
                    conditions, bucket_trades, baseline_wr, z_threshold)
                if rule:
                    rules.append(rule)
        return rules

    def _test_bucket(self, name: str, conditions: dict,
                      trades: list[dict], baseline_wr: float,
                      z_threshold: float) -> PatternRule | None:
        n = len(trades)
        wins = sum(1 for t in trades if t.get("is_win"))
        wr = wins / n
        avg_pnl = sum(t.get("pnl", 0) for t in trades) / n
        avg_r = sum(t.get("r_multiple", 0) for t in trades) / n

        # z-test vs baseline
        se = math.sqrt(baseline_wr * (1 - baseline_wr) / n) if n > 0 else 1
        z = (wr - baseline_wr) / se if se > 0 else 0

        if abs(z) < z_threshold:
            return None

        # Determine action
        if z > 0:
            action = "BOOST"
            score_adjust = min(round(z * 3, 1), 15)
            conf = "HIGH" if z > 2.33 else "MEDIUM" if z > 1.96 else "LOW"
        else:
            if wr < 0.25 and n >= 10:
                action = "BLOCK"
                score_adjust = -30
            else:
                action = "PENALIZE"
                score_adjust = max(round(z * 3, 1), -15)
            conf = "HIGH" if z < -2.33 else "MEDIUM" if z < -1.96 else "LOW"

        desc = (f"{name}: WR={wr:.0%} vs baseline {baseline_wr:.0%} "
                f"(n={n}, z={z:.2f}, avg_pnl={avg_pnl:.0f})")

        return PatternRule(
            name=name,
            conditions=conditions,
            win_rate=round(wr, 3),
            sample_size=n,
            avg_pnl=round(avg_pnl, 2),
            avg_r_multiple=round(avg_r, 3),
            z_score=round(z, 3),
            confidence=conf,
            action=action,
            score_adjust=score_adjust,
            description=desc,
        )

    @staticmethod
    def _adx_zone(adx: float) -> str:
        if adx < 15:
            return "dead"
        elif adx < 20:
            return "flat"
        elif adx < 25:
            return "weak"
        elif adx < 30:
            return "moderate"
        elif adx < 40:
            return "strong"
        return "very_strong"

    def _load_memory(self) -> dict:
        p = Path(MEMORY_PATH)
        if not p.exists():
            return {"trades": [], "lessons": [], "rules": []}
        return json.loads(p.read_text(encoding="utf-8"))

    # ── Feed rules into TradeMemory ───────────────────────────────────

    def inject_rules(self, rules: list[PatternRule] | None = None) -> int:
        rules = rules or self._rules
        if not rules:
            return 0

        mem = self._load_memory()
        existing_ids = {r.get("id", "") for r in mem.get("rules", [])}
        injected = 0

        now = datetime.now(IST).isoformat()
        for rule in rules:
            rule_data = {
                "id": rule.name,
                "pattern": rule.description,
                "condition": rule.conditions,
                "action": rule.action,
                "score_modifier": rule.score_adjust,
                "confidence": rule.z_score,
                "sample_size": rule.sample_size,
                "win_rate": rule.win_rate,
                "baseline_win_rate": 0,
                "created_at": now,
                "last_evaluated": now,
                "active": True,
                "hit_count": 0,
                "correct_predictions": 0,
                "total_predictions": 0,
                "effectiveness": 0.0,
                "source": "pattern_miner",
            }

            if rule.name in existing_ids:
                for i, r in enumerate(mem["rules"]):
                    if r.get("id") == rule.name:
                        mem["rules"][i] = rule_data
                        break
            else:
                mem["rules"].append(rule_data)

            injected += 1

        Path(MEMORY_PATH).write_text(
            json.dumps(mem, indent=2, default=str), encoding="utf-8")
        log.info("Injected %d mined rules into TradeMemory", injected)
        return injected

    def summary(self) -> dict:
        if not self._rules:
            return {}

        boosters = [r for r in self._rules if r.action == "BOOST"]
        penalties = [r for r in self._rules if r.action == "PENALIZE"]
        blockers = [r for r in self._rules if r.action == "BLOCK"]

        return {
            "total_patterns": len(self._rules),
            "boosters": len(boosters),
            "penalties": len(penalties),
            "blockers": len(blockers),
            "top_boosters": [
                {"name": r.name, "wr": r.win_rate, "z": r.z_score,
                 "n": r.sample_size, "adj": r.score_adjust}
                for r in sorted(boosters, key=lambda x: x.z_score, reverse=True)[:5]
            ],
            "top_penalties": [
                {"name": r.name, "wr": r.win_rate, "z": r.z_score,
                 "n": r.sample_size, "adj": r.score_adjust}
                for r in sorted(penalties, key=lambda x: x.z_score)[:5]
            ],
            "blockers_list": [
                {"name": r.name, "wr": r.win_rate, "n": r.sample_size}
                for r in blockers
            ],
        }


# ═════════════════════════════════════════════════════════════════════════
#  COMBINED REVERSE ENGINE
# ═════════════════════════════════════════════════════════════════════════

class ReverseEngine:
    """Orchestrates both autopsy and pattern mining."""

    def __init__(self, sig_gen: SignalGenerator | None = None):
        self.autopsy = AutopsyEngine(sig_gen)
        self.miner = PatternMiner()

    def run_full_analysis(self, limit: int = 0) -> dict:
        log.info("═══ REVERSE ENGINE: Full analysis starting ═══")

        # 1. Run autopsy
        autopsy_results = self.autopsy.run_autopsy(limit=limit)
        autopsy_summary = self.autopsy.summary()

        # 2. Run pattern mining
        mined_rules = self.miner.mine()
        miner_summary = self.miner.summary()

        # 3. Inject rules into TradeMemory
        injected = self.miner.inject_rules()

        report = {
            "timestamp": datetime.now(IST).isoformat(),
            "autopsy": autopsy_summary,
            "patterns": miner_summary,
            "rules_injected": injected,
        }

        # Save combined report
        Path("data/reverse_engine_report.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8")
        log.info("═══ REVERSE ENGINE: Complete. %d autopsies, %d patterns, %d rules injected ═══",
                 len(autopsy_results), len(mined_rules), injected)

        return report

    def print_report(self, report: dict | None = None) -> str:
        report = report or self.run_full_analysis()
        lines = ["═══ REVERSE ENGINE REPORT ═══", ""]

        a = report.get("autopsy", {})
        if a:
            lines.append(f"AUTOPSY: {a.get('valid_trades', 0)} trades analyzed")
            lines.append(f"  Win Rate: {a.get('win_rate', 0):.0%}")
            lines.append(f"  Avg Capture Ratio: {a.get('avg_capture_ratio', 0):.0%}")
            lines.append(f"  P&L Left on Table: ₹{a.get('total_pnl_left_on_table', 0):,.0f}")
            lines.append(f"  Optimal SL: {a.get('avg_optimal_sl_pct', 0):.1f}%  |  Optimal TP: {a.get('avg_optimal_tp_pct', 0):.1f}%")
            lines.append("")

            ind = a.get("indicator_accuracy", {})
            if ind:
                lines.append("  INDICATOR ACCURACY:")
                for name, data in ind.items():
                    lines.append(f"    {name}: {data['accuracy']:.0%} ({data['correct']}/{data['total']})")
            lines.append("")

            comp = a.get("component_scores", {})
            if comp:
                lines.append("  COMPONENT SCORES (avg):")
                for name, score in comp.items():
                    bar = "+" if score > 0 else ""
                    lines.append(f"    {name}: {bar}{score:.2f}")
            lines.append("")

        p = report.get("patterns", {})
        if p:
            lines.append(f"PATTERNS: {p.get('total_patterns', 0)} significant")
            lines.append(f"  Boosters: {p.get('boosters', 0)}  |  Penalties: {p.get('penalties', 0)}  |  Blockers: {p.get('blockers', 0)}")

            for b in p.get("top_boosters", []):
                lines.append(f"  ✓ {b['name']}: WR={b['wr']:.0%} z={b['z']:.1f} adj={b['adj']:+.0f}")
            for b in p.get("top_penalties", []):
                lines.append(f"  ✗ {b['name']}: WR={b['wr']:.0%} z={b['z']:.1f} adj={b['adj']:+.0f}")
            for b in p.get("blockers_list", []):
                lines.append(f"  ⊘ BLOCK {b['name']}: WR={b['wr']:.0%} n={b['n']}")
            lines.append("")

        lines.append(f"Rules injected: {report.get('rules_injected', 0)}")
        return "\n".join(lines)
