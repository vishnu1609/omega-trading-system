# Gates: Spread SLTP tuning

OWNS: strategies/omega_complete.py (SPREAD_SLTP_LEVELS, lines 103-130, 1231-1237)

Scope: Update SLTP levels — TP at 55-65% credit decay, SL at 1.65x entry credit per research findings.

- [ ] G1: SPREAD_SLTP_LEVELS TP values are 55-65% range
  CHECK: cd /Users/vishnu/Desktop/pyhton\ p/omega-trading-system && python3 -c "exec(open('strategies/omega_complete.py').read().split('SPREAD_SLTP_LEVELS')[1].split('}')[0].split('{',1)[1])" 2>&1 || grep -A3 "tp_pct.*5[5-9]\|tp_pct.*6[0-5]" strategies/omega_complete.py | head -5
  EXPECT: tp_pct
  EVIDENCE: pending

- [ ] G2: Spread SL uses 1.65x multiplier
  CHECK: cd /Users/vishnu/Desktop/pyhton\ p/omega-trading-system && grep -c "1.65" strategies/omega_complete.py
  EXPECT: 1
  EVIDENCE: pending
