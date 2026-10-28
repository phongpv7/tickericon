import ctypes
import sys
import threading
from typing import Callable, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import font as tkfont
except ModuleNotFoundError:  # Python built without Tkinter (headless/non-Windows)
    tk = None
    tkfont = None

if sys.platform == 'win32':
    from ctypes import wintypes

    # Private DLL handles, so these argtypes never clash with pystray's own
    _user32 = ctypes.WinDLL('user32')
    _gdi32 = ctypes.WinDLL('gdi32')
    _shell32 = ctypes.WinDLL('shell32')

    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowExW.restype = wintypes.HWND
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = wintypes.LONG
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    _user32.SetWindowLongW.restype = wintypes.LONG
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.GetPixel.restype = wintypes.DWORD
    _shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
else:  # the widget is Windows-only
    _user32 = _gdi32 = _shell32 = None

RGB = Tuple[int, int, int]

# One run of colored text; the widget draws a list of these left to right
Segment = Tuple[str, RGB]

FONT_FAMILY = 'Segoe UI'
FONT_SIZE_PT = 14

# How often the widget redraws, follows the taskbar, and re-asserts topmost
REFRESH_MS = 500

# Pixels between the widget and the notification area, and between text runs
TRAY_GAP = 8
SEGMENT_GAP = 6

# SHQueryUserNotificationState results meaning a full-screen app or presentation is up
_FULLSCREEN_STATES = (2, 3, 4)

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_HWND_TOPMOST = -1
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
_CLR_INVALID = 0xFFFFFFFF


def _taskbar_layout() -> Optional[Tuple[int, int, int, int]]:
    """
    Returns:
        Optional[Tuple[int, int, int, int]]: The primary taskbar's top and
            bottom edges and the left edge of its notification area, as
            (top, bottom, tray_left, taskbar_left), or None while the
            taskbar is missing or hidden.
    """
    taskbar = _user32.FindWindowW('Shell_TrayWnd', None)
    if not taskbar or not _user32.IsWindowVisible(taskbar):
        return None
    tray = _user32.FindWindowExW(taskbar, None, 'TrayNotifyWnd', None)
    if not tray:
        return None
    bar, area = wintypes.RECT(), wintypes.RECT()
    _user32.GetWindowRect(taskbar, ctypes.byref(bar))
    _user32.GetWindowRect(tray, ctypes.byref(area))
    return bar.top, bar.bottom, area.left, bar.left


def _fullscreen_app_running() -> bool:
    """
    Returns:
        bool: True while a full-screen app (video, game, presentation) is up
            and the taskbar is out of sight.
    """
    state = ctypes.c_int()
    if _shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return False
    return state.value in _FULLSCREEN_STATES


def _screen_color(x: int, y: int) -> Optional[str]:
    """
    Args:
        x (int): Screen x coordinate.
        y (int): Screen y coordinate.

    Returns:
        Optional[str]: The '#rrggbb' color on screen at (x, y), or None if
            it can't be read (e.g. while the session is locked).
    """
    hdc = _user32.GetDC(None)
    try:
        color = _gdi32.GetPixel(hdc, x, y)
    finally:
        _user32.ReleaseDC(None, hdc)
    if color == _CLR_INVALID:
        return None
    return '#%02x%02x%02x' % (color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF)


