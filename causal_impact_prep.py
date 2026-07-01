"""Prep the CausalImpact CONTROL series for the July-5-2026 floor-60 deploy.

Control = backtest daily P&L under the OLD config (baseline / floor-50). Rationale:
pre-deploy your live trading uses the old config, so live P&L ~ backtest(floor-50);
post-deploy live switches to floor-60 while this control stays floor-50 — so the
post-period divergence (live minus control) IS the floor-60 causal effect. The
control is deterministic and unaffected by the live deploy, which is exactly what
CausalImpact requires of a control.

Source: the 6 frozen-contract baseline CSVs (IOF_NQ_flr6_{tag}_baseline.csv),
whose active windows are contiguous front-month periods → concatenate by date into
one continuous daily series. Produces a regular weekday series (0-fill no-trade
days) so it aligns cleanly with a daily live-P&L response.

Output: ci_control_backtest_pnl.csv  [date, bt_pnl, bt_trades]

NOTE: currently covers ~2025-03-19 .. 2026-06-26 (the frozen data). After the
deploy, re-copy fresh front-month scid, re-run backtest_m8_floor_validate6.py to
extend the baseline CSVs, and re-run this to extend the control into the post-period.
"""
import csv
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
# contiguous front-month windows (from the flr6 baseline date spans)
CONTRACTS = ["NQM5", "NQU25", "NQZ25", "NQH6", "NQM6", "NQU26"]
OUT = BASE / "ci_control_backtest_pnl.csv"


def daily_pnl(tag):
    """{date_iso: (pnl, n_trades)} from one baseline CSV (EXIT rows, TotalPnL delta)."""
    p = BASE / f"IOF_NQ_flr6_{tag}_baseline.csv"
    if not p.exists():
        print(f"  (missing {p.name} — skip)"); return {}
    out, prev = {}, 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] == "EXIT":
            t = float(r["TotalPnL"]); d = r["Date"]
            pnl, n = out.get(d, (0.0, 0))
            out[d] = (pnl + (t - prev), n + 1); prev = t
    return out


def main():
    merged = {}
    for tag in CONTRACTS:
        dp = daily_pnl(tag)
        for d, v in dp.items():
            # contiguous windows shouldn't collide; if they do, sum
            pnl, n = merged.get(d, (0.0, 0))
            merged[d] = (pnl + v[0], n + v[1])
    if not merged:
        print("No baseline CSVs found — run backtest_m8_floor_validate6.py first.")
        return
    d0 = date.fromisoformat(min(merged)); d1 = date.fromisoformat(max(merged))
    rows, d = [], d0
    while d <= d1:
        if d.weekday() < 5:                      # weekdays only
            pnl, n = merged.get(d.isoformat(), (0.0, 0))
            rows.append((d.isoformat(), round(pnl, 2), n))
        d += timedelta(days=1)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "bt_pnl", "bt_trades"]); w.writerows(rows)
    trade_days = sum(1 for _, _, n in rows if n > 0)
    print(f"Wrote {OUT.name}: {len(rows)} weekday rows "
          f"({d0} .. {d1}), {trade_days} with trades, "
          f"total bt P&L {sum(r[1] for r in rows):+,.0f}")


if __name__ == "__main__":
    main()
