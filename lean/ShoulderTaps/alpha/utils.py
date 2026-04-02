"""Shared utilities for Shoulder Tap Alpha models.

Ported from services/alert-engine/src/evaluators/utils.py with one modification:
is_in_no_trade_window uses Eastern->Pacific offset instead of pytz.
"""

from datetime import time as dt_time
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Swing point detection (Tap 1, 5)
# ---------------------------------------------------------------------------

def find_swing_highs(values: list[float], order: int = 3) -> list[tuple[int, float]]:
    """Find swing high points. A swing high at index i means values[i] is the max
    within values[i-order : i+order+1]. Returns [(index, value), ...]."""
    swings = []
    for i in range(order, len(values) - order):
        window = values[i - order: i + order + 1]
        if values[i] == max(window) and window.count(values[i]) == 1:
            swings.append((i, values[i]))
    return swings


def find_swing_lows(values: list[float], order: int = 3) -> list[tuple[int, float]]:
    """Find swing low points. Mirror of find_swing_highs."""
    swings = []
    for i in range(order, len(values) - order):
        window = values[i - order: i + order + 1]
        if values[i] == min(window) and window.count(values[i]) == 1:
            swings.append((i, values[i]))
    return swings


# ---------------------------------------------------------------------------
# Divergence detection (Tap 1, 5)
# ---------------------------------------------------------------------------

def _nearest_swing(swings, target_idx, tolerance=3):
    """Find the swing point nearest to target_idx within tolerance."""
    best = None
    best_dist = tolerance + 1
    for idx, val in swings:
        dist = abs(idx - target_idx)
        if dist <= tolerance and dist < best_dist:
            best = (idx, val)
            best_dist = dist
    return best


def detect_bullish_divergence(
    price_lows: list[tuple[int, float]],
    osc_lows: list[tuple[int, float]],
    max_bar_gap: int = 20,
) -> list[dict]:
    """Bullish divergence: price makes lower low, oscillator makes higher low."""
    divergences = []
    if len(price_lows) < 2 or len(osc_lows) < 2:
        return divergences
    for i in range(1, len(price_lows)):
        p_prev_idx, p_prev_val = price_lows[i - 1]
        p_curr_idx, p_curr_val = price_lows[i]
        if p_curr_val >= p_prev_val:
            continue
        if abs(p_curr_idx - p_prev_idx) > max_bar_gap:
            continue
        osc_near_prev = _nearest_swing(osc_lows, p_prev_idx, tolerance=3)
        osc_near_curr = _nearest_swing(osc_lows, p_curr_idx, tolerance=3)
        if osc_near_prev and osc_near_curr:
            if osc_near_curr[1] > osc_near_prev[1]:
                divergences.append({
                    "type": "bullish",
                    "price_idx_1": p_prev_idx, "price_idx_2": p_curr_idx,
                    "price_val_1": p_prev_val, "price_val_2": p_curr_val,
                    "osc_idx_1": osc_near_prev[0], "osc_idx_2": osc_near_curr[0],
                    "osc_val_1": osc_near_prev[1], "osc_val_2": osc_near_curr[1],
                })
    return divergences


def detect_bearish_divergence(
    price_highs: list[tuple[int, float]],
    osc_highs: list[tuple[int, float]],
    max_bar_gap: int = 20,
) -> list[dict]:
    """Bearish divergence: price makes higher high, oscillator makes lower high."""
    divergences = []
    if len(price_highs) < 2 or len(osc_highs) < 2:
        return divergences
    for i in range(1, len(price_highs)):
        p_prev_idx, p_prev_val = price_highs[i - 1]
        p_curr_idx, p_curr_val = price_highs[i]
        if p_curr_val <= p_prev_val:
            continue
        if abs(p_curr_idx - p_prev_idx) > max_bar_gap:
            continue
        osc_near_prev = _nearest_swing(osc_highs, p_prev_idx, tolerance=3)
        osc_near_curr = _nearest_swing(osc_highs, p_curr_idx, tolerance=3)
        if osc_near_prev and osc_near_curr:
            if osc_near_curr[1] < osc_near_prev[1]:
                divergences.append({
                    "type": "bearish",
                    "price_idx_1": p_prev_idx, "price_idx_2": p_curr_idx,
                    "price_val_1": p_prev_val, "price_val_2": p_curr_val,
                    "osc_idx_1": osc_near_prev[0], "osc_idx_2": osc_near_curr[0],
                    "osc_val_1": osc_near_prev[1], "osc_val_2": osc_near_curr[1],
                })
    return divergences


# ---------------------------------------------------------------------------
# Hook detection (Tap 1)
# ---------------------------------------------------------------------------

def detect_hooks(osc_values: list[float], direction: str, start_idx: int = 0) -> list[int]:
    """Detect oscillator hooks after a divergence.
    Bull hook: osc was falling and turns up from below 30.
    Bear hook: osc was rising and turns down from above 70.
    Returns list of bar indices where hooks occur."""
    hooks = []
    if len(osc_values) < 3:
        return hooks
    for i in range(max(2, start_idx), len(osc_values)):
        if direction == "BULL":
            if (osc_values[i - 1] < osc_values[i - 2] and
                osc_values[i] > osc_values[i - 1] and
                osc_values[i - 1] < 30):
                hooks.append(i)
        elif direction == "BEAR":
            if (osc_values[i - 1] > osc_values[i - 2] and
                osc_values[i] < osc_values[i - 1] and
                osc_values[i - 1] > 70):
                hooks.append(i)
    return hooks


