"""
MNQU6 fresh-OOS re-test of the M2-disable decision.

MNQ == NQ (same Nasdaq-100 price series; MNQ stored x100, worth 1/10 the $).
So MNQU6 is NQ-equivalent data. Its value here: it runs to 2026-07-22, ~19
trading days PAST the frozen NQU26 (ends 2026-06-30) — genuine fresh OOS that
the NQ frozen set can't provide.

Measured conversions (28 overlap days, run_mnq_oos prep):
  MNQ/NQ volume ratio = 5.88  ->  TARGET_VOL 3000 -> 17500 (NQ bar cadence)
  price_scale = 100 (auto-detected; needs backtest.py low=0 fix in tree)
  PT_VAL kept at 20 (NQ $) so numbers stack directly on the NQ M2-off table;
  micro dollars = these / 10.

Arms (both IMB_MODEL=cpp_stateful, live panel MT=1/DL=800/M8 floor-60):
  m2_on   DISABLE_MODES=set()
  m2_off  DISABLE_MODES={1}   (1 == M2)

Results are split at 2026-06-30:
  OVERLAP (<=06-30)  — should ~reproduce NQU26's M2-off result = validation that
                       the x100/vol-scaling handling is faithful.
  FRESH OOS (>=07-01) — the real independent re-test: does M2-off still win on
                        data neither the fix nor the decision has ever seen?
"""
import csv, sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
SCID = BASE / "FROZEN_MNQU6_0722.scid"
SPLIT = "2026-06-30"   # <= SPLIT: overlap w/ NQU26;  > SPLIT: fresh OOS

_PANEL = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=17500, SCALE_OUT=False,
    MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    M8_FADE_FULL=True, V13_MODEL=False, QUAL_FLOOR_M8=60,
    IMB_MODEL="cpp_stateful", PT_VAL=20.0,
)
SCENARIOS = {
    "m2_on":  {**_PANEL, "DISABLE_MODES": set()},
    "m2_off": {**_PANEL, "DISABLE_MODES": {1}},
}

_BAR_CACHE = {}


def run_scenario(name, ov):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    key = ov["TARGET_VOL"]
    if key in _BAR_CACHE:
        bars = _BAR_CACHE[key]
        backtest.read_scid = lambda path: []
        # keep auto price_scale path irrelevant on cache hit
        backtest.build_volume_bars = \
            lambda recs, target_vol=key, price_scale=1.0: bars
        print(f"  [cache] reusing {len(bars):,} bars")
    else:
        _orig = backtest.build_volume_bars

        def _build_and_cache(recs, target_vol=key, price_scale=1.0,
                             _orig=_orig, _key=key):
            b = _orig(recs, target_vol=target_vol, price_scale=price_scale)
            _BAR_CACHE[_key] = b
            return b
        backtest.build_volume_bars = _build_and_cache
    out = BASE / f"IOF_NQ_mnqoos_{name}.csv"
    sys.argv = ["backtest.py", str(SCID), str(out)]
    print(f"\n===== MNQU6 :: {name} =====")
    backtest.main()
    return out


def split_stats(p):
    """Return dict per segment ('overlap','oos','all') with n/net/md/permode."""
    segs = {"overlap": [], "oos": [], "all": []}
    prev = 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] != "EXIT":
            continue
        t = float(r["TotalPnL"])
        pnl = t - prev
        prev = t
        seg = "overlap" if r["Date"] <= SPLIT else "oos"
        rec = dict(pnl=pnl, mode=r["Mode"], date=r["Date"])
        segs[seg].append(rec)
        segs["all"].append(rec)

    def agg(rows):
        if not rows:
            return dict(n=0, net=0.0, md=0.0, wr=0.0, permode={})
        pn = [x["pnl"] for x in rows]
        eq = pk = md = 0.0
        for x in pn:
            eq += x
            pk = max(pk, eq)
            md = min(md, eq - pk)
        w = [x for x in pn if x > 0]
        pm = defaultdict(lambda: [0, 0.0])
        for x in rows:
            pm[x["mode"]][0] += 1
            pm[x["mode"]][1] += x["pnl"]
        return dict(n=len(rows), net=sum(pn), md=md,
                    wr=100 * len(w) / len(rows), permode=dict(pm))
    return {k: agg(v) for k, v in segs.items()}


def main():
    res = {nm: split_stats(run_scenario(nm, ov)) for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 88)
    print(" MNQU6 M2-disable re-test  (NQ-equiv $, PT_VAL=20; micro $ = /10)")
    print(f"   OVERLAP = trades <= {SPLIT} (validation vs NQU26)")
    print(f"   FRESH OOS = trades >  {SPLIT} (19 days NQ frozen set lacks)")
    print("=" * 88)
    for seg, label in [("overlap", "OVERLAP (val)"), ("oos", "FRESH OOS"),
                       ("all", "FULL MNQU6")]:
        print(f"\n  --- {label} ---")
        print(f"  {'arm':8s} {'n':>4s} {'WR':>5s} {'Net($NQ)':>9s} "
              f"{'Net($MNQ)':>10s} {'MaxDD':>8s}")
        for nm in SCENARIOS:
            r = res[nm][seg]
            print(f"  {nm:8s} {r['n']:>4} {r['wr']:>5.1f} {r['net']:>+9,.0f} "
                  f"{r['net']/10:>+10,.0f} {r['md']:>8,.0f}")
        d = res["m2_off"][seg]["net"] - res["m2_on"][seg]["net"]
        v = "off_better" if d > 0 else ("off_worse" if d < 0 else "tied")
        print(f"  {'DELTA':8s} {'':>4} {'':>5} {d:>+9,.0f} {d/10:>+10,.0f}"
              f"    -> {v}")

    print("\n  per-mode net (FRESH OOS, $NQ):")
    modes = sorted({m for nm in SCENARIOS for m in res[nm]["oos"]["permode"]})
    for mo in modes:
        cells = []
        for nm in SCENARIOS:
            n, v = res[nm]["oos"]["permode"].get(mo, [0, 0.0])
            cells.append(f"{nm}={v:>+8,.0f}({n}t)")
        print(f"    {mo:4s} " + "  ".join(cells))


if __name__ == "__main__":
    main()
