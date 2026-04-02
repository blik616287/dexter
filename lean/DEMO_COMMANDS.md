# Shoulder Taps — Lean Backtest Demo Commands

All commands run from the `lean/` directory:
```bash
cd ~/Desktop/st1/lean
```

---

## 1. Run a Backtest

**Full backtest (equity mode, ~3 seconds):**
```bash
docker run --rm \
  -v $(pwd)/Launcher/config.json:/Lean/Launcher/bin/Debug/config.json \
  -v $(pwd)/Data:/Data \
  -v $(pwd)/Results:/Results \
  -v $(pwd)/ShoulderTaps:/Lean/Algorithm.Python \
  quantconnect/lean:latest
```

**Quick sanity check (just see if it starts):**
```bash
docker run --rm \
  -v $(pwd)/Launcher/config.json:/Lean/Launcher/bin/Debug/config.json \
  -v $(pwd)/Data:/Data \
  -v $(pwd)/Results:/Results \
  -v $(pwd)/ShoulderTaps:/Lean/Algorithm.Python \
  quantconnect/lean:latest 2>&1 | grep -E "(INIT|ERROR|COMPLETE|STATISTICS)"
```

---

## 2. View Results Summary

**Lean built-in statistics (Sharpe, win rate, P&L, etc.):**
```bash
cat Results/ShoulderTapsAlgorithm-summary.json | python3 -m json.tool | grep -A 30 '"statistics"'
```

**Runtime stats (one-liner):**
```bash
cat Results/ShoulderTapsAlgorithm-summary.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for k, v in d['runtimeStatistics'].items():
    print(f'  {k:30s} {v}')
"
```

**Trade statistics (detailed Lean analysis):**
```bash
cat Results/ShoulderTapsAlgorithm-summary.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
ts = d['totalPerformance']['tradeStatistics']
for k, v in ts.items():
    print(f'  {k:40s} {v}')
"
```

---

## 3. View the Signal Log

**All signals and orders (full chronological log):**
```bash
cat Results/ShoulderTapsAlgorithm-log.txt
```

**Just Alpha model signals (no orders or system messages):**
```bash
grep -E '^\d{4}-\d{2}-\d{2}.*\[(dexter|ensemble_|bt_divergence)\]' Results/ShoulderTapsAlgorithm-log.txt
```

**Just order fills:**
```bash
grep '\[ORDER\]' Results/ShoulderTapsAlgorithm-log.txt
```

**The final metrics summary block:**
```bash
grep -A 20 'SHOULDER TAPS BACKTEST COMPLETE' Results/ShoulderTapsAlgorithm-log.txt
```

---

## 4. Filter by Alpha Model

**Dexter signals only:**
```bash
grep '\[dexter\]' Results/ShoulderTapsAlgorithm-log.txt
```

**Ensemble B signals only:**
```bash
grep '\[ensemble_b\]' Results/ShoulderTapsAlgorithm-log.txt
```

**BT Divergence signals (may be empty in short backtests):**
```bash
grep '\[bt_divergence\]' Results/ShoulderTapsAlgorithm-log.txt
```

**Count signals per model:**
```bash
echo "Dexter:       $(grep -c '\[dexter\]' Results/ShoulderTapsAlgorithm-log.txt)"
echo "Ensemble A:   $(grep -c '\[ensemble_a\]' Results/ShoulderTapsAlgorithm-log.txt)"
echo "Ensemble B:   $(grep -c '\[ensemble_b\]' Results/ShoulderTapsAlgorithm-log.txt)"
echo "Ensemble C:   $(grep -c '\[ensemble_c\]' Results/ShoulderTapsAlgorithm-log.txt)"
echo "BT Divergence:$(grep -c '\[bt_divergence\]' Results/ShoulderTapsAlgorithm-log.txt)"
```

---

## 5. Filter by Symbol

**SPY trades only:**
```bash
grep 'SPY' Results/ShoulderTapsAlgorithm-log.txt | grep -E '\[(ORDER|dexter|ensemble)'
```

**MSFT trades only:**
```bash
grep 'MSFT' Results/ShoulderTapsAlgorithm-log.txt | grep -E '\[(ORDER|dexter|ensemble)'
```

