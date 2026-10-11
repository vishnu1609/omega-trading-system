"""
Dhan WebSocket Live Market Feed

Uses DhanHQ MarketFeed WebSocket for real-time tick-by-tick data (~200-500ms latency).
Falls back to REST polling if WebSocket fails.
Maintains a thread-safe LTP cache keyed by "segment:security_id".
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

WS_SEG_MAP = {
    0: 0,    # IDX_I -> MarketFeed.IDX
    1: 1,    # NSE
    2: 2,    # NSE_FNO
    4: 4,    # BSE
    5: 5,    # MCX
    8: 8,    # BSE_FNO
}

HEALTH_CHECK_INTERVAL = 30
DATA_TIMEOUT = 90
MAX_RECONNECT_ATTEMPTS = 10
BASE_RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60


class LiveFeed:
    """Real-time market feed using DhanHQ WebSocket with REST fallback."""

    def __init__(self, client_id: str = "", access_token: str = ""):
        self._client_id = client_id or os.environ.get("DHAN_CLIENT_ID", "")
        self._token = access_token or os.environ.get("DHAN_ACCESS_TOKEN", "")
        self._lock = threading.Lock()
        self._ltp: dict[str, float] = {}
        self._quotes: dict[str, dict] = {}
        self._subscribed: set[tuple[int, str, int]] = set()
        self._ws_feed = None
        self._ws_thread: threading.Thread | None = None
        self._rest_thread: threading.Thread | None = None
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
        self._ws_connected: bool = False
        self._mode: str = "starting"

    def start(self, instruments: list[tuple[int, str, int]] | None = None) -> None:
        if self._running:
            return
        self._fatal_error = False
        self._fatal_reason = ""
        self._reconnect_count = 0
        initial = instruments or []
        self._subscribed = set(initial)
        self._running = True

        self._ws_thread = threading.Thread(
            target=self._ws_feed_loop, daemon=True, name="dhan-ws-feed")
        self._ws_thread.start()

        self._rest_thread = threading.Thread(
            target=self._rest_poll_loop, daemon=True, name="dhan-rest-fallback")
        self._rest_thread.start()

        self._health_thread = threading.Thread(
            target=self._health_loop, daemon=True, name="dhan-feed-health")
        self._health_thread.start()

        log.info("LiveFeed started (WebSocket + REST fallback) with %d instruments", len(initial))

    def _build_ws_instruments(self) -> list[tuple]:
        """Convert internal instrument tuples to DhanHQ MarketFeed format."""
        try:
            from dhanhq import MarketFeed as MF
        except ImportError:
            return []

        ws_instruments = []
        for seg_int, sec_id, sub_type in self._subscribed:
            ws_seg = seg_int
            if sub_type == 15:
                mode = MF.Ticker
            elif sub_type == 17:
                mode = MF.Quote
            elif sub_type == 21:
                mode = MF.Full
            else:
                mode = MF.Ticker
            ws_instruments.append((ws_seg, str(sec_id), mode))
        return ws_instruments

    def _ws_feed_loop(self) -> None:
        """Run DhanHQ WebSocket MarketFeed with auto-reconnect."""
        try:
            from dhanhq import DhanContext, MarketFeed as MF
        except ImportError:
            log.warning("dhanhq package not available — WebSocket feed disabled, using REST only")
            self._mode = "rest_only"
            return

        while self._running:
            ws_instruments = self._build_ws_instruments()
            if not ws_instruments:
                time.sleep(2)
                continue

            try:
                dhan_ctx = DhanContext(self._client_id, self._token)
                self._ws_feed = MF(dhan_ctx, ws_instruments, "v2")
                log.info("WebSocket MarketFeed connecting with %d instruments...", len(ws_instruments))
                self._ws_connected = True
                self._connected = True
                self._mode = "websocket"
                self._reconnect_count = 0

                while self._running and self._ws_connected:
                    try:
                        self._ws_feed.run_forever()
                        response = self._ws_feed.get_data()

                        if response and isinstance(response, dict):
                            self._process_ws_tick(response)

                    except Exception as e:
                        err_str = str(e)
                        if self._is_fatal_error(err_str):
                            log.error("WebSocket fatal error: %s — switching to REST only", err_str)
                            self._fatal_error = True
                            self._fatal_reason = err_str
                            self._ws_connected = False
                            self._mode = "rest_only"
                            return
                        if "connection" in err_str.lower() or "closed" in err_str.lower():
                            log.warning("WebSocket disconnected: %s", err_str)
                            self._ws_connected = False
                            break
                        time.sleep(0.1)

            except Exception as e:
                err_str = str(e)
                log.warning("WebSocket connection failed: %s", err_str)
                self._ws_connected = False
                self._mode = "rest_fallback"

                if self._is_fatal_error(err_str):
                    log.error("Fatal WebSocket error — REST only mode")
                    self._mode = "rest_only"
                    return

            if self._running:
                self._reconnect_count += 1
                delay = min(BASE_RECONNECT_DELAY * (2 ** min(self._reconnect_count - 1, 5)),
                            MAX_RECONNECT_DELAY)
                log.info("WebSocket reconnecting in %ds (attempt %d)", delay, self._reconnect_count)
                time.sleep(delay)

    def _process_ws_tick(self, data: dict) -> None:
        """Process a tick from the WebSocket MarketFeed."""
        now = time.time()
        self._last_tick_time = now
        self._tick_count += 1
        self._tick_count_1m += 1

        sec_id = str(data.get("security_id", data.get("securityId", "")))
        if not sec_id:
            return

        seg = data.get("exchange_segment", data.get("exchangeSegment", ""))
        if isinstance(seg, int):
            seg_str = SEG_INT_TO_STR.get(seg, str(seg))
        else:
            seg_str = str(seg)
        key = f"{seg_str}:{sec_id}"

        ltp = data.get("LTP", data.get("ltp", 0))
        if ltp and float(ltp) > 0:
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
                "timestamp": now,
            }

    def _rest_poll_loop(self) -> None:
        """REST fallback — polls when WebSocket is down or as supplement."""
        import requests
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "access-token": self._token,
            "client-id": self._client_id,
        })
        LTP_URL = "https://api.dhan.co/v2/marketfeed/ltp"

        seg_map = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 4: "BSE_EQ", 5: "MCX_COMM", 8: "BSE_FNO"}
        fail_streak = 0
        rate_limit_backoff = 0

        while self._running:
            if self._ws_connected:
                time.sleep(5)
                continue

            if not self._subscribed:
                time.sleep(2)
                continue

            self._mode = "rest_fallback" if self._mode != "rest_only" else "rest_only"

            by_seg: dict[str, list[int]] = defaultdict(list)
            for seg_int, sec_id, _ in self._subscribed:
                seg_str = seg_map.get(seg_int, str(seg_int))
                by_seg[seg_str].append(int(sec_id))

            payload = dict(by_seg)

            try:
                resp = session.post(LTP_URL, json=payload, timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    now = time.time()
                    self._connected = True
                    self._last_tick_time = now
                    fail_streak = 0
                    rate_limit_backoff = 0

                    container = data.get("data", data)
                    if isinstance(container, dict):
                        for seg_str, instruments in container.items():
                            if isinstance(instruments, dict):
                                for sec_id, info in instruments.items():
                                    ltp = info.get("last_price", 0) if isinstance(info, dict) else 0
                                    if ltp > 0:
                                        key = f"{seg_str}:{sec_id}"
                                        with self._lock:
                                            self._ltp[key] = float(ltp)
                                        self._tick_count += 1
                                        self._tick_count_1m += 1
                elif resp.status_code == 429:
                    rate_limit_backoff = min(rate_limit_backoff + 1, 6)
                    wait = 10 * (2 ** rate_limit_backoff)
                    if rate_limit_backoff <= 1:
                        log.warning("LiveFeed REST rate limited — backing off %ds", wait)
                    time.sleep(wait)
                    continue
                else:
                    fail_streak += 1
                    if fail_streak == 1 or fail_streak % 30 == 0:
                        log.warning("LiveFeed REST failed: HTTP %d", resp.status_code)
            except Exception as e:
                fail_streak += 1
                if fail_streak == 1 or fail_streak % 30 == 0:
                    log.warning("LiveFeed REST error: %s", e)
                self._connected = False

            time.sleep(2)

    def _health_loop(self) -> None:
        """Monitor feed health, detect stalls."""
        while self._running:
            time.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running:
                break
            if self._last_tick_time <= 0:
                continue
            elapsed = time.time() - self._last_tick_time
            if elapsed > DATA_TIMEOUT and self._ws_connected:
                log.error("Data stall: no ticks for %.0fs — forcing WebSocket reconnect", elapsed)
                self._ws_connected = False
                if self._ws_feed:
                    try:
                        self._ws_feed.close_connection()
                    except Exception:
                        pass

    @staticmethod
    def _is_fatal_error(error_str: str) -> bool:
        lower = error_str.lower()
        return any(indicator in lower for indicator in [
            "429", "too many requests", "client id is blocked",
            "subscription", "plan",
            "event loop is already running",
        ])

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
        if not self._running:
            return
        new = [i for i in instruments if i not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)

        if self._ws_connected and self._ws_feed:
            try:
                from dhanhq import MarketFeed as MF
                ws_new = []
                for seg_int, sec_id, sub_type in new:
                    mode = MF.Ticker if sub_type == 15 else MF.Quote if sub_type == 17 else MF.Full if sub_type == 21 else MF.Ticker
                    ws_new.append((seg_int, str(sec_id), mode))
                self._ws_feed.subscribe_symbols(ws_new)
                log.info("WebSocket: subscribed %d new instruments (total: %d)", len(ws_new), len(self._subscribed))
            except Exception as e:
                log.warning("WebSocket subscribe failed: %s — REST will cover", e)
        else:
            log.info("Subscribed %d new instruments (total: %d) — REST mode",
                     len(new), len(self._subscribed))

    def unsubscribe(self, instruments: list[tuple[int, str, int]]) -> None:
        if not self._running:
            return
        self._subscribed -= set(instruments)

        if self._ws_connected and self._ws_feed:
            try:
                from dhanhq import MarketFeed as MF
                ws_unsub = []
                for seg_int, sec_id, sub_type in instruments:
                    mode = MF.Ticker if sub_type == 15 else MF.Quote
                    ws_unsub.append((seg_int, str(sec_id), mode))
                self._ws_feed.unsubscribe_symbols(ws_unsub)
            except Exception:
                pass

        for seg_int, sec_id, _ in instruments:
            seg_str = SEG_INT_TO_STR.get(seg_int, str(seg_int))
            key = f"{seg_str}:{sec_id}"
            with self._lock:
                self._ltp.pop(key, None)
                self._quotes.pop(key, None)

    def update_token(self, token: str) -> None:
        self._token = token
        self._ws_connected = False
        if self._ws_feed:
            try:
                self._ws_feed.close_connection()
            except Exception:
                pass
        log.info("LiveFeed token updated — WebSocket will reconnect with new token")

    def stop(self) -> None:
        self._running = False
        self._connected = False
        self._ws_connected = False
        if self._ws_feed:
            try:
                self._ws_feed.close_connection()
            except Exception:
                pass
        log.info("LiveFeed stopped")

    def get_health_metrics(self) -> dict:
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
            "mode": self._mode,
            "ws_connected": self._ws_connected,
            "fatal_error": self._fatal_error,
            "fatal_reason": self._fatal_reason,
            "reconnect_count": self._reconnect_count,
            "total_ticks": self._tick_count,
            "seconds_since_last_tick": round(since_last, 1),
            "ticks_per_minute": round(ticks_per_min, 1),
            "subscribed_count": len(self._subscribed),
        }

    def reset_tick_window(self) -> None:
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
