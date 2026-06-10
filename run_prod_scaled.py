#!/usr/bin/env python3
"""Production config + 2-lot TP1/TP2 scale-out (fixed 2 contracts).

Mirrors run_prod.py (3k vol, $800 daily loss, news ON, open-cooldown 36,
RTH 09:35) but enables the experimental scale-out engine. Tests whether
the scale-out scheme that helped at 5k carries to live granularity.
Usage: python run_prod_scaled.py <scid>
"""
import os, sys
import backtest as bt

bt.DAILY_LOSS  = 800.0    # live IN_DAILY_LOSS
bt.DAILY_PROF  = 0.0      # no daily profit cap
bt.NEWS_FILTER = 1        # news filter ON
bt.C_OPEN_COOL = 36       # live open-cooldown
bt.TARGET_VOL  = 3000     # live bar size (== default, no builder patch needed)
bt.SCALE_OUT   = True     # multi-lot scale-out
bt.BASE_QTY    = 2        # 2 lots: 1@TP1, runner@TP2-else-trail
bt.SIZE_BY_RM  = False    # fixed at 2 (RM-sizing off)

SCID = sys.argv[1]
stem = os.path.splitext(os.path.basename(SCID))[0].replace(".", "_")
OUT  = os.path.join(bt.BASE_DIR, f"IOF_NQ_prod_scaled_{stem}.csv")
sys.argv = ["backtest.py", SCID, OUT]

print(f"\n##### PROD-SCALED {stem}: vol=3000 loss=800 noProfCap newsON cool=36 "
      f"scaleOut=2lot fixed #####")
bt.main()
