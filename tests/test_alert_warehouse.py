"""Tests for alert_warehouse.py — alert storage and HTTP API."""

from __future__ import annotations

import json
import threading
from http.server import HTTPServer

import pytest

from alert_warehouse import AlertHandler, AlertStore, parse_args


# ---------------------------------------------------------------------------
# AlertStore
# ---------------------------------------------------------------------------

class TestAlertStore:
    @pytest.fixture()
    def store(self, tmp_path):
        return AlertStore(db_path=str(tmp_path / "test.db"))

    @pytest.fixture()
    def sample_alert(self):
        return {
            "timestamp": "2026-03-31T14:45:00",
            "symbol": "AAPL",
            "price": 253.79,
            "alert_type": "DEXTER",
            "entry_exit": "entry",
            "side": "buy",
            "bar_size": "15m",
        }

    def test_insert_returns_id(self, store, sample_alert):
        alert_id = store.insert(sample_alert)
        assert alert_id == 1

    def test_insert_increments_id(self, store, sample_alert):
        id1 = store.insert(sample_alert)
        id2 = store.insert(sample_alert)
        assert id2 == id1 + 1

    def test_count(self, store, sample_alert):
        assert store.count() == 0
        store.insert(sample_alert)
        assert store.count() == 1
        store.insert(sample_alert)
        assert store.count() == 2

    def test_list_alerts_empty(self, store):
        result = store.list_alerts()
        assert result == []

    def test_list_alerts_returns_inserted(self, store, sample_alert):
        store.insert(sample_alert)
        alerts = store.list_alerts()
        assert len(alerts) == 1
        assert alerts[0]["symbol"] == "AAPL"
        assert alerts[0]["price"] == 253.79

    def test_list_alerts_filter_by_symbol(self, store, sample_alert):
        store.insert(sample_alert)
        msft = sample_alert.copy()
        msft["symbol"] = "MSFT"
        msft["price"] = 371.18
        store.insert(msft)

        aapl_only = store.list_alerts(symbol="AAPL")
        assert len(aapl_only) == 1
        assert aapl_only[0]["symbol"] == "AAPL"

    def test_list_alerts_limit(self, store, sample_alert):
        for _ in range(10):
            store.insert(sample_alert)
        alerts = store.list_alerts(limit=3)
        assert len(alerts) == 3

    def test_list_alerts_ordered_desc(self, store):
        for ts in ["2026-03-31T09:30:00", "2026-03-31T14:00:00", "2026-03-31T10:00:00"]:
            store.insert({
                "timestamp": ts, "symbol": "AAPL", "price": 100.0,
                "alert_type": "DEXTER", "entry_exit": "entry",
                "side": "buy", "bar_size": "15m",
            })
        alerts = store.list_alerts()
        timestamps = [a["timestamp"] for a in alerts]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_latest_per_symbol(self, store):
        store.insert({
            "timestamp": "2026-03-31T09:30:00", "symbol": "AAPL", "price": 250.0,
            "alert_type": "DEXTER", "entry_exit": "entry", "side": "buy", "bar_size": "15m",
        })
        store.insert({
            "timestamp": "2026-03-31T14:00:00", "symbol": "AAPL", "price": 255.0,
            "alert_type": "DEXTER", "entry_exit": "entry", "side": "buy", "bar_size": "15m",
        })
        store.insert({
            "timestamp": "2026-03-31T10:00:00", "symbol": "MSFT", "price": 370.0,
            "alert_type": "DEXTER", "entry_exit": "entry", "side": "sell", "bar_size": "15m",
        })

        latest = store.latest_per_symbol()
        symbols = {a["symbol"] for a in latest}
        assert symbols == {"AAPL", "MSFT"}
        aapl = next(a for a in latest if a["symbol"] == "AAPL")
        assert aapl["price"] == 255.0

    def test_stats(self, store):
        store.insert({
            "timestamp": "2026-03-31T09:30:00", "symbol": "AAPL", "price": 250.0,
            "alert_type": "DEXTER", "entry_exit": "entry", "side": "buy", "bar_size": "15m",
        })
        store.insert({
            "timestamp": "2026-03-31T10:00:00", "symbol": "AAPL", "price": 255.0,
            "alert_type": "DEXTER", "entry_exit": "exit", "side": "sell", "bar_size": "15m",
        })

        stats = store.stats()
        assert len(stats) == 1
        s = stats[0]
        assert s["symbol"] == "AAPL"
        assert s["total_alerts"] == 2
        assert s["buys"] == 1
        assert s["sells"] == 1
        assert s["entries"] == 1
        assert s["exits"] == 1


# ---------------------------------------------------------------------------
# HTTP API (AlertHandler)
# ---------------------------------------------------------------------------

