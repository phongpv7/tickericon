from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from src.price_history import Candles

# An indicator's value per candle; None until it has enough candles behind it
Series = List[Optional[float]]

# The ratings, from every signal saying buy to every signal saying sell. The
# rating and findings are worded in Vietnamese, for the app's users
STRONG_BUY, BUY, NEUTRAL, SELL, STRONG_SELL = 'Mua mạnh', 'Mua', 'Trung tính', 'Bán', 'Bán mạnh'

# The moving averages the rating compares the price with, simple and
# exponential, as TradingView's technical rating does (alongside a 20-day
# volume-weighted average, a 9-day Hull average, and the Ichimoku cloud)
_RATING_AVERAGES = (10, 20, 30, 50, 100, 200)

# Ichimoku periods: the conversion line (tenkan-sen), the base line
# (kijun-sen), and leading span B. The leading spans are drawn this many
# candles ahead and the lagging span (chikou) this many behind: TradingView's
# displacement of 26, counting the current candle
ICHIMOKU_PERIODS = (9, 26, 52)
ICHIMOKU_SHIFT = 25

# Fibonacci retracement ratios, measured back from where a swing ended, of the
# biggest swing within about six months of candles (the chart's span)
FIBONACCI_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
FIBONACCI_LOOKBACK = 120

# Price changes listed under performance, in calendar days
_PERFORMANCE_PERIODS = (('1 tuần', 7), ('1 tháng', 30), ('3 tháng', 91), ('6 tháng', 182), ('1 năm', 365))

# What each candlestick pattern suggests, by its Vietnamese name
_PATTERN_MEANINGS = {
    'Sao mai': 'mẫu hình đảo chiều tăng sau nhịp giảm',
    'Sao hôm': 'mẫu hình đảo chiều giảm sau nhịp tăng',
    'Ba chàng lính trắng': 'lực mua mạnh và bền qua ba phiên',
    'Ba con quạ đen': 'lực bán mạnh và bền qua ba phiên',
    'Nhấn chìm tăng': 'bên mua lấn át bên bán, có thể đảo chiều tăng',
    'Nhấn chìm giảm': 'bên bán lấn át bên mua, có thể đảo chiều giảm',
    'Xuyên thủng': 'bên mua đẩy giá qua nửa thân nến giảm trước, có thể đảo chiều tăng',
    'Mây đen che phủ': 'bên bán đẩy giá xuống quá nửa thân nến tăng trước, có thể đảo chiều giảm',
    'Harami tăng': 'đà giảm chững lại, có thể đảo chiều tăng',
    'Harami giảm': 'đà tăng chững lại, có thể đảo chiều giảm',
    'Marubozu tăng': 'bên mua áp đảo suốt phiên',
    'Marubozu giảm': 'bên bán áp đảo suốt phiên',
    'Búa': 'lực cầu bắt đáy mạnh, có thể đảo chiều tăng',
    'Người treo cổ': 'cảnh báo đà tăng suy yếu',
    'Búa ngược': 'có thể đảo chiều tăng nếu phiên sau xác nhận',
    'Sao băng': 'áp lực bán ở vùng giá cao, cảnh báo đảo chiều giảm',
    'Doji': 'bên mua và bên bán cân bằng, thị trường đang lưỡng lự',
}

_DAY_S = 24 * 60 * 60


@dataclass
class Fibonacci:
    """Fibonacci retracement levels of the biggest recent swing."""

    start: int  # index of the candle the swing started at
    end: int    # and ended at
    # (ratio, price) pairs, ratio 0 where the swing ended and 1 where it started
    levels: List[Tuple[float, float]]


@dataclass
class Analysis:
    """A technical read of one symbol's daily candles."""

    # STRONG_BUY, BUY, NEUTRAL, SELL, or STRONG_SELL, from a score between
    # -1 (every signal says sell) and 1 (every signal says buy)
    rating: str
    score: float
    # (buy, neutral, sell) signal counts from the moving averages and the oscillators
    average_signals: Tuple[int, int, int]
    oscillator_signals: Tuple[int, int, int]
    # (topic, finding) pairs, e.g. ('Xu hướng', 'Xu hướng tăng: ...')
    notes: List[Tuple[str, str]]
    # Chart series, aligned with the candles, except the Ichimoku leading
    # spans, which run ICHIMOKU_SHIFT candles past the last one
    sma20: Series
    sma50: Series
    sma200: Series
    band_upper: Series
    band_lower: Series
    rsi: Series
    stochastic_k: Series
    stochastic_d: Series
    stochastic_rsi_k: Series
    stochastic_rsi_d: Series
    tenkan: Series
    kijun: Series
    span_a: Series
    span_b: Series
    chikou: Series
    macd: Series
    macd_signal: Series
    macd_histogram: Series
    fibonacci: Optional[Fibonacci]
    # Every indicator behind the rating, as (name, latest value, signal)
    # rows, the signal 1 for buy, -1 sell, 0 neutral, None without data
    oscillator_table: List[Tuple[str, str, Optional[int]]]
    average_table: List[Tuple[str, str, Optional[int]]]


def sma(values: Sequence[float], period: int) -> Series:
    """Simple moving average."""
    out: Series = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: Sequence[Optional[float]], period: int) -> Series:
    """Exponential moving average, seeded with the simple average of its first period."""
    out: Series = [None] * len(values)
    start = next((i for i, value in enumerate(values) if value is not None), len(values))
    if len(values) - start < period:
        return out
    current = sum(values[start:start + period]) / period
    out[start + period - 1] = current
    weight = 2 / (period + 1)
    for i in range(start + period, len(values)):
        current += (values[i] - current) * weight
        out[i] = current
    return out


def rma(values: Sequence[Optional[float]], period: int) -> Series:
    """Wilder's moving average, seeded with the simple average of its first period."""
    out: Series = [None] * len(values)
    start = next((i for i, value in enumerate(values) if value is not None), len(values))
    if len(values) - start < period:
        return out
    current = sum(values[start:start + period]) / period
    out[start + period - 1] = current
    for i in range(start + period, len(values)):
        current = (current * (period - 1) + values[i]) / period
        out[i] = current
    return out


