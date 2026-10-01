from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)


class TelegramAlert:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None,
                 enabled: bool = True):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.bot_token) and bool(self.chat_id)
        self._url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        if self.enabled:
            log.info("Telegram alerts enabled → chat %s", self.chat_id)
        else:
            log.warning("Telegram alerts DISABLED — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        try:
            requests.post(self._url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
            }, timeout=5)
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)

    def order_filled(self, side: str, symbol: str, qty: int, price: float,
                     order_id: str, **kwargs) -> None:
        score = kwargs.get("score", 0)
        cost = price * qty
        header = "LONG" if "BUY" in side else "SHORT"
        conf = ""
        if score > 0:
            conf_label = "HIGH" if score >= 70 else "MEDIUM" if score >= 50 else "LOW"
            conf = f"\nScore: {score}/100 ({conf_label})"
        self.send(
            f"<b>TRADE OPENED</b>\n"
            f"{'='*24}\n"
            f"{header} {symbol}\n"
            f"Qty: {qty} @ ₹{price:,.2f}\n"
            f"Cost: ₹{cost:,.2f}{conf}\n"
            f"{'='*24}\n"
            f"{datetime.now(IST).strftime('%H:%M:%S IST')}"
        )

    def sl_hit(self, symbol: str, pnl: float, **kwargs) -> None:
        reason = kwargs.get("reason", "")
        entry = kwargs.get("entry", 0)
        exit_price = kwargs.get("exit_price", 0)
        duration = kwargs.get("duration", "")
        tag = "PROFIT" if pnl >= 0 else "LOSS"
        lines = [
            f"<b>TRADE CLOSED — {tag}</b>",
            f"{'='*24}",
            f"{symbol}",
            f"P&L: ₹{pnl:+,.2f}",
        ]
        if entry > 0 and exit_price > 0:
            pct = (exit_price - entry) / entry * 100
            lines.append(f"Entry: {entry:.2f} → Exit: {exit_price:.2f} ({pct:+.1f}%)")
        if reason:
            lines.append(f"Reason: {reason}")
        if duration:
            lines.append(f"Duration: {duration}")
        lines.extend([f"{'='*24}", f"{datetime.now(IST).strftime('%H:%M:%S IST')}"])
        self.send("\n".join(lines))

    def daily_summary(self, total_pnl: float, trades: int, wins: int,
                      losses: int, **kwargs) -> None:
        wr = (wins / trades * 100) if trades > 0 else 0
        tag = "GREEN DAY" if total_pnl >= 0 else "RED DAY"
        self.send(
            f"<b>DAILY REPORT — {tag}</b>\n"
            f"{'='*28}\n"
            f"{datetime.now(IST).strftime('%d %b %Y, %A')}\n\n"
            f"Net P&L: ₹{total_pnl:+,.2f}\n"
            f"Trades: {trades}\n"
            f"Win Rate: {wr:.0f}% ({wins}W / {losses}L)\n"
            f"{'='*28}"
        )

    def error(self, context: str, exc: Exception) -> None:
        self.send(
            f"<b>SYSTEM ALERT</b>\n"
            f"{context}\n"
            f"{type(exc).__name__}: {exc}\n"
            f"{datetime.now(IST).strftime('%H:%M:%S IST')}"
        )

    def bot_started(self, mode: str, strategy: str) -> None:
        self.send(
            f"<b>OMEGA BOT STARTED</b>\n"
            f"{'='*28}\n"
            f"Mode: {mode.upper()}\n"
            f"Strategy: {strategy}\n"
            f"{datetime.now(IST).strftime('%H:%M:%S IST | %d %b %Y')}\n"
            f"{'='*28}"
        )

    def bot_stopped(self, reason: str) -> None:
        self.send(
            f"<b>OMEGA BOT STOPPED</b>\n"
            f"{'='*28}\n"
            f"Reason: {reason}\n"
            f"{datetime.now(IST).strftime('%H:%M:%S IST')}\n"
            f"{'='*28}"
        )

    def signal_detected(self, symbol: str, side: str, score: int,
                        adx: float, strike: int = 0, premium: float = 0) -> None:
        lines = [
            f"<b>SIGNAL DETECTED</b>",
            f"{side} {symbol}",
            f"Score: {score}/100 | ADX: {adx:.1f}",
        ]
        if strike:
            lines.append(f"Strike: {strike} | Premium: ₹{premium:.2f}")
        lines.append(f"{datetime.now(IST).strftime('%H:%M:%S IST')}")
        self.send("\n".join(lines))

    def brain_alert(self, alert_type: str, message: str) -> None:
        self.send(
            f"<b>BRAIN ALERT</b>\n"
            f"Type: {alert_type.upper()}\n"
            f"{message}"
        )

    def risk_gate_blocked(self, gate_name: str, reason: str) -> None:
        self.send(f"<b>TRADE BLOCKED</b>\nGate: {gate_name}\n{reason}")

    def position_update(self, symbol: str, direction: str, entry: float,
                        current: float, pnl: float, trail_sl: float) -> None:
        pct = (current - entry) / entry * 100 if entry > 0 else 0
        self.send(
            f"<b>POSITION UPDATE</b>\n"
            f"{direction} {symbol}\n"
            f"Entry: {entry:.2f} | Now: {current:.2f} ({pct:+.1f}%)\n"
            f"P&L: ₹{pnl:+,.2f} | Trail SL: {trail_sl:.2f}"
        )

    def weekly_summary(self, week_pnl: float, trades: int, wins: int,
                       best_trade: str = "", worst_trade: str = "") -> None:
        wr = (wins / trades * 100) if trades > 0 else 0
        lines = [
            f"<b>WEEKLY REPORT</b>",
            f"Week ending {datetime.now(IST).strftime('%d %b %Y')}",
            f"Net P&L: ₹{week_pnl:+,.2f}",
            f"Trades: {trades} | Win Rate: {wr:.0f}%",
        ]
        if best_trade:
            lines.append(f"Best: {best_trade}")
        if worst_trade:
            lines.append(f"Worst: {worst_trade}")
        self.send("\n".join(lines))

    def monthly_summary(self, month_pnl: float, trades: int, wins: int,
                        top_winners: str = "", top_losers: str = "") -> None:
        wr = (wins / trades * 100) if trades > 0 else 0
        lines = [
            f"<b>MONTHLY REPORT</b>",
            f"{datetime.now(IST).strftime('%B %Y')}",
            f"Net P&L: ₹{month_pnl:+,.2f}",
            f"Trades: {trades} | Win Rate: {wr:.0f}%",
        ]
        if top_winners:
            lines.append(f"Winners: {top_winners}")
        if top_losers:
            lines.append(f"Losers: {top_losers}")
        self.send("\n".join(lines))
