"""
Backtest of IOF_MNQ_M2_M4_3Qty_300SL_500PT.cpp  (SCDLLName IOF_MNQ_M2_M4_FixedBracket)
on MNQ only  (FROZEN_MNQU6_0722.scid).

What this cpp variant IS (verified against the .cpp, 2026-07-23):
  * ONLY M2 (VWAP mean-revert) and M4 (sweep+reclaim) signals fire; M4 has
    same-bar priority. M1/M3/M5/M6/M7/M8 are computed but excluded from the
    goL/goS combination (line 3462-3464).
  * FIXED bracket over the whole position: 300-tick STOP (=75.0 pt) / 500-tick
    TARGET (=125.0 pt). NO trail, NO break-even, NO scratch, NO scale-out, NO
    VP-target override — held to stop or target (line 2481-2484).
  * Fixed size 3 MNQ contracts (line 3593); one OCO bracket for all 3.
  * Quality floor 30 for BOTH modes (line 3581). M2 uses /15 scaling so needs
    finalScore>=5; M4 uses *10 scaling so fires at finalScore>=3.
  * Runtime input defaults in THIS cpp: MAX_TRADES=1, DAILY_LOSS=$600,
    DAILY_PROF=$500, ENTRY_ORD=2 (marketable lmt+2t), NEWS/REGIME/AUTO_DISABLE
    all ON, RTH-only (09:35), flatten 15:55.

MNQ dollars: MNQ = $2/point (NQ/10). 3 lots => $6/point.
  * 75pt stop  = $450 loss   (< $600 loss cap => the tick stop binds first)
  * 125pt tgt  = $750 gain   BUT the $500 DAILY_PROF cap includes OpenProfitLoss,
    so it flattens the winner at ~$500 = 83.3pt intrabar. => the 125pt target is
    UNREACHABLE as written; effective target ~83pt. Two arms expose this:
        as_written : profit lock $500 ON  (what the DLL actually does)
        full_tgt   : profit lock OFF       (pure 75/125 tick bracket)

Fidelity caveats (same proxy the repo's validated A/Bs use):
  * backtest.py does NOT model REGIME_FILTER or AUTO_DISABLE (both ON live) ->
    Python fires some trades the live DLL would block, and never benches a
    losing mode. Both push Python trade-count HIGHER than live.
  * Tie bars (range spans both stop & target) assumed stop-first (conservative).
  * Single MNQ contract only (MNQU6) — NOT a cross-contract A/B. Per repo rule a
    lone-contract result is not shippable on its own.
"""
import csv, sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
SCID = BASE / "FROZEN_MNQU6_0722.scid"

# Exact cpp panel, MNQ account dollars.
_PANEL = dict(
    FIXED_BRACKET=True, FIXED_STOP_TICKS=300, FIXED_TARGET_TICKS=500, FIXED_QTY=3,
    PT_VAL=2.0,                 # MNQ $2/point
    COMMISSION=1.50,            # MNQ RT per contract (retail all-in; see sensitivity note)
    QUAL_FLOOR=30,
    DISABLE_MODES={0, 2, 4, 5, 6, 7},   # leave only M2(1) + M4(3)
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    M8_FADE_FULL=False, V13_MODEL=False,
    SCALE_OUT=False, EARLY_SCRATCH=False,
    ENTRY_ORD=2, NEWS_FILTER=1, C_OPEN_COOL=36,
    MAX_TRADES=1, DAILY_LOSS=0.0, DAILY_PROF=0.0,   # open-PnL locks handle the caps
    IMB_MODEL="cpp_stateful",
)
ARMS = {
    "as_written": {"FIXED_PROFIT_LOCK": 500.0, "FIXED_LOSS_LOCK": 600.0},
    "full_tgt":   {"FIXED_PROFIT_LOCK": 0.0,   "FIXED_LOSS_LOCK": 0.0},
}
VOLS = {
    "nqcadence_17500": 17500,   # MNQ vol/bar that reproduces NQ 3k-vol cadence (run_mnq_oos)
    "literal_3000":    3000,    # cpp's literal "3000 vol bars" spec applied to MNQ
}

_REC_CACHE = {}
_BAR_CACHE = {}


