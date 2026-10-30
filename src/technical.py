from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from src.price_history import Candles

# An indicator's value per candle; None until it has enough candles behind it
Series = List[Optional[float]]

# The moving averages the rating compares the price with, simple and
# exponential, as most trading platforms' technical ratings do
_RATING_AVERAGES = (10, 20, 50, 100, 200)

# Price changes listed under Performance, in calendar days
_PERFORMANCE_PERIODS = (('1W', 7), ('1M', 30), ('3M', 91), ('6M', 182), ('1Y', 365))

_DAY_S = 24 * 60 * 60


@dataclass
class Analysis:
    """A technical read of one symbol's daily candles."""

    # 'Strong buy', 'Buy', 'Neutral', 'Sell', or 'Strong sell', from a score
    # between -1 (every signal says sell) and 1 (every signal says buy)
    rating: str
    score: float
    # (buy, neutral, sell) signal counts from the moving averages and the oscillators
    average_signals: Tuple[int, int, int]
    oscillator_signals: Tuple[int, int, int]
    # (topic, finding) pairs, e.g. ('Trend', 'Uptrend: ...')
    notes: List[Tuple[str, str]]
    # Chart series, aligned with the candles
    sma20: Series
    sma50: Series
    sma200: Series
    band_upper: Series
    band_lower: Series
    rsi: Series


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
    out: Series = [None] * len(closes)
    if len(ranges) < period:
        return out
    current = sum(ranges[:period]) / period
    out[period - 1] = current
    for i in range(period, len(ranges)):
        current = (current * (period - 1) + ranges[i]) / period
        out[i] = current
    return out


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


def cci(candles: Candles, period: int = 20) -> Series:
    """Commodity channel index."""
    typical = [(h + l + c) / 3 for h, l, c in zip(candles.highs, candles.lows, candles.closes)]
    averages = sma(typical, period)
    out: Series = [None] * len(typical)
    for i in range(period - 1, len(typical)):
        deviation = sum(abs(value - averages[i]) for value in typical[i - period + 1:i + 1]) / period
        out[i] = 0.0 if deviation == 0 else (typical[i] - averages[i]) / (0.015 * deviation)
    return out


def _sma_of(values: Series, period: int) -> Series:
    """Simple moving average of a series that starts with gaps."""
    start = next((i for i, value in enumerate(values) if value is not None), len(values))
    return [None] * start + sma(values[start:], period)


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


def analyze(candles: Candles, format_price: Callable[[float], str]) -> Analysis:
    """
    Reads daily candles the way a technical analyst would: trend, momentum,
    volatility, volume, support and resistance, performance, and an overall
    rating from the moving average and oscillator signals.

    Args:
        candles (Candles): Daily candles, oldest first, the last one current.
        format_price (Callable[[float], str]): Formats a price for the notes.

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
    cci20 = cci(candles)
    momentum = [None] * 10 + [closes[i] - closes[i - 10] for i in range(10, len(closes))]

    average_votes = [
        _vote(price > average[-1], price < average[-1])
        for period in _RATING_AVERAGES
        for average in (sma(closes, period), ema(closes, period))
        if average[-1] is not None
    ]
    oscillator_votes = [vote for vote in (
        _rising_vote(rsi14, lambda v: v < 30, lambda v: v > 70),
        _stochastic_vote(k, d),
        _rising_vote(cci20, lambda v: v < -100, lambda v: v > 100),
        _vote(*_latest_pair(macd_line, signal_line, lambda m, s: (m > s, m < s))),
        _rising_vote(momentum, lambda v: True, lambda v: True),
    ) if vote is not None]
    rating, score = _rating(average_votes, oscillator_votes)

    notes = [(topic, text) for topic, text in (
        ('Trend', _trend_note(price, sma20, sma50, sma200)),
        ('Momentum', _momentum_note(rsi14, macd_line, signal_line, histogram)),
        ('Volatility', _volatility_note(candles, upper, lower, format_price)),
        ('Volume', _volume_note(candles)),
        ('Levels', _levels_note(candles, format_price)),
        ('Performance', _performance_note(candles)),
    ) if text]
    return Analysis(
        rating=rating,
        score=score,
        average_signals=_tally(average_votes),
        oscillator_signals=_tally(oscillator_votes),
        notes=notes,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        band_upper=upper,
        band_lower=lower,
        rsi=rsi14,
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


def _rating(average_votes: List[int], oscillator_votes: List[int]) -> Tuple[str, float]:
    """Averages the two groups' net votes into a rating and its score."""
    groups = [votes for votes in (average_votes, oscillator_votes) if votes]
    if not groups:
        return 'Neutral', 0.0
    score = sum(sum(votes) / len(votes) for votes in groups) / len(groups)
    if score > 0.5:
        return 'Strong buy', score
    if score > 0.1:
        return 'Buy', score
    if score >= -0.1:
        return 'Neutral', score
    if score >= -0.5:
        return 'Sell', score
    return 'Strong sell', score


