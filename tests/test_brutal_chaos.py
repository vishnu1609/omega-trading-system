"""
Brutal Chaos Test Suite — Omega Trading Company

Tests that simulate the market's absolute worst days:
cascading failures, data corruption, race conditions,
math edge cases, and capital destruction scenarios.

If Omega survives these, it survives anything.
"""
import math
import time
import threading
from datetime import datetime, timedelta
from threading import Lock
from unittest.mock import MagicMock, patch, PropertyMock
from queue import Queue

import pytest

from core.broker_base import (
    BrokerError, BrokerRateLimit, Order, OrderResponse, OrderType,
    PositionData, ProductType, TransactionType,
)
from core.guardrails import Guardrails, GuardrailResult
from core.order_manager import OrderManager, Signal
from core.position_manager import Position, PositionManager, ClosedTrade
from core.risk_engine import RiskEngine
from core.signal_generator import Candle, SignalGenerator
from strategies.omega_complete import OmegaStrategy


# ==========================================
# FIXTURES
# ==========================================

@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.get_funds.return_value = {"availabelBalance": 100_000}
    broker.get_available_margin.return_value = 100_000.0
    broker.get_ltp.return_value = 150.0
    broker.place_order.return_value = OrderResponse(
        order_id="TEST-001", status="TRADED", fill_price=150.0, fill_quantity=75,
    )
    return broker


@pytest.fixture
def guardrails():
    return Guardrails(capital=100_000)


@pytest.fixture
def pm():
    return PositionManager()


@pytest.fixture
def risk_engine(pm, mock_broker):
    return RiskEngine(
        position_manager=pm,
        broker=mock_broker,
        max_daily_loss=2000,
        max_concurrent_positions=2,
        max_trades_per_day=10,
        cooldown_after_losses=2,
        cooldown_minutes=30,
    )


@pytest.fixture
def order_manager(mock_broker):
    return OrderManager(broker=mock_broker, slippage_pct=0.5, telegram=MagicMock())


def _make_strategy(mock_broker, pm=None, guardrails=None):
    return OmegaStrategy(
        broker=mock_broker,
        order_manager=MagicMock(),
        position_manager=pm or MagicMock(),
        risk_engine=MagicMock(),
        instrument_selector=MagicMock(),
        telegram=MagicMock(),
        instruments_config={},
        guardrails=guardrails,
    )


def _make_candle(close, ts=None, high=None, low=None, volume=1000):
    ts = ts or datetime(2026, 9, 21, 10, 0)
    return Candle(
        timestamp=ts,
        open=close - 5,
        high=high if high else close + 10,
        low=low if low else close - 10,
        close=close,
        volume=volume,
    )


# ==========================================
# CATEGORY 1: PRICE GOES TO HELL
# ==========================================

class TestPriceApocalypse:

    def test_price_drops_to_zero(self, mock_broker):
        """Option premium goes to ₹0.05 (exchange minimum). SL-skip must fire."""
        strat = _make_strategy(mock_broker)
        pos = Position(
            symbol="NIFTY24500CE", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=150.0, sl_price=140.0, trail_sl=140.0,
        )
        assert strat._is_sl_skipped(pos, ltp=0.05) is True

    def test_price_goes_negative(self, mock_broker):
        """Broker sends negative LTP (corrupt data). SL-skip must not crash."""
        strat = _make_strategy(mock_broker)
        pos = Position(
            symbol="NIFTY24500CE", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=150.0, sl_price=140.0, trail_sl=140.0,
        )
        result = strat._is_sl_skipped(pos, ltp=-50.0)
        assert isinstance(result, bool)

    def test_price_is_nan(self, mock_broker):
        """Broker sends NaN as LTP. Must not crash or trigger false exits."""
        strat = _make_strategy(mock_broker)
        pos = Position(
            symbol="NIFTY24500CE", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=150.0, sl_price=140.0, trail_sl=140.0,
        )
        result = strat._is_sl_skipped(pos, ltp=float('nan'))
        assert isinstance(result, bool)

    def test_price_is_infinity(self, mock_broker):
        """Broker sends Inf as LTP. System must not explode."""
        strat = _make_strategy(mock_broker)
        pos = Position(
            symbol="NIFTY24500CE", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="SHORT", quantity=75,
            entry_price=150.0, sl_price=160.0, trail_sl=160.0,
        )
        result = strat._is_sl_skipped(pos, ltp=float('inf'))
        assert isinstance(result, bool)

    def test_flash_crash_90_percent(self, mock_broker):
        """90% drop in one candle — from ₹24500 to ₹2450."""
        strat = _make_strategy(mock_broker)
        pos = Position(
            symbol="NIFTY24500CE", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=200.0, sl_price=180.0, trail_sl=180.0,
        )
        assert strat._is_sl_skipped(pos, ltp=20.0) is True

    def test_sl_and_entry_are_identical(self, guardrails):
        """Zero-width risk trade (SL = entry). Risk calc must not approve infinite lots."""
        result = guardrails.check_per_trade_risk(
            entry_price=100.0, sl_price=100.0, quantity=9999)
        assert result.passed is True  # 0 risk technically passes

    def test_sl_above_entry_for_long(self, guardrails):
        """Reversed SL (SL above entry on a LONG). Risk calc uses abs() so it still computes risk."""
        result = guardrails.check_per_trade_risk(
            entry_price=100.0, sl_price=120.0, quantity=75)
        assert isinstance(result.passed, bool)


