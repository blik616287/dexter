"""Tests for stock_stream.py — historical bars and trade streaming."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from stock_stream import (
    HistoricalBarClient,
    Trade,
    TradeStreamClient,
    parse_args,
)


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------

class TestTrade:
    def test_from_finnhub(self):
        raw = {"s": "AAPL", "p": 253.79, "v": 100, "t": 1743454500000}
        trade = Trade.from_finnhub(raw)
        assert trade.symbol == "AAPL"
        assert trade.price == 253.79
        assert trade.volume == 100
        assert isinstance(trade.timestamp, datetime)

    def test_str_format(self):
        trade = Trade(
            symbol="AAPL",
            price=253.79,
            volume=100,
            timestamp=datetime(2026, 3, 31, 14, 45, 30),
        )
        s = str(trade)
        assert "AAPL" in s
        assert "253.79" in s


# ---------------------------------------------------------------------------
# HistoricalBarClient
# ---------------------------------------------------------------------------

class TestHistoricalBarClient:
    def test_default_symbols(self):
        client = HistoricalBarClient()
        assert client.symbols == ["AAPL", "MSFT"]

    def test_custom_symbols(self):
        client = HistoricalBarClient(symbols=["NVDA", "GOOGL"])
        assert client.symbols == ["NVDA", "GOOGL"]

    @patch("stock_stream.yf.Ticker")
    def test_fetch_calls_yfinance(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame({
            "Open": [100], "High": [105], "Low": [99],
            "Close": [104], "Volume": [1000],
        }, index=pd.DatetimeIndex([datetime(2026, 3, 31, 9, 30)]))
        mock_ticker_cls.return_value = mock_ticker

        client = HistoricalBarClient(symbols=["TEST"])
        result = client.fetch(period="1d", interval="15m", max_bars=0)

        assert "TEST" in result
        assert len(result["TEST"]) == 1
        mock_ticker.history.assert_called_once_with(period="1d", interval="15m")

    @patch("stock_stream.yf.Ticker")
    def test_max_bars_limits_output(self, mock_ticker_cls):
        from datetime import timedelta
        mock_ticker = MagicMock()
        base = datetime(2026, 3, 31, 9, 30)
        idx = pd.DatetimeIndex([base + timedelta(minutes=15 * i) for i in range(20)])
        mock_ticker.history.return_value = pd.DataFrame({
            "Open": range(20), "High": range(20), "Low": range(20),
            "Close": range(20), "Volume": range(20),
        }, index=idx)
        mock_ticker_cls.return_value = mock_ticker

        client = HistoricalBarClient(symbols=["TEST"])
        result = client.fetch(max_bars=5)
        assert len(result["TEST"]) == 5

    @patch("stock_stream.yf.Ticker")
    def test_max_bars_zero_unlimited(self, mock_ticker_cls):
        from datetime import timedelta
        mock_ticker = MagicMock()
        base = datetime(2026, 3, 31, 9, 30)
        idx = pd.DatetimeIndex([base + timedelta(minutes=15 * i) for i in range(20)])
        mock_ticker.history.return_value = pd.DataFrame({
            "Open": range(20), "High": range(20), "Low": range(20),
            "Close": range(20), "Volume": range(20),
        }, index=idx)
        mock_ticker_cls.return_value = mock_ticker

        client = HistoricalBarClient(symbols=["TEST"])
        result = client.fetch(max_bars=0)
        assert len(result["TEST"]) == 20


# ---------------------------------------------------------------------------
# TradeStreamClient
# ---------------------------------------------------------------------------

class TestTradeStreamClient:
    def test_default_symbols(self):
        client = TradeStreamClient()
        assert client.symbols == ["AAPL", "MSFT"]

    def test_url_includes_api_key(self):
        client = TradeStreamClient(api_key="testkey")
        assert "testkey" in client.url

    def test_no_api_key_raises(self):
        client = TradeStreamClient(api_key="")
        with pytest.raises(ValueError, match="Finnhub API key required"):
            client.start()

    def test_custom_callback(self):
        trades = []
        client = TradeStreamClient(on_trade=lambda t: trades.append(t))
        assert client.on_trade is not None

    def test_stop_without_start(self):
        client = TradeStreamClient()
        client.stop()  # Should not raise


# ---------------------------------------------------------------------------
# CLI parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.symbols == ["AAPL", "MSFT"]
        assert args.period == "2d"
        assert args.interval == "15m"
        assert args.max_bars == 10
        assert args.no_stream is False
        assert args.no_history is False
        assert args.verbose is False

    def test_custom_symbols(self):
        args = parse_args(["--symbols", "NVDA", "GOOGL"])
        assert args.symbols == ["NVDA", "GOOGL"]

    def test_max_bars(self):
        args = parse_args(["--max-bars", "25"])
        assert args.max_bars == 25

    def test_no_stream(self):
        args = parse_args(["--no-stream"])
        assert args.no_stream is True

    def test_no_history(self):
        args = parse_args(["--no-history"])
        assert args.no_history is True

    def test_verbose(self):
        args = parse_args(["-v"])
        assert args.verbose is True
