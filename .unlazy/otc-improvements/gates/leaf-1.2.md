# Gates: Entry time windows

OWNS: strategies/omega_complete.py (lines 355-455 timing section)

Scope: Block LONG option buys during 11:00-13:45 midday lull unless score >= 70. Add -8 penalty already exists but enforce hard block for weak signals. Block new 0-DTE entries after 14:30.

- [ ] G1: Midday hard block exists for BUY_CE/BUY_PE between 11:00-13:45 when score < 70
  CHECK: cd /Users/vishnu/Desktop/pyhton\ p/omega-trading-system && grep -c "MIDDAY_BLOCK" strategies/omega_complete.py
  EXPECT: 1
  EVIDENCE: pending

- [ ] G2: 0-DTE entries blocked after 14:30 IST
  CHECK: cd /Users/vishnu/Desktop/pyhton\ p/omega-trading-system && grep -c "0DTE_LATE_BLOCK" strategies/omega_complete.py
  EXPECT: 1
  EVIDENCE: pending
