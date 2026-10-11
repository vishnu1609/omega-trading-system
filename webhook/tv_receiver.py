"""
TradingView Webhook Receiver

Accepts POST alerts from TradingView, validates the payload,
and queues signals for the strategy engine.

Expected JSON payload:
{
    "strategy": "omega_complete",
    "symbol": "BANKNIFTY",
    "side": "BUY_CE" | "BUY_PE" | "SELL_CE" | "SELL_PE" | "BUY" | "SELL" | "EXIT",
    "strike": 50000,          // optional, 0 = auto ATM
    "sl": 100,                // optional, 0 = use default
    "asset": "options"        // optional, "futures" for commodity/index futures
}

Signature verification via HMAC-SHA256 using TV_WEBHOOK_SECRET.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import queue
import time
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from webhook.dashboard import router as dashboard_router

log = logging.getLogger(__name__)

signal_queue: queue.Queue[dict[str, Any]] = queue.Queue()

app = FastAPI(title="Omega Webhook Receiver", docs_url=None, redoc_url=None)
app.include_router(dashboard_router)

def _get_webhook_secret() -> str:
    return os.environ.get("TV_WEBHOOK_SECRET", "")

VALID_SIDES = {"BUY_CE", "BUY_PE", "SELL_CE", "SELL_PE", "BUY", "SELL", "EXIT"}
VALID_STRATEGIES = {"omega_complete", "omega_futures", "omega_v10"}

MAX_QUEUE_SIZE = 20
MAX_REQUESTS_PER_MINUTE = 30

_request_counts: dict[str, list[float]] = defaultdict(list)


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    window = [t for t in _request_counts[ip] if now - t < 60]
    _request_counts[ip] = window
    if len(window) >= MAX_REQUESTS_PER_MINUTE:
        return True
    _request_counts[ip].append(now)
    return False


def verify_signature(raw_body: bytes, signature: str) -> bool:
    secret = _get_webhook_secret()
    if not secret:
        log.warning("No webhook secret configured — accepting all requests")
        return True
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


@app.post("/tv-alert")
async def tv_alert(request: Request) -> dict:
    client_ip = request.client.host if request.client else "unknown"

    if _is_rate_limited(client_ip):
        log.warning("Rate limited: %s", client_ip)
        raise HTTPException(status_code=429, detail="Too many requests")

    raw = await request.body()
    if len(raw) > 4096:
        raise HTTPException(status_code=413, detail="Payload too large")

    sig = request.headers.get("X-Signature", "")

    if not verify_signature(raw, sig):
        log.warning("Invalid webhook signature from %s", client_ip)
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")

    missing = {"strategy", "symbol", "side"} - set(payload.keys())
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {missing}")

    side = str(payload["side"]).upper()
    if side not in VALID_SIDES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid side '{side}'. Valid: {VALID_SIDES}",
        )
    payload["side"] = side

    strategy = str(payload.get("strategy", ""))
    if strategy not in VALID_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy '{strategy}'")

    payload["symbol"] = str(payload["symbol"])[:20]
    payload["strike"] = int(payload.get("strike", 0))
    payload["sl"] = float(payload.get("sl", 0))

    if signal_queue.qsize() >= MAX_QUEUE_SIZE:
        log.error("Signal queue full — dropping signal")
        raise HTTPException(status_code=503, detail="Queue full")

    signal_queue.put(payload)
    log.info("Webhook queued: %s %s %s from %s",
             payload["strategy"], payload["symbol"], side, client_ip)
    return {"ok": True, "queued": signal_queue.qsize()}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "queue_size": signal_queue.qsize()}
