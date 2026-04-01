"""Tests for dexter_alert.py — DEXTER signal evaluator and alert monitor."""

from __future__ import annotations

import json
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from unittest.mock import MagicMock

import pandas as pd
import pytest

from dexter_alert import (
    AlertWebhookClient,
    BarAggregator,
    DexterAlertMonitor,
    DexterEvaluator,
    DexterSignal,
    PartialBar,
    TODVolumeBaseline,
    calc_rvol_tod,
    parse_args,
    sma_slope_pct,
)


# ---------------------------------------------------------------------------
# sma_slope_pct
# ---------------------------------------------------------------------------

class TestSMASlopePct:
    def test_basic_positive_slope(self):
        assert sma_slope_pct([100.0, 101.0]) == pytest.approx(1.0)

    def test_basic_negative_slope(self):
        assert sma_slope_pct([100.0, 99.0]) == pytest.approx(-1.0)

    def test_zero_previous_returns_none(self):
        assert sma_slope_pct([0.0, 1.0]) is None

    def test_insufficient_data_returns_none(self):
        assert sma_slope_pct([100.0]) is None

    def test_empty_returns_none(self):
        assert sma_slope_pct([]) is None

    def test_multi_period(self):
        result = sma_slope_pct([100.0, 105.0, 110.0], period=2)
        assert result == pytest.approx(10.0)

    def test_uses_last_values(self):
        result = sma_slope_pct([50.0, 100.0, 102.0], period=1)
        assert result == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# calc_rvol_tod
# ---------------------------------------------------------------------------

class TestCalcRvolTod:
    def test_basic_rvol(self):
        assert calc_rvol_tod(200.0, [100.0] * 10) == pytest.approx(2.0)

    def test_below_minimum_samples(self):
        assert calc_rvol_tod(200.0, [100.0] * 4) is None

    def test_exactly_five_samples(self):
        assert calc_rvol_tod(150.0, [100.0] * 5) == pytest.approx(1.5)

    def test_empty_list(self):
        assert calc_rvol_tod(100.0, []) is None

    def test_none_list(self):
        assert calc_rvol_tod(100.0, None) is None

    def test_zero_average(self):
        assert calc_rvol_tod(100.0, [0.0] * 10) is None


# ---------------------------------------------------------------------------
# DexterSignal
# ---------------------------------------------------------------------------

class TestDexterSignal:
    @pytest.fixture()
    def signal(self):
        return DexterSignal(
            symbol="AAPL",
            direction="BULL",
            action="BUY",
            strength=72.0,
            close=253.79,
            sma_20=252.5,
            sma_50=248.1,
            atr_ratio=0.00612,
            rvol=1.45,
            slope_20=0.0845,
            slope_50=0.0523,
            atr_14=1.55,
            channel_high=253.0,
            channel_low=251.2,
            timestamp=datetime(2026, 3, 31, 14, 45),
        )

    def test_str_contains_symbol(self, signal):
        assert "AAPL" in str(signal)

    def test_str_contains_direction(self, signal):
        assert "BULL" in str(signal)

    def test_str_contains_price(self, signal):
        assert "253.79" in str(signal)


# ---------------------------------------------------------------------------
# TODVolumeBaseline
# ---------------------------------------------------------------------------

