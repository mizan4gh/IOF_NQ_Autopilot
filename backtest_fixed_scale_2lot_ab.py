"""A/B: 2-contract fixed-$ bracket (stop $500 / TP1 $500 / TP2 $1000 per
contract) vs the live single-lot baseline. Daily loss cap = $1000.

User-specified config, 2026-07-31:
    2 contracts | daily loss $1,000 | TP1 $500/contract | TP2 $1,000/contract
    shared stop $500/contract (chosen so a 2-lot stop = -$1,000 = the cap
    exactly, keeping the repo's "daily loss = one stop" invariant intact).

On NQ at $20/pt that is a 25pt stop, lot1 booking at +25pt and the runner at
+50pt: 1:1 on lot1, 2:1 on the runner, full winner +$1,500, full loser -$1,000.

Two flavours of the one discretionary bit — what the shared stop does after
TP1 fills — are tested separately:
    fix2lot_pure : stop never moves (a true untouched attached OCO)
    fix2lot_be   : stop -> breakeven once TP1 fills (runner risk-free)

Baseline is the CURRENT live panel single-lot (MT=1, DL=800, M8 floor-60, news
ON, early-scratch ON, ATR stop + trail) -- i.e. what is actually deployed.

Prior art this must clear: scaleout_2lot_ab falsified 2-lot TP1/TP2 scale-out
three times (2026-05-27 x2, 2026-07-14), the last under this exact live config,
where it lost on NQM5 and worsened MaxDD on all 3. Those tests used the
STRATEGY's ATR/VP targets with a TRAILING runner; this one uses fixed dollar
targets and a non-trailing runner, so it is a genuinely different exit -- but
the prior is negative and the leverage-vs-edge question below is the crux.

LEVERAGE CONTROL: a 2-lot book trivially doubles P&L when the edge is positive.
The table therefore reports Net/contract alongside Net; if the fixed bracket
only matches baseline on a per-contract basis, it is leverage, not edge, and
per feedback_cross_contract_ab / ab_noise_floor that is not shippable.

Frozen F.US.E* snapshots only -- Sierra rewrites the live Data-dir scids
mid-run (see nqh6_data_rewrite_incident). Bars are built ONCE per contract and
reused across scenarios, so the entry stream is identical by construction and
every delta is attributable to the exit engine alone.

Usage: python backtest_fixed_scale_2lot_ab.py [TAG ...]   (default: all 6)
"""
import csv, sys, math, statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQZ25": "F.US.ENQZ25.scid",   # Dec-2025
    "NQM5":  "F.US.ENQM25.scid",   # Jun-2025
    "NQH6":  "F.US.ENQH26.scid",   # Mar-2026
    "NQU25": "F.US.ENQU25.scid",   # Sep-2025
    "NQM6":  "F.US.ENQM26.scid",   # Jun-2026
    "NQU26": "F.US.ENQU26.scid",   # Sep-2026 (short history)
}

# Current live panel. DAILY_LOSS is the ONE knob the user changed (800 -> 1000);
# see the note printed at the end -- at MAX_TRADES=1 it is provably inert here.
_LIVE = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000,
    MAX_TRADES=1, DAILY_PROF=0.0,
    QUAL_FLOOR=50, QUAL_FLOOR_M8=60, M8_FADE_FULL=True,
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False, ENABLE_M7=False,
    TREND_LONG=False, DISABLE_MODES=set(), V13_MODEL=False,
    IMB_MODEL="cpp_stateful", ENTRY_ORD=2,
    PT_VAL=20.0, COMMISSION=5.0,
    FIXED_BRACKET=False, SCALE_OUT=False,
)
_BASE_EXIT = dict(EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25)
# Fixed bracket owns the exit end-to-end: no scratch, no trail, no CB.
_FIX = dict(
    EARLY_SCRATCH=False,
    FIXED_SCALE=True, FIXED_SCALE_QTY=2,
    FIXED_SCALE_STOP_USD=500.0,
    FIXED_SCALE_TP1_USD=500.0,
    FIXED_SCALE_TP2_USD=1000.0,
)

SCENARIOS = {
    # reference = what is deployed today
    "baseline_1lot":  {**_LIVE, **_BASE_EXIT, "DAILY_LOSS": 800.0,
                       "FIXED_SCALE": False},
    "fix2lot_pure":   {**_LIVE, **_FIX, "DAILY_LOSS": 1000.0,
                       "FIXED_SCALE_BE": False},
    "fix2lot_be":     {**_LIVE, **_FIX, "DAILY_LOSS": 1000.0,
                       "FIXED_SCALE_BE": True},
}
# Control: same fixed bracket at ONE contract. Isolates bracket-vs-leverage --
# lot1 and the runner collapse onto a single lot that books at TP1.
SCENARIOS["fix1lot_ctl"] = {**_LIVE, **_FIX, "DAILY_LOSS": 1000.0,
                            "FIXED_SCALE_BE": False, "FIXED_SCALE_QTY": 1}

QTY = {"baseline_1lot": 1, "fix2lot_pure": 2, "fix2lot_be": 2, "fix1lot_ctl": 1}


def load_bars(fname):
    """Read + bar-build once per contract; every scenario replays these bars."""
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    scid = BASE / fname
    scale = backtest.detect_price_scale(str(scid))
    recs = backtest.read_scid(str(scid))
    bars = backtest.build_volume_bars(recs, target_vol=3000, price_scale=scale)
    print(f"  {fname}: {len(recs):,} recs -> {len(bars):,} bars (scale /{scale:.0f})")
    del recs
    return bars


