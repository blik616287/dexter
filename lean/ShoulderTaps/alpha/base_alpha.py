"""Base class for all Shoulder Tap Alpha models.

Handles bar window fetching, cooldown checks, and translating evaluation
result dicts to Lean Insight objects.
"""

from AlgorithmImports import *
from datetime import timedelta


class BaseShoulderTapAlpha(AlphaModel):
    """Base class for all 5 Shoulder Tap Alpha models."""

    def __init__(self, name, timeframe_label, lookback, cooldown_minutes=60, symbols=None):
        """
        Args:
            name: Evaluator name (e.g. "bt_divergence")
            timeframe_label: Bar window key ("5m", "10m", "15m")
            lookback: Number of bars required for evaluation
            cooldown_minutes: Minimum minutes between signals per symbol
            symbols: List of ticker strings this alpha evaluates
        """
        self._name = name
        self._tf_label = timeframe_label
        self._lookback = lookback
        self._cooldown_minutes = cooldown_minutes
        self._last_fired = {}  # ticker -> datetime
        self._symbols = symbols or []

    def Update(self, algorithm, data):
        """Called by Lean on each data event. Fetch bar windows and evaluate."""
        if algorithm.IsWarmingUp:
            return []

        insights = []
        for ticker in self._symbols:
            window = algorithm._bar_windows.get(ticker, {}).get(self._tf_label, [])
            if len(window) < self._lookback:
                continue

            candles = list(window[-self._lookback:])
            if not candles:
                continue

            if self._in_cooldown(ticker, algorithm.Time):
                continue

            result = self._evaluate(algorithm, ticker, candles)
            if result is not None and result.get("triggered"):
                direction = (InsightDirection.Up
                             if result["direction"] == "BULL"
                             else InsightDirection.Down)
                symbol = algorithm._equity_handles[ticker]
                insight = Insight.Price(
                    symbol,
                    timedelta(minutes=60),
                    direction,
                    magnitude=None,
                    confidence=result.get("strength", 50) / 100.0,
                    sourceModel=self._name,
                    tag=result.get("notes", "")
                )
                insights.append(insight)
                self._last_fired[ticker] = algorithm.Time

                algorithm.Debug(
                    f"[{self._name}] {result['direction']} on {ticker} "
                    f"strength={result.get('strength', 0):.0f} | "
                    f"{result.get('notes', '')}"
                )

                # Fire alert notification if manager is available
                if hasattr(algorithm, '_alert_manager') and algorithm._alert_manager:
                    algorithm._alert_manager.fire_alert(
                        model_name=self._name,
                        symbol=ticker,
                        direction=result["direction"],
                        strength=result.get("strength", 50),
                        trigger_values=result.get("trigger_values", {}),
                        context_values=result.get("context_values", {}),
                    )

                # Record forward return tracking if tracker is available
                if hasattr(algorithm, '_forward_tracker') and algorithm._forward_tracker:
                    fire_price = candles[-1].get("close", 0)
                    algorithm._forward_tracker.record_signal(
                        model=self._name,
                        symbol=ticker,
                        tf_label=self._tf_label,
                        fire_price=fire_price,
                        fire_time=algorithm.Time,
                        bar_window=window,
                    )

        return insights

    def _in_cooldown(self, ticker, current_time):
        """Check if this alpha has fired within cooldown period for given symbol."""
        last = self._last_fired.get(ticker)
        if last is None:
            return False
        return (current_time - last).total_seconds() < self._cooldown_minutes * 60

    def _evaluate(self, algorithm, ticker, candles):
        """Override in subclass. Return dict with keys: triggered, direction, strength, notes,
        trigger_values, context_values. Or return None if not triggered."""
        raise NotImplementedError

    def OnSecuritiesChanged(self, algorithm, changes):
        """Track security changes (no-op for fixed symbol lists)."""
        pass
