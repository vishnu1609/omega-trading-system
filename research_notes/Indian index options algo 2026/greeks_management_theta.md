# Greeks Management for Indian Index Options (NIFTY/BANKNIFTY) — Intraday Trading

## Theta Decay Curve: Intraday Hourly Breakdown for Weekly Options

### Takeaway
Theta decay on Indian weeklies is non-linear and accelerates sharply in the final 1-2 days before expiry, with the afternoon session (especially the last 2 hours) showing the steepest premium erosion. No published hourly theta curve specific to NIFTY/BANKNIFTY weeklies was found — the patterns below are synthesized from practitioner sources and general options theory.

### Cited Findings
- Theta does not decay in a straight line; it decays much faster near expiry, especially in weekly and 0DTE options — [Groww](https://groww.in/blog/theta-decay-in-options-trading)
- Half of an option's total time decay occurs in the last quarter of its life — [Sensibull](https://blog.sensibull.com/2019/02/09/nifty-weeklies-what-to-expect/)
- When the market trades sideways (commonly mid-day sessions), option buyers lose value due to time decay even without price movement — [5paisa](https://www.5paisa.com/stock-market-guide/derivatives-trading-basics/understanding-theta-in-options)
- ATM options carry the highest absolute theta because their premiums contain the most time value — [5paisa](https://www.5paisa.com/stock-market-guide/derivatives-trading-basics/understanding-theta-in-options)
- The absolute value of theta for ATM options rises as expiration approaches; for deep ITM/OTM options, theta actually falls near expiry — [CME Group](https://www.cmegroup.com/education/courses/option-greeks/theta)
- Afternoons, especially the last 2 hours, show accelerated decay as expiry approaches — [StockMojo](https://stockmojo.in/premium-decay/banknifty)
- ATM BANKNIFTY options can lose 70-90% of their morning value by the 3:30 PM close on expiry day (vendor claim, not independently verified) — [StockMojo](https://stockmojo.in/premium-decay/banknifty)
- Anecdotal: holding a straddle through Thursday 15:00 let gamma risk consume about 70% of premium in 10 minutes — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)

### Inferences
- **Estimated intraday theta profile (expiry day, ATM weekly):**
  - 9:15-10:30 IST: Moderate decay; premium still has enough time value. Gap-up/down volatility can offset theta.
  - 10:30-12:00 IST: Steady decay, especially if market is rangebound. Mid-day is the worst time for buyers in sideways markets.
  - 12:00-13:30 IST: Decay accelerates noticeably. This is when the "theta cliff" begins for weeklies.
  - 13:30-14:30 IST: Sharp acceleration. ATM premiums may lose 30-50% of remaining value.
  - 14:30-15:30 IST: Extreme decay. Final 60 minutes is where ATM options can lose 50-70%+ of whatever premium remains.
- For option buyers: exit long positions before 14:00 IST on expiry day to avoid the steepest theta bleed.
- For option sellers: the 12:00-15:30 window is the most favorable for theta collection, but gamma risk rises simultaneously.

### Gaps
- No published, data-backed hourly theta decay curve specific to NIFTY/BANKNIFTY weeklies exists in the sources found. The intraday profile above is inferred from general principles and practitioner anecdotes.
- No source quantified how much theta accrues overnight vs. during trading hours for Indian weeklies.

---

## IV Crush Patterns: Timing and Magnitude

### Takeaway
IV crush on NIFTY/BANKNIFTY expiry day is steep (claimed 30-60% decline), with the sharpest compression occurring in the morning session as the market finds its range, and a secondary collapse in the afternoon. Event-driven IV (budget, RBI policy, elections) shows even more dramatic crush patterns.

### Cited Findings
- Implied volatility collapses 30-60% on expiry day — sell premium early — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)
- Bank Nifty IV often opens elevated because overnight positioning and gap risk carry into the session, then settles as the market finds its range — [StockMojo](https://stockmojo.in/implied-volatility-chart/banknifty)
- The IV line behaves erratically near the close; late expiry-day IV readings are noise rather than signal — [StockMojo](https://stockmojo.in/implied-volatility-chart/banknifty)
- Strategy filter: avoid trading when India VIX is above 20; ATM straddle requires VIX below 16 — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)

### Inferences
- **IV crush timing (expiry day, IST):**
  - 9:15-10:00: IV often elevated from overnight positioning. If no gap event, IV starts compressing.
  - 10:00-11:30: Primary IV crush window — as the day's range establishes, uncertainty drops and IV compresses fastest.
  - 11:30-14:00: IV continues to decline but at a slower rate. Rangebound markets accelerate this.
  - 14:00-15:30: IV readings become unreliable due to low time value; effective IV is near-meaningless for 0DTE options.
- **For option buyers:** IV crush is your enemy. If buying on expiry day, enter only after the initial IV crush (post 10:30) and only for directional conviction plays, not vega plays.
- **For option sellers:** Selling premium at 9:30-10:00 captures both theta and IV crush. The "double tailwind" is strongest in the first 90 minutes.

### Gaps
- The 30-60% IV crush figure is a practitioner claim without supporting data methodology. Actual IV crush varies by market conditions.
- No source provided a day-by-day IV term structure for the expiry week (e.g., Monday IV vs. Tuesday IV vs. expiry day IV).

---

## ATM vs OTM Theta: Quantitative Comparison

### Takeaway
ATM options carry the highest absolute theta in rupee terms, but OTM options have higher theta as a percentage of premium, meaning they decay faster proportionally. For weekly options, this effect is amplified.

### Cited Findings
- ATM options usually carry the highest absolute theta because their premiums contain the most time value relative to ITM and OTM options — [5paisa](https://www.5paisa.com/stock-market-guide/derivatives-trading-basics/understanding-theta-in-options)
- An illustrative example gives ATM theta = -5 (losing Rs 5/day per unit) — [IndMoney](https://www.indmoney.com/learn/fno/theta-in-options)
- For deep ITM/OTM options, theta actually falls as expiry approaches (the premium has little time value left to erode) — [CME Group](https://www.cmegroup.com/education/courses/option-greeks/theta)
- Sensibull example: Monthly ATM at NIFTY 11000, 15% IV = ~Rs 170; Weekly ATM = ~Rs 85 — [Sensibull](https://blog.sensibull.com/2019/02/09/nifty-weeklies-what-to-expect/)

### Inferences
- **Estimated theta comparison (NIFTY weekly, 1 DTE, NIFTY ~25000):**
  - ATM (25000 strike): Theta ~Rs 30-50/unit, Premium ~Rs 80-150. Theta as % of premium: ~25-40%.
  - 1-strike OTM (e.g., 25100 CE): Theta ~Rs 20-35/unit, Premium ~Rs 40-80. Theta as % of premium: ~35-50%.
  - 2-strikes OTM (e.g., 25200 CE): Theta ~Rs 10-20/unit, Premium ~Rs 15-40. Theta as % of premium: ~50-70%.
  - Deep OTM (e.g., 25500 CE): Theta ~Rs 3-8/unit, Premium ~Rs 5-15. Theta as % of premium: ~50-60%.
- These are estimates based on the general pattern. Actual values depend on IV, spot level, and time remaining.
- **Implication for buyers:** ATM options lose the most rupees to theta but have the best chance of recovering via delta. OTM options lose a higher proportion of their value and need a bigger move to compensate.

### Gaps
- No source provided a direct ATM vs. OTM theta comparison table for current NIFTY/BANKNIFTY weeklies with real data.

---

## Delta-Based Strike Selection: Why 0.50-0.58 Delta

### Takeaway
The 0.50-0.58 delta range (ATM to slightly ITM) offers the best balance of directional sensitivity, liquidity, and manageable theta for directional option buyers. Pure ATM (0.50 delta) has peak liquidity; slightly ITM (0.55-0.58) provides better delta-for-premium efficiency.

### Cited Findings
- ATM options sit near 0.50 delta; a 100-point NIFTY move shifts the option by about 50 points — [Groww](https://groww.in/blog/gamma-risk-explained-through-0-dte-options)
- For OTM options (e.g., NIFTY 25100 call when spot is 25000), delta was 0.4 — [IndMoney](https://www.indmoney.com/learn/fno/what-are-option-greeks)
- Gamma is highest for ATM and near-the-money options — [Groww](https://groww.in/blog/gamma-in-options-trading)
- Liquidity filters: OI above 1000 contracts, bid-ask spread below 0.5% of premium, volume above 50 in last 15 minutes — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)

### Inferences
- **Why 0.50-0.58 delta is optimal for directional buyers:**
  - 0.50 delta (ATM): Maximum gamma (fastest delta growth on favorable move), highest liquidity, tightest spreads. But also maximum theta.
  - 0.55-0.58 delta (1 strike ITM): Slightly higher delta means better participation in favorable moves. Lower gamma means less volatile P&L. Theta is lower than ATM. Liquidity is still strong (1 strike from ATM).
  - 0.40-0.45 delta (1 strike OTM): Lower premium but needs a bigger move. Theta as % of premium is higher. Slightly worse liquidity.
  - Below 0.30 delta (deep OTM): Cheap but low probability. Very high theta-to-premium ratio. Poor liquidity on weeklies.
- **Practical rule:** For NIFTY/BANKNIFTY directional plays with 1-2 DTE, use the ATM strike or one strike ITM. The slightly ITM strike gives you a delta edge without significant liquidity penalty.

### Gaps
- No source provided a direct comparison of bid-ask spreads across different delta levels for NIFTY/BANKNIFTY weeklies.

---

## Gamma Risk Management

### Takeaway
Gamma is highest for ATM options near expiry and creates both opportunity (for buyers) and danger (for sellers). On expiry day, gamma rises exponentially in the afternoon, making positions extremely sensitive to small underlying moves. For buyers, high gamma is an advantage if the move is in their favor but amplifies losses on reversals.

### Cited Findings
- High gamma usually comes with high theta decay, so time works against long option holders — [Groww](https://groww.in/blog/gamma-risk-explained-through-0-dte-options)
- Gamma increases exponentially, not linearly, as expiry nears — [Groww](https://groww.in/blog/gamma-risk-explained-through-0-dte-options)
- On expiry day: morning gamma is moderate; afternoon gamma starts rising rapidly; last couple of hours gamma rises sharply and options react violently to small price moves — [Groww](https://groww.in/blog/gamma-risk-explained-through-0-dte-options)
- Gamma is highest for near-the-money and near-expiration options — [Groww](https://groww.in/blog/gamma-in-options-trading)
- After a 100-point NIFTY move, delta of an ATM option can shift from 0.5 to 0.6 — [Groww](https://groww.in/blog/gamma-risk-explained-through-0-dte-options)
- Gamma risk in the last 30 minutes before expiry consumed about 70% of premium in 10 minutes (anecdotal) — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)

### Inferences
- **Gamma danger zones (IST, expiry day):**
  - Before 12:00: Gamma is elevated but manageable. Favorable for directional buyers who want gamma exposure.
  - 12:00-14:00: Gamma starts accelerating. Position sizing should be reduced.
  - 14:00-15:00: High gamma zone. ATM options can swing Rs 10-30 on a 50-point NIFTY move.
  - 15:00-15:30: Extreme gamma. Avoid holding naked positions. A 30-50 point move can cause 50%+ premium swings.
- **For buyers leveraging gamma:**
  - Enter ATM positions when expecting a directional breakout (e.g., support/resistance break, news catalyst).
  - The ideal gamma play window is 9:15-12:00 on expiry day: gamma is high enough to amplify moves but theta hasn't yet dominated.
  - Set strict time stops: if the expected move doesn't happen by 12:00-13:00, exit to avoid theta bleed.
- **For sellers managing gamma risk:**
  - Close or hedge ATM short positions before 14:00 IST on expiry day.
  - Use spreads (not naked shorts) in the last 2 hours.

### Gaps
- No source provided actual gamma values (e.g., gamma = 0.003) for NIFTY/BANKNIFTY ATM weeklies at different DTEs.

---

## Vega Exposure for Intraday

### Takeaway
For ATM NIFTY weeklies, vega is typically Rs 8-15 per unit per 1% IV change. Weekly options have minimal vega compared to monthlies, making them poor vehicles for vega plays but also less vulnerable to IV changes late in the week.

### Cited Findings
- Vega tells you how much your option's premium changes for every 1% move in IV — [IndMoney](https://www.indmoney.com/learn/fno/vega-in-options)
- Example: NIFTY ATM call with vega of 12 — a 1% rise in IV adds Rs 12 per unit; a 2-point rise adds ~Rs 24 — [IndMoney](https://www.indmoney.com/learn/fno/vega-in-options)
- A NIFTY 24,900 call (ATM) showed vega of 12.88 — [Tradejini](https://www.tradejini.com/blogs/the-traders-playbook-for-using-vega-effectively)
- Weekly options do not really have much vega or effect of IV — [Sensibull](https://blog.sensibull.com/2019/02/09/nifty-weeklies-what-to-expect/)
- For full position impact, multiply vega by lot size. With lot size 50 (outdated), a 2-point IV spike = Rs 1,200 — [Tradejini](https://www.tradejini.com/blogs/the-traders-playbook-for-using-vega-effectively)

### Inferences
- **Current lot-level vega impact (NIFTY lot = 65):**
  - ATM weekly, ~2 DTE: Vega ~Rs 10-15/unit. Per lot: Rs 650-975 per 1% IV change.
  - ATM weekly, expiry day: Vega ~Rs 3-6/unit. Per lot: Rs 195-390 per 1% IV change.
  - ATM monthly, ~20 DTE: Vega ~Rs 30-50/unit. Per lot: Rs 1,950-3,250 per 1% IV change.
- **BANKNIFTY (lot = 30):**
  - ATM weekly, ~2 DTE: Vega ~Rs 15-25/unit. Per lot: Rs 450-750 per 1% IV change.
- **Practical implication:** On expiry day, vega is negligible for weeklies. IV changes matter most on Monday-Tuesday of expiry week. By Thursday (or Tuesday, per new expiry schedule), theta and gamma dominate.

### Gaps
- The vega values above are estimates. No source provided a current, verified ATM vega for NIFTY/BANKNIFTY weeklies.

---

## Greeks-Based Exit Rules

### Takeaway
Exit long options before 14:00 IST on expiry day. For non-expiry days, exit when delta drops below 0.25 (OTM, low probability of recovery) or rises above 0.80 (deep ITM, illiquid, better to book profit). Monitor IV percentile and exit if it drops below the 30th percentile during a long position.

### Cited Findings
- No new trades after 14:00 on expiry day; no holding through 15:30 — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)
- ATM straddle: time exit at 15:00 on Wednesday; iron condor: exit all legs at 14:30 if still open — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)
- Close ATM positions before 14:00 due to pin risk — [Sahi](https://www.sahi.com/blogs/5-rules-every-trader-must-follow-on-nifty-options-expiry-day)
- ATM straddle: take profit at 40% of combined premium, stop loss at 80% — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)
- Iron condor: book 50% profit once premium decays to 40% of sold value — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)

### Inferences
- **Theta-based exit rules:**
  - Expiry day: Close all long option positions by 14:00 IST. After this, theta acceleration makes recovery nearly impossible unless a large directional move occurs.
  - 1 DTE: Close long positions by 15:00 IST if the trade hasn't reached target. Holding overnight into expiry day adds massive theta risk.
  - 2+ DTE: Theta is manageable; standard stop-loss and target rules apply.
- **Delta-based exit rules:**
  - Exit long calls/puts when delta drops below 0.20-0.25 (the option is deep OTM and unlikely to recover).
  - Consider booking profit when delta exceeds 0.75-0.80 (deep ITM; the option is expensive, illiquid, and behaves like the underlying — better to close and re-enter at a new ATM strike if still bullish/bearish).
  - Sweet spot for holding: 0.35-0.65 delta range.
- **IV-based exit triggers:**
  - If IV drops more than 3-5 percentage points during your holding period (for weekly options), that's a significant headwind for buyers. Consider exiting.
  - If India VIX drops below 12, long premium strategies face severe headwinds.
  - If India VIX spikes above 20, short premium strategies carry extreme risk.

### Gaps
- No source provided specific IV percentile thresholds for exit decisions on Indian weeklies.
- The delta exit levels (0.25 and 0.80) are inferred from general options practice, not from NIFTY/BANKNIFTY-specific data.

---

## Delta Hedging for Small Accounts (Rs 1-5 Lakh)

### Takeaway
Classical delta hedging (option + underlying futures) is not practical for accounts under Rs 5 lakh due to margin requirements for futures positions. However, spread-based "poor man's delta hedge" using options is feasible.

### Cited Findings
- Current lot sizes: NIFTY 65, BANKNIFTY 30 (as of January 2026) — [AlgoTest](https://algotest.in/blog/nifty-lot-size.md)
- Option buyers pay only the premium (no margin) — [AlgoTest](https://algotest.in/blog/bank-nifty-lot-size-2026-current-quantity-and-one-lot-cost.md)
- Delta hedging requires taking an opposite position in the underlying and rebalancing periodically — [Kotak Securities](https://www.kotaksecurities.com/investing-guide/futures-and-options/delta-hedging-applications)
- Constant adjustment means repeated trades; brokerage, taxes, and slippage eat into the hedge's benefit — [Choice India](https://choiceindia.com/blog/delta-neutral-strategy)

### Inferences
- **Margin math for delta hedging with futures:**
  - 1 lot NIFTY futures (65 units x ~Rs 25,000): Margin ~Rs 1.5-2.0 lakh.
  - 1 lot BANKNIFTY futures (30 units x ~Rs 52,000): Margin ~Rs 1.5-2.0 lakh.
  - For a Rs 1 lakh account: Cannot hold even 1 lot of futures for hedging. Delta hedging with futures is not feasible.
  - For a Rs 3-5 lakh account: Can hold 1 lot of futures but this consumes 30-60% of capital, leaving no room for the option position or additional margin.
- **Alternative: Spread-based hedging (feasible for small accounts):**
  - Buy ATM call + Sell OTM call (bull call spread): Net delta is reduced. Maximum risk is defined. Cost is premium paid minus premium received.
  - Example: Buy NIFTY 25000 CE at Rs 150 + Sell NIFTY 25200 CE at Rs 80. Net cost = Rs 70/unit = Rs 4,550/lot. Max profit = Rs 130/unit = Rs 8,450/lot. This is a defined-risk trade within a Rs 1 lakh account.
  - This isn't true delta hedging but achieves a similar risk-reduction effect for small accounts.
- **Practical conclusion:** For Rs 1-5 lakh accounts, use vertical spreads (bull call/bear put spreads) instead of classical delta hedging. The defined risk structure is more important than maintaining a delta-neutral position.

### Gaps
- No source provided exact 2026 margin requirements for NIFTY/BANKNIFTY futures.
- No source addressed the brokerage cost impact of frequent delta hedging rebalancing for Indian retail accounts.

---

## Greeks Behavior: Expiry Day vs 1-2 DTE

### Takeaway
On expiry day (0 DTE), theta and gamma are at extremes while vega is negligible. At 1-2 DTE, all Greeks are elevated but not yet at crisis levels, making it the last window for manageable directional trades.

### Cited Findings
- The absolute value of theta of an ATM option rises as expiration approaches — [CME Group](https://www.cmegroup.com/education/courses/option-greeks/theta)
- For deep ITM/OTM options, theta falls near expiry (little time value left to erode) — [CME Group](https://www.cmegroup.com/education/courses/option-greeks/theta)
- On expiry day, gamma explodes near the strike — [StockAlarm](https://pro.stockalarm.io/blog/0dte-options-explained)
- Expiry-day theta differed sharply between Upstox (-77.88) and Sensibull (-4.3) for the same contract due to different time-to-expiry modeling — [Upstox Community](https://community.upstox.com/t/discrepancy-in-theta-values-between-upstox-and-sensibull-on-expiry-day/8326)
- Weeklies have higher gamma and theta than monthlies, and little vega — [Sensibull](https://blog.sensibull.com/2019/02/09/nifty-weeklies-what-to-expect/)

### Inferences
- **Estimated Greeks comparison (ATM NIFTY weekly, per unit):**

| Greek | 2 DTE (Monday) | 1 DTE (day before expiry) | 0 DTE (expiry day morning) | 0 DTE (expiry day afternoon) |
|-------|----------------|---------------------------|---------------------------|------------------------------|
| Delta | ~0.50 | ~0.50 | ~0.50 (but shifts rapidly) | ~0.50 (extremely unstable) |
| Gamma | Moderate (~0.002) | High (~0.004) | Very high (~0.008) | Extreme (~0.015+) |
| Theta | Rs 15-25/unit | Rs 25-40/unit | Rs 40-60/unit | Rs 60-100+/unit |
| Vega | Rs 8-12/unit | Rs 4-8/unit | Rs 2-4/unit | ~Rs 0-2/unit |

- **Key behavioral differences:**
  - 2 DTE: All Greeks are "normal." Good window for directional trades. Theta is meaningful but not dominant. Vega still relevant.
  - 1 DTE: Theta accelerates noticeably. Gamma starts becoming a factor. Vega diminishing. Last day to hold overnight without extreme theta risk.
  - 0 DTE morning: Theta is very high but the day is young. Gamma makes trades very responsive to direction. Good for quick scalps if you have conviction.
  - 0 DTE afternoon: Theta is catastrophic for buyers. Gamma is extreme — a 50-point move can double or zero an ATM option. Vega is irrelevant. This is purely a gamma/theta battlefield.
- **Caveat:** Broker platforms calculate expiry-day Greeks differently. The Upstox vs. Sensibull discrepancy (theta of -77.88 vs. -4.3 for the same contract) shows that expiry-day Greek values should not be taken at face value from any single platform.

### Gaps
- The table values are estimates based on general options theory applied to NIFTY. Actual values depend on IV, spot level, and platform calculation methodology.

---

## IV Skew on NIFTY/BANKNIFTY

### Takeaway
Indian index options show a persistent put skew (OTM puts have higher IV than equidistant OTM calls), driven by institutional demand for portfolio insurance. The typical skew is 2-4 percentage points for 25-delta options.

### Cited Findings
- OTM puts on Nifty and Bank Nifty normally carry higher IV than equidistant OTM calls; institutions buy index puts as portfolio insurance, keeping put demand and put IV elevated — [OptionX](https://optionx.trade/blogs/implied-volatility-skew-trading-strategies)
- The skew is particularly persistent in India, partly because of memories of the 2008 and 2020 crashes — [OptionX](https://optionx.trade/blogs/implied-volatility-skew-trading-strategies)
- Illustrative example: NIFTY at 23,000 — 25-delta put (22,500 strike) IV = 18%; 25-delta call (23,500 strike) IV = 15%; skew = 3 percentage points — [OptionX](https://optionx.trade/blogs/implied-volatility-skew-trading-strategies)
- The skew steepened dramatically during March 2020 COVID crash; in calm bull markets it compressed but never flattened — [Share.Market](https://www.share.market/buzz/futures-and-options/volatility-skew-implied-volatility)
- An academic study found positive asymmetry in the IV-moneyness relationship for NSE Nifty options (dated 2008 study) — [USM eJournal](https://ejournal.usm.my/aamjaf/article/download/aamjaf_vol4-no1-2008_3/pdf/641)

### Inferences
- **Skew exploitation strategies for small accounts:**
  - **Put ratio spreads:** Buy 1 ATM put, sell 2 OTM puts. Captures the rich OTM put IV. Risk: naked downside below the lower strike. Requires margin.
  - **Jade lizard / twisted sister:** Sell OTM put (rich IV) + sell OTM call spread. Collects more premium on the put side due to skew.
  - **For buyers:** Buying OTM puts is relatively expensive due to skew. Prefer ATM puts or put debit spreads instead of naked OTM put buys.
  - **For sellers:** OTM put selling benefits from the skew premium, but the tail risk (crash) is exactly what the skew is pricing in. Never sell naked OTM puts without a hedge.
- **Practical rule:** The put skew means that if you're buying OTM puts for protection, you're paying a premium above theoretical fair value. If you're selling OTM puts, you're collecting that premium — but you're also taking on the crash risk that justifies it.

### Gaps
- No source provided time series data showing how the NIFTY/BANKNIFTY skew has evolved over recent years.
- The 3-percentage-point skew example is illustrative; actual skew varies with market conditions and DTE.
- The OptionX source had an internal inconsistency in its sign convention for skew, reducing confidence in its precision.

---

## Real-World Greeks Values: Typical ATM Weekly NIFTY Options

### Takeaway
No single authoritative source provides a current, verified table of typical ATM Greeks for NIFTY/BANKNIFTY weeklies. The estimates below are synthesized from multiple sources and general options pricing theory.

### Cited Findings
- ATM NIFTY call example: Delta = 0.5, a 100-point move shifts premium by ~50 points — [Groww](https://groww.in/blog/gamma-risk-explained-through-0-dte-options)
- NIFTY 24,900 call (ATM) showed vega of 12.88 — [Tradejini](https://www.tradejini.com/blogs/the-traders-playbook-for-using-vega-effectively)
- Illustrative theta = -5 (Rs 5/day per unit) — [IndMoney](https://www.indmoney.com/learn/fno/theta-in-options)
- Monthly ATM at NIFTY 11000, 15% IV = ~Rs 170; Weekly ATM = ~Rs 85 (2019 data, outdated) — [Sensibull](https://blog.sensibull.com/2019/02/09/nifty-weeklies-what-to-expect/)

### Inferences
- **Estimated typical ATM values (NIFTY ~25000, IV ~13-15%, weekly with 2-3 DTE):**

| Parameter | Value (per unit) | Value (per lot, 65 units) |
|-----------|-----------------|--------------------------|
| Premium | Rs 100-180 | Rs 6,500-11,700 |
| Delta | 0.48-0.52 | — |
| Gamma | 0.002-0.004 | — |
| Theta | Rs -12 to -25/day | Rs -780 to -1,625/day |
| Vega | Rs 8-15 per 1% IV | Rs 520-975 per 1% IV |

- **BANKNIFTY (~52000, IV ~15-18%, weekly with 2-3 DTE):**

| Parameter | Value (per unit) | Value (per lot, 30 units) |
|-----------|-----------------|--------------------------|
| Premium | Rs 200-350 | Rs 6,000-10,500 |
| Delta | 0.48-0.52 | — |
| Gamma | 0.001-0.002 | — |
| Theta | Rs -20 to -40/day | Rs -600 to -1,200/day |
| Vega | Rs 15-30 per 1% IV | Rs 450-900 per 1% IV |

- **Important caveat:** These are rough estimates. For actual trading, always check your broker's live Greeks display (Sensibull, Opstra, or your broker's option chain).

### Gaps
- No verified, current data source for typical NIFTY/BANKNIFTY weekly ATM Greeks was found.
- Greeks change significantly with IV regime (low vol vs. high vol periods).

---

## Current Lot Sizes and Expiry Schedule

### Takeaway
As of 2026, NIFTY lot size is 65 and BANKNIFTY is 30. NIFTY weekly expiry has moved to Tuesday (from September 2025 onwards per SEBI). BANKNIFTY weekly expiry status should be confirmed on NSE.

### Cited Findings
- NIFTY lot size: 65; BANKNIFTY lot size: 30 (as of January 1, 2026) — [AlgoTest](https://algotest.in/blog/nifty-lot-size.md)
- NSE moved NIFTY weekly expiry from Thursday to Tuesday from September 1, 2025 onward, following SEBI approval — [Navia](https://navia.co.in/blog/weekly-fo-expiry-insights/)
- Multiple sources conflict on whether BANKNIFTY still has weekly expiry contracts — [DEV Community](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l); [Sahi](https://www.sahi.com/blogs/5-rules-every-trader-must-follow-on-nifty-options-expiry-day)

### Inferences
- With NIFTY lot = 65 at ~Rs 25,000 spot, 1 lot ATM option costs Rs 6,500-11,700 (premium). Feasible for Rs 1 lakh+ accounts.
- With BANKNIFTY lot = 30 at ~Rs 52,000 spot, 1 lot ATM option costs Rs 6,000-10,500 (premium). Also feasible for Rs 1 lakh accounts.
- **Confirm the current expiry day and whether BANKNIFTY still has weekly options on the NSE website before implementing any expiry-day strategy.** SEBI's rationalization of weekly expiries (limiting to one index per exchange) may have removed BANKNIFTY weeklies.

### Gaps
- Conflicting information on BANKNIFTY weekly expiry status. Must verify on NSE directly.
- No exact 2026 margin figures for futures or option selling positions.

---

## Summary: Practical Rules for Greeks-Based Trading

### For Option Buyers (Rs 1-5 Lakh Accounts)

**Entry Rules:**
- Select strikes with 0.48-0.58 delta (ATM to 1 strike ITM)
- Enter after initial IV crush settles (post 10:00-10:30 IST on expiry day)
- Prefer 1-2 DTE over 0 DTE for better risk/reward (theta is high but not catastrophic, gamma gives amplification)
- Check India VIX: avoid buying if VIX < 12 (premiums too cheap, theta dominant) or VIX > 25 (premiums too expensive)

**Exit Rules:**
- **Time stops:** Close by 14:00 IST on expiry day; close by 15:00 IST on 1 DTE
- **Delta stops:** Exit if delta drops below 0.20-0.25 (too far OTM) or rises above 0.80 (book profit, re-enter if needed)
- **Premium stops:** 40% profit target, 60% stop loss of premium paid
- **IV stops:** Exit if IV drops 3+ points during your holding period

**Position Sizing:**
- Max 1% of capital per trade (Rs 1,000 on Rs 1 lakh; Rs 5,000 on Rs 5 lakh)
- Max 2 positions simultaneously
- Use spreads (bull call/bear put) to reduce capital at risk

### For Option Sellers (Rs 3-5 Lakh Accounts)

**Entry Rules:**
- Sell OTM options with delta 0.15-0.25 (probability-based selection)
- Enter on Monday/Tuesday when theta is beginning to accelerate for the week
- Sell straddles only when VIX < 16 and PCR is 0.9-1.1

**Exit Rules:**
- Book profit at 40-50% of premium collected
- Close ATM/near-ATM short positions before 14:00 IST on expiry day
- Stop loss at 80-100% of premium collected (i.e., if the option doubles, exit)
- Always use defined-risk structures (spreads) rather than naked selling

**Hedging:**
- Use vertical spreads instead of delta hedging with futures (capital-efficient)
- For a Rs 5 lakh account, a NIFTY iron condor requires approximately Rs 20,000-30,000 in margin — verify with your broker's margin calculator
