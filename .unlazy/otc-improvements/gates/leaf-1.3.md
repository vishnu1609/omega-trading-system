# Gates: Spread width config

OWNS: strategies/omega_complete.py (lines 1130-1170), core/option_chain.py (resolve_spread_pair)

Scope: Widen spread strikes per research: NIFTY 3-4 OTM steps (150-200pts), BANKNIFTY 4-5 OTM steps (400-500pts). Add min credit filter. Use delta-based sell leg selection.

- [ ] G1: SPREAD_CONFIG dict exists with per-underlying settings
  CHECK: cd /Users/vishnu/Desktop/pyhton\ p/omega-trading-system && grep -c "SPREAD_CONFIG" strategies/omega_complete.py
  EXPECT: 1
  EVIDENCE: pending

- [ ] G2: Min credit filter rejects no-credit spreads
  CHECK: cd /Users/vishnu/Desktop/pyhton\ p/omega-trading-system && grep -c "MIN_CREDIT" strategies/omega_complete.py
  EXPECT: 1
  EVIDENCE: pending
