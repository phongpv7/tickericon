import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import yfinance as yf

from src import vn_market

# Seconds between fetches while a market is open: Yahoo Finance quotes come
# from 1-minute bars, while the realtime price board can be polled every few seconds
FETCH_INTERVAL_S = 60
REALTIME_FETCH_INTERVAL_S = 5

# Maps Yahoo Finance exchange codes to human-friendly names for the tooltip.
# Falls back to the metadata's own "fullExchangeName" when a code is unknown.
_EXCHANGE_NAMES = {
    'NMS': 'Nasdaq',
    'NYQ': 'NYSE',
    'ASE': 'NYSE American',
    'PCX': 'NYSE Arca',
    'LSE': 'London',
    'TOR': 'Toronto',
    'GER': 'Frankfurt',
    'PAR': 'Paris',
    'AMS': 'Amsterdam',
    'HKG': 'Hong Kong',
    'TYO': 'Tokyo',
    'SHH': 'Shanghai',
    'SHZ': 'Shenzhen',
    'NSI': 'NSE India',
    'BSE': 'BSE India',
    'ASX': 'Sydney',
}


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
    Fetches quote data for one or more tickers from Yahoo Finance and
    resolves each ticker's own market session state from its exchange's
    trading hours, so symbols from different markets/timezones are all
    handled correctly. Vietnamese '.VN' symbols, which Yahoo delays by 15
    minutes, come from a realtime local price board instead.
    """

    def __init__(self, ticker_symbols: List[str]):
        """
        Args:
            ticker_symbols (List[str]): The ticker symbols to track.
        """
        self.ticker_symbols = ticker_symbols
        self.price_board = vn_market.PriceBoard()
        # When each Yahoo Finance symbol was last fetched (time.monotonic())
        self._fetched_at: Dict[str, float] = {}

    @property
    def realtime(self) -> bool:
        """
        Returns:
            bool: True when every tracked symbol comes from the realtime
                price board, which can be polled every few seconds.
        """
        return bool(self.ticker_symbols) and all(vn_market.is_vn_symbol(s) for s in self.ticker_symbols)

    def fetch_all(self, previous: Optional[Dict[str, StockData]] = None,
                  force: bool = False) -> Dict[str, StockData]:
        """
        Fetches the latest snapshot for every tracked ticker.

        Note: yfinance has no batch quote endpoint, so this issues one
        request per ticker (sequentially, to avoid hammering Yahoo Finance);
        all '.VN' symbols share a single price board request. Yahoo Finance
        symbols are fetched at most once per FETCH_INTERVAL_S, keeping their
        previous values in between, so a list mixing them with realtime
        symbols can still be polled every few seconds.

        Args:
            previous (Optional[Dict[str, StockData]]): The last known snapshot;
                a ticker whose fetch fails keeps its previous values (flagged
                with has_error) instead of resetting to zeros.
            force (bool): Refetch Yahoo Finance symbols even if fetched recently.

        Returns:
            Dict[str, StockData]: Latest data keyed by ticker symbol.
        """
        previous = previous or {}
        vn_symbols = [s for s in self.ticker_symbols if vn_market.is_vn_symbol(s)]
        vn_data = self._fetch_vn(vn_symbols, previous) if vn_symbols else {}

        now = time.monotonic()
        snapshot = {}
        for symbol in self.ticker_symbols:
            if symbol in vn_data:
                snapshot[symbol] = vn_data[symbol]
            elif (not force and symbol in previous
                    and now - self._fetched_at.get(symbol, float('-inf')) < FETCH_INTERVAL_S):
                snapshot[symbol] = previous[symbol]
            else:
                snapshot[symbol] = self._fetch_one(symbol, previous.get(symbol))
                self._fetched_at[symbol] = now
        return snapshot

    def refresh_interval(self, snapshot: Dict[str, StockData]) -> int:
        """
        Picks how long to wait before the next fetch while some market is open.

        Args:
            snapshot (Dict[str, StockData]): Latest fetched data per ticker.

        Returns:
            int: REALTIME_FETCH_INTERVAL_S while a price board symbol is in
                session, otherwise FETCH_INTERVAL_S.
        """
        if any(vn_market.is_vn_symbol(symbol) and data.state != 'closed' for symbol, data in snapshot.items()):
            return REALTIME_FETCH_INTERVAL_S
        return FETCH_INTERVAL_S

    def _fetch_vn(self, symbols: List[str], previous: Dict[str, StockData]) -> Dict[str, StockData]:
        """
        Fetches Vietnamese symbols from the realtime price board in one
        request. Their market state follows the exchange's fixed session
        schedule, so it stays current even when the fetch fails.

        Args:
            symbols (List[str]): The '.VN' symbols to fetch.
            previous (Dict[str, StockData]): The last known snapshot, whose
                values a symbol keeps (flagged with has_error) if it can't be fetched.

        Returns:
            Dict[str, StockData]: Latest data keyed by ticker symbol.
        """
        now = datetime.now(ZoneInfo("UTC"))
        state = vn_market.session_state(now)
        next_open = vn_market.next_session_start(now)
        try:
            quotes = self.price_board.fetch(symbols)
        except Exception:
            quotes = {}

        snapshot = {}
        for symbol in symbols:
            quote = quotes.get(symbol)
            if quote is None:
                snapshot[symbol] = replace(
                    self._error_result(symbol, previous.get(symbol)),
                    state=state, currency='VND', next_open=next_open,
                )
                continue
            snapshot[symbol] = StockData(
                symbol=symbol,
                price=quote.price,
                change_pct=(quote.price - quote.reference) / quote.reference * 100,
                day_high=quote.day_high,
                day_low=quote.day_low,
                prev_close=quote.reference,
                state=state,
                exchange=quote.exchange,
                currency='VND',
                ceiling=quote.ceiling,
                floor=quote.floor,
                next_open=next_open,
            )
        return snapshot

    def _fetch_one(self, symbol: str, previous: Optional[StockData] = None) -> StockData:
        """
        Fetches and parses quote and session data for a single ticker.

        Args:
            symbol (str): The ticker symbol to fetch, e.g. 'AMD'.
            previous (Optional[StockData]): The last known snapshot for this
                symbol, returned (with has_error set) if the fetch fails.

        Returns:
            StockData: The parsed snapshot, or the last known values with
                has_error set if the fetch failed or returned no data.
        """
        data = StockData(symbol=symbol)
        try:
            ticker = yf.Ticker(symbol)
            # 1m bars: daily bars ignore prepost, hiding extended-hours moves
            todays_data = ticker.history(period='1d', interval='1m', prepost=True)

            if todays_data.empty:
                return self._error_result(symbol, previous)

            data.price = todays_data['Close'].iloc[-1]

            metadata = ticker.history_metadata
            data.exchange = _EXCHANGE_NAMES.get(
                metadata.get('exchangeName', ''),
                metadata.get('fullExchangeName', symbol),
            )
            data.currency = metadata.get('currency', '')
            data.state = self._resolve_state(metadata)
            data.next_open = self._resolve_next_open(metadata)

            prev_close = self._resolve_previous_close(ticker, metadata, data.state)
            data.prev_close = prev_close
            data.change_pct = ((data.price - prev_close) / prev_close) * 100

            # Attempt to fetch high/low via fast_info, fallback to history dataframe
            try:
                data.day_high = ticker.fast_info['day_high']
                data.day_low = ticker.fast_info['day_low']
            except Exception:
                data.day_high = todays_data['High'].max()
                data.day_low = todays_data['Low'].min()

        except Exception:
            return self._error_result(symbol, previous)

        return data

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
    def _resolve_previous_close(ticker, metadata: dict, state: str) -> float:
        """
        Resolves the baseline close for the change percentage.

        During premarket, Yahoo's "regular market" fields still describe the
        previous day's session, so regular_market_previous_close is the close
        from two sessions back. The metadata's regularMarketPrice is the last
        regular close then (the session hasn't traded yet) - the baseline that
        matches the premarket change Yahoo itself displays.

        Args:
            ticker (yf.Ticker): The ticker being fetched.
            metadata (dict): Ticker.history_metadata for the symbol.
            state (str): The already-resolved market state for the symbol.

        Returns:
            float: The previous close to compute the change percentage against.
        """
        if state == 'premarket':
            last_close = metadata.get('regularMarketPrice')
            if last_close:
                return last_close

        # Prefer Yahoo's quoted previous close; fast_info['previous_close'] is
        # derived from daily bars and drifts around session boundaries/weekends.
        try:
            prev_close = ticker.fast_info['regular_market_previous_close']
        except (KeyError, TypeError):
            prev_close = None
        if not prev_close:
            prev_close = ticker.fast_info['previous_close']
        return prev_close

    @staticmethod
    def _resolve_state(metadata: dict) -> str:
        """
        Determines market state from the exchange's own current trading
        period (pre/regular/post), so it works regardless of which
        timezone or exchange the ticker trades on.

        Args:
            metadata (dict): Ticker.history_metadata for the symbol.

        Returns:
            str: One of 'premarket', 'open', 'after_hours', or 'closed'.
        """
        trading_period = metadata.get('currentTradingPeriod')
        if not trading_period:
            return 'closed'

        now = datetime.now(ZoneInfo("UTC"))
        pre, regular, post = trading_period['pre'], trading_period['regular'], trading_period['post']

        if pre['start'] <= now < pre['end']:
            return 'premarket'
        elif regular['start'] <= now < regular['end']:
            return 'open'
        elif post['start'] <= now < post['end']:
            return 'after_hours'
        return 'closed'

    @staticmethod
    def _resolve_next_open(metadata: dict) -> Optional[datetime]:
        """
        Projects the next weekday pre-market open for this exchange, using
        its own local trading hours.

        Args:
            metadata (dict): Ticker.history_metadata for the symbol.

        Returns:
            Optional[datetime]: The next pre-market open time in UTC, or
                None if the metadata doesn't include a trading period.
        """
        trading_period = metadata.get('currentTradingPeriod')
        if not trading_period:
            return None

        candidate = trading_period['pre']['start'].to_pydatetime()
        now = datetime.now(candidate.tzinfo)

        if candidate <= now:
            candidate += timedelta(days=1)

        # Skip Saturday/Sunday; exchange-specific holidays aren't tracked
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)

        return candidate.astimezone(ZoneInfo("UTC"))

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