class TestTODVolumeBaseline:
    @pytest.fixture()
    def baseline(self):
        return TODVolumeBaseline()

    def test_empty_baseline(self, baseline):
        assert baseline.get_volumes("09:30") == []

    def test_build_from_dataframe(self, baseline):
        # Create a DataFrame with timestamps from yesterday
        from datetime import timedelta
        yesterday = datetime.now().date() - timedelta(days=1)
        idx = pd.DatetimeIndex([
            datetime(yesterday.year, yesterday.month, yesterday.day, 9, 30),
            datetime(yesterday.year, yesterday.month, yesterday.day, 9, 45),
            datetime(yesterday.year, yesterday.month, yesterday.day, 10, 0),
        ])
        df = pd.DataFrame({
            "Open": [100, 101, 102],
            "High": [105, 106, 107],
            "Low": [99, 100, 101],
            "Close": [104, 105, 106],
            "Volume": [1000, 2000, 3000],
        }, index=idx)

        baseline.build(df)
        assert baseline.get_volumes("09:30") == [1000.0]
        assert baseline.get_volumes("09:45") == [2000.0]
        assert baseline.get_volumes("10:00") == [3000.0]
        assert baseline.get_volumes("10:15") == []

    def test_excludes_today(self, baseline):
        today = datetime.now()
        idx = pd.DatetimeIndex([today.replace(hour=9, minute=30)])
        df = pd.DataFrame({
            "Open": [100], "High": [105], "Low": [99],
            "Close": [104], "Volume": [1000],
        }, index=idx)

        baseline.build(df)
        assert baseline.get_volumes("09:30") == []

    def test_excludes_zero_volume(self, baseline):
        from datetime import timedelta
        yesterday = datetime.now().date() - timedelta(days=1)
        idx = pd.DatetimeIndex([
            datetime(yesterday.year, yesterday.month, yesterday.day, 9, 30),
        ])
        df = pd.DataFrame({
            "Open": [100], "High": [105], "Low": [99],
            "Close": [104], "Volume": [0],
        }, index=idx)

        baseline.build(df)
        assert baseline.get_volumes("09:30") == []

    def test_rebuild_replaces(self, baseline):
        from datetime import timedelta
        yesterday = datetime.now().date() - timedelta(days=1)
        idx = pd.DatetimeIndex([
            datetime(yesterday.year, yesterday.month, yesterday.day, 9, 30),
        ])

        df1 = pd.DataFrame({
            "Open": [100], "High": [105], "Low": [99],
            "Close": [104], "Volume": [1000],
        }, index=idx)
        df2 = pd.DataFrame({
            "Open": [100], "High": [105], "Low": [99],
            "Close": [104], "Volume": [2000],
        }, index=idx)

        baseline.build(df1)
        assert baseline.get_volumes("09:30") == [1000.0]

        baseline.build(df2)
        assert baseline.get_volumes("09:30") == [2000.0]


# ---------------------------------------------------------------------------
# PartialBar
# ---------------------------------------------------------------------------

class TestPartialBar:
    def test_first_tick(self):
        bar = PartialBar()
        bar.update(100.0, 50.0)
        assert bar.open == 100.0
        assert bar.high == 100.0
        assert bar.low == 100.0
        assert bar.close == 100.0
        assert bar.volume == 50.0
        assert bar.tick_count == 1

    def test_multiple_ticks(self):
        bar = PartialBar()
        bar.update(100.0, 50.0)
        bar.update(105.0, 30.0)
        bar.update(98.0, 20.0)
        bar.update(102.0, 40.0)

        assert bar.open == 100.0
        assert bar.high == 105.0
        assert bar.low == 98.0
        assert bar.close == 102.0
        assert bar.volume == 140.0
        assert bar.tick_count == 4


# ---------------------------------------------------------------------------
# BarAggregator
# ---------------------------------------------------------------------------

class TestBarAggregator:
    def test_bar_completes_on_next_interval(self):
        completed = []

        def on_complete(bar_start, bar):
            completed.append((bar_start, bar))

        agg = BarAggregator(interval_minutes=15, on_bar_complete=on_complete)

        # Ticks in the 09:30 bar
        agg.ingest(100.0, 50.0, datetime(2026, 3, 31, 9, 30, 0))
        agg.ingest(101.0, 30.0, datetime(2026, 3, 31, 9, 35, 0))
        agg.ingest(99.0, 20.0, datetime(2026, 3, 31, 9, 44, 0))
        assert len(completed) == 0

        # First tick in 09:45 bar triggers completion of 09:30
        agg.ingest(102.0, 40.0, datetime(2026, 3, 31, 9, 45, 0))
        assert len(completed) == 1

        bar_start, bar = completed[0]
        assert bar_start == datetime(2026, 3, 31, 9, 30)
        assert bar.open == 100.0
        assert bar.high == 101.0
        assert bar.low == 99.0
        assert bar.close == 99.0
        assert bar.tick_count == 3

    def test_no_callback_if_none(self):
        agg = BarAggregator(interval_minutes=15, on_bar_complete=None)
        agg.ingest(100.0, 50.0, datetime(2026, 3, 31, 9, 30, 0))
        agg.ingest(101.0, 30.0, datetime(2026, 3, 31, 9, 45, 0))
        # No error, just silently continues

    def test_bar_start_snapping(self):
        agg = BarAggregator(interval_minutes=15)
        assert agg._bar_start(datetime(2026, 3, 31, 9, 37)) == datetime(2026, 3, 31, 9, 30)
        assert agg._bar_start(datetime(2026, 3, 31, 9, 45)) == datetime(2026, 3, 31, 9, 45)
        assert agg._bar_start(datetime(2026, 3, 31, 10, 14)) == datetime(2026, 3, 31, 10, 0)


