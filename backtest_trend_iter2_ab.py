"""Trend-long iteration 2: mechanistic bull-trap defenses. 3-contract gate.

Round 1 (2ca2f0f: M1-priority, ts-filter, cap-stop) left two flaws standing:
NQH6 bull-trap reclaims and DD from wide swing-low stops. Round 2 attacks the
TRAP LOSS ITSELF instead of filtering harder:

  TFAIL exit  (TREND_FAIL_EXIT)  pre-T1 close back below VWAP-0.10*ATR kills
              the trade at market. The thesis IS the reclaim; when it fails,
              don't ride to the wide swing-low stop.
  V-depth gate (TREND_MIN_DEPTH=0.5, LB=20)  entry requires a close >=0.5*ATR
              below VWAP within the last 20 bars — a real V-reversal, not a
              chop crossing of VWAP.

Data: FROZEN project-dir snapshots (F.US.E*, copied 2026-06-29) — the old
backtest_trend_long_ab.py read C:\\SierraChart\\Data which Sierra rewrites
mid-run ([[project_nqh6_data_rewrite_incident]]).

  baseline   fade-only prod (M8_FADE_FULL ON, early-scratch ON, floor-60 M8)
  trend      + TREND_LONG (post-iter-1 state: ts-filter, M1-priority)
  tfail      trend + TREND_FAIL_EXIT
  depth      trend + TREND_MIN_DEPTH=0.5
  both       trend + TFAIL + depth

Gate [[feedback_cross_contract_ab]]: ship consideration only if an arm is
test_BETTER vs baseline on ALL THREE (NQZ25/NQM5/NQH6).

Usage: python backtest_trend_iter2_ab.py [NQZ25|NQM5|NQH6]   (default: all)
"""
import csv, math, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQZ25": BASE / "F.US.ENQZ25.scid",   # Dec-2025 (frozen snapshot)
    "NQM5":  BASE / "F.US.ENQM25.scid",   # Jun-2025 (frozen snapshot)
    "NQH6":  BASE / "F.US.ENQH26.scid",   # Mar-2026 (frozen snapshot)
}
_PROD = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(),
    QUAL_FLOOR_M8=60,   # v12.38 live floor
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False,
    TREND_FAIL_EXIT=False, TREND_MIN_DEPTH=0.0,
)
SCENARIOS = {
    "baseline": {**_PROD, "TREND_LONG": False},
    # identical to baseline but runs on CACHED bars — byte-compare of the two
    # CSVs proves cache fidelity per contract (excluded from verdicts)
    "basecache": {**_PROD, "TREND_LONG": False},
    "trend":    {**_PROD, "TREND_LONG": True},
    "tfail":    {**_PROD, "TREND_LONG": True, "TREND_FAIL_EXIT": True},
    "depth":    {**_PROD, "TREND_LONG": True, "TREND_MIN_DEPTH": 0.5},
    "both":     {**_PROD, "TREND_LONG": True, "TREND_FAIL_EXIT": True,
                 "TREND_MIN_DEPTH": 0.5},
}
BASELINE = "baseline"; TEST_ARMS = ["trend", "tfail", "depth", "both"]


# Per-contract volume-bar cache. Bars are identical across scenarios (same
# TARGET_VOL, same frozen scid) and the engine never mutates Bar objects
# (Backtester only reads bar fields; Balance/Imbalance state are separate
# dataclasses). Building 34M-tick bars takes ~10 min per pass — cache them
# once per contract and stub read_scid/build_volume_bars on later scenarios.
# Keyed on (path, TARGET_VOL) so a future bar-size arm can't poison the cache.
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
        backtest.read_scid = lambda path: []          # len()-able, skips 1.4GB read
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
    out = BASE / f"IOF_NQ_tri2_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {name} =====")
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
                    s_day=float("nan"),
                    trl=dict(n=0, net=0.0, wr=0.0, rsn={}))
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
    trl = [t for t in tr if t["mode"] == "M5"]   # trend-long fires as M5
    trw = [t for t in trl if t["pnl"] > 0]
    rsn = defaultdict(lambda: [0, 0.0])
    for t in trl:
        rsn[t["reason"]][0] += 1; rsn[t["reason"]][1] += t["pnl"]
    trld = dict(n=len(trl), net=sum(t["pnl"] for t in trl),
                wr=100*len(trw)/len(trl) if trl else 0.0,
                rsn={k: (v[0], v[1]) for k, v in sorted(rsn.items())})
    return dict(n=n, total=total, pf=pf, wr=100*len(w)/n, max_dd=md,
                s_day=sd, trl=trld)


def verdict(b, t):
    dp = t["total"] - b["total"]; dd = t["s_day"] - b["s_day"]
    if abs(dp) < 1.0 and abs(dd) < 1e-9: return "no-op", dp, dd
    if dp > 0 and dd >= 0: return "test_BETTER", dp, dd
    if dp < 0 or dd < 0: return "test_worse", dp, dd
    return "tied", dp, dd


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    # frozen-snapshot integrity: record mtimes up front, re-check at the end
    mt0 = {t: CONTRACTS[t].stat().st_mtime for t in tags if CONTRACTS[t].exists()}
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}"); continue
        res[tag] = {nm: summarize(run_scenario(nm, ov, scid, tag))
                    for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 100)
    print(" TREND-LONG ITER-2 A/B (prod config, M8 floor-60, frozen scids)")
    print("=" * 100)
    print(f"  {'contract':8s} {'scenario':9s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sort/d':>8s}  {'TRLn':>5s} {'TRL_WR':>7s} {'TRLnet':>9s}  TRL exits")
    for tag, scn in res.items():
        for nm, r in scn.items():
            rs = " ".join(f"{k}:{v[0]}({v[1]:+,.0f})" for k, v in r["trl"]["rsn"].items())
            print(f"  {tag:8s} {nm:9s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>8.3f}  "
                  f"{r['trl']['n']:>5} {r['trl']['wr']:>6.1f}% {r['trl']['net']:>+9,.0f}  {rs}")
        print()

    # cache fidelity: baseline (fresh bars) vs basecache (cached bars) must match
    for tag in res:
        a = (BASE / f"IOF_NQ_tri2_{tag}_baseline.csv").read_bytes()
        b = (BASE / f"IOF_NQ_tri2_{tag}_basecache.csv").read_bytes()
        print(f" cache-fidelity {tag}: {'IDENTICAL' if a == b else '!! MISMATCH — cache unsound, ignore cached arms'}")
    print()

    print(" VERDICT (gate = test_BETTER on all three):")
    for arm in TEST_ARMS:
        vs = []
        for tag, scn in res.items():
            v, dp, dd = verdict(scn[BASELINE], scn[arm]); vs.append(v)
            print(f"   {arm:6s} {tag:8s} {v:12s} dPnL={dp:>+9,.0f}  dSortino={dd:>+.3f}  "
                  f"(TRL: {scn[arm]['trl']['n']}, net {scn[arm]['trl']['net']:+,.0f})")
        ok = all(v == "test_BETTER" for v in vs)
        print(f"   => {arm}: {'SHIP-CANDIDATE' if ok else 'NO-SHIP'} ({', '.join(sorted(set(vs)))})\n")

    for t, m0 in mt0.items():
        if CONTRACTS[t].stat().st_mtime != m0:
            print(f" !! WARNING: {CONTRACTS[t].name} mtime CHANGED during run — results suspect")


if __name__ == "__main__":
    main()
