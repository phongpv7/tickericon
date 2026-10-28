import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import yfinance as yf

from src import commodity_market, crypto_market, vn_market

# Seconds between fetches while a market is open. Yahoo Finance quotes come
# from 1-minute bars, and the Vietnamese price board is polled every second
# during sessions; Binance and Hyperliquid prices stream in continuously, so
# every fetch picks up their latest values without a request
FETCH_INTERVAL_S = 60
PRICE_BOARD_INTERVAL_S = 1
REALTIME_FETCH_INTERVAL_S = 0.25

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
    minutes, come from a realtime local price board instead, crypto symbols
    such as 'BTC-USD' from Binance, and commodity futures such as 'GC=F'
    (delayed by 10 minutes) from Hyperliquid.
    """

    def __init__(self, ticker_symbols: List[str]):
        """
        Args:
            ticker_symbols (List[str]): The ticker symbols to track.
        """
        self.ticker_symbols = ticker_symbols
        self.price_board = vn_market.PriceBoard()
        self.crypto_ticker = crypto_market.CryptoTicker()
        self.commodity_ticker = commodity_market.CommodityTicker()
        # When each Yahoo Finance symbol, and the price board, were last fetched (time.monotonic())
        self._fetched_at: Dict[str, float] = {}
        self._price_board_fetched_at = float('-inf')

    @property
    def realtime(self) -> bool:
        """
        Returns:
            bool: True when every tracked symbol comes from a realtime source
                (the price board, Binance, or Hyperliquid), refreshed every
                second or faster.
        """
        return bool(self.ticker_symbols) and all(self._is_realtime_symbol(s) for s in self.ticker_symbols)

    @staticmethod
    def _is_realtime_symbol(symbol: str) -> bool:
        """
        Args:
            symbol (str): A tracked ticker symbol.

        Returns:
            bool: True for symbols served by a realtime source.
        """
        return (vn_market.is_vn_symbol(symbol) or crypto_market.is_crypto_symbol(symbol)
                or commodity_market.is_commodity_symbol(symbol))

    def close(self) -> None:
        """Stops the realtime price streams; call before dropping this instance."""
        self.crypto_ticker.close()
        self.commodity_ticker.close()

    def fetch_all(self, previous: Optional[Dict[str, StockData]] = None,
                  force: bool = False) -> Dict[str, StockData]:
        """
        Fetches the latest snapshot for every tracked ticker.

        Note: yfinance has no batch quote endpoint, so this issues one
        request per ticker (sequentially, to avoid hammering Yahoo Finance);
        all '.VN' symbols share a single price board request, while crypto
        and commodity prices come from their streams, needing a request only
        while a stream is down. Yahoo Finance symbols are fetched at most once
        per FETCH_INTERVAL_S, and the price board at most once per
        PRICE_BOARD_INTERVAL_S during its sessions (FETCH_INTERVAL_S outside
        them), keeping their previous values in between, so a list mixing them
        with streamed symbols can still be refreshed several times a second.

        Args:
            previous (Optional[Dict[str, StockData]]): The last known snapshot;
                a ticker whose fetch fails keeps its previous values (flagged
                with has_error) instead of resetting to zeros.
            force (bool): Refetch every symbol even if fetched recently.

        Returns:
            Dict[str, StockData]: Latest data keyed by ticker symbol.
        """
        previous = previous or {}
        now = time.monotonic()
        fresh: Dict[str, StockData] = {}

        vn_symbols = [s for s in self.ticker_symbols if vn_market.is_vn_symbol(s)]
        if vn_symbols and (force or self._price_board_due(now)):
            fresh.update(self._fetch_vn(vn_symbols, previous))
            self._price_board_fetched_at = now

        for symbol in self.ticker_symbols:
            if vn_market.is_vn_symbol(symbol):
                continue
            try:
                if crypto_market.is_crypto_symbol(symbol):
                    fresh[symbol] = self._fetch_crypto(symbol)
                    continue
                if commodity_market.is_commodity_symbol(symbol):
                    fresh[symbol] = self._fetch_commodity(symbol)
                    continue
            except Exception:
                pass  # not listed by the realtime source, or unreachable: fall back to Yahoo Finance
            if force or now - self._fetched_at.get(symbol, float('-inf')) >= FETCH_INTERVAL_S:
                fresh[symbol] = self._fetch_one(symbol, previous.get(symbol))
                self._fetched_at[symbol] = now

        # Symbols not refetched this time keep their previous values
        return {
            symbol: fresh[symbol] if symbol in fresh else previous.get(symbol, StockData(symbol=symbol))
            for symbol in self.ticker_symbols
        }

    def _price_board_due(self, now: float) -> bool:
        """
        Args:
            now (float): The current time.monotonic().

        Returns:
            bool: True when the price board should be polled: every
                PRICE_BOARD_INTERVAL_S during a session, otherwise at most once
                per FETCH_INTERVAL_S (streamed symbols keep the update loop
                running around the clock).
        """
        in_session = vn_market.session_state(datetime.now(ZoneInfo("UTC"))) != 'closed'
        interval = PRICE_BOARD_INTERVAL_S if in_session else FETCH_INTERVAL_S
        return now - self._price_board_fetched_at >= interval

    def refresh_interval(self, snapshot: Dict[str, StockData]) -> float:
        """
        Picks how long to wait before the next fetch while some market is open.

        Args:
            snapshot (Dict[str, StockData]): Latest fetched data per ticker.

        Returns:
            float: REALTIME_FETCH_INTERVAL_S while a realtime symbol is
                trading (crypto and commodities always are, Vietnamese stocks
                during sessions), otherwise FETCH_INTERVAL_S.
        """
        if any(self._is_realtime_symbol(symbol) and data.state != 'closed' for symbol, data in snapshot.items()):
            return REALTIME_FETCH_INTERVAL_S
        return FETCH_INTERVAL_S

    def _fetch_crypto(self, symbol: str) -> StockData:
        """
        Fetches a crypto symbol from Binance. Crypto trades around the
        clock; its change is measured from midnight Vietnam time.

        Args:
            symbol (str): A crypto symbol, e.g. 'BTC-USD'.

        Returns:
            StockData: The parsed snapshot.

        Raises:
            Exception: When Binance can't provide the quote.
        """
        quote = self.crypto_ticker.fetch(symbol)
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

    def _fetch_commodity(self, symbol: str) -> StockData:
        """
        Fetches a commodity future from Hyperliquid, whose contracts trade
        around the clock; its change is measured over the last 24 hours.

        Args:
            symbol (str): A mapped futures symbol, e.g. 'GC=F'.

        Returns:
            StockData: The parsed snapshot.

        Raises:
            Exception: When Hyperliquid can't provide the quote.
        """
        quote = self.commodity_ticker.fetch(symbol)
        change_pct = (quote.price - quote.prev_day) / quote.prev_day * 100 if quote.prev_day else 0.0
        return StockData(
            symbol=symbol,
            price=quote.price,
            change_pct=change_pct,
            day_high=quote.day_high,
            day_low=quote.day_low,
            prev_close=quote.prev_day,
            state='open',
            exchange='Hyperliquid',
            currency='USD',
        )

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