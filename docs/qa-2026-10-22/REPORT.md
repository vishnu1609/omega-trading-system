# Omega end-to-end audit — 22 October 2026

**Result: FAIL; full browser interaction coverage is blocked.** The dashboard renders at http://localhost:8000/dashboard, but not every control can be certified as working.

## Environment and scope

- No service was listening on port 8000 initially. Started the actual dashboard router in an isolated FastAPI harness on 127.0.0.1:8000.
- The harness uses synthetic LONG/SHORT positions, a fake broker, a temporary SQLite database, and temporary state under `/tmp/omega-e2e`. It does not launch the trading loop or load `.env` credentials.
- The browser loaded the dashboard and its polling data successfully. Desktop screenshot: `dashboard-desktop.png`.
- Browser click, evaluate, and arbitrary Playwright execution requests were rejected by automatic approval review: `MCP tool call requires approval, but approval policy is never`. No click-based pass claims are made.
- Existing source modifications were preserved. Added this audit, regression tests, and results only. New tests mock broker/DB/wallet dependencies and isolate file writes using pytest temporary directories.
- This does not validate live broker execution, live market feeds, deployment, actual option-chain availability, strategy profitability, or a full signal-to-live-fill lifecycle.

## Results

| Check | Passed | Failed | Setup errors |
|---|---:|---:|---:|
| Existing `tests/` suite | 98 | 32 | 8 |
| New isolated dashboard/webhook audit | 27 | 19 | 0 |

The new 46 checks cover read endpoints, CSRF, normal/invalid SL and TP edits, broker failure, actual fills, rejected/pending/partial exits, spread exits, notes, CSV, option-chain fixtures, analytics, tab dispatch, empty-database reverse analysis, webhook authentication/validation, and queue capacity.

Dashboard JavaScript passes `node --check`. Browser console reported a missing `/favicon.ico` (404), with no page script error reported during the initial load.

## Confirmed findings

| Priority | Finding | Evidence / consequence |
|---|---|---|
| P1 | Close and Close All remove positions before confirming full broker execution | Both endpoints returned success and removed positions for REJECTED, PENDING and 4/10 partial fills; SL cancellation and local accounting proceed despite remaining market exposure. `webhook/dashboard.py:261,371`. |
| P1 | Spread exit omits the hedge leg | With hedge security ID 202, both exit endpoints submitted only primary security ID 101. The hedge remains open while the local spread disappears. `webhook/dashboard.py:245,361`. |
| P1 | SL edit hides broker failure | A broker modification exception still returns `ok:true` and changes the local stop from 80 to 90. The broker stop can remain at 80. `webhook/dashboard.py:454`. |
| P1 | NaN/Infinity corrupt position price state | String non-finite values pass `<= 0` validation, get assigned to the position, then response serialization fails with HTTP 500. Other malformed values also cause 500. `webhook/dashboard.py:442,479`. |
| P2 | Close All records quoted price instead of fill | Quote 110, confirmed fill 112: closed trade records 110 and P&L 100 rather than 120 for 10 units. `webhook/dashboard.py:371,379`. |
| P2 | Reverse Engine tab has no render dispatch | The navigation declares `data-t="reverse"`, but `renderTab()` has no matching case. `rReverse()` is only called from `runReverse()`, so the initial Run Analysis button never renders through navigation. Source-level regression test fails. `webhook/dashboard.py:1467,2534`. |
| P2 | Notes cannot be cleared | Save a note, then submit empty text: backend rejects it and retains the old note. UI save handler ignores the response. `webhook/dashboard.py:503,2169`. |
| P2 | CSV is not escaped | Comma in symbol and newline in reason produce extra columns/rows. `webhook/dashboard.py:531`. |
| P2 | Invalid webhook numeric fields cause HTTP 500 | Signed payloads with `strike:"bad"` or `sl:null` bypass structural validation and fail numeric conversion. `webhook/tv_receiver.py:115`. |
| P2 | Negative rupee values omit the minus sign | Initial rendered dashboard displayed a losing position's -200 P&L as `₹200.00`; helper `fpnl` applies `Math.abs` and only prefixes positive numbers. Red color alone communicates the loss. `webhook/dashboard.py:1333`. |

## Additional source-review concerns (not browser-reproduced)

- Option-chain expiry switching: inline handler reads `window._chainUl`, which is assigned only once to NIFTY; changing index updates a different function-scoped variable. Expiry changes after BANKNIFTY/SENSEX can request NIFTY instead. The index variable also becomes a URL with a query string. Lines 2180–2183, 2198, 2730.
- Analytics refresh: once `_anaData` exists, `rAnalytics()` renders cached data without fetching; resetting `_anaLoaded` after rendering does not trigger a reload. New closed trades can leave charts stale. Lines 1380, 2255–2269.
- Poll refreshes only Dashboard, Positions, Trades and Signals. Other tabs can retain snapshots until revisited. Lines 1384–1391.
- SL/TP saves have no fetch error handler or in-flight disable; browser network-error feedback and duplicate submission behavior remain unverified.

## Dashboard control coverage

| Control | What was verified | Remaining |
|---|---|---|
| Dashboard | Real browser render, polling data, initial console | Close double-click confirmation/timing |
| Positions | Close API full-fill success; failure cases above | Book Profit, Close Loss, Close All click flow |
| SL/TP edit, Save, Cancel | API normal values and invalid/failure paths | Modal open/save/cancel/backdrop/focus behavior |
| Trades, Export CSV, notes | Read API, CSV response and escaping failure, note persistence/deletion failure | Download interaction and debounced typing |
| Signals | Source route/render wiring | Empty/populated view interaction |
| Option Chain: NIFTY/BANKNIFTY/SENSEX, expiry | Fixture-backed endpoint responses for all three | Actual button/expiry interaction and live data |
| Analytics | Cumulative P&L response | Chart rendering and refresh |
| Architecture, ML Engine, Risk, Goal | Navigation and render dispatch exist | Browser tab content and responsiveness |
| Reverse Engine, Run Analysis, Re-run | Missing dispatch confirmed; backend runs on isolated empty DB | Browser flow blocked by missing dispatch and tool restriction |

No mobile/responsive pass, keyboard-accessibility pass, or all-buttons pass is claimed.

## Existing suite interpretation

The existing failures must not all be treated as production defects:

- Eight setup errors use removed `RiskEngine` constructor parameters (`max_trades_per_day`, `allow_mcx_live`).
- Many position tests use IDs such as `N-1`, `SEC_0`, and `BIG_LOSS`, now rejected by numeric security-ID validation; later assertions consequently fail.
- Several tests reference removed MCX fields, changed instance-method signatures, changed allocation wording, and prior journal/lesson persistence behavior.
- Trailing-stop threshold and pattern-detection failures still require reconciliation against intended strategy behavior.

See `existing-suite.txt` for all individual failures; tests were not weakened or rewritten to turn them green.

## Reproduce

From the repository root:

```sh
python3 -m pytest tests -q --disable-warnings --tb=short
python3 -m pytest docs/qa-2026-10-22/test_dashboard_audit.py -q --tb=short
```

The new audit intentionally remains red until the defects are fixed. Prioritize confirmed fill/reconciliation handling, spread leg closure, and atomic broker/local stop updates before using dashboard exit controls for live trading.
