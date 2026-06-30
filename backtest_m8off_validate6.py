"""3-way on 6 frozen contracts: baseline-50 vs floor-60 vs M8-OFF (complete
disable). Answers 'should we completely disable M8?' Reuses the existing
IOF_NQ_flr6_{tag}_{baseline,floor60}.csv and only runs the new m8off arm.
"""
import csv, math, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQU25": BASE / "F.US.ENQU25.scid", "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQM5":  BASE / "F.US.ENQM25.scid", "NQH6":  BASE / "F.US.ENQH26.scid",
    "NQM6":  BASE / "F.US.ENQM26.scid", "NQU26": BASE / "F.US.ENQU26.scid",
}
_PROD = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, TREND_LONG=False, QUAL_FLOOR_M8=None,
)


def run_m8off(scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in {**_PROD, "DISABLE_MODES": {7}}.items():
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_flr6_{tag}_m8off.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: m8off =====")
    backtest.main()
    return out


def wd(a, b):
    d = date.fromisoformat(a); e = date.fromisoformat(b); n = 0
    while d <= e:
        if d.weekday() < 5: n += 1
        d += timedelta(days=1)
    return n


def s(p):
    if not Path(p).exists():
        return None
    tr, pv = [], 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] == "EXIT":
            t = float(r["TotalPnL"]); tr.append((t - pv, r["Date"])); pv = t
    if not tr:
        return dict(n=0, tot=0, md=0, sd=float("nan"), pf=0)
    pn = [x[0] for x in tr]; w = [p for p in pn if p > 0]; l = [p for p in pn if p < 0]
    tot = sum(pn); pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run = md = 0.0
    for p in pn:
        run += p; pk = max(pk, run); md = min(md, run - pk)
    dl = defaultdict(float)
    for x in tr: dl[x[1]] += x[0]
    ds = sorted(dl); nd = wd(ds[0], ds[-1])
    sd = (sum(dl.values()) / nd) / (math.sqrt(sum(min(v, 0) ** 2 for v in dl.values()) / nd) or 1)
    return dict(n=len(tr), tot=tot, md=md, sd=sd, pf=pf)


def main():
    tags = list(CONTRACTS)
    for tag in tags:
        if not (BASE / f"IOF_NQ_flr6_{tag}_m8off.csv").exists():
            run_m8off(CONTRACTS[tag], tag)

    print("\n" + "=" * 88)
    print(" baseline-50 vs floor-60 vs M8-OFF — 6 frozen contracts (NQU26=LIVE), 1-lot")
    print("=" * 88)
    print(f"  {'contract':8s} {'arm':9s} {'n':>3s} {'Net':>9s} {'MaxDD':>8s} {'Sort':>6s} {'PF':>5s}")
    pooled = defaultdict(lambda: [0.0, 0.0])
    for tag in tags:
        rows = {a: s(BASE / f"IOF_NQ_flr6_{tag}_{a}.csv") for a in ("baseline", "floor60", "m8off")}
        for a in ("baseline", "floor60", "m8off"):
            r = rows[a]
            if r is None:
                print(f"  {tag:8s} {a:9s}  (missing)"); continue
            print(f"  {tag:8s} {a:9s} {r['n']:>3} {r['tot']:>+9,.0f} {r['md']:>8,.0f} {r['sd']:>6.2f} {r['pf']:>5.2f}")
            pooled[a][0] += r["tot"]; pooled[a][1] += r["md"]
        b = rows["baseline"]
        for a in ("floor60", "m8off"):
            r = rows[a]
            if r and b:
                print(f"           {a+' vs base':14s} dPnL={r['tot']-b['tot']:>+8,.0f}  dMaxDD={r['md']-b['md']:>+8,.0f}")
        print()

    print(" POOLED across 6 contracts:")
    for a in ("baseline", "floor60", "m8off"):
        print(f"   {a:9s} Net {pooled[a][0]:>+10,.0f}   sum-MaxDD {pooled[a][1]:>+10,.0f}")
    print(f"\n  floor60 vs m8off pooled Net: {pooled['floor60'][0]-pooled['m8off'][0]:+,.0f} "
          f"(positive => floor-60 beats complete disable)")


if __name__ == "__main__":
    main()
