import configparser
from typing import Dict, List, Tuple, TypedDict

from src.vn_market import VN_SUFFIX

RGB = Tuple[int, int, int]

# yfinance has no batch quote endpoint; it issues one HTTP request per
# ticker (Vietnamese '.VN' symbols share a single price board request), so
# the tracked symbol list is capped to keep polling reasonable.
MAX_TICKERS = 20

# Default icon font size in pixels; ~44 is the largest that still fits '9.9' in the 64px icon.
DEFAULT_FONT_SIZE = 36

# Default height of the market-state/error strip at the bottom of the icon, in pixels.
DEFAULT_STRIP_WIDTH = 2

# Largest allowed strip height; anything bigger starts eating into the text area.
MAX_STRIP_WIDTH = 16

# Market states that each have their own color scheme, in display order.
MARKET_STATES = ('open', 'premarket', 'after_hours', 'closed')

# What the ticker shows for each symbol; the static icon shows the price only in 'price' mode.
TICKER_VALUE_MODES = ('percentage', 'price', 'both')
DEFAULT_TICKER_VALUE_MODE = 'percentage'

# Fallback colors used when a config file predates per-state color schemes.
DEFAULT_BG_COLOR: RGB = (50, 50, 50)
DEFAULT_ERROR_COLOR: RGB = (255, 40, 40)
DEFAULT_POSITIVE_COLOR: RGB = (15, 175, 80)
DEFAULT_NEGATIVE_COLOR: RGB = (235, 60, 60)
DEFAULT_STRIP_COLORS: Dict[str, RGB] = {
    'open': (224, 255, 224),
    'premarket': (255, 255, 224),
    'after_hours': (224, 224, 255),
    'closed': (50, 50, 50),
}

# Price bands colored like Vietnamese price boards, overriding the up/down
# colors for markets that publish daily price limits (see StockData.price_band).
PRICE_BANDS = ('ceiling', 'reference', 'floor')
DEFAULT_BAND_COLORS: Dict[str, RGB] = {
    'ceiling': (210, 77, 255),    # purple
    'reference': (245, 197, 24),  # yellow
    'floor': (59, 163, 255),      # blue
}


def display_name(symbol: str, names: Dict[str, str]) -> str:
    """
    Args:
        symbol (str): A configured ticker symbol.
        names (Dict[str, str]): Display names from the config, keyed by symbol.

    Returns:
        str: The symbol's configured display name, or else the symbol itself,
            without the '.VN' suffix Vietnamese stocks are never quoted with.
    """
    if names.get(symbol):
        return names[symbol]
    return symbol[:-len(VN_SUFFIX)] if symbol.endswith(VN_SUFFIX) else symbol


class StateColors(TypedDict):
    positive: RGB
    negative: RGB
    strip: RGB


class AppConfig(TypedDict):
    tickers: List[str]
    names: Dict[str, str]
    alerts: str
    bg_color: RGB
    transparent_background: bool
    error_color: RGB
    state_colors: Dict[str, StateColors]
    band_colors: Dict[str, RGB]
    scroll_speed_ms: int
    display_ms: int
    continuous_scroll: bool
    taskbar_widget: bool
    ticker_value_mode: str
    show_daily_high: bool
    show_daily_low: bool
    high_low_pct: bool
    font_size: int
    strip_width: int