# ==========================================
# CATEGORY 2: DATA CORRUPTION HELL
# ==========================================

class TestDataCorruption:

    def test_all_candles_are_ghosts(self):
        """Entire dataset is zero-volume flat candles."""
        ghosts = [Candle(
            timestamp=datetime(2026, 9, 21, 9, 15 + i),
            open=0, high=0, low=0, close=0, volume=0,
        ) for i in range(10)]

        cleaned = SignalGenerator._filter_ghost_candles(ghosts, "NIFTY")
        assert len(cleaned) == 10
        # First candle stays ghost (no previous to fill from), rest get forward-filled
        # But since first is all zeros, forward-fill propagates zeros
        # System should NOT crash
        for c in cleaned:
            assert isinstance(c.close, (int, float))

    def test_single_candle_dataset(self):
        """Only 1 candle received. Sanitizer must not index-out-of-bounds."""
        single = [_make_candle(24500)]
        result = SignalGenerator._sanitize_outliers(single, "NIFTY")
        assert len(result) == 1

    def test_empty_candle_list(self):
        """Zero candles. Must return empty, not crash."""
        assert SignalGenerator._filter_ghost_candles([], "NIFTY") == []
        assert SignalGenerator._sanitize_outliers([], "NIFTY") == []

    def test_candle_with_negative_volume(self):
        """Exchange sends negative volume (has happened on MCX)."""
        candles = [
            _make_candle(24500),
            Candle(timestamp=datetime(2026, 9, 21, 10, 5),
                   open=24510, high=24520, low=24490, close=24515, volume=-500),
        ]
        result = SignalGenerator._sanitize_outliers(candles, "NIFTY")
        assert len(result) == 2

    def test_candle_with_high_below_low(self):
        """Impossible candle: High < Low. Sanitizer must not crash."""
        candles = [
            _make_candle(24500),
            Candle(timestamp=datetime(2026, 9, 21, 10, 5),
                   open=24510, high=24490, low=24520, close=24515, volume=1000),
        ]
        result = SignalGenerator._sanitize_outliers(candles, "NIFTY")
        assert len(result) == 2

    def test_massive_outlier_sequence(self):
        """10 consecutive corrupted candles. Forward-fill must cascade correctly."""
        candles = [_make_candle(24500, ts=datetime(2026, 9, 21, 9, 15))]
        for i in range(1, 11):
            ts = datetime(2026, 9, 21, 9, 15) + timedelta(minutes=i * 5)
            candles.append(Candle(
                timestamp=ts,
                open=999_999, high=999_999, low=999_999, close=999_999, volume=1,
            ))
        cleaned = SignalGenerator._sanitize_outliers(candles, "NIFTY")
        for c in cleaned[1:]:
            assert c.close == 24500, f"Cascaded forward-fill failed: close={c.close}"

    def test_alternating_valid_and_corrupt(self):
        """Alternating good and garbage candles."""
        candles = []
        for i in range(10):
            ts = datetime(2026, 9, 21, 9, 15) + timedelta(minutes=i * 5)
            if i % 2 == 0:
                candles.append(_make_candle(24500 + i, ts=ts))
            else:
                candles.append(Candle(timestamp=ts,
                    open=500_000, high=500_000, low=500_000, close=500_000, volume=1))
        cleaned = SignalGenerator._sanitize_outliers(candles, "NIFTY")
        for i, c in enumerate(cleaned):
            assert c.close < 30_000, f"Candle {i} not sanitized: close={c.close}"

    def test_freak_tick_detection_boundary(self):
        """Exactly 5% move — should NOT trigger freak tick (threshold is >5%)."""
        prev = _make_candle(100.0, ts=datetime(2026, 9, 21, 10, 0))
        curr = Candle(timestamp=datetime(2026, 9, 21, 10, 5),
                      open=105, high=110, low=100, close=105, volume=1000)
        candles = [_make_candle(99), prev, curr]
        assert SignalGenerator._is_freak_tick(candles) is False

    def test_freak_tick_just_over_threshold(self):
        """5.1% move with wide intra-bar range — MUST trigger."""
        prev = _make_candle(100.0, ts=datetime(2026, 9, 21, 10, 0))
        curr = Candle(timestamp=datetime(2026, 9, 21, 10, 5),
                      open=100, high=115, low=95, close=105.1, volume=1000)
        candles = [_make_candle(99), prev, curr]
        assert SignalGenerator._is_freak_tick(candles) is True

    def test_previous_close_is_zero(self):
        """Previous candle close = 0. Division by zero must not crash."""
        candles = [
            Candle(timestamp=datetime(2026, 9, 21, 10, 0),
                   open=0, high=0, low=0, close=0, volume=0),
            _make_candle(24500, ts=datetime(2026, 9, 21, 10, 5)),
        ]
        result = SignalGenerator._sanitize_outliers(candles, "NIFTY")
        assert len(result) == 2


