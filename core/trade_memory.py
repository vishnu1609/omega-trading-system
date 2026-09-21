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

MEMORY_FILE = "data/omega_memory.json"

# Legacy files (auto-migrated on first load)
_LEGACY_JOURNAL = "data/trade_journal.json"
_LEGACY_LESSONS = "data/lessons.json"
_LEGACY_RULES = "data/adaptive_rules.json"

MIN_SAMPLE_SIZE = 5
MIN_HOUR_SAMPLE_SIZE = 20
DECAY_HALF_LIFE_DAYS = 30
Z_THRESHOLD = 1.65           # 90% confidence (one-tailed z-test)
LOSS_STREAK_BAN_THRESHOLD = 5
BAN_EXPIRY_DAYS = 7
WIN_RATE_WARN_THRESHOLD = 0.40
WIN_RATE_CRITICAL_THRESHOLD = 0.30
SAVE_BATCH_SIZE = 10          # flush hit-count updates every N evaluations
EFFECTIVENESS_MIN_TRADES = 15  # min trades after rule creation to judge it
RULE_MAX_AGE_DAYS = 90        # deactivate stale rules that haven't been re-confirmed

_BASE_SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"]
_ADX_ZONES = [(15, 20, "flat"), (20, 25, "weak"), (25, 30, "moderate"),
              (30, 40, "strong"), (40, 100, "very_strong")]


@dataclass
class TradeContext:
    symbol: str
    direction: str
    option_type: str
    category: str
    entry_price: float
    exit_price: float
    sl_price: float
    pnl: float
    pnl_pct: float
    entry_time: str
    exit_time: str
    entry_hour: int
    entry_day: int
    hold_minutes: float
    exit_reason: str
    signal_score: float
    signal_confidence: str
    adx_value: float
    vwap_position: str
    supertrend_dir: str
    volume_ratio: float
    atr_value: float
    is_win: bool
    r_multiple: float
    strategy: str = "omega_complete"
    id: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = f"{self.symbol}_{self.entry_time}".replace(":", "").replace("-", "")


@dataclass
class Lesson:
    id: str
    rule: str
    condition: dict
    action: str
    score_modifier: float
    taught_at: str
    taught_by: str
    active: bool = True
    hit_count: int = 0
    effectiveness: float = 0.0


@dataclass
class AdaptiveRule:
    id: str
    pattern: str
    condition: dict
    action: str
    score_modifier: float
    confidence: float
    sample_size: int
    win_rate: float
    baseline_win_rate: float
    created_at: str
    last_evaluated: str
    active: bool = True
    hit_count: int = 0
    correct_predictions: int = 0
    total_predictions: int = 0
    effectiveness: float = 0.0