class TaskbarWidget:
    """
    Shows colored text on the Windows taskbar, just left of the notification
    area, in a borderless always-on-top window with a transparent background.
    Tray icons are limited to 16x16 pixels; this has room for whole prices
    in a normal font.

    Runs its own Tk event loop on a background thread and pulls the text to
    show from `provider` on every refresh; an empty result hides the widget.
    """

    def __init__(self, provider: Callable[[], List[Segment]]):
        """
        Args:
            provider (Callable[[], List[Segment]]): Returns the text runs to
                show; called on the widget's own thread.
        """
        self.provider = provider
        self._stop = threading.Event()
        self._root = None
        self._canvas = None
        self._font = None
        self._hwnd = None
        self._drawn = None
        self._width = 1
        self._geometry = None
        self._key_color = None
        self._visible = False

    @staticmethod
    def supported() -> bool:
        """
        Returns:
            bool: True on Windows with Tkinter available.
        """
        return _user32 is not None and tk is not None

    def start(self) -> None:
        """Opens the widget on its own daemon thread."""
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        """Closes the widget at its next refresh."""
        self._stop.set()

    def _run(self) -> None:
        """Creates the window and runs its Tk event loop until stopped."""
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        self._root = root
        self._font = tkfont.Font(root, family=FONT_FAMILY, size=FONT_SIZE_PT, weight='bold')
        self._canvas = tk.Canvas(root, highlightthickness=0, borderwidth=0)
        self._canvas.pack(fill='both', expand=True)
        self._tick()
        root.mainloop()

    def _tick(self) -> None:
        """Refreshes the widget, then schedules the next refresh."""
        if self._stop.is_set():
            self._root.destroy()
            return
        try:
            self._refresh()
        except Exception:
            # A transient Win32/Tk failure (e.g. Explorer restarting) must
            # never end the refresh loop; hide and retry next time.
            self._hide()
        self._root.after(REFRESH_MS, self._tick)

    def _refresh(self) -> None:
        """Redraws the text and moves the window next to the notification area."""
        layout = _taskbar_layout()
        segments = self.provider()
        if layout is None or not segments or _fullscreen_app_running():
            self._hide()
            return
        top, bottom, tray_left, taskbar_left = layout

        # The background takes the taskbar's own color (sampled in the gap
        # next to the tray) and is keyed out, so anti-aliased text edges blend
        # into the taskbar instead of a mismatched color.
        key_color = _screen_color(tray_left - TRAY_GAP // 2, (top + bottom) // 2) or self._key_color or '#000000'
        if key_color != self._key_color:
            self._key_color = key_color
            self._canvas.configure(bg=key_color)
            self._root.attributes('-transparentcolor', key_color)
            self._drawn = None  # redraw so text anti-aliases against the new color

        height = bottom - top
        if segments != self._drawn:
            self._draw(segments, height)

        left = max(taskbar_left, tray_left - TRAY_GAP - self._width)
        geometry = f"{self._width}x{height}+{left}+{top}"
        if geometry != self._geometry:
            self._geometry = geometry
            self._root.geometry(geometry)

        if not self._visible:
            self._visible = True
            self._root.deiconify()
            if self._hwnd is None:
                self._hwnd = wintypes.HWND(int(self._root.wm_frame(), 16))
                # Clicking the text must not steal focus from the active window
                style = _user32.GetWindowLongW(self._hwnd, _GWL_EXSTYLE)
                _user32.SetWindowLongW(self._hwnd, _GWL_EXSTYLE, style | _WS_EX_NOACTIVATE)

        # Clicking the taskbar raises it above other topmost windows; take the top back
        _user32.SetWindowPos(self._hwnd, wintypes.HWND(_HWND_TOPMOST), 0, 0, 0, 0,
                             _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)

    def _draw(self, segments: List[Segment], height: int) -> None:
        """
        Draws the text runs left to right, vertically centered.

        Args:
            segments (List[Segment]): The text runs and their colors.
            height (int): Window height in pixels.
        """
        self._canvas.delete('all')
        x = 0
        for i, (text, color) in enumerate(segments):
            if i:
                x += SEGMENT_GAP
            self._canvas.create_text(x, height // 2, text=text, anchor='w', font=self._font,
                                     fill='#%02x%02x%02x' % color)
            x += self._font.measure(text)
        self._width = max(x, 1)
        self._drawn = segments

    def _hide(self) -> None:
        """Withdraws the window if it's showing."""
        if self._visible:
            self._visible = False
            self._root.withdraw()
