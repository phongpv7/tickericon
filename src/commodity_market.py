import threading
import time
from dataclasses import dataclass
from typing import Dict, Tuple

from curl_cffi import requests

from src.price_stream import PriceStream

# Yahoo Finance futures symbols (which Yahoo delays by 10 minutes) mapped to
# the perpetual contracts on Hyperliquid's trade.xyz exchange, whose oracle
# prices track the same markets in realtime
DEX = 'xyz'
COINS = {
    'GC=F': 'xyz:GOLD',
    'SI=F': 'xyz:SILVER',
    'HG=F': 'xyz:COPPER',
    'PL=F': 'xyz:PLATINUM',
    'PA=F': 'xyz:PALLADIUM',
    'CL=F': 'xyz:CL',
    'BZ=F': 'xyz:BRENTOIL',
    'NG=F': 'xyz:NATGAS',
    'HO=F': 'xyz:HO',
}

INFO_URL = 'https://api.hyperliquid.xyz/info'
STREAM_URL = 'wss://api.hyperliquid.xyz/ws'

# Each IP may spend 1200 request weight per minute, 20 per asset contexts
# request, so while the stream is down REST is polled at most this often
POLL_INTERVAL_S = 5

# Seconds between refreshes of the 24-hour high and low, from hourly candles
RANGE_INTERVAL_S = 60
_DAY_MS = 24 * 60 * 60 * 1000


@dataclass
class CommodityQuote:
    """Latest Hyperliquid price and 24-hour statistics for one commodity, in USD."""

    price: float
    prev_day: float  # the price 24 hours ago
    day_high: float
    day_low: float


def is_commodity_symbol(symbol: str) -> bool:
    """
    Args:
        symbol (str): A configured ticker symbol.

    Returns:
        bool: True for commodity futures served by Hyperliquid, e.g. 'GC=F'.
    """
    return symbol.upper() in COINS


class CommodityTicker:
    """
    Realtime commodity prices from Hyperliquid's public market data, which
    needs no account: each contract's oracle price, which follows the
    underlying market, streams in over a websocket along with its price 24
    hours ago, and the REST API stands in whenever the stream is down.
    """

    def __init__(self, timeout: float = 10):
        """
        Args:
            timeout (float): Request timeout, in seconds.
        """
        self.timeout = timeout
        # Created on first use and reused, keeping the connection alive between fetches
        self._session = None
        self._stream = PriceStream(
            STREAM_URL,
            subscribe_message=lambda coin: {
                'method': 'subscribe', 'subscription': {'type': 'activeAssetCtx', 'coin': coin},
            },
            on_message=self._on_message,
            heartbeat={'method': 'ping'},
        )
        # Latest asset context per coin from the stream, written on its thread
        self._streamed: Dict[str, dict] = {}
        self._streamed_lock = threading.Lock()
        # Asset contexts from the last REST poll, and when it was sent (time.monotonic())
        self._polled: Dict[str, dict] = {}
        self._polled_at = float('-inf')
        # 24-hour (high, low, fetched at) per coin
        self._ranges: Dict[str, Tuple[float, float, float]] = {}

    def fetch(self, symbol: str) -> CommodityQuote:
        """
        Returns the latest quote for one commodity.

        Args:
            symbol (str): A mapped futures symbol, e.g. 'GC=F'.

        Returns:
            CommodityQuote: The oracle price and its 24-hour statistics.

        Raises:
            Exception: When Hyperliquid can't provide the quote, e.g. on
                network errors or while the stream is down between polls.
        """
        coin = COINS[symbol.upper()]
        self._stream.subscribe(coin)
        with self._streamed_lock:
            context = self._streamed.get(coin) if self._stream.live else None
        if context is None:
            context = self._poll(coin)

        price = float(context['oraclePx'])
        high, low = self._day_range(coin, price)
        return CommodityQuote(price=price, prev_day=float(context['prevDayPx']), day_high=high, day_low=low)

    def close(self) -> None:
        """Stops the price stream."""
        self._stream.close()

    def _poll(self, coin: str) -> dict:
        """
        Fetches every trade.xyz asset context in one REST request, at most
        once per POLL_INTERVAL_S.

        Args:
            coin (str): A Hyperliquid coin, e.g. 'xyz:GOLD'.

        Returns:
            dict: The coin's asset context.

        Raises:
            Exception: On network or HTTP errors, until the next poll.
        """
        if time.monotonic() - self._polled_at >= POLL_INTERVAL_S:
            self._polled_at = time.monotonic()
            self._polled = {}
            meta, contexts = self._post({'type': 'metaAndAssetCtxs', 'dex': DEX})
            self._polled = {asset['name']: context for asset, context in zip(meta['universe'], contexts)}
        return self._polled[coin]

    def _day_range(self, coin: str, price: float) -> Tuple[float, float]:
        """
        Args:
            coin (str): A Hyperliquid coin, e.g. 'xyz:GOLD'.
            price (float): The latest price, which may be a new high or low.

        Returns:
            Tuple[float, float]: The 24-hour high and low, refreshed every
                RANGE_INTERVAL_S; just the price until they are known.
        """
        high, low, fetched_at = self._ranges.get(coin, (price, price, float('-inf')))
        if time.monotonic() - fetched_at >= RANGE_INTERVAL_S:
            try:
                now_ms = int(time.time() * 1000)
                candles = self._post({'type': 'candleSnapshot', 'req': {
                    'coin': coin, 'interval': '1h', 'startTime': now_ms - _DAY_MS, 'endTime': now_ms,
                }})
                high = max(float(candle['h']) for candle in candles)
                low = min(float(candle['l']) for candle in candles)
            except Exception:
                pass  # keep the last known range until the next refresh
            self._ranges[coin] = (high, low, time.monotonic())
        return max(high, price), min(low, price)

    def _post(self, body: dict):
        """
        Args:
            body (dict): An info request.

        Returns:
            The decoded JSON response.

        Raises:
            Exception: On network or HTTP errors.
        """
        if self._session is None:
            self._session = requests.Session(impersonate='chrome')
        response = self._session.post(INFO_URL, json=body, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def _on_message(self, message: dict) -> None:
        """
        Records each streamed asset context; called on the stream's thread.

        Args:
            message (dict): A message from the stream.
        """
        if message.get('channel') == 'activeAssetCtx':
            data = message['data']
            with self._streamed_lock:
                self._streamed[data['coin']] = data['ctx']
