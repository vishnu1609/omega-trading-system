"""
Trade Memory — Self-Learning & Self-Healing Engine.

Records every trade with full market context, mines patterns in losses,
auto-adjusts signal scoring, and accepts user-taught scenarios.

How it learns:
  1. Every closed trade is logged with indicators, regime, timing
  2. Pattern detector finds statistically significant loss clusters
  3. Adaptive rules modify signal scores or block signals
  4. User can teach specific scenarios (avoid/prefer)
  5. Rules decay over time — recent data weighted heavier

Self-healing:
  - Auto-raises score threshold after loss streaks
  - Auto-reduces position sizing on cold symbols
  - Auto-extends cooldown after clustered losses
  - Temporary symbol bans after consecutive losses
  - Detects when a parameter stops working
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

JOURNAL_FILE = "data/trade_journal.json"
LESSONS_FILE = "data/lessons.json"
RULES_FILE = "data/adaptive_rules.json"

MIN_SAMPLE_SIZE = 5
DECAY_HALF_LIFE_DAYS = 30
LOSS_STREAK_BAN_THRESHOLD = 5
WIN_RATE_WARN_THRESHOLD = 0.40
WIN_RATE_CRITICAL_THRESHOLD = 0.30


@dataclass
class TradeContext:
    """Full context snapshot of a trade for pattern mining."""
    # Trade identifiers
    symbol: str
    direction: str          # LONG / SHORT
    option_type: str        # CE / PE / ""
    category: str           # INDICES / MCX / INDEX_FUTURES

    # Prices
    entry_price: float
    exit_price: float
    sl_price: float
    pnl: float
    pnl_pct: float

    # Timing
    entry_time: str         # ISO format
    exit_time: str
    entry_hour: int         # 0-23
    entry_day: int          # 0=Mon, 6=Sun
    hold_minutes: float

    # Exit context
    exit_reason: str        # TP, TRAIL_SL, SL_HIT, SL_SKIP_EMERGENCY, SQUARE_OFF

    # Indicators at entry (captured from signal)
    signal_score: float
    signal_confidence: str  # HIGH / MEDIUM / LOW
    adx_value: float
    vwap_position: str      # ABOVE / BELOW
    supertrend_dir: str     # BULL / BEAR
    volume_ratio: float     # current_vol / avg_vol
    atr_value: float

    # Outcome
    is_win: bool
    r_multiple: float       # pnl / risk (entry-SL distance)

    # Metadata
    strategy: str = "omega_complete"
    id: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = f"{self.symbol}_{self.entry_time}".replace(":", "").replace("-", "")


@dataclass
class Lesson:
    """User-taught scenario."""
    id: str
    rule: str               # Human-readable description
    condition: dict         # Machine-evaluable condition
    action: str             # "BLOCK", "PENALIZE", "BOOST"
    score_modifier: float   # -30 to +30
    taught_at: str
    taught_by: str          # "user" or "system"
    active: bool = True
    hit_count: int = 0
    effectiveness: float = 0.0  # tracked over time


@dataclass
class AdaptiveRule:
    """Auto-generated rule from pattern detection."""
    id: str
    pattern: str            # Human-readable pattern description
    condition: dict         # Machine-evaluable
    action: str             # "PENALIZE", "BOOST", "BLOCK", "REDUCE_SIZE"
    score_modifier: float
    confidence: float       # 0-1, based on sample size and significance
    sample_size: int
    win_rate: float         # win rate in this pattern
    baseline_win_rate: float
    created_at: str
    last_evaluated: str
    active: bool = True
    hit_count: int = 0


class TradeMemory:
    """Self-learning trade memory system."""

    def __init__(self, base_path: str = "."):
        self._base = Path(base_path)
        self._journal_path = self._base / JOURNAL_FILE
        self._lessons_path = self._base / LESSONS_FILE
        self._rules_path = self._base / RULES_FILE

        self._trades: list[TradeContext] = []
        self._lessons: list[Lesson] = []
        self._adaptive_rules: list[AdaptiveRule] = []

        self._load()

    def _load(self) -> None:
        self._trades = self._load_json(self._journal_path, self._parse_trade)
        self._lessons = self._load_json(self._lessons_path, self._parse_lesson)
        self._adaptive_rules = self._load_json(self._rules_path, self._parse_rule)
        log.info("TradeMemory loaded: %d trades, %d lessons, %d rules",
                 len(self._trades), len(self._lessons), len(self._adaptive_rules))

    def _load_json(self, path: Path, parser) -> list:
        if not path.exists():
            return []
        try:
            with open(path) as f:
                data = json.load(f)
            return [parser(item) for item in data]
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to load %s: %s", path, e)
            return []

    def _parse_trade(self, d: dict) -> TradeContext:
        return TradeContext(**{k: d[k] for k in TradeContext.__dataclass_fields__ if k in d})

    def _parse_lesson(self, d: dict) -> Lesson:
        return Lesson(**{k: d[k] for k in Lesson.__dataclass_fields__ if k in d})

    def _parse_rule(self, d: dict) -> AdaptiveRule:
        return AdaptiveRule(**{k: d[k] for k in AdaptiveRule.__dataclass_fields__ if k in d})

    def _save_journal(self) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._journal_path, "w") as f:
            json.dump([asdict(t) for t in self._trades], f, indent=2, default=str)

    def _save_lessons(self) -> None:
        self._lessons_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lessons_path, "w") as f:
            json.dump([asdict(l) for l in self._lessons], f, indent=2, default=str)

    def _save_rules(self) -> None:
        self._rules_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._rules_path, "w") as f:
            json.dump([asdict(r) for r in self._adaptive_rules], f, indent=2, default=str)

    # ── Recording ──────────────────────────────────────────────────

    def record_trade(self, trade_ctx: TradeContext) -> None:
        self._trades.append(trade_ctx)
        self._save_journal()
        log.info("Trade recorded: %s %s P&L=₹%.2f (%s)",
                 trade_ctx.symbol, "WIN" if trade_ctx.is_win else "LOSS",
                 trade_ctx.pnl, trade_ctx.exit_reason)

    def build_trade_context(
        self,
        symbol: str, direction: str, option_type: str,
        entry_price: float, exit_price: float, sl_price: float,
        pnl: float, entry_time: datetime, exit_time: datetime,
        exit_reason: str,
        signal_score: float = 0, signal_confidence: str = "LOW",
        adx_value: float = 0, vwap_position: str = "UNKNOWN",
        supertrend_dir: str = "UNKNOWN", volume_ratio: float = 1.0,
        atr_value: float = 0, strategy: str = "omega_complete",
    ) -> TradeContext:
        risk = abs(entry_price - sl_price) * 1 if sl_price > 0 else 1
        category = _categorize(symbol)
        hold_mins = (exit_time - entry_time).total_seconds() / 60 if exit_time > entry_time else 0
        pnl_pct = (pnl / (entry_price * 1)) * 100 if entry_price > 0 else 0

        return TradeContext(
            symbol=symbol, direction=direction, option_type=option_type,
            category=category,
            entry_price=entry_price, exit_price=exit_price, sl_price=sl_price,
            pnl=pnl, pnl_pct=pnl_pct,
            entry_time=entry_time.isoformat(), exit_time=exit_time.isoformat(),
            entry_hour=entry_time.hour, entry_day=entry_time.weekday(),
            hold_minutes=hold_mins, exit_reason=exit_reason,
            signal_score=signal_score, signal_confidence=signal_confidence,
            adx_value=adx_value, vwap_position=vwap_position,
            supertrend_dir=supertrend_dir, volume_ratio=volume_ratio,
            atr_value=atr_value,
            is_win=pnl > 0,
            r_multiple=pnl / risk if risk > 0 else 0,
            strategy=strategy,
        )

    # ── User-taught lessons ────────────────────────────────────────

    def teach(self, rule: str, condition: dict, action: str = "PENALIZE",
              score_modifier: float = -15.0) -> Lesson:
        lesson_id = f"lesson_{len(self._lessons) + 1}_{int(datetime.now(IST).timestamp())}"
        lesson = Lesson(
            id=lesson_id, rule=rule, condition=condition,
            action=action, score_modifier=score_modifier,
            taught_at=datetime.now(IST).isoformat(),
            taught_by="user",
        )
        self._lessons.append(lesson)
        self._save_lessons()
        log.info("Lesson learned: %s → %s (modifier=%.1f)", rule, action, score_modifier)
        return lesson

    def teach_simple(self, description: str, symbol: str = "",
                     hour: int = -1, day: int = -1,
                     action: str = "PENALIZE",
                     score_modifier: float = -15.0) -> Lesson:
        condition = {}
        if symbol:
            condition["symbol"] = symbol.upper()
        if hour >= 0:
            condition["entry_hour"] = hour
        if day >= 0:
            condition["entry_day"] = day
        return self.teach(description, condition, action, score_modifier)

    def forget_lesson(self, lesson_id: str) -> bool:
        for lesson in self._lessons:
            if lesson.id == lesson_id:
                lesson.active = False
                self._save_lessons()
                log.info("Lesson deactivated: %s", lesson.rule)
                return True
        return False

    # ── Pattern Detection ──────────────────────────────────────────

    def analyze_patterns(self) -> list[AdaptiveRule]:
        if len(self._trades) < MIN_SAMPLE_SIZE:
            log.info("Not enough trades (%d) for pattern analysis", len(self._trades))
            return []

        now = datetime.now(IST)
        baseline_wr = self._weighted_win_rate(self._trades, now)
        new_rules: list[AdaptiveRule] = []

        new_rules.extend(self._detect_hour_patterns(baseline_wr, now))
        new_rules.extend(self._detect_symbol_patterns(baseline_wr, now))
        new_rules.extend(self._detect_adx_zone_patterns(baseline_wr, now))
        new_rules.extend(self._detect_confidence_patterns(baseline_wr, now))
        new_rules.extend(self._detect_exit_reason_patterns(baseline_wr, now))
        new_rules.extend(self._detect_day_patterns(baseline_wr, now))
        new_rules.extend(self._detect_regime_patterns(baseline_wr, now))

        self._adaptive_rules = [r for r in self._adaptive_rules if r.active]
        for new_rule in new_rules:
            existing = next((r for r in self._adaptive_rules if r.id == new_rule.id), None)
            if existing:
                existing.win_rate = new_rule.win_rate
                existing.sample_size = new_rule.sample_size
                existing.confidence = new_rule.confidence
                existing.score_modifier = new_rule.score_modifier
                existing.last_evaluated = new_rule.last_evaluated
            else:
                self._adaptive_rules.append(new_rule)

        self._save_rules()
        log.info("Pattern analysis complete: %d active rules", len(self._adaptive_rules))
        return new_rules

    def _detect_hour_patterns(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        rules = []
        for hour in range(9, 16):
            subset = [t for t in self._trades if t.entry_hour == hour]
            rule = self._build_rule_if_significant(
                subset, baseline_wr, now,
                rule_id=f"hour_{hour}",
                pattern=f"Trades entered at {hour}:00",
                condition={"entry_hour": hour},
            )
            if rule:
                rules.append(rule)
        return rules

    def _detect_symbol_patterns(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        rules = []
        symbols = set(t.symbol for t in self._trades)
        base_symbols = set()
        for s in symbols:
            for base in ["NIFTY", "BANKNIFTY", "SENSEX", "GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"]:
                if base in s.upper():
                    base_symbols.add(base)
                    break

        for sym in base_symbols:
            subset = [t for t in self._trades if sym in t.symbol.upper()]
            rule = self._build_rule_if_significant(
                subset, baseline_wr, now,
                rule_id=f"symbol_{sym.lower()}",
                pattern=f"{sym} trades",
                condition={"symbol_contains": sym},
            )
            if rule:
                rules.append(rule)

            streak = self._consecutive_losses(subset)
            if streak >= LOSS_STREAK_BAN_THRESHOLD:
                ban_rule = AdaptiveRule(
                    id=f"ban_{sym.lower()}",
                    pattern=f"{sym}: {streak} consecutive losses — temporary ban",
                    condition={"symbol_contains": sym},
                    action="BLOCK",
                    score_modifier=-100,
                    confidence=0.9,
                    sample_size=streak,
                    win_rate=0.0,
                    baseline_win_rate=baseline_wr,
                    created_at=now.isoformat(),
                    last_evaluated=now.isoformat(),
                )
                rules.append(ban_rule)

        return rules

    def _detect_adx_zone_patterns(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        rules = []
        zones = [(20, 25, "weak_trend"), (25, 30, "moderate"), (30, 40, "strong"), (40, 100, "very_strong")]
        for low, high, label in zones:
            subset = [t for t in self._trades if low <= t.adx_value < high]
            rule = self._build_rule_if_significant(
                subset, baseline_wr, now,
                rule_id=f"adx_{label}",
                pattern=f"ADX {low}-{high} ({label})",
                condition={"adx_range": [low, high]},
            )
            if rule:
                rules.append(rule)
        return rules

    def _detect_confidence_patterns(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        rules = []
        for conf in ["HIGH", "MEDIUM", "LOW"]:
            subset = [t for t in self._trades if t.signal_confidence == conf]
            rule = self._build_rule_if_significant(
                subset, baseline_wr, now,
                rule_id=f"confidence_{conf.lower()}",
                pattern=f"{conf} confidence signals",
                condition={"signal_confidence": conf},
            )
            if rule:
                rules.append(rule)
        return rules

    def _detect_exit_reason_patterns(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        rules = []
        sl_skips = [t for t in self._trades if "SKIP" in t.exit_reason]
        if len(sl_skips) >= 3:
            skip_rate = len(sl_skips) / len(self._trades) if self._trades else 0
            if skip_rate > 0.1:
                rules.append(AdaptiveRule(
                    id="sl_skip_rate_high",
                    pattern=f"SL-skip rate {skip_rate:.0%} (>{10}%) — tighten SL or reduce size",
                    condition={"sl_skip_rate_above": 0.1},
                    action="REDUCE_SIZE",
                    score_modifier=-10,
                    confidence=min(skip_rate * 5, 1.0),
                    sample_size=len(sl_skips),
                    win_rate=0.0,
                    baseline_win_rate=baseline_wr,
                    created_at=datetime.now(IST).isoformat(),
                    last_evaluated=datetime.now(IST).isoformat(),
                ))
        return rules

    def _detect_day_patterns(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        rules = []
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        for day_idx, day_name in enumerate(day_names):
            subset = [t for t in self._trades if t.entry_day == day_idx]
            rule = self._build_rule_if_significant(
                subset, baseline_wr, now,
                rule_id=f"day_{day_name.lower()}",
                pattern=f"{day_name} trades",
                condition={"entry_day": day_idx},
            )
            if rule:
                rules.append(rule)
        return rules

    def _detect_regime_patterns(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        rules = []
        for regime in ["BULL", "BEAR"]:
            subset = [t for t in self._trades if t.supertrend_dir == regime]
            rule = self._build_rule_if_significant(
                subset, baseline_wr, now,
                rule_id=f"regime_{regime.lower()}",
                pattern=f"SuperTrend {regime} regime",
                condition={"supertrend_dir": regime},
            )
            if rule:
                rules.append(rule)

        for vwap_pos in ["ABOVE", "BELOW"]:
            subset = [t for t in self._trades if t.vwap_position == vwap_pos]
            rule = self._build_rule_if_significant(
                subset, baseline_wr, now,
                rule_id=f"vwap_{vwap_pos.lower()}",
                pattern=f"Price {vwap_pos} VWAP",
                condition={"vwap_position": vwap_pos},
            )
            if rule:
                rules.append(rule)
        return rules

    def _build_rule_if_significant(
        self, subset: list[TradeContext], baseline_wr: float,
        now: datetime, rule_id: str, pattern: str, condition: dict,
    ) -> AdaptiveRule | None:
        if len(subset) < MIN_SAMPLE_SIZE:
            return None

        wr = self._weighted_win_rate(subset, now)
        diff = wr - baseline_wr

        if abs(diff) < 0.10:
            return None

        confidence = min(len(subset) / 20, 1.0) * min(abs(diff) / 0.20, 1.0)

        if diff < 0:
            action = "PENALIZE"
            modifier = max(diff * 50, -30)
        else:
            action = "BOOST"
            modifier = min(diff * 40, 25)

        return AdaptiveRule(
            id=rule_id, pattern=pattern, condition=condition,
            action=action, score_modifier=modifier,
            confidence=confidence, sample_size=len(subset),
            win_rate=wr, baseline_win_rate=baseline_wr,
            created_at=now.isoformat(), last_evaluated=now.isoformat(),
        )

    # ── Score Adjustment (called during signal scoring) ────────────

    def get_score_adjustment(self, symbol: str, side: str,
                             entry_hour: int = -1, entry_day: int = -1,
                             adx_value: float = 0, signal_confidence: str = "",
                             supertrend_dir: str = "", vwap_position: str = "",
                             ) -> tuple[float, list[str]]:
        total_adjustment = 0.0
        reasons: list[str] = []

        for lesson in self._lessons:
            if not lesson.active:
                continue
            if self._matches_condition(lesson.condition, symbol=symbol, side=side,
                                       entry_hour=entry_hour, entry_day=entry_day,
                                       adx_value=adx_value, signal_confidence=signal_confidence,
                                       supertrend_dir=supertrend_dir, vwap_position=vwap_position):
                total_adjustment += lesson.score_modifier
                reasons.append(f"[LESSON] {lesson.rule}: {lesson.score_modifier:+.0f}")
                lesson.hit_count += 1

        for rule in self._adaptive_rules:
            if not rule.active:
                continue
            if self._matches_condition(rule.condition, symbol=symbol, side=side,
                                       entry_hour=entry_hour, entry_day=entry_day,
                                       adx_value=adx_value, signal_confidence=signal_confidence,
                                       supertrend_dir=supertrend_dir, vwap_position=vwap_position):
                weighted_mod = rule.score_modifier * rule.confidence
                total_adjustment += weighted_mod
                reasons.append(f"[AUTO] {rule.pattern} (WR={rule.win_rate:.0%}): {weighted_mod:+.1f}")
                rule.hit_count += 1

        if reasons:
            self._save_lessons()
            self._save_rules()

        return total_adjustment, reasons

    def should_block(self, symbol: str, side: str, **kwargs) -> tuple[bool, str]:
        for lesson in self._lessons:
            if not lesson.active or lesson.action != "BLOCK":
                continue
            if self._matches_condition(lesson.condition, symbol=symbol, side=side, **kwargs):
                lesson.hit_count += 1
                self._save_lessons()
                return True, f"Blocked by lesson: {lesson.rule}"

        for rule in self._adaptive_rules:
            if not rule.active or rule.action != "BLOCK":
                continue
            if self._matches_condition(rule.condition, symbol=symbol, side=side, **kwargs):
                rule.hit_count += 1
                self._save_rules()
                return True, f"Blocked by pattern: {rule.pattern}"

        return False, ""

    def get_position_size_modifier(self, symbol: str) -> float:
        for rule in self._adaptive_rules:
            if not rule.active or rule.action != "REDUCE_SIZE":
                continue
            if self._matches_condition(rule.condition, symbol=symbol):
                return 0.5
        return 1.0

    def _matches_condition(self, condition: dict, symbol: str = "",
                           side: str = "", entry_hour: int = -1,
                           entry_day: int = -1, adx_value: float = 0,
                           signal_confidence: str = "",
                           supertrend_dir: str = "",
                           vwap_position: str = "") -> bool:
        for key, value in condition.items():
            if key == "symbol" and value.upper() not in symbol.upper():
                return False
            if key == "symbol_contains" and value.upper() not in symbol.upper():
                return False
            if key == "side" and value != side:
                return False
            if key == "entry_hour" and value != entry_hour:
                return False
            if key == "entry_day" and value != entry_day:
                return False
            if key == "adx_range":
                if not (value[0] <= adx_value < value[1]):
                    return False
            if key == "signal_confidence" and value != signal_confidence:
                return False
            if key == "supertrend_dir" and value != supertrend_dir:
                return False
            if key == "vwap_position" and value != vwap_position:
                return False
        return True

    # ── Self-Healing ───────────────────────────────────────────────

    def get_health_report(self) -> dict:
        if not self._trades:
            return {"status": "NO_DATA", "trades": 0}

        now = datetime.now(IST)
        recent = [t for t in self._trades
                  if (now - datetime.fromisoformat(t.exit_time)).days <= 7]

        total_wr = self._simple_win_rate(self._trades)
        recent_wr = self._simple_win_rate(recent) if recent else total_wr
        streak = self._consecutive_losses(self._trades)

        status = "HEALTHY"
        alerts = []

        if recent_wr < WIN_RATE_CRITICAL_THRESHOLD and len(recent) >= MIN_SAMPLE_SIZE:
            status = "CRITICAL"
            alerts.append(f"7-day win rate {recent_wr:.0%} is critically low")
        elif recent_wr < WIN_RATE_WARN_THRESHOLD and len(recent) >= MIN_SAMPLE_SIZE:
            status = "DEGRADED"
            alerts.append(f"7-day win rate {recent_wr:.0%} below threshold")

        if streak >= LOSS_STREAK_BAN_THRESHOLD:
            status = "CRITICAL"
            alerts.append(f"{streak} consecutive losses — system may need parameter review")
        elif streak >= 3:
            if status == "HEALTHY":
                status = "DEGRADED"
            alerts.append(f"{streak} consecutive losses — monitoring")

        active_bans = [r for r in self._adaptive_rules if r.action == "BLOCK" and r.active]
        if active_bans:
            alerts.append(f"{len(active_bans)} symbol(s) temporarily banned")

        avg_r = self._avg_r_multiple()

        return {
            "status": status,
            "trades": len(self._trades),
            "overall_win_rate": total_wr,
            "recent_win_rate": recent_wr,
            "recent_trades": len(recent),
            "consecutive_losses": streak,
            "avg_r_multiple": avg_r,
            "active_lessons": sum(1 for l in self._lessons if l.active),
            "active_rules": sum(1 for r in self._adaptive_rules if r.active),
            "active_bans": len(active_bans),
            "alerts": alerts,
            "recommended_score_threshold": self._recommended_threshold(total_wr, streak),
        }

    def _recommended_threshold(self, win_rate: float, streak: int) -> float:
        base = 55.0
        if win_rate < WIN_RATE_CRITICAL_THRESHOLD:
            base = 75.0
        elif win_rate < WIN_RATE_WARN_THRESHOLD:
            base = 65.0
        if streak >= 5:
            base = max(base, 70.0)
        elif streak >= 3:
            base += 5
        return base

    # ── Insights (for dashboard) ───────────────────────────────────

    def get_insights(self) -> list[dict]:
        insights = []

        if not self._trades:
            return [{"type": "info", "message": "No trades recorded yet — learning starts after first closed trade"}]

        health = self.get_health_report()

        if health["status"] == "CRITICAL":
            for alert in health["alerts"]:
                insights.append({"type": "critical", "message": alert})

        elif health["status"] == "DEGRADED":
            for alert in health["alerts"]:
                insights.append({"type": "warning", "message": alert})

        best_hour = self._best_performing("entry_hour")
        worst_hour = self._worst_performing("entry_hour")
        if best_hour is not None:
            insights.append({"type": "insight",
                             "message": f"Best hour: {best_hour['value']}:00 "
                                        f"({best_hour['win_rate']:.0%} WR, {best_hour['count']} trades)"})
        if worst_hour is not None:
            insights.append({"type": "insight",
                             "message": f"Worst hour: {worst_hour['value']}:00 "
                                        f"({worst_hour['win_rate']:.0%} WR, {worst_hour['count']} trades)"})

        best_sym = self._best_performing("category")
        worst_sym = self._worst_performing("category")
        if best_sym is not None:
            insights.append({"type": "insight",
                             "message": f"Best category: {best_sym['value']} "
                                        f"({best_sym['win_rate']:.0%} WR)"})
        if worst_sym is not None:
            insights.append({"type": "insight",
                             "message": f"Worst category: {worst_sym['value']} "
                                        f"({worst_sym['win_rate']:.0%} WR)"})

        for lesson in self._lessons:
            if lesson.active:
                insights.append({"type": "lesson",
                                 "message": f"User lesson: {lesson.rule} "
                                            f"(applied {lesson.hit_count}x)"})

        for rule in self._adaptive_rules:
            if rule.active and rule.confidence > 0.5:
                insights.append({"type": "rule",
                                 "message": f"Learned: {rule.pattern} → WR={rule.win_rate:.0%} "
                                            f"vs baseline {rule.baseline_win_rate:.0%} "
                                            f"({rule.score_modifier:+.0f} modifier)"})

        return insights

    def _best_performing(self, field: str) -> dict | None:
        return self._extreme_performing(field, best=True)

    def _worst_performing(self, field: str) -> dict | None:
        return self._extreme_performing(field, best=False)

    def _extreme_performing(self, field: str, best: bool) -> dict | None:
        groups: dict[Any, list[TradeContext]] = {}
        for t in self._trades:
            val = getattr(t, field, None)
            if val is None:
                continue
            groups.setdefault(val, []).append(t)

        valid = {k: v for k, v in groups.items() if len(v) >= MIN_SAMPLE_SIZE}
        if not valid:
            return None

        if best:
            key = max(valid, key=lambda k: self._simple_win_rate(valid[k]))
        else:
            key = min(valid, key=lambda k: self._simple_win_rate(valid[k]))

        return {
            "value": key,
            "win_rate": self._simple_win_rate(valid[key]),
            "count": len(valid[key]),
            "avg_pnl": sum(t.pnl for t in valid[key]) / len(valid[key]),
        }

    # ── Utilities ──────────────────────────────────────────────────

    def _weighted_win_rate(self, trades: list[TradeContext], now: datetime) -> float:
        if not trades:
            return 0.0
        total_weight = 0.0
        weighted_wins = 0.0
        for t in trades:
            try:
                age_days = (now - datetime.fromisoformat(t.exit_time)).days
            except (ValueError, TypeError):
                age_days = 0
            weight = math.exp(-0.693 * age_days / DECAY_HALF_LIFE_DAYS)
            total_weight += weight
            if t.is_win:
                weighted_wins += weight
        return weighted_wins / total_weight if total_weight > 0 else 0.0

    def _simple_win_rate(self, trades: list[TradeContext]) -> float:
        if not trades:
            return 0.0
        return sum(1 for t in trades if t.is_win) / len(trades)

    def _consecutive_losses(self, trades: list[TradeContext]) -> int:
        count = 0
        for t in reversed(trades):
            if t.is_win:
                break
            count += 1
        return count

    def _avg_r_multiple(self) -> float:
        valid = [t for t in self._trades if t.r_multiple != 0]
        if not valid:
            return 0.0
        return sum(t.r_multiple for t in valid) / len(valid)

    @property
    def trade_count(self) -> int:
        return len(self._trades)

    @property
    def lessons(self) -> list[Lesson]:
        return [l for l in self._lessons if l.active]

    @property
    def active_rules(self) -> list[AdaptiveRule]:
        return [r for r in self._adaptive_rules if r.active]


def _categorize(symbol: str) -> str:
    upper = symbol.upper()
    for m in ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"]:
        if m in upper:
            return "MCX"
    if "FUT" in upper:
        return "INDEX_FUTURES"
    return "INDICES"