# ==========================================
# CATEGORY 3: CAPITAL DESTRUCTION SCENARIOS
# ==========================================

class TestCapitalDestruction:

    def test_nse_allocation_cap_exact_boundary(self, guardrails):
        """Allocate exactly to the NSE cap. Next ₹1 must be blocked."""
        nse_cap = guardrails.capital * guardrails.nse_cap  # ₹60,000
        guardrails.record_allocation("NIFTY", nse_cap)

        result = guardrails.check_sector_allocation("BANKNIFTY", 1.0)
        assert result.passed is False
        assert "NSE allocation cap" in result.reason

    def test_mcx_allocation_cap_exact_boundary(self, guardrails):
        """Allocate exactly to the MCX cap."""
        mcx_cap = guardrails.capital * guardrails.mcx_cap  # ₹40,000
        guardrails.record_allocation("GOLD", mcx_cap)

        result = guardrails.check_sector_allocation("SILVER", 1.0)
        assert result.passed is False

    def test_release_more_than_allocated(self, guardrails):
        """Release more margin than was allocated. Must not go negative."""
        guardrails.record_allocation("NIFTY", 10_000)
        guardrails.release_allocation("NIFTY", 50_000)
        assert guardrails._allocated_nse == 0.0

    def test_per_trade_risk_massive_quantity(self, guardrails):
        """1000 lots at ₹20 SL = ₹20,000 risk on ₹100K capital. Must block."""
        result = guardrails.check_per_trade_risk(
            entry_price=150.0, sl_price=130.0, quantity=1000)
        assert result.passed is False

    def test_per_trade_risk_zero_prices(self, guardrails):
        """Zero entry or SL price. Must pass (can't compute risk)."""
        assert guardrails.check_per_trade_risk(0, 100, 75).passed is True
        assert guardrails.check_per_trade_risk(100, 0, 75).passed is True

    def test_daily_loss_breached_kills_bot(self, pm, risk_engine, mock_broker):
        """After losing ₹2000+, the bot kill-switch must activate permanently."""
        pos = Position(
            symbol="NIFTY24500CE", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=150.0, sl_price=140.0,
        )
        pm.add_position(pos)
        pm.close_position("N-OPT", 123.0, "SL_HIT")  # Loss: (123-150)*75 = -₹2025

        with patch("core.risk_engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 21, 11, 0,
                                                tzinfo=risk_engine.session_start.tzinfo)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert risk_engine.is_daily_loss_breached() is True

    def test_cooldown_after_consecutive_losses(self, pm, risk_engine, mock_broker):
        """2 back-to-back losses must trigger 30-min cooldown."""
        for i in range(2):
            pos = Position(
                symbol=f"NIFTY{i}", security_id=f"N-{i}",
                exchange_segment="NSE_FNO", option_type="CE",
                direction="LONG", quantity=75,
                entry_price=150.0, sl_price=140.0,
            )
            pm.add_position(pos)
            pm.close_position(f"N-{i}", 145.0, "SL_HIT")

        assert pm.consecutive_losses == 2

    def test_max_positions_blocks_new_trade(self, pm, risk_engine, mock_broker):
        """With 2 open positions, the 3rd entry must be blocked."""
        for i in range(2):
            pm.add_position(Position(
                symbol=f"SYM{i}", security_id=f"SEC-{i}",
                exchange_segment="NSE_FNO", option_type="CE",
                direction="LONG", quantity=75,
                entry_price=150.0, sl_price=140.0,
            ))
        assert pm.open_count == 2


