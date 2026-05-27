#!/usr/bin/env python3
"""Production-config backtest runner (non-destructive overrides).

Mirrors the live chartbook: 3k vol, single-lot, $800 daily loss, no profit
cap, news filter ON, open-cooldown 36, M6 CtrlScore-gated (default).
Honors DISABLE_MODES env (e.g. DISABLE_MODES=5 to test M6-off).
Usage: python run_prod.py <scid>
"""
import os, sys
import backtest as bt

bt.DAILY_LOSS  = 800.0    # live IN_DAILY_LOSS
bt.DAILY_PROF  = 0.0      # no daily profit cap
bt.NEWS_FILTER = 1        # news filter ON
bt.C_OPEN_COOL = 36       # live open-cooldown
bt.TARGET_VOL  = 3000     # live bar size (== default, no builder patch needed)
bt.SCALE_OUT   = False    # single lot

_dm = os.environ.get("DISABLE_MODES", "").strip()
if _dm:
    bt.DISABLE_MODES = {int(x) for x in _dm.replace(" ", "").split(",") if x}

SCID = sys.argv[1]
stem = os.path.splitext(os.path.basename(SCID))[0].replace(".", "_")
tag  = "M6off" if bt.DISABLE_MODES else "base"
OUT  = os.path.join(bt.BASE_DIR, f"IOF_NQ_prod_{stem}_{tag}.csv")
sys.argv = ["backtest.py", SCID, OUT]

print(f"\n##### PROD {stem} [{tag}]: vol=3000 loss=800 noProfCap newsON cool=36 "
      f"singleLot disable={sorted(bt.DISABLE_MODES)} #####")
bt.main()
