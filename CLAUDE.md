# Response Style

- Be maximally concise. No preamble, no trailing summaries, no restatements.
- Prefer bullet points and code over prose paragraphs.
- Explain only the non-obvious. Skip what the code already says.
- One sentence end-of-turn summary max.

# Omega Trading Company

Algo trading bot for Indian index options (Nifty 50, Bank Nifty, Sensex).
Paper-trading-first. Built by Vishnu with Claude.

## Architecture

```
omega-trading-system/
├── core/                    # Broker + execution layer
│   ├── broker_base.py       # Abstract broker interface
│   ├── broker_dhan.py       # Dhan API adapter
│   ├── broker_paper.py      # Paper trading simulator
│   ├── order_manager.py     # Idempotent orders, retry, slippage buffer
│   ├── position_manager.py  # Tracks LONG + SHORT positions with correct P&L
│   ├── risk_engine.py       # 7-gate safety system (non-negotiable)
│   ├── instrument_master.py # Dhan instrument CSV loader
│   └── instrument_selector.py # Picks best-trending index
├── strategies/
│   └── omega_complete.py    # Protocol Omega confluence strategy
├── backtest/
│   ├── engine.py            # Backtester with buy+sell, cost model
│   └── optimizer.py         # Grid search + walk-forward validation
├── webhook/
│   └── tv_receiver.py       # TradingView webhook receiver (FastAPI)
├── pinescript/
│   ├── omega_v10_adaptive.pine  # Latest: buy+sell, regime detection
│   └── omega_complete_v6.pine   # Earlier version (buy-only)
├── data/
│   └── feed.py              # CSV loader + Dhan historical API
├── utils/
│   ├── logger.py            # File + console logging
│   ├── imessage_alert.py    # iMessage notifications (Apple Messages)
│   └── telegram_alert.py    # Telegram (deprecated — replaced by iMessage)
├── config/
│   ├── settings.yaml        # Runtime config (mode, risk, webhook)
│   └── instruments.yaml     # Index definitions + cost model
├── main.py                  # Entry point (paper default, --live flag)
└── run_backtest.py          # CLI backtest runner
```

## How It Works

1. Pine Script (TradingView) detects signals using 5-factor confluence
2. TradingView fires a webhook → FastAPI server receives it
3. Signal passes through 7 risk gates (daily loss, cooldown, margin, etc.)
4. Order placed on Dhan (limit order with slippage buffer)
5. Position monitored with trailing stop until exit

## Key Concepts

- **BUY mode** (ADX > 25): trending market → buy ATM options
- **SELL mode** (ADX < 20): ranging market → sell OTM options
- **5-factor confluence**: SuperTrend + TEMA + EMA200 + VWAP + ADX must all agree
- **Regime detection**: script analyses market state and recommends buy vs sell

## Commands

```bash
# Run backtest (generates synthetic data if no CSV provided)
python3 run_backtest.py
python3 run_backtest.py --data data/banknifty_5min.csv
python3 run_backtest.py --sell-only
python3 run_backtest.py --optimize --walk-forward

# Start the bot (paper mode)
python3 main.py

# Start the bot (live — requires confirmation)
python3 main.py --live
```

## Config

- API keys: environment variables (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
- Risk params: config/settings.yaml
- Instruments: config/instruments.yaml
- Never hardcode secrets

## Known Limitations

- Backtest uses underlying index price, not actual option premiums (overestimates buy P&L, underestimates sell P&L)
- Instrument master resolution is placeholder — needs Dhan's daily CSV for live
- Zerodha broker adapter not yet built
- No real historical option premium data for accurate backtesting

## Safety Rules

- Paper mode is default. Live requires --live flag + typed confirmation
- 7 risk gates must ALL pass before any trade
- Max daily loss hard-stop (default ₹2,000)
- Forced square-off at 15:15 IST
- All timestamps in Asia/Kolkata
