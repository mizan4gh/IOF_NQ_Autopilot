"""
ORH/ORL as M4 sweep levels — 6-contract A/B on the clean harness.

Hypothesis (from reference_level_taxonomy_hypothesis): every level-based trigger
tested here used COMPUTED levels (POC/VAH/VAL via M2, session VWAP via the
VWAP-touch engine in 3 flavors) and none produced edge; M4's rolling swing
extremes are STRUCTURAL levels — real resting stops — and M4 is the shape that
wins. The opening-range high/low is structural in the same sense and had never
been tested (zero opening-range code existed in the repo before this change).

What is being tested is ONLY the level source. M4's geometry is untouched:
sweep past the level by >1 tick, close back inside, same bull/bear + sc>=m4_min
+ ctrl + VWAP-edge gates, same ATR x1.2 stop. Explicitly NOT tested (all already
falsified here): retest/pullback entry (M4 delayed-entry A/B), scale-out at
1R/2R (project_scaleout_2lot_ab), wider structural stops (M4 stop-overshoot),
fixed brackets (MNQ fixed-bracket).

Arms — all at the live panel (MT=1/DL=800/M8 floor-60/news on/3k vol,
IMB_MODEL=cpp_stateful):
  base        M2 on,  no OR levels            <- current LIVE config
  or_add      M2 on,  OR levels + swing       <- does adding the source help?
  or_only     M2 on,  OR levels replace swing <- does the source have edge alone?
  m2off       M2 off, no OR levels            <- the recommended config
  m2off_or    M2 off, OR levels + swing       <- does it help the recommended one?

Under MAX_TRADES=1 an added trigger competes for the day's single slot, so
or_add is NOT a pure addition — it can displace M6/M4 winners. Trades armed from
an OR level alone are labelled "M4o" in the per-mode table so the source's own
record is visible separately from the displacement effect.

Guard (project_nqh6_data_rewrite_incident): scid mtimes are hashed before and
after the whole run; a mid-run Sierra rewrite invalidates the comparison.
"""
import csv, sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQU25": BASE / "F.US.ENQU25.scid",
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
    "NQM6":  BASE / "F.US.ENQM26.scid",
    "NQU26": BASE / "F.US.ENQU26.scid",
}
_PANEL = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    M8_FADE_FULL=True, V13_MODEL=False, QUAL_FLOOR_M8=60,
    IMB_MODEL="cpp_stateful",
    M4_OR_START=930, M4_OR_END=1000,
)
_OFF = dict(M4_OR_LEVELS=False, M4_OR_MODE="add")
_ADD = dict(M4_OR_LEVELS=True,  M4_OR_MODE="add")
_ONLY = dict(M4_OR_LEVELS=True, M4_OR_MODE="only")

SCENARIOS = {
    "base":     {**_PANEL, **_OFF,  "DISABLE_MODES": set()},
    "or_add":   {**_PANEL, **_ADD,  "DISABLE_MODES": set()},
    "or_only":  {**_PANEL, **_ONLY, "DISABLE_MODES": set()},
    "m2off":    {**_PANEL, **_OFF,  "DISABLE_MODES": {1}},
    "m2off_or": {**_PANEL, **_ADD,  "DISABLE_MODES": {1}},
}
# (test arm, baseline arm) pairs the verdict table compares
PAIRS = [("or_add", "base"), ("or_only", "base"), ("m2off_or", "m2off")]

_BAR_CACHE = {}


def scid_fingerprint():
    return {t: (p.stat().st_mtime_ns, p.stat().st_size)
            for t, p in CONTRACTS.items() if p.exists()}


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
    out = BASE / f"IOF_NQ_m4or_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {name} =====")
    backtest.main()
    return out


