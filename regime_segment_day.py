"""Segment trade outcomes by the day-anchored regime features (DayExtLo,
DayExtHi, DayOS, DayVws) captured by run_regday_instrument.py.

Two questions, both cross-contract:
  1. trend-long (M5): do winners sit in a day-state losers don't share?
     (candidate gate: enable trend-long only in that state)
  2. M8 fades: do losers cluster in "trend-day" states?
     (candidate gate: suppress M8 when the day has gone directional)

Measurement only — the gate ships nothing until it separates on all 3.
"""
import csv
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
TAGS = ["NQZ25", "NQM5", "NQH6"]


def trades(path):
    out, prev = [], 0.0
    for r in csv.DictReader(open(path, newline="")):
        if r["Event"] != "EXIT":
            continue
        t = float(r["TotalPnL"])
        out.append(dict(
            pnl=t - prev, mode=r["Mode"], side=r["Side"], reason=r["ExitReason"],
            dxl=float(r["DayExtLo"]), dxh=float(r["DayExtHi"]),
            dos=float(r["DayOS"]), dvw=float(r["DayVws"]),
            date=r["Date"], time=r["Time"]))
        prev = t
    return out


def bucket(rows, key, edges, label):
    def bk(v):
        for e in edges:
            if v < e: return f"<{e}"
        return f">={edges[-1]}"
    g = defaultdict(lambda: [0, 0, 0.0])
    for t in rows:
        b = g[bk(key(t))]
        b[0] += 1
        if t["pnl"] > 0: b[1] += 1
        b[2] += t["pnl"]
    order = [f"<{e}" for e in edges] + [f">={edges[-1]}"]
    print(f"    {label}:")
    for k in order:
        if k not in g: continue
        n, w, net = g[k]
        print(f"      {k:>6}: n={n:>3}  WR={100*w/n:5.1f}%  net={net:>+9,.0f}")


def wl_means(rows, tag, name):
    w = [t for t in rows if t["pnl"] > 0]; l = [t for t in rows if t["pnl"] <= 0]
    def m(rs, k): return sum(t[k] for t in rs) / len(rs) if rs else float("nan")
    print(f"    W/L feature means ({len(w)}W / {len(l)}L):")
    for k, lab in (("dxl", "DayExtLo"), ("dxh", "DayExtHi"),
                   ("dos", "DayOS"), ("dvw", "DayVws")):
        print(f"      {lab:>8}:  W={m(w,k):+6.2f}   L={m(l,k):+6.2f}")


def main():
    for tag in TAGS:
        p = BASE / f"IOF_NQ_regday_{tag}.csv"
        if not p.exists():
            print(f"missing {p}"); continue
        tr = trades(p)
        trl = [t for t in tr if t["mode"] == "M5"]
        m8  = [t for t in tr if t["mode"] == "M8"]
        print("\n" + "=" * 66)
        print(f" {tag}: {len(tr)} trades  ({len(trl)} trend-long / {len(m8)} M8)")
        print("=" * 66)
        if trl:
            print(f"\n  trend-long (M5), net {sum(t['pnl'] for t in trl):+,.0f}:")
            wl_means(trl, tag, "M5")
            bucket(trl, lambda t: t["dxl"], [1.0, 2.0, 3.0], "DayExtLo (rally off day low, ATR)")
            bucket(trl, lambda t: t["dos"], [-0.3, 0.0, 0.3], "DayOS (VWAP one-sidedness)")
            bucket(trl, lambda t: t["dvw"], [-0.5, 0.0, 0.5], "DayVws (VWAP slope/ATR)")
        if m8:
            print(f"\n  M8 fades, net {sum(t['pnl'] for t in m8):+,.0f}:")
            wl_means(m8, tag, "M8")
            # fade-vs-trend-day: how one-sided/directional was the day at entry,
            # measured AGAINST the fade direction (fading a one-sided trend day
            # is the hypothesized loser). Signed: + = day trends against the fade.
            def against(t):
                d = t["dos"]
                return -d if t["side"] == "LONG" else d
            bucket(m8, against, [-0.3, 0.0, 0.3], "DayOS against fade direction")
            bucket(m8, lambda t: max(t["dxl"], t["dxh"]), [1.5, 2.5, 3.5],
                   "max day extension (ATR)")


if __name__ == "__main__":
    main()
