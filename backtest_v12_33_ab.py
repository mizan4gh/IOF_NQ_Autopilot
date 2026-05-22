"""A/B test: v12.32 baseline (no pullback gate) vs v12.33 wick-reject pullback.

Runs backtest.py twice per contract with M1_PULLBACK toggled, prints side-by-side
metrics. The thing we care about for the live-eval go/no-go: does requiring a
wick-rejection bar at VWAP improve M1 quality enough to justify the lost setups?

Cross-contract A/B convention applies (see [feedback_cross_contract_ab]) —
NQZ25 + NQM5 must agree on direction before shipping. Only flip cpp
sc.Input[16] IN_M1_PULLBACK to 1 in the live chartbook if both contracts
return fixed_better.

Usage:
    python backtest_v12_33_ab.py            # NQZ25 + NQM5 default
    python backtest_v12_33_ab.py NQZ25      # single contract
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

# Apex eval baseline — matches v12.32 SetDefaults
_APEX = dict(
    C_COOL_TRADE=5, C_COOL_LOSS=10, C_COOL_STOP=10,
    C_OPEN_COOL=36, C_VCOOL_PAUSE=40,
    DAILY_LOSS=800.0, DAILY_PROF=1000.0,
    MAX_TRADES=3, ENTRY_ORD=2, NEWS_FILTER=1,
    RISK_MODEL="v12_32_fixed",
)

SCENARIOS = {
    "v12_32_baseline":  {**_APEX, "M1_PULLBACK": 0},
    "v12_33_pullback":  {**_APEX, "M1_PULLBACK": 1},
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
    print(f"  M1_PULLBACK = {overrides['M1_PULLBACK']}")
    bt = backtest.main()
    return out, bt.rm_gated


def summarize(csv_path, rm_gated):
    trades, m1_trades = [], []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Event"] != "EXIT":
                continue
            try:
                pnl = float(row["DayPnL"])
            except (ValueError, KeyError):
                continue
            mode = row.get("Mode", "")
            trades.append((pnl, mode, row.get("ExitReason", "")))
            if mode == "M1":
                m1_trades.append(pnl)

    n = len(trades)
    wins = [p for p, _, _ in trades if p > 0]
    losses = [p for p, _, _ in trades if p < 0]
    gross_win = sum(wins); gross_loss = sum(losses)
    total = gross_win + gross_loss
    pf = (gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf")
    wr = (len(wins) / n * 100) if n > 0 else 0.0
    expectancy = (total / n) if n > 0 else 0.0
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p, _, _ in trades:
        equity += p; peak = max(peak, equity); max_dd = min(max_dd, equity - peak)

    m1_n = len(m1_trades)
    m1_wins = sum(1 for p in m1_trades if p > 0)
    m1_pnl = sum(m1_trades)
    m1_wr = (m1_wins / m1_n * 100) if m1_n > 0 else 0.0

    return dict(
        n=n, wins=len(wins), losses=len(losses),
        total=total, pf=pf, wr=wr,
        expectancy=expectancy, max_dd=max_dd,
        rm_gated=rm_gated,
        m1_n=m1_n, m1_wr=m1_wr, m1_pnl=m1_pnl,
    )


def print_contract(tag, results):
    print("\n" + "=" * 78)
    print(f"COMPARISON: v12.32 baseline vs v12.33 pullback  ({tag})")
    print("=" * 78)
    cols = list(results.keys())
    print(f"{'Metric':<22} " + " ".join(f"{c:>18s}" for c in cols))
    print("-" * 78)
    rows = [
        ("trades_taken",     lambda r: f"{r['n']:>18d}"),
        ("wins / losses",    lambda r: f"{r['wins']:>8d} / {r['losses']:<7d}"),
        ("win_rate",         lambda r: f"{r['wr']:>17.1f}%"),
        ("profit_factor",    lambda r: f"{r['pf']:>18.2f}"),
        ("total_pnl",        lambda r: f"${r['total']:>16.0f}"),
        ("expectancy",       lambda r: f"${r['expectancy']:>16.2f}"),
        ("max_drawdown",     lambda r: f"${r['max_dd']:>16.0f}"),
        ("m1_trades",        lambda r: f"{r['m1_n']:>18d}"),
        ("m1_win_rate",      lambda r: f"{r['m1_wr']:>17.1f}%"),
        ("m1_pnl",           lambda r: f"${r['m1_pnl']:>16.0f}"),
        ("setups_rm_gated",  lambda r: f"{r['rm_gated']:>18d}"),
    ]
    for label, fmt in rows:
        print(f"{label:<22} " + " ".join(fmt(results[c]) for c in cols))

    base = results.get("v12_32_baseline")
    fixed = results.get("v12_33_pullback")
    if base and fixed:
        d_pnl = fixed["total"] - base["total"]
        d_trades = fixed["n"] - base["n"]
        d_m1_n = fixed["m1_n"] - base["m1_n"]
        d_m1_pnl = fixed["m1_pnl"] - base["m1_pnl"]
        d_m1_wr = fixed["m1_wr"] - base["m1_wr"]
        print("-" * 78)
        print(f"DELTA (v12.33 - v12.32):  trades={d_trades:+d}  pnl=${d_pnl:+.0f}")
        print(f"  M1 only:                trades={d_m1_n:+d}  pnl=${d_m1_pnl:+.0f}  wr={d_m1_wr:+.1f}%")


def main():
    if len(sys.argv) > 1:
        contracts = {sys.argv[1]: CONTRACTS[sys.argv[1]]}
    else:
        contracts = CONTRACTS

    all_results = {}
    for tag, scid in contracts.items():
        if not scid.exists():
            print(f"!! SKIP {tag}: {scid} not found")
            continue
        results = {}
        for name, ov in SCENARIOS.items():
            out, gated = run_scenario(name, ov, scid, tag)
            results[name] = summarize(out, gated)
        all_results[tag] = results
        print_contract(tag, results)

    if len(all_results) >= 2:
        print("\n" + "=" * 78)
        print("CROSS-CONTRACT VERDICT")
        print("=" * 78)
        for tag, r in all_results.items():
            base = r["v12_32_baseline"]; fixed = r["v12_33_pullback"]
            d_pnl = fixed["total"] - base["total"]
            d_m1 = fixed["m1_pnl"] - base["m1_pnl"]
            direction = "fixed_better" if d_pnl > 0 else "fixed_worse" if d_pnl < 0 else "tied"
            print(f"  {tag:<8}  pnl_delta=${d_pnl:+.0f}   m1_pnl_delta=${d_m1:+.0f}   {direction}")
        dirs = [
            ("fixed_better" if all_results[t]["v12_33_pullback"]["total"] >
                              all_results[t]["v12_32_baseline"]["total"]
             else "fixed_worse")
            for t in all_results
        ]
        if len(set(dirs)) == 1 and dirs[0] == "fixed_better":
            verdict = "AGREE fixed_better — safe to flip IN_M1_PULLBACK=1 live"
        elif len(set(dirs)) == 1 and dirs[0] == "fixed_worse":
            verdict = "AGREE fixed_worse — DO NOT ship; keep IN_M1_PULLBACK=0"
        else:
            verdict = "DISAGREE — keep IN_M1_PULLBACK=0; investigate before shipping"
        print(f"\n  Cross-contract direction: {verdict}")


if __name__ == "__main__":
    main()