def summarize(p):
    tr, prev = [], 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] != "EXIT":
            continue
        t = float(r["TotalPnL"])
        tr.append(dict(pnl=t - prev, date=r["Date"], mode=r["Mode"]))
        prev = t
    if not tr:
        return dict(n=0, wr=0.0, pf=0.0, tot=0.0, md=0.0, permode={}, pnls=[])
    pn = [t["pnl"] for t in tr]
    w = [x for x in pn if x > 0]
    l = [x for x in pn if x < 0]
    pk = run = md = 0.0
    for x in pn:
        run += x
        pk = max(pk, run)
        md = min(md, run - pk)
    permode = defaultdict(lambda: [0, 0.0])
    for t in tr:
        permode[t["mode"]][0] += 1
        permode[t["mode"]][1] += t["pnl"]
    return dict(n=len(tr), wr=100 * len(w) / len(tr),
                pf=(sum(w) / abs(sum(l)) if l else 9.99),
                tot=sum(pn), md=md, permode=dict(permode), pnls=pn)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    fp_before = scid_fingerprint()
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}")
            continue
        res[tag] = {nm: summarize(run_scenario(nm, ov, scid, tag))
                    for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 100)
    print(" ORH/ORL AS M4 SWEEP LEVELS — 6-contract A/B, live panel")
    print("=" * 100)
    for tag, scn in res.items():
        print(f"\n  {tag}")
        print(f"    {'arm':10s} {'n':>4s} {'WR':>5s} {'PF':>5s} {'Net':>9s} "
              f"{'MaxDD':>9s} {'M4o':>16s}")
        for nm, r in scn.items():
            n_or, p_or = r["permode"].get("M4o", [0, 0.0])
            print(f"    {nm:10s} {r['n']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
                  f"{r['tot']:>+9,.0f} {r['md']:>9,.0f} "
                  f"{p_or:>+10,.0f} ({n_or:>2}t)")
        for test, ctl in PAIRS:
            d = scn[test]["tot"] - scn[ctl]["tot"]
            dd = scn[test]["md"] - scn[ctl]["md"]
            v = "better" if d > 0 else ("worse" if d < 0 else "tied")
            print(f"    {'D ' + test + '-' + ctl:24s} P&L {d:>+9,.0f}   "
                  f"MaxDD {dd:>+9,.0f}   {v}")

    print("\n" + "=" * 100)
    print(" CROSS-CONTRACT GATE (ship only if never worse on P&L and MaxDD)")
    print("=" * 100)
    for test, ctl in PAIRS:
        nb = nw = nt = 0
        ddw = 0
        for tag, scn in res.items():
            d = scn[test]["tot"] - scn[ctl]["tot"]
            if d > 0: nb += 1
            elif d < 0: nw += 1
            else: nt += 1
            if scn[test]["md"] < scn[ctl]["md"]: ddw += 1
        pool_t = sum(scn[test]["tot"] for scn in res.values())
        pool_c = sum(scn[ctl]["tot"] for scn in res.values())
        print(f"  {test:10s} vs {ctl:10s}  better={nb} worse={nw} tied={nt}  "
              f"MaxDD_worse={ddw}/{len(res)}  pooled {pool_c:>+9,.0f} -> "
              f"{pool_t:>+9,.0f}  (delta {pool_t - pool_c:>+9,.0f})")

    print("\n M4o (OR-sourced) pooled record — the source's own edge:")
    for nm in SCENARIOS:
        n = sum(res[t][nm]["permode"].get("M4o", [0, 0.0])[0] for t in res)
        v = sum(res[t][nm]["permode"].get("M4o", [0, 0.0])[1] for t in res)
        if n:
            print(f"  {nm:10s} {n:>3}t  {v:>+9,.0f}  ({v / n:>+8,.0f}/trade)")
        else:
            print(f"  {nm:10s}   0t  (no OR-sourced arms)")

    print("\n per-mode pooled net by arm:")
    modes = sorted({m for t in res for nm in SCENARIOS
                    for m in res[t][nm]["permode"]})
    print(f"  {'mode':6s} " + " ".join(f"{nm:>19s}" for nm in SCENARIOS))
    for mo in modes:
        cells = []
        for nm in SCENARIOS:
            n = sum(res[t][nm]["permode"].get(mo, [0, 0.0])[0] for t in res)
            v = sum(res[t][nm]["permode"].get(mo, [0, 0.0])[1] for t in res)
            cells.append(f"{v:>+10,.0f} ({n:>3}t)")
        print(f"  {mo:6s} " + " ".join(f"{c:>19s}" for c in cells))

    print("\n pooled:")
    for nm in SCENARIOS:
        tot = sum(res[t][nm]["tot"] for t in res)
        n = sum(res[t][nm]["n"] for t in res)
        print(f"  {nm:10s} total={tot:>+9,.0f}  trades={n:>3}  "
              f"worstMaxDD={min(res[t][nm]['md'] for t in res):>+9,.0f}")

    fp_after = scid_fingerprint()
    print("\n scid integrity: " +
          ("OK (unchanged during run)" if fp_after == fp_before
           else "*** CHANGED MID-RUN — RESULTS INVALID ***"))
    if fp_after != fp_before:
        for t in fp_before:
            if fp_before[t] != fp_after.get(t):
                print(f"   {t}: {fp_before[t]} -> {fp_after.get(t)}")


if __name__ == "__main__":
    main()