def wma(values: Sequence[Optional[float]], period: int) -> Series:
    """Weighted moving average, the latest value weighing the most."""
    out: Series = [None] * len(values)
    total_weight = period * (period + 1) / 2
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if None not in window:
            out[i] = sum(weight * value for weight, value in zip(range(1, period + 1), window)) / total_weight
    return out


def hull(values: Sequence[float], period: int = 9) -> Series:
    """Hull moving average: quicker to turn than the others, with little lag."""
    half, full = wma(values, period // 2), wma(values, period)
    raw = [2 * h - f if h is not None and f is not None else None for h, f in zip(half, full)]
    return wma(raw, int(period ** 0.5))


def vwma(candles: Candles, period: int = 20) -> Series:
    """Volume-weighted moving average."""
    closes, volumes = candles.closes, candles.volumes
    out: Series = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        volume = sum(volumes[i - period + 1:i + 1])
        if volume > 0:
            out[i] = sum(close * size for close, size in
                         zip(closes[i - period + 1:i + 1], volumes[i - period + 1:i + 1])) / volume
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    """Relative strength index, with Wilder's smoothing."""
    out: Series = [None] * len(closes)
    if len(closes) <= period:
        return out
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(change, 0) for change in changes[:period]) / period
    loss = sum(max(-change, 0) for change in changes[:period]) / period
    out[period] = _rsi_value(gain, loss)
    for i in range(period, len(changes)):
        gain = (gain * (period - 1) + max(changes[i], 0)) / period
        loss = (loss * (period - 1) + max(-changes[i], 0)) / period
        out[i + 1] = _rsi_value(gain, loss)
    return out


def _rsi_value(gain: float, loss: float) -> float:
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[Series, Series, Series]:
    """
    Returns:
        Tuple[Series, Series, Series]: The MACD line, its signal line, and
            their difference (the histogram).
    """
    line = [f - s if f is not None and s is not None else None
            for f, s in zip(ema(closes, fast), ema(closes, slow))]
    signal_line = ema(line, signal)
    histogram = [m - s if m is not None and s is not None else None for m, s in zip(line, signal_line)]
    return line, signal_line, histogram


def bollinger(closes: Sequence[float], period: int = 20, width: float = 2.0) -> Tuple[Series, Series, Series]:
    """
    Returns:
        Tuple[Series, Series, Series]: The middle band (the simple average)
            and the bands `width` standard deviations above and below it.
    """
    middle = sma(closes, period)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        mean = middle[i]
        deviation = (sum((close - mean) ** 2 for close in closes[i - period + 1:i + 1]) / period) ** 0.5
        upper[i] = mean + width * deviation
        lower[i] = mean - width * deviation
    return middle, upper, lower


def atr(candles: Candles, period: int = 14) -> Series:
    """Average true range, with Wilder's smoothing."""
    highs, lows, closes = candles.highs, candles.lows, candles.closes
    ranges = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    return rma(ranges, period)


def stochastic(candles: Candles, period: int = 14, smooth: int = 3) -> Tuple[Series, Series]:
    """
    Returns:
        Tuple[Series, Series]: The slow stochastic %K and its average, %D.
    """
    highs, lows, closes = candles.highs, candles.lows, candles.closes
    raw: Series = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        high, low = max(highs[i - period + 1:i + 1]), min(lows[i - period + 1:i + 1])
        raw[i] = 50.0 if high == low else (closes[i] - low) / (high - low) * 100
    k = _sma_of(raw, smooth)
    return k, _sma_of(k, smooth)


def stochastic_rsi(closes: Sequence[float], period: int = 14, smooth: int = 3) -> Tuple[Series, Series]:
    """
    Returns:
        Tuple[Series, Series]: The stochastic of the 14-day RSI, %K, and its average, %D.
    """
    values = rsi(closes, period)
    raw: Series = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = values[i - period + 1:i + 1]
        if None not in window:
            high, low = max(window), min(window)
            raw[i] = 50.0 if high == low else (values[i] - low) / (high - low) * 100
    k = _sma_of(raw, smooth)
    return k, _sma_of(k, smooth)


def williams_r(candles: Candles, period: int = 14) -> Series:
    """Williams %R, from 0 (closing at the period's high) to -100 (at its low)."""
    highs, lows, closes = candles.highs, candles.lows, candles.closes
    out: Series = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        high, low = max(highs[i - period + 1:i + 1]), min(lows[i - period + 1:i + 1])
        out[i] = -50.0 if high == low else (high - closes[i]) / (high - low) * -100
    return out


def cci(candles: Candles, period: int = 20) -> Series:
    """Commodity channel index."""
    typical = [(h + l + c) / 3 for h, l, c in zip(candles.highs, candles.lows, candles.closes)]
    averages = sma(typical, period)
    out: Series = [None] * len(typical)
    for i in range(period - 1, len(typical)):
        deviation = sum(abs(value - averages[i]) for value in typical[i - period + 1:i + 1]) / period
        out[i] = 0.0 if deviation == 0 else (typical[i] - averages[i]) / (0.015 * deviation)
    return out


