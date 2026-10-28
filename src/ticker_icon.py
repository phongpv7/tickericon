import itertools
import time
import threading
from datetime import datetime
from typing import Dict
from zoneinfo import ZoneInfo
import pystray

from src.market_api import MarketAPI, StockData
from src.icon_generator import IconGenerator
from src.continuous_scroll import ContinuousScrollGenerator
from src.config_handler import ConfigHandler, display_name
from src.config_window import ConfigWindow
from src.taskbar_widget import TaskbarWidget
from src.alerts import AlertMonitor, parse_alerts

# Animation frame rate for the ticker symbol scroll (higher = smoother, more CPU)
SCROLL_FRAME_INTERVAL_S = 0.04

# Taskbar widget color for a stale (failed to refresh) price
WIDGET_STALE_COLOR = (140, 140, 140)

# Currencies whose stocks trade in whole units, so their prices are shown without decimals
WHOLE_UNIT_CURRENCIES = ('VND', 'KRW', 'IDR')


class TickerIcon:
    """
    Main application controller. Runs two independent background threads —
    one polling market data, one driving the icon animation — so a slow or
    failed API call never stalls the tray icon's visuals.
    """

    def __init__(self, config_path: str = "config.cfg"):
        """
        Args:
            config_path (str): Path to the config.cfg file to load.
        """
        self.config_path = config_path
        self.config = ConfigHandler(config_path).load()
        self.api = MarketAPI(self.config['tickers'])
        self.icon_gen = IconGenerator(self.config)
        self.continuous_gen = ContinuousScrollGenerator(self.icon_gen, self.config)
        self.alert_monitor = self._new_alert_monitor(self.config)
        self.display_thread = None

        # Latest fetched data per symbol, shared between the fetch and display
        # threads, guarded by self.lock.
        self.snapshot: Dict[str, StockData] = {
            symbol: StockData(symbol=symbol) for symbol in self.config['tickers']
        }
        self.lock = threading.Lock()

        # Symbols currently shown by the display loop (quick menu toggles);
        # data is still fetched for every configured ticker. Guarded by self.lock.
        self.enabled_symbols = set(self.config['tickers'])

        # Set by the icon-click handler to jump the display to the next symbol.
        self.skip_requested = threading.Event()

        # Set when the enabled-symbol set changes so display loops re-read it.
        self.display_dirty = threading.Event()

        # Set by the Refresh Now menu item to trigger an immediate data fetch.
        self.refresh_requested = threading.Event()

        # Set once the first fetch completes, so the display loop doesn't
        # show stale/zeroed data before real data is available.
        self.data_ready = threading.Event()

        self.running = False
        self.tray_icon = None
        self.settings_lock = threading.Lock()
        self.settings_open = False

        # Optional price display on the taskbar itself (see TASKBAR_WIDGET)
        self.taskbar_widget = None

    def start(self):
        """Initializes and runs the system tray application."""
        self.running = True

        # Generate initial placeholder icon
        first_symbol = self.config['tickers'][0]
        initial_image = self.icon_gen.generate_app_icon()
        self.tray_icon = pystray.Icon(
            "stock_ticker",
            initial_image,
            title=f"Loading {first_symbol}...",
            menu=self._build_menu()
        )

        # Start the background fetch and display loops
        threading.Thread(target=self._fetch_loop, daemon=True).start()
        self.display_thread = threading.Thread(target=self._display_loop, daemon=True)
        self.display_thread.start()
        self._sync_taskbar_widget()

        # Run system tray (this blocks the main thread until closed)
        self.tray_icon.run()

    def _sync_taskbar_widget(self):
        """Opens or closes the taskbar widget to match the configuration."""
        with self.lock:
            wanted = bool(self.config.get('taskbar_widget')) and TaskbarWidget.supported()
        if wanted and self.taskbar_widget is None:
            self.taskbar_widget = TaskbarWidget(self._taskbar_widget_segments)
            self.taskbar_widget.start()
        elif not wanted and self.taskbar_widget is not None:
            self.taskbar_widget.stop()
            self.taskbar_widget = None

    def _taskbar_widget_segments(self):
        """
        Builds the taskbar widget's text: the current symbol of the rotation
        (see _rotating_symbol) as its name, current price, and daily change,
        colored like the tray icon values, or grayed out while its last
        fetch failed.
        Called on the widget's thread.

        Returns:
            List[Tuple[str, tuple]]: The (text, RGB) runs to draw; empty
                (hiding the widget) while loading.
        """
        if not self.data_ready.is_set():
            return []
        with self.lock:
            enabled = [s for s in self.config['tickers'] if s in self.enabled_symbols]
            symbol = self._rotating_symbol(enabled, self.config['display_ms'])
            data = self.snapshot.get(symbol, StockData(symbol=symbol)) if symbol else None
            names = self.config.get('names', {})
            icon_gen = self.icon_gen
        if data is None:
            return []

        if data.has_error:
            color = WIDGET_STALE_COLOR
        else:
            color = icon_gen.text_color(data.state, data.change_pct, data.price_band)
        price = self._widget_price(data.price, data.currency)
        return [(f"{display_name(symbol, names)} {price} {data.change_pct:+.2f}%", color)]

    @staticmethod
    def _widget_price(price: float, currency: str) -> str:
        """
        Formats a price for the taskbar widget.

        Args:
            price (float): The price to format.
            currency (str): Quote currency code, e.g. 'VND'.

        Returns:
            str: VND in thousands like on Vietnamese price boards ('63.5');
                otherwise with thousands separators and fewer decimals as the
                price grows ('111.28', '4,064.6', '81,404').
        """
        if currency == 'VND':
            return IconGenerator.format_icon_price(price, currency)
        decimals = 2 if price < 1000 else 1 if price < 10000 else 0
        return f"{price:,.{decimals}f}"

    @staticmethod
    def _rotating_symbol(enabled, display_ms: int):
        """
        Picks the symbol the taskbar widget shows now: it rotates through the
        enabled symbols, each staying up for the configured display time.

        Args:
            enabled (List[str]): The enabled symbols, in configuration order.
            display_ms (int): How long each symbol stays up, in milliseconds.

        Returns:
            Optional[str]: The symbol to show, or None if none are enabled.
        """
        if not enabled:
            return None
        return enabled[int(time.monotonic() * 1000 // display_ms) % len(enabled)]

    def _build_menu(self):
        """
        Builds the tray context menu.

        Returns:
            pystray.Menu: The menu attached to the tray icon.
        """
        return pystray.Menu(
            # default=True makes a left click on the icon trigger this item.
            # Items with nothing to do in the current setup are hidden; the
            # menu is rebuilt after a settings save.
            pystray.MenuItem('Next Symbol', self.on_next_symbol, default=True,
                             visible=lambda item: self._cycles_symbols()),
            # Realtime symbols refresh every second or faster anyway
            pystray.MenuItem('Refresh Now', self.on_refresh, visible=lambda item: not self.api.realtime),
            pystray.MenuItem('Symbols', pystray.Menu(self._symbol_menu_items),
                             visible=lambda item: len(self.config['tickers']) > 1),
            pystray.MenuItem('Settings', self.on_settings),
            pystray.MenuItem('Quit', self.on_quit),
        )

    def _cycles_symbols(self) -> bool:
        """
        Returns:
            bool: True when the tray icon rotates through several symbols, so
                jumping to the next one does something. The taskbar widget
                shows every enabled symbol at once instead.
        """
        return self.taskbar_widget is None and len(self._get_enabled_symbols()) > 1

    def on_quit(self, icon, menu_item):
        """
        Callback for the Quit menu option.

        Args:
            icon (pystray.Icon): The tray icon instance to stop.
            menu_item (pystray.MenuItem): The menu item that was clicked.
        """
        self.running = False
        if self.taskbar_widget is not None:
            self.taskbar_widget.stop()
        icon.stop()

    def on_next_symbol(self, icon, menu_item):
        """
        Default tray action, triggered by clicking the icon: jumps the
        display to the next enabled symbol immediately. Does nothing when
        fewer than two symbols are enabled.

        Args:
            icon (pystray.Icon): The tray icon instance.
            menu_item (pystray.MenuItem): The menu item that was activated.
        """
        if len(self._get_enabled_symbols()) > 1:
            self.skip_requested.set()

    def on_refresh(self, icon, menu_item):
        """
        Callback for the Refresh Now menu option: wakes the fetch loop so
        market data is refreshed immediately.

        Args:
            icon (pystray.Icon): The tray icon instance.
            menu_item (pystray.MenuItem): The menu item that was clicked.
        """
        self.refresh_requested.set()

    def on_toggle_symbol(self, symbol: str):
        """
        Toggles a symbol's visibility in the display rotation. This is a
        view-only filter: data is still fetched for every configured
        symbol. The last enabled symbol cannot be disabled.

        Args:
            symbol (str): The configured ticker symbol to toggle.
        """
        with self.lock:
            if symbol in self.enabled_symbols:
                enabled = {s for s in self.config['tickers'] if s in self.enabled_symbols}
                if len(enabled) <= 1:
                    return
                self.enabled_symbols.discard(symbol)
            else:
                self.enabled_symbols.add(symbol)
        self.display_dirty.set()
        if self.tray_icon is not None:
            self.tray_icon.update_menu()

    def _get_enabled_symbols(self):
        """
        Returns:
            List[str]: The enabled symbols, in configuration order.
        """
        with self.lock:
            return [s for s in self.config['tickers'] if s in self.enabled_symbols]

    def _symbol_menu_items(self):
        """
        Generates the dynamic 'Symbols' submenu: one checkable item per
        configured ticker, rebuilt every time the menu opens so it tracks
        config saves and quick toggles.

        Returns:
            List[pystray.MenuItem]: The submenu items.
        """
        with self.lock:
            symbols = list(self.config['tickers'])
            names = self.config.get('names', {})

        def make_action(symbol):
            # pystray rejects actions with >2 args, so bind via closure
            return lambda icon, item: self.on_toggle_symbol(symbol)

        return [
            pystray.MenuItem(
                display_name(symbol, names),
                make_action(symbol),
                checked=lambda item, s=symbol: s in self.enabled_symbols,
            )
            for symbol in symbols
        ]

    def _consume_skip(self) -> bool:
        """
        Clears a pending next-symbol request, if any.

        Returns:
            bool: True if a jump was requested since the last call.
        """
        if self.skip_requested.is_set():
            self.skip_requested.clear()
            return True
        return False

    def on_settings(self, icon, menu_item):
        """Open a settings window from the tray menu.

        Args:
            icon (pystray.Icon): The tray icon instance.
            menu_item (pystray.MenuItem): The menu item that triggered the call.

        Returns:
            None: The window runs on its own daemon thread so the tray menu
                handler is never blocked and shutdown is never held up.
        """
        with self.settings_lock:
            if self.settings_open:
                return
            self.settings_open = True
        threading.Thread(target=self._run_settings_window, daemon=True).start()

    def _run_settings_window(self):
        """Creates and runs the settings window, releasing the reopen guard on close.

        Returns:
            None: Blocks its own thread until the window is closed.
        """
        try:
            window = ConfigWindow(self.config_path, self.config, on_save=self._apply_config)
            window.show()
        except RuntimeError as exc:
            self._notify(str(exc))
        finally:
            with self.settings_lock:
                self.settings_open = False

    def _apply_config(self, config):
        """Reloads the in-memory runtime state after a config save.

        Args:
            config (dict): The updated application configuration.

        Returns:
            None: The active fetch/display loops are synchronized with the new values.
        """
        with self.lock:
            old_snapshot = self.snapshot
            old_api = self.api
            self.config = config
            self.api = MarketAPI(self.config['tickers'])
            self.icon_gen = IconGenerator(self.config)
            self.continuous_gen = ContinuousScrollGenerator(self.icon_gen, self.config)
            self.alert_monitor = self._new_alert_monitor(self.config)
            # Keep already-fetched data for retained tickers so a settings save
            # (e.g. toggling scroll mode) never shows zeroed placeholders.
            self.snapshot = {
                symbol: old_snapshot.get(symbol, StockData(symbol=symbol))
                for symbol in self.config['tickers']
            }
            if any(symbol not in old_snapshot for symbol in self.config['tickers']):
                self.data_ready.clear()
            # Keep quick-toggle states for retained tickers; new tickers start enabled
            old_enabled = self.enabled_symbols
            self.enabled_symbols = {
                symbol for symbol in self.config['tickers']
                if symbol in old_enabled or symbol not in old_snapshot
            }
            if not self.enabled_symbols:
                self.enabled_symbols = set(self.config['tickers'])
        # The new instance opens its own price streams for the new symbols
        old_api.close()
        self.display_dirty.set()
        self._sync_taskbar_widget()

        if self.tray_icon is not None:
            self.tray_icon.update_menu()  # re-evaluates which menu items apply
            self.tray_icon.notify("Settings saved. TickerIcon will use the new configuration.", "TickerIcon")

    def _notify(self, message: str):
        if self.tray_icon is not None:
            self.tray_icon.notify(message, "TickerIcon")

    @staticmethod
    def _new_alert_monitor(config) -> AlertMonitor:
        """
        Args:
            config (dict): The application configuration.

        Returns:
            AlertMonitor: Watches the configured price alerts. Malformed
                entries in a hand-edited config file are skipped; the
                Settings window rejects them before saving.
        """
        alerts = parse_alerts(config.get('alerts', ''), config['tickers'], config.get('names', {}), strict=False)
        return AlertMonitor(alerts)

    def _notify_alerts(self, fired):
        """
        Shows one Windows notification listing the alerts that just reached
        their targets, e.g. 'VHM 65.2 ≥ 65'.

        Args:
            fired (List[Tuple[Alert, StockData]]): Alerts from AlertMonitor.check.
        """
        if not fired or self.tray_icon is None:
            return
        lines = [
            f"{self._display_name(alert.symbol)} {self._widget_price(data.price, data.currency)} "
            f"{'≥' if alert.above else '≤'} {alert.text}"
            for alert, data in fired
        ]
        # Windows caps notification text at 256 characters
        self.tray_icon.notify("\n".join(lines)[:255], "Price alert")

    def _fetch_loop(self):
        """Background loop that periodically refreshes market data for all tickers."""
        while self.running:
            # Refresh Now also refetches symbols that were fetched moments ago
            force = self.refresh_requested.is_set()
            self.refresh_requested.clear()
            with self.lock:
                api = self.api
                config_version = self.config
                previous_snapshot = self.snapshot

            # Pass the last snapshot so failed fetches keep showing last known values
            snapshot = api.fetch_all(previous_snapshot, force=force)

            with self.lock:
                if api is not self.api or config_version is not self.config:
                    continue
                self.snapshot = snapshot
                self.data_ready.set()
                alert_monitor = self.alert_monitor

            self._notify_alerts(alert_monitor.check(snapshot))

            all_closed = all(
                data.state == 'closed' and not data.has_error
                for data in snapshot.values()
            )

            if all_closed:
                next_open = MarketAPI.get_next_open_time(snapshot)
                if next_open is not None:
                    now = datetime.now(ZoneInfo("UTC"))
                    sleep_seconds = max(60, int((next_open - now).total_seconds()))
                else:
                    sleep_seconds = 60
            else:
                sleep_seconds = api.refresh_interval(snapshot)

            self._sleep_between_fetches(sleep_seconds)

    def _sleep_between_fetches(self, seconds: float):
        """
        Sleeps between fetches, waking early on shutdown, a manual refresh
        request, or when a config save cleared data_ready (new tickers need
        an immediate fetch).

        Args:
            seconds (float): Maximum time to sleep for, in seconds.
        """
        end_time = time.monotonic() + seconds
        while (self.running
                and self.data_ready.is_set() and not self.refresh_requested.is_set()
                and time.monotonic() < end_time):
            time.sleep(0.1)

    def _display_loop(self):
        """Background loop that cycles through the enabled tickers, scrolling then showing each one."""
        while self.running:
            if not self.data_ready.is_set():
                self.tray_icon.icon = self.icon_gen.generate_app_icon()
                self.tray_icon.title = "TickerIcon (Loading...)"
                self.data_ready.wait(timeout=0.1)
                continue

            # Clear before reading so a toggle landing after the read still wakes us
            self.display_dirty.clear()
            with self.lock:
                enabled = [s for s in self.config['tickers'] if s in self.enabled_symbols]
                continuous_scroll = self.config['continuous_scroll']

            # The taskbar widget shows the prices; the tray icon keeps the menu
            # and a tooltip for the symbol the widget is showing
            if self.taskbar_widget is not None:
                with self.lock:
                    symbol = self._rotating_symbol(enabled, self.config['display_ms'])
                    data = self.snapshot.get(symbol) if symbol else None
                self.skip_requested.clear()
                self.tray_icon.icon = self.icon_gen.generate_app_icon()
                if data is not None:
                    self._update_tooltip(data)
                self._interruptible_sleep(1.0)
                continue

            # With a single enabled symbol the name scroll is just noise: the
            # tooltip already identifies it, so keep the value on screen permanently.
            if len(enabled) == 1:
                with self.lock:
                    data = self.snapshot.get(enabled[0], StockData(symbol=enabled[0]))
                self.skip_requested.clear()  # nothing to jump to
                self._show_value(data)
                self._interruptible_sleep(1.0)
                continue

            if continuous_scroll:
                self._display_continuous_lap()
                if not self.running:
                    return
                continue

            with self.lock:
                display_ms = self.config['display_ms']
                scroll_speed_ms = self.config['scroll_speed_ms']

            if not enabled:
                self._interruptible_sleep(0.25)
                continue

            for symbol in itertools.cycle(enabled):
                if not self.running:
                    return

                with self.lock:
                    current_enabled = [s for s in self.config['tickers'] if s in self.enabled_symbols]
                    if (current_enabled != enabled or self.config['continuous_scroll']
                            or len(current_enabled) == 1):
                        break
                    data = self.snapshot.get(symbol, StockData(symbol=symbol))
                    display_ms = self.config['display_ms']
                    scroll_speed_ms = self.config['scroll_speed_ms']

                label = self._display_name(symbol)
                try:
                    self._scroll_symbol(label, data.state, scroll_speed_ms, has_error=data.has_error)
                except TypeError:
                    self._scroll_symbol(label, data.state)
                if not self.running:
                    return

                # A click during the name scroll advances straight to the next symbol
                if self._consume_skip() or self.display_dirty.is_set():
                    continue

                self._show_value(data)
                self._interruptible_sleep(display_ms / 1000.0)
                self._consume_skip()

            if not self.running:
                return

    def _display_continuous_lap(self):
        """
        Runs one full pass of the continuous-scroll strip, built from the
        enabled tickers/snapshot captured at the start of the lap so a fetch
        that completes mid-lap never causes a visible rewind or reset.

        Returns:
            None: Frames are rendered directly onto the tray icon until the
                strip has fully scrolled past, the enabled symbols change,
                or the app stops.
        """
        self.display_dirty.clear()
        with self.lock:
            enabled = [s for s in self.config['tickers'] if s in self.enabled_symbols]
            scroll_speed_ms = self.config['scroll_speed_ms']
            snapshot = dict(self.snapshot)
            continuous_gen = self.continuous_gen

        if not enabled:
            self._interruptible_sleep(0.25)
            return

        continuous_gen.build(enabled, snapshot)
        pixels_per_frame = self._continuous_pixels_per_frame(scroll_speed_ms)

        offset = 0
        last_symbol = None
        while offset < continuous_gen.width:
            if not self.running:
                return
            if self.display_dirty.is_set():
                return  # enabled symbols changed; rebuild the strip next lap

            if self._consume_skip():
                offset = continuous_gen.next_segment_offset(offset)

            data = continuous_gen.symbol_at(offset)
            if data is not None and data.symbol != last_symbol:
                self._update_tooltip(data)
                last_symbol = data.symbol

            self.tray_icon.icon = continuous_gen.render_frame(offset)
            time.sleep(SCROLL_FRAME_INTERVAL_S)
            offset += pixels_per_frame

    def _continuous_pixels_per_frame(self, scroll_speed_ms: float) -> int:
        """
        Converts the configured per-character scroll speed into a pixel
        step for the continuous-scroll strip's animation frame cadence.

        Args:
            scroll_speed_ms (float): Configured milliseconds-per-character scroll speed.

        Returns:
            int: Pixels to advance the strip offset on each animation frame (at least 1).
        """
        avg_char_width = max(self.icon_gen.measure_text_width("0123456789") / 10, 1)
        chars_per_second = 1000.0 / scroll_speed_ms
        pixels_per_second = chars_per_second * avg_char_width
        return max(1, round(pixels_per_second * SCROLL_FRAME_INTERVAL_S))

    def _scroll_symbol(self, symbol: str, state: str, scroll_speed_ms: float = None, has_error: bool = False):
        """
        Animates `symbol` scrolling right-to-left across the icon.

        Args:
            symbol (str): The ticker symbol to scroll, e.g. 'AMD'.
            state (str): Current market state, used for the background color.
            scroll_speed_ms (float, optional): Current scroll speed in ms per char.
            has_error (bool): Whether the latest fetch for this symbol failed.

        Returns:
            None: One animation frame sequence is rendered onto the tray icon.
        """
        if scroll_speed_ms is None:
            with self.lock:
                scroll_speed_ms = self.config['scroll_speed_ms']

        text_width = self.icon_gen.measure_text_width(symbol)
        distance = 64 + text_width

        duration_s = max(len(symbol), 1) * (scroll_speed_ms / 1000.0)
        steps = max(int(duration_s / SCROLL_FRAME_INTERVAL_S), 1)

        for step in range(steps + 1):
            if not self.running or self.skip_requested.is_set():
                return
            offset = int(distance * step / steps)
            self.tray_icon.icon = self.icon_gen.generate_scroll_frame(symbol, state, offset, has_error=has_error)
            time.sleep(SCROLL_FRAME_INTERVAL_S)

    def _show_value(self, data: StockData):
        """
        Updates the icon and tooltip to show a ticker's latest value: its
        price in the 'price' ticker value mode, otherwise its price change.

        Args:
            data (StockData): The ticker snapshot to display.
        """
        with self.lock:
            show_price = self.config.get('ticker_value_mode') == 'price'
        if show_price:
            self.tray_icon.icon = self.icon_gen.generate_price_frame(
                data.price, data.currency, data.change_pct, data.state,
                has_error=data.has_error, band=data.price_band,
            )
        else:
            self.tray_icon.icon = self.icon_gen.generate_value_frame(
                data.change_pct, data.state, has_error=data.has_error, band=data.price_band,
            )
        self._update_tooltip(data)

    def _update_tooltip(self, data: StockData):
        """
        Refreshes only the tray tooltip for a ticker, without touching the icon image.

        Args:
            data (StockData): The ticker snapshot to describe in the tooltip.

        Returns:
            None: The tray icon's title is updated in place.
        """
        self.tray_icon.title = self._build_tooltip(data, self._display_name(data.symbol))

    def _display_name(self, symbol: str) -> str:
        """
        Args:
            symbol (str): A configured ticker symbol.

        Returns:
            str: The label shown for it (see config_handler.display_name).
        """
        return display_name(symbol, self.config.get('names', {}))

    @staticmethod
    def _build_tooltip(data: StockData, name: str = '') -> str:
        """
        Formats a ticker's tooltip text.

        Args:
            data (StockData): The ticker snapshot to describe.
            name (str): The ticker's display name; defaults to its symbol.

        Returns:
            str: The multi-line tooltip text for the tray icon.
        """
        state_formatted = data.state.replace("_", " ").title()
        err_indicator = " [Update Failed]" if data.has_error else ""
        market_part = f" - {data.exchange}" if data.exchange else ""
        price = TickerIcon._format_price

        return (
            f"{name or data.symbol}{market_part} ({state_formatted}){err_indicator}\n"
            f"Price: {price(data.price, data.currency)} ({data.change_pct:+.2f}%)\n"
            f"High: {price(data.day_high, data.currency)} | Low: {price(data.day_low, data.currency)}"
        )

    @staticmethod
    def _format_price(value: float, currency: str) -> str:
        """
        Formats a price for the tooltip in its quote currency.

        Args:
            value (float): The price to format.
            currency (str): The quote currency code from Yahoo Finance, e.g.
                'VND'; empty when unknown.

        Returns:
            str: '$123.45' for US dollars (or an unknown currency), otherwise
                the amount followed by its currency code, e.g. '63,500 VND'.
        """
        if currency in ('', 'USD'):
            return f"${value:.2f}"
        decimals = 0 if currency in WHOLE_UNIT_CURRENCIES else 2
        return f"{value:,.{decimals}f} {currency}"

    def _interruptible_sleep(self, seconds: float):
        """
        Sleeps in small increments, waking early on shutdown, a next-symbol
        jump request, or an enabled-symbol change.

        Args:
            seconds (float): Total time to sleep for, in seconds.
        """
        end_time = time.monotonic() + seconds
        while (self.running
                and not self.skip_requested.is_set() and not self.display_dirty.is_set()
                and time.monotonic() < end_time):
            time.sleep(0.1)
