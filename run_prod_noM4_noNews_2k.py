#!/usr/bin/env python3
"""Backtest: M4 disabled, news filter OFF, 2k volume bars.
Usage: python run_prod_noM4_noNews_2k.py <scid>
"""
import os, sys
import backtest as bt

bt.DAILY_LOSS    = 800.0
bt.DAILY_PROF    = 0.0
bt.NEWS_FILTER   = 0
bt.C_OPEN_COOL   = 36
bt.TARGET_VOL    = 2000
bt.SCALE_OUT     = False
bt.DISABLE_MODES = {3}

_orig_build = bt.build_volume_bars
def _build_2k(recs, target_vol=bt.TARGET_VOL, price_scale=1.0):
    return _orig_build(recs, target_vol=bt.TARGET_VOL, price_scale=price_scale)
bt.build_volume_bars = _build_2k

SCID = sys.argv[1]
stem = os.path.splitext(os.path.basename(SCID))[0].replace(".", "_")
OUT  = os.path.join(bt.BASE_DIR, f"IOF_NQ_prod_{stem}_noM4_noNews_vol2k.csv")
sys.argv = ["backtest.py", SCID, OUT]
print(f"\n##### PROD-noM4-noNews-2k {stem}: vol=2000 loss=800 noProfCap "
      f"newsOFF cool=36 singleLot disable={sorted(bt.DISABLE_MODES)} #####")
bt.main()
