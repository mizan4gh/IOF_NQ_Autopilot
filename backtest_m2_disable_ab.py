"""A/B: disable M2 entirely vs live book — SIX frozen contracts, live config.

Motivation (2026-07-14, post VP-look-ahead fix [[project_backtest_vp_lookahead_bug]]):
the M2-only re-run on HONEST levels shows M2 is significantly NEGATIVE at its
tighter floors — pooled f50 mean -$263/trade t=-2.82 (n=16), f46 -$208/trade
t=-2.63 (n=67), both 0-2/6 contracts positive. The quality gradient is INVERTED
(tighter floor => worse per trade), i.e. M2's score is anti-predictive. Every
prior M2 verdict was measured against frozen end-of-file VP levels and is void.

So the live question is not "which floor" (46 is now the WORST band) but
"does M2 belong in the book at all". Single binary lever: DISABLE_MODES={1}.

Baseline models CURRENT live config: MT=1/DL=800 (panel since 2026-07-02,
[[project_mt3_dl800_config]]) + M8 floor-60 (v12.38 deployed 2026-07-05).
Config copied verbatim from backtest_m2_qf46_ab.py.

WHY THIS ISN'T THE SAME TRAP AS FLOOR-46: that lever was a *tuned parameter*
picked as the best of a swept band. This is a binary on/off motivated by a
significant (|t|>2) result. But it IS still selected on the same six contracts
the screen used — a pass here is confirmation, not independent evidence. The
thing this run adds that the screen CANNOT: MT=1 slot displacement. M2 standalone
edge != M2's effect on the book, because an M2 fire consumes the day's only slot
and bumps a possibly-better M1/M4/M6/M8 setup. That mechanism is exactly how
floor-46 died (+$9.9k standalone -> -$1,130 live NQU26).

Gate: cross-contract agreement incl. live NQU26 [[feedback_cross_contract_ab]].
Report t, not just net [[reference_ab_noise_floor]].

Usage: python backtest_m2_disable_ab.py [TAG]   (default: all six)
"""
import csv
import statistics as st
import sys
from pathlib import Path

BASE = Path(__file__).parent

CONTRACTS = {
    "NQU25": BASE / "F.US.ENQU25.scid",   # Sep-2025
    "NQZ25": BASE / "F.US.ENQZ25.scid",   # Dec-2025
    "NQM5":  BASE / "F.US.ENQM25.scid",   # Jun-2025
    "NQH6":  BASE / "F.US.ENQH26.scid",   # Mar-2026
    "NQM6":  BASE / "F.US.ENQM26.scid",   # Jun-2026
    "NQU26": BASE / "F.US.ENQU26.scid",   # Sep-2026 (LIVE)
}

# Verbatim from backtest_m2_qf46_ab.py — current live panel config.
_PROD = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, QUAL_FLOOR_M8=60, M8_FADE_FULL=True,
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False, ENABLE_M7=False,
    TREND_LONG=False,
)

SCENARIOS = {
    "live":   {**_PROD, "DISABLE_MODES": set()},   # baseline: M2 on at floor 50
    "m2_off": {**_PROD, "DISABLE_MODES": {1}},     # M2 (sel==1) suppressed
}
BASELINE = "live"


def run(nm, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_m2off_{tag}_{nm}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {nm} =====")
    backtest.main()
    return out


def summarize(p):
    trades, prev = [], 0.0
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            if r["Event"] != "EXIT":
                continue
            tot = float(r["TotalPnL"])
            trades.append(tot - prev)
            prev = tot
    if not trades:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, dd=0.0, pnls=[])
    w = [x for x in trades if x > 0]
    L = [x for x in trades if x < 0]
    peak = run_ = dd = 0.0
    for x in trades:
        run_ += x; peak = max(peak, run_); dd = min(dd, run_ - peak)
    return dict(n=len(trades), total=sum(trades),
                pf=(sum(w) / abs(sum(L))) if L else float("inf"),
                wr=100 * len(w) / len(trades), dd=dd, pnls=trades)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}")
            continue
        for nm, ov in SCENARIOS.items():
            res.setdefault(tag, {})[nm] = summarize(run(nm, ov, scid, tag))

    print("\n" + "=" * 86)
    print(" DISABLE M2 vs LIVE — 6 frozen contracts, live config (MT=1/DL=800, M8 f60)")
    print("=" * 86)
    print(f"  {'contract':8s} {'live n':>6s} {'live net':>10s} "
          f"{'off n':>6s} {'off net':>10s} {'delta':>10s} {'dd live':>9s} {'dd off':>9s}")
    deltas, better, worse = [], 0, 0
    for tag in res:
        a, b = res[tag]["live"], res[tag]["m2_off"]
        d = b["total"] - a["total"]
        deltas.append(d)
        if d > 0:
            better += 1
        elif d < 0:
            worse += 1
        flag = "" if d >= 0 else "  <- m2_off worse"
        print(f"  {tag:8s} {a['n']:>6} {a['total']:>+10,.0f} "
              f"{b['n']:>6} {b['total']:>+10,.0f} {d:>+10,.0f} "
              f"{a['dd']:>9,.0f} {b['dd']:>9,.0f}{flag}")

    pl = [p for t in res for p in res[t]["live"]["pnls"]]
    po = [p for t in res for p in res[t]["m2_off"]["pnls"]]
    print(f"\n  pooled live : n={len(pl):>4} net={sum(pl):>+9,.0f}")
    print(f"  pooled m2off: n={len(po):>4} net={sum(po):>+9,.0f}")
    print(f"  pooled delta: {sum(po) - sum(pl):>+9,.0f}   "
          f"contracts better {better}/{len(res)}, worse {worse}/{len(res)}")

    # Per-trade t on each arm (pooled). Deltas are paired-by-contract, not by
    # trade, so a paired t across 6 points is meaningless — report arms instead.
    for nm, arr in (("live", pl), ("m2_off", po)):
        if len(arr) > 1:
            m = sum(arr) / len(arr)
            sd = st.pstdev(arr)
            se = sd / len(arr) ** 0.5
            t = m / se if se else 0.0
            print(f"  {nm:7s} mean={m:>+7.0f}/trade  t={t:>+5.2f}"
                  + ("  SIGNIFICANT" if abs(t) >= 2 else "  (ns)"))

    print("\n  SHIP only if: no contract worse (incl. LIVE NQU26) AND MaxDD not worse.")
    print("  A pass = chartbook DISABLE_MODES flip, no cpp change. Confirmation,")
    print("  not independent evidence — same 6 contracts the screen used.")


if __name__ == "__main__":
    main()
