# Omega Trading Company (OTC) — Full System Prompt for AI Assistants

> Use this prompt to onboard any AI (Claude, Gemini, ChatGPT, Copilot, etc.) onto the OTC codebase. Paste it as-is. The AI will understand the full architecture, conventions, and can start contributing immediately.

---

## Project Overview

**Omega Trading Company (OTC)** is a fully automated algorithmic trading bot for **Indian index options** (NIFTY, BANKNIFTY, SENSEX). It runs in **paper trading mode** by default with ₹2,00,000 virtual capital. The system uses confluence-based signal generation (5+ technical indicators must agree), machine learning regime detection, real-time WebSocket feeds, and a premium web dashboard.

- **Language**: Python 3.13
- **Broker**: Dhan (dhanhq API) — paper mode uses PaperBroker simulator
- **Web Framework**: FastAPI + Uvicorn on port 8000
- **Dashboard**: Single-page HTML app served at `/dashboard` (and `/`) — all HTML/CSS/JS is embedded in `webhook/dashboard.py` as a Python string
- **Data Source**: Yahoo Finance (yfinance) for OHLCV candles, Dhan WebSocket for live tick data
- **OS**: macOS (uses iMessage alerts on Mac, Telegram on Linux/Docker)
- **GitHub**: `vishnu1609/omega-trading-system` on `main` branch

---

## Architecture & File Structure

```
omega-trading-system/
├── main.py                          # Entry point — wires all components, runs FastAPI + strategy loop
├── config/
│   ├── settings.yaml                # All tunable params: risk, session times, indicator periods, guardrails
│   └── instruments.yaml             # NIFTY/BANKNIFTY/SENSEX: lot sizes, strike steps, costs
├── core/
│   ├── signal_generator.py          # 2000+ lines — technical analysis engine (SuperTrend, ADX, RSI, BB, VWAP, EMA, Choppiness, OBV, MACD, TTM Squeeze, ORB, multi-timeframe)
│   ├── option_chain.py              # ATM strike resolver, OTM spread pair resolver, chain fetching
│   ├── position_manager.py          # Position dataclass, SL/TP tracking, spread fields, trail SL
│   ├── order_manager.py             # Signal → Order execution, fill simulation
│   ├── risk_engine.py               # Per-trade risk checks, daily loss limits
│   ├── guardrails.py                # Safety: ADX floor, VWAP warmup, stale candle rejection, expiry day cutoff, capital limits
│   ├── wallet.py                    # Virtual wallet: balance, margin, P&L tracking
│   ├── trade_memory.py              # Rule-based memory: stores pattern rules (boosters/blockers) with scores and conditions
│   ├── broker_dhan.py               # Dhan API wrapper (live trading)
│   ├── broker_paper.py              # Paper broker: simulated fills with realistic slippage model
│   ├── broker_base.py               # Abstract broker interface
│   ├── live_feed.py                 # Dhan WebSocket: real-time NIFTY/BANKNIFTY/SENSEX tick data
│   ├── chain_collector.py           # Background option chain data collector
│   ├── greeks.py                    # Black-Scholes Greeks (delta, gamma, theta, vega), DTE calculation
│   ├── reverse_engine.py            # Post-trade autopsy + pattern mining with z-test significance
│   ├── instrument_master.py         # Dhan instrument CSV parser
│   ├── instrument_selector.py       # Maps index → tradeable option contract
│   ├── signal_scorer.py             # Multi-factor signal scoring
│   ├── daily_report.py              # End-of-day P&L report generator
│   └── database.py                  # SQLite trade database
├── strategies/
│   └── omega_complete.py            # Main strategy: entry/exit logic, SL/TP management, spread handling, position lifecycle
├── webhook/
│   ├── dashboard.py                 # 2400+ lines — FastAPI routes + full HTML/CSS/JS dashboard (7 tabs)
│   └── tv_receiver.py              # TradingView webhook receiver
├── ml/
│   ├── engine.py                    # ML prediction engine
│   ├── regime.py                    # Market regime classifier (trending/ranging/volatile)
│   ├── features.py                  # Feature engineering for ML
│   ├── neural_net.py                # Neural network model
│   ├── train.py                     # Training pipeline
│   ├── validation.py                # Walk-forward validation
│   └── data_collector.py            # Training data collection
├── backtest/
│   ├── engine.py                    # Backtesting engine
│   └── optimizer.py                 # Parameter optimization
├── utils/
│   ├── logger.py                    # Structured logging setup
│   ├── market_calendar.py           # NSE/BSE holiday calendar, market hours
│   ├── imessage_alert.py            # macOS iMessage trade notifications
│   └── telegram_alert.py           # Telegram bot trade notifications
├── data/
│   ├── dashboard_state.json         # Live state file (positions, trades, wallet, signals)
│   ├── trade_memory_rules.json      # Learned trading rules
│   ├── autopsy_results.json         # Reverse engine analysis results
│   └── reverse_engine_report.json   # Pattern mining report
├── tests/
│   ├── test_brutal_chaos.py         # Chaos/stress testing
│   ├── test_chaos_resilience.py     # Resilience testing
│   ├── test_trade_memory.py         # Trade memory unit tests
│   └── test_injector.py             # Rule injection tests
├── Dockerfile                       # Python 3.13-slim, exposes 8000
├── requirements.txt                 # fastapi, uvicorn, dhanhq, pandas, numpy, yfinance, etc.
├── .env                             # Secrets: DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID, TV_WEBHOOK_SECRET (NEVER commit)
└── .gitignore                       # Excludes .env, data/, logs/, __pycache__/
```

