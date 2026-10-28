from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from curl_cffi import requests

# Yahoo Finance suffix for Ho Chi Minh Stock Exchange symbols, e.g. 'VHM.VN'.
# Yahoo delays these quotes by 15 minutes, so they come from a local price board instead.
VN_SUFFIX = '.VN'

# JSON feed behind the VPS Securities web price board; takes comma-separated stock codes
BOARD_URL = 'https://bgapidatafeed.vps.com.vn/getliststockdata/{codes}'

VN_TZ = ZoneInfo('Asia/Ho_Chi_Minh')

# Exchange names for the board's market ids
_MARKET_NAMES = {'STO': 'HOSE', 'STX': 'HNX', 'UPX': 'UPCoM'}

# Weekday trading sessions in exchange-local time, mapped to the app's market states
_SESSIONS = (
    (time(9, 0), time(9, 15), 'premarket'),      # ATO opening auction
    (time(9, 15), time(11, 30), 'open'),
    (time(13, 0), time(14, 45), 'open'),         # includes the 14:30 ATC closing auction
    (time(14, 45), time(15, 0), 'after_hours'),  # negotiated deals only
)

# When trading (re)starts each weekday: the opening auction and the end of the lunch break
_SESSION_STARTS = (time(9, 0), time(13, 0))


@dataclass
class BoardQuote:
    """Latest price board values for one stock, in VND."""

    price: float
    reference: float
    ceiling: float
    floor: float
    day_high: float
    day_low: float
    exchange: str


def is_vn_symbol(symbol: str) -> bool:
    """
    Args:
        symbol (str): A configured ticker symbol.

    Returns:
        bool: True for Vietnamese symbols served by the price board, e.g. 'VHM.VN'.
    """
    return symbol.upper().endswith(VN_SUFFIX)


def session_state(now: datetime) -> str:
    """
    Resolves the market state from the exchange's fixed session schedule.
    The lunch break counts as closed; public holidays aren't tracked.

    Args:
        now (datetime): The current time (timezone-aware).

    Returns:
        str: One of 'premarket', 'open', 'after_hours', or 'closed'.
    """
    local = now.astimezone(VN_TZ)
    if local.weekday() >= 5:
        return 'closed'
    for start, end, state in _SESSIONS:
        if start <= local.time() < end:
            return state
    return 'closed'


def next_session_start(now: datetime) -> datetime:
    """
    Finds when trading next (re)starts, so the update loop can sleep
    through the lunch break, nights, and weekends.

    Args:
        now (datetime): The current time (timezone-aware).

    Returns:
        datetime: The next session start in UTC.
    """
    local = now.astimezone(VN_TZ)
    day = local.date()
    while True:
        if day.weekday() < 5:
            for start in _SESSION_STARTS:
                candidate = datetime.combine(day, start, tzinfo=VN_TZ)
                if candidate > local:
                    return candidate.astimezone(ZoneInfo('UTC'))
        day += timedelta(days=1)


class PriceBoard:
    """
    Fetches realtime quotes for Vietnamese '.VN' symbols from the VPS
    price board feed, all symbols in a single request.
    """

    def __init__(self, timeout: float = 10):
        """
        Args:
            timeout (float): Request timeout, in seconds.
        """
        self.timeout = timeout
        # Created on first use and reused, keeping the connection alive between fetches
        self._session = None

    def fetch(self, symbols: List[str]) -> Dict[str, BoardQuote]:
        """
        Fetches the latest quotes for the given symbols.

        Args:
            symbols (List[str]): '.VN' ticker symbols, e.g. ['VHM.VN'].

        Returns:
            Dict[str, BoardQuote]: Quotes keyed by the given symbols; symbols
                the board doesn't list are left out.

        Raises:
            Exception: On network, HTTP, or malformed response errors.
        """
        if self._session is None:
            self._session = requests.Session(impersonate='chrome')

        by_code = {symbol[:-len(VN_SUFFIX)].upper(): symbol for symbol in symbols}
        response = self._session.get(BOARD_URL.format(codes=','.join(by_code)), timeout=self.timeout)
        response.raise_for_status()

        quotes = {}
        for item in response.json():
            symbol = by_code.get(str(item.get('sym', '')).upper())
            quote = self._parse_quote(item)
            if symbol is not None and quote is not None:
                quotes[symbol] = quote
        return quotes

    @staticmethod
    def _parse_quote(item: dict) -> Optional[BoardQuote]:
        """
        Parses one board entry. Prices are in thousands of VND, the high/low
        as strings, and all zero until the stock first trades in the session.
        The board's own change fields are unsigned, so the change is derived
        from the reference price instead.

        Args:
            item (dict): One entry of the board's JSON response.

        Returns:
            Optional[BoardQuote]: The quote, or None without a reference price.
        """
        reference = PriceBoard._thousands_to_vnd(item.get('r'))
        if reference <= 0:
            return None
        price = PriceBoard._thousands_to_vnd(item.get('lastPrice')) or reference
        return BoardQuote(
            price=price,
            reference=reference,
            ceiling=PriceBoard._thousands_to_vnd(item.get('c')),
            floor=PriceBoard._thousands_to_vnd(item.get('f')),
            day_high=PriceBoard._thousands_to_vnd(item.get('highPrice')) or price,
            day_low=PriceBoard._thousands_to_vnd(item.get('lowPrice')) or price,
            exchange=_MARKET_NAMES.get(item.get('marketId'), 'HOSE'),
        )

    @staticmethod
    def _thousands_to_vnd(value) -> float:
        """
        Args:
            value: A board price in thousands of VND, as a number or string.

        Returns:
            float: The price in whole VND, or 0.0 if missing or malformed.
        """
        try:
            return float(round(float(value) * 1000))
        except (TypeError, ValueError, OverflowError):
            return 0.0
