# Gates: BANKNIFTY expiry fix

OWNS: core/option_chain.py

Scope: Ensure BANKNIFTY spread uses monthly expiry (weeklies discontinued Nov 2024). Add get_monthly_expiry method and use it for BANKNIFTY spreads.

- [ ] G1: get_monthly_expiry exists and returns monthly expiry for BANKNIFTY
  CHECK: cd /Users/vishnu/Desktop/pyhton\ p/omega-trading-system && grep -c "def get_monthly_expiry" core/option_chain.py
  EXPECT: 1
  EVIDENCE: pending

- [ ] G2: resolve_spread_pair uses monthly expiry for BANKNIFTY when no expiry given
  CHECK: cd /Users/vishnu/Desktop/pyhton\ p/omega-trading-system && grep -c "get_monthly_expiry" core/option_chain.py
  EXPECT: 2
  EVIDENCE: pending
