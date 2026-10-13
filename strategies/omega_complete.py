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
from core.database import OmegaDB
from core.greeks import compute_greeks, dte_from_expiry, should_exit_spread_by_greeks
from core.wallet import Wallet
try:
    from utils.imessage_alert import IMessageAlert
except ImportError:
    IMessageAlert = None
from utils.telegram_alert import TelegramAlert
try:
    from ml.engine import OmegaMLEngine
except ImportError:
    OmegaMLEngine = None

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

DYNAMIC_SLTP_LEVELS = {
    "LOW": [
        {"level": 1, "sl_pct": 6.0, "tp_pct": 7.0},
        {"level": 2, "sl_pct": 7.0, "tp_pct": 10.0},
        {"level": 3, "sl_pct": 10.0, "tp_pct": 12.5},
        {"level": 4, "sl_pct": 12.5, "tp_pct": 15.0},
    ],
    "MEDIUM": [
        {"level": 1, "sl_pct": 6.0, "tp_pct": 9.0},
        {"level": 2, "sl_pct": 9.0, "tp_pct": 13.0},
        {"level": 3, "sl_pct": 13.0, "tp_pct": 17.0},
        {"level": 4, "sl_pct": 17.0, "tp_pct": 22.0},
    ],
    "HIGH": [
        {"level": 1, "sl_pct": 8.0, "tp_pct": 15.0},
        {"level": 2, "sl_pct": 15.0, "tp_pct": 20.0},
        {"level": 3, "sl_pct": 20.0, "tp_pct": 26.0},
        {"level": 4, "sl_pct": 26.0, "tp_pct": 33.0},
    ],
}

SHORT_DYNAMIC_SLTP_LEVELS = {
    "LOW": [
        {"level": 1, "sl_pct": 12.0, "tp_pct": 5.0},
        {"level": 2, "sl_pct": 5.0, "tp_pct": 8.0},
        {"level": 3, "sl_pct": 8.0, "tp_pct": 12.0},
        {"level": 4, "sl_pct": 12.0, "tp_pct": 15.0},
    ],
    "MEDIUM": [
        {"level": 1, "sl_pct": 15.0, "tp_pct": 7.0},
        {"level": 2, "sl_pct": 7.0, "tp_pct": 11.0},
        {"level": 3, "sl_pct": 11.0, "tp_pct": 15.0},
        {"level": 4, "sl_pct": 15.0, "tp_pct": 20.0},
    ],
    "HIGH": [
        {"level": 1, "sl_pct": 18.0, "tp_pct": 10.0},
        {"level": 2, "sl_pct": 10.0, "tp_pct": 15.0},
        {"level": 3, "sl_pct": 15.0, "tp_pct": 20.0},
        {"level": 4, "sl_pct": 20.0, "tp_pct": 25.0},
    ],
}

SL_GRACE_SECONDS = 120
MIN_OPTION_ATR_PCT = 5.0
BREAKEVEN_PROFIT_PCT = 5.0


