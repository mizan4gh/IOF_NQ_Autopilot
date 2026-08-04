"""VWAP-touch + ABSORPTION/divergence gate — 6 frozen-contract validation.

New confirmation flavor for the 18:00-anchored VWAP-touch engine
([[project_vwap_touch_engine_falsified]]): instead of delta CONFIRMATION (which
failed — buy-delta at a pullback low is backwards), require ABSORPTION / delta-
divergence via divergence().strength. For a long: price dips to the VWAP but the
flow is FAILING to push it down (bullish divergence / down-bars carrying buy-
delta = sellers absorbed). Short = mirror.

Tested directly on the 6 FROZEN F.US.E* snapshots (canonical data — the earlier
sweep on -CME/SC_DATA files inflated NQH6, see the memory). NQU25/NQM6/NQU26 are
out-of-sample; NQU26 is LIVE.

Configs: absorb {>=1, >=2} at 0.50 SD (delta off, pullback off).
Ship gate: net>0 AND PF>1 on ALL 6.

Usage: python backtest_vt_absorb_validate6.py [tag]   (default: all 6)
"""
import csv
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent

CONTRACTS = {
    "NQU25": BASE / "F.US.ENQU25.scid",   # Sep-2025  (OOS)
    "NQZ25": BASE / "F.US.ENQZ25.scid",   # Dec-2025
    "NQM5":  BASE / "F.US.ENQM25.scid",   # Jun-2025
    "NQH6":  BASE / "F.US.ENQH26.scid",   # Mar-2026
    "NQM6":  BASE / "F.US.ENQM26.scid",   # Jun-2026  (OOS)
    "NQU26": BASE / "F.US.ENQU26.scid",   # Sep-2026  (OOS, LIVE)
}
OOS = {"NQU25", "NQM6", "NQU26"}

_BASE = dict(
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
    VWAP_TOUCH_DELTA_MIN=0,
    VWAP_TOUCH_PULLBACK=False,
    VWAP_TOUCH_SLOPE_LB=20,
    VWAP_TOUCH_SLOPE_TOL=0.02,
    VWAP_TOUCH_COOL=3,
)

SCENARIOS = {
    "abs1_sd50": {**_BASE, "VWAP_TOUCH_ABSORB": 1},
    "abs2_sd50": {**_BASE, "VWAP_TOUCH_ABSORB": 2},
}


def run(tag, name, ov, scid):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_vtabs_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {name} (absorb>={ov['VWAP_TOUCH_ABSORB']}) "
          f"[{'OOS' if tag in OOS else 'recheck'}] =====")
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
            trades.append(tot - prev)
            prev = tot
    n = len(trades)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0, pnls=[])
    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p < 0]
    total = sum(trades)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    peak = run_ = max_dd = 0.0
    for p in trades:
        run_ += p; peak = max(peak, run_); max_dd = min(max_dd, run_ - peak)
    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n, max_dd=max_dd, pnls=trades)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = defaultdict(dict)
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}")
            continue
        for name, ov in SCENARIOS.items():
            res[tag][name] = summarize(run(tag, name, ov, scid))

    print("\n" + "=" * 80)
    print(" VWAP-touch + ABSORPTION — 6 FROZEN contracts (OOS never tuned)")
    print("=" * 80)
    for name in SCENARIOS:
        print(f"\n  [{name}]  absorb>={SCENARIOS[name]['VWAP_TOUCH_ABSORB']}, 0.50 SD")
        print(f"  {'contract':8s} {'kind':8s} {'n':>4s} {'WR%':>6s} {'PF':>6s} {'Net':>10s} {'MaxDD':>9s}")
        pooled = []
        npos = 0
        for tag in res:
            r = res[tag][name]
            pooled += r["pnls"]
            ok = r["total"] > 0 and r["pf"] > 1.0
            npos += 1 if ok else 0
            flag = "" if ok else " <-"
            kind = "OOS" if tag in OOS else "recheck"
            print(f"  {tag:8s} {kind:8s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f}{flag}")
        w = [p for p in pooled if p > 0]; L = [p for p in pooled if p < 0]
        ppf = (sum(w) / abs(sum(L))) if L else float("inf")
        print(f"  POOLED {len(pooled):>10} trades  PF={ppf:5.2f}  Net={sum(pooled):>+10,.0f}"
              f"   -> {npos}/{len(res)} pass" + ("  ALL PASS" if npos == len(res) else "  FAIL"))


if __name__ == "__main__":
    main()
