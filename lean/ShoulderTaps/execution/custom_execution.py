"""Custom execution model for Shoulder Taps.

Translates Lean Insights into option or equity orders depending on trading mode.
Replaces the order generation logic from backtest.py.
"""

from AlgorithmImports import *


class ShoulderTapsExecutionModel(ExecutionModel):
    """Execute on Insight signals by opening option or equity positions."""

    EQUITY_SHARES = 100

    def __init__(self, trading_mode="options"):
        """
        Args:
            trading_mode: "options" or "equity"
        """
        self._trading_mode = trading_mode
        self._pending_insights = []

    def Execute(self, algorithm, targets):
        """Convert portfolio targets from Insights into actual market orders."""
        for target in targets:
            if target.Quantity == 0:
                continue

            symbol = target.Symbol
            direction = "BULL" if target.Quantity > 0 else "BEAR"

            if self._trading_mode == "equity":
                self._execute_equity(algorithm, symbol, direction)
            else:
                self._execute_option(algorithm, symbol, direction)

    def _execute_equity(self, algorithm, symbol, direction):
        """Open an equity position: 100 shares long or short."""
        shares = self.EQUITY_SHARES if direction == "BULL" else -self.EQUITY_SHARES
        tag = f"equity_{direction}"
        algorithm.MarketOrder(symbol, shares, tag=tag)
        algorithm.Debug(f"[EXEC] Equity {direction} {self.EQUITY_SHARES} shares of {symbol}")

    def _execute_option(self, algorithm, symbol, direction):
        """Open an option position using the algorithm's contract selector."""
        if not hasattr(algorithm, '_select_option_contract'):
            algorithm.Debug(f"[EXEC] Option execution unavailable — no contract selector")
            return

        contract = algorithm._select_option_contract(symbol, direction)
        if contract is None:
            algorithm.Debug(f"[EXEC] No suitable option contract found for {symbol} {direction}")
            return

        # Add contract to universe if needed
        option_symbol = contract
        if option_symbol not in algorithm.Securities:
            algorithm.AddOptionContract(option_symbol)

        algorithm.MarketOrder(option_symbol, 1, tag=f"option_{direction}")
        algorithm.Debug(
            f"[EXEC] Option {direction} 1 contract of {option_symbol} "
            f"(strike={option_symbol.ID.StrikePrice}, "
            f"exp={option_symbol.ID.Date.strftime('%Y-%m-%d')})"
        )
