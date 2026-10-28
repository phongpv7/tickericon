import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src import vn_market
from src.config_handler import display_name
from src.market_api import StockData

# One alert: a tracked symbol (or its display name), >= or <=, and a price
_ALERT_PATTERN = re.compile(r'^(?P<name>.+?)\s*(?P<op>>=|<=|>|<)\s*(?P<price>\d+(?:\.\d+)?)$')


@dataclass(frozen=True)
class Alert:
    """A price target for one tracked symbol."""

    symbol: str
    # True to alert at or above the target, False at or below it
    above: bool
    # The target in the quote currency (whole VND for Vietnamese stocks)
    target: float
    # The target as the user wrote it, for the notification text
    text: str


def parse_alerts(text: str, tickers: List[str], names: Dict[str, str], strict: bool = True) -> List[Alert]:
    """
    Parses comma-separated alerts like 'VHM>=65, BTC<=80000'.

    Vietnamese stocks are shown in thousands of VND on the taskbar, so their
    targets below 1,000 use that unit too (65 means 65,000 VND); larger
    targets are taken as whole VND.

    Args:
        text (str): The alerts as written in the config or form.
        tickers (List[str]): The tracked symbols an alert may refer to.
        names (Dict[str, str]): Display names keyed by symbol; an alert may
            name a symbol by its display name, e.g. 'Gold>=4100'.
        strict (bool): Raise on an invalid entry instead of skipping it.

    Returns:
        List[Alert]: The parsed alerts, in order.

    Raises:
        ValueError: In strict mode, if an entry is malformed or names a
            symbol that isn't tracked.
    """
    alerts = []
    for entry in text.split(','):
        entry = entry.strip()
        if not entry:
            continue
        match = _ALERT_PATTERN.match(entry)
        symbol = _resolve_symbol(match.group('name'), tickers, names) if match else None
        if symbol is None:
            if strict:
                raise ValueError(f"Invalid alert '{entry}': use a tracked symbol, >= or <=, and a price, "
                                 f"e.g. VHM>=65.")
            continue
        target = float(match.group('price'))
        if vn_market.is_vn_symbol(symbol) and target < 1000:
            target *= 1000
        alerts.append(Alert(symbol=symbol, above=match.group('op').startswith('>'),
                            target=target, text=match.group('price')))
    return alerts


def _resolve_symbol(name: str, tickers: List[str], names: Dict[str, str]) -> Optional[str]:
    """
    Args:
        name (str): A symbol or display name as written in an alert.
        tickers (List[str]): The tracked symbols.
        names (Dict[str, str]): Display names keyed by symbol.

    Returns:
        Optional[str]: The tracked symbol it refers to (case-insensitive),
            or None if none matches.
    """
    key = name.strip().casefold()
    for symbol in tickers:
        if key in (symbol.casefold(), display_name(symbol, names).casefold()):
            return symbol
    return None


class AlertMonitor:
    """
    Tracks which alerts have reached their targets, so each one notifies
    once per crossing: again only after the price has moved back away from
    the target first.
    """

    def __init__(self, alerts: List[Alert]):
        """
        Args:
            alerts (List[Alert]): The alerts to watch.
        """
        self.alerts = alerts
        self._reached: Dict[Alert, bool] = {}

    def check(self, snapshot: Dict[str, StockData]) -> List[Tuple[Alert, StockData]]:
        """
        Compares the latest prices against the alert targets. An alert whose
        target is already reached on the first check fires right away.

        Args:
            snapshot (Dict[str, StockData]): Latest data keyed by symbol.

        Returns:
            List[Tuple[Alert, StockData]]: The alerts that just reached their
                targets, with the data that reached them.
        """
        fired = []
        for alert in self.alerts:
            data = snapshot.get(alert.symbol)
            # Skip failed or not-yet-fetched prices rather than treat them as moves
            if data is None or data.has_error or data.price <= 0:
                continue
            reached = data.price >= alert.target if alert.above else data.price <= alert.target
            if reached and not self._reached.get(alert):
                fired.append((alert, data))
            self._reached[alert] = reached
        return fired
