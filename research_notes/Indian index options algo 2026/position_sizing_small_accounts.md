# Position Sizing for Small Accounts (₹1-5 Lakh) Trading Indian Index Options

## Kelly Criterion for Options Trading

### Takeaway
The standard Kelly formula f* = W − (1−W)/R works for binary outcomes but must be adapted for options where payoffs are continuous; practitioners use fractional Kelly (quarter to half) because full Kelly produces extreme drawdowns and relies on precise probability estimates that are rarely available.

### Cited Findings
- The simplified Kelly formula for trading is f* = W − (1−W)/R, where W = win rate and R = average win / average loss. The general binary form is f* = (bp − q)/b where b is net profit per unit staked — [Fyers](https://fyers.in/blog/what-is-the-kelly-criterion/); [FlashAlpha](https://flashalpha.com/concepts/kelly-criterion)
- Worked example: 55% win rate, +9% avg win, −6% avg loss gives R=1.5, f*=0.25 (25% of capital) — which is dangerously large for real trading — [Ryan O'Connell Finance](https://ryanoconnellfinance.com/kelly-criterion/)
- Standard Kelly assumes binary win/loss, which doesn't fit options well. A call option can expire worthless, finish slightly ITM (partial loss after premium), or deep ITM. One approach integrates over a lognormal price distribution using spot, strike, DTE, IV, premium, and expected return — [FlashAlpha Options API](https://flashalpha.com/articles/position-sizing-api-build-kelly-criterion-options-calculator)
- Retail traders should be cautious estimating probabilities, especially in options where volatility changes rapidly — [StockAlarm](https://pro.stockalarm.io/blog/kelly-criterion-position-sizing-guide)
- With a ~47% win rate, Kelly output is highly sensitive to the payoff ratio R. At W=0.47, R=1.5: f*=0.47−0.53/1.5=0.117 (11.7%); at R=2.0: f*=0.47−0.53/2.0=0.205 (20.5%). Small estimation errors in R cause large swings in recommended size — derived from the formula

### Inferences
- For a regime-dependent payoff ratio, Kelly should be recalculated per regime (trending vs ranging) using rolling estimates of W and R from the last 30-50 trades in that regime
- With ₹1-5L accounts and lot size constraints, Kelly output often exceeds what can practically be traded (1 lot may already be >f*), making fractional Kelly moot — the constraint is the lot size floor, not the Kelly ceiling

### Gaps
- No peer-reviewed source found for options-specific Kelly (lognormal integration method); only vendor implementations
- No data on Kelly performance specifically for Indian index options with their fee structure

---

## Fractional Kelly: Optimal Fraction for Small Accounts

### Takeaway
Half-Kelly (0.5×f*) cuts portfolio volatility roughly in half while reducing expected geometric growth by only ~25%; quarter-Kelly (0.25×f*) is more appropriate when edge estimates are uncertain or based on fewer than 100 trades.

### Cited Findings
- The growth penalty for betting above the true optimum is severe, while the penalty for betting below is mild — this asymmetry favors underbetting — [LuxAlgo](https://www.luxalgo.com/library/concept/kelly-criterion/)
- Half-Kelly cuts volatility roughly in half while reducing expected growth by ~25% (simplified model) — [LuxAlgo](https://www.luxalgo.com/library/concept/kelly-criterion/)
- Without trade history, use conservative estimates: 40-50% win rate with 1.5-2.0 R for swing trading — [Position Sizer Tool](https://fffinstill.com/tools/position-sizer)
- A win-rate estimate from 100 trades has a standard error of roughly ±5%, which is large relative to a typical edge — [LuxAlgo](https://www.luxalgo.com/library/concept/kelly-criterion/)
- If edge is zero or negative, optimal bet is zero — do not trade — [MetricGate](https://metricgate.com/calculator/kelly-criterion-optimal-fraction)

### Inferences
- For accounts under ₹5L with <100 historical trades: use quarter-Kelly (0.25×f*) as the starting fraction
- For accounts with 100+ trades and validated edge: half-Kelly (0.5×f*) is appropriate
- Practical application: if f*=12% and using quarter-Kelly, risk 3% per trade. On ₹2L account, that's ₹6,000 max loss per trade
- Given lot size constraints (NIFTY 65 lots = ~₹6,500-13,000 premium for ATM options), quarter-Kelly may still mean 1 lot is the maximum position

### Gaps
- No empirical study comparing fractional Kelly outcomes specifically on Indian index options
- Optimal fraction likely varies by strategy type (directional buy vs credit spread) but no source addressed this

---

## Anti-Martingale Sizing and House Money Scaling

### Takeaway
Anti-martingale sizing (increase size with equity, decrease in drawdown) is the consensus approach, but no standard multiplier schedule exists; practical implementations use fixed-fraction-of-equity rather than consecutive-win multipliers, with drawdown brakes to halt or reduce trading.

### Cited Findings
- Anti-martingale methods increase position size as profits accrue and decrease as equity drops during drawdown — this takes advantage of positive expectancy in a winning system — [LuxAlgo](https://www.luxalgo.com/library/concept/martingale-anti-martingale.md)
- Martingale (doubling after losses) produces a high win rate and smooth equity curve right up to catastrophic failure — [LuxAlgo](https://www.luxalgo.com/library/concept/martingale-anti-martingale.md)
- Sizing does not create positive expectancy — it only amplifies whatever expectancy the signal has — [TradingView Martingale Simulator](https://tw.tradingview.com/script/WY0XsGrf-Martingale-Strategy-Simulator-BackQuant/)
- A ProRealCode forum example scales size up or down only when win or loss streaks exceed 3 trades — [ProRealCode](https://www.prorealcode.com/topic/win-loss-streak/)
- Reset size after a few wins rather than waiting for a streak to end, since one loss at the end of a streak gives back a lot — [TradingView Grid Script](https://in.tradingview.com/script/oxvR5vMy-Grid-Like-Strategy/)

### Inferences
- **Practical "house money" implementation for ₹1-5L accounts:**
  - Base: fixed fraction of current equity (e.g., 1% of equity per trade)
  - After 3 consecutive wins: increase to 1.5× base size (still capped at max risk %)
  - After 5 consecutive wins: increase to 2× base size
  - Any single loss: reset to base size immediately
  - Drawdown >5% from peak equity: reduce to 0.5× base size
  - Drawdown >10% from peak: halt trading, review strategy
- This is synthesized from multiple sources, not a validated framework — backtest before deploying

### Gaps
- No authoritative source provides specific consecutive-win multipliers or thresholds
- No research on anti-martingale performance in Indian index options specifically
- The interaction between lot size constraints and anti-martingale scaling is unexplored — on small accounts, "scaling up" may mean going from 1 lot to 2, a 100% size increase rather than a gradual scale

---

## Sizing Credit Spreads vs Directional Option Buys

### Takeaway
Credit spreads have defined max loss (strike width minus credit received) and should be sized to that max loss, not premium collected; directional buys risk the entire premium but have no margin requirement beyond it. The key difference is that spread losses are typically 3-4× larger than wins, requiring higher win rates.

### Cited Findings
- Credit spread max loss = (short strike − long strike) − net credit received; max profit = net credit — [Wikipedia](https://en.wikipedia.org/wiki/Credit_spread_(options)); [Strike.money](https://www.strike.money/options/credit-spread)
- Credit spreads have defined maximum loss parameters, unlike naked options which expose traders to unlimited risk (calls) or near-unlimited risk (puts) — [Strike.money](https://www.strike.money/options/credit-spread)
- Loss-to-gain ratios on credit spreads are often 3-4×: for every ₹100 of income, risking ₹800-1,000 — [EliteTrader Forum](https://www.elitetrader.com/et/threads/is-this-always-true.347124/)
- Indian brokerages typically require margin of 40-60% of maximum loss potential for credit spreads — [Bajaj Finserv Securities](https://corpwp.bajajfinservsecurities.in/?p=9344)
- A 90% probability trade still loses 10% of the time, and those losses are multiples of the wins — expectancy, not hit rate, determines edge — [Tapeboard](https://tapeboard.com/glossary/what-is-a-credit-spread)

### Inferences
- **Directional option buys (long calls/puts):**
  - Risk = premium paid (known, capped)
  - Size rule: risk per trade = premium × lot size ≤ X% of capital
  - For ₹2L account at 1% risk: max premium outlay = ₹2,000 per trade
  - Win rate ~40-50%, but winners can be 2-5× premium when directional bets work
- **Credit spreads:**
  - Risk = max loss per spread × number of lots
  - Size rule: max loss per position ≤ X% of capital
  - For ₹2L account at 2% risk: max loss = ₹4,000 per spread position
  - Must also ensure margin availability (40-60% of max loss as margin)
  - Higher win rate (~60-70%) but smaller per-trade gains
- **Portfolio rule:** Total max loss across all open positions should not exceed 5-6% of capital

### Gaps
- No source specifically addressed sizing rules for Indian index option spreads with current margin framework
- Interaction between SPAN margin, exposure margin, and position sizing is not well documented in public sources

---

## Risk Parity Across Multiple Simultaneous Positions

### Takeaway
Risk parity allocates by risk contribution rather than capital; for correlated indices like NIFTY and BANKNIFTY, the practical approach is inverse-volatility weighting with a correlation adjustment, since correlated positions act as one larger bet.

### Cited Findings
- Risk parity aims for each asset to contribute equally to total portfolio risk — [arXiv 2110.06464](https://arxiv.org/pdf/2110.06464)
- The simpler inverse-volatility method matches full risk parity only when all pairwise correlations are equal — [LuxAlgo](https://www.luxalgo.com/library/concept/portfolio-aware-sizing.md)
- Cut size as correlation rises, since correlated positions can act like one larger bet — [LuxAlgo](https://www.luxalgo.com/library/concept/portfolio-aware-sizing.md)
- Risk parity is vulnerable to correlation regime shifts — [Wikipedia](https://en.wikipedia.org/wiki/Risk_parity)

### Inferences
- **NIFTY + BANKNIFTY correlation:** Typically 0.85-0.95. Treat them as ~90% correlated for sizing purposes
- **Practical allocation for ₹1-5L accounts:**
  - Total portfolio risk budget: 3% of capital per day
  - If trading only NIFTY: full 3% budget available
  - If trading NIFTY + BANKNIFTY simultaneously: allocate based on inverse volatility, but reduce total to ~2% (correlation penalty)
  - Simple rule: with 2 correlated positions, each gets 40% of budget (not 50%), leaving 20% buffer for correlation
  - With 3 positions (NIFTY + BANKNIFTY + SENSEX): each gets 25% of budget, total capped at 75% of single-position budget
- **Volatility weighting:** BANKNIFTY is typically 1.3-1.5× more volatile than NIFTY. If budget is ₹6,000 (3% of ₹2L):
  - NIFTY allocation: ₹6,000 × (1/1.0) / (1/1.0 + 1/1.4) = ~₹3,500
  - BANKNIFTY allocation: ₹6,000 × (1/1.4) / (1/1.0 + 1/1.4) = ~₹2,500
  - Then apply correlation discount: reduce both by ~10-15%

### Gaps
- No source covered risk parity specifically for Indian index options with lot size and margin constraints
- The discrete lot sizes make smooth risk parity allocation impossible for small accounts — you either have 1 lot or 0

---

## Dynamic Sizing Based on Recent Performance

### Takeaway
The consensus approach is equity-curve trading: reduce size when equity is below its moving average (drawdown state) and restore or increase when above; specific algorithms use drawdown thresholds and buffer-to-stop ratios rather than fixed rules.

### Cited Findings
- Adapt size to current market volatility (ATR), recent strategy performance (rolling R-multiples), and current drawdown state — [NexusFi](https://nexusfi.com/a/risk-management/dynamic-position-sizing)
- Buffer-to-stop ratio: before each session, divide distance to drawdown floor by typical stop. If under 3, reduce size until the ratio recovers — [FXStreet](https://www.fxstreet.com/education/position-sizing-after-a-loss-why-the-buffer-matters-more-than-the-stop-202609210543)
- If expectancy degrades, cut size until the edge rebuilds — [InternationalTradingInstitute](https://internationaltradinginstitute.com/blog/dynamic-position-sizing-and-risk-management-in-volatile-markets/)
- Tightening stops from 3×ATR to 1×ATR raised total gain but cut win rate from 67% to 45% and produced more pronounced drawdowns — sizing and stop placement interact — [ProRealCode](https://www.prorealcode.com/topic/thoughts-observations-on-stop-losses-and-dynamic-positions-sizing/)

### Inferences
- **Concrete algorithm for ₹1-5L accounts:**
  ```
  equity_sma = 20-trade SMA of equity curve
  peak_equity = rolling maximum of equity
  drawdown_pct = (peak_equity - current_equity) / peak_equity
  
  if current_equity > equity_sma:
      size_multiplier = 1.0  # normal sizing
  elif drawdown_pct < 0.05:
      size_multiplier = 0.75  # mild pullback
  elif drawdown_pct < 0.10:
      size_multiplier = 0.50  # significant drawdown
  elif drawdown_pct < 0.15:
      size_multiplier = 0.25  # severe drawdown
  else:
      size_multiplier = 0.0   # halt trading, review everything
  
  # After recovery above SMA for 3+ trades:
  if consecutive_wins >= 3 and current_equity > equity_sma:
      size_multiplier = min(1.25, size_multiplier * 1.25)
  ```
- This is synthesized, not from a single validated source

### Gaps
- No empirical study measuring the impact of equity-curve-based sizing on Indian index options strategies
- The interaction between dynamic sizing and lot size constraints is not addressed anywhere

---

## Optimal Risk Per Trade: 0.5% vs 1% vs 2%

### Takeaway
For small accounts (<₹5L) with unvalidated strategies, 0.5-1% per trade is the safe range; 2% is the commonly cited ceiling but produces 14%+ drawdowns on a 7-loss streak. The right choice depends on win rate, payoff ratio, and account holder's drawdown tolerance.

### Cited Findings
- Traders with a proven, positive-expectancy system typically stay at 1% or less before considering more — [TradeThatSwing](https://tradethatswing.com/how-much-you-should-risk-per-trade-is-the-standard-1-bs/)
- 2% is a ceiling used as a "golden rule" by some options educators — [Montreal Exchange](https://www.m-x.ca/en/insights?id=99)
- Risking above 2% will most likely produce a drawdown of 20% minimum — [TradingView](https://my.tradingview.com/chart/BTCUSD/ci2edTeE-Chapter-1-1-rule)
- On a 7-loss streak: 2% risk = 14% drawdown, 1% = 7%, 0.5% ≈ 3.5% — [BabyPips Forum](https://forums.babypips.com/t/risk-per-trade-percentage/1231486)
- A trader with 30% win rate has far higher odds of extended losing streaks than one with 50% — [Optimus Futures](https://optimusfutures.com/blog/?p=9924)

### Inferences
- **For ₹1L account:** At 1% risk, max loss per trade = ₹1,000. NIFTY option premium for 1 lot ATM ≈ ₹8,000-15,000. So 1% risk means you can't afford 1 lot unless the stop loss is set at ₹1,000 (a fraction of premium). This forces either: (a) trading far OTM options with smaller premiums, (b) using wider risk percentages, or (c) trading only when premium is cheap (low VIX)
- **For ₹5L account:** At 1% risk = ₹5,000 max loss per trade. Can afford 1 lot NIFTY with a stop, but not much room for error
- **Recommended tiering:**
  - Strategy in testing (first 50 trades): 0.5% risk
  - Strategy validated (50-200 trades, positive expectancy confirmed): 1% risk
  - Strategy mature (200+ trades, multiple market regimes): up to 1.5% risk
  - Never exceed 2% on any single trade regardless of conviction

### Gaps
- No study on optimal risk percentage specifically for Indian index options with their fee structure (STT, brokerage, GST)
- The lot size floor creates a minimum position size that may exceed the desired risk percentage for small accounts

---

## Prop Firm Position Sizing Rules

### Takeaway
Prop firms typically enforce 4-5% daily loss limits and 8-12% max drawdown, with per-trade risk of 1-2%; the core sizing method is dividing the daily budget by per-trade risk to determine maximum concurrent positions.

### Cited Findings
- Daily loss limits typically run 4-5% of starting balance — [TradeZella](https://www.tradezella.com/blog/pass-prop-firm-challenge)
- Per-trade risk in funded accounts is typically 1-2% — [TradeZella](https://www.tradezella.com/blog/pass-prop-firm-challenge)
- Maximum consecutive losers = Daily Drawdown Amount / Risk Per Trade — [NYC Servers Blog](https://newyorkcityservers.com/blog/prop-firm-daily-drawdown-rules)
- Static drawdown is a fixed floor; trailing drawdown can cut buffer even while a trade is profitable — [FundedTrading](https://fundedtrading.com/best-tools-for-low-position-trading)
- Size to volatility (ATR, event risk); reduce contracts in fast markets — [NinjaTrader](https://prop.ninjatrader.com/blogs/how-to-manage-risk-in-prop-trading/)

### Inferences
- **Applying prop firm rules to ₹2L account:**
  - Daily loss cap: 4% = ₹8,000
  - Per-trade risk: 1.5% = ₹3,000
  - Max simultaneous losing trades: ₹8,000 / ₹3,000 ≈ 2-3 positions
  - Max trailing drawdown: 10% = ₹20,000 (halt all trading if equity drops below ₹1,80,000)
  - On event days (RBI policy, budget, expiry): reduce to 0.5% per trade or sit out

### Gaps
- Indian prop firms are not well documented in public sources
- Margin requirements for options selling in India (SPAN + exposure) are much higher than international prop firm models, limiting applicability

---

## Ruin Probability Calculations

### Takeaway
The classic ruin formula P(ruin) = ((1−a)/(1+a))^N shows that at 1% risk per trade with a small edge, ruin probability is negligible; at 5% risk, it climbs above 1%. For real options trading with unequal payoffs, Monte Carlo simulation is more reliable than the closed-form formula.

### Cited Findings
- Classic formula: P(ruin) = ((1−a)/(1+a))^N, where a = edge per unit risk, N = capital / risk per trade — [Backtrex](https://backtrex.com/en/blog/probability-of-ruin-trading-monte-carlo)
- Worked example: 55% win rate, 1:1 R/R, 2% risk on $10,000: ruin ≈ 0.008%. At 5% risk: ≈1.2% — [Backtrex](https://backtrex.com/en/blog/probability-of-ruin-trading-monte-carlo)
- Formula holds only for equal-sized wins and losses with constant win probability — [LuxAlgo](https://www.luxalgo.com/library/concept/risk-of-ruin.md)
- Doubling risk per trade roughly quadruples ruin probability for near-breakeven traders — [TradersSecondBrain](https://traderssecondbrain.com/guides/risk-of-ruin-math)
- Monte Carlo: resample trades, scale size to equity, flag any run breaching ruin threshold. Example: 312 ruins out of 10,000 simulations = 3.12% ruin probability — [Backtrex](https://backtrex.com/en/blog/probability-of-ruin-trading-monte-carlo)
- A 50% loss needs a 100% gain to recover — [NewsGhana](https://www.newsghana.com.gh/why-traders-blow-up-winning-strategies-the-hidden-math-of-risk-of-ruin/)

### Inferences
- **For ₹2L account, targeting <1% probability of 50% drawdown (losing ₹1L):**
  - At W=47%, R=1.5: edge a = (0.47×1.5 − 0.53×1.0)/1.0 = 0.175
  - N = drawdown limit / risk per trade. For 50% drawdown: N = ₹1,00,000 / risk per trade
  - P(ruin) = ((1−0.175)/(1+0.175))^N = (0.703)^N
  - At 1% risk (₹2,000): N=50, P = 0.703^50 ≈ 2.2 × 10^-8 (negligible)
  - At 2% risk (₹4,000): N=25, P = 0.703^25 ≈ 1.5 × 10^-4 (still very low)
  - At 5% risk (₹10,000): N=10, P = 0.703^10 ≈ 0.028 (2.8% — exceeds 1% threshold)
  - **Conclusion: at 1-2% risk per trade, ruin probability is negligible. At 5%, it exceeds the 1% threshold.**
- Caveat: these calculations assume the 47% win rate and 1.5 R/R are accurate. With estimation error, actual ruin probability is higher. Monte Carlo with 10,000+ runs on actual trade data is the reliable method.

### Gaps
- The closed-form doesn't account for options-specific risks (gap risk, early assignment, VIX spikes)
- No ruin probability calculation specific to Indian index options with their fee drag (STT ~0.05% of turnover on options exercise)

---

## NSE Lot Size Constraints and Their Impact on Kelly

### Takeaway
Current lot sizes (NIFTY=65, BANKNIFTY=30) create a minimum position size that dominates sizing decisions for accounts under ₹3L — Kelly and fractional Kelly become irrelevant when you can only trade 1 lot, and the real decision is whether that 1 lot exceeds your risk budget.

### Cited Findings
- NIFTY 50 lot size: 65 units as of January 2026 (down from 75) — [Sahi](https://sahi.com/blogs/nse-derivative-lot-size-changes-what-you-need-to-know-from-december); [AlgoTest](https://algotest.in/blog/nifty-lot-size.md)
- BANKNIFTY lot size: 30 units (went 15→30 in Nov 2024, briefly 35, back to 30 from Jan 2026) — [AlgoTest](https://algotest.in/blog/bank-nifty-lot-size-2026-current-quantity-and-one-lot-cost/)
- BANKNIFTY no longer has weekly expiry after SEBI Nov 2024 changes; NIFTY keeps weekly — [Sahi](https://www.sahi.com/blogs/nifty-lot-size-2026-bank-nifty-sensex)
- NSE revises lot sizes approximately every six months — [Business Standard](https://www.business-standard.com/amp/markets/capital-market-news/nse-revises-market-lot-sizes-for-nifty-bank-nifty-mid-select-f-o-contracts-125033100440_1.html)

### Inferences
- **Minimum position sizes (approximate, Oct 2026):**
  - NIFTY ATM option (₹200 premium): 65 × ₹200 = ₹13,000 per lot
  - NIFTY OTM option (₹50 premium): 65 × ₹50 = ₹3,250 per lot
  - BANKNIFTY ATM option (₹300 premium): 30 × ₹300 = ₹9,000 per lot
  - BANKNIFTY OTM option (₹80 premium): 30 × ₹80 = ₹2,400 per lot
- **Impact on Kelly for small accounts:**
  - ₹1L account at quarter-Kelly f=3%: risk budget = ₹3,000. Can only afford 1 lot of cheap OTM options
  - ₹2L account at half-Kelly f=6%: risk budget = ₹12,000. Can afford 1 lot ATM or 2-3 lots OTM
  - ₹5L account at half-Kelly f=6%: risk budget = ₹30,000. Can afford 2 lots ATM NIFTY or 3 lots BANKNIFTY
  - **Below ₹3L, position sizing is effectively binary: trade 1 lot or don't trade**
- **Fee drag on small positions:**
  - Brokerage: ₹20 flat per order (Dhan/Zerodha)
  - STT on option buy: 0.1% of premium on exercise (0.0625% on sell)
  - GST: 18% on brokerage
  - SEBI charges, exchange charges: ~₹3-5 per lot
  - On a ₹3,000 option trade: total costs ≈ ₹50-80, or 1.7-2.7% of position — significant drag

### Gaps
- Current SPAN + exposure margin requirements for option selling are not publicly documented in a way that allows precise calculations without broker tools
- Lot sizes may change again by the time this is implemented — need to parameterize, not hardcode

---

## Summary: Practical Position Sizing Framework for ₹1-5L Indian Index Options

### Recommended Framework

```
1. CALCULATE KELLY:
   f* = W - (1-W)/R
   Use rolling 50-trade estimates of W and R per regime

2. APPLY FRACTIONAL KELLY:
   - First 50 trades: use 0.25 × f* (quarter Kelly)
   - After 50 validated trades: use 0.50 × f* (half Kelly)
   - Never exceed 0.50 × f*

3. CAP BY ACCOUNT SIZE:
   - Risk per trade = min(fractional_kelly, hard_cap)
   - Hard caps: 0.5% (testing), 1% (validated), 1.5% (mature), 2% (absolute max)

4. LOT SIZE CONSTRAINT:
   lots = floor(risk_budget / (premium × lot_size))
   if lots == 0: skip trade (premium too expensive for risk budget)
   if lots == 1: verify 1 lot risk < hard_cap

5. PORTFOLIO LIMITS:
   - Max 2-3 simultaneous positions
   - Total portfolio risk < 3% of equity per day
   - Correlated positions (NIFTY+BANKNIFTY): reduce each by 15%

6. DYNAMIC ADJUSTMENT:
   - Below 20-trade equity SMA: reduce to 50% normal size
   - Drawdown >5%: reduce to 50%
   - Drawdown >10%: reduce to 25%
   - Drawdown >15%: halt trading

7. CREDIT SPREAD SIZING:
   - Size by max loss (strike width - credit), NOT by premium
   - Max loss per spread ≤ 2% of capital
   - Total open spread max loss ≤ 6% of capital

8. ANTI-MARTINGALE SCALING:
   - Base: fixed fraction of current equity
   - After 3 consecutive wins: 1.5× base
   - After 5 consecutive wins: 2.0× base
   - Any loss: immediate reset to base
   - Keep within all hard caps above
```

### Key Insight for Small Indian Accounts
For accounts under ₹3L, sophisticated position sizing is largely academic. The lot size floor means you're trading 1 lot or 0 lots. The real decisions are:
1. **Which instrument** (NIFTY vs BANKNIFTY vs OTM vs ATM)
2. **Whether to take the trade at all** (does 1 lot exceed your risk budget?)
3. **When to sit out** (drawdown state, high VIX, event risk)

Position sizing becomes meaningful only above ₹3-5L where 2+ lot positions are feasible.
