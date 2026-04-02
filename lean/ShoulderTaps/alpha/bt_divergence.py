"""Tap 1 – BT Divergence (Double Hook) Alpha Model.

Ported from services/alert-engine/src/evaluators/bt_divergence.py.
Detects bullish/bearish divergence between price and the stochastic oscillator,
then requires a "double hook" confirmation (>= 2 hooks) plus a Turn-Confirm bar.
"""

import logging

from .base_alpha import BaseShoulderTapAlpha
from .utils import (
    find_swing_highs,
    find_swing_lows,
    detect_bullish_divergence,
    detect_bearish_divergence,
    detect_hooks,
    is_in_no_trade_window,
    sma_slope_pct,
)

logger = logging.getLogger(__name__)

# Thresholds (previously read from edge_card via _threshold())
_STOCH_NEUTRAL_LOW = 30
_STOCH_NEUTRAL_HIGH = 70
_STOCH_EXTREME_LOW = 15
_STOCH_EXTREME_HIGH = 85
_ADX_FREIGHT_TRAIN = 40
_SMA_SLOPE_FREIGHT_TRAIN = 0.25  # percent per bar
_RECENCY_BARS = 10
_MIN_CANDLES = 10


class BTDivergenceAlpha(BaseShoulderTapAlpha):
    """Tap 1: BT Divergence with Double-Hook confirmation on 10m bars."""

    def __init__(self):
        super().__init__(
            name="bt_divergence",
            timeframe_label="10m",
            lookback=60,
            cooldown_minutes=30,
            symbols=["MSFT", "NVDA", "SPY"],
        )

    def _evaluate(self, algorithm, ticker, candles):
        if len(candles) < _MIN_CANDLES:
            return None

        # 1. Extract stoch_k series, filtering None values
        stoch_k_raw = [c.get("stoch_k") for c in candles]
        stoch_k_values = []
        stoch_idx_map = []
        for i, v in enumerate(stoch_k_raw):
            if v is not None:
                stoch_k_values.append(float(v))
                stoch_idx_map.append(i)

        if len(stoch_k_values) < _MIN_CANDLES:
            return None

        # 2. Price lows / highs
        lows = [float(c["low"]) for c in candles if c.get("low") is not None]
        highs = [float(c["high"]) for c in candles if c.get("high") is not None]

        if len(lows) < _MIN_CANDLES or len(highs) < _MIN_CANDLES:
            return None

        # 3. Swing detection
        price_swing_lows = find_swing_lows(lows)
        price_swing_highs = find_swing_highs(highs)
        osc_swing_lows = find_swing_lows(stoch_k_values)
        osc_swing_highs = find_swing_highs(stoch_k_values)

        # 4. Divergence detection
        bull_divs = detect_bullish_divergence(price_swing_lows, osc_swing_lows)
        bear_divs = detect_bearish_divergence(price_swing_highs, osc_swing_highs)

        n_stoch = len(stoch_k_values)

        # 5. Check recent divergences for double-hook + turn-confirm
        best_result = None

        for div in bull_divs:
            result = self._check_divergence(
                div, "BULL", candles, stoch_k_values, stoch_idx_map, n_stoch,
            )
            if result and result.get("triggered"):
                if best_result is None or result["strength"] > best_result["strength"]:
                    best_result = result

        for div in bear_divs:
            result = self._check_divergence(
                div, "BEAR", candles, stoch_k_values, stoch_idx_map, n_stoch,
            )
            if result and result.get("triggered"):
                if best_result is None or result["strength"] > best_result["strength"]:
                    best_result = result

        return best_result

    def _check_divergence(self, div, direction, candles, stoch_k_values,
                          stoch_idx_map, n_stoch):
        """Evaluate a single divergence for hook count, turn-confirm, and filters."""

        # The second oscillator pivot index (in stoch_k_values space)
        osc_pivot_idx = div["osc_idx_2"]

        # Recency check
        if osc_pivot_idx < n_stoch - _RECENCY_BARS:
            return None

        # 5a. Hook detection from the divergence pivot onward
        hooks = detect_hooks(stoch_k_values, direction, start_idx=osc_pivot_idx)
        hook_count = len(hooks)

        if hook_count < 2:
            return None  # need double hook

        # 6. Turn-Confirm on the latest bar
        latest = candles[-1]
        prior = candles[-2]

        if direction == "BULL":
            if latest.get("close") is None or prior.get("high") is None:
                return None
            if not (latest["close"] > prior["high"]):
                return None
        else:  # BEAR
            if latest.get("close") is None or prior.get("low") is None:
                return None
            if not (latest["close"] < prior["low"]):
                return None

        # 7a. Time gate (9-11am PT)
        candle_time = latest.get("time")
        if candle_time is not None and is_in_no_trade_window(candle_time):
            return None

        # 7b. Stoch in neutral zone
        latest_stoch = stoch_k_values[-1] if stoch_k_values else None
        if latest_stoch is not None and _STOCH_NEUTRAL_LOW <= latest_stoch <= _STOCH_NEUTRAL_HIGH:
            return None

        # 7c. SMA slope "freight train" check
        sma_20_vals = [
            float(c["sma_20"]) for c in candles
            if c.get("sma_20") is not None
        ]
        if sma_20_vals:
            slope = sma_slope_pct(sma_20_vals, period=1)
            if slope is not None and abs(slope) > _SMA_SLOPE_FREIGHT_TRAIN:
                return None

        # 7d. ADX trending too hard
        adx = latest.get("adx_14")
        if adx is not None and adx > _ADX_FREIGHT_TRAIN:
            return None

        # Strength scoring
        strength = 60.0

        osc_val_at_div = div["osc_val_2"]
        if osc_val_at_div < _STOCH_EXTREME_LOW or osc_val_at_div > _STOCH_EXTREME_HIGH:
            strength += 10

        vwap = latest.get("vwap")
        close = latest.get("close")
        if vwap is not None and close is not None and vwap > 0:
            pct_from_vwap = abs(close - vwap) / vwap
            if pct_from_vwap <= 0.005:
                strength += 10

        extra_hooks = hook_count - 2
        strength += extra_hooks * 5
        strength = min(100.0, strength)

        action_type = "BUY" if direction == "BULL" else "SELL"

        return {
            "triggered": True,
            "direction": direction,
            "strength": strength,
            "notes": f"{direction} divergence, {hook_count} hooks, turn-confirm",
            "trigger_values": {
                "divergence_type": div["type"],
                "hook_count": hook_count,
                "action_type": action_type,
                "osc_val_at_div": osc_val_at_div,
                "latest_stoch_k": latest_stoch,
            },
            "context_values": {
                "price_pivot_1": div["price_val_1"],
                "price_pivot_2": div["price_val_2"],
                "osc_pivot_1": div["osc_val_1"],
                "osc_pivot_2": div["osc_val_2"],
                "adx_14": adx,
                "vwap": vwap,
            },
        }
