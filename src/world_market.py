import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

from curl_cffi import requests

from src.price_stream import PriceStream
from src.vn_market import VN_TZ

# Binance quotes every asset against USDT, a dollar stablecoin
QUOTE_ASSET = 'USDT'

# Binance's spot market lists crypto; its USDⓈ-M futures market adds
# perpetual contracts on gold, oil, and US and Asian stocks, which trade
# around the clock
SPOT = 'spot'
FUTURES = 'futures'

SPOT_API = 'https://api.binance.com/api/v3'
FUTURES_API = 'https://fapi.binance.com/fapi/v1'

# Market-data-only websocket endpoints, pushing every trade as it happens
STREAM_URLS = {
    SPOT: 'wss://data-stream.binance.vision/ws',
    FUTURES: 'wss://fstream.binance.com/market/ws',
}

# USDT trades slightly off the dollar, so prices are converted to dollars
# with the USDC/USDT rate; USDC tracks the dollar closely
USD_RATE_PAIR = 'USDCUSDT'

# Seconds between REST refreshes of each pair's open, high, and low for the
# day (and of the dollar rate) while its trades stream in. While a stream is
# down, REST serves the prices too, polled at most every POLL_INTERVAL_S, and
# a REST price older than PRICE_MAX_AGE_S no longer counts as current
DAY_STATS_INTERVAL_S = 60
POLL_INTERVAL_S = 1
PRICE_MAX_AGE_S = 5

# Yahoo Finance symbols from older configurations, mapped to Binance assets
_LEGACY_SYMBOLS = {
    'GC=F': 'XAU',     # gold
    'SI=F': 'XAG',     # silver
    'PL=F': 'XPT',     # platinum
    'PA=F': 'XPD',     # palladium
    'HG=F': 'COPPER',
    'CL=F': 'CL',      # WTI crude
    'BZ=F': 'BZ',      # Brent crude
    'NG=F': 'NATGAS',
}


@dataclass
class WorldQuote:
    """Latest Binance price and day statistics for one asset."""

    price: float
    day_open: float  # at midnight Vietnam time
    day_high: float
    day_low: float


@dataclass
class _Pair:
    """What is known about one Binance pair, kept between fetches."""

    market: Optional[str] = None      # SPOT or FUTURES, once found
    day: Optional[WorldQuote] = None  # the latest REST quote, in USDT
    day_at: float = float('-inf')     # when it arrived (time.monotonic())
    asked_at: float = float('-inf')   # when REST was last asked
    listed: bool = True               # False once Binance rejected the pair


def base_asset(symbol: str) -> str:
    """
    Args:
        symbol (str): A configured symbol for a market outside Vietnam: a
            Binance asset such as 'BTC' or 'XAU', optionally with its quote
            asset ('BTCUSDT'), or an older Yahoo Finance symbol ('BTC-USD', 'GC=F').

    Returns:
        str: The Binance asset, e.g. 'BTC' or 'XAU'.
    """
    symbol = symbol.upper()
    if symbol in _LEGACY_SYMBOLS:
        return _LEGACY_SYMBOLS[symbol]
    for suffix in ('-USD', QUOTE_ASSET):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return symbol[:-len(suffix)]
    return symbol


