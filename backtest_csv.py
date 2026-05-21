#!/usr/bin/env python3
"""
Wrapper: feed pre-aggregated 3000-vol bars from a Sierra-Chart CSV export
(Date, Time, Open, High, Low, Last, Volume, # of Trades, OHLC Avg, HLC Avg,
HL Avg, Bid Volume, Ask Volume, ...) into the existing v12.25-py Backtester.

Usage:
    python backtest_csv.py <input.txt> [out.csv]
"""
import csv, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

from backtest import Bar, Backtester, write_csv, print_summary, BASE_DIR

ET = ZoneInfo("America/New_York")


def load_bars(path: str):
    bars = []
    with open(path, "r", newline="") as f:
        rdr = csv.reader(f)
        header = next(rdr)
        header = [h.strip() for h in header]
        # NOTE: SC exports often duplicate names ("Open", "High", "Low", "Close")
        # for secondary subgraphs. Keep the FIRST occurrence — that's the primary OHLC.
        col = {}
        for i, name in enumerate(header):
            if name not in col:
                col[name] = i

        def idx(*names):
            for n in names:
                if n in col:
                    return col[n]
            raise KeyError(names)

        c_date = idx("Date")
        c_time = idx("Time")
        c_open = idx("Open")
        c_high = idx("High")
        c_low  = idx("Low")
        c_last = idx("Last", "Close")
        c_vol  = idx("Volume")
        c_bid  = idx("Bid Volume")
        c_ask  = idx("Ask Volume")

        i = 0
        for row in rdr:
            if not row or len(row) <= c_ask:
                continue
            ds = row[c_date].strip()
            ts = row[c_time].strip()
            if not ds or not ts:
                continue
            y, m, d = (int(x) for x in ds.split("-"))
            hh, mm, rest = ts.split(":")
            hh, mm = int(hh), int(mm)
            ss = int(float(rest))
            us = int(round((float(rest) - int(float(rest))) * 1_000_000))
            dt = datetime(y, m, d, hh, mm, ss, us, tzinfo=ET)
            o = float(row[c_open]); h = float(row[c_high])
            lo = float(row[c_low]); c = float(row[c_last])
            v = int(float(row[c_vol]))
            bv = int(float(row[c_bid])); av = int(float(row[c_ask]))
            bars.append(Bar(
                dt=dt, open=o, high=h, low=lo, close=c,
                volume=v, bid_vol=bv, ask_vol=av,
                idx=i,
                date_tag=y * 10000 + m * 100 + d,
                hhmm=hh * 100 + mm,
            ))
            i += 1
    return bars


def main():
    if len(sys.argv) < 2:
        print("usage: python backtest_csv.py <input.txt> [out.csv]")
        sys.exit(1)
    src = sys.argv[1]
    stem = os.path.splitext(os.path.basename(src))[0]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE_DIR, f"IOF_NQ_backtest_{stem}.csv")

    print(f"Reading {src}")
    bars = load_bars(src)
    if not bars:
        print("ERROR: no bars parsed."); sys.exit(1)
    first, last = bars[0].dt, bars[-1].dt
    span_days = (last - first).days
    print(f"  {len(bars):,} bars  |  {first.date()} -> {last.date()}  ({span_days} days)")

    avg_vol = sum(b.volume for b in bars) / len(bars)
    print(f"  Avg bar volume: {avg_vol:.0f}  (target=3000)")

    print("Running backtest ...")
    bt = Backtester(bars)
    trades = bt.run()
    exits = sum(1 for t in trades if t.event == "EXIT")
    print(f"  Generated {len(trades):,} records  ({exits} completed trades)")

    write_csv(trades, out)
    print_summary(trades)
    print(f"\nCSV written to: {out}")
    return bt


if __name__ == "__main__":
    main()
