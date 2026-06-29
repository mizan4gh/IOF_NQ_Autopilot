"""A/B: M8 fade-type filter — `no_type2` (drop Imb-corrupted type-2) and
`type4_only` — vs the all-types live baseline. 3-contract gate.

Motivated by 2026-06-29 segmentation ([[project_m8_chopreg_unmeasurable]]):
across NQZ25/NQM5/NQH6 the M8 fade book disagrees cross-contract, and the
disagreement + the −$16.7k NQH6 hole are concentrated in **type-2**, whose
magnitude is corrupted by the Imb-extreme harness bug
([[reference_backtest_imb_extreme_bug]]). **type-4** (the type that fired LIVE
2026-06-29, −$1,015) was the benign one: 14/21 = 67% WR pooled, mildly +.

Two arms vs the same baseline (prod config, M8_FADE_FULL ON, early-scratch ON):
  no_type2     M8_FADE_TYPES={1,3,4}   — suppress type-2 only
  type4_only   M8_FADE_TYPES={4}       — fade absorption setups only

CAVEAT: removing type-2 also removes Imb-bug-inflated *magnitude*, so the
no_type2 dollar delta is an UPPER bound on the real gain (sign is meaningful,
size is not). type4_only is the cleaner signal — it stands on type-4 alone.

Cross-contract gate [[feedback_cross_contract_ab]]: ship ONLY if an arm is
test_better on all three. Single-mode levers here have a strong falsification
prior.

Usage: python backtest_m8_fadetype_ab.py [NQZ25|NQM5|NQH6]   (default: all)
"""
import csv, math, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "F.US.ENQM25.scid",   # Jun-2025
    "NQH6":  SC_DATA / "F.US.ENQH26.scid",   # Mar-2026 (1.23GB genuine NQH6)
}

_PROD = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(), QUAL_FLOOR_M8=None,
)

SCENARIOS = {
    "baseline":   {**_PROD, "M8_FADE_TYPES": {1, 2, 3, 4}},
    "no_type2":   {**_PROD, "M8_FADE_TYPES": {1, 3, 4}},
    "type4_only": {**_PROD, "M8_FADE_TYPES": {4}},
}
BASELINE = "baseline"
TEST_ARMS = ["no_type2", "type4_only"]


def run_scenario(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_m8ft_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n========== {tag} :: {name}  (M8_FADE_TYPES={sorted(overrides['M8_FADE_TYPES'])}) ==========")
    backtest.main()
    return out


def weekdays_between(d0, d1):
    n, d = 0, d0
    while d <= d1:
        if d.weekday() < 5: n += 1
        d += timedelta(days=1)
    return n


def summarize(csv_path):
    trades, prev = [], 0.0
    ftype = {}  # pair SETUP fade_type with the trade
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] == "SETUP":
                ftype["pending"] = int(float(row["FadeType"]))
            elif row["Event"] == "EXIT":
                tot = float(row["TotalPnL"])
                trades.append(dict(pnl=tot - prev, date=row["Date"],
                                   mode=row["Mode"], ft=ftype.pop("pending", 0)))
                prev = tot
    n = len(trades)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0,
                    s_day=float("nan"), m8=dict(n=0, net=0.0, wr=0.0))
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    peak = run = max_dd = 0.0
    for p in pnls:
        run += p; peak = max(peak, run); max_dd = min(max_dd, run - peak)
    daily = defaultdict(float)
    for t in trades: daily[t["date"]] += t["pnl"]
    days = sorted(daily)
    nd = weekdays_between(date.fromisoformat(days[0]), date.fromisoformat(days[-1]))
    mu = sum(daily.values()) / nd
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in daily.values()) / nd)
    s_day = mu / dd if dd > 0 else float("inf")
    m8 = [t["pnl"] for t in trades if t["mode"] == "M8"]
    m8w = [p for p in m8 if p > 0]
    m8d = dict(n=len(m8), net=sum(m8), wr=100*len(m8w)/len(m8) if m8 else 0.0)
    return dict(n=n, total=total, pf=pf, wr=100*len(wins)/n,
                max_dd=max_dd, s_day=s_day, m8=m8d)


def verdict(b, t):
    d_pnl = t["total"] - b["total"]; d_sd = t["s_day"] - b["s_day"]
    if abs(d_pnl) < 1.0 and abs(d_sd) < 1e-9: return "no-op", d_pnl, d_sd
    if d_pnl > 0 and d_sd >= 0: return "test_better", d_pnl, d_sd
    if d_pnl < 0 or d_sd < 0: return "test_worse", d_pnl, d_sd
    return "tied", d_pnl, d_sd


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}"); continue
        results[tag] = {name: summarize(run_scenario(name, ov, scid, tag))
                        for name, ov in SCENARIOS.items()}

    print("\n" + "=" * 86)
    print(" M8 FADE-TYPE A/B (prod config, M8_FADE_FULL ON, early-scratch ON)")
    print("=" * 86)
    print(f"  {'contract':8s} {'scenario':11s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sort/d':>8s} {'M8n':>4s} {'M8net':>9s}")
    for tag, scn in results.items():
        for name, r in scn.items():
            print(f"  {tag:8s} {name:11s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>8.3f} "
                  f"{r['m8']['n']:>4} {r['m8']['net']:>+9,.0f}")
        print()

    print(" VERDICTS (gate = test_better on ALL three):")
    for arm in TEST_ARMS:
        print(f"\n  --- {arm} ---")
        vs = []
        for tag, scn in results.items():
            v, dp, ds = verdict(scn[BASELINE], scn[arm])
            vs.append(v)
            print(f"    {tag:8s} {v:12s} dPnL={dp:>+9,.0f}  dSortino={ds:>+.3f}")
        ok = all(v == "test_better" for v in vs)
        print(f"    => {'SHIP (all test_better)' if ok else 'NO-SHIP (' + ', '.join(sorted(set(vs))) + ')'}")


if __name__ == "__main__":
    main()
