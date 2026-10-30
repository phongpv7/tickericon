import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from src import price_history, vn_market, world_market

# Seconds between fetches while a market is open: Binance trades and price
# board matches stream in continuously, so every fetch picks up their latest
# values without a request
REALTIME_FETCH_INTERVAL_S = 0.25

# Seconds between price board polls during sessions: every second until its
# stream has carried a match for every symbol, then only to refresh the
# reference, ceiling, and floor prices
PRICE_BOARD_INTERVAL_S = 1
STREAMED_PRICE_BOARD_INTERVAL_S = 60

# Seconds between price board polls outside its sessions, and between
# fetches while no tracked market is open
FETCH_INTERVAL_S = 60


@dataclass
class StockData:
    """Latest known snapshot of a single tracked ticker."""

    symbol: str
    price: float = 0.0
    change_pct: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    prev_close: float = 0.0
    state: str = 'closed'
    exchange: str = ''
    currency: str = ''
    # Daily price limits; 0 for exchanges that don't publish them (only Vietnam's do)
    ceiling: float = 0.0
    floor: float = 0.0
    next_open: Optional[datetime] = None
    has_error: bool = False

    @property
    def price_band(self) -> str:
        """
        Where the price sits relative to its daily limits, for coloring it
        like a Vietnamese price board.

        Returns:
            str: 'ceiling' or 'floor' at a daily limit, 'reference' when
                unchanged from the reference price, or '' otherwise and
                whenever the exchange publishes no limits.
        """
        if self.ceiling <= 0 or self.floor <= 0:
            return ''
        if self.price >= self.ceiling:
            return 'ceiling'
        if self.price <= self.floor:
            return 'floor'
        if self.price == self.prev_close:
            return 'reference'
        return ''


