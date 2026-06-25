"""Run all 3 contracts under the LIVE chartbook config (from new.png, 2026-06-25).

Inputs read off the IOF NQ Autopilot study-settings dialog:
  Account=150000, DailyLoss=0 (off), MaxTrades=3, Flatten=1555, MaxPos=1,
  Entry=2 (lmt+2t), NewsFilter=0 (off), DailyProfit=0 (off),
  M1Pullback=2 (dip+reclaim), RegimeFilter=1, FadeEngine=1, AutoDisable=1,
  M1VWAPReclaim=1, SessionStart=0 (RTH 09:35), vol=3000, single-lot.

NOTE vs run_prod.py: live chartbook has NEWS OFF and DAILY_LOSS OFF (run_prod
forced news=1/loss=800). Only delta from current backtest.py defaults is
MAX_TRADES (3 vs 6). Early-scratch / open-cooldown not exposed in the dialog,
left at backtest defaults (EARLY_SCRATCH=False, C_OPEN_COOL=36).

Usage: python run_livecfg.py [NQZ25|NQM5|NQH6]   (default: all three)
"""
import csv
import sys
from pathlib import Path

BASE = Path(r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final")
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "NQM5.CME.scid",
    "NQH6":  SC_DATA / "NQH6.CME.scid",
}

LIVE = dict(
    TARGET_VOL=3000,
    FLATTEN_HHMM=1555,
    QUAL_FLOOR=50,
    DAILY_LOSS=0.0,
    DAILY_PROF=0.0,
    NEWS_FILTER=0,
    ENTRY_ORD=2,
    MAX_TRADES=3,
    SCALE_OUT=False,
    M1_PULLBACK=2,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,  # v12.37 shipped — user override ON
)


def run(scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in LIVE.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_livecfg_{tag}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag}  (LIVE chartbook cfg: maxTrades=3 newsOFF lossOFF) =====")
    backtest.main()
    return out


def summarize(csv_path):
    trades, prev = [], 0.0
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("Event") != "EXIT":
                continue
            tot = float(row["TotalPnL"])
            trades.append(dict(pnl=tot - prev, mode=row["Mode"]))
            prev = tot
    if not trades:
        return dict(n=0, net=0.0, wr=0.0, pf=0.0, modes={})
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    modes = {}
    for t in trades:
        modes[t["mode"]] = modes.get(t["mode"], 0) + 1
    return dict(n=len(pnls), net=sum(pnls), wr=100 * len(wins) / len(pnls),
                pf=pf, modes=modes)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"!! missing scid: {scid}"); continue
        results[tag] = summarize(run(scid, tag))

    print("\n" + "=" * 70)
    print(" LIVE CHARTBOOK CONFIG — all contracts")
    print("=" * 70)
    print(f"  {'contract':8s} {'n':>4s} {'WR%':>6s} {'PF':>6s} {'Net':>11s}  modes")
    tot = 0.0
    for tag, r in results.items():
        modes = " ".join(f"{m}:{c}" for m, c in sorted(r["modes"].items()))
        print(f"  {tag:8s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} {r['net']:>+11,.0f}  {modes}")
        tot += r["net"]
    print("-" * 70)
    print(f"  {'TOTAL':8s} {'':>4} {'':>6} {'':>6} {tot:>+11,.0f}")


if __name__ == "__main__":
    main()
