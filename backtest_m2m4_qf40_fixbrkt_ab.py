"""
M2+M4 / floor-40 / MT=3 / fixed $500-$500 bracket — 6-contract A/B.

Validates IOF_NQ_Autopilot_M2M4_FixBrkt.cpp (2026-07-30 request) against the
shipped v12.38 live config. The variant changes FOUR things at once, so this
runs a LADDER: each arm adds exactly one knob to the previous one, which is the
only way to attribute a pooled delta to a cause rather than to the bundle.

  live        v12.38 as shipped         all modes, QF50/M8-60, MT=1, DL=800,
                                        ATR bracket + trail + early-scratch
  m2m4        + M2/M4 only              DISABLE_MODES={0,2,4,5,6,7}
  m2m4_qf40   + quality floor 40        QUAL_FLOOR=40
  m2m4_mt3    + 3 trades/day            MAX_TRADES=3, DAILY_LOSS=1600
  variant     + fixed $500/$500         FIXED_BRACKET, 100t stop / 100t target
                                        (= 25.00 NQ pts = $500 @ $20/pt),
                                        no trail / BE / scratch / scale-out
                                        <- this is the .cpp exactly

DAILY_LOSS moves with MAX_TRADES on purpose and is not a free parameter: the
engine (and the cpp) refuse new entries once sess_dd > 0.8*DAILY_LOSS, so at
DL=800 the 2nd $500 stop ends the day and MT=3 could never bind. $1,600 = 3
stops + headroom, matching the .cpp default.

LATE_ENTRY_GATE=1500 is set on EVERY arm. backtest.py defaults it to 1555 (off)
but both .cpp builds ship the v12.36 15:00 gate, so leaving it off would make
both arms unfaithful. Set in all arms => the comparison is unaffected.

All arms are 1 lot (SCALE_OUT=False -> 1; FIXED_QTY=1), so P&L is directly
comparable across the ladder. Multiply by 3 for 3-lot dollars.

Prior art this is expected to reproduce (see MEMORY.md) — the point of running
it is to find out whether the COMBINATION escapes any of these:
  - QUAL_FLOOR<50 falsified 4x cross-contract (qf33, qf40, per-mode, M4@40)
  - M2 standalone significantly negative (f50 t=-2.82), gradient INVERTED
  - M2+M4 fixed-bracket on MNQ: -$4,666, t=-3.41
  - M2-disable VALIDATED (+$5,377 pooled) — i.e. the evidence says keep M4 and
    drop M2, which is close to the opposite of this variant's mode set.

Verdict rule (feedback_cross_contract_ab + reference_ab_noise_floor): ship only
if the variant is better on P&L on a majority of contracts AND never worse on
MaxDD AND the pooled per-trade t-stat clears ~2. A pooled delta under about
$8k/contract is inside the noise floor and means nothing on its own.

Guard (project_nqh6_data_rewrite_incident): scid mtimes+sizes are fingerprinted
before and after the run; a mid-run Sierra rewrite invalidates everything.
"""
import csv, statistics, sys
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

# Shared across every arm — the live panel.
_PANEL = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    DAILY_PROF=0.0,
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    M8_FADE_FULL=True, M8_FADE_TYPES={1, 2, 3, 4}, V13_MODEL=False,
    IMB_MODEL="cpp_stateful", ENTRY_ORD=2,
    PT_VAL=20.0, COMMISSION=5.0,
    LATE_ENTRY_GATE=1500,          # v12.36, shipped in both .cpp builds
    FIXED_BRACKET=False,           # arms below flip this on
)
# v12.38 exit machinery (ATR stop + T1/BE/trail + early-scratch)
_ATR_EXITS = dict(EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25)
# M2(1) + M4(3) only
_M2M4 = {0, 2, 4, 5, 6, 7}

# $500 @ NQ $20/pt = 25.00 pts = 100 ticks. Same number both sides => 1:1 R:R.
_FIXBRKT = dict(
    FIXED_BRACKET=True, FIXED_STOP_TICKS=100, FIXED_TARGET_TICKS=100,
    FIXED_QTY=1, FIXED_PROFIT_LOCK=0.0, FIXED_LOSS_LOCK=0.0,
    EARLY_SCRATCH=False,
)

