#!/usr/bin/env python3
"""Diagnose NQH6 ATR corruption. Walks raw .scid → volume bars → WilderATR
and identifies where the ATR explodes. Print bar-level detail for the
suspect dates (2026-02-17, 2026-02-18) where candidates show ATR > 1000.
"""
import os, sys
import numpy as np
import backtest as bt

SCID = r"C:/SierraChart/Data/NQH6.CME.scid"

def main():
    print(f"Reading {SCID}")
    scale = bt.detect_price_scale(SCID)
    print(f"  price_scale = ÷{scale:.0f}")
    recs = bt.read_scid(SCID)
    print(f"  raw records: {len(recs):,}")

    bars = bt.build_volume_bars(recs, price_scale=scale)
    print(f"  3000-vol bars: {len(bars):,}")
    print(f"  date range: {bars[0].dt} -> {bars[-1].dt}")

    # Sanity: bar OHLC range distribution
    ranges = np.array([b.high - b.low for b in bars])
    print(f"\n  bar (high-low) distribution:")
    print(f"    min={ranges.min():.2f}  p50={np.percentile(ranges,50):.2f}  "
          f"p99={np.percentile(ranges,99):.2f}  max={ranges.max():.2f}")

    # Identify bars with insane ranges (> 500 pts = clearly broken on NQ)
    huge = [(i, b) for i, b in enumerate(bars) if (b.high - b.low) > 500]
    print(f"\n  bars with range > 500 pts: {len(huge)}")
    for i, b in huge[:15]:
        print(f"    idx={i:>6}  {b.dt}  O={b.open:.2f} H={b.high:.2f} L={b.low:.2f} "
              f"C={b.close:.2f}  range={b.high-b.low:.2f}  vol={b.volume}")

    # Compute Wilder ATR exactly as backtest does
    atr_calc = bt.WilderATR(period=bt.ATR_PER)
    atrs = []
    for b in bars:
        v = atr_calc.update(b.high, b.low, b.close)
        atrs.append(v)
    atrs = np.array(atrs)
    print(f"\n  ATR distribution:")
    print(f"    min={atrs[atrs>0].min():.2f}  p50={np.percentile(atrs[atrs>0],50):.2f}  "
          f"p99={np.percentile(atrs[atrs>0],99):.2f}  max={atrs.max():.2f}")

    insane_atr = [(i, atrs[i], bars[i]) for i in range(len(bars)) if atrs[i] > 200]
    print(f"\n  bars with ATR > 200: {len(insane_atr)}")

    # Focus on the suspect dates from candidates_NQH6: 2026-02-17, 2026-02-18
    print(f"\n=== Suspect dates: 2026-02-17 and 2026-02-18 ===")
    for target_date in ("2026-02-17", "2026-02-18"):
        print(f"\n  --- {target_date} ---")
        idx_first = next((i for i, b in enumerate(bars)
                          if b.dt.strftime("%Y-%m-%d") == target_date), None)
        if idx_first is None:
            print(f"    no bars for this date"); continue
        # Find session boundary: previous date's last bar + first 30 bars of target
        prev_end = idx_first - 1
        print(f"  prev session last bar (idx {prev_end}):  "
              f"{bars[prev_end].dt}  C={bars[prev_end].close:.2f}  ATR={atrs[prev_end]:.2f}")
        # Print first 35 bars of the target date
        idx_last = next((i for i, b in enumerate(bars[idx_first:], idx_first)
                         if b.dt.strftime("%Y-%m-%d") != target_date), len(bars))
        n_today = idx_last - idx_first
        print(f"  total bars on {target_date}: {n_today}")
        # Print first 5 and any bar where ATR > 200
        print(f"  {'idx':>6} {'time':>8} {'hhmm':>5}  {'O':>9} {'H':>9} {'L':>9} {'C':>9} "
              f"{'range':>8}  {'ATR':>10}")
        printed = 0
        for j in range(idx_first, min(idx_first + 5, idx_last)):
            b = bars[j]
            print(f"  {j:>6} {b.dt.strftime('%H:%M:%S'):>8} {b.hhmm:>5}  "
                  f"{b.open:>9.2f} {b.high:>9.2f} {b.low:>9.2f} {b.close:>9.2f} "
                  f"{b.high-b.low:>8.2f}  {atrs[j]:>10.2f}")
        # Then any bar with ATR > 200
        for j in range(idx_first, idx_last):
            if atrs[j] > 200:
                b = bars[j]
                print(f"  {j:>6} {b.dt.strftime('%H:%M:%S'):>8} {b.hhmm:>5}  "
                      f"{b.open:>9.2f} {b.high:>9.2f} {b.low:>9.2f} {b.close:>9.2f} "
                      f"{b.high-b.low:>8.2f}  {atrs[j]:>10.2f}  <<-- HIGH ATR")
                printed += 1
                if printed > 12: print(f"    (more...)"); break

    # Check session-boundary handling: prev_close vs first bar of session
    print(f"\n=== Session-boundary TR check (gap from prev session close to first bar) ===")
    for j in range(1, len(bars)):
        prev = bars[j-1]; cur = bars[j]
        if prev.dt.date() != cur.dt.date():
            tr = max(cur.high - cur.low,
                     abs(cur.high - prev.close),
                     abs(cur.low  - prev.close))
            if tr > 100:
                print(f"  {cur.dt}  prev_close={prev.close:.2f}  "
                      f"cur_OHLC=({cur.open:.2f},{cur.high:.2f},{cur.low:.2f},{cur.close:.2f})  "
                      f"TR={tr:.2f}")

if __name__ == "__main__":
    main()
