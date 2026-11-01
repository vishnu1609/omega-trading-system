# NSE Index Options Market Microstructure & Expiry Dynamics

## SEBI Regulatory Context (2024-2026)

### Takeaway
SEBI restructured weekly expiries in late 2024, limiting each exchange to one benchmark index for weeklies (NIFTY on NSE, SENSEX on BSE), and in May 2025 standardised all equity derivatives expiry to Tuesday or Thursday. NSE moved to Tuesday expiry effective September 1, 2025. Bank Nifty, FinNifty, and Midcap Select weeklies were discontinued.

### Cited Findings
- SEBI's October 2024 circular limited each exchange to one benchmark index for weekly expiry contracts, effective November 20, 2024. NSE retained NIFTY 50 weekly options; BSE retained SENSEX. Bank Nifty, Midcap Select, FinNifty, Sensex 50, and Bankex weeklies were discontinued. -- [Zerodha](https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing)
- The same circular raised minimum contract value to Rs 15 lakh at introduction, the first revision in nine years. -- [Business Standard](https://www.business-standard.com/amp/markets/news/sebi-announces-six-key-changes-to-curb-speculation-in-derivatives-trading-124100101316_1.html)
- SEBI's May 26, 2025 circular mandated that all equity derivatives expire on either Tuesday or Thursday. Exchanges submitted compliance proposals by June 15, 2025. -- [Outlook Money](https://www.outlookmoney.com/news/fo-trading-all-equity-derivatives-on-exchanges-to-expire-on-either-tuesday-or-thursday-sebi-mandates)
- NSE transitioned its equity derivatives expiry to Tuesday effective September 1, 2025. BSE switched to Thursday. -- [Kotak Securities](https://www.kotaksecurities.com/news/market-news/sebi-to-end-thursday-expiry)
- BSE discontinued weekly index derivatives on Sensex 50 and Bankex following SEBI's directive. -- [Outlook Business](https://www.outlookbusiness.com/news/bse-to-discontinue-weekly-index-derivatives-on-sensex-50-bankex-following-sebis-directive)

### Inferences
- As of October 2026, NIFTY weekly options on NSE expire on **Tuesday**. Any algo strategy referencing "Thursday expiry" or "Wednesday BANKNIFTY expiry" is outdated.
- Bank Nifty weekly options no longer exist. Monthly Bank Nifty contracts remain but expire on the last Tuesday of the month.
- The concentration of all weekly volume into a single index (NIFTY) should increase liquidity depth at NIFTY ATM strikes but reduce inter-index arbitrage opportunities.

### Gaps
- Exact lot sizes post-revision not confirmed from a primary source.

---

## Gamma Exposure (GEX) on NSE

### Takeaway
GEX for NSE index options is an estimate, not an exchange-published figure. The core limitation is that NSE does not publish trade-side data (who is long vs short at each strike), so all GEX computations rely on an assumed sign convention that may be wrong on any given day.

### Cited Findings
- GEX per strike is computed as: gamma x open interest x spot^2 x 1%, reported in Rs crore of hedging per 1% index move. Calls are counted positive and puts negative under the standard convention. -- [OIData](https://www.oidata.in/blog/gamma-exposure-gex-explained)
- NSE does not publish which participants are long or short each contract, so GEX is an estimate. One vendor applies the US convention (dealers long calls, short puts) and warns that if real positioning were reversed, the profile would invert. -- [OIData](https://www.oidata.in/blog/gamma-exposure-gex-explained)
- Participant-Signed GEX re-signs each side using FII and Pro net-long shares from NSE's participant-wise open interest data. One example showed naive GEX of Rs 6,950 Cr versus participant-signed Rs 5,752 Cr, same direction but materially different magnitude. -- [OIData](https://www.oidata.in/blog/gamma-exposure-gex-explained)
- A July 2026 SSRN preprint proposes Participation-Calibrated GEX (PC-GEX) that infers passive gamma sign from order-flow direction, changes in OI, and changes in IV. The author argues the fixed-sign assumption may not hold in structurally heterogeneous markets like India. -- [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7295618)
- A GitHub project (GammaPin) tests the gamma pinning claim by comparing the US sign convention with India's participant-wise positioning, using only public NSE and RBI files. -- [GitHub](https://github.com/guru-bharadwaj20/GammaPin)
- About 59% of Indian index-options turnover is in same-day-expiry contracts. -- [GitHub/GammaPin](https://github.com/guru-bharadwaj20/GammaPin)

### GEX Flip Level and Intraday Price Action
- The gamma flip is defined as the index level where net GEX crosses zero. Above the flip, net GEX is positive (absorbing market -- dealers hedge by selling into rallies and buying dips, creating mean reversion). Below the flip, net GEX is negative (amplifying market -- dealers chase price). -- [OIData](https://www.oidata.in/blog/gamma-exposure-gex-explained)
- "Strongly positive" regime requires net GEX above 0.35 of gross exposure. -- [OIData](https://www.oidata.in/blog/gamma-exposure-gex-explained)
- The call wall (largest call-side gamma strike) tends to act as resistance; the put wall (largest put-side gamma strike) as support. Gamma walls sat closer to spot (23700 and 24000) than raw OI walls (23000 and 25000) on one observed afternoon. -- [OIData](https://www.oidata.in/blog/gamma-exposure-gex-explained)

### Where to Find GEX Data for Indian Markets
- **OIData** (oidata.in/dealer-positioning): live regime, walls, pin risk, flip, per-strike profile, participant-signed cross-check for NIFTY, BANKNIFTY, SENSEX.
- **Opstra** (opstra.definedge.com): GEX and max pain analytics.
- **QuintalMind** (quintalmind.com/gamma-exposure/nifty): live Nifty GEX chart.
- **StockMojo** (stockmojo.in/gamma-exposure/nifty): Nifty gamma exposure dashboard.
- **FNOLab** (fnolab.com/learn/gex-gamma-exposure): GEX explainer and data.
- **OptionsFlow** (optionsflow.in): NIFTY, BankNifty, Sensex, FinNifty & Midcap GEX analytics.
- **VTRender** (vtrender.com/pillar/gamma): Nifty gamma exposure charts.
- **NSE participant-wise open interest** (nseindia.com): published each evening; input for participant-signed GEX.

### Inferences
- GEX should be treated as directional guidance, not a precise level. The sign convention problem means it could be wrong.
- Cross-checking naive GEX against participant-signed GEX adds confidence. When both agree on direction, the reading is more reliable; when they disagree, treat the signal as low confidence.
- For algo implementation: consume GEX as a regime indicator (trending vs mean-reverting) rather than exact support/resistance levels.

### Gaps
- No backtested evidence that GEX flip level predicts intraday NIFTY moves. The GammaPin project aims to test this but results are not yet published.
- No NSE-published GEX product exists.

---

## Max Pain on Weekly Expiry

### Takeaway
Published hit rates for max pain range from ~35% to ~80% depending on the tolerance window and definition used. No independent, peer-reviewed study with clear methodology was found. Max pain works best as a confirming indicator in low-VIX, low-event environments, not as a standalone signal.

### Cited Findings
- One vendor claims NIFTY closes within 1% of max pain on roughly 60-70% of weekly expiries. Another puts it at 35-45% within 0.5-1% on news-free weeks. -- [BankNiftyOptions](https://bankniftyoptions.com/artigos/banknifty-max-pain-strategy); [StockMojo](https://stockmojo.in/max-pain)
- A Bank Nifty guide claims the index closes within 200 points of max pain on approximately 79% of weekly expiries. A different guide from the same site puts it at 56% across all weeks and 82% on event-free weeks. -- [BankNiftyOptions](https://bankniftyoptions.com/artigos/banknifty-max-pain-today-guide)
- A small backtest of 17 logged NIFTY/BankNifty/SENSEX expiry sessions found spot closer to max pain in 8 of 17 (47%), calling it a coin flip. Max pain showed real value in three conditions: low VIX, small opening gap, and agreement with OI walls. -- [MarketsEasy](https://marketseasy.in/learn/max-pain)
- Max pain itself moves intraday as open interest changes. It is not a fixed target. -- [MarketsEasy](https://marketseasy.in/learn/max-pain)
- October 2026 expiry commentary: the index traded around 22,648.75, almost exactly at the max pain level of 22,650. -- [5Paisa](https://www.5paisa.com/blog/nifty-weekly-expiry-06-october-2026-22650-becomes-the-key-pivot)

### Inferences
- Max pain's pull is strongest in low-volatility, non-event weeks when option writers dominate.
- For algo use: treat max pain as a mean-reversion magnet that strengthens as expiry approaches, especially after 1 PM IST. Weight it higher when India VIX < 15 and PCR is near 1.0.
- The +/- 50 points hit rate for NIFTY is likely in the 40-55% range based on available data, not the 70%+ some vendors claim.
- Max pain should be recalculated continuously through the session as OI shifts.

### Gaps
- No rigorous, independent backtest of max pain accuracy for NIFTY weekly expiry with defined tolerance (e.g., +/-50 points) and sufficient sample size.
- Historical Bank Nifty max pain data is now less relevant since Bank Nifty weeklies were discontinued.

---

## Expiry Day Dynamics (Tuesday for NIFTY, as of Sep 2025)

### IV Crush Timing

#### Takeaway
IV crush is primarily event-driven (Budget, RBI policy, election results), not calendar-driven. On regular expiry days, implied volatility declines gradually as time value evaporates, with the steepest drop in the final 2-3 hours.

#### Cited Findings
- Budget-day example: a Nifty 23500 call bought before Budget 2025 at Rs 210 (IV ~24%) and sold after at Rs 165 (IV ~14%), a loss despite correct direction. -- [OneTradeJournal](https://onetradejournal.com/glossary/iv-crush)
- Blog accounts describe decay accelerating in the final two hours of expiry day, with one example of an option falling from ~Rs 40 at 1 PM to ~Rs 5 by 3 PM if the index doesn't move. -- [OptionStrategy.in](https://optionstrategy.in/expiry-day-premium-decay-logic/)
- Common advice: avoid holding long options past 2:00-2:30 PM IST on expiry day. Be cautious buying OTM options after early afternoon. -- [AlgoTest](https://algotest.in/blog/nifty-expiry-day/)
- One source notes that the last day erodes as much premium as days 4 through 9 combined. -- [OptionStrategy.in](https://optionstrategy.in/expiry-day-premium-decay-logic/)

#### Inferences
- **Morning (9:15-11:00 AM IST):** Opening volatility may temporarily inflate premium. Short sellers should wait for the initial spike to settle.
- **Midday (11:00 AM - 1:00 PM IST):** Gradual theta erosion. Range-bound behavior is common.
- **Afternoon (1:00 - 2:30 PM IST):** Decay accelerates noticeably. This is the primary window for short premium strategies.
- **Final hour (2:30 - 3:30 PM IST):** Maximum decay rate. OTM options can collapse to near-zero. Gamma risk peaks for ATM strikes.

### Premium Decay Acceleration

#### Cited Findings
- The last day's theta burn exceeds the previous 5-6 days combined. -- [OptionStrategy.in](https://optionstrategy.in/expiry-day-premium-decay-logic/)
- Blog sources suggest avoiding buying OTM options when India VIX is above roughly 18-20. -- [OptionStrategy.in](https://optionstrategy.in/expiry-day-iv-crush-explained-hinglish/)

### Gamma Scalping Opportunities on Expiry Day

#### Inferences
- ATM options have maximum gamma on expiry day. A 50-point NIFTY move can swing delta from 0.5 to near 0 or 1.
- Gamma scalping is viable for market makers or accounts with low-latency execution. The strategy involves being long ATM straddles and delta-hedging with futures.
- The 59% same-day-expiry turnover concentration means liquidity is deep for ATM weekly strikes.
- Risk: gamma is two-edged. Short gamma positions (iron condors, strangles) face blowup risk from sudden directional moves.

### Pin Risk and Round-Strike Pinning

#### Cited Findings
- A 2004 study of 5 NSE stock options found no evidence of expiry-day pinning. -- [CXOADVISORY](https://www.cxoadvisory.com/equity-options/stock-price-pinning-at-options-expiration)
- An IIM Bangalore study using 2006 and 2010 data found higher trading volume, price return, and price volatility on expiry dates vs non-expiry dates for NIFTY. -- [IIM Bangalore](https://repository.iimb.ac.in/handle/2074/21032)
- US academic research (2003) argues that large open interest can lead aggregate market-maker delta hedging to push the stock toward a strike. -- [CXOADVISORY](https://www.cxoadvisory.com/equity-options/stock-price-pinning-at-options-expiration)
- A trading blog asserts that institutions often pin NIFTY to max OI strike on expiry, but offers no supporting data. -- [Dev.to](https://dev.to/shaktitiwari/nifty-expiry-week-mastery-the-exact-playbook-i-use-for-weekly-options-1e9l)

#### Inferences
- Index pinning evidence for NIFTY is weak and mostly anecdotal. Stock-level pinning studies on NSE also failed to confirm the effect.
- The pinning mechanism (market-maker delta hedging) is theoretically sound but may be diluted in India's highly retail-participatory market.
- For algo use: don't design strategies that rely on pinning. Treat it as an occasional phenomenon, not a tradeable edge.

#### Gaps
- No systematic study of NIFTY weekly expiry-day closing distance from nearest round strike (e.g., multiples of 100).
- No frequency data on how often NIFTY closes within +/-25 points of a 100-point strike on expiry.

---

## Order Flow Analysis: PCR, OI Buildup, Change in OI

### Takeaway
PCR and OI buildup are widely used confirmation tools on NSE, but no fixed thresholds work universally. Change in OI is more informative than the absolute level. The four-state buildup classification (long buildup, short buildup, short covering, long unwinding) is the standard framework.

### Cited Findings
- **OI buildup framework:** Price up + OI up = long buildup; Price down + OI up = short buildup; Price up + OI down = short covering; Price down + OI down = long unwinding. -- [FNOTrader](https://www.fnotrader.com/blog/option-chain-analysis)
- Short covering rallies and long-buildup rallies look the same on a price chart but have different endings. Short covering ends once sellers finish closing. -- [FNOTrader](https://www.fnotrader.com/blog/option-chain-analysis)
- **PCR thresholds conflict across sources:** One guide treats OI PCR < 0.7 as crowded bullishness and > 1.3-1.5 as crowded bearishness. Another says > 1.2 is bearish sentiment. -- [NiftyTrader](https://www.niftytrader.in/markets/put-call-ratio-pcr-explained-how-to-use/); [OneTradeJournal](https://onetradejournal.com/indicators/put-call-ratio-indicator)
- PCR is contrarian at extremes but trend-confirming in the middle range. -- [OneTradeJournal](https://onetradejournal.com/indicators/put-call-ratio-indicator)
- PCR is highly sensitive to the strike range included. Whole-chain and near-the-money readings often disagree. -- [FNOTrader](https://www.fnotrader.com/blog/option-chain-analysis)
- Change in OI is usually more informative than the level. The option chain does not tell you which way price will go by itself. -- [FNOTrader](https://www.fnotrader.com/blog/option-chain-analysis)
- OI walls: heavy call OI above spot is read as resistance; heavy put OI below spot as support. But walls mark where positions sit, not where price must stop. Walls shift as positions roll, especially into expiry. -- [FNOTrader](https://www.fnotrader.com/blog/option-chain-analysis)

### Inferences
- **Practical PCR ranges for NIFTY:** PCR 0.7-1.3 is a neutral zone. Below 0.7 may signal complacency (contrarian bearish). Above 1.3 may signal excessive fear (contrarian bullish). These are starting points that should be calibrated to recent PCR history.
- **OI change is the primary signal, not OI level.** A strike with 10L OI that added 2L today tells more than a strike with 12L OI that hasn't changed.
- **For algo implementation:** Consume change-in-OI at ATM+/-5 strikes every 3-5 minutes. Classify into the four buildup states. Use as confirmation for directional signals, not as standalone entries.

### Gaps
- No backtested evidence of PCR or OI buildup as predictive signals for NIFTY returns.

---

## FII/DII Option Positions as Directional Predictors

### Takeaway
FII/DII flow data explains only a modest portion (~23%) of daily NIFTY moves. The FII/DII ratio appears more predictive than raw flows. No rigorous, published study validates FII option positions as a next-day directional predictor.

### Cited Findings
- FII net flow to next-day NIFTY return correlation: 0.18. DII net flow: 0.12. FII/DII ratio: 0.31. -- [Dev.to analysis](https://dev.to/shaktitiwari/fii-vs-dii-who-actually-moves-nifty-data-driven-analysis-2024-2026-13a0)
- FII/DII flows explain only about 23% of daily NIFTY moves. -- [Dev.to analysis](https://dev.to/shaktitiwari/fii-vs-dii-who-actually-moves-nifty-data-driven-analysis-2024-2026-13a0)
- Regime-based analysis (self-reported, 2024-2026): FII accumulation (net buy > Rs 5,000 Cr over 5 days) preceded +2.1% NIFTY return with 68% probability. FII distribution (net sell > Rs 5,000 Cr over 5 days) preceded -1.8% return with 72% probability. -- [Dev.to analysis](https://dev.to/shaktitiwari/fii-vs-dii-who-actually-moves-nifty-data-driven-analysis-2024-2026-13a0)
- Only sustained FII selling above Rs 10,000 Cr over two weeks precedes meaningful corrections (claim made without supporting data). -- [Dev.to analysis](https://dev.to/shaktitiwari/fii-vs-dii-who-actually-moves-nifty-data-driven-analysis-2024-2026-13a0)
- Self-reported backtest (2.5 years): FII timing strategy returned +31.2% (48 trades, 62% win rate) vs buy-and-hold +18.4%. FII+DII regime filter: +27.8% (32 trades, 69% win rate). Transaction costs not mentioned. -- [Dev.to analysis](https://dev.to/shaktitiwari/fii-vs-dii-who-actually-moves-nifty-data-driven-analysis-2024-2026-13a0)

### Inferences
- FII/DII data is useful as a regime filter (accumulation vs distribution weeks) rather than a daily signal.
- The 5-day rolling FII net is more stable and predictive than single-day data.
- **NSE data source:** NSE publishes daily FII/DII and participant-wise open interest at nseindia.com. This includes index futures, index options, stock futures, and stock options broken by FII, DII, Pro, and Client categories.
- For algo use: incorporate 5-day rolling FII flow direction as a regime classifier. Long bias when FII accumulating; defensive/short bias when FII distributing.

### Gaps
- The Dev.to analysis is self-published and not peer-reviewed. Its methodology and backtest are not independently verified.
- No study specifically tests FII/DII option positions (as distinct from cash equity flows) as directional predictors.

---

## Intraday Volume Profile and Liquidity Patterns

### Takeaway
Academic literature consistently finds U-shaped intraday volume and spread patterns across global exchanges (high at open and close, low at midday). No NSE-specific study was found in this search, but the pattern is expected to hold given the theoretical mechanism (adverse selection, inventory costs).

### Cited Findings
- Studies of Istanbul Stock Exchange, Italian Bourse, and Toronto Stock Exchange all found U-shaped patterns in volume, volatility, and bid-ask spreads. -- [MPRA](https://mpra.ub.uni-muenchen.de/35968/1/MPRA_paper_35968.pdf)
- U-shaped spread pattern attributed to inventory holding costs, specialist/market maker power, and adverse selection risk. -- [University of Wollongong](https://uowoajournals.org/aabfj/article/id/914)
- A 2024 Review of Financial Studies model links the pattern to market-maker power: spreads widen when trading incentives rise, producing higher spreads and volumes at open and close. -- [RFS](https://academicnewsletter.sufe.edu.cn/info/351526)

### Inferences (Applied to NSE)
- **9:15-9:45 AM IST:** High volume, wide spreads, high volatility. Gap fills and opening range breakouts dominate. Avoid market orders on options; use limit orders.
- **10:00 AM - 12:00 PM IST:** Volume settles. Spreads narrow. Best window for mean-reversion strategies.
- **12:00 - 1:30 PM IST:** Volume trough. Spreads may widen for OTM options. Low-liquidity traps possible.
- **1:30 - 2:30 PM IST:** Volume picks up. Institutional activity increases. Trend initiation window.
- **2:30 - 3:30 PM IST:** Peak volume again. Spreads narrow for ATM. Closing moves can be sharp.
- **ATM vs OTM spreads:** ATM options have tightest spreads throughout the day. OTM options (especially >3-4 strikes away) can have 50-100% bid-ask spreads during midday lulls.

### Gaps
- No NSE-specific empirical study of intraday volume profile or bid-ask spread patterns for index options.
- No data on how ATM vs OTM spread ratios change through the day on NSE.

---

## Market Maker Behavior on NSE

### Takeaway
NSE does not have designated market makers for index options in the same sense as US exchanges. However, proprietary traders and algorithmic firms effectively fill this role. Their delta-hedging behavior is theoretically predictable but not empirically validated for NSE index options.

### Cited Findings
- US research: short-gamma dealers hedge by buying as spot rises and selling as it falls, amplifying moves. Long-gamma dealers do the opposite, absorbing moves. -- [OIData](https://www.oidata.in/blog/gamma-exposure-gex-explained)
- A 2003 model argues aggregate market-maker delta hedging can push the stock toward the strike near expiry. -- [CXOADVISORY](https://www.cxoadvisory.com/equity-options/stock-price-pinning-at-options-expiration)
- One unverified Indian source claims market makers' hedging activity was concentrated significantly in weekly expiry options, which by 2023 accounted for the vast majority of NSE's options premium turnover. -- [EquitiesIndia](https://equitiesindia.com/glossary/market-making)
- NSE's market-making framework exists but is primarily for single-stock options and illiquid contracts, not benchmark index options which already have sufficient natural liquidity.

### Inferences
- **Delta-hedging patterns that create predictable moves:** When net GEX is strongly positive, expect range-bound behavior as hedgers sell into strength and buy weakness. When net GEX is negative, expect trending behavior as hedging amplifies directional moves.
- **Expiry-day pattern:** As gamma concentrates at ATM on expiry, small index moves trigger large hedging flows, creating choppy, two-way action in a narrow range. Large directional breaks are less common but more violent.
- For algo use: monitor GEX regime (positive/negative) and adjust strategy type (mean-reversion vs trend-following) accordingly.

### Gaps
- No published study of actual delta-hedging behavior by NSE participants.
- Cannot determine the share of total NIFTY option OI held by market-making firms vs retail vs institutional.

---

## NSE Circuit Breaker Rules and Options Impact

### Takeaway
NSE's market-wide circuit breakers trigger at 10%, 15%, and 20% index moves (either direction) based on Sensex or NIFTY, whichever breaches first. All equity and derivatives trading halts simultaneously.

### Cited Findings
- Circuit breakers trigger at 10%, 15%, and 20% movement in either BSE Sensex or Nifty 50, whichever is breached earlier. -- [NSE](https://streamer.nseindia.com/products-services/equity-market-circuit-breakers)
- The halt covers equity derivatives as well as cash equities. Trading halt is coordinated across all equity and equity derivative markets nationwide. -- [NSE](https://streamer.nseindia.com/products-services/equity-market-circuit-breakers)
- Timing for 10% trigger: before 1:00 PM = 45-minute halt; 1:00-2:30 PM = 15-minute halt; at/after 2:30 PM = no halt. -- [TradeJini](https://tradejini.com/?p=13330)
- Market reopens after circuit filter breach with a pre-open call auction session. -- [NSE](https://streamer.nseindia.com/products-services/equity-market-circuit-breakers)
- System implemented in 2001, last modified by SEBI Circular CIR/MRD/DP/25/2013 dated September 3, 2013. -- [NSE](https://streamer.nseindia.com/products-services/equity-market-circuit-breakers)

### Inferences
- **Options impact:** During a trading halt, options cannot be traded. IV typically spikes on resumption as uncertainty reprices. Limit orders placed before the halt may execute at unfavorable prices on reopening.
- **For algo implementation:** Build in circuit breaker awareness. If NIFTY moves >7% intraday, reduce position sizing or flatten. Avoid holding large short gamma positions on days with >5% gap opens.
- The pre-open call auction after a halt can create significant price discovery -- options may reprice sharply.

### Gaps
- No 2025/2026 amendments to circuit breaker rules were found.
- No specific study of how options IV behaves through a circuit breaker event on NSE.

---

## Actionable Summary for Algo Implementation

### Data Sources to Consume
1. **NSE participant-wise OI** (daily, evening): FII/DII/Pro/Client positions across index futures and options
2. **NSE option chain** (real-time): OI, change in OI, volume, IV for all strikes
3. **GEX from OIData or similar** (real-time): regime indicator, flip level, walls
4. **Max pain** (real-time, recalculated as OI changes): convergence target for expiry day
5. **India VIX** (real-time): regime classifier for strategy selection

### Key Timing Windows (IST, Tuesday Expiry Day)
| Time | Characteristic | Strategy Implication |
|------|---------------|---------------------|
| 9:15-9:45 | High vol, wide spreads, gap reactions | Avoid entries; let price discover |
| 9:45-11:00 | Opening range established | Breakout/breakdown trades |
| 11:00-1:00 | Low vol, narrow range | Scalping, theta collection |
| 1:00-2:00 | Decay accelerates, institutional flow | Trend initiation; close longs |
| 2:00-3:00 | Max decay rate, gamma spikes | Short premium sweetspot; close by 2:30 if long |
| 3:00-3:30 | Final settlement dynamics | Max pin risk; avoid new positions |

### Signal Hierarchy (Strongest to Weakest)
1. **GEX regime** (positive = range, negative = trend) -- sets strategy type
2. **OI change at ATM strikes** -- real-time directional pressure
3. **PCR trend** (rising/falling over session) -- confirms or warns
4. **Max pain level** -- convergence target, strongest after 1 PM
5. **FII 5-day rolling flow** -- regime filter for swing bias

### Key Caution
- All GEX data for NSE relies on assumed sign conventions that cannot be verified.
- Max pain hit rates are likely 40-55% within +/-50 points, not the 70%+ some vendors claim.
- No strategy should rely on a single microstructure indicator. Confluence of 2-3 signals is the minimum.
