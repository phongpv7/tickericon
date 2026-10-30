import ctypes
import queue
import sys
import threading
import time
from typing import Callable, Dict, Optional, Protocol, Set, Tuple, Union

try:
    import tkinter as tk
except ModuleNotFoundError:  # Python built without Tkinter (headless/non-Windows)
    tk = None

from src import technical
from src.market_api import StockData
from src.price_history import Candles, with_live_price

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

# Hover this long before the popup opens, and leave it this long before it closes
SHOW_DELAY_MS = 350
HIDE_DELAY_MS = 400

# How often the open popup redraws with the latest price, and checks on loading history
REDRAW_MS = 1000
LOADING_REDRAW_MS = 150

# How long fetched candles are reused before refetching, and failures before retrying
HISTORY_TTL_S = 600
HISTORY_RETRY_S = 30

# Daily candles on the chart: about six months
VISIBLE_CANDLES = 120

# Where the popup waits while hidden: moving it, unlike showing it, never takes the focus
_OFFSCREEN = -10000

WIDTH = 1040
PAD = 16
# The chart's panels, top to bottom: prices, volume, RSI, month labels
CHART_LEFT, CHART_RIGHT = PAD, 520
AXIS_WIDTH = 56
PRICE_TOP, PRICE_BOTTOM = 70, 290
VOLUME_TOP, VOLUME_BOTTOM = 298, 334
RSI_TOP, RSI_BOTTOM = 344, 404
MONTHS_Y = 418
# The analysis column right of the chart: topics, then their findings
NOTES_LEFT = CHART_RIGHT + AXIS_WIDTH + 24
TOPIC_WIDTH = 84

BACKGROUND = '#1c1c1c'
GRID = '#333333'
TEXT = '#e6e6e6'
MUTED = '#8f8f8f'
UP, DOWN = '#26a65b', '#e5484d'
UP_VOLUME, DOWN_VOLUME = '#1b5e3a', '#7d2a2d'
AVERAGES = (('MA20', '#f5a623'), ('MA50', '#4aa3ff'), ('MA200', '#c084fc'))
BANDS = '#5c6370'
RSI_LINE = '#b39ddb'
RATING_COLORS = {
    'Strong buy': '#1f9d55', 'Buy': '#26a65b', 'Neutral': '#6b6b6b', 'Sell': '#e5484d', 'Strong sell': '#c53030',
}
# Vietnamese price board colors for prices at the reference and the daily limits
BAND_COLORS = {'ceiling': '#d24dff', 'reference': '#f5c518', 'floor': '#3ba3ff'}

FONT = ('Segoe UI', 9)
BOLD = ('Segoe UI', 9, 'bold')
SMALL = ('Segoe UI', 8)
TITLE = ('Segoe UI', 14, 'bold')
BADGE = ('Segoe UI', 10, 'bold')


class AnalysisSource(Protocol):
    """What the popup needs from the app."""

    def pin_symbol(self) -> Optional[str]:
        """Holds the widget on the symbol it shows now, and returns that symbol."""

    def unpin_symbol(self) -> None:
        """Lets the widget rotate through the symbols again."""

    def symbol_quote(self, symbol: str) -> Tuple[StockData, str]:
        """Returns the symbol's latest data and its display name."""

    def symbol_history(self, symbol: str) -> Candles:
        """Fetches about a year of daily candles; blocks, so it's called on a worker thread."""

    def format_price(self, price: float, currency: str) -> str:
        """Formats a price the way the widget shows it."""