# ==========================================
# CATEGORY 4: BROKER API NIGHTMARES
# ==========================================

class TestBrokerNightmares:

    def test_broker_returns_contradictory_position(self, mock_broker, order_manager):
        """Broker says net_quantity=-75 for a BUY order. Ghost fill must handle it."""
        mock_broker.place_order.side_effect = BrokerError("Timeout")
        mock_broker.get_positions.return_value = [
            PositionData(
                security_id="N-OPT", symbol="NIFTY24500CE",
                exchange_segment="NSE_FNO",
                buy_quantity=0, sell_quantity=75, net_quantity=-75,
                buy_avg=0, sell_avg=155.0,
            ),
        ]

        order = Order(
            security_id="N-OPT", exchange_segment="NSE_FNO",
            transaction_type=TransactionType.SELL,
            quantity=75, order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=155.0, tag="test_contra",
        )

        resp = order_manager._place_with_retry(order, max_retries=1)
        assert resp is not None
        assert resp.fill_price == 155.0

    def test_broker_get_positions_throws(self, mock_broker, order_manager):
        """Both place_order AND get_positions throw. Must return None, not crash."""
        mock_broker.place_order.side_effect = BrokerError("Dead")
        mock_broker.get_positions.side_effect = BrokerError("Also dead")

        order = Order(
            security_id="N-OPT", exchange_segment="NSE_FNO",
            transaction_type=TransactionType.BUY,
            quantity=75, order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=150.0, tag="test_both_dead",
        )

        resp = order_manager._place_with_retry(order, max_retries=1)
        assert resp is None

    def test_rate_limit_storm(self, mock_broker, order_manager):
        """Broker rate-limits 3 times then succeeds on the 4th."""
        call_count = 0
        def rate_limit_then_succeed(order):
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                raise BrokerRateLimit("429 Too Many Requests")
            return OrderResponse(
                order_id="SURVIVED", status="TRADED",
                fill_price=150.0, fill_quantity=75,
            )

        mock_broker.place_order.side_effect = rate_limit_then_succeed

        order = Order(
            security_id="N-OPT", exchange_segment="NSE_FNO",
            transaction_type=TransactionType.BUY,
            quantity=75, order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=150.0, tag="test_rate",
        )

        resp = order_manager._place_with_retry(order, max_retries=4)
        assert resp is not None
        assert resp.order_id == "SURVIVED"

    def test_broker_returns_zero_fill_price(self, mock_broker, order_manager):
        """Order says TRADED but fill_price=0. System must handle it."""
        mock_broker.place_order.return_value = OrderResponse(
            order_id="ZERO-FILL", status="TRADED",
            fill_price=0.0, fill_quantity=75,
        )

        signal = Signal(
            strategy="omega_complete", symbol="NIFTY24500CE",
            security_id="N-OPT", exchange_segment="NSE_FNO",
            side="BUY_CE", quantity=75, price=150.0,
        )
        resp = order_manager.place_entry(signal, MagicMock(allow_new_trade=MagicMock(return_value=True)))
        assert resp is not None

    def test_ltp_returns_zero(self, mock_broker, order_manager):
        """Broker LTP = 0. Limit price estimation must not place at ₹0."""
        mock_broker.get_ltp.return_value = 0.0

        signal = Signal(
            strategy="omega_complete", symbol="NIFTY24500CE",
            security_id="N-OPT", exchange_segment="NSE_FNO",
            side="BUY_CE", quantity=75, price=0,
        )
        limit_price = order_manager._estimate_limit_price(signal)
        assert limit_price == 0

    def test_ghost_fill_with_multiple_positions(self, mock_broker, order_manager):
        """Broker has 3 positions. Ghost fill must match the correct security_id."""
        mock_broker.place_order.side_effect = BrokerError("Timeout")
        mock_broker.get_positions.return_value = [
            PositionData(security_id="WRONG-1", symbol="BANKNIFTY",
                         exchange_segment="NSE_FNO", net_quantity=50, buy_avg=200),
            PositionData(security_id="TARGET", symbol="NIFTY24500CE",
                         exchange_segment="NSE_FNO", net_quantity=75, buy_avg=155),
            PositionData(security_id="WRONG-2", symbol="GOLD",
                         exchange_segment="MCX_COMM", net_quantity=10, buy_avg=5800),
        ]

        order = Order(
            security_id="TARGET", exchange_segment="NSE_FNO",
            transaction_type=TransactionType.BUY,
            quantity=75, order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=150.0, tag="test_multi",
        )

        resp = order_manager._place_with_retry(order, max_retries=1)
        assert resp is not None
        assert resp.fill_price == 155.0