def _tally(votes: List[int]) -> Tuple[int, int, int]:
    return votes.count(1), votes.count(0), votes.count(-1)


def _join(items: List[str]) -> str:
    """'20', '20 and 50', '20, 50, and 200'."""
    if len(items) <= 2:
        return ' and '.join(items)
    return ', '.join(items[:-1]) + ', and ' + items[-1]


def _trend_note(price: float, sma20: Series, sma50: Series, sma200: Series) -> Optional[str]:
    averages = [(days, series[-1]) for days, series in (('20', sma20), ('50', sma50), ('200', sma200))
                if series[-1] is not None]
    if len(averages) < 2:
        return None
    s50, s200 = sma50[-1], sma200[-1]
    if s200 is None:
        kind = 'Uptrend' if price > s50 else 'Downtrend'
    elif price > s50 > s200:
        kind = 'Uptrend'
    elif price < s50 < s200:
        kind = 'Downtrend'
    elif price < s50 and price < s200:
        kind = 'Correction'  # below both, while the 50-day still leads the 200-day
    elif price > s50 and price > s200:
        kind = 'Recovery'  # above both, while the 50-day still trails the 200-day
    else:
        kind = 'Sideways'  # between the two

    above = [days for days, average in averages if price > average]
    below = [days for days, average in averages if price < average]
    sides = []
    if above:
        sides.append(f"above its {_join(above)}-day average{'s' if len(above) > 1 else ''}")
    if below:
        sides.append(f"below its {_join(below)}-day average{'s' if len(below) > 1 else ''}")
    text = f"{kind}: the price is {' and '.join(sides)}."

    # A crossing of the 50-day over the 200-day average in the last month or so
    for back in range(1, min(30, len(sma200) - 1)):
        now, before = -back, -back - 1
        if None in (sma50[now], sma200[now], sma50[before], sma200[before]):
            break
        if (sma50[now] > sma200[now]) != (sma50[before] > sma200[before]):
            golden = sma50[now] > sma200[now]
            text += (f" The 50-day average crossed {'above' if golden else 'below'} the 200-day "
                     f"{_days_ago(back - 1)} ({'golden' if golden else 'death'} cross).")
            break
    return text


def _days_ago(days: int) -> str:
    """0 -> 'in the latest session', 1 -> '1 trading day ago'."""
    if days == 0:
        return 'in the latest session'
    return f"{days} trading day{'s' if days > 1 else ''} ago"