class AnalysisPopup:
    """
    A daily chart and technical analysis of the symbol on the taskbar
    widget, shown above it while the mouse rests on the widget or the popup.
    Lives on the widget's Tk thread; price history loads on worker threads.
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
        self._over_widget = False
        self._over_popup = False
        # The widget's right edge and the taskbar's top and bottom, to place the popup by
        self._anchor: Optional[Tuple[int, int, int]] = None
        self._height = PRICE_BOTTOM
        self._redraw_job = None
        # Fetched candles (or the fetch's error) per symbol, with when they arrived (time.monotonic())
        self._history: Dict[str, Tuple[Union[Candles, Exception], float]] = {}
        self._loading: Set[str] = set()
        # (symbol, candles or error) from the worker threads
        self._results: queue.Queue = queue.Queue()

    def set_anchor(self, right: int, taskbar_top: int, taskbar_bottom: int) -> None:
        """
        Args:
            right (int): The widget's right edge, which the popup lines up with.
            taskbar_top (int): The taskbar's top edge.
            taskbar_bottom (int): The taskbar's bottom edge.
        """
        self._anchor = (right, taskbar_top, taskbar_bottom)

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
        canvas.bind('<Enter>', self._popup_entered)
        canvas.bind('<Leave>', self._popup_left)
        window.update_idletasks()
        if _user32 is not None:
            # Clicking the popup must not take the focus either
            hwnd = wintypes.HWND(int(window.wm_frame(), 16))
            style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style | _WS_EX_NOACTIVATE)
        self._window, self._canvas = window, canvas

    def widget_entered(self) -> None:
        """Opens the popup after SHOW_DELAY_MS, unless the mouse leaves first."""
        self._over_widget = True
        if not self._shown:
            self.root.after(SHOW_DELAY_MS, self._show_if_hovered)

    def widget_left(self) -> None:
        """Closes the popup after HIDE_DELAY_MS, unless the mouse comes back."""
        self._over_widget = False
        self.root.after(HIDE_DELAY_MS, self._hide_if_left)

    def hide(self) -> None:
        """Moves the popup out of sight and lets the widget rotate again."""
        if not self._shown:
            return
        self._shown = False
        self._window.geometry(f"+{_OFFSCREEN}+{_OFFSCREEN}")
        if self._redraw_job is not None:
            self.root.after_cancel(self._redraw_job)
            self._redraw_job = None
        self.source.unpin_symbol()

    def _popup_entered(self, _event) -> None:
        self._over_popup = True

    def _popup_left(self, _event) -> None:
        self._over_popup = False
        self.root.after(HIDE_DELAY_MS, self._hide_if_left)

    def _show_if_hovered(self) -> None:
        if not self._over_widget or self._shown or self._anchor is None:
            return
        symbol = self.source.pin_symbol()
        if symbol is None:
            return
        self.prepare()
        self._symbol = symbol
        self._shown = True
        self._redraw()

    def _hide_if_left(self) -> None:
        if not self._over_widget and not self._over_popup:
            self.hide()

    def _redraw(self) -> None:
        """Redraws the open popup, then schedules the next redraw."""
        self._redraw_job = None
        if not self._shown:
            return
        self._collect_results()
        loading = self._draw()
        self._place()
        self._redraw_job = self.root.after(LOADING_REDRAW_MS if loading else REDRAW_MS, self._redraw)

    def _place(self) -> None:
        """Puts the popup just above the taskbar (or below it, at the top of the screen), right-aligned with the widget."""
        right, taskbar_top, taskbar_bottom = self._anchor
        x = max(0, right - WIDTH)
        y = taskbar_top - self._height - 8 if taskbar_top >= self._height + 8 else taskbar_bottom + 8
        self._window.geometry(f"{WIDTH}x{self._height}+{x}+{y}")

    def _candles(self, symbol: str) -> Union[Candles, Exception, None]:
        """
        Returns:
            Union[Candles, Exception, None]: The symbol's cached candles, or the
                error fetching them, or None while they load. Starts a fetch on
                a worker thread when there's nothing fresh in the cache.
        """
        result, fetched_at = self._history.get(symbol, (None, float('-inf')))
        max_age = HISTORY_RETRY_S if isinstance(result, Exception) else HISTORY_TTL_S
        if time.monotonic() - fetched_at >= max_age and symbol not in self._loading:
            self._loading.add(symbol)
            threading.Thread(target=self._fetch, args=(symbol,), daemon=True).start()
        return result

    def _fetch(self, symbol: str) -> None:
        """Fetches a symbol's candles; runs on a worker thread."""
        try:
            result = self.source.symbol_history(symbol)
        except Exception as exc:
            result = exc
        self._results.put((symbol, result))

    def _collect_results(self) -> None:
        """Caches the candles the worker threads have fetched since the last redraw."""
        while True:
            try:
                symbol, result = self._results.get_nowait()
            except queue.Empty:
                return
            self._loading.discard(symbol)
            previous = self._history.get(symbol, (None, 0))[0]
            if isinstance(result, Exception) and isinstance(previous, Candles):
                result = previous  # a failed refresh keeps showing the older candles
            self._history[symbol] = (result, time.monotonic())

    def _draw(self) -> bool:
        """
        Draws the popup for the current symbol.

        Returns:
            bool: True while its candles are still loading.
        """
        canvas = self._canvas
        canvas.delete('all')
        data, name = self.source.symbol_quote(self._symbol)

        def format_price(price: float) -> str:
            return self.source.format_price(price, data.currency)

        candles = self._candles(self._symbol)
        price_color = self._draw_header(data, name, format_price, candles)
        bottom = MONTHS_Y + 8
        message = None
        if isinstance(candles, Candles) and len(candles) >= 2:
            try:
                candles = with_live_price(candles, self._symbol, data.price)
                analysis = technical.analyze(candles, format_price)
                self._draw_chart(candles, analysis, format_price, price_color)
                bottom = max(bottom, self._draw_notes(analysis))
            except Exception as exc:  # odd data must not stop the redraws
                canvas.delete('all')
                self._draw_header(data, name, format_price, candles)
                message = f"Couldn't analyze the price history: {exc}"
        elif candles is None:
            message = 'Loading the price history…'
        else:
            message = f"Couldn't load the price history: {candles}"
        if message:
            canvas.create_text((CHART_LEFT + CHART_RIGHT) // 2, (PRICE_TOP + RSI_BOTTOM) // 2,
                               text=message, fill=MUTED, font=FONT, width=CHART_RIGHT - CHART_LEFT)

        height = bottom + 30
        canvas.create_text(PAD, height - PAD, anchor='w', fill=MUTED, font=SMALL,
                           text="Technical indicators from daily prices, for reference only; not investment advice.")
        if height != self._height:
            self._height = height
            canvas.configure(height=height)
        return candles is None

    def _draw_header(self, data: StockData, name: str, format_price: Callable[[float], str], candles) -> str:
        """
        Draws the symbol's name, price, and day range across the top.

        Returns:
            str: The color the price is shown in, for the chart's price marker.
        """
        canvas = self._canvas
        canvas.create_text(PAD, 12, anchor='nw', text=name, fill=TEXT, font=TITLE)
        months = ''
        if isinstance(candles, Candles):
            months = f", last {round(min(len(candles), VISIBLE_CANDLES) / 21)} months" if data.currency == 'VND' \
                else f", last {round(min(len(candles), VISIBLE_CANDLES) / 30)} months"
        canvas.create_text(PAD, 40, anchor='nw', fill=MUTED, font=FONT,
                           text=f"{data.exchange or 'Loading'} · daily candles{months}")

        color = BAND_COLORS.get(data.price_band) or (UP if data.change_pct > 0 else DOWN if data.change_pct < 0
                                                     else BAND_COLORS['reference'])
        canvas.create_text(WIDTH - PAD, 12, anchor='ne', fill=color, font=TITLE,
                           text=f"{format_price(data.price)}   {data.change_pct:+.2f}%")
        if data.ceiling > 0 and data.floor > 0:
            limits = (f"Floor {format_price(data.floor)}    Reference {format_price(data.prev_close)}    "
                      f"Ceiling {format_price(data.ceiling)}")
        else:
            limits = (f"Today's high {format_price(data.day_high)}    low {format_price(data.day_low)}    "
                      f"open {format_price(data.prev_close)}")
        canvas.create_text(WIDTH - PAD, 40, anchor='ne', fill=MUTED, font=FONT, text=limits)
        return color

    def _draw_chart(self, candles: Candles, analysis: technical.Analysis,
                    format_price: Callable[[float], str], price_color: str) -> None:
        canvas = self._canvas
        count = min(len(candles), VISIBLE_CANDLES)
        first = len(candles) - count
        indexes = range(first, len(candles))
        slot = (CHART_RIGHT - CHART_LEFT) / count
        body = max(1, int(slot * 0.65))

        def x_of(i: int) -> float:
            return CHART_LEFT + (i - first + 0.5) * slot

        # The price scale covers the candles and every line drawn over them
        lines = [(color, series) for (_, color), series in
                 zip(AVERAGES, (analysis.sma20, analysis.sma50, analysis.sma200))]
        visible = [candles.highs[i] for i in indexes] + [candles.lows[i] for i in indexes]
        for series in [s for _, s in lines] + [analysis.band_upper, analysis.band_lower]:
            visible += [series[i] for i in indexes if series[i] is not None]
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

        for series in (analysis.band_upper, analysis.band_lower):
            self._draw_series(series, indexes, x_of, y_of, BANDS, dash=(3, 3))
        for i in indexes:
            rising = candles.closes[i] >= candles.opens[i]
            color = UP if rising else DOWN
            x = x_of(i)
            canvas.create_line(x, y_of(candles.highs[i]), x, y_of(candles.lows[i]), fill=color)
            top, bottom = sorted((y_of(candles.opens[i]), y_of(candles.closes[i])))
            canvas.create_rectangle(x - body / 2, top, x + body / 2, max(bottom, top + 1), fill=color, outline=color)
        for color, series in lines:
            self._draw_series(series, indexes, x_of, y_of, color)

        # The latest price, marked on the axis in the header's color
        price = candles.closes[-1]
        y = y_of(price)
        canvas.create_line(CHART_LEFT, y, CHART_RIGHT, y, fill=price_color, dash=(2, 2))
        label = canvas.create_text(CHART_RIGHT + 6, y, anchor='w', fill='black', font=SMALL, text=format_price(price))
        left, top, right, bottom = canvas.bbox(label)
        canvas.tag_lower(canvas.create_rectangle(left - 3, top - 1, right + 3, bottom + 1,
                                                 fill=price_color, outline=price_color), label)

        legend_x = CHART_LEFT + 4
        for name, color in AVERAGES + (('Bollinger', BANDS),):
            item = canvas.create_text(legend_x, PRICE_TOP + 2, anchor='nw', fill=color, font=SMALL, text=name)
            legend_x = canvas.bbox(item)[2] + 10

        # Volume, scaled to the busiest day shown
        most = max(candles.volumes[i] for i in indexes) or 1
        for i in indexes:
            x = x_of(i)
            height = candles.volumes[i] / most * (VOLUME_BOTTOM - VOLUME_TOP)
            color = UP_VOLUME if candles.closes[i] >= candles.opens[i] else DOWN_VOLUME
            canvas.create_rectangle(x - body / 2, VOLUME_BOTTOM - max(height, 1), x + body / 2, VOLUME_BOTTOM,
                                    fill=color, outline=color)
        canvas.create_text(CHART_RIGHT + 6, VOLUME_TOP, anchor='nw', fill=MUTED, font=SMALL, text='Volume')

        # RSI, with its overbought (70) and oversold (30) lines
        def rsi_y(value: float) -> float:
            return RSI_BOTTOM - value / 100 * (RSI_BOTTOM - RSI_TOP)

        for level in (30, 70):
            canvas.create_line(CHART_LEFT, rsi_y(level), CHART_RIGHT, rsi_y(level), fill=GRID, dash=(2, 2))
            canvas.create_text(CHART_RIGHT + 6, rsi_y(level), anchor='w', fill=MUTED, font=SMALL, text=str(level))
        self._draw_series(analysis.rsi, indexes, x_of, rsi_y, RSI_LINE)
        if analysis.rsi[-1] is not None:
            canvas.create_text(CHART_LEFT + 4, RSI_TOP - 2, anchor='nw', fill=RSI_LINE, font=SMALL,
                               text=f"RSI 14   {analysis.rsi[-1]:.0f}")

        # A label where each month starts
        previous = None
        for i in indexes:
            month = time.strftime('%b', time.gmtime(candles.times[i]))
            if previous is not None and month != previous:
                canvas.create_text(x_of(i), MONTHS_Y, fill=MUTED, font=SMALL, text=month)
            previous = month

    def _draw_series(self, series, indexes, x_of, y_of, color: str, dash=None) -> None:
        """Draws an indicator line through its values that exist."""
        points = []
        for i in indexes:
            if series[i] is not None:
                points += [x_of(i), y_of(series[i])]
        if len(points) >= 4:
            self._canvas.create_line(*points, fill=color, dash=dash)

    def _draw_notes(self, analysis: technical.Analysis) -> int:
        """
        Draws the rating and findings right of the chart.

        Returns:
            int: The y coordinate below the last finding.
        """
        canvas = self._canvas
        x, y = NOTES_LEFT, PRICE_TOP
        color = RATING_COLORS[analysis.rating]
        canvas.create_rectangle(x, y, x + 132, y + 32, fill=color, outline=color)
        canvas.create_text(x + 66, y + 16, fill='white', font=BADGE, text=analysis.rating.upper())
        canvas.create_text(x + 146, y - 1, anchor='nw', fill=MUTED, font=SMALL, text='Technical rating (daily)')
        for row, (group, (buy, neutral, sell)) in enumerate((('Moving averages', analysis.average_signals),
                                                              ('Oscillators', analysis.oscillator_signals))):
            canvas.create_text(x + 146, y + 12 + row * 12, anchor='nw', fill=TEXT, font=SMALL,
                               text=f"{group}: {buy} buy, {neutral} neutral, {sell} sell")

        y += 48
        text_width = WIDTH - PAD - x - TOPIC_WIDTH
        for topic, text in analysis.notes:
            canvas.create_text(x, y, anchor='nw', fill=TEXT, font=BOLD, text=topic)
            item = canvas.create_text(x + TOPIC_WIDTH, y, anchor='nw', fill=TEXT, font=FONT, text=text,
                                      width=text_width)
            y = canvas.bbox(item)[3] + 8
        return y
