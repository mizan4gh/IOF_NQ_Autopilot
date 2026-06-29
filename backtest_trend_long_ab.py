"""A/B: add a VWAP-reclaim-and-go TREND-LONG mode vs the fade-only baseline.
3-contract gate.

Motivated by 2026-06-29 (SCLog + NiceSetup screenshot): the system has NO
trend-long mode, so it missed the 29,300→30,050 V-reversal trend and actually
faded *into* it (M8 short, −$1,015). This prototypes the user-chosen trigger
("VWAP reclaim + go"): LONG when price was below VWAP over the last
TREND_VWAP_LB bars, reclaims it this bar with buyer delta + bull body. Stop =
below the reclaim swing low; targets ATR-based (wide T2 to ride). Rides the
disabled M5 slot → fires in the CSV as mode "M5".

  baseline    fade-only prod (M8_FADE_FULL ON, early-scratch ON)
  trend_long  baseline + TREND_LONG=True

This is a NEW ENTRY MODE, not a gate tweak — treat a pass as a hypothesis to
re-test, not a green light. Cross-contract gate [[feedback_cross_contract_ab]]:
ship consideration only if test_better on all three (NQZ25/NQM5/NQH6).

Usage: python backtest_trend_long_ab.py [NQZ25|NQM5|NQH6]   (default: all)
"""
import csv, math, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")
CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "F.US.ENQM25.scid",
    "NQH6":  SC_DATA / "F.US.ENQH26.scid",
}
_PROD = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(), QUAL_FLOOR_M8=None,
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False,
)
SCENARIOS = {
    "baseline":   {**_PROD, "TREND_LONG": False},
    "trend_long": {**_PROD, "TREND_LONG": True},
}
BASELINE = "baseline"; TEST_ARMS = ["trend_long"]


def run_scenario(name, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_trl_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {name}  (TREND_LONG={ov['TREND_LONG']}) =====")
    backtest.main()
    return out


def wd(d0, d1):
    d = date.fromisoformat(d0); e = date.fromisoformat(d1); n = 0
    while d <= e:
        if d.weekday() < 5: n += 1
        d += timedelta(days=1)
    return n


def summarize(p):
    tr, prev = [], 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] == "EXIT":
            t = float(r["TotalPnL"])
            tr.append(dict(pnl=t - prev, date=r["Date"], mode=r["Mode"],
                           reason=r["ExitReason"])); prev = t
    n = len(tr)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0,
                    s_day=float("nan"), trl=dict(n=0, net=0.0, wr=0.0))
    pn = [t["pnl"] for t in tr]; w = [p for p in pn if p > 0]; l = [p for p in pn if p < 0]
    total = sum(pn); pf = (sum(w) / abs(sum(l))) if l else float("inf")
    pk = run = md = 0.0
    for p in pn:
        run += p; pk = max(pk, run); md = min(md, run - pk)
    dl = defaultdict(float)
    for t in tr: dl[t["date"]] += t["pnl"]
    ds = sorted(dl); nd = wd(ds[0], ds[-1]); mu = sum(dl.values()) / nd
    dd = math.sqrt(sum(min(v, 0.0) ** 2 for v in dl.values()) / nd)
    sd = mu / dd if dd > 0 else float("inf")
    trl = [t["pnl"] for t in tr if t["mode"] == "M5"]   # trend-long fires as M5
    trw = [p for p in trl if p > 0]
    trld = dict(n=len(trl), net=sum(trl), wr=100*len(trw)/len(trl) if trl else 0.0)
    return dict(n=n, total=total, pf=pf, wr=100*len(w)/n, max_dd=md, s_day=sd, trl=trld)


def verdict(b, t):
    dp = t["total"] - b["total"]; dd = t["s_day"] - b["s_day"]
    if abs(dp) < 1.0 and abs(dd) < 1e-9: return "no-op", dp, dd
    if dp > 0 and dd >= 0: return "test_BETTER", dp, dd
    if dp < 0 or dd < 0: return "test_worse", dp, dd
    return "tied", dp, dd


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}"); continue
        res[tag] = {nm: summarize(run_scenario(nm, ov, scid, tag))
                    for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 88)
    print(" TREND-LONG A/B (prod config, M8_FADE_FULL ON, early-scratch ON)")
    print("=" * 88)
    print(f"  {'contract':8s} {'scenario':11s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sort/d':>8s}  {'TRLn':>5s} {'TRL_WR':>7s} {'TRLnet':>9s}")
    for tag, scn in res.items():
        for nm, r in scn.items():
            print(f"  {tag:8s} {nm:11s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>8.3f}  "
                  f"{r['trl']['n']:>5} {r['trl']['wr']:>6.1f}% {r['trl']['net']:>+9,.0f}")
        print()

    print(" VERDICT (gate = test_BETTER on all three):")
    for arm in TEST_ARMS:
        vs = []
        for tag, scn in res.items():
            v, dp, dd = verdict(scn[BASELINE], scn[arm]); vs.append(v)
            print(f"   {tag:8s} {v:12s} dPnL={dp:>+9,.0f}  dSortino={dd:>+.3f}  "
                  f"(trend-longs: {scn[arm]['trl']['n']}, net {scn[arm]['trl']['net']:+,.0f})")
        ok = all(v == "test_BETTER" for v in vs)
        print(f"   => {'SHIP-CANDIDATE' if ok else 'NO-SHIP'} ({', '.join(sorted(set(vs)))})")


if __name__ == "__main__":
    main()
