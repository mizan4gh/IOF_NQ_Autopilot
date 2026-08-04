#!/usr/bin/env python3
"""Same dated audit as audit_today_0709.py but under the EXTENDED session
(00:00-15:45, open-cool 5) validated in [[project_ext_session_0000_ab]],
still at MT=1 / DL=800 / news on / M8 floor 60."""
import os, sys
from datetime import date
import backtest as bt

bt.LOG_NOFIRE      = 1
bt.QUAL_FLOOR_M8   = 60
bt.NEWS_FILTER     = 1
bt.MAX_TRADES      = 1
bt.DAILY_LOSS      = 800.0
bt.RTH_OPEN        = 0
bt.FLATTEN_HHMM    = 1545
bt.LATE_ENTRY_GATE = 1545
bt.C_OPEN_COOL     = 5

SCID = os.path.join(bt.BASE_DIR, "FROZEN_NQU6_0709.scid")
OUT  = os.path.join(bt.BASE_DIR, "IOF_NQ_audit_0709_ext.csv")
sys.argv = ["backtest.py", SCID, OUT]

b = bt.main()

FROM = date(2026, 6, 29)
TODAY = date(2026, 7, 9)

print("\n" + "=" * 70)
print("  EXT-SESSION DATED FUNNEL  (00:00-15:45, cool=5)  since", FROM)
print("=" * 70)
per_day = {}
for dt, mode, gate, detail in b.funnel_audit:
    d = dt.date()
    if d < FROM:
        continue
    per_day.setdefault(d, []).append((dt, mode, gate, detail))
for d in sorted(per_day):
    rows = per_day[d]
    tag = "  <-- TODAY" if d == TODAY else ""
    print(f"\n  {d}  armed={len(rows)}{tag}")
    for dt, mode, gate, detail in rows:
        print(f"      {dt:%H:%M:%S}  {mode:<3} {gate:<9} {detail}")

print("\n" + "=" * 70)
print("  ALL TRADES (full file) under ext session")
print("=" * 70)
for t in b.out:
    print(f"  {t.date} {t.time}  {t.event:<6} {t.mode} {t.side} "
          f"entry={t.entry} exit={t.exit_px} rsn={t.exit_rsn} dayPnL={t.day_pnl if hasattr(t,'day_pnl') else ''}")
