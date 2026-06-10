"""A/B: M1 early-scratch exit vs production baseline — Sortino-focused.

Motivation (sortino_audit.py on 2026-06-09 prod baselines): 100% of downside
variance on all 3 contracts comes from pre-T1 STOP exits, all hold=4 (stop is
only eligible from entry+4). Losers show MFE <= 17 pt by death; winners reach
59-277 pt. Test: at entry+3, if MFE < 25% of stop distance and close is still
better than the stop level, scratch at close instead of riding to the stop.

Primary metric is daily Sortino (vs 0 target, zero-padded over weekdays in
span); PnL/MaxDD reported as guardrails.

Usage: python backtest_early_scratch_ab.py <path.scid>
"""
import csv
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
SCID = Path(sys.argv[1] if len(sys.argv) > 1 else BASE / "NQZ25-CME.scid")

_PROD = dict(
    NEWS_FILTER=1,
    C_OPEN_COOL=36,
    TARGET_VOL=3000,
    SCALE_OUT=False,
    MAX_TRADES=6,
    DAILY_LOSS=800.0,
    DAILY_PROF=0.0,
)

SCENARIOS = {
    "live_prod":     {**_PROD, "EARLY_SCRATCH": False},
    "early_scratch": {**_PROD, "EARLY_SCRATCH": True,
                      "ES_AT_BAR": 3, "ES_MFE_FRAC": 0.25},
}
BASELINE = "live_prod"


def run_scenario(name, overrides):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_scratch_{SCID.stem}_{name}.csv"
    sys.argv = ["backtest.py", str(SCID), str(out)]
    print(f"\n========== SCENARIO: {name} ==========")
    print(f"  overrides: {overrides}")
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
    prev_tot = 0.0
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            tot = float(row["TotalPnL"])
            trades.append(dict(pnl=tot - prev_tot, date=row["Date"],
                               mode=row["Mode"], reason=row["ExitReason"]))
            prev_tot = tot

    n = len(trades)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0,
                    s_day=float("nan"), s_tr=float("nan"),
                    mode_count={}, reason_count={})

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")

    peak = run = max_dd = 0.0
    for p in pnls:
        run += p
        peak = max(peak, run)
        max_dd = min(max_dd, run - peak)

    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["pnl"]
    days = sorted(daily)
    n_days = weekdays_between(date.fromisoformat(days[0]),
                              date.fromisoformat(days[-1]))

    def sortino(rets, periods):
        mu = sum(rets) / periods
        dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / periods)
        return mu / dd if dd > 0 else float("inf")

    s_day = sortino(list(daily.values()), n_days)
    s_tr = sortino(pnls, n)

    mode_count, reason_count = defaultdict(int), defaultdict(int)
    for t in trades:
        mode_count[t["mode"]] += 1
        reason_count[t["reason"]] += 1

    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n,
                max_dd=max_dd, s_day=s_day, s_tr=s_tr,
                mode_count=dict(mode_count), reason_count=dict(reason_count))


def main():
    print(f"\nA/B on {SCID.name}: live_prod vs early_scratch (Sortino target)")
    results = {name: summarize(run_scenario(name, ov))
               for name, ov in SCENARIOS.items()}

    print("\n" + "=" * 75)
    print(f"COMPARISON on {SCID.stem}")
    print("=" * 75)
    hdr = f"{'Metric':<18} " + " ".join(f"{n:>16s}" for n in results)
    print(hdr)
    print("-" * len(hdr))
    fields = [
        ("Trades (N)",      lambda r: f"{r['n']}"),
        ("Win rate %",      lambda r: f"{r['wr']:.1f}"),
        ("Total PnL $",     lambda r: f"{r['total']:.0f}"),
        ("Profit factor",   lambda r: f"{r['pf']:.2f}"),
        ("Max DD $",        lambda r: f"{r['max_dd']:.0f}"),
        ("Sortino/day",     lambda r: f"{r['s_day']:.3f}"),
        ("Sortino/trade",   lambda r: f"{r['s_tr']:.3f}"),
    ]
    for label, fn in fields:
        print(f"{label:<18} " + " ".join(f"{fn(r):>16s}" for r in results.values()))

    base = results[BASELINE]
    print(f"\nDelta vs {BASELINE}:")
    for name, r in results.items():
        if name == BASELINE:
            continue
        d_pnl = r["total"] - base["total"]
        d_sd = r["s_day"] - base["s_day"]
        d_dd = r["max_dd"] - base["max_dd"]
        if d_sd > 0 and d_pnl >= 0:
            verdict = "test_better"
        elif d_sd < 0 or d_pnl < 0:
            verdict = "test_worse"
        else:
            verdict = "tied"
        print(f"  {name:<14}  Sortino/day {d_sd:+.3f}  PnL ${d_pnl:+8,.0f}  "
              f"MaxDD ${d_dd:+8,.0f}  -> {verdict}")

    print("\nExit reasons:")
    all_r = sorted({k for r in results.values() for k in r["reason_count"]})
    print(f"{'Reason':<10} " + " ".join(f"{n:>16s}" for n in results))
    for k in all_r:
        print(f"{k:<10} " + " ".join(f"{r['reason_count'].get(k, 0):>16d}"
                                     for r in results.values()))


if __name__ == "__main__":
    main()
