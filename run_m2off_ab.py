"""
M2-disabled A/B on the CLEAN harness (post VP look-ahead fix + Imb fidelity fix).

Motivation: run_imbfix_ab.py showed M2 = -$4,340 over 14 trades pooled, larger
than the entire pooled loss (-$2,080). With the VP look-ahead bug present, M2
could physically never arm (price sat 277-408pt from the frozen POC), so the old
+$547/mo projection was partly banking a broken subsystem's suppression of a
losing mode. This tests removing M2 for real.

Arms (both IMB_MODEL=cpp_stateful, live panel MT=1/DL=800/M8 floor-60):
  m2_on   — DISABLE_MODES=set()   (identical to run_imbfix_ab.py "imb_fixed")
  m2_off  — DISABLE_MODES={1}     (1 == M2, per MODE_NAMES; backtest.py:2182)

NOT a pure subtraction: MAX_TRADES=1 means removing M2 frees the day's single
trade slot, so other modes can take days M2 used to consume. Expect the delta to
differ from +4,340. Per-mode counts are printed to expose that reallocation.

This is a chartbook-only lever (no cpp change) if it passes.
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
)
SCENARIOS = {
    "m2_on":  {**_PANEL, "DISABLE_MODES": set()},
    "m2_off": {**_PANEL, "DISABLE_MODES": {1}},   # 1 == M2
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
    out = BASE / f"IOF_NQ_m2off_{tag}_{name}.csv"
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
        return dict(n=0, wr=0.0, pf=0.0, tot=0.0, md=0.0, months={}, permode={})
    pn = [t["pnl"] for t in tr]
    w = [x for x in pn if x > 0]
    l = [x for x in pn if x < 0]
    pk = run = md = 0.0
    for x in pn:
        run += x
        pk = max(pk, run)
        md = min(md, run - pk)
    mm = defaultdict(float)
    for t in tr:
        mm[t["date"][:7]] += t["pnl"]
    permode = defaultdict(lambda: [0, 0.0])
    for t in tr:
        permode[t["mode"]][0] += 1
        permode[t["mode"]][1] += t["pnl"]
    return dict(n=len(tr), wr=100 * len(w) / len(tr),
                pf=(sum(w) / abs(sum(l)) if l else 9.99),
                tot=sum(pn), md=md, months=dict(mm), permode=dict(permode))


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}")
            continue
        res[tag] = {nm: summarize(run_scenario(nm, ov, scid, tag))
                    for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 96)
    print(" M2-DISABLED A/B — clean harness (VP-fixed + Imb-fixed), live panel")
    print("=" * 96)
    print(f"  {'contract':8s} {'arm':8s} {'n':>4s} {'WR':>5s} {'PF':>5s} "
          f"{'Net':>9s} {'MaxDD':>9s}   verdict")
    nb = nw = 0
    for tag, scn in res.items():
        for nm, r in scn.items():
            print(f"  {tag:8s} {nm:8s} {r['n']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
                  f"{r['tot']:>+9,.0f} {r['md']:>9,.0f}")
        d = scn["m2_off"]["tot"] - scn["m2_on"]["tot"]
        dd = scn["m2_off"]["md"] - scn["m2_on"]["md"]
        v = "off_better" if d > 0 else ("off_worse" if d < 0 else "tied")
        if d > 0: nb += 1
        elif d < 0: nw += 1
        print(f"  {'':8s} {'DELTA':8s} {'':>4} {'':>5} {'':>5} {d:>+9,.0f} "
              f"{dd:>+9,.0f}   {v}")
        print()

    print(f"  cross-contract gate: off_better={nb}  off_worse={nw}  "
          f"(ship only if never worse)")

    print("\n per-mode net by arm (pooled) — shows MAX_TRADES=1 reallocation:")
    modes = sorted({m for t in res for nm in SCENARIOS
                    for m in res[t][nm]["permode"]})
    print(f"  {'mode':6s} " + " ".join(f"{nm:>20s}" for nm in SCENARIOS))
    for mo in modes:
        cells = []
        for nm in SCENARIOS:
            n = sum(res[t][nm]["permode"].get(mo, [0, 0.0])[0] for t in res)
            v = sum(res[t][nm]["permode"].get(mo, [0, 0.0])[1] for t in res)
            cells.append(f"{v:>+11,.0f} ({n:>3}t)")
        print(f"  {mo:6s} " + " ".join(f"{c:>20s}" for c in cells))

    print("\n pooled:")
    for nm in SCENARIOS:
        tot = sum(res[t][nm]["tot"] for t in res)
        n = sum(res[t][nm]["n"] for t in res)
        print(f"  {nm:8s} total={tot:>+9,.0f}  trades={n:>3}  "
              f"worstMaxDD={min(res[t][nm]['md'] for t in res):>+9,.0f}")


if __name__ == "__main__":
    main()
