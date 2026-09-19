"""
Omega Trading System — Main Entry Point

Paper mode by default. Live mode requires explicit --live flag.
Never trades real money unless you confirm twice.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
import yaml
from dotenv import load_dotenv

from core.broker_base import BrokerBase
from core.broker_dhan import DhanBroker
from core.broker_paper import PaperBroker
from core.instrument_selector import InstrumentSelector
from core.order_manager import OrderManager
from core.position_manager import PositionManager
from core.risk_engine import RiskEngine
from core.signal_generator import SignalGenerator
from strategies.omega_complete import OmegaStrategy
from utils.logger import setup_logging
from utils.telegram_alert import TelegramAlert
from webhook.tv_receiver import app, signal_queue

IST = ZoneInfo("Asia/Kolkata")
load_dotenv()


def load_config() -> tuple[dict, dict]:
    base = Path(__file__).parent / "config"
    with open(base / "settings.yaml") as f:
        settings = yaml.safe_load(f)
    with open(base / "instruments.yaml") as f:
        instruments = yaml.safe_load(f)
    return settings, instruments


def resolve_env(value: str) -> str:
    import os
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def create_broker(mode: str, broker_cfg: dict) -> BrokerBase:
    if mode == "live":
        return DhanBroker(
            client_id=resolve_env(broker_cfg.get("client_id", "")),
            access_token=resolve_env(broker_cfg.get("access_token", "")),
        )
    return PaperBroker()


def main() -> None:
    parser = argparse.ArgumentParser(description="Omega Trading System")
    parser.add_argument("--live", action="store_true",
                        help="Run in LIVE mode (real money). Requires confirmation.")
    parser.add_argument("--webhook-only", action="store_true",
                        help="Only use TradingView webhooks, no built-in signals.")
    parser.add_argument("--watch", nargs="+", default=None,
                        help="Instruments to watch (e.g. NIFTY BANKNIFTY GOLD)")
    args = parser.parse_args()

    settings, instruments_cfg = load_config()

    mode = "live" if args.live else settings.get("strategy", {}).get("mode", "paper")

    if mode == "live":
        print("\n" + "=" * 50)
        print("  WARNING: LIVE TRADING MODE")
        print("  Real money will be used for orders.")
        print("=" * 50)
        confirm = input("Type 'YES I UNDERSTAND' to continue: ")
        if confirm.strip() != "YES I UNDERSTAND":
            print("Aborted. Use paper mode for testing.")
            sys.exit(0)

    setup_logging(
        level=settings.get("logging", {}).get("level", "INFO"),
        log_file=settings.get("logging", {}).get("file", "logs/omega.log"),
    )
    log = logging.getLogger("omega.main")

    log.info("=" * 60)
    log.info("OMEGA TRADING SYSTEM — %s MODE", mode.upper())
    log.info("=" * 60)

    broker = create_broker(mode, settings.get("broker", {}))

    pm = PositionManager()

    risk_cfg = settings.get("risk", {})
    session_cfg = settings.get("session", {})
    re = RiskEngine(
        position_manager=pm,
        broker=broker,
        max_daily_loss=risk_cfg.get("max_daily_loss", 5000),
        max_concurrent_positions=risk_cfg.get("max_concurrent_positions", 2),
        max_trades_per_day=risk_cfg.get("max_trades_per_day", 10),
        sl_buffer_pct=risk_cfg.get("sl_buffer_pct", 1.5),
        cooldown_after_losses=risk_cfg.get("cooldown_after_losses", 2),
        cooldown_minutes=risk_cfg.get("cooldown_minutes", 30),
        margin_buffer_pct=risk_cfg.get("margin_buffer_pct", 10),
        session_start=session_cfg.get("start_time", "09:16"),
        square_off_time=session_cfg.get("square_off_time", "15:15"),
    )

    instruments = instruments_cfg.get("instruments", {})
    for name, cfg in instruments.items():
        if "session_start" in cfg and "square_off" in cfg:
            re.register_instrument_session(name, cfg["session_start"], cfg["square_off"])

    om = OrderManager(broker=broker, slippage_pct=0.5)
    ins_sel = InstrumentSelector(instruments)

    tg_cfg = settings.get("telegram", {})
    tg = TelegramAlert(
        bot_token=resolve_env(tg_cfg.get("bot_token", "")),
        chat_id=resolve_env(tg_cfg.get("chat_id", "")),
        enabled=tg_cfg.get("enabled", True),
    )

    strategy = OmegaStrategy(
        broker=broker,
        order_manager=om,
        position_manager=pm,
        risk_engine=re,
        instrument_selector=ins_sel,
        telegram=tg,
        instruments_config=instruments,
        default_qty_lots=settings.get("strategy", {}).get("default_qty_lots", 1),
        mode=mode,
    )

    tg.bot_started(mode, "omega_complete")

    best = strategy.auto_select_instrument()
    if best:
        log.info("Auto-selected instrument: %s", best)

    shutdown_event = threading.Event()

    def handle_shutdown(signum, frame):
        log.info("Shutdown signal received")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start webhook server (still available for TradingView if user upgrades later)
    webhook_cfg = settings.get("webhook", {})
    server_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={
            "app": app,
            "host": webhook_cfg.get("host", "0.0.0.0"),
            "port": webhook_cfg.get("port", 8000),
            "log_level": "warning",
        },
        daemon=True,
    )
    server_thread.start()
    log.info("Webhook server started on port %d", webhook_cfg.get("port", 8000))

    # Start built-in signal generator (replaces TradingView)
    if not args.webhook_only:
        sig_cfg = settings.get("signal_generator", {})
        watch = args.watch or sig_cfg.get("watch", ["NIFTY", "BANKNIFTY"])

        sig_gen = SignalGenerator(
            instruments=instruments,
            poll_interval=sig_cfg.get("poll_interval", 60),
            st_period=sig_cfg.get("st_period", 10),
            st_mult=sig_cfg.get("st_mult", 3.0),
            adx_len=sig_cfg.get("adx_len", 14),
            adx_threshold=sig_cfg.get("adx_threshold", 25),
            chand_len=sig_cfg.get("chand_len", 22),
            chand_mult=sig_cfg.get("chand_mult", 3.0),
        )

        sig_thread = threading.Thread(
            target=sig_gen.run,
            args=(signal_queue, shutdown_event, watch),
            daemon=True,
        )
        sig_thread.start()
        log.info("Signal generator started — watching: %s", ", ".join(watch))
    else:
        log.info("Webhook-only mode — waiting for TradingView signals")

    log.info("Entering main loop — waiting for signals...")
    monitor_interval = 5

    try:
        while not shutdown_event.is_set():
            while not signal_queue.empty():
                try:
                    payload = signal_queue.get_nowait()
                    strategy.process_signal(payload)
                except Exception as exc:
                    log.error("Signal processing error: %s", exc)
                    tg.error("process_signal", exc)

            try:
                strategy.monitor_positions()
            except Exception as exc:
                log.error("Position monitoring error: %s", exc)
                tg.error("monitor_positions", exc)

            if re.all_sessions_done():
                log.info("All sessions done — closing all positions")
                strategy.square_off_all()
                om.cancel_all_open()

                wins, losses = pm.get_win_loss_stats()
                tg.daily_summary(pm.realised_pnl, pm.trade_count_today,
                                 wins, losses)
                tg.bot_stopped("All sessions ended")

                log.info("EOD P&L: ₹%.2f | Trades: %d | W/L: %d/%d",
                         pm.realised_pnl, pm.trade_count_today, wins, losses)
                break

            if re.is_daily_loss_breached():
                log.critical("DAILY LOSS BREACHED — shutting down")
                strategy.square_off_all()
                om.cancel_all_open()
                tg.bot_stopped(f"Daily loss breached: ₹{pm.daily_pnl:,.2f}")
                break

            shutdown_event.wait(timeout=monitor_interval)

    except Exception as exc:
        log.critical("Unhandled exception: %s", exc, exc_info=True)
        tg.error("main_loop", exc)
    finally:
        log.info("Omega Trading System stopped")


if __name__ == "__main__":
    main()
