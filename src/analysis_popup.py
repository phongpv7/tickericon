import ctypes
import json
import queue
import sys
import threading
import time
from typing import Callable, Dict, FrozenSet, Optional, Protocol, Sequence, Set, Tuple, Union

try:
    import tkinter as tk
except ModuleNotFoundError:  # Python built without Tkinter (headless/non-Windows)
    tk = None

from src import technical
from src.app_identity import app_data_dir
from src.market_api import StockData
from src.price_history import HISTORY_DAYS, RECENT_DAYS, Candles, merge_candles, with_live_price

if sys.platform == 'win32':
    from ctypes import wintypes

    # Private DLL handle, so these argtypes never clash with other modules'
    _user32 = ctypes.WinDLL('user32')
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = wintypes.LONG
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    _user32.SetWindowLongW.restype = wintypes.LONG
else:  # the popup is Windows-only, like the widget it belongs to
    _user32 = None

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000

# Hover this long before the popup opens, and move the mouse off both the
# widget and the popup for this long before it closes (checked on every
# redraw); while the widget shows a symbol without analysis (an international
# one), hovering checks again this often for a Vietnamese stock to come round
SHOW_DELAY_MS = 350
HIDE_DELAY_MS = 400
SHOW_RETRY_MS = 500

# How often the open popup checks for new data, redrawing whatever changed:
# as often as the app refreshes its prices
REDRAW_MS = 250

# How long fetched candles are reused before refetching, and failures before
# retrying; while the popup is open, the latest candles are also refetched
# every RECENT_REFRESH_S, bringing in a new day's candle
HISTORY_TTL_S = 600
HISTORY_RETRY_S = 30
RECENT_REFRESH_S = 10

# Daily candles on the chart: about six months, the span the Fibonacci swing is taken from
VISIBLE_CANDLES = technical.FIBONACCI_LOOKBACK

# The chart's optional overlays, each shown or hidden by clicking its chip
# above the chart, and the analysis column's tabs; the choices are kept in
# PREFERENCES_FILE in the app data folder
OVERLAYS = ('ma', 'bollinger', 'ichimoku', 'fibonacci')
DEFAULT_OVERLAYS = frozenset({'ma', 'ichimoku'})
TABS = (('notes', 'Nhận định'), ('indicators', 'Chỉ báo'))
PREFERENCES_FILE = 'chart.json'

# Where the popup waits while hidden: moving it, unlike showing it, never takes the focus
_OFFSCREEN = -10000

WIDTH = 1560
PAD = 16
# The chart's rows, top to bottom: overlay chips, prices, volume, RSI,
# stochastic, stochastic RSI, MACD, month labels
CHART_LEFT, CHART_RIGHT = PAD, 640
AXIS_WIDTH = 56
CHIPS_TOP = 60
PRICE_TOP, PRICE_BOTTOM = 92, 290
VOLUME_TOP, VOLUME_BOTTOM = 298, 330
RSI_TOP, RSI_BOTTOM = 344, 384
STOCHASTIC_TOP, STOCHASTIC_BOTTOM = 398, 438
STOCHASTIC_RSI_TOP, STOCHASTIC_RSI_BOTTOM = 452, 492
MACD_TOP, MACD_BOTTOM = 506, 550
MONTHS_Y = 564
# The order book and latest matches, right of the chart
BOOK_LEFT, BOOK_RIGHT = CHART_RIGHT + AXIS_WIDTH + 28, CHART_RIGHT + AXIS_WIDTH + 252
TRADE_ROWS = 10
# The rating and analysis, right of the order book: topics, then their findings
NOTES_LEFT = BOOK_RIGHT + 28
TOPIC_WIDTH = 124

BACKGROUND = '#1c1c1c'
GRID = '#333333'
TEXT = '#e6e6e6'
MUTED = '#8f8f8f'
DIM = '#5f5f5f'
CHIP_FILL = '#2b2b2b'
UP, DOWN = '#26a65b', '#e5484d'
UP_VOLUME, DOWN_VOLUME = '#1b5e3a', '#7d2a2d'
AVERAGES = (('MA20', '#f5a623'), ('MA50', '#4aa3ff'), ('MA200', '#c084fc'))
BANDS = '#7a8496'
RSI_LINE = '#b39ddb'
# %K and %D lines of the stochastics, like the MACD and its signal line
K_LINE, D_LINE = '#4aa3ff', '#f5a623'
TENKAN, KIJUN, CHIKOU = '#2dd4bf', '#f472b6', '#a3e635'
# The Ichimoku cloud where leading span A is above B (green) and below it (red):
# fills dark enough for the candles to show over them, and the spans' lines
CLOUD_UP, CLOUD_DOWN = '#173a29', '#3d1d22'
SPAN_A_LINE, SPAN_B_LINE = '#3f9a6b', '#a8505c'
FIBONACCI_LINE = '#c9a94e'
MACD_LINE, MACD_SIGNAL = K_LINE, D_LINE
RATING_COLORS = {
    technical.STRONG_BUY: '#1f9d55', technical.BUY: '#26a65b', technical.NEUTRAL: '#6b6b6b',
    technical.SELL: '#e5484d', technical.STRONG_SELL: '#c53030',
}
SIGNAL_NAMES = {1: ('Mua', UP), 0: ('Trung tính', MUTED), -1: ('Bán', DOWN), None: ('—', DIM)}
# Vietnamese price board colors for prices at the reference and the daily limits
BAND_COLORS = {'ceiling': '#d24dff', 'reference': '#f5c518', 'floor': '#3ba3ff'}

# Each overlay's chip, which is also its legend: (label, color) pairs
CHIPS = (
    ('ma', AVERAGES),
    ('bollinger', (('Bollinger', BANDS),)),
    ('ichimoku', (('Ichimoku', TEXT), ('Tenkan', TENKAN), ('Kijun', KIJUN), ('Chikou', CHIKOU), ('Mây', SPAN_A_LINE))),
    ('fibonacci', (('Fibonacci', FIBONACCI_LINE),)),
)