---

## How It Works (Flow)

1. **`main.py`** starts FastAPI server (port 8000) + strategy loop in a background thread
2. **`LiveFeed`** opens Dhan WebSocket for real-time NIFTY/BANKNIFTY/SENSEX tick data
3. Every 30 seconds, **`SignalGenerator`** fetches 5-min OHLCV candles from Yahoo Finance and computes:
   - SuperTrend (period=10, mult=3.0)
   - ADX (len=14, threshold=20)
   - RSI (period=14, overbought=70, oversold=30)
   - Bollinger Bands (period=20, std=2.0)
   - VWAP (intraday volume-weighted)
   - EMA 9/21 crossover
   - Choppiness Index (period=14, threshold=61.8)
   - Opening Range Breakout (30-min window)
   - TTM Squeeze detection
   - Multi-timeframe confirmation (15-min)
4. Signals require **5-factor confluence**: SuperTrend + TEMA + ADX + EMA200 + VWAP all agreeing
5. **`Guardrails`** validates: ADX floor, VWAP warmup (after 09:30), candle staleness (<5.5 min), expiry day cutoff (12:30), capital limits
6. **`TradeMemory`** applies learned rules: booster patterns increase score, blocker patterns reject signals
7. **`OmegaStrategy`** executes: resolves ATM option via `OptionChainResolver`, places order via `OrderManager`
8. **`PositionManager`** tracks: dynamic SL/TP, trail SL (breakeven at +0.15%, lock at +0.30%, tight trail at +0.50%)
9. **`Wallet`** manages virtual capital: deducts margin on entry, credits on exit
10. **`ReverseEngine`** post-analyzes: replays candles at entry/exit, scores indicator accuracy, mines winning/losing patterns with z-test, auto-injects rules into TradeMemory

---

## Dashboard (webhook/dashboard.py)

The dashboard is a **single-page app** embedded as a Python string in `webhook/dashboard.py`. It is NOT a separate frontend — all HTML, CSS, and JavaScript are inside a triple-quoted Python string (`_HTML`).

### 7 Tabs:
1. **Dashboard** — Hero P&L, stats grid (12 cards), equity curve, risk panel, best/worst trades, leaderboard
2. **Positions** — Live position cards with SL/TP bars, Greeks, editable SL/TP
3. **Trades** — Trade history table with export CSV
4. **Signals** — Recent signal cards with indicator breakdown bars
5. **Option Chain** — Live option chain table with OI bars, ATM highlighting
6. **Analytics** — P&L by exit reason, by index, by hour, by day, long vs short, win streaks, heatmap
7. **Reverse Engine** — Trade autopsy, indicator accuracy, pattern mining results

