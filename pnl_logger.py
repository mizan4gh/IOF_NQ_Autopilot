"""Daily realized-P&L logger for the CausalImpact response series.

Parses the strategy's own per-contract CSVs (SierraChart\\Data\\IOF_NQ_F.US.E*.csv),
which carry realized P&L on EXIT rows (DayPnL = the running day total). For each
trading date it takes the last EXIT's DayPnL = that day's realized P&L, then
writes/refreshes live_pnl.csv (date, realized_pnl). Idempotent: rebuilds the whole
file from source each run, so running it daily can't create duplicates.

NOTE: this is the strategy's SIM P&L (what floor-60 actually changes), not real
Apex fills. Swap the source if you want real account P&L.

Usage (run daily, e.g. via Task Scheduler, from 2026-07-05):
    python pnl_logger.py                 # capture all available EXIT dates
    python pnl_logger.py --since 2025-01-01
"""
import argparse, csv, glob
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")
OUT = BASE / "live_pnl.csv"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="1900-01-01", help="only keep dates >= this")
    ap.add_argument("--source", default=str(SC_DATA / "IOF_NQ_F.US.E*.csv"),
                    help="glob for strategy CSV(s)")
    a = ap.parse_args()

    files = sorted(glob.glob(a.source))
    if not files:
        print(f"no strategy CSVs matched {a.source}"); return

    # collect EXIT rows across all contract files: {date: (time, day_pnl)}, keep last time
    by_date = {}
    for fp in files:
        with open(fp, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("Event") != "EXIT":
                    continue
                d, t = r["Date"], r["Time"]
                try:
                    pnl = float(r["DayPnL"])
                except (KeyError, ValueError):
                    continue
                if d < a.since:
                    continue
                prev = by_date.get(d)
                if prev is None or t >= prev[0]:      # last EXIT of the day wins
                    by_date[d] = (t, pnl)

    rows = [(d, round(v[1], 2)) for d, v in sorted(by_date.items())]
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "realized_pnl"]); w.writerows(rows)

    tot = sum(r[1] for r in rows)
    print(f"wrote {OUT.name}: {len(rows)} trading days "
          f"({rows[0][0] if rows else '-'} .. {rows[-1][0] if rows else '-'}), "
          f"total realized {tot:+,.2f}")
    for d, p in rows[-8:]:
        print(f"  {d}  {p:>+10,.2f}")


if __name__ == "__main__":
    main()
