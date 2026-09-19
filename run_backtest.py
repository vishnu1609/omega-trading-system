"""
Backtest Runner

Usage:
    python run_backtest.py                          # default backtest
    python run_backtest.py --data data/nifty.csv    # your own data
    python run_backtest.py --optimize               # grid search
    python run_backtest.py --walk-forward            # walk-forward validation
    python run_backtest.py --buy-only               # test buy mode only
    python run_backtest.py --sell-only               # test sell mode only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backtest.engine import BacktestEngine, StrategyParams
from backtest.optimizer import ParameterOptimizer
from data.feed import generate_sample_data
from utils.logger import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Omega Backtest Runner")
    parser.add_argument("--data", type=str, default="", help="Path to OHLCV CSV")
    parser.add_argument("--optimize", action="store_true", help="Run grid search")
    parser.add_argument("--walk-forward", action="store_true", help="Walk-forward validation")
    parser.add_argument("--capital", type=float, default=100_000, help="Initial capital")
    parser.add_argument("--lots", type=int, default=1, help="Number of lots")
    parser.add_argument("--lot-size", type=int, default=15, help="Lot size")
    parser.add_argument("--buy-only", action="store_true", help="Test buy mode only")
    parser.add_argument("--sell-only", action="store_true", help="Test sell mode only")
    args = parser.parse_args()

    setup_logging(level="INFO")

    data_path = args.data
    if not data_path or not Path(data_path).exists():
        print("No data file found — generating synthetic data for testing...")
        print("WARNING: Synthetic data is for TESTING ONLY.\n")
        data_path = generate_sample_data(
            symbol="BANKNIFTY", days=90,
            timeframe_min=5, base_price=50000.0,
        )

    enable_buying = not args.sell_only
    enable_selling = not args.buy_only

    if args.optimize:
        print("Running parameter grid search...\n")
        optimizer = ParameterOptimizer(data_path, initial_capital=args.capital)
        results = optimizer.run_grid_search(top_n=10)

        if results:
            optimizer.export_results(results)
            best = results[0].params
            print(f"\nTop result:")
            print(f"   SuperTrend: period={best.st_period}, mult={best.st_multiplier}")
            print(f"   TEMA: {best.tema_length}")
            print(f"   ADX buy/sell: {best.adx_buy_threshold}/{best.adx_sell_threshold}")
            print(f"   Chandelier mult: {best.chandelier_mult}")
            print(f"   SL buffer: {best.sl_buffer_pct}%")
            results[0].in_sample.print_summary()

            if args.walk_forward:
                print("Running walk-forward on best params...\n")
                wf = optimizer.walk_forward(best, n_splits=5)
                optimizer.print_walk_forward(wf)
        return

    if args.walk_forward:
        print("Running walk-forward with default params...\n")
        params = StrategyParams(
            lot_size=args.lot_size, num_lots=args.lots,
            enable_buying=enable_buying, enable_selling=enable_selling,
        )
        optimizer = ParameterOptimizer(data_path, initial_capital=args.capital)
        wf = optimizer.walk_forward(params, n_splits=5)
        optimizer.print_walk_forward(wf)
        return

    print("Running backtest...\n")
    mode_str = "BUY+SELL"
    if args.buy_only:
        mode_str = "BUY ONLY"
    elif args.sell_only:
        mode_str = "SELL ONLY"
    print(f"Mode: {mode_str}\n")

    engine = BacktestEngine.from_csv(data_path, initial_capital=args.capital)
    params = StrategyParams(
        lot_size=args.lot_size, num_lots=args.lots,
        enable_buying=enable_buying, enable_selling=enable_selling,
    )
    results = engine.run(params)
    results.print_summary()

    output = "backtest/trade_log.csv"
    results.to_csv(output)
    print(f"Trade log saved: {output}")


if __name__ == "__main__":
    main()
