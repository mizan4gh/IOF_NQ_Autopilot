#!/usr/bin/env python3
"""Test the M4 stop-overshoot hypothesis (user obs 2026-05-27):
'every M4 stop-out then goes the right direction'.

For EVERY M4 sweep/reclaim shape (captured via the _on_m4_arm hook, incl. ones
that never fire), forward-simulate the path with the production stop/T1/T2 calc
and measure, over a 40-bar window:
  - did price hit the stop before T1 (a "loss")?
  - of those stopped, did T1 print LATER (overshoot-then-reverse)?
  - how far past the stop did the adverse excursion run (overshoot pts)?
  - counterfactual: win-rate vs progressively WIDER stops (sd + k*ATR).

If WR climbs sharply as the stop widens AND stopped trades usually reach T1
after, the stop sits in the overshoot zone -> wider stop / delayed entry helps.
Usage: python audit_m4_overshoot.py <scid>
"""
import os, sys, statistics as st
import backtest as bt

arms = []
bt.Backtester._on_m4_arm = lambda self, i, is_long, ep, atr: arms.append((i, is_long, ep, atr))

bt.TARGET_VOL = 3000          # production granularity (== default; no builder patch)
SCID = sys.argv[1]
stem = os.path.splitext(os.path.basename(SCID))[0]
sys.argv = ["backtest.py", SCID, os.path.join(bt.BASE_DIR, "tmp_m4audit.csv")]
b = bt.main()

bars, ATRm = b.bars, bt.PT_VAL
WIN   = 40                    # forward bars to evaluate each shape
WIDEN = [0.0, 0.25, 0.5, 1.0] # extra ATR added to the stop distance (0 = current)

def sd_t1(atr):
    sd = max(bt.C_STOP_FL, min(bt.C_STOP_CL, atr * bt.C_STOP_ATR))
    t1 = max(bt.C_T1_FL,   min(bt.C_T1_CL,   atr * bt.C_T1_ATR))
    return sd, t1

n = 0
win = {k: 0 for k in WIDEN}; loss = {k: 0 for k in WIDEN}
exp_pts = {k: 0.0 for k in WIDEN}     # T1-capped expectancy in points (win=+T1, stop=-width)
stop_first = 0; stop_then_t1 = 0; overshoot_pts = []
for (i, is_long, ep, atr) in arms:
    sd, t1d = sd_t1(atr)
    t1_bar = None; mae = 0.0
    cross = {k: None for k in WIDEN}     # first bar adverse >= sd+k*ATR
    for j in range(i + 1, min(len(bars), i + 1 + WIN)):
        bj = bars[j]
        adv = (ep - bj.low) if is_long else (bj.high - ep)   # adverse (against)
        fav = (bj.high - ep) if is_long else (ep - bj.low)   # favorable (toward T1)
        mae = max(mae, adv)
        if t1_bar is None and fav >= t1d:
            t1_bar = j
        for k in WIDEN:
            if cross[k] is None and adv >= sd + k * atr:
                cross[k] = j
    n += 1
    # current-stop outcome + overshoot signature
    sb = cross[0.0]
    if sb is not None and (t1_bar is None or sb <= t1_bar):
        stop_first += 1
        overshoot_pts.append(mae - sd)
        if t1_bar is not None and t1_bar > sb:
            stop_then_t1 += 1
    # win/loss per stop width (first-touch ordering; ignore "neither")
    for k in WIDEN:
        sbk = cross[k]
        if t1_bar is not None and (sbk is None or t1_bar < sbk):
            win[k] += 1;  exp_pts[k] += t1d            # booked at T1
        elif sbk is not None:
            loss[k] += 1; exp_pts[k] -= (sd + k * atr)  # stopped at the (wider) stop

print(f"\n===== M4 STOP-OVERSHOOT AUDIT  {stem}  ({n} M4 shapes, {WIN}-bar window) =====")
if n == 0:
    print("  no M4 shapes"); sys.exit()
print(f"  Current stop (sd = clamp(ATR*{bt.C_STOP_ATR}, [{bt.C_STOP_FL:.0f},{bt.C_STOP_CL:.0f}])):")
print(f"    stopped before T1      : {stop_first}/{n}  ({stop_first/n*100:.0f}%)")
if stop_first:
    print(f"    ...of those, T1 LATER  : {stop_then_t1}/{stop_first}  "
          f"({stop_then_t1/stop_first*100:.0f}%)  <-- overshoot-then-reverse")
    print(f"    overshoot past stop pts: median {st.median(overshoot_pts):.1f}, "
          f"mean {st.mean(overshoot_pts):.1f}, max {max(overshoot_pts):.1f}")
print("  Win-rate & T1-capped expectancy vs stop width:")
for k in WIDEN:
    tot = win[k] + loss[k]
    wr  = win[k] / tot * 100 if tot else 0
    ev  = exp_pts[k] / tot if tot else 0          # pts per resolved trade
    lbl = "current" if k == 0 else f"+{k}*ATR"
    print(f"    stop {lbl:>8}: {win[k]:>3}W /{loss[k]:>3}L   WR {wr:4.0f}%   "
          f"E={ev:+6.1f} pts (${ev*bt.PT_VAL:+,.0f})/trade  (n={tot})")
print("  (expectancy caps wins at T1 — a proxy; real runners go to T2/trail.)")
