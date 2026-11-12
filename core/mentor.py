"""
Trade Mentor — Reviews trades and recommends improvements.

Standalone: python -I core/mentor.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DB_PATH = Path(__file__).parent.parent / "data" / "omega.db"


def _conn(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class TradeReviewer:
    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = db_path

    def _q(self, sql: str, params: tuple = ()) -> list[dict]:
        with _conn(self.db_path) as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def _q1(self, sql: str, params: tuple = ()) -> dict | None:
        with _conn(self.db_path) as c:
            r = c.execute(sql, params).fetchone()
            return dict(r) if r else None

    # ── Core analysis ─────────────────────────────────────────────

    def review_session(self, date: str) -> dict:
        trades = self._q(
            "SELECT * FROM trades WHERE date(created_at)=? ORDER BY id", (date,))
        if not trades:
            return {"date": date, "trades": 0, "message": "No trades"}

        wins = [t for t in trades if t["pnl"] >= 0]
        losses = [t for t in trades if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in trades)
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(abs(t["pnl"]) for t in losses) / len(losses) if losses else 0
        profit_factor = (sum(t["pnl"] for t in wins) / sum(abs(t["pnl"]) for t in losses)
                         if losses and sum(abs(t["pnl"]) for t in losses) > 0 else float("inf"))

        # Regime alignment — check if option type matches regime direction
        # DB stores legacy regime (TRENDING/HIGH_VOL/CHOPPY) not ensemble direction,
        # so we infer from option_type: CE in TRENDING = neutral (could be up or down),
        # and cross-reference with supertrend_dir for direction
        aligned, contra = 0, 0
        for t in trades:
            regime = (t.get("volatility_regime") or "").upper()
            opt_type = (t.get("option_type") or "").upper()
            st_dir = (t.get("supertrend_dir") or "").upper()

            if "TREND" in regime:
                is_bullish_trade = opt_type == "CE"
                is_bearish_trend = st_dir == "BEARISH" or "DOWN" in regime
                is_bullish_trend = st_dir == "BULLISH" or "UP" in regime
                if (is_bullish_trade and is_bearish_trend) or (not is_bullish_trade and is_bullish_trend):
                    contra += 1
                elif is_bullish_trend or is_bearish_trend:
                    aligned += 1
                else:
                    aligned += 1  # can't determine direction
            elif regime in ("CHOPPY", "RANGING"):
                aligned += 1
            else:
                aligned += 1

        # Time analysis
        hour_pnl: dict[int, list[float]] = {}
        for t in trades:
            try:
                h = datetime.fromisoformat(t["entry_time"]).hour
            except Exception:
                h = 0
            hour_pnl.setdefault(h, []).append(t["pnl"])

        # Hold time analysis
        hold_wins = [t["hold_minutes"] for t in wins if t.get("hold_minutes")]
        hold_losses = [t["hold_minutes"] for t in losses if t.get("hold_minutes")]

        # Peak distance (R-multiple at exit tells how much was given back)
        r_mults = [t["r_multiple"] for t in trades if t.get("r_multiple")]

        return {
            "date": date,
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100,
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "regime_aligned": aligned,
            "regime_contra": contra,
            "hour_pnl": {h: {"count": len(v), "pnl": round(sum(v), 2),
                             "win_rate": round(sum(1 for x in v if x >= 0) / len(v) * 100, 1)}
                         for h, v in sorted(hour_pnl.items())},
            "avg_hold_win_min": round(sum(hold_wins) / len(hold_wins), 1) if hold_wins else 0,
            "avg_hold_loss_min": round(sum(hold_losses) / len(hold_losses), 1) if hold_losses else 0,
            "avg_r_multiple": round(sum(r_mults) / len(r_mults), 2) if r_mults else 0,
        }

    def get_regime_alignment_stats(self, days: int = 7) -> dict:
        cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
        trades = self._q(
            "SELECT direction, pnl, volatility_regime, signal_score, "
            "signal_confidence, option_type, supertrend_dir "
            "FROM trades WHERE date(created_at) >= ?", (cutoff,))

        with_regime = {"total": 0, "wins": 0, "pnl": 0.0}
        against_regime = {"total": 0, "wins": 0, "pnl": 0.0}

        for t in trades:
            regime = (t.get("volatility_regime") or "").upper()
            opt_type = (t.get("option_type") or "").upper()
            st_dir = (t.get("supertrend_dir") or "").upper()

            is_contra = False
            if "TREND" in regime:
                is_bullish_trade = opt_type == "CE"
                is_bearish_trend = st_dir == "BEARISH" or "DOWN" in regime
                is_bullish_trend = st_dir == "BULLISH" or "UP" in regime
                if is_bullish_trade and is_bearish_trend:
                    is_contra = True
                elif not is_bullish_trade and is_bullish_trend:
                    is_contra = True

            bucket = against_regime if is_contra else with_regime
            bucket["total"] += 1
            bucket["pnl"] += t["pnl"]
            if t["pnl"] >= 0:
                bucket["wins"] += 1

        with_regime["win_rate"] = (with_regime["wins"] / with_regime["total"] * 100
                                   if with_regime["total"] else 0)
        against_regime["win_rate"] = (against_regime["wins"] / against_regime["total"] * 100
                                      if against_regime["total"] else 0)
        with_regime["pnl"] = round(with_regime["pnl"], 2)
        against_regime["pnl"] = round(against_regime["pnl"], 2)

        return {
            "days": days,
            "with_regime": with_regime,
            "against_regime": against_regime,
            "recommendation": self._regime_recommendation(with_regime, against_regime),
        }

    @staticmethod
    def _regime_recommendation(with_r: dict, against_r: dict) -> str:
        if against_r["total"] == 0:
            return "No contra-regime trades found — regime gate is working or insufficient data."
        if against_r["win_rate"] < 30:
            return (f"CRITICAL: Contra-regime trades win only {against_r['win_rate']:.0f}% "
                    f"(P&L: ₹{against_r['pnl']:,.0f}). "
                    "Block signals that oppose the ensemble regime direction.")
        if against_r["win_rate"] < with_r["win_rate"] - 15:
            return (f"Contra-regime win rate ({against_r['win_rate']:.0f}%) is "
                    f"{with_r['win_rate'] - against_r['win_rate']:.0f}pp below aligned trades. "
                    "Add score penalty for regime opposition.")
        return "Regime alignment shows no clear edge yet — need more data."

    def suggest_improvements(self) -> list[str]:
        stats = self._q1(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN pnl>=0 THEN 1 ELSE 0 END) as wins, "
            "AVG(CASE WHEN pnl>=0 THEN pnl END) as avg_win, "
            "AVG(CASE WHEN pnl<0 THEN ABS(pnl) END) as avg_loss, "
            "AVG(hold_minutes) as avg_hold "
            "FROM trades WHERE date(created_at) >= date('now', '-7 days')")
        if not stats or not stats["total"]:
            return ["Not enough trade data yet — need at least a few trades to analyze."]

        suggestions = []
        total = stats["total"]
        win_rate = (stats["wins"] or 0) / total * 100

        if win_rate < 40:
            suggestions.append(
                f"Win rate is {win_rate:.0f}% — raise minimum signal score from 38 to 50 "
                "and require HIGH confidence for directional trades.")
        elif win_rate < 50:
            suggestions.append(
                f"Win rate is {win_rate:.0f}% — add +5 score penalty for signals "
                "opposing ensemble regime direction.")

        avg_win = stats["avg_win"] or 0
        avg_loss = stats["avg_loss"] or 0
        if avg_loss > 0 and avg_win > 0 and avg_loss > avg_win * 1.5:
            suggestions.append(
                f"Avg loss (₹{avg_loss:,.0f}) is {avg_loss/avg_win:.1f}x avg win (₹{avg_win:,.0f}). "
                "Widen initial SL by 1-2 ATR or set tighter TP targets.")

        # Quick losers (< 5 min hold)
        quick_losses = self._q1(
            "SELECT COUNT(*) as cnt FROM trades "
            "WHERE pnl < 0 AND hold_minutes < 5 AND date(created_at) >= date('now', '-7 days')")
        total_losses = self._q1(
            "SELECT COUNT(*) as cnt FROM trades "
            "WHERE pnl < 0 AND date(created_at) >= date('now', '-7 days')")
        if quick_losses and total_losses and total_losses["cnt"] > 0:
            pct = quick_losses["cnt"] / total_losses["cnt"] * 100
            if pct > 50:
                suggestions.append(
                    f"{pct:.0f}% of losses exit within 5 min — increase SL grace period "
                    "from 180s to 300s to let trades breathe.")

        # Contra-regime losses
        regime_stats = self.get_regime_alignment_stats(days=7)
        contra = regime_stats["against_regime"]
        if contra["total"] > 0 and contra["win_rate"] < 35:
            suggestions.append(
                f"Contra-regime trades: {contra['total']} trades, "
                f"{contra['win_rate']:.0f}% win rate, ₹{contra['pnl']:,.0f} P&L. "
                "Block BUY_CE when regime=TRENDING_DOWN, BUY_PE when TRENDING_UP.")

        # Time window analysis
        hour_losses = self._q(
            "SELECT CAST(strftime('%H', entry_time) AS INTEGER) as hour, "
            "COUNT(*) as cnt, SUM(pnl) as pnl "
            "FROM trades WHERE pnl < 0 AND date(created_at) >= date('now', '-7 days') "
            "GROUP BY hour ORDER BY pnl ASC LIMIT 2")
        for h in hour_losses:
            if h["cnt"] >= 2 and h["pnl"] < -500:
                suggestions.append(
                    f"Hour {h['hour']}:00 lost ₹{abs(h['pnl']):,.0f} across {h['cnt']} trades. "
                    "Consider blocking or adding score penalty for this window.")

        if not suggestions:
            suggestions.append("No major issues detected. Keep collecting data.")

        return suggestions

    def score_signal_quality(self, days: int = 7) -> dict:
        cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")

        # Signals generated
        sig_stats = self._q1(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN signal_generated=1 THEN 1 ELSE 0 END) as with_signal "
            "FROM scans WHERE date(created_at) >= ?", (cutoff,))

        # Trades by confidence
        conf_stats = self._q(
            "SELECT signal_confidence as conf, COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl>=0 THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as pnl "
            "FROM trades WHERE date(created_at) >= ? GROUP BY signal_confidence", (cutoff,))

        # Trades by score bucket
        score_stats = self._q(
            "SELECT CASE "
            "  WHEN signal_score < 45 THEN '<45' "
            "  WHEN signal_score < 55 THEN '45-54' "
            "  WHEN signal_score < 65 THEN '55-64' "
            "  ELSE '65+' END as bucket, "
            "COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl>=0 THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as pnl "
            "FROM trades WHERE date(created_at) >= ? GROUP BY bucket", (cutoff,))

        # Find optimal threshold
        thresholds = {}
        for thr in range(35, 75, 5):
            row = self._q1(
                "SELECT COUNT(*) as cnt, "
                "SUM(CASE WHEN pnl>=0 THEN 1 ELSE 0 END) as wins, "
                "SUM(pnl) as pnl "
                "FROM trades WHERE signal_score >= ? AND date(created_at) >= ?",
                (thr, cutoff))
            if row and row["cnt"]:
                thresholds[thr] = {
                    "trades": row["cnt"],
                    "win_rate": round(row["wins"] / row["cnt"] * 100, 1),
                    "pnl": round(row["pnl"], 2),
                }

        best_thr = max(thresholds.items(), key=lambda x: x[1]["pnl"])[0] if thresholds else 50

        return {
            "days": days,
            "total_scans": sig_stats["total"] if sig_stats else 0,
            "scans_with_signal": sig_stats["with_signal"] if sig_stats else 0,
            "by_confidence": {
                r["conf"]: {
                    "trades": r["cnt"],
                    "win_rate": round(r["wins"] / r["cnt"] * 100, 1) if r["cnt"] else 0,
                    "pnl": round(r["pnl"], 2),
                } for r in conf_stats
            },
            "by_score_bucket": {
                r["bucket"]: {
                    "trades": r["cnt"],
                    "win_rate": round(r["wins"] / r["cnt"] * 100, 1) if r["cnt"] else 0,
                    "pnl": round(r["pnl"], 2),
                } for r in score_stats
            },
            "threshold_analysis": thresholds,
            "optimal_threshold": best_thr,
        }

    def daily_brief(self, date: str | None = None) -> str:
        today = date or datetime.now(IST).strftime("%Y-%m-%d")
        session = self.review_session(today)

        if session.get("trades", 0) == 0:
            return f"═══ OMEGA MENTOR — {today} ═══\nNo trades recorded today."

        # Rolling 7-day comparison
        week_stats = self._q1(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN pnl>=0 THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as pnl, AVG(hold_minutes) as avg_hold "
            "FROM trades WHERE date(created_at) >= date(?, '-7 days') AND date(created_at) < ?",
            (today, today))

        regime = self.get_regime_alignment_stats(days=7)
        suggestions = self.suggest_improvements()
        signal_q = self.score_signal_quality(days=7)

        lines = [
            f"{'═' * 56}",
            f"  OMEGA MENTOR BRIEF — {today}",
            f"{'═' * 56}",
            "",
            f"  Trades: {session['trades']}  |  W/L: {session['wins']}/{session['losses']}  |  "
            f"Win Rate: {session['win_rate']:.0f}%",
            f"  P&L: ₹{session['total_pnl']:,.2f}  |  "
            f"Avg Win: ₹{session['avg_win']:,.0f}  |  Avg Loss: ₹{session['avg_loss']:,.0f}",
            f"  Profit Factor: {session['profit_factor']:.2f}  |  "
            f"Avg R-Multiple: {session['avg_r_multiple']:.2f}",
            "",
            "  [REGIME ALIGNMENT]",
            f"  With regime: {session['regime_aligned']} trades  |  "
            f"Against: {session['regime_contra']} trades",
        ]

        if regime["against_regime"]["total"]:
            lines.append(
                f"  7-day contra-regime: {regime['against_regime']['total']} trades, "
                f"{regime['against_regime']['win_rate']:.0f}% win rate, "
                f"₹{regime['against_regime']['pnl']:,.0f}")

        lines.append("")
        lines.append("  [TIME ANALYSIS]")
        for h, data in session["hour_pnl"].items():
            icon = "+" if data["pnl"] >= 0 else "-"
            lines.append(
                f"  {h:02d}:00  {data['count']} trades  {icon}₹{abs(data['pnl']):,.0f}  "
                f"({data['win_rate']:.0f}% win)")

        lines.append("")
        lines.append("  [HOLD TIME]")
        lines.append(
            f"  Winners avg: {session['avg_hold_win_min']:.1f} min  |  "
            f"Losers avg: {session['avg_hold_loss_min']:.1f} min")

        if week_stats and week_stats["total"]:
            week_wr = (week_stats["wins"] or 0) / week_stats["total"] * 100
            lines.extend([
                "",
                "  [7-DAY ROLLING]",
                f"  Trades: {week_stats['total']}  |  Win Rate: {week_wr:.0f}%  |  "
                f"P&L: ₹{(week_stats['pnl'] or 0):,.0f}  |  "
                f"Avg Hold: {(week_stats['avg_hold'] or 0):.1f} min",
            ])

        lines.extend([
            "",
            f"  [SIGNAL QUALITY] (optimal score threshold: {signal_q['optimal_threshold']})",
            f"  Scans: {signal_q['total_scans']:,}  |  "
            f"Signals: {signal_q['scans_with_signal']:,}",
        ])
        for conf, data in signal_q["by_confidence"].items():
            lines.append(
                f"  {conf}: {data['trades']} trades, "
                f"{data['win_rate']:.0f}% win, ₹{data['pnl']:,.0f}")

        lines.extend(["", "  [TOP RECOMMENDATIONS]"])
        for i, s in enumerate(suggestions[:3], 1):
            lines.append(f"  {i}. {s}")

        lines.append(f"{'═' * 56}")
        return "\n".join(lines)


if __name__ == "__main__":
    mentor = TradeReviewer()
    print(mentor.daily_brief())
