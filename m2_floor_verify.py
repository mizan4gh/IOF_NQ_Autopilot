"""Independently rebuild the M2-only floor table from the CSVs.

The reported table came from backtest_m2_only.py's own printed summary. This
recomputes every cell straight off the IOF_NQ_m2only_*.csv trade records and
asserts the claims that were committed in 100a227, so the numbers in the commit
message and memory are verified rather than transcribed.

Usage: python m2_floor_verify.py
"""
import csv
import statistics as st
from pathlib import Path

BASE = Path(__file__).parent
TAGS = ["NQU25", "NQZ25", "NQM5", "NQH6", "NQM6", "NQU26"]
FLOORS = ["m2_f50", "m2_f46", "m2_f40", "m2_f33"]

# Claims committed in 100a227 / memory: floor -> (n, net, contracts_positive)
CLAIMED = {
    "m2_f50": (16, -4205, 2),
    "m2_f46": (67, -13920, 0),
    "m2_f40": (142, -7305, 1),
    "m2_f33": (163, -11705, 1),
}


def trades(p):
    out, prev = [], 0.0
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            if r["Event"] != "EXIT":
                continue
            t = float(r["TotalPnL"])
            out.append(t - prev)
            prev = t
    return out


def main():
    print("=" * 78)
    print(" M2-only floor table — REBUILT FROM CSVs (independent of the log)")
    print("=" * 78)
    ok = True
    means = {}
    for f in FLOORS:
        print(f"\n  [{f}]")
        print(f"  {'contract':8s} {'n':>4s} {'WR%':>6s} {'PF':>6s} {'net':>9s}")
        pooled, npos, missing = [], 0, []
        for t in TAGS:
            p = BASE / f"IOF_NQ_m2only_{t}_{f}.csv"
            if not p.exists():
                missing.append(t)
                continue
            tr = trades(p)
            pooled += tr
            if not tr:
                print(f"  {t:8s} {0:>4} {'-':>6} {'-':>6} {0:>+9,.0f}")
                continue
            w = [x for x in tr if x > 0]
            L = [x for x in tr if x < 0]
            net = sum(tr)
            if net > 0:
                npos += 1
            pf = (sum(w) / abs(sum(L))) if L else float("inf")
            print(f"  {t:8s} {len(tr):>4} {100*len(w)/len(tr):>6.1f} {pf:>6.2f} "
                  f"{net:>+9,.0f}")
        if missing:
            print(f"  MISSING: {missing}")
            ok = False
        n, net = len(pooled), sum(pooled)
        m = net / n if n else 0.0
        sd = st.pstdev(pooled) if n > 1 else 0.0
        se = sd / n ** 0.5 if sd else 0.0
        tt = m / se if se else 0.0
        means[f] = m
        cn, cnet, cpos = CLAIMED[f]
        agree = (n == cn and round(net) == cnet and npos == cpos)
        ok = ok and agree
        print(f"  POOLED n={n} net={net:>+9,.0f} mean={m:>+7.0f} t={tt:>+5.2f} "
              f"positive={npos}/6")
        print(f"  CLAIMED n={cn} net={cnet:>+9,.0f} positive={cpos}/6  "
              f"-> {'MATCH' if agree else 'MISMATCH'}")

    # The headline structural claim: the gradient is inverted.
    grad = [means[f] for f in FLOORS]           # f50, f46, f40, f33
    inverted = grad[0] < grad[1] < grad[2]      # tighter floor = worse per trade
    print(f"\n  mean/trade by floor 50/46/40/33: "
          f"{', '.join(f'{g:+.0f}' for g in grad)}")
    print(f"  INVERTED GRADIENT (f50 < f46 < f40): {inverted}")
    print(f"\n  RESULT: {'ALL CLAIMS VERIFIED' if ok and inverted else 'DISCREPANCY FOUND'}")


if __name__ == "__main__":
    main()