# ---------------------------------------------------------------------------
# DexterEvaluator
# ---------------------------------------------------------------------------

class TestDexterEvaluator:
    @pytest.fixture()
    def evaluator(self):
        tod = TODVolumeBaseline()
        return DexterEvaluator("TEST", tod)

    def test_insufficient_candles_returns_none(self, evaluator):
        bar = PartialBar()
        bar.update(100.0, 1000.0)
        result = evaluator.on_bar(datetime(2026, 3, 31, 9, 30), bar)
        assert result is None

    def test_seed_populates_candles(self, evaluator):
        df = _make_trending_df(n=60, start_price=100.0, trend=0.1)
        evaluator.seed(df)
        assert len(evaluator._candles) == 60

    def test_seed_computes_indicators(self, evaluator):
        df = _make_trending_df(n=60, start_price=100.0, trend=0.1)
        evaluator.seed(df)
        # Last candle should have indicators (enough bars)
        last = evaluator._candles[-1]
        assert last["sma_20"] is not None
        assert last["sma_50"] is not None
        assert last["atr_14"] is not None

    def test_cooldown_blocks_repeat_signal(self, evaluator):
        """Seed data that triggers, then verify cooldown blocks next bar."""
        df = _make_trending_df(n=60, start_price=100.0, trend=0.1)
        evaluator.seed(df)

        # Force a fire timestamp
        evaluator._last_fire = datetime(2026, 3, 31, 14, 30)

        bar = PartialBar()
        bar.update(200.0, 99999.0)
        result = evaluator.on_bar(datetime(2026, 3, 31, 14, 30), bar)
        # Should be blocked by cooldown (same timestamp)
        assert result is None


# ---------------------------------------------------------------------------
# AlertWebhookClient
# ---------------------------------------------------------------------------

class TestAlertWebhookClient:
    @pytest.fixture()
    def mock_server(self):
        """Start a local HTTP server that records POST requests."""
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                api_key = self.headers.get("X-API-Key", "")
                received.append({"body": body, "api_key": api_key})
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        yield f"http://127.0.0.1:{port}", received

        server.shutdown()

    def test_post_success(self, mock_server):
        url, received = mock_server
        client = AlertWebhookClient(url=url, api_key="test-key")
        signal = _make_signal()

        result = client.post(signal, entry_exit="entry")

        assert result is True
        assert len(received) == 1
        assert received[0]["api_key"] == "test-key"
        assert received[0]["body"]["symbol"] == "AAPL"
        assert received[0]["body"]["alert_type"] == "DEXTER"
        assert received[0]["body"]["entry_exit"] == "entry"
        assert received[0]["body"]["side"] == "buy"
        assert received[0]["body"]["bar_size"] == "15m"

    def test_post_failure_returns_false(self):
        client = AlertWebhookClient(
            url="http://127.0.0.1:1",  # unreachable
            api_key="key",
        )
        result = client.post(_make_signal())
        assert result is False

    def test_payload_format(self, mock_server):
        url, received = mock_server
        client = AlertWebhookClient(url=url, api_key="k")
        signal = _make_signal()
        client.post(signal, entry_exit="exit")

        body = received[0]["body"]
        assert set(body.keys()) == {
            "timestamp", "symbol", "price", "alert_type",
            "entry_exit", "side", "bar_size",
        }
        assert body["entry_exit"] == "exit"


