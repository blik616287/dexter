#!/usr/bin/env python3
"""Pull recent live algorithm logs from QuantConnect API."""

import hashlib
import json
import sys
import time
from pathlib import Path

import requests

CREDS_PATH = Path.home() / ".lean" / "credentials"
PROJECT_ID = 28278775


def qc_request(method, path, body=None):
    creds = json.loads(CREDS_PATH.read_text())
    user_id = str(creds["user-id"])
    api_token = creds["api-token"]

    ts = str(int(time.time()))
    auth_hash = hashlib.sha256(f"{api_token}:{ts}".encode()).hexdigest()

    resp = getattr(requests, method)(
        f"https://www.quantconnect.com/api/v2/{path}",
        auth=(user_id, auth_hash),
        headers={"Timestamp": ts, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    return resp.json()


def get_deploy_id():
    data = qc_request("get", f"live/read?projectId={PROJECT_ID}")
    return data.get("LiveResults", {}).get("DeployId", "")


def main():
    # Get current status
    status = qc_request("get", f"live/read?projectId={PROJECT_ID}")
    deploy_id = ""
    if status.get("success") is not False:
        lr = status.get("LiveResults", {})
        deploy_id = lr.get("DeployId", "")
        rs = lr.get("Results", lr).get("RuntimeStatistics", {})
        orders = lr.get("Results", lr).get("Orders", {})
        if rs:
            print("=== Live Status ===")
            for k, v in rs.items():
                print(f"  {k}: {v}")
            print(f"  Orders: {len(orders)}")
            print()

    # Get logs
    now = int(time.time())
    lookback = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
    data = qc_request("post", "live/read/log", {
        "projectId": PROJECT_ID,
        "algorithmId": deploy_id,
        "start": now - lookback,
        "end": now,
    })

    logs = data.get("LiveLogs", data.get("logs", []))
    if isinstance(logs, list):
        # Filter to interesting lines (skip warm-up noise)
        for line in logs:
            if any(k in line for k in [
                "dexter", "DEXTER", "INVALIDATED", "BULL", "BEAR",
                "initialized", "warming", "finished warm",
                "Error", "error", "Exception",
            ]):
                print(line)
        if not any("dexter" in line.lower() or "bull" in line.lower() or "bear" in line.lower() for line in logs):
            print("(No signal activity in logs — showing last 10 lines)")
            for line in logs[-10:]:
                print(line)
    else:
        print(f"Unexpected log format: {type(logs)}")


if __name__ == "__main__":
    main()
