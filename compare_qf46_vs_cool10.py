#!/usr/bin/env python3
"""Compare QF=46/cool=10/loss=1000 vs baseline cool=10/loss=1500/QF=50 (default)."""
import csv, os
from pathlib import Path

BASE = Path(__file__).parent
A = BASE / "IOF_NQ_oneoff_NQM6_1yr_cool10_loss1500.csv"          # baseline
B = BASE / "IOF_NQ_oneoff_NQM6_1yr_qf46_cool10_loss1000.csv"     # new


def summarize(path):
    trades = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            try:
                pnl = float(row["DayPnL"])
            except (ValueError, KeyError):
                continue
            trades.append((pnl, row.get("Mode", ""), row.get("ExitReason", "")))

    n = len(trades)
    wins   = [p for p, _, _ in trades if p > 0]
    losses = [p for p, _, _ in trades if p < 0]
    gw, gl = sum(wins), sum(losses)
    total = gw + gl
    pf = (gw / abs(gl)) if gl < 0 else float("inf")
    wr = (len(wins) / n * 100) if n else 0.0
    aw = (gw / len(wins)) if wins else 0.0
    al = (gl / len(losses)) if losses else 0.0
    exp = (total / n) if n else 0.0

    eq, peak, dd = 0.0, 0.0, 0.0
    for p, _, _ in trades:
        eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)

    modes = {}
    reasons = {}
    for _, m, r in trades:
        modes[m] = modes.get(m, 0) + 1
        reasons[r] = reasons.get(r, 0) + 1

    return dict(n=n, wins=len(wins), losses=len(losses),
                total=total, pf=pf, wr=wr,
                aw=aw, al=al, exp=exp, dd=dd,
                modes=modes, reasons=reasons)


def main():
    if not B.exists():
        print(f"NEW file not yet present: {B.name}"); return
    a = summarize(A); b = summarize(B)
    print(f"\n{'='*78}\nCOMPARE NQM6 1yr  baseline (cool=10/loss=1500/QF=50) vs new (QF=46/loss=1000)\n{'='*78}")
    hdr = f"{'Metric':<18} {'baseline':>16} {'qf46_loss1000':>16} {'delta':>14}"
    print(hdr); print("-"*len(hdr))
    fields = [
        ("Trades (N)",    "n",    "d"),
        ("Wins",          "wins", "d"),
        ("Losses",        "losses","d"),
        ("Win rate %",    "wr",   ".1f"),
        ("Total PnL $",   "total",".0f"),
        ("Profit factor", "pf",   ".2f"),
        ("Avg win $",     "aw",   ".0f"),
        ("Avg loss $",    "al",   ".0f"),
        ("Expectancy $",  "exp",  ".0f"),
        ("Max DD $",      "dd",   ".0f"),
    ]
    for label, key, fmt in fields:
        av, bv = a[key], b[key]
        dv = bv - av if isinstance(av, (int, float)) else None
        if fmt == "d":
            print(f"{label:<18} {av:>16d} {bv:>16d} {(f'{dv:+d}' if dv is not None else ''):>14}")
        else:
            print(f"{label:<18} {av:>16{fmt}} {bv:>16{fmt}} {(f'{dv:+{fmt}}' if dv is not None else ''):>14}")

    print("\nMode mix (baseline | qf46):")
    for m in sorted(set(a["modes"]) | set(b["modes"])):
        print(f"  {m:<5} {a['modes'].get(m,0):>4d}  |  {b['modes'].get(m,0):>4d}")
    print("\nExitReason mix (baseline | qf46):")
    for r in sorted(set(a["reasons"]) | set(b["reasons"])):
        print(f"  {r:<14} {a['reasons'].get(r,0):>4d}  |  {b['reasons'].get(r,0):>4d}")

    d = b["total"] - a["total"]
    v = "test_better" if d > 0 else "test_worse" if d < 0 else "tied"
    print(f"\nDelta PnL = ${d:+.0f}  trades {b['n']-a['n']:+d}  -> {v}")


if __name__ == "__main__":
    main()