class OmegaStrategy:
    """Processes webhook signals and routes through risk → order → broker."""

    def __init__(
        self,
        broker: BrokerBase,
        order_manager: OrderManager,
        position_manager: PositionManager,
        risk_engine: RiskEngine,
        instrument_selector: InstrumentSelector,
        telegram: IMessageAlert | TelegramAlert,
        instruments_config: dict,
        default_qty_lots: int = 1,
        mode: str = "paper",
        guardrails=None,
        trade_memory: TradeMemory | None = None,
        wallet: Wallet | None = None,
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
        self.wallet = wallet
        self.option_chain: OptionChainResolver | None = None
        self.live_feed = None
        self.ml_engine: OmegaMLEngine | None = None
        if OmegaMLEngine:
            try:
                self.ml_engine = OmegaMLEngine(min_confidence=0.55, auto_train=True)
                log.info("ML Engine loaded: %s", self.ml_engine.get_model_stats())
            except Exception as e:
                log.warning("ML Engine init failed (trading continues without ML): %s", e)
                self.ml_engine = None
        self._signal_context: dict[str, dict] = {}
        self._underlying_cache: dict[str, tuple[float, float]] = {}
        self._consecutive_losses: int = 0
        self._last_loss_time: float = 0.0

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

        confidence = payload.get("confidence", "LOW")
        score = payload.get("score", 0)
        log.info("Signal received: %s %s strike=%s sl=%s asset=%s conf=%s score=%.0f",
                 side, symbol, strike, sl, asset, confidence, score)

        underlying_close = payload.get("close", 0)

        if side == "EXIT":
            self._handle_exit(symbol, strategy)
            return

        if side in ("SELL_CE_SPREAD", "SELL_PE_SPREAD"):
            self._handle_spread_entry(payload)
            return

        now_ist = datetime.now(IST)
        hour, minute = now_ist.hour, now_ist.minute
        time_minutes = hour * 60 + minute
        is_sell_entry = side.startswith("SELL")
        is_buy_option = side in ("BUY_CE", "BUY_PE")

        if time_minutes < 9 * 60 + 20:
            log.info("OPENING RANGE FILTER: blocking %s %s before 09:20 IST — first 5 min too choppy (%02d:%02d)",
                     side, symbol, hour, minute)
            return

        if is_sell_entry and time_minutes < 11 * 60 + 30:
            log.info("THETA FILTER: blocking SELL entry before 11:30 IST "
                     "(theta decay too slow at %02d:%02d)", hour, minute)
            return
        if is_sell_entry and time_minutes >= 12 * 60 + 30:
            score = payload.get("score", 0) + 5
            payload["score"] = score
            log.info("THETA BOOST: +5 score for SELL entry after 12:30 IST (accelerated decay)")
        if is_buy_option and time_minutes >= 14 * 60 + 30:
            score = payload.get("score", 0) - 10
            payload["score"] = score
            log.info("THETA PENALTY: -10 score for option BUY after 14:30 IST "
                     "(rapid time decay erodes premium, score now %.0f)", score)

        if "CE" in side:
            score = payload.get("score", 0) + 3
            payload["score"] = score
            log.info("CE BOOST: +3 score for CE trade (CE historically outperforms PE, score=%.0f)", score)

        min_score = 45 if self.mode == "paper" else 55
        if self._consecutive_losses >= 2:
            elapsed = time.time() - self._last_loss_time
            if elapsed < 900:
                remaining = int((900 - elapsed) / 60)
                log.info("LOSS COOLDOWN: %d consecutive losses — %d min cooldown remaining, blocking %s %s",
                         self._consecutive_losses, remaining, side, symbol)
                return
            if confidence == "LOW":
                log.info("LOSS FILTER: %d consecutive losses — skipping LOW confidence %s %s (only MEDIUM/HIGH allowed)",
                         self._consecutive_losses, side, symbol)
                return
            min_score = max(min_score, 55)
        if self._consecutive_losses >= 4:
            min_score = max(min_score, 60)
        if "BANKNIFTY" in symbol:
            min_score += 10
            log.info("BANKNIFTY GATE: +10 min_score (net loser historically, need higher conviction, min_score=%d)", min_score)
        if score < min_score:
            log.info("Signal rejected: %s %s — confidence=%s score=%.0f (need score>=%.0f, mode=%s)",
                     side, symbol, confidence, score, min_score, self.mode)
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
            same_index = False
            corr_count = 0
            for pos in self.pm.open_positions:
                pos_base = None
                for c in correlated:
                    if c in pos.symbol.upper():
                        pos_base = c
                        break
                if pos_base:
                    if pos_base == inst_name.upper() and pos.direction == direction:
                        same_index = True
                    if pos.direction == direction:
                        corr_count += 1
            if same_index:
                log.warning("Same-index position blocked: %s %s already open", direction, inst_name)
                return
            if corr_count >= 2:
                log.warning("Max 2 correlated positions: %s %s blocked (%d already open)",
                            direction, inst_name, corr_count)
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

        vol_regime = payload.get("volatility_regime", "NORMAL")

        if self.ml_engine and self.live_feed:
            try:
                candles = getattr(self.live_feed, 'candle_buffer', [])
                if candles and len(candles) >= 50:
                    from ml.features import Candle as MLCandle
                    ml_candles = [MLCandle(c.timestamp, c.open, c.high, c.low, c.close, c.volume)
                                  for c in candles] if not isinstance(candles[0], MLCandle) else candles
                    ml_result = self.ml_engine.evaluate_signal(
                        ml_candles, signal_direction=direction, signal_score=score)
                    self._signal_context[trade_symbol] = ml_result
                    if not ml_result["approved"]:
                        log.info("ML GATE: %s %s BLOCKED — %s (confidence=%.2f, regime=%s)",
                                 side, symbol, ml_result["reason"],
                                 ml_result["confidence"], ml_result["regime"])
                        return
                    if ml_result.get("regime_adjustments"):
                        adj = ml_result["regime_adjustments"]
                        vol_regime = ml_result["regime"]
                        log.info("ML regime=%s: %s", ml_result["regime"], adj.get("notes", ""))
            except Exception as e:
                log.warning("ML evaluation error (continuing without ML): %s", e)

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
                entry_hour=now.hour, entry_day=now.weekday(),
                volatility_regime=vol_regime)
            if blocked:
                log.warning("Trade blocked by memory: %s", block_reason)
                self.tg.error("MEMORY_BLOCK", Exception(block_reason))
                return

            adj, adj_reasons = self.memory.get_score_adjustment(
                symbol=trade_symbol, side=side,
                entry_hour=now.hour, entry_day=now.weekday(),
                volatility_regime=vol_regime)
            if adj_reasons:
                log.info("Memory adjustments: %s", "; ".join(adj_reasons))

            size_mod = self.memory.get_position_size_modifier(trade_symbol)
            if size_mod < 1.0:
                original_qty = quantity
                quantity = max(int(quantity * size_mod), inst_cfg.get("lot_size", 75))
                signal.quantity = quantity
                log.info("Position size reduced by memory: %d → %d", original_qty, quantity)

        regime_size_mult = {"HIGH_VOL": 0.5, "CHOPPY": 0.7, "NORMAL": 1.0, "TRENDING": 1.2}.get(vol_regime, 1.0)
        if regime_size_mult != 1.0:
            original_qty = quantity
            quantity = max(lot_size, int(quantity * regime_size_mult) // lot_size * lot_size)
            signal.quantity = quantity
            if quantity != original_qty:
                log.info("REGIME sizing (%s): %d → %d (×%.1f)", vol_regime, original_qty, quantity, regime_size_mult)

        score = payload.get("score", 0)
        atr_for_sizing = payload.get("atr", 0)
        confidence_for_risk = payload.get("confidence", "LOW")
        if atr_for_sizing > 0 and entry_price > 0:
            risk_pct = {"HIGH": 3.0, "MEDIUM": 2.0, "LOW": 1.0}.get(confidence_for_risk, 1.0)
            sl_pct_for_sizing = {"HIGH": 8.0, "MEDIUM": 6.0, "LOW": 5.0}.get(confidence_for_risk, 5.0)
            sl_mult_for_sizing = (entry_price * sl_pct_for_sizing / 100) / atr_for_sizing if atr_for_sizing > 0 else 1.5
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

        if contract and hasattr(self.broker, "cache_contract"):
            underlying_price_for_slip = sl * 1.01 if sl > 0 else entry_price * 100
            moneyness = abs(contract.strike - underlying_price_for_slip) / underlying_price_for_slip * 100 if underlying_price_for_slip > 0 else 0
            self.broker.cache_contract(
                contract.security_id,
                bid=contract.bid, ask=contract.ask, oi=contract.oi,
                moneyness_pct=moneyness,
                dte_days=dte_from_expiry(contract.expiry) if contract.expiry else 30,
            )

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

        level_table = SHORT_DYNAMIC_SLTP_LEVELS if direction == "SHORT" else DYNAMIC_SLTP_LEVELS
        levels = level_table.get(confidence, level_table["LOW"])
        sl_pct = levels[0]["sl_pct"]
        tp_pct = levels[0]["tp_pct"]

        if self._consecutive_losses >= 4:
            quantity = max(lot_size, (quantity // 3) // lot_size * lot_size)
            signal.quantity = quantity
            sl_pct = max(sl_pct - 2.0, 3.0)
            log.info("LOSS RECOVERY SEVERE: %d losses — qty to 1/3 (%d), SL tightened to %.1f%%",
                     self._consecutive_losses, quantity, sl_pct)
        elif self._consecutive_losses >= 3:
            quantity = max(lot_size, (quantity // 2) // lot_size * lot_size)
            signal.quantity = quantity
            sl_pct = max(sl_pct - 1.0, 3.0)
            log.info("LOSS RECOVERY: %d losses — qty halved to %d, SL tightened to %.1f%%",
                     self._consecutive_losses, quantity, sl_pct)

        india_vix = payload.get("india_vix", 0)
        if india_vix > 0 and india_vix < 12 and direction == "SHORT":
            quantity = max(lot_size, (quantity * 7 // 10) // lot_size * lot_size)
            signal.quantity = quantity
            log.info("LOW VIX SIZING: VIX=%.1f < 12, SELL qty reduced to %d (low premium)", india_vix, quantity)

        if "PE" in side:
            quantity = max(lot_size, (quantity * 8 // 10) // lot_size * lot_size)
            signal.quantity = quantity
            log.info("PE SIZE REDUCTION: PE qty reduced to %d (PE historically underperforms CE)", quantity)

        if direction == "LONG":
            sl_price = round(fill_price * (1 - sl_pct / 100), 2)
            tp_price = round(fill_price * (1 + tp_pct / 100), 2)
        else:
            sl_price = round(fill_price * (1 + sl_pct / 100), 2)
            tp_price = round(fill_price * (1 - tp_pct / 100), 2)

        log.info("Pct-based SL/TP: %s → SL=%.2f (-%s%%) TP=%.2f (+%s%%)",
                 confidence, sl_price, sl_pct, tp_price, tp_pct)

        option_atr = fill_price * MIN_OPTION_ATR_PCT / 100
        if atr_value > 0 and fill_price > 0:
            delta_est = 0.5
            estimated_premium_atr = atr_value * delta_est
            option_atr = max(option_atr, min(estimated_premium_atr, fill_price * 0.15))

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
            peak_price=fill_price,
            strategy=strategy,
            atr_value=option_atr,
            original_quantity=quantity,
            confidence=confidence,
            expiry=contract.expiry if contract else "",
            underlying=contract.underlying if contract else "",
            strike=contract.strike if contract else 0,
            iv=contract.iv if contract else 0.0,
        )
        self.pm.add_position(position)
        self.re.record_trade_taken()

        if self.wallet:
            margin = fill_price * quantity
            self.wallet.deduct_margin(margin, trade_symbol)

        try:
            db = OmegaDB.get()
            db.upsert_position(
                security_id=security_id, symbol=trade_symbol,
                exchange_segment=exchange_seg, option_type=option_type,
                direction=direction, quantity=quantity,
                entry_price=fill_price, current_price=0,
                sl_price=position.sl_price, trail_sl=position.trail_sl,
                tp_price=position.tp_price, peak_price=fill_price,
                entry_order_id=resp.order_id, strategy=strategy,
                entry_time=position.entry_time.isoformat(),
                dynamic_level=1,
            )
        except Exception as e:
            log.warning("DB position upsert failed: %s", e)

        if self.live_feed and contract and contract.security_id.isdigit():
            from core.live_feed import EXCHANGE_SEG_MAP
            seg_int = EXCHANGE_SEG_MAP.get(exchange_seg, 2)
            self.live_feed.subscribe(
                [(seg_int, contract.security_id, 15)])
            log.info("LiveFeed subscribed: %s sid=%s",
                     trade_symbol, contract.security_id)

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
                "volatility_regime": payload.get("volatility_regime", "NORMAL"),
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
        now = datetime.now(IST)
        for pos in self.pm.open_positions:
            if symbol in pos.symbol or pos.strategy == strategy:
                hold_minutes = (now - pos.entry_time).total_seconds() / 60
                if hold_minutes < 10:
                    log.info("SIGNAL_EXIT blocked for %s — held only %.0f min (need 10 min minimum hold)",
                             pos.symbol, hold_minutes)
                    continue

                if pos.dynamic_level >= 2:
                    log.info("SIGNAL_EXIT blocked for %s — at L%d with locked profit, letting levels manage",
                             pos.symbol, pos.dynamic_level)
                    continue

                ltp = self.broker.get_ltp(pos.security_id, pos.exchange_segment)
                if ltp <= 0:
                    ltp = pos.current_price
                cur_pnl_pct = ((ltp - pos.entry_price) / pos.entry_price * 100
                               if pos.is_long else
                               (pos.entry_price - ltp) / pos.entry_price * 100)
                if cur_pnl_pct > 0:
                    log.info("SIGNAL_EXIT skipped for %s — in profit %.1f%%, letting trail manage",
                             pos.symbol, cur_pnl_pct)
                    continue

                unrealized_loss = abs(ltp - pos.entry_price) * pos.quantity
                if unrealized_loss < 200:
                    log.info("SIGNAL_EXIT skipped for %s — loss Rs %.0f < Rs 200 threshold, letting SL handle",
                             pos.symbol, unrealized_loss)
                    continue

                exit_price = pos.current_price if pos.current_price > 0 else pos.entry_price
                ltp = self.broker.get_ltp(pos.security_id, pos.exchange_segment)
                if ltp > 0:
                    exit_price = ltp
                if pos.is_spread:
                    self._exit_spread(pos, exit_price, "SIGNAL_EXIT")
                else:
                    self._exit_position(pos, exit_price, "SIGNAL_EXIT")
                closed.append(pos.symbol)

        log.info("Exited %d positions on signal", len(closed))

    def _handle_spread_entry(self, payload: dict) -> None:
        symbol = payload["symbol"]
        side = payload["side"]
        option_type = "CE" if "CE" in side else "PE"
        confidence = payload.get("confidence", "MEDIUM")
        score = payload.get("score", 0)

        inst_name = self._resolve_instrument(symbol)
        if not inst_name:
            log.warning("Unknown instrument for spread: %s", symbol)
            return

        inst_cfg = self.instruments.get(inst_name, {})
        lot_size = inst_cfg.get("lot_size", 75)
        quantity = lot_size * self.default_qty_lots
        strike_step = inst_cfg.get("strike_step", 50)

        if not self.option_chain:
            log.warning("No option chain resolver — cannot place spread")
            return

        underlying_price = payload.get("close", 0)
        if underlying_price <= 0:
            underlying_price = payload.get("sl", 0) * 1.01
        if underlying_price <= 0:
            log.warning("No underlying price for spread on %s", inst_name)
            return

        spread = self.option_chain.resolve_spread_pair(
            underlying=inst_name,
            underlying_price=underlying_price,
            option_type=option_type,
            sell_otm_steps=2,
            hedge_otm_steps=6,
            lot_size=lot_size,
            strike_step=strike_step,
        )
        if not spread:
            log.warning("Spread pair resolution failed for %s %s", inst_name, option_type)
            return

        sell_contract, hedge_contract = spread
        net_credit = sell_contract.mid_price - hedge_contract.mid_price
        spread_width = abs(sell_contract.strike - hedge_contract.strike)
        max_loss_per_unit = spread_width - net_credit

        if max_loss_per_unit <= 0:
            log.warning("Invalid spread: max_loss=%.2f (width=%d - credit=%.2f)",
                        max_loss_per_unit, spread_width, net_credit)
            return

        spread_count = sum(1 for p in self.pm.open_positions if p.is_spread)
        if spread_count >= 2:
            log.warning("Max 2 concurrent spreads reached — blocking %s %s", side, inst_name)
            return

        if self.guardrails:
            risk_check = self.guardrails.check_spread_risk(max_loss_per_unit, quantity)
            if not risk_check:
                log.warning("Spread blocked by guardrail: %s", risk_check.reason)
                return

        if hasattr(self.broker, "cache_contract"):
            underlying_price = sell_contract.strike
            for c in (sell_contract, hedge_contract):
                mn = abs(c.strike - underlying_price) / underlying_price * 100 if underlying_price > 0 else 0
                self.broker.cache_contract(
                    c.security_id,
                    bid=c.bid, ask=c.ask, oi=c.oi,
                    moneyness_pct=mn,
                    dte_days=dte_from_expiry(c.expiry) if c.expiry else 30,
                )

        sell_signal = Signal(
            strategy="omega_spread", symbol=sell_contract.trade_symbol,
            security_id=sell_contract.security_id,
            exchange_segment=sell_contract.exchange_segment,
            side="SELL_" + option_type, quantity=quantity,
            price=sell_contract.mid_price, sl=0, strike=sell_contract.strike,
        )
        sell_resp = self.om.place_entry(sell_signal, self.re, instrument=inst_name)
        if not sell_resp:
            log.warning("Sell leg order failed for %s", sell_contract.trade_symbol)
            return

        hedge_signal = Signal(
            strategy="omega_spread", symbol=hedge_contract.trade_symbol,
            security_id=hedge_contract.security_id,
            exchange_segment=hedge_contract.exchange_segment,
            side="BUY_" + option_type, quantity=quantity,
            price=hedge_contract.mid_price, sl=0, strike=hedge_contract.strike,
        )
        hedge_resp = self.om.place_entry(hedge_signal, self.re, instrument=inst_name)
        if not hedge_resp:
            log.warning("Hedge leg order failed for %s — unwinding sell leg", hedge_contract.trade_symbol)
            unwind = Signal(
                strategy="omega_spread", symbol=sell_contract.trade_symbol,
                security_id=sell_contract.security_id,
                exchange_segment=sell_contract.exchange_segment,
                side="EXIT", quantity=quantity, price=0,
            )
            self.om.place_exit(unwind, "SHORT")
            return

        sell_fill = sell_resp.fill_price if sell_resp.fill_price > 0 else sell_contract.mid_price
        hedge_fill = hedge_resp.fill_price if hedge_resp.fill_price > 0 else hedge_contract.mid_price
        actual_credit = sell_fill - hedge_fill
        actual_max_loss = spread_width - actual_credit

        level_table = SHORT_DYNAMIC_SLTP_LEVELS
        levels = level_table.get(confidence, level_table["LOW"])
        sl_pct = levels[0]["sl_pct"]
        tp_pct = levels[0]["tp_pct"]

        sl_price = round(sell_fill * (1 + sl_pct / 100), 2)
        tp_price = round(sell_fill * (1 - tp_pct / 100), 2)

        position = Position(
            symbol=sell_contract.trade_symbol,
            security_id=sell_contract.security_id,
            exchange_segment=sell_contract.exchange_segment,
            option_type=option_type,
            direction="SHORT",
            quantity=quantity,
            entry_price=sell_fill,
            sl_price=sl_price,
            trail_sl=sl_price,
            tp_price=tp_price,
            entry_order_id=sell_resp.order_id,
            peak_price=sell_fill,
            strategy="omega_spread",
            original_quantity=quantity,
            confidence=confidence,
            expiry=sell_contract.expiry,
            underlying=sell_contract.underlying,
            strike=sell_contract.strike,
            iv=sell_contract.iv,
            is_spread=True,
            hedge_security_id=hedge_contract.security_id,
            hedge_trade_symbol=hedge_contract.trade_symbol,
            hedge_strike=hedge_contract.strike,
            spread_width=spread_width,
            net_credit=round(actual_credit, 2),
            max_loss=round(actual_max_loss, 2),
            hedge_entry_price=hedge_fill,
        )
        self.pm.add_position(position)
        self.re.record_trade_taken()

        if self.wallet:
            self.wallet.deduct_spread_margin(actual_max_loss, quantity, sell_contract.trade_symbol)

        if self.guardrails:
            self.guardrails.record_allocation(symbol, actual_max_loss * quantity)

        log.info("SPREAD OPENED: SELL %s @ ₹%.2f / BUY %s @ ₹%.2f | "
                 "credit=₹%.2f, max_loss=₹%.2f/unit, width=%d, qty=%d",
                 sell_contract.trade_symbol, sell_fill,
                 hedge_contract.trade_symbol, hedge_fill,
                 actual_credit, actual_max_loss, spread_width, quantity)

        self.tg.order_filled(
            side=side, symbol=f"{sell_contract.trade_symbol}/{hedge_contract.trade_symbol}",
            qty=quantity, price=sell_fill, order_id=sell_resp.order_id,
            strike=f"{sell_contract.strike}/{hedge_contract.strike}",
            expiry=sell_contract.expiry,
            iv=sell_contract.iv, oi=sell_contract.oi, score=score,
        )

    def _exit_spread(self, pos: Position, sell_ltp: float, reason: str) -> None:
        hedge_ltp = self.broker.get_ltp(pos.hedge_security_id, pos.exchange_segment)
        if hedge_ltp <= 0:
            hedge_ltp = pos.hedge_current_price if pos.hedge_current_price > 0 else pos.hedge_entry_price

        unwind_sell = Signal(
            strategy=pos.strategy, symbol=pos.symbol,
            security_id=pos.security_id, exchange_segment=pos.exchange_segment,
            side="EXIT", quantity=pos.quantity, price=0,
        )
        self.om.place_exit(unwind_sell, "SHORT")

        unwind_hedge = Signal(
            strategy=pos.strategy, symbol=pos.hedge_trade_symbol,
            security_id=pos.hedge_security_id, exchange_segment=pos.exchange_segment,
            side="EXIT", quantity=pos.quantity, price=0,
        )
        self.om.place_exit(unwind_hedge, "LONG")

        sell_pnl = (pos.entry_price - sell_ltp) * pos.quantity
        hedge_pnl = (hedge_ltp - pos.hedge_entry_price) * pos.quantity
        total_pnl = sell_pnl + hedge_pnl

        trade = self.pm.close_position(pos.security_id, sell_ltp, reason)
        if trade:
            trade.pnl = round(total_pnl, 2)
            if trade.pnl >= 0:
                self._consecutive_losses = 0
            else:
                self._consecutive_losses += 1
                self._last_loss_time = time.time()

            if self.guardrails:
                self.guardrails.release_allocation(pos.symbol, pos.max_loss * pos.quantity)
            if self.wallet:
                margin = pos.max_loss * pos.quantity
                self.wallet.release_margin(margin, trade.pnl, pos.symbol, reason)

            try:
                db = OmegaDB.get()
                db.record_trade(
                    symbol=f"{pos.symbol}/{pos.hedge_trade_symbol}",
                    direction="SHORT_SPREAD", option_type=pos.option_type,
                    quantity=pos.quantity, entry_price=pos.entry_price,
                    exit_price=sell_ltp, pnl=trade.pnl,
                    entry_time=trade.entry_time.isoformat(),
                    exit_time=trade.exit_time.isoformat(),
                    exit_reason=reason, strategy=pos.strategy,
                    balance_after=self.wallet.balance if self.wallet else 0,
                )
                db.delete_position(pos.security_id)
            except Exception as e:
                log.warning("DB spread trade record failed: %s", e)

            log.info("SPREAD CLOSED: %s/%s pnl=₹%.2f (sell=₹%.2f, hedge=₹%.2f) reason=%s",
                     pos.symbol, pos.hedge_trade_symbol, total_pnl, sell_pnl, hedge_pnl, reason)

            self.tg.sl_hit(
                f"{pos.symbol}/{pos.hedge_trade_symbol}", trade.pnl,
                reason=reason, entry=pos.entry_price, exit_price=sell_ltp,
                duration=f"credit=₹{pos.net_credit:.2f}",
            )

    def monitor_positions(self) -> None:
        """Called every 1s. Implements 5-layer profit engine:
        1. Breakeven lockdown at 3% profit
        2. Scale-out in 3 tranches
        3. Time-decay targets (options theta)
        4. Momentum hold near TP
        5. Loss recovery mode (managed externally via _consecutive_losses)
        """
        if hasattr(self.broker, 'check_pending_sl_orders'):
            self.broker.check_pending_sl_orders()
        now = datetime.now(IST)
        for pos in self.pm.open_positions:
            try:
                ltp = 0.0
                if self.live_feed:
                    ltp = self.live_feed.get_ltp(pos.security_id, pos.exchange_segment)
                if ltp <= 0:
                    ltp = self.broker.get_ltp(pos.security_id, pos.exchange_segment)
                if ltp <= 0:
                    continue
                self.pm.update_price(pos.security_id, ltp)
            except Exception as exc:
                log.warning("LTP fetch failed for %s: %s", pos.symbol, exc)
                continue

            if pos.is_spread and pos.hedge_security_id:
                try:
                    hedge_ltp = 0.0
                    if self.live_feed:
                        hedge_ltp = self.live_feed.get_ltp(pos.hedge_security_id, pos.exchange_segment)
                    if hedge_ltp <= 0:
                        hedge_ltp = self.broker.get_ltp(pos.hedge_security_id, pos.exchange_segment)
                    if hedge_ltp > 0:
                        pos.hedge_current_price = hedge_ltp
                except Exception:
                    pass

                spot = self._underlying_spot(pos.underlying)
                if pos.expiry and pos.net_credit > 0 and spot > 0 and pos.iv > 0:
                    try:
                        dte = dte_from_expiry(pos.expiry)
                        sell_g = compute_greeks(spot, pos.strike, dte, pos.iv, pos.option_type)
                        hedge_g = compute_greeks(spot, pos.hedge_strike, dte, pos.iv, pos.option_type)
                        current_net = ltp - pos.hedge_current_price
                        should_exit, exit_reason = should_exit_spread_by_greeks(
                            sell_g, hedge_g, pos.net_credit, current_net, dte, spot)
                        if should_exit:
                            log.info("GREEKS EXIT: %s %s reason=%s dte=%.1f theta=%.2f",
                                     pos.direction, pos.symbol, exit_reason, dte, sell_g.theta)
                            self._exit_spread(pos, ltp, exit_reason)
                            continue
                    except Exception as e:
                        log.debug("Greeks calc skipped for %s: %s", pos.symbol, e)

            hold_seconds = (now - pos.entry_time).total_seconds()
            hold_minutes = hold_seconds / 60

            profit_pct = ((ltp - pos.entry_price) / pos.entry_price * 100
                          if pos.is_long else
                          (pos.entry_price - ltp) / pos.entry_price * 100)
            pos.peak_profit_pct = max(pos.peak_profit_pct, profit_pct)

            min_atr = pos.entry_price * MIN_OPTION_ATR_PCT / 100
            premium_atr = max(pos.atr_value, min_atr) if pos.atr_value > 0 else min_atr
            is_bought_option = pos.direction == "LONG" and ("CE" in pos.symbol or "PE" in pos.symbol)
            if is_bought_option:
                premium_atr *= 1.3

            level_table = SHORT_DYNAMIC_SLTP_LEVELS if pos.direction == "SHORT" else DYNAMIC_SLTP_LEVELS
            levels = level_table.get(pos.confidence, level_table["LOW"])
            past_all_levels = pos.max_dynamic_level > 0

            trail_eval = self.re.evaluate_trail(
                current_price=ltp,
                entry_price=pos.entry_price,
                existing_sl=pos.trail_sl,
                direction=pos.direction,
                peak_price=pos.peak_price,
                atr_value=premium_atr,
                hold_seconds=hold_seconds,
            )
            pos.peak_price = trail_eval["peak"]

            new_trail = trail_eval["sl"]
            old_trail = pos.trail_sl

            if past_all_levels:
                tight_width = 1.5
                peak = pos.peak_price
                if pos.is_long:
                    atr_trail = round(peak - (tight_width * premium_atr), 2)
                    if atr_trail > new_trail:
                        new_trail = atr_trail
                else:
                    atr_trail = round(peak + (tight_width * premium_atr), 2)
                    if new_trail <= 0 or atr_trail < new_trail:
                        new_trail = atr_trail

            # --- 1. BREAKEVEN LOCKDOWN ---
            if not pos.breakeven_locked and profit_pct >= BREAKEVEN_PROFIT_PCT:
                be_buffer = max(premium_atr * 0.5, pos.entry_price * 0.015)
                if pos.is_long:
                    be_price = round(pos.entry_price + be_buffer, 2)
                    if be_price > new_trail:
                        new_trail = be_price
                else:
                    be_price = round(pos.entry_price - be_buffer, 2)
                    if new_trail <= 0 or be_price < new_trail:
                        new_trail = be_price
                pos.breakeven_locked = True
                log.info("BREAKEVEN LOCKED: %s %s profit=%.1f%% → SL moved to %.2f (entry=%.2f, buffer=%.2f)",
                         pos.direction, pos.symbol, profit_pct, be_price, pos.entry_price, be_buffer)

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

            if trail_eval["detail"]:
                log.info("Trail: %s %s hold=%.0fs pATR=%.2f profit=%.1f%% | %s",
                         pos.direction, pos.symbol, hold_seconds,
                         premium_atr, profit_pct, trail_eval["detail"])

            # --- 2. SCALE-OUT IN 3 TRANCHES ---
            lot_size = self._get_lot_size(pos.symbol)
            tranche_1_pct = {"HIGH": 5.0, "MEDIUM": 4.0, "LOW": 3.5}.get(pos.confidence, 3.5)
            tranche_2_pct = {"HIGH": 9.0, "MEDIUM": 7.0, "LOW": 5.5}.get(pos.confidence, 5.5)

            if pos.tranche_exits == 0 and profit_pct >= tranche_1_pct:
                exit_qty = max(lot_size, (pos.quantity // 3) // lot_size * lot_size)
                if exit_qty < pos.quantity:
                    log.info("TRANCHE 1: %s %s profit=%.1f%% ≥ %.0f%% — exit %d of %d @ %.2f",
                             pos.direction, pos.symbol, profit_pct, tranche_1_pct,
                             exit_qty, pos.quantity, ltp)
                    self._partial_exit(pos, exit_qty, ltp)
                    pos.tranche_exits = 1

            elif pos.tranche_exits == 1 and profit_pct >= tranche_2_pct:
                exit_qty = max(lot_size, (pos.quantity // 2) // lot_size * lot_size)
                if exit_qty < pos.quantity:
                    log.info("TRANCHE 2: %s %s profit=%.1f%% ≥ %.0f%% — exit %d of %d @ %.2f",
                             pos.direction, pos.symbol, profit_pct, tranche_2_pct,
                             exit_qty, pos.quantity, ltp)
                    self._partial_exit(pos, exit_qty, ltp)
                    pos.tranche_exits = 2

            # --- 3. TIME-DECAY TARGETS (theta awareness) ---
            effective_tp = pos.tp_price
            if pos.tp_price > 0 and hold_minutes > 60:
                if hold_minutes > 120:
                    decay_tp = pos.entry_price + (pos.tp_price - pos.entry_price) * 0.50 if pos.is_long else \
                        pos.entry_price - (pos.entry_price - pos.tp_price) * 0.50
                else:
                    decay_tp = pos.entry_price + (pos.tp_price - pos.entry_price) * 0.75 if pos.is_long else \
                        pos.entry_price - (pos.entry_price - pos.tp_price) * 0.75
                active_sl = pos.trail_sl if pos.trail_sl > 0 else pos.sl_price
                if pos.is_long:
                    min_tp = active_sl + premium_atr * 0.5
                    effective_tp = max(decay_tp, min_tp)
                else:
                    max_tp = active_sl - premium_atr * 0.5
                    effective_tp = min(decay_tp, max_tp) if max_tp > 0 else decay_tp

            # --- GREED EXTENSION: disabled when dynamic levels handle escalation ---
            if past_all_levels and not pos.tp_extended and pos.confidence in ("HIGH", "MEDIUM"):
                greed_thresholds = {"HIGH": (15.0, 20.0), "MEDIUM": (10.0, 12.0)}
                check_pct, extend_pct = greed_thresholds[pos.confidence]
                if profit_pct >= check_pct:
                    if pos.is_long:
                        new_tp = round(pos.entry_price * (1 + extend_pct / 100), 2)
                    else:
                        new_tp = round(pos.entry_price * (1 - extend_pct / 100), 2)
                    log.info("GREED EXTEND: %s %s profit=%.1f%% ≥ %.0f%% → TP %.2f → %.2f (+%.0f%%)",
                             pos.confidence, pos.symbol, profit_pct, check_pct, pos.tp_price, new_tp, extend_pct)
                    pos.tp_price = new_tp
                    effective_tp = new_tp
                    pos.tp_extended = True

            # --- 4. MOMENTUM HOLD: only in ATR trail mode, not during level-ups ---
            tp_hit = self._check_tp_hit(pos, ltp, effective_tp)
            if tp_hit and past_all_levels and profit_pct > pos.peak_profit_pct - 0.3:
                if hold_minutes < 120:
                    log.info("MOMENTUM HOLD: %s %s profit=%.1f%% (peak=%.1f%%) — still climbing, delaying TP",
                             pos.direction, pos.symbol, profit_pct, pos.peak_profit_pct)
                    tp_hit = False

            # --- EXIT CHECKS ---
            if pos.is_sl_hit and hold_seconds >= SL_GRACE_SECONDS:
                log.info("SL HIT: %s %s @ %.2f (SL=%.2f) pnl=%.1f%% hold=%.0fs level=L%d",
                         pos.direction, pos.symbol, ltp, pos.trail_sl, profit_pct, hold_seconds, pos.dynamic_level)
                if pos.is_spread:
                    self._exit_spread(pos, ltp, "TRAIL_SL")
                else:
                    self._exit_position(pos, ltp, "TRAIL_SL")
            elif pos.is_sl_hit and hold_seconds < SL_GRACE_SECONDS:
                log.info("SL GRACE: %s %s SL hit at %.2f but hold=%.0fs < %ds grace — holding",
                         pos.direction, pos.symbol, ltp, hold_seconds, SL_GRACE_SECONDS)

            elif tp_hit:
                level_table = SHORT_DYNAMIC_SLTP_LEVELS if pos.direction == "SHORT" else DYNAMIC_SLTP_LEVELS
                levels = level_table.get(pos.confidence, level_table["LOW"])
                start_level = pos.dynamic_level
                jumped = False
                while pos.dynamic_level < len(levels):
                    next_level = levels[pos.dynamic_level]
                    new_tp_pct = next_level["tp_pct"]
                    if pos.is_long:
                        candidate_tp = round(pos.entry_price * (1 + new_tp_pct / 100), 2)
                    else:
                        candidate_tp = round(pos.entry_price * (1 - new_tp_pct / 100), 2)
                    past_this = (ltp >= candidate_tp) if pos.is_long else (ltp <= candidate_tp)
                    new_sl = pos.tp_price
                    pos.sl_price = new_sl
                    pos.trail_sl = new_sl
                    pos.tp_price = candidate_tp
                    pos.dynamic_level += 1
                    jumped = True
                    if not past_this:
                        break

                if jumped:
                    self.pm.update_trail_sl(pos.security_id, pos.trail_sl)
                    sl_pct_actual = abs(pos.trail_sl - pos.entry_price) / pos.entry_price * 100
                    tp_pct_actual = abs(pos.tp_price - pos.entry_price) / pos.entry_price * 100
                    if pos.dynamic_level - start_level > 1:
                        log.info("MULTI-LEVEL JUMP: %s %s L%d→L%d profit=%.1f%% | SL=%.2f (%.1f%%) TP=%.2f (%.1f%%)",
                                 pos.direction, pos.symbol, start_level, pos.dynamic_level,
                                 profit_pct, pos.trail_sl, sl_pct_actual, pos.tp_price, tp_pct_actual)
                    else:
                        log.info("LEVEL UP: %s %s L%d→L%d profit=%.1f%% | SL=%.2f (%.1f%%) TP=%.2f (%.1f%%)",
                                 pos.direction, pos.symbol, start_level, pos.dynamic_level,
                                 profit_pct, pos.trail_sl, sl_pct_actual, pos.tp_price, tp_pct_actual)
                    if pos.sl_order_id:
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
                        except Exception as e:
                            log.error("Level-up SL order sync failed: %s", e)
                    self.tg.sl_hit(pos.symbol, 0, reason=f"LEVEL_UP_L{pos.dynamic_level}",
                                   entry=pos.entry_price, exit_price=ltp,
                                   duration=f"L{start_level}→L{pos.dynamic_level} SL={sl_pct_actual:.1f}% TP={tp_pct_actual:.1f}%")
                    try:
                        db = OmegaDB.get()
                        db.upsert_position(
                            security_id=pos.security_id,
                            sl_price=pos.sl_price, trail_sl=pos.trail_sl,
                            tp_price=pos.tp_price, dynamic_level=pos.dynamic_level,
                        )
                    except Exception as e:
                        log.warning("DB level-up update failed: %s", e)

                if pos.dynamic_level >= len(levels) and self._check_tp_hit(pos, ltp, pos.tp_price):
                    pos.tp_price = 0
                    pos.max_dynamic_level = pos.dynamic_level
                    log.info("ATR TRAIL MODE: %s %s passed L%d (profit=%.1f%%) — no more levels, ATR 1.5x trailing",
                             pos.direction, pos.symbol, pos.dynamic_level, profit_pct)
                    self.tg.sl_hit(pos.symbol, 0, reason="ATR_TRAIL_MODE",
                                   entry=pos.entry_price, exit_price=ltp,
                                   duration=f"Past L{pos.dynamic_level}, ATR trailing")

            elif self._is_sl_skipped(pos, ltp):
                log.critical("SL-SKIP FREEFALL: %s %s LTP=%.2f fell past SL=%.2f — "
                             "emergency exit at LTP-1%%",
                             pos.direction, pos.symbol, ltp, pos.trail_sl)
                if pos.is_spread:
                    self._exit_spread(pos, ltp, "SL_SKIP_EMERGENCY")
                else:
                    self._exit_position(pos, ltp, "SL_SKIP_EMERGENCY")

    _INDEX_SEC_IDS = {"NIFTY": "13", "BANKNIFTY": "25", "SENSEX": "51",
                      "FINNIFTY": "27", "BANKEX": "12"}

    def _underlying_spot(self, underlying: str) -> float:
        sec_id = self._INDEX_SEC_IDS.get((underlying or "").upper())
        if not sec_id:
            return 0.0
        try:
            if self.live_feed:
                return self.live_feed.get_ltp(sec_id, "IDX_I")
        except Exception:
            pass
        return 0.0

    def _check_tp_hit(self, pos, ltp: float, decayed_tp: float) -> bool:
        if decayed_tp <= 0 or ltp <= 0:
            return False
        if pos.direction == "LONG":
            return ltp >= decayed_tp
        return ltp <= decayed_tp

    def _get_lot_size(self, symbol: str) -> int:
        upper = symbol.upper()
        for name, cfg in self.instruments.items():
            if name in upper:
                return cfg.get("lot_size", 75)
        return 75

    def _partial_exit(self, pos, exit_qty: int, price: float) -> None:
        exit_tag = f"{pos.strategy}_{pos.symbol}_{int(time.time())}_partial"
        exit_signal = Signal(
            strategy=pos.strategy,
            symbol=pos.symbol,
            security_id=pos.security_id,
            exchange_segment=pos.exchange_segment,
            side="EXIT",
            quantity=exit_qty,
            price=0,
            tag=exit_tag,
        )
        self.om.place_exit(exit_signal, pos.direction)

        pnl = (price - pos.entry_price) * exit_qty if pos.direction == "LONG" \
            else (pos.entry_price - price) * exit_qty
        pos.quantity -= exit_qty
        pos.partial_exit_done = True

        if self.wallet:
            margin_released = pos.entry_price * exit_qty
            self.wallet.release_margin(margin_released, pnl, pos.symbol, "PARTIAL_TP")

        self.tg.sl_hit(pos.symbol, round(pnl, 2), reason="PARTIAL_TP",
                       entry=pos.entry_price, exit_price=price,
                       duration=f"partial {exit_qty}qty")

        log.info("PARTIAL EXIT done: %s pnl=₹%.2f, remaining qty=%d",
                 pos.symbol, pnl, pos.quantity)

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
            ltp = self.broker.get_ltp(pos.security_id, pos.exchange_segment)
            if ltp <= 0:
                ltp = pos.current_price if pos.current_price > 0 else pos.entry_price
            if pos.is_spread:
                self._exit_spread(pos, ltp, "SQUARE_OFF")
            else:
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
            if trade.pnl >= 0:
                self._consecutive_losses = 0
            else:
                self._consecutive_losses += 1
                self._last_loss_time = time.time()
                if self._consecutive_losses >= 4:
                    log.warning("LOSS RECOVERY SEVERE: %d consecutive losses — next: 1/3 size, score>=60, 15-min cooldown, MEDIUM/HIGH only",
                                self._consecutive_losses)
                elif self._consecutive_losses >= 2:
                    log.warning("LOSS RECOVERY: %d consecutive losses — 15-min cooldown, MEDIUM/HIGH only, score>=55",
                                self._consecutive_losses)
            if reason in ("TRAIL_SL", "SL_SKIP_EMERGENCY") and trade.pnl < 0:
                self.re.record_symbol_sl(pos.symbol)
            if self.guardrails:
                est_margin = pos.entry_price * pos.quantity
                self.guardrails.release_allocation(pos.symbol, est_margin)
            if self.wallet:
                margin = pos.entry_price * pos.quantity
                self.wallet.release_margin(margin, trade.pnl, pos.symbol, reason)
            try:
                db = OmegaDB.get()
                db.record_trade(
                    symbol=trade.symbol, direction=trade.direction,
                    option_type=trade.option_type, quantity=trade.quantity,
                    entry_price=trade.entry_price, exit_price=trade.exit_price,
                    pnl=trade.pnl,
                    entry_time=trade.entry_time.isoformat(),
                    exit_time=trade.exit_time.isoformat(),
                    exit_reason=reason, strategy=pos.strategy,
                    balance_after=self.wallet.balance if self.wallet else 0,
                )
                db.delete_position(pos.security_id)
            except Exception as e:
                log.warning("DB trade record failed: %s", e)
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
                volatility_regime=ctx.get("volatility_regime", "NORMAL"),
            )
            self.memory.record_trade(trade_ctx)

            if self.ml_engine and self.live_feed:
                try:
                    candles = getattr(self.live_feed, 'candle_buffer', [])
                    if candles and len(candles) >= 50:
                        from ml.features import Candle as MLCandle
                        ml_candles = [MLCandle(c.timestamp, c.open, c.high, c.low, c.close, c.volume)
                                      for c in candles] if not isinstance(candles[0], MLCandle) else candles
                        is_win = trade.pnl > 0
                        self.ml_engine.record_trade_outcome(
                            ml_candles, is_win=is_win, pnl=trade.pnl,
                            entry_time=str(pos.entry_time), symbol=pos.symbol)
                except Exception as e:
                    log.warning("ML outcome recording failed: %s", e)

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