SCENARIOS = {
    "live":      {**_PANEL, **_ATR_EXITS, "DISABLE_MODES": set(),
                  "QUAL_FLOOR": 50, "QUAL_FLOOR_M8": 60,
                  "MAX_TRADES": 1, "DAILY_LOSS": 800.0},
    "m2m4":      {**_PANEL, **_ATR_EXITS, "DISABLE_MODES": _M2M4,
                  "QUAL_FLOOR": 50, "QUAL_FLOOR_M8": 60,
                  "MAX_TRADES": 1, "DAILY_LOSS": 800.0},
    "m2m4_qf40": {**_PANEL, **_ATR_EXITS, "DISABLE_MODES": _M2M4,
                  "QUAL_FLOOR": 40, "QUAL_FLOOR_M8": 60,
                  "MAX_TRADES": 1, "DAILY_LOSS": 800.0},
    "m2m4_mt3":  {**_PANEL, **_ATR_EXITS, "DISABLE_MODES": _M2M4,
                  "QUAL_FLOOR": 40, "QUAL_FLOOR_M8": 60,
                  "MAX_TRADES": 3, "DAILY_LOSS": 1600.0},
    "variant":   {**_PANEL, **_FIXBRKT, "DISABLE_MODES": _M2M4,
                  "QUAL_FLOOR": 40, "QUAL_FLOOR_M8": 60,
                  "MAX_TRADES": 3, "DAILY_LOSS": 1600.0},
    # [2026-07-30] IOF_NQ_Autopilot_FixBrkt500.cpp — production v12.38 with the
    # fixed $500/$500 bracket as the ONLY change. Not part of the ladder above:
    # it hangs directly off `live`, which is what makes it a single-variable
    # test of the bracket. The ladder's own bracket step measures the bracket
    # on top of M2M4/QF40/MT3, which is a different question.
    "fb500":     {**_PANEL, **_FIXBRKT, "DISABLE_MODES": set(),
                  "QUAL_FLOOR": 50, "QUAL_FLOOR_M8": 60,
                  "MAX_TRADES": 1, "DAILY_LOSS": 800.0},
    # [2026-07-30] M2-disable x fixed-bracket. project_m2_disable_validated is
    # the best-evidenced unshipped change on the board (DISABLE_MODES={1},
    # pooled -2,080 -> +5,377, LOO-robust on all 6); fb500 showed the bracket
    # buys MaxDD but costs P&L on 4/6 of production. Question here: does the
    # bracket behave differently on top of a config that is actually healthy?
    # m2off is re-run inside THIS harness (not quoted from the old study) so
    # both arms share the panel, LATE_ENTRY_GATE and IMB_MODEL — otherwise the
    # m2off_fb-vs-m2off delta would confound the bracket with panel drift.
    "m2off":     {**_PANEL, **_ATR_EXITS, "DISABLE_MODES": {1},
                  "QUAL_FLOOR": 50, "QUAL_FLOOR_M8": 60,
                  "MAX_TRADES": 1, "DAILY_LOSS": 800.0},
    "m2off_fb":  {**_PANEL, **_FIXBRKT, "DISABLE_MODES": {1},
                  "QUAL_FLOOR": 50, "QUAL_FLOOR_M8": 60,
                  "MAX_TRADES": 1, "DAILY_LOSS": 800.0},
}
# (test, baseline) pairs. Each ladder step is compared to the step before it,
# so a pooled delta is attributable to the ONE knob that changed; plus the
# whole-bundle comparison that actually decides ship / no-ship.
PAIRS = [
    ("m2m4",      "live"),        # mode set
    ("m2m4_qf40", "m2m4"),        # quality floor
    ("m2m4_mt3",  "m2m4_qf40"),   # trades/day + daily loss
    ("variant",   "m2m4_mt3"),    # fixed bracket, on top of the stack
    ("variant",   "live"),        # the 4-knob bundle — ship / no-ship
    ("fb500",     "live"),        # the bracket ALONE — ship / no-ship
    ("m2off",     "live"),        # reproduce the validated M2-disable here
    ("m2off_fb",  "m2off"),       # does the bracket add on a HEALTHY config?
    ("m2off_fb",  "live"),        # M2-off + bracket combined — ship / no-ship
]

# Bars for the contract currently being processed, shared by its 5 arms. MUST be
# evicted when moving to the next contract: bars for one 1.2GB scid are multiple
# GB, and holding several contracts' worth while the next scid's ~31M records
# load will OOM a 16GB box (killed silently mid-run on 2026-07-30, no traceback).
_BAR_CACHE = {}


def scid_fingerprint():
    return {t: (p.stat().st_mtime_ns, p.stat().st_size)
            for t, p in CONTRACTS.items() if p.exists()}


