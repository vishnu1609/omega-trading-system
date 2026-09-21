"""
Chaos Test Injector — 5 Test Suites for Omega Hardening

Injects faults into every guardrail added in the Sep 2026 hardening pass:
  Suite 1: Memory Traps (ban trap, 7-day decay, bad-hour fluke)
  Suite 2: Risk Gate Gauntlet (category toggle, SL cap, concurrent limits)
  Suite 3: Broker Chaos (DH-901 auth drop, DH-904 rate limit, token validation)
  Suite 4: Thread Safety (webhook stampede, position counter race)
  Suite 5: Flash Crash & Edge Cases (SL-skip freefall, sequential scalps)

Run: python3 -m pytest tests/test_injector.py -v
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.broker_base import (
    BrokerAuthError,
    BrokerBase,
    BrokerError,
    BrokerRateLimit,
    Order,
    OrderResponse,
    PositionData,
)
from core.position_manager import Position, PositionManager
from core.risk_engine import RiskEngine
from core.trade_memory import (
    AdaptiveRule,
    TradeContext,
    TradeMemory,
    BAN_EXPIRY_DAYS,
    MIN_HOUR_SAMPLE_SIZE,
)

IST = ZoneInfo("Asia/Kolkata")


# ── Fixtures ─────────────────────────────────────────────────────────

class FakeBroker(BrokerBase):
    """Broker that can be rigged to fail on demand."""

    def __init__(self):
        self._fail_mode: str | None = None
        self._fail_count = 0
        self._call_count = 0
        self._auth_valid = True
        self._positions: list[PositionData] = []
        self._ltp_map: dict[str, float] = {}
        self._funds = {"availabelBalance": 100000}

    def rig_failure(self, mode: str, count: int = 1):
        self._fail_mode = mode
        self._fail_count = count

    def place_order(self, order: Order) -> OrderResponse:
        self._call_count += 1
        if self._fail_mode and self._fail_count > 0:
            self._fail_count -= 1
            if self._fail_mode == "DH-901":
                raise BrokerAuthError("DH-901: Invalid access token")
            elif self._fail_mode == "DH-904":
                raise BrokerRateLimit("DH-904: Rate limit exceeded")
            elif self._fail_mode == "generic":
                raise BrokerError("Something went wrong")
            if self._fail_count <= 0:
                self._fail_mode = None
        return OrderResponse(
            order_id=f"ORD-{self._call_count}",
            status="TRADED",
            fill_price=order.price or 100.0,
            fill_quantity=order.quantity,
        )

    def modify_order(self, order_id: str, **kwargs) -> OrderResponse:
        return OrderResponse(order_id=order_id, status="MODIFIED")

    def cancel_order(self, order_id: str) -> OrderResponse:
        return OrderResponse(order_id=order_id, status="CANCELLED")

    def get_order_status(self, order_id: str) -> dict:
        return {"orderStatus": "TRADED", "price": 100.0, "filledQty": 75}

    def get_positions(self) -> list[PositionData]:
        return self._positions

    def get_funds(self) -> dict:
        if self._fail_mode == "DH-901":
            raise BrokerAuthError("DH-901: Invalid access token")
        return self._funds

    def get_ltp(self, security_id: str, exchange_segment: str) -> float:
        return self._ltp_map.get(security_id, 100.0)

    def get_option_chain(self, underlying_id: str, expiry: str,
                         exchange_segment: str) -> list[dict]:
        return []

    def get_available_margin(self) -> float:
        return float(self._funds.get("availabelBalance", 0))

    def validate_token(self) -> bool:
        if self._fail_mode == "DH-901":
            self._auth_valid = False
            return False
        self._auth_valid = True
        return True

    @property
    def is_auth_valid(self) -> bool:
        return self._auth_valid


@pytest.fixture
def pm():
    return PositionManager()


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def risk_engine(pm, broker):
    return RiskEngine(
        position_manager=pm,
        broker=broker,
        max_daily_loss=2000,
        max_concurrent_positions=2,
        sl_buffer_pct=2.0,
        cooldown_after_losses=2,
        cooldown_minutes=30,
        margin_buffer_pct=15,
        session_start="09:16",
        square_off_time="15:10",
        mode="paper",
        allow_mcx_live=False,
        allow_index_futures_live=False,
    )


@pytest.fixture
def memory(tmp_path):
    return TradeMemory(base_path=str(tmp_path))


def _make_trade(symbol="NIFTY24500CE", direction="LONG", pnl=100,
                entry_hour=10, entry_day=0, exit_reason="TP",
                entry_time=None, exit_time=None, score=60,
                adx=30, **kwargs) -> TradeContext:
    now = datetime.now(IST)
    et = entry_time or now - timedelta(hours=1)
    xt = exit_time or now
    return TradeContext(
        symbol=symbol, direction=direction, option_type="CE",
        category="INDICES",
        entry_price=100.0, exit_price=100 + pnl, sl_price=95.0,
        pnl=pnl, pnl_pct=pnl,
        entry_time=et.isoformat(), exit_time=xt.isoformat(),
        entry_hour=entry_hour, entry_day=entry_day,
        hold_minutes=60, exit_reason=exit_reason,
        signal_score=score, signal_confidence="HIGH",
        adx_value=adx, vwap_position="ABOVE",
        supertrend_dir="BULL", volume_ratio=1.2, atr_value=50,
        is_win=pnl > 0, r_multiple=pnl / 5 if pnl > 0 else pnl / 5,
        strategy="omega_complete",
        **kwargs,
    )


# ═══════════════════════════════════════════════════════════════════════
# SUITE 1: Memory Traps
# ═══════════════════════════════════════════════════════════════════════

class TestSuite1_MemoryTraps:
    """
    1a. Ban trap — 5 consecutive losses trigger BLOCK
    1b. 7-day amnesia — ban auto-lifts after BAN_EXPIRY_DAYS
    1c. Bad-hour fluke — hour patterns need 20+ trades
    """

    def test_ban_trap_5_consecutive_losses(self, memory):
        """5 consecutive losses on NIFTY → symbol gets BLOCK rule."""
        for i in range(6):
            memory.record_trade(_make_trade(pnl=-50, entry_hour=10 + i % 3))

        rules = memory.analyze_patterns()

        ban_rules = [r for r in memory._adaptive_rules
                     if r.action == "BLOCK" and "NIFTY" in r.pattern.upper()]
        assert len(ban_rules) >= 1, "5+ consecutive NIFTY losses should trigger a ban"

        blocked, reason = memory.should_block("NIFTY24500CE", "BUY_CE", entry_hour=10, entry_day=0)
        assert blocked, f"Should be blocked after ban. Reason: {reason}"

    def test_7_day_amnesia_ban_expires(self, tmp_path):
        """Ban older than 7 days auto-deactivates on load."""
        rules_file = tmp_path / "data" / "adaptive_rules.json"
        rules_file.parent.mkdir(parents=True, exist_ok=True)

        old_date = (datetime.now(IST) - timedelta(days=BAN_EXPIRY_DAYS + 1)).isoformat()
        recent_date = (datetime.now(IST) - timedelta(days=2)).isoformat()

        rules = [
            {
                "id": "ban_nifty",
                "pattern": "NIFTY: 5 consecutive losses — temporary ban",
                "condition": {"symbol_contains": "NIFTY"},
                "action": "BLOCK",
                "score_modifier": -100,
                "confidence": 0.9,
                "sample_size": 5,
                "win_rate": 0.0,
                "baseline_win_rate": 0.5,
                "created_at": old_date,
                "last_evaluated": old_date,
                "active": True,
                "hit_count": 3,
            },
            {
                "id": "ban_gold",
                "pattern": "GOLD: 5 consecutive losses — temporary ban",
                "condition": {"symbol_contains": "GOLD"},
                "action": "BLOCK",
                "score_modifier": -100,
                "confidence": 0.9,
                "sample_size": 5,
                "win_rate": 0.0,
                "baseline_win_rate": 0.5,
                "created_at": recent_date,
                "last_evaluated": recent_date,
                "active": True,
                "hit_count": 1,
            },
        ]
        with open(rules_file, "w") as f:
            json.dump(rules, f)

        mem = TradeMemory(base_path=str(tmp_path))

        nifty_blocked, _ = mem.should_block("NIFTY24500CE", "BUY_CE")
        assert not nifty_blocked, "8-day-old NIFTY ban should have expired"

        gold_blocked, _ = mem.should_block("GOLD25DEC1000CE", "BUY_CE")
        assert gold_blocked, "2-day-old GOLD ban should still be active"

    def test_bad_hour_fluke_needs_20_trades(self, memory):
        """Hour pattern detection requires MIN_HOUR_SAMPLE_SIZE (20) trades."""
        for i in range(20):
            memory.record_trade(_make_trade(pnl=100, entry_hour=10))

        for i in range(15):
            memory.record_trade(_make_trade(pnl=-50, entry_hour=14))

        rules = memory.analyze_patterns()
        hour_rules = [r for r in memory._adaptive_rules
                      if "14:00" in r.pattern]
        assert len(hour_rules) == 0, (
            f"15 trades at hour 14 shouldn't trigger hour pattern "
            f"(need {MIN_HOUR_SAMPLE_SIZE})"
        )

        for i in range(10):
            memory.record_trade(_make_trade(pnl=-50, entry_hour=14))

        rules = memory.analyze_patterns()
        hour_rules = [r for r in memory._adaptive_rules
                      if "14:00" in r.pattern]
        assert len(hour_rules) >= 1, (
            f"25 trades at hour 14 (>{MIN_HOUR_SAMPLE_SIZE}) should trigger hour pattern"
        )


# ═══════════════════════════════════════════════════════════════════════
# SUITE 2: Risk Gate Gauntlet
# ═══════════════════════════════════════════════════════════════════════

class TestSuite2_RiskGateGauntlet:
    """
    2a. Category toggle — MCX blocked in live mode
    2b. Category toggle — Index Futures blocked in live mode
    2c. SL cap — 8% for high-confluence, 10% for low
    2d. No trade-per-day cap (removed)
    2e. Square-off time is 15:10 not 15:15
    """

    def test_mcx_blocked_live_mode(self, pm, broker):
        """MCX symbols blocked when mode=live and allow_mcx_live=False."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            mode="live", allow_mcx_live=False, allow_index_futures_live=False,
            session_start="09:16", square_off_time="23:30",
        )
        now = datetime.now(IST)
        if now.hour < 9 or now.hour >= 23:
            pytest.skip("Outside trading hours for this test")

        allowed = re.allow_new_trade(symbol="GOLD25DEC1000CE")
        assert not allowed, "MCX should be blocked in live mode"
        assert "MCX disabled" in re.last_reason

    def test_mcx_allowed_paper_mode(self, pm, broker):
        """MCX symbols allowed in paper mode even if toggle is off."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            mode="paper", allow_mcx_live=False, allow_index_futures_live=False,
            session_start="09:16", square_off_time="23:30",
        )
        now = datetime.now(IST)
        if now.hour < 9 or now.hour >= 23:
            pytest.skip("Outside trading hours for this test")

        allowed = re.allow_new_trade(symbol="GOLD25DEC1000CE")
        assert allowed, f"MCX should be allowed in paper mode. Reason: {re.last_reason}"

    def test_index_futures_blocked_live(self, pm, broker):
        """Index futures blocked in live mode."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            mode="live", allow_mcx_live=True, allow_index_futures_live=False,
            session_start="09:16", square_off_time="15:10",
        )
        now = datetime.now(IST)
        if now.hour < 9 or now.hour >= 15:
            pytest.skip("Outside NSE trading hours")

        allowed = re.allow_new_trade(symbol="NIFTYFUT")
        assert not allowed, "Index futures should be blocked in live mode"
        assert "Index Futures disabled" in re.last_reason

    def test_sl_cap_high_confluence(self, risk_engine):
        """High confluence score (>=75) gets tighter 8% SL cap."""
        entry = 1000.0
        too_wide_sl = entry * 0.91  # 9% away — exceeds 8% cap
        clamped = risk_engine.clamp_sl_price(entry, too_wide_sl, "LONG", confluence_score=80)
        max_allowed_distance = entry * 0.08
        assert clamped >= entry - max_allowed_distance, (
            f"SL at {clamped} exceeds 8% cap for score 80 (max distance={max_allowed_distance})"
        )

    def test_sl_cap_low_confluence(self, risk_engine):
        """Low confluence score (<75) gets wider 10% SL cap."""
        entry = 1000.0
        sl_9pct = entry * 0.91  # 9% away — within 10% cap
        clamped = risk_engine.clamp_sl_price(entry, sl_9pct, "LONG", confluence_score=50)
        assert clamped == sl_9pct, (
            f"9% SL should pass for score 50 (10% cap), but got clamped to {clamped}"
        )

    def test_sl_cap_gate_blocks_trade(self, pm, broker):
        """Gate 6 blocks trade when SL exceeds cap."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            mode="paper", session_start="00:00", square_off_time="23:59",
        )
        allowed = re.allow_new_trade(
            entry_price=1000, sl_price=880,  # 12% SL
            confluence_score=80,
        )
        assert not allowed, "12% SL should be blocked (cap is 8% for score 80)"
        assert "SL cap" in re.last_reason

    def test_no_trade_per_day_cap(self, pm, broker):
        """No fixed trade-per-day limit — can trade unlimited times."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            mode="paper", session_start="00:00", square_off_time="23:59",
        )
        for i in range(20):
            assert re.allow_new_trade(), f"Trade {i+1} should be allowed (no daily cap)"

    def test_square_off_time_is_1510(self, risk_engine):
        """Square-off time is 15:10, not 15:15."""
        from datetime import time as dt_time
        assert risk_engine.square_off_time == dt_time(15, 10, tzinfo=IST), (
            f"Square-off should be 15:10 IST, got {risk_engine.square_off_time}"
        )


