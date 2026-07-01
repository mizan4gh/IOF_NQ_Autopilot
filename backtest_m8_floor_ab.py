"""A/B: M8 quality floor 60 vs 70 vs baseline(50). 3-contract gate, restored
+ stabilized data (post the 2026-06-29 NQH6 rewrite — see
[[project_nqh6_data_rewrite_incident]]).

For M8 fades qual100 = edge_sc*10, so the floor buckets in 10s:
  baseline  QUAL_FLOOR_M8=None -> QUAL_FLOOR=50  (keep edge>=5)
  floor60   QUAL_FLOOR_M8=60                     (keep edge>=6)
  floor70   QUAL_FLOOR_M8=70                     (keep edge>=7, sheds Q<=60)

Context: M8 is contract-dependent (loser NQZ25, winner NQM5+NQH6), so full
disable fails the gate; floor is the targeted lever
([[project_m8_disable_floor_noop]]). Q70 asks whether tightening past 60 helps.

Usage: python backtest_m8_floor_ab.py [NQZ25|NQM5|NQH6]   (default: all)
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
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(),
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False, TREND_LONG=False,
)
SCENARIOS = {
    "baseline": {**_PROD, "QUAL_FLOOR_M8": None},
    "floor60":  {**_PROD, "QUAL_FLOOR_M8": 60},
    "floor70":  {**_PROD, "QUAL_FLOOR_M8": 70},
}
BASELINE = "baseline"; TEST_ARMS = ["floor60", "floor70"]


def run(nm, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_m8flr_{tag}_{nm}.csv"
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
        return dict(n=0, wr=0, pf=0, tot=0, md=0, sd=float("nan"), m8n=0, m8net=0)
    pn = [x[0] for x in tr]; w = [p for p in pn if p > 0]; l = [p for p in pn if p < 0]
    tot = sum(pn); pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run = md = 0.0
    for p in pn:
        run += p; pk = max(pk, run); md = min(md, run - pk)
    dl = defaultdict(float)
    for x in tr: dl[x[1]] += x[0]
    ds = sorted(dl); nd = wd(ds[0], ds[-1])
    sd = (sum(dl.values()) / nd) / (math.sqrt(sum(min(v, 0) ** 2 for v in dl.values()) / nd) or 1)
    m8 = [x[0] for x in tr if x[2] == "M8"]; m8w = [p for p in m8 if p > 0]
    return dict(n=len(tr), wr=100*len(w)/len(tr), pf=pf, tot=tot, md=md, sd=sd,
                m8n=len(m8), m8net=sum(m8), m8wr=100*len(m8w)/len(m8) if m8 else 0)


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

    print("\n" + "=" * 90)
    print(" M8 QUALITY-FLOOR A/B  (60 vs 70 vs baseline-50), restored+stable data")
    print("=" * 90)
    print(f"  {'contract':8s} {'scenario':9s} {'n':>4s} {'WR':>5s} {'PF':>5s} {'Net':>10s} "
          f"{'MaxDD':>9s} {'Sort':>6s}  {'M8n':>4s} {'M8wr':>5s} {'M8net':>9s}")
    for tag, scn in res.items():
        for nm, r in scn.items():
            print(f"  {tag:8s} {nm:9s} {r['n']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
                  f"{r['tot']:>+10,.0f} {r['md']:>9,.0f} {r['sd']:>6.2f}  "
                  f"{r['m8n']:>4} {r['m8wr']:>4.0f}% {r['m8net']:>+9,.0f}")
        print()

    print(" VERDICTS (gate = test_BETTER on all three):")
    for arm in TEST_ARMS:
        vs = []
        for tag, scn in res.items():
            v, dp, dd = verdict(scn[BASELINE], scn[arm]); vs.append(v)
            print(f"   {arm:8s} {tag:8s} {v:12s} dPnL={dp:>+9,.0f}  dSort={dd:>+.3f}")
        ok = all(x == "test_BETTER" for x in vs)
        print(f"   => {arm}: {'SHIP-CANDIDATE' if ok else 'NO-SHIP'} ({', '.join(sorted(set(vs)))})\n")


if __name__ == "__main__":
    main()
