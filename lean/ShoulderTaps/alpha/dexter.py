"""Tap 2 – DEXTER Alpha Model.

Detects breakout setups where moving averages are aligned, compressed volatility
(low ATR/Close ratio) precedes a 10-bar breakout, confirmed by elevated RVOL.

Evaluates on EVERY OnData tick (minute resolution), not just bar boundaries:
    - Gates 1-3 (MA alignment, slope, ATR) use the last completed 10m bar.
    - Gates 4-5 (breakout, RVOL) use the live price and accumulating volume.
    - Latch suppresses re-trigger within the same bar.
    - Invalidation fires immediately when price falls back inside channel.
    - New bar allows fresh evaluation regardless of prior latch.
    - TTL expires latch silently after 10 minutes.
"""

import logging
from datetime import timedelta

from AlgorithmImports import Insight, InsightDirection

from .base_alpha import BaseShoulderTapAlpha
from .utils import calc_rvol_tod, sma_slope_pct

logger = logging.getLogger(__name__)

# Thresholds — identical to python/dexter_alert.py
_ATR_COMPRESSION_THRESHOLD = 0.008
_RVOL_MINIMUM = 1.2
_BREAKOUT_LOOKBACK = 10
_MA_STRONG_SLOPE_PCT = 0.1
_MIN_CANDLES = 12
_LATCH_TTL_MINUTES = 10
_BAR_MINUTES = 10


class _BreakoutLatch:
    """Tracks an active breakout for invalidation detection."""

    __slots__ = ("bar_time", "direction", "channel_high", "channel_low", "entry_timestamp")

    def __init__(self, bar_time, direction, channel_high, channel_low, entry_timestamp):
        self.bar_time = bar_time
        self.direction = direction
        self.channel_high = channel_high
        self.channel_low = channel_low
        self.entry_timestamp = entry_timestamp

    def is_invalidated(self, close):
        if self.direction == "BULL":
            return close <= self.channel_high
        return close >= self.channel_low

    def is_expired(self, now, ttl_minutes=_LATCH_TTL_MINUTES):
        if self.entry_timestamp is None:
            return False
        elapsed = (now - self.entry_timestamp).total_seconds() / 60
        return elapsed >= ttl_minutes