class MarketAPI:
    """
    Fetches realtime quotes for the tracked tickers: Vietnamese '.VN'
    symbols from the VPS price board, their market state following the
    exchange's session schedule, and every other symbol from Binance (see
    world_market), which trades around the clock.
    """

    def __init__(self, ticker_symbols: List[str]):
        """
        Args:
            ticker_symbols (List[str]): The ticker symbols to track.
        """
        self.ticker_symbols = ticker_symbols
        self.price_board = vn_market.PriceBoard()
        self.world_ticker = world_market.WorldTicker()
        # The price board's latest REST quotes, when they were polled
        # (time.monotonic()), and whether the last poll failed
        self._board_quotes: Dict[str, vn_market.BoardQuote] = {}
        self._price_board_fetched_at = float('-inf')
        self._board_failed = False

    def close(self) -> None:
        """Stops the price streams; call before dropping this instance."""
        self.price_board.close()
        self.world_ticker.close()

    def history(self, symbol: str) -> price_history.Candles:
        """
        Fetches about a year of daily candles for one symbol (see
        price_history.fetch_history), in the currency its quotes use. Blocks
        on the network, and safe to call from any thread.

        Args:
            symbol (str): A tracked symbol, e.g. 'VHM.VN' or 'XAU'.

        Returns:
            price_history.Candles: Its daily candles, oldest first.
        """
        return price_history.fetch_history(symbol, usdt_per_usd=self.world_ticker.usdt_per_usd,
                                           futures=self.world_ticker.is_futures(symbol))

    def fetch_all(self, previous: Optional[Dict[str, StockData]] = None) -> Dict[str, StockData]:
        """
        Fetches the latest snapshot for every tracked ticker.

        Note: prices stream in from the price board and Binance, needing a
        request only while a stream is down or has yet to carry a symbol's
        first match, so the snapshot can be refreshed several times a second.
        All '.VN' symbols share a single price board request (see
        _price_board_due for how often it is sent).

        Args:
            previous (Optional[Dict[str, StockData]]): The last known snapshot;
                a ticker whose fetch fails keeps its previous values (flagged
                with has_error) instead of resetting to zeros.

        Returns:
            Dict[str, StockData]: Latest data keyed by ticker symbol.
        """
        previous = previous or {}
        snapshot: Dict[str, StockData] = {}

        vn_symbols = [s for s in self.ticker_symbols if vn_market.is_vn_symbol(s)]
        if vn_symbols:
            snapshot.update(self._fetch_vn(vn_symbols, previous))
        for symbol in self.ticker_symbols:
            if not vn_market.is_vn_symbol(symbol):
                snapshot[symbol] = self._fetch_world(symbol, previous.get(symbol))

        return {symbol: snapshot[symbol] for symbol in self.ticker_symbols}

    def _price_board_due(self, state: str, streamed: bool) -> bool:
        """
        Args:
            state (str): The price board's current session state.
            streamed (bool): Whether its stream has carried a current match
                for every symbol.

        Returns:
            bool: True when the price board should be polled: every
                PRICE_BOARD_INTERVAL_S during a session, or every
                STREAMED_PRICE_BOARD_INTERVAL_S once every symbol streams, and
                at most once per FETCH_INTERVAL_S outside sessions (Binance
                symbols keep the update loop running around the clock).
        """
        if state == 'closed':
            interval = FETCH_INTERVAL_S
        elif streamed:
            interval = STREAMED_PRICE_BOARD_INTERVAL_S
        else:
            interval = PRICE_BOARD_INTERVAL_S
        return time.monotonic() - self._price_board_fetched_at >= interval

    def refresh_interval(self, snapshot: Dict[str, StockData]) -> float:
        """
        Picks how long to wait before the next fetch.

        Args:
            snapshot (Dict[str, StockData]): Latest fetched data per ticker.

        Returns:
            float: REALTIME_FETCH_INTERVAL_S while some tracked market is open
                (Binance always is, Vietnamese stocks during sessions),
                otherwise FETCH_INTERVAL_S.
        """
        if any(data.state != 'closed' for data in snapshot.values()):
            return REALTIME_FETCH_INTERVAL_S
        return FETCH_INTERVAL_S

    def _fetch_world(self, symbol: str, previous: Optional[StockData]) -> StockData:
        """
        Fetches a symbol from Binance, which trades around the clock; its
        change is measured from midnight Vietnam time.

        Args:
            symbol (str): A symbol outside Vietnam, e.g. 'BTC' or 'XAU'.
            previous (Optional[StockData]): The last known snapshot for this
                symbol, returned (with has_error set) if the fetch fails.

        Returns:
            StockData: The parsed snapshot, or the last known values with
                has_error set if Binance can't provide the quote.
        """
        try:
            quote = self.world_ticker.fetch(symbol)
        except Exception:
            return self._error_result(symbol, previous)
        change_pct = (quote.price - quote.day_open) / quote.day_open * 100 if quote.day_open else 0.0
        return StockData(
            symbol=symbol,
            price=quote.price,
            change_pct=change_pct,
            day_high=quote.day_high,
            day_low=quote.day_low,
            prev_close=quote.day_open,
            state='open',
            exchange='Binance',
            currency='USD',
        )

    def _fetch_vn(self, symbols: List[str], previous: Dict[str, StockData]) -> Dict[str, StockData]:
        """
        Builds the Vietnamese symbols' snapshots: the latest streamed match
        on top of the price board's REST quote, polled when due. Their market
        state follows the exchange's fixed session schedule, so it stays
        current even when the board can't be reached.

        Args:
            symbols (List[str]): The '.VN' symbols to fetch.
            previous (Dict[str, StockData]): The last known snapshot, whose
                values a symbol keeps (flagged with has_error) if it has
                never been fetched.

        Returns:
            Dict[str, StockData]: Latest data keyed by ticker symbol.
        """
        now = datetime.now(ZoneInfo("UTC"))
        state = vn_market.session_state(now)
        next_open = vn_market.next_session_start(now)
        matches = self.price_board.streamed_matches(symbols)
        if self._price_board_due(state, streamed=len(matches) == len(symbols)):
            self._price_board_fetched_at = time.monotonic()
            try:
                self._board_quotes = self.price_board.fetch(symbols)
                self._board_failed = False
            except Exception:
                self._board_failed = True  # keep the last quotes, flagged until a poll succeeds

        snapshot = {}
        for symbol in symbols:
            quote = self._board_quotes.get(symbol)
            if quote is None:
                snapshot[symbol] = replace(
                    self._error_result(symbol, previous.get(symbol)),
                    state=state, currency='VND', next_open=next_open,
                )
                continue
            price, day_high, day_low = quote.price, quote.day_high, quote.day_low
            match = matches.get(symbol)
            # Stocks only trade within the day's limits; a match outside them was misread
            if match is not None and quote.floor <= match.price <= quote.ceiling:
                price, day_high, day_low = match.price, max(day_high, match.day_high), min(day_low, match.day_low)
            else:
                match = None
            snapshot[symbol] = StockData(
                symbol=symbol,
                price=price,
                change_pct=(price - quote.reference) / quote.reference * 100,
                day_high=day_high,
                day_low=day_low,
                prev_close=quote.reference,
                state=state,
                exchange=quote.exchange,
                currency='VND',
                ceiling=quote.ceiling,
                floor=quote.floor,
                next_open=next_open,
                # A streamed match is current even while the board's REST feed is down
                has_error=self._board_failed and match is None,
            )
        return snapshot

    @staticmethod
    def _error_result(symbol: str, previous: Optional[StockData]) -> StockData:
        """
        Builds the snapshot returned for a failed fetch: the last known
        values flagged with has_error, or a zeroed placeholder if the
        symbol has never been fetched successfully.

        Args:
            symbol (str): The ticker symbol whose fetch failed.
            previous (Optional[StockData]): The last known snapshot, if any.

        Returns:
            StockData: The snapshot to display for the failed fetch.
        """
        if previous is not None:
            return replace(previous, has_error=True)
        return StockData(symbol=symbol, has_error=True)

    @staticmethod
    def get_next_open_time(snapshot: Dict[str, StockData]) -> Optional[datetime]:
        """
        Finds the soonest upcoming market open across a snapshot of tickers,
        used to let the update loop sleep longer when every market is closed.

        Args:
            snapshot (Dict[str, StockData]): Latest fetched data per ticker.

        Returns:
            Optional[datetime]: The earliest next open time, or None if no
                ticker in the snapshot has a known next open time.
        """
        next_opens = [d.next_open for d in snapshot.values() if d.next_open is not None]
        return min(next_opens) if next_opens else None
