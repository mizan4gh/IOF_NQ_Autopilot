#!/usr/bin/env python3
"""Production-config backtest at 1000-contract volume bars.

Mirrors run_prod.py (3k -> 1k bar size). Patches build_volume_bars default
since TARGET_VOL is bound at def time. Usage: python run_prod_vol1k.py <scid>
"""
import os, sys
import backtest as bt

VOL = 1000
bt.TARGET_VOL = VOL
# rebind the def-time default arg (target_vol is 1st default in the signature)
bt.build_volume_bars.__defaults__ = (VOL, 1.0)

bt.DAILY_LOSS  = 800.0    # live IN_DAILY_LOSS
bt.DAILY_PROF  = 0.0      # no daily profit cap
bt.NEWS_FILTER = 1        # news filter ON
bt.C_OPEN_COOL = 36       # live open-cooldown
bt.SCALE_OUT   = False    # single lot

_dm = os.environ.get("DISABLE_MODES", "").strip()
if _dm:
    bt.DISABLE_MODES = {int(x) for x in _dm.replace(" ", "").split(",") if x}

SCID = sys.argv[1]
stem = os.path.splitext(os.path.basename(SCID))[0].replace(".", "_")
OUT  = os.path.join(bt.BASE_DIR, f"IOF_NQ_prod_{stem}_vol{VOL}.csv")
sys.argv = ["backtest.py", SCID, OUT]

print(f"\n##### PROD {stem} [vol{VOL}]: vol={VOL} loss=800 noProfCap newsON "
      f"cool=36 singleLot disable={sorted(bt.DISABLE_MODES)} #####")
bt.main()
