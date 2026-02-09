#!/usr/bin/env python3
"""
fetch.py — Download 1-minute bar data from Alpaca
Uses config for credentials and paths.

Usage:
    python3 fetch.py CATX FEED KRRO        # Fetch specific symbols
    python3 fetch.py                        # Re-fetch all existing data/*.csv symbols
"""

import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import pytz

import config as cfg


def fetch_data(symbols: list, days: int = 35):
    """Fetch 1-month of 1-min bars from Alpaca for given symbols."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    cfg.DATA_DIR.mkdir(exist_ok=True)
    client = StockHistoricalDataClient(cfg.APCA_API_KEY_ID, cfg.APCA_API_SECRET_KEY)

    et = pytz.timezone("America/New_York")
    end = datetime.now(et)
    start = end - timedelta(days=days)

    print(f"\n  Fetching 1-min bars: {start.date()} to {end.date()}")
    print(f"  Symbols: {len(symbols)}")

    success = 0
    skipped = 0

    for i, sym in enumerate(symbols, 1):
        outfile = cfg.DATA_DIR / f"{sym}_1Min.csv"
        if outfile.exists():
            mtime = datetime.fromtimestamp(outfile.stat().st_mtime, tz=et)
            if mtime.date() == end.date():
                print(f"  [{i:>2}/{len(symbols)}] {sym:>6} ... CACHED (today)")
                success += 1
                continue

        print(f"  [{i:>2}/{len(symbols)}] {sym:>6} ... ", end="", flush=True)
        try:
            req = StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
                feed=DataFeed.SIP,
            )
            bars = client.get_stock_bars(req)
            df = bars.df

            if df.empty:
                print("NO DATA")
                skipped += 1
                continue

            if isinstance(df.index, pd.MultiIndex):
                df = df.reset_index()
                df = df.drop(columns=["symbol"], errors="ignore")
                df = df.rename(columns={"timestamp": "timestamp_et"})
            else:
                df = df.reset_index()
                df = df.rename(columns={"timestamp": "timestamp_et"})

            df["timestamp_et"] = df["timestamp_et"].dt.tz_convert(et)
            df.to_csv(outfile, index=False)

            bars_count = len(df)
            days_count = df["timestamp_et"].dt.date.nunique()
            print(f"{bars_count:>7,} bars | {days_count} days")
            success += 1

        except Exception as e:
            print(f"ERROR: {e}")
            skipped += 1

        if i < len(symbols):
            time.sleep(0.3)

    print(f"  Done: {success} fetched, {skipped} skipped\n")
    return success


def main():
    if not cfg.APCA_API_KEY_ID or not cfg.APCA_API_SECRET_KEY:
        print("ERROR: Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env")
        sys.exit(1)

    if len(sys.argv) > 1:
        symbols = [s.upper() for s in sys.argv[1:]]
    else:
        existing = sorted(cfg.DATA_DIR.glob("*_1Min.csv"))
        if existing:
            symbols = [p.stem.replace("_1Min", "") for p in existing]
            print(f"  Re-fetching {len(symbols)} existing symbols from data/")
        else:
            print("  No symbols specified and no existing data. Pass symbols as arguments.")
            print("  Usage: python3 fetch.py CATX FEED KRRO")
            sys.exit(1)

    fetch_data(symbols)


if __name__ == "__main__":
    main()
