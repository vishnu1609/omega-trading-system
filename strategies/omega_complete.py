"""
Omega Strategy — Confluence-Based Signal Engine

Translates the Protocol Omega Pine Script into Python.
Entry requires 3+ independent factors agreeing.

ENTRY RULES:
  BUY CE  → SuperTrend bullish + close > TEMA + ADX > threshold
            + close > EMA200 + close > VWAP  (5-factor confluence)
  BUY PE  → SuperTrend bearish + close < TEMA + ADX > threshold
            + close < EMA200 + close < VWAP

SELL CE  → Same as BUY PE direction but in range mode (ADX < sell_threshold)
SELL PE  → Same as BUY CE direction but in range mode

EXIT RULES:
  LONG exit  → price crosses below trailing stop (Chandelier exit)
  SHORT exit → price crosses above trailing stop OR regime shifts to trending

ASSUMPTION THAT COULD LOSE MONEY:
  - The backtest uses underlying index price as a proxy for option premium
    movement. In reality, option premiums are affected by IV, theta, and gamma.
    A 100-point Nifty move does NOT always equal a 100-point CE premium change.
    This overestimates P&L for far OTM strikes and underestimates for ATM.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import re as regex

from core.broker_base import BrokerBase
from core.instrument_selector import InstrumentSelector
from core.option_chain import OptionChainResolver, OptionContract
from core.order_manager import OrderManager, Signal
from core.position_manager import Position, PositionManager
from core.risk_engine import RiskEngine
from core.signal_generator import YAHOO_SYMBOLS
from core.trade_memory import TradeMemory
from utils.imessage_alert import IMessageAlert

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class OmegaStrategy:
    """Processes webhook signals and routes through risk → order → broker."""

    def __init__(
        self,
        broker: BrokerBase,
        order_manager: OrderManager,
        position_manager: PositionManager,
        risk_engine: RiskEngine,
        instrument_selector: InstrumentSelector,
        telegram: IMessageAlert,
        instruments_config: dict,
        default_qty_lots: int = 1,
        mode: str = "paper",
        guardrails=None,
        trade_memory: TradeMemory | None = None,
    ):
        self.broker = broker
        self.om = order_manager
        self.pm = position_manager
        self.re = risk_engine
        self.ins_sel = instrument_selector
        self.tg = telegram
        self.instruments = instruments_config
        self.default_qty_lots = default_qty_lots
        self.mode = mode
        self.guardrails = guardrails
        self.memory = trade_memory
        self.option_chain: OptionChainResolver | None = None
        self.live_feed = None
        self._signal_context: dict[str, dict] = {}
        self._underlying_cache: dict[str, tuple[float, float]] = {}

    def _get_underlying_ltp(self, symbol: str) -> float:
        """Extract underlying name from option symbol and fetch its price."""
        underlying = regex.sub(r'\d*(CE|PE)$', '', symbol).upper()
        if not underlying:
            return 0.0
        now = time.time()
        cached = self._underlying_cache.get(underlying)
        if cached and now - cached[1] < 15:
            return cached[0]
        yahoo_sym = YAHOO_SYMBOLS.get(underlying)
        if not yahoo_sym:
            return 0.0
        try:
            import yfinance as yf
            df = yf.download(yahoo_sym, period='1d', interval='5m', progress=False)
            if not df.empty:
                price = float(df['Close'].iloc[-1].iloc[0]) if hasattr(df['Close'].iloc[-1], 'iloc') else float(df['Close'].iloc[-1])
                self._underlying_cache[underlying] = (price, now)
                return price
        except Exception:
            pass
        return 0.0

    def process_signal(self, payload: dict) -> None:
        """Main entry point — receives a parsed webhook payload."""
        symbol = payload["symbol"]
        side = payload["side"]
        strategy = payload.get("strategy", "omega_complete")
        strike = payload.get("strike", 0)
        sl = payload.get("sl", 0)
        asset = payload.get("asset", "options")

        log.info("Signal received: %s %s strike=%s sl=%s asset=%s",
                 side, symbol, strike, sl, asset)

        underlying_close = payload.get("close", 0)

        if side == "EXIT":
            self._handle_exit(symbol, strategy)
            return

        inst_name = self._resolve_instrument(symbol)
        if not inst_name:
            log.warning("Unknown instrument for symbol %s", symbol)
            return

        inst_cfg = self.instruments.get(inst_name, {})
        lot_size = inst_cfg.get("lot_size", 75)
        quantity = lot_size * self.default_qty_lots
        direction = "LONG" if side.startswith("BUY") else "SHORT"

        option_type = "CE" if side.endswith("CE") else "PE"
        strike_step = inst_cfg.get("strike_step", 50)
        exchange_seg = "NSE_FNO" if inst_cfg.get("exchange") == "NSE" else "BSE_FNO"

        contract: OptionContract | None = None
        if self.option_chain and asset == "options":
            underlying_price = sl * 1.01 if sl > 0 else 0
            contract = self.option_chain.resolve_atm_option(
                underlying=inst_name,
                underlying_price=underlying_price,
                option_type=option_type,
                lot_size=lot_size,
                strike_step=strike_step,
            )

        if contract:
            trade_symbol = contract.trade_symbol
            security_id = contract.security_id
            exchange_seg = contract.exchange_segment
            strike = contract.strike
            entry_price = contract.ltp
        else:
            if not strike:
                strike = self._compute_atm_strike(inst_name, strike_step)
            trade_symbol = f"{inst_name}{strike}{option_type}"
            security_id = trade_symbol
            entry_price = 0

        signal = Signal(
            strategy=strategy,
            symbol=trade_symbol,
            security_id=security_id,
            exchange_segment=exchange_seg,
            side=side,
            quantity=quantity,
            price=entry_price,
            sl=sl,
            strike=strike,
        )

        correlated = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"}
        if inst_name and inst_name.upper() in correlated:
            for pos in self.pm.open_positions:
                pos_base = None
                for c in correlated:
                    if c in pos.symbol.upper():
                        pos_base = c
                        break
                if pos_base and pos_base != inst_name.upper() and pos.direction == direction:
                    log.warning("Correlated position blocked: %s %s already open with %s %s",
                                direction, inst_name, pos.direction, pos.symbol)
                    return

        if self.guardrails and entry_price > 0:
            premium_sl = entry_price * 0.50
            risk_check = self.guardrails.check_per_trade_risk(
                entry_price, entry_price - premium_sl, quantity)
            if not risk_check:
                log.warning("Trade blocked by guardrail: %s", risk_check.reason)
                return
        elif self.guardrails and sl > 0:
            est_premium = sl * 0.01
            est_sl = est_premium * 0.90
            risk_check = self.guardrails.check_per_trade_risk(
                est_premium, est_sl, quantity)
            if not risk_check:
                log.warning("Trade blocked by guardrail: %s", risk_check.reason)
                return

        if self.memory:
            exp = self.memory.get_expectancy(trade_symbol)
            if exp.get("enough_data") and exp.get("win_rate", 0) > 0:
                kelly_size = self.re.kelly_position_size(
                    win_rate=exp["win_rate"],
                    avg_win_r=exp.get("avg_win_r", 1.0),
                    avg_loss_r=exp.get("avg_loss_r", 1.0),
                    capital=self.guardrails.capital if self.guardrails else 100000,
                )
                max_qty_by_kelly = int(kelly_size / max(entry_price, 1)) if entry_price > 0 else quantity
                if max_qty_by_kelly < quantity and max_qty_by_kelly >= lot_size:
                    log.info("Kelly sizing: %d → %d (win=%.0f%%, R:R=%.1f:%.1f)",
                             quantity, max_qty_by_kelly,
                             exp["win_rate"] * 100, exp.get("avg_win_r", 0), exp.get("avg_loss_r", 0))
                    quantity = max_qty_by_kelly
                    signal.quantity = quantity

            now = datetime.now(IST)
            blocked, block_reason = self.memory.should_block(
                symbol=trade_symbol, side=side,
                entry_hour=now.hour, entry_day=now.weekday())
            if blocked:
                log.warning("Trade blocked by memory: %s", block_reason)
                self.tg.error("MEMORY_BLOCK", Exception(block_reason))
                return

            adj, adj_reasons = self.memory.get_score_adjustment(
                symbol=trade_symbol, side=side,
                entry_hour=now.hour, entry_day=now.weekday())
            if adj_reasons:
                log.info("Memory adjustments: %s", "; ".join(adj_reasons))

            size_mod = self.memory.get_position_size_modifier(trade_symbol)
            if size_mod < 1.0:
                original_qty = quantity
                quantity = max(int(quantity * size_mod), inst_cfg.get("lot_size", 75))
                signal.quantity = quantity
                log.info("Position size reduced by memory: %d → %d", original_qty, quantity)

        score = payload.get("score", 0)
        atr_for_sizing = payload.get("atr", 0)
        confidence_for_risk = payload.get("confidence", "LOW")
        if atr_for_sizing > 0 and entry_price > 0:
            risk_pct = {"HIGH": 3.0, "MEDIUM": 2.0, "LOW": 1.0}.get(confidence_for_risk, 1.0)
            sl_mult_for_sizing = {"HIGH": 2.0, "MEDIUM": 1.5, "LOW": 1.0}.get(confidence_for_risk, 1.0)
            cap = self.guardrails.capital if self.guardrails else 100000
            atr_qty = self.re.atr_position_size(
                capital=cap, risk_pct=risk_pct,
                entry_price=entry_price, atr_value=atr_for_sizing,
                sl_multiplier=sl_mult_for_sizing, lot_size=lot_size)
            if atr_qty < quantity:
                log.info("ATR position sizing: %d → %d (risk=%.0f%%, ATR=%.2f, %s)",
                         quantity, atr_qty, risk_pct, atr_for_sizing, confidence_for_risk)
                quantity = atr_qty
                signal.quantity = quantity

        resp = self.om.place_entry(signal, self.re, instrument=inst_name)
        if not resp:
            return

        fill_price = resp.fill_price if resp.fill_price > 0 else entry_price
        if fill_price <= 0 and asset == "options":
            log.warning("No option premium available for %s — cannot calculate P&L accurately, skipping trade", trade_symbol)
            return
        if fill_price <= 0:
            fill_price = underlying_close if underlying_close > 0 else entry_price
        atr_value = payload.get("atr", 0)
        confidence = payload.get("confidence", "LOW")
        if confidence == "HIGH":
            sl_mult, tp_mult = 2.0, 6.0
        elif confidence == "MEDIUM":
            sl_mult, tp_mult = 1.5, 3.0
        else:
            sl_mult, tp_mult = 1.0, 1.5

        if atr_value > 0:
            sl_price = self.re.compute_atr_sl(fill_price, direction, atr_value, multiplier=sl_mult)
            tp_price = self.re.compute_atr_tp(fill_price, direction, atr_value, multiplier=tp_mult)
        else:
            sl_price = self.re.compute_sl_price(fill_price, direction, confluence_score=score)
            tp_price = self.re.compute_tp_price(fill_price, direction)
        log.info("Confidence-based SL/TP: %s → SL=%.2f (%.1f×ATR) TP=%.2f (%.1f×ATR)",
                 confidence, sl_price, sl_mult, tp_price, tp_mult)

        position = Position(
            symbol=trade_symbol,
            security_id=security_id,
            exchange_segment=exchange_seg,
            option_type=option_type,
            direction=direction,
            quantity=quantity,
            entry_price=fill_price,
            sl_price=sl_price,
            trail_sl=sl_price,
            tp_price=tp_price,
            entry_order_id=resp.order_id,
            strategy=strategy,
        )
        self.pm.add_position(position)
        self.re.record_trade_taken()

        if self.live_feed and contract and contract.security_id.isdigit():
            try:
                from dhanhq import MarketFeed as MF
                seg_int = {"NSE_FNO": MF.NSE_FNO, "BSE_FNO": MF.BSE_FNO}.get(
                    exchange_seg, MF.NSE_FNO)
                self.live_feed.subscribe(
                    [(seg_int, contract.security_id, MF.Quote)])
                log.info("LiveFeed subscribed: %s sid=%s",
                         trade_symbol, contract.security_id)
            except Exception as e:
                log.warning("LiveFeed subscribe failed: %s", e)

        if self.memory:
            self._signal_context[trade_symbol] = {
                "signal_score": payload.get("score", 0),
                "signal_confidence": payload.get("confidence", "LOW"),
                "adx_value": payload.get("adx", 0),
                "vwap_position": payload.get("vwap_position", "UNKNOWN"),
                "supertrend_dir": payload.get("supertrend_dir", "UNKNOWN"),
                "volume_ratio": payload.get("volume_ratio", 1.0),
                "atr_value": payload.get("atr", 0),
                "rsi_value": payload.get("rsi", 50),
                "bb_squeeze": payload.get("bb_squeeze", False),
                "ema_aligned": payload.get("ema_aligned", False),
                "orb_breakout": payload.get("orb_breakout", False),
                "confluence_count": payload.get("bull_factors", 0) or payload.get("bear_factors", 0),
                "choppiness": payload.get("choppiness", 50),
            }

        sl_resp = self.om.place_sl_limit(
            security_id=security_id,
            exchange_segment=exchange_seg,
            direction=direction,
            quantity=quantity,
            trigger_price=sl_price,
            buffer_pct=2.0,
        )
        if sl_resp and sl_resp.order_id:
            position.sl_order_id = sl_resp.order_id

        if self.guardrails:
            est_margin = fill_price * quantity
            self.guardrails.record_allocation(symbol, est_margin)

        self.tg.order_filled(
            side=side,
            symbol=trade_symbol,
            qty=quantity,
            price=fill_price,
            order_id=resp.order_id,
            strike=str(strike) if strike else "",
            expiry=contract.expiry if contract else "",
            iv=contract.iv if contract else 0,
            oi=contract.oi if contract else 0,
            score=score,
        )

    def _handle_exit(self, symbol: str, strategy: str) -> None:
        closed = []
        for pos in self.pm.open_positions:
            if symbol in pos.symbol or pos.strategy == strategy:
                trail_at_breakeven = (
                    (pos.direction == "LONG" and pos.trail_sl >= pos.entry_price) or
                    (pos.direction == "SHORT" and 0 < pos.trail_sl <= pos.entry_price)
                )
                if trail_at_breakeven:
                    log.info("SIGNAL_EXIT skipped for %s — trail at breakeven (SL=%.2f, entry=%.2f), letting trail manage",
                             pos.symbol, pos.trail_sl, pos.entry_price)
                    continue

                self._exit_position(pos, pos.current_price if pos.current_price > 0 else
                                    self._get_underlying_ltp(pos.symbol) or pos.entry_price,
                                    "SIGNAL_EXIT")
                closed.append(pos.symbol)

        log.info("Exited %d positions on signal", len(closed))

    def monitor_positions(self) -> None:
        """Called periodically to check SL/TP/trail on open positions."""
        for pos in self.pm.open_positions:
            try:
                ltp = self.broker.get_ltp(pos.security_id, pos.exchange_segment)
                if ltp <= 0:
                    ltp = self._get_underlying_ltp(pos.symbol)
                if ltp <= 0:
                    continue
                self.pm.update_price(pos.security_id, ltp)
            except Exception as exc:
                log.warning("LTP fetch failed for %s: %s", pos.symbol, exc)
                continue

            trail_eval = self.re.evaluate_trail(
                current_price=ltp,
                entry_price=pos.entry_price,
                existing_sl=pos.trail_sl,
                direction=pos.direction,
                peak_price=pos.peak_price,
            )
            pos.peak_price = trail_eval["peak"]

            if self.guardrails:
                new_trail = self.guardrails.compute_step_trail_sl(
                    pos.entry_price, ltp, trail_eval["sl"], pos.direction,
                    trail_pct=self.re.trail_pct)
            else:
                new_trail = trail_eval["sl"]
            old_trail = pos.trail_sl
            self.pm.update_trail_sl(pos.security_id, new_trail)

            if pos.trail_sl != old_trail and pos.sl_order_id:
                try:
                    self.broker.cancel_order(pos.sl_order_id)
                    sl_resp = self.om.place_sl_limit(
                        security_id=pos.security_id,
                        exchange_segment=pos.exchange_segment,
                        direction=pos.direction,
                        quantity=pos.quantity,
                        trigger_price=pos.trail_sl,
                        buffer_pct=2.0,
                    )
                    if sl_resp and sl_resp.order_id:
                        pos.sl_order_id = sl_resp.order_id
                    log.info("Broker SL synced: %s %.2f → %.2f (order=%s)",
                             pos.symbol, old_trail, pos.trail_sl,
                             sl_resp.order_id if sl_resp else "FAILED")
                except Exception as e:
                    log.error("Broker SL sync failed for %s: %s", pos.symbol, e)

            if pos.is_sl_hit:
                log.info("SL HIT: %s %s @ %.2f (SL=%.2f)",
                         pos.direction, pos.symbol, ltp, pos.trail_sl)
                self._exit_position(pos, ltp, "TRAIL_SL")

            elif pos.is_tp_hit:
                log.info("TP HIT: %s %s @ %.2f (TP=%.2f)",
                         pos.direction, pos.symbol, ltp, pos.tp_price)
                self._exit_position(pos, ltp, "TP")

            elif self._is_sl_skipped(pos, ltp):
                log.critical("SL-SKIP FREEFALL: %s %s LTP=%.2f fell past SL=%.2f — "
                             "emergency exit at LTP-1%%",
                             pos.direction, pos.symbol, ltp, pos.trail_sl)
                self._exit_position(pos, ltp, "SL_SKIP_EMERGENCY")

    def _is_sl_skipped(self, pos, ltp: float) -> bool:
        active_sl = pos.trail_sl if pos.trail_sl > 0 else pos.sl_price
        if active_sl <= 0 or ltp <= 0:
            return False
        if pos.direction == "LONG":
            gap_pct = (active_sl - ltp) / active_sl * 100 if active_sl > 0 else 0
            return ltp < active_sl and gap_pct > 0.5
        else:
            gap_pct = (ltp - active_sl) / active_sl * 100 if active_sl > 0 else 0
            return ltp > active_sl and gap_pct > 0.5

    def square_off_all(self) -> None:
        if not self.pm.open_positions:
            return
        log.info("SQUARE OFF: closing %d positions", self.pm.open_count)
        for pos in self.pm.open_positions:
            ltp = pos.current_price if pos.current_price > 0 else pos.entry_price
            self._exit_position(pos, ltp, "SQUARE_OFF")

    def _exit_position(self, pos: Position, price: float, reason: str) -> None:
        exit_tag = f"{pos.strategy}_{pos.symbol}_{int(time.time())}_{reason.lower()}"
        exit_signal = Signal(
            strategy=pos.strategy,
            symbol=pos.symbol,
            security_id=pos.security_id,
            exchange_segment=pos.exchange_segment,
            side="EXIT",
            quantity=pos.quantity,
            price=0,
            tag=exit_tag,
        )
        self.om.place_exit(exit_signal, pos.direction)
        trade = self.pm.close_position(pos.security_id, price, reason)
        if trade:
            if self.guardrails:
                est_margin = pos.entry_price * pos.quantity
                self.guardrails.release_allocation(pos.symbol, est_margin)
            duration = ""
            if hasattr(trade, "entry_time") and hasattr(trade, "exit_time"):
                try:
                    dur = trade.exit_time - trade.entry_time
                    mins = int(dur.total_seconds() // 60)
                    if mins >= 60:
                        duration = f"{mins // 60}h {mins % 60}m"
                    else:
                        duration = f"{mins}m"
                except Exception:
                    pass
            self.tg.sl_hit(
                trade.symbol, trade.pnl,
                reason=reason,
                entry=pos.entry_price,
                exit_price=price,
                duration=duration,
            )
            self._record_to_memory(pos, trade, reason)

    def _record_to_memory(self, pos: Position, trade, reason: str) -> None:
        if not self.memory:
            return
        try:
            ctx = self._signal_context.pop(pos.symbol, self._signal_context.pop(pos.security_id, {}))
            trade_ctx = self.memory.build_trade_context(
                symbol=pos.symbol, direction=pos.direction,
                option_type=pos.option_type,
                entry_price=pos.entry_price, exit_price=trade.exit_price,
                sl_price=pos.sl_price, pnl=trade.pnl,
                entry_time=pos.entry_time, exit_time=trade.exit_time,
                exit_reason=reason,
                signal_score=ctx.get("signal_score", 0),
                signal_confidence=ctx.get("signal_confidence", "LOW"),
                adx_value=ctx.get("adx_value", 0),
                vwap_position=ctx.get("vwap_position", "UNKNOWN"),
                supertrend_dir=ctx.get("supertrend_dir", "UNKNOWN"),
                volume_ratio=ctx.get("volume_ratio", 1.0),
                atr_value=ctx.get("atr_value", 0),
                strategy=pos.strategy,
            )
            self.memory.record_trade(trade_ctx)

            if self.memory.trade_count % 10 == 0:
                self.memory.analyze_patterns()
                health = self.memory.get_health_report()
                if health["status"] in ("CRITICAL", "DEGRADED"):
                    self.tg.error("MEMORY_HEALTH",
                                  Exception(f"System health: {health['status']} — "
                                            f"{'; '.join(health['alerts'])}"))
        except Exception as e:
            log.error("Failed to record trade to memory: %s", e)

    def auto_select_instrument(self) -> str | None:
        from core.signal_generator import calc_adx, calc_atr, SignalGenerator
        scores = []
        for name in self.instruments:
            adx_val = 25.0
            vol_rank = 0.5
            try:
                sg = SignalGenerator(self.instruments, poll_interval=5)
                candles = sg.fetch_candles(name)
                if candles and len(candles) > 20:
                    adx_vals = calc_adx(candles, 14)
                    adx_val = adx_vals[-1] if adx_vals else 25.0
                    atr_vals = calc_atr(candles, 14)
                    atr_pct = (atr_vals[-1] / candles[-1].close * 100) if candles[-1].close > 0 and atr_vals[-1] > 0 else 0.5
                    vol_rank = min(atr_pct / 2.0, 1.0)
            except Exception:
                pass
            scores.append(self.ins_sel.score(
                name=name,
                adx=adx_val,
                volume_rank=vol_rank,
                iv_percentile=40.0,
                spread_pct=0.3,
            ))
        if scores:
            best = self.ins_sel.select_best(scores)
            return best.name
        return None

    def _resolve_instrument(self, symbol: str) -> str | None:
        upper = symbol.upper()
        if upper in self.instruments:
            return upper
        best, best_len = None, 0
        for name in self.instruments:
            if name in upper and len(name) > best_len:
                best, best_len = name, len(name)
        return best

    def _compute_atm_strike(self, inst_name: str, strike_step: int) -> int:
        cfg = self.instruments.get(inst_name, {})
        sec_id = cfg.get("dhan_security_id", "")
        exchange = "NSE_FNO" if cfg.get("exchange") == "NSE" else "BSE_FNO"
        try:
            ltp = self.broker.get_ltp(sec_id, exchange)
            if ltp > 0:
                return round(ltp / strike_step) * strike_step
        except Exception:
            pass
        return 0

    def _lookup_security_id(self, option_symbol: str, exchange_segment: str) -> str:
        return option_symbol
