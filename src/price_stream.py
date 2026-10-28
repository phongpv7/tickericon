import json
import threading
import time
from typing import Callable, List, Optional

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

    def __init__(self, url: str, subscribe_message: Callable[[List[str]], object],
                 on_message: Callable, heartbeat: Optional[dict] = None):
        """
        Args:
            url (str): The websocket URL.
            subscribe_message (Callable[[List[str]], object]): Builds the
                message that subscribes to a list of topics; one message covers
                every new topic, as servers cap how many messages a client may send.
            on_message (Callable): Receives every message, parsed from JSON;
                called on the stream's thread.
            heartbeat (Optional[dict]): A message the server answers, sent every HEARTBEAT_S.
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
        # When the current connection opened (infinity until it has); values
        # streamed before it are older than whatever was missed while reconnecting
        self.connected_at = float('inf')

    @property
    def live(self) -> bool:
        """
        Returns:
            bool: True while connected and hearing from the server, so the
                values it streamed since connected_at are current.
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
            self.connected_at = float('inf')
            retry_s = RETRY_MIN_S if heard else min(retry_s * 2, RETRY_MAX_S)
            self._closed.wait(retry_s)

    def _stream(self) -> None:
        """Runs one connection until it fails, goes silent, or the stream is closed."""
        ws = WebSocket()
        try:
            ws.connect(self.url, impersonate='chrome', timeout=10)
            subscribed = set()
            heard_at = sent_at = self.connected_at = time.monotonic()
            while not self._closed.is_set():
                with self._lock:
                    new_topics = sorted(self._topics - subscribed)
                if new_topics:
                    ws.send_json(self.subscribe_message(new_topics))
                    subscribed.update(new_topics)

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

                if self.heartbeat is not None and now - sent_at >= HEARTBEAT_S:
                    ws.send_json(self.heartbeat)
                    sent_at = now
        finally:
            ws.close()


class SocketIOStream(PriceStream):
    """
    A PriceStream speaking Socket.IO (Engine.IO 4) over its websocket
    transport: it connects to the default namespace before subscribing,
    answers the server's pings, and hands on_message each event's name and
    first argument.
    """

    def __init__(self, url: str, subscribe_message: Callable[[List[str]], list],
                 on_message: Callable[[str, object], None]):
        """
        Args:
            url (str): The Socket.IO websocket URL, e.g. ending in
                '/socket.io/?EIO=4&transport=websocket'.
            subscribe_message (Callable[[List[str]], list]): Builds the event,
                as [name, *arguments], that subscribes to a list of topics.
            on_message (Callable[[str, object], None]): Receives every event's
                name and first argument; called on the stream's thread.
        """
        super().__init__(url, subscribe_message, on_message)

    def _stream(self) -> None:
        """Runs one connection until it fails, goes silent, or the stream is closed."""
        ws = WebSocket()
        try:
            ws.connect(self.url, impersonate='chrome', timeout=10)
            # Topics can only be subscribed once the namespace connection is confirmed
            subscribed = None
            heard_at = time.monotonic()
            while not self._closed.is_set():
                if subscribed is not None:
                    with self._lock:
                        new_topics = sorted(self._topics - subscribed)
                    if new_topics:
                        ws.send_str('42' + json.dumps(self.subscribe_message(new_topics)))
                        subscribed.update(new_topics)

                try:
                    packet = ws.recv_str(timeout=1)
                except WebSocketTimeout:
                    packet = None
                now = time.monotonic()
                if packet is None:
                    # The server pings every half minute or so
                    if now - heard_at >= SILENCE_S:
                        return
                    continue
                heard_at = self._heard_at = now

                if packet.startswith('0'):  # transport open: connect to the default namespace
                    ws.send_str('40')
                elif packet == '2':  # ping
                    ws.send_str('3')
                elif packet.startswith('40'):  # namespace connected
                    subscribed = set()
                    self.connected_at = now
                elif packet.startswith('42'):  # event: [name, *arguments]
                    event, *args = json.loads(packet[2:])
                    self.on_message(event, args[0] if args else None)
                elif packet.startswith(('1', '41', '44')):  # closed, disconnected, or refused
                    return
        finally:
            ws.close()
