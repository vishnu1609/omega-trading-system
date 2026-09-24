"""
Dhan WebSocket Live Market Feed

Uses the official dhanhq MarketFeed for real-time tick data.
Maintains a thread-safe LTP cache keyed by (exchange_segment, security_id).

Resilience patterns adapted from OpenAlgo's DhanWebSocket:
- Data stall watchdog (detects silent TCP-alive but data-dead connections)
- Exponential backoff reconnect (5s base, 60s cap, 10 attempts)
- Fatal error detection (429 / expired subscription → stop reconnecting)
- Tick rate metrics for self-learning feedback
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

HEALTH_CHECK_INTERVAL = 30
DATA_TIMEOUT = 90
MAX_RECONNECT_ATTEMPTS = 10
BASE_RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60


class LiveFeed:
    """Thread-safe wrapper around DhanHQ MarketFeed WebSocket.

    Adds resilience: data-stall watchdog, auto-reconnect with exponential
    backoff, fatal error detection, and tick-rate metrics.
    """

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
        self._health_thread: threading.Thread | None = None
        self._running = False

        self._last_tick_time: float = 0
        self._tick_count: int = 0
        self._tick_count_1m: int = 0
        self._tick_window_start: float = 0
        self._fatal_error: bool = False
        self._fatal_reason: str = ""
        self._reconnect_count: int = 0
        self._connected: bool = False

    def start(self, instruments: list[tuple[int, str, int]] | None = None) -> None:
        if self._running:
            return
        self._fatal_error = False
        self._fatal_reason = ""
        self._reconnect_count = 0
        initial = instruments or []
        self._subscribed = set(initial)
        self._running = True

        self._thread = threading.Thread(
            target=self._connect_loop, daemon=True, name="dhan-ws-connect")
        self._thread.start()
        log.info("LiveFeed started with %d instruments", len(initial))

    def _connect_loop(self) -> None:
        """Connect with exponential backoff on failure."""
        while self._running and not self._fatal_error:
            try:
                from dhanhq import DhanContext, MarketFeed
            except ImportError:
                log.error("dhanhq not installed — pip install dhanhq")
                self._running = False
                return

            ctx = DhanContext(self._client_id, self._token)
            try:
                self._feed = MarketFeed(ctx, list(self._subscribed), "v2")
            except Exception as e:
                log.error("MarketFeed init failed: %s", e)
                if self._is_fatal_error(str(e)):
                    self._fatal_error = True
                    self._fatal_reason = str(e)
                    log.critical("Fatal feed error — stopping: %s", e)
                    self._running = False
                    return
                self._backoff_sleep()
                continue

            self._connected = True
            self._last_tick_time = time.time()
            self._reconnect_count = 0

            self._reader_thread = threading.Thread(
                target=self._read_loop, daemon=True, name="dhan-ws-reader")
            self._reader_thread.start()

            self._start_health_check()

            try:
                self._feed.run_forever()
            except Exception as e:
                log.error("WebSocket loop exited: %s", e)
                if self._is_fatal_error(str(e)):
                    self._fatal_error = True
                    self._fatal_reason = str(e)
                    log.critical("Fatal feed error — stopping: %s", e)
                    self._running = False
                    return

            self._connected = False
            if not self._running:
                break

            self._backoff_sleep()

    def _backoff_sleep(self) -> None:
        self._reconnect_count += 1
        if self._reconnect_count >= MAX_RECONNECT_ATTEMPTS:
            log.error("Max reconnect attempts (%d) reached — stopping",
                      MAX_RECONNECT_ATTEMPTS)
            self._running = False
            return
        delay = min(BASE_RECONNECT_DELAY * (2 ** (self._reconnect_count - 1)),
                    MAX_RECONNECT_DELAY)
        log.warning("Reconnecting in %ds (attempt %d/%d)",
                    delay, self._reconnect_count, MAX_RECONNECT_ATTEMPTS)
        time.sleep(delay)

    @staticmethod
    def _is_fatal_error(error_str: str) -> bool:
        """Detect non-recoverable errors that should stop reconnection."""
        lower = error_str.lower()
        return any(indicator in lower for indicator in [
            "429", "too many requests", "client id is blocked",
            "subscription", "plan",
        ])

    def _start_health_check(self) -> None:
        if self._health_thread and self._health_thread.is_alive():
            return
        self._health_thread = threading.Thread(
            target=self._health_loop, daemon=True, name="dhan-ws-health")
        self._health_thread.start()

    def _health_loop(self) -> None:
        """Detect silent data stalls — force reconnect if no ticks within timeout."""
        while self._running and self._connected:
            time.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running or not self._connected:
                break
            if self._last_tick_time <= 0:
                continue
            elapsed = time.time() - self._last_tick_time
            if elapsed > DATA_TIMEOUT:
                log.error("Data stall: no ticks for %.0fs (threshold %ds) — "
                          "forcing reconnect", elapsed, DATA_TIMEOUT)
                self._connected = False
                if self._feed:
                    try:
                        self._feed.close_connection()
                    except Exception:
                        pass
                break

    def _read_loop(self) -> None:
        while self._running and self._connected:
            try:
                data = self._feed.get_data()
                if data:
                    self._last_tick_time = time.time()
                    self._tick_count += 1
                    self._tick_count_1m += 1
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
        self._connected = False
        if self._feed:
            try:
                self._feed.close_connection()
            except Exception:
                pass
        log.info("LiveFeed stopped")

    def get_health_metrics(self) -> dict:
        """Return feed health for dashboard / self-learning."""
        now = time.time()
        since_last = now - self._last_tick_time if self._last_tick_time > 0 else -1
        ticks_per_min = 0
        if self._tick_window_start > 0:
            window = now - self._tick_window_start
            if window > 0:
                ticks_per_min = self._tick_count_1m / window * 60
        return {
            "connected": self._connected,
            "running": self._running,
            "fatal_error": self._fatal_error,
            "fatal_reason": self._fatal_reason,
            "reconnect_count": self._reconnect_count,
            "total_ticks": self._tick_count,
            "seconds_since_last_tick": round(since_last, 1),
            "ticks_per_minute": round(ticks_per_min, 1),
            "subscribed_count": len(self._subscribed),
        }

    def reset_tick_window(self) -> None:
        """Reset the 1-minute tick counter (call from main loop every minute)."""
        self._tick_count_1m = 0
        self._tick_window_start = time.time()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def subscribed_count(self) -> int:
        return len(self._subscribed)