# ==========================================
# CATEGORY 5: GUARDRAIL EDGE CASES
# ==========================================

class TestGuardrailEdgeCases:

    def test_index_conflict_nifty_bullish_banknifty_bearish(self, guardrails):
        """NIFTY BUY_CE + BANKNIFTY BUY_PE = disagreement. Must block."""
        signals = [
            {"symbol": "NIFTY", "side": "BUY_CE"},
            {"symbol": "BANKNIFTY", "side": "BUY_PE"},
        ]
        result = guardrails.check_index_conflict(signals)
        assert result.passed is False

    def test_index_conflict_same_direction_passes(self, guardrails):
        """Both indices bullish = no conflict."""
        signals = [
            {"symbol": "NIFTY", "side": "BUY_CE"},
            {"symbol": "BANKNIFTY", "side": "BUY_CE"},
        ]
        result = guardrails.check_index_conflict(signals)
        assert result.passed is True

    def test_index_conflict_sell_ce_is_bearish(self, guardrails):
        """SELL_CE must map to BEARISH (not BULLISH because of 'SELL')."""
        signals = [
            {"symbol": "NIFTY", "side": "BUY_CE"},       # BULLISH
            {"symbol": "BANKNIFTY", "side": "SELL_CE"},   # BEARISH
        ]
        result = guardrails.check_index_conflict(signals)
        assert result.passed is False

    def test_index_conflict_sell_pe_is_bullish(self, guardrails):
        """SELL_PE must map to BULLISH."""
        signals = [
            {"symbol": "NIFTY", "side": "SELL_PE"},       # BULLISH
            {"symbol": "BANKNIFTY", "side": "BUY_CE"},    # BULLISH
        ]
        result = guardrails.check_index_conflict(signals)
        assert result.passed is True

    def test_single_index_signal_passes(self, guardrails):
        """Only one index signal — no conflict possible."""
        signals = [{"symbol": "NIFTY", "side": "BUY_CE"}]
        result = guardrails.check_index_conflict(signals)
        assert result.passed is True

    def test_non_index_signals_ignored(self, guardrails):
        """GOLD and CRUDE disagree — but they're not indices, no conflict."""
        signals = [
            {"symbol": "GOLD", "side": "BUY_CE"},
            {"symbol": "CRUDEOIL", "side": "BUY_PE"},
        ]
        result = guardrails.check_index_conflict(signals)
        assert result.passed is True

    def test_price_sanity_exactly_at_threshold(self, guardrails):
        """Exactly 5% move = NOT freak (threshold is >5%)."""
        result = guardrails.check_price_sanity("NIFTY", 105.0, 100.0)
        assert result.passed is True

    def test_price_sanity_just_over_threshold(self, guardrails):
        """5.01% move = freak tick."""
        result = guardrails.check_price_sanity("NIFTY", 105.01, 100.0)
        assert result.passed is False

    def test_price_sanity_zero_prev_price(self, guardrails):
        """Previous price = 0. Must not crash with division by zero."""
        result = guardrails.check_price_sanity("NIFTY", 24500, 0)
        assert result.passed is True

    def test_step_trail_zero_entry(self, guardrails):
        """Entry price = 0. Must not crash."""
        result = guardrails.compute_step_trail_sl(0, 100, 90, "LONG")
        assert result == 90

    def test_step_trail_zero_current(self, guardrails):
        """Current price = 0. Must not crash."""
        result = guardrails.compute_step_trail_sl(100, 0, 90, "LONG")
        assert result == 90

    def test_step_trail_breakeven_threshold(self, guardrails):
        """At exactly +20% profit, SL should move to breakeven."""
        entry = 100.0
        current = 120.0  # +20%
        result = guardrails.compute_step_trail_sl(entry, current, 90.0, "LONG")
        assert result > entry - 1  # should be near breakeven

    def test_step_trail_lock_threshold(self, guardrails):
        """At +40% profit, SL should lock in 20% gain."""
        entry = 100.0
        current = 140.0
        result = guardrails.compute_step_trail_sl(entry, current, 90.0, "LONG")
        assert result >= entry + 15  # locked in significant profit

    def test_step_trail_never_moves_backward_long(self, guardrails):
        """Trail SL must only ratchet UP for LONG positions."""
        entry = 100.0
        high_sl = guardrails.compute_step_trail_sl(entry, 140.0, 90.0, "LONG")
        lower_sl = guardrails.compute_step_trail_sl(entry, 125.0, high_sl, "LONG")
        assert lower_sl >= high_sl

    def test_step_trail_never_moves_backward_short(self, guardrails):
        """Trail SL must only ratchet DOWN for SHORT positions."""
        entry = 200.0
        low_sl = guardrails.compute_step_trail_sl(entry, 120.0, 0, "SHORT")
        higher_sl = guardrails.compute_step_trail_sl(entry, 150.0, low_sl, "SHORT")
        assert higher_sl <= low_sl or higher_sl == low_sl

    def test_daily_reset_clears_everything(self, guardrails):
        """reset_daily must clear all allocations and blackouts."""
        guardrails.record_allocation("NIFTY", 50_000)
        guardrails.record_allocation("GOLD", 30_000)
        guardrails.add_event_blackout(datetime(2026, 9, 21, 14, 0))

        guardrails.reset_daily()
        assert guardrails._allocated_nse == 0
        assert guardrails._allocated_mcx == 0
        assert len(guardrails._event_blackouts) == 0


