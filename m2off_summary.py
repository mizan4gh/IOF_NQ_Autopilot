"""Summarize the disable-M2 in-strategy A/B from whatever CSVs exist on disk.

The full run was killed mid-flight; this rebuilds the table from completed
contracts so a partial read is possible without re-running. Reports t per arm
([[reference_ab_noise_floor]]) and flags contracts still pending.

Usage: python m2off_summary.py
"""
import csv
import statistics as st
from pathlib import Path

BASE = Path(__file__).parent
TAGS = ["NQU25", "NQZ25", "NQM5", "NQH6", "NQM6", "NQU26"]


def summ(p):
    tr, prev = [], 0.0
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            if r["Event"] != "EXIT":
                continue
            t = float(r["TotalPnL"])
            tr.append(t - prev)
            prev = t
    peak = run = dd = 0.0
    for x in tr:
        run += x
        peak = max(peak, run)
        dd = min(dd, run - peak)
    return tr, dd


def main():
    print("=" * 86)
    print(" DISABLE M2 vs LIVE — in-strategy, live config (MT=1/DL=800, M8 f60)")
    print("=" * 86)
    print(f"  {'contract':8s} {'liveN':>5s} {'live net':>9s} {'offN':>5s} "
          f"{'off net':>9s} {'delta':>9s} {'ddLive':>8s} {'ddOff':>8s}")
    pl, po = [], []
    better = worse = same = have = 0
    for t in TAGS:
        a = BASE / f"IOF_NQ_m2off_{t}_live.csv"
        b = BASE / f"IOF_NQ_m2off_{t}_m2_off.csv"
        if not (a.exists() and b.exists()):
            print(f"  {t:8s}   -- pending --")
            continue
        have += 1
        ta, da = summ(a)
        tb, db = summ(b)
        d = sum(tb) - sum(ta)
        pl += ta
        po += tb
        if d > 0:
            better += 1
        elif d < 0:
            worse += 1
        else:
            same += 1
        flag = "" if d >= 0 else "  <- m2_off worse"
        print(f"  {t:8s} {len(ta):>5} {sum(ta):>+9,.0f} {len(tb):>5} "
              f"{sum(tb):>+9,.0f} {d:>+9,.0f} {da:>8,.0f} {db:>8,.0f}{flag}")

    if not have:
        print("\n  no completed contracts yet")
        return
    print(f"\n  {have}/6 contracts complete")
    print(f"  pooled live : n={len(pl):>4} net={sum(pl):>+9,.0f}")
    print(f"  pooled m2off: n={len(po):>4} net={sum(po):>+9,.0f}")
    print(f"  pooled delta: {sum(po) - sum(pl):>+9,.0f}   "
          f"better {better}/{have}, worse {worse}/{have}, tied {same}/{have}")
    for nm, arr in (("live", pl), ("m2_off", po)):
        if len(arr) > 1:
            m = sum(arr) / len(arr)
            sd = st.pstdev(arr)
            se = sd / len(arr) ** 0.5
            tt = m / se if se else 0.0
            print(f"  {nm:7s} mean={m:>+7.0f}/trade  t={tt:>+5.2f}"
                  + ("  SIGNIFICANT" if abs(tt) >= 2 else "  (ns)"))
    print("\n  SHIP only if no contract worse (incl. LIVE NQU26) AND MaxDD not worse.")


if __name__ == "__main__":
    main()
