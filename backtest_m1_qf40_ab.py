"""A/B: M1-only QUAL_FLOOR=40 vs live 50 — 3-contract gate.

Motivated by the 2026-06-12 funnel report (funnel_report.py): the qual floor
is the dominant post-arm killer, and its M1 slice (q40 band: 4 NQZ25 /
2 NQM5 / 5 NQH6 kills) is the only piece that is BOTH untested and
evaluable in Python (M4's floor lives in cpp finalScore math
[[project_m4_sc_bump_unreachable]]; M2's band is twice-falsified
[[project_m2_qf25_falsified]]). M1 is the strongest backtest mode
(positive net on all three contracts), so admitting its 40-49 band is the
one plausible frequency unlock left.

Floor lowered for M1 (sel==0) ONLY; all other modes stay at 50.

Cross-contract gate: must AGREE test_better on NQZ25 + NQM5 + NQH6
[[feedback_cross_contract_ab]] before any cpp change.

Usage: python backtest_m1_qf40_ab.py [NQZ25|NQM5|NQH6]   (default: all three)
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
    "qf50_live":   {**_PROD, "QUAL_FLOOR_M1": None},
    "m1qf40_test": {**_PROD, "QUAL_FLOOR_M1": 40},
}
BASELINE = "qf50_live"


def run_scenario(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_m1qf40_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  QUAL_FLOOR=50 QUAL_FLOOR_M1={overrides['QUAL_FLOOR_M1']}")
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
                    s_day=float("nan"), mode_count={}, reason_count={},
                    m1=dict(n=0, net=0.0, wr=0.0, pf=0.0), n_days=0)

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

    mode_count, reason_count = defaultdict(int), defaultdict(int)
    for t in trades:
        mode_count[t["mode"]] += 1
        reason_count[t["reason"]] += 1

    m1_pnls = [t["pnl"] for t in trades if t["mode"] == "M1"]
    m1_w = [p for p in m1_pnls if p > 0]
    m1_l = [p for p in m1_pnls if p < 0]
    m1 = dict(n=len(m1_pnls), net=sum(m1_pnls),
              wr=100 * len(m1_w) / len(m1_pnls) if m1_pnls else 0.0,
              pf=(sum(m1_w) / abs(sum(m1_l))) if m1_l else float("inf"))

    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n,
                max_dd=max_dd, s_day=s_day, n_days=len(daily),
                mode_count=dict(mode_count), reason_count=dict(reason_count),
                m1=m1)


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
    print(" M1-ONLY QUAL_FLOOR=40 A/B SUMMARY (prod config, early-scratch ON)")
    print("=" * 78)
    print(f"  {'contract':8s} {'scenario':12s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s} {'tradedays':>9s}  modes")
    for tag, scn in results.items():
        for name, r in scn.items():
            modes = " ".join(f"{m}:{c}" for m, c in sorted(r["mode_count"].items()))
            print(f"  {tag:8s} {name:12s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>10.3f} "
                  f"{r['n_days']:>9}  {modes}")
        print()

    print(" M1-only breakdown (what the lever unlocked):")
    print(f"  {'contract':8s} {'scenario':12s} {'M1_n':>5s} {'M1_WR%':>7s} {'M1_PF':>6s} {'M1_Net':>10s}")
    for tag, scn in results.items():
        for name, r in scn.items():
            m = r["m1"]
            print(f"  {tag:8s} {name:12s} {m['n']:>5} {m['wr']:>7.1f} {m['pf']:>6.2f} {m['net']:>+10,.0f}")
        print()

    print(" Verdict per contract (m1qf40 vs qf50):")
    verdicts = {}
    for tag, scn in results.items():
        b, t = scn[BASELINE], scn["m1qf40_test"]
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
        print("\n test not better anywhere -- keep floor 50")


if __name__ == "__main__":
    main()