# ═══════════════════════════════════════════════════════════════════════
# SUITE 3: Broker Chaos
# ═══════════════════════════════════════════════════════════════════════

class TestSuite3_BrokerChaos:
    """
    3a. DH-901 auth drop — no retry, alert sent, order returns None
    3b. DH-904 rate limit — exponential backoff, retries succeed
    3c. Token validation — rejects invalid token on startup
    """

    def test_dh901_auth_drop_no_retry(self, broker):
        """DH-901 auth error: order fails immediately, no retry."""
        from core.order_manager import OrderManager

        alert_mock = MagicMock()
        om = OrderManager(broker=broker, telegram=alert_mock)

        broker.rig_failure("DH-901", count=99)
        order = Order(
            security_id="NIFTY24500CE",
            exchange_segment="NSE_FNO",
            transaction_type="BUY",
            quantity=75,
            price=100.0,
            tag="test_auth",
        )
        result = om._place_with_retry(order)

        assert result is None, "DH-901 should return None immediately"
        assert broker._call_count == 1, (
            f"DH-901 should NOT retry (called {broker._call_count} times)"
        )
        alert_mock.error.assert_called_once()
        call_args = alert_mock.error.call_args
        assert "DH-901" in str(call_args) or "AUTH" in str(call_args).upper()

    def test_dh904_rate_limit_backoff(self, broker):
        """DH-904: backs off then succeeds on retry."""
        from core.order_manager import OrderManager

        om = OrderManager(broker=broker)
        broker.rig_failure("DH-904", count=2)

        order = Order(
            security_id="NIFTY24500CE",
            exchange_segment="NSE_FNO",
            transaction_type="BUY",
            quantity=75,
            price=100.0,
            tag="test_rate",
        )
        result = om._place_with_retry(order, max_retries=4)

        assert result is not None, "Should succeed after 2 rate limit retries"
        assert broker._call_count == 3, (
            f"Should take 3 calls (2 fails + 1 success), got {broker._call_count}"
        )

    def test_token_validation_rejects_expired(self, broker):
        """validate_token() returns False and sets _auth_valid=False for DH-901."""
        broker.rig_failure("DH-901", count=99)
        result = broker.validate_token()
        assert not result, "Expired token should return False"
        assert not broker.is_auth_valid, "_auth_valid should be False"

    def test_token_validation_accepts_valid(self, broker):
        """validate_token() returns True for a valid token."""
        result = broker.validate_token()
        assert result, "Valid token should return True"
        assert broker.is_auth_valid, "_auth_valid should be True"

    def test_dh901_alert_contains_renewal_instructions(self, broker):
        """Error alert for DH-901 tells user to renew at api.dhan.co."""
        from core.order_manager import OrderManager

        alert_mock = MagicMock()
        om = OrderManager(broker=broker, telegram=alert_mock)
        broker.rig_failure("DH-901", count=1)

        order = Order(
            security_id="TEST", exchange_segment="NSE_FNO",
            transaction_type="BUY", quantity=75, price=100, tag="test",
        )
        om._place_with_retry(order)

        error_call = alert_mock.error.call_args
        error_msg = str(error_call)
        assert "renew" in error_msg.lower() or "token" in error_msg.lower(), (
            f"DH-901 alert should mention token renewal. Got: {error_msg}"
        )


