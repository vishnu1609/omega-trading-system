"""
Omega Trade Analytics Module
Reads trade history from omega_memory.json + omega.db, computes comprehensive stats.
"""

import json
import sqlite3
import os
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MEMORY_PATH = BASE_DIR / "data" / "omega_memory.json"
DB_PATH = BASE_DIR / "data" / "omega.db"
REPORT_PATH = BASE_DIR / "logs" / "analytics_report.txt"

SCORE_BUCKETS = [(0, 40), (40, 50), (50, 60), (60, 70), (70, 200)]
SCORE_LABELS = ["0-40", "40-50", "50-60", "60-70", "70+"]


def _load_memory_trades() -> list[dict]:
    if not MEMORY_PATH.exists():
        return []
    with open(MEMORY_PATH) as f:
        data = json.load(f)
    return data.get("trades", [])


def _load_db_trades() -> list[dict]:
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades").fetchall()
        conn.close()
        trades = []
        for r in rows:
            d = dict(r)
            # Map DB columns to memory format where missing
            d.setdefault("pnl_pct", 0)
            d.setdefault("entry_hour", _parse_hour(d.get("entry_time", "")))
            d.setdefault("entry_day", _parse_day(d.get("entry_time", "")))
            d.setdefault("hold_minutes", _calc_hold(d.get("entry_time", ""), d.get("exit_time", "")))
            d.setdefault("signal_score", 0)
            d.setdefault("signal_confidence", "UNKNOWN")
            d.setdefault("adx_value", 0)
            d.setdefault("vwap_position", "UNKNOWN")
            d.setdefault("supertrend_dir", "UNKNOWN")
            d.setdefault("volume_ratio", 1.0)
            d.setdefault("atr_value", 0)
            d.setdefault("is_win", d.get("pnl", 0) > 0)
            d.setdefault("r_multiple", 0)
            d.setdefault("volatility_regime", "UNKNOWN")
            trades.append(d)
        return trades
    except Exception:
        return []


def _parse_hour(ts: str) -> int:
    try:
        return datetime.fromisoformat(ts).hour
    except Exception:
        return 0


def _parse_day(ts: str) -> int:
    try:
        return datetime.fromisoformat(ts).weekday()
    except Exception:
        return 0


def _calc_hold(entry: str, exit_: str) -> float:
    try:
        e = datetime.fromisoformat(entry)
        x = datetime.fromisoformat(exit_)
        return (x - e).total_seconds() / 60
    except Exception:
        return 0


def _symbol_prefix(symbol: str) -> str:
    s = symbol.upper()
    if s.startswith("BANKNIFTY"):
        return "BANKNIFTY"
    if s.startswith("NIFTY"):
        return "NIFTY"
    if s.startswith("SENSEX"):
        return "SENSEX"
    return "OTHER"


def load_trades() -> list[dict]:
    """Load and deduplicate trades from memory + DB."""
    mem = _load_memory_trades()
    db = _load_db_trades()
    seen = set()
    merged = []
    for t in mem:
        key = t.get("entry_time", "")
        if key not in seen:
            seen.add(key)
            merged.append(t)
    for t in db:
        key = t.get("entry_time", "")
        if key not in seen:
            seen.add(key)
            merged.append(t)
    return merged


def _group_stats(trades: list[dict], key_fn) -> dict:
    groups = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    result = {}
    for k, ts in sorted(groups.items(), key=lambda x: str(x[0])):
        wins = [t for t in ts if t.get("is_win")]
        total_pnl = sum(t.get("pnl", 0) for t in ts)
        result[k] = {
            "trades": len(ts),
            "win_rate": round(len(wins) / len(ts) * 100, 1) if ts else 0,
            "pnl": round(total_pnl, 2),
        }
    return result


