"""A/B: the 2026-07-02 live panel config (MAX_TRADES=3, DAILY_LOSS=800) vs the
old prod baseline (MT=6) and the validated 1pd/no-caps flip (MT=1, caps off).

Context: 2026-07-02 live session lost $1,030 with caps OFF and multi-trade on
(partial 1pd flip — unvalidated hybrid). User's proposed fix per RERUN.png:
restore DL=800, set MT=3. This run places that config between the two
validated reference points on the 3 frozen contracts.

  mt6_dl800   old prod baseline (MAX_TRADES=6, DAILY_LOSS=800, DAILY_PROF=0)
  mt3_dl800   the RERUN.png panel (MAX_TRADES=3, DAILY_LOSS=800)
  mt1_nocap   validated 1pd flip (MAX_TRADES=1, caps off)  [2026-06-06 gate pass]
  mt1_dl800   1pd with loss cap left ON — proven byte-identical to mt1_nocap on
              all 3 frozen contracts (2026-07-02): with MT=1, every DAILY_LOSS
              touchpoint (force-close, entry gate, in_recovery threshold) only
              acts after the day's first closed trade, which MT=1 already blocks.
              This is the LIVE panel config chosen 2026-07-02: the cap is free
              insurance against another partial-flip (caps-off) misconfiguration.

All arms: vol3000, news ON, cool=36, single lot, ENTRY_ORD=2 (panel In:9),
M8_FADE_FULL + floor-60 (v12.38 live), early-scratch ON (v12.37), M1 pullback=2.
Frozen project-dir scids; per-contract bar cache (fidelity proven 2026-07-02,
basecache byte-identical all 3 in backtest_trend_iter2_ab.py).

Usage: python backtest_mt3_ab.py [NQZ25|NQM5|NQH6]   (default: all)
"""
import csv, math, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
}
_COMMON = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(),
    QUAL_FLOOR_M8=60, M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False,
    TREND_LONG=False, TREND_FAIL_EXIT=False, TREND_MIN_DEPTH=0.0,
    ENTRY_ORD=2,
)
SCENARIOS = {
    "mt6_dl800": {**_COMMON, "MAX_TRADES": 6, "DAILY_LOSS": 800.0, "DAILY_PROF": 0.0},
    "mt3_dl800": {**_COMMON, "MAX_TRADES": 3, "DAILY_LOSS": 800.0, "DAILY_PROF": 0.0},
    "mt1_nocap": {**_COMMON, "MAX_TRADES": 1, "DAILY_LOSS": 0.0,   "DAILY_PROF": 0.0},
    "mt1_dl800": {**_COMMON, "MAX_TRADES": 1, "DAILY_LOSS": 800.0, "DAILY_PROF": 0.0},
}
BASELINE = "mt6_dl800"; TEST_ARMS = ["mt3_dl800", "mt1_nocap", "mt1_dl800"]

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
    out = BASE / f"IOF_NQ_mt3_{tag}_{name}.csv"
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
            tr.append(dict(pnl=t - prev, date=r["Date"])); prev = t
    n = len(tr)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0,
                    s_day=float("nan"), worst_day=0.0)
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
    return dict(n=n, total=total, pf=pf, wr=100*len(w)/n, max_dd=md,
                s_day=sd, worst_day=min(dl.values()))


def verdict(b, t):
    dp = t["total"] - b["total"]; dd = t["s_day"] - b["s_day"]
    if abs(dp) < 1.0 and abs(dd) < 1e-9: return "no-op", dp, dd
    if dp > 0 and dd >= 0: return "test_BETTER", dp, dd
    if dp < 0 or dd < 0: return "test_worse", dp, dd
    return "tied", dp, dd


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    mt0 = {t: CONTRACTS[t].stat().st_mtime for t in tags if CONTRACTS[t].exists()}
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}"); continue
        res[tag] = {nm: summarize(run_scenario(nm, ov, scid, tag))
                    for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 92)
    print(" MAX_TRADES/DAILY_LOSS CONFIG A/B (RERUN.png panel vs references, frozen scids)")
    print("=" * 92)
    print(f"  {'contract':8s} {'scenario':10s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'WorstDay':>9s} {'Sort/d':>8s}")
    for tag, scn in res.items():
        for nm, r in scn.items():
            print(f"  {tag:8s} {nm:10s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['worst_day']:>9,.0f} "
                  f"{r['s_day']:>8.3f}")
        print()

    print(" VERDICT vs mt6_dl800 baseline:")
    for arm in TEST_ARMS:
        vs = []
        for tag, scn in res.items():
            v, dp, dd = verdict(scn[BASELINE], scn[arm]); vs.append(v)
            print(f"   {arm:10s} {tag:8s} {v:12s} dPnL={dp:>+9,.0f}  dSortino={dd:>+.3f}")
        ok = all(v in ("test_BETTER", "no-op") for v in vs)
        print(f"   => {arm}: {'OK (never worse)' if ok else 'WORSE somewhere'} "
              f"({', '.join(sorted(set(vs)))})\n")

    for t, m0 in mt0.items():
        if CONTRACTS[t].stat().st_mtime != m0:
            print(f" !! WARNING: {CONTRACTS[t].name} mtime CHANGED during run — results suspect")


if __name__ == "__main__":
    main()
