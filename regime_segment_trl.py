"""Segment iter-2 trade outcomes by the EXISTING regime columns (TrendReg /
ChopReg / VolReg) before building a new day-level classifier.

Question: does any already-computed regime state separate trend-long (M5)
winners from losers — and M8 fade losers from winners — consistently across
the 3 frozen contracts? If yes, the intervention A/B can gate on it directly;
if no, a new day-anchored classifier needs to earn its place first.

Reads the IOF_NQ_tri2_{tag}_trend.csv files written by backtest_trend_iter2_ab.py
(trend arm = TREND_LONG on, so M5 rows are trend-longs; M8 rows unchanged).
No backtest run needed.
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
        out.append(dict(pnl=t - prev, mode=r["Mode"], reason=r["ExitReason"],
                        tr=int(r["TrendReg"]), cr=int(r["ChopReg"]),
                        vr=int(r["VolReg"]), ts=float(r["TrendTS"]),
                        date=r["Date"], time=r["Time"]))
        prev = t
    return out


def seg(rows, key):
    g = defaultdict(lambda: [0, 0, 0.0])   # n, wins, net
    for t in rows:
        b = g[key(t)]
        b[0] += 1
        if t["pnl"] > 0: b[1] += 1
        b[2] += t["pnl"]
    return dict(sorted(g.items()))


def show(title, rows, key, label):
    print(f"\n  {title} by {label}:")
    for k, (n, w, net) in seg(rows, key).items():
        print(f"    {label}={k!s:>5}: n={n:>3}  WR={100*w/n:5.1f}%  net={net:>+9,.0f}")


def main():
    for tag in TAGS:
        p = BASE / f"IOF_NQ_tri2_{tag}_trend.csv"
        if not p.exists():
            print(f"missing {p}"); continue
        tr = trades(p)
        trl = [t for t in tr if t["mode"] == "M5"]
        m8  = [t for t in tr if t["mode"] == "M8"]
        print("\n" + "=" * 64)
        print(f" {tag}: {len(tr)} trades ({len(trl)} trend-long, {len(m8)} M8)")
        print("=" * 64)
        show("trend-long (M5)", trl, lambda t: t["tr"], "TrendReg")
        show("trend-long (M5)", trl, lambda t: t["cr"], "ChopReg")
        show("trend-long (M5)", trl, lambda t: t["vr"], "VolReg")
        # ts in coarse buckets
        show("trend-long (M5)", trl,
             lambda t: round(t["ts"] * 2) / 2, "ts(0.5)")
        show("M8 fades", m8, lambda t: t["tr"], "TrendReg")


if __name__ == "__main__":
    main()
