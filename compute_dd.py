#!/usr/bin/env python3
"""Compute max drawdown across the equity curve of a backtest CSV.

Uses the TotalPnL column on EXIT rows (running cumulative net P&L) as the
equity curve. Reports trades, net, MaxDD, biggest single loss, worst streak.
"""
import sys, csv, os

def dd_for(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r["Event"] != "EXIT":
                continue
            try:
                tot = float(r["TotalPnL"]) if r["TotalPnL"] else 0.0
            except ValueError:
                tot = 0.0
            rows.append((r["Date"], r["Time"], r["Mode"], r["ExitReason"], tot))
    if not rows:
        print(f"  {os.path.basename(path):60s}  no trades")
        return
    peak = 0.0; mdd = 0.0; mdd_at = None
    streak = 0; worst_streak = 0
    biggest_loss = 0.0
    prev_tot = 0.0
    for i,(d,t,m,xr,tot) in enumerate(rows):
        peak = max(peak, tot)
        dd = tot - peak
        if dd < mdd:
            mdd = dd
            mdd_at = (i+1, d, t, m, xr)
        per_trade = tot - prev_tot
        if per_trade < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
            biggest_loss = min(biggest_loss, per_trade)
        else:
            streak = 0
        prev_tot = tot
    name = os.path.basename(path).replace("IOF_NQ_prod_", "").replace(".csv","")
    final = rows[-1][4]
    print(f"  {name:55s}  N={len(rows):2d}  Net=${final:>8,.0f}  "
          f"MaxDD=${mdd:>8,.0f}  worstLossStreak={worst_streak}  "
          f"biggestLoss=${biggest_loss:>7,.0f}")
    if mdd_at:
        i,d,t,m,xr = mdd_at
        print(f"        bottom: trade #{i} ({d} {t} {m} {xr})")

if __name__ == "__main__":
    for p in sys.argv[1:]:
        dd_for(p)
