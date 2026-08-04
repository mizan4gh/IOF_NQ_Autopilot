"""VWAP-touch engine + DELTA confirmation — 3-contract sweep.

Adds order-flow delta confirmation to the 18:00-anchored VWAP-touch engine
([[project_vwap_touch_engine_falsified]] — bare engine had no portable edge).
Direction still from the VWAP slope; now at least VWAP_TOUCH_DELTA_MIN of the
last 5 bars must lean the trade's way before entering. Question: does requiring
flow behind the touch turn the ~50% WR / negative-expectancy engine positive?

Sweeps SD width {0.25, 0.50} x delta {3, 4}. Baseline (delta 0) numbers are in
[[project_vwap_touch_engine_falsified]] (NQZ25 +230 / NQM5 +365 best cells).
Ship gate: net>0 AND PF>1 on all three [[feedback_cross_contract_ab]].

Usage: python backtest_vwap_touch_delta_ab.py [NQZ25|NQM5|NQH6]   (default: all)
"""
import csv
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "NQM5.CME.scid",
    "NQH6":  SC_DATA / "NQH6.CME.scid",
}

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
    VWAP_TOUCH_SLOPE_LB=20,
    VWAP_TOUCH_SLOPE_TOL=0.02,
    VWAP_TOUCH_COOL=3,
)

SCENARIOS = {
    "sd25_d3": {**_BASE, "VWAP_NEAR_SD": 0.25, "VWAP_TOUCH_DELTA_MIN": 3},
    "sd25_d4": {**_BASE, "VWAP_NEAR_SD": 0.25, "VWAP_TOUCH_DELTA_MIN": 4},
    "sd50_d3": {**_BASE, "VWAP_NEAR_SD": 0.50, "VWAP_TOUCH_DELTA_MIN": 3},
    "sd50_d4": {**_BASE, "VWAP_NEAR_SD": 0.50, "VWAP_TOUCH_DELTA_MIN": 4},
}


def run_scenario(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_vtd_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {name} (SD={overrides['VWAP_NEAR_SD']} "
          f"delta>={overrides['VWAP_TOUCH_DELTA_MIN']}) =====")
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
            trades.append(dict(pnl=tot - prev, date=row["Date"], side=row.get("Side", "")))
            prev = tot
    n = len(trades)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0, s_day=float("nan"),
                    n_long=0, n_short=0)
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    peak = run = max_dd = 0.0
    for p in pnls:
        run += p; peak = max(peak, run); max_dd = min(max_dd, run - peak)
    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["pnl"]
    days = sorted(daily)
    n_days = weekdays_between(date.fromisoformat(days[0]), date.fromisoformat(days[-1]))
    mu = sum(daily.values()) / n_days
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in daily.values()) / n_days)
    s_day = mu / dd if dd > 0 else float("inf")
    n_long = sum(1 for t in trades if t["side"] == "LONG")
    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n, max_dd=max_dd,
                s_day=s_day, n_long=n_long, n_short=n - n_long)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}")
            continue
        results[tag] = {name: summarize(run_scenario(name, ov, scid, tag))
                        for name, ov in SCENARIOS.items()}

    print("\n" + "=" * 88)
    print(" VWAP-TOUCH + DELTA CONFIRMATION (18:00 anchor) — 3-contract sweep")
    print("=" * 88)
    print(f"  {'contract':8s} {'scen':8s} {'n':>4s} {'L/S':>7s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s}")
    for tag, scn in results.items():
        for name, r in scn.items():
            ls = f"{r['n_long']}/{r['n_short']}"
            print(f"  {tag:8s} {name:8s} {r['n']:>4} {ls:>7s} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>10.3f}")
        print()

    print(" Ship gate (net>0 AND PF>1 on ALL 3 contracts):")
    for name in SCENARIOS:
        rows = [results[t][name] for t in results]
        ok = all(r["total"] > 0 and r["pf"] > 1.0 for r in rows)
        pooled = sum(r["total"] for r in rows)
        print(f"  {name:8s} pooled {pooled:>+10,.0f}  -> {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