class TestAlertAPI:
    @pytest.fixture()
    def server(self, tmp_path):
        """Start a test server and return (url, store)."""
        store = AlertStore(db_path=str(tmp_path / "test.db"))
        AlertHandler.store = store

        httpd = HTTPServer(("127.0.0.1", 0), AlertHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        yield f"http://127.0.0.1:{port}", store

        httpd.shutdown()

    def _post(self, url, payload, api_key="6fd7fa3c95c9296eb3fc376eea08146e"):
        import urllib.request
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{url}/alerts",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
        )
        return urllib.request.urlopen(req)

    def _get(self, url, path="/alerts"):
        import urllib.request
        return json.loads(urllib.request.urlopen(f"{url}{path}").read())

    @pytest.fixture()
    def valid_payload(self):
        return {
            "timestamp": "2026-03-31T14:45:00",
            "symbol": "AAPL",
            "price": 253.79,
            "alert_type": "DEXTER",
            "entry_exit": "entry",
            "side": "buy",
            "bar_size": "15m",
        }

    def test_post_stores_alert(self, server, valid_payload):
        url, store = server
        resp = self._post(url, valid_payload)
        assert resp.status == 201
        assert store.count() == 1

    def test_post_returns_id(self, server, valid_payload):
        url, _ = server
        resp = self._post(url, valid_payload)
        body = json.loads(resp.read())
        assert body["id"] == 1
        assert body["status"] == "stored"

    def test_post_bad_api_key(self, server, valid_payload):
        url, _ = server
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            self._post(url, valid_payload, api_key="wrong")
        assert exc_info.value.code == 401

    def test_post_missing_fields(self, server):
        url, _ = server
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            self._post(url, {"symbol": "AAPL"})
        assert exc_info.value.code == 400

    def test_post_invalid_entry_exit(self, server, valid_payload):
        url, _ = server
        valid_payload["entry_exit"] = "invalid"
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            self._post(url, valid_payload)
        assert exc_info.value.code == 400

    def test_post_invalid_side(self, server, valid_payload):
        url, _ = server
        valid_payload["side"] = "invalid"
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            self._post(url, valid_payload)
        assert exc_info.value.code == 400

    def test_get_alerts(self, server, valid_payload):
        url, _ = server
        self._post(url, valid_payload)
        result = self._get(url, "/alerts")
        assert result["count"] == 1
        assert result["alerts"][0]["symbol"] == "AAPL"

    def test_get_alerts_by_symbol(self, server, valid_payload):
        url, _ = server
        self._post(url, valid_payload)
        msft = valid_payload.copy()
        msft["symbol"] = "MSFT"
        self._post(url, msft)

        result = self._get(url, "/alerts?symbol=AAPL")
        assert result["count"] == 1

    def test_get_latest(self, server, valid_payload):
        url, _ = server
        self._post(url, valid_payload)
        result = self._get(url, "/alerts/latest")
        assert len(result["alerts"]) == 1

    def test_get_stats(self, server, valid_payload):
        url, _ = server
        self._post(url, valid_payload)
        result = self._get(url, "/alerts/stats")
        assert len(result["stats"]) == 1
        assert result["stats"][0]["total_alerts"] == 1

    def test_health(self, server):
        url, _ = server
        result = self._get(url, "/health")
        assert result["status"] == "ok"
        assert result["alerts_stored"] == 0

    def test_get_404(self, server):
        url, _ = server
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            self._get(url, "/nonexistent")
        assert exc_info.value.code == 404

    def test_post_404(self, server):
        url, _ = server
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            f"{url}/nonexistent",
            data=b'{}',
            headers={"Content-Type": "application/json", "X-API-Key": "6fd7fa3c95c9296eb3fc376eea08146e"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 404

    def test_post_empty_body(self, server):
        url, _ = server
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            f"{url}/alerts",
            data=b'',
            headers={"Content-Type": "application/json", "X-API-Key": "6fd7fa3c95c9296eb3fc376eea08146e"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400

    def test_post_invalid_json(self, server):
        url, _ = server
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            f"{url}/alerts",
            data=b'not json',
            headers={
                "Content-Type": "application/json",
                "Content-Length": "8",
                "X-API-Key": "6fd7fa3c95c9296eb3fc376eea08146e",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400


# ---------------------------------------------------------------------------
# CLI parse_args
# ---------------------------------------------------------------------------

class TestWarehouseParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.port == 8080
        assert args.db == "alerts.db"
        assert args.verbose is False

    def test_custom_port(self):
        args = parse_args(["--port", "9090"])
        assert args.port == 9090

    def test_custom_db(self):
        args = parse_args(["--db", "/tmp/my.db"])
        assert args.db == "/tmp/my.db"

    def test_verbose(self):
        args = parse_args(["-v"])
        assert args.verbose is True