class DexterAlpha(BaseShoulderTapAlpha):
    """Tap 2: DEXTER – MA-aligned ATR-compressed breakout with RVOL.

    Called on every OnData (minute resolution). Uses live price for
    breakout detection and invalidation, completed 10m bars for indicators.
    """

    def __init__(self):
        super().__init__(
            name="dexter",
            timeframe_label="10m",
            lookback=60,
            cooldown_minutes=0,
            symbols=[
                "AAPL", "MSFT", "GOOGL", "AMZN", "META",
                "NVDA", "AVGO",
                "JPM", "V", "GS",
                "UNH", "LLY",
                "COST", "HD",
                "CRM",
            ],
        )
        self._latches = {}
        # Track accumulating volume per ticker within the current 10m bar
        # {ticker: {"bar_start": datetime, "volume": float}}
        self._live_volume = {}

    def Update(self, algorithm, data):
        """Called on every OnData (minute resolution).

        Uses completed 10m bars for gates 1-3 indicators, live price
        and accumulating volume for gates 4-5. Handles latch engagement,
        invalidation, and TTL expiry on every tick.
        """
        if algorithm.IsWarmingUp:
            return []

        insights = []
        current_time = algorithm.Time

        for ticker in self._symbols:
            symbol = algorithm._equity_handles.get(ticker)
            if symbol is None:
                continue

            # Live price from the securities object
            current_price = algorithm.Securities[symbol].Price
            if current_price <= 0:
                continue

            # Accumulate volume for the current 10m bar
            bar_start = self._snap_bar_start(current_time)
            live_vol = self._accumulate_volume(ticker, bar_start, data, symbol)

            # Get completed 10m bar history for indicators
            window = algorithm._bar_windows.get(ticker, {}).get(self._tf_label, [])
            if len(window) < _MIN_CANDLES:
                continue
            candles = list(window[-self._lookback:])
            if not candles:
                continue

            # --- Check active latch ---
            latch = self._latches.get(ticker)
            if latch is not None:
                if latch.is_expired(current_time):
                    # TTL exceeded — silently release
                    del self._latches[ticker]
                elif latch.bar_time == bar_start:
                    # Same bar as the latch — suppress re-trigger
                    continue
                else:
                    # Different bar — check if the LAST COMPLETED BAR closed
                    # back inside the channel (invalidation on bar close only)
                    last_close = float(candles[-1].get("close", 0))
                    if latch.is_invalidated(last_close):
                        entry_ts = latch.entry_timestamp
                        del self._latches[ticker]

                        algorithm.Debug(
                            f"[dexter] INVALIDATED {ticker} — bar closed "
                            f"at ${last_close:.2f} inside channel "
                            f"(entry was {entry_ts})"
                        )
                        if hasattr(algorithm, '_alert_manager') and algorithm._alert_manager:
                            algorithm._alert_manager.fire_alert(
                                model_name="dexter",
                                symbol=ticker,
                                direction="INVALIDATED",
                                strength=0,
                                trigger_values={
                                    "action_type": "EXIT",
                                    "close": last_close,
                                    "entry_timestamp": str(entry_ts),
                                },
                                context_values={
                                    "channel_high": latch.channel_high,
                                    "channel_low": latch.channel_low,
                                    "original_direction": latch.direction,
                                },
                            )
                        continue
                    # Bar closed outside channel — latch holds, allow new eval

            # --- Evaluate 5 gates using live price + completed bar indicators ---
            result = self._evaluate_live(
                algorithm, ticker, candles, current_price, live_vol, bar_start,
            )
            if result is not None and result.get("triggered"):
                direction = result["direction"]

                # Engage latch
                self._latches[ticker] = _BreakoutLatch(
                    bar_time=bar_start,
                    direction=direction,
                    channel_high=result["context_values"]["channel_high"],
                    channel_low=result["context_values"]["channel_low"],
                    entry_timestamp=current_time,
                )

                # Emit Insight
                insight_dir = (InsightDirection.Up if direction == "BULL"
                               else InsightDirection.Down)
                insight = Insight.Price(
                    symbol,
                    timedelta(minutes=60),
                    insight_dir,
                    magnitude=None,
                    confidence=result.get("strength", 50) / 100.0,
                    sourceModel=self._name,
                    tag=result.get("notes", ""),
                )
                insights.append(insight)

                algorithm.Debug(
                    f"[dexter] {direction} on {ticker} "
                    f"strength={result.get('strength', 0):.0f} "
                    f"@ ${current_price:.2f} | {result.get('notes', '')}"
                )

                if hasattr(algorithm, '_alert_manager') and algorithm._alert_manager:
                    algorithm._alert_manager.fire_alert(
                        model_name=self._name,
                        symbol=ticker,
                        direction=direction,
                        strength=result.get("strength", 50),
                        trigger_values=result.get("trigger_values", {}),
                        context_values=result.get("context_values", {}),
                    )

                if hasattr(algorithm, '_forward_tracker') and algorithm._forward_tracker:
                    algorithm._forward_tracker.record_signal(
                        model=self._name,
                        symbol=ticker,
                        tf_label=self._tf_label,
                        fire_price=current_price,
                        fire_time=current_time,
                        bar_window=window,
                    )

        return insights

    # ------------------------------------------------------------------
    # Live evaluation — gates 1-3 from bars, gates 4-5 from live data
    # ------------------------------------------------------------------

    def _evaluate_live(self, algorithm, ticker, candles, live_close, live_volume, bar_start):
        """Run 5-gate evaluation using live price and volume.

        Gates 1-3 use the last completed bar's indicators.
        Gates 4-5 use the live close and accumulating volume.
        """
        latest = candles[-1]

        # --- Gate 1: MA alignment (from completed bars) ---
        sma_20 = latest.get("sma_20")
        sma_50 = latest.get("sma_50")
        if sma_20 is None or sma_50 is None:
            return None

        sma_20 = float(sma_20)
        sma_50 = float(sma_50)

        if sma_20 > sma_50:
            direction = "BULL"
        elif sma_20 < sma_50:
            direction = "BEAR"
        else:
            return None

        # --- Gate 2: Slope agreement (from completed bars) ---
        sma_20_series = [
            float(c["sma_20"]) for c in candles if c.get("sma_20") is not None
        ]
        sma_50_series = [
            float(c["sma_50"]) for c in candles if c.get("sma_50") is not None
        ]
        if len(sma_20_series) < 2 or len(sma_50_series) < 2:
            return None

        slope_20 = sma_slope_pct(sma_20_series, period=1)
        slope_50 = sma_slope_pct(sma_50_series, period=1)
        if slope_20 is None or slope_50 is None:
            return None

        if direction == "BULL" and not (slope_20 > 0 and slope_50 > 0):
            return None
        if direction == "BEAR" and not (slope_20 < 0 and slope_50 < 0):
            return None

        # --- Gate 3: ATR compression (ATR from bars, close from live) ---
        atr_14 = latest.get("atr_14")
        if atr_14 is None or live_close == 0:
            return None

        atr_14 = float(atr_14)
        atr_ratio = atr_14 / live_close
        if atr_ratio > _ATR_COMPRESSION_THRESHOLD:
            return None

        # --- Gate 4: 10-bar breakout (channel from bars, close from live) ---
        if len(candles) < _BREAKOUT_LOOKBACK:
            return None

        channel_candles = candles[-_BREAKOUT_LOOKBACK:]
        channel_highs = [
            float(c["high"]) for c in channel_candles if c.get("high") is not None
        ]
        channel_lows = [
            float(c["low"]) for c in channel_candles if c.get("low") is not None
        ]
        if not channel_highs or not channel_lows:
            return None

        if direction == "BULL" and not (live_close > max(channel_highs)):
            return None
        if direction == "BEAR" and not (live_close < min(channel_lows)):
            return None

        # --- Gate 5: RVOL TOD (live volume vs historical baseline) ---
        if live_volume == 0:
            return None

        rvol = None
        tod_vols = algorithm.get_tod_volumes(ticker, self._tf_label, bar_start)
        if tod_vols:
            rvol = calc_rvol_tod(float(live_volume), tod_vols)

        if rvol is None or rvol < _RVOL_MINIMUM:
            return None

        # --- All gates passed — strength scoring ---
        strength = 60.0
        rvol_bonus = min(20.0, (rvol - _RVOL_MINIMUM) * 50)
        strength += max(0.0, rvol_bonus)
        if abs(slope_20) > _MA_STRONG_SLOPE_PCT and abs(slope_50) > _MA_STRONG_SLOPE_PCT:
            strength += 5
        strength = min(100.0, strength)

        action_type = "BUY" if direction == "BULL" else "SELL"

        return {
            "triggered": True,
            "direction": direction,
            "strength": strength,
            "notes": (
                f"{direction} DEXTER breakout, "
                f"ATR ratio={atr_ratio:.5f}, RVOL={rvol:.2f}"
            ),
            "trigger_values": {
                "action_type": action_type,
                "sma_20": sma_20,
                "sma_50": sma_50,
                "atr_ratio": round(atr_ratio, 6),
                "rvol": round(rvol, 2),
                "close": live_close,
            },
            "context_values": {
                "slope_20": round(slope_20, 4),
                "slope_50": round(slope_50, 4),
                "atr_14": atr_14,
                "channel_high": max(channel_highs),
                "channel_low": min(channel_lows),
            },
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _snap_bar_start(self, dt):
        """Snap a datetime to the start of its 10m bar interval."""
        minutes = (dt.hour * 60 + dt.minute) // _BAR_MINUTES * _BAR_MINUTES
        return dt.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)

    def _accumulate_volume(self, ticker, bar_start, data, symbol):
        """Track accumulating volume within the current 10m bar."""
        entry = self._live_volume.get(ticker)
        if entry is None or entry["bar_start"] != bar_start:
            self._live_volume[ticker] = {"bar_start": bar_start, "volume": 0}

        if data.ContainsKey(symbol) and data[symbol] is not None:
            bar = data[symbol]
            if hasattr(bar, 'Volume'):
                self._live_volume[ticker]["volume"] += float(bar.Volume)

        return self._live_volume[ticker]["volume"]

    def _evaluate(self, algorithm, ticker, candles):
        """Not used — kept for interface compatibility with BaseShoulderTapAlpha."""
        return None

    def clear_channel(self, ticker):
        """Remove latch tracking for a ticker (called on EOD close)."""
        self._latches.pop(ticker, None)
        self._live_volume.pop(ticker, None)
