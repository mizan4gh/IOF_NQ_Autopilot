"""A/B: news filter ON (live config) vs OFF, on the current live panel config.

Baseline = LIVE config as of 2026-07-02 (MAX_TRADES=1, DAILY_LOSS=800, vol3000,
floor-60 M8, early-scratch, M1 pullback=2, ENTRY_ORD=2). Test arm flips only
NEWS_FILTER 1 -> 0, which removes the three time-window blackouts
(08:25-08:35, 09:55-10:05, 13:55-14:05) and nothing else — the spike/vcool
protection in backtest.py is decoupled from NEWS_FILTER and runs in both arms.

Frozen project-dir scids; per-contract bar cache (pattern from
backtest_trend_iter2_ab.py / backtest_mt3_ab.py); scid mtime checked
before/after per [[project_nqh6_data_rewrite_incident]].

Usage: python backtest_news_ab.py [NQZ25|NQM5|NQH6]   (default: all)
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
    C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(),
    QUAL_FLOOR_M8=60, M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False,
    TREND_LONG=False, TREND_FAIL_EXIT=False, TREND_MIN_DEPTH=0.0,
    ENTRY_ORD=2, MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    NEWS_OVERRIDE_M4_RECLAIM=False,
)
SCENARIOS = {
    "news_on":  {**_COMMON, "NEWS_FILTER": 1},
    "news_off": {**_COMMON, "NEWS_FILTER": 0},
}
BASELINE = "news_on"; TEST_ARMS = ["news_off"]

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
    out = BASE / f"IOF_NQ_news_{tag}_{name}.csv"
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
    print(" NEWS FILTER A/B (live mt1_dl800 config, frozen scids)")
    print("=" * 92)
    print(f"  {'contract':8s} {'scenario':10s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'WorstDay':>9s} {'Sort/d':>8s}")
    for tag, scn in res.items():
        for nm, r in scn.items():
            print(f"  {tag:8s} {nm:10s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['worst_day']:>9,.0f} "
                  f"{r['s_day']:>8.3f}")
        print()

    print(" VERDICT vs news_on baseline:")
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
