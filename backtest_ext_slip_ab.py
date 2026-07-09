"""Overnight-slippage sensitivity for the extended session 00:00-15:45.

Follow-up to backtest_ext_session_ab.py: the MT=6 extended session passed 3/3 on
P&L, but the fill model (marketable limit +2t, exact stop fills) has only been
sanity-checked against RTH behavior. This sweep charges OVERNIGHT_SLIP_TICKS
extra ticks per side on fills before 09:35 ET (entry + market-type exits; T1/T2
limit fills exempt) and asks how many ticks of thin-book reality the overnight
edge can absorb.

Run at MT=6 only: at MT=1 the extended session was already ~tied on NQZ25 with
zero haircut, so any positive slip fails it trivially — the open question is
whether the MT=6 pass survives.

  prod_session  RTH 09:35-15:55 baseline (knob irrelevant: no overnight fills)
  ext_slip0     00:00-15:45, no haircut   (= ext_session from the mt6 run)
  ext_slip1     +1 tick/side overnight    ($5/side)   mild
  ext_slip2     +2 ticks/side overnight   ($10/side)  realistic-conservative
  ext_slip3     +3 ticks/side overnight   ($15/side)  harsh

Usage: python backtest_ext_slip_ab.py <path.scid>
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
    MAX_TRADES=6,
    DAILY_LOSS=800.0,
    DAILY_PROF=0.0,
)
_EXT = {"RTH_OPEN": 0, "FLATTEN_HHMM": 1545, "LATE_ENTRY_GATE": 1545,
        "C_OPEN_COOL": 5}

SCENARIOS = {
    "prod_session": {**_PROD, "RTH_OPEN": 935, "FLATTEN_HHMM": 1555,
                     "LATE_ENTRY_GATE": 1555, "C_OPEN_COOL": 36},
    "ext_slip0":    {**_PROD, **_EXT, "OVERNIGHT_SLIP_TICKS": 0.0},
    "ext_slip1":    {**_PROD, **_EXT, "OVERNIGHT_SLIP_TICKS": 1.0},
    "ext_slip2":    {**_PROD, **_EXT, "OVERNIGHT_SLIP_TICKS": 2.0},
    "ext_slip3":    {**_PROD, **_EXT, "OVERNIGHT_SLIP_TICKS": 3.0},
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

    out = BASE / f"IOF_NQ_extslip_{SCID.stem}_{name}.csv"
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
            trades.append((tot, row.get("Mode", ""), row.get("Time", "")))

    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, losses=0, total=0.0, pf=0.0, wr=0.0,
                    expectancy=0.0, max_dd=0.0, overnight=0)

    per_trade = [trades[0][0]] + [trades[i][0] - trades[i-1][0] for i in range(1, n)]
    wins = [p for p in per_trade if p > 0]
    losses = [p for p in per_trade if p < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    total = trades[-1][0]

    peak, max_dd = 0.0, 0.0
    for tot, _, _ in trades:
        peak = max(peak, tot)
        max_dd = min(max_dd, tot - peak)

    overnight = 0
    for _, _, t in trades:
        try:
            if int(t[:2]) * 100 + int(t[3:5]) < 935:
                overnight += 1
        except (ValueError, IndexError):
            pass

    return dict(n=n, wins=len(wins), losses=len(losses), total=total,
                pf=(gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf"),
                wr=(len(wins) / n * 100), expectancy=total / n,
                max_dd=max_dd, overnight=overnight)


def main():
    print(f"\nOvernight-slip sweep on {SCID.name} (MT=6 extended session)")
    results = {name: summarize(run_scenario(name, ov)) for name, ov in SCENARIOS.items()}

    print("\n" + "=" * 95)
    print(f"COMPARISON on {SCID.stem}")
    print("=" * 95)
    hdr = f"{'Metric':<18} " + " ".join(f"{n:>14s}" for n in results)
    print(hdr)
    print("-" * len(hdr))
    fields = [
        ("Trades (N)",       lambda r: f"{r['n']}"),
        ("Pre-9:35 exits",   lambda r: f"{r['overnight']}"),
        ("Win rate %",       lambda r: f"{r['wr']:.1f}"),
        ("Total PnL $",      lambda r: f"{r['total']:.0f}"),
        ("Profit factor",    lambda r: f"{r['pf']:.2f}"),
        ("Expectancy $/tr",  lambda r: f"{r['expectancy']:.0f}"),
        ("Max DD $",         lambda r: f"{r['max_dd']:.0f}"),
    ]
    for label, fn in fields:
        print(f"{label:<18} " + " ".join(f"{fn(r):>14s}" for r in results.values()))

    base = results[BASELINE]
    print(f"\nDelta vs {BASELINE}:")
    for name, r in results.items():
        if name == BASELINE:
            continue
        d_pnl = r["total"] - base["total"]
        verdict = "test_better" if d_pnl > 0 else "test_worse" if d_pnl < 0 else "tied"
        print(f"  {name:<12}  PnL ${d_pnl:+8,.0f}  Trades {r['n'] - base['n']:+4d}  "
              f"MaxDD ${r['max_dd'] - base['max_dd']:+8,.0f}  -> {verdict}")


if __name__ == "__main__":
    main()
