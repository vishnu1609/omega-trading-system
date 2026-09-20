"""
Live HTML Dashboard — served at localhost:8000/dashboard

Dark-themed dashboard with:
  - Top-line stats + advanced indicators (Sharpe, max drawdown, avg R:R, streak)
  - P&L heatmap (hourly performance grid)
  - Open positions + recent signals with score bars
  - Category-wise P&L breakdown
  - Trade leaderboard (best & worst trades ranked)

Auto-refreshes every 30 seconds.
"""
from __future__ import annotations

import json
from collections import defaultdict
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


def _compute_advanced_stats(closed):
    """Compute Sharpe-like ratio, max drawdown, avg R:R, streaks, profit factor."""
    if not closed:
        return {
            "sharpe": 0, "max_dd": 0, "max_dd_pct": 0,
            "avg_win": 0, "avg_loss": 0, "rr_ratio": 0,
            "best_streak": 0, "worst_streak": 0, "current_streak": 0,
            "current_streak_type": "",
            "profit_factor": 0, "expectancy": 0,
            "avg_hold_pnl": 0,
        }

    pnls = [t.get("pnl", 0) for t in closed]
    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    rr_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    mean_pnl = sum(pnls) / len(pnls)
    variance = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls) if len(pnls) > 1 else 0
    std_pnl = variance ** 0.5
    sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

    expectancy = mean_pnl

    best_streak = worst_streak = current_streak = 0
    current_type = ""
    run = 0
    last_sign = None
    for p in pnls:
        sign = "W" if p >= 0 else "L"
        if sign == last_sign:
            run += 1
        else:
            run = 1
        last_sign = sign
        if sign == "W":
            best_streak = max(best_streak, run)
        else:
            worst_streak = max(worst_streak, run)
    current_streak = run
    current_type = last_sign or ""

    initial_capital = 100_000
    max_dd_pct = (max_dd / initial_capital * 100) if initial_capital > 0 else 0

    return {
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 2),
        "max_dd_pct": round(max_dd_pct, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "rr_ratio": round(rr_ratio, 2),
        "best_streak": best_streak,
        "worst_streak": worst_streak,
        "current_streak": current_streak,
        "current_streak_type": current_type,
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
    }


def _build_heatmap(closed):
    """Build hourly P&L heatmap data for the trading day."""
    hours = {}
    for h in range(9, 24):
        hours[h] = {"pnl": 0.0, "trades": 0, "wins": 0}

    for t in closed:
        entry = t.get("entry_time")
        if isinstance(entry, str):
            try:
                entry = datetime.fromisoformat(entry)
            except Exception:
                entry = None
        if not entry:
            continue
        h = entry.hour if hasattr(entry, "hour") else 12
        if h in hours:
            hours[h]["pnl"] += t.get("pnl", 0)
            hours[h]["trades"] += 1
            if t.get("pnl", 0) >= 0:
                hours[h]["wins"] += 1

    # Also map trades by approximate exit time via index in list
    if not any(hours[h]["trades"] for h in hours):
        # Fallback: distribute trades evenly across market hours by list position
        nse_hours = list(range(9, 16))
        mcx_hours = list(range(9, 24))
        for i, t in enumerate(closed):
            sym = t.get("symbol", "").upper()
            is_mcx = any(m in sym for m in ["GOLD", "SILVER", "CRUDE", "NATURAL"])
            pool = mcx_hours if is_mcx else nse_hours
            h = pool[i % len(pool)]
            hours[h]["pnl"] += t.get("pnl", 0)
            hours[h]["trades"] += 1
            if t.get("pnl", 0) >= 0:
                hours[h]["wins"] += 1

    return hours


def _build_leaderboard(closed, top_n=10):
    """Rank trades by P&L — best and worst."""
    if not closed:
        return [], []
    sorted_trades = sorted(closed, key=lambda t: t.get("pnl", 0), reverse=True)
    best = sorted_trades[:top_n]
    worst = sorted_trades[-top_n:][::-1]
    return best, worst


