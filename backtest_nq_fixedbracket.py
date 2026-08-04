"""
NQ counterpart to backtest_mnq_fixedbracket.py — same M2+M4 fixed 75/125pt
bracket, floor 30, MT=1, native NQ 3000-vol bars, sized at NQ $20/pt.

Purpose: compare the fixed-bracket M2+M4 strategy MNQ-vs-NQ and across the
frozen NQ contract set (cross-contract robustness the repo always requires).

  * NQU26 (F.US.ENQU26) is the SAME underlying/expiry as MNQU6 -> the tightest
    apples-to-apples: MNQ-full_tgt points should ~= NQU26 points. Isolates the
    pure instrument-multiplier + micro-cap effect.
  * NQZ25 / NQM5 / NQH6 are different quarters -> genuine cross-period OOS.

The $600/$500 daily caps in the .cpp are sized for MICRO ($2/pt); on NQ ($20/pt)
they are nonsensical (a $600 loss cap = 10pt < the 75pt stop). So NQ runs the
pure tick bracket (no open-PnL cap). Instrument-independent comparison metric =
POINTS per trade and trade-count / WR / t.
"""
import csv, sys, statistics
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQU26": "F.US.ENQU26.scid",   # Sep-2026 — direct pair w/ MNQU6
    "NQZ25": "F.US.ENQZ25.scid",   # Dec-2025
    "NQM5":  "F.US.ENQM25.scid",   # Jun-2025
    "NQH6":  "F.US.ENQH26.scid",   # Mar-2026
}
NQ_LOTS = 1   # per-lot NQ; x3 for 3-lot NQ dollars

_PANEL = dict(
    FIXED_BRACKET=True, FIXED_STOP_TICKS=300, FIXED_TARGET_TICKS=500,
    FIXED_QTY=NQ_LOTS, FIXED_PROFIT_LOCK=0.0, FIXED_LOSS_LOCK=0.0,   # pure bracket
    PT_VAL=20.0, COMMISSION=5.0,
    QUAL_FLOOR=30,
    DISABLE_MODES={0, 2, 4, 5, 6, 7},   # leave only M2(1) + M4(3)
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    M8_FADE_FULL=False, V13_MODEL=False,
    SCALE_OUT=False, EARLY_SCRATCH=False,
    ENTRY_ORD=2, NEWS_FILTER=1, C_OPEN_COOL=36,
    MAX_TRADES=1, DAILY_LOSS=0.0, DAILY_PROF=0.0,
    IMB_MODEL="cpp_stateful", TARGET_VOL=3000,
)


def run(label, fname):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in _PANEL.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    scid = BASE / fname
    scale = backtest.detect_price_scale(str(scid))
    print(f"\n===== NQ {label} :: {fname}  (scale /{scale:.0f}) =====")
    recs = backtest.read_scid(str(scid))
    bars = backtest.build_volume_bars(recs, target_vol=3000, price_scale=scale)
    print(f"  {len(recs):,} recs -> {len(bars):,} bars")
    bt = backtest.Backtester(bars)
    trades = bt.run()
    backtest.write_csv(trades, BASE / f"IOF_NQ_fixbrkt_{label}.csv")
    return trades


def stats(trades):
    rows, prev = [], 0.0
    for t in trades:
        if t.event != "EXIT":
            continue
        pnl = t.tot_pnl - prev
        prev = t.tot_pnl
        # points per trade (instrument-independent): $ / (PT_VAL*qty), + commission back
        rows.append(dict(pnl=pnl, mode=t.mode, rsn=t.exit_rsn))
    if not rows:
        return dict(n=0, net=0.0, md=0.0, wr=0.0, avg=0.0, t=0.0,
                    ppt=0.0, permode={}, byrsn={})
    pn = [r["pnl"] for r in rows]
    eq = pk = md = 0.0
    for x in pn:
        eq += x; pk = max(pk, eq); md = min(md, eq - pk)
    w = [x for x in pn if x > 0]
    pm = defaultdict(lambda: [0, 0.0]); br = defaultdict(lambda: [0, 0.0])
    for r in rows:
        pm[r["mode"]][0] += 1; pm[r["mode"]][1] += r["pnl"]
        br[r["rsn"]][0] += 1;  br[r["rsn"]][1] += r["pnl"]
    se = statistics.pstdev(pn) / (len(pn) ** 0.5) if len(pn) > 1 else 0.0
    tt = (sum(pn) / len(pn)) / se if se > 0 else 0.0
    # avg points/trade = avg $ per lot / $20  (add commission back for gross points)
    ppt = (sum(pn) / len(rows)) / (20.0 * NQ_LOTS) + (5.0 / 20.0)
    return dict(n=len(rows), net=sum(pn), md=md, wr=100 * len(w) / len(rows),
                avg=sum(pn) / len(rows), t=tt, ppt=ppt,
                permode=dict(pm), byrsn=dict(br))


def main():
    res = {lb: stats(run(lb, fn)) for lb, fn in CONTRACTS.items()}

    print("\n" + "=" * 84)
    print(" NQ fixed-bracket M2+M4  (75pt stop / 125pt tgt, floor 30, MT=1, 1 NQ lot)")
    print("   native 3000-vol NQ bars, NQ $20/pt, pure tick bracket (no micro caps)")
    print("=" * 84)
    print(f"  {'contract':8s} {'n':>4s} {'WR%':>5s} {'Net$(1lot)':>10s} "
          f"{'Net$(3lot)':>10s} {'Avg$':>7s} {'AvgPts':>7s} {'t':>6s} {'MaxDD$':>9s}")
    tot1 = tot3 = totn = 0.0
    for lb in CONTRACTS:
        r = res[lb]
        tot1 += r["net"]; tot3 += r["net"] * 3; totn += r["n"]
        print(f"  {lb:8s} {r['n']:>4} {r['wr']:>5.1f} {r['net']:>+10,.0f} "
              f"{r['net']*3:>+10,.0f} {r['avg']:>+7,.0f} {r['ppt']:>+7.1f} "
              f"{r['t']:>+6.2f} {r['md']:>+9,.0f}")
    print(f"  {'POOLED':8s} {int(totn):>4} {'':>5} {tot1:>+10,.0f} {tot3:>+10,.0f}")

    print("\n  per-mode net $/1lot:")
    for lb in CONTRACTS:
        pm = "  ".join(f"{m}={v:>+7,.0f}({n}t)"
                       for m, (n, v) in sorted(res[lb]["permode"].items()))
        print(f"    {lb:8s} {pm}")
    print("\n  exit-reason net $/1lot:")
    for lb in CONTRACTS:
        br = "  ".join(f"{k}={v:>+7,.0f}({n}t)"
                       for k, (n, v) in sorted(res[lb]["byrsn"].items()))
        print(f"    {lb:8s} {br}")


if __name__ == "__main__":
    main()