def run_scenario(name, ov, scid, tag, force=False):
    # Resume: a completed arm's CSV is its result. Adding a NEW arm then costs
    # only that arm per contract instead of re-running the whole panel.
    out = BASE / f"IOF_NQ_m2m4fb_{tag}_{name}.csv"
    if out.exists() and out.stat().st_size > 0 and not force:
        print(f"  [skip] {tag}::{name} already run -> {out.name}")
        return out
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
        tr.append(dict(pnl=t - prev, date=r["Date"], mode=r["Mode"],
                       rsn=r.get("ExitReason", "")))
        prev = t
    if not tr:
        return dict(n=0, wr=0.0, pf=0.0, tot=0.0, md=0.0, t=0.0,
                    permode={}, byrsn={}, pnls=[], days=0)
    pn = [t["pnl"] for t in tr]
    w = [x for x in pn if x > 0]
    l = [x for x in pn if x < 0]
    pk = run = md = 0.0
    for x in pn:
        run += x
        pk = max(pk, run)
        md = min(md, run - pk)
    permode = defaultdict(lambda: [0, 0.0])
    byrsn = defaultdict(lambda: [0, 0.0])
    for t in tr:
        permode[t["mode"]][0] += 1; permode[t["mode"]][1] += t["pnl"]
        byrsn[t["rsn"]][0] += 1;    byrsn[t["rsn"]][1] += t["pnl"]
    se = statistics.pstdev(pn) / (len(pn) ** 0.5) if len(pn) > 1 else 0.0
    tt = (sum(pn) / len(pn)) / se if se > 0 else 0.0
    return dict(n=len(tr), wr=100 * len(w) / len(tr),
                pf=(sum(w) / abs(sum(l)) if l else 9.99),
                tot=sum(pn), md=md, t=tt, permode=dict(permode),
                byrsn=dict(byrsn), pnls=pn, days=len({t["date"] for t in tr}))


