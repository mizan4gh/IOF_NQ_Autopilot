"""A/B test: v12.31 buggy risk-EMA vs v12.32 fixed (per-bar gated).

Runs backtest.py twice per contract with RISK_MODEL toggled, prints side-by-side
metrics. The thing we care about for the live-eval go/no-go: did the bug cause
SETUPs to be silently killed by the RM<C_RM_FLOOR gate (cpp line 3453)?

Cross-contract A/B convention applies (see [feedback_cross_contract_ab]) —
NQZ25 + NQM5 must agree on direction before shipping.

Usage:
    python backtest_v12_32_ab.py            # NQZ25 + NQM5 default
    python backtest_v12_32_ab.py NQZ25      # single contract
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
)

SCENARIOS = {
    "v12_31_buggy": {**_APEX, "RISK_MODEL": "v12_31_buggy"},
    "v12_32_fixed": {**_APEX, "RISK_MODEL": "v12_32_fixed"},
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
    print(f"  RISK_MODEL = {overrides['RISK_MODEL']}")
    bt = backtest.main()
    return out, bt.rm_gated


def summarize(csv_path, rm_gated):
    trades = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Event"] == "EXIT":
                try:
                    pnl = float(row["DayPnL"])
                except (ValueError, KeyError):
                    continue
                trades.append((pnl, row.get("Mode", ""), row.get("ExitReason", "")))

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

    return dict(
        n=n, wins=len(wins), losses=len(losses),
        total=total, pf=pf, wr=wr,
        expectancy=expectancy, max_dd=max_dd,
        rm_gated=rm_gated,
    )


def print_contract(tag, results):
    print("\n" + "=" * 78)
    print(f"COMPARISON: v12.31 buggy vs v12.32 fixed  ({tag})")
    print("=" * 78)
    cols = list(results.keys())
    print(f"{'Metric':<22} " + " ".join(f"{c:>16s}" for c in cols))
    print("-" * 78)
    rows = [
        ("setups_rm_gated",  lambda r: f"{r['rm_gated']:>16d}"),
        ("trades_taken",     lambda r: f"{r['n']:>16d}"),
        ("wins / losses",    lambda r: f"{r['wins']:>7d} / {r['losses']:<6d}"),
        ("win_rate",         lambda r: f"{r['wr']:>15.1f}%"),
        ("profit_factor",    lambda r: f"{r['pf']:>16.2f}"),
        ("total_pnl",        lambda r: f"${r['total']:>14.0f}"),
        ("expectancy",       lambda r: f"${r['expectancy']:>14.2f}"),
        ("max_drawdown",     lambda r: f"${r['max_dd']:>14.0f}"),
    ]
    for label, fmt in rows:
        print(f"{label:<22} " + " ".join(fmt(results[c]) for c in cols))

    buggy = results.get("v12_31_buggy")
    fixed = results.get("v12_32_fixed")
    if buggy and fixed:
        d_pnl = fixed["total"] - buggy["total"]
        d_trades = fixed["n"] - buggy["n"]
        d_gated = fixed["rm_gated"] - buggy["rm_gated"]
        print("-" * 78)
        print(f"DELTA (fixed - buggy):   trades={d_trades:+d}   gated={d_gated:+d}   pnl=${d_pnl:+.0f}")


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
            buggy = r["v12_31_buggy"]; fixed = r["v12_32_fixed"]
            d_pnl = fixed["total"] - buggy["total"]
            d_gated = buggy["rm_gated"] - fixed["rm_gated"]  # buggy gates MORE
            direction = "fixed_better" if d_pnl > 0 else "fixed_worse" if d_pnl < 0 else "tied"
            print(f"  {tag:<8}  pnl_delta=${d_pnl:+.0f}   gated_diff={d_gated:+d}   {direction}")
        dirs = [
            ("fixed_better" if all_results[t]["v12_32_fixed"]["total"] >
                              all_results[t]["v12_31_buggy"]["total"]
             else "fixed_worse")
            for t in all_results
        ]
        agree = len(set(dirs)) == 1
        print(f"\n  Cross-contract direction: {'AGREE — ship v12.32' if agree else 'DISAGREE — investigate before shipping'}")


if __name__ == "__main__":
    main()
