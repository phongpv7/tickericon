import json
import threading
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from curl_cffi import requests

from src.price_stream import SocketIOStream

# Suffix marking Vietnamese stocks (HOSE, HNX, and UPCoM), e.g. 'VHM.VN'
VN_SUFFIX = '.VN'

# JSON feed behind the VPS Securities web price board; takes comma-separated stock codes
BOARD_URL = 'https://bgapidatafeed.vps.com.vn/getliststockdata/{codes}'

# Socket.IO feed behind the same board, pushing every match as it happens
STREAM_URL = 'wss://bgdatafeed.vps.com.vn/socket.io/?EIO=4&transport=websocket'

# Ids of the board's stream messages: matches (in 'stock' events), and one
# side's best bids or asks and foreign investors' trading (in 'board' events)
_MATCH_MESSAGE_ID = 3220
_BOOK_MESSAGE_ID = 3210
_FOREIGN_MESSAGE_ID = 3250

# The board counts volume in lots of ten shares, on every exchange
_SHARES_PER_LOT = 10

# How many of each stock's latest streamed matches are kept
TRADES_KEPT = 30

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


# One side of the order book: its best (price in VND, shares) levels, best first
Levels = Tuple[Tuple[float, float], ...]


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
    volume: float = 0.0  # shares matched today
    # Shares foreign investors bought and sold today, what they paid and got
    # for them (VND), and the shares they may still buy (their room)
    foreign_bought: float = 0.0
    foreign_sold: float = 0.0
    foreign_bought_value: float = 0.0
    foreign_sold_value: float = 0.0
    foreign_room: float = 0.0
    # The order book's three best bids and asks
    bids: Levels = ()
    asks: Levels = ()


@dataclass
class BoardMatch:
    """A match streamed from the price board, in VND."""

    price: float
    day_high: float
    day_low: float
    volume: float = 0.0  # shares matched today, this match included


@dataclass(frozen=True)
class BoardTrade:
    """One match streamed from the price board."""

    time: str      # exchange time, 'HH:MM:SS'
    price: float   # VND
    volume: float  # shares
    # 'B' when a buyer took an ask (active buying), 'S' when a seller hit a bid, '' if not given
    side: str


@dataclass
class BoardForeign:
    """Foreign investors' trading in one stock today, streamed from the price board."""

    bought: float  # shares
    sold: float
    bought_value: float  # VND
    sold_value: float
    room: float    # shares they may still buy


def is_vn_symbol(symbol: str) -> bool:
    """
    Args:
        symbol (str): A configured ticker symbol.

    Returns:
        bool: True for Vietnamese symbols served by the price board, e.g. 'VHM.VN'.
    """
    return symbol.upper().endswith(VN_SUFFIX)


