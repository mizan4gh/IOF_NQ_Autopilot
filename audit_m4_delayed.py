#!/usr/bin/env python3
"""Test M4 DELAYED ENTRY (retest of the swept level) vs immediate entry.

For every M4 sweep/reclaim shape (via _on_m4_arm, incl. ungated), forward-sim:
  IMMEDIATE: enter at the rejection-bar close (current behaviour).
  DELAYED  : wait for price to retest the swept level (sw) within K bars, then
             enter there with a tight structural stop just beyond sw; SAME
             absolute target. If price reaches the target BEFORE retesting sw,
             the delayed entry MISSES that winner (counted as 0).
Compares win-rate, fill rate, and expectancy/shape. If delayed E/shape > immediate
on BOTH contracts, the retest entry beats taking every signal at the close.
Config: production granularity 3k bars, NEWS OFF (per request).
Usage: python audit_m4_delayed.py <scid>
"""
import os, sys
import backtest as bt

arms = []
bt.Backtester._on_m4_arm = lambda self, i, is_long, ep, atr, sw=0.0: arms.append((i, is_long, ep, atr, sw))

bt.TARGET_VOL  = 5000     # per request (5k bars)
bt.NEWS_FILTER = 0        # NO NEWS (per request)
_obvb = bt.build_volume_bars   # TARGET_VOL is bound as a default arg at import; force 5k
bt.build_volume_bars = lambda recs, price_scale=1.0, target_vol=5000: _obvb(recs, target_vol=5000, price_scale=price_scale)
SCID = sys.argv[1]
stem = os.path.splitext(os.path.basename(SCID))[0]
sys.argv = ["backtest.py", SCID, os.path.join(bt.BASE_DIR, "tmp_m4audit.csv")]
b = bt.main()
bars, PT = b.bars, bt.PT_VAL

K, WIN = 8, 40            # retest must occur within K bars; evaluate over WIN
def lv(atr):
    return (max(bt.C_STOP_FL, min(bt.C_STOP_CL, atr * bt.C_STOP_ATR)),
            max(bt.C_T1_FL,   min(bt.C_T1_CL,   atr * bt.C_T1_ATR)))

imm = {"w": 0, "l": 0, "n": 0, "e": 0.0}
dly = {"w": 0, "l": 0, "miss": 0, "nofill": 0, "e": 0.0}
n = 0
for (i, lng, ep, atr, sw) in arms:
    sd, t1d = lv(atr)
    if lng: S0, T = ep - sd, ep + t1d
    else:   S0, T = ep + sd, ep - t1d
    n += 1
    # ---- IMMEDIATE ----
    out = None
    for j in range(i + 1, min(len(bars), i + 1 + WIN)):
        bj = bars[j]
        ht = (bj.high >= T) if lng else (bj.low <= T)
        hs = (bj.low <= S0) if lng else (bj.high >= S0)
        if ht: out = ("w", +t1d); break
        if hs: out = ("l", -sd);  break
    if out: imm[out[0]] += 1; imm["e"] += out[1]
    else:   imm["n"] += 1
    # ---- DELAYED (retest of sw) ----
    stop_d = max(S0, sw + 0.25 * atr) if not lng else min(S0, sw - 0.25 * atr)
    fill = None
    for j in range(i + 1, min(len(bars), i + 1 + K)):
        bj = bars[j]
        ht = (bj.high >= T) if lng else (bj.low <= T)
        rt = (bj.low <= sw) if lng else (bj.high >= sw)
        if ht and not rt:        # target hit before any retest -> missed winner
            dly["miss"] += 1; fill = "miss"; break
        if rt:
            fill = j; break
    if fill is None:
        dly["nofill"] += 1
    elif fill != "miss":
        jf = fill; res = None
        win_pts = abs(T - sw); loss_pts = -abs(sw - stop_d)
        for j in range(jf, min(len(bars), i + 1 + WIN)):
            bj = bars[j]
            ht = (bj.high >= T) if lng else (bj.low <= T)
            hs = (bj.low <= stop_d) if lng else (bj.high >= stop_d)
            if ht: res = ("w", win_pts); break
            if hs: res = ("l", loss_pts); break
        if res: dly[res[0]] += 1; dly["e"] += res[1]

print(f"\n===== M4 DELAYED-ENTRY AUDIT  {stem}  ({n} shapes, {bt.TARGET_VOL//1000}k, NO NEWS) =====")
if n:
    iwr = imm["w"] / (imm["w"] + imm["l"]) * 100 if (imm["w"] + imm["l"]) else 0
    print(f"  IMMEDIATE: {imm['w']}W /{imm['l']}L /{imm['n']}neither  WR {iwr:.0f}%  "
          f"E={imm['e']/n:+.1f} pts (${imm['e']/n*PT:+,.0f})/shape")
    fills = dly["w"] + dly["l"]
    dwr = dly["w"] / fills * 100 if fills else 0
    print(f"  DELAYED  : {dly['w']}W /{dly['l']}L  (fills {fills}/{n}={fills/n*100:.0f}%, "
          f"missed-winners {dly['miss']}, no-fill {dly['nofill']})  WR {dwr:.0f}%  "
          f"E={dly['e']/n:+.1f} pts (${dly['e']/n*PT:+,.0f})/shape")
