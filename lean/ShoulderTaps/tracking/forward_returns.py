"""Forward return tracker for MAE/MFE analysis.

Ported from services/alert-engine/src/forward_tracker.py.
Tracks bar-1, bar-3, bar-6 returns and max adverse/favorable excursion
after each signal fires.
"""


class ForwardReturnTracker:
    """Track post-signal MAE/MFE using the algorithm's bar windows."""

    def __init__(self):
        self._pending = []    # signals waiting for enough forward bars
        self._completed = []  # signals with all 6 forward bars computed

    def record_signal(self, model, symbol, tf_label, fire_price, fire_time, bar_window):
        """Record a new signal for forward return tracking.

        Args:
            model: Alpha model name
            symbol: Ticker string
            tf_label: Timeframe label ("5m", "10m", "15m")
            fire_price: Price at signal fire
            fire_time: Algorithm time at signal fire
            bar_window: Reference to the live bar window list
        """
        self._pending.append({
            "model": model,
            "symbol": symbol,
            "tf": tf_label,
            "fire_price": fire_price,
            "fire_time": fire_time,
            "fire_idx": len(bar_window) - 1,
            "bar_window_ref": bar_window,
        })

    def update(self):
        """Check pending records for completion (6+ forward bars available)."""
        still_pending = []
        for rec in self._pending:
            window = rec["bar_window_ref"]
            fire_idx = rec["fire_idx"]
            fire_price = rec["fire_price"]

            if fire_idx + 1 >= len(window):
                still_pending.append(rec)
                continue

            bars_after = window[fire_idx + 1:]

            if len(bars_after) >= 6:
                result = self._compute_forward(fire_price, bars_after)
                result["model"] = rec["model"]
                result["symbol"] = rec["symbol"]
                result["tf"] = rec["tf"]
                result["fire_time"] = rec["fire_time"]
                self._completed.append(result)
            else:
                still_pending.append(rec)

        self._pending = still_pending

    def _compute_forward(self, fire_price, bars_after):
        """Compute bar-1/3/6 returns and MAE/MFE.

        Same math as forward_tracker.py lines 54-69 in the original system.
        """
        result = {}
        if fire_price == 0:
            return result

        for horizon, label in [(1, "1"), (3, "3"), (6, "6")]:
            if len(bars_after) >= horizon:
                bar_close = bars_after[horizon - 1].get("close", fire_price)
                bar_return = ((bar_close - fire_price) / fire_price) * 100

                lows = [b.get("low", fire_price) for b in bars_after[:horizon]
                        if b.get("low") is not None]
                highs = [b.get("high", fire_price) for b in bars_after[:horizon]
                         if b.get("high") is not None]

                mae = ((min(lows) - fire_price) / fire_price) * 100 if lows else 0
                mfe = ((max(highs) - fire_price) / fire_price) * 100 if highs else 0

                result[f"bar_{label}_return"] = round(bar_return, 4)
                result[f"mae_{label}"] = round(mae, 4)
                result[f"mfe_{label}"] = round(mfe, 4)

        return result

    def get_completed(self):
        """Return all completed forward return records."""
        return list(self._completed)

    def get_pending_count(self):
        """Return number of signals still waiting for forward bars."""
        return len(self._pending)