def vn_symbol(code: str) -> str:
    """
    Args:
        code (str): A Vietnamese stock code, e.g. 'VHM' (or already 'VHM.VN').

    Returns:
        str: The symbol the app tracks it by, e.g. 'VHM.VN'.
    """
    code = code.upper()
    return code if code.endswith(VN_SUFFIX) else code + VN_SUFFIX


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
    Realtime quotes for Vietnamese '.VN' symbols from the VPS price board:
    full quotes for all symbols in a single REST request, and every match
    as it happens over the board's Socket.IO stream.
    """

    def __init__(self, timeout: float = 10):
        """
        Args:
            timeout (float): Request timeout, in seconds.
        """
        self.timeout = timeout
        # Created on first use and reused, keeping the connection alive between fetches
        self._session = None
        self._stream = SocketIOStream(
            STREAM_URL,
            # Joins the codes as one comma-separated list, like the web board
            subscribe_message=lambda codes: ['regs', json.dumps({'action': 'join', 'list': ','.join(codes)})],
            on_message=self._on_event,
        )
        # Latest streamed match, order book side, and foreign trading per stock
        # code (the book's by (code, 'B' or 'S')), each with when it arrived
        # (monotonic()) and its trading day; and each code's latest matches,
        # newest last. Written on the stream's thread
        self._matches: Dict[str, Tuple[BoardMatch, float, date]] = {}
        self._books: Dict[Tuple[str, str], Tuple[Levels, float, date]] = {}
        self._foreign: Dict[str, Tuple[BoardForeign, float, date]] = {}
        self._trades: Dict[str, Tuple[Deque[BoardTrade], float, date]] = {}
        # Reentrant, as streamed_trades copies the trades under it via _streamed
        self._matches_lock = threading.RLock()

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
        for code in by_code:
            self._stream.subscribe(code)
        response = self._session.get(BOARD_URL.format(codes=','.join(by_code)), timeout=self.timeout)
        response.raise_for_status()

        quotes = {}
        for item in response.json():
            symbol = by_code.get(str(item.get('sym', '')).upper())
            quote = self._parse_quote(item)
            if symbol is not None and quote is not None:
                quotes[symbol] = quote
        return quotes

    def streamed_matches(self, symbols: List[str]) -> Dict[str, BoardMatch]:
        """
        Args:
            symbols (List[str]): '.VN' ticker symbols, e.g. ['VHM.VN'].

        Returns:
            Dict[str, BoardMatch]: Each symbol's latest match today, streamed
                since the stream last connected (matches missed while it
                reconnected make older ones stale); symbols without one are
                left out.
        """
        return self._streamed(self._matches, symbols)

    def streamed_books(self, symbols: List[str]) -> Dict[str, Tuple[Optional[Levels], Optional[Levels]]]:
        """
        Args:
            symbols (List[str]): '.VN' ticker symbols, e.g. ['VHM.VN'].

        Returns:
            Dict[str, Tuple[Optional[Levels], Optional[Levels]]]: Each symbol's
                best bids and asks as last streamed, either None where that
                side hasn't streamed since the stream last connected.
        """
        bids = self._streamed(self._books, symbols, side='B')
        asks = self._streamed(self._books, symbols, side='S')
        return {symbol: (bids.get(symbol), asks.get(symbol)) for symbol in symbols if symbol in bids or symbol in asks}

    def streamed_foreign(self, symbols: List[str]) -> Dict[str, BoardForeign]:
        """
        Args:
            symbols (List[str]): '.VN' ticker symbols, e.g. ['VHM.VN'].

        Returns:
            Dict[str, BoardForeign]: Each symbol's foreign trading today, as
                last streamed since the stream last connected; symbols without
                any are left out.
        """
        return self._streamed(self._foreign, symbols)

    def streamed_trades(self, symbols: List[str]) -> Dict[str, Tuple[BoardTrade, ...]]:
        """
        Args:
            symbols (List[str]): '.VN' ticker symbols, e.g. ['VHM.VN'].

        Returns:
            Dict[str, Tuple[BoardTrade, ...]]: Each symbol's latest matches
                today, up to TRADES_KEPT, newest first; symbols without any
                are left out.
        """
        with self._matches_lock:
            return {symbol: tuple(reversed(trades))
                    for symbol, trades in self._streamed(self._trades, symbols).items()}

    def close(self) -> None:
        """Stops the match stream."""
        self._stream.close()

    def _streamed(self, entries: dict, symbols: List[str], side: Optional[str] = None) -> dict:
        """
        Args:
            entries (dict): Streamed values by stock code (or by code and
                side, given `side`), each with when it arrived and its day.
            symbols (List[str]): '.VN' ticker symbols, e.g. ['VHM.VN'].
            side (Optional[str]): The order book side, for the books.

        Returns:
            dict: The symbols' values from today that streamed since the
                stream last connected (values from before it reconnected may
                have changed unseen).
        """
        if not self._stream.live:
            return {}
        today = datetime.now(VN_TZ).date()
        with self._matches_lock:
            entries = dict(entries)
        current = {}
        for symbol in symbols:
            code = symbol[:-len(VN_SUFFIX)].upper()
            entry = entries.get(code if side is None else (code, side))
            if entry is not None and entry[1] >= self._stream.connected_at and entry[2] == today:
                current[symbol] = entry[0]
        return current

    def _on_event(self, event: str, payload) -> None:
        """
        Records each streamed match, order book change, and foreign trading
        update; called on the stream's thread.

        Args:
            event (str): The Socket.IO event name.
            payload: The event's argument: {'data': {...}} with the board's
                own fields, prices in thousands of VND and volumes in lots.
        """
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return
        arrived = (monotonic(), datetime.now(VN_TZ).date())
        message = (event, data.get('id'))
        if message == ('board', _BOOK_MESSAGE_ID) and data.get('side') in ('B', 'S'):
            levels = self._levels(data.get(key) for key in ('g1', 'g2', 'g3'))
            with self._matches_lock:
                self._books[(str(data.get('sym', '')).upper(), data['side'])] = (levels, *arrived)
        elif message == ('board', _FOREIGN_MESSAGE_ID):
            foreign = BoardForeign(
                bought=self._lots_to_shares(data.get('fBVol')),
                sold=self._lots_to_shares(data.get('fSVolume')),
                bought_value=self._thousands_to_vnd(data.get('fBValue')),
                sold_value=self._thousands_to_vnd(data.get('fSValue')),
                room=self._shares(data.get('fRoom')),
            )
            with self._matches_lock:
                self._foreign[str(data.get('symbol', '')).upper()] = (foreign, *arrived)
        elif message == ('stock', _MATCH_MESSAGE_ID):
            self._record_match(data, arrived)

    def _record_match(self, data: dict, arrived: Tuple[float, date]) -> None:
        """Records one streamed match, as the stock's latest and among its recent trades."""
        price = self._thousands_to_vnd(data.get('lastPrice'))
        if price <= 0:
            return
        code = str(data.get('sym', '')).upper()
        match = BoardMatch(
            price=price,
            day_high=self._thousands_to_vnd(data.get('hp')) or price,
            day_low=self._thousands_to_vnd(data.get('lp')) or price,
            volume=self._lots_to_shares(data.get('totalVol')),
        )
        side = data.get('side')
        trade = BoardTrade(time=str(data.get('time', '')), price=price,
                           volume=self._lots_to_shares(data.get('lastVol')), side=side if side in ('B', 'S') else '')
        with self._matches_lock:
            self._matches[code] = (match, *arrived)
            trades = self._trades.get(code)
            if trades is None or trades[2] != arrived[1]:  # a new day starts afresh
                trades = (deque(maxlen=TRADES_KEPT), *arrived)
            trades[0].append(trade)
            self._trades[code] = (trades[0], *arrived)

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
            volume=PriceBoard._lots_to_shares(item.get('lot')),
            foreign_bought=PriceBoard._lots_to_shares(item.get('fBVol')),
            foreign_sold=PriceBoard._lots_to_shares(item.get('fSVolume')),
            foreign_bought_value=PriceBoard._thousands_to_vnd(item.get('fBValue')),
            foreign_sold_value=PriceBoard._thousands_to_vnd(item.get('fSValue')),
            foreign_room=PriceBoard._shares(item.get('fRoom')),
            bids=PriceBoard._levels(item.get(key) for key in ('g1', 'g2', 'g3')),
            asks=PriceBoard._levels(item.get(key) for key in ('g4', 'g5', 'g6')),
        )

    @staticmethod
    def _levels(values) -> Levels:
        """
        Args:
            values: Order book levels as the board writes them,
                'price|lots|color', e.g. '62.3|90|i', prices in thousands of VND.

        Returns:
            Levels: The levels with a price, as (VND, shares); auction orders
                ('ATO', 'ATC') and empty levels are left out.
        """
        levels = []
        for value in values:
            parts = str(value or '').split('|')
            price = PriceBoard._thousands_to_vnd(parts[0])
            if price > 0 and len(parts) > 1:
                levels.append((price, PriceBoard._lots_to_shares(parts[1])))
        return tuple(levels)

    @staticmethod
    def _lots_to_shares(value) -> float:
        """
        Args:
            value: A board volume in lots of ten shares, as a number or string.

        Returns:
            float: The volume in shares, or 0.0 if missing or malformed.
        """
        return PriceBoard._shares(value) * _SHARES_PER_LOT

    @staticmethod
    def _shares(value) -> float:
        """
        Args:
            value: A number of shares, as a number or string.

        Returns:
            float: The number, or 0.0 if missing or malformed.
        """
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0

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
