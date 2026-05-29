"""A/B test: v12.35 baseline vs v12.36 late-entry gate (R2b: skip after 15:00 ET).

Per [[project_r2b_late_entry_skip]] — the only ML-derived hand-rule that
survived 3-contract expanding-month walk-forward on bug-fixed candidate data
(+$12,370 across 267 test candidates, +$875 across 18 historical prod fires).

This Python A/B is the cross-contract gate before the cpp change:
  - LATE_ENTRY_GATE = 1555  (default, off — equals FLATTEN_HHMM)
  - LATE_ENTRY_GATE = 1500  (R2b — skip new entries after 15:00 ET)

Per [[feedback_cross_contract_ab]]: NQZ25 + NQM5 must AGREE fixed_better
before adding `if (bar.HHMM >= 1500) return;` to IOF_NQ_Autopilot.cpp.

Usage:
    python backtest_v12_36_ab.py
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
    QUAL_FLOOR=50,  # current production
)

SCENARIOS = {
    "v12_35_baseline":     {**_APEX, "LATE_ENTRY_GATE": 1555},  # off
    "v12_36_late_gate":    {**_APEX, "LATE_ENTRY_GATE": 1500},  # R2b
}


def run_scenario(name, overrides, scid_path, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_backtest_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid_path), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  LATE_ENTRY_GATE = {overrides['LATE_ENTRY_GATE']}")
    bt = backtest.main()
    return out, bt.rm_gated


def summarize(csv_path):
    trades = []
    late_trades = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            try:
                pnl = float(row["DayPnL"])
            except (ValueError, KeyError):
                continue
            time = row.get("Time", "")
            mode = row.get("Mode", "")
            trades.append((pnl, mode, time))
            # Was this trade started at HHMM >= 1500?
            hhmm = 0
            try:
                hh, mm = time.split(":")[:2]
                hhmm = int(hh) * 100 + int(mm)
            except: pass
            if hhmm >= 1500:
                late_trades.append((pnl, mode))
    if not trades:
        return dict(n=0, net=0.0, wr=0.0, pf=0.0, late_n=0, late_pnl=0.0)
    pnls = [t[0] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    return dict(n=n, net=sum(pnls), wr=wins / n, pf=pf,
                late_n=len(late_trades), late_pnl=sum(p for p, _ in late_trades))


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

    print("\n" + "=" * 80)
    print(" v12.36 LATE-ENTRY GATE — A/B SUMMARY")
    print("=" * 80)
    print(f"  {'contract':8s} {'scenario':22s} {'n':>4} {'WR':>7} {'PF':>6} "
          f"{'Net':>11}  {'late_n':>7} {'late_pnl':>10}  {'rm_gtd':>7}")
    for tag, scn in results.items():
        for name in ["v12_35_baseline", "v12_36_late_gate"]:
            r = scn.get(name)
            if r is None: continue
            print(f"  {tag:8s} {name:22s} {r['n']:>4} {r['wr']:>6.1%} {r['pf']:>6.2f} "
                  f"{r['net']:>+11,.0f}  {r['late_n']:>7} {r['late_pnl']:>+10,.0f}  "
                  f"{r['rm_gated']:>7}")
        print()

    print(" Cross-contract verdict:")
    deltas = {}
    for tag in results:
        base = results[tag]["v12_35_baseline"]["net"]
        gate = results[tag]["v12_36_late_gate"]["net"]
        deltas[tag] = gate - base
        print(f"   {tag:8s} delta = ${deltas[tag]:+,.0f}  "
              f"(baseline ${base:+,.0f} -> gate ${gate:+,.0f})")
    if all(d > 0 for d in deltas.values()):
        print("\n  ★ AGREE fixed_better — safe to add `if (bar.HHMM >= 1500) return;` in cpp v12.36")
    elif all(d >= 0 for d in deltas.values()):
        print("\n  ✓ AGREE neutral-or-better — rule does no harm. Ship if mechanism is sound.")
    elif all(d < 0 for d in deltas.values()):
        print("\n  ✗ AGREE fixed_worse — DO NOT ship.")
    else:
        print("\n  ✗ DISAGREE — do not ship per [[feedback_cross_contract_ab]].")


if __name__ == "__main__":
    main()
