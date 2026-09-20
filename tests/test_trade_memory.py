"""Tests for the self-learning Trade Memory engine."""
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.trade_memory import TradeMemory, TradeContext, Lesson, AdaptiveRule

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "data").mkdir()
        yield d


@pytest.fixture
def memory(tmp_dir):
    return TradeMemory(base_path=tmp_dir)


def _make_trade(symbol="NIFTY24500CE", direction="LONG", pnl=1000.0,
                entry_hour=10, entry_day=0, adx=28.0,
                signal_confidence="HIGH", exit_reason="TP",
                supertrend_dir="BULL", vwap_position="ABOVE",
                entry_offset_days=0):
    now = datetime.now(IST) - timedelta(days=entry_offset_days)
    entry = now.replace(hour=entry_hour, minute=15)
    exit_t = entry + timedelta(minutes=45)
    return TradeContext(
        symbol=symbol, direction=direction, option_type="CE",
        category="INDICES",
        entry_price=155.0, exit_price=155.0 + (pnl / 75),
        sl_price=148.0, pnl=pnl, pnl_pct=pnl / 155 / 75 * 100,
        entry_time=entry.isoformat(), exit_time=exit_t.isoformat(),
        entry_hour=entry_hour, entry_day=entry_day,
        hold_minutes=45, exit_reason=exit_reason,
        signal_score=72, signal_confidence=signal_confidence,
        adx_value=adx, vwap_position=vwap_position,
        supertrend_dir=supertrend_dir, volume_ratio=1.2,
        atr_value=50.0, is_win=pnl > 0,
        r_multiple=pnl / (155.0 - 148.0) / 75 if pnl != 0 else 0,
    )


class TestTradeRecording:
    def test_record_and_persist(self, memory, tmp_dir):
        trade = _make_trade(pnl=1500)
        memory.record_trade(trade)
        assert memory.trade_count == 1

        journal = Path(tmp_dir) / "data" / "trade_journal.json"
        assert journal.exists()
        data = json.loads(journal.read_text())
        assert len(data) == 1
        assert data[0]["pnl"] == 1500

    def test_reload_persisted_trades(self, tmp_dir):
        mem1 = TradeMemory(base_path=tmp_dir)
        mem1.record_trade(_make_trade(pnl=1000))
        mem1.record_trade(_make_trade(pnl=-500))
        assert mem1.trade_count == 2

        mem2 = TradeMemory(base_path=tmp_dir)
        assert mem2.trade_count == 2

    def test_build_trade_context(self, memory):
        now = datetime.now(IST)
        ctx = memory.build_trade_context(
            symbol="GOLD25DEC", direction="LONG", option_type="CE",
            entry_price=5800, exit_price=5850, sl_price=5750,
            pnl=5000, entry_time=now, exit_time=now + timedelta(hours=1),
            exit_reason="TP", adx_value=32.5,
        )
        assert ctx.symbol == "GOLD25DEC"
        assert ctx.category == "MCX"
        assert ctx.is_win is True
        assert ctx.hold_minutes == 60.0


class TestUserLessons:
    def test_teach_and_persist(self, memory, tmp_dir):
        lesson = memory.teach(
            rule="Don't trade GOLD before US Fed meeting",
            condition={"symbol_contains": "GOLD"},
            action="BLOCK",
            score_modifier=-100,
        )
        assert lesson.id.startswith("lesson_")
        assert lesson.active is True
        assert len(memory.lessons) == 1

        lessons_file = Path(tmp_dir) / "data" / "lessons.json"
        assert lessons_file.exists()

    def test_teach_simple(self, memory):
        lesson = memory.teach_simple(
            description="Avoid 2pm trades — lunch volatility trap",
            hour=14,
            score_modifier=-20,
        )
        assert lesson.condition == {"entry_hour": 14}

    def test_forget_lesson(self, memory):
        lesson = memory.teach("test rule", {"symbol": "NIFTY"})
        assert len(memory.lessons) == 1
        memory.forget_lesson(lesson.id)
        assert len(memory.lessons) == 0

    def test_lesson_blocks_trade(self, memory):
        memory.teach(
            rule="Block NATURALGAS",
            condition={"symbol_contains": "NATURALGAS"},
            action="BLOCK",
            score_modifier=-100,
        )
        blocked, reason = memory.should_block("NATURALGAS25OCT", "BUY_CE")
        assert blocked is True
        assert "NATURALGAS" in reason

    def test_non_matching_lesson_passes(self, memory):
        memory.teach(
            rule="Block NATURALGAS",
            condition={"symbol_contains": "NATURALGAS"},
            action="BLOCK",
        )
        blocked, _ = memory.should_block("NIFTY24500CE", "BUY_CE")
        assert blocked is False