def dmi(candles: Candles, period: int = 14) -> Tuple[Series, Series, Series]:
    """
    Returns:
        Tuple[Series, Series, Series]: The directional indicators +DI and -DI,
            and the average directional index (ADX), which measures how
            strong the trend is, whichever its direction.
    """
    highs, lows, closes = candles.highs, candles.lows, candles.closes
    count = len(closes)
    plus_move: Series = [None] * count
    minus_move: Series = [None] * count
    ranges: Series = [None] * count
    for i in range(1, count):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_move[i] = up if up > down and up > 0 else 0.0
        minus_move[i] = down if down > up and down > 0 else 0.0
        ranges[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    plus_smooth, minus_smooth, range_smooth = rma(plus_move, period), rma(minus_move, period), rma(ranges, period)
    plus_di: Series = [None] * count
    minus_di: Series = [None] * count
    dx: Series = [None] * count
    for i in range(count):
        if range_smooth[i] is None:
            continue
        plus_di[i] = 100 * plus_smooth[i] / range_smooth[i] if range_smooth[i] else 0.0
        minus_di[i] = 100 * minus_smooth[i] / range_smooth[i] if range_smooth[i] else 0.0
        total = plus_di[i] + minus_di[i]
        dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / total if total else 0.0
    return plus_di, minus_di, rma(dx, period)


def awesome_oscillator(candles: Candles) -> Series:
    """Awesome oscillator: the 5-day average of each candle's midpoint less the 34-day one."""
    midpoints = [(h + l) / 2 for h, l in zip(candles.highs, candles.lows)]
    return [fast - slow if fast is not None and slow is not None else None
            for fast, slow in zip(sma(midpoints, 5), sma(midpoints, 34))]


def ultimate_oscillator(candles: Candles, periods: Tuple[int, int, int] = (7, 14, 28)) -> Series:
    """Ultimate oscillator: buying pressure over three periods, weighted toward the shortest."""
    highs, lows, closes = candles.highs, candles.lows, candles.closes
    pressure = [0.0] + [closes[i] - min(lows[i], closes[i - 1]) for i in range(1, len(closes))]
    ranges = [0.0] + [max(highs[i], closes[i - 1]) - min(lows[i], closes[i - 1]) for i in range(1, len(closes))]
    out: Series = [None] * len(closes)
    for i in range(max(periods), len(closes)):
        averages = []
        for period in periods:
            total = sum(ranges[i - period + 1:i + 1])
            averages.append(sum(pressure[i - period + 1:i + 1]) / total if total else 0.0)
        out[i] = 100 * (4 * averages[0] + 2 * averages[1] + averages[2]) / 7
    return out


def bull_bear_power(candles: Candles, period: int = 13) -> Series:
    """Elder's bull power plus bear power: how far the high and low reach past the 13-day EMA."""
    average = ema(candles.closes, period)
    return [high + low - 2 * mean if mean is not None else None
            for high, low, mean in zip(candles.highs, candles.lows, average)]


def money_flow_index(candles: Candles, period: int = 14) -> Series:
    """Money flow index: a volume-weighted RSI of the typical price."""
    typical = [(h + l + c) / 3 for h, l, c in zip(candles.highs, candles.lows, candles.closes)]
    flows = [price * volume for price, volume in zip(typical, candles.volumes)]
    out: Series = [None] * len(typical)
    for i in range(period, len(typical)):
        inflow = sum(flows[j] for j in range(i - period + 1, i + 1) if typical[j] > typical[j - 1])
        outflow = sum(flows[j] for j in range(i - period + 1, i + 1) if typical[j] < typical[j - 1])
        out[i] = 50.0 if inflow == outflow == 0 else 100.0 if outflow == 0 else 100 - 100 / (1 + inflow / outflow)
    return out


def ichimoku(candles: Candles) -> Tuple[Series, Series, Series, Series, Series]:
    """
    Returns:
        Tuple[Series, Series, Series, Series, Series]: The conversion line
            (tenkan-sen) and base line (kijun-sen); the leading spans A and B
            (senkou span), the cloud's edges, shifted ICHIMOKU_SHIFT candles
            ahead so they run that far past the last candle; and the lagging
            span (chikou span), each close shifted ICHIMOKU_SHIFT candles back.
    """
    conversion_period, base_period, span_b_period = ICHIMOKU_PERIODS
    tenkan = _midpoints(candles, conversion_period)
    kijun = _midpoints(candles, base_period)
    span_a = [(t + k) / 2 if t is not None and k is not None else None for t, k in zip(tenkan, kijun)]
    span_b = _midpoints(candles, span_b_period)
    shift: Series = [None] * ICHIMOKU_SHIFT
    chikou = list(candles.closes[ICHIMOKU_SHIFT:]) + shift
    return tenkan, kijun, shift + span_a, shift + span_b, chikou


def _midpoints(candles: Candles, period: int) -> Series:
    """The middle of the highest high and the lowest low of each `period` candles."""
    highs, lows = candles.highs, candles.lows
    out: Series = [None] * len(highs)
    for i in range(period - 1, len(highs)):
        out[i] = (max(highs[i - period + 1:i + 1]) + min(lows[i - period + 1:i + 1])) / 2
    return out


def _sma_of(values: Series, period: int) -> Series:
    """Simple moving average of a series that starts with gaps."""
    start = next((i for i, value in enumerate(values) if value is not None), len(values))
    return [None] * start + sma(values[start:], period)


def fibonacci(candles: Candles, lookback: int = FIBONACCI_LOOKBACK) -> Optional[Fibonacci]:
    """
    Finds the swing between the highest high and the lowest low of the last
    `lookback` candles, and its retracement levels: a fall's measured up
    from its low, a rise's down from its high.
    """
    first = max(0, len(candles) - lookback)
    top = max(range(first, len(candles)), key=lambda i: candles.highs[i])
    bottom = min(range(first, len(candles)), key=lambda i: candles.lows[i])
    high, low = candles.highs[top], candles.lows[bottom]
    if high <= low:
        return None
    if bottom > top:  # a fall
        return Fibonacci(top, bottom, [(ratio, low + ratio * (high - low)) for ratio in FIBONACCI_RATIOS])
    return Fibonacci(bottom, top, [(ratio, high - ratio * (high - low)) for ratio in FIBONACCI_RATIOS])


def candle_pattern(candles: Candles) -> Optional[Tuple[str, int]]:
    """
    Recognizes the classic candlestick pattern ending at the latest candle,
    preferring the three-candle patterns, then the two-candle ones.

    Returns:
        Optional[Tuple[str, int]]: The pattern's Vietnamese name and what it
            signals: 1 a turn up or strong buying, -1 a turn down or strong
            selling, 0 indecision; None when no pattern formed.
    """
    count = len(candles)
    if count < 12:
        return None
    o, h, l, c = candles.opens, candles.highs, candles.lows, candles.closes
    body = [abs(close - open_) for open_, close in zip(o, c)]
    usual_body = sum(body[-11:-1]) / 10 or 1e-9

    def rising(i):
        return c[i] > o[i]

    def falling(i):
        return c[i] < o[i]

    def long_body(i):
        return body[i] >= usual_body

    def small_body(i):
        return body[i] <= usual_body * 0.5

    def trend_before(i):
        """1 if the price rose over the five sessions before candle i, -1 if it fell, else 0."""
        change = (c[i - 1] - c[i - 6]) / c[i - 6] if c[i - 6] else 0
        return 1 if change > 0.01 else -1 if change < -0.01 else 0

    first, middle, last = count - 3, count - 2, count - 1
    if (trend_before(first) < 0 and falling(first) and long_body(first) and small_body(middle)
            and max(o[middle], c[middle]) <= c[first] and rising(last) and c[last] > (o[first] + c[first]) / 2):
        return 'Sao mai', 1
    if (trend_before(first) > 0 and rising(first) and long_body(first) and small_body(middle)
            and min(o[middle], c[middle]) >= c[first] and falling(last) and c[last] < (o[first] + c[first]) / 2):
        return 'Sao hôm', -1
    three = (first, middle, last)
    if (all(rising(i) and body[i] >= usual_body * 0.6 and h[i] - c[i] <= body[i] * 0.5 for i in three)
            and c[first] < c[middle] < c[last] and o[first] <= o[middle] <= c[first] and o[middle] <= o[last] <= c[middle]):
        return 'Ba chàng lính trắng', 1
    if (all(falling(i) and body[i] >= usual_body * 0.6 and c[i] - l[i] <= body[i] * 0.5 for i in three)
            and c[first] > c[middle] > c[last] and c[first] <= o[middle] <= o[first] and c[middle] <= o[last] <= o[middle]):
        return 'Ba con quạ đen', -1

    before = middle
    if trend_before(before) < 0 and falling(before) and rising(last) and o[last] <= c[before] \
            and c[last] >= o[before] and body[last] > body[before]:
        return 'Nhấn chìm tăng', 1
    if trend_before(before) > 0 and rising(before) and falling(last) and o[last] >= c[before] \
            and c[last] <= o[before] and body[last] > body[before]:
        return 'Nhấn chìm giảm', -1
    if trend_before(before) < 0 and falling(before) and long_body(before) and rising(last) \
            and o[last] < c[before] and (o[before] + c[before]) / 2 < c[last] < o[before]:
        return 'Xuyên thủng', 1
    if trend_before(before) > 0 and rising(before) and long_body(before) and falling(last) \
            and o[last] > c[before] and o[before] < c[last] < (o[before] + c[before]) / 2:
        return 'Mây đen che phủ', -1
    if trend_before(before) < 0 and falling(before) and long_body(before) and rising(last) and small_body(last) \
            and c[before] <= o[last] and c[last] <= o[before]:
        return 'Harami tăng', 1
    if trend_before(before) > 0 and rising(before) and long_body(before) and falling(last) and small_body(last) \
            and o[before] <= c[last] and o[last] <= c[before]:
        return 'Harami giảm', -1

    span = h[last] - l[last]
    if span <= 0:
        return None
    upper, lower = h[last] - max(o[last], c[last]), min(o[last], c[last]) - l[last]
    if body[last] <= span * 0.1:
        return 'Doji', 0
    if body[last] >= span * 0.9 and body[last] >= usual_body * 1.3:
        return ('Marubozu tăng', 1) if rising(last) else ('Marubozu giảm', -1)
    trend = trend_before(last)
    if lower >= body[last] * 2 and upper <= span * 0.15 and trend:
        return ('Búa', 1) if trend < 0 else ('Người treo cổ', -1)
    if upper >= body[last] * 2 and lower <= span * 0.15 and trend:
        return ('Búa ngược', 1) if trend < 0 else ('Sao băng', -1)
    return None


def support_resistance(candles: Candles, price: float, window: int = 5, lookback: int = 120,
                       count: int = 2) -> Tuple[List[float], List[float]]:
    """
    Finds price levels where recent swings turned: highs and lows that are
    the extreme of the `window` candles on either side. Turns within 1.5% of
    each other count as one level, the middle one of them, so every level
    is a price that actually traded.

    Returns:
        Tuple[List[float], List[float]]: Up to `count` support levels below
            the price and resistance levels above it, nearest first.
    """
    highs, lows = candles.highs, candles.lows
    turns = []
    for i in range(max(window, len(highs) - lookback), len(highs) - window):
        if highs[i] == max(highs[i - window:i + window + 1]):
            turns.append(highs[i])
        if lows[i] == min(lows[i - window:i + window + 1]):
            turns.append(lows[i])

    levels: List[List[float]] = []
    for turn in sorted(turns):
        if levels and turn <= levels[-1][-1] * 1.015:
            levels[-1].append(turn)
        else:
            levels.append([turn])
    merged = [level[len(level) // 2] for level in levels]
    supports = sorted((level for level in merged if level < price * 0.995), reverse=True)[:count]
    resistances = sorted(level for level in merged if level > price * 1.005)[:count]
    return supports, resistances


def analyze(candles: Candles, format_price: Callable[[float], str],
            foreign: Optional[Tuple[float, float, float, float, float]] = None,
            session_open: bool = False) -> Analysis:
    """
    Reads daily candles the way a technical analyst would: trend, the
    Ichimoku cloud, trend strength, momentum, volatility, volume, candlestick
    patterns, support and resistance, Fibonacci retracements, performance,
    and an overall rating from the moving average and oscillator signals,
    built like TradingView's technical rating.

    Args:
        candles (Candles): Daily candles, oldest first, the last one current.
        format_price (Callable[[float], str]): Formats a price for the notes.
        foreign (Optional[Tuple[float, float, float, float, float]]): For
            Vietnamese stocks, the shares foreign investors bought and sold
            today, the shares they may still buy (the foreign room), and
            what they paid and got for the shares (VND, 0 if unknown).
        session_open (bool): Whether the last candle is still forming.

    Returns:
        Analysis: The rating, notes, and chart series.
    """
    closes = candles.closes
    price = closes[-1]
    sma20, sma50, sma200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    _, upper, lower = bollinger(closes)
    rsi14 = rsi(closes)
    macd_line, signal_line, histogram = macd(closes)
    k, d = stochastic(candles)
    rsi_k, rsi_d = stochastic_rsi(closes)
    cci20 = cci(candles)
    plus_di, minus_di, adx = dmi(candles)
    awesome = awesome_oscillator(candles)
    williams = williams_r(candles)
    power = bull_bear_power(candles)
    ultimate = ultimate_oscillator(candles)
    mfi14 = money_flow_index(candles)
    momentum = [None] * 10 + [closes[i] - closes[i - 10] for i in range(10, len(closes))]
    tenkan, kijun, span_a, span_b, chikou = ichimoku(candles)
    swing = fibonacci(candles)

    def number(value: Optional[float]) -> str:
        return '—' if value is None else f"{value:.2f}"

    def in_price(value: Optional[float]) -> str:
        return '—' if value is None else format_price(value)

    def against_price(average: Series) -> Optional[int]:
        return None if average[-1] is None else _vote(price > average[-1], price < average[-1])

    # The indicators behind the rating, in TradingView's order
    average_table = [(f"{name} ({period})", in_price(series[-1]), against_price(series))
                     for period in _RATING_AVERAGES
                     for name, series in (('EMA', ema(closes, period)), ('SMA', sma(closes, period)))]
    volume_weighted, hull_average = vwma(candles), hull(closes)
    average_table += [
        ('Ichimoku (9, 26, 52, 26)', in_price(kijun[-1]),
         _ichimoku_vote(price, tenkan, kijun, span_a, span_b, len(closes) - 1)),
        ('VWMA (20)', in_price(volume_weighted[-1]), against_price(volume_weighted)),
        ('Hull MA (9)', in_price(hull_average[-1]), against_price(hull_average)),
    ]
    uptrend = sma50[-1] is not None and price > sma50[-1]
    downtrend = sma50[-1] is not None and price < sma50[-1]
    oscillator_table = [
        ('RSI (14)', number(rsi14[-1]), _rising_vote(rsi14, lambda v: v < 30, lambda v: v > 70)),
        ('Stochastic %K/%D (14, 3, 3)', f"{number(k[-1])} / {number(d[-1])}", _stochastic_vote(k, d)),
        ('CCI (20)', number(cci20[-1]), _rising_vote(cci20, lambda v: v < -100, lambda v: v > 100)),
        ('ADX (14)', number(adx[-1]), _adx_vote(plus_di, minus_di, adx)),
        ('Awesome Oscillator', in_price(awesome[-1]), _awesome_vote(awesome)),
        ('Momentum (10)', in_price(momentum[-1]), _rising_vote(momentum, lambda v: True, lambda v: True)),
        ('MACD (12, 26)', in_price(macd_line[-1]),
         _vote(*_latest_pair(macd_line, signal_line, lambda m, s: (m > s, m < s)))),
        ('Stoch RSI %K/%D (3, 3, 14, 14)', f"{number(rsi_k[-1])} / {number(rsi_d[-1])}",
         _stochastic_rsi_vote(rsi_k, rsi_d, uptrend, downtrend)),
        ('Williams %R (14)', number(williams[-1]), _rising_vote(williams, lambda v: v < -80, lambda v: v > -20)),
        ('Bull Bear Power', in_price(power[-1]), _bull_bear_vote(power, uptrend, downtrend)),
        ('Ultimate Oscillator (7, 14, 28)', number(ultimate[-1]),
         None if ultimate[-1] is None else _vote(ultimate[-1] > 70, ultimate[-1] < 30)),
    ]
    rating, score = _rating([vote for _, _, vote in average_table if vote is not None],
                            [vote for _, _, vote in oscillator_table if vote is not None])

    notes = [(topic, text) for topic, text in (
        ('Xu hướng', _trend_note(price, sma20, sma50, sma200)),
        ('Ichimoku', _ichimoku_note(candles, tenkan, kijun, span_a, span_b, format_price)),
        ('Sức mạnh xu hướng', _strength_note(plus_di, minus_di, adx)),
        ('Động lượng', _momentum_note(rsi14, k, macd_line, signal_line, histogram)),
        ('Biến động', _volatility_note(candles, upper, lower, format_price)),
        ('Khối lượng', _volume_note(candles, mfi14)),
        ('Khối ngoại', _foreign_note(foreign, price) if foreign else None),
        ('Mẫu hình nến', _pattern_note(candles, session_open)),
        ('Ngưỡng giá', _levels_note(candles, format_price)),
        ('Fibonacci', _fibonacci_note(swing, price, format_price)),
        ('Hiệu suất', _performance_note(candles)),
    ) if text]
    return Analysis(
        rating=rating,
        score=score,
        average_signals=_tally([vote for _, _, vote in average_table if vote is not None]),
        oscillator_signals=_tally([vote for _, _, vote in oscillator_table if vote is not None]),
        notes=notes,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        band_upper=upper,
        band_lower=lower,
        rsi=rsi14,
        stochastic_k=k,
        stochastic_d=d,
        stochastic_rsi_k=rsi_k,
        stochastic_rsi_d=rsi_d,
        tenkan=tenkan,
        kijun=kijun,
        span_a=span_a,
        span_b=span_b,
        chikou=chikou,
        macd=macd_line,
        macd_signal=signal_line,
        macd_histogram=histogram,
        fibonacci=swing,
        oscillator_table=oscillator_table,
        average_table=average_table,
    )


def _vote(buy: bool, sell: bool) -> Optional[int]:
    """1 for a buy signal, -1 for sell, 0 for neutral; None without data."""
    if buy is None:
        return None
    return 1 if buy else -1 if sell else 0


def _latest_pair(a: Series, b: Series, compare) -> Tuple[Optional[bool], Optional[bool]]:
    """Applies compare to both series' latest values, or (None, None) without them."""
    if a[-1] is None or b[-1] is None:
        return None, None
    return compare(a[-1], b[-1])


def _rising_vote(series: Series, buy_zone, sell_zone) -> Optional[int]:
    """Buy when in the buy zone and rising, sell when in the sell zone and falling."""
    if len(series) < 2 or series[-1] is None or series[-2] is None:
        return None
    rising = series[-1] > series[-2]
    falling = series[-1] < series[-2]
    return _vote(buy_zone(series[-1]) and rising, sell_zone(series[-1]) and falling)


def _stochastic_vote(k: Series, d: Series) -> Optional[int]:
    """Buy when oversold with %K above %D, sell when overbought with %K below it."""
    if k[-1] is None or d[-1] is None:
        return None
    return _vote(k[-1] < 20 and k[-1] > d[-1], k[-1] > 80 and k[-1] < d[-1])


def _stochastic_rsi_vote(k: Series, d: Series, uptrend: bool, downtrend: bool) -> Optional[int]:
    """Buy when oversold in a downtrend with %K above %D, sell when overbought in an uptrend with %K below it."""
    if k[-1] is None or d[-1] is None:
        return None
    return _vote(downtrend and k[-1] < 20 and d[-1] < 20 and k[-1] > d[-1],
                 uptrend and k[-1] > 80 and d[-1] > 80 and k[-1] < d[-1])


def _adx_vote(plus_di: Series, minus_di: Series, adx: Series) -> Optional[int]:
    """With a strong (ADX above 20) and strengthening trend, the leading directional indicator's side."""
    if None in (plus_di[-1], minus_di[-1], adx[-1], adx[-2]):
        return None
    strengthening = adx[-1] > 20 and adx[-1] > adx[-2]
    return _vote(strengthening and plus_di[-1] > minus_di[-1], strengthening and plus_di[-1] < minus_di[-1])


def _awesome_vote(awesome: Series) -> Optional[int]:
    """Buy on a cross above zero or a saucer above it, sell on the mirror images."""
    if len(awesome) < 3 or None in awesome[-3:]:
        return None
    before, previous, latest = awesome[-3:]
    return _vote(previous < 0 <= latest or (latest > 0 and before > previous < latest),
                 previous > 0 >= latest or (latest < 0 and before < previous > latest))


def _bull_bear_vote(power: Series, uptrend: bool, downtrend: bool) -> Optional[int]:
    """Buy on a recovering dip in an uptrend, sell on a fading bounce in a downtrend."""
    if len(power) < 2 or power[-1] is None or power[-2] is None:
        return None
    return _vote(uptrend and power[-1] < 0 and power[-1] > power[-2],
                 downtrend and power[-1] > 0 and power[-1] < power[-2])


def _ichimoku_vote(price: float, tenkan: Series, kijun: Series, span_a: Series, span_b: Series,
                   last: int) -> Optional[int]:
    """Buy above the cloud with the conversion line above the base line, sell below it with the reverse."""
    if None in (span_a[last], span_b[last], tenkan[-1], kijun[-1]):
        return None
    return _vote(price > max(span_a[last], span_b[last]) and tenkan[-1] > kijun[-1],
                 price < min(span_a[last], span_b[last]) and tenkan[-1] < kijun[-1])


def _rating(average_votes: List[int], oscillator_votes: List[int]) -> Tuple[str, float]:
    """Averages the two groups' net votes into a rating and its score."""
    groups = [votes for votes in (average_votes, oscillator_votes) if votes]
    if not groups:
        return NEUTRAL, 0.0
    score = sum(sum(votes) / len(votes) for votes in groups) / len(groups)
    if score > 0.5:
        return STRONG_BUY, score
    if score > 0.1:
        return BUY, score
    if score >= -0.1:
        return NEUTRAL, score
    if score >= -0.5:
        return SELL, score
    return STRONG_SELL, score


def _tally(votes: List[int]) -> Tuple[int, int, int]:
    return votes.count(1), votes.count(0), votes.count(-1)


def _join(items: List[str]) -> str:
    """'MA20', 'MA20 và MA50', 'MA20, MA50 và MA200'."""
    if len(items) <= 2:
        return ' và '.join(items)
    return ', '.join(items[:-1]) + ' và ' + items[-1]


def _trend_note(price: float, sma20: Series, sma50: Series, sma200: Series) -> Optional[str]:
    averages = [(name, series[-1]) for name, series in (('MA20', sma20), ('MA50', sma50), ('MA200', sma200))
                if series[-1] is not None]
    if len(averages) < 2:
        return None
    s50, s200 = sma50[-1], sma200[-1]
    if s200 is None:
        kind = 'Xu hướng tăng' if price > s50 else 'Xu hướng giảm'
    elif price > s50 > s200:
        kind = 'Xu hướng tăng'
    elif price < s50 < s200:
        kind = 'Xu hướng giảm'
    elif price < s50 and price < s200:
        kind = 'Điều chỉnh'  # below both, while the 50-day still leads the 200-day
    elif price > s50 and price > s200:
        kind = 'Phục hồi'  # above both, while the 50-day still trails the 200-day
    else:
        kind = 'Đi ngang'  # between the two

    above = [name for name, average in averages if price > average]
    below = [name for name, average in averages if price < average]
    sides = []
    if above:
        sides.append(f"trên {_join(above)}")
    if below:
        sides.append(f"dưới {_join(below)}")
    text = f"{kind}: giá nằm {' nhưng '.join(sides)}." if sides else f"{kind}."

    # A crossing of the 50-day over the 200-day average in the last month or so
    for back in range(1, min(30, len(sma200) - 1)):
        now, before = -back, -back - 1
        if None in (sma50[now], sma200[now], sma50[before], sma200[before]):
            break
        if (sma50[now] > sma200[now]) != (sma50[before] > sma200[before]):
            golden = sma50[now] > sma200[now]
            text += (f" MA50 cắt {'lên' if golden else 'xuống'} MA200 {_days_ago(back - 1)} "
                     f"({'giao cắt vàng' if golden else 'giao cắt tử thần'}).")
            break
    return text


def _ichimoku_note(candles: Candles, tenkan: Series, kijun: Series, span_a: Series, span_b: Series,
                   format_price: Callable[[float], str]) -> Optional[str]:
    """The price against the cloud, the conversion and base lines, the cloud ahead, and the lagging span."""
    closes = candles.closes
    last, price = len(closes) - 1, closes[-1]
    if last < ICHIMOKU_SHIFT or None in (span_a[last], span_b[last], span_a[-1], span_b[-1], tenkan[-1], kijun[-1]):
        return None
    top, bottom = max(span_a[last], span_b[last]), min(span_a[last], span_b[last])
    if price > top:
        parts, signals = [f"Giá nằm trên mây, đỉnh mây {format_price(top)} là hỗ trợ"], [1]
    elif price < bottom:
        parts, signals = [f"Giá nằm dưới mây, đáy mây {format_price(bottom)} là kháng cự"], [-1]
    else:
        parts, signals = [f"Giá nằm trong mây ({format_price(bottom)} – {format_price(top)}), vùng giằng co"], [0]

    above = tenkan[-1] > kijun[-1]
    crossed = next((back - 1 for back in range(1, 6)
                    if None not in (tenkan[-back - 1], kijun[-back - 1])
                    and (tenkan[-back] > kijun[-back]) != (tenkan[-back - 1] > kijun[-back - 1])), None)
    if crossed is not None:
        parts.append(f"Tenkan cắt {'lên' if above else 'xuống'} Kijun {_days_ago(crossed)}")
    else:
        parts.append(f"Tenkan nằm {'trên' if above else 'dưới'} Kijun")
    signals.append(1 if above else -1 if tenkan[-1] < kijun[-1] else 0)

    green_ahead = span_a[-1] > span_b[-1]
    cloud = f"mây phía trước màu {'xanh' if green_ahead else 'đỏ'}"
    if (span_a[last] > span_b[last]) != green_ahead:
        cloud += ' (mây sắp đổi màu)'
    parts.append(cloud)
    signals.append(1 if green_ahead else -1)

    past = closes[last - ICHIMOKU_SHIFT]
    parts.append(f"Chikou nằm {'trên' if price > past else 'dưới' if price < past else 'ngang'} giá 26 phiên trước")
    signals.append(1 if price > past else -1 if price < past else 0)

    text = '; '.join(parts) + '.'
    if all(signal == 1 for signal in signals):
        text += ' Cả 4 tín hiệu đều tích cực: xu hướng tăng mạnh.'
    elif all(signal == -1 for signal in signals):
        text += ' Cả 4 tín hiệu đều tiêu cực: xu hướng giảm mạnh.'
    return text


def _strength_note(plus_di: Series, minus_di: Series, adx: Series) -> Optional[str]:
    if None in (adx[-1], adx[-2], plus_di[-1], minus_di[-1]):
        return None
    value = adx[-1]
    strength = ('xu hướng rất mạnh' if value >= 40 else 'xu hướng mạnh' if value >= 25
                else 'xu hướng đang hình thành' if value >= 20 else 'chưa có xu hướng rõ, giá đi ngang')
    leader = 'bên mua chiếm ưu thế' if plus_di[-1] > minus_di[-1] else 'bên bán chiếm ưu thế'
    return (f"ADX 14 ở mức {value:.0f} ({strength}) và đang {'tăng' if value > adx[-2] else 'giảm'}; "
            f"+DI {plus_di[-1]:.0f}, −DI {minus_di[-1]:.0f}: {leader}.")


def _days_ago(days: int) -> str:
    """0 -> 'trong phiên gần nhất', 3 -> 'cách đây 3 phiên'."""
    if days == 0:
        return 'trong phiên gần nhất'
    return f"cách đây {days} phiên"


def _momentum_note(rsi14: Series, k: Series, macd_line: Series, signal_line: Series,
                   histogram: Series) -> Optional[str]:
    parts = []
    if rsi14[-1] is not None:
        value = rsi14[-1]
        zone = ('quá mua' if value >= 70 else 'quá bán' if value <= 30
                else 'nghiêng về tăng' if value >= 50 else 'nghiêng về giảm')
        parts.append(f"RSI 14 ở mức {value:.0f} ({zone}).")
    if k[-1] is not None:
        zone = ' (quá mua)' if k[-1] >= 80 else ' (quá bán)' if k[-1] <= 20 else ''
        parts.append(f"Stochastic ở mức {k[-1]:.0f}{zone}.")
    if None not in (macd_line[-1], signal_line[-1], histogram[-1], histogram[-2]):
        above = macd_line[-1] > signal_line[-1]
        crossed = next((back - 1 for back in range(1, 4)
                        if histogram[-back - 1] is not None and (histogram[-back] > 0) != (histogram[-back - 1] > 0)),
                       None)
        if crossed is not None:
            parts.append(f"MACD cắt {'lên' if above else 'xuống'} đường tín hiệu {_days_ago(crossed)}.")
        else:
            # The histogram growing away from zero means the move is gathering pace
            strengthening = (histogram[-1] > histogram[-2]) == above
            parts.append(f"MACD nằm {'trên' if above else 'dưới'} đường tín hiệu, "
                         f"đà {'tăng' if above else 'giảm'} đang {'mạnh lên' if strengthening else 'yếu đi'}.")
    return ' '.join(parts) or None


def _volatility_note(candles: Candles, upper: Series, lower: Series,
                     format_price: Callable[[float], str]) -> Optional[str]:
    average_range = atr(candles)[-1]
    if average_range is None or upper[-1] is None:
        return None
    price = candles.closes[-1]
    text = f"Biên độ trung bình {format_price(average_range)} ({average_range / price * 100:.1f}%) mỗi phiên; "
    position = (price - lower[-1]) / (upper[-1] - lower[-1]) if upper[-1] > lower[-1] else 0.5
    if position > 1:
        text += "giá vượt lên trên dải trên Bollinger."
    elif position >= 0.8:
        text += "giá đang sát dải trên Bollinger."
    elif position < 0:
        text += "giá thủng xuống dưới dải dưới Bollinger."
    elif position <= 0.2:
        text += "giá đang sát dải dưới Bollinger."
    else:
        text += "giá nằm trong dải Bollinger."

    widths = [(u - l) / ((u + l) / 2) for u, l in zip(upper[-120:], lower[-120:]) if u is not None and l is not None]
    if len(widths) >= 60 and widths[-1] <= min(widths) * 1.05:
        text += " Dải Bollinger đang hẹp nhất trong nhiều tháng, thường báo trước một nhịp biến động mạnh."
    return text


def _volume_note(candles: Candles, mfi14: Series) -> Optional[str]:
    volumes, closes = candles.volumes, candles.closes
    if len(volumes) < 21:
        return None
    average = sum(volumes[-21:-1]) / 20
    if average <= 0:
        return None
    ratio = volumes[-1] / average
    text = f"Hôm nay {compact(volumes[-1])}, bằng {ratio:.1f} lần trung bình 20 phiên"
    if ratio >= 1.5 and closes[-1] != closes[-2]:
        text += ', lực mua mạnh' if closes[-1] > closes[-2] else ', lực bán mạnh'
    text += '.'
    if mfi14[-1] is not None:
        value = mfi14[-1]
        flow = ('quá mua' if value >= 80 else 'quá bán' if value <= 20
                else 'dòng tiền vào nhiều hơn ra' if value >= 50 else 'dòng tiền ra nhiều hơn vào')
        text += f" MFI 14 ở mức {value:.0f} ({flow})."
    return text


def _foreign_note(foreign: Tuple[float, float, float, float, float], price: float) -> Optional[str]:
    bought, sold, room, bought_value, sold_value = foreign
    net = bought - sold
    if bought or sold:
        text = f"Hôm nay mua {compact(bought)}, bán {compact(sold)} cổ phiếu"
        if net:
            # What the board reports they paid and got, else the shares at today's price
            net_value = abs(bought_value - sold_value) if bought_value or sold_value else abs(net) * price
            text += f": {'mua' if net > 0 else 'bán'} ròng {compact(abs(net))} cổ phiếu ({compact(net_value)} đồng)."
        else:
            text += ': mua bán cân bằng.'
    else:
        text = 'Hôm nay khối ngoại chưa giao dịch.'
    if room > 0:
        text += f" Room còn {compact(room)} cổ phiếu."
    elif bought or sold:
        text += ' Đã hết room.'
    return text


def _pattern_note(candles: Candles, session_open: bool) -> str:
    found = candle_pattern(candles)
    if found is None:
        return 'Không có mẫu hình nến đáng chú ý ở các phiên gần nhất.'
    name, _ = found
    text = f"{name}: {_PATTERN_MEANINGS[name]}."
    if session_open:
        text += ' Nến hôm nay chưa đóng cửa nên mẫu hình có thể còn thay đổi.'
    return text


def _levels_note(candles: Candles, format_price: Callable[[float], str]) -> Optional[str]:
    price = candles.closes[-1]
    supports, resistances = support_resistance(candles, price)
    levels = []
    if supports:
        levels.append('hỗ trợ ' + ', '.join(format_price(level) for level in supports))
    if resistances:
        levels.append('kháng cự ' + ', '.join(format_price(level) for level in resistances))
    sentences = []
    if levels:
        text = '; '.join(levels) + '.'
        sentences.append(text[0].upper() + text[1:])

    start = candles.times[-1] - 365 * _DAY_S
    year = [i for i, t in enumerate(candles.times) if t >= start]
    high = max(candles.highs[i] for i in year)
    low = min(candles.lows[i] for i in year)
    if high > low > 0:
        below_high = (1 - price / high) * 100
        above_low = (price / low - 1) * 100
        sentences.append(
            (f"Giá đang ở đỉnh 52 tuần {format_price(high)}" if below_high < 0.5 else
             f"Đỉnh 52 tuần {format_price(high)}, giá thấp hơn {below_high:.0f}%") + '; ' +
            (f"đáy 52 tuần {format_price(low)}, giá đang ở đáy." if above_low < 0.5 else
             f"đáy 52 tuần {format_price(low)}, giá cao hơn {above_low:.0f}%.")
        )
    return ' '.join(sentences) or None


def _fibonacci_note(swing: Optional[Fibonacci], price: float, format_price: Callable[[float], str]) -> Optional[str]:
    if swing is None:
        return None
    end_price, start_price = swing.levels[0][1], swing.levels[-1][1]
    fell = start_price > end_price
    taken_back = (price - end_price) / (start_price - end_price) * 100
    text = (f"{'Nhịp giảm' if fell else 'Nhịp tăng'} {format_price(start_price)} → {format_price(end_price)}: "
            f"giá đã {'hồi phục' if fell else 'điều chỉnh'} {taken_back:.0f}%.")
    # A fall's retracements above the price resist a recovery; a rise's below it support a pullback
    inner = [(ratio, level) for ratio, level in swing.levels if 0 < ratio < 1]
    ahead = [(ratio, level) for ratio, level in inner if (level > price if fell else level < price)][:2]
    if ahead:
        text += (f" {'Kháng cự' if fell else 'Hỗ trợ'} Fibonacci gần: "
                 + ', '.join(f"{ratio * 100:g}% ({format_price(level)})" for ratio, level in ahead) + '.')
    return text


def _performance_note(candles: Candles) -> Optional[str]:
    price, times = candles.closes[-1], candles.times
    changes = []
    for label, days in _PERFORMANCE_PERIODS:
        cutoff = times[-1] - days * _DAY_S
        if times[0] > cutoff:
            break
        base = next(candles.closes[i] for i in range(len(times) - 1, -1, -1) if times[i] <= cutoff)
        changes.append(f"{label} {(price - base) / base * 100:+.1f}%")
    return '   '.join(changes) or None


def compact(value: float) -> str:
    """4,320,000 -> '4.3 triệu'."""
    for size, unit in ((1e9, 'tỷ'), (1e6, 'triệu'), (1e3, 'nghìn')):
        if value >= size:
            return f"{value / size:.1f} {unit}"
    return f"{value:.0f}"