def _momentum_note(rsi14: Series, macd_line: Series, signal_line: Series, histogram: Series) -> Optional[str]:
    parts = []
    if rsi14[-1] is not None:
        value = rsi14[-1]
        zone = ('overbought' if value >= 70 else 'oversold' if value <= 30
                else 'leaning bullish' if value >= 50 else 'leaning bearish')
        parts.append(f"RSI 14 at {value:.0f} ({zone}).")
    if None not in (macd_line[-1], signal_line[-1], histogram[-1], histogram[-2]):
        above = macd_line[-1] > signal_line[-1]
        crossed = next((back - 1 for back in range(1, 4)
                        if histogram[-back - 1] is not None and (histogram[-back] > 0) != (histogram[-back - 1] > 0)),
                       None)
        side = 'above' if above else 'below'
        if crossed is not None:
            parts.append(f"MACD crossed {side} its signal line {_days_ago(crossed)}.")
        else:
            trend = 'rising' if histogram[-1] > histogram[-2] else 'falling'
            parts.append(f"MACD is {side} its signal line ({'bullish' if above else 'bearish'}), "
                         f"its histogram {trend}.")
    return ' '.join(parts) or None


def _volatility_note(candles: Candles, upper: Series, lower: Series,
                     format_price: Callable[[float], str]) -> Optional[str]:
    average_range = atr(candles)[-1]
    if average_range is None or upper[-1] is None:
        return None
    price = candles.closes[-1]
    text = f"Moves {format_price(average_range)} ({average_range / price * 100:.1f}%) a day on average; "
    position = (price - lower[-1]) / (upper[-1] - lower[-1]) if upper[-1] > lower[-1] else 0.5
    if position >= 0.8:
        text += "the price is near its upper Bollinger band."
    elif position <= 0.2:
        text += "the price is near its lower Bollinger band."
    else:
        text += "the price is inside its Bollinger bands."

    widths = [(u - l) / ((u + l) / 2) for u, l in zip(upper[-120:], lower[-120:]) if u is not None and l is not None]
    if len(widths) >= 60 and widths[-1] <= min(widths) * 1.05:
        text += " The bands are their narrowest in months, which often comes before a big move."
    return text


def _volume_note(candles: Candles) -> Optional[str]:
    volumes, closes = candles.volumes, candles.closes
    if len(volumes) < 21:
        return None
    average = sum(volumes[-21:-1]) / 20
    if average <= 0:
        return None
    ratio = volumes[-1] / average
    text = f"{_compact(volumes[-1])} today, {ratio:.1f}× the 20-day average"
    if ratio >= 1.5 and closes[-1] != closes[-2]:
        text += ': heavy buying' if closes[-1] > closes[-2] else ': heavy selling'
    return text + '.'


def _levels_note(candles: Candles, format_price: Callable[[float], str]) -> Optional[str]:
    price = candles.closes[-1]
    supports, resistances = support_resistance(candles, price)
    parts = []
    if supports:
        parts.append('Support ' + ', '.join(format_price(level) for level in supports))
    if resistances:
        parts.append('resistance ' + ', '.join(format_price(level) for level in resistances))

    start = candles.times[-1] - 365 * _DAY_S
    year = [i for i, t in enumerate(candles.times) if t >= start]
    high = max(candles.highs[i] for i in year)
    low = min(candles.lows[i] for i in year)
    if high > low:
        parts.append(f"52-week range {format_price(low)} to {format_price(high)}, "
                     f"the price {(price - low) / (high - low) * 100:.0f}% of the way up")
    if not parts:
        return None
    text = '; '.join(parts) + '.'
    return text[0].upper() + text[1:]


def _performance_note(candles: Candles) -> Optional[str]:
    price, times = candles.closes[-1], candles.times
    changes = []
    for label, days in _PERFORMANCE_PERIODS:
        cutoff = times[-1] - days * _DAY_S
        if times[0] > cutoff:
            break
        base = next(candles.closes[i] for i in range(len(times) - 1, -1, -1) if times[i] <= cutoff)
        changes.append(f"{label} {(price - base) / base * 100:+.1f}%")
    return '  '.join(changes) or None


def _compact(value: float) -> str:
    """4,320,000 -> '4.3M'."""
    for size, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if value >= size:
            return f"{value / size:.1f}{suffix}"
    return f"{value:.0f}"
