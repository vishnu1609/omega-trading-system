"""
Backtesting Engine

Simulates strategy execution on historical OHLCV data with realistic
Indian market cost modeling (STT, brokerage, slippage, GST).

Supports both LONG (buy options) and SHORT (sell options) trades.
Uses a regime detector to decide which mode to use.

CRITICAL ASSUMPTION:
  This backtests on the UNDERLYING INDEX price, not actual option premiums.
  Option P&L is approximated as delta-1 movement. In reality:
  - ATM options have ~0.5 delta, not 1.0
  - Far OTM options have much lower delta
  - Theta eats into buy-side P&L every minute
  - IV crush after events can destroy premium regardless of direction

  This means backtest results are OPTIMISTIC for option buyers and
  PESSIMISTIC for option sellers. Real-world results will differ.
  Use this as a directional signal filter, not a P&L predictor.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class CostModel:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.0625
    exchange_txn_pct: float = 0.053
    sebi_per_crore: float = 10.0
    gst_pct: float = 18.0
    stamp_duty_pct: float = 0.003
    slippage_ticks: int = 2
    tick_size: float = 0.05

    def compute_round_trip(self, entry_price: float, exit_price: float,
                           quantity: int) -> float:
        buy_value = entry_price * quantity
        sell_value = exit_price * quantity
        turnover = buy_value + sell_value

        brokerage = self.brokerage_per_order * 2
        stt = sell_value * self.stt_sell_pct / 100
        exchange_txn = turnover * self.exchange_txn_pct / 100
        sebi = turnover / 1_00_00_000 * self.sebi_per_crore
        gst = (brokerage + exchange_txn + sebi) * self.gst_pct / 100
        stamp = buy_value * self.stamp_duty_pct / 100
        slippage = self.slippage_ticks * self.tick_size * quantity * 2

        return round(brokerage + stt + exchange_txn + sebi + gst + stamp + slippage, 2)


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ── Indicators ───────────────────────────────────────────────────────────

def ema(values: list[float], period: int) -> list[float]:
    result = [0.0] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def tema(values: list[float], period: int) -> list[float]:
    e1 = ema(values, period)
    e2 = ema(e1, period)
    e3 = ema(e2, period)
    return [3 * a - 3 * b + c for a, b, c in zip(e1, e2, e3)]


def atr(candles: list[Candle], period: int) -> list[float]:
    trs: list[float] = [0.0]
    for i in range(1, len(candles)):
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        trs.append(tr)
    return ema(trs, period)


def supertrend(candles: list[Candle], period: int, multiplier: float
               ) -> tuple[list[float], list[int]]:
    atr_vals = atr(candles, period)
    n = len(candles)
    upper = [0.0] * n
    lower = [0.0] * n
    st_val = [0.0] * n
    st_dir = [0] * n

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


def adx(candles: list[Candle], period: int) -> list[float]:
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

    smooth_tr = ema(tr_list, period)
    smooth_pdm = ema(plus_dm, period)
    smooth_mdm = ema(minus_dm, period)

    dx = [0.0] * n
    for i in range(n):
        if smooth_tr[i] == 0:
            continue
        pdi = smooth_pdm[i] / smooth_tr[i] * 100
        mdi = smooth_mdm[i] / smooth_tr[i] * 100
        denom = pdi + mdi
        if denom > 0:
            dx[i] = abs(pdi - mdi) / denom * 100

    return ema(dx, period)


def vwap_daily(candles: list[Candle]) -> list[float]:
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


# ── Trade & Results ──────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    entry_time: datetime
    exit_time: datetime
    side: str                   # CE or PE
    direction: str              # LONG or SHORT
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str


@dataclass
class BacktestResults:
    trades: list[BacktestTrade] = field(default_factory=list)
    initial_capital: float = 100_000
    cost_model: CostModel = field(default_factory=CostModel)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def long_trades(self) -> int:
        return sum(1 for t in self.trades if t.direction == "LONG")

    @property
    def short_trades(self) -> int:
        return sum(1 for t in self.trades if t.direction == "SHORT")

    @property
    def winners(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl > 0)

    @property
    def losers(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl <= 0)

    @property
    def win_rate(self) -> float:
        return (self.winners / self.total_trades * 100) if self.total_trades else 0

    @property
    def long_win_rate(self) -> float:
        longs = [t for t in self.trades if t.direction == "LONG"]
        if not longs:
            return 0
        return sum(1 for t in longs if t.net_pnl > 0) / len(longs) * 100

    @property
    def short_win_rate(self) -> float:
        shorts = [t for t in self.trades if t.direction == "SHORT"]
        if not shorts:
            return 0
        return sum(1 for t in shorts if t.net_pnl > 0) / len(shorts) * 100

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def total_costs(self) -> float:
        return sum(t.costs for t in self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def avg_winner(self) -> float:
        wins = [t.net_pnl for t in self.trades if t.net_pnl > 0]
        return sum(wins) / len(wins) if wins else 0

    @property
    def avg_loser(self) -> float:
        losses = [t.net_pnl for t in self.trades if t.net_pnl <= 0]
        return sum(losses) / len(losses) if losses else 0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.net_pnl for t in self.trades if t.net_pnl > 0)
        gross_loss = abs(sum(t.net_pnl for t in self.trades if t.net_pnl <= 0))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown(self) -> float:
        equity = self.initial_capital
        peak = equity
        max_dd = 0.0
        for t in self.trades:
            equity += t.net_pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd

    @property
    def max_drawdown_pct(self) -> float:
        equity = self.initial_capital
        peak = equity
        max_dd_pct = 0.0
        for t in self.trades:
            equity += t.net_pnl
            peak = max(peak, equity)
            dd_pct = (peak - equity) / peak * 100 if peak > 0 else 0
            max_dd_pct = max(max_dd_pct, dd_pct)
        return max_dd_pct

    @property
    def sharpe_ratio(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        returns = [t.net_pnl for t in self.trades]
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std = variance ** 0.5
        if std == 0:
            return 0.0
        return (mean_r / std) * (250 ** 0.5)

    @property
    def max_consecutive_losses(self) -> int:
        max_streak = current = 0
        for t in self.trades:
            if t.net_pnl <= 0:
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    @property
    def equity_curve(self) -> list[float]:
        curve = [self.initial_capital]
        for t in self.trades:
            curve.append(curve[-1] + t.net_pnl)
        return curve

    def print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("  OMEGA BACKTEST RESULTS")
        print("=" * 60)
        print(f"  Total Trades:          {self.total_trades}")
        print(f"    Long (Buy):          {self.long_trades} "
              f"(WR {self.long_win_rate:.1f}%)")
        print(f"    Short (Sell):        {self.short_trades} "
              f"(WR {self.short_win_rate:.1f}%)")
        print(f"  Winners:               {self.winners} ({self.win_rate:.1f}%)")
        print(f"  Losers:                {self.losers}")
        print(f"  Max Consec. Losses:    {self.max_consecutive_losses}")
        print("-" * 60)
        print(f"  Gross P&L:             ₹{self.gross_pnl:>12,.2f}")
        print(f"  Total Costs:           ₹{self.total_costs:>12,.2f}")
        print(f"  Net P&L:               ₹{self.net_pnl:>12,.2f}")
        print("-" * 60)
        print(f"  Avg Winner:            ₹{self.avg_winner:>12,.2f}")
        print(f"  Avg Loser:             ₹{self.avg_loser:>12,.2f}")
        print(f"  Profit Factor:         {self.profit_factor:>12.2f}")
        print(f"  Sharpe Ratio:          {self.sharpe_ratio:>12.2f}")
        print("-" * 60)
        print(f"  Max Drawdown:          ₹{self.max_drawdown:>12,.2f}")
        print(f"  Max Drawdown %:        {self.max_drawdown_pct:>11.2f}%")
        print(f"  Final Equity:          ₹{self.equity_curve[-1]:>12,.2f}")
        print("=" * 60 + "\n")

    def to_csv(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "entry_time", "exit_time", "side", "direction",
                "entry_price", "exit_price", "quantity",
                "gross_pnl", "costs", "net_pnl", "exit_reason",
            ])
            for t in self.trades:
                writer.writerow([
                    t.entry_time.isoformat(), t.exit_time.isoformat(),
                    t.side, t.direction, t.entry_price, t.exit_price,
                    t.quantity, t.gross_pnl, t.costs, t.net_pnl, t.exit_reason,
                ])


# ── Strategy Params ──────────────────────────────────────────────────────

@dataclass
class StrategyParams:
    # Trend
    st_period: int = 10
    st_multiplier: float = 3.0
    tema_length: int = 21
    ema_length: int = 200

    # ADX regime thresholds
    adx_length: int = 14
    adx_buy_threshold: int = 25     # above this = trending → BUY options
    adx_sell_threshold: int = 20    # below this = ranging → SELL options

    # Trailing stop
    chandelier_length: int = 22
    chandelier_mult: float = 3.0

    # Risk
    sl_buffer_pct: float = 2.0
    session_start: dt_time = field(default_factory=lambda: dt_time(9, 16))
    session_end: dt_time = field(default_factory=lambda: dt_time(15, 15))
    lot_size: int = 15
    num_lots: int = 1
    max_daily_loss: float = 2000
    max_trades_per_day: int = 5

    # Mode control
    enable_buying: bool = True
    enable_selling: bool = True


# ── Backtest Engine ──────────────────────────────────────────────────────

class BacktestEngine:
    def __init__(self, candles: list[Candle], cost_model: CostModel | None = None,
                 initial_capital: float = 100_000):
        self.candles = candles
        self.cost_model = cost_model or CostModel()
        self.initial_capital = initial_capital

    @classmethod
    def from_csv(cls, filepath: str, cost_config: dict | None = None,
                 initial_capital: float = 100_000) -> BacktestEngine:
        candles = []
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Data file not found: {filepath}")

        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts_str = (row.get("datetime") or row.get("timestamp")
                          or row.get("date", ""))
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                            "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                            "%Y-%m-%d"):
                    try:
                        ts = datetime.strptime(ts_str, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    continue

                candles.append(Candle(
                    timestamp=ts,
                    open=float(row.get("open", 0)),
                    high=float(row.get("high", 0)),
                    low=float(row.get("low", 0)),
                    close=float(row.get("close", 0)),
                    volume=float(row.get("volume", 0)),
                ))

        cm = CostModel(**(cost_config or {}))
        log.info("Loaded %d candles from %s (%s → %s)",
                 len(candles), filepath,
                 candles[0].timestamp if candles else "N/A",
                 candles[-1].timestamp if candles else "N/A")
        return cls(candles, cm, initial_capital)

    def run(self, params: StrategyParams | None = None) -> BacktestResults:
        p = params or StrategyParams()
        candles = self.candles
        n = len(candles)

        if n < p.ema_length + 50:
            log.warning("Not enough data: %d candles, need %d+", n, p.ema_length + 50)
            return BacktestResults(initial_capital=self.initial_capital,
                                   cost_model=self.cost_model)

        closes = [c.close for c in candles]
        st_val, st_dir = supertrend(candles, p.st_period, p.st_multiplier)
        tema_vals = tema(closes, p.tema_length)
        adx_vals = adx(candles, p.adx_length)
        ema_vals = ema(closes, p.ema_length)
        vwap_vals = vwap_daily(candles)
        atr_vals = atr(candles, p.chandelier_length)

        trades: list[BacktestTrade] = []
        quantity = p.lot_size * p.num_lots
        daily_pnl = 0.0
        trades_today = 0
        current_date = None

        in_trade = False
        trade_side = ""
        trade_direction = ""
        entry_price = 0.0
        entry_time = candles[0].timestamp
        trail_sl = 0.0
        highest_since_entry = 0.0
        lowest_since_entry = float("inf")

        warmup = max(p.ema_length, p.chandelier_length, p.adx_length * 2) + 10

        for i in range(warmup, n):
            c = candles[i]
            t = c.timestamp.time()

            if c.timestamp.date() != current_date:
                current_date = c.timestamp.date()
                daily_pnl = 0.0
                trades_today = 0

            if t < p.session_start or t > p.session_end:
                if in_trade and t > p.session_end:
                    gross = self._compute_gross(
                        entry_price, c.close, quantity, trade_direction)
                    costs = self.cost_model.compute_round_trip(
                        entry_price, c.close, quantity)
                    trades.append(BacktestTrade(
                        entry_time=entry_time, exit_time=c.timestamp,
                        side=trade_side, direction=trade_direction,
                        entry_price=entry_price, exit_price=c.close,
                        quantity=quantity, gross_pnl=round(gross, 2),
                        costs=costs, net_pnl=round(gross - costs, 2),
                        exit_reason="SQUARE_OFF",
                    ))
                    daily_pnl += gross - costs
                    in_trade = False
                continue

            if daily_pnl <= -p.max_daily_loss:
                if in_trade:
                    gross = self._compute_gross(
                        entry_price, c.close, quantity, trade_direction)
                    costs = self.cost_model.compute_round_trip(
                        entry_price, c.close, quantity)
                    trades.append(BacktestTrade(
                        entry_time=entry_time, exit_time=c.timestamp,
                        side=trade_side, direction=trade_direction,
                        entry_price=entry_price, exit_price=c.close,
                        quantity=quantity, gross_pnl=round(gross, 2),
                        costs=costs, net_pnl=round(gross - costs, 2),
                        exit_reason="DAILY_LOSS_CAP",
                    ))
                    daily_pnl += gross - costs
                    in_trade = False
                continue

            # ── Confluence signals ──────────────────────────────────────
            st_bull = st_dir[i] < 0
            st_bear = st_dir[i] > 0
            tema_bull = c.close > tema_vals[i] and tema_vals[i] > 0
            tema_bear = c.close < tema_vals[i] and tema_vals[i] > 0
            adx_strong = adx_vals[i] > p.adx_buy_threshold
            adx_weak = adx_vals[i] < p.adx_sell_threshold
            above_ema = c.close > ema_vals[i] and ema_vals[i] > 0
            below_ema = c.close < ema_vals[i] and ema_vals[i] > 0
            above_vwap = c.close > vwap_vals[i]
            below_vwap = c.close < vwap_vals[i]

            # 5-factor confluence for BUY
            buy_ce_signal = (st_bull and tema_bull and adx_strong
                             and above_ema and above_vwap)
            buy_pe_signal = (st_bear and tema_bear and adx_strong
                             and below_ema and below_vwap)

            # SELL signals: range mode + directional bias from EMA200
            sell_ce_signal = (adx_weak and below_ema and not st_bull
                              and below_vwap)
            sell_pe_signal = (adx_weak and above_ema and not st_bear
                              and above_vwap)

            # First-bar triggers (avoid re-entry on same signal)
            prev_st_bull = st_dir[i - 1] < 0
            prev_tema_bull = (candles[i - 1].close > tema_vals[i - 1]
                              and tema_vals[i - 1] > 0)
            prev_above_ema = (candles[i - 1].close > ema_vals[i - 1]
                              and ema_vals[i - 1] > 0)
            prev_buy_ce = prev_st_bull and prev_tema_bull and prev_above_ema

            prev_st_bear = st_dir[i - 1] > 0
            prev_tema_bear = (candles[i - 1].close < tema_vals[i - 1]
                              and tema_vals[i - 1] > 0)
            prev_below_ema = (candles[i - 1].close < ema_vals[i - 1]
                              and ema_vals[i - 1] > 0)
            prev_buy_pe = prev_st_bear and prev_tema_bear and prev_below_ema

            buy_ce_trigger = buy_ce_signal and not prev_buy_ce
            buy_pe_trigger = buy_pe_signal and not prev_buy_pe

            prev_adx_weak = adx_vals[i - 1] < p.adx_sell_threshold
            sell_ce_trigger = (sell_ce_signal
                               and not (prev_adx_weak and prev_below_ema))
            sell_pe_trigger = (sell_pe_signal
                               and not (prev_adx_weak and prev_above_ema))

            # ── Exit management ─────────────────────────────────────────
            if in_trade:
                if trade_direction == "LONG":
                    if trade_side == "CE":
                        highest_since_entry = max(highest_since_entry, c.high)
                        new_trail = (highest_since_entry
                                     - p.chandelier_mult * atr_vals[i])
                        trail_sl = max(trail_sl, new_trail)
                        if c.close <= trail_sl:
                            exit_price = trail_sl
                            gross = self._compute_gross(
                                entry_price, exit_price, quantity, "LONG")
                            costs = self.cost_model.compute_round_trip(
                                entry_price, exit_price, quantity)
                            trades.append(BacktestTrade(
                                entry_time=entry_time, exit_time=c.timestamp,
                                side="CE", direction="LONG",
                                entry_price=entry_price,
                                exit_price=round(exit_price, 2),
                                quantity=quantity,
                                gross_pnl=round(gross, 2), costs=costs,
                                net_pnl=round(gross - costs, 2),
                                exit_reason="TRAIL_SL",
                            ))
                            daily_pnl += gross - costs
                            trades_today += 1
                            in_trade = False
                    else:
                        lowest_since_entry = min(lowest_since_entry, c.low)
                        new_trail = (lowest_since_entry
                                     + p.chandelier_mult * atr_vals[i])
                        trail_sl = min(trail_sl, new_trail)
                        if c.close >= trail_sl:
                            exit_price = trail_sl
                            gross = self._compute_gross(
                                entry_price, exit_price, quantity, "LONG")
                            costs = self.cost_model.compute_round_trip(
                                entry_price, exit_price, quantity)
                            trades.append(BacktestTrade(
                                entry_time=entry_time, exit_time=c.timestamp,
                                side="PE", direction="LONG",
                                entry_price=entry_price,
                                exit_price=round(exit_price, 2),
                                quantity=quantity,
                                gross_pnl=round(gross, 2), costs=costs,
                                net_pnl=round(gross - costs, 2),
                                exit_reason="TRAIL_SL",
                            ))
                            daily_pnl += gross - costs
                            trades_today += 1
                            in_trade = False

                elif trade_direction == "SHORT":
                    # Short exit: regime shifts to trending against position
                    exit_short = False
                    if trade_side == "CE" and adx_strong and st_bull:
                        exit_short = True
                    elif trade_side == "PE" and adx_strong and st_bear:
                        exit_short = True

                    # Also exit if price moves too far against (2x ATR)
                    if trade_side == "CE":
                        if c.close > entry_price + 2 * atr_vals[i] * p.chandelier_mult:
                            exit_short = True
                    else:
                        if c.close < entry_price - 2 * atr_vals[i] * p.chandelier_mult:
                            exit_short = True

                    if exit_short:
                        gross = self._compute_gross(
                            entry_price, c.close, quantity, "SHORT")
                        costs = self.cost_model.compute_round_trip(
                            entry_price, c.close, quantity)
                        trades.append(BacktestTrade(
                            entry_time=entry_time, exit_time=c.timestamp,
                            side=trade_side, direction="SHORT",
                            entry_price=entry_price, exit_price=c.close,
                            quantity=quantity, gross_pnl=round(gross, 2),
                            costs=costs, net_pnl=round(gross - costs, 2),
                            exit_reason="REGIME_SHIFT",
                        ))
                        daily_pnl += gross - costs
                        trades_today += 1
                        in_trade = False

            # ── New entries ─────────────────────────────────────────────
            if not in_trade and trades_today < p.max_trades_per_day:
                entered = False

                # BUY entries (trending market)
                if p.enable_buying and not entered:
                    if buy_ce_trigger:
                        in_trade = True
                        entered = True
                        trade_side = "CE"
                        trade_direction = "LONG"
                        entry_price = c.close
                        entry_time = c.timestamp
                        highest_since_entry = c.high
                        trail_sl = c.close - p.chandelier_mult * atr_vals[i]
                    elif buy_pe_trigger:
                        in_trade = True
                        entered = True
                        trade_side = "PE"
                        trade_direction = "LONG"
                        entry_price = c.close
                        entry_time = c.timestamp
                        lowest_since_entry = c.low
                        trail_sl = c.close + p.chandelier_mult * atr_vals[i]

                # SELL entries (ranging market)
                if p.enable_selling and not entered:
                    if sell_ce_trigger:
                        in_trade = True
                        trade_side = "CE"
                        trade_direction = "SHORT"
                        entry_price = c.close
                        entry_time = c.timestamp
                    elif sell_pe_trigger:
                        in_trade = True
                        trade_side = "PE"
                        trade_direction = "SHORT"
                        entry_price = c.close
                        entry_time = c.timestamp

        results = BacktestResults(
            trades=trades,
            initial_capital=self.initial_capital,
            cost_model=self.cost_model,
        )
        log.info("Backtest: %d trades (%d long, %d short), net P&L ₹%.2f",
                 results.total_trades, results.long_trades,
                 results.short_trades, results.net_pnl)
        return results

    @staticmethod
    def _compute_gross(entry: float, exit: float, qty: int,
                       direction: str) -> float:
        if direction == "LONG":
            return (exit - entry) * qty
        return (entry - exit) * qty
