"""
Pre-Market Scout — Runs before market open (9:00 AM IST).

Gathers global cues, FII/DII flows, VIX, key levels, and sets
the day's directional bias for the bot to consume.

Output: data/day_context.json (read by OmegaStrategy at startup)
Standalone: PYTHONPATH=. python3 core/scout.py
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)

CONTEXT_PATH = Path(__file__).parent.parent / "data" / "day_context.json"

# Key support/resistance levels updated periodically
NIFTY_LEVELS = {"major_support": [25000, 24800, 24500], "major_resistance": [25500, 25700, 26000]}
SENSEX_LEVELS = {"major_support": [81500, 81000, 80500], "major_resistance": [82500, 83000, 83500]}
BANKNIFTY_LEVELS = {"major_support": [53000, 52500, 52000], "major_resistance": [54000, 54500, 55000]}


class PreMarketScout:
    def __init__(self):
        self._context: dict = {}

    def gather_vix(self) -> dict:
        try:
            import yfinance as yf
            df = yf.download("^INDIAVIX", period="5d", interval="1d", progress=False)
            if df.empty:
                return {"vix": 0, "vix_change": 0, "vix_regime": "UNKNOWN"}
            close_col = df["Close"]
            if hasattr(close_col.iloc[-1], "iloc"):
                vix = float(close_col.iloc[-1].iloc[0])
                prev_vix = float(close_col.iloc[-2].iloc[0]) if len(df) >= 2 else vix
            else:
                vix = float(close_col.iloc[-1])
                prev_vix = float(close_col.iloc[-2]) if len(df) >= 2 else vix
            change = round(vix - prev_vix, 2)
            regime = "HIGH" if vix > 20 else "ELEVATED" if vix > 15 else "LOW"
            return {"vix": round(vix, 2), "vix_change": change, "vix_regime": regime}
        except Exception as e:
            log.warning("VIX fetch failed: %s", e)
            return {"vix": 0, "vix_change": 0, "vix_regime": "UNKNOWN"}

    def gather_global_cues(self) -> dict:
        tickers = {
            "SGX_NIFTY": "^NSEI",
            "DOW_FUTURES": "YM=F",
            "NASDAQ_FUTURES": "NQ=F",
            "HANG_SENG": "^HSI",
            "NIKKEI": "^N225",
            "USD_INR": "USDINR=X",
            "CRUDE": "CL=F",
            "GOLD": "GC=F",
        }
        cues = {}
        try:
            import yfinance as yf
            for name, ticker in tickers.items():
                try:
                    df = yf.download(ticker, period="2d", interval="1d", progress=False)
                    if df.empty or len(df) < 2:
                        continue
                    close_col = df["Close"]
                    if hasattr(close_col.iloc[-1], "iloc"):
                        last = float(close_col.iloc[-1].iloc[0])
                        prev = float(close_col.iloc[-2].iloc[0])
                    else:
                        last = float(close_col.iloc[-1])
                        prev = float(close_col.iloc[-2])
                    chg_pct = round((last - prev) / prev * 100, 2) if prev > 0 else 0
                    cues[name] = {"price": round(last, 2), "change_pct": chg_pct}
                except Exception:
                    pass
        except ImportError:
            log.warning("yfinance not available for global cues")
        return cues

    def compute_bias(self, vix_data: dict, global_cues: dict) -> dict:
        bullish_score = 0
        bearish_score = 0

        # VIX direction
        if vix_data["vix_change"] < -0.5:
            bullish_score += 1  # falling VIX = bullish
        elif vix_data["vix_change"] > 0.5:
            bearish_score += 1

        # VIX level
        if vix_data["vix"] > 20:
            bearish_score += 1  # high VIX = fear
        elif vix_data["vix"] < 13:
            bullish_score += 1

        # Global cues
        for name in ["DOW_FUTURES", "NASDAQ_FUTURES"]:
            cue = global_cues.get(name, {})
            chg = cue.get("change_pct", 0)
            if chg > 0.3:
                bullish_score += 1
            elif chg < -0.3:
                bearish_score += 1

        # Asia
        for name in ["HANG_SENG", "NIKKEI"]:
            cue = global_cues.get(name, {})
            chg = cue.get("change_pct", 0)
            if chg > 0.5:
                bullish_score += 1
            elif chg < -0.5:
                bearish_score += 1

        # Crude (rising crude = bearish for India)
        crude = global_cues.get("CRUDE", {})
        crude_chg = crude.get("change_pct", 0)
        if crude_chg > 1.0:
            bearish_score += 1
        elif crude_chg < -1.0:
            bullish_score += 1

        net = bullish_score - bearish_score
        if net >= 2:
            bias = "BULLISH"
        elif net <= -2:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        return {
            "bias": bias,
            "bullish_score": bullish_score,
            "bearish_score": bearish_score,
            "net_score": net,
        }

    def compute_key_levels(self, global_cues: dict) -> dict:
        return {
            "NIFTY": NIFTY_LEVELS,
            "SENSEX": SENSEX_LEVELS,
            "BANKNIFTY": BANKNIFTY_LEVELS,
        }

    def run(self) -> dict:
        log.info("Pre-Market Scout starting...")
        t0 = time.time()

        vix_data = self.gather_vix()
        global_cues = self.gather_global_cues()
        bias = self.compute_bias(vix_data, global_cues)
        levels = self.compute_key_levels(global_cues)

        context = {
            "date": datetime.now(IST).strftime("%Y-%m-%d"),
            "generated_at": datetime.now(IST).isoformat(),
            "vix": vix_data,
            "global_cues": global_cues,
            "bias": bias,
            "key_levels": levels,
            "recommendations": self._generate_recommendations(vix_data, bias, global_cues),
        }

        CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTEXT_PATH.write_text(json.dumps(context, indent=2))
        elapsed = time.time() - t0
        log.info("Pre-Market Scout done in %.1fs — bias=%s, VIX=%.1f (%s)",
                 elapsed, bias["bias"], vix_data["vix"], vix_data["vix_regime"])
        return context

    def _generate_recommendations(self, vix: dict, bias: dict, cues: dict) -> list[str]:
        recs = []
        b = bias["bias"]

        if b == "BULLISH":
            recs.append("Favor BUY_CE signals. Tighten PE entry requirements.")
        elif b == "BEARISH":
            recs.append("Favor BUY_PE signals. Tighten CE entry requirements.")
        else:
            recs.append("No clear directional bias — prefer credit spreads or tighter entries.")

        if vix["vix_regime"] == "HIGH":
            recs.append("HIGH VIX: Reduce position sizes. Expect whipsaws. Widen SLs.")
        elif vix["vix_regime"] == "LOW":
            recs.append("LOW VIX: Premiums are cheap. Consider buying options over spreads.")

        crude = cues.get("CRUDE", {})
        if crude.get("change_pct", 0) > 2:
            recs.append("Crude spiking — bearish for Indian markets. Watch energy-heavy indices.")

        return recs


def load_day_context() -> dict | None:
    if not CONTEXT_PATH.exists():
        return None
    try:
        ctx = json.loads(CONTEXT_PATH.read_text())
        if ctx.get("date") != datetime.now(IST).strftime("%Y-%m-%d"):
            return None
        return ctx
    except Exception:
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s")
    scout = PreMarketScout()
    ctx = scout.run()
    print(json.dumps(ctx, indent=2))
