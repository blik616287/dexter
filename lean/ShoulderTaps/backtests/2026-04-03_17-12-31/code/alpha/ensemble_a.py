"""Tap 3 – ENSEMBLE A (Momentum Confluence) Alpha Model.

Ported from services/alert-engine/src/evaluators/ensemble_a.py.
All 7 gates must pass for a trigger. Direction is determined by the SMA(5) slope,
then every other gate is checked for that direction.
"""

import logging

from .base_alpha import BaseShoulderTapAlpha
from .utils import (
    sma_slope_pct,
    slope_sign,
    calc_rvol_simple,
    is_in_no_trade_window,
)
from .proxies import proxy_flight_check, proxy_sam

logger = logging.getLogger(__name__)

# Thresholds
_MIN_SMA5_SLOPE = 0.20
_MIN_FLIGHT_CHECK = 3
_MIN_SAM = 3
_MIN_RVOL = 1.10
_MIN_SECTOR_PCT = 0.50


class EnsembleAAlpha(BaseShoulderTapAlpha):
    """Tap 3: Ensemble A – 7-gate momentum confluence on 5m bars."""

    def __init__(self):
        super().__init__(
            name="ensemble_a",
            timeframe_label="5m",
            lookback=50,
            cooldown_minutes=60,
            symbols=["MSFT", "NVDA"],
        )

    def _evaluate(self, algorithm, ticker, candles):
        trigger_values = {}
        context_values = {}

        if len(candles) < 5:
            return None

        market_state = algorithm.get_market_state()

        # Gate 1 – SMA(5) slope with acceleration check
        sma5_vals = [c.get("sma_5") for c in candles if c.get("sma_5") is not None]
        if len(sma5_vals) < 4:
            return None

        slope_now = sma_slope_pct(sma5_vals, period=1)
        slope_prior = sma_slope_pct(sma5_vals[:-1], period=1)

        if slope_now is None or slope_prior is None:
            return None

        trigger_values["sma5_slope_pct"] = round(slope_now, 4)
        trigger_values["sma5_slope_prior"] = round(slope_prior, 4)

        if slope_now >= _MIN_SMA5_SLOPE and slope_now > slope_prior:
            direction = "BULL"
        elif slope_now <= -_MIN_SMA5_SLOPE and slope_now < slope_prior:
            direction = "BEAR"
        else:
            return None

        trigger_values["direction"] = direction

        # Gate 2 – Flight Check
        fc = proxy_flight_check(candles)
        trigger_values["flight_check"] = fc

        if direction == "BULL" and fc < _MIN_FLIGHT_CHECK:
            return None
        if direction == "BEAR" and fc > -_MIN_FLIGHT_CHECK:
            return None

        # Gate 3 – SAM
        sam = proxy_sam(market_state, candles)
        trigger_values["sam"] = sam

        if direction == "BULL" and sam < _MIN_SAM:
            return None
        if direction == "BEAR" and sam > -_MIN_SAM:
            return None

        # Gate 4 – RVOL
        volumes = [c.get("volume", 0) or 0 for c in candles]
        rvol = calc_rvol_simple(volumes, 20)
        trigger_values["rvol_20"] = round(rvol, 3) if rvol is not None else None

        if rvol is None or rvol < _MIN_RVOL:
            return None

        # Gate 5 – VIX slope (from algorithm's VIX bar window)
        vix_candles = algorithm._bar_windows.get("VIX", {}).get("5m", [])
        vix_closes = [c.get("close") for c in vix_candles if c.get("close") is not None]
        vix_slope = slope_sign(vix_closes, lookback=10)
        trigger_values["vix_slope_sign"] = vix_slope

        if direction == "BULL" and vix_slope >= 0:
            return None
        if direction == "BEAR" and vix_slope <= 0:
            return None

        # Gate 6 – Sector ETF
        sector_ticker = algorithm.SYMBOL_TO_SECTOR.get(ticker)
        sector_pct = None
        if sector_ticker and hasattr(algorithm, '_sector_pct_change'):
            sector_pct = algorithm._sector_pct_change.get(sector_ticker)
        trigger_values["sector_etf_pct"] = sector_pct

        if sector_pct is None:
            return None
        if direction == "BULL" and sector_pct < _MIN_SECTOR_PCT:
            return None
        if direction == "BEAR" and sector_pct > -_MIN_SECTOR_PCT:
            return None

        # Gate 7 – Time gate
        candle_time = candles[-1].get("time")
        if candle_time is not None and is_in_no_trade_window(candle_time):
            trigger_values["no_trade_window"] = True
            return None
        trigger_values["no_trade_window"] = False

        # VWAP context (logged, NOT a hard gate)
        price = candles[-1].get("close")
        vwap = candles[-1].get("vwap")
        if price is not None and vwap is not None:
            if direction == "BULL":
                trigger_values["vwap_aligned"] = price > vwap
            else:
                trigger_values["vwap_aligned"] = price < vwap
        else:
            trigger_values["vwap_aligned"] = None

        # Strength calculation
        strength = 55.0
        if rvol is not None and rvol > 2.0:
            strength += 5
        if direction == "BULL" and fc > 6:
            strength += 5
        elif direction == "BEAR" and fc < -6:
            strength += 5
        if direction == "BULL" and sam > 6:
            strength += 5
        elif direction == "BEAR" and sam < -6:
            strength += 5
        if abs(slope_now) > 0.50:
            strength += 5
        strength = min(strength, 100.0)

        context_values["symbol"] = ticker
        context_values["timeframe"] = "5m"

        return {
            "triggered": True,
            "direction": direction,
            "strength": strength,
            "notes": (
                f"{direction} ENSEMBLE_A | fc={fc:.1f} sam={sam:.1f} "
                f"rvol={rvol:.2f} slope={slope_now:.4f}"
            ),
            "trigger_values": trigger_values,
            "context_values": context_values,
        }