# ---------------------------------------------------------------------------
# Candle patterns (Tap 5)
# ---------------------------------------------------------------------------

def is_engulfing_bullish(candles: list[dict], idx: int) -> bool:
    if idx < 1:
        return False
    prev, curr = candles[idx - 1], candles[idx]
    if None in (prev.get("open"), prev.get("close"), curr.get("open"), curr.get("close")):
        return False
    return (
        prev["close"] < prev["open"] and
        curr["close"] > curr["open"] and
        curr["open"] <= prev["close"] and
        curr["close"] >= prev["open"]
    )


def is_engulfing_bearish(candles: list[dict], idx: int) -> bool:
    if idx < 1:
        return False
    prev, curr = candles[idx - 1], candles[idx]
    if None in (prev.get("open"), prev.get("close"), curr.get("open"), curr.get("close")):
        return False
    return (
        prev["close"] > prev["open"] and
        curr["close"] < curr["open"] and
        curr["open"] >= prev["close"] and
        curr["close"] <= prev["open"]
    )


def is_hammer(candle: dict, body_ratio: float = 0.3) -> bool:
    """Hammer: small body at top, long lower shadow."""
    o, h, l, c = candle.get("open"), candle.get("high"), candle.get("low"), candle.get("close")
    if None in (o, h, l, c):
        return False
    total_range = h - l
    if total_range == 0:
        return False
    body = abs(c - o)
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    return body / total_range <= body_ratio and lower_shadow >= 2 * body and upper_shadow <= body


def is_shooting_star(candle: dict, body_ratio: float = 0.3) -> bool:
    """Shooting star: small body at bottom, long upper shadow."""
    o, h, l, c = candle.get("open"), candle.get("high"), candle.get("low"), candle.get("close")
    if None in (o, h, l, c):
        return False
    total_range = h - l
    if total_range == 0:
        return False
    body = abs(c - o)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    return body / total_range <= body_ratio and upper_shadow >= 2 * body and lower_shadow <= body


def has_reversal_candle(candles: list[dict], idx: int, direction: str) -> str | None:
    """Check if bar at idx is a reversal candle. Returns pattern name or None."""
    if direction == "BULL":
        if is_engulfing_bullish(candles, idx):
            return "engulfing_bullish"
        if is_hammer(candles[idx]):
            return "hammer"
        c = candles[idx]
        if c.get("high") and c.get("low") and c.get("close") and c.get("open"):
            total = c["high"] - c["low"]
            if total > 0:
                lower_wick = min(c["open"], c["close"]) - c["low"]
                if lower_wick / total >= 0.6 and c["close"] > c["open"]:
                    return "long_lower_wick"
    elif direction == "BEAR":
        if is_engulfing_bearish(candles, idx):
            return "engulfing_bearish"
        if is_shooting_star(candles[idx]):
            return "shooting_star"
        c = candles[idx]
        if c.get("high") and c.get("low") and c.get("close") and c.get("open"):
            total = c["high"] - c["low"]
            if total > 0:
                upper_wick = c["high"] - max(c["open"], c["close"])
                if upper_wick / total >= 0.6 and c["close"] < c["open"]:
                    return "long_upper_wick"
    return None


# ---------------------------------------------------------------------------
# RVOL calculations (Tap 2, 3, 5)
# ---------------------------------------------------------------------------

def calc_rvol_simple(volumes: list[float], period: int = 20) -> float | None:
    """RVOL = current volume / avg(prior N bars volume)."""
    if len(volumes) < period + 1:
        return None
    avg_vol = sum(volumes[-(period + 1):-1]) / period
    if avg_vol == 0:
        return None
    return volumes[-1] / avg_vol


def calc_rvol_tod(current_volume: float, tod_volumes: list[float]) -> float | None:
    """Time-of-day RVOL = current bar volume / avg volume for same TOD slot."""
    if not tod_volumes or len(tod_volumes) < 5:
        return None
    avg = sum(tod_volumes) / len(tod_volumes)
    if avg == 0:
        return None
    return current_volume / avg


# ---------------------------------------------------------------------------
# Slope calculations (Tap 2, 3, 4)
# ---------------------------------------------------------------------------

def sma_slope_pct(values: list[float], period: int = 1) -> float | None:
    """Percentage slope over `period` bars: (val[-1] - val[-1-period]) / val[-1-period] * 100."""
    if len(values) < period + 1:
        return None
    prev = values[-(period + 1)]
    curr = values[-1]
    if prev == 0:
        return None
    return (curr - prev) / abs(prev) * 100


def slope_sign(values: list[float], lookback: int = 10) -> int:
    """Return +1, -1, or 0 for the slope direction over lookback bars."""
    if len(values) < lookback + 1:
        return 0
    diff = values[-1] - values[-(lookback + 1)]
    if diff > 0:
        return 1
    elif diff < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Time gate (Tap 1, 3)
# ---------------------------------------------------------------------------

def is_in_no_trade_window(candle_time, start_hour: int = 9, start_min: int = 0,
                          end_hour: int = 11, end_min: int = 0) -> bool:
    """Check if candle time falls within a no-trade window (Pacific Time).
    In QuantConnect, algorithm time is Eastern. Pacific = Eastern - 3 hours.
    Returns True if inside the window (DO NOT TRADE)."""
    # Lean runs in Eastern time; Pacific is 3 hours behind
    pacific_hour = (candle_time.hour - 3) % 24
    local_time = dt_time(pacific_hour, candle_time.minute)

    start = dt_time(start_hour, start_min)
    end = dt_time(end_hour, end_min)
    return start <= local_time <= end
