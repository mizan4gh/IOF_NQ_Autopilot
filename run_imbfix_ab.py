"""
IMB_MODEL A/B — legacy_stateless (pre-fix) vs cpp_stateful (fidelity fix).

Both arms use the LIVE panel (MT=1, DL=800, M8 floor-60, news on, 3k vol,
early-scratch) — identical to run_v13_projection.py's "v12_live" arm. The ONLY
difference is backtest.IMB_MODEL.

Purpose: re-derive the monthly profit / MaxDD projection on a harness whose
imbalance subsystem actually matches the live cpp (struct cpp:911, update
cpp:2868-2899). The legacy stateless port used score>=2 / 0.55-0.65 bands and
had no strength-decay persistence.

Scope note: imb feeds M6's breakout confirm as well as M7/M8, so this is NOT an
M8-only lever — expect M6 to move too. Per-mode deltas are printed for that.

Also counts degenerate M8 geometry (ExitReason==T2 booked at a LOSS), the
documented tell for the wrong-side-target artifact.
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
    QUAL_FLOOR=50, DISABLE_MODES=set(), M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    M8_FADE_FULL=True, V13_MODEL=False, QUAL_FLOOR_M8=60,
)
SCENARIOS = {
    "imb_legacy": {**_PANEL, "IMB_MODEL": "legacy_stateless"},
    "imb_fixed":  {**_PANEL, "IMB_MODEL": "cpp_stateful"},
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
    out = BASE / f"IOF_NQ_imbab_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {name} =====")
    backtest.main()
    return out


def month_iter(m0, m1):
    y, m = int(m0[:4]), int(m0[5:7])
    while True:
        cur = f"{y:04d}-{m:02d}"
        yield cur
        if cur == m1:
            return
        m += 1
        if m == 13:
            y, m = y + 1, 1


def summarize(p):
    tr, prev = [], 0.0
    deg = 0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] != "EXIT":
            continue
        t = float(r["TotalPnL"])
        pnl = t - prev
        prev = t
        tr.append(dict(pnl=pnl, date=r["Date"], mode=r["Mode"]))
        if r.get("ExitReason") == "T2" and pnl < 0:
            deg += 1
    if not tr:
        return dict(n=0, wr=0.0, pf=0.0, tot=0.0, md=0.0, months={},
                    permode={}, deg=0)
    pn = [t["pnl"] for t in tr]
    w = [x for x in pn if x > 0]
    l = [x for x in pn if x < 0]
    tot = sum(pn)
    pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run = md = 0.0
    for x in pn:
        run += x
        pk = max(pk, run)
        md = min(md, run - pk)
    mm = defaultdict(float)
    for t in tr:
        mm[t["date"][:7]] += t["pnl"]
    ms = sorted(mm)
    months = {m: mm.get(m, 0.0) for m in month_iter(ms[0], ms[-1])}
    permode = defaultdict(lambda: [0, 0.0])
    for t in tr:
        permode[t["mode"]][0] += 1
        permode[t["mode"]][1] += t["pnl"]
    return dict(n=len(tr), wr=100 * len(w) / len(tr), pf=pf, tot=tot, md=md,
                months=months, permode=dict(permode), deg=deg)


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

    print("\n" + "=" * 104)
    print(" IMB_MODEL A/B — live panel (MT=1 DL=800 M8f60), 6 frozen contracts")
    print("=" * 104)
    print(f"  {'contract':8s} {'arm':11s} {'n':>4s} {'WR':>5s} {'PF':>5s} "
          f"{'Net':>9s} {'MaxDD':>9s} {'mo':>3s} {'avg/mo':>8s} "
          f"{'worst-mo':>9s} {'best-mo':>8s} {'degT2':>6s}")
    for tag, scn in res.items():
        for nm, r in scn.items():
            mv = list(r["months"].values())
            nmo = len(mv)
            avg = sum(mv) / nmo if nmo else 0.0
            print(f"  {tag:8s} {nm:11s} {r['n']:>4} {r['wr']:>5.1f} "
                  f"{r['pf']:>5.2f} {r['tot']:>+9,.0f} {r['md']:>9,.0f} "
                  f"{nmo:>3} {avg:>+8,.0f} {min(mv) if mv else 0:>+9,.0f} "
                  f"{max(mv) if mv else 0:>+8,.0f} {r['deg']:>6}")
        d = scn["imb_fixed"]["tot"] - scn["imb_legacy"]["tot"]
        print(f"  {'':8s} {'DELTA':11s} {'':>4} {'':>5} {'':>5} {d:>+9,.0f}")
        print()

    print(" per-mode net by arm (pooled over contracts):")
    modes = sorted({m for tag in res for nm in SCENARIOS
                    for m in res[tag][nm]["permode"]})
    print(f"  {'mode':6s} " + " ".join(f"{nm:>22s}" for nm in SCENARIOS))
    for mo in modes:
        cells = []
        for nm in SCENARIOS:
            n = sum(res[t][nm]["permode"].get(mo, [0, 0.0])[0] for t in res)
            v = sum(res[t][nm]["permode"].get(mo, [0, 0.0])[1] for t in res)
            cells.append(f"{v:>+13,.0f} ({n:>3}t)")
        print(f"  {mo:6s} " + " ".join(f"{c:>22s}" for c in cells))

    print("\n pooled monthly distribution per arm (all contract-months):")
    for nm in SCENARIOS:
        allm = []
        for tag in res:
            allm += list(res[tag][nm]["months"].values())
        if not allm:
            continue
        allm.sort()
        k = len(allm)
        med = allm[k // 2] if k % 2 else (allm[k // 2 - 1] + allm[k // 2]) / 2
        mean = sum(allm) / k
        neg = sum(1 for v in allm if v < 0)
        wmd = min(res[t][nm]["md"] for t in res)
        tot = sum(res[t][nm]["tot"] for t in res)
        dg = sum(res[t][nm]["deg"] for t in res)
        print(f"  {nm:11s} months={k:>3} mean={mean:>+8,.0f} med={med:>+8,.0f} "
              f"p10={allm[max(0, k // 10)]:>+8,.0f} worst={allm[0]:>+9,.0f} "
              f"best={allm[-1]:>+8,.0f} neg-mo={100 * neg / k:>4.0f}%  "
              f"worstMaxDD={wmd:>9,.0f}  total={tot:>+9,.0f}  degT2={dg}")


if __name__ == "__main__":
    main()
