# Parallel AI Research Prompts — Omega Trading System

## Context (paste this at the top of BOTH prompts)

```
I'm building an algo trading bot for Indian index options (NIFTY, BANKNIFTY, SENSEX) via Dhan broker API. Python + FastAPI. Paper trading mode currently.

Current system:
- Signal generation: SuperTrend, ADX, RSI, Bollinger Bands, EMA crossover, VWAP, Choppiness Index, Opening Range Breakout
- 15-factor scoring system (0-100), confidence tiers: HIGH (>=65), MEDIUM (>=52), LOW (<52)
- ML layer: neural net [50,64,32,16,2], 70% accuracy on historical, but trade buffer stuck at 0/20 (not learning from live trades)
- Volatility regime detection: TRENDING, CHOPPY, RANGING, VOLATILE
- Dynamic SL/TP by confidence tier (e.g. HIGH: 12% SL / 18% TP first level)
- Credit spread strategy for choppy markets (sell OTM + buy further OTM hedge)
- ATM strike selection for directional trades
- Current win rate: ~30-50% on paper trades, need to improve to 55%+ consistently
- Trades: NIFTY lot=75, BANKNIFTY lot=15, SENSEX lot=10
- Session: 9:16-15:10 IST, Indian market hours

Problems:
1. Most signals score 53-62 (MEDIUM) and get blocked by ML gate in CHOPPY regime
2. Trailing SL triggers too early — locking in ₹3-5 profit on trades that could run ₹500+
3. Win rate not improving — need better entry timing and exit management
4. ML engine not self-learning (buffer stays 0/20)
5. Too many conflicting signals across indices — need better filtering
```

---

## PROMPT FOR GEMINI (Deep Research Mode)

```
[PASTE CONTEXT ABOVE]

You are a quantitative trading research assistant. I need deep, actionable research on these 5 topics. For each, give me:
- The specific mathematical formula or algorithm
- Python pseudocode I can implement
- Parameter values tuned for Indian index options (NIFTY/BANKNIFTY 5-min candles)
- Backtested evidence or academic citations

### Research Topics:

1. **OPTIMAL TRAILING STOP FOR OPTIONS**
   - Compare: Chandelier Exit vs ATR-based trail vs Parabolic SAR vs SuperTrend trail vs percentage-based
   - Which works best for 5-min options trades lasting 15-90 minutes?
   - What's the optimal initial SL distance for ATM NIFTY options (premium ₹100-300)?
   - How should trail tighten as profit grows? Give me exact breakpoints.
   - Research "volatility-adjusted trailing stops" — how to widen SL in high-vol and tighten in low-vol

2. **REGIME-ADAPTIVE ENTRY FILTERS**
   - Best indicators to confirm entries in CHOPPY vs TRENDING regimes
   - In choppy markets: should we trade mean-reversion (sell rallies, buy dips) or sit out?
   - Optimal ADX thresholds for Indian indices (current: 20, but ADX=25-35 seems to block valid moves)
   - Research "market microstructure noise filters" for 5-min candles
   - How do professional quant funds handle regime transitions?

3. **OPTIONS-SPECIFIC ENTRY TIMING**
   - Best time-of-day for option entries on NIFTY/BANKNIFTY (historical win rate by 30-min window)
   - How to use IV percentile as entry filter (enter when IV is elevated vs compressed?)
   - Greeks-based entry: optimal delta range for directional trades
   - Expiry day (0-DTE) vs weekly vs monthly — which has best risk-adjusted returns?
   - Research "options momentum" — does a rising option premium predict continuation?

4. **SIGNAL SCORING IMPROVEMENTS**
   - Research the best combination of 5-7 technical indicators for short-term options trading
   - How should indicators be weighted? (equal weight vs ML-learned weights vs information-ratio based?)
   - What's the optimal score threshold for entry? (currently 52 for MEDIUM)
   - How to handle indicator conflicts (RSI says buy, MACD says sell)?
   - Research "indicator consensus" systems used by hedge funds

5. **ML SELF-LEARNING ARCHITECTURE**
   - Best online learning approaches for small-sample trading (we get ~5-15 trades/day)
   - XGBoost vs LightGBM vs neural net for <1000 sample classification
   - How to handle class imbalance (more losses than wins)?
   - Feature importance: which features actually predict option trade outcomes?
   - Research "meta-learning" or "few-shot learning" for trading systems
   - How to detect when the model is stale and needs retraining?

FORMAT: For each topic, give me a structured response with:
- Key finding (1 sentence)
- Algorithm/formula
- Python pseudocode (implementable)
- Recommended parameters for Indian index options
- Source/citation
```

