from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

log = logging.getLogger(__name__)


class TelegramAlert:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None,
                 enabled: bool = True):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.bot_token) and bool(self.chat_id)
        self._url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        try:
            requests.post(self._url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "Markdown",
            }, timeout=5)
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)

    def order_filled(self, side: str, symbol: str, qty: int, price: float,
                     order_id: str) -> None:
        self.send(
            f"✅ *ORDER FILLED*\n"
            f"  {side} {symbol}\n"
            f"  Qty: {qty} | Price: ₹{price:.2f}\n"
            f"  ID: `{order_id}`"
        )

    def sl_hit(self, symbol: str, pnl: float) -> None:
        emoji = "🔴" if pnl < 0 else "🟢"
        self.send(
            f"{emoji} *STOP LOSS HIT*\n"
            f"  {symbol}\n"
            f"  P&L: ₹{pnl:+,.2f}"
        )

    def daily_summary(self, total_pnl: float, trades: int, wins: int, losses: int) -> None:
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        wr = (wins / trades * 100) if trades > 0 else 0
        self.send(
            f"{emoji} *EOD SUMMARY* — {datetime.now().strftime('%d %b %Y')}\n"
            f"  P&L: ₹{total_pnl:+,.2f}\n"
            f"  Trades: {trades} | Win Rate: {wr:.0f}%\n"
            f"  Wins: {wins} | Losses: {losses}"
        )

    def error(self, context: str, exc: Exception) -> None:
        self.send(
            f"🚨 *ERROR*\n"
            f"  Context: {context}\n"
            f"  {type(exc).__name__}: {exc}"
        )

    def bot_started(self, mode: str, strategy: str) -> None:
        self.send(
            f"🤖 *BOT STARTED*\n"
            f"  Mode: {mode.upper()}\n"
            f"  Strategy: {strategy}\n"
            f"  Time: {datetime.now().strftime('%H:%M:%S IST')}"
        )

    def bot_stopped(self, reason: str) -> None:
        self.send(
            f"⛔ *BOT STOPPED*\n"
            f"  Reason: {reason}\n"
            f"  Time: {datetime.now().strftime('%H:%M:%S IST')}"
        )
