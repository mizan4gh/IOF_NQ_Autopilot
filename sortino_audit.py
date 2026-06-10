"""Sortino audit of production-baseline backtest CSVs.

Computes per-day and per-trade Sortino, and decomposes downside deviation
by mode / exit reason / hour, so any "improve Sortino" lever targets the
actual source of downside instead of a guess.

Usage: python sortino_audit.py <log1.csv> [log2.csv ...]
"""
import csv
import math
import sys
from collections import defaultdict
from datetime import date, timedelta


def load_trades(path):
    trades = []
    prev_tot = 0.0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            tot = float(row["TotalPnL"])
            pnl = tot - prev_tot
            prev_tot = tot
            trades.append(dict(
                date=row["Date"], time=row["Time"], mode=row["Mode"],
                side=row["Side"], reason=row["ExitReason"], pnl=pnl,
                mae=float(row["MAE"]), mfe=float(row["MFE"]),
                hold=int(row["HoldBars"]),
            ))
    return trades


def weekdays_between(d0, d1):
    n, d = 0, d0
    while d <= d1:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def sortino(returns, n_periods=None):
    """Sortino vs 0 target. n_periods pads with zero-return periods
    (no-trade days) so selective strategies aren't unfairly flattered."""
    n = n_periods if n_periods else len(returns)
    if n == 0:
        return float("nan"), 0.0, 0.0
    mean = sum(returns) / n
    dd2 = sum(min(r, 0.0) ** 2 for r in returns) / n
    dd = math.sqrt(dd2)
    return (mean / dd if dd > 0 else float("inf")), mean, dd


def audit(path):
    trades = load_trades(path)
    name = path.split("\\")[-1].split("/")[-1]
    print(f"\n{'=' * 78}\n{name}  ({len(trades)} trades)\n{'=' * 78}")
    if not trades:
        return None

    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["pnl"]
    days = sorted(daily)
    d0 = date.fromisoformat(days[0])
    d1 = date.fromisoformat(days[-1])
    n_days = weekdays_between(d0, d1)

    day_rets = list(daily.values())
    s_day, mu_day, dd_day = sortino(day_rets, n_periods=n_days)
    s_tr, mu_tr, dd_tr = sortino([t["pnl"] for t in trades])

    total = sum(t["pnl"] for t in trades)
    print(f"Span {days[0]} .. {days[-1]}  ({n_days} weekdays, "
          f"{len(days)} active days)  Total ${total:+,.0f}")
    print(f"Daily Sortino  {s_day:7.3f}   (mean ${mu_day:+7.1f}/day, "
          f"downside-dev ${dd_day:7.1f}, ann~{s_day * math.sqrt(252):.2f})")
    print(f"Trade Sortino  {s_tr:7.3f}   (mean ${mu_tr:+7.1f}/trade, "
          f"downside-dev ${dd_tr:7.1f})")

    # Downside decomposition: each trade's share of sum(min(pnl,0)^2)
    dd_total = sum(min(t["pnl"], 0.0) ** 2 for t in trades)
    print(f"\nDownside variance shares (what hurts Sortino):")
    for key in ("mode", "reason"):
        shares = defaultdict(lambda: [0.0, 0, 0.0])
        for t in trades:
            s = shares[t[key]]
            s[0] += min(t["pnl"], 0.0) ** 2
            s[1] += 1
            s[2] += t["pnl"]
        print(f"  by {key}:")
        for k, (d2, n, pnl) in sorted(shares.items(),
                                      key=lambda kv: -kv[1][0]):
            pct = 100 * d2 / dd_total if dd_total else 0.0
            print(f"    {k:<10} {pct:5.1f}% of downside-var | "
                  f"{n:2d} trades | net ${pnl:+8,.0f}")

    print(f"\nWorst trades:")
    for t in sorted(trades, key=lambda t: t["pnl"])[:5]:
        print(f"  {t['date']} {t['time']}  {t['side']:<5} {t['mode']:<3} "
              f"{t['reason']:<10} ${t['pnl']:+8,.0f}  "
              f"MAE {t['mae']:5.1f}  MFE {t['mfe']:6.1f}  hold {t['hold']}")
    return dict(name=name, s_day=s_day, s_tr=s_tr, total=total,
                n=len(trades), dd_day=dd_day)


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(1)
    rows = [r for r in (audit(p) for p in paths) if r]
    if len(rows) > 1:
        print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
        print(f"{'file':<42} {'N':>3} {'total$':>9} "
              f"{'Sortino/day':>12} {'Sortino/tr':>11}")
        for r in rows:
            print(f"{r['name']:<42} {r['n']:>3} {r['total']:>+9,.0f} "
                  f"{r['s_day']:>12.3f} {r['s_tr']:>11.3f}")


if __name__ == "__main__":
    main()
