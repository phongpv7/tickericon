import threading
import time
from typing import Callable

from curl_cffi.requests import WebSocket, WebSocketTimeout

# Seconds between heartbeat messages, which the server answers, so a
# connection that died without closing is noticed
HEARTBEAT_S = 20

# A connection that hears nothing for this long, not even a heartbeat reply, is reopened
SILENCE_S = 60

# Seconds to wait before reconnecting, doubling while the server stays unreachable
RETRY_MIN_S = 1
RETRY_MAX_S = 60


class PriceStream:
    """
    Keeps a websocket market data feed open on a background thread: it
    subscribes to the requested topics, hands every message to a callback,
    and reconnects (subscribing again) whenever the connection fails or goes
    silent.
    """

    def __init__(self, url: str, subscribe_message: Callable[[str], dict],
                 on_message: Callable[[dict], None], heartbeat: dict):
        """
        Args:
            url (str): The websocket URL.
            subscribe_message (Callable[[str], dict]): Builds the message that
                subscribes to one topic.
            on_message (Callable[[dict], None]): Receives every message, parsed
                from JSON; called on the stream's thread.
            heartbeat (dict): A message the server answers, sent every HEARTBEAT_S.
        """
        self.url = url
        self.subscribe_message = subscribe_message
        self.on_message = on_message
        self.heartbeat = heartbeat
        # Guards _topics and _thread, which subscribe() changes from other threads
        self._lock = threading.Lock()
        self._topics = set()
        self._thread = None
        self._closed = threading.Event()
        # When the current connection last heard from the server (time.monotonic())
        self._heard_at = float('-inf')

    @property
    def live(self) -> bool:
        """
        Returns:
            bool: True while connected and hearing from the server, so the
                values it streamed are current.
        """
        return time.monotonic() - self._heard_at < SILENCE_S

    def subscribe(self, topic: str) -> None:
        """
        Adds a topic to stream, opening the connection on first use.

        Args:
            topic (str): The topic, in the form subscribe_message expects.
        """
        with self._lock:
            self._topics.add(topic)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def close(self) -> None:
        """Stops the stream; its connection closes within a second."""
        self._closed.set()

    def _run(self) -> None:
        """Streams until closed, reconnecting whenever a connection ends."""
        retry_s = RETRY_MIN_S
        while not self._closed.is_set():
            started_at = time.monotonic()
            try:
                self._stream()
            except Exception:
                pass  # unreachable, refused, or dropped: reconnect below
            # A connection that worked reconnects right away; back off while
            # the server stays unreachable
            heard = self._heard_at >= started_at
            self._heard_at = float('-inf')
            retry_s = RETRY_MIN_S if heard else min(retry_s * 2, RETRY_MAX_S)
            self._closed.wait(retry_s)

    def _stream(self) -> None:
        """Runs one connection until it fails, goes silent, or the stream is closed."""
        ws = WebSocket()
        try:
            ws.connect(self.url, impersonate='chrome', timeout=10)
            subscribed = set()
            heard_at = sent_at = time.monotonic()
            while not self._closed.is_set():
                with self._lock:
                    new_topics = self._topics - subscribed
                for topic in new_topics:
                    ws.send_json(self.subscribe_message(topic))
                    subscribed.add(topic)

                # A short wait keeps new topics and close() responsive
                try:
                    message = ws.recv_json(timeout=1)
                except WebSocketTimeout:
                    message = None
                now = time.monotonic()
                if message is not None:
                    heard_at = self._heard_at = now
                    self.on_message(message)
                elif now - heard_at >= SILENCE_S:
                    return

                if now - sent_at >= HEARTBEAT_S:
                    ws.send_json(self.heartbeat)
                    sent_at = now
        finally:
            ws.close()
