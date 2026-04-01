#!/usr/bin/env python3
"""Real-time streaming and historical bar data for equities.

Provides two core capabilities via free data sources:
    - 15-minute OHLCV historical bars (yfinance / Yahoo Finance)
    - Real-time trade streaming via websocket (Finnhub)

Usage::

    from stock_stream import HistoricalBarClient, TradeStreamClient

    bars = HistoricalBarClient(symbols=["AAPL", "MSFT"])
    data = bars.fetch(period="2d", interval="15m")

    stream = TradeStreamClient(symbols=["AAPL", "MSFT"], api_key="...")
    stream.start()

CLI::

    export FINNHUB_API_KEY="your_key"
    python3 stock_stream.py [--symbols AAPL MSFT] [--period 2d] [--interval 15m]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import pandas as pd
import websocket
import yfinance as yf

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS: list[str] = ["AAPL", "MSFT"]
DEFAULT_PERIOD: str = "2d"
DEFAULT_INTERVAL: str = "15m"
DEFAULT_MAX_BARS: int = 10
FINNHUB_WS_URL: str = "wss://ws.finnhub.io"
FINNHUB_API_KEY: str = os.environ.get(
    "FINNHUB_API_KEY", "d762r3hr01qm4b7souqgd762r3hr01qm4b7sour0"
)


@dataclass
class Trade:
    """A single trade tick from the Finnhub websocket."""

    symbol: str
    price: float
    volume: float
    timestamp: datetime

    @classmethod
    def from_finnhub(cls, raw: dict) -> Trade:
        """Construct a Trade from a Finnhub websocket message entry."""
        return cls(
            symbol=raw["s"],
            price=raw["p"],
            volume=raw["v"],
            timestamp=datetime.fromtimestamp(raw["t"] / 1000),
        )

    def __str__(self) -> str:
        ts_str = self.timestamp.strftime("%H:%M:%S.%f")[:-3]
        return f"{self.symbol:<6}  ${self.price:<10.2f}  vol={self.volume:<8}  {ts_str}"


class HistoricalBarClient:
    """Fetches intraday OHLCV bars from Yahoo Finance via yfinance.

    Args:
        symbols: Ticker symbols to fetch.
    """

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols = symbols or DEFAULT_SYMBOLS

    def fetch(
        self,
        period: str = DEFAULT_PERIOD,
        interval: str = DEFAULT_INTERVAL,
        max_bars: int = DEFAULT_MAX_BARS,
    ) -> dict[str, pd.DataFrame]:
        """Fetch historical bars for all configured symbols.

        Args:
            period: Lookback period (e.g. ``"2d"``, ``"5d"``).
            interval: Bar interval (e.g. ``"15m"``, ``"1h"``).
            max_bars: Maximum number of most recent bars to return.
                Use ``0`` for unlimited.

        Returns:
            Mapping of symbol to a :class:`pandas.DataFrame` with columns
            ``Open``, ``High``, ``Low``, ``Close``, ``Volume``.
        """
        results: dict[str, pd.DataFrame] = {}
        for symbol in self.symbols:
            logger.info("Fetching %s bars: period=%s interval=%s", symbol, period, interval)
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval)
            if max_bars > 0:
                df = df.tail(max_bars)
            results[symbol] = df
            if df.empty:
                logger.warning("No data returned for %s (market may be closed)", symbol)
            else:
                logger.info("Received %d bars for %s", len(df), symbol)
        return results

    def print_bars(
        self,
        period: str = DEFAULT_PERIOD,
        interval: str = DEFAULT_INTERVAL,
        max_bars: int = DEFAULT_MAX_BARS,
    ) -> None:
        """Fetch and pretty-print bars to stdout."""
        results = self.fetch(period=period, interval=interval, max_bars=max_bars)

        print("=" * 70)
        print(f"  HISTORICAL BARS  ({period}, {interval})")
        print("=" * 70)

        for symbol, df in results.items():
            if df.empty:
                print(f"\n[{symbol}] No data returned.")
                continue

            print(f"\n{'─' * 70}")
            print(f"  {symbol}  —  {len(df)} bars")
            print(f"{'─' * 70}")
            header = (
                f"{'Datetime':<22} {'Open':>10} {'High':>10} "
                f"{'Low':>10} {'Close':>10} {'Volume':>12}"
            )
            print(header)
            print(
                f"{'─' * 22} {'─' * 10} {'─' * 10} "
                f"{'─' * 10} {'─' * 10} {'─' * 12}"
            )

            for ts, row in df.iterrows():
                dt_str = ts.strftime("%Y-%m-%d %H:%M")
                print(
                    f"{dt_str:<22} {row['Open']:>10.2f} {row['High']:>10.2f} "
                    f"{row['Low']:>10.2f} {row['Close']:>10.2f} "
                    f"{int(row['Volume']):>12,}"
                )
        print()


@dataclass
class TradeStreamClient:
    """Streams real-time trades from Finnhub via websocket.

    Args:
        symbols: Ticker symbols to subscribe to.
        api_key: Finnhub API key (free tier).
        on_trade: Optional callback invoked with each :class:`Trade`.
            Defaults to printing the trade to stdout.
    """

    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    api_key: str = FINNHUB_API_KEY
    on_trade: Callable[[Trade], None] | None = None
    _ws: websocket.WebSocketApp | None = field(default=None, init=False, repr=False)

    @property
    def url(self) -> str:
        """Finnhub websocket endpoint with API key."""
        return f"{FINNHUB_WS_URL}?token={self.api_key}"

    def start(self) -> None:
        """Open the websocket connection and block until closed.

        Raises:
            ValueError: If no API key is configured.
        """
        if not self.api_key:
            raise ValueError(
                "Finnhub API key required. Set FINNHUB_API_KEY or pass api_key. "
                "Sign up free at https://finnhub.io"
            )

        self._ws = websocket.WebSocketApp(
            self.url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        logger.info("Connecting to Finnhub websocket for %s", self.symbols)
        self._ws.run_forever()

    def stop(self) -> None:
        """Gracefully close the websocket connection."""
        if self._ws is not None:
            self._ws.close()
            logger.info("Websocket connection closed")

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        """Subscribe to configured symbols on connection open."""
        for symbol in self.symbols:
            ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
            logger.info("Subscribed to %s", symbol)

        print("=" * 70)
        print("  REAL-TIME STREAM  (Finnhub websocket)")
        print("=" * 70)
        print(f"  {'Symbol':<6}  {'Price':<12}  {'Volume':<10}  {'Time'}")
        print(f"  {'─' * 6}  {'─' * 12}  {'─' * 10}  {'─' * 12}")

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        """Parse incoming trade messages and dispatch to callback."""
        data = json.loads(message)
        if data.get("type") != "trade":
            return

        for raw_trade in data["data"]:
            trade = Trade.from_finnhub(raw_trade)
            if self.on_trade is not None:
                self.on_trade(trade)
            else:
                print(f"  {trade}")

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        """Log websocket errors."""
        logger.error("Websocket error: %s", error)

    def _on_close(
        self,
        ws: websocket.WebSocketApp,
        close_status: int | None,
        close_msg: str | None,
    ) -> None:
        """Log websocket closure."""
        logger.info("Websocket closed (status=%s, msg=%s)", close_status, close_msg)
        print("\n[Stream closed]")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Fetch historical bars and stream real-time trades.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help="Ticker symbols (default: %(default)s)",
    )
    parser.add_argument(
        "--period",
        default=DEFAULT_PERIOD,
        help="Historical lookback period (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        default=DEFAULT_INTERVAL,
        help="Bar interval (default: %(default)s)",
    )
    parser.add_argument(
        "--max-bars",
        type=int,
        default=DEFAULT_MAX_BARS,
        help="Max number of recent bars to display, 0 for all (default: %(default)s)",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Skip real-time streaming, only fetch historical bars",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Skip historical bars, only stream real-time",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
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

    if not args.no_history:
        client = HistoricalBarClient(symbols=args.symbols)
        client.print_bars(
            period=args.period,
            interval=args.interval,
            max_bars=args.max_bars,
        )

    if not args.no_stream:
        stream = TradeStreamClient(symbols=args.symbols)
        print("Starting real-time stream (Ctrl+C to stop)...\n")
        try:
            stream.start()
        except KeyboardInterrupt:
            stream.stop()
            print("\nStopped.")


if __name__ == "__main__":
    main()
