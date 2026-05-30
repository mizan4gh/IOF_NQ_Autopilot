"""A/B test: bump M4 minimum sc gate by +1 — i.e. skip the marginal-score M4 setups.

Motivation 2026-05-29: live -$805 M4 stop today (Score=5 minimum) prompted the
question "every M4 stop-out, can you optimize or remove?" Prior attempts have
already failed cross-contract:
  - wider M4 stop (project_m4_stop_overshoot)
  - delayed/retest entry (project_m4_stop_overshoot)
  - per-mode M4 quality floor at 40 (project_m4_floor_40_failed)
  - M4 disable entirely (project_m6_disable_improves_wr — reversed in prod-config)

This test attacks a DIFFERENT lever — the sc-component count gate (m4_min in
backtest.py:1305). Baseline today: m4_min = 3 if |div|>=2 else 4. Test bumps
both by +1 (→ 4 / 5), filtering the lowest-sc M4 arms.

Per [[feedback_cross_contract_ab]]: NQZ25 + NQH6 must AGREE fixed_better
(or at least neither worse) before any cpp gate change.

Usage:
    python backtest_m4_score5_ab.py
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQH6":  SC_DATA / "NQH6.CME.scid",   # genuine Mar-2026 data per project_nqh26_duplicate_data
}

_APEX = dict(
    C_COOL_TRADE=5, C_COOL_LOSS=10, C_COOL_STOP=10,
    C_OPEN_COOL=36, C_VCOOL_PAUSE=40,
    DAILY_LOSS=800.0, DAILY_PROF=1000.0,
    MAX_TRADES=3, ENTRY_ORD=2, NEWS_FILTER=1,
    RISK_MODEL="v12_32_fixed",
    M1_PULLBACK=2,           # match current production (v12.34 dip+reclaim)
    QUAL_FLOOR=50,           # current production
)

SCENARIOS = {
    "baseline_m4_bump0":    {**_APEX, "M4_SC_BUMP": 0},  # current production
    "tight_m4_bump1":       {**_APEX, "M4_SC_BUMP": 1},  # skip marginal-sc M4s
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
    print(f"  M4_SC_BUMP = {overrides['M4_SC_BUMP']}")
    bt = backtest.main()
    return out, bt.rm_gated


def summarize(csv_path):
    trades = []
    m4_trades = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            try:
                pnl = float(row["DayPnL"])
            except (ValueError, KeyError):
                continue
            mode = row.get("Mode", "")
            trades.append((pnl, mode))
            if mode == "M4":
                m4_trades.append(pnl)
    if not trades:
        return dict(n=0, net=0.0, wr=0.0, pf=0.0, m4_n=0, m4_pnl=0.0, m4_wr=0.0)
    pnls = [t[0] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    m4_wins = sum(1 for p in m4_trades if p > 0)
    m4_wr = m4_wins / len(m4_trades) if m4_trades else 0.0
    return dict(n=n, net=sum(pnls), wr=wins / n, pf=pf,
                m4_n=len(m4_trades), m4_pnl=sum(m4_trades), m4_wr=m4_wr)


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

    print("\n" + "=" * 95)
    print(" M4 SC-BUMP A/B SUMMARY  (skip lowest-sc M4 setups)")
    print("=" * 95)
    print(f"  {'contract':8s} {'scenario':22s} {'n':>4} {'WR':>6} {'PF':>6} "
          f"{'Net':>11}  {'m4_n':>5} {'m4_WR':>6} {'m4_pnl':>10}  {'rm_gtd':>7}")
    for tag, scn in results.items():
        for name in ["baseline_m4_bump0", "tight_m4_bump1"]:
            r = scn.get(name)
            if r is None: continue
            print(f"  {tag:8s} {name:22s} {r['n']:>4} {r['wr']:>5.1%} {r['pf']:>6.2f} "
                  f"{r['net']:>+11,.0f}  {r['m4_n']:>5} {r['m4_wr']:>5.1%} "
                  f"{r['m4_pnl']:>+10,.0f}  {r['rm_gated']:>7}")
        print()

    print(" Cross-contract verdict:")
    deltas = {}
    for tag in results:
        base = results[tag]["baseline_m4_bump0"]["net"]
        tght = results[tag]["tight_m4_bump1"]["net"]
        deltas[tag] = tght - base
        print(f"   {tag:8s} delta = ${deltas[tag]:+,.0f}  "
              f"(baseline ${base:+,.0f} -> tight ${tght:+,.0f})")
    vals = list(deltas.values())
    if not vals:
        print("\n  (no results)")
    elif all(d > 0 for d in vals):
        print("\n  AGREE fixed_better -- candidate for cpp ship.")
    elif all(d >= 0 for d in vals):
        print("\n  AGREE neutral-or-better -- does no harm. Mechanism check needed.")
    elif all(d < 0 for d in vals):
        print("\n  AGREE fixed_worse -- DO NOT ship; M4 marginal-sc setups are net positive.")
    else:
        print("\n  DISAGREE -- do not ship per [[feedback_cross_contract_ab]].")


if __name__ == "__main__":
    main()
