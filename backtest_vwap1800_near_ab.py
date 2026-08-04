"""A/B: 18:00-anchored VWAP proximity filter — enter only when price is within
5-10 pts of the Globex-anchored VWAP. This is the ONLY filter (no directional
gate, no quality-floor change, exits unchanged).

User request: "anchor at 1800 and place order when price is very close (5 to 10
points) to VWAP. this only filter."

Levers (test arms):
  VWAP_ANCHOR_HHMM=1800  — anchor the session VWAP at the 18:00 ET Globex open;
                           it then spans the overnight session into RTH.
  VWAP_NEAR_PTS={5,10}   — only allow an entry when abs(close - vwap) <= N pts.

Baseline = current production config (v12.37, MT=6/DL=800), no anchor/near.
Note: M4 (needs >=0.35*ATR from VWAP) and M6 (breakout) structurally conflict
with a near-VWAP gate, so expect M1/M2 to survive and M4/M6 to be suppressed.

Cross-contract gate [[feedback_cross_contract_ab]]: AGREE test_better on all 3.

Usage: python backtest_vwap1800_near_ab.py [NQZ25|NQM5|NQH6]   (default: all)
"""
import csv
import math
import sys
from collections import defaultdict, Counter
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
    QUAL_FLOOR=50,          # unchanged — no q lever in this test
    VWAP_FILTER_ALL=False,  # no directional gate
    VWAP_ANCHOR_RTH=False,
    VWAP_ANCHOR_HHMM=None,
    VWAP_NEAR_PTS=0.0,
)

SCENARIOS = {
    "baseline_live": {**_PROD},
    "near5_1800":    {**_PROD, "VWAP_ANCHOR_HHMM": 1800, "VWAP_NEAR_PTS": 5.0},
    "near10_1800":   {**_PROD, "VWAP_ANCHOR_HHMM": 1800, "VWAP_NEAR_PTS": 10.0},
}
BASELINE = "baseline_live"
TESTS = ["near5_1800", "near10_1800"]


def run_scenario(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_vwap1800near_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  VWAP_ANCHOR_HHMM={overrides.get('VWAP_ANCHOR_HHMM')} "
          f"VWAP_NEAR_PTS={overrides.get('VWAP_NEAR_PTS')}")
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
                    s_day=float("nan"), mode_net={})

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

    mode_net = defaultdict(float)
    for t in trades:
        mode_net[t["mode"]] += t["pnl"]

    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n,
                max_dd=max_dd, s_day=s_day,
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
    print(" 18:00-VWAP PROXIMITY FILTER (5/10 pts)  A/B SUMMARY (prod config)")
    print("=" * 84)
    print(f"  {'contract':8s} {'scenario':14s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s}")
    for tag, scn in results.items():
        for name, r in scn.items():
            print(f"  {tag:8s} {name:14s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>10.3f}")
        print()

    print(" Per-mode net (baseline vs each near arm):")
    for tag, scn in results.items():
        b = scn[BASELINE]
        allmodes = sorted(set().union(*[set(scn[s]["mode_net"]) for s in scn]))
        for name in [BASELINE] + TESTS:
            cells = "  ".join(f"{m}:{scn[name]['mode_net'].get(m, 0):+d}" for m in allmodes)
            print(f"  {tag:8s} {name:14s} {cells}")
        print()

    print(" Verdict per contract (each near arm vs baseline):")
    for tag, scn in results.items():
        b = scn[BASELINE]
        for name in TESTS:
            t = scn[name]
            d_pnl = t["total"] - b["total"]
            d_sd = t["s_day"] - b["s_day"]
            d_dd = t["max_dd"] - b["max_dd"]
            if d_pnl > 0 and d_sd >= 0:
                v = "test_better"
            elif d_pnl < 0 or d_sd < 0:
                v = "test_worse"
            else:
                v = "tied"
            print(f"  {tag:8s} {name:14s} PnL {d_pnl:+9,.0f}  Sortino/d {d_sd:+.3f}  "
                  f"MaxDD {d_dd:+9,.0f}  -> {v}")
        print()


if __name__ == "__main__":
    main()
