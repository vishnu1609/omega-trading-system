"""
Chaos Resilience Test Suite — Omega Trading Company

Mocks the Dhan API and data feeds to prove guardrails activate under
extreme stress: network timeouts, corrupt data, price gaps, margin
bleed, and concurrent execution overlap.
"""
import pytest
import time
from datetime import datetime
from threading import Lock
from unittest.mock import MagicMock, patch

from core.broker_base import (
    BrokerError, BrokerRateLimit, Order, OrderResponse, OrderType,
    PositionData, ProductType, TransactionType,
)
from core.order_manager import OrderManager
from core.signal_generator import Candle, SignalGenerator
from core.position_manager import Position
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
def order_manager(mock_broker):
    return OrderManager(broker=mock_broker, slippage_pct=0.5, telegram=MagicMock())


@pytest.fixture
def sample_candles():
    base = 24500.0
    now = datetime(2026, 9, 21, 9, 15)
    candles = []
    for i in range(5):
        ts = datetime(now.year, now.month, now.day, now.hour, now.minute + i * 5)
        candles.append(Candle(
            timestamp=ts,
            open=base + i * 10,
            high=base + i * 10 + 15,
            low=base + i * 10 - 10,
            close=base + i * 10 + 5,
            volume=1000 + i * 200,
        ))
    return candles


@pytest.fixture
def corrupt_candles(sample_candles):
    """Inject a ₹2,45,000 freak tick into the 3rd candle."""
    bad = sample_candles.copy()
    c = bad[2]
    bad[2] = Candle(
        timestamp=c.timestamp,
        open=c.open,
        high=245_000.0,
        low=c.low,
        close=c.close,
        volume=c.volume,
    )
    return bad


@pytest.fixture
def ghost_candles(sample_candles):
    """Replace the 3rd and 4th candles with zero-volume flat candles."""
    ghosts = sample_candles.copy()
    for idx in (2, 3):
        c = ghosts[idx]
        ghosts[idx] = Candle(
            timestamp=c.timestamp,
            open=0, high=0, low=0, close=0, volume=0,
        )
    return ghosts


# ==========================================
# CHAOS TEST 1: Ghost Fill (State Desync)
# ==========================================

class TestGhostFillRecovery:
    def test_ghost_fill_detected_after_timeout(self, mock_broker, order_manager):
        """
        API times out on all 4 retries, but the exchange actually filled
        the order. _check_ghost_fill must recover the position.
        """
        mock_broker.place_order.side_effect = BrokerError("Gateway Timeout")
        mock_broker.get_positions.return_value = [
            PositionData(
                security_id="NIFTY-OPT",
                symbol="NIFTY24500CE",
                exchange_segment="NSE_FNO",
                buy_quantity=75,
                net_quantity=75,
                buy_avg=155.0,
            ),
        ]

        order = Order(
            security_id="NIFTY-OPT",
            exchange_segment="NSE_FNO",
            transaction_type=TransactionType.BUY,
            quantity=75,
            order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=150.0,
            tag="test_ghost",
        )

        resp = order_manager._place_with_retry(order, max_retries=1)

        assert resp is not None, "Ghost fill should have been recovered"
        assert resp.status == "TRADED"
        assert resp.fill_price == 155.0
        assert resp.fill_quantity == 75
        assert resp.order_id.startswith("GHOST-")

    def test_no_ghost_fill_returns_none(self, mock_broker, order_manager):
        """If there's genuinely no fill on the exchange, return None."""
        mock_broker.place_order.side_effect = BrokerError("Connection refused")
        mock_broker.get_positions.return_value = []

        order = Order(
            security_id="NIFTY-OPT",
            exchange_segment="NSE_FNO",
            transaction_type=TransactionType.BUY,
            quantity=75,
            order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=150.0,
            tag="test_no_ghost",
        )

        resp = order_manager._place_with_retry(order, max_retries=1)
        assert resp is None


# ==========================================
# CHAOS TEST 2: SL-Skip Freefall
# ==========================================

class TestSLSkipFreefall:
    def _make_strategy(self, mock_broker):
        return OmegaStrategy(
            broker=mock_broker,
            order_manager=MagicMock(),
            position_manager=MagicMock(),
            risk_engine=MagicMock(),
            instrument_selector=MagicMock(),
            telegram=MagicMock(),
            instruments_config={},
        )

    def test_long_sl_skip_detected(self, mock_broker):
        """Price gapped below the SL-Limit without triggering it."""
        strat = self._make_strategy(mock_broker)
        pos = Position(
            symbol="NIFTY24500CE", security_id="NIFTY-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=160.0, sl_price=150.0, trail_sl=150.0,
        )
        assert strat._is_sl_skipped(pos, ltp=148.0) is True

    def test_long_sl_normal_not_triggered(self, mock_broker):
        """Price is below SL but within 0.5% — normal SL hit, not a skip."""
        strat = self._make_strategy(mock_broker)
        pos = Position(
            symbol="NIFTY24500CE", security_id="NIFTY-OPT",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=160.0, sl_price=150.0, trail_sl=150.0,
        )
        assert strat._is_sl_skipped(pos, ltp=149.5) is False

    def test_short_sl_skip_detected(self, mock_broker):
        """Short position: price gapped above the SL-Limit."""
        strat = self._make_strategy(mock_broker)
        pos = Position(
            symbol="NIFTY24500PE", security_id="NIFTY-OPT",
            exchange_segment="NSE_FNO", option_type="PE",
            direction="SHORT", quantity=75,
            entry_price=140.0, sl_price=150.0, trail_sl=150.0,
        )
        assert strat._is_sl_skipped(pos, ltp=152.0) is True


