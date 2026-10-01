"""
Omega Trading Dashboard — Professional Web UI

Client-side SPA served at /dashboard on port 8000.
Fetches /api/state every second for live updates.
Tab navigation: Dashboard | Positions | Trades | Signals
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
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


_bot: dict = {}


def register_bot(**refs):
    """Called by main.py to give dashboard access to strategy/pm/broker."""
    _bot.update(refs)


def _market_status() -> str:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return "CLOSED"
    t = now.hour * 60 + now.minute
    if t < 555:
        return "PRE-MARKET"
    if t < 915:
        return "OPEN"
    if t < 930:
        return "CLOSING"
    return "CLOSED"


def _token_expiry() -> int:
    import base64 as _b64, os as _os
    tok = _os.environ.get("DHAN_ACCESS_TOKEN", "")
    if not tok:
        return 0
    try:
        payload = tok.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(_b64.urlsafe_b64decode(payload))
        return int(data.get("exp", 0))
    except Exception:
        return 0


@router.get("/api/state")
async def api_state():
    state = _read_state()
    idx = state.get("indices", {})
    closed = idx.get("closed_trades", [])
    positions = idx.get("positions", [])
    stats = _stats(closed)
    notes = state.get("trade_notes", {})

    total_pnl = sum(t.get("pnl", 0) for t in closed)
    total_trades = len(closed)
    wins = sum(1 for t in closed if t.get("pnl", 0) >= 0)
    losses = total_trades - wins
    wr = (wins / total_trades * 100) if total_trades > 0 else 0
    unr = sum(p.get("pnl", 0) for p in positions)

    return {
        "mode": idx.get("mode", "paper").upper(),
        "watching": idx.get("watching", []),
        "started_at": idx.get("started_at", ""),
        "scan_count": idx.get("scan_count", 0),
        "last_update": idx.get("last_update", ""),
        "wallet": idx.get("wallet", {}),
        "positions": positions,
        "closed_trades": closed,
        "signals": idx.get("signals", []),
        "risk_metrics": idx.get("risk_metrics", {}),
        "stats": {
            **stats,
            "total_pnl": round(total_pnl, 2),
            "unrealised": round(unr, 2),
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wr, 1),
        },
        "memory": idx.get("memory", {}),
        "market_status": _market_status(),
        "server_time": datetime.now(IST).isoformat(),
        "token_expiry": _token_expiry(),
        "trade_notes": notes,
        "index_prices": idx.get("index_prices", {}),
    }


@router.post("/api/close_position")
async def close_position(request: Request):
    """Manually close a single position from the dashboard."""
    import logging
    log = logging.getLogger("omega.dashboard")
    body = await request.json()
    security_id = body.get("security_id", "")
    reason = body.get("reason", "MANUAL_CLOSE")

    pm = _bot.get("pm")
    broker = _bot.get("broker")
    strategy = _bot.get("strategy")
    wallet = _bot.get("wallet")

    if not pm or not broker:
        return {"ok": False, "error": "Bot not connected"}

    pos = pm._open.get(security_id)
    if not pos:
        return {"ok": False, "error": "Position not found"}

    ltp = broker.get_ltp(pos.security_id, pos.exchange_segment)
    if ltp <= 0:
        ltp = pos.current_price if pos.current_price > 0 else pos.entry_price

    from core.broker_base import Order
    exit_side = "SELL" if pos.is_long else "BUY"
    exit_order = Order(
        security_id=pos.security_id,
        exchange_segment=pos.exchange_segment,
        transaction_type=exit_side,
        quantity=pos.quantity,
        order_type="MARKET",
        product_type="INTRADAY",
        price=ltp,
        tag=f"MANUAL_{pos.symbol}",
    )
    resp = broker.place_order(exit_order)

    if pos.sl_order_id:
        try:
            broker.cancel_order(pos.sl_order_id)
        except Exception:
            pass

    fill = resp.fill_price if resp.fill_price > 0 else ltp
    trade = pm.close_position(security_id, fill, reason)
    if trade and wallet:
        wallet.record_trade(trade.pnl, trade.entry_price * trade.quantity)

    log.info("MANUAL CLOSE from dashboard: %s %s @ %.2f reason=%s pnl=%.2f",
             pos.direction, pos.symbol, fill, reason,
             trade.pnl if trade else 0)

    return {
        "ok": True,
        "symbol": pos.symbol,
        "fill_price": fill,
        "pnl": trade.pnl if trade else 0,
        "reason": reason,
    }


@router.post("/api/close_all")
async def close_all(request: Request):
    """Square off ALL open positions from the dashboard."""
    import logging
    log = logging.getLogger("omega.dashboard")

    pm = _bot.get("pm")
    broker = _bot.get("broker")
    wallet = _bot.get("wallet")

    if not pm or not broker:
        return {"ok": False, "error": "Bot not connected"}

    results = []
    for sid in list(pm._open.keys()):
        pos = pm._open.get(sid)
        if not pos:
            continue

        ltp = broker.get_ltp(pos.security_id, pos.exchange_segment)
        if ltp <= 0:
            ltp = pos.current_price if pos.current_price > 0 else pos.entry_price

        from core.broker_base import Order
        exit_side = "SELL" if pos.is_long else "BUY"
        exit_order = Order(
            security_id=pos.security_id,
            exchange_segment=pos.exchange_segment,
            transaction_type=exit_side,
            quantity=pos.quantity,
            order_type="MARKET",
            product_type="INTRADAY",
            price=ltp,
            tag=f"MANUAL_{pos.symbol}",
        )
        broker.place_order(exit_order)

        if pos.sl_order_id:
            try:
                broker.cancel_order(pos.sl_order_id)
            except Exception:
                pass

        trade = pm.close_position(sid, ltp, "MANUAL_SQUARE_OFF")
        if trade and wallet:
            wallet.record_trade(trade.pnl, trade.entry_price * trade.quantity)
        results.append({
            "symbol": pos.symbol,
            "pnl": trade.pnl if trade else 0,
        })

    log.info("MANUAL SQUARE OFF from dashboard: %d positions closed", len(results))
    return {"ok": True, "closed": results}


@router.post("/api/modify_sl")
async def modify_sl(request: Request):
    """Manually adjust stop loss for a position."""
    import logging
    log = logging.getLogger("omega.dashboard")
    body = await request.json()
    security_id = body.get("security_id", "")
    new_sl = float(body.get("new_sl", 0))

    pm = _bot.get("pm")
    broker = _bot.get("broker")

    if not pm or not broker:
        return {"ok": False, "error": "Bot not connected"}

    pos = pm._open.get(security_id)
    if not pos:
        return {"ok": False, "error": "Position not found"}
    if new_sl <= 0:
        return {"ok": False, "error": "Invalid SL price"}

    old_sl = pos.trail_sl if pos.trail_sl > 0 else pos.sl_price
    pos.sl_price = new_sl
    pos.trail_sl = new_sl

    if pos.sl_order_id:
        try:
            broker.modify_order(pos.sl_order_id, trigger_price=new_sl, price=new_sl)
        except Exception:
            pass

    log.info("MANUAL SL from dashboard: %s SL %.2f → %.2f", pos.symbol, old_sl, new_sl)
    return {"ok": True, "symbol": pos.symbol, "old_sl": old_sl, "new_sl": new_sl}


@router.post("/api/modify_tp")
async def modify_tp(request: Request):
    """Manually adjust take profit for a position."""
    import logging
    log = logging.getLogger("omega.dashboard")
    body = await request.json()
    security_id = body.get("security_id", "")
    new_tp = float(body.get("new_tp", 0))

    pm = _bot.get("pm")
    if not pm:
        return {"ok": False, "error": "Bot not connected"}

    pos = pm._open.get(security_id)
    if not pos:
        return {"ok": False, "error": "Position not found"}
    if new_tp <= 0:
        return {"ok": False, "error": "Invalid TP price"}

    old_tp = getattr(pos, "tp_price", 0) or 0
    pos.tp_price = new_tp

    log.info("MANUAL TP from dashboard: %s TP %.2f → %.2f", pos.symbol, old_tp, new_tp)
    return {"ok": True, "symbol": pos.symbol, "old_tp": old_tp, "new_tp": new_tp}


@router.post("/api/add_note")
async def add_note(request: Request):
    """Add a note to a trade."""
    body = await request.json()
    trade_key = body.get("trade_key", "")
    note = body.get("note", "").strip()
    if not trade_key or not note:
        return {"ok": False, "error": "Missing trade_key or note"}

    state = _read_state()
    notes = state.get("trade_notes", {})
    notes[trade_key] = note
    state["trade_notes"] = notes
    _write_state(state)
    return {"ok": True, "trade_key": trade_key, "note": note}


@router.get("/api/export_csv")
async def export_csv():
    """Export closed trades as CSV."""
    from fastapi.responses import Response
    state = _read_state()
    idx = state.get("indices", {})
    closed = idx.get("closed_trades", [])
    lines = ["Symbol,Direction,Entry,Exit,P&L,P&L%,Reason,Time"]
    for t in closed:
        e = t.get("entry", 0)
        x = t.get("exit", 0)
        pct = ((x - e) / e * 100) if e > 0 else 0
        if t.get("direction") == "SHORT":
            pct = -pct
        lines.append(f"{t.get('symbol','')},{t.get('direction','')},{e},{x},{t.get('pnl',0):.2f},{pct:.1f},{t.get('reason','')},{t.get('exit_time','')}")
    csv_text = "\n".join(lines)
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=omega_trades.csv"})


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Omega Trading System</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg-0:#04060c;--bg-1:#080c18;--bg-2:#0d1220;--bg-3:#141c34;
  --border:#1c2848;--border-dim:rgba(28,40,72,0.3);
  --text-1:#eef1f8;--text-2:#8894b8;--text-3:#4a5578;
  --gold:#c9a84c;--gold-light:#e8d48b;--gold-dim:rgba(201,168,76,.06);
  --gold-glow:0 0 30px rgba(201,168,76,.2);
  --accent:#c9a84c;--accent-g:#e8d48b;--accent-dim:rgba(201,168,76,.06);
  --cyan:#4ecdc4;--cyan-dim:rgba(78,205,196,.06);
  --profit:#00e68a;--profit-bg:rgba(0,230,138,0.06);--profit-glow:0 0 24px rgba(0,230,138,.2);
  --loss:#ff4d5e;--loss-bg:rgba(255,77,94,0.06);--loss-glow:0 0 24px rgba(255,77,94,.2);
  --warn:#f0b90b;--warn-bg:rgba(240,185,11,0.06);
  --glass:rgba(10,15,28,.72);--glass-border:rgba(201,168,76,.07);
}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg-0);color:var(--text-1);
  min-height:100vh;-webkit-font-smoothing:antialiased;position:relative;overflow-x:hidden}
.mono{font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}
.serif{font-family:'DM Serif Display',Georgia,serif}

/* PREMIUM BACKGROUND */
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 120% 60% at 50% -30%,rgba(201,168,76,.07),transparent 60%),
    radial-gradient(ellipse 80% 50% at 100% 80%,rgba(78,205,196,.03),transparent 50%),
    radial-gradient(ellipse 60% 60% at 0% 100%,rgba(201,168,76,.03),transparent 50%)}
body::after{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.35;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.03'/%3E%3C/svg%3E")}

/* SCROLLBAR */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(201,168,76,.2);border-radius:10px}
::-webkit-scrollbar-thumb:hover{background:rgba(201,168,76,.35)}

/* HEADER */
.hdr{display:flex;align-items:center;justify-content:space-between;padding:0 28px;height:64px;
  background:linear-gradient(180deg,rgba(10,14,26,.96),rgba(8,12,24,.9));
  border-bottom:1px solid rgba(201,168,76,.08);position:sticky;top:0;z-index:100;
  backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px)}
.hdr::after{content:'';position:absolute;bottom:-1px;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent 5%,var(--gold) 30%,var(--gold-light) 50%,var(--gold) 70%,transparent 95%);
  opacity:.4}
.brand{display:flex;align-items:center;gap:14px}
.brand-i{width:44px;height:44px;
  background:linear-gradient(145deg,#dbb85c,var(--gold),#a88832);
  border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:900;
  color:#1a1400;text-shadow:0 1px 2px rgba(0,0,0,.3);
  box-shadow:0 0 28px rgba(201,168,76,.3),0 4px 12px rgba(0,0,0,.4),
    inset 0 1px 0 rgba(255,255,255,.25),inset 0 -1px 0 rgba(0,0,0,.2);
  animation:logoGlow 4s ease-in-out infinite alternate}
@keyframes logoGlow{0%{box-shadow:0 0 20px rgba(201,168,76,.2),0 4px 12px rgba(0,0,0,.4)}
  100%{box-shadow:0 0 40px rgba(201,168,76,.35),0 4px 12px rgba(0,0,0,.4),0 0 80px rgba(201,168,76,.08)}}
.brand-n{font-family:'DM Serif Display',Georgia,serif;font-size:19px;font-weight:400;letter-spacing:2px;
  background:linear-gradient(90deg,#f5e6c8,var(--gold),var(--gold-light),var(--gold),#f5e6c8);
  background-size:200% auto;-webkit-background-clip:text;-webkit-text-fill-color:transparent;
  animation:brandShift 4s linear infinite}
@keyframes brandShift{0%{background-position:0% center}100%{background-position:200% center}}
.hdr-r{display:flex;align-items:center;gap:14px}
.pill{padding:5px 14px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase}
.pill-p{background:rgba(0,230,138,.06);color:var(--profit);border:1px solid rgba(0,230,138,.18)}
.pill-l{background:rgba(255,77,94,.06);color:var(--loss);border:1px solid rgba(255,77,94,.18)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--profit);
  box-shadow:0 0 6px rgba(0,230,138,.5);animation:pulse 2.5s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 4px rgba(0,230,138,.3)}50%{box-shadow:0 0 10px rgba(0,230,138,.5),0 0 20px rgba(0,230,138,.15)}}
.clock{font-size:12px;color:var(--text-3);letter-spacing:.5px}

/* SYSTEM BAR */
.sbar{display:flex;align-items:center;gap:24px;padding:8px 28px;
  background:rgba(8,12,24,.5);border-bottom:1px solid var(--border-dim);font-size:11px;color:var(--text-3);
  flex-wrap:wrap;overflow-x:auto;letter-spacing:.2px}
.sbar span{white-space:nowrap}
.sbar b{color:var(--text-2)}

/* INDEX TICKER */
.idx-ticker{display:flex;align-items:center;justify-content:center;gap:32px;padding:10px 28px;
  background:linear-gradient(180deg,rgba(10,14,28,.7),rgba(6,10,20,.9));
  border-bottom:1px solid rgba(201,168,76,.08);font-size:12px}
.idx-t{display:flex;align-items:center;gap:8px;white-space:nowrap}
.idx-name{color:var(--text-3);font-size:10px;text-transform:uppercase;letter-spacing:1.5px;font-weight:600}
.idx-price{color:var(--accent-g);font-family:var(--mono);font-size:16px;font-weight:700;letter-spacing:.5px}
.idx-price.stale{color:var(--text-3);opacity:.5}
.idx-dot{width:5px;height:5px;border-radius:50%;background:var(--profit);
  box-shadow:0 0 4px var(--profit);animation:pulse 2s infinite}
.idx-dot.off{background:var(--text-3);box-shadow:none;animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* WALLET */
.wlt{display:flex;background:linear-gradient(180deg,rgba(12,18,34,.85),rgba(6,8,16,.95));
  border-bottom:1px solid rgba(201,168,76,.06);overflow-x:auto;position:relative;padding:4px 0}
.wlt::before{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(201,168,76,.12),transparent)}
.wlt-i{flex:1;padding:14px 24px;text-align:center;min-width:150px;
  border-right:1px solid rgba(201,168,76,.05);position:relative}
.wlt-i:last-child{border-right:none}
.wlt-l{font-size:9px;color:var(--text-3);text-transform:uppercase;letter-spacing:2px;margin-bottom:6px}
.wlt-v{font-size:17px;font-weight:700}

/* TABS */
.tabs{display:flex;gap:0;background:rgba(8,12,24,.6);border-bottom:1px solid var(--border-dim);
  padding:0 28px;overflow-x:auto}
.tab{padding:13px 26px;font-size:13px;font-weight:600;color:var(--text-3);background:none;border:none;
  cursor:pointer;border-bottom:2px solid transparent;transition:all .25s;white-space:nowrap;font-family:inherit;
  position:relative;letter-spacing:.3px}
.tab:hover{color:var(--text-2)}
.tab.on{color:var(--gold-light);border-bottom-color:var(--gold);
  text-shadow:0 0 16px rgba(201,168,76,.25)}

/* MAIN */
main{padding:24px;max-width:1440px;margin:0 auto;position:relative;z-index:1}
.tp{display:none}.tp.on{display:block;animation:fadeUp .3s ease}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}

/* STATS GRID */
.sg{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:28px}
.sc{background:var(--glass);border:1px solid rgba(201,168,76,.06);border-radius:14px;padding:20px 16px;
  text-align:center;transition:all .3s;backdrop-filter:blur(12px);position:relative;overflow:hidden}
.sc::before{content:'';position:absolute;inset:0;border-radius:14px;opacity:0;transition:opacity .3s;
  background:linear-gradient(135deg,rgba(201,168,76,.04),transparent 60%);pointer-events:none}
.sc::after{content:'';position:absolute;top:0;left:20%;right:20%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(201,168,76,.15),transparent);pointer-events:none}
.sc:hover{border-color:rgba(201,168,76,.15);transform:translateY(-3px);
  box-shadow:0 12px 35px rgba(0,0,0,.35),0 0 20px rgba(201,168,76,.04)}
.sc:hover::before{opacity:1}
.sv{font-size:24px;font-weight:700;line-height:1.2}
.sl{font-size:9px;color:var(--text-3);text-transform:uppercase;letter-spacing:1.2px;margin-top:6px}
.ss{font-size:10px;color:var(--text-3);margin-top:3px}
.sc-hl{border-color:rgba(0,230,138,.12)!important;box-shadow:var(--profit-glow)}
.sc-hl-loss{border-color:rgba(255,77,94,.12)!important;box-shadow:var(--loss-glow)}

/* CARDS */
.cd{background:var(--glass);border:1px solid rgba(201,168,76,.06);border-radius:16px;padding:22px;
  margin-bottom:16px;backdrop-filter:blur(12px);transition:all .3s;position:relative;overflow:hidden}
.cd::before{content:'';position:absolute;top:0;left:15%;right:15%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(201,168,76,.12),transparent);pointer-events:none}
.cd:hover{border-color:rgba(201,168,76,.1)}
.ct{font-size:11px;font-weight:700;color:var(--gold);text-transform:uppercase;letter-spacing:1.5px;
  margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border-dim);
  display:flex;align-items:center;gap:10px}
.ct::before{content:'';width:3px;height:16px;border-radius:2px;
  background:linear-gradient(180deg,var(--gold),var(--gold-light))}
.tc{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}

/* ROWS */
.pr,.sr,.tr,.lr{display:flex;align-items:center;gap:10px;padding:8px 0;
  border-bottom:1px solid var(--border-dim);font-size:13px;transition:background .15s}
.pr:hover,.sr:hover,.tr:hover,.lr:hover{background:rgba(124,106,239,.03);margin:0 -8px;padding-left:8px;
  padding-right:8px;border-radius:6px}
.pr:last-child,.sr:last-child,.tr:last-child,.lr:last-child{border-bottom:none}
.db{padding:3px 10px;border-radius:5px;font-size:10px;font-weight:700;letter-spacing:.3px}
.db-l{background:rgba(0,255,163,.08);color:var(--profit);border:1px solid rgba(0,255,163,.15)}
.db-s{background:rgba(255,71,87,.08);color:var(--loss);border:1px solid rgba(255,71,87,.15)}
.sym{font-weight:600;min-width:100px}

/* SCORE BAR */
.sbw{position:relative;width:70px;height:16px;background:var(--bg-3);border-radius:4px;overflow:hidden}
.sbf{position:absolute;top:0;left:0;height:100%;border-radius:4px;opacity:.25}
.sbn{position:relative;z-index:1;font-size:10px;font-weight:700;line-height:16px;padding-left:5px}

/* CONF BADGE */
.cb{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px}
.cb-h{color:var(--profit);background:var(--profit-bg);border:1px solid rgba(0,255,163,.12)}
.cb-m{color:var(--warn);background:var(--warn-bg);border:1px solid rgba(255,190,11,.12)}
.cb-lo{color:var(--loss);background:var(--loss-bg);border:1px solid rgba(255,71,87,.12)}

/* POSITION CARD */
.pc{background:var(--glass);border:1px solid rgba(201,168,76,.08);border-radius:16px;padding:24px;
  margin-bottom:16px;backdrop-filter:blur(12px);transition:all .3s;position:relative;overflow:hidden}
.pc::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--gold),var(--gold-light),var(--gold));opacity:.5}
.pc:hover{border-color:rgba(201,168,76,.15);box-shadow:0 8px 30px rgba(0,0,0,.3)}
.pc-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.pc-p{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:16px}

/* SL/TP BAR */
.stb{position:relative;height:8px;background:var(--bg-3);border-radius:4px;margin:16px 0 8px;
  box-shadow:inset 0 1px 3px rgba(0,0,0,.3)}
.stb-f{position:absolute;top:0;height:100%;border-radius:4px;opacity:.3;
  box-shadow:0 0 10px currentColor}
.stb-m{position:absolute;top:-5px;height:18px;border-radius:1px}
.stb-l{display:flex;justify-content:space-between;font-size:11px;color:var(--text-3)}

/* SIGNAL CARD */
.xc{background:var(--glass);border:1px solid var(--glass-border);border-radius:14px;padding:22px;
  margin-bottom:14px;backdrop-filter:blur(8px);transition:all .3s;position:relative;overflow:hidden}
.xc::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--cyan),transparent);opacity:.3}
.xd{display:grid;grid-template-columns:1fr 1fr;gap:6px 18px;margin-top:14px}
.xdr{display:flex;align-items:center;gap:6px;font-size:11px}
.xdl{min-width:44px;color:var(--text-3);text-transform:uppercase;letter-spacing:.3px;font-size:10px}
.xdb{flex:1;height:5px;background:var(--bg-3);border-radius:3px;overflow:hidden;
  box-shadow:inset 0 1px 2px rgba(0,0,0,.2)}
.xdf{height:100%;border-radius:3px;transition:width .3s;box-shadow:0 0 6px currentColor}
.xdv{min-width:22px;text-align:right;font-weight:600;font-size:10px}

/* TRADES TABLE */
.ttb{width:100%;border-collapse:collapse;font-size:13px}
.ttb th{text-align:left;font-size:10px;color:var(--gold);text-transform:uppercase;
  letter-spacing:.8px;padding:12px 14px;border-bottom:1px solid rgba(201,168,76,.1);font-weight:700}
.ttb td{padding:12px 14px;border-bottom:1px solid var(--border-dim);transition:background .15s}
.ttb tr:hover td{background:rgba(201,168,76,.02)}

/* RISK PANEL */
.rp{border-left:3px solid var(--profit)}
.rzb{padding:5px 14px;border-radius:20px;font-weight:700;font-size:11px;letter-spacing:.3px}
.rg{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:14px;margin-top:14px}
.ril{font-size:9px;color:var(--text-3);text-transform:uppercase;letter-spacing:1px}
.riv{font-size:20px;font-weight:700;margin-top:3px}
.pgb{height:6px;background:var(--bg-3);border-radius:3px;overflow:hidden;margin-top:5px;
  box-shadow:inset 0 1px 2px rgba(0,0,0,.2)}
.pgf{height:100%;border-radius:3px;transition:width .3s;box-shadow:0 0 8px currentColor}

/* EQUITY CHART */
.eq-w{height:140px;margin:10px 0}
.eq-w canvas{width:100%;height:100%;display:block}

/* LEADERBOARD */
.lr-r{font-weight:700;min-width:28px;color:var(--text-3)}
.lr-s{font-weight:600;min-width:100px}
.lr-re{color:var(--text-3);font-size:11px}

/* ACTION BUTTONS */
.abtn{padding:6px 14px;border-radius:6px;font-size:12px;font-weight:600;border:1px solid;
  cursor:pointer;font-family:inherit;transition:all .2s;backdrop-filter:blur(8px)}
.abtn:hover{transform:translateY(-1px)}
.abtn:active{transform:translateY(0)}
.abtn-profit{background:rgba(0,255,163,.08);color:var(--profit);border-color:rgba(0,255,163,.25)}
.abtn-profit:hover{background:rgba(0,255,163,.15);box-shadow:0 0 15px rgba(0,255,163,.15)}
.abtn-loss{background:rgba(255,71,87,.08);color:var(--loss);border-color:rgba(255,71,87,.25)}
.abtn-loss:hover{background:rgba(255,71,87,.15);box-shadow:0 0 15px rgba(255,71,87,.15)}
.abtn-danger{background:rgba(255,71,87,.06);color:var(--loss);border-color:rgba(255,71,87,.2)}
.abtn-danger:hover{background:rgba(255,71,87,.12);box-shadow:0 0 20px rgba(255,71,87,.1)}

/* EMPTY */
.emp{text-align:center;color:var(--text-3);font-size:13px;padding:28px 0}

/* MODAL */
.modal-ov{position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:500;display:flex;align-items:center;
  justify-content:center;backdrop-filter:blur(8px);animation:fadeIn .2s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal-box{background:var(--bg-1);border:1px solid rgba(201,168,76,.12);border-radius:18px;padding:30px;
  min-width:340px;max-width:440px;width:90%;
  box-shadow:0 30px 90px rgba(0,0,0,.6),0 0 50px rgba(201,168,76,.05);animation:modalUp .3s ease}
@keyframes modalUp{from{opacity:0;transform:translateY(20px) scale(.97)}to{opacity:1;transform:none}}
.modal-title{font-size:17px;font-weight:700;margin-bottom:20px;color:var(--text-1);
  display:flex;align-items:center;gap:10px}
.modal-row{margin-bottom:16px}
.modal-lbl{font-size:10px;color:var(--gold);text-transform:uppercase;letter-spacing:1.2px;margin-bottom:6px;font-weight:600}
.modal-inp{width:100%;padding:12px 16px;background:var(--bg-3);border:1px solid var(--border);
  border-radius:10px;color:var(--text-1);font-family:'JetBrains Mono',monospace;font-size:14px;
  outline:none;transition:border-color .2s}
.modal-inp:focus{border-color:var(--gold)}
.modal-inp::placeholder{color:var(--text-3)}
.modal-btns{display:flex;gap:12px;margin-top:22px}
.modal-btn{flex:1;padding:11px;border-radius:10px;font-weight:600;font-size:13px;cursor:pointer;
  border:1px solid;font-family:inherit;transition:all .2s}
.modal-btn-p{background:linear-gradient(135deg,rgba(201,168,76,.12),rgba(201,168,76,.06));
  color:var(--gold-light);border-color:rgba(201,168,76,.25)}
.modal-btn-p:hover{background:linear-gradient(135deg,rgba(201,168,76,.2),rgba(201,168,76,.1));
  box-shadow:0 0 20px rgba(201,168,76,.1)}
.modal-btn-c{background:transparent;color:var(--text-3);border-color:var(--border)}
.modal-btn-c:hover{background:var(--bg-3)}

/* TOKEN BANNER */
.token-ban{padding:10px 28px;font-size:12px;font-weight:600;text-align:center;display:none;letter-spacing:.4px}
.token-ban.warn{display:block;
  background:linear-gradient(90deg,rgba(240,185,11,.04),rgba(240,185,11,.1),rgba(240,185,11,.04));
  color:var(--warn);border-bottom:1px solid rgba(240,185,11,.15)}
.token-ban.crit{display:block;
  background:linear-gradient(90deg,rgba(255,77,94,.04),rgba(255,77,94,.1),rgba(255,77,94,.04));
  color:var(--loss);border-bottom:1px solid rgba(255,77,94,.15);animation:banPulse 2s infinite}
@keyframes banPulse{0%,100%{opacity:1}50%{opacity:.75}}

/* MARKET STATUS */
.mkt-st{padding:4px 12px;border-radius:20px;font-size:9px;font-weight:700;letter-spacing:.8px;
  text-transform:uppercase;display:flex;align-items:center;gap:5px}
.mkt-open{background:rgba(0,255,163,.08);color:var(--profit);border:1px solid rgba(0,255,163,.2)}
.mkt-pre{background:rgba(255,190,11,.08);color:var(--warn);border:1px solid rgba(255,190,11,.2)}
.mkt-closed{background:rgba(255,71,87,.06);color:var(--text-3);border:1px solid rgba(255,255,255,.06)}
.mkt-closing{background:rgba(255,190,11,.08);color:#f97316;border:1px solid rgba(249,115,22,.2)}

/* SESSION TIMER */
.sess-timer{font-size:11px;color:var(--text-3);display:flex;align-items:center;gap:6px}
.sess-timer .tm{color:var(--gold-light);font-weight:600}
.sess-timer.urgent .tm{color:var(--loss);animation:tmPulse 1s infinite}
@keyframes tmPulse{0%,100%{opacity:1}50%{opacity:.5}}

/* SL/TP EDIT */
.sltp-row{display:flex;align-items:center;gap:6px;margin-top:6px}
.sltp-val{font-size:12px;cursor:pointer;padding:3px 8px;border-radius:5px;transition:all .2s;
  border:1px solid transparent}
.sltp-val:hover{border-color:var(--border);background:var(--bg-3)}
.sltp-edit{font-size:9px;cursor:pointer;padding:3px 8px;border-radius:4px;
  background:rgba(201,168,76,.05);color:var(--gold);border:1px solid rgba(201,168,76,.1);
  transition:all .2s;font-weight:600;letter-spacing:.3px}
.sltp-edit:hover{background:rgba(201,168,76,.1);border-color:rgba(201,168,76,.2)}

/* TRADE NOTE */
.note-inp{width:100%;padding:6px 10px;background:var(--bg-3);border:1px solid var(--border-dim);
  border-radius:6px;color:var(--text-2);font-size:11px;font-family:inherit;outline:none;
  margin-top:6px;resize:none;transition:border-color .2s}
.note-inp:focus{border-color:var(--accent)}
.note-saved{font-size:10px;color:var(--text-3);font-style:italic;margin-top:4px}

/* P&L ALERT */
.pnl-alert{position:fixed;top:70px;right:24px;z-index:400;padding:14px 22px;border-radius:12px;
  font-size:13px;font-weight:600;backdrop-filter:blur(16px);animation:alertSlide .3s ease;
  box-shadow:0 12px 40px rgba(0,0,0,.4)}
.pnl-alert.profit{background:rgba(0,255,163,.12);color:var(--profit);border:1px solid rgba(0,255,163,.25)}
.pnl-alert.loss{background:rgba(255,71,87,.12);color:var(--loss);border:1px solid rgba(255,71,87,.25)}
@keyframes alertSlide{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:none}}

/* EXPORT BTN */
.export-btn{padding:7px 18px;border-radius:8px;font-size:11px;font-weight:600;cursor:pointer;
  background:rgba(201,168,76,.06);color:var(--gold-light);border:1px solid rgba(201,168,76,.15);
  font-family:inherit;transition:all .2s;letter-spacing:.4px}
.export-btn:hover{background:rgba(201,168,76,.12);box-shadow:0 0 18px rgba(201,168,76,.08)}

/* HERO PNL */
.hero{text-align:center;padding:44px 0 16px;margin-bottom:12px;position:relative}
.hero::before{content:'';position:absolute;top:0;left:50%;transform:translateX(-50%);
  width:80%;max-width:600px;height:100%;
  background:radial-gradient(ellipse at center,rgba(201,168,76,.04),transparent 70%);
  pointer-events:none}
.hero-val{font-family:'DM Serif Display',Georgia,serif;font-size:60px;font-weight:400;
  letter-spacing:-1px;line-height:1.1;
  background:linear-gradient(180deg,currentColor 40%,rgba(255,255,255,.5));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  filter:drop-shadow(0 0 40px currentColor);transition:color .3s}
.hero-lbl{font-size:10px;color:var(--text-3);text-transform:uppercase;letter-spacing:4px;margin-top:10px}
.hero-sub{font-size:16px;font-weight:600;margin-top:12px;opacity:.85}

/* FOOTER */
.ftr{text-align:center;padding:32px 28px;color:var(--text-3);font-size:11px;letter-spacing:1px;
  position:relative;z-index:1}
.ftr::before{content:'';display:block;height:1px;margin-bottom:32px;
  background:linear-gradient(90deg,transparent,rgba(201,168,76,.12),transparent)}

/* LOADING */
.loading{display:flex;align-items:center;justify-content:center;height:200px;color:var(--text-3);gap:12px;
  font-size:14px}
.loading::after{content:'';width:22px;height:22px;border:2px solid rgba(201,168,76,.15);
  border-top-color:var(--gold);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* RESPONSIVE */
@media(max-width:768px){
  .hdr{padding:0 16px;height:56px}
  .sbar{padding:7px 16px;gap:16px}
  main{padding:16px}
  .tc{grid-template-columns:1fr}
  .sg{grid-template-columns:repeat(3,1fr);gap:8px}
  .brand-n{font-size:15px;letter-spacing:1.5px}
  .wlt-i{padding:12px 16px;min-width:130px}
  .wlt-v{font-size:15px}
  .tabs{padding:0 16px}
  .tab{padding:11px 18px;font-size:12px}
  .xd{grid-template-columns:1fr}
  .pc-p{flex-direction:column;gap:4px}
  .hero{padding:28px 0 8px}
  .hero-val{font-size:40px}
}
@media(max-width:480px){
  .sg{grid-template-columns:repeat(2,1fr)}
  .sv{font-size:18px}
  .hero-val{font-size:32px}
}
</style>
</head>
<body>

<div class="token-ban" id="token-ban"></div>

<header class="hdr">
  <div class="brand">
    <div class="brand-i">&Omega;</div>
    <div class="brand-n">Omega Trading</div>
  </div>
  <div class="hdr-r">
    <span class="mkt-closed" id="mkt-st">CLOSED</span>
    <div class="sess-timer" id="sess-timer"><span>Session:</span><span class="tm mono" id="sess-tm">--:--</span></div>
    <span class="pill pill-p" id="mode-pill">PAPER</span>
    <div class="dot" id="live-dot"></div>
    <div class="clock mono" id="clock"></div>
  </div>
</header>

<div class="sbar" id="sbar"></div>
<div class="idx-ticker" id="idx-ticker"></div>
<div class="wlt" id="wallet"></div>

<nav class="tabs">
  <button class="tab on" data-t="dash">Dashboard</button>
  <button class="tab" data-t="pos">Positions</button>
  <button class="tab" data-t="trades">Trades</button>
  <button class="tab" data-t="sigs">Signals</button>
</nav>

<main>
  <div class="tp on" id="tp-dash"><div class="loading">Loading</div></div>
  <div class="tp" id="tp-pos"></div>
  <div class="tp" id="tp-trades"></div>
  <div class="tp" id="tp-sigs"></div>
</main>

<div class="ftr"><span class="serif" style="color:var(--gold);font-size:13px;letter-spacing:2px">Omega Trading System</span><br><span style="margin-top:4px;display:inline-block">Precision. Performance. Profit.</span></div>
<div id="modal-root"></div>
<div id="pnl-alerts"></div>

<script>
(function(){
let S=null,tab='dash';

function fmt(n,d){
  if(d===undefined)d=2;
  return new Intl.NumberFormat('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}).format(n);
}
function fpnl(n){return (n>=0?'+':'')+String.fromCharCode(8377)+fmt(Math.abs(n))}
function pc(n){return n>=0?'var(--profit)':'var(--loss)'}
function cc(c){return c==='HIGH'?'cb-h':c==='MEDIUM'?'cb-m':'cb-lo'}
function scc(s){return s>=70?'var(--profit)':s>=45?'var(--warn)':'var(--loss)'}

/* Clock */
function tick(){
  const d=new Date();
  const t=d.toLocaleString('en-GB',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  document.getElementById('clock').textContent=t+' IST';
}
setInterval(tick,1000);tick();

/* Tabs */
document.querySelectorAll('.tab').forEach(b=>{
  b.onclick=()=>{
    tab=b.dataset.t;
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    document.querySelectorAll('.tp').forEach(x=>x.classList.remove('on'));
    document.getElementById('tp-'+tab).classList.add('on');
    if(S)renderTab();
  };
});

function render(){
  if(!S)return;
  renderHdr();
  renderIdx();
  renderWlt();
  renderTab();
}

function renderHdr(){
  const m=S.mode||'PAPER';
  const p=document.getElementById('mode-pill');
  p.textContent=m;
  p.className='pill '+(m==='LIVE'?'pill-l':'pill-p');

  const ms=S.market_status||'CLOSED';
  const mEl=document.getElementById('mkt-st');
  const mCls={OPEN:'mkt-open','PRE-MARKET':'mkt-pre',CLOSING:'mkt-closing',CLOSED:'mkt-closed'};
  mEl.className='mkt-st '+(mCls[ms]||'mkt-closed');
  mEl.innerHTML=(ms==='OPEN'?'<span style="width:6px;height:6px;border-radius:50%;background:var(--profit);box-shadow:0 0 6px var(--profit)"></span>':'')+ms;

  updateSessionTimer(ms);
  updateTokenBanner();

  const w=S.watching||[];
  const sc=S.scan_count||0;
  const sa=S.started_at||'';
  const lu=S.last_update||'';
  const luT=lu.length>11?lu.substring(11,19):'';
  const saT=sa.length>11?sa.substring(11,19):(sa||'--');
  document.getElementById('sbar').innerHTML=
    '<span>Watching: <b>'+w.join(', ')+'</b></span>'+
    '<span>Scans: <b>'+sc+'</b></span>'+
    '<span>Started: '+saT+'</span>'+
    '<span>Updated: '+luT+'</span>'+
    '<span>Market: <b style="color:'+(ms==='OPEN'?'var(--profit)':ms==='CLOSING'?'#f97316':'var(--text-3)')+'">'+ms+'</b></span>';
}

function renderIdx(){
  const ip=S.index_prices||{};
  const el=document.getElementById('idx-ticker');
  let h='';
  for(const [name,price] of Object.entries(ip)){
    const live=price>0;
    const cls=live?'':'stale';
    const dot=live?'idx-dot':'idx-dot off';
    const pStr=live?String.fromCharCode(8377)+price.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}):'--';
    h+='<div class="idx-t"><span class="'+dot+'"></span><span class="idx-name">'+name+'</span><span class="idx-price '+cls+' mono">'+pStr+'</span></div>';
  }
  if(!h) h='<span style="color:var(--text-3);font-size:10px;letter-spacing:1px">INDEX FEED CONNECTING...</span>';
  el.innerHTML=h;
}

function renderWlt(){
  const w=S.wallet||{};
  const b=w.balance||0,a=w.available||0,it=w.in_trade||0,tp=w.today_pnl||0;
  const sc=w.starting_capital||200000;
  const tot=b-sc;
  const pct=sc>0?(tot/sc*100):0;
  document.getElementById('wallet').innerHTML=
    wi('Balance','var(--text-1)',b)+
    wi('Available','var(--accent-g)',a)+
    wi('In Trade','var(--warn)',it)+
    wi('Today P&L',pc(tp),tp,true)+
    wi('Total P&L',pc(tot),tot,true,'<span style="font-size:11px;opacity:.8"> ('+(pct>=0?'+':'')+pct.toFixed(1)+'%)</span>');
}
function wi(l,c,v,sign,extra){
  const val=sign?fpnl(v):String.fromCharCode(8377)+fmt(v);
  return '<div class="wlt-i"><div class="wlt-l">'+l+'</div><div class="wlt-v mono" style="color:'+c+'">'+val+(extra||'')+'</div></div>';
}

function renderTab(){
  switch(tab){
    case'dash':rDash();break;
    case'pos':rPos();break;
    case'trades':rTrades();break;
    case'sigs':rSigs();break;
  }
}

/* ── DASHBOARD ── */
function rDash(){
  const s=S.stats||{};
  const pos=S.positions||[];
  const sigs=S.signals||[];
  const risk=S.risk_metrics||{};
  const closed=S.closed_trades||[];
  const R=String.fromCharCode(8377);

  const sharpeC=(s.sharpe||0)>=1?'var(--profit)':(s.sharpe||0)>=0?'var(--warn)':'var(--loss)';
  const pfC=(s.profit_factor||0)>=1.5?'var(--profit)':(s.profit_factor||0)>=1?'var(--warn)':'var(--loss)';
  const si=s.streak_type==='W'?'&#x1F525; ':s.streak_type==='L'?'&#x2744;&#xFE0F; ':'';
  const expC=(s.expectancy||0)>=0?'var(--profit)':'var(--loss)';

  const totalCombined=(s.total_pnl||0)+(s.unrealised||0);
  const tcC=pc(totalCombined);

  let h='<div class="hero"><div class="hero-val mono" style="color:'+tcC+'">'+fpnl(totalCombined)+'</div>'+
    '<div class="hero-lbl">Total P&L (Realised + Unrealised)</div>'+
    '<div class="hero-sub" style="color:var(--text-2)">'+
    '<span style="color:'+pc(s.total_pnl||0)+'">'+fpnl(s.total_pnl||0)+' realised</span> &nbsp;+&nbsp; '+
    '<span style="color:'+pc(s.unrealised||0)+'">'+fpnl(s.unrealised||0)+' open</span></div></div>';

  h+='<div class="sg">'+
    sc2(fpnl(s.total_pnl||0),'Realised P&L',pc(s.total_pnl||0),'border-top:3px solid '+pc(s.total_pnl||0),'',(s.total_pnl||0)>=0?'sc-hl':'sc-hl-loss')+
    sc2(fpnl(s.unrealised||0),'Unrealised',pc(s.unrealised||0),'','',(s.unrealised||0)>=0?'sc-hl':'sc-hl-loss')+
    sc2(s.total_trades||0,'Trades','','',(s.wins||0)+'W / '+(s.losses||0)+'L')+
    sc2((s.win_rate||0).toFixed(0)+'%','Win Rate')+
    sc2((s.sharpe||0).toFixed(2),'Sharpe',sharpeC)+
    sc2((s.profit_factor||0).toFixed(2)+'x','Profit Factor',pfC)+
    sc2(R+fmt(s.max_dd||0,0),'Max Drawdown','var(--loss)')+
    sc2((s.rr_ratio||0).toFixed(1)+':1','Avg R:R','','',(R+fmt(s.avg_win||0,0)+' / '+R+fmt(s.avg_loss||0,0)))+
    sc2(si+(s.current_streak||0),'Streak','','','Best: '+(s.best_streak||0)+'W &middot; Worst: '+(s.worst_streak||0)+'L')+
    sc2(R+fmt(s.expectancy||0,0),'Expectancy',expC)+
    sc2(pos.length,'Open Positions')+
    sc2(sigs.length,'Signals')+
    '</div>';

  if(closed.length>0){
    h+='<div class="cd"><div class="ct">Equity Curve</div><div class="eq-w"><canvas id="eq-cv"></canvas></div></div>';
  }

  h+='<div class="tc"><div class="cd"><div class="ct">Open Positions</div>';
  if(!pos.length)h+='<div class="emp">No open positions</div>';
  else pos.forEach(p=>{
    const e=p.entry||0,c=p.current||0;
    let pct=e>0?((c-e)/e*100):0;
    if(p.direction==='SHORT')pct=-pct;
    const sid=p.security_id||'';
    const reason=(p.pnl||0)>=0?'BOOK_PROFIT':'BOOK_LOSS';
    h+='<div class="pr">'+
      '<span class="db '+(p.direction==='LONG'?'db-l':'db-s')+'">'+(p.direction||'')+'</span>'+
      '<span class="sym">'+(p.symbol||'')+'</span>'+
      '<span class="mono" style="font-size:12px">'+R+fmt(e)+' &rarr; '+R+fmt(c)+'</span>'+
      '<span class="mono" style="color:'+pc(p.pnl||0)+';font-weight:600">'+fpnl(p.pnl||0)+'</span>'+
      '<span style="color:'+pc(p.pnl||0)+';font-size:11px">'+(pct>=0?'+':'')+pct.toFixed(1)+'%</span>'+
      (sid?'<button class="abtn '+((p.pnl||0)>=0?'abtn-profit':'abtn-loss')+'" style="margin-left:auto;padding:3px 10px;font-size:10px" '+
      'onclick="doAction(\\'/api/close_position\\',{security_id:\\''+sid+'\\',reason:\\''+reason+'\\'},\\'Close '+(p.symbol||'')+'?\\')">Close</button>':'')+
      '</div>';
  });
  h+='</div><div class="cd"><div class="ct">Recent Signals</div>';
  const rs=sigs.slice(-10).reverse();
  if(!rs.length)h+='<div class="emp">Waiting for signals...</div>';
  else rs.forEach(sg=>{
    const side=sg.side||'';
    const scl=side.includes('BUY')?'color:var(--profit)':side.includes('SELL')?'color:var(--loss)':'color:var(--warn)';
    const sc=sg.score||0;
    h+='<div class="sr">'+
      '<span style="font-weight:700;min-width:60px;'+scl+'">'+side+'</span>'+
      '<span class="sym">'+(sg.symbol||'')+'</span>'+
      '<span class="sbw"><span class="sbf" style="width:'+Math.min(sc,100)+'%;background:'+scc(sc)+'"></span>'+
      '<span class="sbn mono">'+sc.toFixed(0)+'</span></span>'+
      '<span class="cb '+cc(sg.confidence||'')+'">'+(sg.confidence||'')+'</span></div>';
  });
  h+='</div></div>';

  if(risk&&Object.keys(risk).length){
    const z=risk.risk_zone||'SAFE';
    const zc={SAFE:'var(--profit)',ELEVATED:'var(--warn)',WARNING:'#f97316',CRITICAL:'var(--loss)'}[z]||'var(--text-3)';
    const rP=risk.daily_pnl||0;
    const lp=risk.daily_loss_used_pct||0;
    const bw=Math.min(lp,100);
    const bc=lp<30?'var(--profit)':lp<50?'var(--warn)':lp<80?'#f97316':'var(--loss)';
    const cl=risk.consecutive_losses||0;
    const mt=(risk.trades_today||0)+(risk.trades_remaining||6);
    const killed=risk.bot_killed?' &mdash; BOT KILLED':'';

    h+='<div class="cd rp" style="border-left-color:'+zc+'"><div class="ct">Risk Monitor</div>'+
      '<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">'+
      '<span class="rzb" style="background:'+zc+'22;color:'+zc+';border:1px solid '+zc+'44">'+z+killed+'</span>'+
      '<span class="mono" style="color:'+pc(rP)+';font-weight:700;font-size:16px">'+fpnl(rP)+'</span>'+
      '<span style="color:var(--text-3);font-size:12px">('+(risk.daily_pnl_pct||0>=0?'+':'')+
      (risk.daily_pnl_pct||0).toFixed(2)+'%)</span></div>'+
      '<div class="rg">'+
      '<div><div class="ril">Daily Loss Used</div>'+
      '<div class="pgb"><div class="pgf" style="width:'+bw+'%;background:'+bc+'"></div></div>'+
      '<div class="mono" style="font-size:12px;font-weight:600;margin-top:2px">'+lp.toFixed(0)+'%</div></div>'+
      '<div><div class="ril">Trades Today</div><div class="riv mono">'+(risk.trades_today||0)+
      ' <span style="color:var(--text-3);font-size:12px">/ '+mt+'</span></div></div>'+
      '<div><div class="ril">Consecutive Losses</div><div class="riv mono" style="color:'+(cl>=2?'var(--loss)':'var(--text-1)')+'">'+cl+'</div></div>'+
      '<div><div class="ril">Recovery Needed</div><div class="riv mono" style="color:'+
      ((risk.drawdown_recovery_pct||0)>5?'var(--loss)':'var(--text-1)')+'">'+(risk.drawdown_recovery_pct||0).toFixed(1)+'%</div></div>'+
      '</div></div>';
  }

  document.getElementById('tp-dash').innerHTML=h;
  if(closed.length>0)drawEq(closed);
}
function sc2(v,l,c,st,sub,cls){
  return '<div class="sc'+(cls?' '+cls:'')+'"'+(st?' style="'+st+'"':'')+'>'+
    '<div class="sv mono"'+(c?' style="color:'+c+'"':'')+'>'+v+'</div>'+
    '<div class="sl">'+l+'</div>'+(sub?'<div class="ss">'+sub+'</div>':'')+'</div>';
}

/* ── ACTIONS ── */
let _acting=false;
async function doAction(url,body,confirmMsg){
  if(_acting)return;
  if(!confirm(confirmMsg))return;
  _acting=true;
  try{
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){showToast(d.symbol?(d.symbol+' closed: '+fpnl(d.pnl||0)):('Closed '+d.closed?.length+' positions'),'ok')}
    else{showToast('Error: '+(d.error||'unknown'),'err')}
  }catch(e){showToast('Network error','err')}
  _acting=false;
}
function showToast(msg,type){
  let t=document.getElementById('toast');
  if(!t){t=document.createElement('div');t.id='toast';document.body.appendChild(t)}
  t.textContent=msg;
  t.style.cssText='position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:10px 24px;'+
    'border-radius:8px;font-size:13px;font-weight:600;z-index:999;backdrop-filter:blur(12px);'+
    'transition:opacity .3s;opacity:1;'+
    (type==='ok'?'background:rgba(0,255,163,.15);color:var(--profit);border:1px solid rgba(0,255,163,.3)':
    'background:rgba(255,71,87,.15);color:var(--loss);border:1px solid rgba(255,71,87,.3)');
  clearTimeout(t._tm);
  t._tm=setTimeout(()=>{t.style.opacity='0'},3000);
}

/* ── POSITIONS ── */
function rPos(){
  const pos=S.positions||[];
  const R=String.fromCharCode(8377);
  if(!pos.length){
    document.getElementById('tp-pos').innerHTML='<div class="cd"><div class="emp" style="padding:40px 0"><div style="font-size:28px;margin-bottom:8px">&#x1F4CA;</div>No open positions right now</div></div>';
    return;
  }
  let h='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'+
    '<span style="font-size:14px;font-weight:600;color:var(--text-2)">'+pos.length+' Open Position'+(pos.length>1?'s':'')+'</span>'+
    '<button class="abtn abtn-danger" onclick="doAction(\\'/api/close_all\\',{},\\'Close ALL '+pos.length+' position(s)? This cannot be undone.\\')">'+
    'Close All Positions</button></div>';

  pos.forEach(p=>{
    const e=p.entry||0,c=p.current||0,sl=p.sl||0;
    const sid=p.security_id||'';
    const tp=p.tp||0;
    const conf=p.confidence||'';
    let pct=e>0?((c-e)/e*100):0;
    if(p.direction==='SHORT')pct=-pct;
    const estTp=tp>0?tp:e+(e-sl)*1.5;
    const lo=Math.min(sl,e,c),hi=Math.max(estTp,e,c);
    const pad=(hi-lo)*0.1||1;
    const mn=lo-pad,rng=hi+pad-mn||1;
    const slP=((sl-mn)/rng*100),eP=((e-mn)/rng*100),cP=((c-mn)/rng*100);
    const tpP=tp>0?((tp-mn)/rng*100):0;
    const fL=Math.min(eP,cP),fW=Math.abs(cP-eP);
    const fc=c>=e?'var(--profit)':'var(--loss)';
    const pnlVal=p.pnl||0;
    const reason=pnlVal>=0?'BOOK_PROFIT':'BOOK_LOSS';

    h+='<div class="pc"><div class="pc-h"><div style="display:flex;align-items:center;gap:10px">'+
      '<span class="db '+(p.direction==='LONG'?'db-l':'db-s')+'">'+(p.direction||'')+'</span>'+
      '<span style="font-weight:700;font-size:16px">'+(p.symbol||'')+'</span>'+
      (conf?'<span class="cb '+cc(conf)+'">'+conf+'</span>':'')+'</div>'+
      '<div style="display:flex;gap:8px">'+
      '<button class="abtn '+(pnlVal>=0?'abtn-profit':'abtn-loss')+'" onclick="doAction(\\'/api/close_position\\','+
      '{security_id:\\''+sid+'\\',reason:\\''+reason+'\\'},\\''+
      (pnlVal>=0?'Book profit':'Book loss')+' on '+(p.symbol||'')+'? ('+fpnl(pnlVal)+')\\')">'+
      (pnlVal>=0?'Book Profit':'Close Loss')+'</button></div></div>'+
      '<div class="pc-p">'+
      '<span style="color:var(--text-2);font-size:14px">'+R+fmt(e)+'</span>'+
      '<span style="color:var(--text-3)">&rarr;</span>'+
      '<span class="mono" style="font-size:20px;font-weight:700;color:'+pc(pnlVal)+'">'+R+fmt(c)+'</span>'+
      '<span class="mono" style="font-size:18px;font-weight:700;color:'+pc(pnlVal)+'">'+fpnl(pnlVal)+'</span>'+
      '<span style="color:'+pc(pnlVal)+';font-size:13px">('+(pct>=0?'+':'')+pct.toFixed(1)+'%)</span></div>'+
      '<div class="stb"><div class="stb-f" style="left:'+fL+'%;width:'+fW+'%;background:'+fc+'"></div>'+
      '<div class="stb-m" style="left:'+slP+'%;background:var(--loss);width:2px" title="Stop Loss"></div>'+
      '<div class="stb-m" style="left:'+eP+'%;background:var(--text-3);width:1px" title="Entry"></div>'+
      (tpP>0?'<div class="stb-m" style="left:'+tpP+'%;background:var(--profit);width:2px" title="Take Profit"></div>':'')+
      '<div class="stb-m" style="left:'+cP+'%;background:'+fc+';width:3px" title="Current"></div></div>'+
      '<div class="stb-l"><span style="color:var(--loss)">SL '+R+fmt(sl)+'</span>'+
      '<span>Entry '+R+fmt(e)+'</span>'+
      (tp>0?'<span style="color:var(--profit)">TP '+R+fmt(tp)+'</span>':'<span>Current</span>')+'</div>'+
      '<div class="sltp-row">'+
      '<span class="sltp-val" style="color:var(--loss)" onclick="editSL(\\''+sid+'\\',\\''+(p.symbol||'').replace(/'/g,"")+'\\','+sl+')">SL: '+R+fmt(sl)+'</span>'+
      '<span class="sltp-edit" onclick="editSL(\\''+sid+'\\',\\''+(p.symbol||'').replace(/'/g,"")+'\\','+sl+')">Edit</span>'+
      '<span class="sltp-val" style="color:var(--profit)" onclick="editTP(\\''+sid+'\\',\\''+(p.symbol||'').replace(/'/g,"")+'\\','+tp+')">TP: '+(tp>0?R+fmt(tp):'Not set')+'</span>'+
      '<span class="sltp-edit" onclick="editTP(\\''+sid+'\\',\\''+(p.symbol||'').replace(/'/g,"")+'\\','+tp+')">Edit</span>'+
      '</div></div>';
  });
  document.getElementById('tp-pos').innerHTML=h;
}

/* ── TRADES ── */
function rTrades(){
  const closed=S.closed_trades||[];
  const R=String.fromCharCode(8377);
  const notes=S.trade_notes||{};
  let h='<div class="cd"><div class="ct" style="justify-content:space-between">'+
    '<span style="display:flex;align-items:center;gap:8px"><span style="width:3px;height:14px;border-radius:2px;background:var(--accent)"></span>Trade History ('+closed.length+' trades)</span>'+
    (closed.length?'<button class="export-btn" onclick="exportCSV()">&#x1F4E5; Export CSV</button>':'')+'</div>';
  if(!closed.length) h+='<div class="emp">No trades yet</div>';
  else{
    h+='<div style="overflow-x:auto"><table class="ttb"><thead><tr>'+
      '<th>Symbol</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&L</th><th>%</th><th>Reason</th><th>Notes</th></tr></thead><tbody>';
    [...closed].reverse().forEach((t,i)=>{
      const e=t.entry||0,x=t.exit||0;
      let pct=e>0?((x-e)/e*100):0;
      if(t.direction==='SHORT')pct=-pct;
      const key=(t.symbol||'')+'_'+(t.exit_time||i);
      const note=notes[key]||'';
      h+='<tr><td style="font-weight:600">'+(t.symbol||'')+'</td>'+
        '<td><span class="db '+(t.direction==='LONG'?'db-l':'db-s')+'">'+(t.direction||'')[0]+'</span></td>'+
        '<td class="mono">'+R+fmt(e)+'</td><td class="mono">'+R+fmt(x)+'</td>'+
        '<td class="mono" style="color:'+pc(t.pnl||0)+';font-weight:600">'+fpnl(t.pnl||0)+'</td>'+
        '<td style="color:'+pc(t.pnl||0)+';font-size:12px">'+(pct>=0?'+':'')+pct.toFixed(1)+'%</td>'+
        '<td style="color:var(--text-3);font-size:12px">'+(t.reason||'')+'</td>'+
        '<td><input class="note-inp" style="width:120px;margin:0" placeholder="Add note..." value="'+note.replace(/"/g,'&quot;')+'" '+
        'oninput="saveNote(\\''+key.replace(/'/g,"")+'\\',this)"></td></tr>';
    });
    h+='</tbody></table></div>';
  }
  h+='</div>';

  if(closed.length>0){
    const sorted=[...closed].sort((a,b)=>(b.pnl||0)-(a.pnl||0));
    const best=sorted.slice(0,5);
    const worst=sorted.slice(-5).reverse();
    const medals=['&#x1F947;','&#x1F948;','&#x1F949;','',''];

    h+='<div class="tc">';
    h+='<div class="cd"><div class="ct">&#x1F3C6; Best Trades</div>';
    best.forEach((t,i)=>{
      h+='<div class="lr"><span class="lr-r">'+medals[i]+(i+1)+'</span>'+
        '<span class="lr-s">'+(t.direction||'')[0]+' '+(t.symbol||'')+'</span>'+
        '<span class="mono" style="color:'+pc(t.pnl||0)+';font-weight:700">'+fpnl(t.pnl||0)+'</span>'+
        '<span class="lr-re">'+(t.reason||'')+'</span></div>';
    });
    h+='</div><div class="cd"><div class="ct">&#x1F480; Worst Trades</div>';
    worst.forEach((t,i)=>{
      h+='<div class="lr"><span class="lr-r">'+(i===0?'&#x1F480;':'')+(i+1)+'</span>'+
        '<span class="lr-s">'+(t.direction||'')[0]+' '+(t.symbol||'')+'</span>'+
        '<span class="mono" style="color:'+pc(t.pnl||0)+';font-weight:700">'+fpnl(t.pnl||0)+'</span>'+
        '<span class="lr-re">'+(t.reason||'')+'</span></div>';
    });
    h+='</div></div>';
  }
  document.getElementById('tp-trades').innerHTML=h;
}

/* ── SIGNALS ── */
function rSigs(){
  const sigs=S.signals||[];
  if(!sigs.length){
    document.getElementById('tp-sigs').innerHTML='<div class="cd"><div class="emp" style="padding:40px 0"><div style="font-size:28px;margin-bottom:8px">&#x1F4E1;</div>No signals received yet</div></div>';
    return;
  }
  const factors=[['adx','ADX'],['vwap','VWAP'],['rr','R:R'],['st','STrend'],
    ['vol','Volume'],['rsi','RSI'],['bb','BB'],['ema','EMA'],
    ['orb','ORB'],['confluence','Conf'],['time','Time'],['momentum','Mom'],
    ['pdhl','PDH/L'],['regime','Regime']];
  let h='';
  [...sigs].reverse().forEach(sg=>{
    const side=sg.side||'';
    const scl=side.includes('BUY')?'color:var(--profit)':side.includes('SELL')?'color:var(--loss)':'color:var(--warn)';
    const sc=sg.score||0;
    const det=sg.score_details||{};

    h+='<div class="xc"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">'+
      '<div style="display:flex;align-items:center;gap:10px">'+
      '<span style="font-weight:700;font-size:14px;'+scl+'">'+side+'</span>'+
      '<span style="font-weight:700;font-size:16px">'+(sg.symbol||'')+'</span></div>'+
      '<div style="display:flex;align-items:center;gap:10px">'+
      '<span class="cb '+cc(sg.confidence||'')+'">'+(sg.confidence||'')+'</span>'+
      '<span class="mono" style="font-size:22px;font-weight:800;color:'+scc(sc)+'">'+sc.toFixed(0)+'</span></div></div>'+
      '<div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text-2);margin-bottom:8px">'+
      '<span>ADX '+(sg.adx||0).toFixed(1)+'</span>'+
      '<span>RSI '+(sg.rsi||0).toFixed(1)+'</span>'+
      '<span>VWAP '+(sg.vwap_position||'')+'</span>'+
      '<span>ST '+(sg.supertrend_dir||'')+'</span>'+
      '<span>Vol '+(sg.volume_ratio||0).toFixed(2)+'x</span>'+
      '<span>Regime '+(sg.volatility_regime||'')+'</span></div>';
    if(Object.keys(det).length){
      h+='<div class="xd">';
      factors.forEach(([k,l])=>{
        const v=det[k]||0;
        const fc=scc(v);
        h+='<div class="xdr"><span class="xdl">'+l+'</span>'+
          '<div class="xdb"><div class="xdf" style="width:'+v+'%;background:'+fc+'"></div></div>'+
          '<span class="xdv mono" style="color:'+fc+'">'+v+'</span></div>';
      });
      h+='</div>';
    }
    h+='</div>';
  });
  document.getElementById('tp-sigs').innerHTML=h;
}

/* ── EQUITY CHART ── */
function drawEq(closed){
  const cv=document.getElementById('eq-cv');
  if(!cv)return;
  const ctx=cv.getContext('2d');
  const dpr=window.devicePixelRatio||1;
  const rect=cv.getBoundingClientRect();
  cv.width=rect.width*dpr;
  cv.height=rect.height*dpr;
  ctx.scale(dpr,dpr);
  const w=rect.width,ht=rect.height;

  let cum=0;
  const pts=[0];
  closed.forEach(t=>{cum+=(t.pnl||0);pts.push(cum)});
  const mn=Math.min(...pts),mx=Math.max(...pts);
  const rng=mx-mn||1;
  const pd=8;

  ctx.clearRect(0,0,w,ht);

  const zy=ht-pd-((0-mn)/rng*(ht-2*pd));
  ctx.strokeStyle='rgba(148,163,184,.12)';
  ctx.lineWidth=1;
  ctx.setLineDash([4,4]);
  ctx.beginPath();ctx.moveTo(pd,zy);ctx.lineTo(w-pd,zy);ctx.stroke();
  ctx.setLineDash([]);

  ctx.beginPath();
  pts.forEach((v,i)=>{
    const x=pd+(i/(pts.length-1||1))*(w-2*pd);
    const y=ht-pd-((v-mn)/rng*(ht-2*pd));
    i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
  });
  const last=pts[pts.length-1];
  ctx.strokeStyle=last>=0?'#10b981':'#ef4444';
  ctx.lineWidth=2;
  ctx.stroke();

  const lx=pd+((pts.length-1)/(pts.length-1||1))*(w-2*pd);
  ctx.lineTo(lx,ht-pd);ctx.lineTo(pd,ht-pd);ctx.closePath();
  const gr=ctx.createLinearGradient(0,0,0,ht);
  const fb=last>=0?'16,185,129':'239,68,68';
  gr.addColorStop(0,'rgba('+fb+',.12)');
  gr.addColorStop(1,'rgba('+fb+',.01)');
  ctx.fillStyle=gr;ctx.fill();

  ctx.fillStyle=last>=0?'#10b981':'#ef4444';
  ctx.font='600 11px "JetBrains Mono",monospace';
  ctx.textAlign='right';
  const ly=ht-pd-((last-mn)/rng*(ht-2*pd));
  ctx.fillText(String.fromCharCode(8377)+(last>=0?'+':'')+last.toFixed(0),w-pd,ly-6);
}

/* ── SESSION TIMER ── */
function updateSessionTimer(ms){
  const el=document.getElementById('sess-timer');
  const tm=document.getElementById('sess-tm');
  if(ms==='CLOSED'||ms==='PRE-MARKET'){
    if(ms==='PRE-MARKET'){
      const now=new Date();
      const open=new Date(now);open.setHours(3,45,0,0);
      const diff=open-now;
      if(diff>0){
        const m=Math.floor(diff/60000),s=Math.floor((diff%60000)/1000);
        tm.textContent=m+'m '+s+'s to open';
        el.classList.remove('urgent');
      }else{tm.textContent='Opening...';}
    }else{tm.textContent='Closed';el.classList.remove('urgent');}
    return;
  }
  const now=new Date();
  const ist=new Date(now.toLocaleString('en-US',{timeZone:'Asia/Kolkata'}));
  const close=new Date(ist);close.setHours(15,15,0,0);
  const diff=close-ist;
  if(diff<=0){tm.textContent='Session ended';el.classList.remove('urgent');return;}
  const h=Math.floor(diff/3600000),m=Math.floor((diff%3600000)/60000),s=Math.floor((diff%60000)/1000);
  tm.textContent=(h>0?h+'h ':'')+(m>0?m+'m ':'')+(s+'s');
  el.classList.toggle('urgent',diff<900000);
}

/* ── TOKEN EXPIRY ── */
function updateTokenBanner(){
  const exp=S.token_expiry||0;
  const ban=document.getElementById('token-ban');
  if(!exp){ban.className='token-ban';ban.style.display='none';return;}
  const now=Math.floor(Date.now()/1000);
  const hrs=(exp-now)/3600;
  if(hrs<0){ban.className='token-ban crit';ban.innerHTML='&#x26A0; DHAN TOKEN EXPIRED — Renew immediately at api.dhan.co';ban.style.display='block';}
  else if(hrs<24){ban.className='token-ban crit';ban.innerHTML='&#x26A0; Token expires in '+Math.floor(hrs)+'h '+Math.floor((hrs%1)*60)+'m — Renew at api.dhan.co';ban.style.display='block';}
  else if(hrs<72){ban.className='token-ban warn';ban.innerHTML='Token expires in '+Math.floor(hrs/24)+'d '+Math.floor(hrs%24)+'h — Consider renewing at api.dhan.co';ban.style.display='block';}
  else{ban.className='token-ban';ban.style.display='none';}
}

/* ── P&L ALERTS ── */
let _lastPnl=null,_alertCooldown=0;
function checkPnlAlerts(){
  const pos=S.positions||[];
  if(!pos.length)return;
  const totalUnr=pos.reduce((s,p)=>s+(p.pnl||0),0);
  if(_lastPnl===null){_lastPnl=totalUnr;return;}
  const diff=totalUnr-_lastPnl;
  const now=Date.now();
  if(now<_alertCooldown){_lastPnl=totalUnr;return;}
  if(Math.abs(diff)>=500){
    showPnlAlert(diff>=0,diff);
    _alertCooldown=now+30000;
    if('Notification' in window&&Notification.permission==='granted'){
      new Notification('Omega P&L Alert',{body:(diff>=0?'+':'')+'₹'+Math.abs(diff).toFixed(0)+' change',icon:'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><text y="28" font-size="28">Ω</text></svg>'});
    }
  }
  _lastPnl=totalUnr;
}
function showPnlAlert(isProfit,amt){
  const el=document.createElement('div');
  el.className='pnl-alert '+(isProfit?'profit':'loss');
  el.innerHTML=(isProfit?'&#x1F4C8; +':'&#x1F4C9; ')+'₹'+Math.abs(amt).toFixed(0)+' unrealised swing';
  document.getElementById('pnl-alerts').appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transition='opacity .3s';setTimeout(()=>el.remove(),400)},4000);
}
if('Notification' in window&&Notification.permission==='default'){Notification.requestPermission();}

/* ── MODAL SYSTEM ── */
function showModal(title,body,onSave){
  const root=document.getElementById('modal-root');
  root.innerHTML='<div class="modal-ov" onclick="if(event.target===this)closeModal()">'+
    '<div class="modal-box"><div class="modal-title">'+title+'</div>'+body+
    '<div class="modal-btns"><button class="modal-btn modal-btn-c" onclick="closeModal()">Cancel</button>'+
    '<button class="modal-btn modal-btn-p" id="modal-save">Save</button></div></div></div>';
  document.getElementById('modal-save').onclick=onSave;
}
function closeModal(){document.getElementById('modal-root').innerHTML='';}

function editSL(sid,symbol,currentSL){
  showModal('Edit Stop Loss — '+symbol,
    '<div class="modal-row"><div class="modal-lbl">Current SL</div><div class="mono" style="font-size:16px;color:var(--loss)">₹'+fmt(currentSL)+'</div></div>'+
    '<div class="modal-row"><div class="modal-lbl">New Stop Loss</div><input class="modal-inp" id="inp-sl" type="number" step="0.05" value="'+currentSL.toFixed(2)+'" placeholder="Enter new SL price"></div>',
    async()=>{
      const v=parseFloat(document.getElementById('inp-sl').value);
      if(!v||v<=0){showToast('Invalid SL price','err');return;}
      const r=await fetch('/api/modify_sl',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({security_id:sid,new_sl:v})});
      const d=await r.json();
      if(d.ok){showToast(d.symbol+' SL → ₹'+fmt(v),'ok');closeModal();}else{showToast('Error: '+(d.error||'unknown'),'err');}
    });
}

function editTP(sid,symbol,currentTP){
  showModal('Edit Take Profit — '+symbol,
    '<div class="modal-row"><div class="modal-lbl">Current TP</div><div class="mono" style="font-size:16px;color:var(--profit)">'+(currentTP>0?'₹'+fmt(currentTP):'Not set')+'</div></div>'+
    '<div class="modal-row"><div class="modal-lbl">New Take Profit</div><input class="modal-inp" id="inp-tp" type="number" step="0.05" value="'+(currentTP>0?currentTP.toFixed(2):'')+'" placeholder="Enter TP price"></div>',
    async()=>{
      const v=parseFloat(document.getElementById('inp-tp').value);
      if(!v||v<=0){showToast('Invalid TP price','err');return;}
      const r=await fetch('/api/modify_tp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({security_id:sid,new_tp:v})});
      const d=await r.json();
      if(d.ok){showToast(d.symbol+' TP → ₹'+fmt(v),'ok');closeModal();}else{showToast('Error: '+(d.error||'unknown'),'err');}
    });
}

/* ── TRADE NOTES ── */
let _noteTm={};
function saveNote(key,el){
  clearTimeout(_noteTm[key]);
  _noteTm[key]=setTimeout(async()=>{
    const note=el.value.trim();
    await fetch('/api/add_note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trade_key:key,note:note})});
  },800);
}

/* ── EXPORT CSV ── */
function exportCSV(){window.open('/api/export_csv','_blank');}

/* ── FETCH LOOP ── */
async function poll(){
  try{
    const r=await fetch('/api/state');
    S=await r.json();
    document.getElementById('live-dot').style.background='#10b981';
    render();
    checkPnlAlerts();
  }catch(e){
    document.getElementById('live-dot').style.background='#ef4444';
  }
  setTimeout(poll,1000);
}
poll();
})();
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=_HTML)
