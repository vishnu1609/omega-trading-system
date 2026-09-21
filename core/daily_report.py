"""
Daily Results Report — Separate P&L by category.

Categories:
  - INDICES: NIFTY, BANKNIFTY, FINNIFTY, SENSEX, BANKEX
  - FUTURES: NIFTY_FUT, BANKNIFTY_FUT
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

CATEGORY_MAP = {
    "NIFTY": "INDICES",
    "BANKNIFTY": "INDICES",
    "FINNIFTY": "INDICES",
    "SENSEX": "INDICES",
    "BANKEX": "INDICES",
    "NIFTY_FUT": "INDEX_FUTURES",
    "BANKNIFTY_FUT": "INDEX_FUTURES",
}


def _categorize(symbol: str) -> str:
    upper = symbol.upper()
    for name, cat in sorted(CATEGORY_MAP.items(), key=lambda x: -len(x[0])):
        if name in upper:
            return cat
    return "OTHER"


def generate_daily_report(closed_trades, signals_log: list[dict],
                          realised_pnl: float) -> str:
    categories: dict[str, dict] = {}

    for trade in closed_trades:
        cat = _categorize(trade.symbol)
        if cat not in categories:
            categories[cat] = {"trades": 0, "wins": 0, "losses": 0,
                               "pnl": 0.0, "details": []}
        c = categories[cat]
        c["trades"] += 1
        c["pnl"] += trade.pnl
        if trade.pnl >= 0:
            c["wins"] += 1
        else:
            c["losses"] += 1
        c["details"].append({
            "symbol": trade.symbol,
            "direction": trade.direction,
            "entry": trade.entry_price,
            "exit": trade.exit_price,
            "pnl": trade.pnl,
            "reason": trade.exit_reason,
        })

    for sig in signals_log:
        cat = _categorize(sig.get("symbol", ""))
        if cat not in categories:
            categories[cat] = {"trades": 0, "wins": 0, "losses": 0,
                               "pnl": 0.0, "details": []}
        if "signals" not in categories[cat]:
            categories[cat]["signals"] = 0
        categories[cat]["signals"] = categories[cat].get("signals", 0) + 1

    today = datetime.now(IST).strftime("%Y-%m-%d")
    lines = [
        f"{'='*60}",
        f"  OMEGA DAILY REPORT — {today}",
        f"{'='*60}",
        "",
    ]

    if not categories:
        lines.append("  No trades or signals today.")
        lines.append("")

    for cat in ["INDICES", "INDEX_FUTURES", "OTHER"]:
        if cat not in categories:
            continue
        c = categories[cat]
        win_rate = (c["wins"] / c["trades"] * 100) if c["trades"] > 0 else 0
        lines.append(f"  [{cat}]")
        lines.append(f"  Trades: {c['trades']}  |  W/L: {c['wins']}/{c['losses']}  |  Win Rate: {win_rate:.0f}%")
        lines.append(f"  P&L: ₹{c['pnl']:,.2f}")
        signals = c.get("signals", 0)
        if signals:
            lines.append(f"  Signals generated: {signals}")
        for d in c["details"]:
            lines.append(f"    {d['direction']} {d['symbol']}: ₹{d['entry']:.2f} → ₹{d['exit']:.2f} = ₹{d['pnl']:+,.2f} ({d['reason']})")
        lines.append("")

    lines.append(f"  TOTAL P&L: ₹{realised_pnl:,.2f}")
    lines.append(f"{'='*60}")

    report = "\n".join(lines)
    return report


def save_daily_report(report: str, closed_trades, signals_log: list[dict]):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    report_dir = Path(__file__).parent.parent / "logs" / "daily_reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    report_file = report_dir / f"{today}.txt"
    report_file.write_text(report)

    data = {
        "date": today,
        "categories": {},
        "total_pnl": sum(t.pnl for t in closed_trades),
        "total_trades": len(closed_trades),
    }

    for trade in closed_trades:
        cat = _categorize(trade.symbol)
        if cat not in data["categories"]:
            data["categories"][cat] = {"trades": 0, "wins": 0, "pnl": 0.0}
        data["categories"][cat]["trades"] += 1
        data["categories"][cat]["pnl"] += trade.pnl
        if trade.pnl >= 0:
            data["categories"][cat]["wins"] += 1

    json_file = report_dir / f"{today}.json"
    json_file.write_text(json.dumps(data, indent=2))

    log.info("Daily report saved: %s", report_file)
