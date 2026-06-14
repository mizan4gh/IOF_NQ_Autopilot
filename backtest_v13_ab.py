"""A/B: FULL v13 model vs v12.37 — 3-contract gate.

The complete v13 representation backtest.py can express:
  - mode set M1/M4/M6/M8 (drop M2/M3 via DISABLE_MODES={1,2})
  - V13_MODEL=True → M8 = balance-edge fade w/o trap term + trend-exhaustion
    (type 3) fade that the v12.37 proxy never modeled.

NOT modeled (so this is still a floor, not a ceiling, on v13 fidelity):
  - v13's reduced confirmation score. backtest.py's score is a 5-bar delta
    count, never the cpp's 12-signal S1-S12 sum, so the dropped S7/S8/S10/S11
    sub-signals were never present to remove. A faithful cpp-score port is
    partly impossible (S8 iceberg / S7 single-print need DOM/tick data).
True final validation still requires Sierra replay of the v13 DLL.

Cross-contract gate: [[feedback_cross_contract_ab]].
Related: [[project_v13_rewrite]], backtest_v13_modes_ab.py (mode-set only).

Usage: python backtest_v13_ab.py [NQZ25|NQM5|NQH6]   (default: all three)
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
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50,
)

SCENARIOS = {
    "v1237":  {**_PROD, "DISABLE_MODES": set(),    "V13_MODEL": False},
    "v13":    {**_PROD, "DISABLE_MODES": {1, 2},   "V13_MODEL": True},
}
BASELINE = "v1237"


def run_scenario(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_v13_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  DISABLE_MODES={overrides['DISABLE_MODES'] or '{}'}  V13_MODEL={overrides['V13_MODEL']}")
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
                    s_day=float("nan"), mode_count={}, mode_net={}, n_days=0)

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
                max_dd=max_dd, s_day=s_day, n_days=len(daily),
                mode_count=dict(mode_count), mode_net=dict(mode_net))


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

    print("\n" + "=" * 80)
    print(" FULL v13 MODEL (modes M1/M4/M6/M8 + v13 M8) vs v12.37 — prod config")
    print("=" * 80)
    print(f"  {'contract':8s} {'scenario':8s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s}  modes")
    for tag, scn in results.items():
        for name, r in scn.items():
            modes = " ".join(f"{m}:{c}({r['mode_net'][m]:+,.0f})"
                             for m, c in sorted(r["mode_count"].items()))
            print(f"  {tag:8s} {name:8s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>10.3f}  {modes}")
        print()

    print(" Verdict per contract (v13 vs v1237):")
    verdicts = {}
    for tag, scn in results.items():
        b, t = scn[BASELINE], scn["v13"]
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
        print("\n ALL AGREE test_better -- v13 (as modeled) beats v12.37 cross-contract")
    elif vs == {"tied"}:
        print("\n TIED -- v13 (as modeled) matches v12.37")
    elif "test_better" in vs:
        print("\n DISAGREE -- v13 not a clear win; do not ship over v12.37")
    else:
        print("\n test_worse somewhere -- v13 (as modeled) costs on >=1 contract")


if __name__ == "__main__":
    main()
