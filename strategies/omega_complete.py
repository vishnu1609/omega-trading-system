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
        {"level": 1, "sl_pct": 10.0, "tp_pct": 20.0},
        {"level": 2, "sl_pct": 12.0, "tp_pct": 28.0},
        {"level": 3, "sl_pct": 15.0, "tp_pct": 35.0},
        {"level": 4, "sl_pct": 18.0, "tp_pct": 42.0},
    ],
    "MEDIUM": [
        {"level": 1, "sl_pct": 10.0, "tp_pct": 25.0},
        {"level": 2, "sl_pct": 13.0, "tp_pct": 33.0},
        {"level": 3, "sl_pct": 16.0, "tp_pct": 40.0},
        {"level": 4, "sl_pct": 20.0, "tp_pct": 48.0},
    ],
    "HIGH": [
        {"level": 1, "sl_pct": 12.0, "tp_pct": 30.0},
        {"level": 2, "sl_pct": 15.0, "tp_pct": 40.0},
        {"level": 3, "sl_pct": 18.0, "tp_pct": 50.0},
        {"level": 4, "sl_pct": 22.0, "tp_pct": 60.0},
    ],
}

SHORT_DYNAMIC_SLTP_LEVELS = {
    "LOW": [
        {"level": 1, "sl_pct": 25.0, "tp_pct": 8.0},
        {"level": 2, "sl_pct": 15.0, "tp_pct": 15.0},
        {"level": 3, "sl_pct": 18.0, "tp_pct": 22.0},
        {"level": 4, "sl_pct": 22.0, "tp_pct": 30.0},
    ],
    "MEDIUM": [
        {"level": 1, "sl_pct": 30.0, "tp_pct": 10.0},
        {"level": 2, "sl_pct": 18.0, "tp_pct": 18.0},
        {"level": 3, "sl_pct": 22.0, "tp_pct": 25.0},
        {"level": 4, "sl_pct": 25.0, "tp_pct": 35.0},
    ],
    "HIGH": [
        {"level": 1, "sl_pct": 35.0, "tp_pct": 12.0},
        {"level": 2, "sl_pct": 20.0, "tp_pct": 20.0},
        {"level": 3, "sl_pct": 25.0, "tp_pct": 30.0},
        {"level": 4, "sl_pct": 30.0, "tp_pct": 40.0},
    ],
}

# Spread-specific levels: tighter SL (cap at 150% of net credit), wider TP (capture 60%+ of credit)
SPREAD_SLTP_LEVELS = {
    "LOW": [
        {"level": 1, "sl_pct": 120.0, "tp_pct": 50.0},
        {"level": 2, "sl_pct": 80.0, "tp_pct": 65.0},
        {"level": 3, "sl_pct": 60.0, "tp_pct": 80.0},
    ],
    "MEDIUM": [
        {"level": 1, "sl_pct": 150.0, "tp_pct": 55.0},
        {"level": 2, "sl_pct": 100.0, "tp_pct": 70.0},
        {"level": 3, "sl_pct": 80.0, "tp_pct": 85.0},
    ],
    "HIGH": [
        {"level": 1, "sl_pct": 150.0, "tp_pct": 60.0},
        {"level": 2, "sl_pct": 100.0, "tp_pct": 75.0},
        {"level": 3, "sl_pct": 80.0, "tp_pct": 90.0},
    ],
}

EQUITY_DYNAMIC_SLTP_LEVELS = {
    "LOW": [
        {"level": 1, "sl_pct": 2.0, "tp_pct": 4.0},
        {"level": 2, "sl_pct": 2.5, "tp_pct": 6.0},
        {"level": 3, "sl_pct": 3.0, "tp_pct": 8.0},
        {"level": 4, "sl_pct": 3.5, "tp_pct": 10.0},
    ],
    "MEDIUM": [
        {"level": 1, "sl_pct": 2.5, "tp_pct": 5.0},
        {"level": 2, "sl_pct": 3.0, "tp_pct": 7.0},
        {"level": 3, "sl_pct": 3.5, "tp_pct": 10.0},
        {"level": 4, "sl_pct": 4.0, "tp_pct": 13.0},
    ],
    "HIGH": [
        {"level": 1, "sl_pct": 3.0, "tp_pct": 6.0},
        {"level": 2, "sl_pct": 3.5, "tp_pct": 9.0},
        {"level": 3, "sl_pct": 4.0, "tp_pct": 12.0},
        {"level": 4, "sl_pct": 5.0, "tp_pct": 15.0},
    ],
}

EQUITY_SHORT_DYNAMIC_SLTP_LEVELS = {
    "LOW": [
        {"level": 1, "sl_pct": 2.5, "tp_pct": 3.0},
        {"level": 2, "sl_pct": 2.0, "tp_pct": 5.0},
        {"level": 3, "sl_pct": 2.5, "tp_pct": 7.0},
        {"level": 4, "sl_pct": 3.0, "tp_pct": 9.0},
    ],
    "MEDIUM": [
        {"level": 1, "sl_pct": 3.0, "tp_pct": 4.0},
        {"level": 2, "sl_pct": 2.5, "tp_pct": 6.0},
        {"level": 3, "sl_pct": 3.0, "tp_pct": 8.0},
        {"level": 4, "sl_pct": 3.5, "tp_pct": 11.0},
    ],
    "HIGH": [
        {"level": 1, "sl_pct": 3.5, "tp_pct": 5.0},
        {"level": 2, "sl_pct": 3.0, "tp_pct": 7.0},
        {"level": 3, "sl_pct": 3.5, "tp_pct": 10.0},
        {"level": 4, "sl_pct": 4.0, "tp_pct": 13.0},
    ],
}

STOCK_OPTION_DYNAMIC_SLTP_LEVELS = {
    "LOW": [
        {"level": 1, "sl_pct": 8.0, "tp_pct": 15.0},
        {"level": 2, "sl_pct": 10.0, "tp_pct": 22.0},
        {"level": 3, "sl_pct": 12.0, "tp_pct": 28.0},
        {"level": 4, "sl_pct": 15.0, "tp_pct": 35.0},
    ],
    "MEDIUM": [
        {"level": 1, "sl_pct": 10.0, "tp_pct": 20.0},
        {"level": 2, "sl_pct": 12.0, "tp_pct": 28.0},
        {"level": 3, "sl_pct": 15.0, "tp_pct": 35.0},
        {"level": 4, "sl_pct": 18.0, "tp_pct": 42.0},
    ],
    "HIGH": [
        {"level": 1, "sl_pct": 12.0, "tp_pct": 25.0},
        {"level": 2, "sl_pct": 15.0, "tp_pct": 35.0},
        {"level": 3, "sl_pct": 18.0, "tp_pct": 42.0},
        {"level": 4, "sl_pct": 22.0, "tp_pct": 50.0},
    ],
}

