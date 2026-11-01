# Backtesting Pitfalls for Indian Index Options (NIFTY/BANKNIFTY)

## 1. Liquidity Illusions and Slippage

### Takeaway
Backtests assume perfect fills at quoted prices, but real NIFTY/BANKNIFTY options experience significant slippage, especially on OTM strikes and during volatile sessions. A practitioner rule of thumb for Nifty futures is 1-2 index points per trade; options slippage can be substantially worse.

### Cited Findings
- "Most backtesting platforms do not factor in slippage accurately" and backtests assume large orders execute without moving the market — [MarketCalls](https://www.marketcalls.in/system-trading/understanding-slippage-and-its-impact-on-trading-strategy-performance.html)
- For Nifty futures backtests, "consider incorporating 1-2 points of slippage to reflect real market conditions" — [MarketCalls](https://www.marketcalls.in/system-trading/understanding-slippage-and-its-impact-on-trading-strategy-performance.html)
- A 5-lot buy on a thin book can average well above the quoted price because only 1-2 lots are available at each level — [Navia](https://navia.co.in/blog/slippage-in-trading/)
- Stop-losses can execute well below trigger prices during fast moves on Nifty expiry days — [Navia](https://navia.co.in/blog/slippage-in-trading/)
- VIX is a close proxy for slippage severity — [OneTradeJournal](https://onetradejournal.com/platforms/backtest-vs-live-results)
- An Indian options backtest worked example showed reported profit of Rs 26,250 shrinking to approximately Rs 2,031 after applying charges, slippage, and spreads — [share.market](https://www.share.market/buzz/futures-and-options/backtesting-trading-strategies-in-india/)

### Inferences
- Options slippage is likely 2-5x worse than futures slippage due to wider spreads and thinner books
- For programmatic correction: assume minimum 1-2 points slippage on ATM NIFTY options, 3-5 points on OTM strikes, and 5-10+ points during high-VIX sessions
- Expiry day slippage on weekly options is the worst case — order book depth collapses as time value erodes

### Gaps
- No peer-reviewed study quantifying actual fill slippage on NIFTY/BANKNIFTY options by moneyness
- No exchange-published execution quality data for Indian options

---

## 2. Bid-Ask Spread Impact

### Takeaway
ATM NIFTY weekly options can have spreads as tight as Rs 0.05-1.00, but OTM strikes can show Rs 1.50+ spreads, and during volatile sessions (VIX > 25), even ATM BANKNIFTY spreads have been observed at Rs 80-150. Every round-trip trade pays the spread twice.

### Cited Findings
- "In a very liquid option (like Nifty weekly ATM), you might see just a 0.05 or 0.1 difference between bid and ask" — [Sahi](https://www.sahi.com/blogs/10-role-of-market-makers-in-options)
- "For liquid options (like Nifty ATM), the bid-ask spread will be small (maybe a rupee or less). For illiquid strikes, the spread could be wide" — [ICICIdirect](https://icicidirect.com/ilearn/futures-and-options/articles/why-liquidity-matters-to-an-options-trader)
- Far OTM example: "Rs 2 bid vs Rs 3.5 ask" — a 75% spread relative to premium — [Sahi](https://www.sahi.com/blogs/8-how-to-read-an-option-chain-in)
- When VIX hit 28.91 on March 30, 2026, Bank Nifty ATM spreads "ballooned to Rs 80-150" — [Sahi](https://www.sahi.com/blogs/10-role-of-market-makers-in-options)
- ATM stock options (even large caps like Trent) can show Rs 4-5 spread — [TradingQnA](https://tradingqna.com/t/looking-for-some-sanity-check-nos-around-bid-ask-prices/169658)
- ITM options are "usually illiquid" — [Zerodha Z-Connect](https://zerodha.com/z-connect/?p=239336)
- BANKNIFTY option tick size is Rs 0.05 — [5paisa](https://www.5paisa.com/blog/bank-nifty-intraday-options)

### Inferences
- **Programmatic correction factors** (per leg, per side):
  - ATM NIFTY weekly: Rs 0.50-1.00 spread cost
  - 1-2 strikes OTM: Rs 1.00-2.00
  - Deep OTM (5+ strikes away): Rs 2.00-5.00
  - High VIX (>25) multiplier: 3-5x normal spread
  - Near expiry (<1 hour): 2-3x normal spread
- A credit spread (2 legs) pays spread cost 4 times (entry buy + entry sell + exit buy + exit sell)
- On a Rs 5 premium option, a Rs 1 round-trip spread cost is 20% of premium — devastating for low-premium strategies

### Gaps
- No systematic study of intraday bid-ask spread patterns (morning vs afternoon) for NIFTY options
- No data on how spreads change in the last 30 minutes before weekly expiry

---

## 3. STT (Securities Transaction Tax) Impact

### Takeaway
STT was revised effective October 1, 2024 (to 0.1% on options sell-side) and again on April 1, 2026 (to 0.15% on options sell-side). For option sellers, STT alone can consume 5-15% of premium collected on cheap options. Backtests spanning multiple years must apply the correct STT rate for each period.

### Cited Findings
- **Current rates (effective April 1, 2026):** Options sold: 0.15% of premium on sell side; Options exercised: 0.15% of intrinsic value — [Zerodha Support](https://support.zerodha.com/category/account-opening/charges-at-zerodha/statutory-and-exchange/articles/how-is-the-securities-transaction-tax-stt-calculated)
- **Previous rates (Oct 1, 2024 - Mar 31, 2026):** Options sold: 0.1% of premium on sell side; Options exercised: 0.125% of intrinsic value — [Zerodha Support](https://support.zerodha.com/category/account-opening/charges-at-zerodha/statutory-and-exchange/articles/how-is-the-securities-transaction-tax-stt-calculated); [Bajaj Broking](https://www.bajajbroking.in/blog/securities-transaction-tax)
- **Pre-October 2024 rates:** Options sold: 0.0625% of premium; Options exercised: 0.125% — [Zerodha Support](https://support.zerodha.com/category/account-opening/charges-at-zerodha/statutory-and-exchange/articles/how-is-the-securities-transaction-tax-stt-calculated)
- Rounding rule: "If the paise portion >= 50, round up to nearest rupee; if < 50, round down" — [Zerodha Support](https://support.zerodha.com/category/account-opening/charges-at-zerodha/statutory-and-exchange/articles/how-is-the-securities-transaction-tax-stt-calculated)

### Inferences
- **Worked example (current rates, NIFTY lot 75):**
  - Sell 1 lot ATM put at Rs 100 premium: Turnover = 75 x 100 = Rs 7,500. STT = 0.15% x 7,500 = Rs 11.25
  - Sell 1 lot cheap OTM put at Rs 5 premium: Turnover = 75 x 5 = Rs 375. STT = Rs 0.56. But fixed costs (brokerage Rs 20) dominate
  - **Exercise trap:** If you buy a call at Rs 50 and it expires ITM with intrinsic value of Rs 5,000 (75 x Rs 66.67): STT = 0.15% x 5,000 = Rs 7.50. But if intrinsic value is Rs 50,000: STT = Rs 75
- **STT rate timeline for backtests:**
  - Before Oct 2024: 0.0625% sell-side
  - Oct 2024 - Mar 2026: 0.1% sell-side
  - Apr 2026 onwards: 0.15% sell-side
- Backtests must apply the period-correct rate; using a single rate across years will be wrong by up to 2.4x

### Gaps
- Conflicting STT rates across sources for historical periods; primary SEBI gazette notifications should be cross-checked

---

## 4. Total Transaction Costs Per Trade

### Takeaway
Total round-trip cost for a NIFTY options trade is approximately Rs 45-70 per lot for a single buy-then-sell, depending on premium. For high-frequency strategies trading many lots daily, costs can consume 50%+ of gross profits.

### Cited Findings
- **Brokerage:** Zerodha flat Rs 20 per executed order; Dhan flat Rs 20 per executed order — [Zerodha Charges](https://zerodha.com/charges); [Dhan Pricing](https://dhan.co/pricing)
- **Exchange transaction charges:** NSE 0.03553% on premium; BSE 0.0325% on premium — [Zerodha Charges](https://zerodha.com/charges)
- **SEBI turnover fee:** Rs 10 per crore of turnover — [Zerodha Charges](https://zerodha.com/charges)
- **GST:** 18% on (brokerage + SEBI charges + exchange transaction charges) — [Zerodha Charges](https://zerodha.com/charges)
- **Stamp duty:** 0.003% or Rs 300 per crore, on buy side only — [Zerodha Charges](https://zerodha.com/charges)
- **NSE IPFT charge:** Rs 0.01 per crore + GST — [Zerodha Charges](https://zerodha.com/charges)
- SEBI removed exchange transaction charge rebate for all trading members — [Inc42](https://inc42.com/buzz/zerodha-mulls-ending-zero-brokerage-model)
- Zerodha may increase F&O brokerage to Rs 40 for select traders from April 2026 — [Equentis](https://www.equentis.com/blog/zerodha-doubles-intraday-fo-brokerage-to-%E2%82%B940-what-it-means-for-traders-from-april-1/)

### Inferences
- **Worked example — single NIFTY option lot, buy at Rs 100, sell at Rs 105 (lot size 75):**
  - Buy premium turnover: 75 x 100 = Rs 7,500
  - Sell premium turnover: 75 x 105 = Rs 7,875
  - Brokerage: Rs 20 (buy) + Rs 20 (sell) = Rs 40
  - STT (sell side): 0.15% x 7,875 = Rs 11.81
  - Exchange charges (both sides): 0.03553% x (7,500 + 7,875) = Rs 5.47
  - SEBI fee: negligible (Rs 10/crore)
  - Stamp duty (buy side): 0.003% x 7,500 = Rs 0.23
  - GST: 18% x (40 + 5.47 + ~0) = Rs 8.18
  - **Total cost: ~Rs 65.69**
  - **Gross profit: 75 x 5 = Rs 375**
  - **Cost as % of gross profit: ~17.5%**
- For a scalping strategy targeting Rs 2-3 moves, costs can exceed 30-40% of gross profit
- **Programmatic cost model (per lot, per side):**
  ```
  brokerage = 20  # per order
  stt_sell = 0.0015 * sell_premium * lot_size  # sell side only
  exchange = 0.0003553 * premium * lot_size  # both sides
  stamp_buy = 0.00003 * buy_premium * lot_size  # buy side only
  gst = 0.18 * (brokerage + exchange + sebi_fee)
  ```

### Gaps
- Dhan's exact current charge structure should be verified on dhan.co directly
- Physical delivery charges for options that expire ITM need separate calculation

---

## 5. Survivorship Bias in Indian Options Data

### Takeaway
Survivorship bias in Indian options backtesting manifests differently than equities: the main issues are expired strike data disappearing from free sources, lot size changes breaking position sizing assumptions, and index constituent changes affecting correlation-based strategies.

### Cited Findings
- Survivorship bias occurs when databases only contain stocks that currently exist, dropping delisted companies — [MarketCalls](https://www.marketcalls.in/llm-models/how-to-build-survivorship-bias-free-nifty-50-historical-data-using-grokbot.html)
- A backtest "already knows which companies would survive" if using current index constituents — [MarketCalls](https://www.marketcalls.in/llm-models/how-to-build-survivorship-bias-free-nifty-50-historical-data-using-grokbot.html)
- Point-in-time constituent lists should be built from NSE Indices announcements — [MarketCalls](https://www.marketcalls.in/llm-models/how-to-build-survivorship-bias-free-nifty-50-historical-data-using-grokbot.html)
- Survivorship bias can overstate annualized returns by about 4.9 percentage points in some studies — [share.market](https://www.share.market/buzz/futures-and-options/backtesting-trading-strategies-in-india/)
- NSE Bhavcopy and historical archives are recommended as primary data sources — [UTradeAlgos](https://www.utradealgos.com/blog/what-makes-a-great-backtesting-platform-in-india)

### Inferences
- **Options-specific survivorship issues:**
  - Expired weekly option chains are often unavailable from free sources (Yahoo Finance, Google Finance)
  - Strike prices that existed during a backtest period may not appear in current data downloads
  - Lot size changes (25->75 for NIFTY, 15->30 for BANKNIFTY) mean historical trades cannot be directly compared
  - Weekly expiry discontinuation (BANKNIFTY weeklies stopped Nov 2024) means strategies relying on weekly BANKNIFTY data have no forward analog
- Free data sources typically lack: tick-level option data, order book depth, historical IV surfaces
- Paid providers (Opstra, TrueData, Global Data Feeds) offer better coverage but still have gaps pre-2020

### Gaps
- No systematic comparison of data quality across Indian options data providers
- Yahoo Finance's coverage of historical NSE option chains is undocumented; assumed to be limited to recent months

---

## 6. Look-Ahead Bias in Indian Markets

### Takeaway
Indian market data has specific timing quirks that create look-ahead bias: FII/DII data is published after market close, OI data settles after hours, and the NSE data feed has known delays that backtests ignore.

### Cited Findings
- Look-ahead bias occurs when a backtest uses information not available at trade time, such as using next-day OI data or end-of-day settlement prices for intraday decisions — [share.market](https://www.share.market/buzz/futures-and-options/backtesting-trading-strategies-in-india/)
- A 2024-2026 backtest crosses multiple market regimes (pre-SEBI changes, post-SEBI changes, lot size changes, weekly expiry changes) — [share.market](https://www.share.market/buzz/futures-and-options/backtesting-trading-strategies-in-india/)

### Inferences
- **India-specific look-ahead risks:**
  - FII/DII position data: published by NSE after 5:30 PM, not available for same-day trading decisions
  - OI settlement: final OI figures are available after market close, not during trading
  - Bhavcopy timestamps: EOD data published after hours; using "close" prices for intraday signals is look-ahead
  - IV and Greeks: most free data provides EOD IV; real-time IV calculation requires live feeds
  - Index rebalancing: constituent changes are announced in advance but implemented on specific dates
- **Programmatic fixes:**
  - Lag all external signals (FII data, OI changes) by at least 1 trading day
  - Use only data available at the bar's open time, not its close
  - For IV-based strategies, use previous day's closing IV or real-time calculated IV

### Gaps
- Exact publication timestamps for NSE FII/DII data, OI settlement data not independently verified
- No study quantifying the look-ahead bias impact on Indian options strategy returns

---

## 7. Multi-Leg Strategy Backtesting (Credit Spreads, Iron Condors)

### Takeaway
Multi-leg options strategies are particularly prone to backtesting overestimation in India because backtests assume simultaneous execution of all legs at quoted prices, while real-world multi-leg fills are sequential and subject to leg risk.

### Cited Findings
- One source illustrates a backtest that assumes a 5-lot buy fills entirely at the quoted price, while in reality "only 1-2 lots are available at the top level" — [Navia](https://navia.co.in/blog/slippage-in-trading/)
- ITM options are "usually illiquid," which affects the far leg of spreads — [Zerodha Z-Connect](https://zerodha.com/z-connect/?p=239336)

### Inferences
- **Credit spread fill reality:**
  - A bull put spread requires selling ATM put + buying OTM put simultaneously
  - Backtests use mid-prices of both legs at the same timestamp
  - Real execution: sell leg fills first (at bid), then buy leg fills (at ask) — net credit is always less than backtested
  - Slippage on the protective leg (OTM, less liquid) is larger
  - Spread legging risk: market moves between leg fills can widen the spread or convert a spread into a naked position
- **Programmatic corrections for multi-leg:**
  - Add per-leg spread cost: at minimum Rs 1-2 per leg for ATM, Rs 2-5 for OTM
  - Assume 0.5-2 second delay between legs for sequential execution
  - Total cost for a 2-leg spread: 4x spread cost + 2x brokerage + slippage on each leg
  - For iron condors (4 legs): 8x spread cost + 4x brokerage
- **Margin considerations:**
  - SPAN margin + exposure margin required for short options
  - Hedged positions get margin benefit, but brokers may require full margin before the hedge leg fills
  - 2% extra ELM on expiry day short options (SEBI 2024 rule)

### Gaps
- No published data on average time between multi-leg fills on NSE
- No study comparing backtested vs actual fill quality for option spreads in India

---

## 8. NSE Circuit Breakers and Gap Risk

### Takeaway
Market-wide circuit breakers at 10%, 15%, and 20% halt all equity and derivative trading simultaneously. Backtests cannot capture the gap risk on resumption, and options positions are frozen during halts with no ability to adjust.

### Cited Findings
- SEBI's index-based market-wide circuit breaker triggers at 10%, 15%, and 20% movement of Sensex/Nifty, calculated on previous day's close — [5paisa](https://www.5paisa.com/finschool/stock-market-circuit-breakers-in-india/)
- "Your hedge in Nifty options is frozen at exactly the moment you most want to adjust it" — [OneTradeJournal](https://onetradejournal.com/learn/circuit-limits-explained)
- "Once trading resumes the price can gap straight through your intended exit" — [OneTradeJournal](https://onetradejournal.com/learn/circuit-limits-explained)
- On March 23, 2020, Sensex fell 10% triggering a 45-minute halt; selling continued after resumption — [5paisa](https://www.5paisa.com/finschool/stock-market-circuit-breakers-in-india/)
- A 20% circuit breaker closes the market for the rest of the session — [OneTradeJournal](https://onetradejournal.com/glossary/market-crash)

### Inferences
- **Backtesting blind spots:**
  - OHLC data during a circuit breaker day shows a smooth candle; the reality is a halt + gap
  - Stop-losses set during backtests execute at the stop price; real stops execute at the resumption price (potentially far worse)
  - Short option sellers face margin calls during halts with no ability to close
  - Backtested max drawdown during March 2020 likely understates real drawdown by 20-50%
- **Programmatic corrections:**
  - Flag all days where intraday range exceeded 3% for manual review
  - On circuit breaker days, assume stop-losses execute at 2x the intended loss
  - Add tail-risk scenarios: model a 10% gap-down with no exit possible for 45 minutes
  - Size positions assuming circuit breaker events occur 1-2x per year

### Gaps
- Current halt duration rules (post-2020 revisions) need verification from NSE/SEBI primary documents
- No quantification of how much circuit breaker gap risk understates max drawdown in options backtests

---

## 9. Data Quality: Yahoo Finance vs NSE vs Broker Data

### Takeaway
Free data sources (Yahoo Finance) have significant gaps for Indian options backtesting. NSE Bhavcopy is the gold standard for EOD data but lacks tick-level granularity. Paid providers vary in quality and coverage.

### Cited Findings
- NSE's EOD Bhavcopy and historical archives are recommended as primary data sources — [UTradeAlgos](https://www.utradealgos.com/blog/what-makes-a-great-backtesting-platform-in-india)
- "Platforms connected to NSE and BSE now offer years of tick-by-tick and end-of-day data" — [UTradeAlgos](https://www.utradealgos.com/blog/what-makes-a-great-backtesting-platform-in-india)

### Inferences
- **Data source comparison:**
  | Source | Options History | Tick Data | OI Data | Greeks/IV | Cost |
  |--------|----------------|-----------|---------|-----------|------|
  | Yahoo Finance | Limited, no chain history | No | No | No | Free |
  | NSE Bhavcopy | EOD, all strikes | No | EOD OI | No | Free |
  | Opstra | Multi-year | No | Yes | Pre-computed | Paid |
  | TrueData | Multi-year | Yes (1-min) | Yes | Yes | Rs 2,000+/mo |
  | Global Data Feeds | Multi-year | Yes (tick) | Yes | Yes | Rs 3,000+/mo |
  | Broker APIs (Dhan/Zerodha) | 1-2 years | 1-min candles | Yes | Limited | With account |
- **Common data quality issues:**
  - Yahoo Finance: missing strikes, delayed updates, no expired options chains, no intraday
  - NSE Bhavcopy: reliable but EOD only; requires building your own database
  - Broker historical data: limited lookback period (typically 1-2 years)
  - All sources: options data from before 2020 is patchy
- **Recommendation:** Use NSE Bhavcopy for EOD backtests, paid provider for intraday. Never use Yahoo Finance for options backtesting.

### Gaps
- No independent audit comparing data quality across Indian options data providers
- Historical coverage depth for each paid provider not systematically documented

---

## 10. SEBI 2024-2025 Regulatory Changes Impact on Backtests

### Takeaway
SEBI's 2024-2025 changes fundamentally altered the options trading landscape: lot sizes tripled, weekly expiries were restricted to NIFTY only, and margins increased. Any backtest spanning this period must account for these structural breaks or it is meaningless.

### Cited Findings
- SEBI raised contract notional value to Rs 15-20 lakh from Rs 5-10 lakh — [Zerodha Z-Connect](https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing)
- NIFTY lot size changed from 25 to 75; BANKNIFTY from 15 to 30 — [Zerodha Z-Connect](https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing)
- Weekly derivatives discontinued for BANKNIFTY (last weekly expiry November 2024) — [Motilal Oswal](https://www.motilaloswal.com/learning-centre/2024/10/nse-discontinues-weekly-derivatives-on-bank-nifty-nifty-midcap-select-and-finnifty/)
- 2% extreme loss margin (ELM) added on expiry-day short options — [Motilal Oswal](https://www.motilaloswal.com/learning-centre/2024/10/nse-discontinues-weekly-derivatives-on-bank-nifty-nifty-midcap-select-and-finnifty/)
- Example: Nifty 27,000 call with 25 lot needs extra Rs 12,500 (27,000 x 25 x 2%) on expiry day — [Motilal Oswal](https://www.motilaloswal.com/learning-centre/2024/10/nse-discontinues-weekly-derivatives-on-bank-nifty-nifty-midcap-select-and-finnifty/)
- NSE revised lot sizes again effective October 28, 2025 (new lot numbers not confirmed in search) — [Sahi](https://sahi.com/blogs/nse-cuts-lot-sizes-in-nifty-bank)
- SEBI removed exchange transaction charge rebate for trading members — [Inc42](https://inc42.com/buzz/zerodha-mulls-ending-zero-brokerage-model)
- Expiry day moved from Thursday to Tuesday for NIFTY — [share.market](https://www.share.market/buzz/futures-and-options/backtesting-trading-strategies-in-india/)

### Inferences
- **Backtest structural breaks to handle:**
  - Lot size changes: normalize all P&L to per-lot or per-unit basis
  - Weekly vs monthly: BANKNIFTY strategies using weekly expiry data before Nov 2024 have no equivalent going forward
  - Margin regime: same strategy requires 2-3x more capital post-changes
  - Expiry day change: Thursday patterns (historically backtested) do not apply to Tuesday expiries
  - STT rate changes: three different rates across 2023-2026
- **Programmatic handling:**
  ```python
  # Lot size timeline
  NIFTY_LOT = {
      'before_nov_2024': 25,
      'nov_2024_to_oct_2025': 75,
      'after_oct_2025': 'check_nse_circular'  # revised again
  }
  BANKNIFTY_LOT = {
      'before_nov_2024': 15,
      'after_nov_2024': 30
  }
  # Weekly expiry availability
  BANKNIFTY_WEEKLY_AVAILABLE = date < datetime(2024, 11, 20)  # approximate
  ```

### Gaps
- October 2025 revised lot sizes for NIFTY and BANKNIFTY not confirmed
- Exact effective dates for all changes should be verified against NSE circulars

---

## 11. Realistic Fill Assumptions

### Takeaway
Market orders get filled but with slippage; limit orders may not fill at all. Backtests using limit-order assumptions overstate returns by counting trades that would never have executed.

### Cited Findings
- Backtests assume orders fill at the backtested price, but "live orders can move the market or fill only partly" — [OneTradeJournal](https://onetradejournal.com/platforms/backtest-vs-live-results)
- For a strategy targeting Rs 10 moves with Rs 5 slippage per trade, "the effective profit is halved" — [MarketCalls](https://www.marketcalls.in/system-trading/understanding-slippage-and-its-impact-on-trading-strategy-performance.html)

### Inferences
- **Fill model for backtests:**
  - Market orders: assume fill at worst of (bid/ask + slippage buffer)
  - Limit orders: assume fill only if price trades through the limit by at least 1 tick
  - Partial fills: on OTM options with < 500 OI, assume only 50% fill probability
  - Time-of-day: first 15 minutes and last 15 minutes have the widest spreads and worst fills
  - Weekly expiry day: liquidity concentrates on the current week's strikes; next week is thin
- **Programmatic fill simulator:**
  ```python
  def realistic_fill_price(side, quoted_price, spread, vix, oi, is_expiry_day):
      base_slippage = spread / 2  # half-spread minimum
      vix_multiplier = max(1, vix / 15)  # 15 is "normal" VIX
      liquidity_penalty = 0 if oi > 1000 else (2.0 if oi > 100 else 5.0)
      expiry_penalty = 1.5 if is_expiry_day else 1.0
      total_slippage = (base_slippage + liquidity_penalty) * vix_multiplier * expiry_penalty
      if side == 'buy':
          return quoted_price + total_slippage
      else:
          return quoted_price - total_slippage
  ```

### Gaps
- No NSE data on partial fill rates by strike and moneyness
- No study on optimal order type (market vs limit) for Indian options algo execution

---

## 12. Backtest Metrics Reliability

### Takeaway
No single metric is reliable for Indian options backtests. Sharpe ratio ignores tail risk (critical for options), profit factor can be inflated by a few large wins, and trade count matters more than most traders realize. Use a combination of Sortino ratio, max drawdown, expectancy, and trade count.

### Cited Findings
- "A profit factor alone is not enough" and backtests should use linked metrics — [Backtrex](https://backtrex.com/en/blog/how-to-read-backtesting-results-metrics)
- Sharpe "doesn't distinguish between upside and downside volatility" — paired with Sortino ratio — [ORATS](https://orats.com/university/measuring-performance)
- Profit factor above 2.0 on historical data "should raise suspicion" — [LuxAlgo](https://www.luxalgo.com/blog/top-7-metrics-for-backtesting-results/)
- 30+ trades minimum for reliability, 100+ for confidence — [LuxAlgo](https://www.luxalgo.com/blog/top-7-metrics-for-backtesting-results/)
- Expectancy described as "the metric most directly tied to long-term profitability" — [Backtrex](https://backtrex.com/en/blog/how-to-read-backtesting-results-metrics)

### Inferences
- **Options-specific metric issues:**
  - Sharpe ratio for option selling strategies is artificially high: many small wins + rare large losses = high Sharpe until the tail event
  - Win rate alone is misleading: credit spreads can have 80%+ win rate but still lose money
  - Profit factor must be calculated after all transaction costs
  - Max drawdown should be calculated on a per-trade basis, not just peak-to-trough of equity curve
- **Recommended metric stack:**
  1. Sortino ratio (penalizes downside only) > 1.5
  2. Profit factor (after all costs) > 1.3
  3. Max drawdown < 20% of capital
  4. Trade count > 200 for statistical significance
  5. Expectancy per trade > 2x total cost per trade
  6. Tail ratio: average winning trade / average losing trade (for option sellers, this should be scrutinized)
  7. Calmar ratio (annual return / max drawdown)

### Gaps
- No study specific to Indian options establishing which metrics best predict forward performance

---

## 13. Backtest Return Haircut / Degradation Factor

### Takeaway
The practitioner consensus is to haircut backtested returns by 40-60%. For Indian options specifically, the combination of higher transaction costs, wider spreads, and regulatory changes suggests a 50-70% haircut is more appropriate.

### Cited Findings
- "Practitioners commonly discount a backtest's reported Sharpe ratio by half" (the 50% rule of thumb) — [HarbourFront Quant](https://harbourfrontquant.substack.com/p/why-backtests-decay-regime-dependence)
- Harvey and Liu argue the usual 50% haircut can "excessively penalize strategies with high Sharpe ratios" and propose a framework based on number of tests run — [Duke University (Harvey)](https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF)
- Backtested strategies experience "approximately 2% to 3% per year" performance decay in live trading — [HarbourFront Quant](https://harbourfrontquant.substack.com/p/why-backtests-decay-regime-dependence)
- An Indian options backtest example showed Rs 26,250 profit shrinking to Rs 2,031 after realistic adjustments (~92% reduction) — [share.market](https://www.share.market/buzz/futures-and-options/backtesting-trading-strategies-in-india/)

### Inferences
- **Indian options-specific degradation factors:**
  - Transaction cost drag: -15 to -25% (higher than US markets due to STT)
  - Slippage and spread: -10 to -20%
  - Regime change (SEBI rules): -5 to -15%
  - Overfitting / curve-fitting: -10 to -20%
  - Execution reality (partial fills, delays): -5 to -10%
  - **Cumulative realistic haircut: 50-70% of backtested gross returns**
- **Programmatic degradation model:**
  ```python
  def apply_backtest_haircut(backtested_return, num_parameters, backtest_years):
      cost_drag = 0.20  # 20% for transaction costs
      slippage_drag = 0.15  # 15% for slippage/spread
      overfit_penalty = min(0.05 * num_parameters, 0.30)  # more params = more overfit
      regime_penalty = 0.10 if backtest_years > 2 else 0.05
      execution_drag = 0.08
      total_haircut = 1 - (1-cost_drag)*(1-slippage_drag)*(1-overfit_penalty)*(1-regime_penalty)*(1-execution_drag)
      return backtested_return * (1 - total_haircut)
  ```
- **Rule of thumb:** If a backtest shows 100% annual return, expect 30-50% live. If it shows 30%, expect 10-15%.
- A strategy that is not profitable after a 60% haircut should not be traded live.

### Gaps
- The 92% reduction in the Indian example may be an extreme case; no systematic study confirms this level of degradation
- Harvey and Liu's framework has not been applied specifically to Indian options markets
