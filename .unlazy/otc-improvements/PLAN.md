# Plan: OTC Research-Driven Improvements

Scope: otc-improvements
Depth: tree 4
Mode: orchestrated

## Contract

- Interfaces: spread entry via `_handle_spread_entry`, timing via `_process_signal`, SLTP via `SPREAD_SLTP_LEVELS` and monitor loop, option chain via `resolve_spread_pair`
- Ownership: see dispatch table
- Dependencies: leaf 1.1 (expiry) and 1.2 (timing) are independent; leaf 1.3 (spread width) needs 1.1; leaf 1.4 (SLTP) needs 1.3
- Host launch mode: sequential fallback
- Toolchain: Python 3.11+, zsh, VPS deploy via SSH/systemd
- Conventions: IST timezone, all times in 24h format, log.info for actions, log.warning for blocks

## Current contract inventory

Contract revision: 1.

| ID | Required outcome or constraint | Owner | Observing gate or manual review | Disposition | Revision |
|---|---|---|---|---|---|
| C1 | BANKNIFTY spread expiry selection uses monthly (not weekly) | leaf-1.1 | leaf-1.1:G1, leaf-1.1:G2 | ACTIVE | 1 |
| C2 | LONG option entries blocked during 11:00-13:45 midday lull (score hurdle) | leaf-1.2 | leaf-1.2:G1, leaf-1.2:G2 | ACTIVE | 1 |
| C3 | Spread width widened: NIFTY 150-200pts, BANKNIFTY 400-500pts with min credit filter | leaf-1.3 | leaf-1.3:G1, leaf-1.3:G2 | ACTIVE | 1 |
| C4 | Spread SLTP: TP at 55-65% credit decay, SL at 1.65x entry credit | leaf-1.4 | leaf-1.4:G1, leaf-1.4:G2 | ACTIVE | 1 |
| C5 | All changes deployed to VPS and bot restarted | node-1.0 | node-1.0:G1 | ACTIVE | 1 |

## Tree

- 1 OTC improvements .............. GATES.md
  - 1.1 BANKNIFTY expiry fix ...... gates/leaf-1.1.md
  - 1.2 Entry time windows ........ gates/leaf-1.2.md
  - 1.3 Spread width config ....... gates/leaf-1.3.md
  - 1.4 Spread SLTP tuning ........ gates/leaf-1.4.md
  - 1.0 Deploy integration ........ gates/node-1.0.md

## Leaf dispatch table

| Leaf | Owns | Needs | Tier | Planned wave | State |
|---|---|---|---|---|---|
| 1.1 | core/option_chain.py | - | mechanical | 1 | READY |
| 1.2 | strategies/omega_complete.py:355-455 | - | judgment | 1 | READY |
| 1.3 | strategies/omega_complete.py:1130-1170, core/option_chain.py:538-587 | 1.1 | mechanical | 2 | WAITING |
| 1.4 | strategies/omega_complete.py:103-130,1231-1237 | 1.3 | mechanical | 2 | WAITING |
