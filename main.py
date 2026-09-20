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
from datetime import datetime
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
from core.daily_report import generate_daily_report, save_daily_report
from core.guardrails import Guardrails
from core.signal_generator import SignalGenerator
from core.trade_memory import TradeMemory
from webhook.dashboard import update_state
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

    tg_cfg = settings.get("telegram", {})
    tg = TelegramAlert(
        bot_token=resolve_env(tg_cfg.get("bot_token", "")),
        chat_id=resolve_env(tg_cfg.get("chat_id", "")),
        enabled=tg_cfg.get("enabled", True),
    )

    om = OrderManager(broker=broker, slippage_pct=0.5, telegram=tg)
    ins_sel = InstrumentSelector(instruments)

    guardrails_cfg = settings.get("guardrails", {})
    capital = guardrails_cfg.get("capital", 10_000)
    guardrails = Guardrails(
        capital=capital,
        max_risk_per_trade_pct=guardrails_cfg.get("max_risk_per_trade_pct", 2.0),
        nse_allocation_cap=guardrails_cfg.get("nse_allocation_cap", 0.60),
        mcx_allocation_cap=guardrails_cfg.get("mcx_allocation_cap", 0.40),
        adx_min_threshold=guardrails_cfg.get("adx_min_threshold", 22.0),
        vwap_warmup_time=guardrails_cfg.get("vwap_warmup_time", "09:30"),
        candle_max_staleness_sec=guardrails_cfg.get("candle_max_staleness_sec", 330),
        expiry_day_cutoff=guardrails_cfg.get("expiry_day_cutoff", "12:30"),
        natgas_thursday_close=guardrails_cfg.get("natgas_thursday_close", "19:50"),
        natgas_thursday_reopen=guardrails_cfg.get("natgas_thursday_reopen", "20:15"),
    )
    log.info("Guardrails initialized: capital=₹%s, risk/trade=%s%%, ADX min=%s",
             f"{capital:,}", guardrails_cfg.get("max_risk_per_trade_pct", 2.0),
             guardrails_cfg.get("adx_min_threshold", 22.0))

    base_path = str(Path(__file__).parent)
    memory = TradeMemory(base_path=base_path)
    health = memory.get_health_report()
    log.info("TradeMemory loaded: %d trades, %d lessons, %d rules, status=%s",
             health["trades"], health.get("active_lessons", 0),
             health.get("active_rules", 0), health["status"])
    if health.get("alerts"):
        for alert in health["alerts"]:
            log.warning("TradeMemory alert: %s", alert)

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
        trade_memory=memory,
    )

    tg.bot_started(mode, "omega_complete")

    try:
        broker_positions = broker.get_positions()
        orphans = [p for p in broker_positions if p.net_quantity != 0]
        if orphans:
            log.warning("STATE SYNC: Found %d orphan position(s) on broker:", len(orphans))
            for op in orphans:
                log.warning("  %s: net_qty=%d, buy_avg=%.2f",
                            op.symbol, op.net_quantity, op.buy_avg)
                from core.position_manager import Position
                direction = "LONG" if op.net_quantity > 0 else "SHORT"
                entry = op.buy_avg if direction == "LONG" else op.sell_avg
                pos = Position(
                    symbol=op.symbol,
                    security_id=op.security_id,
                    exchange_segment=op.exchange_segment,
                    option_type="",
                    direction=direction,
                    quantity=abs(op.net_quantity),
                    entry_price=entry,
                    current_price=entry,
                    sl_price=re.compute_sl_price(entry, direction),
                    trail_sl=re.compute_sl_price(entry, direction),
                    strategy="orphan_sync",
                )
                pm.add_position(pos)
            log.info("STATE SYNC: Reconstructed %d position(s) from broker", len(orphans))
            tg.error("state_sync", Exception(
                f"Bot restarted with {len(orphans)} open position(s) — "
                f"trailing stops re-armed"))
    except Exception as exc:
        log.warning("State sync skipped (broker unavailable): %s", exc)

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
    sig_cfg = settings.get("signal_generator", {})
    watch = args.watch or sig_cfg.get("watch", ["NIFTY", "BANKNIFTY"])
    sig_gen = None

    if not args.webhook_only:

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
        sig_gen.set_guardrails(guardrails)

        sig_thread = threading.Thread(
            target=sig_gen.run,
            args=(signal_queue, shutdown_event, watch),
            daemon=True,
        )
        sig_thread.start()
        log.info("Signal generator started — watching: %s", ", ".join(watch))
    else:
        log.info("Webhook-only mode — waiting for TradingView signals")

    def _broker_heartbeat():
        fail_count = 0
        while not shutdown_event.is_set():
            try:
                broker.get_available_margin()
                if fail_count > 0:
                    log.info("Broker heartbeat recovered after %d failures", fail_count)
                fail_count = 0
            except Exception:
                fail_count += 1
                if fail_count >= 3:
                    log.critical("BROKER HEARTBEAT FAILED %d consecutive times", fail_count)
                    tg.error("heartbeat", Exception(
                        f"Broker API unreachable {fail_count}x — open positions at risk"))
                    fail_count = 0
            shutdown_event.wait(timeout=30)

    heartbeat_thread = threading.Thread(target=_broker_heartbeat, daemon=True)
    heartbeat_thread.start()

    log.info("Entering main loop — waiting for signals...")
    monitor_interval = 5
    margin_sync_interval = 180  # sync broker margin every 3 minutes
    _last_margin_sync = 0
    signals_log: list[dict] = []
    started_at = datetime.now(IST).strftime("%H:%M:%S")

    update_state(
        mode=mode,
        started_at=started_at,
        watching=watch if not args.webhook_only else [],
    )

    try:
        while not shutdown_event.is_set():
            while not signal_queue.empty():
                try:
                    payload = signal_queue.get_nowait()
                    signals_log.append(payload)
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
                tg.error("monitor_positions", exc)

            now_ts = time.time()
            if now_ts - _last_margin_sync > margin_sync_interval:
                _last_margin_sync = now_ts
                try:
                    real_margin = broker.get_available_margin()
                    local_pnl = pm.daily_pnl
                    if real_margin < re.max_daily_loss * 0.5:
                        log.warning("MARGIN SYNC: Real margin ₹%.0f is critically low "
                                    "(local P&L: ₹%.0f)", real_margin, local_pnl)
                    if real_margin <= 0 and pm.open_count > 0:
                        log.critical("MARGIN SYNC: Zero margin with open positions — "
                                     "triggering emergency square-off")
                        strategy.square_off_all()
                        tg.error("margin_sync", Exception(
                            f"Real margin depleted (₹{real_margin:.0f}). "
                            f"All positions squared off."))
                except Exception:
                    pass

            update_state(
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
                scan_count=sig_gen._scan_count if sig_gen else 0,
                memory={
                    "health": memory.get_health_report(),
                    "insights": memory.get_insights(),
                },
            )

            if re.all_sessions_done():
                log.info("All sessions done — closing all positions")
                strategy.square_off_all()
                om.cancel_all_open()

                wins, losses = pm.get_win_loss_stats()
                tg.daily_summary(pm.realised_pnl, pm.trade_count_today,
                                 wins, losses)
                tg.bot_stopped("All sessions ended")

                report = generate_daily_report(
                    pm.closed_trades, signals_log, pm.realised_pnl)
                save_daily_report(report, pm.closed_trades, signals_log)
                log.info("\n%s", report)
                break

            if re.is_daily_loss_breached():
                log.critical("DAILY LOSS BREACHED — shutting down")
                strategy.square_off_all()
                om.cancel_all_open()
                tg.bot_stopped(f"Daily loss breached: ₹{pm.daily_pnl:,.2f}")
                report = generate_daily_report(
                    pm.closed_trades, signals_log, pm.realised_pnl)
                save_daily_report(report, pm.closed_trades, signals_log)
                log.info("\n%s", report)
                break

            shutdown_event.wait(timeout=monitor_interval)

    except Exception as exc:
        log.critical("Unhandled exception: %s", exc, exc_info=True)
        tg.error("main_loop", exc)
    finally:
        log.info("Omega Trading System stopped")


if __name__ == "__main__":
    main()