# ---------------------------------------------------------------------------
# DexterAlertMonitor
# ---------------------------------------------------------------------------

class TestDexterAlertMonitor:
    def test_single_symbol_init(self):
        m = DexterAlertMonitor(symbol="aapl")
        assert m.symbol == "AAPL"

    def test_local_webhook_uses_default_key(self):
        m = DexterAlertMonitor(
            symbol="AAPL",
            webhook_url="http://warehouse:8080/alerts",
        )
        assert m._webhook is not None
        assert m._webhook._api_key == "6fd7fa3c95c9296eb3fc376eea08146e"

    def test_external_webhook_requires_key(self):
        with pytest.raises(ValueError, match="External webhook requires"):
            DexterAlertMonitor(
                symbol="AAPL",
                webhook_url="https://example.com/api/alert",
                webhook_api_key="",
            )

    def test_external_webhook_with_key(self):
        m = DexterAlertMonitor(
            symbol="AAPL",
            webhook_url="https://example.com/api/alert",
            webhook_api_key="my-key",
        )
        assert m._webhook is not None
        assert m._webhook._api_key == "my-key"

    def test_no_webhook_url_means_no_client(self):
        m = DexterAlertMonitor(symbol="AAPL", webhook_url="")
        assert m._webhook is None

    def test_on_signal_callback(self):
        signals = []
        m = DexterAlertMonitor(
            symbol="AAPL",
            on_signal=lambda s: signals.append(s),
        )
        sig = _make_signal()
        m._on_signal(sig)
        assert len(signals) == 1


# ---------------------------------------------------------------------------
# CLI parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_symbol_required(self):
        args = parse_args(["AAPL"])
        assert args.symbol == "AAPL"

    def test_webhook_url(self):
        args = parse_args(["AAPL", "--webhook-url", "http://example.com"])
        assert args.webhook_url == "http://example.com"

    def test_webhook_api_key(self):
        args = parse_args(["AAPL", "--webhook-api-key", "mykey"])
        assert args.webhook_api_key == "mykey"

    def test_verbose(self):
        args = parse_args(["AAPL", "-v"])
        assert args.verbose is True

    def test_defaults(self):
        args = parse_args(["MSFT"])
        assert args.symbol == "MSFT"
        assert args.verbose is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(**overrides) -> DexterSignal:
    defaults = dict(
        symbol="AAPL",
        direction="BULL",
        action="BUY",
        strength=72.0,
        close=253.79,
        sma_20=252.5,
        sma_50=248.1,
        atr_ratio=0.00612,
        rvol=1.45,
        slope_20=0.0845,
        slope_50=0.0523,
        atr_14=1.55,
        channel_high=253.0,
        channel_low=251.2,
        timestamp=datetime(2026, 3, 31, 14, 45),
    )
    defaults.update(overrides)
    return DexterSignal(**defaults)


# ---------------------------------------------------------------------------
# DexterEvaluator — gate-level tests
# ---------------------------------------------------------------------------