# What each market state means for the prices shown
STATE_NAMES = {
    'open': 'đang giao dịch',
    'premarket': 'phiên mở cửa ATO',
    'after_hours': 'giao dịch thỏa thuận',
    'closed': 'ngoài giờ giao dịch',
}

FONT = ('Segoe UI', 9)
BOLD = ('Segoe UI', 9, 'bold')
SMALL = ('Segoe UI', 8)
TITLE = ('Segoe UI', 14, 'bold')
BADGE = ('Segoe UI', 10, 'bold')


def _load_preferences() -> Tuple[FrozenSet[str], str]:
    """Returns: the overlays and tab last chosen, or the defaults."""
    try:
        with open(app_data_dir() / PREFERENCES_FILE, encoding='utf-8') as file:
            saved = json.load(file)
        overlays = frozenset(overlay for overlay in saved['overlays'] if overlay in OVERLAYS)
        tab = saved.get('tab') if saved.get('tab') in dict(TABS) else TABS[0][0]
        return overlays, tab
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return DEFAULT_OVERLAYS, TABS[0][0]


def _save_preferences(overlays: FrozenSet[str], tab: str) -> None:
    try:
        path = app_data_dir() / PREFERENCES_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'overlays': sorted(overlays), 'tab': tab}), encoding='utf-8')
    except OSError:
        pass  # the choice just isn't remembered


class AnalysisSource(Protocol):
    """What the popup needs from the app."""

    def pin_symbol(self) -> Optional[str]:
        """Holds the widget on the stock it shows now and returns it, or None if it has no analysis."""

    def unpin_symbol(self) -> None:
        """Lets the widget rotate through the symbols again."""

    def symbol_quote(self, symbol: str) -> Tuple[StockData, str]:
        """Returns the symbol's latest data and its display name."""

    def symbol_history(self, symbol: str, days: int) -> Candles:
        """Fetches daily candles `days` back; blocks, so it's called on a worker thread."""

    def format_price(self, price: float, currency: str) -> str:
        """Formats a price the way the widget shows it."""