class WorldTicker:
    """
    Realtime prices for every market outside Vietnam from Binance's public
    market data, which needs no account: crypto from its spot market, and
    gold, oil, stocks, and coins it lists only there from its futures market.
    Every trade streams in over a websocket; the day's open, high, and low
    come from REST, which also stands in for a stream whenever it is down.
    The day starts at midnight Vietnam time, and prices are converted from
    USDT to US dollars.
    """

    def __init__(self, timeout: float = 10):
        """
        Args:
            timeout (float): Request timeout, in seconds.
        """
        self.timeout = timeout
        # Created on first use and reused, keeping the connection alive between fetches
        self._session = None
        self._streams = {
            market: PriceStream(
                url,
                subscribe_message=lambda topics: {'method': 'SUBSCRIBE', 'params': topics, 'id': 1},
                on_message=self._on_message,
                heartbeat={'method': 'LIST_SUBSCRIPTIONS', 'id': 2},
            )
            for market, url in STREAM_URLS.items()
        }
        # Last streamed trade per pair: its price in USDT and when it arrived
        # (time.monotonic()), written on the streams' threads
        self._trades: Dict[str, Tuple[float, float]] = {}
        self._trades_lock = threading.Lock()
        self._pairs: Dict[str, _Pair] = {}
        # USDT per dollar, and when it was fetched (time.monotonic())
        self._usd_rate: Tuple[float, float] = (1.0, float('-inf'))

    def fetch(self, symbol: str) -> WorldQuote:
        """
        Returns the latest quote for one asset, in dollars: its last streamed
        trade, with the day's statistics refreshed from REST every
        DAY_STATS_INTERVAL_S.

        Args:
            symbol (str): A configured symbol, e.g. 'BTC' or 'XAU' (see base_asset).

        Returns:
            WorldQuote: The latest price and the current day's statistics.

        Raises:
            LookupError: When Binance has no current quote: it doesn't list
                the asset, or it can't be reached.
        """
        name = base_asset(symbol) + QUOTE_ASSET
        pair = self._pairs.setdefault(name, _Pair())
        price = self._streamed_price(name, pair.market)

        now = time.monotonic()
        # Without a streamed price REST stands in, so it is asked more often
        interval = POLL_INTERVAL_S if price is None and pair.listed else DAY_STATS_INTERVAL_S
        if now - pair.asked_at >= interval:
            pair.asked_at = now
            self._refresh_day(name, pair)

        if pair.day is None or (price is None and time.monotonic() - pair.day_at > PRICE_MAX_AGE_S):
            raise LookupError(f"No current Binance quote for {name}")
        if price is None:
            price = pair.day.price
        rate = self._usdt_per_usd()
        return WorldQuote(
            price=price / rate,
            day_open=pair.day.day_open / rate,
            day_high=max(pair.day.day_high, price) / rate,
            day_low=min(pair.day.day_low, price) / rate,
        )

    def is_futures(self, symbol: str) -> bool:
        """
        Args:
            symbol (str): A configured symbol, e.g. 'XAU'.

        Returns:
            bool: True once its prices are known to come from the futures market.
        """
        pair = self._pairs.get(base_asset(symbol) + QUOTE_ASSET)
        return pair is not None and pair.market == FUTURES

    @property
    def usdt_per_usd(self) -> float:
        """
        Returns:
            float: How many USDT a dollar buys, as last fetched (1 until then);
                reading it never sends a request, so any thread may.
        """
        return self._usd_rate[0]

    def close(self) -> None:
        """Stops the trade streams."""
        for stream in self._streams.values():
            stream.close()

    def _streamed_price(self, name: str, market: Optional[str]) -> Optional[float]:
        """
        Args:
            name (str): The Binance pair, e.g. 'BTCUSDT'.
            market (Optional[str]): The pair's market, if already known.

        Returns:
            Optional[float]: The pair's last trade price in USDT, if its stream
                is up and has carried one since it last connected.
        """
        if market is None:
            return None
        stream = self._streams[market]
        with self._trades_lock:
            trade = self._trades.get(name)
        if trade is None or not stream.live or trade[1] < stream.connected_at:
            return None
        return trade[0]

    def _refresh_day(self, name: str, pair: _Pair) -> None:
        """
        Refreshes a pair's day statistics from REST, subscribing to its trades
        once Binance lists it. Failures keep the last known statistics.

        Args:
            name (str): The Binance pair, e.g. 'BTCUSDT'.
            pair (_Pair): What is known about it, updated in place.
        """
        try:
            pair.market, pair.day = self._fetch_day(name, pair.market)
        except LookupError:
            pair.listed = False
            return
        except Exception:
            return  # unreachable: retried after the next interval
        pair.day_at = time.monotonic()
        pair.listed = True
        self._streams[pair.market].subscribe(name.lower() + '@aggTrade')

    def _fetch_day(self, name: str, market: Optional[str]) -> Tuple[str, WorldQuote]:
        """
        Fetches a pair's day statistics, finding its market on first use:
        spot when Binance lists the pair there, otherwise futures.

        Args:
            name (str): The Binance pair, e.g. 'XAUUSDT'.
            market (Optional[str]): The pair's market, if already known.

        Returns:
            Tuple[str, WorldQuote]: The pair's market and REST quote, in USDT.

        Raises:
            LookupError: When Binance lists the pair on neither market.
            Exception: On network or other HTTP errors.
        """
        if market != FUTURES:
            day = self._fetch_spot_day(name)
            if day is not None:
                return SPOT, day
        day = self._fetch_futures_day(name)
        if day is None:
            raise LookupError(f"Binance doesn't list {name}")
        return FUTURES, day

    def _fetch_spot_day(self, name: str) -> Optional[WorldQuote]:
        """
        Args:
            name (str): The Binance pair, e.g. 'BTCUSDT'.

        Returns:
            Optional[WorldQuote]: Its spot ticker for the current Vietnam day,
                in USDT, or None when the spot market doesn't list it.
        """
        # timeZone=7 starts the day at midnight Vietnam time
        response = self._get(f'{SPOT_API}/ticker/tradingDay?symbol={name}&timeZone=7')
        if response is None:
            return None
        data = response.json()
        return WorldQuote(
            price=float(data['lastPrice']),
            day_open=float(data['openPrice']),
            day_high=float(data['highPrice']),
            day_low=float(data['lowPrice']),
        )

    def _fetch_futures_day(self, name: str) -> Optional[WorldQuote]:
        """
        Args:
            name (str): The Binance pair, e.g. 'XAUUSDT'.

        Returns:
            Optional[WorldQuote]: Its futures statistics for the current
                Vietnam day, in USDT, or None when the futures market doesn't
                list it.
        """
        # Futures have no daily ticker by time zone, but Vietnam is a whole
        # number of hours from UTC, so hourly candles since midnight add up to the day
        midnight = datetime.now(VN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        response = self._get(f'{FUTURES_API}/klines?symbol={name}&interval=1h'
                             f'&startTime={int(midnight.timestamp() * 1000)}&limit=24')
        if response is None:
            return None
        candles = response.json()  # per hour: [open time, open, high, low, close, ...]
        return WorldQuote(
            price=float(candles[-1][4]),
            day_open=float(candles[0][1]),
            day_high=max(float(candle[2]) for candle in candles),
            day_low=min(float(candle[3]) for candle in candles),
        )

    def _usdt_per_usd(self) -> float:
        """
        Returns:
            float: How many USDT a dollar buys (the USDC/USDT price),
                refreshed every DAY_STATS_INTERVAL_S; 1 until first fetched.
        """
        rate, fetched_at = self._usd_rate
        if time.monotonic() - fetched_at >= DAY_STATS_INTERVAL_S:
            try:
                rate = float(self._get(f'{SPOT_API}/ticker/price?symbol={USD_RATE_PAIR}').json()['price'])
            except Exception:
                pass  # keep the last known rate until the next refresh
            self._usd_rate = (rate, time.monotonic())
        return rate

    def _get(self, url: str):
        """
        Args:
            url (str): A Binance REST URL.

        Returns:
            The response, or None when Binance rejects the pair (HTTP 400,
            'Invalid symbol').

        Raises:
            Exception: On network or other HTTP errors.
        """
        if self._session is None:
            self._session = requests.Session(impersonate='chrome')
        response = self._session.get(url, timeout=self.timeout)
        if response.status_code == 400:
            return None
        response.raise_for_status()
        return response

    def _on_message(self, message: dict) -> None:
        """
        Records each streamed trade's price; called on the streams' threads.

        Args:
            message (dict): A message from a stream.
        """
        if message.get('e') == 'aggTrade':
            with self._trades_lock:
                self._trades[message['s']] = (float(message['p']), time.monotonic())
