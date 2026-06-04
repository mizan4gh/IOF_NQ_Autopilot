"""A/B sweep: C_OPEN_COOL ∈ {20, 25, 30, 36} on production config.

Production profile (matches run_prod.py): 3k vol bars, single-lot,
DAILY_LOSS=$800, DAILY_PROF=0 (no profit cap), NEWS_FILTER=1, M4 on.
Only C_OPEN_COOL varies. Apex live = 36 (baseline).

Hypothesis: lower open-cooldown lets early-RTH M4 sweeps through that the
current 36-min lockout blanks. Watch for: more trades, WR holding,
no MaxDD blow-out.

Usage: python backtest_opencool_ab.py <path.scid>
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
SCID = Path(sys.argv[1] if len(sys.argv) > 1 else BASE / "NQZ25-CME.scid")

_PROD = dict(
    DAILY_LOSS=800.0,
    DAILY_PROF=0.0,
    NEWS_FILTER=1,
    TARGET_VOL=3000,
    SCALE_OUT=False,
)

SCENARIOS = {
    "cool36_live":     {**_PROD, "C_OPEN_COOL": 36},
    "cool30":          {**_PROD, "C_OPEN_COOL": 30},
    "cool25":          {**_PROD, "C_OPEN_COOL": 25},
    "cool20":          {**_PROD, "C_OPEN_COOL": 20},
}
BASELINE = "cool36_live"


def run_scenario(name, overrides):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    stem = SCID.stem
    out = BASE / f"IOF_NQ_opencool_{stem}_{name}.csv"
    sys.argv = ["backtest.py", str(SCID), str(out)]
    print(f"\n========== SCENARIO: {name} ==========")
    print(f"  overrides: {overrides}")
    backtest.main()
    return out


def summarize(csv_path):
    trades = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            try:
                tot = float(row["TotalPnL"])
            except (ValueError, KeyError):
                continue
            trades.append((tot, row.get("Mode", ""), row.get("ExitReason", "")))

    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, losses=0, total=0.0, pf=0.0, wr=0.0,
                    avg_win=0.0, avg_loss=0.0, expectancy=0.0, max_dd=0.0,
                    mode_count={})

    per_trade = [trades[0][0]] + [trades[i][0] - trades[i-1][0] for i in range(1, n)]
    wins = [p for p in per_trade if p > 0]
    losses = [p for p in per_trade if p < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    total = trades[-1][0]
    pf = (gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf")
    wr = (len(wins) / n * 100)
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    expectancy = total / n

    peak, max_dd = 0.0, 0.0
    for tot, _, _ in trades:
        peak = max(peak, tot)
        max_dd = min(max_dd, tot - peak)

    mode_count = {}
    for _, m, _ in trades:
        mode_count[m] = mode_count.get(m, 0) + 1

    return dict(n=n, wins=len(wins), losses=len(losses),
                total=total, pf=pf, wr=wr,
                avg_win=avg_win, avg_loss=avg_loss,
                expectancy=expectancy, max_dd=max_dd,
                mode_count=mode_count)


def main():
    print(f"\nA/B sweep on {SCID.name}: C_OPEN_COOL {{20, 25, 30, 36}}")
    results = {name: summarize(run_scenario(name, ov)) for name, ov in SCENARIOS.items()}

    print("\n" + "=" * 90)
    print(f"COMPARISON: open-cooldown sweep on {SCID.stem}  (prod config)")
    print("=" * 90)
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

    base = results[BASELINE]
    print(f"\nDelta vs {BASELINE}:")
    for name, r in results.items():
        if name == BASELINE:
            continue
        d_pnl = r["total"] - base["total"]
        d_n = r["n"] - base["n"]
        d_dd = r["max_dd"] - base["max_dd"]
        verdict = "test_better" if d_pnl > 0 else "test_worse" if d_pnl < 0 else "tied"
        print(f"  {name:<14}  PnL ${d_pnl:+8,.0f}  Trades {d_n:+3d}  "
              f"MaxDD ${d_dd:+8,.0f}  -> {verdict}")

    print("\nMode mix:")
    all_modes = sorted({m for r in results.values() for m in r["mode_count"]})
    print(f"{'Mode':<6} " + " ".join(f"{n:>16s}" for n in results))
    for m in all_modes:
        print(f"{m:<6} " + " ".join(f"{r['mode_count'].get(m, 0):>16d}" for r in results.values()))


if __name__ == "__main__":
    main()
