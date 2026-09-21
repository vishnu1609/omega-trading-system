"""
MCX Trading System — Commodity Options (GOLD, SILVER, CRUDEOIL, NATURALGAS)

Separate system from the index options bot.
Shares core modules (broker, risk engine, alerts) from parent directory.
Paper mode by default.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Queue
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from dotenv import load_dotenv

from core.broker_base import BrokerAuthError
from core.broker_dhan import DhanBroker
from core.broker_paper import PaperBroker
from core.order_manager import OrderManager
from core.position_manager import PositionManager
from core.risk_engine import RiskEngine
from core.daily_report import generate_daily_report, save_daily_report
from core.instrument_selector import InstrumentSelector
from strategies.omega_complete import OmegaStrategy
from mcx_guardrails import MCXGuardrails
from mcx_signal_generator import MCXSignalGenerator
from webhook.dashboard import update_state
from webhook.tv_receiver import app as webhook_app
from utils.logger import setup_logging
from utils.imessage_alert import IMessageAlert

IST = ZoneInfo("Asia/Kolkata")
load_dotenv(Path(__file__).parent.parent / ".env")


def load_config() -> tuple[dict, dict]:
    base = Path(__file__).parent / "config"
    with open(base / "settings.yaml") as f:
        settings = yaml.safe_load(f)
    with open(base / "instruments.yaml") as f:
        instruments = yaml.safe_load(f)
    return settings, instruments


def resolve_env(value: str) -> str:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="MCX Trading System")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--watch", nargs="+", default=None)
    args = parser.parse_args()

    settings, instruments_cfg = load_config()
    mode = "live" if args.live else settings.get("strategy", {}).get("mode", "paper")

    if mode == "live":
        print("\n" + "=" * 50)
        print("  WARNING: LIVE TRADING MODE — MCX COMMODITIES")
        print("  Real money will be used for orders.")
        print("=" * 50)
        confirm = input("Type 'YES I UNDERSTAND' to continue: ")
        if confirm.strip() != "YES I UNDERSTAND":
            print("Aborted.")
            sys.exit(0)

    setup_logging(
        level=settings.get("logging", {}).get("level", "INFO"),
        log_file=settings.get("logging", {}).get("file", "logs/mcx.log"),
    )
    log = logging.getLogger("mcx.main")

    log.info("=" * 60)
    log.info("MCX TRADING SYSTEM — %s MODE", mode.upper())
    log.info("Started at %s IST", datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)

    broker_cfg = settings.get("broker", {})
    if mode == "live":
        broker = DhanBroker(
            client_id=resolve_env(broker_cfg.get("client_id", "")),
            access_token=resolve_env(broker_cfg.get("access_token", "")),
        )
    else:
        broker = PaperBroker()

    pm = PositionManager()
    risk_cfg = settings.get("risk", {})
    session_cfg = settings.get("session", {})
    re = RiskEngine(
        position_manager=pm,
        broker=broker,
        max_daily_loss=risk_cfg.get("max_daily_loss", 5000),
        max_concurrent_positions=risk_cfg.get("max_concurrent_positions", 2),
        sl_buffer_pct=risk_cfg.get("sl_buffer_pct", 2.5),
        cooldown_after_losses=risk_cfg.get("cooldown_after_losses", 2),
        cooldown_minutes=risk_cfg.get("cooldown_minutes", 30),
        margin_buffer_pct=risk_cfg.get("margin_buffer_pct", 20),
        session_start=session_cfg.get("start_time", "09:00"),
        square_off_time=session_cfg.get("square_off_time", "23:15"),
        mode=mode,
    )

    instruments = instruments_cfg.get("instruments", {})
    sig_cfg = settings.get("signal_generator", {})
    watch = args.watch or sig_cfg.get("watch", ["GOLD", "CRUDEOIL", "NATURALGAS"])
    watch_set = {w.upper() for w in watch}

    for name, cfg in instruments.items():
        if "session_start" in cfg and "square_off" in cfg:
            if watch_set and name.upper() not in watch_set:
                continue
            re.register_instrument_session(name, cfg["session_start"], cfg["square_off"])

    im_cfg = settings.get("imessage", {})
    tg = IMessageAlert(
        recipient=im_cfg.get("recipient", ""),
        enabled=im_cfg.get("enabled", True),
    )

    om = OrderManager(broker=broker, slippage_pct=0.5, telegram=tg)
    ins_sel = InstrumentSelector(instruments)

    guardrails_cfg = settings.get("guardrails", {})
    guardrails = MCXGuardrails(
        capital=guardrails_cfg.get("capital", 100000),
        max_risk_per_trade_pct=guardrails_cfg.get("max_risk_per_trade_pct", 3.0),
        nse_allocation_cap=guardrails_cfg.get("mcx_allocation_cap", 0.70),
        adx_min_threshold=guardrails_cfg.get("adx_min_threshold", 18.0),
        vwap_warmup_time=guardrails_cfg.get("vwap_warmup_time", "09:15"),
        candle_max_staleness_sec=guardrails_cfg.get("candle_max_staleness_sec", 330),
        expiry_day_cutoff=guardrails_cfg.get("expiry_day_cutoff", "20:00"),
        natgas_thursday_close=guardrails_cfg.get("natgas_thursday_close", "19:50"),
        natgas_thursday_reopen=guardrails_cfg.get("natgas_thursday_reopen", "20:15"),
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
        guardrails=guardrails,
    )

    import uvicorn
    server_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={
            "app": webhook_app,
            "host": "0.0.0.0",
            "port": 8001,
            "log_level": "warning",
        },
        daemon=True,
    )
    server_thread.start()
    log.info("MCX dashboard on http://localhost:8001")

    started_at = datetime.now(IST).strftime("%H:%M:%S")
    update_state(system="mcx", mode=mode, started_at=started_at, watching=watch)

    tg.bot_started(mode, "mcx_system")
    log.info("MCX system initialized — watching: %s", ", ".join(watch))

    shutdown_event = threading.Event()

    def handle_shutdown(signum, frame):
        log.info("Shutdown signal received")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    signal_queue = Queue()

    sig_gen = MCXSignalGenerator(
        instruments=instruments,
        poll_interval=sig_cfg.get("poll_interval", 60),
        st_period=sig_cfg.get("st_period", 10),
        st_mult=sig_cfg.get("st_mult", 3.0),
        adx_len=sig_cfg.get("adx_len", 14),
        adx_threshold=sig_cfg.get("adx_threshold", 22),
        chand_len=sig_cfg.get("chand_len", 22),
        chand_mult=sig_cfg.get("chand_mult", 3.0),
    )
    sig_gen.set_guardrails(guardrails)

    sig_thread = threading.Thread(
        target=sig_gen.run,
        args=(signal_queue, shutdown_event, watch),
        daemon=True,
    )
    sig_thread.start()
    log.info("MCX signal generator started — polling every %ds", sig_cfg.get("poll_interval", 60))

    monitor_interval = 5
    signals_log: list[dict] = []

    try:
        while not shutdown_event.is_set():
            while not signal_queue.empty():
                try:
                    payload = signal_queue.get_nowait()
                    signals_log.append(payload)
                    log.info("SIGNAL: %s %s (score=%.0f)",
                             payload.get("side"), payload.get("symbol"),
                             payload.get("score", 0))
                    strategy.process_signal(payload)
                except Exception as exc:
                    log.error("Signal processing error: %s", exc)
                    tg.error("process_signal", exc)

            try:
                strategy.monitor_positions()

                if guardrails.should_close_natgas():
                    for pos in pm.open_positions:
                        if "NATURALGAS" in pos.symbol.upper():
                            log.warning("NatGas Thursday auto-close: %s", pos.symbol)
                            ltp = pos.current_price or pos.entry_price
                            strategy._exit_position(pos, ltp, "NATGAS_THURSDAY")
            except Exception as exc:
                log.error("Position monitoring error: %s", exc)

            update_state(
                system="mcx",
                positions=[{
                    "symbol": p.symbol,
                    "direction": p.direction,
                    "entry": p.entry_price,
                    "current": p.current_price,
                    "pnl": p.unrealised_pnl,
                    "sl": p.trail_sl or p.sl_price,
                } for p in pm.open_positions],
                closed_trades=[{
                    "symbol": t.symbol,
                    "direction": t.direction,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "pnl": t.pnl,
                    "reason": t.exit_reason,
                } for t in pm.closed_trades],
                signals=signals_log[-50:],
                scan_count=sig_gen._scan_count,
            )

            if re.all_sessions_done():
                log.info("All MCX sessions done — closing positions")
                strategy.square_off_all()
                om.cancel_all_open()
                wins, losses = pm.get_win_loss_stats()
                tg.daily_summary(pm.realised_pnl, pm.trade_count_today, wins, losses)
                tg.bot_stopped("MCX sessions ended")
                report = generate_daily_report(
                    pm.closed_trades, signals_log, pm.realised_pnl)
                save_daily_report(report, pm.closed_trades, signals_log)
                log.info("\n%s", report)
                break

            if re.is_daily_loss_breached():
                log.critical("DAILY LOSS BREACHED — shutting down MCX")
                strategy.square_off_all()
                tg.bot_stopped(f"MCX daily loss breached: ₹{pm.daily_pnl:,.2f}")
                break

            shutdown_event.wait(timeout=monitor_interval)

    except Exception as exc:
        log.critical("Unhandled exception: %s", exc, exc_info=True)
        tg.error("mcx_main_loop", exc)
    finally:
        log.info("MCX Trading System stopped at %s IST",
                 datetime.now(IST).strftime("%H:%M:%S"))


if __name__ == "__main__":
    main()
