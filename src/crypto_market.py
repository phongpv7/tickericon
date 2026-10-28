import threading
import time
from dataclasses import dataclass
from typing import Dict, Tuple

from curl_cffi import requests

from src.price_stream import PriceStream

# Yahoo Finance crypto symbols end in '-USD' (e.g. 'BTC-USD'); Binance quotes
# the same coins against USDT, a dollar stablecoin
CRYPTO_SUFFIX = '-USD'

# Binance's statistics for the current day; timeZone=7 starts the day at
# midnight Vietnam time, so the change matches the local trading day
TICKER_URL = 'https://api.binance.com/api/v3/ticker/tradingDay?symbol={pair}&timeZone=7'

# Binance's market-data-only websocket endpoint, pushing every trade as it happens
STREAM_URL = 'wss://data-stream.binance.vision/ws'

# Seconds between REST refreshes of the day's open, high, and low while
# trades stream in; while the stream is down, REST serves the prices too,
# polled at most every POLL_INTERVAL_S
DAY_STATS_INTERVAL_S = 60
POLL_INTERVAL_S = 1


@dataclass
class CryptoQuote:
    """Latest Binance price and day statistics for one coin, in USD."""

    price: float
    day_open: float
    day_high: float
    day_low: float


def is_crypto_symbol(symbol: str) -> bool:
    """
    Args:
        symbol (str): A configured ticker symbol.

    Returns:
        bool: True for Yahoo Finance style crypto symbols, e.g. 'BTC-USD'.
    """
    return symbol.upper().endswith(CRYPTO_SUFFIX)


class CryptoTicker:
    """
    Realtime crypto prices from Binance's public market data, which needs no
    account: every trade streams in over a websocket, while the day's open,
    high, and low come from the REST ticker, which also stands in for the
    stream whenever it is down.
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
            subscribe_message=lambda topic: {'method': 'SUBSCRIBE', 'params': [topic], 'id': 1},
            on_message=self._on_message,
            heartbeat={'method': 'LIST_SUBSCRIPTIONS', 'id': 2},
        )
        # Last streamed trade price per pair, written on the stream's thread
        self._trades: Dict[str, float] = {}
        self._trades_lock = threading.Lock()
        # Last REST quote per pair, with when it was fetched (time.monotonic())
        self._day_quotes: Dict[str, Tuple[CryptoQuote, float]] = {}

    def fetch(self, symbol: str) -> CryptoQuote:
        """
        Returns the latest quote for one coin: its last streamed trade, with
        the day's statistics refreshed from REST every DAY_STATS_INTERVAL_S.

        Args:
            symbol (str): A crypto symbol, e.g. 'BTC-USD'.

        Returns:
            CryptoQuote: The latest price and the current day's statistics.

        Raises:
            Exception: When Binance can't provide the quote, e.g. on network
                errors, or HTTP 400 for a coin Binance doesn't list.
        """
        pair = symbol[:-len(CRYPTO_SUFFIX)].upper() + 'USDT'
        with self._trades_lock:
            price = self._trades.get(pair) if self._stream.live else None
        day, fetched_at = self._day_quotes.get(pair, (None, float('-inf')))
        age = time.monotonic() - fetched_at
        if day is None or age >= DAY_STATS_INTERVAL_S or (price is None and age >= POLL_INTERVAL_S):
            try:
                day = self._fetch_day(pair)
            except Exception:
                if day is None or price is None:
                    raise
                # The stream still has the price; retry the statistics later
            self._day_quotes[pair] = (day, time.monotonic())
            # Listed on Binance, so its trades can stream from now on
            self._stream.subscribe(pair.lower() + '@aggTrade')

        if price is None:
            return day
        return CryptoQuote(
            price=price,
            day_open=day.day_open,
            day_high=max(day.day_high, price),
            day_low=min(day.day_low, price),
        )

    def close(self) -> None:
        """Stops the trade stream."""
        self._stream.close()

    def _fetch_day(self, pair: str) -> CryptoQuote:
        """
        Fetches one pair's REST ticker for the current Vietnam day.

        Args:
            pair (str): A Binance pair, e.g. 'BTCUSDT'.

        Returns:
            CryptoQuote: The last price and the day's statistics.

        Raises:
            Exception: On network or HTTP errors.
        """
        if self._session is None:
            self._session = requests.Session(impersonate='chrome')

        response = self._session.get(TICKER_URL.format(pair=pair), timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        return CryptoQuote(
            price=float(data['lastPrice']),
            day_open=float(data['openPrice']),
            day_high=float(data['highPrice']),
            day_low=float(data['lowPrice']),
        )

    def _on_message(self, message: dict) -> None:
        """
        Records each streamed trade's price; called on the stream's thread.

        Args:
            message (dict): A message from the stream.
        """
        if message.get('e') == 'aggTrade':
            with self._trades_lock:
                self._trades[message['s']] = float(message['p'])
