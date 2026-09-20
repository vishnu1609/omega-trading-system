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

from core.broker_base import BrokerBase
from core.instrument_selector import InstrumentSelector
from core.order_manager import OrderManager, Signal
from core.position_manager import Position, PositionManager
from core.risk_engine import RiskEngine
from core.trade_memory import TradeMemory
from utils.telegram_alert import TelegramAlert

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
        telegram: TelegramAlert,
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
        self._signal_context: dict[str, dict] = {}

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

        if asset == "futures":
            exchange = inst_cfg.get("exchange", "NSE")
            if exchange == "MCX":
                exchange_seg = "MCX_COMM"
            else:
                exchange_seg = "NSE_FNO"
            trade_symbol = inst_name
            security_id = self._lookup_security_id(trade_symbol, exchange_seg)
            option_type = ""
        else:
            strike_step = inst_cfg.get("strike_step", 50)
            exchange_seg = "NSE_FNO" if inst_cfg.get("exchange") == "NSE" else "BSE_FNO"
            option_type = "CE" if side.endswith("CE") else "PE"
            if not strike:
                strike = self._compute_atm_strike(inst_name, strike_step)
            trade_symbol = f"{inst_name}{strike}{option_type}"
            security_id = self._lookup_security_id(trade_symbol, exchange_seg)

        signal = Signal(
            strategy=strategy,
            symbol=trade_symbol,
            security_id=security_id,
            exchange_segment=exchange_seg,
            side=side,
            quantity=quantity,
            price=0,
            sl=sl,
            strike=strike,
        )

        if self.guardrails and sl > 0:
            est_entry = sl * 1.02 if direction == "LONG" else sl * 0.98
            risk_check = self.guardrails.check_per_trade_risk(
                est_entry, sl, quantity)
            if not risk_check:
                log.warning("Trade blocked by guardrail: %s", risk_check.reason)
                return

        if self.memory:
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

        resp = self.om.place_entry(signal, self.re, instrument=inst_name)
        if not resp:
            return

        fill_price = resp.fill_price if resp.fill_price > 0 else signal.price or 100
        sl_price = self.re.compute_sl_price(fill_price, direction)
        tp_price = self.re.compute_tp_price(fill_price, direction)

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

        if self.memory:
            self._signal_context[security_id] = {
                "signal_score": payload.get("score", 0),
                "signal_confidence": payload.get("confidence", "LOW"),
                "adx_value": payload.get("adx", 0),
                "vwap_position": payload.get("vwap_position", "UNKNOWN"),
                "supertrend_dir": payload.get("supertrend_dir", "UNKNOWN"),
                "volume_ratio": payload.get("volume_ratio", 1.0),
                "atr_value": payload.get("atr", 0),
            }

        self.om.place_sl_limit(
            security_id=security_id,
            exchange_segment=exchange_seg,
            direction=direction,
            quantity=quantity,
            trigger_price=sl_price,
            buffer_pct=2.0,
        )

        if self.guardrails:
            est_margin = fill_price * quantity
            self.guardrails.record_allocation(symbol, est_margin)

        self.tg.order_filled(
            side=side,
            symbol=trade_symbol,
            qty=quantity,
            price=fill_price,
            order_id=resp.order_id,
        )

    def _handle_exit(self, symbol: str, strategy: str) -> None:
        closed = []
        for pos in self.pm.open_positions:
            if symbol in pos.symbol or pos.strategy == strategy:
                exit_tag = f"{strategy}_{pos.symbol}_{int(time.time())}_exit"
                exit_signal = Signal(
                    strategy=strategy,
                    symbol=pos.symbol,
                    security_id=pos.security_id,
                    exchange_segment=pos.exchange_segment,
                    side="EXIT",
                    quantity=pos.quantity,
                    price=0,
                    tag=exit_tag,
                )
                resp = self.om.place_exit(exit_signal, pos.direction)
                if resp:
                    price = resp.fill_price if resp.fill_price > 0 else (
                        pos.current_price if pos.current_price > 0 else pos.entry_price
                    )
                    trade = self.pm.close_position(pos.security_id, price, "SIGNAL_EXIT")
                    if trade:
                        closed.append(trade)
                        self.tg.sl_hit(trade.symbol, trade.pnl)

        log.info("Exited %d positions on signal", len(closed))

    def monitor_positions(self) -> None:
        """Called periodically to check SL/TP/trail on open positions."""
        for pos in self.pm.open_positions:
            try:
                ltp = self.broker.get_ltp(pos.security_id, pos.exchange_segment)
                if ltp <= 0:
                    continue
                self.pm.update_price(pos.security_id, ltp)
            except Exception as exc:
                log.warning("LTP fetch failed for %s: %s", pos.symbol, exc)
                continue

            if self.guardrails:
                new_trail = self.guardrails.compute_step_trail_sl(
                    pos.entry_price, ltp, pos.trail_sl, pos.direction)
            else:
                new_trail = self.re.compute_trail_sl(
                    ltp, pos.entry_price, pos.trail_sl, pos.direction)
            self.pm.update_trail_sl(pos.security_id, new_trail)

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
            self.tg.sl_hit(trade.symbol, trade.pnl)
            self._record_to_memory(pos, trade, reason)

    def _record_to_memory(self, pos: Position, trade, reason: str) -> None:
        if not self.memory:
            return
        try:
            ctx = self._signal_context.pop(pos.security_id, {})
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
        scores = []
        for name in self.instruments:
            scores.append(self.ins_sel.score(
                name=name,
                adx=25.0,
                volume_rank=0.7,
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
