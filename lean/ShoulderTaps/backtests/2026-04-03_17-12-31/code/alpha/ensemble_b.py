"""Tap 4 – ENSEMBLE B (SPY Scalp) Alpha Model.

Ported from services/alert-engine/src/evaluators/ensemble_b.py.
SPY-only evaluator combining TICK structure, TICK SMA slope, HIRO slope,
Vol Trace, and a regime gate (Flight Check + SAM).
"""

import logging

from .base_alpha import BaseShoulderTapAlpha
from .proxies import (
    proxy_flight_check,
    proxy_sam,
    proxy_tick_structure,
    proxy_tick_sma_slope,
    proxy_hiro_slope,
    proxy_vol_trace,
)

logger = logging.getLogger(__name__)

# Regime thresholds
_FC_STRONG_BEAR = -5
_SAM_STRONG_BEAR = -5
_FC_STRONG_BULL = 5
_SAM_STRONG_BULL = 5


class EnsembleBAlpha(BaseShoulderTapAlpha):
    """Tap 4: Ensemble B – SPY-only 5-gate scalp on 5m bars."""

    def __init__(self):
        super().__init__(
            name="ensemble_b",
            timeframe_label="5m",
            lookback=50,
            cooldown_minutes=15,
            symbols=["SPY"],
        )

    def _evaluate(self, algorithm, ticker, candles):
        trigger_values = {}
        context_values = {}
        market_state = algorithm.get_market_state()

        if len(candles) < 25:
            return None

        # Get SPY 1m candles for TICK/HIRO proxies
        spy_1m_candles = algorithm._bar_windows.get("SPY", {}).get("1m_raw", [])

        # Gate 1 – TICK structure
        tick_struct = proxy_tick_structure(spy_1m_candles)
        if tick_struct is None:
            return None

        direction = tick_struct["direction"]
        trigger_values["tick_structure"] = tick_struct
        trigger_values["direction"] = direction

        # Gate 2 – TICK SMA slope must agree with direction
        tick_sma = proxy_tick_sma_slope(candles)
        trigger_values["tick_sma_slope"] = tick_sma

        if tick_sma is None:
            return None
        if direction == "BULL" and tick_sma <= 0:
            return None
        if direction == "BEAR" and tick_sma >= 0:
            return None

        # Gate 3 – HIRO slope must agree with direction
        hiro = proxy_hiro_slope(spy_1m_candles)
        trigger_values["hiro_slope"] = hiro

        if hiro is None:
            return None
        if direction == "BULL" and hiro <= 0:
            return None
        if direction == "BEAR" and hiro >= 0:
            return None

        # Gate 4 – Vol Trace must match direction
        vol_trace = proxy_vol_trace(candles)
        trigger_values["vol_trace"] = vol_trace

        if vol_trace is None:
            return None
        if vol_trace != direction:
            return None

        # Gate 5 – Regime gate (Flight Check + SAM)
        fc = proxy_flight_check(candles)
        sam = proxy_sam(market_state, candles)
        trigger_values["flight_check"] = fc
        trigger_values["sam"] = sam

        allows_long = True
        allows_short = True

        if fc <= _FC_STRONG_BEAR or sam <= _SAM_STRONG_BEAR:
            allows_long = False
        if fc >= _FC_STRONG_BULL or sam >= _SAM_STRONG_BULL:
            allows_short = False

        if direction == "BULL" and not allows_long:
            return None
        if direction == "BEAR" and not allows_short:
            return None

        # Strength calculation
        strength = 60.0

        test1 = tick_struct.get("test1", 0)
        test2 = tick_struct.get("test2", 0)
        test_gap = abs(test2 - test1)
        trigger_values["tick_test_gap"] = round(test_gap, 4)
        avg_test_mag = (abs(test1) + abs(test2)) / 2 if (test1 or test2) else 1
        if avg_test_mag > 0 and test_gap / avg_test_mag > 0.3:
            strength += 10

        strength = min(strength, 100.0)

        context_values["symbol"] = ticker
        context_values["timeframe"] = "5m"

        return {
            "triggered": True,
            "direction": direction,
            "strength": strength,
            "notes": (
                f"{direction} ENSEMBLE_B | fc={fc:.1f} sam={sam:.1f} "
                f"tick_sma={tick_sma:.4f} hiro={hiro:.4f} vol_trace={vol_trace}"
            ),
            "trigger_values": trigger_values,
            "context_values": context_values,
        }