def _score_bucket(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "0-40"
    for (lo, hi), label in zip(SCORE_BUCKETS, SCORE_LABELS):
        if lo <= s < hi:
            return label
    return "70+"


def compute_analytics(trades: list[dict] | None = None) -> dict:
    if trades is None:
        trades = load_trades()

    if not trades:
        return {"error": "No trades found", "total_trades": 0}

    wins = [t for t in trades if t.get("is_win")]
    losses = [t for t in trades if not t.get("is_win")]
    pnls = [t.get("pnl", 0) for t in trades]
    win_pnls = [t.get("pnl", 0) for t in wins]
    loss_pnls = [t.get("pnl", 0) for t in losses]

    total = len(trades)
    win_rate = round(len(wins) / total * 100, 1)
    avg_win = round(sum(win_pnls) / len(wins), 2) if wins else 0
    avg_loss = round(sum(loss_pnls) / len(losses), 2) if losses else 0
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")
    expectancy = round(sum(pnls) / total, 2)

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    # Sharpe-like: mean(pnl) / std(pnl)
    mean_pnl = sum(pnls) / total
    variance = sum((p - mean_pnl) ** 2 for p in pnls) / total if total > 1 else 0
    std_pnl = math.sqrt(variance)
    sharpe = round(mean_pnl / std_pnl, 3) if std_pnl > 0 else 0

    # Streaks
    current_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    ws = ls = 0
    for t in trades:
        if t.get("is_win"):
            ws += 1
            ls = 0
        else:
            ls += 1
            ws = 0
        max_win_streak = max(max_win_streak, ws)
        max_loss_streak = max(max_loss_streak, ls)
    # Current streak
    if trades:
        last_win = trades[-1].get("is_win")
        cs = 0
        for t in reversed(trades):
            if t.get("is_win") == last_win:
                cs += 1
            else:
                break
        current_streak = cs if last_win else -cs

    # R-multiple
    win_r = [t.get("r_multiple", 0) for t in wins if t.get("r_multiple")]
    loss_r = [t.get("r_multiple", 0) for t in losses if t.get("r_multiple")]
    avg_r_win = round(sum(win_r) / len(win_r), 2) if win_r else 0
    avg_r_loss = round(sum(loss_r) / len(loss_r), 2) if loss_r else 0

    # By exit reason
    exit_groups = defaultdict(list)
    for t in trades:
        exit_groups[t.get("exit_reason", "UNKNOWN")].append(t.get("pnl", 0))
    by_exit = {}
    for reason, pnl_list in sorted(exit_groups.items()):
        by_exit[reason] = {
            "count": len(pnl_list),
            "avg_pnl": round(sum(pnl_list) / len(pnl_list), 2),
        }

    return {
        "overall": {
            "total_trades": total,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": expectancy,
            "max_drawdown": max_dd,
            "sharpe_ratio": sharpe,
            "total_pnl": round(sum(pnls), 2),
        },
        "by_hour": _group_stats(trades, lambda t: t.get("entry_hour", 0)),
        "by_confidence": _group_stats(trades, lambda t: t.get("signal_confidence", "UNKNOWN")),
        "by_volatility_regime": _group_stats(trades, lambda t: t.get("volatility_regime", "UNKNOWN")),
        "by_symbol": _group_stats(trades, lambda t: _symbol_prefix(t.get("symbol", ""))),
        "by_exit_reason": by_exit,
        "by_score_bucket": _group_stats(trades, lambda t: _score_bucket(t.get("signal_score", 0))),
        "streaks": {
            "current": current_streak,
            "max_win": max_win_streak,
            "max_loss": max_loss_streak,
        },
        "r_multiple": {
            "avg_r_win": avg_r_win,
            "avg_r_loss": avg_r_loss,
        },
    }


def get_edge_summary(analytics: dict | None = None) -> dict:
    """Top 3 profitable and top 3 unprofitable filters across hour/confidence/regime."""
    if analytics is None:
        analytics = compute_analytics()

    if analytics.get("error"):
        return {"profitable": [], "unprofitable": []}

    all_filters = []
    for dim_key, dim_label in [
        ("by_hour", "hour"),
        ("by_confidence", "confidence"),
        ("by_volatility_regime", "regime"),
    ]:
        dim = analytics.get(dim_key, {})
        for val, stats in dim.items():
            if stats["trades"] >= 2:  # need at least 2 trades
                all_filters.append({
                    "filter": f"{dim_label}={val}",
                    "trades": stats["trades"],
                    "win_rate": stats["win_rate"],
                    "pnl": stats["pnl"],
                })

    by_pnl = sorted(all_filters, key=lambda x: x["pnl"], reverse=True)
    return {
        "profitable": by_pnl[:3],
        "unprofitable": by_pnl[-3:][::-1] if len(by_pnl) >= 3 else by_pnl[::-1],
    }


def generate_report(analytics: dict | None = None) -> str:
    if analytics is None:
        analytics = compute_analytics()

    if analytics.get("error"):
        return f"No trades to analyze: {analytics.get('error')}"

    o = analytics["overall"]
    lines = [
        "=" * 60,
        "  OMEGA TRADE ANALYTICS REPORT",
        f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        "",
        "--- OVERALL ---",
        f"  Total Trades:    {o['total_trades']}",
        f"  Win Rate:        {o['win_rate']}%",
        f"  Profit Factor:   {o['profit_factor']}",
        f"  Avg Win:         {o['avg_win']:+.2f}",
        f"  Avg Loss:        {o['avg_loss']:+.2f}",
        f"  Expectancy:      {o['expectancy']:+.2f} per trade",
        f"  Max Drawdown:    {o['max_drawdown']:.2f}",
        f"  Sharpe Ratio:    {o['sharpe_ratio']}",
        f"  Total PnL:       {o['total_pnl']:+.2f}",
        "",
    ]

    # Streaks
    s = analytics["streaks"]
    lines += [
        "--- STREAKS ---",
        f"  Current:         {'W' if s['current'] > 0 else 'L'}{abs(s['current'])}",
        f"  Max Win Streak:  {s['max_win']}",
        f"  Max Loss Streak: {s['max_loss']}",
        "",
    ]

    # R-multiple
    r = analytics["r_multiple"]
    lines += [
        "--- R-MULTIPLE ---",
        f"  Avg R (wins):    {r['avg_r_win']:+.2f}",
        f"  Avg R (losses):  {r['avg_r_loss']:+.2f}",
        "",
    ]

    # Group tables
    def _table(title, data, extra_col=None):
        lines = [f"--- {title} ---"]
        for k, v in data.items():
            if extra_col:
                lines.append(f"  {str(k):>15s}  |  {extra_col(v)}")
            elif isinstance(v, dict) and "trades" in v:
                lines.append(f"  {str(k):>15s}  |  N={v['trades']:>3d}  WR={v['win_rate']:>5.1f}%  PnL={v['pnl']:>+10.2f}")
            elif isinstance(v, dict) and "count" in v:
                lines.append(f"  {str(k):>15s}  |  N={v['count']:>3d}  Avg PnL={v['avg_pnl']:>+10.2f}")
        lines.append("")
        return lines

    lines += _table("BY HOUR", analytics["by_hour"])
    lines += _table("BY CONFIDENCE", analytics["by_confidence"])
    lines += _table("BY VOLATILITY REGIME", analytics["by_volatility_regime"])
    lines += _table("BY SYMBOL", analytics["by_symbol"])
    lines += _table("BY EXIT REASON", analytics["by_exit_reason"])
    lines += _table("BY SCORE BUCKET", analytics["by_score_bucket"])

    # Edge summary
    edge = get_edge_summary(analytics)
    lines += ["--- EDGE SUMMARY ---", "  Top Profitable Filters:"]
    for e in edge["profitable"]:
        lines.append(f"    {e['filter']:>25s}  N={e['trades']}  WR={e['win_rate']}%  PnL={e['pnl']:+.2f}")
    lines += ["  Top Unprofitable Filters:"]
    for e in edge["unprofitable"]:
        lines.append(f"    {e['filter']:>25s}  N={e['trades']}  WR={e['win_rate']}%  PnL={e['pnl']:+.2f}")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    trades = load_trades()
    print(f"Loaded {len(trades)} trades")
    analytics = compute_analytics(trades)
    report = generate_report(analytics)
    print(report)

    # Save
    os.makedirs(REPORT_PATH.parent, exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write(report)
    print(f"Report saved to {REPORT_PATH}")