class TradeMemory:

    def __init__(self, base_path: str = "."):
        self._base = Path(base_path)
        self._memory_path = self._base / MEMORY_FILE
        self._trades: list[TradeContext] = []
        self._lessons: list[Lesson] = []
        self._adaptive_rules: list[AdaptiveRule] = []
        self._dirty = False
        self._eval_count = 0
        self._load()

    # ── Storage (single file + migration) ─────────────────────────

    def _load(self) -> None:
        if self._memory_path.exists():
            self._load_unified()
        else:
            self._migrate_legacy()
        expired = self._apply_decay()
        log.info("TradeMemory loaded: %d trades, %d lessons, %d rules (%d expired)",
                 len(self._trades), len(self._lessons), len(self._adaptive_rules), expired)

    def _load_unified(self) -> None:
        try:
            with open(self._memory_path) as f:
                data = json.load(f)
            self._trades = [self._parse(TradeContext, d) for d in data.get("trades", [])]
            self._lessons = [self._parse(Lesson, d) for d in data.get("lessons", [])]
            self._adaptive_rules = [self._parse(AdaptiveRule, d) for d in data.get("rules", [])]
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to load %s: %s", self._memory_path, e)

    def _migrate_legacy(self) -> None:
        jp = self._base / _LEGACY_JOURNAL
        lp = self._base / _LEGACY_LESSONS
        rp = self._base / _LEGACY_RULES
        if not any(p.exists() for p in (jp, lp, rp)):
            return
        log.info("Migrating legacy 3-file format → single omega_memory.json")
        self._trades = self._load_legacy(jp, TradeContext)
        self._lessons = self._load_legacy(lp, Lesson)
        self._adaptive_rules = self._load_legacy(rp, AdaptiveRule)
        self._save()

    def _load_legacy(self, path: Path, cls) -> list:
        if not path.exists():
            return []
        try:
            with open(path) as f:
                return [self._parse(cls, d) for d in json.load(f)]
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to load legacy %s: %s", path, e)
            return []

    @staticmethod
    def _parse(cls, d: dict):
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def _save(self) -> None:
        self._memory_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._memory_path, "w") as f:
            json.dump({
                "trades": [asdict(t) for t in self._trades],
                "lessons": [asdict(l) for l in self._lessons],
                "rules": [asdict(r) for r in self._adaptive_rules],
            }, f, indent=2, default=str)
        self._dirty = False
        self._eval_count = 0

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._eval_count += 1
        if self._eval_count >= SAVE_BATCH_SIZE:
            self._save()

    def flush(self) -> None:
        if self._dirty:
            self._save()

    # ── Decay ─────────────────────────────────────────────────────

    def _apply_decay(self) -> int:
        now = datetime.now(IST)
        expired = 0
        for rule in self._adaptive_rules:
            if not rule.active:
                continue
            age = self._rule_age_days(rule, now)
            if rule.action == "BLOCK" and age >= BAN_EXPIRY_DAYS:
                rule.active = False
                expired += 1
                log.info("Ban lifted: '%s' (aged %d days)", rule.pattern, age)
            elif age >= RULE_MAX_AGE_DAYS and rule.hit_count < 3:
                rule.active = False
                expired += 1
                log.info("Stale rule pruned: '%s' (aged %d days, %d hits)",
                         rule.pattern, age, rule.hit_count)
        if expired:
            self._save()
        return expired

    @staticmethod
    def _rule_age_days(rule: AdaptiveRule, now: datetime | None = None) -> int:
        now = now or datetime.now(IST)
        try:
            created = datetime.fromisoformat(rule.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=IST)
            return (now - created).days
        except (ValueError, TypeError):
            return 0

    # ── Recording ─────────────────────────────────────────────────

    def record_trade(self, trade_ctx: TradeContext) -> None:
        self._trades.append(trade_ctx)
        self._track_rule_effectiveness(trade_ctx)
        self._save()
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
        risk = abs(entry_price - sl_price) if sl_price > 0 else 1
        hold_mins = (exit_time - entry_time).total_seconds() / 60 if exit_time > entry_time else 0
        pnl_pct = (pnl / entry_price) * 100 if entry_price > 0 else 0
        return TradeContext(
            symbol=symbol, direction=direction, option_type=option_type,
            category=_categorize(symbol),
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

    # ── User-taught lessons ───────────────────────────────────────

    def teach(self, rule: str, condition: dict, action: str = "PENALIZE",
              score_modifier: float = -15.0) -> Lesson:
        lesson_id = f"lesson_{len(self._lessons) + 1}_{int(datetime.now(IST).timestamp())}"
        lesson = Lesson(
            id=lesson_id, rule=rule, condition=condition,
            action=action, score_modifier=score_modifier,
            taught_at=datetime.now(IST).isoformat(), taught_by="user",
        )
        self._lessons.append(lesson)
        self._save()
        log.info("Lesson learned: %s → %s (modifier=%.1f)", rule, action, score_modifier)
        return lesson

    def teach_simple(self, description: str, symbol: str = "",
                     hour: int = -1, day: int = -1,
                     action: str = "PENALIZE",
                     score_modifier: float = -15.0) -> Lesson:
        condition: dict[str, Any] = {}
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
                self._save()
                log.info("Lesson deactivated: %s", lesson.rule)
                return True
        return False

    # ── Pattern Detection (single-pass + z-test + multi-factor) ──

    def analyze_patterns(self) -> list[AdaptiveRule]:
        if len(self._trades) < MIN_SAMPLE_SIZE:
            return []

        now = datetime.now(IST)
        baseline_wr = self._weighted_win_rate(self._trades, now)
        groups = self._group_trades()

        new_rules: list[AdaptiveRule] = []

        min_sizes = {"entry_hour": MIN_HOUR_SAMPLE_SIZE}
        for dimension, buckets in groups.items():
            min_n = min_sizes.get(dimension, MIN_SAMPLE_SIZE)
            for value, subset in buckets.items():
                if len(subset) < min_n:
                    continue
                label = self._pattern_label(dimension, value)
                rule = self._test_significance(
                    subset, baseline_wr, now,
                    rule_id=f"{dimension}_{value}",
                    pattern=label,
                    condition={dimension: value},
                )
                if rule:
                    new_rules.append(rule)

        new_rules.extend(self._detect_multi_factor(baseline_wr, now))
        new_rules.extend(self._detect_loss_streaks(baseline_wr, now))
        new_rules.extend(self._detect_sl_skip_rate(baseline_wr, now))

        self._evaluate_effectiveness()
        self._merge_rules(new_rules)
        self._save()
        log.info("Pattern analysis: %d active rules", sum(1 for r in self._adaptive_rules if r.active))
        return new_rules

    def _group_trades(self) -> dict[str, dict[Any, list[TradeContext]]]:
        groups: dict[str, dict[Any, list]] = {
            "entry_hour": {}, "entry_day": {}, "symbol_base": {},
            "adx_zone": {}, "signal_confidence": {},
            "supertrend_dir": {}, "vwap_position": {},
        }
        for t in self._trades:
            groups["entry_hour"].setdefault(t.entry_hour, []).append(t)
            groups["entry_day"].setdefault(t.entry_day, []).append(t)
            base = _extract_base(t.symbol)
            if base:
                groups["symbol_base"].setdefault(base, []).append(t)
            zone = _adx_zone_label(t.adx_value)
            if zone:
                groups["adx_zone"].setdefault(zone, []).append(t)
            if t.signal_confidence:
                groups["signal_confidence"].setdefault(t.signal_confidence, []).append(t)
            if t.supertrend_dir:
                groups["supertrend_dir"].setdefault(t.supertrend_dir, []).append(t)
            if t.vwap_position:
                groups["vwap_position"].setdefault(t.vwap_position, []).append(t)
        return groups

    def _detect_multi_factor(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        combos: dict[str, list[TradeContext]] = {}
        for t in self._trades:
            base = _extract_base(t.symbol)
            if base:
                key = f"{base}_h{t.entry_hour}"
                combos.setdefault(key, []).append(t)
            if t.supertrend_dir:
                zone = _adx_zone_label(t.adx_value)
                if zone:
                    key2 = f"{t.supertrend_dir}_{zone}"
                    combos.setdefault(key2, []).append(t)

        rules = []
        for key, subset in combos.items():
            if len(subset) < MIN_SAMPLE_SIZE:
                continue
            rule = self._test_significance(
                subset, baseline_wr, now,
                rule_id=f"combo_{key}",
                pattern=f"Multi-factor: {key}",
                condition={"multi_factor": key},
            )
            if rule:
                rules.append(rule)
        return rules

    def _detect_loss_streaks(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        rules = []
        for base in _BASE_SYMBOLS:
            subset = [t for t in self._trades if base in t.symbol.upper()]
            streak = self._consecutive_losses(subset)
            if streak >= LOSS_STREAK_BAN_THRESHOLD:
                rules.append(AdaptiveRule(
                    id=f"ban_{base.lower()}", pattern=f"{base}: {streak} consecutive losses — ban",
                    condition={"symbol_contains": base}, action="BLOCK", score_modifier=-100,
                    confidence=0.9, sample_size=streak, win_rate=0.0,
                    baseline_win_rate=baseline_wr,
                    created_at=now.isoformat(), last_evaluated=now.isoformat(),
                ))
        return rules

    def _detect_sl_skip_rate(self, baseline_wr: float, now: datetime) -> list[AdaptiveRule]:
        skips = [t for t in self._trades if "SKIP" in t.exit_reason]
        if len(skips) < 3 or not self._trades:
            return []
        rate = len(skips) / len(self._trades)
        if rate <= 0.10:
            return []
        return [AdaptiveRule(
            id="sl_skip_rate_high",
            pattern=f"SL-skip rate {rate:.0%} — reduce size",
            condition={"sl_skip_rate_above": 0.1}, action="REDUCE_SIZE",
            score_modifier=-10, confidence=min(rate * 5, 1.0),
            sample_size=len(skips), win_rate=0.0, baseline_win_rate=baseline_wr,
            created_at=now.isoformat(), last_evaluated=now.isoformat(),
        )]

    def _test_significance(
        self, subset: list[TradeContext], baseline_wr: float,
        now: datetime, rule_id: str, pattern: str, condition: dict,
    ) -> AdaptiveRule | None:
        n = len(subset)
        wr = self._weighted_win_rate(subset, now)
        z = _z_test_proportions(wr, n, baseline_wr, len(self._trades))

        if abs(z) < Z_THRESHOLD:
            return None

        confidence = min(abs(z) / 3.0, 1.0) * min(n / 30, 1.0)
        diff = wr - baseline_wr

        if diff < 0:
            action = "PENALIZE"
            modifier = max(diff * 50, -30)
        else:
            action = "BOOST"
            modifier = min(diff * 40, 25)

        return AdaptiveRule(
            id=rule_id, pattern=pattern, condition=condition,
            action=action, score_modifier=modifier,
            confidence=confidence, sample_size=n,
            win_rate=wr, baseline_win_rate=baseline_wr,
            created_at=now.isoformat(), last_evaluated=now.isoformat(),
        )

    def _merge_rules(self, new_rules: list[AdaptiveRule]) -> None:
        self._adaptive_rules = [r for r in self._adaptive_rules if r.active]
        for new in new_rules:
            existing = next((r for r in self._adaptive_rules if r.id == new.id), None)
            if existing:
                existing.win_rate = new.win_rate
                existing.sample_size = new.sample_size
                existing.confidence = new.confidence
                existing.score_modifier = new.score_modifier
                existing.last_evaluated = new.last_evaluated
            else:
                self._adaptive_rules.append(new)

    # ── Rule Effectiveness Tracking ──────────────────────────────

    def _track_rule_effectiveness(self, trade: TradeContext) -> None:
        for rule in self._adaptive_rules:
            if not rule.active:
                continue
            if self._matches_condition(rule.condition, symbol=trade.symbol,
                                       side=trade.direction,
                                       entry_hour=trade.entry_hour,
                                       entry_day=trade.entry_day,
                                       adx_value=trade.adx_value,
                                       signal_confidence=trade.signal_confidence,
                                       supertrend_dir=trade.supertrend_dir,
                                       vwap_position=trade.vwap_position):
                rule.total_predictions += 1
                predicted_bad = rule.action in ("PENALIZE", "BLOCK", "REDUCE_SIZE")
                actual_bad = not trade.is_win
                if predicted_bad == actual_bad:
                    rule.correct_predictions += 1

    def _evaluate_effectiveness(self) -> None:
        for rule in self._adaptive_rules:
            if not rule.active or rule.total_predictions < EFFECTIVENESS_MIN_TRADES:
                continue
            rule.effectiveness = rule.correct_predictions / rule.total_predictions
            if rule.effectiveness < 0.45 and rule.action != "BLOCK":
                rule.active = False
                log.info("Rule deactivated — low effectiveness %.0f%%: %s",
                         rule.effectiveness * 100, rule.pattern)

    # ── Score Adjustment ─────────────────────────────────────────

    def get_score_adjustment(self, symbol: str, side: str,
                             entry_hour: int = -1, entry_day: int = -1,
                             adx_value: float = 0, signal_confidence: str = "",
                             supertrend_dir: str = "", vwap_position: str = "",
                             ) -> tuple[float, list[str]]:
        total = 0.0
        reasons: list[str] = []
        now = datetime.now(IST)

        for lesson in self._lessons:
            if not lesson.active:
                continue
            if self._matches_condition(lesson.condition, symbol=symbol, side=side,
                                       entry_hour=entry_hour, entry_day=entry_day,
                                       adx_value=adx_value, signal_confidence=signal_confidence,
                                       supertrend_dir=supertrend_dir, vwap_position=vwap_position):
                total += lesson.score_modifier
                reasons.append(f"[LESSON] {lesson.rule}: {lesson.score_modifier:+.0f}")
                lesson.hit_count += 1

        for rule in self._adaptive_rules:
            if not rule.active:
                continue
            if self._matches_condition(rule.condition, symbol=symbol, side=side,
                                       entry_hour=entry_hour, entry_day=entry_day,
                                       adx_value=adx_value, signal_confidence=signal_confidence,
                                       supertrend_dir=supertrend_dir, vwap_position=vwap_position):
                age = self._rule_age_days(rule, now)
                decay = math.exp(-0.693 * age / (DECAY_HALF_LIFE_DAYS * 2))
                weighted_mod = rule.score_modifier * rule.confidence * decay
                total += weighted_mod
                reasons.append(f"[AUTO] {rule.pattern} (WR={rule.win_rate:.0%}, "
                               f"eff={rule.effectiveness:.0%}): {weighted_mod:+.1f}")
                rule.hit_count += 1

        if reasons:
            self._mark_dirty()

        return total, reasons

    def should_block(self, symbol: str, side: str, **kwargs) -> tuple[bool, str]:
        for lesson in self._lessons:
            if not lesson.active or lesson.action != "BLOCK":
                continue
            if self._matches_condition(lesson.condition, symbol=symbol, side=side, **kwargs):
                lesson.hit_count += 1
                self._mark_dirty()
                return True, f"Blocked by lesson: {lesson.rule}"

        for rule in self._adaptive_rules:
            if not rule.active or rule.action != "BLOCK":
                continue
            if self._matches_condition(rule.condition, symbol=symbol, side=side, **kwargs):
                rule.hit_count += 1
                self._mark_dirty()
                return True, f"Blocked by pattern: {rule.pattern}"

        return False, ""

    def get_position_size_modifier(self, symbol: str) -> float:
        base_mod = 1.0
        for rule in self._adaptive_rules:
            if not rule.active or rule.action != "REDUCE_SIZE":
                continue
            if self._matches_condition(rule.condition, symbol=symbol):
                base_mod = min(base_mod, 0.5)

        kelly = self.get_kelly_sizing(symbol)
        drawdown = self.get_drawdown_factor()

        return min(base_mod, kelly, drawdown)

    # ── Kelly-Criterion Position Sizing ──────────────────────────

    def get_kelly_sizing(self, symbol: str, side: str = "", **kwargs) -> float:
        base = _extract_base(symbol)
        subset = [t for t in self._trades if base and base in t.symbol.upper()] if base else self._trades

        if len(subset) < MIN_SAMPLE_SIZE:
            return 1.0

        wins = [t for t in subset if t.is_win]
        losses = [t for t in subset if not t.is_win]

        if not wins or not losses:
            return 1.0

        p = len(wins) / len(subset)
        avg_win = sum(abs(t.pnl) for t in wins) / len(wins)
        avg_loss = sum(abs(t.pnl) for t in losses) / len(losses)
        b = avg_win / avg_loss if avg_loss > 0 else 1.0

        kelly = (b * p - (1 - p)) / b if b > 0 else 0
        half_kelly = max(kelly / 2, 0.25)

        return min(half_kelly, 1.0)

    # ── Drawdown-Based Risk Reduction ────────────────────────────

    def get_drawdown_factor(self) -> float:
        if not self._trades:
            return 1.0

        streak = self._consecutive_losses(self._trades)
        recent = self._trades[-20:] if len(self._trades) >= 10 else self._trades
        recent_wr = self._simple_win_rate(recent)

        if streak >= 5:
            return 0.25
        if streak >= 3:
            return 0.5
        if recent_wr < WIN_RATE_CRITICAL_THRESHOLD and len(recent) >= 10:
            return 0.5
        if recent_wr < WIN_RATE_WARN_THRESHOLD and len(recent) >= 10:
            return 0.75
        return 1.0

    # ── Condition Matching ───────────────────────────────────────

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
            if key == "symbol_base":
                if value.upper() not in symbol.upper():
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
            if key == "adx_zone":
                actual_zone = _adx_zone_label(adx_value)
                if actual_zone != value:
                    return False
            if key == "signal_confidence" and value != signal_confidence:
                return False
            if key == "supertrend_dir" and value != supertrend_dir:
                return False
            if key == "vwap_position" and value != vwap_position:
                return False
            if key == "multi_factor":
                base = _extract_base(symbol)
                candidates = []
                if base:
                    candidates.append(f"{base}_h{entry_hour}")
                if supertrend_dir:
                    zone = _adx_zone_label(adx_value)
                    if zone:
                        candidates.append(f"{supertrend_dir}_{zone}")
                if value not in candidates:
                    return False
        return True

    # ── Health Report ────────────────────────────────────────────

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
        alerts: list[str] = []

        if recent_wr < WIN_RATE_CRITICAL_THRESHOLD and len(recent) >= MIN_SAMPLE_SIZE:
            status = "CRITICAL"
            alerts.append(f"7-day win rate {recent_wr:.0%} is critically low")
        elif recent_wr < WIN_RATE_WARN_THRESHOLD and len(recent) >= MIN_SAMPLE_SIZE:
            status = "DEGRADED"
            alerts.append(f"7-day win rate {recent_wr:.0%} below threshold")

        if streak >= LOSS_STREAK_BAN_THRESHOLD:
            status = "CRITICAL"
            alerts.append(f"{streak} consecutive losses — system needs review")
        elif streak >= 3:
            if status == "HEALTHY":
                status = "DEGRADED"
            alerts.append(f"{streak} consecutive losses — monitoring")

        active_bans = [r for r in self._adaptive_rules if r.action == "BLOCK" and r.active]
        if active_bans:
            alerts.append(f"{len(active_bans)} symbol(s) temporarily banned")

        drawdown = self.get_drawdown_factor()
        if drawdown < 1.0:
            alerts.append(f"Drawdown protection active — sizing at {drawdown:.0%}")

        bad_rules = [r for r in self._adaptive_rules
                     if r.active and r.total_predictions >= EFFECTIVENESS_MIN_TRADES
                     and r.effectiveness < 0.50]
        if bad_rules:
            alerts.append(f"{len(bad_rules)} rule(s) underperforming — will be pruned")

        return {
            "status": status,
            "trades": len(self._trades),
            "overall_win_rate": total_wr,
            "recent_win_rate": recent_wr,
            "recent_trades": len(recent),
            "consecutive_losses": streak,
            "avg_r_multiple": self._avg_r_multiple(),
            "active_lessons": sum(1 for l in self._lessons if l.active),
            "active_rules": sum(1 for r in self._adaptive_rules if r.active),
            "active_bans": len(active_bans),
            "drawdown_factor": drawdown,
            "kelly_overall": self.get_kelly_sizing(""),
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

    # ── Insights ─────────────────────────────────────────────────

    def get_insights(self) -> list[dict]:
        if not self._trades:
            return [{"type": "info", "message": "No trades yet — learning starts after first close"}]

        insights: list[dict] = []
        health = self.get_health_report()

        if health["status"] == "CRITICAL":
            for alert in health["alerts"]:
                insights.append({"type": "critical", "message": alert})
        elif health["status"] == "DEGRADED":
            for alert in health["alerts"]:
                insights.append({"type": "warning", "message": alert})

        best_hour = self._best_performing("entry_hour")
        worst_hour = self._worst_performing("entry_hour")
        if best_hour:
            insights.append({"type": "insight",
                             "message": f"Best hour: {best_hour['value']}:00 "
                                        f"({best_hour['win_rate']:.0%} WR, {best_hour['count']} trades)"})
        if worst_hour:
            insights.append({"type": "insight",
                             "message": f"Worst hour: {worst_hour['value']}:00 "
                                        f"({worst_hour['win_rate']:.0%} WR, {worst_hour['count']} trades)"})

        best_cat = self._best_performing("category")
        worst_cat = self._worst_performing("category")
        if best_cat:
            insights.append({"type": "insight",
                             "message": f"Best category: {best_cat['value']} ({best_cat['win_rate']:.0%} WR)"})
        if worst_cat:
            insights.append({"type": "insight",
                             "message": f"Worst category: {worst_cat['value']} ({worst_cat['win_rate']:.0%} WR)"})

        drawdown = health.get("drawdown_factor", 1.0)
        if drawdown < 1.0:
            insights.append({"type": "warning",
                             "message": f"Drawdown protection: sizing reduced to {drawdown:.0%}"})

        kelly = health.get("kelly_overall", 1.0)
        if kelly < 0.5:
            insights.append({"type": "warning",
                             "message": f"Kelly optimal size: {kelly:.0%} — edge is thin, trade small"})

        for lesson in self._lessons:
            if lesson.active:
                insights.append({"type": "lesson",
                                 "message": f"User lesson: {lesson.rule} (applied {lesson.hit_count}x)"})

        for rule in self._adaptive_rules:
            if rule.active and rule.confidence > 0.5:
                eff_str = f", eff={rule.effectiveness:.0%}" if rule.total_predictions >= 5 else ""
                insights.append({"type": "rule",
                                 "message": f"Learned: {rule.pattern} → WR={rule.win_rate:.0%} "
                                            f"vs baseline {rule.baseline_win_rate:.0%} "
                                            f"({rule.score_modifier:+.0f}{eff_str})"})

        return insights

    # ── Export / Import ──────────────────────────────────────────

    def export_brain(self) -> dict:
        return {
            "exported_at": datetime.now(IST).isoformat(),
            "trade_count": len(self._trades),
            "lessons": [asdict(l) for l in self._lessons if l.active],
            "rules": [asdict(r) for r in self._adaptive_rules if r.active],
            "health": self.get_health_report(),
        }

    def import_lessons(self, lessons_data: list[dict]) -> int:
        imported = 0
        for d in lessons_data:
            existing = next((l for l in self._lessons if l.id == d.get("id")), None)
            if not existing:
                self._lessons.append(self._parse(Lesson, d))
                imported += 1
        if imported:
            self._save()
        return imported

    # ── Utilities ────────────────────────────────────────────────

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

        key_fn = max if best else min
        key = key_fn(valid, key=lambda k: self._simple_win_rate(valid[k]))
        return {
            "value": key,
            "win_rate": self._simple_win_rate(valid[key]),
            "count": len(valid[key]),
            "avg_pnl": sum(t.pnl for t in valid[key]) / len(valid[key]),
        }

    @staticmethod
    def _weighted_win_rate(trades: list[TradeContext], now: datetime) -> float:
        if not trades:
            return 0.0
        total_w = 0.0
        win_w = 0.0
        for t in trades:
            try:
                age = (now - datetime.fromisoformat(t.exit_time)).days
            except (ValueError, TypeError):
                age = 0
            w = math.exp(-0.693 * age / DECAY_HALF_LIFE_DAYS)
            total_w += w
            if t.is_win:
                win_w += w
        return win_w / total_w if total_w > 0 else 0.0

    @staticmethod
    def _simple_win_rate(trades: list[TradeContext]) -> float:
        if not trades:
            return 0.0
        return sum(1 for t in trades if t.is_win) / len(trades)

    @staticmethod
    def _consecutive_losses(trades: list[TradeContext]) -> int:
        count = 0
        for t in reversed(trades):
            if t.is_win:
                break
            count += 1
        return count

    def _avg_r_multiple(self) -> float:
        valid = [t for t in self._trades if t.r_multiple != 0]
        return sum(t.r_multiple for t in valid) / len(valid) if valid else 0.0

    @staticmethod
    def _pattern_label(dimension: str, value: Any) -> str:
        labels = {
            "entry_hour": lambda v: f"Trades at {v}:00",
            "entry_day": lambda v: f"{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][v]} trades",
            "symbol_base": lambda v: f"{v} trades",
            "adx_zone": lambda v: f"ADX {v} zone",
            "signal_confidence": lambda v: f"{v} confidence signals",
            "supertrend_dir": lambda v: f"SuperTrend {v} regime",
            "vwap_position": lambda v: f"Price {v} VWAP",
        }
        fn = labels.get(dimension)
        return fn(value) if fn else f"{dimension}={value}"

    @property
    def trade_count(self) -> int:
        return len(self._trades)

    @property
    def lessons(self) -> list[Lesson]:
        return [l for l in self._lessons if l.active]

    @property
    def active_rules(self) -> list[AdaptiveRule]:
        return [r for r in self._adaptive_rules if r.active]


# ── Module-level helpers ─────────────────────────────────────────

def _z_test_proportions(p1: float, n1: int, p2: float, n2: int) -> float:
    if n1 < 2 or n2 < 2:
        return 0.0
    p_pool = (p1 * n1 + p2 * n2) / (n1 + n2)
    if p_pool <= 0 or p_pool >= 1:
        return 0.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    return (p1 - p2) / se if se > 0 else 0.0


def _extract_base(symbol: str) -> str:
    upper = symbol.upper()
    for base in _BASE_SYMBOLS:
        if base in upper:
            return base
    return ""


def _adx_zone_label(adx: float) -> str:
    for low, high, label in _ADX_ZONES:
        if low <= adx < high:
            return label
    return ""


def _categorize(symbol: str) -> str:
    upper = symbol.upper()
    if "FUT" in upper:
        return "INDEX_FUTURES"
    return "INDICES"