class AnalysisPopup:
    """
    A daily chart, the order book, and a technical analysis of the
    Vietnamese stock on the taskbar widget, shown above it while the mouse
    rests on the widget or the popup. Lives on the widget's Tk thread; price
    history loads on worker threads.
    """

    def __init__(self, root, source: AnalysisSource):
        """
        Args:
            root (tk.Tk): The taskbar widget's window, on whose thread this runs.
            source (AnalysisSource): The app's data.
        """
        self.root = root
        self.source = source
        self._window = None
        self._canvas = None
        self._symbol: Optional[str] = None
        self._shown = False
        self._overlays, self._tab = _load_preferences()
        # The widget's (left, top, right, bottom) on screen, its top and bottom
        # being the taskbar's, to place the popup by; and the popup's own
        # (left, top, right, bottom) while shown
        self._anchor: Optional[Tuple[int, int, int, int]] = None
        self._placed: Optional[Tuple[int, int, int, int]] = None
        # Since when the mouse has been off the widget and the popup (time.monotonic())
        self._away_since: Optional[float] = None
        self._height = PRICE_BOTTOM
        self._geometry: Optional[str] = None
        self._redraw_job = None
        # What the popup was last drawn from, to skip redrawing when nothing changed
        self._drawn: Optional[tuple] = None
        # Fetched candles (or the fetch's error) per symbol, with when the
        # whole history arrived (time.monotonic()), and a count of the cache's
        # changes, which tells the redraws when to draw
        self._history: Dict[str, Tuple[Union[Candles, Exception], float]] = {}
        self._history_version = 0
        # When each symbol's latest candles were last asked for (time.monotonic())
        self._recent_at: Dict[str, float] = {}
        self._loading: Set[str] = set()
        # (symbol, days fetched, candles or error) from the worker threads
        self._results: queue.Queue = queue.Queue()

    def set_anchor(self, left: int, top: int, right: int, bottom: int) -> None:
        """
        Args:
            left (int): The widget's left edge.
            top (int): Its top edge, the taskbar's.
            right (int): Its right edge, which the popup lines up with.
            bottom (int): Its bottom edge, the taskbar's.
        """
        self._anchor = (left, top, right, bottom)

    def prepare(self) -> None:
        """
        Creates the popup window, off screen. Showing a window can take the
        focus from the one the user is working in, so this happens once, at
        startup; afterwards the popup only moves in and out of sight, which
        never does.
        """
        if self._window is not None:
            return
        window = tk.Toplevel(self.root)
        window.overrideredirect(True)
        window.attributes('-topmost', True)
        window.geometry(f"{WIDTH}x{self._height}+{_OFFSCREEN}+{_OFFSCREEN}")
        canvas = tk.Canvas(window, width=WIDTH, height=self._height, bg=BACKGROUND,
                           highlightthickness=0, borderwidth=0)
        canvas.pack(fill='both', expand=True)
        # The overlay chips and the tabs are clickable
        clickable = [(f'chip-{overlay}', lambda overlay=overlay: self._toggle_overlay(overlay)) for overlay in OVERLAYS]
        clickable += [(f'tab-{tab}', lambda tab=tab: self._select_tab(tab)) for tab, _ in TABS]
        for tag, action in clickable:
            canvas.tag_bind(tag, '<Button-1>', lambda _event, action=action: action())
            canvas.tag_bind(tag, '<Enter>', lambda _event: canvas.configure(cursor='hand2'))
            canvas.tag_bind(tag, '<Leave>', lambda _event: canvas.configure(cursor=''))
        window.update_idletasks()
        if _user32 is not None:
            # Clicking the popup must not take the focus either
            hwnd = wintypes.HWND(int(window.wm_frame(), 16))
            style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style | _WS_EX_NOACTIVATE)
        self._window, self._canvas = window, canvas

    def widget_entered(self) -> None:
        """Opens the popup after SHOW_DELAY_MS, if the mouse is still on the widget then."""
        if not self._shown:
            self.root.after(SHOW_DELAY_MS, self._show_if_hovered)

    def hide(self) -> None:
        """Moves the popup out of sight and lets the widget rotate again."""
        if not self._shown:
            return
        self._shown = False
        self._window.geometry(f"+{_OFFSCREEN}+{_OFFSCREEN}")
        self._geometry = self._drawn = self._placed = self._away_since = None
        if self._redraw_job is not None:
            self.root.after_cancel(self._redraw_job)
            self._redraw_job = None
        self.source.unpin_symbol()

    def _show_if_hovered(self) -> None:
        if self._shown or self._anchor is None or not self._mouse_on_widget():
            return
        symbol = self.source.pin_symbol()
        if symbol is None:
            # Not a Vietnamese stock: wait for one while the mouse stays
            self.root.after(SHOW_RETRY_MS, self._show_if_hovered)
            return
        self.prepare()
        self._symbol = symbol
        self._shown = True
        self._redraw()

    def _mouse_on_widget(self) -> bool:
        """Whether the mouse is anywhere on the widget, its keyed-out background included."""
        x, y = self.root.winfo_pointerxy()
        left, top, right, bottom = self._anchor
        return left <= x < right and top <= y < bottom

    def _mouse_gone(self) -> bool:
        """
        Tracks the mouse from where it actually is, rather than from enter
        and leave events, which Windows doesn't always deliver for windows
        with a keyed-out background, or that never take the focus.

        Returns:
            bool: True once the mouse has been off the widget, the popup, and
                the gap between them for HIDE_DELAY_MS.
        """
        x, y = self.root.winfo_pointerxy()
        near = self._mouse_on_widget()
        if not near and self._placed is not None:
            # The popup, stretched over the gap separating it from the taskbar
            left, top, right, bottom = self._placed
            taskbar_top, taskbar_bottom = self._anchor[1], self._anchor[3]
            near = left <= x < right and min(top, taskbar_bottom) <= y < max(bottom, taskbar_top)
        if near:
            self._away_since = None
            return False
        if self._away_since is None:
            self._away_since = time.monotonic()
        return time.monotonic() - self._away_since >= HIDE_DELAY_MS / 1000

    def _toggle_overlay(self, overlay: str) -> None:
        """Shows or hides one of the chart's overlays, remembering the choice."""
        self._overlays = self._overlays ^ {overlay}
        self._choice_made()

    def _select_tab(self, tab: str) -> None:
        """Switches the analysis column to a tab, remembering the choice."""
        self._tab = tab
        self._choice_made()

    def _choice_made(self) -> None:
        """Saves the overlays and tab chosen, and redraws at once."""
        _save_preferences(self._overlays, self._tab)
        if self._redraw_job is not None:
            self.root.after_cancel(self._redraw_job)
        self._redraw()

    def _redraw(self) -> None:
        """
        Redraws the open popup whenever its quote, order book, matches, or
        candles have changed, so the chart and the analysis follow the market
        as it trades, then schedules the next check; closes it instead once
        the mouse has gone.
        """
        self._redraw_job = None
        if not self._shown:
            return
        if self._mouse_gone():
            self.hide()
            return
        self._collect_results()
        data, name = self.source.symbol_quote(self._symbol)
        candles = self._candles(self._symbol)
        drawn_from = (data, name, self._history_version, self._overlays, self._tab)
        if drawn_from != self._drawn:
            self._drawn = drawn_from
            self._draw(data, name, candles)
        self._place()
        self._redraw_job = self.root.after(REDRAW_MS, self._redraw)

    def _place(self) -> None:
        """
        Puts the popup right-aligned with the widget, just above the taskbar,
        or just below it when the taskbar is at the top of the screen.
        """
        _, taskbar_top, right, taskbar_bottom = self._anchor
        x = max(0, right - WIDTH)
        if taskbar_top > self.root.winfo_screenheight() / 2:
            y = max(0, taskbar_top - self._height - 8)
        else:
            y = taskbar_bottom + 8
        self._placed = (x, y, x + WIDTH, y + self._height)
        geometry = f"{WIDTH}x{self._height}+{x}+{y}"
        if geometry != self._geometry:
            self._geometry = geometry
            self._window.geometry(geometry)

    def _candles(self, symbol: str) -> Union[Candles, Exception, None]:
        """
        Returns:
            Union[Candles, Exception, None]: The symbol's cached candles, or the
                error fetching them, or None while they load. Starts a fetch on
                a worker thread: of the whole history when there's nothing
                fresh in the cache, otherwise of the latest candles every
                RECENT_REFRESH_S.
        """
        result, fetched_at = self._history.get(symbol, (None, float('-inf')))
        if symbol in self._loading:
            return result
        now = time.monotonic()
        max_age = HISTORY_RETRY_S if isinstance(result, Exception) else HISTORY_TTL_S
        if now - fetched_at >= max_age:
            days = HISTORY_DAYS
        elif isinstance(result, Candles) and now - self._recent_at.get(symbol, fetched_at) >= RECENT_REFRESH_S:
            days = RECENT_DAYS
        else:
            return result
        self._loading.add(symbol)
        self._recent_at[symbol] = now
        threading.Thread(target=self._fetch, args=(symbol, days), daemon=True).start()
        return result

    def _fetch(self, symbol: str, days: int) -> None:
        """Fetches a symbol's candles `days` back; runs on a worker thread."""
        try:
            result = self.source.symbol_history(symbol, days)
        except Exception as exc:
            result = exc
        self._results.put((symbol, days, result))

    def _collect_results(self) -> None:
        """Caches the candles the worker threads have fetched since the last check."""
        while True:
            try:
                symbol, days, result = self._results.get_nowait()
            except queue.Empty:
                return
            self._loading.discard(symbol)
            previous, fetched_at = self._history.get(symbol, (None, float('-inf')))
            if days == RECENT_DAYS:
                # The latest candles update the history; a failure waits for the next refresh
                if not isinstance(result, Candles) or not isinstance(previous, Candles):
                    continue
                self._history[symbol] = (merge_candles(previous, result), fetched_at)
            else:
                if isinstance(result, Exception) and isinstance(previous, Candles):
                    result = previous  # a failed refresh keeps showing the older candles
                self._history[symbol] = (result, time.monotonic())
            self._history_version += 1

    def _draw(self, data: StockData, name: str, candles: Union[Candles, Exception, None]) -> None:
        """
        Draws the popup for the current stock: its latest quote, order book,
        and matches, and its candles brought up to that quote, with their
        analysis.

        Args:
            data (StockData): The stock's latest data.
            name (str): Its display name.
            candles (Union[Candles, Exception, None]): Its cached candles, the
                error fetching them, or None while they load.
        """
        canvas = self._canvas
        canvas.delete('all')

        def format_price(price: float) -> str:
            return self.source.format_price(price, data.currency)

        price_color = self._draw_header(data, name, format_price, candles)
        self._draw_chips()
        bottom = max(MONTHS_Y + 8, self._draw_book(data, format_price))
        message = None
        if isinstance(candles, Candles) and len(candles) >= 2:
            try:
                candles = with_live_price(candles, data.price, data.volume)
                foreign = (data.foreign_bought, data.foreign_sold, data.foreign_room,
                           data.foreign_bought_value, data.foreign_sold_value)
                analysis = technical.analyze(candles, format_price, foreign=foreign if any(foreign) else None,
                                             session_open=data.state != 'closed')
                self._draw_chart(candles, analysis, format_price, price_color)
                bottom = max(bottom, self._draw_analysis(analysis))
            except Exception as exc:  # odd data must not stop the redraws
                canvas.delete('all')
                self._draw_header(data, name, format_price, candles)
                message = f"Không phân tích được lịch sử giá: {exc}"
        elif candles is None:
            message = 'Đang tải lịch sử giá…'
        else:
            message = f"Không tải được lịch sử giá: {candles}"
        if message:
            canvas.create_text((CHART_LEFT + CHART_RIGHT) // 2, (PRICE_TOP + RSI_BOTTOM) // 2,
                               text=message, fill=MUTED, font=FONT, width=CHART_RIGHT - CHART_LEFT)

        height = bottom + 30
        canvas.create_text(PAD, height - PAD, anchor='w', fill=MUTED, font=SMALL,
                           text="Chỉ báo kỹ thuật tính từ giá theo ngày, chỉ để tham khảo, "
                                "không phải khuyến nghị đầu tư.")
        if height != self._height:
            self._height = height
            canvas.configure(height=height)

    def _band_color(self, data: StockData, price: float) -> str:
        """Returns: the price board's color for a price, given the day's reference and limits."""
        if data.ceiling > 0 and price >= data.ceiling:
            return BAND_COLORS['ceiling']
        if data.floor > 0 and price <= data.floor:
            return BAND_COLORS['floor']
        if price == data.prev_close:
            return BAND_COLORS['reference']
        return UP if price > data.prev_close else DOWN

    def _draw_header(self, data: StockData, name: str, format_price: Callable[[float], str], candles) -> str:
        """
        Draws the stock's name, price, and the day's reference and limits across the top.

        Returns:
            str: The color the price is shown in, for the chart's price marker.
        """
        canvas = self._canvas
        canvas.create_text(PAD, 12, anchor='nw', text=name, fill=TEXT, font=TITLE)
        months = ''
        if isinstance(candles, Candles):
            months = f", {round(min(len(candles), VISIBLE_CANDLES) / 21)} tháng gần nhất"
        state = 'mất kết nối, đang thử lại' if data.has_error else STATE_NAMES.get(data.state, data.state)
        canvas.create_text(PAD, 40, anchor='nw', fill=MUTED, font=FONT,
                           text=f"{data.exchange or 'Đang tải'} · nến ngày{months} · {state}")

        color = self._band_color(data, data.price) if data.prev_close > 0 else TEXT
        canvas.create_text(WIDTH - PAD, 12, anchor='ne', fill=color, font=TITLE,
                           text=f"{format_price(data.price)}   {data.change_pct:+.2f}%")
        if data.ceiling > 0 and data.floor > 0:
            right = WIDTH - PAD
            for text, text_color in reversed(((f"Trần {format_price(data.ceiling)}", BAND_COLORS['ceiling']),
                                              (f"Tham chiếu {format_price(data.prev_close)}", BAND_COLORS['reference']),
                                              (f"Sàn {format_price(data.floor)}", BAND_COLORS['floor']))):
                item = canvas.create_text(right, 40, anchor='ne', fill=text_color, font=FONT, text=text)
                right = canvas.bbox(item)[0] - 18
        return color

    def _draw_chips(self) -> None:
        """
        Draws a chip per overlay above the chart, doubling as its legend:
        colored while the overlay is shown, dimmed while hidden. Clicking
        one toggles its overlay (see prepare).
        """
        canvas = self._canvas
        x = CHART_LEFT
        for overlay, legend in CHIPS:
            shown = overlay in self._overlays
            tag = f'chip-{overlay}'
            right = x + 8
            for label, color in legend:
                item = canvas.create_text(right, CHIPS_TOP + 9, anchor='w', font=SMALL, text=label,
                                          fill=color if shown else DIM, tags=(tag,))
                right = canvas.bbox(item)[2] + 6
            box = canvas.create_rectangle(x, CHIPS_TOP, right + 2, CHIPS_TOP + 18, tags=(tag,),
                                          fill=CHIP_FILL if shown else BACKGROUND, outline=MUTED if shown else GRID)
            canvas.tag_lower(box)
            x = right + 10
        canvas.create_text(x, CHIPS_TOP + 9, anchor='w', fill=DIM, font=SMALL, text='bấm để bật/tắt')

    def _draw_book(self, data: StockData, format_price: Callable[[float], str]) -> int:
        """
        Draws the order book's three best bids and asks, how they weigh
        against each other, foreign investors' trading and room, the latest
        active buy and sell, and the latest matches, like the price board
        shows them.

        Returns:
            int: The y coordinate below what was drawn.
        """
        canvas = self._canvas
        left, right = BOOK_LEFT, BOOK_RIGHT
        middle = (left + right) // 2

        def volume_text(volume: float) -> str:
            return f"{volume:,.0f}"

        canvas.create_text(left, CHIPS_TOP, anchor='nw', fill=TEXT, font=BOLD, text='Sổ lệnh')
        canvas.create_text(right, CHIPS_TOP + 2, anchor='ne', fill=DIM, font=SMALL, text='KL: cổ phiếu')
        y = CHIPS_TOP + 24
        canvas.create_text(left, y, anchor='nw', fill=MUTED, font=SMALL, text='Giá mua')
        canvas.create_text(middle - 8, y, anchor='ne', fill=MUTED, font=SMALL, text='KL mua')
        canvas.create_text(middle + 8, y, anchor='nw', fill=MUTED, font=SMALL, text='Giá bán')
        canvas.create_text(right, y, anchor='ne', fill=MUTED, font=SMALL, text='KL bán')
        for row in range(3):
            y += 17
            if row < len(data.bids):
                price, volume = data.bids[row]
                canvas.create_text(left, y, anchor='nw', fill=self._band_color(data, price), font=FONT,
                                   text=format_price(price))
                canvas.create_text(middle - 8, y, anchor='ne', fill=TEXT, font=FONT, text=volume_text(volume))
            if row < len(data.asks):
                price, volume = data.asks[row]
                canvas.create_text(middle + 8, y, anchor='nw', fill=self._band_color(data, price), font=FONT,
                                   text=format_price(price))
                canvas.create_text(right, y, anchor='ne', fill=TEXT, font=FONT, text=volume_text(volume))

        # How the bids weigh against the asks, as a split bar
        bid_total, ask_total = sum(volume for _, volume in data.bids), sum(volume for _, volume in data.asks)
        y += 24
        if bid_total + ask_total > 0:
            split = left + (right - left) * bid_total / (bid_total + ask_total)
            canvas.create_rectangle(left, y, split, y + 6, fill=UP, outline=UP)
            canvas.create_rectangle(split, y, right, y + 6, fill=DOWN, outline=DOWN)
            canvas.create_text(left, y + 10, anchor='nw', fill=UP, font=SMALL, text=f"Dư mua {volume_text(bid_total)}")
            canvas.create_text(right, y + 10, anchor='ne', fill=DOWN, font=SMALL,
                               text=f"Dư bán {volume_text(ask_total)}")
            if bid_total and ask_total:
                more, fewer = ('mua', 'bán') if bid_total >= ask_total else ('bán', 'mua')
                canvas.create_text(left, y + 25, anchor='nw', fill=MUTED, font=SMALL,
                                   text=f"Dư {more} gấp {max(bid_total, ask_total) / min(bid_total, ask_total):.1f} "
                                        f"lần dư {fewer}")
        y += 50

        # Foreign investors' trading today and the room they have left, like the board's foreign columns
        canvas.create_text(left, y, anchor='nw', fill=TEXT, font=BOLD, text='Khối ngoại')
        y += 22
        net, net_value = data.foreign_bought - data.foreign_sold, data.foreign_bought_value - data.foreign_sold_value
        for label, color, shares, value in (
                ('Mua', UP, data.foreign_bought, data.foreign_bought_value),
                ('Bán', DOWN, data.foreign_sold, data.foreign_sold_value),
                ('Mua ròng' if net >= 0 else 'Bán ròng', UP if net > 0 else DOWN if net < 0 else MUTED,
                 abs(net), abs(net_value))):
            canvas.create_text(left, y, anchor='nw', fill=color, font=SMALL, text=label)
            canvas.create_text(right, y, anchor='ne', fill=TEXT, font=SMALL,
                               text=f"{volume_text(shares)} cp" + (f" · {technical.compact(value)} đ" if value else ''))
            y += 16
        canvas.create_text(left, y, anchor='nw', fill=MUTED, font=SMALL, text='Room còn lại')
        canvas.create_text(right, y, anchor='ne', fill=TEXT, font=SMALL,
                           text=f"{volume_text(data.foreign_room)} cp" if data.foreign_room > 0
                           else 'Hết room' if data.prev_close > 0 else '—')
        y += 26

        canvas.create_text(left, y, anchor='nw', fill=TEXT, font=BOLD, text='Khớp lệnh')
        if data.trades:
            canvas.create_text(right, y + 2, anchor='ne', fill=DIM, font=SMALL, text=f"{len(data.trades)} lệnh gần nhất")
        y += 22
        # The latest active buy (a buyer taking an ask) and sell (a seller hitting a bid)
        for side, label, color in (('B', 'Mua gần nhất', UP), ('S', 'Bán gần nhất', DOWN)):
            trade = next((trade for trade in data.trades if trade.side == side), None)
            text = '—' if trade is None else f"{volume_text(trade.volume)} cp · {format_price(trade.price)} · {trade.time}"
            canvas.create_text(left, y, anchor='nw', fill=color, font=SMALL, text=label)
            canvas.create_text(right, y, anchor='ne', fill=TEXT, font=SMALL, text=text)
            y += 16
        # What the listed matches add up to on each side
        if data.trades:
            for side, label in (('B', 'Tổng mua chủ động'), ('S', 'Tổng bán chủ động')):
                total = sum(trade.volume for trade in data.trades if trade.side == side)
                canvas.create_text(left, y, anchor='nw', fill=MUTED, font=SMALL, text=label)
                canvas.create_text(right, y, anchor='ne', fill=MUTED, font=SMALL, text=f"{volume_text(total)} cp")
                y += 16
        y += 6

        columns = ((left, 'nw', 'Giờ'), (middle - 4, 'ne', 'Giá'), (right - 30, 'ne', 'KL'), (right, 'ne', 'M/B'))
        for x, anchor, title in columns:
            canvas.create_text(x, y, anchor=anchor, fill=MUTED, font=SMALL, text=title)
        if not data.trades:
            canvas.create_text(left, y + 18, anchor='nw', fill=DIM, font=SMALL,
                               text='Chưa có lệnh khớp nào từ khi mở bảng giá' if data.state != 'closed'
                               else 'Ngoài giờ giao dịch')
        for trade in data.trades[:TRADE_ROWS]:
            y += 16
            side, side_color = {'B': ('M', UP), 'S': ('B', DOWN)}.get(trade.side, ('', MUTED))
            canvas.create_text(left, y, anchor='nw', fill=MUTED, font=SMALL, text=trade.time)
            canvas.create_text(middle - 4, y, anchor='ne', fill=self._band_color(data, trade.price), font=SMALL,
                               text=format_price(trade.price))
            canvas.create_text(right - 30, y, anchor='ne', fill=TEXT, font=SMALL, text=volume_text(trade.volume))
            canvas.create_text(right, y, anchor='ne', fill=side_color, font=BOLD, text=side)
        return y + 24

    def _draw_chart(self, candles: Candles, analysis: technical.Analysis,
                    format_price: Callable[[float], str], price_color: str) -> None:
        canvas = self._canvas
        overlays = self._overlays
        count = min(len(candles), VISIBLE_CANDLES)
        first, last = len(candles) - count, len(candles) - 1
        indexes = range(first, last + 1)
        # The Ichimoku cloud runs past the last candle, into room kept free on the right
        ahead = technical.ICHIMOKU_SHIFT if 'ichimoku' in overlays else 0
        cloud_indexes = range(first, last + ahead + 1)
        slot = (CHART_RIGHT - CHART_LEFT) / (count + ahead)
        body = max(1, int(slot * 0.65))

        def x_of(i: int) -> float:
            return CHART_LEFT + (i - first + 0.5) * slot

        # The lines drawn over the candles, as (series, color, dash)
        lines = []
        if 'ma' in overlays:
            lines += [(series, color, None) for (_, color), series in
                      zip(AVERAGES, (analysis.sma20, analysis.sma50, analysis.sma200))]
        if 'bollinger' in overlays:
            lines += [(analysis.band_upper, BANDS, (3, 3)), (analysis.band_lower, BANDS, (3, 3))]
        if 'ichimoku' in overlays:
            lines += [(analysis.chikou, CHIKOU, None), (analysis.tenkan, TENKAN, None),
                      (analysis.kijun, KIJUN, None)]
        fibonacci = analysis.fibonacci if 'fibonacci' in overlays else None

        # The price scale covers the candles and everything drawn over them
        visible = [candles.highs[i] for i in indexes] + [candles.lows[i] for i in indexes]
        for series, _, _ in lines:
            visible += [series[i] for i in indexes if series[i] is not None]
        if ahead:
            visible += [series[i] for series in (analysis.span_a, analysis.span_b)
                        for i in cloud_indexes if series[i] is not None]
        if fibonacci is not None:
            visible += [level for _, level in fibonacci.levels]
        low, high = min(visible), max(visible)
        margin = (high - low) * 0.05 or abs(high) * 0.01 or 1
        low, high = low - margin, high + margin

        def y_of(price: float) -> float:
            return PRICE_BOTTOM - (price - low) / (high - low) * (PRICE_BOTTOM - PRICE_TOP)

        for step in range(5):
            price = low + (high - low) * step / 4
            y = y_of(price)
            canvas.create_line(CHART_LEFT, y, CHART_RIGHT, y, fill=GRID)
            canvas.create_text(CHART_RIGHT + 6, y, anchor='w', fill=MUTED, font=SMALL, text=format_price(price))

        if ahead:
            self._draw_cloud(analysis.span_a, analysis.span_b, cloud_indexes, x_of, y_of)
        if fibonacci is not None:
            left = x_of(max(first, fibonacci.start))
            for ratio, level in fibonacci.levels:
                y = y_of(level)
                canvas.create_line(left, y, CHART_RIGHT, y, fill=FIBONACCI_LINE, dash=(1, 3))
                canvas.create_text(left + 2, y - 1, anchor='sw', fill=FIBONACCI_LINE, font=SMALL,
                                   text=f"{ratio * 100:g}%  {format_price(level)}")
        for i in indexes:
            rising = candles.closes[i] >= candles.opens[i]
            color = UP if rising else DOWN
            x = x_of(i)
            canvas.create_line(x, y_of(candles.highs[i]), x, y_of(candles.lows[i]), fill=color)
            top, bottom = sorted((y_of(candles.opens[i]), y_of(candles.closes[i])))
            canvas.create_rectangle(x - body / 2, top, x + body / 2, max(bottom, top + 1), fill=color, outline=color)
        for series, color, dash in lines:
            self._draw_series(series, indexes, x_of, y_of, color, dash=dash)

        # The latest price, marked on the axis in the header's color
        price = candles.closes[-1]
        y = y_of(price)
        canvas.create_line(CHART_LEFT, y, CHART_RIGHT, y, fill=price_color, dash=(2, 2))
        label = canvas.create_text(CHART_RIGHT + 6, y, anchor='w', fill='black', font=SMALL, text=format_price(price))
        left, top, right, bottom = canvas.bbox(label)
        canvas.tag_lower(canvas.create_rectangle(left - 3, top - 1, right + 3, bottom + 1,
                                                 fill=price_color, outline=price_color), label)

        # Volume, scaled to the busiest day shown
        most = max(candles.volumes[i] for i in indexes) or 1
        for i in indexes:
            x = x_of(i)
            height = candles.volumes[i] / most * (VOLUME_BOTTOM - VOLUME_TOP)
            color = UP_VOLUME if candles.closes[i] >= candles.opens[i] else DOWN_VOLUME
            canvas.create_rectangle(x - body / 2, VOLUME_BOTTOM - max(height, 1), x + body / 2, VOLUME_BOTTOM,
                                    fill=color, outline=color)
        canvas.create_text(CHART_RIGHT + 6, VOLUME_TOP, anchor='nw', fill=MUTED, font=SMALL, text='Khối lượng')

        # The oscillators from 0 to 100, each with its overbought and oversold lines
        self._draw_oscillator('RSI 14', ((analysis.rsi, RSI_LINE),), (30, 70), RSI_TOP, RSI_BOTTOM, indexes, x_of)
        self._draw_oscillator('Stochastic 14 3 3', ((analysis.stochastic_k, K_LINE), (analysis.stochastic_d, D_LINE)),
                              (20, 80), STOCHASTIC_TOP, STOCHASTIC_BOTTOM, indexes, x_of)
        self._draw_oscillator('Stoch RSI 3 3 14 14',
                              ((analysis.stochastic_rsi_k, K_LINE), (analysis.stochastic_rsi_d, D_LINE)),
                              (20, 80), STOCHASTIC_RSI_TOP, STOCHASTIC_RSI_BOTTOM, indexes, x_of)
        self._draw_macd(analysis, indexes, x_of, body, format_price)

        # A label where each month starts: T6 for June (tháng 6), and so on
        previous = None
        for i in indexes:
            month = time.gmtime(candles.times[i]).tm_mon
            if previous is not None and month != previous:
                canvas.create_text(x_of(i), MONTHS_Y, fill=MUTED, font=SMALL, text=f"T{month}")
            previous = month

    def _draw_oscillator(self, title: str, lines: Sequence[Tuple[technical.Series, str]], levels: Tuple[int, int],
                         top: int, bottom: int, indexes: range, x_of: Callable[[int], float]) -> None:
        """
        Draws a panel for an oscillator ranging from 0 to 100: its lines (such
        as %K and %D), its oversold and overbought levels, and its title with
        the lines' latest values in their colors.
        """
        canvas = self._canvas

        def y_of(value: float) -> float:
            return bottom - value / 100 * (bottom - top)

        for level in levels:
            canvas.create_line(CHART_LEFT, y_of(level), CHART_RIGHT, y_of(level), fill=GRID, dash=(2, 2))
            canvas.create_text(CHART_RIGHT + 6, y_of(level), anchor='w', fill=MUTED, font=SMALL, text=str(level))
        for series, color in lines:
            self._draw_series(series, indexes, x_of, y_of, color)
        runs = [(title, MUTED)] + [(f"{series[-1]:.0f}", color) for series, color in lines if series[-1] is not None]
        self._draw_runs(CHART_LEFT + 4, top - 2, runs)

    def _draw_cloud(self, span_a: technical.Series, span_b: technical.Series, indexes: range,
                    x_of: Callable[[int], float], y_of: Callable[[float], float]) -> None:
        """Fills the Ichimoku cloud between its leading spans: green where span A is above B, red where below."""
        previous = None
        for i in indexes:
            a, b = span_a[i], span_b[i]
            if a is None or b is None:
                previous = None
                continue
            x = x_of(i)
            if previous is not None:
                x0, a0, b0 = previous
                if (a0 >= b0) == (a >= b):
                    self._cloud_piece(((x0, a0), (x, a), (x, b), (x0, b0)), a >= b, y_of)
                else:
                    # The spans cross between the two candles: split there, each side its own color
                    share = (a0 - b0) / ((a0 - b0) - (a - b))
                    cross = (x0 + share * (x - x0), a0 + share * (a - a0))
                    self._cloud_piece(((x0, a0), cross, (x0, b0)), a0 >= b0, y_of)
                    self._cloud_piece((cross, (x, a), (x, b)), a >= b, y_of)
            previous = (x, a, b)
        self._draw_series(span_a, indexes, x_of, y_of, SPAN_A_LINE)
        self._draw_series(span_b, indexes, x_of, y_of, SPAN_B_LINE)

    def _cloud_piece(self, points, green: bool, y_of: Callable[[float], float]) -> None:
        """Fills one piece of the cloud, given as (x, price) corners."""
        color = CLOUD_UP if green else CLOUD_DOWN
        self._canvas.create_polygon(*[coordinate for x, price in points for coordinate in (x, y_of(price))],
                                    fill=color, outline=color)

    def _draw_macd(self, analysis: technical.Analysis, indexes: range, x_of: Callable[[int], float],
                   body: float, format_price: Callable[[float], str]) -> None:
        """Draws the MACD panel: the histogram around zero, the MACD line, and its signal line."""
        canvas = self._canvas
        histogram = analysis.macd_histogram
        values = [series[i] for series in (analysis.macd, analysis.macd_signal, histogram)
                  for i in indexes if series[i] is not None]
        if not values:
            return
        top, bottom = max(values + [0.0]), min(values + [0.0])
        span = (top - bottom) or 1

        def macd_y(value: float) -> float:
            return MACD_BOTTOM - (value - bottom) / span * (MACD_BOTTOM - MACD_TOP)

        zero = macd_y(0)
        canvas.create_line(CHART_LEFT, zero, CHART_RIGHT, zero, fill=GRID)
        canvas.create_text(CHART_RIGHT + 6, zero, anchor='w', fill=MUTED, font=SMALL, text='0')
        for i in indexes:
            value = histogram[i]
            if value is None:
                continue
            previous = histogram[i - 1] if i > 0 else None
            # Bright while the bars grow away from zero, dim while they shrink back
            growing = previous is None or abs(value) >= abs(previous)
            color = (UP if growing else UP_VOLUME) if value >= 0 else (DOWN if growing else DOWN_VOLUME)
            x, y = x_of(i), macd_y(value)
            canvas.create_rectangle(x - body / 2, min(zero, y), x + body / 2, max(zero, y), fill=color, outline=color)
        self._draw_series(analysis.macd, indexes, x_of, macd_y, MACD_LINE)
        self._draw_series(analysis.macd_signal, indexes, x_of, macd_y, MACD_SIGNAL)
        runs = [('MACD 12 26 9', MUTED)]
        if analysis.macd[-1] is not None and analysis.macd_signal[-1] is not None:
            runs += [(format_price(analysis.macd[-1]), MACD_LINE), (format_price(analysis.macd_signal[-1]), MACD_SIGNAL)]
        self._draw_runs(CHART_LEFT + 4, MACD_TOP - 2, runs)

    def _draw_runs(self, x: float, y: float, runs: Sequence[Tuple[str, str]]) -> None:
        """Draws (text, color) runs side by side, left to right, over a backdrop hiding the lines beneath."""
        canvas = self._canvas
        items = []
        for text, color in runs:
            items.append(canvas.create_text(x, y, anchor='nw', fill=color, font=SMALL, text=text))
            x = canvas.bbox(items[-1])[2] + 8
        left, top, right, bottom = canvas.bbox(*items)
        canvas.tag_lower(canvas.create_rectangle(left - 2, top, right + 2, bottom, fill=BACKGROUND, outline=BACKGROUND),
                         items[0])

    def _draw_series(self, series, indexes, x_of, y_of, color: str, dash=None) -> None:
        """Draws an indicator line through its values that exist."""
        points = []
        for i in indexes:
            if series[i] is not None:
                points += [x_of(i), y_of(series[i])]
        if len(points) >= 4:
            self._canvas.create_line(*points, fill=color, dash=dash)

    def _draw_analysis(self, analysis: technical.Analysis) -> int:
        """
        Draws the rating, then the chosen tab: the findings, or the table of
        every indicator behind the rating.

        Returns:
            int: The y coordinate below what was drawn.
        """
        canvas = self._canvas
        x, y = NOTES_LEFT, CHIPS_TOP
        color = RATING_COLORS[analysis.rating]
        canvas.create_rectangle(x, y, x + 132, y + 32, fill=color, outline=color)
        canvas.create_text(x + 66, y + 16, fill='white', font=BADGE, text=analysis.rating.upper())
        canvas.create_text(x + 146, y - 1, anchor='nw', fill=MUTED, font=SMALL, text='Đánh giá kỹ thuật (khung ngày)')
        for row, (group, (buy, neutral, sell)) in enumerate((('Đường trung bình', analysis.average_signals),
                                                              ('Chỉ báo dao động', analysis.oscillator_signals))):
            canvas.create_text(x + 146, y + 12 + row * 12, anchor='nw', fill=TEXT, font=SMALL,
                               text=f"{group}: {buy} mua, {neutral} trung tính, {sell} bán")

        # The tabs, the chosen one underlined
        y += 44
        tab_x = x
        for tab, label in TABS:
            chosen = tab == self._tab
            item = canvas.create_text(tab_x, y, anchor='nw', font=BOLD if chosen else FONT, text=label,
                                      fill=TEXT if chosen else MUTED, tags=(f'tab-{tab}',))
            left, top, right, bottom = canvas.bbox(item)
            # A box behind the label catches clicks between its letters too
            canvas.tag_lower(canvas.create_rectangle(left - 6, top - 3, right + 6, bottom + 3, fill=BACKGROUND,
                                                     outline=BACKGROUND, tags=(f'tab-{tab}',)), item)
            if chosen:
                canvas.create_line(left, bottom + 2, right, bottom + 2, fill=TEXT, width=2)
            tab_x = right + 24
        canvas.create_line(x, y + 21, WIDTH - PAD, y + 21, fill=GRID)
        y += 30
        if self._tab == 'indicators':
            return self._draw_indicator_tables(analysis, x, y)

        text_width = WIDTH - PAD - x - TOPIC_WIDTH
        for topic, text in analysis.notes:
            canvas.create_text(x, y, anchor='nw', fill=TEXT, font=BOLD, text=topic)
            item = canvas.create_text(x + TOPIC_WIDTH, y, anchor='nw', fill=TEXT, font=FONT, text=text,
                                      width=text_width)
            y = canvas.bbox(item)[3] + 8
        return y

    def _draw_indicator_tables(self, analysis: technical.Analysis, x: int, y: int) -> int:
        """
        Draws the oscillators and the moving averages side by side, each row
        with the indicator's latest value and its signal, as TradingView does.

        Returns:
            int: The y coordinate below the longer table.
        """
        canvas = self._canvas
        middle = x + (WIDTH - PAD - x) * 9 // 16
        bottoms = []
        for title, rows, left, right in (('Chỉ báo dao động', analysis.oscillator_table, x, middle - 16),
                                         ('Đường trung bình', analysis.average_table, middle + 8, WIDTH - PAD)):
            canvas.create_text(left, y, anchor='nw', fill=TEXT, font=BOLD, text=title)
            row_y = y + 20
            for heading, anchor, column_x in (('Tên', 'nw', left), ('Giá trị', 'ne', right - 64),
                                              ('Tín hiệu', 'ne', right)):
                canvas.create_text(column_x, row_y, anchor=anchor, fill=MUTED, font=SMALL, text=heading)
            for name, value, signal in rows:
                row_y += 16
                label, color = SIGNAL_NAMES[signal]
                canvas.create_text(left, row_y, anchor='nw', fill=TEXT, font=SMALL, text=name)
                canvas.create_text(right - 64, row_y, anchor='ne', fill=TEXT, font=SMALL, text=value)
                canvas.create_text(right, row_y, anchor='ne', fill=color, font=SMALL, text=label)
            bottoms.append(row_y + 20)
        return max(bottoms)