class ConfigHandler:
    """
    Reads config.cfg and exposes its values as a typed dict, converting
    hex color strings into RGB tuples usable by the icon generator.
    """

    def __init__(self, config_path: str = "config.cfg"):
        """
        Args:
            config_path (str): Path to the config.cfg file to load.
        """
        self.config_path = config_path

    @staticmethod
    def _hex_to_rgb(value: str) -> RGB:
        """
        Converts a '#RRGGBB' hex string into an (R, G, B) tuple.

        Args:
            value (str): Hex color string, e.g. '#0FAF50'.

        Returns:
            RGB: The corresponding (R, G, B) tuple.
        """
        value = value.strip().lstrip('#')
        if len(value) != 6:
            raise ValueError(f"Invalid hex color '{value}'. Expected #RRGGBB.")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    @staticmethod
    def _rgb_to_hex(value: RGB) -> str:
        """Convert an RGB tuple into a hexadecimal color string.

        Args:
            value (RGB): An (R, G, B) tuple to convert.

        Returns:
            str: A '#RRGGBB' color string.
        """
        return '#%02X%02X%02X' % value

    @staticmethod
    def parse_tickers(text: str) -> Tuple[List[str], Dict[str, str]]:
        """
        Parses a comma-separated ticker list whose entries may carry a
        display name after a colon, e.g. 'GC=F:Gold, VHM.VN'.

        Args:
            text (str): The ticker list as written in the config or form.

        Returns:
            Tuple[List[str], Dict[str, str]]: The upper-cased symbols in order,
                and the display names keyed by symbol for entries that have one.
        """
        tickers, names = [], {}
        for entry in text.split(","):
            symbol, _, name = entry.partition(":")
            symbol = symbol.strip().upper()
            if not symbol:
                continue
            tickers.append(symbol)
            if name.strip():
                names[symbol] = name.strip()
        return tickers, names

    @staticmethod
    def format_tickers(tickers: List[str], names: Dict[str, str], separator: str = ",") -> str:
        """
        Formats symbols and their display names back into a ticker list.

        Args:
            tickers (List[str]): The symbols, in order.
            names (Dict[str, str]): Display names keyed by symbol.
            separator (str): The text placed between entries.

        Returns:
            str: e.g. 'GC=F:Gold,VHM.VN'.
        """
        return separator.join(f"{s}:{names[s]}" if names.get(s) else s for s in tickers)

    def _read_text(self) -> str:
        """
        Reads the config file as UTF-8, how save() writes it, so display
        names like 'Vàng' survive; falls back to the system encoding for a
        file edited by hand in another one.

        Returns:
            str: The file contents.
        """
        try:
            with open(self.config_path, "r", encoding="utf-8-sig") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(self.config_path, "r") as f:
                return f.read()

    def _read_color(self, section, keys, default: RGB) -> RGB:
        """Reads the first present color key from a config section.

        Args:
            section (configparser.SectionProxy): The parsed config section.
            keys (Iterable[str]): Config keys to try in order; later entries
                are legacy names kept for backward compatibility.
            default (RGB): Color returned when none of the keys are present.

        Returns:
            RGB: The resolved (R, G, B) color.
        """
        for key in keys:
            if key in section:
                return self._hex_to_rgb(section[key])
        return default

    def load(self) -> AppConfig:
        """
        Parses config.cfg into a typed configuration dict, accepting both
        the current per-state color keys and the legacy shared-color keys.

        Returns:
            AppConfig: The tracked ticker symbols, resolved RGB colors, and
                animation timing settings.
        """
        parser = configparser.ConfigParser()
        # config.cfg has no section headers, so provide a default one for configparser
        parser.read_string(f"[DEFAULT]\n{self._read_text()}")
        section = parser["DEFAULT"]

        tickers, names = self.parse_tickers(section["TICKER"])
        if len(tickers) > MAX_TICKERS:
            tickers = tickers[:MAX_TICKERS]
            names = {symbol: name for symbol, name in names.items() if symbol in tickers}

        scroll_speed_ms = section.getint("SCROLL_SPEED_MS")
        if "DISPLAY_MS" in section:
            display_ms = section.getint("DISPLAY_MS")
        else:
            # Legacy configs stored the display time in seconds
            display_ms = round(section.getfloat("DISPLAY_SECONDS") * 1000)
        font_size = section.getint("FONT_SIZE", fallback=DEFAULT_FONT_SIZE)
        strip_width = section.getint("STRIP_WIDTH", fallback=DEFAULT_STRIP_WIDTH)
        if scroll_speed_ms <= 0:
            raise ValueError("SCROLL_SPEED_MS must be positive.")
        if display_ms <= 0:
            raise ValueError("DISPLAY_MS must be positive.")
        if font_size <= 0:
            raise ValueError("FONT_SIZE must be positive.")
        if not 0 <= strip_width <= MAX_STRIP_WIDTH:
            raise ValueError(f"STRIP_WIDTH must be between 0 and {MAX_STRIP_WIDTH}.")

        ticker_value_mode = section.get("TICKER_VALUE_MODE", DEFAULT_TICKER_VALUE_MODE).strip().lower()
        if ticker_value_mode not in TICKER_VALUE_MODES:
            raise ValueError(f"TICKER_VALUE_MODE must be one of: {', '.join(TICKER_VALUE_MODES)}.")

        state_colors: Dict[str, StateColors] = {}
        for state in MARKET_STATES:
            prefix = f"COLOR_{state.upper()}"
            state_colors[state] = StateColors(
                positive=self._read_color(section, (f"{prefix}_POSITIVE", "COLOR_POSITIVE"), DEFAULT_POSITIVE_COLOR),
                negative=self._read_color(section, (f"{prefix}_NEGATIVE", "COLOR_NEGATIVE"), DEFAULT_NEGATIVE_COLOR),
                strip=self._read_color(section, (f"{prefix}_STRIP", prefix), DEFAULT_STRIP_COLORS[state]),
            )

        band_colors = {
            band: self._read_color(section, (f"COLOR_{band.upper()}",), DEFAULT_BAND_COLORS[band])
            for band in PRICE_BANDS
        }

        return AppConfig(
            tickers=tickers,
            names=names,
            alerts=section.get("ALERTS", fallback="").strip(),
            bg_color=self._read_color(section, ("COLOR_BACKGROUND", "COLOR_CLOSED"), DEFAULT_BG_COLOR),
            transparent_background=section.getboolean("TRANSPARENT_BACKGROUND", fallback=False),
            error_color=self._read_color(section, ("COLOR_ERROR", "COLOR_ERROR_BORDER"), DEFAULT_ERROR_COLOR),
            state_colors=state_colors,
            band_colors=band_colors,
            scroll_speed_ms=scroll_speed_ms,
            display_ms=display_ms,
            continuous_scroll=section.getboolean("CONTINUOUS_SCROLL", fallback=True),
            taskbar_widget=section.getboolean("TASKBAR_WIDGET", fallback=False),
            ticker_value_mode=ticker_value_mode,
            show_daily_high=section.getboolean("SHOW_DAILY_HIGH", fallback=False),
            show_daily_low=section.getboolean("SHOW_DAILY_LOW", fallback=False),
            high_low_pct=section.getboolean("HIGH_LOW_PCT", fallback=False),
            font_size=font_size,
            strip_width=strip_width,
        )

    def save(self, config: AppConfig):
        """Persist a configuration dict to the on-disk config file.

        Args:
            config (AppConfig): A fully populated configuration dictionary.

        Returns:
            None: The file is written in-place at config_path.

        Raises:
            ValueError: If the ticker list or timing values are invalid.
            OSError: If the config file cannot be written.
        """
        tickers = [symbol.strip().upper() for symbol in config['tickers'] if symbol.strip()]
        if not tickers:
            raise ValueError("At least one ticker is required.")
        if len(tickers) > MAX_TICKERS:
            raise ValueError(f"Ticker list exceeds MAX_TICKERS ({MAX_TICKERS}).")

        scroll_speed_ms = int(config['scroll_speed_ms'])
        display_ms = int(config['display_ms'])
        font_size = int(config.get('font_size', DEFAULT_FONT_SIZE))
        strip_width = int(config.get('strip_width', DEFAULT_STRIP_WIDTH))
        if scroll_speed_ms <= 0:
            raise ValueError("SCROLL_SPEED_MS must be positive.")
        if display_ms <= 0:
            raise ValueError("DISPLAY_MS must be positive.")
        if font_size <= 0:
            raise ValueError("FONT_SIZE must be positive.")
        if not 0 <= strip_width <= MAX_STRIP_WIDTH:
            raise ValueError(f"STRIP_WIDTH must be between 0 and {MAX_STRIP_WIDTH}.")

        ticker_value_mode = str(config.get('ticker_value_mode', DEFAULT_TICKER_VALUE_MODE)).strip().lower()
        if ticker_value_mode not in TICKER_VALUE_MODES:
            raise ValueError(f"TICKER_VALUE_MODE must be one of: {', '.join(TICKER_VALUE_MODES)}.")

        state_colors = config['state_colors']
        missing_states = [state for state in MARKET_STATES if state not in state_colors]
        if missing_states:
            raise ValueError(f"Missing color scheme for market states: {', '.join(missing_states)}.")

        lines = [
            f"# Symbols to track, comma separated (max {MAX_TICKERS} — '.VN' symbols share one request,",
            "# every other symbol takes one per minute);",
            "# add :Name after a symbol to label it, e.g. GC=F:Gold",
            f"TICKER={self.format_tickers(tickers, config.get('names', {}))}",
            "",
            "# Price alerts: a Windows notification when a symbol reaches a price, comma separated,",
            "# e.g. VHM>=65, BTC<=80000 (Vietnamese stocks in thousands of VND, as on the taskbar)",
            f"ALERTS={config.get('alerts', '').strip()}",
            "",
            "# Static icon background color",
            f"COLOR_BACKGROUND={self._rgb_to_hex(config['bg_color'])}",
            "",
            "# Leave the icon background transparent instead (the background color still",
            "# picks the black or white symbol text)",
            f"TRANSPARENT_BACKGROUND={'true' if config.get('transparent_background', False) else 'false'}",
            "",
            "# Strip color shown when a data fetch fails",
            f"COLOR_ERROR={self._rgb_to_hex(config['error_color'])}",
            "",
            "# Per-market-state colors: positive/negative percentage text and the bottom strip",
        ]
        for state in MARKET_STATES:
            scheme = state_colors[state]
            missing = [k for k in ('positive', 'negative', 'strip') if k not in scheme]
            if missing:
                raise ValueError(
                    f"Missing {', '.join(missing)} color(s) for market state '{state}'."
                )
            prefix = f"COLOR_{state.upper()}"
            lines += [
                f"{prefix}_POSITIVE={self._rgb_to_hex(scheme['positive'])}",
                f"{prefix}_NEGATIVE={self._rgb_to_hex(scheme['negative'])}",
                f"{prefix}_STRIP={self._rgb_to_hex(scheme['strip'])}",
            ]
        band_colors = config.get('band_colors', DEFAULT_BAND_COLORS)
        lines += [
            "",
            "# Price text colors at the daily ceiling, the reference price, and the daily floor,",
            "# for exchanges that publish daily price limits (Vietnam)",
        ]
        lines += [
            f"COLOR_{band.upper()}={self._rgb_to_hex(band_colors.get(band, DEFAULT_BAND_COLORS[band]))}"
            for band in PRICE_BANDS
        ]
        lines += [
            "",
            "# Ticker symbol scroll speed, in milliseconds per character",
            f"SCROLL_SPEED_MS={scroll_speed_ms}",
            "",
            "# How long each price stays visible after scrolling, in milliseconds",
            f"DISPLAY_MS={display_ms}",
            "",
            "# Scroll every tracked symbol continuously in one line instead of one at a time",
            f"CONTINUOUS_SCROLL={'true' if config.get('continuous_scroll', True) else 'false'}",
            "",
            "# Show the prices as text on the taskbar next to the tray icons (Windows only);",
            "# the tray icon then shows the app icon, keeping its menu and tooltip",
            f"TASKBAR_WIDGET={'true' if config.get('taskbar_widget', False) else 'false'}",
            "",
            "# What the ticker shows per symbol: percentage, price, or both",
            "# (the static icon shows the price only in price mode, otherwise the percentage)",
            f"TICKER_VALUE_MODE={ticker_value_mode}",
            "",
            "# Show the daily high (↑) and/or low (↓) in the continuous ticker;",
            "# HIGH_LOW_PCT also appends their change percentage vs. previous close",
            f"SHOW_DAILY_HIGH={'true' if config.get('show_daily_high', False) else 'false'}",
            f"SHOW_DAILY_LOW={'true' if config.get('show_daily_low', False) else 'false'}",
            f"HIGH_LOW_PCT={'true' if config.get('high_low_pct', False) else 'false'}",
            "",
            "# Icon font size in pixels (values above ~44 may clip the percentage text)",
            f"FONT_SIZE={font_size}",
            "",
            "# Height of the market-state/error strip at the bottom of the icon, in pixels (0 disables it)",
            f"STRIP_WIDTH={strip_width}",
        ]

        with open(self.config_path, "w", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
