"""
Dhan WebSocket Live Market Feed

Uses the official dhanhq MarketFeed for real-time tick data.
Maintains a thread-safe LTP cache keyed by (exchange_segment, security_id).
"""
from __future__ import annotations

import logging
import os
import ssl
import threading
import time
from collections import defaultdict

try:
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    _orig_create_default_context = ssl.create_default_context

    def _patched_default_context(*args, **kwargs):
        kwargs.setdefault("cafile", certifi.where())
        return _orig_create_default_context(*args, **kwargs)

    ssl.create_default_context = _patched_default_context
except ImportError:
    pass

log = logging.getLogger(__name__)

EXCHANGE_SEG_MAP = {
    "IDX_I": 0,
    "NSE": 1,
    "NSE_FNO": 2,
    "BSE": 4,
    "BSE_FNO": 8,
    "MCX": 5,
}

SEG_INT_TO_STR = {v: k for k, v in EXCHANGE_SEG_MAP.items()}


class LiveFeed:
    """Thread-safe wrapper around DhanHQ MarketFeed WebSocket."""

    def __init__(self, client_id: str = "", access_token: str = ""):
        self._client_id = client_id or os.environ.get("DHAN_CLIENT_ID", "")
        self._token = access_token or os.environ.get("DHAN_ACCESS_TOKEN", "")
        self._lock = threading.Lock()
        self._ltp: dict[str, float] = {}
        self._quotes: dict[str, dict] = {}
        self._subscribed: set[tuple[int, str, int]] = set()
        self._feed = None
        self._thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._running = False

    def start(self, instruments: list[tuple[int, str, int]] | None = None) -> None:
        if self._running:
            return
        try:
            from dhanhq import DhanContext, MarketFeed
        except ImportError:
            log.error("dhanhq not installed — pip install dhanhq")
            return

        ctx = DhanContext(self._client_id, self._token)
        initial = instruments or []
        self._subscribed = set(initial)

        try:
            self._feed = MarketFeed(ctx, list(initial), "v2")
        except Exception as e:
            log.error("MarketFeed init failed: %s", e)
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._ws_loop, daemon=True, name="dhan-ws")
        self._thread.start()

        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="dhan-ws-reader")
        self._reader_thread.start()
        log.info("LiveFeed started with %d instruments", len(initial))

    def _ws_loop(self) -> None:
        try:
            self._feed.run_forever()
        except Exception as e:
            log.error("WebSocket loop exited: %s", e)
            self._running = False

    def _read_loop(self) -> None:
        while self._running:
            try:
                data = self._feed.get_data()
                if data:
                    self._process_tick(data)
            except Exception:
                pass
            time.sleep(0.05)

    def _process_tick(self, data: dict) -> None:
        sec_id = str(data.get("security_id", data.get("securityId", "")))
        if not sec_id:
            return

        seg = data.get("exchange_segment", data.get("exchangeSegment", ""))
        seg_str = SEG_INT_TO_STR.get(seg, str(seg)) if isinstance(seg, int) else str(seg)
        key = f"{seg_str}:{sec_id}"

        ltp = data.get("LTP", data.get("ltp", 0))
        if ltp and ltp > 0:
            with self._lock:
                self._ltp[key] = float(ltp)

        with self._lock:
            self._quotes[key] = {
                "ltp": float(ltp) if ltp else 0,
                "open": float(data.get("open", 0)),
                "high": float(data.get("high", 0)),
                "low": float(data.get("low", 0)),
                "close": float(data.get("close", data.get("prev_close", 0))),
                "volume": int(data.get("volume", 0)),
                "oi": int(data.get("oi", data.get("OI", 0))),
                "timestamp": data.get("timestamp", time.time()),
            }

    def get_ltp(self, security_id: str, exchange_segment: str = "") -> float:
        key = f"{exchange_segment}:{security_id}" if exchange_segment else None
        with self._lock:
            if key and key in self._ltp:
                return self._ltp[key]
            for k, v in self._ltp.items():
                if k.endswith(f":{security_id}"):
                    return v
        return 0.0

    def get_quote(self, security_id: str, exchange_segment: str = "") -> dict:
        key = f"{exchange_segment}:{security_id}" if exchange_segment else None
        with self._lock:
            if key and key in self._quotes:
                return dict(self._quotes[key])
            for k, v in self._quotes.items():
                if k.endswith(f":{security_id}"):
                    return dict(v)
        return {}

    def subscribe(self, instruments: list[tuple[int, str, int]]) -> None:
        if not self._feed or not self._running:
            return
        new = [i for i in instruments if i not in self._subscribed]
        if not new:
            return
        try:
            self._feed.subscribe_symbols(new)
            self._subscribed.update(new)
            log.info("Subscribed %d new instruments (total: %d)",
                     len(new), len(self._subscribed))
        except Exception as e:
            log.error("Subscribe failed: %s", e)

    def unsubscribe(self, instruments: list[tuple[int, str, int]]) -> None:
        if not self._feed or not self._running:
            return
        try:
            self._feed.unsubscribe_symbols(instruments)
            self._subscribed -= set(instruments)
        except Exception as e:
            log.error("Unsubscribe failed: %s", e)

    def update_token(self, token: str) -> None:
        self._token = token
        log.info("LiveFeed token updated — restart needed for new WS connection")

    def stop(self) -> None:
        self._running = False
        if self._feed:
            try:
                self._feed.close_connection()
            except Exception:
                pass
        log.info("LiveFeed stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed_count(self) -> int:
        return len(self._subscribed)
