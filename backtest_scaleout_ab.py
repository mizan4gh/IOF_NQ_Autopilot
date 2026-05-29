"""A/B test: SCALE_OUT (2-lot scale at T1) vs baseline single-lot on bug-fixed harness.

Per [[project_scaleout_2lot_ab]] memory: rejected at production-config (3k
single-lot) because M6 trailing winners get capped at T1. Cross-contract on
NQZ25 + NQH6. Re-running with bug-fixed bar builder.
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQH6":  SC_DATA / "NQH6.CME.scid",
}

_APEX = dict(
    C_COOL_TRADE=5, C_COOL_LOSS=10, C_COOL_STOP=10,
    C_OPEN_COOL=10, C_VCOOL_PAUSE=40,
    DAILY_LOSS=800.0, DAILY_PROF=1000.0,
    MAX_TRADES=3, ENTRY_ORD=2, NEWS_FILTER=1,
    RISK_MODEL="v12_32_fixed",
    M1_PULLBACK=2, QUAL_FLOOR=50,
    LATE_ENTRY_GATE=1500,
)

SCENARIOS = {
    "baseline_single_lot": {**_APEX, "SCALE_OUT": False},
    "scale_out_2lot":      {**_APEX, "SCALE_OUT": True},
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
    print(f"  SCALE_OUT = {overrides['SCALE_OUT']}")
    backtest.main()
    return out


def summarize(p):
    trades = []
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            if r["Event"] != "EXIT": continue
            try: trades.append(float(r["DayPnL"]))
            except: pass
    if not trades: return dict(n=0, net=0, wr=0, pf=0)
    wins = sum(1 for p in trades if p > 0)
    gw = sum(p for p in trades if p > 0); gl = -sum(p for p in trades if p <= 0)
    return dict(n=len(trades), wr=wins/len(trades),
                pf=gw/gl if gl > 0 else float("inf"), net=sum(trades))


def main():
    contracts = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in contracts:
        scid = CONTRACTS[tag]
        if not scid.exists(): print(f"missing: {scid}"); continue
        results[tag] = {}
        for name, ovr in SCENARIOS.items():
            out = run_scenario(name, ovr, scid, tag)
            results[tag][name] = summarize(out)

    print("\n" + "=" * 78)
    print(" 2-LOT SCALE-OUT A/B (clean bar builder)")
    print("=" * 78)
    print(f"  {'contract':8s} {'scenario':22s} {'n':>3} {'WR':>7} {'PF':>6} {'Net':>11}")
    for tag, scn in results.items():
        for name in ["baseline_single_lot", "scale_out_2lot"]:
            r = scn[name]
            print(f"  {tag:8s} {name:22s} {r['n']:>3} {r['wr']:>6.1%} {r['pf']:>6.2f} {r['net']:>+11,.0f}")
        print()
    print(" Cross-contract deltas (scale_out − baseline):")
    deltas = {tag: results[tag]["scale_out_2lot"]["net"] - results[tag]["baseline_single_lot"]["net"]
              for tag in results}
    for tag, d in deltas.items():
        print(f"   {tag:8s} delta = ${d:+,.0f}")
    if all(d > 0 for d in deltas.values()):
        print("  ★ AGREE — scale-out helps both. Reconsider deploying.")
    elif all(d < 0 for d in deltas.values()):
        print("  ✗ AGREE — scale-out hurts both (confirms memory).")
    else:
        print("  ✗ DISAGREE — don't deploy.")


if __name__ == "__main__":
    main()
