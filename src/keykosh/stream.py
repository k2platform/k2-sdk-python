"""Server-Sent Events reader for the config change stream.

``GET /api/config/token/**/stream`` (OFFLINE_AND_HOTRELOAD_DESIGN.md §4).

SSE over ``urllib`` rather than a STOMP/WebSocket client: a WS client would take the Python
SDK from one dependency to two. SSE needs nothing beyond the standard library, works through
ALBs and proxies, and auto-reconnect is part of the protocol.

The stream carries a *signal*, not values — the caller re-fetches ``/current`` on each event.
A reconnect therefore self-heals a client that missed events while disconnected.
"""

from __future__ import annotations

import json
import random
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

_BACKOFF_MIN_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0

#: Socket read timeout. The read loop wakes this often to re-check the stop flag, which is
#: what makes ``stop()`` prompt without closing a file object another thread is reading —
#: doing that deadlocks inside ``http.client``.
_READ_TIMEOUT_SECONDS = 5.0

#: With the server's 20s heartbeat, three missed intervals means the connection is a
#: blackhole (a proxy dropped it without an EOF) — reconnect rather than wait forever.
_LIVENESS_TIMEOUT_SECONDS = 65.0


class ConfigStream:
    """A background thread holding one SSE connection open, reconnecting with backoff."""

    def __init__(
        self,
        url: str,
        token: Callable[[], str],
        on_event: Callable[[dict], None],
        on_unavailable: Callable[[str], None],
        on_retry: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._url = url
        self._token = token
        self._on_event = on_event
        self._on_unavailable = on_unavailable
        self._on_retry = on_retry
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="k2-config-stream", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker to exit. Non-blocking; the daemon thread unwinds on its next read.

        Deliberately does *not* close the response here: closing an ``http.client`` file object
        that another thread is blocked in ``readline()`` on deadlocks. The socket read timeout
        is what bounds how long the worker takes to notice.
        """
        self._stop.set()

    def _run(self) -> None:
        attempt = 0
        ever_connected = False
        while not self._stop.is_set():
            try:
                response = self._connect()
            except urllib.error.HTTPError as e:
                # 401/403 means the token cannot stream; 404 means the server predates
                # /stream. Either way push is unavailable — never fail the app over it.
                if not ever_connected:
                    return self._on_unavailable(f"HTTP {e.code}")
                attempt = self._backoff(attempt)
                continue
            except Exception as e:  # noqa: BLE001
                if self._stop.is_set():
                    return
                if not ever_connected:
                    return self._on_unavailable(str(e))
                attempt = self._backoff(attempt)
                continue

            ever_connected = True
            attempt = 0
            try:
                self._read_events(response)
            except Exception:  # noqa: BLE001
                pass  # drop; reconnect below
            finally:
                try:
                    response.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.is_set():
                return
            attempt = self._backoff(attempt)

    def _connect(self):
        token = self._token()
        request = urllib.request.Request(self._url, method="GET")
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("X-API-Token", token)
        request.add_header("Accept", "text/event-stream")
        request.add_header("Cache-Control", "no-cache")
        # noqa: S310 — the URL is the caller's own platform, never a vendor host
        return urllib.request.urlopen(request, timeout=_READ_TIMEOUT_SECONDS)  # noqa: S310

    def _read_events(self, response) -> None:
        """Consume the ``text/event-stream`` framing, dispatching each ``config.changed``."""
        event = "message"
        data_lines: list[str] = []
        last_byte_at = time.monotonic()
        while not self._stop.is_set():
            try:
                raw = response.readline()
            except (TimeoutError, socket.timeout, OSError) as e:
                # A read timeout is the normal idle case — go round and re-check the stop flag.
                if isinstance(e, OSError) and not isinstance(e, (TimeoutError, socket.timeout)):
                    raise
                if time.monotonic() - last_byte_at > _LIVENESS_TIMEOUT_SECONDS:
                    return  # heartbeats stopped arriving — reconnect
                continue
            if not raw:
                return  # server closed
            last_byte_at = time.monotonic()
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                # Blank line terminates an event. A `: ping` heartbeat leaves no fields.
                # The event is a signal, so a payload-less `config.changed` still means
                # "re-fetch" — same tolerance as the Node and Java readers.
                if event == "config.changed":
                    self._dispatch("\n".join(data_lines))
                event, data_lines = "message", []
                continue
            if line.startswith(":"):
                continue  # comment heartbeat
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data_lines.append(value)

    def _dispatch(self, payload: str) -> None:
        try:
            self._on_event(json.loads(payload))
        except ValueError:
            self._on_event({})

    def _backoff(self, attempt: int) -> int:
        """Jittered exponential backoff, 1s → 30s. Returns the next attempt counter."""
        base = min(_BACKOFF_MAX_SECONDS, _BACKOFF_MIN_SECONDS * (2**attempt))
        delay = base / 2 + random.random() * (base / 2)  # noqa: S311 — jitter, not crypto
        if self._on_retry:
            self._on_retry(delay)
        self._stop.wait(delay)
        return attempt + 1
