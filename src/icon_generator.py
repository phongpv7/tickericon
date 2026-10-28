import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.config_handler import DEFAULT_BAND_COLORS, DEFAULT_FONT_SIZE, DEFAULT_STRIP_WIDTH, AppConfig

# Prices are rendered at this font size, then scaled down to fit the icon
PRICE_RENDER_FONT_SIZE = 128

# Gap kept between a price and the icon edges/strip, in pixels
PRICE_MARGIN = 1


class IconGenerator:
    """
    Generates dynamic 64x64 PIL Images for the system tray icon on a static
    (or transparent) background, with a bottom strip indicating market state
    or fetch errors.
    """

    @staticmethod
    def _resolve_app_icon_path() -> Path:
        """
        Locates assets/tickericon.ico in the PyInstaller bundle (frozen) or the project root (dev).

        Returns:
            Path: The resolved filesystem path to the application icon file.
        """
        if getattr(sys, 'frozen', False):
            # PyInstaller 6+ onedir places bundled data under _internal (sys._MEIPASS)
            base_dir = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
        else:
            base_dir = Path(__file__).parent.parent
        return base_dir / "assets" / "tickericon.ico"

    def __init__(self, config: AppConfig):
        """
        Args:
            config (AppConfig): Loaded app configuration with the per-state
                color schemes and the global background/error colors.
        """
        self.bg_color = config['bg_color']
        # Leave the background transparent instead of filling it with bg_color
        self.transparent = bool(config.get('transparent_background', False))
        self.error_color = config['error_color']
        # Per-state schemes: {'open'|'premarket'|'after_hours'|'closed':
        # {'positive', 'negative', 'strip'}}
        self.state_colors = config['state_colors']
        # Price band colors: {'ceiling'|'reference'|'floor': RGB}
        self.band_colors = config.get('band_colors', DEFAULT_BAND_COLORS)

        # Height of the bottom state/error strip in pixels (0 disables it)
        self.strip_width = config.get('strip_width', DEFAULT_STRIP_WIDTH)

        # Pre-load font to optimize rendering loop
        font_size = config.get('font_size', DEFAULT_FONT_SIZE)
        try:
            self.font = ImageFont.truetype("arialbd.ttf", font_size)
        except IOError:
            self.font = ImageFont.load_default()
        # Fonts at other sizes (the large price render), loaded on first use
        self._fonts_by_size = {font_size: self.font}

        # Pre-load the static application icon, shown while loading and beside the taskbar widget
        try:
            self._app_icon = Image.open(self._resolve_app_icon_path()).convert('RGB').resize((64, 64))
        except (IOError, OSError):
            self._app_icon = None

    def _state_scheme(self, state: str) -> dict:
        """
        Resolves the color scheme for a market state, falling back to the
        'closed' scheme for unknown states.

        Args:
            state (str): Current market state (e.g., 'open', 'closed').

        Returns:
            dict: The state's {'positive', 'negative', 'strip'} colors.
        """
        return self.state_colors.get(state, self.state_colors['closed'])

    def text_color(self, state: str, change_pct: float, band: str = '') -> tuple:
        """
        Resolves the value text color: the price band's color at a daily
        limit or the reference price, otherwise the state's color scheme.

        Args:
            state (str): Current market state (e.g., 'open', 'closed').
            change_pct (float): Percentage change for the day.
            band (str): The ticker's StockData.price_band ('ceiling',
                'reference', 'floor', or '').

        Returns:
            tuple: The (R, G, B) text color.
        """
        if band in self.band_colors:
            return self.band_colors[band]
        scheme = self._state_scheme(state)
        return scheme['positive'] if change_pct >= 0 else scheme['negative']

    def strip_color(self, state: str, has_error: bool = False) -> tuple:
        """
        Resolves the color of the bottom indicator strip.

        Args:
            state (str): Current market state (e.g., 'open', 'closed').
            has_error (bool): Whether the last data fetch failed; takes
                precedence over the market state.

        Returns:
            tuple: The (R, G, B) strip color.
        """
        if has_error:
            return self.error_color
        return self._state_scheme(state)['strip']

    def new_frame(self, width: int, text_color: tuple) -> Image.Image:
        """
        Creates a blank 64px-tall frame filled with the static background,
        or a transparent one when the transparent background is enabled.

        Args:
            width (int): Pixel width of the frame.
            text_color (tuple): The (R, G, B) color of the text to be drawn
                on it. A transparent frame carries it at zero opacity, so
                anti-aliased text edges fade out instead of darkening.

        Returns:
            Image.Image: An RGB frame, or an RGBA one when transparent.
        """
        if self.transparent:
            return Image.new('RGBA', (width, 64), (*text_color, 0))
        return Image.new('RGB', (width, 64), color=self.bg_color)

    def strip_visible(self, color: tuple) -> bool:
        """
        Args:
            color (tuple): The (R, G, B) strip color.

        Returns:
            bool: False when the strip is disabled (width 0), or when it would
                match the background it's meant to blend into on a
                transparent frame (the default closed-state strip).
        """
        if self.strip_width <= 0:
            return False
        return not (self.transparent and color == self.bg_color)

    def draw_strip(self, draw: ImageDraw.ImageDraw, width: int, color: tuple) -> None:
        """
        Draws the bottom indicator strip onto an icon-sized drawing surface.
        Does nothing when the strip isn't visible (see strip_visible).

        Args:
            draw (ImageDraw.ImageDraw): Drawing context of the target image.
            width (int): Pixel width of the target image.
            color (tuple): The (R, G, B) strip color.
        """
        if not self.strip_visible(color):
            return
        draw.rectangle([(0, 64 - self.strip_width), (width - 1, 63)], fill=color)

    def generate_value_frame(self, change_pct: float, state: str, has_error: bool = False,
                             band: str = '') -> Image.Image:
        """
        Creates the icon image based on market state and price action.

        Args:
            change_pct (float): Percentage change for the day.
            state (str): Current market state (e.g., 'open', 'closed').
            has_error (bool): Whether the last data fetch failed; colors the
                bottom strip with the error color.
            band (str): The ticker's StockData.price_band; overrides the
                text color at a daily limit or the reference price.

        Returns:
            Image.Image: A 64x64 Pillow Image ready for pystray.
        """
        text_color = self.text_color(state, change_pct, band)

        img = self.new_frame(64, text_color)
        draw = ImageDraw.Draw(img)

        # Format text: drop decimal for double digits to keep font large
        abs_change = abs(change_pct)
        if abs_change >= 9.95:
            text = f"{abs_change:.0f}"
        else:
            text = f"{abs_change:.1f}"

        # Calculate text bounding box to perfectly center it
        bbox = draw.textbbox((0, 0), text, font=self.font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x = (64 - text_width) / 2
        y = self._text_y(text_height)

        draw.text((x, y), text, fill=text_color, font=self.font)

        self.draw_strip(draw, 64, self.strip_color(state, has_error))

        return img

    def generate_price_frame(self, price: float, currency: str, change_pct: float, state: str,
                             has_error: bool = False, band: str = '') -> Image.Image:
        """
        Creates the icon image showing a ticker's price, colored by its daily
        change like the percentage frame.

        Args:
            price (float): Latest price, in the quote currency.
            currency (str): Quote currency code, e.g. 'VND'.
            change_pct (float): Percentage change for the day; picks the text color.
            state (str): Current market state (e.g., 'open', 'closed').
            has_error (bool): Whether the last data fetch failed; colors the
                bottom strip with the error color.
            band (str): The ticker's StockData.price_band; overrides the
                text color at a daily limit or the reference price.

        Returns:
            Image.Image: A 64x64 Pillow Image ready for pystray.
        """
        text = self.format_icon_price(price, currency)
        text_color = self.text_color(state, change_pct, band)

        img = self.new_frame(64, text_color)
        draw = ImageDraw.Draw(img)

        # Scale the price as large as fits the space above the strip
        glyphs = self._render_glyphs(text)
        box_width = 64 - 2 * PRICE_MARGIN
        box_height = 64 - self.strip_width - 2 * PRICE_MARGIN
        scale = min(box_width / glyphs.width, box_height / glyphs.height)
        width = max(1, round(glyphs.width * scale))
        height = max(1, round(glyphs.height * scale))
        mask = glyphs.resize((width, height), Image.Resampling.LANCZOS)

        img.paste(text_color, ((64 - width) // 2, PRICE_MARGIN + (box_height - height) // 2), mask)

        self.draw_strip(draw, 64, self.strip_color(state, has_error))

        return img

    def _render_glyphs(self, text: str) -> Image.Image:
        """
        Renders `text` at a large size as a grayscale coverage mask, so it
        can be scaled down to any box while staying sharp.

        Args:
            text (str): The text to render.

        Returns:
            Image.Image: An 'L' mode mask cropped to the text's ink.
        """
        font = self._font_of_size(PRICE_RENDER_FONT_SIZE)
        bbox = ImageDraw.Draw(Image.new('L', (1, 1))).textbbox((0, 0), text, font=font)
        mask = Image.new('L', (max(bbox[2], 1), max(bbox[3], 1)), 0)
        ImageDraw.Draw(mask).text((0, 0), text, fill=255, font=font)
        return mask.crop(mask.getbbox() or (0, 0, 1, 1))

    @staticmethod
    def format_icon_price(price: float, currency: str) -> str:
        """
        Formats a price compactly enough to fit the icon.

        Args:
            price (float): The price to format.
            currency (str): Quote currency code, e.g. 'VND'.

        Returns:
            str: For VND, thousands with up to two decimals like on Vietnamese
                price boards (63,500 -> '63.5'); otherwise fewer decimals as
                the price grows ('9.87', '45.7', '123').
        """
        if currency == 'VND':
            return f"{price / 1000:.2f}".rstrip('0').rstrip('.')
        if price >= 100:
            return f"{price:.0f}"
        if price >= 10:
            return f"{price:.1f}"
        return f"{price:.2f}"

    def _font_of_size(self, size: int):
        """
        Args:
            size (int): Font size in pixels.

        Returns:
            ImageFont.FreeTypeFont: The icon font at `size`, or the default
                font unchanged when the icon font isn't available.
        """
        if size not in self._fonts_by_size:
            try:
                self._fonts_by_size[size] = ImageFont.truetype("arialbd.ttf", size)
            except IOError:
                self._fonts_by_size[size] = self.font
        return self._fonts_by_size[size]

    def generate_loading_frame(self) -> Image.Image:
        """
        Creates a static placeholder icon shown before the first data fetch completes.

        Returns:
            Image.Image: A 64x64 Pillow Image ready for pystray.
        """
        text_color = self._contrast_text_color(self.bg_color)

        img = self.new_frame(64, text_color)
        draw = ImageDraw.Draw(img)

        text = "..."
        bbox = draw.textbbox((0, 0), text, font=self.font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x = (64 - text_width) / 2
        y = self._text_y(text_height)

        draw.text((x, y), text, fill=text_color, font=self.font)

        return img

    def generate_app_icon(self) -> Image.Image:
        """
        Returns the static application icon, shown while loading and beside the taskbar widget.

        Returns:
            Image.Image: A 64x64 Pillow Image ready for pystray.
        """
        if self._app_icon is not None:
            return self._app_icon.copy()
        return self.generate_loading_frame()

    def generate_scroll_frame(self, symbol: str, state: str, offset: int, has_error: bool = False) -> Image.Image:
        """
        Creates one frame of the right-to-left ticker symbol marquee.

        Args:
            symbol (str): The ticker symbol being scrolled, e.g. 'AMD'.
            state (str): Current market state, used for the bottom strip color.
            offset (int): Pixels the text has travelled from the right edge.
            has_error (bool): Whether the last data fetch failed; takes
                precedence over market state for strip color.

        Returns:
            Image.Image: A 64x64 Pillow Image ready for pystray.
        """
        text_color = self._contrast_text_color(self.bg_color)

        img = self.new_frame(64, text_color)
        draw = ImageDraw.Draw(img)

        bbox = draw.textbbox((0, 0), symbol, font=self.font)
        text_height = bbox[3] - bbox[1]

        x = 64 - offset
        y = self._text_y(text_height)

        draw.text((x, y), symbol, fill=text_color, font=self.font)

        self.draw_strip(draw, 64, self.strip_color(state, has_error))

        return img

    def _text_y(self, text_height: float) -> float:
        """
        Computes the vertical text position: centered with a slight upward
        offset for visual balance, raised further by the strip width so the
        text never overlaps the bottom indicator strip.

        Args:
            text_height (float): Height of the rendered text in pixels.

        Returns:
            float: The y coordinate to draw the text at.
        """
        return (64 - text_height) / 2 - 4 - self.strip_width

    def measure_text_width(self, text: str) -> int:
        """
        Measures the pixel width `text` would occupy when rendered with the
        icon font, used to size the ticker symbol scroll animation.

        Args:
            text (str): The text to measure.

        Returns:
            int: The width, in pixels.
        """
        bbox = ImageDraw.Draw(Image.new('RGB', (1, 1))).textbbox((0, 0), text, font=self.font)
        return bbox[2] - bbox[0]

    @staticmethod
    def _contrast_text_color(bg_color: tuple) -> tuple:
        """
        Picks black or white text so the ticker symbol stays legible on any background.

        Args:
            bg_color (tuple): The (R, G, B) background color to contrast against.

        Returns:
            tuple: (0, 0, 0) for black or (255, 255, 255) for white text.
        """
        luminance = 0.299 * bg_color[0] + 0.587 * bg_color[1] + 0.114 * bg_color[2]
        return (0, 0, 0) if luminance > 140 else (255, 255, 255)