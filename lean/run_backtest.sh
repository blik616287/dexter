#!/bin/bash
# Run Shoulder Taps backtest via QuantConnect Lean Docker image
#
# Usage: ./run_backtest.sh [--equity]  (default: options mode)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Override trading mode if --equity flag passed
if [ "$1" = "--equity" ]; then
    echo "Running in EQUITY mode"
    # Modify config on-the-fly (future: use jq)
fi

echo "Starting Shoulder Taps backtest..."
echo "  Config: $SCRIPT_DIR/Launcher/config.json"
echo "  Algorithm: $SCRIPT_DIR/ShoulderTaps/"
echo "  Data: $SCRIPT_DIR/Data/"
echo "  Results: $SCRIPT_DIR/Results/"

docker run --rm \
    -v "$SCRIPT_DIR/Launcher/config.json:/Lean/Launcher/bin/Debug/config.json" \
    -v "$SCRIPT_DIR/Data:/Data:ro" \
    -v "$SCRIPT_DIR/Results:/Results" \
    -v "$SCRIPT_DIR/ShoulderTaps:/Lean/Algorithm.Python" \
    --name lean-shoulder-taps \
    quantconnect/lean:latest

echo ""
echo "Backtest complete. Results in: $SCRIPT_DIR/Results/"
