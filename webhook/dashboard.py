"""
Live HTML Dashboard — served at localhost:8000/dashboard

Reads shared state from the bot and renders a dark-themed dashboard
with category-wise P&L, open positions, signal history, and live stats.
Auto-refreshes every 30 seconds.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

IST = ZoneInfo("Asia/Kolkata")
router = APIRouter()

_state = {
    "mode": "paper",
    "started_at": "",
    "positions": [],
    "closed_trades": [],
    "signals": [],
    "scan_count": 0,
    "watching": [],
}


def update_state(**kwargs):
    _state.update(kwargs)


def _cat(symbol: str) -> str:
    s = symbol.upper()
    mcx = ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"]
    for m in mcx:
        if m in s:
            return "MCX"
    futs = ["NIFTY_FUT", "BANKNIFTY_FUT"]
    for f in futs:
        if f in s:
            return "INDEX_FUTURES"
    return "INDICES"


def _build_categories(closed_trades):
    cats = {}
    for t in closed_trades:
        c = _cat(t.get("symbol", ""))
        if c not in cats:
            cats[c] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "details": []}
        cats[c]["trades"] += 1
        cats[c]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) >= 0:
            cats[c]["wins"] += 1
        else:
            cats[c]["losses"] += 1
        cats[c]["details"].append(t)
    return cats


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    mode = _state.get("mode", "paper").upper()
    started = _state.get("started_at", "—")
    watching = _state.get("watching", [])
    scan_count = _state.get("scan_count", 0)

    positions = _state.get("positions", [])
    closed = _state.get("closed_trades", [])
    signals = _state.get("signals", [])

    total_pnl = sum(t.get("pnl", 0) for t in closed)
    total_trades = len(closed)
    wins = sum(1 for t in closed if t.get("pnl", 0) >= 0)
    losses = total_trades - wins
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    cats = _build_categories(closed)

    cat_cards = ""
    cat_order = ["INDICES", "MCX", "INDEX_FUTURES", "OTHER"]
    cat_icons = {"INDICES": "📊", "MCX": "🏭", "INDEX_FUTURES": "📈", "OTHER": "📦"}
    cat_colors = {"INDICES": "#6366f1", "MCX": "#f59e0b", "INDEX_FUTURES": "#10b981", "OTHER": "#6b7280"}

    for cat_name in cat_order:
        if cat_name not in cats:
            cats[cat_name] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "details": []}
        c = cats[cat_name]
        wr = (c["wins"] / c["trades"] * 100) if c["trades"] > 0 else 0
        pnl_color = "#10b981" if c["pnl"] >= 0 else "#ef4444"
        icon = cat_icons.get(cat_name, "📦")
        color = cat_colors.get(cat_name, "#6b7280")

        trades_html = ""
        for d in c.get("details", [])[-5:]:
            d_pnl_color = "#10b981" if d.get("pnl", 0) >= 0 else "#ef4444"
            trades_html += f'''<div class="trade-row">
                <span class="trade-symbol">{d.get("direction","")[:1]} {d.get("symbol","")}</span>
                <span>₹{d.get("entry",0):.2f} → ₹{d.get("exit",0):.2f}</span>
                <span style="color:{d_pnl_color};font-weight:600">₹{d.get("pnl",0):+,.2f}</span>
                <span class="trade-reason">{d.get("reason","")}</span>
            </div>'''

        if not trades_html:
            trades_html = '<div class="no-trades">No trades yet</div>'

        cat_cards += f'''
        <div class="cat-card">
            <div class="cat-header" style="border-left:4px solid {color}">
                <span class="cat-icon">{icon}</span>
                <span class="cat-name">{cat_name}</span>
            </div>
            <div class="cat-stats">
                <div class="stat">
                    <div class="stat-value" style="color:{pnl_color}">₹{c["pnl"]:+,.2f}</div>
                    <div class="stat-label">P&L</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{c["trades"]}</div>
                    <div class="stat-label">Trades</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{c["wins"]}/{c["losses"]}</div>
                    <div class="stat-label">W/L</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{wr:.0f}%</div>
                    <div class="stat-label">Win Rate</div>
                </div>
            </div>
            <div class="cat-trades">{trades_html}</div>
        </div>'''

    positions_html = ""
    for p in positions:
        pnl_color = "#10b981" if p.get("pnl", 0) >= 0 else "#ef4444"
        positions_html += f'''<div class="pos-row">
            <span class="pos-dir {'pos-long' if p.get('direction')=='LONG' else 'pos-short'}">{p.get("direction","")}</span>
            <span class="pos-symbol">{p.get("symbol","")}</span>
            <span>Entry: ₹{p.get("entry",0):.2f}</span>
            <span>LTP: ₹{p.get("current",0):.2f}</span>
            <span style="color:{pnl_color};font-weight:600">₹{p.get("pnl",0):+,.2f}</span>
            <span>SL: ₹{p.get("sl",0):.2f}</span>
        </div>'''
    if not positions_html:
        positions_html = '<div class="no-trades">No open positions</div>'

    signals_html = ""
    for s in reversed(signals[-15:]):
        side = s.get("side", "")
        side_class = "sig-buy" if "BUY" in side else ("sig-sell" if "SELL" in side else "sig-exit")
        signals_html += f'''<div class="sig-row">
            <span class="{side_class}">{side}</span>
            <span>{s.get("symbol","")}</span>
            <span class="sig-strategy">{s.get("strategy","")}</span>
            <span>SL: {s.get("sl",0)}</span>
        </div>'''
    if not signals_html:
        signals_html = '<div class="no-trades">Waiting for signals...</div>'

    total_pnl_color = "#10b981" if total_pnl >= 0 else "#ef4444"
    mode_color = "#10b981" if mode == "PAPER" else "#ef4444"

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>Omega Dashboard</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: #0a0a0f;
    color: #e2e8f0;
    min-height: 100vh;
}}
.header {{
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    padding: 24px 32px;
    border-bottom: 1px solid #1e293b;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 16px;
}}
.logo {{
    display: flex;
    align-items: center;
    gap: 12px;
}}
.logo-icon {{
    width: 40px;
    height: 40px;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
}}
.logo h1 {{
    font-size: 22px;
    font-weight: 700;
    background: linear-gradient(135deg, #c7d2fe, #a5b4fc);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}}
.header-meta {{
    display: flex;
    gap: 24px;
    font-size: 13px;
    color: #94a3b8;
}}
.mode-badge {{
    padding: 4px 12px;
    border-radius: 20px;
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
    background: {mode_color}22;
    color: {mode_color};
    border: 1px solid {mode_color}44;
}}
.top-stats {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    padding: 24px 32px;
}}
.top-stat {{
    background: #111827;
    border: 1px solid #1e293b;
    border-radius: 12px;
    padding: 20px;
    text-align: center;
}}
.top-stat-value {{
    font-size: 28px;
    font-weight: 700;
    margin-bottom: 4px;
}}
.top-stat-label {{
    font-size: 12px;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 1px;
}}
.container {{
    padding: 0 32px 32px;
}}
.section-title {{
    font-size: 16px;
    font-weight: 600;
    color: #94a3b8;
    margin: 24px 0 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
}}
.cat-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 16px;
}}
.cat-card {{
    background: #111827;
    border: 1px solid #1e293b;
    border-radius: 12px;
    overflow: hidden;
}}
.cat-header {{
    padding: 16px 20px;
    display: flex;
    align-items: center;
    gap: 10px;
    background: #0f172a;
}}
.cat-icon {{ font-size: 20px; }}
.cat-name {{
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}
.cat-stats {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    padding: 16px 20px;
    gap: 8px;
    border-bottom: 1px solid #1e293b;
}}
.stat {{ text-align: center; }}
.stat-value {{
    font-size: 18px;
    font-weight: 700;
}}
.stat-label {{
    font-size: 11px;
    color: #64748b;
    text-transform: uppercase;
    margin-top: 2px;
}}
.cat-trades {{
    padding: 12px 20px;
    max-height: 200px;
    overflow-y: auto;
}}
.trade-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 0;
    border-bottom: 1px solid #1e293b22;
    font-size: 13px;
    gap: 8px;
}}
.trade-symbol {{ font-weight: 600; min-width: 100px; }}
.trade-reason {{ color: #64748b; font-size: 11px; }}
.no-trades {{
    color: #475569;
    font-size: 13px;
    padding: 12px 0;
    text-align: center;
}}
.positions-box, .signals-box {{
    background: #111827;
    border: 1px solid #1e293b;
    border-radius: 12px;
    padding: 20px;
    margin-top: 16px;
}}
.pos-row, .sig-row {{
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 10px 0;
    border-bottom: 1px solid #1e293b22;
    font-size: 13px;
}}
.pos-dir {{
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
}}
.pos-long {{ background: #10b98122; color: #10b981; }}
.pos-short {{ background: #ef444422; color: #ef4444; }}
.pos-symbol {{ font-weight: 600; }}
.sig-buy {{ color: #10b981; font-weight: 700; min-width: 70px; }}
.sig-sell {{ color: #ef4444; font-weight: 700; min-width: 70px; }}
.sig-exit {{ color: #f59e0b; font-weight: 700; min-width: 70px; }}
.sig-strategy {{ color: #64748b; font-size: 11px; }}
.footer {{
    text-align: center;
    padding: 24px;
    color: #334155;
    font-size: 12px;
}}
.two-col {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
}}
@media (max-width: 768px) {{
    .header {{ padding: 16px; }}
    .top-stats {{ padding: 16px; grid-template-columns: repeat(2, 1fr); }}
    .container {{ padding: 0 16px 16px; }}
    .cat-grid {{ grid-template-columns: 1fr; }}
    .two-col {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>

<div class="header">
    <div class="logo">
        <div class="logo-icon">Ω</div>
        <h1>OMEGA TRADING SYSTEM</h1>
    </div>
    <div class="header-meta">
        <span class="mode-badge">{mode} MODE</span>
        <span>Started: {started}</span>
        <span>Watching: {", ".join(watching)}</span>
        <span>Scans: {scan_count}</span>
        <span>{now}</span>
    </div>
</div>

<div class="top-stats">
    <div class="top-stat">
        <div class="top-stat-value" style="color:{total_pnl_color}">₹{total_pnl:+,.2f}</div>
        <div class="top-stat-label">Total P&L</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{total_trades}</div>
        <div class="top-stat-label">Total Trades</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{wins}/{losses}</div>
        <div class="top-stat-label">Win / Loss</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{win_rate:.0f}%</div>
        <div class="top-stat-label">Win Rate</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{len(positions)}</div>
        <div class="top-stat-label">Open Positions</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{len(signals)}</div>
        <div class="top-stat-label">Signals Today</div>
    </div>
</div>

<div class="container">
    <div class="section-title">Category Performance</div>
    <div class="cat-grid">{cat_cards}</div>

    <div class="two-col">
        <div>
            <div class="section-title">Open Positions</div>
            <div class="positions-box">{positions_html}</div>
        </div>
        <div>
            <div class="section-title">Recent Signals</div>
            <div class="signals-box">{signals_html}</div>
        </div>
    </div>
</div>

<div class="footer">
    Omega Trading System &mdash; Auto-refreshes every 30s &mdash; Paper mode: no real money at risk
</div>

</body>
</html>'''
    return HTMLResponse(content=html)


@router.get("/api/state")
async def api_state():
    return _state