---

## PROMPT FOR CHATGPT (Web Browsing + Code Mode)

```
[PASTE CONTEXT ABOVE]

You are a trading systems engineer. I need practical, code-ready research. Search the web for the latest (2024-2026) papers, blog posts, and open-source implementations.

### Research Topics:

1. **PROFESSIONAL EXIT MANAGEMENT SYSTEMS**
   - Search for "adaptive profit trailing" implementations on GitHub
   - Find how Zerodha Streak, Tradetron, and other Indian algo platforms handle trailing SL
   - Research "chandelier exit for options" — any modifications needed for premium decay?
   - Find the best open-source trailing SL implementations in Python
   - What do prop trading firms use? Research Jump Trading, Citadel, Two Sigma approaches (any public info)

2. **INDIAN MARKET SPECIFIC PATTERNS**
   - Search for academic papers on NIFTY/BANKNIFTY intraday patterns (2023-2026)
   - What time windows have the highest win rate for options entries?
   - Research "NIFTY opening range breakout" success rates
   - Find data on BANKNIFTY weekly expiry patterns (Thursday expiry effects)
   - FII/DII flow impact on intraday options — any predictive value?
   - Search for "India VIX" as entry filter — optimal VIX ranges for buying vs selling options

3. **MODERN ALGO TRADING ARCHITECTURES**
   - Search GitHub for the best open-source algo trading bots (Python, 2024-2026)
   - How do modern systems handle signal conflicts across multiple instruments?
   - Research "ensemble signal generation" — combining multiple strategies
   - Find implementations of "Kelly criterion" for position sizing in options
   - Search for "reinforcement learning for options trading" recent papers

4. **DHAN API OPTIMIZATION**
   - Search Dhan API docs for websocket-based option chain streaming
   - Research rate limit best practices for Indian broker APIs
   - Find any open-source Dhan API wrappers with built-in caching
   - How to get real-time Greeks from Dhan? Or calculate them locally?
   - Search for "Dhan API options chain" implementations

5. **RISK MANAGEMENT FOR SMALL ACCOUNTS (₹1-2 LAKH)**
   - Research optimal position sizing for ₹1L capital trading index options
   - Kelly criterion vs fixed fractional vs volatility-based sizing
   - How many concurrent positions for ₹1L account?
   - Maximum drawdown limits — what % triggers strategy review?
   - Research "anti-martingale" position sizing for winning streaks

FORMAT: For each topic:
- Top 3 findings with source URLs
- Code snippets (Python) where available
- Specific numbers/parameters for Indian markets
- One concrete recommendation I can implement today
```

---

## WHAT I (CLAUDE) WILL DO IN PARALLEL

While you feed those prompts to Gemini and ChatGPT, I'll work on:
1. Fix the ML self-learning pipeline (buffer stuck at 0/20)
2. Tune trailing SL parameters based on historical VPS trade data
3. Implement regime-adaptive signal thresholds
4. Review and optimize the 15-factor scoring weights
5. Add IV percentile as a signal filter

---

## HOW TO FEED RESULTS BACK

When you get responses, paste them to Claude with:
```
Here are the research results from [Gemini/ChatGPT]. Implement the best findings:

[paste response]
```

I'll extract the actionable items, cross-reference across all 3 AIs, and implement the consensus best practices.
