"""Shoulder Taps QuantConnect/Lean Algorithm.

Main entry point. Composes all 5 Alpha models with indicator infrastructure,
options trading, Greeks-based exits, custom metrics, and alert notifications.

Replaces: data-collector, alert-engine, api, frontend, Redis, nginx, PostgreSQL.
"""

from AlgorithmImports import *
from datetime import timedelta
from collections import defaultdict
import json

from alpha import (
    BTDivergenceAlpha,
    DexterAlpha,
    EnsembleAAlpha,
    EnsembleBAlpha,
    EnsembleCAlpha,
)
from execution.custom_execution import ShoulderTapsExecutionModel
from tracking.metrics import TradeMetricsTracker
from tracking.forward_returns import ForwardReturnTracker
from notifications.alert_manager import AlertManager


class ShoulderTapsAlgorithm(QCAlgorithm):
    """Main algorithm: 5 Alpha models, options trading, custom metrics."""

    # --- Configuration ---
    CORE_SYMBOLS = [
        "AAPL", "MSFT", "GOOGL", "AMZN", "META",
        "NVDA", "AVGO",
        "JPM", "V", "GS",
        "UNH", "LLY",
        "COST", "HD",
        "CRM",
    ]
    SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
                   "XLP", "XLU", "XLB", "XLRE", "XLC"]
    SYMBOL_TO_SECTOR = {"MSFT": "XLK", "NVDA": "XLK", "SPY": None}

    # Options exit parameters (from backtest.py defaults)
    PREMIUM_STOP_PCT = 50.0
    PREMIUM_TARGET_PCT = 100.0
    DELTA_FLOOR = 0.10
    MIN_PREMIUM = 0.05  # $0.05/share minimum
    STRIKE_OFFSET_PCT = 2.0

    # ATR-based exits
    ATR_PROFIT_MULTIPLIER = 1.5   # Full profit target
    ATR_PROFIT_FLOOR = 0.75       # Once reached, locks in as trailing floor
    ATR_STOP_MULTIPLIER = 1.5     # Stop loss
    MAX_HOLD_MINUTES = 0          # 0 = disabled
    LAST_ENTRY_MINUTE = 945       # 15:45 ET = no new entries after (15*60+45)

    BAR_WINDOW_MAX = 100

    # ------------------------------------------------------------------ #
    # Initialize
    # ------------------------------------------------------------------ #
    def Initialize(self):
        # --- Test email notification (remove after confirming) ---
        email = self.GetParameter("alert_email")
        if email and self.LiveMode:
            self.Notify.Email(email, "ShoulderTaps: Test Alert", "Email alerting is working.")

        # --- Backtest window (configurable via parameters) ---
        start_year = int(self.GetParameter("start_year") or 2024)
        start_month = int(self.GetParameter("start_month") or 1)
        end_year = int(self.GetParameter("end_year") or 2024)
        end_month = int(self.GetParameter("end_month") or 7)
        cash = int(self.GetParameter("cash") or 100000)
        self._trading_mode = self.GetParameter("trading_mode") or "options"

        # Override ATR exit params from config (for grid testing)
        pt = self.GetParameter("atr_pt")
        if pt: self.ATR_PROFIT_MULTIPLIER = float(pt)
        fl = self.GetParameter("atr_floor")
        if fl: self.ATR_PROFIT_FLOOR = float(fl)
        sl = self.GetParameter("atr_sl")
        if sl: self.ATR_STOP_MULTIPLIER = float(sl)

        self.SetStartDate(start_year, start_month, 1)
        self.SetEndDate(end_year, end_month, 1)
        self.SetCash(cash)
        self.SetTimeZone("America/New_York")

        # --- Data storage ---
        self._bar_windows = defaultdict(lambda: defaultdict(list))
        self._indicators = defaultdict(lambda: defaultdict(dict))
        self._consolidators = defaultdict(dict)

        # Time-of-day volume tracking for RVOL TOD
        # Structure: {ticker: {tf_label: {minute_bucket: [volumes]}}}
        self._tod_volumes = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

        # VIX and sector state
        self._vix_regime = "normal"
        self._vix_close = 20.0
        self._risk_free_rate = 0.05
        self._sector_pct_change = {}  # sector_ticker -> pct_change
        self._sector_day_open = {}    # sector_ticker -> day's open price

        # --- Core equities (Resolution.Minute for 1m raw bars) ---
        self._equity_handles = {}
        for ticker in self.CORE_SYMBOLS:
            equity = self.AddEquity(ticker, Resolution.Second)
            equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
            self._equity_handles[ticker] = equity.Symbol

        # --- Consolidators + indicators per symbol/timeframe ---
        # Dexter only needs 10m bars, but we set up all TFs for flexibility
        for ticker in self.CORE_SYMBOLS:
            symbol = self._equity_handles[ticker]
            for tf_label, minutes in [("5m", 5), ("10m", 10), ("15m", 15)]:
                self._setup_consolidator(symbol, ticker, tf_label, minutes)

        # --- Options chains (Phase 3) ---
        if self._trading_mode == "options":
            self._setup_options()

        # --- Slippage and fees ---
        slippage = 0.02 if self._trading_mode == "options" else 0.0005
        for ticker in self.CORE_SYMBOLS:
            sym = self._equity_handles[ticker]
            self.Securities[sym].SetSlippageModel(
                ConstantSlippageModel(slippage)
            )
            self.Securities[sym].SetFeeModel(ConstantFeeModel(1.0))

        # --- Alpha models ---
        self._alpha_models = [
            DexterAlpha(),
        ]

        # --- Tracking and notifications ---
        self._metrics = TradeMetricsTracker()
        self._forward_tracker = ForwardReturnTracker()
        self._alert_manager = AlertManager(self)

        # --- Position tracking ---
        self._option_entries = {}  # symbol -> {"entry_price": float, "model": str}
        self._equity_entries = {}  # symbol -> {"entry_price": float, "model": str, "direction": str}

        # --- EOD close schedule ---
        self.Schedule.On(
            self.DateRules.EveryDay("AAPL"),
            self.TimeRules.BeforeMarketClose("AAPL", 1),
            self._liquidate_eod,
        )

        # --- Forward return update schedule ---
        self.Schedule.On(
            self.DateRules.EveryDay("AAPL"),
            self.TimeRules.Every(timedelta(minutes=5)),
            self._update_forward_returns,
        )

        # --- End of backtest persistence ---
        self.Schedule.On(
            self.DateRules.On(end_year, end_month, 1),
            self.TimeRules.Midnight,
            self._persist_results,
        )

        # --- Warm up ---
        # 10 trading days covers SMA50 on 10m bars (50 bars = ~13 trading days).
        # TOD RVOL gate (40-day spec) self-regulates: calc_rvol_tod requires >=5
        # entries per slot, so no signals fire until enough TOD data accumulates.
        self.SetWarmUp(timedelta(days=10))

        self.Debug(f"[INIT] ShoulderTaps initialized | mode={self._trading_mode} "
                   f"| symbols={self.CORE_SYMBOLS}")

    # ------------------------------------------------------------------ #
    # Consolidator + Indicator Setup
    # ------------------------------------------------------------------ #
    def _setup_consolidator(self, symbol, ticker, tf_label, minutes):
        """Create a TradeBarConsolidator and register 12 indicators."""
        consolidator = TradeBarConsolidator(timedelta(minutes=minutes))
        consolidator.DataConsolidated += (
            lambda sender, bar, t=ticker, tf=tf_label:
            self._on_consolidated_bar(t, tf, bar)
        )
        self.SubscriptionManager.AddConsolidator(symbol, consolidator)
        self._consolidators[ticker][tf_label] = consolidator

        inds = self._create_indicator_set(symbol, f"{ticker}_{tf_label}")
        for name, ind in inds.items():
            self.RegisterIndicator(symbol, ind, consolidator)
        self._indicators[ticker][tf_label] = inds

    def _create_indicator_set(self, symbol, prefix):
        """Create the standard set of 12 indicators."""
        return {
            "sma_5": SimpleMovingAverage(f"{prefix}_SMA5", 5),
            "sma_20": SimpleMovingAverage(f"{prefix}_SMA20", 20),
            "sma_50": SimpleMovingAverage(f"{prefix}_SMA50", 50),
            "ema_12": ExponentialMovingAverage(f"{prefix}_EMA12", 12),
            "ema_26": ExponentialMovingAverage(f"{prefix}_EMA26", 26),
            "rsi_14": RelativeStrengthIndex(f"{prefix}_RSI14", 14),
            "macd": MovingAverageConvergenceDivergence(f"{prefix}_MACD", 12, 26, 9),
            "atr_14": AverageTrueRange(f"{prefix}_ATR14", 14),
            "stoch": Stochastic(f"{prefix}_STOCH", 14, 3, 3),
            "adx_14": AverageDirectionalIndex(f"{prefix}_ADX14", 14),
            "obv": OnBalanceVolume(f"{prefix}_OBV"),
            "roc_10": RateOfChange(f"{prefix}_ROC10", 10),
        }

    def _on_consolidated_bar(self, ticker, tf_label, bar):
        """Build candle dict from consolidated bar + indicator snapshots."""
        inds = self._indicators[ticker][tf_label]
        candle = self._build_candle_dict(bar, inds)

        window = self._bar_windows[ticker][tf_label]
        window.append(candle)
        if len(window) > self.BAR_WINDOW_MAX:
            window.pop(0)

        # Accumulate TOD volume history (minutes since midnight as bucket key)
        minute_bucket = bar.EndTime.hour * 60 + bar.EndTime.minute
        tod_list = self._tod_volumes[ticker][tf_label][minute_bucket]
        tod_list.append(float(bar.Volume))
        if len(tod_list) > 30:  # 30-day baseline (yfinance 1m data limit)
            tod_list.pop(0)                # spec calls for 40-day; requires QC Security Master subscription

    def _on_vix_bar(self, sender, bar):
        """Accumulate VIX 5m bars for Ensemble A VIX slope gate."""
        candle = {
            "time": bar.EndTime,
            "open": float(bar.Open),
            "high": float(bar.High),
            "low": float(bar.Low),
            "close": float(bar.Close),
            "volume": float(bar.Volume),
        }
        window = self._bar_windows["VIX"]["5m"]
        window.append(candle)
        if len(window) > self.BAR_WINDOW_MAX:
            window.pop(0)

        # Update VIX regime
        self._vix_close = float(bar.Close)
        self._classify_vix_regime(self._vix_close)

    def _build_candle_dict(self, bar, inds):
        """Build a candle dict with all indicator values."""
        def _val(ind):
            return float(ind.Current.Value) if ind.IsReady else None

        candle = {
            "time": bar.EndTime,
            "open": float(bar.Open),
            "high": float(bar.High),
            "low": float(bar.Low),
            "close": float(bar.Close),
            "volume": float(bar.Volume),
            "sma_5": _val(inds["sma_5"]),
            "sma_20": _val(inds["sma_20"]),
            "sma_50": _val(inds["sma_50"]),
            "ema_12": _val(inds["ema_12"]),
            "ema_26": _val(inds["ema_26"]),
            "rsi_14": _val(inds["rsi_14"]),
            "macd": _val(inds["macd"]),
            "macd_signal": (
                float(inds["macd"].Signal.Current.Value)
                if inds["macd"].IsReady else None
            ),
            "macd_hist": (
                float(inds["macd"].Histogram.Current.Value)
                if inds["macd"].IsReady else None
            ),
            "atr_14": _val(inds["atr_14"]),
            "stoch_k": (
                float(inds["stoch"].StochK.Current.Value)
                if inds["stoch"].IsReady else None
            ),
            "stoch_d": (
                float(inds["stoch"].StochD.Current.Value)
                if inds["stoch"].IsReady else None
            ),
            "adx_14": _val(inds["adx_14"]),
            "dmp_14": (
                float(inds["adx_14"].PositiveDirectionalIndex.Current.Value)
                if inds["adx_14"].IsReady else None
            ),
            "dmn_14": (
                float(inds["adx_14"].NegativeDirectionalIndex.Current.Value)
                if inds["adx_14"].IsReady else None
            ),
            "obv": _val(inds["obv"]),
            "roc_10": _val(inds["roc_10"]),
        }

        # VWAP: compute from daily cumulative TP * Volume / Volume
        # Lean's IntradayVwap requires special handling; we use a simple approach
        tp = (candle["high"] + candle["low"] + candle["close"]) / 3
        candle["vwap"] = tp  # Simplified; full VWAP tracked via consolidator state

        return candle

    # ------------------------------------------------------------------ #
    # Market State
    # ------------------------------------------------------------------ #
    def _classify_vix_regime(self, vix_close):
        """VIX regime classification matching data-collector logic."""
        if vix_close < 15:
            self._vix_regime = "low_vol"
        elif vix_close < 20:
            self._vix_regime = "normal"
        elif vix_close < 25:
            self._vix_regime = "elevated"
        else:
            self._vix_regime = "high_vol"

    def get_market_state(self):
        """Build a market_state dict compatible with evaluator signatures."""
        return {
            "vix": self._vix_close,
            "market_regime": self._vix_regime,
            "risk_free_rate": self._risk_free_rate,
        }

    def get_tod_volumes(self, ticker, tf_label, bar_time):
        """Get historical volumes for the same time-of-day bucket.

        Returns list of past volumes at this time slot, or None.
        """
        minute_bucket = bar_time.hour * 60 + bar_time.minute
        vols = self._tod_volumes.get(ticker, {}).get(tf_label, {}).get(minute_bucket)
        if vols and len(vols) >= 2:
            # Exclude the most recent entry (it's the current bar we're evaluating)
            return vols[:-1]
        return None

    # ------------------------------------------------------------------ #
    # Options Setup (Phase 3)
    # ------------------------------------------------------------------ #
    def _setup_options(self):
        """Add option chain subscriptions for core symbols."""
        for ticker in self.CORE_SYMBOLS:
            symbol = self._equity_handles[ticker]
            option = self.AddOption(ticker)
            option.SetFilter(lambda u: u.Strikes(-5, 5).Expiration(0, 30))
            option.PriceModel = OptionPriceModels.BinomialCoxRossRubinstein()

    def _select_option_contract(self, equity_symbol, direction,
                                strike_offset_pct=None):
        """Select an option contract for the given signal direction.

        Returns the option Symbol or None if no suitable contract found.
        """
        if strike_offset_pct is None:
            strike_offset_pct = self.STRIKE_OFFSET_PCT

        ticker = str(equity_symbol)
        # Get the canonical equity symbol
        if ticker in self._equity_handles:
            equity_symbol = self._equity_handles[ticker]

        underlying_price = self.Securities[equity_symbol].Price
        if underlying_price <= 0:
            return None

        if direction == "BULL":
            target_strike = underlying_price * (1 + strike_offset_pct / 100)
            right = OptionRight.Call
        else:
            target_strike = underlying_price * (1 - strike_offset_pct / 100)
            right = OptionRight.Put

        # Get option chain
        chain = self.OptionChainProvider.GetOptionContractList(
            equity_symbol, self.Time
        )
        if not chain:
            return None

        # Filter: correct right, 7-30 DTE
        candidates = []
        for contract in chain:
            if contract.ID.OptionRight != right:
                continue
            dte = (contract.ID.Date - self.Time).days
            if not (7 <= dte <= 30):
                continue
            candidates.append(contract)

        if not candidates:
            return None

        # Select nearest strike to target
        best = min(candidates, key=lambda c: abs(c.ID.StrikePrice - target_strike))

        # Check minimum premium
        if best in self.Securities:
            price = self.Securities[best].Price
            if price < self.MIN_PREMIUM:
                return None

        return best

    # ------------------------------------------------------------------ #
    # OnData — main loop
    # ------------------------------------------------------------------ #
    def OnData(self, data):
        if self.IsWarmingUp:
            return

        # Run Alpha models
        for alpha in self._alpha_models:
            insights = alpha.Update(self, data)
            for insight in insights:
                self._on_insight(insight)

        # Check Dexter invalidations (close back inside consolidation range)
        # NOTE: Disabled — 8.7% WR in testing, too aggressive for short intraday holds.
        # Profit take + EOD close is the better exit combination.
        # for alpha in self._alpha_models:
        #     if hasattr(alpha, 'check_invalidations'):
        #         invalidated = alpha.check_invalidations(self)
        #         for ticker, direction in invalidated:
        #             symbol = self._equity_handles.get(ticker)
        #             if symbol and symbol in self._equity_entries:
        #                 entry_info = self._equity_entries.pop(symbol)
        #                 pnl = float(self.Portfolio[symbol].UnrealizedProfit) if self.Portfolio[symbol].Invested else 0
        #                 self._metrics.record_trade_close(
        #                     symbol=symbol, pnl=pnl,
        #                     exit_reason="invalidation",
        #                     model_name=entry_info.get("model", "unknown"),
        #                 )
        #                 self.Liquidate(symbol, tag="invalidation")
        #                 self.Debug(f"[INVALIDATION] {ticker} {direction} closed back inside range | P&L={pnl:.2f}")

        # Check ATR-based profit takes
        if self._trading_mode == "equity":
            self._check_atr_profit_takes()

        # Check options exits
        if self._trading_mode == "options":
            self._check_options_exits()

    def _update_sector_etfs(self, data):
        """Track intraday sector ETF % change for Ensemble A gate."""
        for ticker, symbol in self._sector_handles.items():
            if symbol in data.Bars:
                bar = data.Bars[symbol]
                close = float(bar.Close)

                # Track day's open
                if ticker not in self._sector_day_open or bar.Time.date() != getattr(
                    self, '_sector_last_date', {}).get(ticker):
                    self._sector_day_open[ticker] = float(bar.Open)
                    if not hasattr(self, '_sector_last_date'):
                        self._sector_last_date = {}
                    self._sector_last_date[ticker] = bar.Time.date()

                day_open = self._sector_day_open.get(ticker)
                if day_open and day_open > 0:
                    self._sector_pct_change[ticker] = (
                        (close - day_open) / day_open * 100
                    )

    def _on_insight(self, insight):
        """Handle a new Insight from an Alpha model."""
        symbol = insight.Symbol
        direction = "BULL" if insight.Direction == InsightDirection.Up else "BEAR"
        model = insight.SourceModel or "unknown"
        strength = (insight.Confidence or 0.5) * 100

        # Record signal in metrics tracker
        self._metrics.record_signal(model, symbol, direction, strength, self.Time)

        # Execute trade
        if self._trading_mode == "equity":
            # Skip if already in a position for this symbol
            if symbol in self._equity_entries:
                return
            # Block new entries after LAST_ENTRY_MINUTE (not enough time to resolve before EOD)
            current_minute = self.Time.hour * 60 + self.Time.minute
            if current_minute >= self.LAST_ENTRY_MINUTE:
                return
            # Check buying power before ordering
            price = self.Securities[symbol].Price
            required = 100 * price
            if required > self.Portfolio.MarginRemaining * 0.9:
                self.Debug(f"[SKIP] Insufficient margin for {symbol}: need {required:.0f}")
                return
            shares = 100 if direction == "BULL" else -100
            self.MarketOrder(symbol, shares, tag=f"{model}_{direction}")

            # Get current ATR for profit target
            ticker_str = str(symbol).split(" ")[0] if " " in str(symbol) else str(symbol)
            entry_atr = None
            for t, handle in self._equity_handles.items():
                if handle == symbol:
                    ticker_str = t
                    break
            window = self._bar_windows.get(ticker_str, {}).get("10m", [])
            if window and window[-1].get("atr_14") is not None:
                entry_atr = float(window[-1]["atr_14"])

            self._equity_entries[symbol] = {
                "entry_price": price,
                "model": model,
                "direction": direction,
                "entry_time": self.Time,
                "entry_atr": entry_atr,
                "floor_hit": False,
            }
        else:
            contract = self._select_option_contract(symbol, direction)
            if contract is not None:
                if contract not in self.Securities:
                    self.AddOptionContract(contract)
                self.MarketOrder(contract, 1, tag=f"{model}_{direction}")
                self._option_entries[contract] = {
                    "entry_price": self.Securities[contract].Price
                    if contract in self.Securities else 0,
                    "model": model,
                    "direction": direction,
                    "entry_time": self.Time,
                }

    # ------------------------------------------------------------------ #
    # Options Exit Logic (Phase 3)
    # ------------------------------------------------------------------ #
    def _check_atr_profit_takes(self):
        """Exit equity positions that have reached ATR-based profit target.

        Target = entry_price +/- (ATR at entry * ATR_PROFIT_MULTIPLIER).
        """
        for symbol in list(self._equity_entries.keys()):
            entry_info = self._equity_entries[symbol]
            entry_atr = entry_info.get("entry_atr")
            if entry_atr is None or entry_atr <= 0:
                continue

            if not self.Portfolio[symbol].Invested:
                continue

            current_price = self.Securities[symbol].Price
            entry_price = entry_info["entry_price"]
            direction = entry_info["direction"]
            profit_target = entry_atr * self.ATR_PROFIT_MULTIPLIER
            floor_level = entry_atr * self.ATR_PROFIT_FLOOR
            stop_target = entry_atr * self.ATR_STOP_MULTIPLIER

            # Calculate current favorable excursion
            if direction == "BULL":
                gain = current_price - entry_price
                loss = entry_price - current_price
            else:
                gain = entry_price - current_price
                loss = current_price - entry_price

            # Track whether price has breached the floor level
            if gain >= floor_level:
                entry_info["floor_hit"] = True

            # Check full profit target
            profit_hit = gain >= profit_target

            # Check trailing floor: breached 1.0x ATR but retraced back to it
            floor_exit = False
            if entry_info["floor_hit"] and not profit_hit and gain <= floor_level:
                floor_exit = True

            # Check stop loss
            stop_hit = loss >= stop_target

            # Check max hold time
            time_exit = False
            entry_time = entry_info.get("entry_time")
            if entry_time and self.MAX_HOLD_MINUTES > 0:
                hold_minutes = (self.Time - entry_time).total_seconds() / 60
                if hold_minutes >= self.MAX_HOLD_MINUTES:
                    time_exit = True

            if profit_hit or floor_exit or stop_hit or time_exit:
                if profit_hit:
                    exit_reason = "atr_profit_take"
                elif floor_exit:
                    exit_reason = "atr_floor_take"
                elif stop_hit:
                    exit_reason = "atr_stop_loss"
                else:
                    exit_reason = "time_stop"

                pnl = float(self.Portfolio[symbol].UnrealizedProfit)
                model = entry_info.get("model", "unknown")

                ticker_str = "?"
                for t, h in self._equity_handles.items():
                    if h == symbol:
                        ticker_str = t
                        break

                self._equity_entries.pop(symbol)
                self._metrics.record_trade_close(
                    symbol=symbol, pnl=pnl,
                    exit_reason=exit_reason,
                    model_name=model,
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=current_price,
                    entry_atr=entry_atr,
                    entry_time=entry_info.get("entry_time"),
                    exit_time=self.Time,
                    ticker=ticker_str,
                )
                self.Liquidate(symbol, tag=exit_reason)

                # Clear Dexter channel tracking
                for alpha in self._alpha_models:
                    if hasattr(alpha, 'clear_channel'):
                        for t2, h in self._equity_handles.items():
                            if h == symbol:
                                alpha.clear_channel(t2)
                                break

                if profit_hit:
                    self.Debug(
                        f"[ATR_PROFIT] {ticker_str} {direction} hit {self.ATR_PROFIT_MULTIPLIER}x ATR "
                        f"target=${profit_target:.2f} | P&L={pnl:.2f}"
                    )
                elif floor_exit:
                    self.Debug(
                        f"[ATR_FLOOR] {ticker_str} {direction} retraced to {self.ATR_PROFIT_FLOOR}x ATR "
                        f"floor=${floor_level:.2f} | P&L={pnl:.2f}"
                    )
                elif time_exit:
                    hold_min = int((self.Time - entry_time).total_seconds() / 60)
                    self.Debug(
                        f"[TIME_STOP] {ticker_str} {direction} held {hold_min}m "
                        f"(max {self.MAX_HOLD_MINUTES}m) | P&L={pnl:.2f}"
                    )
                else:
                    self.Debug(
                        f"[ATR_STOP] {ticker_str} {direction} hit {self.ATR_STOP_MULTIPLIER}x ATR "
                        f"stop=${stop_target:.2f} | P&L={pnl:.2f}"
                    )

    def _check_options_exits(self):
        """Greeks-based exit hierarchy for option positions.

        Priority: premium_stop > premium_target > delta_floor > price SL/TP
        Ported from backtest.py lines 427-529.
        """
        for holding in list(self.Portfolio.Values):
            if not holding.Invested:
                continue
            if holding.Symbol.SecurityType != SecurityType.Option:
                continue

            option_symbol = holding.Symbol
            entry_info = self._option_entries.get(option_symbol, {})
            entry_price = entry_info.get("entry_price", 0)

            if entry_price <= 0:
                continue

            current_price = holding.Price
            if current_price <= 0:
                continue

            prem_change_pct = (current_price - entry_price) / entry_price * 100

            # Premium stop
            if prem_change_pct <= -self.PREMIUM_STOP_PCT:
                self._close_option(option_symbol, "premium_stop")
                continue

            # Premium target
            if prem_change_pct >= self.PREMIUM_TARGET_PCT:
                self._close_option(option_symbol, "premium_target")
                continue

            # Delta floor
            if option_symbol in self.Securities:
                option_sec = self.Securities[option_symbol]
            else:
                continue
            if hasattr(option_sec, 'Greeks') and option_sec.Greeks:
                delta = option_sec.Greeks.Delta
                if abs(delta) < self.DELTA_FLOOR:
                    self._close_option(option_symbol, "delta_floor")
                    continue

    def _close_option(self, option_symbol, exit_reason):
        """Close an option position and record metrics."""
        entry_info = self._option_entries.pop(option_symbol, {})
        model = entry_info.get("model", "unknown")

        if not self.Portfolio.ContainsKey(option_symbol):
            return
        holding = self.Portfolio[option_symbol]
        if holding.Invested:
            pnl = holding.UnrealizedProfit
            self._metrics.record_trade_close(
                symbol=option_symbol,
                pnl=float(pnl),
                exit_reason=exit_reason,
                model_name=model,
                entry_premium=entry_info.get("entry_price", 0),
                exit_premium=holding.Price if holding else 0,
            )
            self.Liquidate(option_symbol, tag=exit_reason)
            self.Debug(
                f"[EXIT] {exit_reason} | {option_symbol} | "
                f"P&L={pnl:.2f} | model={model}"
            )

    def _liquidate_eod(self):
        """Close all positions at end of day."""
        for holding in list(self.Portfolio.Values):
            if holding.Invested:
                symbol = holding.Symbol
                pnl = float(holding.UnrealizedProfit)

                if symbol.SecurityType == SecurityType.Option:
                    entry_info = self._option_entries.pop(symbol, {})
                    model = entry_info.get("model", "unknown")
                    self._metrics.record_trade_close(
                        symbol=symbol,
                        pnl=pnl,
                        exit_reason="eod",
                        model_name=model,
                        entry_premium=entry_info.get("entry_price", 0),
                        exit_premium=holding.Price,
                    )
                elif symbol.SecurityType == SecurityType.Equity:
                    entry_info = self._equity_entries.pop(symbol, {})
                    model = entry_info.get("model", "unknown")
                    ticker_str = "?"
                    for t, h in self._equity_handles.items():
                        if h == symbol:
                            ticker_str = t
                            break
                    self._metrics.record_trade_close(
                        symbol=symbol,
                        pnl=pnl,
                        exit_reason="eod",
                        model_name=model,
                        direction=entry_info.get("direction"),
                        entry_price=entry_info.get("entry_price", 0),
                        exit_price=self.Securities[symbol].Price,
                        entry_atr=entry_info.get("entry_atr", 0),
                        entry_time=entry_info.get("entry_time"),
                        exit_time=self.Time,
                        ticker=ticker_str,
                    )

                self.Liquidate(symbol, tag="eod")

        # Clear all Dexter channel tracking at EOD
        for alpha in self._alpha_models:
            if hasattr(alpha, '_active_channels'):
                alpha._active_channels.clear()

    # ------------------------------------------------------------------ #
    # Order Events
    # ------------------------------------------------------------------ #
    def OnOrderEvent(self, orderEvent):
        """Track order fills for equity mode metrics."""
        if orderEvent.Status != OrderStatus.Filled:
            return

        symbol = orderEvent.Symbol
        order = self.Transactions.GetOrderById(orderEvent.OrderId)
        tag = order.Tag if order else ""

        self.Debug(
            f"[ORDER] {symbol} filled | qty={orderEvent.FillQuantity} "
            f"price={orderEvent.FillPrice:.2f} | tag={tag}"
        )

    # ------------------------------------------------------------------ #
    # Scheduled Tasks
    # ------------------------------------------------------------------ #
    def _update_forward_returns(self):
        """Periodically update forward return tracker."""
        if self._forward_tracker:
            self._forward_tracker.update()

    def _persist_results(self):
        """Persist all results at end of backtest."""
        # Alert history
        self._alert_manager.persist()

        # Trade metrics
        summary = self._metrics.summary_all_models()
        try:
            self.ObjectStore.Save(
                "shoulder_taps/metrics_summary",
                json.dumps(summary, default=str),
            )
        except Exception as e:
            self.Debug(f"[PERSIST] Metrics save failed: {e}")

        # Forward returns
        completed = self._forward_tracker.get_completed()
        try:
            self.ObjectStore.Save(
                "shoulder_taps/forward_returns",
                json.dumps(completed, default=str),
            )
        except Exception as e:
            self.Debug(f"[PERSIST] Forward returns save failed: {e}")

        # ============================================================
        # OVERALL SUMMARY
        # ============================================================
        self.Debug("=" * 80)
        self.Debug("SHOULDER TAPS BACKTEST COMPLETE")
        self.Debug("=" * 80)
        agg = summary.get("aggregate", {})
        self.Debug("")
        self.Debug("--- OVERALL STATS ---")
        self.Debug(f"  Signals:          {agg.get('total_signals', 0)}")
        self.Debug(f"  Completed Trips:  {agg.get('completed_trips', 0)}")
        self.Debug(f"  Win Rate:         {agg.get('win_rate', 0)}%")
        self.Debug(f"  Total P&L:        ${agg.get('total_pnl', 0):.2f}")
        self.Debug(f"  Avg P&L/Trade:    ${agg.get('avg_pnl', 0):.2f}")
        self.Debug(f"  Avg Win:          ${agg.get('avg_win', 0):.2f}")
        self.Debug(f"  Avg Loss:         ${agg.get('avg_loss', 0):.2f}")
        self.Debug(f"  Largest Win:      ${agg.get('largest_win', 0):.2f}")
        self.Debug(f"  Largest Loss:     ${agg.get('largest_loss', 0):.2f}")
        self.Debug(f"  Profit Factor:    {agg.get('profit_factor', 'N/A')}")
        self.Debug(f"  Payoff Ratio:     {agg.get('payoff_ratio', 'N/A')}")
        self.Debug(f"  Expectancy:       ${agg.get('expectancy', 0):.2f}")
        self.Debug(f"  Sharpe:           {agg.get('sharpe_ratio', 'N/A')}")
        self.Debug(f"  Sortino:          {agg.get('sortino_ratio', 'N/A')}")
        self.Debug(f"  Max Drawdown:     ${agg.get('max_drawdown', 0):.2f}")
        self.Debug(f"  Max Win Streak:   {agg.get('max_consecutive_wins', 0)}")
        self.Debug(f"  Max Loss Streak:  {agg.get('max_consecutive_losses', 0)}")
        self.Debug(f"  Avg Hold Time:    {agg.get('avg_hold_minutes', 0):.0f} min")
        self.Debug(f"  Min Hold Time:    {agg.get('min_hold_minutes', 0)} min")
        self.Debug(f"  Max Hold Time:    {agg.get('max_hold_minutes', 0)} min")
        self.Debug(f"  Alerts Fired:     {self._alert_manager.get_alert_count()}")
        self.Debug(
            f"  Forward Returns:  {len(completed)} completed, "
            f"{self._forward_tracker.get_pending_count()} pending"
        )

        # ============================================================
        # PER-MODEL SUMMARY
        # ============================================================
        self.Debug("")
        self.Debug("--- PER-MODEL ---")
        for model_name, model_summary in summary.items():
            if model_name == "aggregate":
                continue
            self.Debug(
                f"  [{model_name}] signals={model_summary.get('total_signals', 0)} "
                f"trips={model_summary.get('completed_trips', 0)} "
                f"WR={model_summary.get('win_rate', 0)}% "
                f"P&L=${model_summary.get('total_pnl', 0):.2f} "
                f"PF={model_summary.get('profit_factor', 'N/A')} "
                f"exp=${model_summary.get('expectancy', 0):.2f}"
            )

        # ============================================================
        # EXIT TYPE BREAKDOWN
        # ============================================================
        self.Debug("")
        self.Debug("--- EXIT TYPE BREAKDOWN ---")
        exit_stats = self._metrics.compute_per_exit_type()
        exit_order = ["atr_profit_take", "atr_floor_take", "atr_stop_loss", "time_stop", "eod"]
        exit_labels = {
            "atr_profit_take": "Profit Take",
            "atr_floor_take": "Floor Take ",
            "atr_stop_loss":  "Stop Loss  ",
            "time_stop":      "Time Stop  ",
            "eod":            "EOD Close  ",
        }
        self.Debug(f"  {'Type':<14} {'Trips':>5} {'WR':>6} {'P&L':>10} {'Avg P&L':>10} {'Avg Hold':>10}")
        self.Debug(f"  {'-'*14} {'-'*5} {'-'*6} {'-'*10} {'-'*10} {'-'*10}")
        for reason in exit_order:
            if reason in exit_stats:
                es = exit_stats[reason]
                label = exit_labels.get(reason, reason)
                self.Debug(
                    f"  {label:<14} {es['count']:>5} {es['win_rate']:>5.1f}% "
                    f"${es['total_pnl']:>9.2f} ${es['avg_pnl']:>9.2f} "
                    f"{es['avg_hold_minutes']:>8.0f}m"
                )

        # ============================================================
        # PER-SYMBOL PERFORMANCE
        # ============================================================
        self.Debug("")
        self.Debug("--- PER-SYMBOL PERFORMANCE (sorted by P&L) ---")
        symbol_stats = self._metrics.compute_per_symbol()
        sorted_symbols = sorted(symbol_stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True)

        self.Debug(
            f"  {'Ticker':<6} {'Trips':>5} {'WR':>6} {'P&L':>10} {'AvgP&L':>8} "
            f"{'AvgWin':>8} {'AvgLoss':>8} {'Best':>8} {'Worst':>8} "
            f"{'Hold':>6} {'PT':>3} {'FL':>3} {'SL':>3} {'TS':>3} {'EOD':>3} "
            f"{'Bull':>5} {'Bear':>5}"
        )
        self.Debug(
            f"  {'-'*6} {'-'*5} {'-'*6} {'-'*10} {'-'*8} "
            f"{'-'*8} {'-'*8} {'-'*8} {'-'*8} "
            f"{'-'*6} {'-'*3} {'-'*3} {'-'*3} {'-'*3} {'-'*3} "
            f"{'-'*5} {'-'*5}"
        )
        for ticker, ss in sorted_symbols:
            self.Debug(
                f"  {ticker:<6} {ss['trips']:>5} {ss['win_rate']:>5.1f}% "
                f"${ss['total_pnl']:>9.2f} ${ss['avg_pnl']:>7.2f} "
                f"${ss['avg_win']:>7.2f} ${ss['avg_loss']:>7.2f} "
                f"${ss['largest_win']:>7.2f} ${ss['largest_loss']:>7.2f} "
                f"{ss['avg_hold_minutes']:>4.0f}m "
                f"{ss['pt_count']:>3} {ss['floor_count']:>3} {ss['sl_count']:>3} {ss['ts_count']:>3} {ss['eod_count']:>3} "
                f"{ss['bull_trips']:>5} {ss['bear_trips']:>5}"
            )

        # ============================================================
        # TRADE LOG
        # ============================================================
        self.Debug("")
        self.Debug("--- TRADE LOG ---")
        trade_log = self._metrics.get_trade_log()
        self.Debug(
            f"  {'#':>3} {'Ticker':<6} {'Dir':<5} {'Entry':>10} {'Exit':>10} "
            f"{'P&L':>10} {'ATR':>6} {'Hold':>6} {'Exit Type':<16}"
        )
        self.Debug(
            f"  {'-'*3} {'-'*6} {'-'*5} {'-'*10} {'-'*10} "
            f"{'-'*10} {'-'*6} {'-'*6} {'-'*16}"
        )
        for i, trade in enumerate(trade_log, 1):
            exit_label = trade["exit_reason"].replace("atr_", "").replace("_", " ")
            self.Debug(
                f"  {i:>3} {trade['ticker']:<6} {(trade.get('direction') or '?'):<5} "
                f"${trade.get('entry_price', 0):>9.2f} ${trade.get('exit_price', 0):>9.2f} "
                f"${trade['pnl']:>9.2f} ${trade.get('entry_atr', 0):>5.2f} "
                f"{trade.get('hold_minutes', 0):>4}m "
                f"{exit_label:<16}"
            )

    # ------------------------------------------------------------------ #
    # End of Algorithm
    # ------------------------------------------------------------------ #
    def OnEndOfAlgorithm(self):
        """Final cleanup and persistence."""
        self._persist_results()