def _build_memory_panel(memory_state: dict) -> str:
    if not memory_state:
        return ""

    health = memory_state.get("health", {})
    insights = memory_state.get("insights", [])
    status = health.get("status", "NO_DATA")

    status_colors = {
        "HEALTHY": "#10b981", "DEGRADED": "#f59e0b",
        "CRITICAL": "#ef4444", "NO_DATA": "#6b7280",
    }
    status_icons = {
        "HEALTHY": "🧠", "DEGRADED": "⚠️",
        "CRITICAL": "🚨", "NO_DATA": "💤",
    }
    sc = status_colors.get(status, "#6b7280")
    si = status_icons.get(status, "🧠")

    stats_html = f"""
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:12px;">
        <div class="stat-card" style="border-color:{sc}">
            <div class="stat-value" style="color:{sc}">{si} {status}</div>
            <div class="stat-label">BRAIN STATUS</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{health.get('trades', 0)}</div>
            <div class="stat-label">TRADES LEARNED</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{health.get('active_lessons', 0)}</div>
            <div class="stat-label">USER LESSONS</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{health.get('active_rules', 0)}</div>
            <div class="stat-label">AUTO RULES</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{health.get('overall_win_rate', 0):.0%}</div>
            <div class="stat-label">LEARNED WR</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{health.get('recommended_score_threshold', 55):.0f}</div>
            <div class="stat-label">REC. THRESHOLD</div>
        </div>
    </div>"""

    insights_html = ""
    type_icons = {
        "critical": "🚨", "warning": "⚠️", "insight": "💡",
        "lesson": "📖", "rule": "🤖", "info": "ℹ️",
    }
    type_colors = {
        "critical": "#ef4444", "warning": "#f59e0b", "insight": "#6366f1",
        "lesson": "#10b981", "rule": "#3b82f6", "info": "#6b7280",
    }
    for item in insights[:8]:
        itype = item.get("type", "info")
        icon = type_icons.get(itype, "ℹ️")
        color = type_colors.get(itype, "#6b7280")
        msg = item.get("message", "")
        insights_html += f"""
        <div style="display:flex;align-items:center;gap:8px;padding:6px 10px;
                    background:rgba({','.join(str(int(color.lstrip('#')[i:i+2], 16)) for i in (0,2,4))},0.1);
                    border-left:3px solid {color};border-radius:4px;margin-bottom:6px;font-size:13px;">
            <span>{icon}</span>
            <span style="color:#cbd5e1;">{msg}</span>
        </div>"""

    if not insights_html:
        insights_html = '<div style="color:#64748b;font-size:13px;padding:8px;">No insights yet — learning starts after first closed trade</div>'

    return f"""
    <div class="section-title">🧠 Self-Learning Engine</div>
    <div class="panel-box">
        {stats_html}
        <div style="margin-top:8px;">
            <div style="font-size:12px;color:#64748b;text-transform:uppercase;margin-bottom:6px;">Insights &amp; Learned Patterns</div>
            {insights_html}
        </div>
    </div>"""


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
    unrealised = sum(p.get("pnl", 0) for p in positions)

    cats = _build_categories(closed)
    stats = _compute_advanced_stats(closed)
    heatmap = _build_heatmap(closed)
    best_trades, worst_trades = _build_leaderboard(closed)
    memory_panel = _build_memory_panel(_state.get("memory", {}))

    # ── Category Cards ──
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

    # ── P&L Heatmap ──
    heatmap_cells = ""
    pnl_values = [heatmap[h]["pnl"] for h in heatmap if heatmap[h]["trades"] > 0]
    max_abs = max(abs(v) for v in pnl_values) if pnl_values else 1

    for h in range(9, 24):
        data = heatmap[h]
        pnl = data["pnl"]
        trades = data["trades"]

        if trades == 0:
            bg = "#1e293b"
            text_color = "#475569"
        elif pnl > 0:
            intensity = min(pnl / max_abs, 1.0) if max_abs > 0 else 0
            alpha = 0.15 + intensity * 0.55
            bg = f"rgba(16,185,129,{alpha:.2f})"
            text_color = "#10b981"
        else:
            intensity = min(abs(pnl) / max_abs, 1.0) if max_abs > 0 else 0
            alpha = 0.15 + intensity * 0.55
            bg = f"rgba(239,68,68,{alpha:.2f})"
            text_color = "#ef4444"

        label = f"{h:02d}:00"
        wr_h = (data["wins"] / trades * 100) if trades > 0 else 0

        heatmap_cells += f'''<div class="heat-cell" style="background:{bg}">
            <div class="heat-hour">{label}</div>
            <div class="heat-pnl" style="color:{text_color}">{"₹" + f"{pnl:+,.0f}" if trades > 0 else "—"}</div>
            <div class="heat-trades">{f"{trades}T · {wr_h:.0f}%" if trades > 0 else ""}</div>
        </div>'''

    # ── Positions ──
    positions_html = ""
    for p in positions:
        pnl_color = "#10b981" if p.get("pnl", 0) >= 0 else "#ef4444"
        entry = p.get("entry", 0)
        current = p.get("current", 0)
        pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
        if p.get("direction") == "SHORT":
            pnl_pct = -pnl_pct
        positions_html += f'''<div class="pos-row">
            <span class="pos-dir {'pos-long' if p.get('direction')=='LONG' else 'pos-short'}">{p.get("direction","")}</span>
            <span class="pos-symbol">{p.get("symbol","")}</span>
            <span>₹{entry:.2f}</span>
            <span>₹{current:.2f}</span>
            <span style="color:{pnl_color};font-weight:600">₹{p.get("pnl",0):+,.2f}</span>
            <span style="color:{pnl_color};font-size:11px">{pnl_pct:+.1f}%</span>
            <span class="pos-sl">SL ₹{p.get("sl",0):.2f}</span>
        </div>'''
    if not positions_html:
        positions_html = '<div class="no-trades">No open positions</div>'

    # ── Signals ──
    signals_html = ""
    for s in reversed(signals[-15:]):
        side = s.get("side", "")
        side_class = "sig-buy" if "BUY" in side else ("sig-sell" if "SELL" in side else "sig-exit")
        score = s.get("score", 0)
        conf = s.get("confidence", "")
        conf_class = "conf-high" if conf == "HIGH" else ("conf-med" if conf == "MEDIUM" else "conf-low")
        score_bar_w = min(score, 100)
        score_color = "#10b981" if score >= 75 else ("#f59e0b" if score >= 55 else "#ef4444")
        signals_html += f'''<div class="sig-row">
            <span class="{side_class}">{side}</span>
            <span class="sig-symbol">{s.get("symbol","")}</span>
            <span class="sig-score-wrap">
                <span class="sig-score-bar" style="width:{score_bar_w}%;background:{score_color}"></span>
                <span class="sig-score-num">{score:.0f}</span>
            </span>
            <span class="{conf_class}">{conf}</span>
        </div>'''
    if not signals_html:
        signals_html = '<div class="no-trades">Waiting for signals...</div>'

    # ── Leaderboard ──
    def _lb_row(t, rank, medal=""):
        pnl = t.get("pnl", 0)
        pc = "#10b981" if pnl >= 0 else "#ef4444"
        entry = t.get("entry", 0)
        exit_p = t.get("exit", 0)
        pnl_pct = ((exit_p - entry) / entry * 100) if entry > 0 else 0
        d = t.get("direction", "")[:1]
        return f'''<div class="lb-row">
            <span class="lb-rank">{medal}{rank}</span>
            <span class="lb-symbol">{d} {t.get("symbol","")}</span>
            <span>₹{entry:.2f} → ₹{exit_p:.2f}</span>
            <span style="color:{pc};font-weight:700">₹{pnl:+,.2f}</span>
            <span style="color:{pc};font-size:11px">{pnl_pct:+.1f}%</span>
            <span class="lb-reason">{t.get("reason","")}</span>
        </div>'''

    best_html = ""
    medals = ["🥇", "🥈", "🥉"] + [""] * 10
    for i, t in enumerate(best_trades[:5]):
        best_html += _lb_row(t, i + 1, medals[i])
    if not best_html:
        best_html = '<div class="no-trades">No trades yet</div>'

    worst_html = ""
    for i, t in enumerate(worst_trades[:5]):
        worst_html += _lb_row(t, i + 1, "💀" if i == 0 else "")
    if not worst_html:
        worst_html = '<div class="no-trades">No trades yet</div>'

    # ── Color vars ──
    total_pnl_color = "#10b981" if total_pnl >= 0 else "#ef4444"
    mode_color = "#10b981" if mode == "PAPER" else "#ef4444"
    streak_icon = "🔥" if stats["current_streak_type"] == "W" else "❄️" if stats["current_streak_type"] == "L" else ""
    sharpe_color = "#10b981" if stats["sharpe"] >= 1 else ("#f59e0b" if stats["sharpe"] >= 0 else "#ef4444")
    pf_color = "#10b981" if stats["profit_factor"] >= 1.5 else ("#f59e0b" if stats["profit_factor"] >= 1 else "#ef4444")
    unr_color = "#10b981" if unrealised >= 0 else "#ef4444"

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
.logo {{ display:flex; align-items:center; gap:12px; }}
.logo-icon {{
    width:40px; height:40px;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    border-radius:10px;
    display:flex; align-items:center; justify-content:center;
    font-size:20px;
}}
.logo h1 {{
    font-size:22px; font-weight:700;
    background: linear-gradient(135deg, #c7d2fe, #a5b4fc);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent;
}}
.header-meta {{ display:flex; gap:24px; font-size:13px; color:#94a3b8; flex-wrap:wrap; }}
.mode-badge {{
    padding:4px 12px; border-radius:20px; font-weight:600; font-size:12px;
    text-transform:uppercase;
    background:{mode_color}22; color:{mode_color}; border:1px solid {mode_color}44;
}}

/* ── Top Stats ── */
.top-stats {{
    display:grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap:12px; padding:20px 32px;
}}
.top-stat {{
    background:#111827; border:1px solid #1e293b; border-radius:12px;
    padding:16px; text-align:center;
}}
.top-stat-value {{ font-size:24px; font-weight:700; margin-bottom:2px; }}
.top-stat-label {{
    font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:1px;
}}
.top-stat-sub {{ font-size:10px; color:#475569; margin-top:2px; }}

/* ── Section ── */
.container {{ padding:0 32px 32px; }}
.section-title {{
    font-size:15px; font-weight:600; color:#94a3b8;
    margin:24px 0 12px; text-transform:uppercase; letter-spacing:1px;
}}

/* ── Heatmap ── */
.heatmap-grid {{
    display:grid;
    grid-template-columns: repeat(auto-fill, minmax(80px, 1fr));
    gap:6px;
}}
.heat-cell {{
    border-radius:8px; padding:10px 6px; text-align:center;
    border:1px solid #1e293b;
    transition: transform 0.15s;
}}
.heat-cell:hover {{ transform:scale(1.05); }}
.heat-hour {{ font-size:11px; color:#94a3b8; font-weight:600; }}
.heat-pnl {{ font-size:15px; font-weight:700; margin:4px 0 2px; }}
.heat-trades {{ font-size:10px; color:#64748b; }}

/* ── Category Cards ── */
.cat-grid {{
    display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:16px;
}}
.cat-card {{
    background:#111827; border:1px solid #1e293b; border-radius:12px; overflow:hidden;
}}
.cat-header {{
    padding:14px 20px; display:flex; align-items:center; gap:10px; background:#0f172a;
}}
.cat-icon {{ font-size:20px; }}
.cat-name {{ font-size:15px; font-weight:600; letter-spacing:0.5px; }}
.cat-stats {{
    display:grid; grid-template-columns:repeat(4,1fr);
    padding:14px 20px; gap:8px; border-bottom:1px solid #1e293b;
}}
.stat {{ text-align:center; }}
.stat-value {{ font-size:17px; font-weight:700; }}
.stat-label {{ font-size:11px; color:#64748b; text-transform:uppercase; margin-top:2px; }}
.cat-trades {{ padding:10px 20px; max-height:180px; overflow-y:auto; }}
.trade-row {{
    display:flex; justify-content:space-between; align-items:center;
    padding:7px 0; border-bottom:1px solid #1e293b22; font-size:13px; gap:8px;
}}
.trade-symbol {{ font-weight:600; min-width:100px; }}
.trade-reason {{ color:#64748b; font-size:11px; }}
.no-trades {{ color:#475569; font-size:13px; padding:12px 0; text-align:center; }}

/* ── Positions / Signals ── */
.panel-box {{
    background:#111827; border:1px solid #1e293b; border-radius:12px; padding:16px;
}}
.pos-row, .sig-row {{
    display:flex; align-items:center; gap:12px;
    padding:8px 0; border-bottom:1px solid #1e293b22; font-size:13px;
}}
.pos-dir {{
    padding:2px 8px; border-radius:4px; font-size:11px; font-weight:700;
}}
.pos-long {{ background:#10b98122; color:#10b981; }}
.pos-short {{ background:#ef444422; color:#ef4444; }}
.pos-symbol {{ font-weight:600; }}
.pos-sl {{ color:#64748b; font-size:11px; }}
.sig-buy {{ color:#10b981; font-weight:700; min-width:70px; }}
.sig-sell {{ color:#ef4444; font-weight:700; min-width:70px; }}
.sig-exit {{ color:#f59e0b; font-weight:700; min-width:70px; }}
.sig-symbol {{ font-weight:600; min-width:90px; }}
.sig-score-wrap {{
    position:relative; width:80px; height:18px;
    background:#1e293b; border-radius:4px; overflow:hidden;
}}
.sig-score-bar {{
    position:absolute; top:0; left:0; height:100%; border-radius:4px; opacity:0.3;
}}
.sig-score-num {{
    position:relative; z-index:1; font-size:11px; font-weight:700;
    line-height:18px; padding-left:6px;
}}
.conf-high {{ color:#10b981; font-weight:700; font-size:11px; }}
.conf-med {{ color:#f59e0b; font-weight:700; font-size:11px; }}
.conf-low {{ color:#ef4444; font-weight:700; font-size:11px; }}

/* ── Leaderboard ── */
.lb-grid {{
    display:grid; grid-template-columns:1fr 1fr; gap:16px;
}}
.lb-box {{
    background:#111827; border:1px solid #1e293b; border-radius:12px; overflow:hidden;
}}
.lb-header {{
    padding:12px 20px; font-size:14px; font-weight:700; background:#0f172a;
    display:flex; align-items:center; gap:8px;
    border-bottom:1px solid #1e293b;
}}
.lb-body {{ padding:8px 16px; }}
.lb-row {{
    display:flex; align-items:center; gap:10px;
    padding:8px 0; border-bottom:1px solid #1e293b22; font-size:13px;
}}
.lb-rank {{ font-weight:700; min-width:30px; color:#64748b; }}
.lb-symbol {{ font-weight:600; min-width:110px; }}
.lb-reason {{ color:#64748b; font-size:11px; }}

/* ── Layout ── */
.two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
.three-col {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; }}
.footer {{
    text-align:center; padding:24px; color:#334155; font-size:12px;
}}
@media (max-width: 900px) {{
    .header {{ padding:16px; }}
    .top-stats {{ padding:12px; grid-template-columns:repeat(3,1fr); }}
    .container {{ padding:0 16px 16px; }}
    .cat-grid, .two-col, .lb-grid {{ grid-template-columns:1fr; }}
    .heatmap-grid {{ grid-template-columns:repeat(5,1fr); }}
}}
@media (max-width: 500px) {{
    .top-stats {{ grid-template-columns:repeat(2,1fr); }}
    .heatmap-grid {{ grid-template-columns:repeat(3,1fr); }}
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

<!-- ── TOP STATS ── -->
<div class="top-stats">
    <div class="top-stat">
        <div class="top-stat-value" style="color:{total_pnl_color}">₹{total_pnl:+,.2f}</div>
        <div class="top-stat-label">Realised P&L</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value" style="color:{unr_color}">₹{unrealised:+,.2f}</div>
        <div class="top-stat-label">Unrealised</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{total_trades}</div>
        <div class="top-stat-label">Trades</div>
        <div class="top-stat-sub">{wins}W / {losses}L</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{win_rate:.0f}%</div>
        <div class="top-stat-label">Win Rate</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value" style="color:{sharpe_color}">{stats["sharpe"]}</div>
        <div class="top-stat-label">Sharpe Ratio</div>
        <div class="top-stat-sub">{"Good" if stats["sharpe"] >= 1 else "Needs work"}</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value" style="color:{pf_color}">{stats["profit_factor"]}x</div>
        <div class="top-stat-label">Profit Factor</div>
        <div class="top-stat-sub">Win$ / Loss$</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value" style="color:#ef4444">₹{stats["max_dd"]:,.0f}</div>
        <div class="top-stat-label">Max Drawdown</div>
        <div class="top-stat-sub">{stats["max_dd_pct"]:.1f}% of capital</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{stats["rr_ratio"]:.1f}:1</div>
        <div class="top-stat-label">Avg R:R</div>
        <div class="top-stat-sub">₹{stats["avg_win"]:,.0f} / ₹{stats["avg_loss"]:,.0f}</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{streak_icon} {stats["current_streak"]}</div>
        <div class="top-stat-label">Current Streak</div>
        <div class="top-stat-sub">Best: {stats["best_streak"]}W · Worst: {stats["worst_streak"]}L</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value" style="color:{'#10b981' if stats['expectancy'] >= 0 else '#ef4444'}">₹{stats["expectancy"]:+,.0f}</div>
        <div class="top-stat-label">Expectancy</div>
        <div class="top-stat-sub">Avg P&L per trade</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{len(positions)}</div>
        <div class="top-stat-label">Open</div>
    </div>
    <div class="top-stat">
        <div class="top-stat-value">{len(signals)}</div>
        <div class="top-stat-label">Signals</div>
    </div>
</div>

<div class="container">

    <!-- ── P&L HEATMAP ── -->
    <div class="section-title">P&L Heatmap — Hourly Performance</div>
    <div class="heatmap-grid">{heatmap_cells}</div>

    <!-- ── CATEGORY PERFORMANCE ── -->
    <div class="section-title">Category Performance</div>
    <div class="cat-grid">{cat_cards}</div>

    <!-- ── LEADERBOARD ── -->
    <div class="section-title">Trade Leaderboard</div>
    <div class="lb-grid">
        <div class="lb-box">
            <div class="lb-header">🏆 Best Trades</div>
            <div class="lb-body">{best_html}</div>
        </div>
        <div class="lb-box">
            <div class="lb-header">💀 Worst Trades</div>
            <div class="lb-body">{worst_html}</div>
        </div>
    </div>

    <!-- ── POSITIONS / SIGNALS ── -->
    <div class="two-col">
        <div>
            <div class="section-title">Open Positions</div>
            <div class="panel-box">{positions_html}</div>
        </div>
        <div>
            <div class="section-title">Recent Signals</div>
            <div class="panel-box">{signals_html}</div>
        </div>
    </div>

    <!-- ── SELF-LEARNING ENGINE ── -->
    {memory_panel}

</div>

<div class="footer">
    Omega Trading System &mdash; Auto-refreshes every 30s &mdash; {mode} mode
</div>

</body>
</html>'''
    return HTMLResponse(content=html)


@router.get("/api/state")
async def api_state():
    return _state