SL_GRACE_SECONDS = 180
MIN_OPTION_ATR_PCT = 5.0
MIN_EQUITY_ATR_PCT = 1.5
BREAKEVEN_PROFIT_PCT = 12.0  # aligned with 0.8R deadband — chandelier handles the rest
EQUITY_BREAKEVEN_PROFIT_PCT = 3.0
MAX_RISK_PER_TRADE = 1500.0
ATR_SL_MULTIPLIER = 1.5
ATR_SL_FLOOR = {"NIFTY": 15.0, "BANKNIFTY": 35.0, "SENSEX": 20.0, "FINNIFTY": 15.0, "BANKEX": 15.0}


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
                self.ml_engine = OmegaMLEngine(min_confidence=0.60, auto_train=True)
                log.info("ML Engine loaded: %s", self.ml_engine.get_model_stats())
            except Exception as e:
                log.warning("ML Engine init failed (trading continues without ML): %s", e)
                self.ml_engine = None
        self._signal_context: dict[str, dict] = {}
        self._underlying_cache: dict[str, tuple[float, float]] = {}
        self._consecutive_losses: int = 0
        self._consecutive_wins: int = 0
        self._last_loss_time: float = 0.0
        self._symbol_loss_cooldown: dict[str, float] = {}  # underlying → timestamp of last loss
        self._recent_signal_dirs: dict[str, tuple[str, float]] = {}  # symbol → (direction, timestamp)

    def _get_level_table(self, asset: str, direction: str, is_spread: bool = False) -> dict:
        """Pick the right SL/TP level table based on asset class and direction."""
        if is_spread:
            return SPREAD_SLTP_LEVELS
        if asset == "equity":
            return EQUITY_SHORT_DYNAMIC_SLTP_LEVELS if direction == "SHORT" else EQUITY_DYNAMIC_SLTP_LEVELS
        elif asset == "stock_options":
            return SHORT_DYNAMIC_SLTP_LEVELS if direction == "SHORT" else STOCK_OPTION_DYNAMIC_SLTP_LEVELS
        else:
            return SHORT_DYNAMIC_SLTP_LEVELS if direction == "SHORT" else DYNAMIC_SLTP_LEVELS

    def _record_signal_audit(self, payload: dict, status: str, reason: str = "", trade_symbol: str = "") -> None:
        payload["execution_status"] = status
        payload["status_reason"] = reason
        if trade_symbol:
            payload["resolved_symbol"] = trade_symbol
        try:
            db = OmegaDB.get()
            details = {
                "status": status,
                "reason": reason,
                "resolved_symbol": trade_symbol,
                "adx": payload.get("adx", 0),
                "rsi": payload.get("rsi", 50),
                "vwap_position": payload.get("vwap_position", ""),
                "supertrend_dir": payload.get("supertrend_dir", ""),
                "volatility_regime": payload.get("volatility_regime", "NORMAL"),
                "score_details": payload.get("score_details", {}),
            }
            db.record_signal(
                symbol=payload.get("symbol", ""),
                side=payload.get("side", ""),
                strategy=payload.get("strategy", "omega_complete"),
                score=float(payload.get("score", 0)),
                confidence=payload.get("confidence", "LOW"),
                asset=payload.get("asset", "options"),
                details=details,
            )
        except Exception:
            pass
        if self.ml_engine:
            try:
                candles = None
                inst_name = payload.get("symbol", "")
                if hasattr(self, 'sig_gen') and self.sig_gen and hasattr(self.sig_gen, '_candle_store'):
                    candles = self.sig_gen._candle_store.get(inst_name)
                if not candles and self.live_feed:
                    candles = getattr(self.live_feed, 'candle_buffer', None)
                if candles:
                    from ml.features import Candle as MLCandle
                    if not isinstance(candles[0], MLCandle):
                        candles = [MLCandle(
                            c.timestamp.timestamp() if hasattr(c.timestamp, 'timestamp') else float(c.timestamp),
                            c.open, c.high, c.low, c.close, c.volume) for c in candles]
                self.ml_engine.record_signal(payload, status, candles)
            except Exception:
                pass

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

        self.tg.signal_detected(
            symbol=symbol, side=side, score=score,
            adx=payload.get("adx", 0), strike=strike,
            premium=payload.get("entry_price", 0),
            confidence=confidence,
            regime=payload.get("vol_regime", ""),
            rsi=payload.get("rsi", 0),
            supertrend=payload.get("supertrend_direction", ""),
            vwap=payload.get("vwap_position", ""),
            choppiness=payload.get("choppiness", 0),
            atr=payload.get("atr", 0),
            close=payload.get("close", 0),
            oi=payload.get("oi", 0),
            iv=payload.get("iv", 0),
        )

        underlying_close = payload.get("close", 0)

        if side == "EXIT":
            self._handle_exit(symbol, strategy)
            return

        if side in ("SELL_CE_SPREAD", "SELL_PE_SPREAD"):
            self._handle_spread_entry(payload)
            return

        # --- SELL/SHORT block: only allow BUY_CE and BUY_PE for now ---
        if side.startswith("SELL") and side not in ("SELL_CE_SPREAD", "SELL_PE_SPREAD"):
            log.info("SELL BLOCKED: naked %s %s — only BUY orders allowed (building sell strategy)", side, symbol)
            self._record_signal_audit(payload, "BLOCKED_SELL_DISABLED", "Naked sell disabled")
            return

        now_ist = datetime.now(IST)
        hour, minute = now_ist.hour, now_ist.minute
        time_minutes = hour * 60 + minute
        is_sell_entry = side.startswith("SELL")
        is_buy_option = side in ("BUY_CE", "BUY_PE")

        warmup_cutoff = 9 * 60 + 30 if self.mode == "paper" else 10 * 60
        if time_minutes < warmup_cutoff:
            log.info("WARMUP FILTER: blocking %s %s before %s IST (%02d:%02d)",
                     side, symbol, "09:30" if self.mode == "paper" else "10:00", hour, minute)
            self._record_signal_audit(payload, "BLOCKED_WARMUP", f"Before {'09:30' if self.mode == 'paper' else '10:00'} IST")
            return

        if is_sell_entry and time_minutes < 11 * 60 + 30:
            log.info("THETA FILTER: blocking SELL entry before 11:30 IST "
                     "(theta decay too slow at %02d:%02d)", hour, minute)
            return
        if is_sell_entry and time_minutes >= 12 * 60 + 30:
            score = payload.get("score", 0) + 5
            payload["score"] = score
            log.info("THETA BOOST: +5 score for SELL entry after 12:30 IST (accelerated decay)")
        late_cutoff = 14 * 60 + 30 if self.mode == "paper" else 14 * 60
        if is_buy_option and time_minutes >= late_cutoff:
            cutoff_str = "14:30" if self.mode == "paper" else "14:00"
            log.info("LATE ENTRY BLOCK: blocking %s %s after %s IST — theta crush zone", side, symbol, cutoff_str)
            self._record_signal_audit(payload, "BLOCKED_LATE_ENTRY", f"After {cutoff_str} IST")
            return

        # --- CHOPPY REGIME BLOCK: no directional buys in choppy markets ---
        vol_regime_early = payload.get("volatility_regime", "NORMAL")
        if is_buy_option and vol_regime_early == "CHOPPY":
            log.info("CHOPPY REGIME BLOCK: %s %s — regime=%s, directional buys disabled (use spreads)",
                     side, symbol, vol_regime_early)
            self._record_signal_audit(payload, "BLOCKED_CHOPPY_REGIME", f"Directional buy in {vol_regime_early}")
            return

        # --- PER-UNDERLYING COOLDOWN: 30-min block after a loss on same underlying ---
        if is_buy_option:
            underlying = self._resolve_instrument(symbol) or symbol
            last_loss = self._symbol_loss_cooldown.get(underlying, 0)
            cooldown_secs = 1800  # 30 minutes
            elapsed = time.time() - last_loss
            if last_loss > 0 and elapsed < cooldown_secs:
                remaining = int((cooldown_secs - elapsed) / 60)
                log.info("SYMBOL COOLDOWN: %s blocked — %s lost %d min ago, %d min remaining",
                         symbol, underlying, int(elapsed / 60), remaining)
                self._record_signal_audit(payload, "BLOCKED_SYMBOL_COOLDOWN",
                                          f"{underlying} loss cooldown, {remaining}min left")
                return

        # --- TIME-OF-DAY SCORING (U-shaped intraday persistence) ---
        # Morning momentum 09:20-10:45: 60% persistence → easier entry
        # Late morning 10:45-11:30: transition → slightly harder
        # Midday lull 11:30-13:15: 51.8% persistence (random walk) → block weak signals
        # Afternoon build 13:15-14:00: pre-breakout watch
        # Late session 14:00-15:10: 59% persistence → easier entry
        tod_mod = 0
        if is_buy_option:
            if time_minutes <= 10 * 60 + 45:
                tod_mod = 3  # morning momentum window
            elif time_minutes < 11 * 60 + 30:
                tod_mod = -2  # transition — slightly harder
            elif time_minutes <= 13 * 60 + 15:
                tod_mod = -8  # midday lull — strong penalty for directional buys
            elif time_minutes < 14 * 60:
                tod_mod = -1  # afternoon build
            else:
                tod_mod = 2  # late session momentum
            if tod_mod != 0:
                score = payload.get("score", 0) + tod_mod
                payload["score"] = score
                log.info("TOD MODIFIER: %+d score at %02d:%02d (%s, score=%.0f)",
                         tod_mod, hour, minute,
                         "morning" if tod_mod > 0 and time_minutes < 11*60 else
                         "midday_lull" if tod_mod < -5 else
                         "late_session" if tod_mod > 0 else "transition",
                         score)

        weekday = now_ist.weekday()
        if weekday == 0:
            score = payload.get("score", 0) + 3
            payload["score"] = score
            log.info("MONDAY EDGE: +3 score (Monday reversal tendency, score=%.0f)", score)

        if self.guardrails and hasattr(self.guardrails, "check_chop_window"):
            chop_check = self.guardrails.check_chop_window(symbol, score=score, now=now_ist)
            if not chop_check:
                log.info("CHOP LOCKOUT: %s", chop_check.reason)
                self._record_signal_audit(payload, "BLOCKED_CHOP", chop_check.reason)
                return

        # --- Multi-timeframe RSI filter ---
        rsi_5m = payload.get("rsi", 50)
        rsi_15m = payload.get("rsi_15m", rsi_5m)
        if side.startswith("BUY_C") and (rsi_5m < 30 or rsi_15m < 25):
            log.info("RSI FILTER: BUY CE blocked — RSI5m=%.0f RSI15m=%.0f (need >=30/25)", rsi_5m, rsi_15m)
            self._record_signal_audit(payload, "BLOCKED_RSI", f"BUY CE RSI5={rsi_5m:.0f} RSI15={rsi_15m:.0f}")
            return
        if side.startswith("BUY_P") and (rsi_5m > 70 or rsi_15m > 75):
            log.info("RSI FILTER: BUY PE blocked — RSI5m=%.0f RSI15m=%.0f (need <=70/75)", rsi_5m, rsi_15m)
            self._record_signal_audit(payload, "BLOCKED_RSI", f"BUY PE RSI5={rsi_5m:.0f} RSI15={rsi_15m:.0f}")
            return

        if "PE" in side:
            score = payload.get("score", 0) + 3
            payload["score"] = score
            log.info("PE BOOST: +3 score (PE historically +₹5,972 vs CE -₹3,985, score=%.0f)", score)
        if "CE" in side:
            score = payload.get("score", 0) - 1
            payload["score"] = score
            log.info("CE PENALTY: -1 score (CE historically weaker, score=%.0f)", score)

        min_score = 45 if self.mode == "paper" else 55
        vol_regime = payload.get("volatility_regime", "NORMAL")

        if confidence == "LOW" and score < 55:
            log.info("LOW CONFIDENCE BLOCKED: %s %s score=%.0f < 55 — LOW needs higher score",
                     side, symbol, score)
            self._record_signal_audit(payload, "BLOCKED_LOW_CONFIDENCE", f"LOW conf score={score:.0f}")
            return

        if self._consecutive_losses >= 6:
            elapsed = time.time() - self._last_loss_time
            if elapsed < 1800:
                remaining = int((1800 - elapsed) / 60)
                log.info("HARD COOLDOWN: %d consecutive losses — %d min pause remaining, blocking %s %s",
                         self._consecutive_losses, remaining, side, symbol)
                self._record_signal_audit(payload, "BLOCKED_HARD_COOLDOWN", f"{self._consecutive_losses} losses, {remaining}min left")
                return
            min_score = max(min_score, 62)
        elif self._consecutive_losses >= 4:
            elapsed = time.time() - self._last_loss_time
            if elapsed < 600:
                remaining = int((600 - elapsed) / 60)
                log.info("LOSS COOLDOWN: %d consecutive losses — %d min cooldown remaining, blocking %s %s",
                         self._consecutive_losses, remaining, side, symbol)
                self._record_signal_audit(payload, "BLOCKED_LOSS_COOLDOWN", f"{self._consecutive_losses} losses, {remaining}min left")
                return
            min_score = max(min_score, 58)
        if "BANKNIFTY" in symbol and self.mode != "paper":
            min_score += 2
            log.info("BANKNIFTY GATE: +2 min_score (min_score=%d)", min_score)
        if "SENSEX" in symbol.upper():
            sensex_min = 55 if self.mode == "paper" else 60
            min_score = max(min_score, sensex_min)
            log.info("SENSEX GATE: min_score raised to %d (wider spreads, lower liquidity)", min_score)
        if score < min_score:
            log.info("Signal rejected: %s %s — confidence=%s score=%.0f (need score>=%.0f, mode=%s)",
                     side, symbol, confidence, score, min_score, self.mode)
            self._record_signal_audit(payload, "BLOCKED_SCORE", f"score {score:.0f} < {min_score:.0f}")
            return

        inst_name = self._resolve_instrument(symbol)
        if not inst_name:
            log.warning("Unknown instrument for symbol %s", symbol)
            return

        # --- CROSS-INDEX DIVERGENCE VETO ---
        # Track recent signal directions; block if this index opposes another within 5 min
        sig_dir = "CE" if "CE" in side else "PE"
        self._recent_signal_dirs[inst_name] = (sig_dir, time.time())
        if is_buy_option and score < 70:
            cross_indices = {"NIFTY", "BANKNIFTY", "SENSEX"} - {inst_name}
            for other in cross_indices:
                other_dir, other_ts = self._recent_signal_dirs.get(other, ("", 0))
                if other_dir and (time.time() - other_ts) < 300:  # within 5 min
                    if other_dir != sig_dir:
                        log.warning("DIVERGENCE VETO: %s %s opposes recent %s %s signal — indices diverging",
                                    inst_name, sig_dir, other, other_dir)
                        self._record_signal_audit(payload, "BLOCKED_DIVERGENCE",
                                                  f"{inst_name} {sig_dir} vs {other} {other_dir}")
                        return

        inst_cfg = self.instruments.get(inst_name, {})
        lot_size = inst_cfg.get("lot_size", 65)
        # Prefer instrument master (daily Dhan CSV) for lot size — handles SEBI transitions
        if self.option_chain:
            im_lot = self.option_chain.resolve_lot_size(inst_name, fallback=lot_size)
            if im_lot != lot_size:
                log.info("LOT SIZE: %s instrument master=%d (config=%d) — using master", inst_name, im_lot, lot_size)
                lot_size = im_lot
        conf_lots = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}.get(confidence, 1) if self.mode == "paper" else self.default_qty_lots
        if conf_lots == 0:
            log.info("LOW CONFIDENCE SKIP: %s %s — not trading LOW confidence signals", side, symbol)
            self._record_signal_audit(payload, "BLOCKED_LOW_CONF_SIZING", "LOW confidence = 0 lots")
            return
        quantity = lot_size * conf_lots
        # Anti-martingale: scale UP after consecutive wins (house money effect)
        if self._consecutive_wins >= 3 and self.mode == "paper":
            extra_lots = min(self._consecutive_wins - 2, 2)  # max +2 lots bonus
            quantity += lot_size * extra_lots
            log.info("ANTI-MARTINGALE: %d consecutive wins — +%d lots (qty=%d, house money)",
                     self._consecutive_wins, extra_lots, quantity)
        if self._consecutive_losses >= 4:
            quantity = max(lot_size, quantity // 2)
            log.info("LOSS REDUCED SIZING: %d consecutive losses — halved to %d", self._consecutive_losses, quantity)
        if time_minutes < 10 * 60 + 30:
            quantity = lot_size
            log.info("EARLY SESSION SIZING: before 10:30 — capped to 1 lot (%d)", quantity)
        direction = "LONG" if side.startswith("BUY") else "SHORT"

        option_type = "CE" if side.endswith("CE") else "PE"
        strike_step = inst_cfg.get("strike_step", 50)
        exchange_seg = "NSE_FNO" if inst_cfg.get("exchange") == "NSE" else "BSE_FNO"

        # === EQUITY CASH PATH: direct stock buy/sell ===
        if asset == "equity":
            equity_price = payload.get("close", 0) or payload.get("entry_price", 0) or sl
            if equity_price <= 0:
                log.warning("EQUITY BLOCKED: no price for %s", symbol)
                self._record_signal_audit(payload, "BLOCKED", "no equity price")
                return

            trade_symbol = symbol.upper()
            security_id = payload.get("security_id", "")
            exchange_seg = "NSE_EQ"
            entry_price = equity_price

            if not security_id and self.option_chain and hasattr(self.option_chain, '_instrument_master'):
                im = self.option_chain._instrument_master
                if im:
                    results = im.search(trade_symbol, segment="NSE_EQ")
                    if results:
                        security_id = str(results[0].security_id)

            quantity = int(payload.get("quantity", 1))
            if quantity <= 0:
                max_per_trade = MAX_RISK_PER_TRADE
                risk_pct = EQUITY_DYNAMIC_SLTP_LEVELS.get(confidence, EQUITY_DYNAMIC_SLTP_LEVELS["LOW"])[0]["sl_pct"] / 100
                quantity = max(1, int(max_per_trade / (entry_price * risk_pct)))

            level_table = self._get_level_table("equity", direction)
            levels = level_table.get(confidence, level_table["LOW"])
            sl_pct = levels[0]["sl_pct"]
            tp_pct = levels[0]["tp_pct"]

            if direction == "LONG":
                sl_price = round(entry_price * (1 - sl_pct / 100), 2)
                tp_price = round(entry_price * (1 + tp_pct / 100), 2)
            else:
                sl_price = round(entry_price * (1 + sl_pct / 100), 2)
                tp_price = round(entry_price * (1 - tp_pct / 100), 2)

            log.info("EQUITY TRADE: %s %s %s qty=%d entry=₹%.2f SL=₹%.2f (%.1f%%) TP=₹%.2f (%.1f%%)",
                     direction, trade_symbol, confidence, quantity, entry_price, sl_price, sl_pct, tp_price, tp_pct)

            eq_broker = getattr(self, 'equity_broker', None)
            if not eq_broker:
                log.warning("EQUITY BLOCKED: no equity broker (Groww) configured for %s", trade_symbol)
                self._record_signal_audit(payload, "BLOCKED", "no_equity_broker")
                return

            from core.broker_base import Order as BrokerOrder, OrderType, ProductType, TransactionType
            txn_type = "BUY" if direction == "LONG" else "SELL"
            product = "CNC"
            order_ref = f"OM-{trade_symbol[:8]}-{int(time.time()) % 100000:05d}"

            order = BrokerOrder(
                security_id=trade_symbol,
                exchange_segment="NSE_EQ",
                transaction_type=txn_type,
                quantity=quantity,
                order_type="MARKET",
                product_type=product,
                price=0,
                tag=order_ref,
            )
            try:
                resp = eq_broker.place_order(order)
            except Exception as e:
                log.error("EQUITY ORDER FAILED: %s — %s", trade_symbol, e)
                self._record_signal_audit(payload, "BLOCKED", f"equity_order_error:{e}")
                return

            if not resp or resp.status in ("REJECTED", "FAILED"):
                log.warning("EQUITY ORDER REJECTED: %s %s", trade_symbol, resp)
                self._record_signal_audit(payload, "BLOCKED", f"equity_rejected:{resp}")
                return

            fill_price = resp.fill_price if resp.fill_price > 0 else entry_price
            eq_order_id = resp.order_id
            position = Position(
                symbol=trade_symbol, security_id=trade_symbol,
                exchange_segment=exchange_seg, option_type="EQ",
                direction=direction, quantity=quantity,
                entry_price=fill_price, sl_price=sl_price, trail_sl=sl_price,
                tp_price=tp_price, entry_order_id=eq_order_id,
                peak_price=fill_price, strategy="omega_equity",
                atr_value=fill_price * MIN_EQUITY_ATR_PCT / 100,
                original_quantity=quantity, confidence=confidence,
                underlying=trade_symbol,
                asset_type="equity",
            )
            self.pm.add_position(position)
            self.re.record_trade_taken()

            if self.wallet:
                margin = fill_price * quantity
                if not self.wallet.deduct_margin(margin, trade_symbol):
                    log.warning("EQUITY BLOCKED: insufficient funds for %s (need ₹%.2f)", trade_symbol, margin)
                    self.pm._open.pop(security_id, None)
                    return

            self.tg.trade_opened(symbol=trade_symbol, direction=direction,
                                  entry_price=fill_price, sl=sl_price, tp=tp_price,
                                  quantity=quantity, confidence=confidence)
            return

        # === STOCK OPTIONS PATH: same as index options with stock-specific levels ===
        contract: OptionContract | None = None
        if self.option_chain and asset in ("options", "stock_options"):
            underlying_price = sl * 1.01 if sl > 0 else 0
            if is_buy_option and hasattr(self.option_chain, 'resolve_delta_option'):
                contract = self.option_chain.resolve_delta_option(
                    underlying=inst_name,
                    underlying_price=underlying_price,
                    option_type=option_type,
                    target_delta=0.55,
                    delta_band=(0.45, 0.65),
                    lot_size=lot_size,
                    strike_step=strike_step,
                )
            if not contract:
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

            # --- BID-ASK SPREAD VETO: block if spread > 1.5% of mid price ---
            if contract.bid > 0 and contract.ask > 0:
                spread = contract.ask - contract.bid
                spread_pct = (spread / contract.mid_price * 100) if contract.mid_price > 0 else 999
                if spread_pct > 1.5:
                    log.info("SPREAD VETO: %s bid=%.2f ask=%.2f spread=%.2f (%.1f%% > 1.5%%) — illiquid",
                             trade_symbol, contract.bid, contract.ask, spread, spread_pct)
                    self._record_signal_audit(payload, "BLOCKED_WIDE_SPREAD",
                                              f"spread {spread_pct:.1f}% > 1.5%")
                    return
                if contract.oi == 0:
                    log.info("SPREAD VETO: %s OI=0 — no open interest, skipping", trade_symbol)
                    self._record_signal_audit(payload, "BLOCKED_NO_OI", "OI=0")
                    return

            # --- IV CRUSH FILTER: block BUY entries when IV is extremely high (>80th percentile) ---
            # High IV means expensive premiums + imminent IV crush risk
            if is_buy_option and contract.iv > 0:
                # Typical IV ranges: NIFTY 10-35%, BANKNIFTY 15-45%, SENSEX 12-30%
                iv_high_threshold = {"NIFTY": 30, "BANKNIFTY": 40, "SENSEX": 28}.get(inst_name, 35)
                if contract.iv > iv_high_threshold:
                    log.warning("IV FILTER: %s IV=%.1f%% > %d%% threshold — expensive premium, IV crush risk",
                                trade_symbol, contract.iv, iv_high_threshold)
                    self._record_signal_audit(payload, "BLOCKED_HIGH_IV",
                                              f"IV {contract.iv:.1f}% > {iv_high_threshold}%")
                    return
        else:
            if not strike:
                strike = self._compute_atm_strike(inst_name, strike_step)
            trade_symbol = f"{inst_name}{strike}{option_type}"
            security_id = ""
            entry_price = 0
            log.warning("NO CONTRACT resolved for %s — cannot trade without security_id", trade_symbol)

        if not security_id or not security_id.isdigit():
            log.warning("TRADE BLOCKED: no valid security_id for %s (got '%s')", trade_symbol, security_id)
            self._record_signal_audit(payload, "BLOCKED", reason=f"no_security_id:{trade_symbol}")
            return

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
            if corr_count >= 1:
                log.warning("CORRELATED LIMIT: %s %s blocked — %d same-direction index position(s) already open "
                            "(NIFTY/BN/SENSEX ~90%% correlated, treating as single portfolio bet)",
                            direction, inst_name, corr_count)
                return

        if self.guardrails and entry_price > 0:
            sl_pct = DYNAMIC_SLTP_LEVELS.get(confidence, DYNAMIC_SLTP_LEVELS["MEDIUM"])[0]["sl_pct"] / 100
            premium_sl = entry_price * sl_pct
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

        if self.ml_engine:
            try:
                candles = []
                if hasattr(self, 'sig_gen') and self.sig_gen and hasattr(self.sig_gen, '_candle_store'):
                    candles = self.sig_gen._candle_store.get(inst_name, [])
                if not candles and self.live_feed:
                    candles = getattr(self.live_feed, 'candle_buffer', [])
                if candles and len(candles) >= 50:
                    from ml.features import Candle as MLCandle
                    if not isinstance(candles[0], MLCandle):
                        ml_candles = [MLCandle(
                            c.timestamp.timestamp() if hasattr(c.timestamp, 'timestamp') else float(c.timestamp),
                            c.open, c.high, c.low, c.close, c.volume) for c in candles]
                    else:
                        ml_candles = candles
                    ml_result = self.ml_engine.evaluate_signal(
                        ml_candles, signal_direction=direction, signal_score=score)
                    self._signal_context[trade_symbol] = ml_result
                    if not ml_result["approved"]:
                        log.info("ML SHADOW: %s %s would-block — %s (confidence=%.2f, regime=%s)",
                                 side, symbol, ml_result["reason"],
                                 ml_result["confidence"], ml_result["regime"])
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
                return

            adj, adj_reasons = self.memory.get_score_adjustment(
                symbol=trade_symbol, side=side,
                entry_hour=now.hour, entry_day=now.weekday(),
                volatility_regime=vol_regime)
            if adj_reasons:
                log.info("Memory adjustments: %s", "; ".join(adj_reasons))

            if self.mode != "paper":
                size_mod = self.memory.get_position_size_modifier(trade_symbol)
                if size_mod < 1.0:
                    original_qty = quantity
                    quantity = max(int(quantity * size_mod), inst_cfg.get("lot_size", 65))
                    signal.quantity = quantity
                    log.info("Position size reduced by memory: %d → %d", original_qty, quantity)

        if self.mode != "paper":
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
            sl_pct_for_sizing = {"HIGH": 12.0, "MEDIUM": 10.0, "LOW": 10.0}.get(confidence_for_risk, 10.0)
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

        if self.mode != "paper":
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

        if "CE" in side and self.mode != "paper":
            quantity = max(lot_size, (quantity * 8 // 10) // lot_size * lot_size)
            signal.quantity = quantity
            log.info("CE SIZE REDUCTION: CE qty reduced to %d (CE historically net negative)", quantity)

        # --- VIX-adjusted SL widening ---
        india_vix_for_sl = payload.get("india_vix", 0)
        if india_vix_for_sl > 18:
            vix_mult = min(1.4, 1.0 + (india_vix_for_sl - 18) * 0.02)
            sl_pct = round(sl_pct * vix_mult, 1)
            log.info("VIX SL WIDEN: VIX=%.1f > 18 → SL widened to %.1f%% (×%.2f)", india_vix_for_sl, sl_pct, vix_mult)

        # --- ATR-based SL with floor ---
        underlying_name = inst_name.upper().replace("_FUT", "")
        atr_sl_floor = ATR_SL_FLOOR.get(underlying_name, 15.0)
        use_atr_sl = False
        if atr_value > 0 and fill_price > 0:
            delta_est = 0.5
            atr_sl_amount = max(atr_value * delta_est * ATR_SL_MULTIPLIER, atr_sl_floor)
            atr_sl_pct_equiv = (atr_sl_amount / fill_price) * 100
            if atr_sl_pct_equiv < sl_pct * 1.5:
                use_atr_sl = True
                log.info("ATR SL: %.1fx ATR → ₹%.2f (%.1f%%) floor=₹%.0f | pct fallback=%.1f%%",
                         ATR_SL_MULTIPLIER, atr_sl_amount, atr_sl_pct_equiv, atr_sl_floor, sl_pct)

        if use_atr_sl:
            if direction == "LONG":
                sl_price = round(fill_price - atr_sl_amount, 2)
                tp_price = round(fill_price * (1 + tp_pct / 100), 2)
            else:
                sl_price = round(fill_price + atr_sl_amount, 2)
                tp_price = round(fill_price * (1 - tp_pct / 100), 2)
            log.info("ATR-based SL/TP: %s → SL=%.2f (₹%.2f from entry) TP=%.2f (+%s%%)",
                     confidence, sl_price, atr_sl_amount, tp_price, tp_pct)
        else:
            if direction == "LONG":
                sl_price = round(fill_price * (1 - sl_pct / 100), 2)
                tp_price = round(fill_price * (1 + tp_pct / 100), 2)
            else:
                sl_price = round(fill_price * (1 + sl_pct / 100), 2)
                tp_price = round(fill_price * (1 - tp_pct / 100), 2)
            log.info("Pct-based SL/TP: %s → SL=%.2f (-%s%%) TP=%.2f (+%s%%)",
                     confidence, sl_price, sl_pct, tp_price, tp_pct)

        # --- Max risk cap ₹1,500 per trade ---
        risk_per_unit = abs(fill_price - sl_price)
        trade_risk = risk_per_unit * quantity
        if trade_risk > MAX_RISK_PER_TRADE and risk_per_unit > 0:
            max_qty = int(MAX_RISK_PER_TRADE / risk_per_unit)
            max_qty = max(lot_size, (max_qty // lot_size) * lot_size)
            if max_qty < quantity:
                log.info("RISK CAP: ₹%.0f risk (₹%.2f/unit × %d) exceeds ₹%.0f — qty %d → %d",
                         trade_risk, risk_per_unit, quantity, MAX_RISK_PER_TRADE, quantity, max_qty)
                quantity = max_qty
                signal.quantity = quantity

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
            underlying_entry_price=self._get_underlying_ltp(inst_name),
            asset_type="stock_options" if asset == "stock_options" else "options",
        )
        self.pm.add_position(position)
        self.re.record_trade_taken()

        if self.wallet:
            margin = fill_price * quantity
            if not self.wallet.deduct_margin(margin, trade_symbol):
                log.warning("TRADE BLOCKED: insufficient wallet funds for %s (need ₹%.2f)", trade_symbol, margin)
                self.pm._open.pop(security_id, None)
                self._record_signal_audit(payload, "BLOCKED_FUNDS", f"need ₹{margin:.0f}")
                return

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
        # SIGNAL_EXIT disabled — 0% win rate across 12 trades, -₹1,778 total loss
        # Trail SL handles all exits (58% WR, +₹3,767). Let positions ride.
        log.info("SIGNAL_EXIT disabled — letting TRAIL_SL manage all exits for %s", symbol)

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
        lot_size = inst_cfg.get("lot_size", 65)
        spread_conf = payload.get("confidence", "MEDIUM")
        conf_lots = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(spread_conf, 1) if self.mode == "paper" else self.default_qty_lots
        if lot_size >= 50 and conf_lots > 1:
            conf_lots = max(1, conf_lots - 1)
        quantity = lot_size * conf_lots
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
            hedge_otm_steps=4,
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

        max_spreads = 8 if self.mode == "paper" else 2
        spread_count = sum(1 for p in self.pm.open_positions if p.is_spread)
        if spread_count >= max_spreads:
            log.warning("Max %d concurrent spreads reached — blocking %s %s", max_spreads, side, inst_name)
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

        level_table = SPREAD_SLTP_LEVELS
        levels = level_table.get(confidence, level_table["LOW"])

        # SL/TP are reference only — actual exits use net-premium % in monitor_positions
        # Set sell leg SL wide enough to never trigger is_sl_hit (spreads bypass it)
        sl_price = round(sell_fill * 1.50, 2)
        tp_price = round(sell_fill * 0.50, 2)

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

        if self.live_feed and sell_contract.security_id.isdigit():
            from core.live_feed import EXCHANGE_SEG_MAP
            seg_int = EXCHANGE_SEG_MAP.get(sell_contract.exchange_segment, 2)
            self.live_feed.subscribe(
                [(seg_int, sell_contract.security_id, 15)])
            log.info("LiveFeed subscribed SELL leg: %s sid=%s",
                     sell_contract.trade_symbol, sell_contract.security_id)
        if self.live_feed and hedge_contract.security_id.isdigit():
            from core.live_feed import EXCHANGE_SEG_MAP
            seg_int = EXCHANGE_SEG_MAP.get(hedge_contract.exchange_segment, 2)
            self.live_feed.subscribe(
                [(seg_int, hedge_contract.security_id, 15)])
            log.info("LiveFeed subscribed HEDGE leg: %s sid=%s",
                     hedge_contract.trade_symbol, hedge_contract.security_id)

        if self.wallet:
            if not self.wallet.deduct_spread_margin(actual_max_loss, quantity, sell_contract.trade_symbol):
                log.warning("SPREAD BLOCKED: insufficient wallet funds for %s (need ₹%.2f)",
                            sell_contract.trade_symbol, actual_max_loss * quantity)
                self.pm._open.pop(sell_contract.security_id, None)
                return

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
        hedge_ltp = 0.0
        if self.live_feed:
            hedge_ltp = self.live_feed.get_ltp(pos.hedge_security_id, pos.exchange_segment)
        if hedge_ltp <= 0:
            hedge_ltp = self.broker.get_ltp(pos.hedge_security_id, pos.exchange_segment)
        if hedge_ltp <= 0:
            hedge_ltp = pos.hedge_current_price if pos.hedge_current_price > 0 else pos.hedge_entry_price
        pos.hedge_current_price = hedge_ltp

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
                self._consecutive_wins += 1
            else:
                self._consecutive_wins = 0
                self._consecutive_losses += 1
                self._last_loss_time = time.time()
                underlying = self._resolve_instrument(pos.symbol) or pos.symbol
                self._symbol_loss_cooldown[underlying] = time.time()

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

        # Counterfactual resolution (throttled internally to every 5 min)
        if self.ml_engine:
            try:
                prices = {sym: self._get_underlying_ltp(sym) for sym in ("NIFTY", "BANKNIFTY", "SENSEX")}
                self.ml_engine.resolve_counterfactuals(prices)
            except Exception:
                pass

        now = datetime.now(IST)
        for pos in self.pm.open_positions:
            try:
                ltp = 0.0
                _pos_asset = getattr(pos, 'asset_type', 'options')
                if _pos_asset == "equity" and getattr(self, 'equity_broker', None):
                    ltp = self.equity_broker.get_ltp(pos.symbol, pos.exchange_segment)
                else:
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

            # --- EXPIRY-DAY 14:00 EXIT: gamma explodes in last 90 min ---
            if pos.expiry and pos.direction == "LONG":
                try:
                    from core.greeks import dte_from_expiry
                    dte = dte_from_expiry(pos.expiry)
                    if dte <= 0.1 and now.hour >= 14:
                        log.info("EXPIRY EXIT: %s %s — 0DTE past 14:00 IST, gamma risk too high (dte=%.2f)",
                                 pos.direction, pos.symbol, dte)
                        if pos.is_spread:
                            self._exit_spread(pos, ltp, "EXPIRY_DAY_1400")
                        else:
                            self._exit_position(pos, ltp, "EXPIRY_DAY_1400")
                        continue
                except Exception:
                    pass

            if pos.is_spread and pos.hedge_security_id:
                try:
                    hedge_ltp = 0.0
                    if self.live_feed:
                        hedge_ltp = self.live_feed.get_ltp(pos.hedge_security_id, pos.exchange_segment)
                    if hedge_ltp <= 0:
                        hedge_ltp = self.broker.get_ltp(pos.hedge_security_id, pos.exchange_segment)
                    if hedge_ltp > 0:
                        pos.hedge_current_price = hedge_ltp
                    else:
                        log.warning("Hedge LTP=0 for %s (sid=%s) — spread SL blind",
                                    pos.hedge_trade_symbol, pos.hedge_security_id)
                except Exception as e:
                    log.warning("Hedge LTP fetch failed for %s: %s", pos.hedge_trade_symbol, e)

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

            _asset = getattr(pos, 'asset_type', 'options')
            if _asset == "equity":
                min_atr = pos.entry_price * MIN_EQUITY_ATR_PCT / 100
            else:
                min_atr = pos.entry_price * MIN_OPTION_ATR_PCT / 100
            premium_atr = max(pos.atr_value, min_atr) if pos.atr_value > 0 else min_atr
            is_bought_option = pos.direction == "LONG" and ("CE" in pos.symbol or "PE" in pos.symbol)
            if is_bought_option and _asset != "equity":
                premium_atr *= 1.3

            level_table = self._get_level_table(getattr(pos, 'asset_type', 'options'), pos.direction, is_spread=pos.is_spread)
            levels = level_table.get(pos.confidence, level_table["LOW"])
            past_all_levels = pos.max_dynamic_level > 0

            # Spreads: SL/TP on net premium only — no directional trail
            if pos.is_spread and pos.net_credit > 0 and pos.hedge_current_price > 0:
                net_premium = ltp - pos.hedge_current_price
                net_pnl_pct = (pos.net_credit - net_premium) / pos.net_credit * 100
                sl_pct = levels[0]["sl_pct"]
                if net_pnl_pct < -sl_pct:
                    log.info("SPREAD_SL: %s net_credit=%.2f net_now=%.2f loss=%.1f%% > %s%%",
                             pos.symbol, pos.net_credit, net_premium, net_pnl_pct, sl_pct)
                    self._exit_spread(pos, ltp, "SPREAD_NET_SL")
                    continue
                tp_pct = levels[min(pos.dynamic_level, len(levels)-1)]["tp_pct"]
                if net_pnl_pct >= tp_pct:
                    log.info("SPREAD_TP: %s net_credit=%.2f net_now=%.2f profit=%.1f%% >= %s%%",
                             pos.symbol, pos.net_credit, net_premium, net_pnl_pct, tp_pct)
                    self._exit_spread(pos, ltp, "SPREAD_NET_TP")
                    continue
                # Time-based TP: if profitable and within 30 min of close, take it
                if net_pnl_pct > 0 and now.hour >= 15 and now.minute >= 0:
                    log.info("SPREAD_TIME_TP: %s net_credit=%.2f net_now=%.2f profit=%.1f%% — EOD exit",
                             pos.symbol, pos.net_credit, net_premium, net_pnl_pct)
                    self._exit_spread(pos, ltp, "SPREAD_TIME_TP")
                    continue
                # Skip directional trail for spreads — net premium SL handles it
                new_trail = pos.trail_sl
                old_trail = pos.trail_sl
                trail_eval = {"detail": "", "r_multiple": 0, "tier": "", "peak": pos.peak_price, "sl": pos.trail_sl}
            else:
                trail_eval = self.re.evaluate_trail(
                    current_price=ltp,
                    entry_price=pos.entry_price,
                    existing_sl=pos.trail_sl,
                    direction=pos.direction,
                    peak_price=pos.peak_price,
                    atr_value=premium_atr,
                    hold_seconds=hold_seconds,
                    initial_sl=pos.sl_price,
                )
                pos.peak_price = trail_eval["peak"]

                new_trail = trail_eval["sl"]
                old_trail = pos.trail_sl

            if hold_minutes < 15 and new_trail != old_trail:
                log.info("TRAIL DIAG: %s ltp=%.2f entry=%.2f trail %.2f→%.2f profit=%.1f%% R=%.2f %s hold=%.0fm detail=%s",
                         pos.symbol, ltp, pos.entry_price, old_trail, new_trail, profit_pct,
                         trail_eval.get("r_multiple", 0), trail_eval.get("tier", ""),
                         hold_minutes, trail_eval.get("detail", ""))

            # Early trail freeze removed — let trades breathe with wider trail widths instead

            if past_all_levels:
                tight_width = 4.0
                peak = pos.peak_price
                if pos.is_long:
                    atr_trail = round(peak - (tight_width * premium_atr), 2)
                    if atr_trail > new_trail:
                        new_trail = atr_trail
                else:
                    atr_trail = round(peak + (tight_width * premium_atr), 2)
                    if new_trail <= 0 or atr_trail < new_trail:
                        new_trail = atr_trail

            # --- PROFIT PROTECTION: if we were up 15%+, guarantee at least 5% profit ---
            if pos.peak_profit_pct >= 15.0 and not pos.breakeven_locked:
                min_profit_sl = pos.entry_price * (1.05 if pos.is_long else 0.95)
                if pos.is_long and new_trail < min_profit_sl:
                    new_trail = round(min_profit_sl, 2)
                elif not pos.is_long and (new_trail > min_profit_sl or new_trail <= 0):
                    new_trail = round(min_profit_sl, 2)

            # --- 1. BREAKEVEN LOCKDOWN ---
            if not pos.breakeven_locked and profit_pct >= BREAKEVEN_PROFIT_PCT:
                be_buffer = pos.entry_price * 0.03
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
                log.info("Trail: %s %s hold=%.0fs pATR=%.2f profit=%.1f%% R=%.2f %s | %s",
                         pos.direction, pos.symbol, hold_seconds,
                         premium_atr, profit_pct,
                         trail_eval.get("r_multiple", 0), trail_eval.get("tier", ""),
                         trail_eval["detail"])

            # --- 2. SCALE-OUT IN 3 TRANCHES ---
            lot_size = self._get_lot_size(pos.symbol)
            tranche_1_pct = {"HIGH": 10.0, "MEDIUM": 8.0, "LOW": 6.0}.get(pos.confidence, 6.0)
            tranche_2_pct = {"HIGH": 18.0, "MEDIUM": 14.0, "LOW": 10.0}.get(pos.confidence, 10.0)

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

            # --- 3. TIME-DECAY TARGETS: disabled — let trades reach full TP ---
            effective_tp = pos.tp_price

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

            # --- STAGNATION EXIT: disabled — let trades breathe ---

            # --- DUAL-DOMAIN EXIT: underlying trail + option catastrophe ---
            if not pos.is_spread and pos.underlying and pos.underlying_entry_price > 0:
                u_ltp = self._get_underlying_ltp(pos.underlying)
                if u_ltp > 0:
                    u_atr = pos.atr_value * (pos.underlying_entry_price / max(pos.entry_price, 1))
                    dual = self.re.evaluate_dual_domain_trail(
                        option_price=ltp,
                        option_entry=pos.entry_price,
                        underlying_price=u_ltp,
                        underlying_entry=pos.underlying_entry_price,
                        direction=pos.direction,
                        option_sl=pos.trail_sl,
                        underlying_peak=pos.underlying_peak_price,
                        underlying_atr=u_atr,
                        hold_seconds=hold_seconds,
                    )
                    pos.underlying_peak_price = dual["underlying_peak"]
                    if dual["exit"]:
                        log.info("DUAL_DOMAIN_EXIT: %s %s — %s", pos.direction, pos.symbol, dual["reason"])
                        self._exit_position(pos, ltp, f"DUAL_DOMAIN_{dual['domain'].upper()}")
                        continue

            # --- EXIT CHECKS ---
            # Spreads use net-premium SL only (handled above), skip individual leg trail SL
            if pos.is_sl_hit and not pos.is_spread and hold_seconds >= SL_GRACE_SECONDS:
                log.info("SL HIT: %s %s @ %.2f (SL=%.2f) pnl=%.1f%% hold=%.0fs level=L%d",
                         pos.direction, pos.symbol, ltp, pos.trail_sl, profit_pct, hold_seconds, pos.dynamic_level)
                self._exit_position(pos, ltp, "TRAIL_SL")
            elif pos.is_sl_hit and not pos.is_spread and hold_seconds < SL_GRACE_SECONDS:
                log.info("SL GRACE: %s %s SL hit at %.2f but hold=%.0fs < %ds grace — holding",
                         pos.direction, pos.symbol, ltp, hold_seconds, SL_GRACE_SECONDS)

            elif tp_hit:
                level_table = self._get_level_table(getattr(pos, 'asset_type', 'options'), pos.direction, is_spread=pos.is_spread)
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
                return cfg.get("lot_size", 65)
        return 65

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
            return ltp < active_sl and gap_pct > 3.0
        else:
            gap_pct = (ltp - active_sl) / active_sl * 100 if active_sl > 0 else 0
            return ltp > active_sl and gap_pct > 3.0

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
        _asset = getattr(pos, 'asset_type', 'options')
        if _asset == "equity" and getattr(self, 'equity_broker', None):
            from core.broker_base import Order as BrokerOrder
            txn_type = "SELL" if pos.direction == "LONG" else "BUY"
            order_ref = f"OM-X{pos.symbol[:6]}-{int(time.time()) % 100000:05d}"
            order = BrokerOrder(
                security_id=pos.symbol,
                exchange_segment="NSE_EQ",
                transaction_type=txn_type,
                quantity=pos.quantity,
                order_type="MARKET",
                product_type="CNC",
                price=0,
                tag=order_ref,
            )
            try:
                resp = self.equity_broker.place_order(order)
                log.info("GROWW EXIT: %s %s qty=%d reason=%s → %s",
                         txn_type, pos.symbol, pos.quantity, reason,
                         resp.status if resp else "NO_RESP")
            except Exception as e:
                log.error("GROWW EXIT FAILED: %s %s — %s", pos.symbol, reason, e)
        else:
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
                self._consecutive_wins += 1
            else:
                self._consecutive_wins = 0
                self._consecutive_losses += 1
                self._last_loss_time = time.time()
                underlying = self._resolve_instrument(pos.symbol) or pos.symbol
                self._symbol_loss_cooldown[underlying] = time.time()
                log.info("SYMBOL COOLDOWN SET: %s — 30-min block on %s after ₹%.0f loss",
                         pos.symbol, underlying, trade.pnl)
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
                ctx = self._signal_context.get(pos.symbol, self._signal_context.get(pos.security_id, {}))
                hold_min = 0
                try:
                    hold_min = (trade.exit_time - trade.entry_time).total_seconds() / 60
                except Exception:
                    pass
                db.record_trade(
                    symbol=trade.symbol, direction=trade.direction,
                    option_type=trade.option_type, quantity=trade.quantity,
                    entry_price=trade.entry_price, exit_price=trade.exit_price,
                    pnl=trade.pnl,
                    entry_time=trade.entry_time.isoformat(),
                    exit_time=trade.exit_time.isoformat(),
                    exit_reason=reason, strategy=pos.strategy,
                    balance_after=self.wallet.balance if self.wallet else 0,
                    signal_score=ctx.get("signal_score", 0),
                    signal_confidence=ctx.get("signal_confidence", pos.confidence),
                    volatility_regime=ctx.get("volatility_regime", "NORMAL"),
                    adx_value=ctx.get("adx_value", 0),
                    rsi_value=ctx.get("rsi_value", 50),
                    atr_value=ctx.get("atr_value", 0),
                    volume_ratio=ctx.get("volume_ratio", 1.0),
                    supertrend_dir=ctx.get("supertrend_dir", ""),
                    vwap_position=ctx.get("vwap_position", ""),
                    hold_minutes=hold_min,
                    r_multiple=pos.peak_profit_pct / max(abs(trade.pnl / max(trade.entry_price, 1)), 0.01) if trade.pnl != 0 else 0,
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

            if self.ml_engine:
                try:
                    candles = []
                    inst = self._resolve_instrument(pos.symbol) or ""
                    if hasattr(self, 'sig_gen') and self.sig_gen and hasattr(self.sig_gen, '_candle_store'):
                        candles = self.sig_gen._candle_store.get(inst, [])
                        if not candles:
                            log.debug("ML: no candles in store for inst=%s (symbol=%s), keys=%s",
                                      inst, pos.symbol, list(self.sig_gen._candle_store.keys()))
                    if not candles and self.live_feed:
                        candles = getattr(self.live_feed, 'candle_buffer', [])
                    if not candles or len(candles) < 50:
                        log.info("ML OUTCOME SKIPPED: %s — only %d candles (need 50), inst=%s",
                                 pos.symbol, len(candles), inst)
                    if candles and len(candles) >= 50:
                        from ml.features import Candle as MLCandle
                        ml_candles = []
                        for c in candles:
                            if isinstance(c, MLCandle):
                                ml_candles.append(c)
                            else:
                                import datetime as _dt
                                t = c.timestamp.timestamp() if isinstance(c.timestamp, _dt.datetime) else float(c.timestamp)
                                ml_candles.append(MLCandle(t, c.open, c.high, c.low, c.close, getattr(c, 'volume', 0.0)))
                        is_win = trade.pnl > 0
                        hold_secs = 0
                        try:
                            hold_secs = (trade.exit_time - trade.entry_time).total_seconds()
                        except Exception:
                            pass
                        self.ml_engine.record_trade_outcome(
                            ml_candles, is_win=is_win, pnl=trade.pnl,
                            entry_time=str(pos.entry_time), symbol=pos.symbol,
                            exit_reason=reason, hold_seconds=hold_secs)
                        stats = self.ml_engine.get_model_stats()
                        buf = stats.get("buffer", {})
                        log.info("ML OUTCOME RECORDED: %s win=%s pnl=₹%.2f — signals=%d trades=%d",
                                 pos.symbol, is_win, trade.pnl,
                                 buf.get("signals", 0), buf.get("trades", 0))
                except Exception as e:
                    log.warning("ML outcome recording failed: %s", e)

            if self.memory.trade_count % 10 == 0:
                self.memory.analyze_patterns()
                health = self.memory.get_health_report()
                if health["status"] in ("CRITICAL", "DEGRADED"):
                    self.tg.error("MEMORY_HEALTH",
                                  Exception(f"System health: {health['status']} — "
                                            f"{'; '.join(health['alerts'])}"))

            if self.memory.trade_count % 5 == 0:
                try:
                    from core.analytics import load_trades, compute_analytics, get_edge_summary
                    all_trades = load_trades()
                    analytics = compute_analytics(all_trades)
                    edge = get_edge_summary(analytics)
                    o = analytics["overall"]
                    log.info("ANALYTICS [%d trades]: WR=%.1f%% PF=%.2f Exp=₹%.0f MaxDD=₹%.0f | Edge: %s profitable, %s unprofitable",
                             o["total_trades"], o["win_rate"], o["profit_factor"], o["expectancy"],
                             o["max_drawdown"],
                             edge["profitable"][0]["filter"] if edge["profitable"] else "none",
                             edge["unprofitable"][0]["filter"] if edge["unprofitable"] else "none")
                except Exception:
                    pass
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
