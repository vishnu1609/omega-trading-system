"""
Omega Quantitative Modules — Research Reference Implementation

Source: Gemini Deep Research + ChatGPT Deep Research cross-referenced output (Oct 2026).
Contains standalone implementations for future integration:

1. OptionsChandelierExitEngine — Theta-compensated, profit-tiered Chandelier exit
2. MicrostructureRegimeEngine — Kalman filter + KER/CI/ADX hysteresis regime classifier
3. OptionsTimingAndGreeksGate — Intraday U-shape timing + IV slope + delta-velocity
4. ConsensusScoringEngine — 6 orthogonal factors replacing 15-factor system
5. SelfHealingMLEngine — SQLite shadow logging + LightGBM + SGD meta-adapter
6. CrossIndexPortfolioCoordinator — NIFTY/BNF divergence veto + Half-Kelly sizing
7. VectorizedLocalGreeksEngine — Sub-ms Black-Scholes on WebSocket ticks
8. SmallAccountRiskGovernor — Anti-martingale + Dhan kill-switch

IMPORTANT: This file is a REFERENCE. Active implementations are integrated into:
  - core/risk_engine.py (evaluate_trail with R-multiple deadband)
  - strategies/omega_complete.py (time-of-day modifiers, trail integration)
  - Future: core/signal_generator.py (regime classifier, 6-factor scoring)

Status: Chandelier exit + time-of-day modifiers implemented.
Pending: Full regime engine, 6-factor scoring, ML self-healing buffer, cross-index veto.
"""

# Full research code preserved below for incremental integration.
# See docs/parallel-research-prompts.md for the original research prompts.
# See memory/project_omega_research_findings.md for the priority roadmap.
