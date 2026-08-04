"""Out-of-sample validation of the ONLY gate-passing VWAP-touch config:
sd50_d4 = 18:00-anchored VWAP-touch engine, 0.50 SD proximity band, delta>=4
confirmation (4 of last 5 bars lean the trade side), slope-direction, pullback
OFF. On the first 3 contracts it passed the ship gate but on only 9-20 trades
each ([[project_vwap_touch_engine_falsified]] follow-up).

Runs the SAME config on all 6 FROZEN F.US.E* snapshots (the canonical, non-
rewritten data — [[project_nqh6_data_rewrite_incident]]). NQU25/NQM6/NQU26 are
genuinely out-of-sample (never touched in the sweep); NQZ25/NQM5/NQH6 re-checked
on frozen data for apples-to-apples. NQU26 is the LIVE contract.

Ship gate: net>0 AND PF>1 on ALL 6. Given the tiny per-contract samples, also
report pooled stats and total trade count.

Usage: python backtest_vt_sd50d4_validate6.py [tag]   (default: all 6)
"""
import csv
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent

CONTRACTS = {
    "NQU25": BASE / "F.US.ENQU25.scid",   # Sep-2025  (out-of-sample)
    "NQZ25": BASE / "F.US.ENQZ25.scid",   # Dec-2025  (recheck)
    "NQM5":  BASE / "F.US.ENQM25.scid",   # Jun-2025  (recheck)
    "NQH6":  BASE / "F.US.ENQH26.scid",   # Mar-2026  (recheck)
    "NQM6":  BASE / "F.US.ENQM26.scid",   # Jun-2026  (out-of-sample)
    "NQU26": BASE / "F.US.ENQU26.scid",   # Sep-2026  (out-of-sample, LIVE)
}
OOS = {"NQU25", "NQM6", "NQU26"}

CFG = dict(
    NEWS_FILTER=1,
    TARGET_VOL=3000,
    SCALE_OUT=False,
    MAX_TRADES=6,
    DAILY_LOSS=800.0,
    DAILY_PROF=0.0,
    VWAP_TOUCH_TRIGGER=True,
    VWAP_ANCHOR_HHMM=1800,
    VWAP_NEAR_PTS=0.0,
    VWAP_NEAR_SD=0.50,
    VWAP_TOUCH_DELTA_MIN=4,
    VWAP_TOUCH_PULLBACK=False,
    VWAP_TOUCH_SLOPE_LB=20,
    VWAP_TOUCH_SLOPE_TOL=0.02,
    VWAP_TOUCH_COOL=3,
)


def run(tag, scid):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in CFG.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_vt_sd50d4_{tag}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: sd50_d4 ({'OOS' if tag in OOS else 'recheck'}) =====")
    backtest.main()
    return out


def weekdays_between(d0, d1):
    n, d = 0, d0
    while d <= d1:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def summarize(csv_path):
    trades = []
    prev = 0.0
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            tot = float(row["TotalPnL"])
            trades.append(dict(pnl=tot - prev, date=row["Date"]))
            prev = tot
    n = len(trades)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0, pnls=[])
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    peak = run_ = max_dd = 0.0
    for p in pnls:
        run_ += p; peak = max(peak, run_); max_dd = min(max_dd, run_ - peak)
    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n, max_dd=max_dd, pnls=pnls)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}")
            continue
        res[tag] = summarize(run(tag, scid))

    print("\n" + "=" * 78)
    print(" VWAP-touch sd50_d4 — 6 FROZEN contracts (OOS = never in the sweep)")
    print("=" * 78)
    print(f"  {'contract':8s} {'kind':8s} {'n':>4s} {'WR%':>6s} {'PF':>6s} {'Net':>10s} {'MaxDD':>9s}")
    all_pnls = []
    npos = 0
    for tag, r in res.items():
        kind = "OOS" if tag in OOS else "recheck"
        all_pnls += r["pnls"]
        ok = r["total"] > 0 and r["pf"] > 1.0
        npos += 1 if ok else 0
        flag = " <-" if not ok else ""
        print(f"  {tag:8s} {kind:8s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
              f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f}{flag}")

    pooled = sum(all_pnls)
    w = [p for p in all_pnls if p > 0]
    L = [p for p in all_pnls if p < 0]
    ppf = (sum(w) / abs(sum(L))) if L else float("inf")
    print("-" * 78)
    print(f"  POOLED   {len(all_pnls):>17} trades  PF={ppf:5.2f}  Net={pooled:>+10,.0f}")
    print(f"\n  contracts net>0 & PF>1: {npos}/{len(res)}")
    if npos == len(res):
        print("  ALL PASS — sd50_d4 survives out-of-sample")
    else:
        print("  FAILS on >=1 contract — sd50_d4 does NOT generalize")


if __name__ == "__main__":
    main()
