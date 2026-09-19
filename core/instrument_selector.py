from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class InstrumentScore:
    name: str
    adx: float = 0.0
    volume_rank: float = 0.0       # 0-1, higher = more liquid
    iv_percentile: float = 0.0     # 0-100, higher = more expensive premiums
    spread_pct: float = 0.0        # bid-ask spread as % of mid — lower = better
    total_score: float = 0.0


class InstrumentSelector:
    """Picks the best-trending index for the current session.

    Ranking logic:
    - ADX (40% weight): higher ADX = stronger trend = cleaner signals
    - Volume rank (25%): more volume = tighter spreads, less slippage
    - IV percentile inverse (20%): lower IV = cheaper options = better R:R for buyers
    - Spread (15% inverse): tighter spread = less friction
    """

    WEIGHTS = {
        "adx": 0.40,
        "volume": 0.25,
        "iv_inverse": 0.20,
        "spread_inverse": 0.15,
    }

    def __init__(self, instruments: dict) -> None:
        self.instruments = instruments

    def score(self, name: str, adx: float, volume_rank: float,
              iv_percentile: float, spread_pct: float) -> InstrumentScore:
        adx_norm = min(adx / 50, 1.0)
        vol_norm = min(volume_rank, 1.0)
        iv_inv = 1 - min(iv_percentile / 100, 1.0)
        spread_inv = 1 - min(spread_pct / 2, 1.0)  # cap at 2% spread

        total = (
            self.WEIGHTS["adx"] * adx_norm
            + self.WEIGHTS["volume"] * vol_norm
            + self.WEIGHTS["iv_inverse"] * iv_inv
            + self.WEIGHTS["spread_inverse"] * spread_inv
        )

        return InstrumentScore(
            name=name,
            adx=adx,
            volume_rank=volume_rank,
            iv_percentile=iv_percentile,
            spread_pct=spread_pct,
            total_score=round(total, 4),
        )

    def select_best(self, scores: list[InstrumentScore]) -> InstrumentScore:
        if not scores:
            raise ValueError("No instruments scored")
        ranked = sorted(scores, key=lambda s: s.total_score, reverse=True)
        best = ranked[0]
        log.info("Instrument ranking: %s",
                 " > ".join(f"{s.name}({s.total_score:.3f})" for s in ranked))
        log.info("Selected: %s (ADX=%.1f, Vol=%.2f, IV=%.0f%%, Spread=%.3f%%)",
                 best.name, best.adx, best.volume_rank,
                 best.iv_percentile, best.spread_pct)
        return best

    def get_config(self, name: str) -> dict:
        cfg = self.instruments.get(name)
        if not cfg:
            raise KeyError(f"Instrument {name} not found in config")
        return cfg
