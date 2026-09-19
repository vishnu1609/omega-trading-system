"""
Parameter Optimizer — Grid Search + Walk-Forward Validation

Prevents overfitting by splitting data into in-sample (optimize) and
out-of-sample (validate) windows.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

from backtest.engine import BacktestEngine, BacktestResults, CostModel, StrategyParams

log = logging.getLogger(__name__)


@dataclass
class OptimizationResult:
    params: StrategyParams
    in_sample: BacktestResults
    out_sample: BacktestResults | None = None
    rank_score: float = 0.0


@dataclass
class WalkForwardResult:
    splits: list[dict] = field(default_factory=list)
    combined_pnl: float = 0.0
    combined_trades: int = 0
    avg_profit_factor: float = 0.0
    avg_win_rate: float = 0.0
    worst_split_pnl: float = 0.0
    is_robust: bool = False


def rank_score(results: BacktestResults) -> float:
    if results.total_trades < 10:
        return -999

    pf = min(results.profit_factor, 5.0)
    wr = results.win_rate / 100
    dd_impact = min(results.max_drawdown_pct / 100, 1.0)
    trade_bonus = min(results.total_trades / 100, 1.0)

    return round(pf * wr * (1 - dd_impact * 0.5) * (0.5 + 0.5 * trade_bonus), 4)


PARAM_GRID = {
    "st_period":          [7, 10, 14],
    "st_multiplier":      [2.0, 3.0, 4.0],
    "tema_length":        [14, 21, 34],
    "adx_buy_threshold":  [20, 25, 30],
    "adx_sell_threshold": [15, 20],
    "chandelier_mult":    [2.5, 3.0, 3.5],
    "sl_buffer_pct":      [1.0, 1.5, 2.0],
}


class ParameterOptimizer:
    def __init__(self, data_path: str, cost_config: dict | None = None,
                 initial_capital: float = 100_000):
        self.data_path = data_path
        self.cost_config = cost_config
        self.initial_capital = initial_capital

    def run_grid_search(self, param_grid: dict | None = None,
                        train_pct: float = 0.7,
                        top_n: int = 10) -> list[OptimizationResult]:
        grid = param_grid or PARAM_GRID
        engine = BacktestEngine.from_csv(self.data_path, self.cost_config,
                                         self.initial_capital)

        total_candles = len(engine.candles)
        split_idx = int(total_candles * train_pct)

        train_engine = BacktestEngine(engine.candles[:split_idx],
                                      engine.cost_model, self.initial_capital)
        test_engine = BacktestEngine(engine.candles[split_idx:],
                                     engine.cost_model, self.initial_capital)

        keys = list(grid.keys())
        combos = list(product(*[grid[k] for k in keys]))
        total = len(combos)
        log.info("Grid search: %d combos, train=%d, test=%d candles",
                 total, split_idx, total_candles - split_idx)

        results: list[OptimizationResult] = []

        for idx, combo in enumerate(combos):
            params = StrategyParams(**dict(zip(keys, combo)))
            in_sample = train_engine.run(params)
            score = rank_score(in_sample)

            if score <= 0:
                continue

            out_sample = test_engine.run(params)
            oos_score = rank_score(out_sample)
            combined_score = score * 0.4 + oos_score * 0.6

            results.append(OptimizationResult(
                params=params,
                in_sample=in_sample,
                out_sample=out_sample,
                rank_score=combined_score,
            ))

            if (idx + 1) % 50 == 0:
                log.info("Progress: %d/%d combos", idx + 1, total)

        results.sort(key=lambda r: r.rank_score, reverse=True)
        top = results[:top_n]

        log.info("Grid search complete. Top %d:", min(top_n, len(top)))
        for i, r in enumerate(top):
            oos = r.out_sample
            log.info("  #%d score=%.4f | IS: PF=%.2f WR=%.1f%% | "
                     "OOS: PF=%.2f WR=%.1f%%",
                     i + 1, r.rank_score,
                     r.in_sample.profit_factor, r.in_sample.win_rate,
                     oos.profit_factor if oos else 0,
                     oos.win_rate if oos else 0)

        return top

    def walk_forward(self, params: StrategyParams,
                     n_splits: int = 5) -> WalkForwardResult:
        engine = BacktestEngine.from_csv(self.data_path, self.cost_config,
                                         self.initial_capital)
        total = len(engine.candles)
        chunk = total // (n_splits + 1)

        wf = WalkForwardResult()

        for i in range(n_splits):
            test_start = chunk * (i + 2)
            test_end = min(test_start + chunk, total)
            if test_end <= test_start:
                break

            test_engine = BacktestEngine(
                engine.candles[test_start:test_end],
                engine.cost_model, self.initial_capital,
            )
            result = test_engine.run(params)

            split = {
                "split": i + 1,
                "test_from": engine.candles[test_start].timestamp.isoformat(),
                "test_to": engine.candles[min(test_end - 1, total - 1)].timestamp.isoformat(),
                "trades": result.total_trades,
                "long_trades": result.long_trades,
                "short_trades": result.short_trades,
                "net_pnl": result.net_pnl,
                "win_rate": result.win_rate,
                "profit_factor": result.profit_factor,
                "max_dd_pct": result.max_drawdown_pct,
            }
            wf.splits.append(split)
            wf.combined_pnl += result.net_pnl
            wf.combined_trades += result.total_trades

            log.info("WF split %d/%d: PnL=₹%.0f WR=%.0f%% PF=%.2f "
                     "(L:%d S:%d)",
                     i + 1, n_splits, result.net_pnl,
                     result.win_rate, result.profit_factor,
                     result.long_trades, result.short_trades)

        if wf.splits:
            pfs = [s["profit_factor"] for s in wf.splits if s["profit_factor"] > 0]
            wrs = [s["win_rate"] for s in wf.splits]
            wf.avg_profit_factor = sum(pfs) / len(pfs) if pfs else 0
            wf.avg_win_rate = sum(wrs) / len(wrs) if wrs else 0
            wf.worst_split_pnl = min(s["net_pnl"] for s in wf.splits)

            profitable = sum(1 for s in wf.splits if s["net_pnl"] > 0)
            wf.is_robust = (
                profitable >= n_splits * 0.6
                and wf.avg_profit_factor > 1.2
                and wf.avg_win_rate > 40
            )

        return wf

    def print_walk_forward(self, wf: WalkForwardResult) -> None:
        print("\n" + "=" * 65)
        print("  WALK-FORWARD VALIDATION")
        print("=" * 65)
        for s in wf.splits:
            ok = "+" if s["net_pnl"] > 0 else "-"
            print(f"  [{ok}] Split {s['split']}: "
                  f"PnL=₹{s['net_pnl']:>8,.0f} | "
                  f"WR={s['win_rate']:>5.1f}% | "
                  f"PF={s['profit_factor']:>5.2f} | "
                  f"L:{s['long_trades']} S:{s['short_trades']}")
        print("-" * 65)
        print(f"  Combined PnL:      ₹{wf.combined_pnl:>10,.2f}")
        print(f"  Total Trades:      {wf.combined_trades:>10}")
        print(f"  Avg Profit Factor: {wf.avg_profit_factor:>10.2f}")
        print(f"  Avg Win Rate:      {wf.avg_win_rate:>9.1f}%")
        print(f"  Worst Split PnL:   ₹{wf.worst_split_pnl:>10,.2f}")
        print(f"  Robust:            {'YES' if wf.is_robust else 'NO'}")
        print("=" * 65 + "\n")

    def export_results(self, results: list[OptimizationResult],
                       path: str = "backtest/optimization_results.csv") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "rank", "score", "st_period", "st_mult", "tema_len",
                "adx_buy", "adx_sell", "chand_mult", "sl_buffer",
                "is_trades", "is_pnl", "is_wr", "is_pf", "is_dd",
                "oos_trades", "oos_pnl", "oos_wr", "oos_pf", "oos_dd",
            ])
            for i, r in enumerate(results):
                p = r.params
                is_r = r.in_sample
                oos_r = r.out_sample
                writer.writerow([
                    i + 1, r.rank_score,
                    p.st_period, p.st_multiplier, p.tema_length,
                    p.adx_buy_threshold, p.adx_sell_threshold,
                    p.chandelier_mult, p.sl_buffer_pct,
                    is_r.total_trades, f"{is_r.net_pnl:.0f}",
                    f"{is_r.win_rate:.1f}", f"{is_r.profit_factor:.2f}",
                    f"{is_r.max_drawdown_pct:.1f}",
                    oos_r.total_trades if oos_r else 0,
                    f"{oos_r.net_pnl:.0f}" if oos_r else "0",
                    f"{oos_r.win_rate:.1f}" if oos_r else "0",
                    f"{oos_r.profit_factor:.2f}" if oos_r else "0",
                    f"{oos_r.max_drawdown_pct:.1f}" if oos_r else "0",
                ])
        log.info("Results exported → %s", path)
