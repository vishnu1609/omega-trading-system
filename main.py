"""
Omega Trading Company — Main Entry Point

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

from core.broker_base import BrokerAuthError, BrokerBase
from core.broker_dhan import DhanBroker
from core.broker_paper import PaperBroker
from core.instrument_selector import InstrumentSelector
from core.order_manager import OrderManager
from core.position_manager import PositionManager
from core.risk_engine import RiskEngine
from core.daily_report import generate_daily_report, save_daily_report
from core.guardrails import Guardrails
from core.live_feed import LiveFeed, EXCHANGE_SEG_MAP
from core.chain_collector import ChainCollector
from core.greeks import compute_greeks, dte_from_expiry
from core.option_chain import OptionChainResolver
from core.signal_generator import SignalGenerator
from core.trade_memory import TradeMemory
from core.wallet import Wallet
from webhook.dashboard import update_state, register_bot
from strategies.omega_complete import OmegaStrategy
from utils.logger import setup_logging
import platform as _platform
if _platform.system() == "Darwin":
    from utils.imessage_alert import IMessageAlert as _AlertClass
else:
    from utils.telegram_alert import TelegramAlert as _AlertClass
from utils.market_calendar import MarketCalendar
from webhook.tv_receiver import app, signal_queue

IST = ZoneInfo("Asia/Kolkata")
load_dotenv(override=True)


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
    import os as _os, atexit as _atexit
    _pid_file = Path(__file__).parent / "data" / "omega.pid"
    _pid_file.parent.mkdir(parents=True, exist_ok=True)
    if _pid_file.exists():
        try:
            old_pid = int(_pid_file.read_text().strip())
            _os.kill(old_pid, 0)
            print(f"Another bot instance (PID {old_pid}) is already running. Kill it first or delete data/omega.pid")
            sys.exit(1)
        except (OSError, ValueError):
            pass
    _pid_file.write_text(str(_os.getpid()))
    _atexit.register(lambda: _pid_file.unlink(missing_ok=True))

    parser = argparse.ArgumentParser(description="Omega Trading Company")
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
    log.info("OMEGA TRADING COMPANY (OTC) — %s MODE", mode.upper())
    log.info("=" * 60)

    broker = create_broker(mode, settings.get("broker", {}))
    if isinstance(broker, PaperBroker):
        broker_cfg = settings.get("broker", {})
        data_broker = DhanBroker(
            client_id=resolve_env(broker_cfg.get("client_id", "")),
            access_token=resolve_env(broker_cfg.get("access_token", "")),
        )
        broker.data_broker = data_broker
        log.info("Paper mode: Dhan API wired for real option LTP data")

    pm = PositionManager()
    restored = pm.load_state()
    if restored:
        log.info("Restored %d paper position(s) from previous session", restored)

    try:
        from core.database import OmegaDB
        from core.position_manager import ClosedTrade
        db = OmegaDB.get()
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        db_trades = db.get_trades(limit=200, date=today_str)
        for t in reversed(db_trades):
            ct = ClosedTrade(
                symbol=t["symbol"], option_type=t.get("option_type", ""),
                direction=t["direction"], quantity=t["quantity"],
                entry_price=t["entry_price"], exit_price=t["exit_price"],
                pnl=t["pnl"],
                entry_time=datetime.fromisoformat(t["entry_time"]).replace(tzinfo=IST)
                    if datetime.fromisoformat(t["entry_time"]).tzinfo is None
                    else datetime.fromisoformat(t["entry_time"]),
                exit_time=datetime.fromisoformat(t["exit_time"]).replace(tzinfo=IST)
                    if datetime.fromisoformat(t["exit_time"]).tzinfo is None
                    else datetime.fromisoformat(t["exit_time"]),
                exit_reason=t.get("exit_reason", ""),
            )
            pm._closed.append(ct)
        if pm._closed:
            log.info("Loaded %d closed trade(s) from DB for today", len(pm._closed))
    except Exception as e:
        log.warning("Could not load closed trades from DB: %s", e)

    risk_cfg = settings.get("risk", {})
    session_cfg = settings.get("session", {})
    category_cfg = settings.get("category_toggle", {})
    re = RiskEngine(
        position_manager=pm,
        broker=broker,
        max_daily_loss=risk_cfg.get("max_daily_loss", 3000),
        max_concurrent_positions=risk_cfg.get("max_concurrent_positions", 2),
        max_daily_trades=risk_cfg.get("max_daily_trades", 6),
        sl_buffer_pct=risk_cfg.get("sl_buffer_pct", 0.3),
        trail_pct=risk_cfg.get("trail_pct", 0.15),
        cooldown_after_losses=risk_cfg.get("cooldown_after_losses", 2),
        cooldown_minutes=risk_cfg.get("cooldown_minutes", 30),
        margin_buffer_pct=risk_cfg.get("margin_buffer_pct", 10),
        session_start=session_cfg.get("start_time", "09:16"),
        square_off_time=session_cfg.get("square_off_time", "15:10"),
        mode=mode,
        allow_index_futures_live=category_cfg.get("allow_index_futures_live", False),
    )

    instruments = instruments_cfg.get("instruments", {})
    _sg_watch = settings.get("signal_generator", {}).get("watch", [])
    _watch_set = {w.upper() for w in (args.watch or _sg_watch)}
    for name, cfg in instruments.items():
        if "session_start" in cfg and "square_off" in cfg:
            if _watch_set and name.upper() not in _watch_set:
                continue
            re.register_instrument_session(name, cfg["session_start"], cfg["square_off"])

    if _platform.system() == "Darwin":
        im_cfg = settings.get("imessage", {})
        tg = _AlertClass(
            recipient=im_cfg.get("recipient", ""),
            enabled=im_cfg.get("enabled", True),
        )
    else:
        tg_cfg = settings.get("telegram", {})
        tg = _AlertClass(
            bot_token=tg_cfg.get("bot_token", ""),
            chat_id=tg_cfg.get("chat_id", ""),
            enabled=tg_cfg.get("enabled", True),
        )

    om = OrderManager(broker=broker, slippage_pct=0.5, telegram=tg)
    ins_sel = InstrumentSelector(instruments)

    guardrails_cfg = settings.get("guardrails", {})
    capital = guardrails_cfg.get("capital", 10_000)
    guardrails = Guardrails(
        capital=capital,
        max_risk_per_trade_pct=guardrails_cfg.get("max_risk_per_trade_pct", 6.0),
        nse_allocation_cap=guardrails_cfg.get("nse_allocation_cap", 0.70),
        adx_min_threshold=guardrails_cfg.get("adx_min_threshold", 22.0),
        vwap_warmup_time=guardrails_cfg.get("vwap_warmup_time", "09:30"),
        candle_max_staleness_sec=guardrails_cfg.get("candle_max_staleness_sec", 330),
        expiry_day_cutoff=guardrails_cfg.get("expiry_day_cutoff", "12:30"),
        step_trail_breakeven_pct=guardrails_cfg.get("step_trail_breakeven_pct", 0.15),
        step_trail_lock_pct=guardrails_cfg.get("step_trail_lock_pct", 0.30),
        step_trail_tight_pct=guardrails_cfg.get("step_trail_tight_pct", 0.50),
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

    option_chain = OptionChainResolver()
    log.info("Option chain resolver initialized (Dhan API)")

    live_feed = LiveFeed()
    index_instruments = [
        (0, "13", 15),   # NIFTY 50 — LTP
        (0, "25", 15),   # BANKNIFTY — LTP
        (0, "51", 15),   # SENSEX — LTP
    ]
    live_feed.start(index_instruments)
    log.info("LiveFeed started — WebSocket subscribed to %d index instruments", len(index_instruments))

    wallet = Wallet(starting_capital=100_000.0)
    wallet.reconcile_in_trade(pm.open_positions)
    log.info("Wallet loaded: balance=₹%.2f available=₹%.2f", wallet.balance, wallet.available)

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
        wallet=wallet,
    )
    strategy.option_chain = option_chain
    strategy.live_feed = live_feed

    register_bot(strategy=strategy, pm=pm, broker=broker, om=om, wallet=wallet, guardrails=guardrails, live_feed=live_feed)

    if mode == "live" and hasattr(broker, 'validate_token'):
        if not broker.validate_token():
            log.critical("DH-901: Invalid or expired token — refusing to start")
            tg.error("DH-901_STARTUP", Exception(
                "CRITICAL: Dhan token invalid. Renew at api.dhan.co and restart."))
            sys.exit(1)

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
                    peak_price=entry,
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
            "host": webhook_cfg.get("host", "127.0.0.1"),
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
            signal_cooldown_sec=sig_cfg.get("signal_cooldown_sec", 600),
            rsi_period=sig_cfg.get("rsi_period", 14),
            rsi_ob=sig_cfg.get("rsi_overbought", 70.0),
            rsi_os=sig_cfg.get("rsi_oversold", 30.0),
            bb_period=sig_cfg.get("bb_period", 20),
            bb_std=sig_cfg.get("bb_std", 2.0),
            ema_fast=sig_cfg.get("ema_fast", 9),
            ema_slow=sig_cfg.get("ema_slow", 21),
            chop_period=sig_cfg.get("chop_period", 14),
            chop_threshold=sig_cfg.get("chop_threshold", 61.8),
            orb_minutes=sig_cfg.get("orb_minutes", 30),
        )
        sig_gen.set_guardrails(guardrails)
        register_bot(sig_gen=sig_gen)

        calendar = MarketCalendar(base_path=base_path)
        sig_gen.set_calendar(calendar)
        status = calendar.market_status()
        log.info("Market status: %s | Next trading day: %s",
                 status["status"], status["next_trading_day"])

        sig_thread = threading.Thread(
            target=sig_gen.run,
            args=(signal_queue, shutdown_event, watch),
            daemon=True,
        )
        sig_thread.start()
        log.info("Signal generator started — watching: %s", ", ".join(watch))
    else:
        log.info("Webhook-only mode — waiting for TradingView signals")

    chain_collector = ChainCollector(option_chain, interval_minutes=5)
    chain_collector.start()

    _IDX_SEC = {"NIFTY": "13", "BANKNIFTY": "25", "SENSEX": "51",
                "FINNIFTY": "27", "BANKEX": "12"}

    def _greeks_for_pos(p) -> dict:
        try:
            if not p.expiry or not p.strike or p.iv <= 0:
                return {}
            sec_id = _IDX_SEC.get((p.underlying or "").upper())
            if not sec_id:
                return {}
            spot = live_feed.get_ltp(sec_id, "IDX_I")
            if spot <= 0:
                return {}
            dte = dte_from_expiry(p.expiry)
            g = compute_greeks(spot, p.strike, dte, p.iv, p.option_type)
            return {"delta": g.delta, "gamma": g.gamma, "theta": g.theta,
                    "vega": g.vega, "dte": round(dte, 1)}
        except Exception:
            return {}

    _signals_frozen = threading.Event()
    _sessions_ended = False

    def _broker_heartbeat():
        import os, requests as _req
        fail_count = 0
        _last_renew = 0
        RENEW_INTERVAL = 3600 * 6
        while not shutdown_event.is_set():
            try:
                broker.get_available_margin()
                if fail_count > 0:
                    log.info("Broker heartbeat recovered after %d failures", fail_count)
                    if _signals_frozen.is_set():
                        _signals_frozen.clear()
                        log.info("Signals unfrozen — broker connection restored")
                fail_count = 0
            except BrokerAuthError:
                if not _signals_frozen.is_set():
                    _signals_frozen.set()
                    log.critical("DH-901: Token expired — attempting auto-renewal")
                    try:
                        token = os.environ.get("DHAN_ACCESS_TOKEN", "")
                        cid = os.environ.get("DHAN_CLIENT_ID", "")
                        resp = _req.get("https://api.dhan.co/v2/RenewToken",
                                        headers={"access-token": token, "client-id": cid},
                                        timeout=10)
                        if resp.status_code == 200:
                            new_token = resp.json().get("access_token", "")
                            if new_token:
                                os.environ["DHAN_ACCESS_TOKEN"] = new_token
                                option_chain.update_token(new_token)
                                live_feed.update_token(new_token)
                                if hasattr(broker, '_client'):
                                    broker._client.headers["access-token"] = new_token
                                _signals_frozen.clear()
                                log.info("Token auto-renewed successfully")
                                tg.send("TOKEN RENEWED\nDhan access token refreshed automatically")
                            else:
                                raise Exception("Empty token in response")
                        else:
                            raise Exception(f"Renew failed: {resp.status_code}")
                    except Exception as renew_err:
                        log.critical("Token auto-renewal failed: %s", renew_err)
                        tg.error("DH-901_MID_SESSION", Exception(
                            "CRITICAL: Token expired & auto-renew failed. "
                            "Renew at api.dhan.co and restart."))
            except Exception:
                fail_count += 1
                if fail_count >= 3:
                    log.critical("BROKER HEARTBEAT FAILED %d consecutive times", fail_count)
                    tg.error("heartbeat", Exception(
                        f"Broker API unreachable {fail_count}x — open positions at risk"))
                    fail_count = 0

            now_ts = time.time()
            if now_ts - _last_renew > RENEW_INTERVAL:
                _last_renew = now_ts
                try:
                    token = os.environ.get("DHAN_ACCESS_TOKEN", "")
                    cid = os.environ.get("DHAN_CLIENT_ID", "")
                    resp = _req.get("https://api.dhan.co/v2/RenewToken",
                                    headers={"access-token": token, "client-id": cid},
                                    timeout=10)
                    if resp.status_code == 200:
                        new_token = resp.json().get("access_token", "")
                        if new_token:
                            os.environ["DHAN_ACCESS_TOKEN"] = new_token
                            option_chain.update_token(new_token)
                            log.info("Proactive token renewal succeeded")
                except Exception:
                    pass

            shutdown_event.wait(timeout=30)

    heartbeat_thread = threading.Thread(target=_broker_heartbeat, daemon=True)
    heartbeat_thread.start()

    log.info("Entering main loop — waiting for signals...")
    monitor_interval = 1
    margin_sync_interval = 180  # sync broker margin every 3 minutes
    _last_margin_sync = 0
    signals_log: list[dict] = []
    started_at = datetime.now(IST).strftime("%H:%M:%S")
    _last_trading_date = datetime.now(IST).date()

    update_state(
        system="indices",
        mode=mode,
        started_at=started_at,
        watching=watch if not args.webhook_only else [],
    )

    try:
        while not shutdown_event.is_set():
            today = datetime.now(IST).date()
            if today != _last_trading_date:
                log.info("New trading day detected: %s → %s", _last_trading_date, today)
                re.reset_daily()
                pm.reset_daily()
                guardrails.reset_daily()
                if sig_gen:
                    sig_gen._orb_levels.clear()
                    sig_gen._last_signal.clear()
                    sig_gen._trade_dir.clear()
                signals_log.clear()
                _last_trading_date = today

            while not signal_queue.empty():
                try:
                    payload = signal_queue.get_nowait()
                    if _signals_frozen.is_set():
                        log.warning("Signal DROPPED (frozen — auth expired): %s %s",
                                    payload.get("side"), payload.get("symbol"))
                        continue
                    signals_log.append(payload)
                    strategy.process_signal(payload)
                except Exception as exc:
                    log.error("Signal processing error: %s", exc)
                    tg.error("process_signal", exc)

            try:
                strategy.monitor_positions()
                pm.save_state()
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

            index_ltps = {
                "NIFTY": live_feed.get_ltp("13", "IDX_I"),
                "BANKNIFTY": live_feed.get_ltp("25", "IDX_I"),
                "SENSEX": live_feed.get_ltp("51", "IDX_I"),
            }

            try:
                update_state(
                    system="indices",
                    index_prices=index_ltps,
                positions=[{
                    "symbol": p.symbol,
                    "security_id": p.security_id,
                    "direction": p.direction,
                    "entry": p.entry_price,
                    "current": p.current_price,
                    "pnl": p.unrealised_pnl,
                    "sl": p.trail_sl or p.sl_price,
                    "tp": p.tp_price,
                    "confidence": p.confidence,
                    "dynamic_level": p.dynamic_level,
                    "max_dynamic_level": p.max_dynamic_level,
                    "entry_time": p.entry_time.isoformat(),
                    "tranche_exits": p.tranche_exits,
                    "option_type": p.option_type,
                    "quantity": p.quantity,
                    "expiry": p.expiry,
                    "is_spread": p.is_spread,
                    "hedge_trade_symbol": p.hedge_trade_symbol,
                    "hedge_strike": p.hedge_strike,
                    "spread_width": p.spread_width,
                    "net_credit": p.net_credit,
                    "max_loss": p.max_loss,
                    "hedge_entry_price": p.hedge_entry_price,
                    "hedge_current_price": p.hedge_current_price,
                    **(_greeks_for_pos(p)),
                } for p in pm.open_positions],
                closed_trades=[{
                    "symbol": t.symbol,
                    "direction": t.direction,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "pnl": t.pnl,
                    "reason": t.exit_reason,
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat(),
                    "option_type": t.option_type,
                    "quantity": t.quantity,
                } for t in pm.closed_trades],
                signals=signals_log[-50:],
                scan_count=sig_gen._scan_count if sig_gen else 0,
                memory={
                    "health": memory.get_health_report(),
                    "insights": memory.get_insights(),
                    "expectancy": memory.get_expectancy(),
                },
                risk_metrics=re.get_risk_metrics(
                    capital=guardrails.capital if guardrails else 100000),
                feed_health=live_feed.get_health_metrics(),
                broker_metrics=broker.get_broker_metrics() if hasattr(broker, "get_broker_metrics") else {},
                wallet=wallet.snapshot(),
                consecutive_losses=getattr(strategy, "_consecutive_losses", 0),
                loss_cooldown_remaining=max(0, 900 - (time.time() - getattr(strategy, "_last_loss_time", 0))) if getattr(strategy, "_consecutive_losses", 0) >= 3 else 0,
                )
            except Exception as dash_err:
                log.debug("Dashboard state update failed: %s", dash_err)

            if re.all_sessions_done():
                if not _sessions_ended:
                    _sessions_ended = True
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
                    log.info("Dashboard still running on port %d",
                             webhook_cfg.get("port", 8000))
                shutdown_event.wait(timeout=5)
                continue

            if re.is_daily_loss_breached():
                if not _sessions_ended:
                    _sessions_ended = True
                    log.critical("DAILY LOSS BREACHED — shutting down trading")
                    strategy.square_off_all()
                    om.cancel_all_open()
                    tg.bot_stopped(f"Daily loss breached: ₹{pm.daily_pnl:,.2f}")
                    report = generate_daily_report(
                        pm.closed_trades, signals_log, pm.realised_pnl)
                    save_daily_report(report, pm.closed_trades, signals_log)
                    log.info("\n%s", report)
                    log.info("Dashboard still running on port %d",
                             webhook_cfg.get("port", 8000))
                shutdown_event.wait(timeout=5)
                continue

            shutdown_event.wait(timeout=monitor_interval)

    except Exception as exc:
        log.critical("Unhandled exception: %s", exc, exc_info=True)
        tg.error("main_loop", exc)
    finally:
        log.info("Omega Trading Company stopped")


if __name__ == "__main__":
    main()
