"""ES portability projection for the live v12.38 config (NOT a ship gate).

Question: does the NQ strategy's edge survive a move to ES?

Measured conversions (probe vs NQU26/ESU6 over 28 common days, 2026-07-06):
  price NQ/ES  = 4.00  -> all point-denominated floors scale /4
  volume ES/NQ = 2.76  -> TARGET_VOL 3000 -> 8000 to match NQ bar cadence
  PT_VAL       = 50.0  (ES $50/pt vs NQ $20/pt)
Dollar note: with price/4 and $50/pt, a same-percent move on ES pays
0.625x the NQ dollars (50/20 / 4) — compare shapes (PF/WR), not raw Net.

Arms (live panel: MT=1, DL=800, news on, cool 36, early-scratch on,
M8_FADE_FULL, M8 floor 60):
  es_naive   symbol swap only (PT_VAL=50) — floors stay in NQ points, so the
             stop clamp [20,40]pt forces ~3x-too-wide ES stops. Expected
             distorted; included to show why a plain swap is wrong.
  es_scaled  cadence + floor scaled port (the fair test).

Data: FROZEN_ES*.CME.scid snapshots copied from Sierra Data 2026-07-06
(price_scale=100 files — requires the backtest.py low=0 fix in tree).
Usage: python run_es_projection.py [ESZ5|...]   (default: all six)
"""
import csv, sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "ESM5": BASE / "FROZEN_ESM5.CME.scid",   # Jun-2025
    "ESU5": BASE / "FROZEN_ESU5.CME.scid",   # Sep-2025
    "ESZ5": BASE / "FROZEN_ESZ5.CME.scid",   # Dec-2025
    "ESH6": BASE / "FROZEN_ESH6.CME.scid",   # Mar-2026
    "ESM6": BASE / "FROZEN_ESM6.CME.scid",   # Jun-2026
    "ESU6": BASE / "FROZEN_ESU6.CME.scid",   # Sep-2026 (live, short history)
}
_PANEL = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, SCALE_OUT=False,
    MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, DISABLE_MODES=set(),
    M8_FADE_FULL=True, QUAL_FLOOR_M8=60, M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False, V13_MODEL=False,
    PT_VAL=50.0,
)
SCENARIOS = {
    "es_naive":  {**_PANEL, "TARGET_VOL": 3000},
    "es_scaled": {**_PANEL, "TARGET_VOL": 8000,
                  "C_STOP_FL": 5.0,   "C_STOP_CL": 10.0,
                  "C_T1_FL":   6.25,  "C_T1_CL":   12.5,
                  "C_T2_FL":  18.75,  "C_T2_CL":   31.25},
}

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
    out = BASE / f"IOF_ES_proj_{tag}_{name}.csv"
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
    print(" ES PORTABILITY PROJECTION — live panel (MT=1 DL=800), 6 frozen ES contracts")
    print("=" * 100)
    print(f"  {'contract':8s} {'arm':10s} {'n':>4s} {'WR':>5s} {'PF':>5s} "
          f"{'Net':>9s} {'MaxDD':>9s} {'mo':>3s} {'avg/mo':>8s} {'worst-mo':>9s} "
          f"{'best-mo':>8s} {'M8net':>8s} {'M8n':>4s}")
    for tag, scn in res.items():
        for nm, r in scn.items():
            mv = list(r["months"].values())
            nmo = len(mv)
            avg = sum(mv) / nmo if nmo else 0.0
            print(f"  {tag:8s} {nm:10s} {r['n']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
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
        print(f"  {nm:10s} months={k:>3} mean={mean:>+8,.0f} median={med:>+8,.0f} "
              f"p10={allm[max(0, k//10)]:>+8,.0f} worst={allm[0]:>+9,.0f} "
              f"best={allm[-1]:>+8,.0f} neg-mo={100*neg/k:>4.0f}%  "
              f"worstMaxDD={wmd:>9,.0f}")


if __name__ == "__main__":
    main()
