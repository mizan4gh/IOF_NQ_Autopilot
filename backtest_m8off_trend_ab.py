"""A/B: disable M8 entirely (DISABLE_MODES={7}), and M8-off + trend-long,
vs the all-modes baseline. 3-contract gate.

Motivated by the 2026-06-29 trend-long finding: the trend-long's apparent NQH6
"win" was an artifact of it occupying bars that would otherwise be LOSING M8
fades. That points straight at M8 as the real NQH6 problem (M8 −$16.7k there).
So test the direct lever: just turn M8 off. And test whether the trend-long
adds genuine edge once M8 is gone (m8_off_trend) — if m8_off_trend ≈ m8_off,
the trend-long was only ever masking M8.

  baseline       all modes on (M8_FADE_FULL, prod)
  m8_off         DISABLE_MODES={7}
  m8_off_trend   DISABLE_MODES={7} + TREND_LONG (ts>=0.5)

Prior: m8_off passed the 3-contract gate (2026-06-26) but magnitude is inflated
by the Imb bug [[reference_backtest_imb_extreme_bug]] — floor-60 was preferred
([[project_m8_disable_floor_noop]]). This re-confirms on restored data.

Usage: python backtest_m8off_trend_ab.py [NQZ25|NQM5|NQH6]   (default: all)
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
    QUAL_FLOOR=50, M8_FADE_FULL=True, QUAL_FLOOR_M8=None,
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False,
    TREND_LONG=False, TREND_MIN_TS=0.5, TREND_MAX_CHOP=99,
)
SCENARIOS = {
    "baseline":     {**_PROD, "DISABLE_MODES": set()},
    "m8_off":       {**_PROD, "DISABLE_MODES": {7}},
    "m8_off_trend": {**_PROD, "DISABLE_MODES": {7}, "TREND_LONG": True},
}
BASELINE = "baseline"; TEST_ARMS = ["m8_off", "m8_off_trend"]


def run(nm, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_m8off_{tag}_{nm}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {nm} =====")
    backtest.main()
    return out


def wd(a, b):
    d = date.fromisoformat(a); e = date.fromisoformat(b); n = 0
    while d <= e:
        if d.weekday() < 5: n += 1
        d += timedelta(days=1)
    return n


def s(p):
    tr, pv = [], 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] == "EXIT":
            t = float(r["TotalPnL"]); tr.append((t - pv, r["Date"], r["Mode"])); pv = t
    if not tr:
        return dict(n=0, wr=0, pf=0, tot=0, md=0, sd=float("nan"), by={})
    pn = [x[0] for x in tr]; w = [p for p in pn if p > 0]; l = [p for p in pn if p < 0]
    tot = sum(pn); pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run = md = 0.0
    for p in pn:
        run += p; pk = max(pk, run); md = min(md, run - pk)
    dl = defaultdict(float)
    for x in tr: dl[x[1]] += x[0]
    ds = sorted(dl); nd = wd(ds[0], ds[-1])
    sd = (sum(dl.values()) / nd) / (math.sqrt(sum(min(v, 0) ** 2 for v in dl.values()) / nd) or 1)
    by = defaultdict(float)
    for x in tr: by[x[2]] += x[0]
    return dict(n=len(tr), wr=100*len(w)/len(tr), pf=pf, tot=tot, md=md, sd=sd, by=dict(by))


def verdict(b, t):
    dp = t["tot"] - b["tot"]; dd = t["sd"] - b["sd"]
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
        res[tag] = {nm: s(run(nm, ov, scid, tag)) for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 92)
    print(" M8-OFF (+trend) A/B  — prod config, restored data")
    print("=" * 92)
    print(f"  {'contract':8s} {'scenario':13s} {'n':>4s} {'WR':>5s} {'PF':>5s} {'Net':>10s} "
          f"{'MaxDD':>9s} {'Sort':>6s}   per-mode net")
    for tag, scn in res.items():
        for nm, r in scn.items():
            modes = " ".join(f"{m}:{v:+,.0f}" for m, v in sorted(r["by"].items()))
            print(f"  {tag:8s} {nm:13s} {r['n']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
                  f"{r['tot']:>+10,.0f} {r['md']:>9,.0f} {r['sd']:>6.2f}   {modes}")
        print()

    print(" VERDICTS (gate = test_BETTER on all three):")
    for arm in TEST_ARMS:
        vs = []
        for tag, scn in res.items():
            v, dp, dd = verdict(scn[BASELINE], scn[arm]); vs.append(v)
            print(f"   {arm:13s} {tag:8s} {v:12s} dPnL={dp:>+9,.0f}  dSort={dd:>+.3f}")
        ok = all(x == "test_BETTER" for x in vs)
        print(f"   => {arm}: {'SHIP-CANDIDATE' if ok else 'NO-SHIP'} ({', '.join(sorted(set(vs)))})\n")


if __name__ == "__main__":
    main()
