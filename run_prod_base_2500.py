#!/usr/bin/env python3
"""Baseline (M4 on, news on) at 2.5k volume bars.
Usage: python run_prod_base_2500.py <scid>
"""
import os, sys
import backtest as bt

bt.DAILY_LOSS    = 800.0
bt.DAILY_PROF    = 0.0
bt.NEWS_FILTER   = 1
bt.C_OPEN_COOL   = 36
bt.TARGET_VOL    = 2500
bt.SCALE_OUT     = False

_orig_build = bt.build_volume_bars
def _build(recs, target_vol=bt.TARGET_VOL, price_scale=1.0):
    return _orig_build(recs, target_vol=bt.TARGET_VOL, price_scale=price_scale)
bt.build_volume_bars = _build

SCID = sys.argv[1]
stem = os.path.splitext(os.path.basename(SCID))[0].replace(".", "_")
OUT  = os.path.join(bt.BASE_DIR, f"IOF_NQ_prod_{stem}_base_vol2500.csv")
sys.argv = ["backtest.py", SCID, OUT]
print(f"\n##### PROD-base-2500 {stem}: vol=2500 loss=800 noProfCap newsON "
      f"cool=36 singleLot #####")
bt.main()
