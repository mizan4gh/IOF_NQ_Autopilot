"""A/B: 2-lot TP1/TP2 scale-out vs live single-lot baseline, CURRENT live config.

"Backtest with TP1 and TP2" = 2 lots per entry: lot1 exits at TP1, the runner
exits at TP2 (else trails). Baseline = live single-lot (trail after T1->BE).

Config = current live panel: MT=1, DAILY_LOSS=800, M8 floor-60, news ON,
early-scratch ON. Frozen F.US.E* snapshots (Sierra rewrites the live Data-dir
scids -- see memory nqh6_data_rewrite_incident). Canonical 3-contract gate.

Prior falsifications: scaleout_2lot_ab (harmful at MT=3/floor-50) and
hard_tp1_exit_ab (disagreed). Re-run because live config changed to MT=1/floor-60.

Usage: python backtest_scaleout_tp1tp2_ab.py [TAG]   (default: NQZ25 NQM5 NQH6)
"""
import csv, sys, math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQZ25": BASE / "F.US.ENQZ25.scid",   # Dec-2025
    "NQM5":  BASE / "F.US.ENQM25.scid",   # Jun-2025
    "NQH6":  BASE / "F.US.ENQH26.scid",   # Mar-2026
}
_LIVE = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000,
    MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, QUAL_FLOOR_M8=60, M8_FADE_FULL=True,
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False, TREND_LONG=False,
    DISABLE_MODES=set(),
)
SCENARIOS = {
    "baseline_1lot": {**_LIVE, "SCALE_OUT": False, "BASE_QTY": 1, "SIZE_BY_RM": False},
    "scaleout_tp1tp2": {**_LIVE, "SCALE_OUT": True, "BASE_QTY": 2, "SIZE_BY_RM": False},
}


def run(nm, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_so2_{tag}_{nm}.csv"
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
        return dict(n=0, wr=0, pf=0, tot=0, md=0, sd=float("nan"))
    pn = [x[0] for x in tr]; w = [p for p in pn if p > 0]; l = [p for p in pn if p < 0]
    tot = sum(pn); pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run = md = 0.0
    for p in pn:
        run += p; pk = max(pk, run); md = min(md, run - pk)
    dl = defaultdict(float)
    for x in tr: dl[x[1]] += x[0]
    ds = sorted(dl); nd = wd(ds[0], ds[-1])
    sd = (sum(dl.values()) / nd) / (math.sqrt(sum(min(v, 0) ** 2 for v in dl.values()) / nd) or 1)
    return dict(n=len(tr), wr=100 * len(w) / len(tr), pf=pf, tot=tot, md=md, sd=sd)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists(): print(f"missing: {scid}"); continue
        res[tag] = {nm: s(run(nm, ov, scid, tag)) for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 86)
    print(" 2-LOT TP1/TP2 SCALE-OUT vs LIVE 1-LOT  (MT=1, DL=800, M8 floor-60)")
    print("=" * 86)
    print(f"  {'contract':8s} {'scenario':16s} {'n':>3} {'WR':>6} {'PF':>6} {'Net':>11} {'MaxDD':>10} {'Sortino':>8}")
    for tag, scn in res.items():
        for nm in SCENARIOS:
            r = scn[nm]
            print(f"  {tag:8s} {nm:16s} {r['n']:>3} {r['wr']:>5.0f}% {r['pf']:>6.2f} "
                  f"{r['tot']:>+11,.0f} {r['md']:>+10,.0f} {r['sd']:>8.3f}")
        print()

    print(" Deltas (scaleout - baseline):")
    pdel, dddel = {}, {}
    for tag in res:
        b, f = res[tag]["baseline_1lot"], res[tag]["scaleout_tp1tp2"]
        pdel[tag] = f["tot"] - b["tot"]; dddel[tag] = f["md"] - b["md"]
        print(f"   {tag:8s} P&L {pdel[tag]:>+9,.0f}   MaxDD {dddel[tag]:>+9,.0f}"
              f"   (dd better = positive)")
    np_ = sum(d > 0 for d in pdel.values())
    print(f"\n P&L gate  : scaleout better on {np_}/{len(pdel)} => "
          + ("SHIP-CANDIDATE" if np_ == len(pdel) else "NO-SHIP (disagree/worse)"))
    ndd = sum(d > 0 for d in dddel.values())
    print(f" MaxDD gate: scaleout better on {ndd}/{len(dddel)}")


if __name__ == "__main__":
    main()