def welch_t(a, b):
    """Two-sample Welch t on per-trade P&L. Tests whether the arms' per-trade
    means differ; unequal n and unequal variance are both expected here."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    va, vb = statistics.pvariance(a), statistics.pvariance(b)
    se = (va / len(a) + vb / len(b)) ** 0.5
    if se <= 0:
        return 0.0
    return (statistics.fmean(a) - statistics.fmean(b)) / se


def load_existing(tag):
    """Summaries for a contract already run to CSV, or None if any arm is
    missing. Lets the report be rebuilt without re-running (and lets the run be
    done one contract at a time to keep peak memory to a single scid)."""
    out = {}
    for nm in SCENARIOS:
        p = BASE / f"IOF_NQ_m2m4fb_{tag}_{nm}.csv"
        if not p.exists():
            return None
        out[nm] = summarize(p)
    return out


def main():
    # --report : rebuild the tables from CSVs on disk, run nothing.
    # <TAG>    : run one contract only (recommended — see _BAR_CACHE note).
    argv = [a for a in sys.argv[1:] if a != "--report"]
    report_only = "--report" in sys.argv
    tags = list(CONTRACTS) if not argv else [argv[0]]
    fp_before = scid_fingerprint()
    res = {}
    if report_only:
        for tag in CONTRACTS:
            r = load_existing(tag)
            if r:
                res[tag] = r
            else:
                print(f"  [report] skipping {tag} — not all arms present")
    for tag in ([] if report_only else tags):
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}")
            continue
        _BAR_CACHE.clear()      # evict previous contract's bars before loading
        res[tag] = {nm: summarize(run_scenario(nm, ov, scid, tag))
                    for nm, ov in SCENARIOS.items()}
    _BAR_CACHE.clear()

    print("\n" + "=" * 104)
    print(" M2+M4 / QF40 / MT=3 / FIXED $500-$500 BRACKET — 6-contract ladder A/B"
          "  (1 lot, NQ $20/pt)")
    print("=" * 104)
    for tag, scn in res.items():
        print(f"\n  {tag}")
        print(f"    {'arm':10s} {'n':>4s} {'days':>5s} {'WR':>5s} {'PF':>5s} "
              f"{'Net':>9s} {'Avg':>7s} {'t':>6s} {'MaxDD':>9s}")
        for nm, r in scn.items():
            avg = r["tot"] / r["n"] if r["n"] else 0.0
            print(f"    {nm:10s} {r['n']:>4} {r['days']:>5} {r['wr']:>5.1f} "
                  f"{r['pf']:>5.2f} {r['tot']:>+9,.0f} {avg:>+7,.0f} "
                  f"{r['t']:>+6.2f} {r['md']:>9,.0f}")
        for test, ctl in PAIRS:
            d = scn[test]["tot"] - scn[ctl]["tot"]
            dd = scn[test]["md"] - scn[ctl]["md"]
            v = "better" if d > 0 else ("worse" if d < 0 else "tied")
            print(f"    {'D ' + test + '-' + ctl:26s} P&L {d:>+9,.0f}   "
                  f"MaxDD {dd:>+9,.0f}   {v}")

    print("\n" + "=" * 104)
    print(" CROSS-CONTRACT GATE — ship only if better on P&L on most contracts,"
          " never worse on MaxDD,")
    print("   and the per-trade Welch t clears ~2. Pooled deltas under"
          " ~$8k/contract are inside the noise floor.")
    print("=" * 104)
    print(f"  {'test':10s} {'vs':10s} {'bet':>4s} {'wor':>4s} {'tie':>4s} "
          f"{'ddWorse':>8s} {'pooled_ctl':>11s} {'pooled_test':>11s} "
          f"{'delta':>10s} {'t':>6s}")
    for test, ctl in PAIRS:
        nb = nw = nt = ddw = 0
        for tag, scn in res.items():
            d = scn[test]["tot"] - scn[ctl]["tot"]
            if d > 0: nb += 1
            elif d < 0: nw += 1
            else: nt += 1
            if scn[test]["md"] < scn[ctl]["md"]: ddw += 1
        pool_t = sum(scn[test]["tot"] for scn in res.values())
        pool_c = sum(scn[ctl]["tot"] for scn in res.values())
        at = [x for scn in res.values() for x in scn[test]["pnls"]]
        ac = [x for scn in res.values() for x in scn[ctl]["pnls"]]
        print(f"  {test:10s} {ctl:10s} {nb:>4} {nw:>4} {nt:>4} "
              f"{str(ddw) + '/' + str(len(res)):>8s} {pool_c:>+11,.0f} "
              f"{pool_t:>+11,.0f} {pool_t - pool_c:>+10,.0f} "
              f"{welch_t(at, ac):>+6.2f}")

    print("\n per-mode pooled net by arm:")
    modes = sorted({m for t in res for nm in SCENARIOS
                    for m in res[t][nm]["permode"]})
    print(f"  {'mode':6s} " + " ".join(f"{nm:>18s}" for nm in SCENARIOS))
    for mo in modes:
        cells = []
        for nm in SCENARIOS:
            n = sum(res[t][nm]["permode"].get(mo, [0, 0.0])[0] for t in res)
            v = sum(res[t][nm]["permode"].get(mo, [0, 0.0])[1] for t in res)
            cells.append(f"{v:>+9,.0f} ({n:>3}t)")
        print(f"  {mo:6s} " + " ".join(f"{c:>18s}" for c in cells))

    print("\n exit-reason pooled net by arm (fixed bracket should show only"
          " STOP / TP / FLAT):")
    rsns = sorted({r for t in res for nm in SCENARIOS
                   for r in res[t][nm]["byrsn"]})
    print(f"  {'reason':12s} " + " ".join(f"{nm:>18s}" for nm in SCENARIOS))
    for rs in rsns:
        cells = []
        for nm in SCENARIOS:
            n = sum(res[t][nm]["byrsn"].get(rs, [0, 0.0])[0] for t in res)
            v = sum(res[t][nm]["byrsn"].get(rs, [0, 0.0])[1] for t in res)
            cells.append(f"{v:>+9,.0f} ({n:>3}t)")
        print(f"  {rs:12s} " + " ".join(f"{c:>18s}" for c in cells))

    print("\n pooled:")
    for nm in SCENARIOS:
        tot = sum(res[t][nm]["tot"] for t in res)
        n = sum(res[t][nm]["n"] for t in res)
        pn = [x for t in res for x in res[t][nm]["pnls"]]
        se = statistics.pstdev(pn) / (len(pn) ** 0.5) if len(pn) > 1 else 0.0
        tt = (statistics.fmean(pn) / se) if se > 0 else 0.0
        print(f"  {nm:10s} total={tot:>+9,.0f}  x3lot={tot * 3:>+10,.0f}  "
              f"trades={n:>4}  avg={tot / n if n else 0:>+7,.0f}  "
              f"t={tt:>+5.2f}  worstMaxDD="
              f"{min(res[t][nm]['md'] for t in res):>+9,.0f}")

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