def run(nm, ov, bars, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    print(f"\n===== {tag} :: {nm} =====")
    trades = backtest.Backtester(bars).run()
    out = BASE / f"IOF_NQ_fixscale_{tag}_{nm}.csv"
    backtest.write_csv(trades, out)
    return out


def wd(a, b):
    d = date.fromisoformat(a); e = date.fromisoformat(b); n = 0
    while d <= e:
        if d.weekday() < 5: n += 1
        d += timedelta(days=1)
    return n


def s(p):
    """Per-trade stats from the EXIT rows (TotalPnL is cumulative)."""
    tr, pv = [], 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] == "EXIT":
            t = float(r["TotalPnL"]); tr.append((t - pv, r["Date"], r["Mode"])); pv = t
    if not tr:
        return dict(n=0, wr=0, pf=0, tot=0, md=0, sd=float("nan"), t=float("nan"))
    pn = [x[0] for x in tr]; w = [p for p in pn if p > 0]; l = [p for p in pn if p < 0]
    tot = sum(pn); pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run_ = md = 0.0
    for p in pn:
        run_ += p; pk = max(pk, run_); md = min(md, run_ - pk)
    dl = defaultdict(float)
    for x in tr: dl[x[1]] += x[0]
    ds = sorted(dl); nd = wd(ds[0], ds[-1])
    sd = (sum(dl.values()) / nd) / (math.sqrt(sum(min(v, 0) ** 2 for v in dl.values()) / nd) or 1)
    # t on mean-per-trade vs 0 -- the ab_noise_floor check
    if len(pn) > 1:
        se = statistics.stdev(pn) / math.sqrt(len(pn))
        tst = (tot / len(pn)) / se if se else float("nan")
    else:
        tst = float("nan")
    return dict(n=len(tr), wr=100 * len(w) / len(tr), pf=pf, tot=tot, md=md,
                sd=sd, t=tst)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else sys.argv[1:]
    res = {}
    for tag in tags:
        fname = CONTRACTS[tag]
        if not (BASE / fname).exists():
            print(f"missing: {fname}"); continue
        print(f"\n########## {tag} ##########")
        bars = load_bars(fname)
        res[tag] = {nm: s(run(nm, ov, bars, tag)) for nm, ov in SCENARIOS.items()}
        del bars

    w = 104
    print("\n" + "=" * w)
    print(" 2-LOT FIXED $ BRACKET  (stop $500 / TP1 $500 / TP2 $1000 per contract"
          ", DL=$1000)")
    print(" vs LIVE 1-LOT BASELINE (MT=1, DL=800, M8 floor-60, ATR stop + trail)")
    print("=" * w)
    hdr = (f"  {'contract':8s} {'scenario':14s} {'n':>3} {'WR':>6} {'PF':>6} "
           f"{'Net':>10} {'Net/ctr':>9} {'MaxDD':>9} {'Sortino':>8} {'t':>6}")
    print(hdr)
    for tag in res:
        for nm in SCENARIOS:
            r = res[tag][nm]
            print(f"  {tag:8s} {nm:14s} {r['n']:>3} {r['wr']:>5.0f}% {r['pf']:>6.2f} "
                  f"{r['tot']:>+10,.0f} {r['tot']/QTY[nm]:>+9,.0f} {r['md']:>+9,.0f} "
                  f"{r['sd']:>8.3f} {r['t']:>6.2f}")
        print()

    print("=" * w)
    print(" GATES  (vs baseline_1lot)")
    print("=" * w)
    for nm in ("fix2lot_pure", "fix2lot_be", "fix1lot_ctl"):
        pdel, dddel, perctr = {}, {}, {}
        for tag in res:
            b, f = res[tag]["baseline_1lot"], res[tag][nm]
            pdel[tag] = f["tot"] - b["tot"]
            dddel[tag] = f["md"] - b["md"]          # positive = shallower DD
            perctr[tag] = f["tot"] / QTY[nm] - b["tot"]
        n = len(pdel)
        print(f"\n {nm}:")
        for tag in res:
            print(f"   {tag:8s} P&L {pdel[tag]:>+9,.0f}   MaxDD {dddel[tag]:>+9,.0f}"
                  f"   P&L/contract {perctr[tag]:>+9,.0f}")
        npl = sum(d > 0 for d in pdel.values())
        ndd = sum(d > 0 for d in dddel.values())
        npc = sum(d > 0 for d in perctr.values())
        print(f"   P&L gate        : better on {npl}/{n} => "
              + ("PASS" if npl == n else "FAIL (cross-contract disagreement)"))
        print(f"   MaxDD gate      : better on {ndd}/{n}")
        print(f"   Per-contract    : better on {npc}/{n} => "
              + ("real edge change" if npc == n else
                 "NOT edge -- gains are leverage" if npl > npc else "worse per contract"))
        print(f"   Pooled P&L delta: {sum(pdel.values()):>+10,.0f}"
              f"   pooled/contract {sum(perctr.values()):>+10,.0f}")

    print("\n" + "-" * w)
    print(" NOTE on DAILY_LOSS=1000: at MAX_TRADES=1 the cap cannot bind in this")
    print(" harness. day_pnl only updates at _close, so it never truncates an open")
    print(" position; the sess_dd>0.8*DL entry gate reads 0 at each day's first")
    print(" trade; and RiskState.day_reset() clears in_recovery nightly. DL=800 and")
    print(" DL=1000 are therefore expected byte-identical (cf. dailyloss_sweep_noop).")
    print(" What the $1000 figure DOES buy is live headroom: the 2-lot 25pt stop is")
    print(" -$1,000 exactly, so the cap no longer fires mid-stop as it does at $800.")
    print("-" * w)


if __name__ == "__main__":
    main()
