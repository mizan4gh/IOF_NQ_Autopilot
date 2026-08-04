"""Is the VWAP-touch+ABSORPTION loss concentrated or broad-based?

Concentrated (a few outlier trades carry it) => something specific, maybe fixable.
Broad-based (the median trade loses, trimming outliers doesn't flip the sign)
=> no edge, close the thread.

Reports per contract: trade distribution, what trimming the worst-K does, and
splits by side / exit reason / hour that could localize a defect.

Usage: python vtabs_diag_concentration.py [scenario]   (default: abs1_sd50)
"""
import csv
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
SCEN = sys.argv[1] if len(sys.argv) > 1 else "abs1_sd50"
TAGS = ["NQU25", "NQZ25", "NQM5", "NQH6", "NQM6", "NQU26"]


def load(tag):
    """Return EXIT rows with per-trade P&L differenced off the TotalPnL curve."""
    p = BASE / f"IOF_NQ_vtabs_{tag}_{SCEN}.csv"
    if not p.exists():
        return None
    rows, prev = [], 0.0
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            if r["Event"] != "EXIT":
                continue
            tot = float(r["TotalPnL"])
            r["_pnl"] = tot - prev
            prev = tot
            rows.append(r)
    return rows


def grp(rows, key):
    d = defaultdict(list)
    for r in rows:
        d[key(r)].append(r["_pnl"])
    return d


def show_groups(title, d):
    print(f"    {title}")
    for k in sorted(d, key=lambda k: -len(d[k])):
        v = d[k]
        w = [x for x in v if x > 0]
        L = [x for x in v if x < 0]
        pf = (sum(w) / abs(sum(L))) if L else float("inf")
        print(f"      {str(k):10s} n={len(v):>3}  WR={100*len(w)/len(v):>5.1f}%  "
              f"PF={pf:>5.2f}  net={sum(v):>+9,.0f}")


def main():
    print(f"\n{'='*78}\n CONCENTRATION DIAGNOSTIC  [{SCEN}]\n{'='*78}")
    for tag in TAGS:
        rows = load(tag)
        if not rows:
            continue
        pnls = [r["_pnl"] for r in rows]
        n, tot = len(pnls), sum(pnls)
        srt = sorted(pnls)
        print(f"\n  {tag}  n={n}  net={tot:>+9,.0f}  mean={tot/n:>+7.0f}  "
              f"median={st.median(pnls):>+7.0f}")
        sd = st.pstdev(pnls)
        se = sd / (n ** 0.5) if n else 0.0
        print(f"    per-trade sd={sd:,.0f}  se={se:,.0f}  "
              f"t={(tot/n)/se if se else 0:>+5.2f}")

        # Concentration: does trimming the worst-K losers flip the sign?
        print(f"    worst 5: {', '.join(f'{x:+,.0f}' for x in srt[:5])}")
        print(f"    best  5: {', '.join(f'{x:+,.0f}' for x in srt[-5:])}")
        for k in (1, 3, 5, 10):
            if k >= n:
                break
            trimmed = sum(srt[k:])
            share = (sum(srt[:k]) / tot * 100) if tot else 0.0
            print(f"    drop worst {k:>2}: net={trimmed:>+9,.0f}   "
                  f"(worst {k} = {share:>5.1f}% of net)")

        show_groups("by side:", grp(rows, lambda r: r["Side"]))
        show_groups("by exit:", grp(rows, lambda r: r["ExitReason"]))
        show_groups("by hour:", grp(rows, lambda r: r["Time"][:2] + "h"))


if __name__ == "__main__":
    main()
