"""A/B test: QUAL_FLOOR=50 (live) vs QUAL_FLOOR=46 vs QUAL_FLOOR=40.

Per [[project_v1226_qualfloor_test]] memory: QUAL_FLOOR was tested at 40 in
v12.26-29, REJECTED cross-contract (NQZ25 $-2,955 vs NQM5 $+8,145 disagreed)
in v12.30 revert. Re-running with the FIXED bar builder (NQH6/NQM5 had ATR
corruption due to low=0 records) to check if the NQM5 result was inflated by
the bug.

Cross-contract A/B convention: NQZ25 + NQM5 must AGREE fixed_better.

Usage:
    python backtest_qualfloor_ab.py
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "NQM5.CME.scid",
}

_APEX = dict(
    C_COOL_TRADE=5, C_COOL_LOSS=10, C_COOL_STOP=10,
    C_OPEN_COOL=36, C_VCOOL_PAUSE=40,
    DAILY_LOSS=800.0, DAILY_PROF=1000.0,
    MAX_TRADES=3, ENTRY_ORD=2, NEWS_FILTER=1,
    RISK_MODEL="v12_32_fixed",
    M1_PULLBACK=2,  # match current production (v12.34 dip+reclaim)
)

SCENARIOS = {
    "qf50_live":     {**_APEX, "QUAL_FLOOR": 50},
    "qf46_proposed": {**_APEX, "QUAL_FLOOR": 46},
    "qf40_legacy":   {**_APEX, "QUAL_FLOOR": 40},
}


def run_scenario(name, overrides, scid_path, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_backtest_qf_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid_path), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  QUAL_FLOOR = {overrides['QUAL_FLOOR']}")
    bt = backtest.main()
    return out, bt.rm_gated


def summarize(csv_path):
    trades = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            try:
                pnl = float(row["DayPnL"])
            except (ValueError, KeyError):
                continue
            trades.append((pnl, row.get("Mode", "")))
    if not trades:
        return dict(n=0, net=0.0, wr=0.0, pf=0.0)
    pnls = [t[0] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = -sum(p for p in pnls if p <= 0)
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    return dict(n=n, net=sum(pnls), wr=wins / n, pf=pf)


def main():
    contracts = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in contracts:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}"); continue
        results[tag] = {}
        for name, ovr in SCENARIOS.items():
            out, rm_gated = run_scenario(name, ovr, scid, tag)
            results[tag][name] = summarize(out)
            results[tag][name]["rm_gated"] = rm_gated

    print("\n" + "=" * 78)
    print(" QUAL_FLOOR A/B SUMMARY")
    print("=" * 78)
    print(f"  {'contract':8s} {'scenario':18s} {'n':>4s} {'WR':>7s} {'PF':>6s} {'Net':>11s} {'rm_gtd':>8s}")
    for tag, scn in results.items():
        for name in ["qf50_live", "qf46_proposed", "qf40_legacy"]:
            r = scn.get(name)
            if r is None: continue
            print(f"  {tag:8s} {name:18s} {r['n']:>4} {r['wr']:>6.1%} {r['pf']:>6.2f} {r['net']:>+11,.0f} {r['rm_gated']:>8}")
        print()

    # Decision per contract: which qf maximizes net?
    print(" Cross-contract winner per QUAL_FLOOR:")
    for name in ["qf50_live", "qf46_proposed", "qf40_legacy"]:
        nets = {tag: scn[name]["net"] for tag, scn in results.items() if name in scn}
        deltas_vs_live = {tag: nets[tag] - results[tag]["qf50_live"]["net"] for tag in nets}
        agree = all(d > 0 for d in deltas_vs_live.values())
        flag = "★ AGREE BETTER" if agree else ("✓ agree neutral/worse" if all(d <= 0 for d in deltas_vs_live.values()) else "✗ DISAGREE")
        print(f"  {name:18s}  deltas vs qf50: " +
              "  ".join(f"{t}={d:+,.0f}" for t, d in deltas_vs_live.items()) + f"   {flag}")


if __name__ == "__main__":
    main()
