"""A/B test: M2-only QUAL_FLOOR=25 vs production QUAL_FLOOR=50.

Motivated by 2026-06-04 live session: 6 M2 setups (5 LONG, 1 SHORT) armed
and were skipped at qual 20-26 < floor 50. Question: would lowering the M2
floor to 25 unlock profitable trades, or just dump in low-conviction losers?

Critically: floor lowered only for M2 (sel==1). Other modes (M1/M3/M4/M6/M7)
stay at QUAL_FLOOR=50 so we're testing the M2 lever in isolation.

Reference: prior global qf40 test FAILED cross-contract
[[project_v1226_qualfloor_test]]. This is a NARROWER lever — only M2.

Cross-contract gate: must AGREE fixed_better across NQZ25 + NQM5 + NQH6
[[feedback_cross_contract_ab]] before any cpp change.

Usage:
    python backtest_m2_qual25_ab.py
    python backtest_m2_qual25_ab.py NQZ25   # single contract
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "NQM5.CME.scid",
    "NQH6":  SC_DATA / "NQH6.CME.scid",
}

_APEX = dict(
    C_COOL_TRADE=5, C_COOL_LOSS=10, C_COOL_STOP=10,
    C_OPEN_COOL=36, C_VCOOL_PAUSE=40,
    DAILY_LOSS=800.0, DAILY_PROF=1000.0,
    MAX_TRADES=3, ENTRY_ORD=2, NEWS_FILTER=1,
    RISK_MODEL="v12_32_fixed",
    M1_PULLBACK=2,
)

SCENARIOS = {
    "live_qf50":     {**_APEX, "QUAL_FLOOR": 50, "QUAL_FLOOR_M2": None},
    "m2only_qf25":   {**_APEX, "QUAL_FLOOR": 50, "QUAL_FLOOR_M2": 25},
}


def run_scenario(name, overrides, scid_path, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_backtest_m2qf_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid_path), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  QUAL_FLOOR={overrides['QUAL_FLOOR']} QUAL_FLOOR_M2={overrides['QUAL_FLOOR_M2']}")
    bt = backtest.main()
    return out


def summarize(csv_path):
    trades_all = []
    trades_m2 = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            try:
                pnl = float(row["DayPnL"])
            except (ValueError, KeyError):
                continue
            mode = row.get("Mode", "")
            trades_all.append(pnl)
            if mode == "M2":
                trades_m2.append(pnl)

    def stats(pnls):
        if not pnls:
            return dict(n=0, net=0.0, wr=0.0, pf=0.0)
        wins = sum(1 for p in pnls if p > 0)
        gross_w = sum(p for p in pnls if p > 0)
        gross_l = -sum(p for p in pnls if p <= 0)
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        return dict(n=len(pnls), net=sum(pnls), wr=wins / len(pnls), pf=pf)

    return dict(all=stats(trades_all), m2=stats(trades_m2))


def main():
    contracts = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in contracts:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}"); continue
        results[tag] = {}
        for name, ovr in SCENARIOS.items():
            out = run_scenario(name, ovr, scid, tag)
            results[tag][name] = summarize(out)

    print("\n" + "=" * 88)
    print(" M2-ONLY QUAL_FLOOR=25 vs LIVE QUAL_FLOOR=50 — ALL TRADES")
    print("=" * 88)
    print(f"  {'contract':8s} {'scenario':14s} {'n':>4s} {'WR':>7s} {'PF':>6s} {'Net':>11s}")
    for tag, scn in results.items():
        for name in ["live_qf50", "m2only_qf25"]:
            r = scn.get(name);
            if r is None: continue
            a = r["all"]
            print(f"  {tag:8s} {name:14s} {a['n']:>4} {a['wr']:>6.1%} {a['pf']:>6.2f} {a['net']:>+11,.0f}")
        print()

    print("=" * 88)
    print(" M2-ONLY (mode breakdown — what the lever actually unlocked)")
    print("=" * 88)
    print(f"  {'contract':8s} {'scenario':14s} {'M2_n':>5s} {'M2_WR':>7s} {'M2_PF':>6s} {'M2_Net':>11s}")
    for tag, scn in results.items():
        for name in ["live_qf50", "m2only_qf25"]:
            r = scn.get(name);
            if r is None: continue
            m = r["m2"]
            print(f"  {tag:8s} {name:14s} {m['n']:>5} {m['wr']:>6.1%} {m['pf']:>6.2f} {m['net']:>+11,.0f}")
        print()

    print("=" * 88)
    print(" CROSS-CONTRACT VERDICT (m2only_qf25 net delta vs live_qf50)")
    print("=" * 88)
    deltas = {}
    for tag, scn in results.items():
        if "live_qf50" in scn and "m2only_qf25" in scn:
            deltas[tag] = scn["m2only_qf25"]["all"]["net"] - scn["live_qf50"]["all"]["net"]
    for t, d in deltas.items():
        print(f"  {t}: {d:+,.0f}")
    if deltas:
        if all(d > 0 for d in deltas.values()):
            verdict = "AGREE BETTER — candidate for cpp ship"
        elif all(d < 0 for d in deltas.values()):
            verdict = "AGREE WORSE — falsified, do not ship"
        elif all(d == 0 for d in deltas.values()):
            verdict = "NO-OP — M2 floor=25 changed nothing (no new fires unlocked)"
        else:
            verdict = "DISAGREE — falsified per cross-contract gate, do not ship"
        print(f"\n  Verdict: {verdict}")


if __name__ == "__main__":
    main()