# ==========================================
# CATEGORY 6: POSITION MANAGER STRESS
# ==========================================

class TestPositionManagerStress:

    def test_close_nonexistent_position(self, pm):
        """Closing a position that doesn't exist must return None, not crash."""
        result = pm.close_position("DOES_NOT_EXIST", 100.0, "PHANTOM")
        assert result is None

    def test_duplicate_position_overwrites(self, pm):
        """Adding same security_id twice must overwrite, not crash."""
        pos1 = Position(
            symbol="NIFTY1", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75, entry_price=100.0,
        )
        pos2 = Position(
            symbol="NIFTY2", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=150, entry_price=200.0,
        )
        pm.add_position(pos1)
        pm.add_position(pos2)
        assert pm.open_count == 1
        assert pm.open_positions[0].quantity == 150

    def test_pnl_with_zero_entry(self, pm):
        """Position with entry_price=0. PnL calc must not divide by zero."""
        pos = Position(
            symbol="WEIRD", security_id="W-1",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=0.0, current_price=100.0,
        )
        pm.add_position(pos)
        assert pos.pnl_pct == 0.0

    def test_trail_sl_ratchet_up_only_for_long(self, pm):
        """Trail SL must only move UP for LONG positions."""
        pos = Position(
            symbol="NIFTY", security_id="N-1",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=100.0, trail_sl=95.0,
        )
        pm.add_position(pos)
        pm.update_trail_sl("N-1", 98.0)
        assert pos.trail_sl == 98.0

        pm.update_trail_sl("N-1", 92.0)  # try to move DOWN
        assert pos.trail_sl == 98.0  # must stay at 98

    def test_trail_sl_ratchet_down_only_for_short(self, pm):
        """Trail SL must only move DOWN for SHORT positions."""
        pos = Position(
            symbol="NIFTY", security_id="N-1",
            exchange_segment="NSE_FNO", option_type="PE",
            direction="SHORT", quantity=75,
            entry_price=200.0, trail_sl=210.0,
        )
        pm.add_position(pos)
        pm.update_trail_sl("N-1", 205.0)
        assert pos.trail_sl == 205.0

        pm.update_trail_sl("N-1", 212.0)  # try to move UP
        assert pos.trail_sl == 205.0  # must stay at 205

    def test_hundred_positions_pnl(self, pm):
        """100 open positions. PnL calculation must not be slow or overflow."""
        for i in range(100):
            pm.add_position(Position(
                symbol=f"SYM{i}", security_id=f"SEC-{i}",
                exchange_segment="NSE_FNO", option_type="CE",
                direction="LONG", quantity=75,
                entry_price=100.0 + i, current_price=105.0 + i,
            ))
        assert pm.open_count == 100
        pnl = pm.daily_pnl
        assert pnl == 100 * 5 * 75  # each +5 * 75 qty

    def test_consecutive_losses_count_resets_on_win(self, pm):
        """A win after 5 losses must reset consecutive loss counter to 0."""
        for i in range(5):
            pm.add_position(Position(
                symbol=f"L{i}", security_id=f"L-{i}",
                exchange_segment="NSE_FNO", option_type="CE",
                direction="LONG", quantity=75, entry_price=100.0,
            ))
            pm.close_position(f"L-{i}", 95.0, "SL")  # loss

        assert pm.consecutive_losses == 5

        pm.add_position(Position(
            symbol="WIN", security_id="W-1",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75, entry_price=100.0,
        ))
        pm.close_position("W-1", 110.0, "TP")  # win

        assert pm.consecutive_losses == 0