def _load_bars(backtest, target_vol):
    if target_vol in _BAR_CACHE:
        return _BAR_CACHE[target_vol]
    if "recs" not in _REC_CACHE:
        print(f"Reading {SCID.name} (once) ...")
        scale = backtest.detect_price_scale(str(SCID))
        _REC_CACHE["scale"] = scale
        _REC_CACHE["recs"] = backtest.read_scid(str(SCID))
        print(f"  {len(_REC_CACHE['recs']):,} records  price_scale=/{scale:.0f}")
    print(f"Building {target_vol}-vol bars ...")
    bars = backtest.build_volume_bars(_REC_CACHE["recs"], target_vol=target_vol,
                                      price_scale=_REC_CACHE["scale"])
    _BAR_CACHE[target_vol] = bars
    print(f"  {len(bars):,} bars")
    return bars


def run(vol_name, target_vol, arm_name, arm_ov):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    ov = {**_PANEL, **arm_ov, "TARGET_VOL": target_vol}
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    # bar cache is keyed by vol and survives module reloads (module-level dicts here)
    bars = _load_bars(backtest, target_vol)
    bt = backtest.Backtester(bars)
    trades = bt.run()
    out = BASE / f"IOF_MNQ_fixbrkt_{vol_name}_{arm_name}.csv"
    backtest.write_csv(trades, out)
    return trades


def stats(trades):
    rows, prev = [], 0.0
    for t in trades:
        if t.event != "EXIT":
            continue
        pnl = t.tot_pnl - prev
        prev = t.tot_pnl
        rows.append(dict(pnl=pnl, mode=t.mode, rsn=t.exit_rsn, date=t.date))
    if not rows:
        return dict(n=0, net=0.0, md=0.0, wr=0.0, permode={}, byrsn={})
    pn = [r["pnl"] for r in rows]
    eq = pk = md = 0.0
    for x in pn:
        eq += x; pk = max(pk, eq); md = min(md, eq - pk)
    w = [x for x in pn if x > 0]
    pm = defaultdict(lambda: [0, 0.0]); br = defaultdict(lambda: [0, 0.0])
    for r in rows:
        pm[r["mode"]][0] += 1; pm[r["mode"]][1] += r["pnl"]
        br[r["rsn"]][0] += 1;  br[r["rsn"]][1] += r["pnl"]
    import statistics
    se = statistics.pstdev(pn) / (len(pn) ** 0.5) if len(pn) > 1 else 0.0
    t_stat = (sum(pn) / len(pn)) / se if se > 0 else 0.0
    return dict(n=len(rows), net=sum(pn), md=md, wr=100 * len(w) / len(rows),
                permode=dict(pm), byrsn=dict(br), avg=sum(pn) / len(rows), t=t_stat)


def main():
    res = {}
    for vn, tv in VOLS.items():
        for an, av in ARMS.items():
            print(f"\n===== {vn} :: {an} =====")
            res[(vn, an)] = stats(run(vn, tv, an, av))

    print("\n" + "=" * 78)
    print(" IOF_MNQ_M2_M4_FixedBracket  —  MNQ (MNQU6) — actual MNQ account $")
    print("   3 lots, 75pt stop / 125pt tgt, M2+M4 only, floor 30, MT=1, news ON")
    print("=" * 78)
    hdr = f"  {'cadence':16s} {'arm':11s} {'n':>4s} {'WR%':>5s} {'Net$':>9s} {'Avg$':>7s} {'t':>6s} {'MaxDD$':>9s}"
    for vn in VOLS:
        print()
        print(hdr)
        for an in ARMS:
            r = res[(vn, an)]
            print(f"  {vn:16s} {an:11s} {r['n']:>4} {r['wr']:>5.1f} "
                  f"{r['net']:>+9,.0f} {r['avg']:>+7,.0f} {r['t']:>+6.2f} {r['md']:>+9,.0f}")

    print("\n  per-mode / per-exit breakdown (as_written arms):")
    for vn in VOLS:
        r = res[(vn, "as_written")]
        pm = "  ".join(f"{m}={v:>+7,.0f}({n}t)" for m, (n, v) in sorted(r["permode"].items()))
        br = "  ".join(f"{k}={v:>+7,.0f}({n}t)" for k, (n, v) in sorted(r["byrsn"].items()))
        print(f"    {vn:16s} modes: {pm}")
        print(f"    {'':16s} exits: {br}")

    print("\n  Note: 'as_written' = DLL as configured ($500 profit cap truncates the")
    print("  125pt target at ~83pt / $500). 'full_tgt' = profit cap off = pure 75/125.")
    print("  Commission = $1.50/contract RT; MNQ = $2/pt. Micro, single contract, no")
    print("  regime/auto-disable modeling. NOT a cross-contract A/B.")


if __name__ == "__main__":
    main()
