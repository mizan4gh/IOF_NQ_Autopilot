"""A/B: C_OPEN_COOL=36 (live) vs C_OPEN_COOL=10 (proposed) on one .scid.

Apex production profile held constant. Prints side-by-side metrics.
Usage: python backtest_cool10_ab.py <path.scid>
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
SCID = Path(sys.argv[1] if len(sys.argv) > 1 else BASE / "NQZ25-CME.scid")

_APEX = dict(
    C_COOL_TRADE=5, C_COOL_LOSS=10, C_COOL_STOP=10,
    C_VCOOL_PAUSE=40,
    DAILY_LOSS=800.0, DAILY_PROF=1000.0,
    MAX_TRADES=3, ENTRY_ORD=2,
    NEWS_FILTER=1,
)

SCENARIOS = {
    "cool36_live":     {**_APEX, "C_OPEN_COOL": 36},
    "cool10_proposed": {**_APEX, "C_OPEN_COOL": 10},
}


def run_scenario(name, overrides):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    stem = SCID.stem
    out = BASE / f"IOF_NQ_backtest_{stem}_{name}.csv"
    sys.argv = ["backtest.py", str(SCID), str(out)]
    print(f"\n========== SCENARIO: {name} ==========")
    print(f"  overrides: {overrides}")
    backtest.main()
    return out


def summarize(csv_path):
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
    gross_win = sum(wins)
    gross_loss = sum(losses)
    total = gross_win + gross_loss
    pf = (gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf")
    wr = (len(wins) / n * 100) if n > 0 else 0.0
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    expectancy = (total / n) if n > 0 else 0.0

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p, _, _ in trades:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    mode_count = {}
    for _, m, _ in trades:
        mode_count[m] = mode_count.get(m, 0) + 1

    return dict(
        n=n, wins=len(wins), losses=len(losses),
        total=total, pf=pf, wr=wr,
        avg_win=avg_win, avg_loss=avg_loss,
        expectancy=expectancy, max_dd=max_dd,
        mode_count=mode_count,
    )


def main():
    print(f"\nA/B on {SCID.name}: C_OPEN_COOL 36 vs 10")
    results = {name: summarize(run_scenario(name, ov)) for name, ov in SCENARIOS.items()}

    print("\n" + "=" * 78)
    print(f"COMPARISON: cool A/B on {SCID.stem}  (Apex profile)")
    print("=" * 78)
    hdr = f"{'Metric':<18} " + " ".join(f"{n:>16s}" for n in results)
    print(hdr)
    print("-" * len(hdr))
    fields = [
        ("Trades (N)",       lambda r: f"{r['n']}"),
        ("Wins / Losses",    lambda r: f"{r['wins']}/{r['losses']}"),
        ("Win rate %",       lambda r: f"{r['wr']:.1f}"),
        ("Total PnL $",      lambda r: f"{r['total']:.0f}"),
        ("Profit factor",    lambda r: f"{r['pf']:.2f}"),
        ("Avg win $",        lambda r: f"{r['avg_win']:.0f}"),
        ("Avg loss $",       lambda r: f"{r['avg_loss']:.0f}"),
        ("Expectancy $/tr",  lambda r: f"{r['expectancy']:.0f}"),
        ("Max DD $",         lambda r: f"{r['max_dd']:.0f}"),
    ]
    for label, fn in fields:
        print(f"{label:<18} " + " ".join(f"{fn(r):>16s}" for r in results.values()))

    a, b = results["cool36_live"], results["cool10_proposed"]
    delta_pnl = b["total"] - a["total"]
    delta_n = b["n"] - a["n"]
    verdict = "test_better" if delta_pnl > 0 else "test_worse" if delta_pnl < 0 else "tied"
    print(f"\nDelta (cool10 - cool36): PnL=${delta_pnl:+.0f}  Trades={delta_n:+d}  -> {verdict}")

    print("\nMode mix:")
    all_modes = sorted({m for r in results.values() for m in r["mode_count"]})
    print(f"{'Mode':<6} " + " ".join(f"{n:>16s}" for n in results))
    for m in all_modes:
        print(f"{m:<6} " + " ".join(f"{r['mode_count'].get(m, 0):>16d}" for r in results.values()))


if __name__ == "__main__":
    main()