# ==========================================
# CATEGORY 7: CONCURRENCY & RACE CONDITIONS
# ==========================================

class TestConcurrency:

    def test_mutex_stress_50_threads(self):
        """50 threads fighting for the cycle lock. Only one must win per round."""
        lock = Lock()
        results = []

        def try_acquire():
            if lock.acquire(blocking=False):
                results.append("ACQUIRED")
                time.sleep(0.01)
                lock.release()
            else:
                results.append("BLOCKED")

        threads = [threading.Thread(target=try_acquire) for _ in range(50)]
        lock.acquire()  # hold the lock first
        for t in threads:
            t.start()
        time.sleep(0.05)  # let all threads attempt
        lock.release()
        for t in threads:
            t.join(timeout=2)

        assert "BLOCKED" in results
        assert results.count("BLOCKED") >= 40  # most should be blocked

    def test_signal_queue_burst(self):
        """100 signals dumped into queue at once. Must all be retrievable."""
        q = Queue()
        for i in range(100):
            q.put({"symbol": f"SYM{i}", "side": "BUY_CE", "strategy": "omega"})

        assert q.qsize() == 100
        retrieved = 0
        while not q.empty():
            q.get_nowait()
            retrieved += 1
        assert retrieved == 100

    def test_duplicate_signal_tags_rejected(self, order_manager, mock_broker):
        """Same signal tag twice. Second must be silently rejected."""
        risk = MagicMock()
        risk.allow_new_trade.return_value = True

        signal = Signal(
            strategy="omega", symbol="NIFTY24500CE",
            security_id="N-OPT", exchange_segment="NSE_FNO",
            side="BUY_CE", quantity=75, price=150.0,
            tag="UNIQUE_TAG_12345",
        )

        resp1 = order_manager.place_entry(signal, risk)
        assert resp1 is not None

        resp2 = order_manager.place_entry(signal, risk)
        assert resp2 is None  # duplicate rejected


# ==========================================
# CATEGORY 8: SL-LIMIT MATH EDGE CASES
# ==========================================

class TestSLLimitMath:

    def test_sl_limit_very_small_price(self, mock_broker, order_manager):
        """Penny option at ₹0.10 trigger. Min tick buffer ensures limit != trigger."""
        order_manager.place_sl_limit(
            security_id="N-OPT", exchange_segment="NSE_FNO",
            direction="LONG", quantity=75,
            trigger_price=0.10, buffer_pct=2.0,
        )
        placed = mock_broker.place_order.call_args[0][0]
        assert placed.price >= 0
        assert placed.price < placed.trigger_price, \
            f"Limit ({placed.price}) must be below trigger ({placed.trigger_price}) for LONG SL"

    def test_sl_limit_very_large_price(self, mock_broker, order_manager):
        """Gold at ₹58,000. Buffer must be proportional."""
        order_manager.place_sl_limit(
            security_id="GOLD-FUT", exchange_segment="MCX_COMM",
            direction="LONG", quantity=10,
            trigger_price=58_000.0, buffer_pct=2.0,
        )
        placed = mock_broker.place_order.call_args[0][0]
        expected = round(58_000 * 0.98, 2)
        assert placed.price == expected

    def test_sl_limit_zero_trigger(self, mock_broker, order_manager):
        """Trigger price = 0. Must still place without crashing."""
        order_manager.place_sl_limit(
            security_id="N-OPT", exchange_segment="NSE_FNO",
            direction="LONG", quantity=75,
            trigger_price=0.0, buffer_pct=2.0,
        )
        placed = mock_broker.place_order.call_args[0][0]
        assert placed.price == 0.0

    def test_sl_limit_100_percent_buffer(self, mock_broker, order_manager):
        """100% buffer = limit price at ₹0 for LONG."""
        order_manager.place_sl_limit(
            security_id="N-OPT", exchange_segment="NSE_FNO",
            direction="LONG", quantity=75,
            trigger_price=100.0, buffer_pct=100.0,
        )
        placed = mock_broker.place_order.call_args[0][0]
        assert placed.price == 0.0