**NVDA trades only:**
```bash
grep 'NVDA' Results/ShoulderTapsAlgorithm-log.txt | grep -E '\[(ORDER|dexter|ensemble)'
```

---

## 6. Analyze Order Events

**Total order count and direction breakdown:**
```bash
cat Results/ShoulderTapsAlgorithm-order-events.json | python3 -c "
import json, sys
events = json.load(sys.stdin)
fills = [e for e in events if e['status'] == 'filled']
buys = [e for e in fills if e['direction'] == 'buy']
sells = [e for e in fills if e['direction'] == 'sell']
print(f'Total fills: {len(fills)}')
print(f'  Buys:  {len(buys)}')
print(f'  Sells: {len(sells)}')
print()
symbols = set(e['symbolValue'] for e in fills)
for sym in sorted(symbols):
    sym_fills = [e for e in fills if e['symbolValue'] == sym]
    total_vol = sum(abs(e['fillQuantity']) * e['fillPrice'] for e in sym_fills)
    print(f'  {sym}: {len(sym_fills)} fills, \${total_vol:,.0f} volume')
"
```

**Trade P&L per round trip (entry → EOD close):**
```bash
cat Results/ShoulderTapsAlgorithm-order-events.json | python3 -c "
import json, sys
from datetime import datetime
events = json.load(sys.stdin)
fills = [e for e in events if e['status'] == 'filled']

# Pair entries with exits
positions = {}
trades = []
for e in fills:
    sym = e['symbolValue']
    qty = e['fillQuantity']
    price = e['fillPrice']
    if sym not in positions:
        positions[sym] = {'qty': 0, 'cost': 0}
    pos = positions[sym]
    if pos['qty'] == 0:
        pos['qty'] = qty
        pos['cost'] = price
        pos['entry_time'] = e['time']
    else:
        pnl = (price - pos['cost']) * pos['qty']
        trades.append({
            'symbol': sym,
            'direction': 'LONG' if pos['qty'] > 0 else 'SHORT',
            'entry': pos['cost'],
            'exit': price,
            'pnl': pnl * (100 if abs(pos['qty']) == 100 else 1),
        })
        pos['qty'] = 0
        pos['cost'] = 0

print(f'{'Symbol':<6} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'P&L':>10}')
print('-' * 48)
total = 0
for t in trades:
    p = t['pnl']
    total += p
    print(f\"{t['symbol']:<6} {t['direction']:<6} {t['entry']:>10.2f} {t['exit']:>10.2f} {p:>+10.2f}\")
print('-' * 48)
print(f\"{'TOTAL':<34} {total:>+10.2f}\")
"
```

---

## 7. Data Management

**Re-download latest 1m data from Yahoo Finance:**
```bash
python3 download_data.py
```

**Check how much data we have per symbol:**
```bash
echo "=== Equity Minute Data ==="
for dir in Data/equity/usa/minute/*/; do
    ticker=$(basename "$dir")
    count=$(ls "$dir"*.zip 2>/dev/null | wc -l)
    echo "  $ticker: $count day files"
done
echo ""
echo "=== Index Minute Data ==="
for dir in Data/index/usa/minute/*/; do
    ticker=$(basename "$dir")
    count=$(ls "$dir"*.zip 2>/dev/null | wc -l)
    echo "  $ticker: $count day files"
done
```

**Peek inside a data file (see raw bar format):**
```bash
# Pick any zip file and show first 5 bars
file=$(ls Data/equity/usa/minute/spy/*.zip | head -1)
echo "File: $file"
python3 -c "
import zipfile, sys
with zipfile.ZipFile('$file') as z:
    for name in z.namelist():
        with z.open(name) as f:
            lines = f.read().decode().strip().split('\n')
            print(f'Bars in file: {len(lines)}')
            print('Format: Milliseconds,Open,High,Low,Close,Volume')
            print('(Prices in 10000ths of dollar)')
            print()
            for line in lines[:5]:
                print(f'  {line}')
            print('  ...')
"
```

---

## 8. Change Backtest Parameters

Parameters are in `Launcher/config.json`. Edit them to change behavior:

**Switch to equity mode (100-share trades, no options):**
```bash
python3 -c "
import json
with open('Launcher/config.json') as f: cfg = json.load(f)
cfg['parameters']['trading_mode'] = 'equity'
with open('Launcher/config.json', 'w') as f: json.dump(cfg, f, indent=4)
print('Set trading_mode=equity')
"
```

**Switch to options mode (1 contract per signal):**
```bash
python3 -c "
import json
with open('Launcher/config.json') as f: cfg = json.load(f)
cfg['parameters']['trading_mode'] = 'options'
with open('Launcher/config.json', 'w') as f: json.dump(cfg, f, indent=4)
print('Set trading_mode=options')
"
```

**Change starting cash:**
```bash
python3 -c "
import json
with open('Launcher/config.json') as f: cfg = json.load(f)
cfg['parameters']['cash'] = '200000'
with open('Launcher/config.json', 'w') as f: json.dump(cfg, f, indent=4)
print('Set cash=200000')
"
```

**View current parameters:**
```bash
cat Launcher/config.json | python3 -c "
import json, sys
cfg = json.load(sys.stdin)
print('Current parameters:')
for k, v in cfg['parameters'].items():
    print(f'  {k}: {v}')
"
```

---

## 9. Inspect Alpha Model Code

**See which symbols and timeframes each model uses:**
```bash
grep -n "SYMBOLS\|_tf_label\|_lookback\|_cooldown" \
  ShoulderTaps/alpha/bt_divergence.py \
  ShoulderTaps/alpha/dexter.py \
  ShoulderTaps/alpha/ensemble_a.py \
  ShoulderTaps/alpha/ensemble_b.py \
  ShoulderTaps/alpha/ensemble_c.py
```

**See gate thresholds for Dexter:**
```bash
grep -n "ATR_COMPRESS\|RVOL_MIN\|BREAKOUT\|SLOPE" ShoulderTaps/alpha/dexter.py
```

**See gate thresholds for Ensemble A (7-gate model):**
```bash
grep -n "SMA_SLOPE\|FC_THRESH\|SAM_THRESH\|RVOL\|SECTOR\|VIX" ShoulderTaps/alpha/ensemble_a.py
```

---

## 10. Full Engine Log (Verbose)

**Complete Lean engine output (includes data loading, subscriptions, timing):**
```bash
cat Results/log.txt | head -100
```

**Data request success/failure report:**
```bash
cat Results/data-monitor-report-*.json | python3 -m json.tool
```

**See which data files failed to load:**
```bash
cat Results/failed-data-requests-*.txt | head -30
```

**See which data files loaded successfully:**
```bash
cat Results/succeeded-data-requests-*.txt | head -30
```

---

## 11. Compare Runs

**Save current results before re-running:**
```bash
mkdir -p Results/archive
cp Results/ShoulderTapsAlgorithm-summary.json Results/archive/run_$(date +%Y%m%d_%H%M%S).json
```

**Diff two saved runs:**
```bash
# After saving at least two runs:
files=(Results/archive/run_*.json)
if [ ${#files[@]} -ge 2 ]; then
    echo "=== Run 1 ==="
    cat "${files[-2]}" | python3 -c "import json,sys; [print(f'  {k}: {v}') for k,v in json.load(sys.stdin)['statistics'].items()]"
    echo ""
    echo "=== Run 2 ==="
    cat "${files[-1]}" | python3 -c "import json,sys; [print(f'  {k}: {v}') for k,v in json.load(sys.stdin)['statistics'].items()]"
else
    echo "Need at least 2 saved runs. Run the backtest again and save."
fi
```

---

## 12. Quick Health Check

**One command to verify the full pipeline:**
```bash
docker run --rm \
  -v $(pwd)/Launcher/config.json:/Lean/Launcher/bin/Debug/config.json \
  -v $(pwd)/Data:/Data \
  -v $(pwd)/Results:/Results \
  -v $(pwd)/ShoulderTaps:/Lean/Algorithm.Python \
  quantconnect/lean:latest 2>&1 | grep -E "(INIT|ERROR|COMPLETE|Total Orders|Net Profit|Sharpe|Win Rate)" \
  && echo "" \
  && echo "--- Custom Metrics ---" \
  && grep -A 12 'BACKTEST COMPLETE' Results/ShoulderTapsAlgorithm-log.txt | tail -11
```