### Dashboard Design:
- **Dark theme** — Deep navy/black background with gold (#c9a84c) accent
- **Fonts** — Inter (body), DM Serif Display (hero/brand), JetBrains Mono (numbers)
- **Mobile responsive** — @media breakpoints at 768px and 480px
- **Auto-refresh** — Fetches `/api/state` every 1 second via `setInterval`
- **CSRF protection** — Token injected at render time, sent as `X-CSRF-Token` header

### API Endpoints:
- `GET /api/state` — Full bot state (positions, trades, wallet, signals, config, indices, feed health)
- `GET /api/option_chain/{underlying}` — Live option chain for NIFTY/BANKNIFTY/SENSEX
- `GET /api/trade_analytics` — Aggregated analytics data
- `GET /api/reverse_engine` — Run reverse engineering analysis
- `GET /api/export_csv` — Download trade history as CSV
- `POST /api/update_sltp` — Modify SL/TP for an active position (CSRF protected)
- `POST /api/close_position` — Force-close a position (CSRF protected)
- `POST /api/close_all` — Emergency close all positions (CSRF protected)
- `GET /health` — Health check

---

## Key Technical Details

### Signal Types:
- `BUY_CE` — Buy Call (bullish directional)
- `BUY_PE` — Buy Put (bearish directional)
- `SELL_CE_SPREAD` — Bear call credit spread (range/choppy regime)
- `SELL_PE_SPREAD` — Bull put credit spread (range/choppy regime)

### Position Fields (dataclass):
```python
Position:
  trade_id, security_id, trade_symbol, exchange, qty, direction,
  entry_price, current_price, sl_price, tp_price, entry_time,
  signal_score, signal_confidence, instrument_name,
  is_spread, hedge_security_id, hedge_trade_symbol, hedge_strike,
  spread_width, net_credit, max_loss,
  entry_score_breakdown, trail_activated, breakeven_locked
```

### Trade Memory Rule Schema:
```json
{
  "id": "pattern_name",
  "pattern": "description",
  "condition": {"key": "value_match"},
  "action": "boost_score | penalize_score | block_signal",
  "score_modifier": 15.0,
  "confidence": 0.75,
  "sample_size": 20,
  "z_score": 2.1,
  "source": "reverse_engine"
}
```

### Instruments:
| Index | Lot Size | Strike Step | Exchange | Expiry Day |
|-------|----------|-------------|----------|------------|
| NIFTY | 75 | 50 | NSE | Thursday |
| BANKNIFTY | 15 | 100 | NSE | Wednesday |
| SENSEX | 10 | 100 | BSE | Friday |

### Risk Parameters:
- Capital: ₹2,00,000
- Max risk per trade: 8% (₹16,000)
- Max daily loss: ₹20,000
- Max concurrent positions: 10
- NSE allocation cap: 90%
- Default qty: 3 lots

---

## Coding Conventions

- **No comments** unless the WHY is non-obvious
- **No docstrings** on internal methods — only module-level docstrings
- **Inline styles in dashboard JS** — the dashboard HTML/CSS/JS is all in one Python string, JS builds HTML with string concatenation (`h+='<div>...'`)
- **Config-driven** — all tunable parameters in `config/settings.yaml`, never hardcoded
- **IST timezone** throughout — `ZoneInfo("Asia/Kolkata")`
- **Logging** via Python `logging` module, structured format
- **Error handling** — try/except at boundaries only, let internal code fail fast
- **File-based state** — `data/dashboard_state.json` is the single source of truth written by the strategy loop and read by the dashboard API

---

## How to Run

```bash
# Install dependencies
pip install -r requirements.txt

# Set up .env file with your Dhan credentials
# DHAN_CLIENT_ID=your_client_id
# DHAN_ACCESS_TOKEN=your_token (expires every 24h, renew at api.dhan.co)

# Run in paper mode (default)
python main.py

# Dashboard will be at http://localhost:8000/dashboard
# API at http://localhost:8000/api/state
```

---

## Current Performance (Paper Trading)

- 22 trades completed
- 50% win rate
- ₹3,358 total P&L
- 1.87x profit factor
- 0.25 Sharpe ratio
- 1.9:1 avg reward:risk
- Best indicators: Choppiness (96% accuracy), RSI (82%)
- Worst indicators: SuperTrend (18%), VWAP (23%)
- 9 pattern rules auto-injected by reverse engine

---

## What You Can Work On

When contributing, focus on these areas:

1. **Dashboard UI improvements** — Better charts, animations, new visualizations (all changes go in `webhook/dashboard.py` inside the `_HTML` string)
2. **Strategy optimization** — Improve signal generation in `core/signal_generator.py`, adjust parameters in `config/settings.yaml`
3. **ML model** — Enhance regime detection in `ml/`, add new features in `ml/features.py`
4. **New indicators** — Add to `core/signal_generator.py` (follow existing pattern: compute in `_generate_signals()`, add to signal dict)
5. **Backtesting** — Improve `backtest/engine.py` for more realistic option P&L simulation
6. **Risk management** — Enhance `core/guardrails.py` and `core/risk_engine.py`
7. **Alerts** — Better trade notifications in `utils/telegram_alert.py`

### Rules:
- **NEVER** commit `.env` or any secrets
- **NEVER** switch to live mode without explicit confirmation
- **ALWAYS** test in paper mode first
- Keep the dashboard as a single embedded HTML string (don't split into separate files)
- All times are IST (Asia/Kolkata)
- The bot runs on port 8000 — don't change this
- Dhan token expires every 24 hours — the dashboard shows a warning banner

---

## Quick Reference Commands

```bash
# Check bot status
curl http://localhost:8000/health

# Get current state
curl http://localhost:8000/api/state | python -m json.tool

# Get trade analytics
curl http://localhost:8000/api/trade_analytics | python -m json.tool

# Run reverse engine
curl http://localhost:8000/api/reverse_engine | python -m json.tool

# View dashboard
open http://localhost:8000/dashboard
```
