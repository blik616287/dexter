#!/usr/bin/env python3
"""Real-time DEXTER (Shoulder Tap 2) alert monitor.

Ports the LEAN DexterAlpha evaluator to run against live data:
    - Seeds with 60 days of 15m bars from yfinance (TOD RVOL baseline)
    - Computes SMA(20), SMA(50), ATR(14) via pandas_ta
    - Streams real-time trades from Finnhub, aggregates into 15m bars
    - Evaluates the 5-gate Dexter signal on each completed bar
    - Re-seeds history daily at market open (09:30 ET)
    - Posts alerts to a webhook endpoint with X-API-Key auth

Usage::

    # Single symbol (preferred)
    monitor = DexterAlertMonitor(symbol="AAPL")
    monitor.run()

    # Multiple instances in separate threads/processes
    aapl = DexterAlertMonitor(symbol="AAPL")
    msft = DexterAlertMonitor(symbol="MSFT")

CLI::

    python3 dexter_alert.py AAPL [-v]
    python3 dexter_alert.py MSFT [-v]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import urllib.request
import urllib.error
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time as dt_time

import pandas as pd
import pandas_ta as ta
import websocket
import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — match LEAN DexterAlpha exactly
# ---------------------------------------------------------------------------

FINNHUB_WS_URL: str = "wss://ws.finnhub.io"
FINNHUB_API_KEY: str = os.environ.get(
    "FINNHUB_API_KEY", "d762r3hr01qm4b7souqgd762r3hr01qm4b7sour0"
)

_DEFAULT_WEBHOOK_API_KEY: str = "6fd7fa3c95c9296eb3fc376eea08146e"
_LOCAL_WAREHOUSE_HOST: str = "warehouse"

WEBHOOK_API_KEY: str = os.environ.get("DEXTER_WEBHOOK_API_KEY", "")
WEBHOOK_URL: str = os.environ.get("DEXTER_WEBHOOK_URL", "")

ATR_COMPRESSION_THRESHOLD: float = 0.008
RVOL_MINIMUM: float = 1.2
BREAKOUT_LOOKBACK: int = 10
MA_STRONG_SLOPE_PCT: float = 0.1
MIN_CANDLES: int = 12
BAR_INTERVAL_MINUTES: int = 15
COOLDOWN_BARS: int = 1
HISTORY_PERIOD: str = "60d"
MARKET_OPEN: dt_time = dt_time(9, 30)
MARKET_CLOSE: dt_time = dt_time(16, 0)


# ---------------------------------------------------------------------------
# Utility functions — ported from lean/ShoulderTaps/alpha/utils.py
# ---------------------------------------------------------------------------

def sma_slope_pct(values: list[float], period: int = 1) -> float | None:
    """Percentage slope over ``period`` bars."""
    if len(values) < period + 1:
        return None
    prev = values[-(period + 1)]
    curr = values[-1]
    if prev == 0:
        return None
    return (curr - prev) / abs(prev) * 100


def calc_rvol_tod(current_volume: float, tod_volumes: list[float]) -> float | None:
    """Time-of-day RVOL = current bar volume / avg volume for same TOD slot."""
    if not tod_volumes or len(tod_volumes) < 5:
        return None
    avg = sum(tod_volumes) / len(tod_volumes)
    if avg == 0:
        return None
    return current_volume / avg


# ---------------------------------------------------------------------------
# AlertWebhookClient — posts alerts to an aggregator API
# ---------------------------------------------------------------------------

class AlertWebhookClient:
    """Posts structured alert payloads to a webhook endpoint.

    Args:
        url: The webhook endpoint URL.
        api_key: Value for the ``X-API-Key`` header.
    """

    def __init__(self, url: str, api_key: str = WEBHOOK_API_KEY) -> None:
        self._url = url
        self._api_key = api_key

    def post(self, signal: DexterSignal, entry_exit: str = "entry") -> bool:
        """Post an alert payload. Returns True on success.

        Args:
            signal: The Dexter signal that fired.
            entry_exit: ``"entry"`` or ``"exit"``.
        """
        payload = {
            "timestamp": signal.timestamp.isoformat(),
            "symbol": signal.symbol,
            "price": signal.close,
            "alert_type": "DEXTER",
            "entry_exit": entry_exit,
            "side": signal.action.lower(),
            "bar_size": f"{BAR_INTERVAL_MINUTES}m",
        }

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self._api_key,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(
                    "[%s] Webhook posted (%d): %s",
                    signal.symbol, resp.status, payload,
                )
                return True
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            logger.error("[%s] Webhook failed: %s", signal.symbol, exc)
            return False


# ---------------------------------------------------------------------------
# DexterSignal — result dataclass
# ---------------------------------------------------------------------------

@dataclass
class DexterSignal:
    """Fired when all 5 Dexter gates pass on a single bar."""

    symbol: str
    direction: str
    action: str
    strength: float
    close: float
    sma_20: float
    sma_50: float
    atr_ratio: float
    rvol: float
    slope_20: float
    slope_50: float
    atr_14: float
    channel_high: float
    channel_low: float
    timestamp: datetime

    def __str__(self) -> str:
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M")
        return (
            f"\n{'*' * 70}\n"
            f"  DEXTER ALERT — {self.symbol} — {self.direction} {self.action}\n"
            f"{'*' * 70}\n"
            f"  Time:          {ts}\n"
            f"  Close:         ${self.close:.2f}\n"
            f"  Strength:      {self.strength:.0f}/100\n"
            f"  SMA(20):       {self.sma_20:.2f}\n"
            f"  SMA(50):       {self.sma_50:.2f}\n"
            f"  Slope(20):     {self.slope_20:.4f}%\n"
            f"  Slope(50):     {self.slope_50:.4f}%\n"
            f"  ATR(14):       {self.atr_14:.4f}\n"
            f"  ATR ratio:     {self.atr_ratio:.5f} (thresh {ATR_COMPRESSION_THRESHOLD})\n"
            f"  RVOL (TOD):    {self.rvol:.2f}x (min {RVOL_MINIMUM})\n"
            f"  Channel high:  {self.channel_high:.2f}\n"
            f"  Channel low:   {self.channel_low:.2f}\n"
            f"{'*' * 70}\n"
        )


# ---------------------------------------------------------------------------
# TODVolumeBaseline — per-slot volume history for a single symbol
# ---------------------------------------------------------------------------

class TODVolumeBaseline:
    """Maintains time-of-day volume baseline for one symbol.

    Groups volume by HH:MM slot (e.g. ``"09:30"``, ``"09:45"``).
    Excludes the current day so live bars aren't compared against themselves.
    """

    def __init__(self) -> None:
        # {slot_str: [vol1, vol2, ...]}
        self._slots: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def build(self, df: pd.DataFrame) -> None:
        """Populate baseline from a yfinance DataFrame.

        Replaces any existing baseline (used for daily refresh).
        Excludes today's bars.
        """
        if df.empty:
            return

        new_slots: dict[str, list[float]] = defaultdict(list)
        today = datetime.now().date()

        for ts, row in df.iterrows():
            if ts.to_pydatetime().date() >= today:
                continue
            slot = ts.strftime("%H:%M")
            vol = float(row["Volume"])
            if vol > 0:
                new_slots[slot].append(vol)

        with self._lock:
            self._slots = new_slots

        total = sum(len(v) for v in new_slots.values())
        logger.info(
            "TOD baseline: %d slots, %d samples (avg %.0f/slot)",
            len(new_slots), total,
            total / len(new_slots) if new_slots else 0,
        )

    def get_volumes(self, slot: str) -> list[float]:
        """Return historical volumes for a given TOD slot."""
        with self._lock:
            return list(self._slots.get(slot, []))


# ---------------------------------------------------------------------------
# BarAggregator — converts streaming trades into 15m OHLCV bars
# ---------------------------------------------------------------------------

@dataclass
class PartialBar:
    """In-progress bar being built from trade ticks."""

    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    volume: float = 0.0
    tick_count: int = 0

    def update(self, price: float, volume: float) -> None:
        """Incorporate a new trade tick."""
        if self.tick_count == 0:
            self.open = price
            self.high = price
            self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
        self.close = price
        self.volume += volume
        self.tick_count += 1


class BarAggregator:
    """Aggregates streaming trades into fixed-interval OHLCV bars for one symbol.

    Fires two callbacks:
        - ``on_bar_complete``: when a bar finishes (first tick of next interval)
        - ``on_tick``: on every trade, with the current partial bar state
    """

    def __init__(
        self,
        interval_minutes: int = BAR_INTERVAL_MINUTES,
        on_bar_complete: callable = None,
        on_tick: callable = None,
    ) -> None:
        self._interval = interval_minutes
        self._on_bar_complete = on_bar_complete
        self._on_tick = on_tick
        self._current_start: datetime | None = None
        self._current_bar: PartialBar | None = None

    def _bar_start(self, ts: datetime) -> datetime:
        """Snap a timestamp to the start of its bar interval."""
        minutes = (ts.hour * 60 + ts.minute) // self._interval * self._interval
        return ts.replace(
            hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0
        )

    def ingest(self, price: float, volume: float, ts: datetime) -> None:
        """Process a single trade tick."""
        bar_start = self._bar_start(ts)

        if self._current_bar is not None:
            if bar_start > self._current_start:
                # Previous bar is complete
                if self._current_bar.tick_count > 0 and self._on_bar_complete:
                    self._on_bar_complete(self._current_start, self._current_bar)
                self._current_bar = PartialBar()
                self._current_bar.update(price, volume)
                self._current_start = bar_start
            else:
                self._current_bar.update(price, volume)
        else:
            self._current_bar = PartialBar()
            self._current_bar.update(price, volume)
            self._current_start = bar_start

        # Fire on every tick with the live partial bar
        if self._on_tick and self._current_bar.tick_count > 0:
            self._on_tick(self._current_start, self._current_bar)


# ---------------------------------------------------------------------------
# DexterEvaluator — the 5-gate signal logic (from LEAN DexterAlpha)
# ---------------------------------------------------------------------------

class DexterEvaluator:
    """Evaluates the 5-gate DEXTER signal on a rolling window of candles.

    Operates on a single symbol. Maintains candle history and cooldown state.
    """

    def __init__(self, symbol: str, tod_baseline: TODVolumeBaseline) -> None:
        self._symbol = symbol
        self._tod = tod_baseline
        self._candles: list[dict] = []
        self._last_fire: datetime | None = None
        self._lock = threading.Lock()

    def seed(self, df: pd.DataFrame) -> None:
        """Seed candle history from a yfinance DataFrame with indicators.

        Replaces existing history (used for daily refresh).
        """
        if df.empty:
            return

        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        df["sma_20"] = ta.sma(df["close"], length=20)
        df["sma_50"] = ta.sma(df["close"], length=50)
        df["atr_14"] = ta.atr(df["high"], df["low"], df["close"], length=14)

        candles = []
        for ts, row in df.iterrows():
            candles.append({
                "time": ts.to_pydatetime(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "sma_20": float(row["sma_20"]) if pd.notna(row.get("sma_20")) else None,
                "sma_50": float(row["sma_50"]) if pd.notna(row.get("sma_50")) else None,
                "atr_14": float(row["atr_14"]) if pd.notna(row.get("atr_14")) else None,
            })

        with self._lock:
            self._candles = candles
            self._last_fire = None

        valid = sum(1 for c in candles if c["sma_50"] is not None)
        logger.info(
            "Seeded %s: %d candles (%d with full indicators)",
            self._symbol, len(candles), valid,
        )

    def on_bar(self, bar_start: datetime, bar: PartialBar) -> None:
        """Called when a 15m bar completes. Appends to history and recomputes
        indicators. Does NOT evaluate — evaluation happens on every tick via
        :meth:`on_tick`.
        """
        with self._lock:
            self._candles.append({
                "time": bar_start,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "sma_20": None,
                "sma_50": None,
                "atr_14": None,
            })
            self._recompute_indicators()

    def on_tick(self, bar_start: datetime, partial: PartialBar) -> DexterSignal | None:
        """Evaluate the 5-gate signal using live tick data.

        Gates 1-3 (MA alignment, slope, ATR compression) use indicators
        from the last completed bar. Gates 4-5 (breakout, RVOL) use the
        live partial bar's close and volume.

        Returns a :class:`DexterSignal` if triggered, else ``None``.
        """
        with self._lock:
            if len(self._candles) < MIN_CANDLES:
                return None

            # Cooldown — one signal per bar interval
            if self._last_fire is not None:
                elapsed = (bar_start - self._last_fire).total_seconds() / 60
                if elapsed < BAR_INTERVAL_MINUTES * COOLDOWN_BARS:
                    return None

            signal = self._evaluate_live(bar_start, partial)
            if signal is not None:
                self._last_fire = bar_start
            return signal

    def _recompute_indicators(self) -> None:
        """Recompute SMA(20), SMA(50), ATR(14) from candle history."""
        closes = pd.Series([c["close"] for c in self._candles])
        highs = pd.Series([c["high"] for c in self._candles])
        lows = pd.Series([c["low"] for c in self._candles])

        sma_20 = ta.sma(closes, length=20)
        sma_50 = ta.sma(closes, length=50)
        atr_14 = ta.atr(highs, lows, closes, length=14)

        for i, candle in enumerate(self._candles):
            candle["sma_20"] = (
                float(sma_20.iloc[i]) if sma_20 is not None and pd.notna(sma_20.iloc[i]) else None
            )
            candle["sma_50"] = (
                float(sma_50.iloc[i]) if sma_50 is not None and pd.notna(sma_50.iloc[i]) else None
            )
            candle["atr_14"] = (
                float(atr_14.iloc[i]) if atr_14 is not None and pd.notna(atr_14.iloc[i]) else None
            )

    def _evaluate_live(
        self, bar_start: datetime, partial: PartialBar,
    ) -> DexterSignal | None:
        """Run the 5-gate evaluation against live tick data.

        Gates 1-3 use indicators from the last completed bar in history.
        Gates 4-5 use the live partial bar's close and volume.
        """
        candles = self._candles

        # Last completed bar provides indicators
        latest = candles[-1]

        # --- Gate 1: MA alignment (from completed bars) ---
        sma_20 = latest.get("sma_20")
        sma_50 = latest.get("sma_50")
        if sma_20 is None or sma_50 is None:
            return None

        if sma_20 > sma_50:
            direction = "BULL"
        elif sma_20 < sma_50:
            direction = "BEAR"
        else:
            return None

        # --- Gate 2: Slope agreement (from completed bars) ---
        sma_20_series = [c["sma_20"] for c in candles if c.get("sma_20") is not None]
        sma_50_series = [c["sma_50"] for c in candles if c.get("sma_50") is not None]
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

        # --- Gate 3: ATR compression (ATR from completed bars, close from live) ---
        atr_14 = latest.get("atr_14")
        close = partial.close
        if atr_14 is None or close == 0:
            return None

        atr_ratio = atr_14 / close
        if atr_ratio > ATR_COMPRESSION_THRESHOLD:
            return None

        # --- Gate 4: 10-bar breakout (channel from completed bars, close from live) ---
        if len(candles) < BREAKOUT_LOOKBACK:
            return None

        channel_candles = candles[-BREAKOUT_LOOKBACK:]
        channel_highs = [c["high"] for c in channel_candles if c.get("high") is not None]
        channel_lows = [c["low"] for c in channel_candles if c.get("low") is not None]
        if not channel_highs or not channel_lows:
            return None

        if direction == "BULL" and not (close > max(channel_highs)):
            return None
        if direction == "BEAR" and not (close < min(channel_lows)):
            return None

        # --- Gate 5: RVOL (live volume vs TOD baseline) ---
        current_volume = partial.volume
        if current_volume == 0:
            return None

        slot = bar_start.strftime("%H:%M")
        tod_volumes = self._tod.get_volumes(slot)
        rvol = calc_rvol_tod(current_volume, tod_volumes)
        if rvol is None or rvol < RVOL_MINIMUM:
            return None

        # --- All gates passed — compute strength ---
        strength = 60.0
        rvol_bonus = min(20.0, (rvol - RVOL_MINIMUM) * 50)
        strength += max(0.0, rvol_bonus)
        if abs(slope_20) > MA_STRONG_SLOPE_PCT and abs(slope_50) > MA_STRONG_SLOPE_PCT:
            strength += 5
        strength = min(100.0, strength)

        return DexterSignal(
            symbol=self._symbol,
            direction=direction,
            action="BUY" if direction == "BULL" else "SELL",
            strength=strength,
            close=close,
            sma_20=sma_20,
            sma_50=sma_50,
            atr_ratio=round(atr_ratio, 6),
            rvol=round(rvol, 2),
            slope_20=round(slope_20, 4),
            slope_50=round(slope_50, 4),
            atr_14=atr_14,
            channel_high=max(channel_highs),
            channel_low=min(channel_lows),
            timestamp=bar_start,
        )


# ---------------------------------------------------------------------------
# DexterAlertMonitor — single-symbol orchestrator
# ---------------------------------------------------------------------------

class DexterAlertMonitor:
    """Monitors a single symbol for DEXTER signals in real time.

    Seeds with 60 days of 15m history, streams live trades from Finnhub,
    and re-fetches history daily at market open to keep the TOD baseline
    and indicator history fresh.

    Args:
        symbol: Ticker symbol to monitor (e.g. ``"AAPL"``).
        api_key: Finnhub API key.
        on_signal: Optional callback for fired signals. Defaults to print.
    """

    def __init__(
        self,
        symbol: str,
        api_key: str = FINNHUB_API_KEY,
        webhook_url: str = WEBHOOK_URL,
        webhook_api_key: str = WEBHOOK_API_KEY,
        on_signal: callable = None,
    ) -> None:
        self.symbol = symbol.upper()
        self._api_key = api_key
        self._on_signal = on_signal or self._default_on_signal

        self._webhook: AlertWebhookClient | None = None
        if webhook_url:
            resolved_key = self._resolve_api_key(webhook_url, webhook_api_key)
            self._webhook = AlertWebhookClient(url=webhook_url, api_key=resolved_key)

        self._tod = TODVolumeBaseline()
        self._evaluator = DexterEvaluator(self.symbol, self._tod)
        self._aggregator = BarAggregator(
            interval_minutes=BAR_INTERVAL_MINUTES,
            on_bar_complete=self._on_bar_complete,
            on_tick=self._on_tick,
        )
        self._ws: websocket.WebSocketApp | None = None
        self._refresh_timer: threading.Timer | None = None
        self._last_seed_date: datetime | None = None

    @staticmethod
    def _resolve_api_key(webhook_url: str, provided_key: str) -> str:
        """Resolve the webhook API key.

        Local warehouse (URL contains the warehouse hostname) uses the
        default key. External endpoints require the key to be explicitly
        provided via ``--webhook-api-key`` or ``DEXTER_WEBHOOK_API_KEY``.
        """
        is_local = _LOCAL_WAREHOUSE_HOST in webhook_url
        if is_local:
            return provided_key or _DEFAULT_WEBHOOK_API_KEY
        if not provided_key:
            raise ValueError(
                "External webhook requires an API key. "
                "Pass --webhook-api-key or set DEXTER_WEBHOOK_API_KEY."
            )
        return provided_key

    def run(self) -> None:
        """Seed historical data, schedule daily refresh, start live stream.

        Blocks until stopped.
        """
        self._seed()
        self._schedule_daily_refresh()
        self._start_stream()

    def stop(self) -> None:
        """Gracefully shut down the websocket and refresh timer."""
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
        if self._ws is not None:
            self._ws.close()

    # -- Seeding & refresh --

    def _fetch_history(self) -> pd.DataFrame:
        """Fetch 60 days of 15m bars from yfinance."""
        ticker = yf.Ticker(self.symbol)
        return ticker.history(period=HISTORY_PERIOD, interval="15m")

    def _seed(self) -> None:
        """Fetch history, build TOD baseline, seed evaluator."""
        print(f"  [{self.symbol}] Fetching {HISTORY_PERIOD} of 15m bars...", end=" ", flush=True)
        df = self._fetch_history()

        if df.empty:
            print("NO DATA")
            return

        self._tod.build(df)
        self._evaluator.seed(df)
        self._last_seed_date = datetime.now()

        today_bars = sum(
            1 for ts in df.index
            if ts.to_pydatetime().date() == datetime.now().date()
        )
        print(f"{len(df)} bars ({today_bars} today)")

    def _refresh(self) -> None:
        """Re-fetch history and rebuild baseline + evaluator state.

        Called daily at market open by the refresh timer.
        """
        today = datetime.now().date()
        if self._last_seed_date and self._last_seed_date.date() >= today:
            logger.debug("Already seeded today, skipping refresh")
            self._schedule_daily_refresh()
            return

        print(f"\n  [{self.symbol}] Daily refresh — re-seeding {HISTORY_PERIOD} history...")
        df = self._fetch_history()

        if not df.empty:
            self._tod.build(df)
            self._evaluator.seed(df)
            self._last_seed_date = datetime.now()
            print(f"  [{self.symbol}] Refresh complete: {len(df)} bars")
        else:
            print(f"  [{self.symbol}] Refresh failed — no data returned")

        self._schedule_daily_refresh()

    def _schedule_daily_refresh(self) -> None:
        """Schedule the next refresh for market open (09:30 ET).

        Uses a simple timer. If market open has already passed today,
        schedules for tomorrow.
        """
        now = datetime.now()
        target = now.replace(
            hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0,
        )
        if target <= now:
            # Already past market open today, schedule for tomorrow
            target = target.replace(day=target.day + 1)
            # Handle month rollover via timedelta
            from datetime import timedelta
            if target.day == 1:
                target = now + timedelta(days=1)
                target = target.replace(
                    hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                    second=0, microsecond=0,
                )

        delay = (target - now).total_seconds()
        self._refresh_timer = threading.Timer(delay, self._refresh)
        self._refresh_timer.daemon = True
        self._refresh_timer.start()

        logger.info(
            "[%s] Next refresh scheduled at %s (%.0f seconds)",
            self.symbol, target.strftime("%Y-%m-%d %H:%M"), delay,
        )

    # -- Live stream --

    def _start_stream(self) -> None:
        """Connect to Finnhub websocket and stream trades."""
        if not self._api_key:
            raise ValueError(
                "Finnhub API key required. Set FINNHUB_API_KEY env var. "
                "Sign up free at https://finnhub.io"
            )

        url = f"{FINNHUB_WS_URL}?token={self._api_key}"
        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )

        print(f"\n  [{self.symbol}] DEXTER monitor live (Ctrl+C to stop)")
        print(f"  Bar: {BAR_INTERVAL_MINUTES}m | "
              f"ATR<{ATR_COMPRESSION_THRESHOLD} | "
              f"{BREAKOUT_LOOKBACK}-bar breakout | "
              f"RVOL>={RVOL_MINIMUM}")
        print(f"  {'─' * 60}\n")

        self._ws.run_forever()

    def _on_ws_open(self, ws: websocket.WebSocketApp) -> None:
        ws.send(json.dumps({"type": "subscribe", "symbol": self.symbol}))
        logger.info("Subscribed to %s", self.symbol)

    def _on_ws_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        data = json.loads(message)
        if data.get("type") != "trade":
            return
        for raw in data["data"]:
            if raw["s"] != self.symbol:
                continue
            ts = datetime.fromtimestamp(raw["t"] / 1000)
            self._aggregator.ingest(raw["p"], raw["v"], ts)

    def _on_ws_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        logger.error("[%s] Websocket error: %s", self.symbol, error)

    def _on_ws_close(
        self,
        ws: websocket.WebSocketApp,
        status: int | None,
        msg: str | None,
    ) -> None:
        logger.info("[%s] Websocket closed (status=%s)", self.symbol, status)
        print(f"\n  [{self.symbol}] Stream closed")

    # -- Callbacks --

    def _on_bar_complete(self, bar_start: datetime, bar: PartialBar) -> None:
        """Called when a 15m bar completes. Commits it to history for
        indicator recalculation. Does not evaluate — that happens on_tick.
        """
        logger.debug(
            "[%s] Bar %s O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
            self.symbol, bar_start.strftime("%H:%M"),
            bar.open, bar.high, bar.low, bar.close, bar.volume,
        )
        self._evaluator.on_bar(bar_start, bar)

    def _on_tick(self, bar_start: datetime, partial: PartialBar) -> None:
        """Called on every trade tick. Evaluates the 5-gate signal using
        the live partial bar against historical context.
        """
        signal = self._evaluator.on_tick(bar_start, partial)
        if signal is not None:
            self._on_signal(signal)
            if self._webhook:
                self._webhook.post(signal, entry_exit="entry")

    @staticmethod
    def _default_on_signal(signal: DexterSignal) -> None:
        """Default handler: print the alert to stdout."""
        print(signal)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Real-time DEXTER alert monitor for a single symbol.",
    )
    parser.add_argument(
        "symbol",
        help="Ticker symbol to monitor (e.g. AAPL)",
    )
    parser.add_argument(
        "--webhook-url",
        default=WEBHOOK_URL,
        help="Webhook URL to POST alerts to (default: env DEXTER_WEBHOOK_URL)",
    )
    parser.add_argument(
        "--webhook-api-key",
        default=WEBHOOK_API_KEY,
        help="X-API-Key for the webhook endpoint (required for external URLs, "
             "default: env DEXTER_WEBHOOK_API_KEY)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging (shows every completed bar)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    monitor = DexterAlertMonitor(
        symbol=args.symbol,
        webhook_url=args.webhook_url,
        webhook_api_key=args.webhook_api_key,
    )
    try:
        monitor.run()
    except KeyboardInterrupt:
        monitor.stop()
        print("\nStopped.")


if __name__ == "__main__":
    main()
