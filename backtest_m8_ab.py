"""A/B: M8 disable AND M8-only QUAL_FLOOR=60 vs live baseline — 3-contract gate.

Motivated by the 2026-06-26 live NQU26 session (SCLog.txt): the day's only
loss came entirely from Mode M8 — two minimum-quality fade shorts
(Q=50, Sc=2) that got run over fighting an uptrend:
  Trade#2 SHORT M8 @29556.50 -> STOP -$920 (Hold 1, MAE 44.5, MFE 16.5)
  Trade#3 SHORT M8 @29547.50 -> STOP -$625 (Hold 2, MAE 31.5, MFE 11.0)
while the M4 workhorse (Q=70 Sc=7) WON +$395. Net day -$1,150 = pure M8.

M8 is the flagged / least-validated mode ([[project_v13_rewrite]]: "M8
redefined + score reduced, needs own 3-contract A/B"). This harness tests two
independent levers, each vs the same live baseline:

  m8_off       DISABLE_MODES={7}     — suppress M8 entirely
  m8_floor60   QUAL_FLOOR_M8=60      — block the low-conviction band.
               For M8, qual100 = edge_sc*10 (backtest.py:832-833), so quality
               is bucketed in 10s; floor 60 cleanly sheds the Q<=50 fades
               (today's losers were Q=50) while keeping edge>=6 setups.

Baseline = current production config (v12.37 early-scratch ON), identical to
backtest_m6_qual40_ab.py's _PROD.

Cross-contract gate [[feedback_cross_contract_ab]]: a lever ships ONLY if it
AGREES test_better on NQZ25 + NQM5 + NQH6. Prior single-mode floor sweeps
(M2 qf25/30/40, M6 qf40, M1 qf40, qf33, qf_profile) all FALSIFIED or no-op'd,
so the prior is strongly against a single-mode lever surviving — disagreement
or no-op is the expected outcome, not a surprise.

Usage: python backtest_m8_ab.py [NQZ25|NQM5|NQH6]   (default: all three)
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
    M8_FADE_FULL=True,  # [v18 M8 port] model the LIVE cpp fade engine (types
                        # 1-4, backtest.py:1540-1723) so M8 actually fires —
                        # otherwise backtest M8 is near-dead and this A/B is
                        # vacuous (the 2026-06-26 no-op run). ON in ALL arms;
                        # the test arms then disable / floor that live M8.
)

# Each test arm resets BOTH knobs so scenarios don't leak across module reloads.
SCENARIOS = {
    "baseline":    {**_PROD, "DISABLE_MODES": set(),  "QUAL_FLOOR_M8": None},
    "m8_off":      {**_PROD, "DISABLE_MODES": {7},    "QUAL_FLOOR_M8": None},
    "m8_floor60":  {**_PROD, "DISABLE_MODES": set(),  "QUAL_FLOOR_M8": 60},
}
BASELINE = "baseline"
TEST_ARMS = ["m8_off", "m8_floor60"]


def run_scenario(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_m8ab_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  DISABLE_MODES={overrides['DISABLE_MODES']} "
          f"QUAL_FLOOR_M8={overrides['QUAL_FLOOR_M8']}")
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
                    s_day=float("nan"), mode_count={},
                    m8=dict(n=0, net=0.0, wr=0.0, pf=0.0))

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

    mode_count = defaultdict(int)
    for t in trades:
        mode_count[t["mode"]] += 1

    m8_pnls = [t["pnl"] for t in trades if t["mode"] == "M8"]
    m8_w = [p for p in m8_pnls if p > 0]
    m8_l = [p for p in m8_pnls if p < 0]
    m8 = dict(n=len(m8_pnls), net=sum(m8_pnls),
              wr=100 * len(m8_w) / len(m8_pnls) if m8_pnls else 0.0,
              pf=(sum(m8_w) / abs(sum(m8_l))) if m8_l else float("inf"))

    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n,
                max_dd=max_dd, s_day=s_day,
                mode_count=dict(mode_count), m8=m8)


def verdict(b, t):
    d_pnl = t["total"] - b["total"]
    d_sd = t["s_day"] - b["s_day"]
    if abs(d_pnl) < 1.0 and abs(d_sd) < 1e-9:
        return "no-op", d_pnl, d_sd
    if d_pnl > 0 and d_sd >= 0:
        return "test_better", d_pnl, d_sd
    if d_pnl < 0 or d_sd < 0:
        return "test_worse", d_pnl, d_sd
    return "tied", d_pnl, d_sd


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

    print("\n" + "=" * 82)
    print(" M8 DISABLE / M8-FLOOR-60 A/B SUMMARY (prod config, early-scratch ON)")
    print("=" * 82)
    print(f"  {'contract':8s} {'scenario':12s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s}  modes")
    for tag, scn in results.items():
        for name, r in scn.items():
            modes = " ".join(f"{m}:{c}" for m, c in sorted(r["mode_count"].items()))
            print(f"  {tag:8s} {name:12s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>10.3f}  {modes}")
        print()

    print(" M8-only breakdown (what M8 contributes in the baseline):")
    print(f"  {'contract':8s} {'M8_n':>5s} {'M8_WR%':>7s} {'M8_PF':>6s} {'M8_Net':>10s}")
    for tag, scn in results.items():
        m = scn[BASELINE]["m8"]
        print(f"  {tag:8s} {m['n']:>5} {m['wr']:>7.1f} {m['pf']:>6.2f} {m['net']:>+10,.0f}")
    print()

    for arm in TEST_ARMS:
        print(f" Verdict per contract ({arm} vs baseline):")
        verds = {}
        for tag, scn in results.items():
            v, d_pnl, d_sd = verdict(scn[BASELINE], scn[arm])
            d_dd = scn[arm]["max_dd"] - scn[BASELINE]["max_dd"]
            verds[tag] = v
            print(f"  {tag:8s} PnL {d_pnl:+9,.0f}  Sortino/d {d_sd:+.3f}  "
                  f"MaxDD {d_dd:+9,.0f}  -> {v}")
        vs = set(verds.values())
        if vs == {"test_better"}:
            print(f"  ==> {arm}: ALL AGREE test_better -- candidate to ship\n")
        elif vs <= {"no-op", "tied"}:
            print(f"  ==> {arm}: NO-OP (M8 quality bucket unreachable / no effect)\n")
        elif "test_better" in vs:
            print(f"  ==> {arm}: DISAGREE -- do not ship\n")
        else:
            print(f"  ==> {arm}: not better anywhere -- keep M8 as-is\n")


if __name__ == "__main__":
    main()
