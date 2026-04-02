#!/bin/bash
# Grid test for PT/Floor/SL combinations
# Each run takes ~3 seconds

cd /home/blik/Desktop/st1/lean

configs=(
  "1.5 1.0 1.5"
  "1.5 1.0 1.25"
  "1.5 0.75 1.5"
  "1.5 0.75 1.25"
  "1.5 1.25 1.5"
  "2.0 1.0 1.5"
  "2.0 1.25 1.5"
  "2.0 1.5 1.5"
)

echo "PT    Floor SL    Trips WR     P&L         PF      MaxDD       AvgHold"
echo "----- ----- ----- ----- ------ ----------- ------- ----------- -------"

for cfg in "${configs[@]}"; do
  read -r pt fl sl <<< "$cfg"

  # Update config.json with new parameters
  python3 -c "
import json
with open('Launcher/config.json') as f:
    c = json.load(f)
c['parameters']['atr_pt'] = '$pt'
c['parameters']['atr_floor'] = '$fl'
c['parameters']['atr_sl'] = '$sl'
with open('Launcher/config.json', 'w') as f:
    json.dump(c, f, indent=4)
"

  # Run backtest
  docker run --rm \
    -v $(pwd)/Launcher/config.json:/Lean/Launcher/bin/Debug/config.json \
    -v $(pwd)/Data:/Data \
    -v $(pwd)/Results:/Results \
    -v $(pwd)/ShoulderTaps:/Lean/Algorithm.Python \
    quantconnect/lean:latest 2>&1 > /dev/null

  # Parse results from log
  python3 -c "
import re
with open('Results/ShoulderTapsAlgorithm-log.txt') as f:
    log = f.read()

trips = re.search(r'Completed Trips:\s+(\d+)', log)
wr = re.search(r'Win Rate:\s+([\d.]+)%', log)
pnl = re.search(r'Total P&L:\s+\\\$([-\d.]+)', log)
pf = re.search(r'Profit Factor:\s+([\d.]+|None)', log)
dd = re.search(r'Max Drawdown:\s+\\\$([\d.]+)', log)
hold = re.search(r'Avg Hold Time:\s+(\d+) min', log)

print(f'$pt   $fl   $sl   {trips.group(1) if trips else \"?\":>5} {wr.group(1) if wr else \"?\":>5}% \${float(pnl.group(1)) if pnl else 0:>10.2f} {pf.group(1) if pf else \"?\":>7} \${float(dd.group(1)) if dd else 0:>10.2f} {hold.group(1) if hold else \"?\":>5}m')
"
done

# Clean up: remove grid test params from config
python3 -c "
import json
with open('Launcher/config.json') as f:
    c = json.load(f)
for k in ['atr_pt', 'atr_floor', 'atr_sl']:
    c['parameters'].pop(k, None)
with open('Launcher/config.json', 'w') as f:
    json.dump(c, f, indent=4)
"
