"""
OTC Dashboard — Omega Trading Company Web UI

Client-side SPA served at /dashboard on port 8000.
Fetches /api/state every second for live updates.
Tab navigation: Dashboard | Positions | Trades | Signals
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

IST = ZoneInfo("Asia/Kolkata")
router = APIRouter()

_CSRF_TOKEN = secrets.token_hex(32)


def _check_csrf(request: Request):
    token = request.headers.get("X-CSRF-Token", "")
    if not secrets.compare_digest(token, _CSRF_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

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
    for _ in range(3):
        try:
            tmp.write_text(json.dumps(data, default=str))
            tmp.replace(STATE_FILE)
            return
        except (FileNotFoundError, OSError):
            pass
    try:
        STATE_FILE.write_text(json.dumps(data, default=str))
    except Exception:
        pass


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

    return {"sharpe": round(sharpe, 2), "max_dd": round(dd), "max_dd_pct": round(dd / 2000 * 100, 1),
            "avg_win": round(avg_win), "avg_loss": round(avg_loss), "rr_ratio": round(rr, 2),
            "best_streak": best_s, "worst_streak": worst_s, "current_streak": run,
            "streak_type": last or "", "profit_factor": round(pf, 2), "expectancy": round(mean)}


_bot: dict = {}


def register_bot(**refs):
    """Called by main.py to give dashboard access to strategy/pm/broker."""
    _bot.update(refs)


def _get_ml_info() -> dict:
    strategy = _bot.get("strategy")
    if not strategy or not hasattr(strategy, "ml_engine") or not strategy.ml_engine:
        return {}
    try:
        return strategy.ml_engine.get_model_stats()
    except Exception:
        return {}


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


def _token_hours_remaining() -> float:
    import base64 as _b64, os as _os, time as _time
    tok = _os.environ.get("DHAN_ACCESS_TOKEN", "")
    if not tok:
        return -1
    try:
        payload = tok.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(_b64.urlsafe_b64decode(payload))
        exp = int(data.get("exp", 0))
        return (exp - _time.time()) / 3600
    except Exception:
        return -1


@router.get("/health")
async def health():
    return {"status": "ok"}


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
        "token_hours_remaining": _token_hours_remaining(),
        "trade_notes": notes,
        "index_prices": idx.get("index_prices", {}),
        "feed_health": idx.get("feed_health", {}),
        "consecutive_losses": idx.get("consecutive_losses", 0),
        "loss_cooldown_remaining": idx.get("loss_cooldown_remaining", 0),
        "csrf": _CSRF_TOKEN,
        "ml": _get_ml_info(),
    }


@router.post("/api/close_position")
async def close_position(request: Request):
    """Manually close a single position from the dashboard."""
    _check_csrf(request)
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

    live_feed = _bot.get("live_feed")
    ltp = 0
    if live_feed:
        ltp = live_feed.get_ltp(pos.security_id, pos.exchange_segment)
    if ltp <= 0:
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
    if trade:
        margin = trade.entry_price * trade.quantity
        if wallet:
            wallet.release_margin(margin, trade.pnl, pos.symbol, reason)
        guardrails = _bot.get("guardrails")
        if guardrails:
            try:
                guardrails.release_allocation(pos.symbol, margin)
            except Exception:
                pass
        try:
            from core.database import OmegaDB
            db = OmegaDB.get()
            db.record_trade(
                symbol=trade.symbol, direction=trade.direction,
                option_type=trade.option_type, quantity=trade.quantity,
                entry_price=trade.entry_price, exit_price=trade.exit_price,
                pnl=trade.pnl,
                entry_time=trade.entry_time.isoformat(),
                exit_time=trade.exit_time.isoformat(),
                exit_reason=reason, strategy=pos.strategy,
                balance_after=wallet.balance if wallet else 0,
            )
            db.delete_position(pos.security_id)
        except Exception as e:
            log.warning("DB trade record failed: %s", e)
        if strategy and hasattr(strategy, "_consecutive_losses"):
            if trade.pnl >= 0:
                strategy._consecutive_losses = 0
            else:
                strategy._consecutive_losses += 1

        tg = getattr(strategy, "tg", None) if strategy else None
        if tg:
            try:
                hold = (trade.exit_time - trade.entry_time).total_seconds()
                tg.sl_hit(pos.symbol, round(trade.pnl, 2), reason=reason,
                          entry=trade.entry_price, exit_price=fill,
                          duration=f"{int(hold // 60)}m (dashboard)")
            except Exception:
                pass

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
    _check_csrf(request)
    import logging
    log = logging.getLogger("omega.dashboard")

    pm = _bot.get("pm")
    broker = _bot.get("broker")
    wallet = _bot.get("wallet")

    if not pm or not broker:
        return {"ok": False, "error": "Bot not connected"}

    live_feed = _bot.get("live_feed")
    results = []
    for sid in list(pm._open.keys()):
        pos = pm._open.get(sid)
        if not pos:
            continue

        ltp = 0
        if live_feed:
            ltp = live_feed.get_ltp(pos.security_id, pos.exchange_segment)
        if ltp <= 0:
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
        if trade:
            margin = trade.entry_price * trade.quantity
            if wallet:
                wallet.release_margin(margin, trade.pnl, pos.symbol, "MANUAL_SQUARE_OFF")
            try:
                from core.database import OmegaDB
                db = OmegaDB.get()
                db.record_trade(
                    symbol=trade.symbol, direction=trade.direction,
                    option_type=trade.option_type, quantity=trade.quantity,
                    entry_price=trade.entry_price, exit_price=trade.exit_price,
                    pnl=trade.pnl,
                    entry_time=trade.entry_time.isoformat(),
                    exit_time=trade.exit_time.isoformat(),
                    exit_reason="MANUAL_SQUARE_OFF", strategy=pos.strategy,
                    balance_after=wallet.balance if wallet else 0,
                )
                db.delete_position(pos.security_id)
            except Exception as e:
                log.warning("DB trade record failed: %s", e)
            strategy = _bot.get("strategy")
            if strategy and hasattr(strategy, "_consecutive_losses"):
                if trade.pnl >= 0:
                    strategy._consecutive_losses = 0
                else:
                    strategy._consecutive_losses += 1
            tg = getattr(strategy, "tg", None) if strategy else None
            if tg:
                try:
                    hold = (trade.exit_time - trade.entry_time).total_seconds()
                    tg.sl_hit(pos.symbol, round(trade.pnl, 2), reason="MANUAL_SQUARE_OFF",
                              entry=trade.entry_price, exit_price=ltp,
                              duration=f"{int(hold // 60)}m (dashboard)")
                except Exception:
                    pass
        results.append({
            "symbol": pos.symbol,
            "pnl": trade.pnl if trade else 0,
        })

    log.info("MANUAL SQUARE OFF from dashboard: %d positions closed", len(results))
    return {"ok": True, "closed": results}


@router.post("/api/modify_sl")
async def modify_sl(request: Request):
    """Manually adjust stop loss for a position."""
    _check_csrf(request)
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
    _check_csrf(request)
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
    _check_csrf(request)
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
    lines = ["Symbol,Direction,Option,Qty,Entry,Exit,P&L,P&L%,Reason,Entry Time,Exit Time"]
    for t in closed:
        e = t.get("entry", 0)
        x = t.get("exit", 0)
        pct = ((x - e) / e * 100) if e > 0 else 0
        if t.get("direction") == "SHORT":
            pct = -pct
        et = t.get("entry_time", "")
        xt = t.get("exit_time", "")
        lines.append(f"{t.get('symbol','')},{t.get('direction','')},{t.get('option_type','')},{t.get('quantity','')},{e},{x},{t.get('pnl',0):.2f},{pct:.1f},{t.get('reason','')},{et},{xt}")
    csv_text = "\n".join(lines)
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=omega_trades.csv"})


@router.get("/api/option_chain/{underlying}")
async def api_option_chain(underlying: str, expiry: str = ""):
    from core.option_chain import OptionChainResolver, UNDERLYING_MAP
    underlying = underlying.upper()
    if underlying not in UNDERLYING_MAP:
        return {"error": f"Unknown underlying: {underlying}"}
    resolver = _bot.get("_oc_resolver")
    if not resolver:
        import os
        resolver = OptionChainResolver(
            os.environ.get("DHAN_ACCESS_TOKEN", ""),
            os.environ.get("DHAN_CLIENT_ID", ""))
        _bot["_oc_resolver"] = resolver
    if not expiry:
        expiry = resolver.get_nearest_expiry(underlying)
    if not expiry:
        return {"error": "No expiry found"}
    chain = resolver.fetch_chain(underlying, expiry)
    expiries = resolver.get_expiry_list(underlying)
    oc = chain.get("data", chain).get("oc", {})
    spot = chain.get("data", chain).get("last_price", 0)
    all_rows = []
    for strike_str, sides in sorted(oc.items(), key=lambda x: float(x[0])):
        try:
            s = float(strike_str)
        except (ValueError, TypeError):
            continue
        ce = sides.get("ce", {})
        pe = sides.get("pe", {})
        all_rows.append({
            "strike": s,
            "ce_ltp": ce.get("last_price", 0),
            "ce_oi": ce.get("oi", 0),
            "ce_iv": ce.get("implied_volatility", 0),
            "ce_volume": ce.get("volume", 0),
            "ce_bid": ce.get("top_bid_price", 0),
            "ce_ask": ce.get("top_ask_price", 0),
            "ce_change": ce.get("change", 0),
            "pe_ltp": pe.get("last_price", 0),
            "pe_oi": pe.get("oi", 0),
            "pe_iv": pe.get("implied_volatility", 0),
            "pe_volume": pe.get("volume", 0),
            "pe_bid": pe.get("top_bid_price", 0),
            "pe_ask": pe.get("top_ask_price", 0),
            "pe_change": pe.get("change", 0),
        })
    active_rows = [r for r in all_rows if r["ce_oi"] > 0 or r["pe_oi"] > 0]
    if not active_rows and len(expiries) >= 2 and expiry == expiries[0]:
        expiry = expiries[1]
        chain = resolver.fetch_chain(underlying, expiry)
        oc = chain.get("data", chain).get("oc", {})
        spot = chain.get("data", chain).get("last_price", 0) or spot
        all_rows = []
        for strike_str, sides in sorted(oc.items(), key=lambda x: float(x[0])):
            try:
                s = float(strike_str)
            except (ValueError, TypeError):
                continue
            ce = sides.get("ce", {})
            pe = sides.get("pe", {})
            all_rows.append({
                "strike": s,
                "ce_ltp": ce.get("last_price", 0),
                "ce_oi": ce.get("oi", 0),
                "ce_iv": ce.get("implied_volatility", 0),
                "ce_volume": ce.get("volume", 0),
                "ce_bid": ce.get("top_bid_price", 0),
                "ce_ask": ce.get("top_ask_price", 0),
                "ce_change": ce.get("change", 0),
                "pe_ltp": pe.get("last_price", 0),
                "pe_oi": pe.get("oi", 0),
                "pe_iv": pe.get("implied_volatility", 0),
                "pe_volume": pe.get("volume", 0),
                "pe_bid": pe.get("top_bid_price", 0),
                "pe_ask": pe.get("top_ask_price", 0),
                "pe_change": pe.get("change", 0),
            })
    if spot > 0:
        step = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100,
                "FINNIFTY": 50, "MIDCPNIFTY": 25}.get(underlying, 100)
        atm = round(spot / step) * step
        spread = step * 20
        rows = [r for r in all_rows if abs(r["strike"] - atm) <= spread]
    else:
        atm = 0
        rows = all_rows[:40]
    return {"underlying": underlying, "expiry": expiry, "expiries": expiries,
            "spot": spot, "atm": atm, "rows": rows,
            "server_time": datetime.now(IST).isoformat()}


@router.get("/api/trade_analytics")
async def api_trade_analytics():
    state = _read_state()
    idx = state.get("indices", {})
    closed = idx.get("closed_trades", [])
    hourly = {}
    by_symbol = {}
    by_reason = {}
    pnl_series = []
    cumulative = 0.0
    for t in closed:
        pnl = t.get("pnl", 0)
        cumulative += pnl
        pnl_series.append(round(cumulative, 2))
        et = t.get("entry_time", "")
        if et:
            try:
                hr = datetime.fromisoformat(et).hour
                hourly.setdefault(hr, {"count": 0, "pnl": 0})
                hourly[hr]["count"] += 1
                hourly[hr]["pnl"] = round(hourly[hr]["pnl"] + pnl, 2)
            except Exception:
                pass
        sym = t.get("symbol", "?")
        base = sym.rstrip("0123456789").rstrip("CEPE") if sym else "?"
        by_symbol.setdefault(base, {"count": 0, "pnl": 0, "wins": 0})
        by_symbol[base]["count"] += 1
        by_symbol[base]["pnl"] = round(by_symbol[base]["pnl"] + pnl, 2)
        if pnl >= 0:
            by_symbol[base]["wins"] += 1
        reason = t.get("reason", "?")
        by_reason.setdefault(reason, {"count": 0, "pnl": 0})
        by_reason[reason]["count"] += 1
        by_reason[reason]["pnl"] = round(by_reason[reason]["pnl"] + pnl, 2)
    return {"hourly": hourly, "by_symbol": by_symbol, "by_reason": by_reason,
            "pnl_series": pnl_series, "total_trades": len(closed)}


@router.get("/api/reverse_engine")
async def api_reverse_engine():
    from core.reverse_engine import ReverseEngine
    sig_gen = _bot.get("sig_gen")
    engine = ReverseEngine(sig_gen)
    report = engine.run_full_analysis()
    # Load autopsy details
    autopsy_file = Path("data/autopsy_results.json")
    autopsies = []
    if autopsy_file.exists():
        import json as _j
        autopsies = _j.loads(autopsy_file.read_text())
    report["autopsies"] = autopsies
    return report


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Omega Trading Company</title>
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
.tp{display:none}.tp.on{display:block}

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

/* REVERSE ENGINE CARDS */
.sc2{background:var(--glass);border:1px solid rgba(201,168,76,.06);border-radius:10px;padding:14px 12px;text-align:center}
.tbl{border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;font-size:10px;color:var(--gold);text-transform:uppercase;letter-spacing:.6px;
  padding:8px 10px;border-bottom:1px solid rgba(201,168,76,.1);font-weight:700}
.tbl td{padding:8px 10px;border-bottom:1px solid var(--border-dim);font-size:12px;white-space:nowrap}
.tbl tr:hover td{background:rgba(201,168,76,.02)}

/* TRADES TABLE */
.ttb-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
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
@keyframes confirmBlink{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.6;transform:scale(1.05)}}

/* FEED INDICATOR */
.feed-dot{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:4px}
.feed-ws{background:var(--profit);box-shadow:0 0 6px var(--profit);animation:pulse 2s infinite}
.feed-rest{background:var(--warn);box-shadow:0 0 4px var(--warn)}
.feed-off{background:var(--loss);box-shadow:0 0 4px var(--loss)}

/* OPTION CHAIN */
.oc-controls{display:flex;align-items:center;gap:12px;padding:12px 20px;flex-wrap:wrap}
.oc-btn{padding:6px 18px;border-radius:8px;border:1px solid var(--border);background:var(--bg-2);
  color:var(--text-2);font-size:12px;font-weight:600;cursor:pointer;transition:.2s;letter-spacing:.5px}
.oc-btn:hover,.oc-btn.on{background:var(--gold-dim);border-color:var(--gold);color:var(--gold)}
.oc-exp{padding:5px 12px;border-radius:6px;border:1px solid var(--border);background:var(--bg-2);
  color:var(--text-1);font-size:11px;font-family:'JetBrains Mono',monospace}
.oc-spot{font-size:13px;color:var(--accent-g);font-weight:700;font-family:'JetBrains Mono',monospace;
  margin-left:auto}
.oc-tbl{width:100%;border-collapse:collapse;font-size:11px;font-family:'JetBrains Mono',monospace}
.oc-tbl th{padding:8px 6px;background:rgba(201,168,76,.04);color:var(--text-3);
  font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;
  position:sticky;top:0;z-index:2;border-bottom:1px solid var(--border)}
.oc-tbl td{padding:5px 6px;text-align:right;border-bottom:1px solid var(--border-dim);white-space:nowrap}
.oc-tbl .ce-side{background:rgba(0,230,138,.02)}
.oc-tbl .pe-side{background:rgba(255,77,94,.02)}
.oc-tbl .strike-col{text-align:center;font-weight:700;color:var(--text-1);font-size:12px;
  background:rgba(201,168,76,.04);border-left:1px solid var(--border);border-right:1px solid var(--border)}
.oc-tbl .atm{background:rgba(201,168,76,.08) !important}
.oc-tbl .itm{opacity:.5}
.oc-wrap{max-height:500px;overflow-y:auto;border:1px solid var(--border);border-radius:12px;
  background:var(--bg-1)}
.oi-bar{height:4px;border-radius:2px;display:block;margin-top:2px}
.oi-ce{background:linear-gradient(90deg,transparent,var(--profit))}
.oi-pe{background:linear-gradient(270deg,transparent,var(--loss))}

/* ANALYTICS TAB */
.ana-section{margin-bottom:24px}
.ana-section .ct{margin-bottom:12px}
.heatmap{display:grid;grid-template-columns:repeat(auto-fill,minmax(60px,1fr));gap:4px}
.hm-cell{padding:10px 4px;text-align:center;border-radius:6px;font-size:11px;font-weight:600;
  font-family:'JetBrains Mono',monospace}
.reason-row{display:flex;align-items:center;gap:12px;padding:8px 12px;
  border-bottom:1px solid var(--border-dim);font-size:12px}
.reason-row:last-child{border-bottom:none}
.reason-lbl{min-width:100px;font-weight:600;color:var(--text-2);text-transform:uppercase;
  font-size:10px;letter-spacing:.5px}
.reason-ct{min-width:40px;text-align:right;color:var(--text-1);font-weight:700}
.reason-pnl{min-width:80px;text-align:right;font-weight:700;font-family:'JetBrains Mono',monospace}
.sym-row{display:flex;align-items:center;gap:12px;padding:8px 12px;
  border-bottom:1px solid var(--border-dim);font-size:12px}
.sym-row:last-child{border-bottom:none}
.sym-name{min-width:100px;font-weight:700;color:var(--accent-g)}
.sym-wr{min-width:60px;text-align:center;font-size:10px;padding:2px 8px;
  border-radius:10px;font-weight:700}

/* ANALYTICS */
.ana-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.ana-bar-row{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border-dim)}
.ana-bar-row:last-child{border-bottom:none}
.ana-lbl{min-width:90px;font-size:12px;font-weight:600;color:var(--text-2)}
.ana-bar{flex:1;height:24px;background:var(--bg-3);border-radius:6px;overflow:hidden;position:relative;
  box-shadow:inset 0 1px 3px rgba(0,0,0,.2)}
.ana-bar-fill{height:100%;border-radius:6px;transition:width .5s ease;position:relative;min-width:2px}
.ana-bar-fill::after{content:attr(data-val);position:absolute;right:8px;top:50%;transform:translateY(-50%);
  font-size:10px;font-weight:700;color:rgba(255,255,255,.9);text-shadow:0 1px 2px rgba(0,0,0,.5)}
.ana-stats{display:flex;gap:16px;font-size:11px;color:var(--text-3);min-width:140px;justify-content:flex-end}
.ana-stats b{color:var(--text-2)}
.ana-vs{display:flex;align-items:center;gap:16px;padding:20px 0}
.ana-vs-side{flex:1;text-align:center}
.ana-vs-val{font-size:32px;font-weight:800;line-height:1.2}
.ana-vs-lbl{font-size:10px;color:var(--text-3);text-transform:uppercase;letter-spacing:1.5px;margin-top:4px}
.ana-vs-sub{font-size:12px;color:var(--text-2);margin-top:4px}
.ana-vs-div{width:1px;height:80px;background:linear-gradient(180deg,transparent,var(--border),transparent)}
.ana-vs-badge{padding:4px 12px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:.5px;
  display:inline-block;margin-top:6px}
.donut-w{width:120px;height:120px;margin:0 auto 8px}

/* RESPONSIVE — TABLET */
@media(max-width:768px){
  .hdr{padding:0 16px;height:56px}
  .hdr-r{gap:8px}
  .brand-i{width:36px;height:36px;font-size:18px;border-radius:10px}
  .brand-n{font-size:15px;letter-spacing:1.5px}
  .pill{padding:4px 10px;font-size:9px}
  .clock{font-size:11px}
  .sess-timer{font-size:10px}
  .sbar{padding:7px 16px;gap:12px;font-size:10px}
  .idx-ticker{gap:16px;padding:8px 16px;flex-wrap:wrap;justify-content:flex-start}
  .idx-price{font-size:13px}
  .idx-name{font-size:9px}
  .wlt{flex-wrap:wrap}
  .wlt-i{padding:12px 16px;min-width:calc(33.33% - 1px);flex:1 1 auto}
  .wlt-v{font-size:15px}
  .wlt-l{font-size:8px;letter-spacing:1.5px}
  .tabs{padding:0 16px;gap:0}
  .tab{padding:11px 18px;font-size:12px}
  main{padding:16px}
  .tc{grid-template-columns:1fr}
  .sg{grid-template-columns:repeat(3,1fr);gap:8px}
  .sc{padding:14px 10px;border-radius:10px}
  .sv{font-size:20px}
  .sl{font-size:8px;letter-spacing:1px}
  .hero{padding:28px 0 8px}
  .hero-val{font-size:40px}
  .hero-sub{font-size:14px}
  .hero-lbl{font-size:9px;letter-spacing:3px}
  .cd{padding:16px;border-radius:12px}
  .pc{padding:18px;border-radius:12px}
  .pc-h{flex-direction:column;align-items:flex-start;gap:8px}
  .pc-p{flex-direction:column;gap:4px}
  .xd{grid-template-columns:1fr}
  .xc{padding:16px}
  .ttb{font-size:12px}
  .ttb th{padding:8px 8px;font-size:9px}
  .ttb td{padding:8px 8px}
  .oc-tbl{font-size:10px}
  .oc-tbl th{padding:6px 4px;font-size:8px}
  .oc-tbl td{padding:4px 4px}
  .ana-grid{grid-template-columns:1fr}
  .ana-bar-row{flex-wrap:wrap;gap:6px}
  .ana-stats{min-width:auto;gap:8px;width:100%}
  .ana-lbl{min-width:70px;font-size:11px}
  .ana-vs-val{font-size:24px}
  .rg{grid-template-columns:repeat(2,1fr);gap:10px}
  .riv{font-size:16px}
  .modal-box{padding:22px;min-width:auto;width:94%}
  .pnl-alert{right:16px;left:16px;font-size:12px}
  .ftr{padding:24px 16px}
  .token-ban{padding:8px 16px;font-size:11px}
}
/* RESPONSIVE — PHONE */
@media(max-width:480px){
  .hdr{height:52px;padding:0 12px}
  .hdr-r{gap:6px}
  .brand-i{width:32px;height:32px;font-size:16px;border-radius:8px}
  .brand-n{font-size:13px;letter-spacing:1px}
  .pill{padding:3px 8px;font-size:8px}
  .clock{display:none}
  .sess-timer{display:none}
  .mkt-st{font-size:8px;padding:3px 8px}
  .sbar{padding:6px 12px;gap:8px;font-size:9px}
  .idx-ticker{gap:10px;padding:6px 12px}
  .idx-price{font-size:12px}
  .wlt-i{min-width:calc(50% - 1px);padding:10px 12px}
  .wlt-v{font-size:14px}
  .tabs{padding:0 8px}
  .tab{padding:10px 14px;font-size:11px}
  main{padding:12px}
  .sg{grid-template-columns:repeat(2,1fr);gap:6px}
  .sc{padding:12px 8px;border-radius:8px}
  .sv{font-size:18px}
  .sl{font-size:7px;letter-spacing:.8px}
  .hero-val{font-size:32px}
  .hero-sub{font-size:13px}
  .cd{padding:14px;margin-bottom:12px}
  .ct{font-size:10px;letter-spacing:1px;margin-bottom:12px;padding-bottom:10px}
  .pc{padding:14px}
  .db{font-size:9px;padding:2px 8px}
  .sym{min-width:70px;font-size:12px}
  .ttb{font-size:11px}
  .ttb th{padding:6px 6px;font-size:8px;letter-spacing:.5px}
  .ttb td{padding:6px 6px;font-size:11px}
  .oc-controls{padding:8px 12px;gap:8px}
  .oc-btn{padding:5px 12px;font-size:11px}
  .oc-tbl{font-size:9px}
  .oc-tbl th{padding:5px 3px;font-size:7px}
  .oc-tbl td{padding:3px 3px}
  .oc-wrap{max-height:400px}
  .ana-bar-row{padding:6px 0}
  .ana-lbl{min-width:55px;font-size:10px}
  .ana-vs-val{font-size:20px}
  .donut-w{width:90px;height:90px}
  .rg{grid-template-columns:1fr 1fr;gap:8px}
  .riv{font-size:14px}
  .modal-box{padding:18px;border-radius:14px}
  .modal-title{font-size:15px}
  .modal-inp{padding:10px 12px;font-size:13px}
  .export-btn{padding:6px 14px;font-size:10px}
  .abtn{padding:5px 12px;font-size:11px}
  .sltp-row{flex-wrap:wrap}
  .ftr{padding:20px 12px;font-size:10px}
}
/* TABLE + OPTION CHAIN MOBILE SCROLL */
@media(max-width:640px){
  .ttb-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -12px;padding:0 12px}
  .ttb{min-width:600px}
  .oc-wrap{margin:0 -12px;border-radius:0;border-left:none;border-right:none}
  .oc-tbl{min-width:500px}
}

/* ── ARCHITECTURE / ML / RISK / GOAL ── */
.g3{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.g4{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
.g2{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.arch{padding:16px;background:var(--bg2);border:1px solid var(--bdr);border-radius:10px}
.arch h3{font-size:13px;font-weight:700;margin:8px 0 4px}
.arch p{font-size:10px;color:var(--t3);line-height:1.5}
.arch-i{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;
  font-size:18px;font-weight:900}
.ic-g{background:rgba(0,230,138,.1);color:var(--profit)}
.ic-p{background:rgba(168,85,247,.1);color:var(--purple)}
.ic-c{background:rgba(255,77,94,.1);color:var(--loss)}
.ic-gr{background:rgba(78,205,196,.1);color:var(--cyan)}
.ic-b{background:rgba(59,130,246,.1);color:var(--blue)}
.ic-w{background:rgba(240,185,11,.1);color:var(--warn)}
.tech{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.tech span{padding:2px 8px;border-radius:10px;font-size:8px;background:var(--bg3);color:var(--t2);
  border:1px solid var(--bdr)}
.ml-a{display:flex;align-items:center;justify-content:center;gap:6px;padding:16px 0;flex-wrap:wrap}
.ml-n{text-align:center;background:var(--bg3);border-radius:8px;padding:12px 18px;border:1px solid var(--bdr)}
.nv{font-size:22px;font-weight:800;font-family:'JetBrains Mono',monospace}
.nl{font-size:9px;color:var(--t3);margin-top:2px}
.ml-ar{color:var(--t3);font-size:18px}
.rg{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px}
.rg-l{font-size:10px;color:var(--t3);margin-bottom:3px}
.rg-v{font-size:14px;font-weight:700}
.pgb{height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;margin-top:4px}
.pgf{height:100%;border-radius:3px;transition:width .3s}
.rzb{padding:4px 12px;border-radius:12px;font-size:10px;font-weight:700;letter-spacing:.5px}
.sebi{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}
.sebi-i{display:flex;gap:8px;padding:10px;background:var(--bg3);border-radius:8px;align-items:flex-start}
.sebi-ck{color:var(--profit);font-weight:700;font-size:14px;flex-shrink:0}
.sebi-i h4{font-size:11px;font-weight:700;margin-bottom:2px}
.sebi-i p{font-size:10px;color:var(--t3)}
.goal{display:flex;align-items:center;justify-content:center;gap:8px;flex-wrap:wrap;padding:20px 0}
.goal-s{padding:14px 20px;border:1px solid var(--bdr);border-radius:10px;text-align:center;
  background:var(--bg3);min-width:80px}
.goal-s.now{border-color:var(--gold);box-shadow:0 0 12px rgba(201,168,76,.15)}
.goal-l{font-size:9px;color:var(--t3);margin-bottom:4px;text-transform:uppercase;letter-spacing:1px}
.goal-v{font-size:18px;font-weight:800}
.goal-a{color:var(--t3);font-size:16px}
</style>
</head>
<body>

<div class="token-ban" id="token-ban"></div>

<header class="hdr">
  <div class="brand">
    <div class="brand-i">&Omega;</div>
    <div class="brand-n">OTC</div>
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
  <button class="tab" data-t="chain">Option Chain</button>
  <button class="tab" data-t="analytics">Analytics</button>
  <button class="tab" data-t="arch">Architecture</button>
  <button class="tab" data-t="ml">ML Engine</button>
  <button class="tab" data-t="risk">Risk</button>
  <button class="tab" data-t="goal">Goal</button>
  <button class="tab" data-t="reverse">Reverse Engine</button>
</nav>

<main>
  <div class="tp on" id="tp-dash"><div class="loading">Loading</div></div>
  <div class="tp" id="tp-pos"></div>
  <div class="tp" id="tp-trades"></div>
  <div class="tp" id="tp-sigs"></div>
  <div class="tp" id="tp-chain"><div class="loading">Select an index to load option chain</div></div>
  <div class="tp" id="tp-analytics"><div class="loading">Loading analytics...</div></div>
  <div class="tp" id="tp-arch"></div>
  <div class="tp" id="tp-ml"></div>
  <div class="tp" id="tp-risk"></div>
  <div class="tp" id="tp-goal"></div>
  <div class="tp" id="tp-reverse"><div class="loading">Click "Run Analysis" to start reverse engineering</div></div>
</main>

<div class="ftr"><span class="serif" style="color:var(--gold);font-size:13px;letter-spacing:2px">Omega Trading Company</span><br><span style="margin-top:4px;display:inline-block">Precision. Performance. Profit.</span></div>
<div id="modal-root"></div>
<div id="pnl-alerts"></div>

<script>
(function(){
let _csrf='__CSRF_TOKEN__';
let S=null,tab='dash';

function fmt(n,d){
  if(d===undefined)d=2;
  return new Intl.NumberFormat('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}).format(n);
}
function fpnl(n){return (n>=0?'+':'')+String.fromCharCode(8377)+fmt(Math.abs(n))}
function pc(n){return n>=0?'var(--profit)':'var(--loss)'}
function esc(s){if(!s)return '';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function cc(c){return c==='HIGH'?'cb-h':c==='MEDIUM'?'cb-m':'cb-lo'}
function scc(s){return s>=70?'var(--profit)':s>=45?'var(--warn)':'var(--loss)'}
function parseSym(sym){
  const m=sym.match(/^(NIFTY|BANKNIFTY|SENSEX|FINNIFTY|MIDCPNIFTY)(\\d+)(CE|PE)$/i);
  if(!m) return {idx:sym,strike:'',type:'',label:sym};
  return {idx:m[1],strike:m[2],type:m[3],label:m[1]+' '+m[2]+' '+m[3]};
}
function fmtExpiry(exp){
  if(!exp)return '';
  try{
    const d=new Date(exp+'T00:00:00');
    const mon=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return d.getDate()+' '+mon[d.getMonth()]+' '+String(d.getFullYear()).slice(-2);
  }catch(e){return exp;}
}
function instrLabel(sym,expiry,dir,ot,qty){
  const p=parseSym(sym);
  const side=dir==='LONG'?'BUY':'SELL';
  const exp=fmtExpiry(expiry);
  return (exp?exp+' ':'')+ p.idx+' '+p.strike+' '+(ot||p.type)+' '+side+(qty?' x'+qty:'');
}

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
    renderTab();
    if(tab==='chain'&&!_chainData)loadChain('NIFTY');
    if(tab==='analytics'){_anaLoaded=false;}
  };
});

function render(){
  if(!S)return;
  renderHdr();
  renderIdx();
  renderWlt();
  rDash();rPos();rTrades();rSigs();
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
  const fh=S.feed_health||{};
  const fMode=fh.mode||'unknown';
  const fWs=fh.ws_connected;
  const fTpm=fh.ticks_per_minute||0;
  const fColor=fWs?'var(--profit)':fMode==='rest_only'?'var(--loss)':'var(--warn)';
  const fLabel=fWs?'WS Live':fMode==='rest_only'?'REST Only':fMode==='rest_fallback'?'REST Fallback':fMode;
  const closs=S.consecutive_losses||0;
  const clossHtml=closs>=2?'<span>Recovery: <b style="color:var(--loss)">'+closs+' losses</b></span>':'';
  const cooldown=S.loss_cooldown_remaining||0;
  const coolHtml=cooldown>0?'<span style="color:var(--loss)">Cooldown: <b>'+Math.ceil(cooldown/60)+'m</b></span>':'';

  document.getElementById('sbar').innerHTML=
    '<span>Watching: <b>'+w.join(', ')+'</b></span>'+
    '<span>Scans: <b>'+sc+'</b></span>'+
    '<span>Started: '+saT+'</span>'+
    '<span>Updated: '+luT+'</span>'+
    '<span>Feed: <b style="color:'+fColor+'">'+fLabel+'</b>'+(fTpm>0?' <span style="color:var(--text-3)">('+fTpm.toFixed(0)+' ticks/m)</span>':'')+'</span>'+
    '<span>Market: <b style="color:'+(ms==='OPEN'?'var(--profit)':ms==='CLOSING'?'#f97316':'var(--text-3)')+'">'+ms+'</b></span>'+
    clossHtml+coolHtml;
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
  const sc=w.starting_capital||100000;
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
    case'chain':rChain();break;
    case'analytics':rAnalytics();break;
    case'arch':rArch();break;
    case'ml':rML();break;
    case'risk':rRisk();break;
    case'goal':rGoal();break;
  }
}

/* ── DASHBOARD ── */
function rDash(){
  if(Object.keys(_confirmTimers).length>0||_acting)return;
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

  const buyT=closed.filter(t=>t.direction==='LONG');
  const sellT=closed.filter(t=>t.direction!=='LONG');
  const buyPnl=buyT.reduce((a,t)=>a+(t.pnl||0),0);
  const sellPnl=sellT.reduce((a,t)=>a+(t.pnl||0),0);
  const buyW=buyT.filter(t=>(t.pnl||0)>=0).length;
  const sellW=sellT.filter(t=>(t.pnl||0)>=0).length;

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
    h+='<div class="sg" style="margin-top:12px">'+
      sc2(fpnl(buyPnl),'BUY P&L',pc(buyPnl),'border-left:3px solid var(--profit)',''+buyT.length+' trades ('+buyW+'W/'+(buyT.length-buyW)+'L)')+
      sc2(fpnl(sellPnl),'SELL P&L',pc(sellPnl),'border-left:3px solid var(--loss)',''+sellT.length+' trades ('+sellW+'W/'+(sellT.length-sellW)+'L)')+
    '</div>';
  }

  if(closed.length>0){
    h+='<div class="cd"><div class="ct">Equity Curve</div><div class="eq-w"><canvas id="eq-cv"></canvas></div></div>';
  }

  h+='<div class="tc"><div class="cd"><div class="ct">Open Positions</div>';
  if(!pos.length)h+='<div class="emp">No open positions</div>';
  else pos.forEach(p=>{
    const e=p.entry||0,c=p.current||0;
    const isShort=p.direction!=='LONG';
    let pct=e>0?((c-e)/e*100):0;
    if(isShort)pct=-pct;
    const sid=p.security_id||'';
    const reason=(p.pnl||0)>=0?'BOOK_PROFIT':'BOOK_LOSS';
    h+='<div class="pr">'+
      '<span class="db '+(isShort?'db-s':'db-l')+'">'+(isShort?'SELL':'BUY')+'</span>'+
      '<span class="sym">'+parseSym(p.symbol||'').label+'</span>'+
      '<span class="mono" style="font-size:12px">'+R+fmt(e)+' &rarr; '+R+fmt(c)+'</span>'+
      '<span class="mono" style="color:'+pc(p.pnl||0)+';font-weight:600">'+fpnl(p.pnl||0)+'</span>'+
      '<span style="color:'+pc(p.pnl||0)+';font-size:11px">'+(pct>=0?'+':'')+pct.toFixed(1)+'%</span>'+
      (sid?'<button class="abtn '+((p.pnl||0)>=0?'abtn-profit':'abtn-loss')+'" style="margin-left:auto;padding:3px 10px;font-size:10px" '+
      'onclick="doAction(\\'/api/close_position\\',{security_id:\\''+sid+'\\',reason:\\''+reason+'\\'},null,this)">Close</button>':'')+
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

  if(closed.length>=3){
    h+=renderAnalytics(closed);
  }

  if(risk&&Object.keys(risk).length){
    const z=risk.risk_zone||'SAFE';
    const zc={SAFE:'var(--profit)',ELEVATED:'var(--warn)',WARNING:'#f97316',CRITICAL:'var(--loss)'}[z]||'var(--text-3)';
    const rP=risk.daily_pnl||0;
    const lp=risk.daily_loss_used_pct||0;
    const bw=Math.min(lp,100);
    const bc=lp<30?'var(--profit)':lp<50?'var(--warn)':lp<80?'#f97316':'var(--loss)';
    const cl=risk.consecutive_losses||0;
    const killed=risk.bot_killed?' &mdash; BOT KILLED':'';

    h+='<div class="cd rp" style="border-left-color:'+zc+'"><div class="ct">Risk Monitor</div>'+
      '<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">'+
      '<span class="rzb" style="background:'+zc+'22;color:'+zc+';border:1px solid '+zc+'44">'+z+killed+'</span>'+
      '<span class="mono" style="color:'+pc(rP)+';font-weight:700;font-size:16px">'+fpnl(rP)+'</span>'+
      '<span style="color:var(--text-3);font-size:12px">('+((risk.daily_pnl_pct||0)>=0?'+':'')+
      (risk.daily_pnl_pct||0).toFixed(2)+'%)</span></div>'+
      '<div class="rg">'+
      '<div><div class="ril">Daily Loss Used</div>'+
      '<div class="pgb"><div class="pgf" style="width:'+bw+'%;background:'+bc+'"></div></div>'+
      '<div class="mono" style="font-size:12px;font-weight:600;margin-top:2px">'+lp.toFixed(0)+'%</div></div>'+
      '<div><div class="ril">Trades Today</div><div class="riv mono">'+(risk.trades_today||0)+
      ((risk.max_daily_trades||999)<100?' <span style="color:var(--text-3);font-size:12px">/ '+(risk.max_daily_trades||999)+'</span>':'')+'</div></div>'+
      '<div><div class="ril">Consecutive Losses</div><div class="riv mono" style="color:'+(cl>=2?'var(--loss)':'var(--text-1)')+'">'+cl+'</div></div>'+
      '<div><div class="ril">Recovery Needed</div><div class="riv mono" style="color:'+
      ((risk.drawdown_recovery_pct||0)>5?'var(--loss)':'var(--text-1)')+'">'+(risk.drawdown_recovery_pct||0).toFixed(1)+'%</div></div>'+
      '</div></div>';
  }

  document.getElementById('tp-dash').innerHTML=h;
  if(closed.length>0)drawEq(closed);
}
function renderAnalytics(closed){
  const R=String.fromCharCode(8377);
  let h='';

  const byIdx={};
  closed.forEach(t=>{
    let idx='Other';
    const sym=(t.symbol||'').toUpperCase();
    if(sym.includes('BANKNIFTY'))idx='BANKNIFTY';
    else if(sym.includes('NIFTY'))idx='NIFTY';
    else if(sym.includes('SENSEX'))idx='SENSEX';
    if(!byIdx[idx])byIdx[idx]={pnl:0,trades:0,wins:0};
    byIdx[idx].pnl+=t.pnl||0;
    byIdx[idx].trades++;
    if((t.pnl||0)>=0)byIdx[idx].wins++;
  });
  const maxIdxPnl=Math.max(...Object.values(byIdx).map(v=>Math.abs(v.pnl)),1);

  const byCE={pnl:0,trades:0,wins:0},byPE={pnl:0,trades:0,wins:0};
  closed.forEach(t=>{
    const sym=(t.symbol||'').toUpperCase();
    if(sym.includes('CE')){byCE.pnl+=t.pnl||0;byCE.trades++;if((t.pnl||0)>=0)byCE.wins++;}
    if(sym.includes('PE')){byPE.pnl+=t.pnl||0;byPE.trades++;if((t.pnl||0)>=0)byPE.wins++;}
  });

  const byReason={};
  closed.forEach(t=>{
    const r=t.reason||'UNKNOWN';
    if(!byReason[r])byReason[r]={pnl:0,trades:0,wins:0};
    byReason[r].pnl+=t.pnl||0;
    byReason[r].trades++;
    if((t.pnl||0)>=0)byReason[r].wins++;
  });
  const maxReasonPnl=Math.max(...Object.values(byReason).map(v=>Math.abs(v.pnl)),1);

  h+='<div class="ana-grid">';

  h+='<div class="cd"><div class="ct">Performance by Index</div>';
  Object.entries(byIdx).sort((a,b)=>b[1].pnl-a[1].pnl).forEach(([name,d])=>{
    const wr=d.trades>0?(d.wins/d.trades*100):0;
    const barW=Math.abs(d.pnl)/maxIdxPnl*100;
    const barC=d.pnl>=0?'var(--profit)':'var(--loss)';
    h+='<div class="ana-bar-row"><span class="ana-lbl">'+name+'</span>'+
      '<div class="ana-bar"><div class="ana-bar-fill" style="width:'+barW+'%;background:'+barC+'" data-val="'+fpnl(d.pnl)+'"></div></div>'+
      '<div class="ana-stats"><span>'+d.trades+'t</span><span><b>'+wr.toFixed(0)+'%</b> WR</span></div></div>';
  });
  h+='</div>';

  h+='<div class="cd"><div class="ct">CE vs PE</div><div class="ana-vs">';
  const ceWr=byCE.trades>0?(byCE.wins/byCE.trades*100):0;
  const peWr=byPE.trades>0?(byPE.wins/byPE.trades*100):0;
  const ceWin=byCE.pnl>byPE.pnl;
  h+='<div class="ana-vs-side"><div class="ana-vs-val mono" style="color:'+(byCE.pnl>=0?'var(--profit)':'var(--loss)')+'">'+fpnl(byCE.pnl)+'</div>'+
    '<div class="ana-vs-lbl">Call Options (CE)</div>'+
    '<div class="ana-vs-sub">'+byCE.trades+' trades &middot; '+ceWr.toFixed(0)+'% WR</div>'+
    '<span class="ana-vs-badge" style="background:'+(ceWin?'var(--profit-bg)':'var(--loss-bg)')+';color:'+(ceWin?'var(--profit)':'var(--loss)')+';border:1px solid '+(ceWin?'rgba(0,230,138,.2)':'rgba(255,77,94,.2)')+'">'+(ceWin?'OUTPERFORMING':'UNDERPERFORMING')+'</span></div>';
  h+='<div class="ana-vs-div"></div>';
  h+='<div class="ana-vs-side"><div class="ana-vs-val mono" style="color:'+(byPE.pnl>=0?'var(--profit)':'var(--loss)')+'">'+fpnl(byPE.pnl)+'</div>'+
    '<div class="ana-vs-lbl">Put Options (PE)</div>'+
    '<div class="ana-vs-sub">'+byPE.trades+' trades &middot; '+peWr.toFixed(0)+'% WR</div>'+
    '<span class="ana-vs-badge" style="background:'+(!ceWin?'var(--profit-bg)':'var(--loss-bg)')+';color:'+(!ceWin?'var(--profit)':'var(--loss)')+';border:1px solid '+(!ceWin?'rgba(0,230,138,.2)':'rgba(255,77,94,.2)')+'">'+(!ceWin?'OUTPERFORMING':'UNDERPERFORMING')+'</span></div>';
  h+='</div></div>';

  h+='</div>';

  h+='<div class="cd"><div class="ct">Exit Reason Analysis</div>';
  Object.entries(byReason).sort((a,b)=>b[1].trades-a[1].trades).forEach(([reason,d])=>{
    const wr=d.trades>0?(d.wins/d.trades*100):0;
    const barW=Math.abs(d.pnl)/maxReasonPnl*100;
    const barC=d.pnl>=0?'var(--profit)':'var(--loss)';
    const label=reason.replace(/_/g,' ');
    h+='<div class="ana-bar-row"><span class="ana-lbl">'+label+'</span>'+
      '<div class="ana-bar"><div class="ana-bar-fill" style="width:'+barW+'%;background:'+barC+'" data-val="'+fpnl(d.pnl)+'"></div></div>'+
      '<div class="ana-stats"><span>'+d.trades+'t</span><span><b>'+wr.toFixed(0)+'%</b> WR</span></div></div>';
  });
  h+='</div>';

  return h;
}

function sc2(v,l,c,st,sub,cls){
  return '<div class="sc'+(cls?' '+cls:'')+'"'+(st?' style="'+st+'"':'')+'>'+
    '<div class="sv mono"'+(c?' style="color:'+c+'"':'')+'>'+v+'</div>'+
    '<div class="sl">'+l+'</div>'+(sub?'<div class="ss">'+sub+'</div>':'')+'</div>';
}

/* ── ACTIONS ── */
let _acting=false;
const _confirmTimers={};
async function doAction(url,body,confirmMsg,btnEl){
  if(_acting)return;
  if(btnEl){
    const key=url+JSON.stringify(body);
    if(!_confirmTimers[key]){
      const origText=btnEl.textContent;
      const origClass=btnEl.className;
      btnEl.textContent='TAP AGAIN';
      btnEl.style.cssText+='animation:confirmBlink .5s ease 3;';
      _confirmTimers[key]=setTimeout(()=>{btnEl.textContent=origText;btnEl.className=origClass;btnEl.style.animation='';delete _confirmTimers[key]},2500);
      return;
    }
    clearTimeout(_confirmTimers[key]);delete _confirmTimers[key];
    btnEl.textContent='Closing...';btnEl.disabled=true;
  }
  _acting=true;
  try{
    const ac=new AbortController();
    const tm=setTimeout(()=>ac.abort(),8000);
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':_csrf},body:JSON.stringify(body),signal:ac.signal});
    clearTimeout(tm);
    const d=await r.json();
    if(d.ok){showToast(d.symbol?(d.symbol+' closed @ '+String.fromCharCode(8377)+(d.fill_price||0).toFixed(2)+' ('+fpnl(d.pnl||0)+')'):('Closed '+d.closed?.length+' positions'),'ok')}
    else{showToast('Error: '+(d.error||'unknown'),'err')}
  }catch(e){showToast(e.name==='AbortError'?'Timeout — check positions':'Network error','err')}
  _acting=false;
  if(btnEl){btnEl.disabled=false;}
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
  if(Object.keys(_confirmTimers).length>0||_acting)return;
  const pos=S.positions||[];
  const R=String.fromCharCode(8377);
  if(!pos.length){
    document.getElementById('tp-pos').innerHTML='<div class="cd"><div class="emp" style="padding:40px 0"><div style="font-size:28px;margin-bottom:8px">&#x1F4CA;</div>No open positions right now</div></div>';
    return;
  }
  let h='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'+
    '<span style="font-size:14px;font-weight:600;color:var(--text-2)">'+pos.length+' Open Position'+(pos.length>1?'s':'')+'</span>'+
    '<button class="abtn abtn-danger" onclick="doAction(\\'/api/close_all\\',{},null,this)">'+
    'Close All Positions</button></div>';

  pos.forEach(p=>{
    const e=p.entry||0,c=p.current||0,sl=p.sl||0;
    const sid=p.security_id||'';
    const tp=p.tp||0;
    const conf=p.confidence||'';
    const isShort=p.direction!=='LONG';
    let pct=e>0?((c-e)/e*100):0;
    if(isShort)pct=-pct;
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

    const dl=p.dynamic_level||1;
    const ot=p.option_type||'';
    let holdStr='';
    if(p.entry_time){try{const et=new Date(p.entry_time);const diff=Date.now()-et.getTime();const hm=Math.floor(diff/60000);holdStr=hm>=60?Math.floor(hm/60)+'h '+hm%60+'m':hm+'m';}catch(e){}}

    const il=instrLabel(p.symbol||'',p.expiry||'',p.direction||'',ot,p.quantity||0);
    const isSpr=p.is_spread||false;
    if(isSpr){
      const sellSym=parseSym(p.symbol||'').label;
      const hedgeSym=parseSym(p.hedge_trade_symbol||'').label;
      const nc=p.net_credit||0;
      const ml=p.max_loss||0;
      const sw=p.spread_width||0;
      const hep=p.hedge_entry_price||0;
      const hcp=p.hedge_current_price||0;
      const sellEntry=e;
      const sellCurr=c;
      const netEntry=nc;
      const netCurr=(sellCurr-hcp);
      const sprPnl=pnlVal;
      const maxProfit=nc*p.quantity;
      const maxLoss=ml*p.quantity;
      const profitPct=nc>0?((nc-netCurr)/nc*100):0;
      h+='<div class="pc" style="border-left:3px solid #a855f7"><div class="pc-h"><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'+
        '<span style="padding:3px 10px;border-radius:4px;font-size:11px;font-weight:700;background:rgba(168,85,247,.18);color:#c084fc;border:1px solid rgba(168,85,247,.3);letter-spacing:.5px">CREDIT SPREAD</span>'+
        '<span class="db db-s">SELL '+(ot||'')+'</span>'+
        (p.expiry?'<span style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;background:rgba(59,130,246,.1);color:#60a5fa;border:1px solid rgba(59,130,246,.15)">'+esc(fmtExpiry(p.expiry))+'</span>':'')+
        (conf?'<span class="cb '+cc(conf)+'">'+esc(conf)+'</span>':'')+
        '<span style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;background:rgba(201,168,76,.08);color:var(--gold);border:1px solid rgba(201,168,76,.12)">L'+dl+'</span>'+
        (holdStr?'<span style="font-size:11px;color:var(--text-3)">'+holdStr+'</span>':'')+'</div>'+
        '<div style="display:flex;gap:8px">'+
        '<button class="abtn '+(pnlVal>=0?'abtn-profit':'abtn-loss')+'" onclick="doAction(\\'/api/close_position\\','+
        '{security_id:\\''+sid+'\\',reason:\\''+reason+'\\'},null,this)">'+
        (pnlVal>=0?'Book Profit':'Close Loss')+'</button></div></div>'+
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:8px 12px;background:rgba(168,85,247,.05);border-radius:8px;margin:4px 0">'+
        '<div style="display:flex;flex-direction:column;gap:4px;padding:8px;border-radius:6px;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.15)">'+
          '<span style="font-size:10px;font-weight:700;color:#ef4444;letter-spacing:.5px">SELL LEG</span>'+
          '<span style="font-weight:700;font-size:14px;color:var(--text-1)">'+esc(sellSym)+'</span>'+
          '<span style="font-size:12px;color:var(--text-2)">'+R+fmt(sellEntry)+' <span style="color:var(--text-3)">&rarr;</span> <b style="color:var(--text-1)">'+R+fmt(sellCurr)+'</b></span>'+
        '</div>'+
        '<div style="display:flex;flex-direction:column;gap:4px;padding:8px;border-radius:6px;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.15)">'+
          '<span style="font-size:10px;font-weight:700;color:#22c55e;letter-spacing:.5px">BUY HEDGE</span>'+
          '<span style="font-weight:700;font-size:14px;color:var(--text-1)">'+esc(hedgeSym)+'</span>'+
          '<span style="font-size:12px;color:var(--text-2)">'+R+fmt(hep)+' <span style="color:var(--text-3)">&rarr;</span> <b style="color:var(--text-1)">'+R+fmt(hcp)+'</b></span>'+
        '</div></div>'+
        '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:8px 0">'+
          '<div style="text-align:center;padding:6px;border-radius:6px;background:rgba(34,197,94,.06)">'+
            '<div style="font-size:10px;color:var(--text-3)">Net Credit</div>'+
            '<div style="font-size:16px;font-weight:700;color:var(--profit)">'+R+fmt(nc)+'</div></div>'+
          '<div style="text-align:center;padding:6px;border-radius:6px;background:rgba(239,68,68,.06)">'+
            '<div style="font-size:10px;color:var(--text-3)">Max Loss</div>'+
            '<div style="font-size:16px;font-weight:700;color:var(--loss)">'+R+fmt(ml)+'<span style="font-size:10px">/unit</span></div></div>'+
          '<div style="text-align:center;padding:6px;border-radius:6px;background:rgba(59,130,246,.06)">'+
            '<div style="font-size:10px;color:var(--text-3)">Width</div>'+
            '<div style="font-size:16px;font-weight:700;color:var(--cyan)">'+sw+'</div></div>'+
          '<div style="text-align:center;padding:6px;border-radius:6px;background:'+(sprPnl>=0?'rgba(34,197,94,.06)':'rgba(239,68,68,.06)')+'">'+
            '<div style="font-size:10px;color:var(--text-3)">Spread P&L</div>'+
            '<div style="font-size:16px;font-weight:700;color:'+pc(sprPnl)+'">'+fpnl(sprPnl)+'</div></div>'+
        '</div>'+
        '<div class="pc-p" style="justify-content:center;gap:12px">'+
        '<span style="font-size:12px;color:var(--text-3)">Net Premium:</span>'+
        '<span style="color:var(--text-2);font-size:14px">'+R+fmt(nc)+'</span>'+
        '<span style="color:var(--text-3)">&rarr;</span>'+
        '<span class="mono" style="font-size:20px;font-weight:700;color:'+pc(pnlVal)+'">'+R+fmt(netCurr>0?netCurr:c)+'</span>'+
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
        '</div>';
    } else {
      h+='<div class="pc"><div class="pc-h"><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'+
        '<span class="db '+(isShort?'db-s':'db-l')+'">'+(isShort?'SELL':'BUY')+' '+(ot||'')+'</span>'+
        '<span style="font-weight:700;font-size:16px">'+esc(parseSym(p.symbol||'').label)+'</span>'+
        (p.expiry?'<span style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;background:rgba(59,130,246,.1);color:#60a5fa;border:1px solid rgba(59,130,246,.15)">'+esc(fmtExpiry(p.expiry))+'</span>':'')+
        (conf?'<span class="cb '+cc(conf)+'">'+esc(conf)+'</span>':'')+
        '<span style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;background:rgba(201,168,76,.08);color:var(--gold);border:1px solid rgba(201,168,76,.12)">L'+dl+'</span>'+
        (holdStr?'<span style="font-size:11px;color:var(--text-3)">'+holdStr+'</span>':'')+'</div>'+
        '<div style="display:flex;gap:8px">'+
        '<button class="abtn '+(pnlVal>=0?'abtn-profit':'abtn-loss')+'" onclick="doAction(\\'/api/close_position\\','+
        '{security_id:\\''+sid+'\\',reason:\\''+reason+'\\'},null,this)">'+
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
        '</div>';
    }
    h+=(p.delta!=null?'<div style="display:flex;gap:16px;padding:6px 12px;background:rgba(59,130,246,.06);border-radius:6px;font-size:12px;margin-top:4px">'+
        '<span style="color:var(--text-2)">Δ <b style="color:var(--cyan)">'+(p.delta||0).toFixed(3)+'</b></span>'+
        '<span style="color:var(--text-2)">Γ <b>'+(p.gamma||0).toFixed(5)+'</b></span>'+
        '<span style="color:var(--text-2)">Θ <b style="color:'+(p.theta<0?'var(--loss)':'var(--profit)')+'">'+((p.theta||0)>=0?'+':'')+(p.theta||0).toFixed(2)+'</b>/day</span>'+
        '<span style="color:var(--text-2)">V <b>'+(p.vega||0).toFixed(2)+'</b></span>'+
        '<span style="color:var(--text-2)">DTE <b style="color:var(--warn)">'+(p.dte||0).toFixed(1)+'d</b></span>'+
      '</div>':'')+
      '</div>';
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
    h+='<div class="ttb-wrap"><table class="ttb"><thead><tr>'+
      '<th>Instrument</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>%</th><th>Reason</th><th>Hold</th><th>Notes</th></tr></thead><tbody>';
    [...closed].reverse().forEach((t,i)=>{
      const e=t.entry||0,x=t.exit||0;
      const tShort=t.direction!=='LONG';
      let pct=e>0?((x-e)/e*100):0;
      if(tShort)pct=-pct;
      const key=(t.symbol||'')+'_'+(t.exit_time||i);
      const note=notes[key]||'';
      let holdStr='';
      if(t.entry_time&&t.exit_time){try{const d=new Date(t.exit_time)-new Date(t.entry_time);const m=Math.floor(d/60000);holdStr=m>=60?Math.floor(m/60)+'h '+m%60+'m':m+'m';}catch(e){}}
      const ps=parseSym(t.symbol||'');
      const side=tShort?'SELL':'BUY';
      h+='<tr><td style="font-weight:600;white-space:nowrap">'+esc(ps.label)+'</td>'+
        '<td><span class="db '+(tShort?'db-s':'db-l')+'">'+esc(side)+'</span></td>'+
        '<td class="mono" style="font-size:12px">'+(t.quantity||'')+'</td>'+
        '<td class="mono">'+R+fmt(e)+'</td><td class="mono">'+R+fmt(x)+'</td>'+
        '<td class="mono" style="color:'+pc(t.pnl||0)+';font-weight:600">'+fpnl(t.pnl||0)+'</td>'+
        '<td style="color:'+pc(t.pnl||0)+';font-size:12px">'+(pct>=0?'+':'')+pct.toFixed(1)+'%</td>'+
        '<td style="color:var(--text-3);font-size:12px">'+esc(t.reason||'')+'</td>'+
        '<td style="color:var(--text-3);font-size:11px">'+(holdStr||'')+'</td>'+
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
      const ps=parseSym(t.symbol||'');
      h+='<div class="lr"><span class="lr-r">'+medals[i]+(i+1)+'</span>'+
        '<span class="lr-s">'+(t.direction!=='LONG'?'SELL':'BUY')+' '+esc(ps.label)+'</span>'+
        '<span class="mono" style="color:'+pc(t.pnl||0)+';font-weight:700">'+fpnl(t.pnl||0)+'</span>'+
        '<span class="lr-re">'+esc(t.reason||'')+'</span></div>';
    });
    h+='</div><div class="cd"><div class="ct">&#x1F480; Worst Trades</div>';
    worst.forEach((t,i)=>{
      const ps=parseSym(t.symbol||'');
      h+='<div class="lr"><span class="lr-r">'+(i===0?'&#x1F480;':'')+(i+1)+'</span>'+
        '<span class="lr-s">'+(t.direction!=='LONG'?'SELL':'BUY')+' '+esc(ps.label)+'</span>'+
        '<span class="mono" style="color:'+pc(t.pnl||0)+';font-weight:700">'+fpnl(t.pnl||0)+'</span>'+
        '<span class="lr-re">'+esc(t.reason||'')+'</span></div>';
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
      '<span style="font-weight:700;font-size:14px;'+scl+'">'+esc(side)+'</span>'+
      '<span style="font-weight:700;font-size:16px">'+esc(sg.symbol||'')+'</span></div>'+
      '<div style="display:flex;align-items:center;gap:10px">'+
      '<span class="cb '+cc(sg.confidence||'')+'">'+esc(sg.confidence||'')+'</span>'+
      '<span class="mono" style="font-size:22px;font-weight:800;color:'+scc(sc)+'">'+sc.toFixed(0)+'</span></div></div>'+
      '<div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text-2);margin-bottom:8px">'+
      '<span>ADX '+(sg.adx||0).toFixed(1)+'</span>'+
      '<span>RSI '+(sg.rsi||0).toFixed(1)+'</span>'+
      '<span>VWAP '+esc(sg.vwap_position||'')+'</span>'+
      '<span>ST '+esc(sg.supertrend_dir||'')+'</span>'+
      '<span>Vol '+(sg.volume_ratio||0).toFixed(2)+'x</span>'+
      '<span>Regime '+esc(sg.volatility_regime||'')+'</span></div>';
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
  const ist=new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Kolkata'}));
  if(ms==='CLOSED'||ms==='PRE-MARKET'){
    if(ms==='PRE-MARKET'){
      const open=new Date(ist);open.setHours(9,15,0,0);
      const diff=open-ist;
      if(diff>0){
        const m=Math.floor(diff/60000),s=Math.floor((diff%60000)/1000);
        tm.textContent=m+'m '+s+'s to open';
        el.classList.remove('urgent');
      }else{tm.textContent='Opening...';}
    }else{tm.textContent='Closed';el.classList.remove('urgent');}
    return;
  }
  const close=new Date(ist);close.setHours(15,15,0,0);
  const diff=close-ist;
  if(diff<=0){tm.textContent='Session ended';el.classList.remove('urgent');return;}
  const h=Math.floor(diff/3600000),m=Math.floor((diff%3600000)/60000),s=Math.floor((diff%60000)/1000);
  tm.textContent=(h>0?h+'h ':'')+(m>0?m+'m ':'')+(s+'s');
  el.classList.toggle('urgent',diff<900000);
}

/* ── TOKEN EXPIRY ── */
function updateTokenBanner(){
  const hrs=S.token_hours_remaining;
  const ban=document.getElementById('token-ban');
  if(hrs==null||hrs===-1){ban.className='token-ban';ban.style.display='none';return;}
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
    '<div class="modal-box"><div class="modal-title">'+esc(title)+'</div>'+body+
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
      const r=await fetch('/api/modify_sl',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':_csrf},body:JSON.stringify({security_id:sid,new_sl:v})});
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
      const r=await fetch('/api/modify_tp',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':_csrf},body:JSON.stringify({security_id:sid,new_tp:v})});
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
    await fetch('/api/add_note',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':_csrf},body:JSON.stringify({trade_key:key,note:note})});
  },800);
}

/* ── EXPORT CSV ── */
function exportCSV(){window.open('/api/export_csv','_blank');}

/* ── OPTION CHAIN ── */
let _chainUl='NIFTY',_chainData=null,_chainLoading=false,_chainScrollOnce=false;
async function loadChain(ul){
  _chainUl=ul;_chainLoading=true;_chainScrollOnce=true;rChain();
  try{
    const r=await fetch('/api/option_chain/'+ul);
    _chainData=await r.json();
  }catch(e){_chainData={error:e.message};}
  _chainLoading=false;rChain();
}
function rChain(){
  const el=document.getElementById('tp-chain');
  let h='<div class="cd"><div class="ct" style="display:flex;align-items:center;gap:8px"><span style="color:var(--gold)">&#x2636;</span> Option Chain</div>';
  h+='<div class="oc-controls">';
  ['NIFTY','BANKNIFTY','SENSEX'].forEach(u=>{
    h+='<button class="oc-btn'+(u===_chainUl?' on':'')+'" onclick="loadChain(\\''+u+'\\')">'+u+'</button>';
  });
  if(_chainData&&!_chainData.error){
    const exps=_chainData.expiries||[];
    if(exps.length>1){
      h+='<select class="oc-exp" onchange="loadChain(_chainUl+\\'?expiry=\\'+this.value)">';
      exps.forEach(e=>{h+='<option'+(e===_chainData.expiry?' selected':'')+'>'+esc(e)+'</option>';});
      h+='</select>';
    }
    h+='<span class="oc-spot">SPOT: '+String.fromCharCode(8377)+fmt(_chainData.spot||0)+'</span>';
  }
  h+='</div>';
  if(_chainLoading){h+='<div class="loading">Loading chain...</div></div>';el.innerHTML=h;return;}
  if(!_chainData||_chainData.error){
    h+='<div class="emp" style="padding:30px 0;text-align:center;color:var(--text-3)">'+(_chainData?esc(_chainData.error):'Click an index above')+'</div></div>';
    el.innerHTML=h;return;
  }
  const rows=_chainData.rows||[];
  const spot=_chainData.spot||0;
  const atm=_chainData.atm||0;
  const maxOI=Math.max(...rows.map(r=>Math.max(r.ce_oi||0,r.pe_oi||0)),1);
  h+='<div style="margin:8px 0;display:flex;gap:12px;font-size:12px;color:var(--text-3)">'+
    '<span>Expiry: <b style="color:var(--text-1)">'+esc(fmtExpiry(_chainData.expiry))+'</b></span>'+
    '<span>ATM: <b style="color:var(--gold)">'+fmt(atm,0)+'</b></span>'+
    '<span>Strikes: <b style="color:var(--text-1)">'+rows.length+'</b></span></div>';
  h+='<div class="oc-wrap"><table class="oc-tbl"><thead><tr>';
  h+='<th style="color:var(--profit)">CE OI</th><th>Vol</th><th>IV%</th><th>Bid</th><th>Ask</th><th style="color:var(--profit)">CE LTP</th>';
  h+='<th style="color:var(--gold)">STRIKE</th>';
  h+='<th style="color:var(--loss)">PE LTP</th><th>Bid</th><th>Ask</th><th>IV%</th><th>Vol</th><th style="color:var(--loss)">PE OI</th>';
  h+='</tr></thead><tbody>';
  rows.forEach(r=>{
    const isAtm=atm>0&&r.strike===atm;
    const ceItm=r.strike<spot,peItm=r.strike>spot;
    const ac=isAtm?' atm':'';
    h+='<tr>';
    h+='<td class="ce-side'+(ceItm?' itm':'')+ac+'" style="text-align:left">'+(r.ce_oi?fmt(r.ce_oi,0):'—');
    h+='<span class="oi-bar oi-ce" style="width:'+Math.round(r.ce_oi/maxOI*100)+'%"></span></td>';
    h+='<td class="ce-side'+(ceItm?' itm':'')+ac+'">'+(r.ce_volume?fmt(r.ce_volume,0):'—')+'</td>';
    h+='<td class="ce-side'+(ceItm?' itm':'')+ac+'" style="color:var(--cyan)">'+(r.ce_iv?r.ce_iv.toFixed(1):'—')+'</td>';
    h+='<td class="ce-side'+(ceItm?' itm':'')+ac+'">'+(r.ce_bid||'—')+'</td>';
    h+='<td class="ce-side'+(ceItm?' itm':'')+ac+'">'+(r.ce_ask||'—')+'</td>';
    h+='<td class="ce-side'+(ceItm?' itm':'')+ac+'" style="font-weight:700;color:'+(r.ce_change>=0?'var(--profit)':'var(--loss)')+'">'+(r.ce_ltp||'—')+'</td>';
    h+='<td class="strike-col'+ac+'">'+fmt(r.strike,0)+'</td>';
    h+='<td class="pe-side'+(peItm?' itm':'')+ac+'" style="font-weight:700;color:'+(r.pe_change>=0?'var(--profit)':'var(--loss)')+'">'+(r.pe_ltp||'—')+'</td>';
    h+='<td class="pe-side'+(peItm?' itm':'')+ac+'">'+(r.pe_bid||'—')+'</td>';
    h+='<td class="pe-side'+(peItm?' itm':'')+ac+'">'+(r.pe_ask||'—')+'</td>';
    h+='<td class="pe-side'+(peItm?' itm':'')+ac+'" style="color:var(--cyan)">'+(r.pe_iv?r.pe_iv.toFixed(1):'—')+'</td>';
    h+='<td class="pe-side'+(peItm?' itm':'')+ac+'">'+(r.pe_volume?fmt(r.pe_volume,0):'—')+'</td>';
    h+='<td class="pe-side'+(peItm?' itm':'')+ac+'" style="text-align:left">'+(r.pe_oi?fmt(r.pe_oi,0):'—');
    h+='<span class="oi-bar oi-pe" style="width:'+Math.round(r.pe_oi/maxOI*100)+'%"></span></td>';
    h+='</tr>';
  });
  h+='</tbody></table></div></div>';
  el.innerHTML=h;
  if(_chainScrollOnce){
    _chainScrollOnce=false;
    const atmEl=el.querySelector('.atm');
    if(atmEl)atmEl.scrollIntoView({block:'center',behavior:'smooth'});
  }
}

/* ── ANALYTICS ── */
let _anaData=null,_anaLoaded=false;
async function loadAnalytics(){
  if(_anaLoaded)return;
  try{
    const r=await fetch('/api/trade_analytics');
    _anaData=await r.json();
    _anaLoaded=true;
  }catch(e){}
  rAnalytics();
}
function rAnalytics(){
  const el=document.getElementById('tp-analytics');
  if(tab!=='analytics')return;
  if(!_anaData){loadAnalytics();return;}
  let h='';

  /* P&L by exit reason */
  h+='<div class="cd ana-section"><div class="ct" style="display:flex;align-items:center;gap:8px"><span style="color:var(--gold)">&#x1F4CA;</span> P&L by Exit Reason</div>';
  const br=_anaData.by_reason||{};
  const reasons=Object.entries(br).sort((a,b)=>b[1].count-a[1].count);
  if(reasons.length){
    reasons.forEach(([r,d])=>{
      const c=d.pnl>=0?'var(--profit)':'var(--loss)';
      h+='<div class="reason-row"><span class="reason-lbl">'+r+'</span>';
      h+='<span class="reason-ct">'+d.count+' trades</span>';
      h+='<span class="reason-pnl" style="color:'+c+'">'+fpnl(d.pnl)+'</span></div>';
    });
  } else{h+='<div class="emp" style="padding:20px;text-align:center;color:var(--text-3)">No trades yet</div>';}
  h+='</div>';

  /* P&L by symbol */
  h+='<div class="cd ana-section"><div class="ct" style="display:flex;align-items:center;gap:8px"><span style="color:var(--gold)">&#x1F3AF;</span> P&L by Index</div>';
  const bs=_anaData.by_symbol||{};
  const syms=Object.entries(bs).sort((a,b)=>b[1].pnl-a[1].pnl);
  if(syms.length){
    syms.forEach(([s,d])=>{
      const c=d.pnl>=0?'var(--profit)':'var(--loss)';
      const wr=d.count>0?Math.round(d.wins/d.count*100):0;
      const wrC=wr>=60?'var(--profit)':wr>=40?'var(--warn)':'var(--loss)';
      h+='<div class="sym-row"><span class="sym-name">'+s+'</span>';
      h+='<span class="reason-ct">'+d.count+' trades</span>';
      h+='<span class="sym-wr" style="background:rgba('+(wr>=50?'0,230,138':'255,77,94')+',.1);color:'+wrC+'">'+wr+'% WR</span>';
      h+='<span class="reason-pnl" style="color:'+c+'">'+fpnl(d.pnl)+'</span></div>';
    });
  }
  h+='</div>';

  /* Hourly heatmap */
  h+='<div class="cd ana-section"><div class="ct" style="display:flex;align-items:center;gap:8px"><span style="color:var(--gold)">&#x1F552;</span> Hourly Performance</div>';
  const hr=_anaData.hourly||{};
  const hrs=Object.entries(hr).sort((a,b)=>parseInt(a[0])-parseInt(b[0]));
  if(hrs.length){
    h+='<div class="heatmap">';
    for(let i=9;i<=15;i++){
      const d=hr[String(i)]||{count:0,pnl:0};
      const bg=d.pnl>0?'rgba(0,230,138,'+(0.1+Math.min(Math.abs(d.pnl)/1000,0.5))+')':
               d.pnl<0?'rgba(255,77,94,'+(0.1+Math.min(Math.abs(d.pnl)/1000,0.5))+')':'var(--bg-3)';
      const tc=d.pnl>=0?'var(--profit)':'var(--loss)';
      h+='<div class="hm-cell" style="background:'+bg+'"><div style="font-size:9px;color:var(--text-3);margin-bottom:4px">'+i+':00</div>';
      h+='<div style="color:'+(d.count?tc:'var(--text-3)')+'">'+(d.count?fpnl(d.pnl):'—')+'</div>';
      h+='<div style="font-size:9px;color:var(--text-3);margin-top:2px">'+(d.count||0)+' trades</div></div>';
    }
    h+='</div>';
  } else{h+='<div class="emp" style="padding:20px;text-align:center;color:var(--text-3)">No hourly data yet</div>';}
  h+='</div>';

  /* Equity curve from analytics */
  const ps=_anaData.pnl_series||[];
  if(ps.length>1){
    h+='<div class="cd ana-section"><div class="ct" style="display:flex;align-items:center;gap:8px"><span style="color:var(--gold)">&#x1F4C8;</span> Cumulative P&L Curve</div>';
    const mn=Math.min(...ps,0),mx=Math.max(...ps,1);
    const W=680,H=200,pad=10;
    h+='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;display:block">';
    h+='<defs><linearGradient id="aFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+(ps[ps.length-1]>=0?'rgba(0,230,138,.3)':'rgba(255,77,94,.3)')+'"/><stop offset="1" stop-color="transparent"/></linearGradient></defs>';
    const rng=mx-mn||1;
    const pts=ps.map((v,i)=>{
      const x=pad+i/(ps.length-1)*(W-2*pad);
      const y=H-pad-(v-mn)/rng*(H-2*pad);
      return x.toFixed(1)+','+y.toFixed(1);
    });
    const zeroY=H-pad-(0-mn)/rng*(H-2*pad);
    h+='<line x1="'+pad+'" y1="'+zeroY.toFixed(1)+'" x2="'+(W-pad)+'" y2="'+zeroY.toFixed(1)+'" stroke="var(--border)" stroke-dasharray="4,4"/>';
    h+='<polygon points="'+pad+','+zeroY.toFixed(1)+' '+pts.join(' ')+' '+(W-pad)+','+zeroY.toFixed(1)+'" fill="url(#aFill)"/>';
    h+='<polyline points="'+pts.join(' ')+'" fill="none" stroke="'+(ps[ps.length-1]>=0?'var(--profit)':'var(--loss)')+'" stroke-width="2"/>';
    h+='</svg></div>';
  }

  el.innerHTML=h;
}

/* ── ARCHITECTURE TAB ── */
function rArch(){
  const el=document.getElementById('tp-arch');
  if(tab!=='arch')return;
  const ml=S.ml||{};const sig=ml.signal_classifier||{};const reg=ml.regime_detector||{};
  const buf=ml.trade_buffer||{};
  let h='<div class="g3" style="margin-bottom:16px">';
  h+='<div class="arch"><div class="arch-i ic-g">&Omega;</div><h3>Omega Strategy</h3><p>Confluence scoring: SuperTrend, VWAP, ADX, EMA, RSI, ORB, Divergence. Dynamic SL/TP with 4-level escalation.</p><div class="tech"><span>Python</span><span>FastAPI</span><span>ATR SL</span></div></div>';
  h+='<div class="arch"><div class="arch-i ic-p">&#9881;</div><h3>ML Engine</h3><p>50-feature neural network (50&rarr;32&rarr;16&rarr;2). Auto-retrains on trades. K-Means regime detection for market adaptation.</p><div class="tech"><span>NumPy</span><span>Neural Net</span><span>K-Means</span></div></div>';
  h+='<div class="arch"><div class="arch-i ic-c">&#9878;</div><h3>Risk Engine</h3><p>7-gate safety layer. Loss limits, position caps, ATR trailing stops, Kelly sizing, cooldown periods.</p><div class="tech"><span>&#8377;1,500/trade</span><span>3% daily cap</span></div></div>';
  h+='<div class="arch"><div class="arch-i ic-gr">&#9733;</div><h3>Trade Memory</h3><p>4-trade pattern memory. Auto-blocks poor symbols. Adjusts sizing by instrument expectancy.</p><div class="tech"><span>SQLite</span><span>JSON</span><span>4 trades</span></div></div>';
  h+='<div class="arch"><div class="arch-i ic-b">&#9889;</div><h3>Live Feed</h3><p>Dhan WebSocket real-time prices. REST+Yahoo fallback. ATM option chain resolution.</p><div class="tech"><span>WebSocket</span><span>REST</span><span>Dhan</span></div></div>';
  h+='<div class="arch"><div class="arch-i ic-w">&#9888;</div><h3>Guardrails</h3><p>Chop lockout, VIX-adjusted SL, session sizing caps, loss cooldowns. Prevents overtrading.</p><div class="tech"><span>VIX</span><span>Choppiness</span></div></div>';
  h+='</div>';
  h+='<div class="cd"><div class="ct">Data Pipeline</div><div class="g4">';
  const sigs=S.signals||[];const closed=S.closed_trades||[];
  h+='<div style="text-align:center;padding:14px;background:var(--bg3);border-radius:8px"><div class="mono" style="font-size:20px;font-weight:700;color:var(--cyan)">Signals</div><div style="font-size:9px;color:var(--t3);margin-top:3px">'+sigs.length+' logged &rarr; signal_log.json</div></div>';
  h+='<div style="text-align:center;padding:14px;background:var(--bg3);border-radius:8px"><div class="mono" style="font-size:20px;font-weight:700;color:var(--profit)">Trades</div><div style="font-size:9px;color:var(--t3);margin-top:3px">'+closed.length+' fields &rarr; SQLite</div></div>';
  h+='<div style="text-align:center;padding:14px;background:var(--bg3);border-radius:8px"><div class="mono" style="font-size:20px;font-weight:700;color:var(--purple)">ML Buffer</div><div style="font-size:9px;color:var(--t3);margin-top:3px">'+(buf.size||0)+' entries</div></div>';
  h+='<div style="text-align:center;padding:14px;background:var(--bg3);border-radius:8px"><div class="mono" style="font-size:20px;font-weight:700;color:var(--gold)">50 Features</div><div style="font-size:9px;color:var(--t3);margin-top:3px">Extracted per signal</div></div>';
  h+='</div></div>';
  el.innerHTML=h;
}

/* ── ML ENGINE TAB ── */
function rML(){
  const el=document.getElementById('tp-ml');
  if(tab!=='ml')return;
  const ml=S.ml||{};const sig=ml.signal_classifier||{};const reg=ml.regime_detector||{};
  const buf=ml.trade_buffer||{};const layers=(sig.layers||[50,64,32,16,2]);
  const names=['Input','Hidden 1','Hidden 2','Hidden 3','Output'];
  const colors=['var(--cyan)','var(--gold)','var(--purple)','var(--warn)','var(--profit)'];
  let h='<div class="cd" style="margin-bottom:16px"><div class="ct">Neural Network</div><div class="ml-a">';
  layers.forEach((n,i)=>{
    if(i>0)h+='<div class="ml-ar">&rarr;</div>';
    h+='<div class="ml-n"><div class="nv" style="color:'+colors[i%colors.length]+'">'+n+'</div><div class="nl">'+(names[i]||'Layer '+i)+'</div></div>';
  });
  h+='</div>';
  h+='<div style="text-align:center;font-size:11px;color:var(--t3);margin-top:6px">Sigmoid &bull; Binary cross-entropy &bull; Auto-retrain after '+(buf.auto_retrain_at||20)+' new trades</div></div>';
  const acc=((buf.wins||0)/Math.max(1,(buf.wins||0)+(buf.losses||0))*100).toFixed(1);
  h+='<div class="sg" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px">';
  h+='<div class="sc"><div class="sv mono" style="color:var(--profit)">'+acc+'%</div><div class="sl">Accuracy</div></div>';
  h+='<div class="sc"><div class="sv mono" style="color:var(--cyan)">'+(buf.size||0).toLocaleString()+'</div><div class="sl">Buffer</div></div>';
  h+='<div class="sc"><div class="sv mono" style="color:var(--gold)">'+(ml.confidence_threshold||0.55)+'</div><div class="sl">Threshold</div></div>';
  h+='<div class="sc"><div class="sv mono" style="color:var(--purple)">'+(sig.iterations_trained||0)+'</div><div class="sl">Iterations</div></div>';
  h+='</div>';
  h+='<div class="g2">';
  h+='<div class="cd"><div class="ct">50 Features</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:5px;font-size:10px">';
  h+='<div style="padding:7px;background:var(--bg3);border-radius:5px"><b style="color:var(--cyan)">Price</b><br><span style="color:var(--t3)">close_norm, high_low_range, body_ratio, upper_wick, lower_wick</span></div>';
  h+='<div style="padding:7px;background:var(--bg3);border-radius:5px"><b style="color:var(--gold)">Trend</b><br><span style="color:var(--t3)">ema_fast_slope, ema_slow_slope, supertrend, ema_cross, adx</span></div>';
  h+='<div style="padding:7px;background:var(--bg3);border-radius:5px"><b style="color:var(--purple)">Momentum</b><br><span style="color:var(--t3)">rsi, rsi_slope, macd_hist, momentum_5, price_acceleration</span></div>';
  h+='<div style="padding:7px;background:var(--bg3);border-radius:5px"><b style="color:var(--profit)">Volume</b><br><span style="color:var(--t3)">volume_ratio, vwap_dist, obv_slope, consec_green, consec_red</span></div>';
  h+='<div style="padding:7px;background:var(--bg3);border-radius:5px"><b style="color:var(--warn)">Volatility</b><br><span style="color:var(--t3)">bb_width, bb_position, atr, range_expansion, choppiness</span></div>';
  h+='<div style="padding:7px;background:var(--bg3);border-radius:5px"><b style="color:var(--loss)">Context</b><br><span style="color:var(--t3)">hour_sin, hour_cos, day_sin, day_cos, session_progress</span></div>';
  h+='</div></div>';
  const curReg=(reg.current_regime||'UNKNOWN');
  h+='<div class="cd"><div class="ct">Regime Detection (K-Means)</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">';
  const regimes=[{n:'TRENDING',c:'var(--profit)',d:'ADX > 25'},{n:'RANGING',c:'var(--cyan)',d:'ADX < 20'},{n:'VOLATILE',c:'var(--warn)',d:'High ATR'},{n:'CHOPPY',c:'var(--loss)',d:'CI > 61.8'}];
  regimes.forEach(r=>{
    const act=curReg.toUpperCase()===r.n;
    h+='<div style="padding:14px;background:rgba('+(act?'201,168,76,.08':'255,255,255,.02')+');border:1px solid '+(act?'var(--gold)':'var(--bdr)')+';border-radius:8px;text-align:center'+(act?';box-shadow:0 0 12px rgba(201,168,76,.15)':'')+'">';
    h+='<div style="font-size:16px;font-weight:800;color:'+r.c+'">'+r.n+'</div>';
    h+='<div style="font-size:9px;color:var(--t3);margin-top:3px">'+r.d+'</div></div>';
  });
  h+='</div>';
  h+='<div style="text-align:center;font-size:11px;color:var(--t2);margin-top:12px">Current: <span style="color:var(--warn);font-weight:700">'+curReg+'</span> &bull; '+(reg.n_clusters||4)+' clusters &bull; Auto-adapts parameters</div>';
  h+='</div></div>';
  el.innerHTML=h;
}

/* ── RISK TAB ── */
function rRisk(){
  const el=document.getElementById('tp-risk');
  if(tab!=='risk')return;
  const s=S.stats||{};const w=S.wallet||{};
  const dailyLoss=Math.min(0,s.realised_pnl||0);
  const cap=w.balance||100000;
  const lossPct=Math.abs(dailyLoss/cap*100).toFixed(1);
  const lossBarW=Math.min(100,Math.abs(dailyLoss/cap*100/3*100));
  const consLoss=s.consecutive_losses||0;
  const trades=s.trade_count||0;
  const maxTrades=8;
  const riskLevel=lossPct>2?'HIGH':(lossPct>1?'ELEVATED':'NORMAL');
  const riskCol=riskLevel==='HIGH'?'var(--loss)':(riskLevel==='ELEVATED'?'var(--warn)':'var(--profit)');

  let h='<div class="cd rp" style="margin-bottom:16px"><div class="ct">Risk Monitor</div>';
  h+='<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap">';
  h+='<span class="rzb" style="background:rgba('+(riskLevel==='NORMAL'?'0,230,138':'255,77,94')+', .1);color:'+riskCol+';border:1px solid '+riskCol+'">'+riskLevel+'</span>';
  h+='<span class="mono" style="color:var(--t3);font-weight:700;font-size:15px">'+fpnl(dailyLoss)+'</span>';
  h+='<span style="color:var(--t3);font-size:11px">('+lossPct+'%)</span></div>';
  h+='<div class="rg">';
  h+='<div><div class="rg-l">Daily Loss Used</div><div class="pgb"><div class="pgf" style="width:'+lossBarW+'%;background:'+riskCol+'"></div></div><div class="mono" style="font-size:11px;font-weight:600;margin-top:2px">'+lossPct+'%</div></div>';
  h+='<div><div class="rg-l">Trades Today</div><div class="rg-v mono">'+trades+' <span style="color:var(--t3);font-size:11px">/ '+maxTrades+'</span></div></div>';
  h+='<div><div class="rg-l">Consecutive Losses</div><div class="rg-v mono" style="color:'+(consLoss>=3?'var(--loss)':'var(--t3)')+'">'+consLoss+'</div></div>';
  h+='<div><div class="rg-l">Recovery Needed</div><div class="rg-v mono" style="color:var(--t3)">'+(lossPct>0?(lossPct*100/(100-lossPct)).toFixed(1):0)+'%</div></div>';
  h+='<div><div class="rg-l">Drawdown Protection</div><div class="rg-v mono" style="color:var(--profit)">'+(100-parseFloat(lossPct)).toFixed(0)+'%</div></div>';
  h+='<div><div class="rg-l">Max Risk/Trade</div><div class="rg-v mono">'+String.fromCharCode(8377)+'1,500</div></div>';
  h+='</div></div>';

  h+='<div class="cd" style="margin-bottom:16px"><div class="ct">7-Gate Safety Layer</div>';
  h+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px">';
  const gates=[
    {n:'1. Capital Preservation',d:'Max 8% capital risk/trade. 3% daily loss cap.',c:'var(--profit)'},
    {n:'2. Position Sizing',d:'Kelly criterion. Drawdown halves size to 50%.',c:'var(--cyan)'},
    {n:'3. ADX Filter',d:'Min ADX 10 for directional trades.',c:'var(--gold)'},
    {n:'4. VIX-Adjusted Stops',d:'SL widens when VIX elevated.',c:'var(--purple)'},
    {n:'5. Loss Cooldown',d:'5-min cooldown after 3 consecutive losses.',c:'var(--warn)'},
    {n:'6. Chop Lockout',d:'CI > 61.8 blocks directional entries.',c:'var(--loss)'},
    {n:'7. Session Guards',d:'Early caps, no trades in last 15 min.',c:'var(--blue)'}
  ];
  gates.forEach(g=>{
    h+='<div style="padding:12px;background:var(--bg3);border-radius:8px;border-left:3px solid '+g.c+'"><div style="font-size:11px;font-weight:700;margin-bottom:3px">'+g.n+'</div><div style="font-size:10px;color:var(--t3)">'+g.d+'</div></div>';
  });
  h+='</div></div>';

  h+='<div class="cd"><div class="ct">ATR Trailing Stops</div><div class="g4">';
  const phases=[
    {t:'0-10 MIN',v:'4.0&times;',c:'var(--profit)',bg:'0,230,138'},
    {t:'10-30 MIN',v:'3.5&times;',c:'var(--cyan)',bg:'78,205,196'},
    {t:'30-60 MIN',v:'3.0&times;',c:'var(--gold)',bg:'201,168,76'},
    {t:'60+ MIN',v:'2.5&times;',c:'var(--warn)',bg:'240,185,11'}
  ];
  phases.forEach(p=>{
    h+='<div style="text-align:center;padding:14px;background:rgba('+p.bg+',.03);border:1px solid rgba('+p.bg+',.08);border-radius:8px"><div style="font-size:9px;color:var(--t3)">'+p.t+'</div><div class="mono" style="font-size:20px;font-weight:700;color:'+p.c+'">'+p.v+'</div><div style="font-size:9px;color:var(--t3)">ATR width</div></div>';
  });
  h+='</div>';
  h+='<div style="text-align:center;margin-top:10px;font-size:11px;color:var(--t3)">Min distance: 8% from peak &bull; Breakeven at 3.0&times; ATR &bull; Buffer: 0.5&times; ATR</div></div>';
  el.innerHTML=h;
}

/* ── GOAL TAB ── */
function rGoal(){
  const el=document.getElementById('tp-goal');
  if(tab!=='goal')return;
  const w=S.wallet||{};const bal=w.balance||100000;
  const stages=[
    {l:'Start',v:String.fromCharCode(8377)+'1L',t:100000},
    {l:'Double 1',v:String.fromCharCode(8377)+'2L',t:200000},
    {l:'Double 2',v:String.fromCharCode(8377)+'4L',t:400000},
    {l:'Double 3',v:String.fromCharCode(8377)+'8L',t:800000},
    {l:'Target',v:'Go Live',t:800001}
  ];
  let currentIdx=0;
  stages.forEach((s,i)=>{if(bal>=s.t)currentIdx=i;});

  let h='<div class="cd" style="margin-bottom:16px"><div class="ct">Compound Doubling Path</div><div class="goal">';
  stages.forEach((s,i)=>{
    if(i>0)h+='<div class="goal-a">&rarr;</div>';
    const isCur=i===currentIdx;
    const done=i<currentIdx;
    h+='<div class="goal-s'+(isCur?' now':'')+'" style="'+(done?'border-color:var(--profit);opacity:.7':'')+(i===stages.length-1?';border-color:var(--profit)':'')+'">';
    h+='<div class="goal-l">'+s.l+'</div>';
    h+='<div class="goal-v mono" style="color:'+(isCur?'var(--gold)':(done?'var(--profit)':'var(--t2)'))+'">'+s.v+'</div></div>';
  });
  h+='</div>';
  h+='<p style="text-align:center;color:var(--t3);font-size:11px;margin-top:8px">Current: '+String.fromCharCode(8377)+fmt(bal)+' &bull; No fixed timeline. Prove the edge is real, then deploy real capital.</p></div>';

  h+='<div class="cd" style="margin-bottom:16px"><div class="ct">SEBI Algo Trading Compliance (April 2026)</div><div class="sebi">';
  const sebi=[
    {t:'Strategy Tagging',d:'Each order tagged with strategy ID'},
    {t:'Static IP Ready',d:'Fixed IP for broker API'},
    {t:'Order Rate Cap',d:'10 orders/sec enforced'},
    {t:'2FA Auth',d:'Daily JWT token with expiry'},
    {t:'Audit Trail',d:'Full signal + trade logging'},
    {t:'Kill Switch',d:'Auto-kill on critical risk'}
  ];
  sebi.forEach(s=>{
    h+='<div class="sebi-i"><span class="sebi-ck">&#10003;</span><div><h4>'+s.t+'</h4><p>'+s.d+'</p></div></div>';
  });
  h+='</div></div>';

  h+='<div class="cd"><div class="ct">Arthashastra Principles</div>';
  h+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px">';
  const principles=[
    {t:'Kosha (Treasury)',d:'Capital preservation above all. Never risk more than you can afford to lose.'},
    {t:'Saptanga (7 Pillars)',d:'7 confluence factors mirror the 7 pillars of statecraft. All must align.'},
    {t:'Anvikshiki (Analysis)',d:'ML-driven analysis before action. No trade without systematic evaluation.'},
    {t:'Dandaniti (Discipline)',d:'Guardrails enforce discipline even when emotions fail. Rules are non-negotiable.'}
  ];
  principles.forEach(p=>{
    h+='<div style="padding:12px;background:var(--bg3);border-radius:8px"><div style="font-size:11px;font-weight:700;color:var(--gold);margin-bottom:3px">'+p.t+'</div><div style="font-size:10px;color:var(--t3)">'+p.d+'</div></div>';
  });
  h+='</div></div>';
  el.innerHTML=h;
}

/* ── REVERSE ENGINE TAB ── */
let _revData=null;
let _revLoading=false;
async function rReverse(){
  const el=document.getElementById('tp-reverse');
  if(tab!=='reverse')return;
  if(_revLoading){el.innerHTML='<div class="loading">Running reverse engine analysis... this may take 30-60 seconds</div>';return;}
  if(!_revData){
    el.innerHTML='<div style="text-align:center;padding:40px"><button onclick="runReverse()" class="btn-action" style="padding:12px 32px;font-size:15px;background:var(--gold);color:var(--bg-0);border:none;border-radius:8px;cursor:pointer;font-weight:700">Run Analysis</button><p style="color:var(--text-3);margin-top:12px;font-size:13px">Analyzes all closed trades: indicator accuracy, optimal SL/TP, pattern mining</p></div>';
    return;
  }
  const R=_revData;
  let h='';

  /* Autopsy Summary */
  const a=R.autopsy||{};
  if(a.valid_trades>0){
    h+='<div class="cd"><div class="ct" style="display:flex;align-items:center;gap:8px"><span style="color:var(--gold)">&#x1F52C;</span> Trade Autopsy</div>';
    h+='<div class="sg">';
    h+='<div class="sc2"><div class="sl">Trades Analyzed</div><div class="sv">'+a.valid_trades+'</div></div>';
    h+='<div class="sc2"><div class="sl">Win Rate</div><div class="sv">'+(a.win_rate*100).toFixed(0)+'%</div></div>';
    const capC=a.avg_capture_ratio>=0.5?'var(--profit)':'var(--loss)';
    h+='<div class="sc2"><div class="sl">Avg Capture</div><div class="sv" style="color:'+capC+'">'+(a.avg_capture_ratio*100).toFixed(0)+'%</div></div>';
    const leftC=a.total_pnl_left_on_table>0?'var(--warn)':'var(--profit)';
    h+='<div class="sc2"><div class="sl">P&L Left on Table</div><div class="sv" style="color:'+leftC+'">'+fpnl(a.total_pnl_left_on_table)+'</div></div>';
    h+='<div class="sc2"><div class="sl">Optimal SL</div><div class="sv">'+a.avg_optimal_sl_pct.toFixed(1)+'%</div></div>';
    h+='<div class="sc2"><div class="sl">Optimal TP</div><div class="sv">'+a.avg_optimal_tp_pct.toFixed(1)+'%</div></div>';
    h+='</div>';

    /* Indicator accuracy */
    const ind=a.indicator_accuracy||{};
    const indArr=Object.entries(ind).sort((a,b)=>b[1].accuracy-a[1].accuracy);
    if(indArr.length){
      h+='<div style="margin-top:16px"><div class="sl" style="margin-bottom:8px;font-size:12px">INDICATOR ACCURACY</div>';
      indArr.forEach(([name,d])=>{
        const pct=(d.accuracy*100).toFixed(0);
        const barW=Math.max(pct,5);
        const barC=d.accuracy>=0.6?'var(--profit)':d.accuracy>=0.4?'var(--warn)':'var(--loss)';
        h+='<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">';
        h+='<span style="width:100px;font-size:12px;color:var(--text-2)">'+name+'</span>';
        h+='<div style="flex:1;height:16px;background:var(--bg-2);border-radius:4px;overflow:hidden"><div style="width:'+barW+'%;height:100%;background:'+barC+';border-radius:4px;transition:width 0.3s"></div></div>';
        h+='<span style="width:55px;text-align:right;font-size:12px;font-weight:600;color:'+barC+'">'+pct+'%</span>';
        h+='</div>';
      });
      h+='</div>';
    }

    /* Component scores */
    const comp=a.component_scores||{};
    const compArr=Object.entries(comp).sort((a,b)=>b[1]-a[1]);
    if(compArr.length){
      h+='<div style="margin-top:16px"><div class="sl" style="margin-bottom:8px;font-size:12px">COMPONENT SCORES (avg)</div>';
      compArr.forEach(([name,score])=>{
        const c=score>0?'var(--profit)':'var(--loss)';
        const sign=score>0?'+':'';
        h+='<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px">';
        h+='<span style="color:var(--text-2)">'+name+'</span>';
        h+='<span style="color:'+c+';font-weight:600;font-family:JetBrains Mono,monospace">'+sign+score.toFixed(2)+'</span></div>';
      });
      h+='</div>';
    }

    h+='</div>';
  }

  /* Pattern Mining */
  const p=R.patterns||{};
  if(p.total_patterns>0){
    h+='<div class="cd" style="margin-top:12px"><div class="ct" style="display:flex;align-items:center;gap:8px"><span style="color:var(--gold)">&#x26CF;</span> Pattern Mining</div>';
    h+='<div class="sg">';
    h+='<div class="sc2"><div class="sl">Patterns Found</div><div class="sv">'+p.total_patterns+'</div></div>';
    h+='<div class="sc2" style="border-left:2px solid var(--profit)"><div class="sl">Boosters</div><div class="sv" style="color:var(--profit)">'+p.boosters+'</div></div>';
    h+='<div class="sc2" style="border-left:2px solid var(--loss)"><div class="sl">Penalties</div><div class="sv" style="color:var(--loss)">'+p.penalties+'</div></div>';
    h+='<div class="sc2" style="border-left:2px solid var(--warn)"><div class="sl">Blockers</div><div class="sv" style="color:var(--warn)">'+p.blockers+'</div></div>';
    h+='</div>';

    /* Top boosters */
    const boost=p.top_boosters||[];
    if(boost.length){
      h+='<div style="margin-top:16px"><div class="sl" style="margin-bottom:8px;font-size:12px;color:var(--profit)">BOOSTERS (winning patterns)</div>';
      boost.forEach(b=>{
        h+='<div style="display:flex;justify-content:space-between;align-items:center;padding:8px;margin-bottom:4px;background:var(--profit-bg);border-radius:6px;border-left:3px solid var(--profit)">';
        h+='<span style="font-size:12px;color:var(--text-1)">'+b.name.replace(/_/g,' ')+'</span>';
        h+='<span style="font-size:12px;color:var(--profit);font-family:JetBrains Mono,monospace">WR='+(b.wr*100).toFixed(0)+'% n='+b.n+' z='+b.z.toFixed(1)+' adj='+b.adj.toFixed(0)+'</span></div>';
      });
      h+='</div>';
    }

    /* Top penalties */
    const pen=p.top_penalties||[];
    if(pen.length){
      h+='<div style="margin-top:12px"><div class="sl" style="margin-bottom:8px;font-size:12px;color:var(--loss)">PENALTIES (losing patterns)</div>';
      pen.forEach(b=>{
        h+='<div style="display:flex;justify-content:space-between;align-items:center;padding:8px;margin-bottom:4px;background:var(--loss-bg);border-radius:6px;border-left:3px solid var(--loss)">';
        h+='<span style="font-size:12px;color:var(--text-1)">'+b.name.replace(/_/g,' ')+'</span>';
        h+='<span style="font-size:12px;color:var(--loss);font-family:JetBrains Mono,monospace">WR='+(b.wr*100).toFixed(0)+'% n='+b.n+' z='+b.z.toFixed(1)+' adj='+b.adj.toFixed(0)+'</span></div>';
      });
      h+='</div>';
    }

    /* Blockers */
    const blk=p.blockers_list||[];
    if(blk.length){
      h+='<div style="margin-top:12px"><div class="sl" style="margin-bottom:8px;font-size:12px;color:var(--warn)">BLOCKED PATTERNS (auto-reject)</div>';
      blk.forEach(b=>{
        h+='<div style="display:flex;justify-content:space-between;align-items:center;padding:8px;margin-bottom:4px;background:var(--warn-bg);border-radius:6px;border-left:3px solid var(--warn)">';
        h+='<span style="font-size:12px;color:var(--text-1)">'+b.name.replace(/_/g,' ')+'</span>';
        h+='<span style="font-size:12px;color:var(--warn);font-family:JetBrains Mono,monospace">WR='+(b.wr*100).toFixed(0)+'% n='+b.n+'</span></div>';
      });
      h+='</div>';
    }

    h+='</div>';
  }

  /* Per-trade autopsy details */
  const auts=(R.autopsies||[]).filter(a=>a.data_quality==='OK');
  if(auts.length){
    h+='<div class="cd" style="margin-top:12px"><div class="ct" style="display:flex;align-items:center;gap:8px"><span style="color:var(--gold)">&#x1F4CB;</span> Trade-by-Trade Autopsy</div>';
    h+='<div style="overflow-x:auto"><table class="tbl" style="width:100%"><thead><tr>';
    h+='<th>ID</th><th>Symbol</th><th>Dir</th><th>P&L</th><th>Optimal</th><th>Capture</th><th>Left</th><th>Accuracy</th><th>Exit</th>';
    h+='</tr></thead><tbody>';
    auts.sort((a,b)=>b.trade_id-a.trade_id).forEach(t=>{
      const pC=t.actual_pnl>=0?'var(--profit)':'var(--loss)';
      const capC=t.capture_ratio>=0.5?'var(--profit)':'var(--loss)';
      const accC=t.indicator_accuracy>=0.6?'var(--profit)':t.indicator_accuracy>=0.4?'var(--warn)':'var(--loss)';
      h+='<tr>';
      h+='<td>'+t.trade_id+'</td>';
      h+='<td style="font-size:11px">'+t.symbol.substring(0,18)+'</td>';
      h+='<td>'+(t.direction!=='LONG'?'SELL':'BUY')+'</td>';
      h+='<td style="color:'+pC+'">'+fpnl(t.actual_pnl)+'</td>';
      h+='<td style="color:var(--profit)">'+fpnl(t.optimal_pnl)+'</td>';
      h+='<td style="color:'+capC+'">'+(t.capture_ratio*100).toFixed(0)+'%</td>';
      h+='<td style="color:var(--warn)">'+fpnl(t.pnl_left_on_table)+'</td>';
      h+='<td style="color:'+accC+'">'+(t.indicator_accuracy*100).toFixed(0)+'%</td>';
      h+='<td style="font-size:11px">'+t.exit_reason+'</td>';
      h+='</tr>';
    });
    h+='</tbody></table></div></div>';
  }

  h+='<div style="text-align:center;margin-top:16px"><span style="color:var(--text-3);font-size:12px">Rules injected: '+R.rules_injected+'</span>';
  h+='<button onclick="runReverse()" style="margin-left:16px;padding:6px 16px;font-size:12px;background:var(--bg-3);color:var(--text-2);border:1px solid var(--border);border-radius:6px;cursor:pointer">Re-run</button></div>';

  el.innerHTML=h;
}
async function runReverse(){
  _revLoading=true;
  rReverse();
  try{
    const r=await fetch('/api/reverse_engine');
    _revData=await r.json();
  }catch(e){
    showToast('Reverse engine failed: '+e.message,'error');
  }
  _revLoading=false;
  rReverse();
}
window.runReverse=runReverse;

/* ── FETCH LOOP ── */
let _lastGoodPoll=Date.now();
let _pollFails=0;
async function poll(){
  try{
    const ac=new AbortController();
    const tm=setTimeout(()=>ac.abort(),3000);
    const r=await fetch('/api/state',{signal:ac.signal});
    clearTimeout(tm);
    S=await r.json();
    if(S.csrf)_csrf=S.csrf;
    _lastGoodPoll=Date.now();
    _pollFails=0;
    document.getElementById('live-dot').style.background='#10b981';
    render();
    checkPnlAlerts();
  }catch(e){
    _pollFails++;
    document.getElementById('live-dot').style.background=_pollFails>3?'#ef4444':'#f59e0b';
  }
  const staleMs=Date.now()-_lastGoodPoll;
  const staleSec=Math.floor(staleMs/1000);
  const dot=document.getElementById('live-dot');
  if(staleSec>3){dot.style.background='#f59e0b';dot.title='Data '+staleSec+'s stale'}
  else{dot.title='Live'}
  setTimeout(poll,_pollFails>5?2000:1000);
}
poll();
window.doAction=doAction;
window.editSL=editSL;
window.editTP=editTP;
window.showModal=showModal;
window.closeModal=closeModal;
window.exportCSV=exportCSV;
window.saveNote=saveNote;
window.showToast=showToast;
window.loadChain=loadChain;
window._chainUl=_chainUl;
})();
</script>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=_HTML.replace("__CSRF_TOKEN__", _CSRF_TOKEN))
