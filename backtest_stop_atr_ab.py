"""Full backtest A/B: widen C_STOP_ATR (M4 stop hypothesis from clean audit).

M4 overshoot audit on bug-fixed data showed wider stops BETTER on both NQZ25
and NQH6 in T1-capped expectancy. This is the production-config validation:
does the audit's expectancy lift translate to net P&L lift under full trade
management (T2, trail, breakeven, daily-loss cap)?

Caveat: wider stops require raising C_STOP_CL ceiling too — current 40-pt
ceiling clamps anything beyond C_STOP_ATR ~= 1.33 (at typical ATR=30).

Scenarios:
  baseline:  C_STOP_ATR=1.2,  C_STOP_CL=40  (current production)
  atr_145:   C_STOP_ATR=1.45, C_STOP_CL=50  (audit's smallest confident lift)
  atr_170:   C_STOP_ATR=1.7,  C_STOP_CL=60  (audit's +0.5*ATR equivalent)
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
    LATE_ENTRY_GATE=1500,   # v12.36 deployed
)

SCENARIOS = {
    "baseline_atr12":  {**_APEX, "C_STOP_ATR": 1.2,  "C_STOP_CL": 40.0},
    "atr_145":         {**_APEX, "C_STOP_ATR": 1.45, "C_STOP_CL": 50.0},
    "atr_170":         {**_APEX, "C_STOP_ATR": 1.7,  "C_STOP_CL": 60.0},
}


def run_scenario(name, overrides, scid_path, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_backtest_{tag}_stop_{name}.csv"
    sys.argv = ["backtest.py", str(scid_path), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  C_STOP_ATR={overrides['C_STOP_ATR']}  C_STOP_CL={overrides['C_STOP_CL']}")
    backtest.main()
    return out


def summarize(p):
    trades = []
    m4_trades = []
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            if r["Event"] != "EXIT": continue
            try: pnl = float(r["DayPnL"])
            except: continue
            trades.append(pnl)
            if r.get("Mode") == "M4": m4_trades.append(pnl)
    if not trades: return dict(n=0, net=0, wr=0, pf=0, m4_n=0, m4_net=0)
    wins = sum(1 for p in trades if p > 0)
    gw = sum(p for p in trades if p > 0); gl = -sum(p for p in trades if p <= 0)
    return dict(n=len(trades), wr=wins/len(trades),
                pf=gw/gl if gl > 0 else float("inf"),
                net=sum(trades),
                m4_n=len(m4_trades), m4_net=sum(m4_trades))


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

    print("\n" + "=" * 84)
    print(" C_STOP_ATR A/B — full production backtest (clean bar builder)")
    print("=" * 84)
    print(f"  {'contract':8s} {'scenario':18s} {'n':>3} {'WR':>7} {'PF':>6} {'Net':>11}  {'M4_n':>5} {'M4_net':>10}")
    for tag, scn in results.items():
        for name in ["baseline_atr12", "atr_145", "atr_170"]:
            r = scn[name]
            print(f"  {tag:8s} {name:18s} {r['n']:>3} {r['wr']:>6.1%} {r['pf']:>6.2f} {r['net']:>+11,.0f}  "
                  f"{r['m4_n']:>5} {r['m4_net']:>+10,.0f}")
        print()

    print(" Cross-contract verdict per scenario (vs baseline):")
    for sname in ["atr_145", "atr_170"]:
        deltas = {tag: results[tag][sname]["net"] - results[tag]["baseline_atr12"]["net"]
                  for tag in results}
        for tag, d in deltas.items():
            print(f"   {sname}  {tag:8s} delta = ${d:+,.0f}")
        if all(d > 0 for d in deltas.values()):
            print(f"   ★ {sname}: AGREE fixed_better — candidate for v12.37 cpp change")
        elif all(d < 0 for d in deltas.values()):
            print(f"   ✗ {sname}: AGREE fixed_worse — don't deploy")
        else:
            print(f"   ✗ {sname}: DISAGREE — don't deploy per cross-contract rule")
        print()


if __name__ == "__main__":
    main()
