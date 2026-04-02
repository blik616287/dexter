"""Download minute-resolution market data and convert to Lean format.

Lean expects equity minute data as:
  Data/equity/usa/minute/{ticker}/{YYYYMMDD}_trade.zip
Each zip contains a CSV: {YYYYMMDD}_trade.csv with columns:
  Milliseconds,Open,High,Low,Close,Volume
  (Milliseconds = ms from midnight; prices in 10000ths of dollar)

For index data (VIX):
  Data/index/usa/minute/{ticker}/{YYYYMMDD}_trade.zip
"""

import os
import csv
import zipfile
import io
from datetime import datetime, timedelta
import yfinance as yf

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Data")

# Symbols to download
EQUITY_SYMBOLS = ["SPY", "MSFT", "NVDA",
                  "AAPL", "GOOGL", "AMZN", "META", "AVGO",
                  "JPM", "V", "GS", "UNH", "LLY",
                  "COST", "HD", "CRM",
                  "XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
                  "XLP", "XLU", "XLB", "XLRE", "XLC"]
INDEX_SYMBOLS = ["^VIX"]  # yfinance uses ^VIX for VIX

# Date range — Yahoo limits 1m data to last 30 days
# Use recent dates for testing; switch to QuantConnect data for full backtest
START_DATE = "2026-01-20"   # ~4 weeks ago (within 30-day 1m limit)
END_DATE = "2026-02-18"     # today+1


def ms_from_midnight(dt):
    """Convert a datetime to milliseconds from midnight."""
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((dt - midnight).total_seconds() * 1000)


def price_to_lean(price):
    """Convert price to Lean's scaled integer format (10000x)."""
    return int(round(price * 10000))


def download_and_convert(ticker, is_index=False):
    """Download minute data for a single ticker and write Lean zip files."""
    yf_ticker = ticker
    lean_ticker = ticker.lower().replace("^", "")

    if is_index:
        data_subdir = os.path.join(DATA_DIR, "index", "usa", "minute", lean_ticker)
    else:
        data_subdir = os.path.join(DATA_DIR, "equity", "usa", "minute", lean_ticker)

    os.makedirs(data_subdir, exist_ok=True)

    print(f"Downloading {yf_ticker}...")

    # yfinance limits 1m data to 8 days at a time, so we chunk
    # For 1m data: max 7 days per request, max 30 days history
    # Use 5m or 2m for longer periods, but Lean consolidators handle the rest
    # Actually, let's use 1m intervals with chunked downloads

    start = datetime.strptime(START_DATE, "%Y-%m-%d")
    end = datetime.strptime(END_DATE, "%Y-%m-%d")

    all_data = []
    current = start

    while current < end:
        chunk_end = min(current + timedelta(days=5), end)
        try:
            df = yf.download(
                yf_ticker,
                start=current.strftime("%Y-%m-%d"),
                end=chunk_end.strftime("%Y-%m-%d"),
                interval="1m",
                progress=False,
                auto_adjust=True,
            )
            if not df.empty:
                all_data.append(df)
                print(f"  {yf_ticker}: {current.date()} to {chunk_end.date()} => {len(df)} rows")
            else:
                print(f"  {yf_ticker}: {current.date()} to {chunk_end.date()} => no data")
        except Exception as e:
            print(f"  {yf_ticker}: {current.date()} to {chunk_end.date()} => error: {e}")

        current = chunk_end

    if not all_data:
        print(f"  WARNING: No data for {yf_ticker}")
        return 0

    import pandas as pd
    df = pd.concat(all_data)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep='first')]

    # Handle MultiIndex columns from yfinance
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Group by date and write zip files
    df['date'] = df.index.date
    dates = df['date'].unique()
    total_bars = 0

    for date in dates:
        day_data = df[df['date'] == date]
        date_str = date.strftime("%Y%m%d")
        csv_name = f"{date_str}_trade.csv"
        zip_path = os.path.join(data_subdir, f"{date_str}_trade.zip")

        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)

        for idx, row in day_data.iterrows():
            ms = ms_from_midnight(idx.to_pydatetime())
            o = price_to_lean(row['Open'])
            h = price_to_lean(row['High'])
            l = price_to_lean(row['Low'])
            c = price_to_lean(row['Close'])
            v = int(row['Volume']) if not pd.isna(row['Volume']) else 0
            writer.writerow([ms, o, h, l, c, v])
            total_bars += 1

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(csv_name, csv_buffer.getvalue())

    print(f"  {yf_ticker}: Wrote {len(dates)} day files, {total_bars} total bars")
    return total_bars


def main():
    print("=" * 60)
    print("Lean Data Download & Conversion")
    print(f"Period: {START_DATE} to {END_DATE}")
    print("=" * 60)

    total = 0

    # Equities
    for ticker in EQUITY_SYMBOLS:
        bars = download_and_convert(ticker, is_index=False)
        total += bars

    # Index (VIX)
    for ticker in INDEX_SYMBOLS:
        bars = download_and_convert(ticker, is_index=True)
        total += bars

    print("=" * 60)
    print(f"Done. Total bars written: {total}")
    print(f"Data directory: {DATA_DIR}")


if __name__ == "__main__":
    main()
