import bisect
import calendar
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import List

from curl_cffi import requests

from src.vn_market import VN_SUFFIX, VN_TZ, is_vn_symbol

# TradingView-style daily history behind the VPS price board, prices in thousands of VND
VPS_HISTORY_URL = 'https://histdatafeed.vps.com.vn/tradingview/history?symbol={code}&resolution=D&from={start}&to={end}'

# Calendar days of daily candles to fetch: a little over a year, enough for
# the 200-day average and the 52-week range
HISTORY_DAYS = 400

# Calendar days fetched to bring the latest candles up to date; a week
# spans weekends and short holidays
RECENT_DAYS = 7

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


def fetch_history(symbol: str, days: int = HISTORY_DAYS, timeout: float = 10) -> Candles:
    """
    Fetches a Vietnamese stock's daily candles from the VPS price board, in
    VND, adjusted for dividends and splits.

    Args:
        symbol (str): A '.VN' symbol, e.g. 'VHM.VN'.
        days (int): How many calendar days back to fetch.
        timeout (float): Request timeout, in seconds.

    Returns:
        Candles: The daily candles, oldest first.

    Raises:
        Exception: On network or HTTP errors, or LookupError when there is
            no history for the symbol.
    """
    if not is_vn_symbol(symbol):
        raise LookupError(f"No price history for {symbol}: only Vietnamese stocks have it")
    code = symbol[:-len(VN_SUFFIX)].upper()
    end = int(time.time())
    response = requests.get(VPS_HISTORY_URL.format(code=code, start=end - days * _DAY_S, end=end),
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


def merge_candles(history: Candles, recent: Candles) -> Candles:
    """
    Args:
        history (Candles): Candles fetched earlier.
        recent (Candles): The latest few candles, fetched since.

    Returns:
        Candles: The history up to where the recent candles start, then the
            recent candles, which replace any for the same days.
    """
    if not recent:
        return history
    keep = bisect.bisect_left(history.times, recent.times[0])
    return Candles(
        times=history.times[:keep] + recent.times,
        opens=history.opens[:keep] + recent.opens,
        highs=history.highs[:keep] + recent.highs,
        lows=history.lows[:keep] + recent.lows,
        closes=history.closes[:keep] + recent.closes,
        volumes=history.volumes[:keep] + recent.volumes,
    )


def with_live_price(candles: Candles, price: float, volume: float = 0.0) -> Candles:
    """
    Brings today's candle up to the latest streamed price and volume, which
    the history feed lags behind.

    Args:
        candles (Candles): The fetched daily candles.
        price (float): The latest price.
        volume (float): The shares traded today so far, or 0 if unknown.

    Returns:
        Candles: A copy whose last candle closes at the price, if that candle
            is today's (in Vietnam); otherwise the candles unchanged.
    """
    if not candles or price <= 0:
        return candles
    if candles.times[-1] != calendar.timegm(datetime.now(VN_TZ).date().timetuple()):
        return candles
    return replace(
        candles,
        highs=candles.highs[:-1] + [max(candles.highs[-1], price)],
        lows=candles.lows[:-1] + [min(candles.lows[-1], price)],
        closes=candles.closes[:-1] + [price],
        volumes=candles.volumes[:-1] + [max(candles.volumes[-1], volume)],
    )
