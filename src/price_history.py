import calendar
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import List

from curl_cffi import requests

from src.vn_market import VN_SUFFIX, VN_TZ, is_vn_symbol
from src.world_market import QUOTE_ASSET, base_asset

# TradingView-style daily history behind the VPS price board, prices in thousands of VND
VPS_HISTORY_URL = 'https://histdatafeed.vps.com.vn/tradingview/history?symbol={code}&resolution=D&from={start}&to={end}'

# Binance daily candles: the spot market, or else the USDⓈ-M futures market
BINANCE_KLINES_URLS = (
    'https://api.binance.com/api/v3/klines?symbol={pair}&interval=1d&limit={days}',
    'https://fapi.binance.com/fapi/v1/klines?symbol={pair}&interval=1d&limit={days}',
)

# Calendar days of daily candles to fetch: a little over a year, enough for
# the 200-day average and the 52-week range
HISTORY_DAYS = 400

_DAY_S = 24 * 60 * 60


@dataclass
class Candles:
    """Daily candles, oldest first, each stamped with its day's midnight (UTC seconds)."""

    times: List[int]
    opens: List[float]
    highs: List[float]
    lows: List[float]
    closes: List[float]
    volumes: List[float]

    def __len__(self) -> int:
        return len(self.times)


def fetch_history(symbol: str, usdt_per_usd: float = 1.0, futures: bool = False, timeout: float = 10) -> Candles:
    """
    Fetches about a year of daily candles for one symbol: Vietnamese stocks
    from the VPS price board, in VND (adjusted for dividends and splits), and
    everything else from Binance, converted to dollars.

    Args:
        symbol (str): A tracked symbol, e.g. 'VHM.VN' or 'XAU'.
        usdt_per_usd (float): How many USDT a dollar buys, for Binance prices.
        futures (bool): Whether Binance lists the symbol on its futures
            market rather than spot, when that's known; saves a request.
        timeout (float): Request timeout, in seconds.

    Returns:
        Candles: The daily candles, oldest first.

    Raises:
        Exception: On network or HTTP errors, or LookupError when there is
            no history for the symbol.
    """
    if is_vn_symbol(symbol):
        return _vps_history(symbol[:-len(VN_SUFFIX)].upper(), timeout)
    urls = BINANCE_KLINES_URLS[::-1] if futures else BINANCE_KLINES_URLS
    return _binance_history(base_asset(symbol) + QUOTE_ASSET, usdt_per_usd, urls, timeout)


def with_live_price(candles: Candles, symbol: str, price: float) -> Candles:
    """
    Brings today's candle up to the latest streamed price, which the history
    feeds lag behind.

    Args:
        candles (Candles): The fetched daily candles.
        symbol (str): Their symbol, which decides when its day starts:
            midnight Vietnam time on the price board, midnight UTC on Binance.
        price (float): The latest price.

    Returns:
        Candles: A copy whose last candle closes at the price, if that candle
            is today's; otherwise the candles unchanged.
    """
    if not candles or price <= 0:
        return candles
    if is_vn_symbol(symbol):
        today = calendar.timegm(datetime.now(VN_TZ).date().timetuple())
    else:
        today = int(time.time()) // _DAY_S * _DAY_S
    if candles.times[-1] != today:
        return candles
    return replace(
        candles,
        highs=candles.highs[:-1] + [max(candles.highs[-1], price)],
        lows=candles.lows[:-1] + [min(candles.lows[-1], price)],
        closes=candles.closes[:-1] + [price],
    )


def _vps_history(code: str, timeout: float) -> Candles:
    """
    Args:
        code (str): A Vietnamese stock code, e.g. 'VHM'.
        timeout (float): Request timeout, in seconds.

    Returns:
        Candles: Its daily candles from the VPS price board, in VND.
    """
    end = int(time.time())
    response = requests.get(VPS_HISTORY_URL.format(code=code, start=end - HISTORY_DAYS * _DAY_S, end=end),
                            impersonate='chrome', timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if data.get('s') != 'ok' or not data.get('t'):
        raise LookupError(f"No price history for {code}")

    def in_vnd(prices):
        return [float(price) * 1000 for price in prices]

    return Candles(
        times=[int(t) for t in data['t']],
        opens=in_vnd(data['o']),
        highs=in_vnd(data['h']),
        lows=in_vnd(data['l']),
        closes=in_vnd(data['c']),
        volumes=[float(volume) for volume in data['v']],
    )


def _binance_history(pair: str, usdt_per_usd: float, urls, timeout: float) -> Candles:
    """
    Args:
        pair (str): A Binance pair, e.g. 'BTCUSDT'.
        usdt_per_usd (float): How many USDT a dollar buys.
        urls (Tuple[str, ...]): The markets' candle URLs, the likelier first.
        timeout (float): Request timeout, in seconds.

    Returns:
        Candles: Its daily candles in dollars, from the first market that
            lists the pair.
    """
    for url in urls:
        response = requests.get(url.format(pair=pair, days=HISTORY_DAYS), impersonate='chrome', timeout=timeout)
        if response.status_code == 400:  # not listed on this market
            continue
        response.raise_for_status()
        rows = response.json()  # per day: [open time (ms), open, high, low, close, volume, ...]
        if not rows:
            break

        def in_usd(column):
            return [float(row[column]) / usdt_per_usd for row in rows]

        return Candles(
            times=[int(row[0]) // 1000 for row in rows],
            opens=in_usd(1),
            highs=in_usd(2),
            lows=in_usd(3),
            closes=in_usd(4),
            volumes=[float(row[5]) for row in rows],
        )
    raise LookupError(f"No Binance price history for {pair}")