class TestScoreAdjustment:
    def test_lesson_adjusts_score(self, memory):
        memory.teach(
            rule="Penalize afternoon trades",
            condition={"entry_hour": 14},
            action="PENALIZE",
            score_modifier=-15.0,
        )
        adj, reasons = memory.get_score_adjustment(
            "NIFTY24500CE", "BUY_CE", entry_hour=14)
        assert adj == -15.0
        assert len(reasons) == 1
        assert "LESSON" in reasons[0]

    def test_no_adjustment_when_no_match(self, memory):
        memory.teach("test", {"entry_hour": 14}, score_modifier=-10)
        adj, reasons = memory.get_score_adjustment(
            "NIFTY24500CE", "BUY_CE", entry_hour=10)
        assert adj == 0.0
        assert len(reasons) == 0

    def test_multiple_adjustments_stack(self, memory):
        memory.teach("rule1", {"entry_hour": 10}, score_modifier=-10)
        memory.teach("rule2", {"symbol_contains": "NIFTY"}, score_modifier=-5)
        adj, reasons = memory.get_score_adjustment(
            "NIFTY24500CE", "BUY_CE", entry_hour=10)
        assert adj == -15.0
        assert len(reasons) == 2


class TestPatternDetection:
    def test_no_analysis_with_few_trades(self, memory):
        memory.record_trade(_make_trade(pnl=1000))
        rules = memory.analyze_patterns()
        assert len(rules) == 0

    def test_detects_losing_hour(self, memory):
        for i in range(6):
            memory.record_trade(_make_trade(pnl=-500, entry_hour=14, entry_offset_days=i))
        for i in range(6):
            memory.record_trade(_make_trade(pnl=1000, entry_hour=10, entry_offset_days=i))

        rules = memory.analyze_patterns()
        hour_rules = [r for r in rules if "14:00" in r.pattern]
        assert len(hour_rules) >= 1
        assert hour_rules[0].action == "PENALIZE"

    def test_detects_symbol_consecutive_losses(self, memory):
        for i in range(6):
            memory.record_trade(_make_trade(
                symbol="NATURALGAS25OCT", pnl=-2000, entry_offset_days=i))
        for i in range(6):
            memory.record_trade(_make_trade(pnl=1000, entry_offset_days=i))

        rules = memory.analyze_patterns()
        ban_rules = [r for r in rules if r.action == "BLOCK" and "NATURALGAS" in r.pattern]
        assert len(ban_rules) >= 1

    def test_detects_adx_zone_weakness(self, memory):
        for i in range(6):
            memory.record_trade(_make_trade(pnl=-300, adx=23, entry_offset_days=i))
        for i in range(6):
            memory.record_trade(_make_trade(pnl=1500, adx=35, entry_offset_days=i))

        rules = memory.analyze_patterns()
        adx_rules = [r for r in rules if "ADX" in r.pattern and "weak" in r.pattern]
        assert len(adx_rules) >= 1


