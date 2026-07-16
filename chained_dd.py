"""
Account-level (chained) equity analysis across the 6 frozen contracts.

Why: run_v13_projection.py / run_imbfix_ab.py report MaxDD PER CONTRACT and
mean/mo over CONTRACT-months. Both understate account reality:

  1. MaxDD: a per-contract MaxDD only sees drawdown inside one contract's
     window. A real account rolls front-month continuously, so a losing streak
     that straddles a roll compounds across contracts and is invisible per-file.

  2. Month count: contract windows are non-overlapping but adjacent windows
     share a ROLL MONTH (e.g. NQM5 ends 2025-06-10, NQU25 starts 2025-06-18 ->
     June-2025 counted twice). Dividing pooled P&L by 20 "contract-months" when
     the calendar only spans ~16 months understates $/month.

This pools every trade across contracts on a single equity curve ordered by
timestamp, verifies the windows really are non-overlapping (chaining is only
valid if they are), and reports true account MaxDD + calendar-month stats.

Usage:  python chained_dd.py <glob-prefix>
        python chained_dd.py IOF_NQ_imbab_*_imb_fixed.csv
"""
import csv, glob, sys
from collections import defaultdict


def load(pattern):
    trades = []
    windows = {}
    for f in sorted(glob.glob(pattern)):
        tag = f.split("_")[3] if "imbab" in f else f.split("_")[3]
        prev = 0.0
        ds = []
        for r in csv.DictReader(open(f, newline="")):
            if r["Event"] != "EXIT":
                continue
            t = float(r["TotalPnL"])
            pnl = t - prev
            prev = t
            trades.append(dict(tag=tag, date=r["Date"], time=r.get("Time", ""),
                               pnl=pnl, mode=r["Mode"]))
            ds.append(r["Date"])
        if ds:
            windows[tag] = (min(ds), max(ds))
    return trades, windows


def main():
    pattern = sys.argv[1] if len(sys.argv) > 1 else "IOF_NQ_imbab_*_imb_fixed.csv"
    trades, windows = load(pattern)
    if not trades:
        print(f"no trades matched {pattern}")
        return

    print(f"=== {pattern} ===")
    print("contract windows:")
    iv = sorted((a, b, t) for t, (a, b) in windows.items())
    for a, b, t in iv:
        print(f"  {t:<6s} {a} -> {b}")
    bad = [(iv[i][2], iv[i][1], iv[i+1][2], iv[i+1][0])
           for i in range(len(iv) - 1) if iv[i][1] >= iv[i+1][0]]
    if bad:
        print("\n  !! OVERLAPPING windows -- chaining NOT valid, would double-count:")
        for a, ae, b, bs in bad:
            print(f"     {a} ends {ae} but {b} starts {bs}")
        return
    print("  (non-overlapping -> chaining valid: front-month roll)")

    trades.sort(key=lambda t: (t["date"], t["time"]))

    # chained account equity
    eq = pk = 0.0
    md = 0.0
    md_at = None
    pk_date = trades[0]["date"]
    dd_start = None
    worst_span = None
    for t in trades:
        eq += t["pnl"]
        if eq > pk:
            pk = eq
            pk_date = t["date"]
            dd_start = None
        else:
            if dd_start is None:
                dd_start = pk_date
            if eq - pk < md:
                md = eq - pk
                md_at = t["date"]
                worst_span = (dd_start, t["date"])

    # per-contract MaxDD, for comparison
    per = {}
    for tag in windows:
        e = p = m = 0.0
        for t in [x for x in trades if x["tag"] == tag]:
            e += t["pnl"]
            p = max(p, e)
            m = min(m, e - p)
        per[tag] = m

    # calendar months (merged across rolls)
    cm = defaultdict(float)
    for t in trades:
        cm[t["date"][:7]] += t["pnl"]
    months = sorted(cm)
    # fill calendar gaps with 0 so idle months count
    y0, m0 = int(months[0][:4]), int(months[0][5:7])
    y1, m1 = int(months[-1][:4]), int(months[-1][5:7])
    full = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        full.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    vals = [cm.get(k, 0.0) for k in full]

    tot = sum(t["pnl"] for t in trades)
    neg = sum(1 for v in vals if v < 0)
    print(f"\ntrades              : {len(trades)}")
    print(f"total net           : {tot:+,.0f}")
    print(f"calendar span       : {full[0]} .. {full[-1]}  ({len(full)} months)")
    print(f"contract-months     : {sum(1 for _ in glob.glob(pattern))} files -> "
          f"roll months double-counted in the per-contract tables")
    print(f"mean / CALENDAR mo  : {tot/len(full):+,.0f}")
    print(f"median calendar mo  : {sorted(vals)[len(vals)//2]:+,.0f}")
    print(f"neg calendar months : {neg}/{len(full)} ({100*neg/len(full):.0f}%)")
    print()
    print(f"CHAINED account MaxDD : {md:+,.0f}   (trough {md_at})")
    if worst_span:
        print(f"  drawdown ran        : {worst_span[0]} -> {worst_span[1]}")
    print(f"worst PER-CONTRACT DD : {min(per.values()):+,.0f}  "
          f"({min(per, key=per.get)})")
    print(f"  understatement      : {md - min(per.values()):+,.0f} "
          f"({abs(md/min(per.values())):.2f}x deeper when chained)")
    print("\nper-contract MaxDD:")
    for t in sorted(per, key=lambda k: iv and [x[2] for x in iv].index(k)):
        print(f"  {t:<6s} {per[t]:+,.0f}")
    print("\nworst 6 calendar months:")
    for k in sorted(full, key=lambda k: cm.get(k, 0.0))[:6]:
        print(f"  {k}  {cm.get(k,0.0):+,.0f}")


if __name__ == "__main__":
    main()
