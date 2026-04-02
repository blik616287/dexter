"""Tap 5 – ENSEMBLE C (BT Divergence Reversal) Alpha Model.

Ported from services/alert-engine/src/evaluators/ensemble_c.py.
Dual-timeframe (5m + 15m) orchestration included here (was in main.py in
the original system). Detects stochastic divergence + VWAP stretch + reversal
candle + elevated RVOL. DOUBLE CONFIRM when both TFs fire same direction.
"""

import logging

from .base_alpha import BaseShoulderTapAlpha
from .utils import (
    find_swing_lows,
    find_swing_highs,
    detect_bullish_divergence,
    detect_bearish_divergence,
    has_reversal_candle,
    calc_rvol_simple,
)

logger = logging.getLogger(__name__)

# Thresholds
_VWAP_STRETCH_LOW = 0.99
_VWAP_STRETCH_HIGH = 1.01
_MIN_RVOL = 1.3
_MIN_CANDLES = 15
_DOUBLE_CONFIRM_BOOST = 20


class EnsembleCAlpha(BaseShoulderTapAlpha):
    """Tap 5: Ensemble C – dual-timeframe divergence reversal on 5m + 15m bars."""

    def __init__(self):
        super().__init__(
            name="ensemble_c",
            timeframe_label="5m",  # primary timeframe
            lookback=60,
            cooldown_minutes=30,
            symbols=["MSFT", "NVDA", "SPY"],
        )

    def _evaluate(self, algorithm, ticker, candles):
        """Dual-timeframe evaluation: run gate logic on both 5m and 15m windows."""
        candles_5m = candles  # already the 5m window from base class
        candles_15m = algorithm._bar_windows.get(ticker, {}).get("15m", [])

        result_5m = (
            self._evaluate_single_tf(candles_5m, ticker)
            if len(candles_5m) >= _MIN_CANDLES
            else None
        )
        result_15m = (
            self._evaluate_single_tf(list(candles_15m[-60:]), ticker)
            if len(candles_15m) >= _MIN_CANDLES
            else None
        )

        fired_5m = result_5m and result_5m.get("triggered")
        fired_15m = result_15m and result_15m.get("triggered")

        # Double-confirm logic (from main.py lines 311-326 in original)
        if (fired_5m and fired_15m
                and result_5m["direction"] == result_15m["direction"]):
            return {
                "triggered": True,
                "direction": result_5m["direction"],
                "strength": min(100, result_5m["strength"] + _DOUBLE_CONFIRM_BOOST),
                "notes": (
                    f"DOUBLE CONFIRM: 5m + 15m both {result_5m['direction']}"
                ),
                "trigger_values": result_5m.get("trigger_values", {}),
                "context_values": result_5m.get("context_values", {}),
            }
        elif fired_5m:
            return result_5m
        elif fired_15m:
            return result_15m
        return None

    def _evaluate_single_tf(self, candles, ticker):
        """Evaluate a single timeframe. Port of EnsembleCEvaluator.evaluate()."""
        trigger_values = {}
        context_values = {}

        if len(candles) < _MIN_CANDLES:
            return None

        # Gate 1 – BT Divergence detection
        prices_close = [c.get("close") for c in candles]
        stoch_k_vals = [c.get("stoch_k") for c in candles]

        # Handle None values
        prices_close = [v if v is not None else 0 for v in prices_close]
        stoch_k_vals = [v if v is not None else 50 for v in stoch_k_vals]

        prices_low = [c.get("low", c.get("close", 0)) or 0 for c in candles]
        prices_high = [c.get("high", c.get("close", 0)) or 0 for c in candles]

        price_swing_lows = find_swing_lows(prices_low, order=3)
        price_swing_highs = find_swing_highs(prices_high, order=3)
        osc_swing_lows = find_swing_lows(stoch_k_vals, order=3)
        osc_swing_highs = find_swing_highs(stoch_k_vals, order=3)

        bull_divs = detect_bullish_divergence(price_swing_lows, osc_swing_lows)
        bear_divs = detect_bearish_divergence(price_swing_highs, osc_swing_highs)

        n = len(candles)
        recent_cutoff = n - 15
        recent_bull = [d for d in bull_divs if d["price_idx_2"] >= recent_cutoff]
        recent_bear = [d for d in bear_divs if d["price_idx_2"] >= recent_cutoff]

        trigger_values["bull_divergences_found"] = len(recent_bull)
        trigger_values["bear_divergences_found"] = len(recent_bear)

        if not recent_bull and not recent_bear:
            return None

        # Determine direction
        direction = None
        chosen_div = None

        if recent_bull and recent_bear:
            latest_bull_idx = max(d["price_idx_2"] for d in recent_bull)
            latest_bear_idx = max(d["price_idx_2"] for d in recent_bear)
            if latest_bull_idx >= latest_bear_idx:
                direction = "BULL"
                chosen_div = max(recent_bull, key=lambda d: d["price_idx_2"])
            else:
                direction = "BEAR"
                chosen_div = max(recent_bear, key=lambda d: d["price_idx_2"])
        elif recent_bull:
            direction = "BULL"
            chosen_div = max(recent_bull, key=lambda d: d["price_idx_2"])
        else:
            direction = "BEAR"
            chosen_div = max(recent_bear, key=lambda d: d["price_idx_2"])

        trigger_values["direction"] = direction
        trigger_values["divergence"] = chosen_div

        # Gate 2 – VWAP stretch (extreme)
        latest = candles[-1]
        vwap = latest.get("vwap")
        low = latest.get("low")
        high = latest.get("high")

        if vwap is None or vwap == 0:
            return None

        if direction == "BULL":
            if low is None or low > vwap * _VWAP_STRETCH_LOW:
                return None
            stretch_pct = (vwap - low) / vwap * 100
        else:
            if high is None or high < vwap * _VWAP_STRETCH_HIGH:
                return None
            stretch_pct = (high - vwap) / vwap * 100

        trigger_values["vwap_stretch"] = True
        trigger_values["vwap_stretch_pct"] = round(stretch_pct, 3)

        # Gate 3 – Reversal candle
        last_idx = len(candles) - 1
        reversal = has_reversal_candle(candles, last_idx, direction)
        trigger_values["reversal_candle"] = reversal

        if reversal is None:
            return None

        # Gate 4 – RVOL >= 1.3
        volumes = [c.get("volume", 0) or 0 for c in candles]
        rvol = calc_rvol_simple(volumes, 20)
        trigger_values["rvol_20"] = round(rvol, 3) if rvol is not None else None

        if rvol is None or rvol < _MIN_RVOL:
            return None

        # Strength calculation
        strength = 65.0
        if rvol > 2.0:
            strength += 10
        if stretch_pct > 1.5:
            strength += 5
        strength = min(strength, 100.0)

        context_values["symbol"] = ticker
        context_values["timeframe"] = "5m"

        return {
            "triggered": True,
            "direction": direction,
            "strength": strength,
            "notes": (
                f"{direction} ENSEMBLE_C | reversal={reversal} "
                f"rvol={rvol:.2f} vwap_stretch={stretch_pct:.2f}%"
            ),
            "trigger_values": trigger_values,
            "context_values": context_values,
        }
