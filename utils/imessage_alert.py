from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)


class IMessageAlert:
    def __init__(self, recipient: str | None = None, enabled: bool = True):
        self.recipient = recipient or ""
        self.enabled = enabled and bool(self.recipient)
        if self.enabled:
            log.info("iMessage alerts enabled → %s", self.recipient)

    def _escape(self, text: str) -> str:
        return (text
                .replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t"))

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        try:
            script = (
                f'tell application "Messages" to send "{self._escape(message)}" '
                f'to buddy "{self._escape(self.recipient)}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                timeout=10, capture_output=True, text=True,
            )
        except Exception as exc:
            log.warning("iMessage send failed: %s", exc)

    def order_filled(self, side: str, symbol: str, qty: int, price: float,
                     order_id: str, **kwargs) -> None:
        strike = kwargs.get("strike", "")
        expiry = kwargs.get("expiry", "")
        iv = kwargs.get("iv", 0)
        oi = kwargs.get("oi", 0)
        score = kwargs.get("score", 0)
        cost = price * qty

        header = "LONG" if "BUY" in side else "SHORT"
        opt_type = "CE" if "CE" in side else "PE"

        lines = [
            f"{'='*24}",
            f"  TRADE OPENED  {opt_type}",
            f"{'='*24}",
            f"  {symbol}",
        ]
        if expiry:
            lines.append(f"  Expiry: {expiry}")
        lines.extend([
            f"",
            f"  {header} {qty} lots @ Rs.{price:,.2f}",
            f"  Total Cost: Rs.{cost:,.2f}",
        ])
        if iv > 0:
            lines.append(f"  IV: {iv:.1f}%  |  OI: {oi:,}")
        if score > 0:
            conf = "HIGH" if score >= 70 else "MEDIUM" if score >= 50 else "LOW"
            lines.append(f"  Score: {score}/100 ({conf})")
        lines.extend([
            f"",
            f"  {datetime.now(IST).strftime('%H:%M:%S IST')}",
            f"{'='*24}",
        ])
        self.send("\n".join(lines))

    def sl_hit(self, symbol: str, pnl: float, **kwargs) -> None:
        reason = kwargs.get("reason", "")
        entry = kwargs.get("entry", 0)
        exit_price = kwargs.get("exit_price", 0)
        duration = kwargs.get("duration", "")

        tag = "PROFIT" if pnl >= 0 else "LOSS"
        emoji_line = "  WINNER" if pnl >= 0 else "  STOPPED OUT"

        lines = [
            f"{'='*24}",
            f"  TRADE CLOSED  {tag}",
            f"{'='*24}",
            f"{emoji_line}",
            f"  {symbol}",
            f"",
            f"  P&L: Rs.{pnl:+,.2f}",
        ]
        if entry > 0 and exit_price > 0:
            pct = (exit_price - entry) / entry * 100
            lines.append(f"  Entry: {entry:.2f} -> Exit: {exit_price:.2f} ({pct:+.1f}%)")
        if reason:
            lines.append(f"  Reason: {reason}")
        if duration:
            lines.append(f"  Duration: {duration}")
        lines.extend([
            f"",
            f"  {datetime.now(IST).strftime('%H:%M:%S IST')}",
            f"{'='*24}",
        ])
        self.send("\n".join(lines))

    def daily_summary(self, total_pnl: float, trades: int, wins: int,
                      losses: int, **kwargs) -> None:
        wr = (wins / trades * 100) if trades > 0 else 0
        tag = "GREEN DAY" if total_pnl >= 0 else "RED DAY"
        capital = kwargs.get("capital", 100000)
        pct = (total_pnl / capital * 100) if capital > 0 else 0

        lines = [
            f"{'='*28}",
            f"    DAILY REPORT  {tag}",
            f"{'='*28}",
            f"  {datetime.now(IST).strftime('%d %b %Y, %A')}",
            f"",
            f"  Net P&L:    Rs.{total_pnl:+,.2f} ({pct:+.2f}%)",
            f"  Trades:     {trades}",
            f"  Win Rate:   {wr:.0f}% ({wins}W / {losses}L)",
        ]
        best = kwargs.get("best_trade", "")
        worst = kwargs.get("worst_trade", "")
        if best:
            lines.append(f"  Best:       {best}")
        if worst:
            lines.append(f"  Worst:      {worst}")
        lines.extend([
            f"",
            f"{'='*28}",
        ])
        self.send("\n".join(lines))

    def weekly_summary(self, week_pnl: float, trades: int, wins: int,
                       best_trade: str = "", worst_trade: str = "") -> None:
        wr = (wins / trades * 100) if trades > 0 else 0
        lines = [
            f"{'='*28}",
            f"    WEEKLY REPORT",
            f"{'='*28}",
            f"  Week ending {datetime.now(IST).strftime('%d %b %Y')}",
            f"",
            f"  Net P&L:  Rs.{week_pnl:+,.2f}",
            f"  Trades:   {trades}  |  Win Rate: {wr:.0f}%",
        ]
        if best_trade:
            lines.append(f"  Best:     {best_trade}")
        if worst_trade:
            lines.append(f"  Worst:    {worst_trade}")
        lines.extend([f"", f"{'='*28}"])
        self.send("\n".join(lines))

    def monthly_summary(self, month_pnl: float, trades: int, wins: int,
                        top_winners: str = "", top_losers: str = "") -> None:
        wr = (wins / trades * 100) if trades > 0 else 0
        lines = [
            f"{'='*28}",
            f"    MONTHLY REPORT",
            f"{'='*28}",
            f"  {datetime.now(IST).strftime('%B %Y')}",
            f"",
            f"  Net P&L:  Rs.{month_pnl:+,.2f}",
            f"  Trades:   {trades}  |  Win Rate: {wr:.0f}%",
        ]
        if top_winners:
            lines.append(f"  Winners:  {top_winners}")
        if top_losers:
            lines.append(f"  Losers:   {top_losers}")
        lines.extend([f"", f"{'='*28}"])
        self.send("\n".join(lines))

    def brain_alert(self, alert_type: str, message: str) -> None:
        lines = [
            f"{'='*24}",
            f"  BRAIN ALERT",
            f"{'='*24}",
            f"  Type: {alert_type.upper()}",
            f"  {message}",
            f"{'='*24}",
        ]
        self.send("\n".join(lines))

    def risk_gate_blocked(self, gate_name: str, reason: str) -> None:
        lines = [
            f"  TRADE BLOCKED",
            f"  Gate: {gate_name}",
            f"  {reason}",
        ]
        self.send("\n".join(lines))

    def error(self, context: str, exc: Exception) -> None:
        lines = [
            f"  SYSTEM ALERT",
            f"  {context}",
            f"  {type(exc).__name__}: {exc}",
            f"  {datetime.now(IST).strftime('%H:%M:%S IST')}",
        ]
        self.send("\n".join(lines))

    def bot_started(self, mode: str, strategy: str) -> None:
        lines = [
            f"{'='*28}",
            f"  OMEGA TRADING COMPANY",
            f"{'='*28}",
            f"  Mode:     {mode.upper()}",
            f"  Strategy: {strategy}",
            f"  Time:     {datetime.now(IST).strftime('%H:%M:%S IST')}",
            f"  Date:     {datetime.now(IST).strftime('%d %b %Y, %A')}",
            f"{'='*28}",
        ]
        self.send("\n".join(lines))

    def bot_stopped(self, reason: str) -> None:
        lines = [
            f"{'='*28}",
            f"    OTC BOT STOPPED",
            f"{'='*28}",
            f"  Reason: {reason}",
            f"  Time:   {datetime.now(IST).strftime('%H:%M:%S IST')}",
            f"{'='*28}",
        ]
        self.send("\n".join(lines))

    def signal_detected(self, symbol: str, side: str, score: int,
                        adx: float, strike: int = 0, premium: float = 0,
                        **kw) -> None:
        conf = kw.get("confidence", "")
        regime = kw.get("regime", "")
        rsi = kw.get("rsi", 0)
        supertrend = kw.get("supertrend", "")
        vwap_rel = kw.get("vwap", "")
        chop = kw.get("choppiness", 0)
        atr = kw.get("atr", 0)
        close = kw.get("close", 0)
        mem_adj = kw.get("memory_adj", 0)
        blocked = kw.get("blocked", False)
        block_reason = kw.get("block_reason", "")
        oi = kw.get("oi", 0)
        iv = kw.get("iv", 0)

        direction = "BULL" if ("BUY" in side and "CE" in side) or ("SELL" in side and "PE" in side) else "BEAR"
        status = "BLOCKED" if blocked else "LIVE"
        conf_tag = f"  [{conf}]" if conf else ""

        lines = [
            f"{'='*28}",
            f"  SIGNAL  {side}{conf_tag}",
            f"{'='*28}",
            f"  {symbol}  |  {direction}",
            f"  Score: {score}/100  |  {status}",
        ]
        if strike:
            lines.append(f"  Strike: {strike}  |  Rs.{premium:,.2f}")
        if close:
            lines.append(f"  Index: {close:,.2f}")
        lines.append(f"")
        if adx:
            lines.append(f"  ADX {adx:.1f}  |  RSI {rsi:.1f}")
        if supertrend:
            lines.append(f"  ST: {supertrend}  |  VWAP: {vwap_rel}")
        if chop:
            lines.append(f"  Chop: {chop:.1f}  |  ATR: {atr:.2f}")
        if regime:
            lines.append(f"  Regime: {regime}")
        if iv > 0:
            lines.append(f"  IV: {iv:.1f}%  |  OI: {oi:,}")
        if mem_adj:
            lines.append(f"  Memory: {mem_adj:+.0f}")
        if blocked and block_reason:
            lines.append(f"  Reason: {block_reason}")
        lines.extend([
            f"",
            f"  {datetime.now(IST).strftime('%H:%M:%S IST')}",
            f"{'='*28}",
        ])
        self.send("\n".join(lines))

    def position_update(self, symbol: str, direction: str, entry: float,
                        current: float, pnl: float, trail_sl: float) -> None:
        pct = (current - entry) / entry * 100 if entry > 0 else 0
        lines = [
            f"  POSITION UPDATE",
            f"  {direction} {symbol}",
            f"  Entry: {entry:.2f}  |  Now: {current:.2f} ({pct:+.1f}%)",
            f"  P&L: Rs.{pnl:+,.2f}  |  Trail SL: {trail_sl:.2f}",
        ]
        self.send("\n".join(lines))
