#!/usr/bin/env python3
"""Backtest: M4 disabled, news filter OFF, 3k volume bars (prod bar size).

Isolates the M4-off + news-off effect from the bar-size lever.
Single-lot, $800 daily loss, no profit cap, open-cooldown 36.
Usage: python run_prod_noM4_noNews_3k.py <scid>
"""
import os, sys
import backtest as bt

bt.DAILY_LOSS    = 800.0
bt.DAILY_PROF    = 0.0
bt.NEWS_FILTER   = 0
bt.C_OPEN_COOL   = 36
bt.TARGET_VOL    = 3000
bt.SCALE_OUT     = False
bt.DISABLE_MODES = {3}   # M4

SCID = sys.argv[1]
stem = os.path.splitext(os.path.basename(SCID))[0].replace(".", "_")
OUT  = os.path.join(bt.BASE_DIR, f"IOF_NQ_prod_{stem}_noM4_noNews_vol3k.csv")
sys.argv = ["backtest.py", SCID, OUT]

print(f"\n##### PROD-noM4-noNews-3k {stem}: vol=3000 loss=800 noProfCap "
      f"newsOFF cool=36 singleLot disable={sorted(bt.DISABLE_MODES)} #####")
bt.main()
