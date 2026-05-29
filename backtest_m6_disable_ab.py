"""A/B test: M6 disable (DISABLE_MODES = {5}) vs baseline on bug-fixed harness.

Per [[project_m6_disable_improves_wr]] memory: M6-off looked good in
5k/scale-out config but production-config (3k single-lot) REVERSED it —
M6 was a winner on BOTH contracts. Don't disable.

Re-running with bug-fixed bar builder (NQH6 was price_scale=100 affected).
Cross-contract: NQZ25 + NQH6.
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
    LATE_ENTRY_GATE=1500,  # v12.36 default-on
)

SCENARIOS = {
    "baseline_m6_on":   {**_APEX, "DISABLE_MODES": set()},
    "m6_disabled":      {**_APEX, "DISABLE_MODES": {5}},
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
    print(f"  DISABLE_MODES = {overrides['DISABLE_MODES']}")
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
    print(" M6 DISABLE A/B (clean bar builder)")
    print("=" * 78)
    print(f"  {'contract':8s} {'scenario':18s} {'n':>3} {'WR':>7} {'PF':>6} {'Net':>11}")
    for tag, scn in results.items():
        for name in ["baseline_m6_on", "m6_disabled"]:
            r = scn[name]
            print(f"  {tag:8s} {name:18s} {r['n']:>3} {r['wr']:>6.1%} {r['pf']:>6.2f} {r['net']:>+11,.0f}")
        print()
    print(" Cross-contract deltas (m6_disabled − baseline):")
    deltas = {tag: results[tag]["m6_disabled"]["net"] - results[tag]["baseline_m6_on"]["net"]
              for tag in results}
    for tag, d in deltas.items():
        print(f"   {tag:8s} delta = ${d:+,.0f}")
    if all(d > 0 for d in deltas.values()):
        print("  ★ AGREE — disabling M6 helps both contracts. Reconsider M6 removal.")
    elif all(d < 0 for d in deltas.values()):
        print("  ✗ AGREE — M6 contributes positively on both. Don't disable (confirms memory).")
    else:
        print("  ✗ DISAGREE — keep M6 enabled per cross-contract rule.")


if __name__ == "__main__":
    main()
