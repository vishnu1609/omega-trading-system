"""
Live HTML Dashboard — served at /dashboard

Two-system view: INDICES and MCX, each with its own stats.
Uses a shared JSON file so both bots can push state independently.
Auto-refreshes every 1 second.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

IST = ZoneInfo("Asia/Kolkata")
router = APIRouter()

STATE_FILE = Path(__file__).parent.parent / "data" / "dashboard_state.json"


def _read_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _write_state(data: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str))
    tmp.replace(STATE_FILE)


def update_state(system: str = "indices", **kwargs):
    state = _read_state()
    if system not in state:
        state[system] = {}
    state[system].update(kwargs)
    state[system]["last_update"] = datetime.now(IST).isoformat()
    _write_state(state)


def _stats(closed: list[dict]) -> dict:
    if not closed:
        return {"sharpe": 0, "max_dd": 0, "max_dd_pct": 0, "avg_win": 0,
                "avg_loss": 0, "rr_ratio": 0, "best_streak": 0,
                "worst_streak": 0, "current_streak": 0, "streak_type": "",
                "profit_factor": 0, "expectancy": 0}

    pnls = [t.get("pnl", 0) for t in closed]
    wins = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    rr = avg_win / avg_loss if avg_loss > 0 else 0
    mean = sum(pnls) / len(pnls)
    var = sum((p - mean) ** 2 for p in pnls) / len(pnls) if len(pnls) > 1 else 0
    std = var ** 0.5
    sharpe = mean / std if std > 0 else 0

    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)

    gp = sum(wins) if wins else 0
    gl = abs(sum(losses)) if losses else 0
    pf = gp / gl if gl > 0 else 0

    best_s = worst_s = run = 0
    last = None
    for p in pnls:
        s = "W" if p >= 0 else "L"
        run = run + 1 if s == last else 1
        last = s
        if s == "W":
            best_s = max(best_s, run)
        else:
            worst_s = max(worst_s, run)

    return {"sharpe": round(sharpe, 2), "max_dd": round(dd), "max_dd_pct": round(dd / 1000, 1),
            "avg_win": round(avg_win), "avg_loss": round(avg_loss), "rr_ratio": round(rr, 2),
            "best_streak": best_s, "worst_streak": worst_s, "current_streak": run,
            "streak_type": last or "", "profit_factor": round(pf, 2), "expectancy": round(mean)}


def _build_risk_html(risk: dict) -> str:
    if not risk:
        return ""
    zone = risk.get("risk_zone", "SAFE")
    zone_colors = {
        "SAFE": "#10b981", "ELEVATED": "#f59e0b",
        "WARNING": "#f97316", "CRITICAL": "#ef4444",
    }
    zc = zone_colors.get(zone, "#64748b")
    pnl = risk.get("daily_pnl", 0)
    pnl_c = "#10b981" if pnl >= 0 else "#ef4444"
    loss_pct = risk.get("daily_loss_used_pct", 0)
    bar_w = min(loss_pct, 100)
    bar_c = "#10b981" if loss_pct < 30 else ("#f59e0b" if loss_pct < 50 else ("#f97316" if loss_pct < 80 else "#ef4444"))
    killed = risk.get("bot_killed", False)
    return f'''
        <div class="sec-title">Risk Monitor</div>
        <div class="panel" style="border-left:3px solid {zc}">
            <div style="display:flex;align-items:center;gap:14px;margin-bottom:12px;flex-wrap:wrap">
                <span style="background:{zc}22;color:{zc};border:1px solid {zc}44;padding:4px 12px;border-radius:20px;font-weight:700;font-size:12px">{zone}{"  BOT KILLED" if killed else ""}</span>
                <span style="color:{pnl_c};font-weight:700;font-size:16px">₹{pnl:+,.2f}</span>
                <span style="color:#64748b;font-size:12px">({risk.get("daily_pnl_pct", 0):+.2f}%)</span>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px">
                <div>
                    <div style="color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:1px">Daily Loss Used</div>
                    <div style="position:relative;height:18px;background:#1e293b;border-radius:4px;margin-top:4px;overflow:hidden">
                        <div style="position:absolute;top:0;left:0;height:100%;width:{bar_w}%;background:{bar_c};opacity:0.4;border-radius:4px"></div>
                        <span style="position:relative;z-index:1;font-size:12px;font-weight:700;line-height:18px;padding-left:6px">{loss_pct:.0f}%</span>
                    </div>
                </div>
                <div>
                    <div style="color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:1px">Trades Today</div>
                    <div style="font-size:18px;font-weight:700;margin-top:2px">{risk.get("trades_today", 0)} <span style="color:#64748b;font-size:12px">/ {risk.get("trades_today", 0) + risk.get("trades_remaining", 6)}</span></div>
                </div>
                <div>
                    <div style="color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:1px">Consecutive Losses</div>
                    <div style="font-size:18px;font-weight:700;margin-top:2px;color:{"#ef4444" if risk.get("consecutive_losses", 0) >= 2 else "#e2e8f0"}">{risk.get("consecutive_losses", 0)}</div>
                </div>
                <div>
                    <div style="color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:1px">Recovery Needed</div>
                    <div style="font-size:18px;font-weight:700;margin-top:2px;color:{"#ef4444" if risk.get("drawdown_recovery_pct", 0) > 5 else "#e2e8f0"}">{risk.get("drawdown_recovery_pct", 0):.1f}%</div>
                </div>
            </div>
        </div>'''


def _build_wallet_html(w: dict) -> str:
    if not w:
        return ""
    bal = w.get("balance", 0)
    avail = w.get("available", 0)
    in_t = w.get("in_trade", 0)
    tp = w.get("today_pnl", 0)
    start_cap = w.get("starting_capital", 200000)
    total_pnl = bal - start_cap
    tp_c = "#10b981" if tp >= 0 else "#ef4444"
    tot_c = "#10b981" if total_pnl >= 0 else "#ef4444"
    pct = (total_pnl / start_cap * 100) if start_cap > 0 else 0
    return f'''
        <div class="wallet-bar">
            <div class="wallet-item">
                <div class="wallet-lbl">BALANCE</div>
                <div class="wallet-val">₹{bal:,.2f}</div>
            </div>
            <div class="wallet-item">
                <div class="wallet-lbl">AVAILABLE</div>
                <div class="wallet-val" style="color:#a5b4fc">₹{avail:,.2f}</div>
            </div>
            <div class="wallet-item">
                <div class="wallet-lbl">IN TRADE</div>
                <div class="wallet-val" style="color:#fbbf24">₹{in_t:,.2f}</div>
            </div>
            <div class="wallet-item">
                <div class="wallet-lbl">TODAY P&L</div>
                <div class="wallet-val" style="color:{tp_c}">₹{tp:+,.2f}</div>
            </div>
            <div class="wallet-item">
                <div class="wallet-lbl">TOTAL P&L</div>
                <div class="wallet-val" style="color:{tot_c}">₹{total_pnl:+,.2f} <span style="font-size:12px">({pct:+.1f}%)</span></div>
            </div>
        </div>'''


def _build_system_html(sys_data: dict, sys_name: str, color: str, icon: str) -> str:
    mode = sys_data.get("mode", "paper").upper()
    started = sys_data.get("started_at", "—")
    watching = sys_data.get("watching", [])
    scan_count = sys_data.get("scan_count", 0)
    positions = sys_data.get("positions", [])
    closed = sys_data.get("closed_trades", [])
    signals = sys_data.get("signals", [])
    last_update = sys_data.get("last_update", "—")
    risk = sys_data.get("risk_metrics", {})
    wallet = sys_data.get("wallet", {})

    total_pnl = sum(t.get("pnl", 0) for t in closed)
    total_trades = len(closed)
    wins = sum(1 for t in closed if t.get("pnl", 0) >= 0)
    losses = total_trades - wins
    wr = (wins / total_trades * 100) if total_trades > 0 else 0
    unr = sum(p.get("pnl", 0) for p in positions)
    st = _stats(closed)

    pnl_c = "#10b981" if total_pnl >= 0 else "#ef4444"
    unr_c = "#10b981" if unr >= 0 else "#ef4444"
    mode_c = "#10b981" if mode == "PAPER" else "#ef4444"
    sharpe_c = "#10b981" if st["sharpe"] >= 1 else ("#f59e0b" if st["sharpe"] >= 0 else "#ef4444")
    pf_c = "#10b981" if st["profit_factor"] >= 1.5 else ("#f59e0b" if st["profit_factor"] >= 1 else "#ef4444")
    streak_icon = "🔥" if st["streak_type"] == "W" else "❄️" if st["streak_type"] == "L" else ""

    # Positions
    pos_html = ""
    for p in positions:
        pc = "#10b981" if p.get("pnl", 0) >= 0 else "#ef4444"
        dc = "pos-long" if p.get("direction") == "LONG" else "pos-short"
        entry = p.get("entry", 0)
        cur = p.get("current", 0)
        pct = ((cur - entry) / entry * 100) if entry > 0 else 0
        if p.get("direction") == "SHORT":
            pct = -pct
        pos_html += f'''<div class="pos-row">
            <span class="pos-dir {dc}">{p.get("direction","")}</span>
            <span class="pos-symbol">{p.get("symbol","")}</span>
            <span>₹{entry:.2f} → ₹{cur:.2f}</span>
            <span style="color:{pc};font-weight:600">₹{p.get("pnl",0):+,.2f}</span>
            <span style="color:{pc};font-size:11px">{pct:+.1f}%</span>
            <span class="pos-sl">SL ₹{p.get("sl",0):.2f}</span>
        </div>'''
    if not pos_html:
        pos_html = '<div class="no-data">No open positions</div>'

    # Signals
    sig_html = ""
    for s in reversed(signals[-15:]):
        side = s.get("side", "")
        sc = "sig-buy" if "BUY" in side else ("sig-sell" if "SELL" in side else "sig-exit")
        score = s.get("score", 0)
        conf = s.get("confidence", "")
        cc = "conf-high" if conf == "HIGH" else ("conf-med" if conf == "MEDIUM" else "conf-low")
        bar_c = "#10b981" if score >= 75 else ("#f59e0b" if score >= 55 else "#ef4444")
        sig_html += f'''<div class="sig-row">
            <span class="{sc}">{side}</span>
            <span class="sig-sym">{s.get("symbol","")}</span>
            <span class="score-wrap">
                <span class="score-bar" style="width:{min(score,100)}%;background:{bar_c}"></span>
                <span class="score-num">{score:.0f}</span>
            </span>
            <span class="{cc}">{conf}</span>
        </div>'''
    if not sig_html:
        sig_html = '<div class="no-data">Waiting for signals...</div>'

    # Trades
    trades_html = ""
    for t in reversed(closed[-20:]):
        tc = "#10b981" if t.get("pnl", 0) >= 0 else "#ef4444"
        d = t.get("direction", "")[:1]
        trades_html += f'''<div class="trade-row">
            <span class="trade-sym">{d} {t.get("symbol","")}</span>
            <span>₹{t.get("entry",0):.2f} → ₹{t.get("exit",0):.2f}</span>
            <span style="color:{tc};font-weight:600">₹{t.get("pnl",0):+,.2f}</span>
            <span class="trade-reason">{t.get("reason","")}</span>
        </div>'''
    if not trades_html:
        trades_html = '<div class="no-data">No trades yet</div>'

    # Leaderboard
    sorted_t = sorted(closed, key=lambda t: t.get("pnl", 0), reverse=True)
    best = sorted_t[:5]
    worst = sorted_t[-5:][::-1] if len(sorted_t) > 0 else []
    medals = ["🥇", "🥈", "🥉", "", ""]

    def lb_row(t, i, m=""):
        p = t.get("pnl", 0)
        c = "#10b981" if p >= 0 else "#ef4444"
        return f'''<div class="lb-row">
            <span class="lb-rank">{m}{i+1}</span>
            <span class="lb-sym">{t.get("direction","")[:1]} {t.get("symbol","")}</span>
            <span style="color:{c};font-weight:700">₹{p:+,.2f}</span>
            <span class="lb-reason">{t.get("reason","")}</span>
        </div>'''

    best_html = "".join(lb_row(t, i, medals[i]) for i, t in enumerate(best)) or '<div class="no-data">—</div>'
    worst_html = "".join(lb_row(t, i, "💀" if i == 0 else "") for i, t in enumerate(worst)) or '<div class="no-data">—</div>'

    wallet_html = _build_wallet_html(wallet)

    return f'''
    <div class="sys-header" style="border-left:4px solid {color}">
        <div class="sys-meta">
            <span class="mode-badge" style="background:{mode_c}22;color:{mode_c};border-color:{mode_c}44">{mode}</span>
            <span>Started: {started}</span>
            <span>Watching: {", ".join(watching)}</span>
            <span>Scans: {scan_count}</span>
            <span>Updated: {last_update[11:19] if len(str(last_update)) > 11 else last_update}</span>
        </div>
    </div>

    {wallet_html}

    <div class="top-stats">
        <div class="stat-card" style="border-top:3px solid {color}">
            <div class="stat-val" style="color:{pnl_c}">₹{total_pnl:+,.2f}</div>
            <div class="stat-lbl">REALISED P&L</div>
        </div>
        <div class="stat-card">
            <div class="stat-val" style="color:{unr_c}">₹{unr:+,.2f}</div>
            <div class="stat-lbl">UNREALISED</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{total_trades}</div>
            <div class="stat-lbl">TRADES</div>
            <div class="stat-sub">{wins}W / {losses}L</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{wr:.0f}%</div>
            <div class="stat-lbl">WIN RATE</div>
        </div>
        <div class="stat-card">
            <div class="stat-val" style="color:{sharpe_c}">{st["sharpe"]}</div>
            <div class="stat-lbl">SHARPE</div>
        </div>
        <div class="stat-card">
            <div class="stat-val" style="color:{pf_c}">{st["profit_factor"]}x</div>
            <div class="stat-lbl">PROFIT FACTOR</div>
        </div>
        <div class="stat-card">
            <div class="stat-val" style="color:#ef4444">₹{st["max_dd"]:,}</div>
            <div class="stat-lbl">MAX DRAWDOWN</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{st["rr_ratio"]:.1f}:1</div>
            <div class="stat-lbl">AVG R:R</div>
            <div class="stat-sub">₹{st["avg_win"]:,} / ₹{st["avg_loss"]:,}</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{streak_icon} {st["current_streak"]}</div>
            <div class="stat-lbl">STREAK</div>
            <div class="stat-sub">Best: {st["best_streak"]}W · Worst: {st["worst_streak"]}L</div>
        </div>
        <div class="stat-card">
            <div class="stat-val" style="color:{'#10b981' if st['expectancy']>=0 else '#ef4444'}">₹{st["expectancy"]:+,}</div>
            <div class="stat-lbl">EXPECTANCY</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{len(positions)}</div>
            <div class="stat-lbl">OPEN</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{len(signals)}</div>
            <div class="stat-lbl">SIGNALS</div>
        </div>
    </div>

    <div class="content">
        <div class="two-col">
            <div>
                <div class="sec-title">Open Positions</div>
                <div class="panel">{pos_html}</div>
            </div>
            <div>
                <div class="sec-title">Recent Signals</div>
                <div class="panel">{sig_html}</div>
            </div>
        </div>

        <div class="sec-title">Trades ({total_trades})</div>
        <div class="panel" style="max-height:400px;overflow-y:auto">{trades_html}</div>

        <div class="sec-title">Leaderboard</div>
        <div class="two-col">
            <div class="panel">
                <div class="panel-header">🏆 Best Trades</div>
                {best_html}
            </div>
            <div class="panel">
                <div class="panel-header">💀 Worst Trades</div>
                {worst_html}
            </div>
        </div>

        {_build_risk_html(risk)}
    </div>'''


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    state = _read_state()
    indices_data = state.get("indices", {})

    indices_html = _build_system_html(indices_data, "INDICES", "#6366f1", "📊")

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Omega Dashboard</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: #0a0a0f; color: #e2e8f0; min-height: 100vh;
}}

/* Header */
.header {{
    background: linear-gradient(135deg, #1a1a2e, #16213e);
    padding: 20px 32px;
    border-bottom: 1px solid #1e293b;
    display: flex; align-items: center; justify-content: space-between;
}}
.logo {{ display:flex; align-items:center; gap:12px; }}
.logo-icon {{
    width:40px; height:40px;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:20px;
}}
.logo h1 {{
    font-size:22px; font-weight:700;
    background: linear-gradient(135deg, #c7d2fe, #a5b4fc);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent;
}}
.clock {{ color:#64748b; font-size:13px; }}


/* System Header */
.sys-header {{
    padding:16px 32px; background:#0f172a; border-bottom:1px solid #1e293b;
}}
.sys-meta {{
    display:flex; gap:20px; font-size:13px; color:#94a3b8; flex-wrap:wrap; align-items:center;
}}
.mode-badge {{
    padding:3px 10px; border-radius:20px; font-weight:600; font-size:11px;
    text-transform:uppercase; border:1px solid;
}}

/* Stats */
.top-stats {{
    display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));
    gap:10px; padding:20px 32px;
}}
.stat-card {{
    background:#111827; border:1px solid #1e293b; border-radius:10px;
    padding:14px; text-align:center;
}}
.stat-val {{ font-size:22px; font-weight:700; }}
.stat-lbl {{ font-size:10px; color:#64748b; text-transform:uppercase; letter-spacing:1px; margin-top:2px; }}
.stat-sub {{ font-size:10px; color:#475569; margin-top:2px; }}

/* Content */
.content {{ padding:0 32px 32px; }}
.sec-title {{
    font-size:14px; font-weight:600; color:#94a3b8;
    margin:20px 0 10px; text-transform:uppercase; letter-spacing:1px;
}}
.panel {{
    background:#111827; border:1px solid #1e293b; border-radius:10px; padding:14px;
}}
.panel-header {{
    font-size:13px; font-weight:700; color:#94a3b8; margin-bottom:10px;
    padding-bottom:8px; border-bottom:1px solid #1e293b;
}}
.two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}

/* Rows */
.pos-row, .sig-row, .trade-row, .lb-row {{
    display:flex; align-items:center; gap:10px;
    padding:7px 0; border-bottom:1px solid #1e293b22; font-size:13px;
}}
.pos-dir {{ padding:2px 8px; border-radius:4px; font-size:11px; font-weight:700; }}
.pos-long {{ background:#10b98122; color:#10b981; }}
.pos-short {{ background:#ef444422; color:#ef4444; }}
.pos-symbol, .sig-sym, .trade-sym, .lb-sym {{ font-weight:600; min-width:90px; }}
.pos-sl {{ color:#64748b; font-size:11px; }}
.trade-reason, .lb-reason {{ color:#64748b; font-size:11px; }}
.sig-buy {{ color:#10b981; font-weight:700; min-width:65px; }}
.sig-sell {{ color:#ef4444; font-weight:700; min-width:65px; }}
.sig-exit {{ color:#f59e0b; font-weight:700; min-width:65px; }}
.score-wrap {{
    position:relative; width:70px; height:16px;
    background:#1e293b; border-radius:4px; overflow:hidden;
}}
.score-bar {{ position:absolute; top:0; left:0; height:100%; border-radius:4px; opacity:0.3; }}
.score-num {{ position:relative; z-index:1; font-size:11px; font-weight:700; line-height:16px; padding-left:5px; }}
.conf-high {{ color:#10b981; font-weight:700; font-size:11px; }}
.conf-med {{ color:#f59e0b; font-weight:700; font-size:11px; }}
.conf-low {{ color:#ef4444; font-weight:700; font-size:11px; }}
.lb-rank {{ font-weight:700; min-width:28px; color:#64748b; }}
.no-data {{ color:#475569; font-size:13px; padding:10px 0; text-align:center; }}

/* Wallet */
.wallet-bar {{
    display:flex; gap:0; background:linear-gradient(135deg, #0f172a, #1a1a2e);
    border-bottom:1px solid #1e293b; padding:0;
}}
.wallet-item {{
    flex:1; padding:14px 20px; text-align:center;
    border-right:1px solid #1e293b22;
}}
.wallet-item:last-child {{ border-right:none; }}
.wallet-lbl {{
    font-size:9px; color:#64748b; text-transform:uppercase;
    letter-spacing:1.5px; margin-bottom:4px;
}}
.wallet-val {{ font-size:18px; font-weight:700; color:#e2e8f0; }}

.footer {{ text-align:center; padding:20px; color:#334155; font-size:12px; }}

@media (max-width: 800px) {{
    .top-stats {{ grid-template-columns:repeat(3,1fr); padding:12px; }}
    .content {{ padding:0 14px 14px; }}
    .two-col {{ grid-template-columns:1fr; }}
    .toggle-bar {{ padding:0 14px; }}
    .sys-header {{ padding:12px 14px; }}
}}
</style>
</head>
<body>

<div class="header">
    <div class="logo">
        <div class="logo-icon">Ω</div>
        <h1>OMEGA TRADING SYSTEM</h1>
    </div>
    <div class="clock">{now}</div>
</div>

{indices_html}

<div class="footer">Omega Trading System &mdash; Live</div>
<script>
(function(){{
    let t=null;
    function refresh(){{
        fetch('/dashboard',{{headers:{{'X-Partial':'1'}}}})
            .then(r=>r.text())
            .then(html=>{{
                const p=new DOMParser();
                const d=p.parseFromString(html,'text/html');
                const oldBody=document.body;
                const newBody=d.body;
                if(newBody){{
                    const scrollY=window.scrollY;
                    oldBody.innerHTML=newBody.innerHTML;
                    window.scrollTo(0,scrollY);
                }}
            }})
            .catch(()=>{{}})
            .finally(()=>{{t=setTimeout(refresh,1000)}});
    }}
    t=setTimeout(refresh,1000);
}})();
</script>
</body>
</html>'''
    return HTMLResponse(content=html)


@router.get("/api/state")
async def api_state():
    return _read_state()


@router.get("/api/db/dashboard")
async def api_db_dashboard():
    from core.database import OmegaDB
    return OmegaDB.get().get_dashboard_data()


@router.get("/api/db/trades")
async def api_db_trades(limit: int = 50, date: str = ""):
    from core.database import OmegaDB
    return OmegaDB.get().get_trades(limit=limit, date=date)


@router.get("/api/db/wallet")
async def api_db_wallet():
    from core.database import OmegaDB
    return OmegaDB.get().get_wallet()


@router.get("/api/db/positions")
async def api_db_positions():
    from core.database import OmegaDB
    return OmegaDB.get().get_positions()


@router.get("/api/db/stats")
async def api_db_stats(date: str = ""):
    from core.database import OmegaDB
    return OmegaDB.get().get_trade_stats(date=date)


@router.get("/api/db/daily")
async def api_db_daily(limit: int = 30):
    from core.database import OmegaDB
    return OmegaDB.get().get_daily_stats(limit=limit)