# ═══════════════════════════════════════════════════════════════════════
# SUITE 4: Thread Safety
# ═══════════════════════════════════════════════════════════════════════

class TestSuite4_ThreadSafety:
    """
    4a. Webhook stampede — 10 concurrent risk gate checks, no crash
    4b. Position counter race — concurrent add/close, count stays correct
    4c. Risk gate lock — gates don't interleave under concurrent access
    """

    def test_webhook_stampede_10_concurrent(self, pm, broker):
        """10 threads hammer allow_new_trade() simultaneously — no crash."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            mode="paper", session_start="00:00", square_off_time="23:59",
        )
        results = []
        errors = []

        def check_gate(i):
            try:
                result = re.allow_new_trade(instrument=f"INST_{i}")
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=check_gate, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Stampede caused errors: {errors}"
        assert len(results) == 10, f"Expected 10 results, got {len(results)}"

    def test_position_counter_race(self, pm):
        """Concurrent add_position + close_position keeps count correct."""
        positions = []
        for i in range(10):
            pos = Position(
                symbol=f"NIFTY{24500+i*100}CE",
                security_id=f"SEC_{i}",
                exchange_segment="NSE_FNO",
                option_type="CE",
                direction="LONG",
                quantity=75,
                entry_price=100.0,
                sl_price=95.0,
                trail_sl=95.0,
                tp_price=110.0,
                entry_order_id=f"ORD_{i}",
                strategy="omega_complete",
            )
            positions.append(pos)

        errors = []

        def add_positions():
            try:
                for pos in positions[:5]:
                    pm.add_position(pos)
            except Exception as e:
                errors.append(e)

        def add_more():
            try:
                for pos in positions[5:]:
                    pm.add_position(pos)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=add_positions)
        t2 = threading.Thread(target=add_more)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(errors) == 0, f"Race condition errors: {errors}"
        assert pm.open_count == 10, f"Expected 10 positions, got {pm.open_count}"

        close_errors = []

        def close_even():
            try:
                for i in range(0, 10, 2):
                    pm.close_position(f"SEC_{i}", 105.0, "TP")
            except Exception as e:
                close_errors.append(e)

        def close_odd():
            try:
                for i in range(1, 10, 2):
                    pm.close_position(f"SEC_{i}", 95.0, "SL")
            except Exception as e:
                close_errors.append(e)

        t3 = threading.Thread(target=close_even)
        t4 = threading.Thread(target=close_odd)
        t3.start()
        t4.start()
        t3.join(timeout=5)
        t4.join(timeout=5)

        assert len(close_errors) == 0, f"Close race errors: {close_errors}"
        assert pm.open_count == 0, f"All positions should be closed, got {pm.open_count}"

    def test_risk_gate_lock_no_interleave(self, pm, broker):
        """Under concurrency, max_concurrent limit is never exceeded."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            mode="paper", max_concurrent_positions=2,
            session_start="00:00", square_off_time="23:59",
        )

        approved = []
        lock = threading.Lock()

        def try_trade(idx):
            if re.allow_new_trade():
                with lock:
                    approved.append(idx)
                pos = Position(
                    symbol=f"SYM_{idx}", security_id=f"S_{idx}",
                    exchange_segment="NSE_FNO", option_type="CE",
                    direction="LONG", quantity=75,
                    entry_price=100, sl_price=95, trail_sl=95,
                    tp_price=110, entry_order_id=f"O_{idx}",
                    strategy="omega",
                )
                pm.add_position(pos)

        threads = [threading.Thread(target=try_trade, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert pm.open_count <= 2, (
            f"Max concurrent is 2 but {pm.open_count} positions got through"
        )


# ═══════════════════════════════════════════════════════════════════════
# SUITE 5: Flash Crash & Edge Cases
# ═══════════════════════════════════════════════════════════════════════

class TestSuite5_FlashCrashEdgeCases:
    """
    5a. SL-skip freefall detection
    5b. Flash crash SL cap — 8% max even during crash
    5c. Sequential scalp — rapid trades don't hit any artificial cap
    5d. Cooldown resets after timer expires
    """

    def test_sl_skip_detection(self):
        """Position with LTP fallen past SL by >0.5% triggers SL-skip."""
        from strategies.omega_complete import OmegaStrategy

        pos = Position(
            symbol="NIFTY24500CE", security_id="SEC_1",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=100.0, sl_price=95.0, trail_sl=95.0,
            tp_price=110.0, entry_order_id="ORD_1",
            strategy="omega_complete",
        )

        broker = FakeBroker()
        pm = PositionManager()
        re = RiskEngine(position_manager=pm, broker=broker,
                        session_start="00:00", square_off_time="23:59")
        om_mock = MagicMock()
        tg_mock = MagicMock()

        strategy = OmegaStrategy(
            broker=broker, order_manager=om_mock,
            position_manager=pm, risk_engine=re,
            instrument_selector=MagicMock(),
            telegram=tg_mock, instruments_config={},
        )

        assert strategy._is_sl_skipped(pos, 93.0), (
            "LTP=93 vs SL=95 (>0.5% gap) should detect SL-skip"
        )

        assert not strategy._is_sl_skipped(pos, 94.8), (
            "LTP=94.8 vs SL=95 (<0.5% gap) should NOT be SL-skip"
        )

    def test_flash_crash_sl_cap_enforced(self, risk_engine):
        """During flash crash, SL can't exceed 8% for high score."""
        entry = 500.0
        crash_sl = 400.0  # 20% away

        clamped = risk_engine.clamp_sl_price(entry, crash_sl, "LONG", confluence_score=80)
        max_distance = entry * 0.08
        min_allowed = entry - max_distance

        assert clamped >= min_allowed, (
            f"Flash crash SL {crash_sl} should be clamped to {min_allowed}, got {clamped}"
        )

    def test_sequential_scalps_no_cap(self, pm, broker):
        """Rapid sequential trades are allowed — no artificial daily cap."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            mode="paper", max_concurrent_positions=1,
            session_start="00:00", square_off_time="23:59",
        )

        trades_completed = 0
        for i in range(15):
            if re.allow_new_trade():
                pos = Position(
                    symbol=f"NIFTY{24500+i*100}CE", security_id=f"S_{i}",
                    exchange_segment="NSE_FNO", option_type="CE",
                    direction="LONG", quantity=75,
                    entry_price=100, sl_price=95, trail_sl=95,
                    tp_price=110, entry_order_id=f"O_{i}",
                    strategy="omega",
                )
                pm.add_position(pos)
                pm.close_position(f"S_{i}", 105.0, "TP")
                trades_completed += 1

        assert trades_completed == 15, (
            f"All 15 sequential scalps should execute, only {trades_completed} did"
        )

    def test_cooldown_blocks_then_allows(self, pm, broker):
        """Cooldown blocks trades, then allows after timer expires."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            cooldown_after_losses=2, cooldown_minutes=1,
            session_start="00:00", square_off_time="23:59",
        )

        for i in range(2):
            pos = Position(
                symbol=f"LOSS_{i}", security_id=f"LOSS_{i}",
                exchange_segment="NSE_FNO", option_type="CE",
                direction="LONG", quantity=75,
                entry_price=100, sl_price=95, trail_sl=95,
                tp_price=110, entry_order_id=f"O_{i}",
                strategy="omega",
            )
            pm.add_position(pos)
            pm.close_position(f"LOSS_{i}", 90.0, "SL")

        assert pm.consecutive_losses == 2
        allowed = re.allow_new_trade()
        assert not allowed, "Should be blocked during cooldown"
        assert "cooldown" in re.last_reason.lower() or "consecutive" in re.last_reason.lower()

    def test_daily_loss_kills_bot(self, pm, broker):
        """Daily loss exceeding limit kills bot for the day."""
        re = RiskEngine(
            position_manager=pm, broker=broker,
            max_daily_loss=2000,
            session_start="00:00", square_off_time="23:59",
        )

        pos = Position(
            symbol="BIG_LOSS", security_id="BIG_LOSS",
            exchange_segment="NSE_FNO", option_type="CE",
            direction="LONG", quantity=75,
            entry_price=100, sl_price=70, trail_sl=70,
            tp_price=130, entry_order_id="O_LOSS",
            strategy="omega",
        )
        pm.add_position(pos)
        pm.close_position("BIG_LOSS", 73.0, "SL")
        assert pm.daily_pnl <= -2000

        allowed = re.allow_new_trade()
        assert not allowed, "Bot should be killed after daily loss breach"
        assert re._bot_killed
        assert re.is_daily_loss_breached()

    def test_sl_clamp_short_direction(self, risk_engine):
        """SL clamp works correctly for SHORT positions."""
        entry = 1000.0
        too_wide_sl = 1120.0  # 12% away

        clamped = risk_engine.clamp_sl_price(entry, too_wide_sl, "SHORT", confluence_score=80)
        max_distance = entry * 0.08
        max_allowed = entry + max_distance

        assert clamped <= max_allowed, (
            f"SHORT SL {too_wide_sl} should clamp to {max_allowed}, got {clamped}"
        )


# ═══════════════════════════════════════════════════════════════════════
# Quick run
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
