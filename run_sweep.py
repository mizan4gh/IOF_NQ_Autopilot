#!/usr/bin/env python3
"""Param-sweep runner (non-destructive overrides of backtest.py globals).

User-requested config (2026-05-27):
  vol=5000, daily_loss=1500, no daily profit cap, no news filter, open-cooldown=10.
NOTE: multi-contract TP1/TP2 scale-out is NOT modeled here (single-lot engine).
Usage: python run_sweep.py <scid>
"""
import os, sys
import backtest as bt

# ── overrides ────────────────────────────────────────────────────────────────
bt.DAILY_LOSS  = 1500.0   # was 0/800
bt.DAILY_PROF  = 0.0      # no daily profit cap
bt.NEWS_FILTER = 0        # news filter off
bt.C_OPEN_COOL = 10       # was 36
bt.TARGET_VOL  = 5000     # was 3000 (also force the builder below)
bt.SCALE_OUT   = True     # multi-lot TP1/TP2 scale-out
bt.BASE_QTY    = 2        # 2 lots: 1@TP1, runner@TP2-else-trail
bt.SIZE_BY_RM  = True     # qty = max(1, round(2 * risk_mult))
_dm = os.environ.get("DISABLE_MODES", "").strip()
if _dm:
    bt.DISABLE_MODES = {int(x) for x in _dm.replace(" ", "").split(",") if x}
    print(f"  DISABLE_MODES = {sorted(bt.DISABLE_MODES)}")

# main() calls build_volume_bars without target_vol, so the 3000 default arg
# would win — monkeypatch to force 5000.
_orig_bvb = bt.build_volume_bars
bt.build_volume_bars = lambda recs, price_scale=1.0, target_vol=5000: \
    _orig_bvb(recs, target_vol=5000, price_scale=price_scale)

SCID = sys.argv[1]
stem = os.path.splitext(os.path.basename(SCID))[0].replace(".", "_")
OUT  = os.path.join(bt.BASE_DIR, f"IOF_NQ_sweep_{stem}.csv")
sys.argv = ["backtest.py", SCID, OUT]

print(f"\n##### SWEEP {stem}: vol=5000 dailyLoss=1500 noProfCap newsOFF openCool=10 "
      f"scaleOut=2lot/RM #####")
b = bt.main()

# qty distribution across entries (how often the 2nd lot actually engaged)
from collections import Counter
qd = Counter(t.qty for t in b.out if t.event == "SETUP")
print("  Entry qty distribution:", dict(sorted(qd.items())))
