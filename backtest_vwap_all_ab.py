"""A/B: daily-VWAP directional filter on ALL modes, in isolation.

Isolates the VWAP_FILTER_ALL lever from the M2-floor change tested in
backtest_vwap_all_m2q40_ab.py ([[project_vwap_all_m2q40_falsified]]). Only knob
that flips: VWAP_FILTER_ALL. Every mode may trade only aligned with session
VWAP (long > vwap, short < vwap); all quality floors stay at production 50.

Question: does the directional overlay help or hurt the existing (unmodified)
mode mix? M1 already carries its own VWAP-slope filter (v12.22); several modes
(M4 sweep-fade, M8 fade) are edge/mean-reversion by thesis, so a trend-align
overlay may cut their winners.

Baseline = current production config (v12.37, MT=6/DL=800). Cross-contract gate
[[feedback_cross_contract_ab]]: AGREE test_better on all 3 before any ship.

Usage: python backtest_vwap_all_ab.py [NQZ25|NQM5|NQH6]   (default: all)
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
    QUAL_FLOOR_M2=None,
    VWAP_FILTER_ALL=False,
    VWAP_ANCHOR_RTH=False,
)

SCENARIOS = {
    "baseline_live":  {**_PROD},
    "vwapAll_mdnt":   {**_PROD, "VWAP_FILTER_ALL": True},                          # midnight-anchored (legacy VWAP)
    "vwapAll_rth":    {**_PROD, "VWAP_FILTER_ALL": True, "VWAP_ANCHOR_RTH": True}, # RTH-anchored daily VWAP
}
BASELINE = "baseline_live"
TEST = "vwapAll_rth"   # verdict headline compares the true daily-VWAP arm


def run_scenario(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_vwapAll_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  VWAP_FILTER_ALL={overrides.get('VWAP_FILTER_ALL')} QUAL_FLOOR=50 (all modes)")
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
            trades.append(dict(pnl=tot - prev_tot, date=row["Date"], mode=row["Mode"]))
            prev_tot = tot

    n = len(trades)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0,
                    s_day=float("nan"), mode_count={}, mode_net={})

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
    for t in trades:
        mode_count[t["mode"]] += 1
        mode_net[t["mode"]] += t["pnl"]

    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n,
                max_dd=max_dd, s_day=s_day,
                mode_count=dict(mode_count),
                mode_net={k: round(v) for k, v in mode_net.items()})


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

    print("\n" + "=" * 84)
    print(" VWAP-FILTER-ALL (isolated)  A/B SUMMARY (prod config)")
    print("=" * 84)
    print(f"  {'contract':8s} {'scenario':16s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s}")
    for tag, scn in results.items():
        for name, r in scn.items():
            print(f"  {tag:8s} {name:16s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>10.3f}")
        print()

    print(" Per-mode net (baseline -> test):")
    for tag, scn in results.items():
        b, t = scn[BASELINE], scn[TEST]
        modes = sorted(set(b["mode_net"]) | set(t["mode_net"]))
        cells = "  ".join(
            f"{m} {b['mode_net'].get(m, 0):+d}->{t['mode_net'].get(m, 0):+d}"
            for m in modes)
        print(f"  {tag:8s} {cells}")
    print()

    print(" Verdict per contract (test vs baseline):")
    verdicts = {}
    for tag, scn in results.items():
        b, t = scn[BASELINE], scn[TEST]
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
    elif vs == {"tied"}:
        print("\n TIED all 3 -- VWAP filter is a no-op (never binds)")
    else:
        print("\n test not better anywhere -- keep baseline (no VWAP filter)")


if __name__ == "__main__":
    main()
