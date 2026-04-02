#!/usr/bin/env python3
"""Alert data warehouse — receives and stores DEXTER signal alerts.

Provides a POST endpoint for alert ingestion and GET endpoints for querying.
Stores alerts in a local SQLite database.

Usage::

    python3 alert_warehouse.py [--port 8080] [--db alerts.db]

Endpoints::

    POST /alerts          — Ingest an alert (requires X-API-Key header)
    GET  /alerts          — List all alerts (optional ?symbol=AAPL&limit=50)
    GET  /alerts/latest   — Most recent alert per symbol
    GET  /alerts/stats    — Aggregate stats per symbol
    GET  /health          — Health check
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

EXPECTED_API_KEY: str = "6fd7fa3c95c9296eb3fc376eea08146e"
DEFAULT_PORT: int = 8080
DEFAULT_DB: str = "alerts.db"

REQUIRED_FIELDS = {
    "timestamp", "symbol", "price", "alert_type",
    "entry_exit", "side", "bar_size",
}
VALID_ENTRY_EXIT = {"entry", "exit"}
VALID_SIDES = {"buy", "sell"}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class AlertStore:
    """SQLite-backed alert storage. Thread-safe."""

    def __init__(self, db_path: str = DEFAULT_DB) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    price           REAL NOT NULL,
                    alert_type      TEXT NOT NULL,
                    entry_exit      TEXT NOT NULL,
                    side            TEXT NOT NULL,
                    bar_size        TEXT NOT NULL,
                    strength        REAL,
                    entry_timestamp TEXT,
                    source          TEXT DEFAULT 'unknown',
                    received        TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alerts_symbol
                ON alerts (symbol, timestamp DESC)
            """)

    def insert(self, alert: dict) -> int:
        """Insert an alert and return its ID."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO alerts
                   (timestamp, symbol, price, alert_type, entry_exit, side,
                    bar_size, strength, entry_timestamp, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    alert["timestamp"],
                    alert["symbol"],
                    alert["price"],
                    alert["alert_type"],
                    alert["entry_exit"],
                    alert["side"],
                    alert["bar_size"],
                    alert.get("strength"),
                    alert.get("entry_timestamp"),
                    alert.get("source", "unknown"),
                ),
            )
            return cur.lastrowid

    def list_alerts(
        self,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return recent alerts, optionally filtered by symbol."""
        with self._lock, self._connect() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM alerts WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
                    (symbol.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def latest_per_symbol(self) -> list[dict]:
        """Return the most recent alert for each symbol."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM alerts
                WHERE id IN (
                    SELECT MAX(id) FROM alerts GROUP BY symbol
                )
                ORDER BY timestamp DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> list[dict]:
        """Return aggregate stats per symbol."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("""
                SELECT
                    symbol,
                    COUNT(*)                          AS total_alerts,
                    SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END)  AS buys,
                    SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) AS sells,
                    SUM(CASE WHEN entry_exit='entry' THEN 1 ELSE 0 END) AS entries,
                    SUM(CASE WHEN entry_exit='exit' THEN 1 ELSE 0 END)  AS exits,
                    MIN(timestamp)                    AS first_alert,
                    MAX(timestamp)                    AS last_alert
                FROM alerts
                GROUP BY symbol
                ORDER BY total_alerts DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def count(self) -> int:
        """Total alert count."""
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class AlertHandler(BaseHTTPRequestHandler):
    """Handles alert ingestion and query endpoints."""

    store: AlertStore  # set on the class by the server

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/alerts":
            self._respond(404, {"error": "not found"})
            return

        # Auth
        api_key = self.headers.get("X-API-Key", "")
        if api_key != EXPECTED_API_KEY:
            self._respond(401, {"error": "invalid or missing X-API-Key"})
            return

        # Parse body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._respond(400, {"error": "empty body"})
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid JSON"})
            return

        # Validate fields
        missing = REQUIRED_FIELDS - set(body.keys())
        if missing:
            self._respond(400, {"error": f"missing fields: {sorted(missing)}"})
            return

        if body["entry_exit"] not in VALID_ENTRY_EXIT:
            self._respond(400, {"error": f"entry_exit must be one of {VALID_ENTRY_EXIT}"})
            return

        if body["side"] not in VALID_SIDES:
            self._respond(400, {"error": f"side must be one of {VALID_SIDES}"})
            return

        # Store
        alert_id = self.store.insert(body)
        logger.info(
            "Alert #%d: %s %s %s %s @ $%.2f",
            alert_id, body["symbol"], body["alert_type"],
            body["entry_exit"], body["side"], body["price"],
        )

        # Print to console
        print(
            f"  [{datetime.now().strftime('%H:%M:%S')}] "
            f"#{alert_id}  {body['symbol']:<6} {body['alert_type']:<8} "
            f"{body['entry_exit']:<5} {body['side']:<4} "
            f"${body['price']:<10.2f} ({body['bar_size']})"
        )

        self._respond(201, {"id": alert_id, "status": "stored"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._respond(200, {
                "status": "ok",
                "alerts_stored": self.store.count(),
            })

        elif parsed.path == "/alerts":
            symbol = params.get("symbol", [None])[0]
            limit = int(params.get("limit", [50])[0])
            alerts = self.store.list_alerts(symbol=symbol, limit=limit)
            self._respond(200, {"count": len(alerts), "alerts": alerts})

        elif parsed.path == "/alerts/latest":
            latest = self.store.latest_per_symbol()
            self._respond(200, {"alerts": latest})

        elif parsed.path == "/alerts/stats":
            stats = self.store.stats()
            self._respond(200, {"stats": stats})

        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status: int, body: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, indent=2).encode("utf-8"))

    def log_message(self, format, *args) -> None:
        """Suppress default access log noise."""
        logger.debug(format, *args)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def run_server(port: int = DEFAULT_PORT, db_path: str = DEFAULT_DB) -> None:
    """Start the alert warehouse HTTP server."""
    store = AlertStore(db_path=db_path)
    AlertHandler.store = store

    server = HTTPServer(("0.0.0.0", port), AlertHandler)

    print("=" * 60)
    print("  ALERT WAREHOUSE")
    print("=" * 60)
    print(f"  Listening:  http://0.0.0.0:{port}")
    print(f"  Database:   {db_path}")
    print("  Endpoints:")
    print("    POST /alerts         — ingest alert (X-API-Key required)")
    print("    GET  /alerts         — list alerts (?symbol=AAPL&limit=50)")
    print("    GET  /alerts/latest  — most recent per symbol")
    print("    GET  /alerts/stats   — aggregate stats")
    print("    GET  /health         — health check")
    print(f"  {'─' * 56}")
    print()

    server.serve_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Alert data warehouse — receives and stores DEXTER alerts.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help="HTTP port (default: %(default)s)",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help="SQLite database path (default: %(default)s)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        run_server(port=args.port, db_path=args.db)
    except KeyboardInterrupt:
        print("\nShutdown.")


if __name__ == "__main__":
    main()
