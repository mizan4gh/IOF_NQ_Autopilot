"""VWAP-touch entry ENGINE — standalone backtest across 3 contracts (SD bands).

From the live-chart circles: anchor the VWAP at 18:00 (Globex) and place an order
at every fresh return to the VWAP — long when VWAP is rising, short when falling.
Proximity measured in VWAP STANDARD DEVIATIONS (the blue SD bands), per user.
This is a NEW entry engine (replaces the M1..M8 cascade), not a filter.

Knobs (backtest.py): VWAP_TOUCH_TRIGGER, VWAP_ANCHOR_HHMM=1800, VWAP_NEAR_SD,
VWAP_TOUCH_SLOPE_LB/TOL, VWAP_TOUCH_COOL. Standard ATR stop + T1/T2 + trail exit.

Sweeps VWAP_NEAR_SD in {0.25, 0.5, 1.0}. Ship gate: net>0 AND PF>1 on all three
[[feedback_cross_contract_ab]].

Usage: python backtest_vwap_touch_ab.py [NQZ25|NQM5|NQH6]   (default: all)
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
    "sd0p25": {**_BASE, "VWAP_NEAR_SD": 0.25},
    "sd0p50": {**_BASE, "VWAP_NEAR_SD": 0.50},
    "sd1p00": {**_BASE, "VWAP_NEAR_SD": 1.00},
}


def run_scenario(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_vtouch_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n========== {tag} :: {name} (NEAR_SD={overrides['VWAP_NEAR_SD']}) ==========")
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
    print(" VWAP-TOUCH ENGINE (18:00 anchor, SD bands) — STANDALONE A/B")
    print("=" * 88)
    print(f"  {'contract':8s} {'scenario':8s} {'n':>4s} {'L/S':>7s} {'WR%':>6s} {'PF':>6s} "
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
        best = "PASS" if ok else "FAIL"
        detail = "" if ok else "  (net<=0 or PF<=1 on >=1 contract)"
        print(f"  {name:8s} pooled {pooled:>+10,.0f}  -> {best}{detail}")


if __name__ == "__main__":
    main()
