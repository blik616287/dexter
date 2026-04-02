"""Custom trade metrics tracker.

Tracks per-Alpha-model performance metrics not built into Lean's stats.
Replaces backtest.py _compute_summary() for exit reason breakdown, per-model
win rates, and options-specific metrics.
"""

import math
from collections import Counter, defaultdict


class TradeMetricsTracker:
    """Track per-model trade performance beyond Lean's built-in statistics."""

    def __init__(self):
        self._trades = []   # completed trade dicts
        self._signals = []  # all signal dicts

    def record_signal(self, model_name, symbol, direction, strength, time):
        """Record every fired signal (regardless of whether it becomes a trade)."""
        self._signals.append({
            "model": model_name,
            "symbol": str(symbol),
            "direction": direction,
            "strength": strength,
            "time": time,
        })

    def record_trade_close(self, symbol, pnl, exit_reason, model_name,
                           entry_premium=0, exit_premium=0,
                           iv_at_entry=0, dte_at_entry=0,
                           direction=None, entry_price=0, exit_price=0,
                           entry_atr=0, entry_time=None, exit_time=None,
                           ticker=None, rvol=None, atr_ratio=None):
        """Record a completed round-trip trade."""
        hold_minutes = 0
        if entry_time and exit_time:
            hold_minutes = int((exit_time - entry_time).total_seconds() / 60)

        self._trades.append({
            "symbol": str(symbol),
            "ticker": ticker or str(symbol).split(" ")[0],
            "pnl": pnl,
            "exit_reason": exit_reason,
            "model": model_name,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_atr": entry_atr,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "hold_minutes": hold_minutes,
            "entry_premium": entry_premium,
            "exit_premium": exit_premium,
            "iv": iv_at_entry,
            "dte": dte_at_entry,
            "rvol": rvol,
            "atr_ratio": atr_ratio,
        })

    def compute_summary(self, model_name=None):
        """Compute metrics matching backtest.py _compute_summary() output."""
        trades = [t for t in self._trades
                  if model_name is None or t["model"] == model_name]
        signals = [s for s in self._signals
                   if model_name is None or s["model"] == model_name]
        n = len(trades)

        if n == 0:
            return {
                "total_signals": len(signals),
                "completed_trips": 0,
                "wins": 0, "losses": 0,
                "win_rate": 0,
                "total_pnl": 0,
            }

        pnl_values = [t["pnl"] for t in trades]
        wins = sum(1 for p in pnl_values if p > 0)
        losses = n - wins
        total_pnl = sum(pnl_values)
        mean_pnl = total_pnl / n

        # Sharpe and Sortino (same math as backtest.py lines 819-831)
        sharpe = None
        sortino = None
        if n >= 2:
            variance = sum((p - mean_pnl) ** 2 for p in pnl_values) / (n - 1)
            std = math.sqrt(variance)
            if std > 0:
                sharpe = mean_pnl / std
            neg = [p for p in pnl_values if p < 0]
            if neg:
                downside_var = sum(p ** 2 for p in neg) / n
                ds = math.sqrt(downside_var)
                if ds > 0:
                    sortino = mean_pnl / ds

        # Win/loss streaks
        max_consec_wins = max_consec_losses = 0
        curr_wins = curr_losses = 0
        for p in pnl_values:
            if p > 0:
                curr_wins += 1
                curr_losses = 0
                max_consec_wins = max(max_consec_wins, curr_wins)
            else:
                curr_losses += 1
                curr_wins = 0
                max_consec_losses = max(max_consec_losses, curr_losses)

        # Average win/loss
        win_vals = [p for p in pnl_values if p > 0]
        loss_vals = [p for p in pnl_values if p <= 0]
        avg_win = sum(win_vals) / len(win_vals) if win_vals else 0
        avg_loss = sum(loss_vals) / len(loss_vals) if loss_vals else 0
        largest_win = max(win_vals) if win_vals else 0
        largest_loss = min(loss_vals) if loss_vals else 0

        # Payoff ratio and expectancy
        payoff_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else None
        win_rate = wins / n
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * abs(avg_loss))

        # Profit factor
        total_wins = sum(win_vals) if win_vals else 0
        total_losses = abs(sum(loss_vals)) if loss_vals else 0
        profit_factor = total_wins / total_losses if total_losses > 0 else None

        # Max drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for p in pnl_values:
            cumulative += p
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)

        # Hold time stats
        hold_times = [t["hold_minutes"] for t in trades if t.get("hold_minutes", 0) > 0]
        avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0
        min_hold = min(hold_times) if hold_times else 0
        max_hold = max(hold_times) if hold_times else 0

        # Exit reason breakdown
        exit_counts = Counter(t["exit_reason"] for t in trades)
        exit_breakdown = {
            reason: {"count": count, "pct": round(count / n * 100, 1)}
            for reason, count in exit_counts.items()
        }

        # Options-specific metrics
        iv_entries = [t["iv"] for t in trades if t.get("iv")]
        dte_entries = [t["dte"] for t in trades if t.get("dte")]
        avg_iv = sum(iv_entries) / len(iv_entries) if iv_entries else None
        avg_dte = sum(dte_entries) / len(dte_entries) if dte_entries else None

        return {
            "total_signals": len(signals),
            "completed_trips": n,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / n * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(mean_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "largest_win": round(largest_win, 2),
            "largest_loss": round(largest_loss, 2),
            "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
            "sortino_ratio": round(sortino, 4) if sortino is not None else None,
            "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
            "payoff_ratio": round(payoff_ratio, 4) if payoff_ratio is not None else None,
            "expectancy": round(expectancy, 2),
            "max_drawdown": round(max_dd, 2),
            "max_consecutive_wins": max_consec_wins,
            "max_consecutive_losses": max_consec_losses,
            "avg_hold_minutes": round(avg_hold, 1),
            "min_hold_minutes": min_hold,
            "max_hold_minutes": max_hold,
            "exit_reason_breakdown": exit_breakdown,
            "avg_iv_at_entry": round(avg_iv, 4) if avg_iv is not None else None,
            "avg_dte_at_entry": round(avg_dte, 1) if avg_dte is not None else None,
        }

    def compute_per_symbol(self, model_name=None):
        """Compute per-symbol performance breakdown."""
        trades = [t for t in self._trades
                  if model_name is None or t["model"] == model_name]

        by_ticker = defaultdict(list)
        for t in trades:
            by_ticker[t["ticker"]].append(t)

        results = {}
        for ticker, ticker_trades in sorted(by_ticker.items()):
            n = len(ticker_trades)
            pnl_values = [t["pnl"] for t in ticker_trades]
            wins = sum(1 for p in pnl_values if p > 0)
            total_pnl = sum(pnl_values)

            # Exit type counts
            exit_counts = Counter(t["exit_reason"] for t in ticker_trades)

            # Win/loss per exit type
            exit_stats = {}
            for reason in ["atr_profit_take", "atr_floor_take", "atr_stop_loss", "eod"]:
                reason_trades = [t for t in ticker_trades if t["exit_reason"] == reason]
                if reason_trades:
                    rn = len(reason_trades)
                    rw = sum(1 for t in reason_trades if t["pnl"] > 0)
                    rpnl = sum(t["pnl"] for t in reason_trades)
                    exit_stats[reason] = {
                        "count": rn, "wins": rw,
                        "pnl": round(rpnl, 2), "avg_pnl": round(rpnl / rn, 2),
                    }

            # Hold time
            holds = [t["hold_minutes"] for t in ticker_trades if t.get("hold_minutes", 0) > 0]
            avg_hold = sum(holds) / len(holds) if holds else 0

            # Average ATR at entry
            atrs = [t["entry_atr"] for t in ticker_trades if t.get("entry_atr")]
            avg_atr = sum(atrs) / len(atrs) if atrs else 0

            # Direction breakdown
            bull_trades = [t for t in ticker_trades if t.get("direction") == "BULL"]
            bear_trades = [t for t in ticker_trades if t.get("direction") == "BEAR"]
            bull_pnl = sum(t["pnl"] for t in bull_trades)
            bear_pnl = sum(t["pnl"] for t in bear_trades)

            # Win/loss values
            win_vals = [t["pnl"] for t in ticker_trades if t["pnl"] > 0]
            loss_vals = [t["pnl"] for t in ticker_trades if t["pnl"] <= 0]
            avg_win = sum(win_vals) / len(win_vals) if win_vals else 0
            avg_loss = sum(loss_vals) / len(loss_vals) if loss_vals else 0
            largest_win = max(win_vals) if win_vals else 0
            largest_loss = min(loss_vals) if loss_vals else 0

            results[ticker] = {
                "trips": n,
                "wins": wins,
                "win_rate": round(wins / n * 100, 1),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(total_pnl / n, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "largest_win": round(largest_win, 2),
                "largest_loss": round(largest_loss, 2),
                "avg_hold_minutes": round(avg_hold, 1),
                "avg_atr": round(avg_atr, 4) if avg_atr else 0,
                "bull_trips": len(bull_trades),
                "bull_pnl": round(bull_pnl, 2),
                "bear_trips": len(bear_trades),
                "bear_pnl": round(bear_pnl, 2),
                "pt_count": exit_counts.get("atr_profit_take", 0),
                "floor_count": exit_counts.get("atr_floor_take", 0),
                "sl_count": exit_counts.get("atr_stop_loss", 0),
                "ts_count": exit_counts.get("time_stop", 0),
                "eod_count": exit_counts.get("eod", 0),
                "exit_stats": exit_stats,
            }

        return results

    def compute_per_exit_type(self, model_name=None):
        """Compute stats grouped by exit type."""
        trades = [t for t in self._trades
                  if model_name is None or t["model"] == model_name]

        by_exit = defaultdict(list)
        for t in trades:
            by_exit[t["exit_reason"]].append(t)

        results = {}
        for reason, reason_trades in by_exit.items():
            n = len(reason_trades)
            pnl_values = [t["pnl"] for t in reason_trades]
            wins = sum(1 for p in pnl_values if p > 0)
            total_pnl = sum(pnl_values)
            holds = [t["hold_minutes"] for t in reason_trades if t.get("hold_minutes", 0) > 0]

            results[reason] = {
                "count": n,
                "wins": wins,
                "win_rate": round(wins / n * 100, 1),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(total_pnl / n, 2),
                "avg_hold_minutes": round(sum(holds) / len(holds), 1) if holds else 0,
            }

        return results

    def get_trade_log(self):
        """Return full trade log sorted by entry time."""
        return sorted(self._trades, key=lambda t: t.get("entry_time") or "")

    def summary_all_models(self):
        """Compute summary for each model individually plus aggregate."""
        models = set(t["model"] for t in self._trades)
        result = {"aggregate": self.compute_summary()}
        for model in sorted(models):
            result[model] = self.compute_summary(model)
        return result
