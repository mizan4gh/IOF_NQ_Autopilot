"""A/B: T1/T2 at volume-profile levels (POC/VAH/VAL) vs ATR-multiple targets.

User-proposed (2026-06-12, v13 sketch): "Target at POC, VAH, VAL". The one
exit-side idea not yet tested ([[project_trade_freq_funnel]] context; prior
exit A/Bs: hard-TP1 falsified, half-MFE falsified). VP_TARGETS in backtest.py:
T1 = nearest VP level >= 0.5*ATR beyond entry, T2 = next level out; each
falls back to the ATR/mode target when no level qualifies. Stops, BE and
trail mechanics identical in both arms.

Cross-contract gate: must AGREE test_better on NQZ25 + NQM5 + NQH6
[[feedback_cross_contract_ab]] before any cpp change.

Usage: python backtest_vp_targets_ab.py [NQZ25|NQM5|NQH6]   (default: all three)
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

_PROD = dict(
    NEWS_FILTER=1,
    C_OPEN_COOL=36,
    TARGET_VOL=3000,
    SCALE_OUT=False,
    MAX_TRADES=6,
    DAILY_LOSS=800.0,
    DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,  # v12.37 shipped
    QUAL_FLOOR=50,
)

SCENARIOS = {
    "atr_live":  {**_PROD, "VP_TARGETS": False},
    "vp_test":   {**_PROD, "VP_TARGETS": True},
}
BASELINE = "atr_live"


def run_scenario(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_vptgt_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  VP_TARGETS={overrides['VP_TARGETS']}")
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
                    s_day=float("nan"), mode_count={}, mode_net={},
                    reason_count={}, n_days=0)

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
    mu = sum(daily.values()) / n_days
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in daily.values()) / n_days)
    s_day = mu / dd if dd > 0 else float("inf")

    mode_count, mode_net = defaultdict(int), defaultdict(float)
    reason_count = defaultdict(int)
    for t in trades:
        mode_count[t["mode"]] += 1
        mode_net[t["mode"]] += t["pnl"]
        reason_count[t["reason"]] += 1

    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n,
                max_dd=max_dd, s_day=s_day, n_days=len(daily),
                mode_count=dict(mode_count), mode_net=dict(mode_net),
                reason_count=dict(reason_count))


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

    print("\n" + "=" * 78)
    print(" VP-LEVEL TARGETS (POC/VAH/VAL) vs ATR TARGETS — prod config, v12.37")
    print("=" * 78)
    print(f"  {'contract':8s} {'scenario':10s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s}  exits")
    for tag, scn in results.items():
        for name, r in scn.items():
            exits = " ".join(f"{k}:{v}" for k, v in sorted(r["reason_count"].items()))
            print(f"  {tag:8s} {name:10s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>10.3f}  {exits}")
        print()

    print(" Per-mode net:")
    for tag, scn in results.items():
        for name, r in scn.items():
            modes = " ".join(f"{m}:{c}({r['mode_net'][m]:+,.0f})"
                             for m, c in sorted(r["mode_count"].items()))
            print(f"  {tag:8s} {name:10s} {modes}")
        print()

    print(" Verdict per contract (vp_test vs atr_live):")
    verdicts = {}
    for tag, scn in results.items():
        b, t = scn[BASELINE], scn["vp_test"]
        d_pnl = t["total"] - b["total"]
        d_sd = t["s_day"] - b["s_day"]
        d_dd = t["max_dd"] - b["max_dd"]
        if d_pnl > 0 and d_sd >= 0:
            v = "test_better"
        elif d_pnl < 0 or d_sd < 0:
            v = "test_worse"
        else:
            v = "tied"
        verdicts[tag] = v
        print(f"  {tag:8s} PnL {d_pnl:+9,.0f}  Sortino/d {d_sd:+.3f}  "
              f"MaxDD {d_dd:+9,.0f}  -> {v}")

    vs = set(verdicts.values())
    if vs == {"test_better"}:
        print("\n ALL AGREE test_better -- candidate to ship")
    elif "test_better" in vs:
        print("\n DISAGREE -- do not ship")
    else:
        print("\n test not better anywhere -- keep ATR targets")


if __name__ == "__main__":
    main()
