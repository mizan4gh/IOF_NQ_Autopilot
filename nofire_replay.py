#!/usr/bin/env python3
"""Replay NQZ25 with v12.35 LOG_NOFIRE=1 and report the arm->fire funnel.

Why this exists: the cpp v12.35 NOFIRE diagnostic logs to the Sierra Message
Log, which can't be driven headless. backtest.py is a faithful mirror of the
cpp trigger logic (M1_PULLBACK=2 == v12.34/35), so enabling the ported NOFIRE
flag here reproduces the same per-bar diagnostic over historical bars.
"""
import os, sys
import backtest as bt

bt.LOG_NOFIRE = 1
SCID = os.path.join(bt.BASE_DIR, "NQZ25-CME.scid")
OUT  = os.path.join(bt.BASE_DIR, "IOF_NQ_nofire_NQZ25.csv")
sys.argv = ["backtest.py", SCID, OUT]

b = bt.main()

NAMES = bt.MODE_NAMES
setups = {}
for t in b.out:
    if t.event == "SETUP":
        setups[t.mode] = setups.get(t.mode, 0) + 1

print("\n" + "=" * 64)
print("  v12.35  ARM -> FIRE FUNNEL   (eligible bars only)")
print("=" * 64)
print(f"  {'Mode':<5}{'armed_L':>9}{'armed_S':>9}{'armed_tot':>11}{'SETUPs':>9}{'killed':>9}")
for m in (0, 1, 2, 3, 5, 7):
    al, as_ = b.arm_long[m], b.arm_short[m]
    tot = al + as_
    fired = setups.get(NAMES[m], 0)
    print(f"  {NAMES[m]:<5}{al:>9}{as_:>9}{tot:>11}{fired:>9}{max(0, tot - fired):>9}")
print("  (armed = mode detected a shape pre-gates; killed = lost to priority /")
print("   regime / RM-floor / cooldown / quality gates before a SETUP row)")

print("\n" + "=" * 64)
print("  LOG_NOFIRE  (shape possible, NOTHING armed)")
print("=" * 64)
print(f"  Bars with a shape but no arm : {b.nofire_bars}")
print(f"  Shape-possibility breakdown  : "
      + ", ".join(f"{k}={v}" for k, v in b.nofire_possible.items()))
print("  M4 near-miss block reasons (why the swept bar didn't arm):")
for k, v in sorted(b.nofire_why.items(), key=lambda kv: -kv[1]):
    print(f"      {k:<16}{v:>6}")
print("\n  Sample [V18A NOFIRE] lines (first %d):" % len(b.nofire))
for ln in b.nofire[:25]:
    print("   " + ln)
