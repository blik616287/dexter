#!/usr/bin/env python3
"""Run sequential backtests on QC with different parameters.

Tests bar sizes (1m, 5m, 10m, 15m) × exit params (PT/Floor/SL).

Usage:
    python3 python/grid_test.py
"""

import hashlib
import json
import sys
import time
from pathlib import Path

import requests

CREDS_PATH = Path.home() / ".lean" / "credentials"
PROJECT_ID = 28278775
CONFIG_PATH = Path("lean/ShoulderTaps/config.json")
DEXTER_PATH = Path("lean/ShoulderTaps/alpha/dexter.py")

# Grid: bar_minutes, pt, floor, sl
GRID = [
    # Bar size tests (with best exit params so far)
    (5,  2.0, 1.0, 1.0, "5m-wide-pt-tight-sl"),
    (10, 2.0, 1.0, 1.0, "10m-wide-pt-tight-sl"),
    (15, 2.0, 1.0, 1.0, "15m-wide-pt-tight-sl"),
    # 10m with different exits
    (10, 1.5, 0.75, 1.5, "10m-baseline"),
    (10, 3.0, 1.0, 0.75, "10m-3x-pt-tight-sl"),
    (10, 2.0, 1.0, 1.0, "10m-wide-pt-tight-sl-2"),
    # 15m with different exits
    (15, 1.5, 0.75, 1.5, "15m-baseline"),
    (15, 3.0, 1.0, 0.75, "15m-3x-pt-tight-sl"),
]


def qc_api(method, path, body=None):
    creds = json.loads(CREDS_PATH.read_text())
    user_id = str(creds["user-id"])
    api_token = creds["api-token"]
    ts = str(int(time.time()))
    auth_hash = hashlib.sha256(f"{api_token}:{ts}".encode()).hexdigest()
    func = getattr(requests, method)
    resp = func(
        f"https://www.quantconnect.com/api/v2/{path}",
        auth=(user_id, auth_hash),
        headers={"Timestamp": ts, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    return resp.json()


def update_config(pt, floor, sl):
    config = json.loads(CONFIG_PATH.read_text())
    config["parameters"]["atr_pt"] = str(pt)
    config["parameters"]["atr_floor"] = str(floor)
    config["parameters"]["atr_sl"] = str(sl)
    CONFIG_PATH.write_text(json.dumps(config, indent=4) + "\n")


def update_bar_size(minutes):
    """Update _BAR_MINUTES in dexter.py."""
    content = DEXTER_PATH.read_text()
    import re
    content = re.sub(r'_BAR_MINUTES = \d+', f'_BAR_MINUTES = {minutes}', content)
    # Also update timeframe_label
    tf_label = f"{minutes}m"
    content = re.sub(
        r'timeframe_label="\d+m"',
        f'timeframe_label="{tf_label}"',
        content,
    )
    DEXTER_PATH.write_text(content)


def push():
    import subprocess
    result = subprocess.run(
        ["lean", "cloud", "push", "--project", "ShoulderTaps", "--force"],
        capture_output=True, text=True, cwd="lean",
    )
    return result.returncode == 0


def run_backtest(label):
    import subprocess
    result = subprocess.run(
        ["lean", "cloud", "backtest", "ShoulderTaps", "--name", label],
        capture_output=True, text=True, cwd="lean",
    )
    # Extract backtest ID from URL
    for line in (result.stdout + result.stderr).split("\n"):
        if "quantconnect.com/project/" in line:
            parts = line.strip().split("/")
            bt_id = parts[-1]
            if len(bt_id) >= 30:
                return bt_id
    return None


def wait_for_backtest(bt_id, timeout=600):
    start = time.time()
    while time.time() - start < timeout:
        data = qc_api("get", f"backtests/read?projectId={PROJECT_ID}&backtestId={bt_id}")
        bt = data.get("backtest", {})
        if bt.get("error"):
            return {"error": bt["error"][:200]}
        if bt.get("completed"):
            return bt.get("statistics", {})
        pct = bt.get("progress", 0)
        sys.stdout.write(f"\r  Progress: {pct:.0%}   ")
        sys.stdout.flush()
        time.sleep(10)
    return {"error": "timeout"}


def main():
    results = []

    for bar_min, pt, floor, sl, label in GRID:
        print(f"\n{'='*60}")
        print(f"  {label}: bar={bar_min}m  PT={pt}x  Floor={floor}x  SL={sl}x")
        print(f"{'='*60}")

        update_config(pt, floor, sl)
        update_bar_size(bar_min)

        if not push():
            print("  Push failed, skipping")
            continue

        bt_id = run_backtest(label)
        if bt_id is None:
            print("  Failed to start backtest")
            continue

        print(f"  Backtest: {bt_id}")
        stats = wait_for_backtest(bt_id)
        print()

        if "error" in stats:
            print(f"  Error: {stats['error']}")
            continue

        row = {
            "label": label, "bar": bar_min, "pt": pt, "floor": floor, "sl": sl,
            "wr": stats.get("Win Rate", "?"),
            "net": stats.get("Net Profit", "?"),
            "sharpe": stats.get("Sharpe Ratio", "?"),
            "dd": stats.get("Drawdown", "?"),
            "orders": stats.get("Total Orders", "?"),
            "avg_win": stats.get("Average Win", "?"),
            "avg_loss": stats.get("Average Loss", "?"),
            "plr": stats.get("Profit-Loss Ratio", "?"),
        }
        results.append(row)
        print(f"  WR={row['wr']}  Net={row['net']}  Sharpe={row['sharpe']}  "
              f"DD={row['dd']}  PLR={row['plr']}  Orders={row['orders']}")

    # Summary
    print(f"\n\n{'='*100}")
    print("GRID TEST RESULTS")
    print(f"{'='*100}")
    print(f"{'Label':<25} {'Bar':>4} {'PT':>4} {'FL':>4} {'SL':>4} "
          f"{'WR':>5} {'Net':>9} {'Sharpe':>8} {'DD':>7} {'PLR':>5} {'Orders':>7}")
    print("-" * 100)
    for r in sorted(results, key=lambda x: float(str(x['net']).replace('%','').replace(',','').replace('$','') or '0'), reverse=True):
        print(f"{r['label']:<25} {r['bar']:>4} {r['pt']:>4} {r['floor']:>4} {r['sl']:>4} "
              f"{r['wr']:>5} {r['net']:>9} {r['sharpe']:>8} {r['dd']:>7} {r['plr']:>5} {r['orders']:>7}")

    # Restore defaults
    update_config(1.5, 0.75, 1.5)
    update_bar_size(10)
    push()


if __name__ == "__main__":
    main()