class TestDexterEvaluatorGates:
    """Test individual gate failures in the evaluator."""

    @pytest.fixture()
    def seeded_evaluator(self):
        """Evaluator seeded with 60 bars of uptrending data."""
        tod = TODVolumeBaseline()
        evaluator = DexterEvaluator("TEST", tod)
        df = _make_trending_df(n=60, start_price=100.0, trend=0.1)
        evaluator.seed(df)
        return evaluator

    def test_gate1_no_sma_returns_none(self):
        """Gate 1 fails when SMA values are None (insufficient data)."""
        tod = TODVolumeBaseline()
        evaluator = DexterEvaluator("TEST", tod)
        # Seed with only 15 bars — sma_50 will be None
        df = _make_trending_df(n=15, start_price=100.0, trend=0.1)
        evaluator.seed(df)
        bar = PartialBar()
        bar.update(120.0, 5000.0)
        result = evaluator.on_bar(datetime(2026, 4, 1, 10, 0), bar)
        assert result is None

    def test_gate3_high_atr_returns_none(self, seeded_evaluator):
        """Gate 3 fails when ATR ratio is too high (volatile stock)."""
        # Inject a bar with huge price swing to blow up ATR
        bar = PartialBar()
        bar.update(200.0, 5000.0)
        bar.update(100.0, 5000.0)  # Massive range
        result = seeded_evaluator.on_bar(datetime(2026, 4, 1, 10, 0), bar)
        assert result is None

    def test_gate5_low_rvol_returns_none(self, seeded_evaluator):
        """Gate 5 fails when volume is too low relative to TOD baseline."""
        # With no TOD baseline data, RVOL will be None
        bar = PartialBar()
        bar.update(120.0, 1.0)  # Tiny volume
        result = seeded_evaluator.on_bar(datetime(2026, 4, 1, 10, 0), bar)
        assert result is None


class TestDexterAlertMonitorResolveKey:
    def test_local_url_with_empty_key_uses_default(self):
        key = DexterAlertMonitor._resolve_api_key("http://warehouse:8080/alerts", "")
        assert key == "6fd7fa3c95c9296eb3fc376eea08146e"

    def test_local_url_with_custom_key_uses_custom(self):
        key = DexterAlertMonitor._resolve_api_key("http://warehouse:8080/alerts", "custom")
        assert key == "custom"

    def test_external_url_with_key(self):
        key = DexterAlertMonitor._resolve_api_key("https://example.com/api", "my-key")
        assert key == "my-key"

    def test_external_url_without_key_raises(self):
        with pytest.raises(ValueError):
            DexterAlertMonitor._resolve_api_key("https://example.com/api", "")


class TestDexterAlertMonitorBarCallback:
    def test_bar_complete_posts_webhook_on_signal(self):
        """When a signal fires, the webhook is called."""
        signals = []
        webhook_posts = []

        m = DexterAlertMonitor(symbol="TEST", on_signal=lambda s: signals.append(s))
        # Mock the webhook
        m._webhook = MagicMock()
        m._webhook.post = lambda sig, entry_exit: webhook_posts.append((sig, entry_exit))

        # Mock evaluator to always return a signal
        mock_signal = _make_signal(symbol="TEST")
        m._evaluator.on_bar = MagicMock(return_value=mock_signal)

        bar = PartialBar()
        bar.update(100.0, 1000.0)
        m._on_bar_complete(datetime(2026, 3, 31, 14, 30), bar)

        assert len(signals) == 1
        assert len(webhook_posts) == 1
        assert webhook_posts[0][1] == "entry"

    def test_bar_complete_no_webhook_when_no_signal(self):
        """When no signal fires, the webhook is not called."""
        m = DexterAlertMonitor(symbol="TEST")
        m._webhook = MagicMock()
        m._evaluator.on_bar = MagicMock(return_value=None)

        bar = PartialBar()
        bar.update(100.0, 1000.0)
        m._on_bar_complete(datetime(2026, 3, 31, 14, 30), bar)

        m._webhook.post.assert_not_called()


def _make_trending_df(n: int = 60, start_price: float = 100.0, trend: float = 0.1) -> pd.DataFrame:
    """Create a synthetic trending OHLCV DataFrame for testing."""
    from datetime import timedelta

    base = datetime(2026, 3, 28, 9, 30)
    timestamps = []
    data = []
    price = start_price

    for i in range(n):
        price += trend
        bar_open = price
        bar_high = price + 0.5
        bar_low = price - 0.3
        bar_close = price + 0.2
        bar_vol = 10000 + i * 100
        timestamps.append(base + timedelta(minutes=15 * i))
        data.append({"Open": bar_open, "High": bar_high, "Low": bar_low, "Close": bar_close, "Volume": bar_vol})

    idx = pd.DatetimeIndex(timestamps)
    return pd.DataFrame(data, index=idx)
