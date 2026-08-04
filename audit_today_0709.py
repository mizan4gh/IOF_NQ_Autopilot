#!/usr/bin/env python3
"""Dated funnel audit for the 2026-07-09 no-fire day on NQU6 frozen snapshot.

Mirrors the LIVE panel config (floors 50 / M8=60, late-entry 15:00, MT=1,
DL=800) and prints, for the last N sessions, every armed setup with the gate
that killed it, plus M4 near-miss (shape-but-no-arm) reasons for today.
"""
import os, sys
from datetime import date
import backtest as bt

bt.LOG_NOFIRE      = 1
bt.QUAL_FLOOR_M8   = 60      # v12.36+ live floor-60 deploy
bt.LATE_ENTRY_GATE = 1500    # R2b shipped v12.36
bt.MAX_TRADES      = 1       # live panel MT=1
bt.DAILY_LOSS      = 800.0   # live panel DL=$800

SCID = os.path.join(bt.BASE_DIR, "FROZEN_NQU6_0709.scid")
OUT  = os.path.join(bt.BASE_DIR, "IOF_NQ_audit_0709.csv")
sys.argv = ["backtest.py", SCID, OUT]

b = bt.main()

TODAY = date(2026, 7, 9)
FROM  = date(2026, 6, 29)

print("\n" + "=" * 70)
print("  DATED FUNNEL AUDIT  (live-mirror config)  sessions since", FROM)
print("=" * 70)
per_day = {}
for dt, mode, gate, detail in b.funnel_audit:
    d = dt.date()
    if d < FROM:
        continue
    per_day.setdefault(d, []).append((dt, mode, gate, detail))

for d in sorted(per_day):
    rows = per_day[d]
    kills = {}
    for _, _, gate, _ in rows:
        kills[gate] = kills.get(gate, 0) + 1
    tag = "  <-- TODAY" if d == TODAY else ""
    print(f"\n  {d}  armed={len(rows)}  " +
          ", ".join(f"{k}={v}" for k, v in sorted(kills.items())) + tag)
    for dt, mode, gate, detail in rows:
        print(f"      {dt:%H:%M:%S}  {mode:<3} {gate:<9} {detail}")

print("\n" + "=" * 70)
print("  M4 NEAR-MISSES (shape possible, nothing armed) — last sessions")
print("=" * 70)
nf_day = {}
for dt, whys in b.nofire_audit:
    d = dt.date()
    if d < FROM:
        continue
    nf_day.setdefault(d, []).append((dt, whys))
for d in sorted(nf_day):
    print(f"\n  {d}  ({len(nf_day[d])} near-miss bars)")
    if d == TODAY:
        for dt, whys in nf_day[d]:
            print(f"      {dt:%H:%M:%S}  {', '.join(whys)}")
    else:
        agg = {}
        for _, whys in nf_day[d]:
            for w in whys:
                agg[w] = agg.get(w, 0) + 1
        print("      " + ", ".join(f"{k}={v}" for k, v in sorted(agg.items())))

print("\n" + "=" * 70)
print("  SETUPS / TRADES in b.out since", FROM)
print("=" * 70)
for t in b.out:
    if t.date >= "2026-06-29":
        print(f"  {t.date} {t.time}  {t.event:<6} {t.mode} {t.side} "
              f"entry={t.entry} exit={t.exit_px} rsn={t.exit_rsn}")
