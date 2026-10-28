try:
    import tkinter as tk
    from tkinter import colorchooser, messagebox, ttk
except ModuleNotFoundError:  # Python built without Tkinter (headless/non-Windows)
    tk = None
    colorchooser = None
    messagebox = None
    ttk = None

from src.config_handler import (
    ConfigHandler,
    DEFAULT_BAND_COLORS,
    DEFAULT_FONT_SIZE,
    DEFAULT_STRIP_WIDTH,
    DEFAULT_TICKER_VALUE_MODE,
    MARKET_STATES,
    MAX_STRIP_WIDTH,
    MAX_TICKERS,
    PRICE_BANDS,
    TICKER_VALUE_MODES,
)
from src.alerts import parse_alerts

# Uniform outer/section margin in pixels
PADDING = 12

STATE_LABELS = {
    'open': 'Open',
    'premarket': 'Premarket',
    'after_hours': 'After hours',
    'closed': 'Closed',
}

VALUE_MODE_LABELS = {
    'percentage': 'Percentage',
    'price': 'Price',
    'both': 'Both',
}

BAND_LABELS = {
    'ceiling': 'Ceiling',
    'reference': 'Reference',
    'floor': 'Floor',
}


class ConfigWindow:
    """Settings dialog for editing the tray app's persisted config,
    organized into Symbols / Alerts / General / Colors tabs with native
    number inputs and a color picker per color field."""

    def __init__(self, config_path: str, config: dict, on_save=None):
        """Initialize the configuration dialog.

        Args:
            config_path (str): The path to the config.cfg file to edit.
            config (dict): The currently loaded configuration values.
            on_save (callable, optional): Callback triggered after a successful save.

        Raises:
            RuntimeError: If Tkinter is unavailable in the current environment.
        """
        if tk is None or ttk is None:
            raise RuntimeError("Tkinter is not available in this environment.")

        self.config_path = config_path
        self.config = config
        self.on_save = on_save

        self.root = tk.Tk()
        self.root.title("TickerIcon Settings")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        self.entries = {}
        self._swatches = {}
        self._build_form()

    def _build_form(self):
        """Create all of the widgets for the configuration form.

        Returns:
            None: The form is built in-place on the window's root widget.
        """
        container = ttk.Frame(self.root, padding=PADDING)
        container.pack(fill="both", expand=True)

        # One tab per section keeps the window short enough for any screen,
        # with Save and Cancel always in view below the tabs
        notebook = ttk.Notebook(container)
        notebook.pack(fill="both", expand=True)
        for title, build_section in (("Symbols", self._build_symbols_section),
                                     ("Alerts", self._build_alerts_section),
                                     ("General", self._build_general_section),
                                     ("Colors", self._build_colors_section)):
            tab = ttk.Frame(notebook, padding=PADDING)
            notebook.add(tab, text=title)
            build_section(tab)

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=(PADDING, 0))
        ttk.Button(actions, text="Save", command=self._save).pack(side="right")
        ttk.Button(actions, text="Cancel", command=self.root.destroy).pack(side="right", padx=(0, 8))

    def _build_section(self, parent, description: str):
        """Creates a tab's body with a short description at the top.

        Args:
            parent (ttk.Frame): The tab to fill.
            description (str): A short explanation of the tab's fields.

        Returns:
            ttk.Frame: The tab body to place fields into.
        """
        section = ttk.Frame(parent)
        section.pack(fill="x")
        ttk.Label(section, text=description, foreground="gray").pack(anchor="w", pady=(0, 6))
        return section

    def _build_general_section(self, parent):
        """Builds the General section: timing and icon dimension inputs.

        Args:
            parent (ttk.Frame): The container to add the section to.
        """
        section = self._build_section(
            parent,
            "Animation timing (milliseconds) and icon dimensions (pixels).",
        )

        self._add_spinbox(section, "Scroll speed (ms)", "scroll_speed_ms",
                          self.config["scroll_speed_ms"], from_=100, to=10000, increment=100)
        self._add_spinbox(section, "Display time (ms)", "display_ms",
                          self.config["display_ms"], from_=100, to=60000, increment=100)
        self._add_spinbox(section, "Icon font size (px)", "font_size",
                          self.config.get("font_size", DEFAULT_FONT_SIZE), from_=1, to=64, increment=1)
        self._add_spinbox(section, "Strip width (px)", "strip_width",
                          self.config.get("strip_width", DEFAULT_STRIP_WIDTH), from_=0, to=MAX_STRIP_WIDTH, increment=1)

        self.continuous_scroll_var = tk.BooleanVar(value=self.config.get("continuous_scroll", True))
        ttk.Checkbutton(
            section,
            text="Scroll all symbols continuously in one line",
            variable=self.continuous_scroll_var,
        ).pack(anchor="w", pady=(6, 0))

        self.taskbar_widget_var = tk.BooleanVar(value=self.config.get("taskbar_widget", False))
        ttk.Checkbutton(
            section,
            text="Show prices on the taskbar next to the tray icons",
            variable=self.taskbar_widget_var,
        ).pack(anchor="w", pady=(2, 0))

        row = ttk.Frame(section)
        row.pack(fill="x", pady=(6, 2))
        ttk.Label(row, text="Ticker values", width=20).pack(side="left")
        value_mode = ttk.Combobox(
            row, state="readonly", width=12,
            values=[VALUE_MODE_LABELS[mode] for mode in TICKER_VALUE_MODES],
        )
        value_mode.set(VALUE_MODE_LABELS[self.config.get("ticker_value_mode", DEFAULT_TICKER_VALUE_MODE)])
        value_mode.pack(side="left")
        self.entries["ticker_value_mode"] = value_mode

        self.show_daily_high_var = tk.BooleanVar(value=self.config.get("show_daily_high", False))
        ttk.Checkbutton(
            section,
            text="Show daily high (\u2191) in the ticker",
            variable=self.show_daily_high_var,
        ).pack(anchor="w", pady=(2, 0))

        self.show_daily_low_var = tk.BooleanVar(value=self.config.get("show_daily_low", False))
        ttk.Checkbutton(
            section,
            text="Show daily low (\u2193) in the ticker",
            variable=self.show_daily_low_var,
        ).pack(anchor="w", pady=(2, 0))

        self.high_low_pct_var = tk.BooleanVar(value=self.config.get("high_low_pct", False))
        ttk.Checkbutton(
            section,
            text="Show high/low change percentages",
            variable=self.high_low_pct_var,
        ).pack(anchor="w", pady=(2, 0))

    def _build_symbols_section(self, parent):
        """Builds the Symbols section: the international and Vietnamese watchlists.

        Args:
            parent (ttk.Frame): The container to add the section to.
        """
        section = self._build_section(
            parent,
            f"Comma separated, up to {MAX_TICKERS} in all; add :Name to label one, e.g. XAU:Gold.\n"
            "International: Binance assets, e.g. BTC, ETH, XAU (gold), BZ (Brent), AAPL.\n"
            "Vietnam: stock codes, e.g. VHM, VIC, SSI.",
        )
        international, vietnam = ConfigHandler.format_watchlists(
            self.config["tickers"], self.config.get("names", {}), separator=", ")
        for label, key, value in (("International", "international", international),
                                  ("Vietnam", "vietnam", vietnam)):
            row = ttk.Frame(section)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=20).pack(side="left")
            entry = ttk.Entry(row, width=48)
            entry.insert(0, value)
            entry.pack(side="left", fill="x", expand=True)
            self.entries[key] = entry

    def _build_alerts_section(self, parent):
        """Builds the Alerts section: price targets that raise a Windows notification.

        Args:
            parent (ttk.Frame): The container to add the section to.
        """
        section = self._build_section(
            parent,
            "Notify when a price reaches a target, e.g. VHM>=65, BTC<=80000 (Vietnamese stocks in thousands).",
        )
        self.entries["alerts"] = ttk.Entry(section, width=60)
        self.entries["alerts"].insert(0, self.config.get("alerts", ""))
        self.entries["alerts"].pack(fill="x")

    def _build_colors_section(self, parent):
        """Builds the Colors section: global colors plus one side-by-side
        subsection per market state.

        Args:
            parent (ttk.Frame): The container to add the section to.
        """
        section = self._build_section(
            parent,
            "Hex colors (#RRGGBB); click a swatch to open the color picker.",
        )

        global_row = ttk.Frame(section)
        global_row.pack(fill="x", pady=(0, 6))
        self._add_color_field(global_row, "Background", "bg_color", self.config["bg_color"])
        self._add_color_field(global_row, "Error strip", "error_color", self.config["error_color"])

        self.transparent_background_var = tk.BooleanVar(value=self.config.get("transparent_background", False))
        ttk.Checkbutton(
            section,
            text="Transparent background",
            variable=self.transparent_background_var,
        ).pack(anchor="w", pady=(0, 6))

        states_row = ttk.Frame(section)
        states_row.pack(fill="x")
        for column, state in enumerate(MARKET_STATES):
            frame = ttk.Labelframe(states_row, text=STATE_LABELS[state], padding=6)
            frame.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0))
            states_row.columnconfigure(column, weight=1)
            scheme = self.config["state_colors"][state]
            self._add_color_field(frame, "Positive", f"{state}_positive", scheme["positive"])
            self._add_color_field(frame, "Negative", f"{state}_negative", scheme["negative"])
            self._add_color_field(frame, "Strip", f"{state}_strip", scheme["strip"])

        # Override positive/negative at the daily limits and the reference price
        bands = ttk.Labelframe(section, text="Price limits (Vietnam)", padding=6)
        bands.pack(fill="x", pady=(6, 0))
        band_colors = self.config.get("band_colors", DEFAULT_BAND_COLORS)
        for band in PRICE_BANDS:
            self._add_color_field(bands, BAND_LABELS[band], f"{band}_color", band_colors[band])

    def _add_spinbox(self, parent, label: str, key: str, value, from_: int, to: int, increment: int):
        """Adds a labelled integer spinbox row.

        Args:
            parent (ttk.Frame): The container to add the row to.
            label (str): The field label shown to the user.
            key (str): The entries-dict key to store the widget under.
            value: The initial value.
            from_ (int): Minimum selectable value.
            to (int): Maximum selectable value.
            increment (int): Step applied by the up/down arrows.
        """
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=20).pack(side="left")
        spinbox = ttk.Spinbox(row, from_=from_, to=to, increment=increment, width=10)
        spinbox.set(str(value))
        spinbox.pack(side="left")
        self.entries[key] = spinbox

    def _add_color_field(self, parent, label: str, key: str, rgb):
        """Adds a labelled hex color entry with a clickable picker swatch.

        Args:
            parent (ttk.Frame): The container to add the row to.
            label (str): The field label shown to the user.
            key (str): The entries-dict key to store the widget under.
            rgb (tuple): The initial (R, G, B) color.
        """
        hex_value = ConfigHandler._rgb_to_hex(rgb)
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=12).pack(side="left")
        entry = ttk.Entry(row, width=9)
        entry.insert(0, hex_value)
        entry.pack(side="left")
        # Plain tk.Button: ttk buttons can't show a background color swatch
        swatch = tk.Button(row, width=2, bg=hex_value,
                           command=lambda: self._pick_color(key, label))
        swatch.pack(side="left", padx=(4, 0))
        self.entries[key] = entry
        self._swatches[key] = swatch

    def _pick_color(self, key: str, label: str):
        """Opens the system color picker and writes the choice back to the field.

        Args:
            key (str): The entries-dict key of the color field.
            label (str): The field label, used as the picker dialog title.
        """
        current = self.entries[key].get().strip()
        if not current.startswith("#"):
            current = f"#{current}"
        try:
            result = colorchooser.askcolor(color=current, parent=self.root, title=f"{label} color")
        except tk.TclError:
            # Current entry text isn't a valid color; open the picker without a preset
            result = colorchooser.askcolor(parent=self.root, title=f"{label} color")
        if result and result[1]:
            hex_value = result[1].upper()
            self.entries[key].delete(0, "end")
            self.entries[key].insert(0, hex_value)
            self._swatches[key].configure(bg=hex_value)

    def _save(self):
        """Validate the form, persist the config, and invoke the save callback.

        Returns:
            None: The user is notified through the dialog if validation fails.
        """
        try:
            config = self._collect_config()
            ConfigHandler(self.config_path).save(config)
            if self.on_save is not None:
                self.on_save(config)
            self.root.destroy()
        except OSError as exc:
            messagebox.showerror("Save failed", f"Could not write config file: {exc}")
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc))

    def _get_int(self, key: str, label: str) -> int:
        """Reads an integer field, raising a user-friendly error when invalid.

        Args:
            key (str): The entries-dict key of the field.
            label (str): The field label used in error messages.

        Returns:
            int: The parsed value.

        Raises:
            ValueError: If the field does not contain an integer.
        """
        try:
            return int(self.entries[key].get())
        except ValueError as exc:
            raise ValueError(f"{label} must be an integer.") from exc

    def _collect_config(self) -> dict:
        """Read the form values and return a normalized configuration dict.

        Returns:
            dict: A configuration dictionary matching the repository's config schema.

        Raises:
            ValueError: If required values are missing or invalid.
        """
        tickers, names = ConfigHandler.parse_watchlists(
            self.entries["international"].get(), self.entries["vietnam"].get())
        if not tickers:
            raise ValueError("At least one ticker is required.")
        if len(tickers) > MAX_TICKERS:
            raise ValueError(f"Ticker list exceeds MAX_TICKERS ({MAX_TICKERS}).")

        alerts = self.entries["alerts"].get().strip()
        parse_alerts(alerts, tickers, names)  # raises ValueError on a malformed alert

        def get_hex(key: str):
            value = self.entries[key].get().strip()
            if not value.startswith("#"):
                value = f"#{value}"
            try:
                return ConfigHandler._hex_to_rgb(value)
            except Exception as exc:  # GUI-side validation
                raise ValueError(f"{key.replace('_', ' ').title()} must be a hex color like #0FAF50") from exc

        scroll_speed_ms = self._get_int("scroll_speed_ms", "Scroll speed")
        if scroll_speed_ms <= 0:
            raise ValueError("Scroll speed must be positive.")

        display_ms = self._get_int("display_ms", "Display time")
        if display_ms <= 0:
            raise ValueError("Display time must be positive.")

        font_size = self._get_int("font_size", "Font size")
        if font_size <= 0:
            raise ValueError("Font size must be positive.")

        strip_width = self._get_int("strip_width", "Strip width")
        if not 0 <= strip_width <= MAX_STRIP_WIDTH:
            raise ValueError(f"Strip width must be between 0 and {MAX_STRIP_WIDTH}.")

        mode_label = self.entries["ticker_value_mode"].get().strip()
        mode_by_label = {label: mode for mode, label in VALUE_MODE_LABELS.items()}
        if mode_label not in mode_by_label:
            raise ValueError(f"Ticker values must be one of: {', '.join(VALUE_MODE_LABELS.values())}.")

        return {
            "tickers": tickers,
            "names": names,
            "alerts": alerts,
            "bg_color": get_hex("bg_color"),
            "transparent_background": bool(self.transparent_background_var.get()),
            "error_color": get_hex("error_color"),
            "state_colors": {
                state: {
                    "positive": get_hex(f"{state}_positive"),
                    "negative": get_hex(f"{state}_negative"),
                    "strip": get_hex(f"{state}_strip"),
                }
                for state in MARKET_STATES
            },
            "band_colors": {band: get_hex(f"{band}_color") for band in PRICE_BANDS},
            "scroll_speed_ms": scroll_speed_ms,
            "display_ms": display_ms,
            "continuous_scroll": bool(self.continuous_scroll_var.get()),
            "taskbar_widget": bool(self.taskbar_widget_var.get()),
            "ticker_value_mode": mode_by_label[mode_label],
            "show_daily_high": bool(self.show_daily_high_var.get()),
            "show_daily_low": bool(self.show_daily_low_var.get()),
            "high_low_pct": bool(self.high_low_pct_var.get()),
            "font_size": font_size,
            "strip_width": strip_width,
        }

    def show(self):
        """Start the Tkinter event loop for the settings window.

        Returns:
            None: This blocks until the window is closed.
        """
        self.root.mainloop()