class TestSelfHealing:
    def test_healthy_with_no_trades(self, memory):
        health = memory.get_health_report()
        assert health["status"] == "NO_DATA"

    def test_healthy_with_good_trades(self, memory):
        for i in range(10):
            memory.record_trade(_make_trade(pnl=1000, entry_offset_days=i))
        health = memory.get_health_report()
        assert health["status"] == "HEALTHY"
        assert health["overall_win_rate"] == 1.0

    def test_degraded_on_low_win_rate(self, memory):
        for i in range(8):
            memory.record_trade(_make_trade(pnl=-500, entry_offset_days=i))
        for i in range(2):
            memory.record_trade(_make_trade(pnl=1000, entry_offset_days=i + 8))
        health = memory.get_health_report()
        assert health["status"] in ("DEGRADED", "CRITICAL")

    def test_critical_on_loss_streak(self, memory):
        for i in range(6):
            memory.record_trade(_make_trade(pnl=-1000, entry_offset_days=i))
        health = memory.get_health_report()
        assert health["consecutive_losses"] >= 5
        assert health["status"] == "CRITICAL"

    def test_recommended_threshold_increases(self, memory):
        for i in range(6):
            memory.record_trade(_make_trade(pnl=-1000, entry_offset_days=i))
        health = memory.get_health_report()
        assert health["recommended_score_threshold"] >= 70

    def test_position_size_modifier(self, memory):
        assert memory.get_position_size_modifier("NIFTY24500CE") == 1.0


class TestInsights:
    def test_no_data_insight(self, memory):
        insights = memory.get_insights()
        assert len(insights) == 1
        assert insights[0]["type"] == "info"

    def test_insights_with_data(self, memory):
        for i in range(6):
            memory.record_trade(_make_trade(pnl=1000, entry_hour=10, entry_offset_days=i))
        for i in range(6):
            memory.record_trade(_make_trade(pnl=-500, entry_hour=14, entry_offset_days=i))
        memory.analyze_patterns()
        insights = memory.get_insights()
        assert len(insights) > 0

    def test_lesson_appears_in_insights(self, memory):
        memory.record_trade(_make_trade(pnl=1000))
        memory.teach("Avoid Monday mornings", {"entry_day": 0})
        insights = memory.get_insights()
        lesson_insights = [i for i in insights if i["type"] == "lesson"]
        assert len(lesson_insights) == 1


class TestConditionMatching:
    def test_symbol_contains_match(self, memory):
        memory.teach("test", {"symbol_contains": "GOLD"}, score_modifier=-10)
        adj, _ = memory.get_score_adjustment("GOLD25DEC", "BUY_CE")
        assert adj == -10.0

    def test_adx_range_match(self, memory):
        memory.teach("test", {"adx_range": [20, 25]}, score_modifier=-10)
        adj, _ = memory.get_score_adjustment("NIFTY", "BUY_CE", adx_value=22)
        assert adj == -10.0

    def test_adx_range_no_match(self, memory):
        memory.teach("test", {"adx_range": [20, 25]}, score_modifier=-10)
        adj, _ = memory.get_score_adjustment("NIFTY", "BUY_CE", adx_value=30)
        assert adj == 0.0

    def test_combined_conditions(self, memory):
        memory.teach("test", {"symbol_contains": "NIFTY", "entry_hour": 10}, score_modifier=-10)
        adj1, _ = memory.get_score_adjustment("NIFTY24500CE", "BUY_CE", entry_hour=10)
        assert adj1 == -10.0
        adj2, _ = memory.get_score_adjustment("NIFTY24500CE", "BUY_CE", entry_hour=11)
        assert adj2 == 0.0

    def test_supertrend_condition(self, memory):
        memory.teach("test", {"supertrend_dir": "BEAR"}, score_modifier=-10)
        adj, _ = memory.get_score_adjustment("NIFTY", "BUY_CE", supertrend_dir="BEAR")
        assert adj == -10.0
        adj2, _ = memory.get_score_adjustment("NIFTY", "BUY_CE", supertrend_dir="BULL")
        assert adj2 == 0.0