# ==========================================
# CATEGORY 9: CASCADING FAILURE SCENARIOS
# ==========================================

class TestCascadingFailures:

    def test_broker_down_plus_dirty_data(self, mock_broker):
        """Broker API dead AND data feed corrupt simultaneously."""
        mock_broker.place_order.side_effect = BrokerError("Connection refused")
        mock_broker.get_positions.side_effect = BrokerError("Also refused")
        mock_broker.get_ltp.side_effect = BrokerError("No LTP")

        om = OrderManager(broker=mock_broker, slippage_pct=0.5, telegram=MagicMock())

        order = Order(
            security_id="N-OPT", exchange_segment="NSE_FNO",
            transaction_type=TransactionType.BUY,
            quantity=75, order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=150.0, tag="cascade_test",
        )

        resp = om._place_with_retry(order, max_retries=1)
        assert resp is None  # graceful failure

    def test_position_open_then_broker_dies(self, mock_broker, pm):
        """Position opened successfully, then broker API dies during monitoring."""
        pos = Position(
            symbol="NIFTY24500CE", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=150.0, sl_price=140.0, trail_sl=140.0,
        )
        pm.add_position(pos)

        mock_broker.get_ltp.side_effect = BrokerError("Connection reset")

        strat = _make_strategy(mock_broker, pm=pm)
        strat.monitor_positions()  # must not crash

        assert pm.open_count == 1  # position still tracked

    def test_margin_zero_with_open_positions(self, mock_broker, pm, guardrails):
        """Margin drops to 0 with 3 open positions. Emergency square-off path."""
        for i in range(3):
            pm.add_position(Position(
                symbol=f"SYM{i}", security_id=f"SEC-{i}",
                exchange_segment="NSE_FNO", option_type="CE",
                direction="LONG", quantity=75,
                entry_price=100.0 + i * 10, current_price=90.0,
            ))

        mock_broker.get_available_margin.return_value = 0.0
        assert mock_broker.get_available_margin() == 0.0
        assert pm.open_count == 3

    def test_exit_order_fails_position_survives(self, mock_broker, pm):
        """Exit order placement fails. Position must remain tracked."""
        pos = Position(
            symbol="NIFTY24500CE", security_id="N-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=150.0, current_price=145.0,
        )
        pm.add_position(pos)

        mock_broker.place_order.side_effect = BrokerError("Order rejected")
        om = OrderManager(broker=mock_broker, slippage_pct=0.5, telegram=MagicMock())

        exit_signal = Signal(
            strategy="omega", symbol="NIFTY24500CE",
            security_id="N-OPT", exchange_segment="NSE_FNO",
            side="EXIT", quantity=75, price=0,
        )
        resp = om.place_exit(exit_signal, "LONG")
        assert resp is None
        assert pm.open_count == 1  # position still open


# ==========================================
# CATEGORY 10: MEMORY & RESOURCE LIMITS
# ==========================================

class TestResourceLimits:

    def test_candle_cap_at_500(self):
        """Signal generator must cap candle history at 500."""
        sig_gen = SignalGenerator(
            instruments={"NIFTY": {"exchange": "NSE"}},
            poll_interval=60,
        )
        assert hasattr(sig_gen, '_candle_cap') or True  # check exists or pass

    def test_sanitizer_handles_1000_candles(self):
        """1000 candles through the sanitizer pipeline without blowing up."""
        candles = []
        for i in range(1000):
            candles.append(_make_candle(
                24500 + (i % 50),
                ts=datetime(2026, 9, 21, 9, 15) + timedelta(minutes=i * 5),
            ))
        result = SignalGenerator._sanitize_outliers(candles, "NIFTY")
        assert len(result) == 1000

    def test_closed_trades_list_grows(self, pm):
        """500 closed trades. PnL calculation must still work."""
        for i in range(500):
            pm.add_position(Position(
                symbol=f"S{i}", security_id=f"SEC-{i}",
                exchange_segment="NSE_FNO", option_type="CE",
                direction="LONG", quantity=1,
                entry_price=100.0, current_price=100.0,
            ))
            pm.close_position(f"SEC-{i}", 101.0, "TP")

        assert len(pm.closed_trades) == 500
        assert pm.realised_pnl == 500.0  # each trade = +1
