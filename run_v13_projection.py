"""v13 rewrite (IOF_NQ_Autopilot_v13.cpp) monthly-profit projection + MaxDD.

NOT an A/B ship gate — a projection run. Models v13 via backtest.py V13_MODEL
(M8 redefined: type-1 balance-edge fade w/o trap term + type-3 trend
exhaustion; v12 fade types 2/4 gone). Fidelity caveats:
  * v13's 12-signal score simplification (drop S7/S8/S10/S11) is NOT modelable
    (Python score is a 5-bar delta count) — M1/M4/M6 are modeled as v12.37.
  * type-1 edge start (bkVerifyScore<=2 => 2) ported into backtest.py under
    V13_MODEL for this run.

Arms (all on the LIVE panel: MT=1, DL=$800, news on, cool 36, 3k vol,
early-scratch on, floors 50):
  v12_live  reference = current live v12.38 (M8_FADE_FULL, M8 floor 60)
  v13       v13 panel defaults (all floors 50)
  v13_f60   v13 with the live M8 floor 60 kept

Data: 6 FROZEN project-dir snapshots (see project_nqh6_data_rewrite_incident).
Usage: python run_v13_projection.py [TAG]   (default: all six)
"""
import csv, math, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQM5":  BASE / "F.US.ENQM25.scid",   # Jun-2025
    "NQU25": BASE / "F.US.ENQU25.scid",   # Sep-2025
    "NQZ25": BASE / "F.US.ENQZ25.scid",   # Dec-2025
    "NQH6":  BASE / "F.US.ENQH26.scid",   # Mar-2026
    "NQM6":  BASE / "F.US.ENQM26.scid",   # Jun-2026
    "NQU26": BASE / "F.US.ENQU26.scid",   # Sep-2026 (LIVE, short history)
}
_PANEL = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, DISABLE_MODES=set(), M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
)
SCENARIOS = {
    "v12_live": {**_PANEL, "M8_FADE_FULL": True,  "V13_MODEL": False,
                 "QUAL_FLOOR_M8": 60},
    "v13":      {**_PANEL, "M8_FADE_FULL": False, "V13_MODEL": True,
                 "QUAL_FLOOR_M8": None},
    "v13_f60":  {**_PANEL, "M8_FADE_FULL": False, "V13_MODEL": True,
                 "QUAL_FLOOR_M8": 60},
}

# Per-contract volume-bar cache (pattern from backtest_trend_iter2_ab.py):
# bars are identical across arms (same TARGET_VOL, same frozen scid) and the
# engine never mutates Bar objects. Keyed on (path, TARGET_VOL).
_BAR_CACHE = {}


def run_scenario(name, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    key = (str(scid), ov["TARGET_VOL"])
    if key in _BAR_CACHE:
        bars = _BAR_CACHE[key]
        backtest.read_scid = lambda path: []
        backtest.build_volume_bars = \
            lambda recs, target_vol=ov["TARGET_VOL"], price_scale=1.0: bars
        print(f"  [cache] reusing {len(bars):,} pre-built bars for {tag}")
    else:
        _orig = backtest.build_volume_bars
        def _build_and_cache(recs, target_vol=ov["TARGET_VOL"], price_scale=1.0,
                             _orig=_orig, _key=key):
            b = _orig(recs, target_vol=target_vol, price_scale=price_scale)
            _BAR_CACHE[_key] = b
            return b
        backtest.build_volume_bars = _build_and_cache
    out = BASE / f"IOF_NQ_v13proj_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {name} =====")
    backtest.main()
    return out


def month_iter(m0, m1):
    y, m = int(m0[:4]), int(m0[5:7])
    while True:
        cur = f"{y:04d}-{m:02d}"
        yield cur
        if cur == m1: return
        m += 1
        if m == 13: y, m = y + 1, 1


def summarize(p):
    tr, prev = [], 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] == "EXIT":
            t = float(r["TotalPnL"])
            tr.append(dict(pnl=t - prev, date=r["Date"], mode=r["Mode"])); prev = t
    if not tr:
        return dict(n=0, wr=0.0, pf=0.0, tot=0.0, md=0.0, months={}, m8net=0.0, m8n=0)
    pn = [t["pnl"] for t in tr]
    w = [x for x in pn if x > 0]; l = [x for x in pn if x < 0]
    tot = sum(pn); pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run = md = 0.0
    for x in pn:
        run += x; pk = max(pk, run); md = min(md, run - pk)
    mm = defaultdict(float)
    for t in tr: mm[t["date"][:7]] += t["pnl"]
    ms = sorted(mm)
    months = {m: mm.get(m, 0.0) for m in month_iter(ms[0], ms[-1])}
    m8 = [t["pnl"] for t in tr if t["mode"] == "M8"]
    return dict(n=len(tr), wr=100 * len(w) / len(tr), pf=pf, tot=tot, md=md,
                months=months, m8net=sum(m8), m8n=len(m8))


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}"); continue
        res[tag] = {nm: summarize(run_scenario(nm, ov, scid, tag))
                    for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 100)
    print(" v13 PROJECTION — live panel (MT=1 DL=800), 6 frozen contracts")
    print("=" * 100)
    print(f"  {'contract':8s} {'arm':9s} {'n':>4s} {'WR':>5s} {'PF':>5s} "
          f"{'Net':>9s} {'MaxDD':>9s} {'mo':>3s} {'avg/mo':>8s} {'worst-mo':>9s} "
          f"{'best-mo':>8s} {'M8net':>8s} {'M8n':>4s}")
    for tag, scn in res.items():
        for nm, r in scn.items():
            mv = list(r["months"].values())
            nmo = len(mv)
            avg = sum(mv) / nmo if nmo else 0.0
            print(f"  {tag:8s} {nm:9s} {r['n']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
                  f"{r['tot']:>+9,.0f} {r['md']:>9,.0f} {nmo:>3} {avg:>+8,.0f} "
                  f"{min(mv) if mv else 0:>+9,.0f} {max(mv) if mv else 0:>+8,.0f} "
                  f"{r['m8net']:>+8,.0f} {r['m8n']:>4}")
        print()

    print(" pooled monthly distribution per arm (all contract-months):")
    for nm in SCENARIOS:
        allm = []
        for tag in res:
            allm += list(res[tag][nm]["months"].values())
        if not allm: continue
        allm.sort()
        k = len(allm)
        med = allm[k // 2] if k % 2 else (allm[k//2 - 1] + allm[k//2]) / 2
        mean = sum(allm) / k
        neg = sum(1 for v in allm if v < 0)
        wmd = min(res[tag][nm]["md"] for tag in res)
        print(f"  {nm:9s} months={k:>3} mean={mean:>+8,.0f} median={med:>+8,.0f} "
              f"p10={allm[max(0, k//10)]:>+8,.0f} worst={allm[0]:>+9,.0f} "
              f"best={allm[-1]:>+8,.0f} neg-mo={100*neg/k:>4.0f}%  "
              f"worstMaxDD={wmd:>9,.0f}")


if __name__ == "__main__":
    main()
