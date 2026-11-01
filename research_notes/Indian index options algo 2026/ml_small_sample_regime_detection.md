# ML for Small-Sample Trade Signal Classification & Advanced Regime Detection

## ML Models for <500 Labeled Trades

### Takeaway
Gradient-boosted trees (LightGBM/XGBoost) with heavy regularization and logistic regression are the practical choices at <500 samples; Bayesian methods offer principled uncertainty but add complexity. Below ~200 samples, no model produces reliable out-of-sample predictions for trade classification.

### Cited Findings
- A financial data stream classification study found performance becomes unstable below ~200 training samples — [arXiv 1307.7244](https://arxiv.org/pdf/1307.7244)
- LightGBM's leaf-wise growth can overfit on small datasets if not carefully tuned (max_depth, min_data_in_leaf, subsample) — [Medium: Understanding LightGBM](https://medium.com/@jeremychrist23/understanding-lightgbm-a-performance-boosting-algorithm-for-machine-learning-756a1edb38c4)
- SmallML (2025 preprint) proposes Bayesian transfer learning achieving enterprise-level accuracy with 50-200 observations, though tested on churn data, not trading — [arXiv 2511.14049](https://arxiv.org/html/2511.14049v1)
- A Brazilian equities study found LightGBM consistently outperformed buy-and-hold for bullish reversal prediction — [SBC/SEMISH proceedings](https://sol.sbc.org.br/index.php/semish/article/download/43537/43300/)
- An Orebro University working paper found LightGBM slightly better than XGBoost for prediction accuracy in cold-start settings — [EconPapers](https://econpapers.repec.org/paper/hhsoruesi/2024_5f002.htm)

### Inferences
- **Recommended stack for <500 trades:**
  - **Logistic regression with L1/L2** — baseline, interpretable, resists overfitting with <200 samples
  - **LightGBM** — use with `max_depth=3-5`, `min_data_in_leaf=20-50`, `feature_fraction=0.5`, `bagging_fraction=0.7`, `lambda_l1/l2` regularization. At <300 samples, restrict to <15 features
  - **XGBoost** — similar performance to LightGBM; slight edge with `dart` booster for small data
  - **Bayesian logistic regression** (PyMC/Stan) — gives calibrated probabilities and uncertainty intervals, useful for bet-sizing
- **Minimum sample sizes (practical guidance):**
  - Logistic regression: ~50-100 per class (100-200 total)
  - Tree ensembles: ~150-250 per class (300-500 total)
  - Neural networks: not recommended below 5,000-10,000 samples
- At 45-55% win rate with 500 trades, the class imbalance is mild; SMOTE or class weights are unnecessary and may introduce noise. Focus on proper calibration instead.

### Gaps
- No controlled comparison of these models specifically at N=300-500 on intraday options data exists in the literature
- No study found comparing R-multiple gates vs probability gates for signal filtering


## Class Imbalance Handling at Small Sample Sizes

### Takeaway
With 45-55% win rate and <500 samples, the "imbalance" is negligible. The real problem is that many trades cluster near breakeven, making the classification boundary noisy. Focus on label quality (triple-barrier with meaningful thresholds) rather than resampling.

### Cited Findings
- Triple-barrier labeling (from Lopez de Prado's AFML) assigns labels based on which barrier (profit-take, stop-loss, or time-expiry) is hit first, producing cleaner labels than fixed-horizon returns — referenced in [wraquant docs](https://wraquant.readthedocs.io/en/latest/tutorials/ml_alpha_research.html)
- Meta-labeling: train a primary model for direction, then a secondary model that predicts whether the primary's signal will be profitable, outputting a probability used for bet-sizing — [wraquant ML docs](https://wraquant.readthedocs.io/en/stable/api/ml.html)

### Inferences
- **Label engineering > resampling:** Use triple-barrier with barriers set at meaningful levels (e.g., 1x ATR profit-take, 0.7x ATR stop-loss) to create unambiguous labels
- **Meta-labeling** is the best approach for small samples: the secondary model only needs to learn P(profit | signal), which is an easier task than learning the full signal
- For the 45-55% split, use `scale_pos_weight` in LightGBM only if the split drifts beyond 40-60%

### Gaps
- No empirical comparison of meta-labeling vs direct classification on Indian index options with <500 samples


## Feature Selection to Prevent Overfitting (50 Features, <500 Samples)

### Takeaway
With a 10:1 sample-to-feature ratio, aggressive dimensionality reduction is mandatory. Use clustered feature importance with purged cross-validation, and target <15 active features.

### Cited Findings
- MDI (Mean Decrease Impurity) feature importance understates duplicated predictors; MDA (Mean Decrease Accuracy) can erase them entirely. Permuting clustered groups of correlated columns together is recommended — [MQL5 article on feature importance](https://www.mql5.com/en/articles/23564)
- Purged cross-validation removes training observations whose information intervals overlap test intervals, critical when using triple-barrier labels with variable-length holding periods — [QuantT: Purged CV](https://www.quantt.co.uk/resources/purged-cross-validation)
- Lopez de Prado shows that after trying only 7 strategy configurations, a researcher can find a 2-year backtest with Sharpe >1 when true OOS Sharpe is 0 — [Bailey & Lopez de Prado, "Pseudo-Mathematics and Financial Charlatanism"](https://scholarworks.wmich.edu/math_pubs/40/)

### Inferences
- **Pipeline for 50 features, <500 samples:**
  1. Cluster features by correlation (Spearman) into ~10-15 groups
  2. Within each cluster, pick the feature with highest single-feature MDA (purged CV)
  3. Run forward stepwise selection with purged CV, adding features only if OOS log-loss improves by >0.5%
  4. Final model should use 8-15 features max
- **Validation protocol:** Purged K-fold (K=5) with embargo gap = max holding period. Walk-forward with expanding window as secondary check
- **Deflated Sharpe Ratio** should be used to adjust for multiple testing when comparing feature sets

### Gaps
- Exact performance of clustered MDA vs standard MDA on small trading datasets not quantified in any source found


## Transfer Learning and Pre-Training for Trading

### Takeaway
No mature, validated transfer learning framework exists for intraday options trading. The most practical approach is "warm-starting" from a model trained on a larger historical dataset, then fine-tuning on the target regime.

### Cited Findings
- SmallML proposes Bayesian transfer learning for small datasets (50-200 samples), using informative priors from a source domain — [arXiv 2511.14049](https://arxiv.org/html/2511.14049v1)
- Incremental learning of stock trends via meta-learning with dynamic adaptation proposes learning-to-learn across market conditions — [arXiv 2401.03865](https://arxiv.org/pdf/2401.03865)

### Inferences
- **Practical bootstrap approach for Omega:**
  1. Train a LightGBM on 2+ years of historical NIFTY/BANKNIFTY 5-min bar features (10K+ samples) with weak labels (next-bar return sign)
  2. Extract the learned feature importances and tree structure as "prior knowledge"
  3. Fine-tune on <500 labeled trades using higher learning rate + fewer boosting rounds
  4. Alternative: use the historical model's predictions as an additional feature (stacking)
- Cross-index transfer (NIFTY -> BANKNIFTY -> SENSEX) is plausible since regime features (ATR percentile, ER, volume profile) are instrument-agnostic

### Gaps
- No validated evidence that transfer learning across Indian indices improves small-sample classification


## Online Learning / Incremental Updates

### Takeaway
Bayesian online updating and SGD with exponential decay are both viable for sub-second inference. The key trade-off is stability vs adaptability: aggressive adaptation chases noise, while slow adaptation misses regime shifts.

### Cited Findings
- A Bayesian method for online training of neural networks for forex toxic-trade prediction reports <1ms per update-and-predict cycle — [Oxford thesis](https://ora.ox.ac.uk/objects/uuid:709b4275-affc-4e4b-8dce-3642f9a2ce70)
- A PhD thesis on online portfolio selection developed Bayesian treatment of online gradient descent enabling automatic, data-dependent hyperparameter tuning — [Oxford thesis](https://ora.ox.ac.uk/objects/uuid:fdf755f9-c71b-45ea-a688-cadf7af1f1ed)
- Online learning models use constant memory and constant work per bar — [LuxAlgo: Online Learning](https://www.luxalgo.com/library/concept/online-incremental-learning.md)

### Inferences
- **Recommended approach for Omega:**
  - Use LightGBM for daily/weekly batch retraining (full model rebuild with expanding window)
  - Use Bayesian logistic regression (online mode) for intra-day signal probability updates
  - Exponential decay weight on training samples: `sample_weight = exp(-lambda * age_in_days)` with `lambda = 0.01-0.05`
  - Track model calibration (Brier score) on a rolling 50-trade window; trigger full retrain if Brier score degrades >10%
- SGDClassifier from sklearn supports `partial_fit()` for true online updates, but trees do not natively support this

### Gaps
- No head-to-head comparison of online Bayesian vs batch SGD for intraday options signal classification


## Counterfactual / Shadow Logging for Blocked Signals

### Takeaway
Shadow logging requires recording the features, predicted signal, and the market outcome for trades the model recommended but the gate blocked. The key challenge is that the gate's decision creates selection bias; inverse propensity scoring or randomized exploration can debias the training data.

### Cited Findings
- Lawrence et al. (EMNLP 2017) show counterfactual learning from deterministic bandit logs is possible using additive and multiplicative control variates — [arXiv 1707.09118](https://arxiv.org/abs/1707.09118)
- Stripe's approach to counterfactual evaluation proposes injecting randomness in production to generate unbiased training data — [PyData 2015](https://pydata.org/seattle2015/schedule/presentation/17/)
- Standard correction is inverse propensity scoring: weight each logged outcome by 1/P(action was taken) — [arXiv 1707.09118](https://arxiv.org/abs/1707.09118)

### Inferences
- **Shadow logging implementation for Omega:**
  1. Log every signal the ML model produces, including those blocked by regime gates
  2. For blocked signals, record: timestamp, features, predicted direction, predicted probability, gate state, and reason for blocking
  3. Track what the market did after the block: did price hit the would-be TP or SL first?
  4. For debiased training: use **inverse propensity weighting** — if the gate blocks 40% of signals, weight blocked-but-would-have-won signals by 1/0.4 = 2.5x
  5. Simpler alternative: **epsilon-greedy exploration** — let 5-10% of blocked signals through at minimum size to get unbiased outcomes
  6. Use shadow outcomes to retrain the gate itself, not just the signal model
- The epsilon-greedy approach is safer than full shadow deployment because it bounds the cost of exploration

### Gaps
- No paper directly addresses shadow logging of blocked trades in the context of options trading
- The optimal exploration rate (epsilon) for trading systems is not established in the literature


## Hidden Markov Models for Indian Market Regime Detection

### Takeaway
A 2-state GaussianHMM on daily returns is the baseline; 3-state models (bull/bear/sideways) are preferred by practitioners but harder to fit with intraday data. Indian-market-specific evidence is limited to a single student project using 120 days of Zerodha data.

### Cited Findings
- QuantInsti EPAT project: 2-state GaussianHMM on 120 days of intraday data from 15 Indian instruments (NIFTY50, HDFCBANK, RELIANCE, etc.). Low-vol regime = trade, high-vol = stay flat. TCS moved from loss to profit; ONGC, INFY, LT, SBIN saw sharply reduced losses — [QuantInsti Blog](https://blog.quantinsti.com/market-regime-detection-hidden-markov-model-project-fahim/)
- QuantConnect 3-state HMM for intraday: Sharpe ratio 1.1-1.9 across parameter variations on 2020 US data (in-sample only) — [QuantConnect Forum](https://quantconnect.com/forum/discussion/17900)
- HMM with side information for intraday momentum trading — [arXiv 2006.08307](https://arxiv.org/pdf/2006.08307)

### Inferences
- **Recommended HMM setup for Omega:**
  - **States:** 3 (trending-up, trending-down, ranging/choppy)
  - **Observation model:** GaussianHMM from `hmmlearn`, with 2-3 features: log returns, realized volatility (rolling 20-bar), volume ratio
  - **Covariance type:** `full` for 3 features, `diag` if using >5 features
  - **Training:** Rolling 60-day window of 5-min bars (~4,500 observations per window), retrain daily
  - **Inference:** Viterbi decoding for most-likely state sequence; forward algorithm for real-time P(state)
  - **Regime gating:** Only take signals when P(trending) > 0.6; exit when P(ranging) > 0.5
  - Implementation: `hmmlearn.GaussianHMM(n_components=3, covariance_type='full', n_iter=100)`
- Transition probabilities are learned from data; expect high self-transition probability (>0.9) reflecting regime persistence

### Gaps
- No out-of-sample performance numbers for HMM on NIFTY/BANKNIFTY intraday options
- Optimal number of states for Indian index options not empirically determined


## Online Changepoint Detection (BOCPD, PELT)

### Takeaway
BOCPD detects regime shifts online without prespecifying the number of regimes. It maintains a posterior over "run length" (time since last change) and is well-suited for real-time trading. Detection lag is typically 5-20 bars depending on the hazard function.

### Cited Findings
- Tsaknaki, Lillo & Mazzarisi propose score-driven BOCPD with temporal correlation within regimes, reporting better out-of-sample prediction than i.i.d. models on NASDAQ data — [arXiv 2307.02375](https://arxiv.org/html/2307.02375v2); published in [Quantitative Finance 2025](https://ideas.repec.org/a/taf/quantf/v25y2025i2p307-322.html)
- BOCPD doesn't need a preset number of regimes and operates online rather than segmenting data after the fact — [arXiv 2307.02375](https://arxiv.org/html/2307.02375v2)
- Wood, Roberts & Zohren insert online changepoint detection into a Deep Momentum Network to handle trend reversals — [arXiv 2105.13727](https://arxiv.org/pdf/2105.13727.pdf)
- BOCPD maintains a posterior over run length; a regime break shows as probability mass collapsing toward zero run length — [MQL5 BOCPD README](https://forge.mql5.io/ayantrader/BOCPD/raw/branch/main/README.md)

### Inferences
- **BOCPD implementation for Omega:**
  - Use `bayesian_changepoint_detection` Python package or Meta's `kats` library
  - Observation model: Normal-Inverse-Gamma conjugate prior on 5-min returns
  - Hazard function: constant hazard `1/lambda` where `lambda` = expected regime length (e.g., 250 bars = ~4 trading hours)
  - Detection speed: tunable via hazard rate — shorter lambda = faster detection but more false positives
  - Real-time: O(T) memory for exact inference; use truncated run-length for O(1) by keeping only the last R=500 run lengths
- **PELT** (Pruned Exact Linear Time) is better for offline batch analysis; use it for end-of-day regime labeling and training data preparation
- **Practical integration:** BOCPD fires a "regime change" event when P(run_length < 5) > 0.5; this can gate the HMM or override the static ADX gate

### Gaps
- No study measures BOCPD detection lag specifically on NIFTY/BANKNIFTY 5-min bars
- Comparison of BOCPD vs PELT detection speed on Indian market data not found


## Kalman Filter Based Regime Classification

### Takeaway
An adaptive Kalman filter with Efficiency Ratio-scaled process noise provides smooth regime classification with built-in responsiveness tuning. The ER threshold of 0.40 separating trending from ranging is a common default.

### Cited Findings
- Adaptive Kalman Trend Filter (MQL5): ER scales process noise via linear blend between min/max multipliers (0.5x to 5.0x) on base Q=0.005. Regime bands = rolling StdDev of residual * 1.5, widened as ER falls. Regime threshold: ER > 0.40 = trending — [MQL5 Code 76149](https://www.mql5.com/en/code/76149)
- Cadence Veil indicator: combines H-Infinity filter, R-squared gating, and Kaufman adaptive efficiency with a hysteresis state machine preventing rapid regime flip-flopping. Five market states classified — [TradingView: Cadence Veil](https://vn.tradingview.com/scripts/kaufmans)

### Inferences
- **Kalman regime filter for Omega:**
  - State: estimated "true price"; observation: actual close
  - Process noise Q = base_Q * (min_mult + ER * (max_mult - min_mult))
  - When ER high: Q large, filter tracks price closely (trending)
  - When ER low: Q small, filter smooths heavily (ranging)
  - Regime bands: Kalman_line +/- k * rolling_std(residual), where k inversely proportional to ER
  - **Hysteresis implementation:** Use separate thresholds for entering and exiting trending state:
    - Enter trending: ER > 0.45 for 3+ consecutive bars
    - Exit trending: ER < 0.35 for 3+ consecutive bars
    - Dead zone 0.35-0.45: maintain current state
  - This prevents flip-flopping at the boundary
- Default parameters (ER period=20, band period=20, band mult=1.5, regime threshold=0.40) are reasonable starting points for 5-min bars

### Gaps
- No backtested performance of Kalman-ER regime classification on Indian indices
- Optimal hysteresis thresholds for NIFTY/BANKNIFTY not established empirically


## Ensemble Regime Detection

### Takeaway
No single regime detector is reliable alone. Combining ADX velocity, Choppiness Index, HMM state probabilities, and volatility clustering into a voting or weighted ensemble with hysteresis is the recommended approach.

### Cited Findings
- Volatility Regime Clustering (TradingView): k-means on ATR values, 2-4 clusters, requires 3+ consecutive confirmations before regime change — [TradingView: Volatility Regime Clustering](https://in.tradingview.com/script/aXyFjDVd-Volatility-Regime-Clustering)
- BOCPD provides a probability of regime change that can be combined with indicator-based detection — [arXiv 2307.02375](https://arxiv.org/html/2307.02375v2)

### Inferences
- **Ensemble regime detector for Omega (proposed architecture):**

  ```
  Regime Score = w1*ADX_signal + w2*CHOP_signal + w3*HMM_prob + w4*BOCPD_signal + w5*ER_signal
  ```

  Where each component outputs a value in [0, 1] (0 = ranging, 1 = trending):
  
  | Component | Signal Calculation | Weight |
  |-----------|-------------------|--------|
  | ADX velocity | ADX > 25 AND ADX rising = 1.0; ADX > 25 AND falling = 0.5; else 0 | 0.20 |
  | Choppiness | 1 - (CHOP - 38.2) / (61.8 - 38.2), clamped [0,1] | 0.15 |
  | HMM state | P(trending state) from GaussianHMM | 0.25 |
  | BOCPD | 1 - P(run_length < threshold) when in trending; P(run_length < threshold) when in ranging | 0.25 |
  | Kaufman ER | (ER - 0.3) / (0.6 - 0.3), clamped [0,1] | 0.15 |

  - **Hysteresis:** Enter trending when score > 0.60 for 2+ bars; exit to ranging when score < 0.40 for 2+ bars
  - Dead zone [0.40, 0.60]: maintain current regime state
  - **Override:** BOCPD changepoint (P > 0.8) forces immediate regime reassessment regardless of ensemble score

- **Key regime features ranked by expected importance:**
  1. ATR percentile (20-bar ATR vs 100-bar ATR distribution)
  2. Kaufman Efficiency Ratio (20-bar)
  3. Candle body ratio (|close-open| / high-low, smoothed)
  4. Volume profile (current volume / 20-bar SMA volume)
  5. Tick entropy (Shannon entropy of up/down ticks in rolling window) — theoretical value high but implementation complexity also high
  
### Gaps
- No empirical study compares ensemble regime detection vs single-indicator gating for Indian index options
- Optimal weights for the ensemble components not established — would need to be learned from historical regime-labeled data
- Tick-by-tick entropy calculation feasibility not confirmed for NIFTY/BANKNIFTY real-time feeds


## Overfitting Prevention and Validation Protocol

### Takeaway
With <500 trades, the probability of backtest overfitting is very high even with few parameter variations. Purged cross-validation, the Deflated Sharpe Ratio, and minimum backtest length calculations are essential safeguards.

### Cited Findings
- After trying only 7 strategy configurations, a researcher can find a 2-year backtest with Sharpe >1 when true OOS Sharpe is 0 — [Bailey & Lopez de Prado](https://scholarworks.wmich.edu/math_pubs/40/)
- Standard holdout methods don't control for the number of trials attempted — [eScholarship: Probability of Backtest Overfitting](https://escholarship.org/content/qt4w1110bb/qt4w1110bb.pdf)
- The Deflated Sharpe Ratio adjusts for non-normal returns and multiple testing — [Bailey & Lopez de Prado, 2014](https://www.davidhbailey.com/dhbpapers/overfitting.pdf)

### Inferences
- **Validation protocol for Omega ML layer:**
  1. **Purged K-Fold CV** (K=5) with embargo = max holding period (e.g., 30 bars for ~2.5hr max hold)
  2. **Walk-forward validation:** Train on expanding window, test on next 50 trades, step forward by 25
  3. **Deflated Sharpe Ratio** to adjust reported performance for number of model variants tried
  4. **Minimum Backtest Length:** With N trials, MinBTL = N * (expected_SR / target_SR)^2 years approximately
  5. **Combinatorial Purged CV (CPCV):** generates multiple train/test combinations without information leakage — most robust method from AFML
  6. **Rule of thumb:** If you've tried >10 feature/parameter combinations on 500 trades, your OOS estimates are likely inflated by >30%

### Gaps
- Deflated Sharpe Ratio implementation for options P&L (which has fat tails and asymmetric returns) not specifically addressed