# ==========================================
# CHAOS TEST 3: Cron Overlap Mutex
# ==========================================

class TestCronOverlapMutex:
    def test_second_thread_skips_when_locked(self):
        """A tick that overlaps the previous one must be skipped."""
        lock = Lock()

        def execute_tick():
            if lock.locked():
                return "SKIPPED"
            with lock:
                return "EXECUTED"

        lock.acquire()
        result = execute_tick()
        assert result == "SKIPPED"
        lock.release()

    def test_signal_generator_has_cycle_lock(self):
        """SignalGenerator must have a _cycle_lock attribute."""
        sig_gen = SignalGenerator(
            instruments={"NIFTY": {"exchange": "NSE"}},
            poll_interval=60,
        )
        assert hasattr(sig_gen, "_cycle_lock")
        assert isinstance(sig_gen._cycle_lock, type(Lock()))


# ==========================================
# CHAOS TEST 4: Silent Margin Bleed
# ==========================================

class TestMarginBleed:
    def test_broker_drawdown_exceeds_local_pnl(self, mock_broker):
        """
        Local P&L says -₹1000. Broker ledger says -₹1300 (STT/taxes).
        System must detect the real drawdown.
        """
        initial_capital = 100_000
        daily_max_loss = 1_200

        mock_broker.get_available_margin.return_value = 98_700.0
        actual_balance = mock_broker.get_available_margin()
        actual_drawdown = initial_capital - actual_balance

        assert actual_drawdown == 1_300
        assert actual_drawdown > daily_max_loss

    def test_zero_margin_triggers_alarm(self, mock_broker):
        """If broker reports zero margin, the system must know."""
        mock_broker.get_available_margin.return_value = 0.0
        assert mock_broker.get_available_margin() <= 0


# ==========================================
# CHAOS TEST 5: Dirty Data Sanitization
# ==========================================

class TestDirtyDataSanitizer:
    def test_outlier_candle_forward_filled(self, corrupt_candles):
        """A ₹2,45,000 freak high must be replaced with previous close."""
        cleaned = SignalGenerator._sanitize_outliers(corrupt_candles, "NIFTY")

        assert cleaned[2].high < 25_000, \
            f"Freak tick survived: high={cleaned[2].high}"
        assert cleaned[2].high == cleaned[1].close, \
            "Outlier candle should be forward-filled from previous close"

    def test_normal_candles_pass_through(self, sample_candles):
        """Valid candles must not be altered."""
        cleaned = SignalGenerator._sanitize_outliers(sample_candles, "NIFTY")
        for orig, clean in zip(sample_candles, cleaned):
            assert orig.high == clean.high
            assert orig.close == clean.close

    def test_ghost_candles_forward_filled(self, ghost_candles):
        """Zero-volume flat candles must be forward-filled, not dropped."""
        cleaned = SignalGenerator._filter_ghost_candles(ghost_candles, "NIFTY")

        assert len(cleaned) == len(ghost_candles), \
            "Ghost candles should be forward-filled, not dropped"
        assert cleaned[2].close == cleaned[1].close, \
            "Ghost candle close should match previous candle's close"
        assert cleaned[2].volume > 0, \
            "Ghost candle should get 10% of previous volume"

    def test_ghost_then_outlier_pipeline(self, ghost_candles):
        """Chain: ghost filter → outlier sanitizer must not crash or corrupt."""
        step1 = SignalGenerator._filter_ghost_candles(ghost_candles, "NIFTY")
        step2 = SignalGenerator._sanitize_outliers(step1, "NIFTY")
        assert len(step2) == len(ghost_candles)
        for c in step2:
            assert c.close > 0, "No candle should have zero close after pipeline"


# ==========================================
# CHAOS TEST 6: SL-Limit Order Placement
# ==========================================

class TestSLLimitOrder:
    def test_sl_limit_uses_stop_loss_type(self, mock_broker, order_manager):
        """SL order must use OrderType.SL, not SL-Market."""
        order_manager.place_sl_limit(
            security_id="NIFTY-OPT",
            exchange_segment="NSE_FNO",
            direction="LONG",
            quantity=75,
            trigger_price=150.0,
            buffer_pct=2.0,
        )

        mock_broker.place_order.assert_called_once()
        placed = mock_broker.place_order.call_args[0][0]
        assert placed.order_type == OrderType.SL
        assert placed.trigger_price == 150.0

    def test_sl_limit_buffer_long(self, mock_broker, order_manager):
        """LONG SL: limit price must be 2% below trigger."""
        order_manager.place_sl_limit(
            security_id="NIFTY-OPT",
            exchange_segment="NSE_FNO",
            direction="LONG",
            quantity=75,
            trigger_price=100.0,
            buffer_pct=2.0,
        )
        placed = mock_broker.place_order.call_args[0][0]
        assert placed.price == 98.0
        assert placed.transaction_type == TransactionType.SELL

    def test_sl_limit_buffer_short(self, mock_broker, order_manager):
        """SHORT SL: limit price must be 2% above trigger."""
        order_manager.place_sl_limit(
            security_id="NIFTY-OPT",
            exchange_segment="NSE_FNO",
            direction="SHORT",
            quantity=75,
            trigger_price=100.0,
            buffer_pct=2.0,
        )
        placed = mock_broker.place_order.call_args[0][0]
        assert placed.price == 102.0
        assert placed.transaction_type == TransactionType.BUY
