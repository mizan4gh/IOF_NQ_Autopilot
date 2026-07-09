"""A/B: extended session 00:00-15:45 ET (5-bar open cooldown) vs prod 09:35-15:55.

User request 2026-07-08: "backtest 12 am to 3:45 PM, 5 bar cooldown".
Scenarios (all at live prod config MT=1 / DL=$800 / news on, per
[[project_mt3_dl800_config]]):

  prod_session   RTH 09:35-15:55, C_OPEN_COOL=36        (baseline = live)
  ext_session    RTH 00:00-15:45, C_OPEN_COOL=5
  ext_allcool5   same + C_COOL_LOSS=5, C_COOL_STOP=5    (uniform 5-bar cooldowns)

Session reset is keyed on ET date_tag change (midnight), so the 00:00 open
aligns with the engine's day reset. LATE_ENTRY_GATE is assigned from
FLATTEN_HHMM at import time -> must be overridden explicitly.

Usage: python backtest_ext_session_ab.py <path.scid>
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
SCID = Path(sys.argv[1] if len(sys.argv) > 1 else BASE / "F.US.ENQZ25.scid")

_PROD = dict(
    NEWS_FILTER=1,
    TARGET_VOL=3000,
    SCALE_OUT=False,
    MAX_TRADES=1,
    DAILY_LOSS=800.0,
    DAILY_PROF=0.0,
)
TAG = "mt1"  # output-file tag so MT=1 and MT=6 runs don't clobber each other

SCENARIOS = {
    "prod_session": {**_PROD, "RTH_OPEN": 935, "FLATTEN_HHMM": 1555,
                     "LATE_ENTRY_GATE": 1555, "C_OPEN_COOL": 36},
    "ext_session":  {**_PROD, "RTH_OPEN": 0, "FLATTEN_HHMM": 1545,
                     "LATE_ENTRY_GATE": 1545, "C_OPEN_COOL": 5},
    "ext_allcool5": {**_PROD, "RTH_OPEN": 0, "FLATTEN_HHMM": 1545,
                     "LATE_ENTRY_GATE": 1545, "C_OPEN_COOL": 5,
                     "C_COOL_LOSS": 5, "C_COOL_STOP": 5},
}
BASELINE = "prod_session"


def run_scenario(name, overrides):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_extsess_{TAG}_{SCID.stem}_{name}.csv"
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
            trades.append((tot, row.get("Mode", ""), row.get("ExitReason", ""),
                           row.get("Time", "")))

    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, losses=0, total=0.0, pf=0.0, wr=0.0,
                    avg_win=0.0, avg_loss=0.0, expectancy=0.0, max_dd=0.0,
                    mode_count={}, overnight=0)

    per_trade = [trades[0][0]] + [trades[i][0] - trades[i-1][0] for i in range(1, n)]
    wins = [p for p in per_trade if p > 0]
    losses = [p for p in per_trade if p < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    total = trades[-1][0]
    pf = (gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf")

    peak, max_dd = 0.0, 0.0
    for tot, _, _, _ in trades:
        peak = max(peak, tot)
        max_dd = min(max_dd, tot - peak)

    mode_count = {}
    overnight = 0
    for _, m, _, t in trades:
        mode_count[m] = mode_count.get(m, 0) + 1
        # exit-time HH:MM:SS before 09:35 -> overnight/pre-RTH trade
        try:
            hh, mm = int(t[:2]), int(t[3:5])
            if hh * 100 + mm < 935:
                overnight += 1
        except (ValueError, IndexError):
            pass

    return dict(n=n, wins=len(wins), losses=len(losses),
                total=total, pf=pf, wr=(len(wins) / n * 100),
                avg_win=(gross_win / len(wins)) if wins else 0.0,
                avg_loss=(gross_loss / len(losses)) if losses else 0.0,
                expectancy=total / n, max_dd=max_dd,
                mode_count=mode_count, overnight=overnight)


def main():
    print(f"\nA/B on {SCID.name}: prod session vs extended 00:00-15:45")
    results = {name: summarize(run_scenario(name, ov)) for name, ov in SCENARIOS.items()}

    print("\n" + "=" * 80)
    print(f"COMPARISON on {SCID.stem}")
    print("=" * 80)
    hdr = f"{'Metric':<18} " + " ".join(f"{n:>16s}" for n in results)
    print(hdr)
    print("-" * len(hdr))
    fields = [
        ("Trades (N)",       lambda r: f"{r['n']}"),
        ("Pre-9:35 exits",   lambda r: f"{r['overnight']}"),
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
        verdict = "test_better" if d_pnl > 0 else "test_worse" if d_pnl < 0 else "tied"
        print(f"  {name:<14}  PnL ${d_pnl:+8,.0f}  Trades {r['n'] - base['n']:+4d}  "
              f"MaxDD ${r['max_dd'] - base['max_dd']:+8,.0f}  -> {verdict}")

    print("\nMode mix:")
    all_modes = sorted({m for r in results.values() for m in r["mode_count"]})
    print(f"{'Mode':<6} " + " ".join(f"{n:>16s}" for n in results))
    for m in all_modes:
        print(f"{m:<6} " + " ".join(f"{r['mode_count'].get(m, 0):>16d}" for r in results.values()))


if __name__ == "__main__":
    main()
