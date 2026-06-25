"""A/B: LIVE chartbook config (new.png) at QUAL_FLOOR=50 vs 40 — v12.37, 3-contract.

Same live config as run_livecfg.py (MaxTrades=3, news OFF, daily-loss OFF,
M1-pullback=dip, early-scratch ON = v12.37 confirmed in live DLL), but sweeps
the GLOBAL qual floor 50 -> 40. Note global qf40 failed before under OTHER
configs (v12.26 apex/news-on disagreed; qf33 falsified all-3) — this tests it
under the actual deployed config.

Cross-contract gate: [[feedback_cross_contract_ab]] — need AGREE better to ship.

Usage: python run_livecfg_qf40_ab.py [NQZ25|NQM5|NQH6]   (default: all three)
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
    TARGET_VOL=3000, FLATTEN_HHMM=1555,
    DAILY_LOSS=0.0, DAILY_PROF=0.0, NEWS_FILTER=0,
    ENTRY_ORD=2, MAX_TRADES=3, SCALE_OUT=False, M1_PULLBACK=2,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,  # v12.37, in live DLL
)

SCENARIOS = {
    "qf50_live": {**LIVE, "QUAL_FLOOR": 50},
    "qf40_test": {**LIVE, "QUAL_FLOOR": 40},
}


def run(name, overrides, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_livecfgqf_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {name}  (QUAL_FLOOR={overrides['QUAL_FLOOR']}, live cfg) =====")
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
        return dict(n=0, net=0.0, wr=0.0, pf=0.0, max_dd=0.0, modes={})
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    peak = run_eq = max_dd = 0.0
    for p in pnls:
        run_eq += p; peak = max(peak, run_eq); max_dd = min(max_dd, run_eq - peak)
    modes = {}
    for t in trades:
        modes[t["mode"]] = modes.get(t["mode"], 0) + 1
    return dict(n=len(pnls), net=sum(pnls), wr=100 * len(wins) / len(pnls),
                pf=pf, max_dd=max_dd, modes=modes)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"!! missing scid: {scid}"); continue
        results[tag] = {name: summarize(run(name, ov, scid, tag))
                        for name, ov in SCENARIOS.items()}

    print("\n" + "=" * 76)
    print(" LIVE CONFIG — QUAL_FLOOR 50 vs 40 (v12.37)")
    print("=" * 76)
    print(f"  {'contract':8s} {'scenario':10s} {'n':>4s} {'WR%':>6s} {'PF':>6s} {'Net':>11s} {'MaxDD':>9s}  modes")
    for tag, scn in results.items():
        for name in ["qf50_live", "qf40_test"]:
            r = scn[name]
            modes = " ".join(f"{m}:{c}" for m, c in sorted(r["modes"].items()))
            print(f"  {tag:8s} {name:10s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['net']:>+11,.0f} {r['max_dd']:>9,.0f}  {modes}")
        print()

    print(" Verdict (qf40 - qf50) per contract:")
    deltas = {}
    for tag, scn in results.items():
        d = scn["qf40_test"]["net"] - scn["qf50_live"]["net"]
        deltas[tag] = d
        print(f"   {tag:8s} {d:>+11,.0f}")
    if all(d > 0 for d in deltas.values()):
        print("\n  ALL AGREE BETTER -> candidate")
    elif all(d <= 0 for d in deltas.values()):
        print("\n  agree neutral/worse -> keep floor 50")
    else:
        print("\n  DISAGREE -> reject (single-contract overfit)")


if __name__ == "__main__":
    main()
