#!/usr/bin/env python3
"""One-off: 1yr NQM6 3k-vol bars, OPEN_COOL=10, DAILY_LOSS=1500, no profit cap, news OFF."""
import sys, os
import backtest as bt

bt.TARGET_VOL  = 3000
bt.C_OPEN_COOL = 10
bt.DAILY_LOSS  = 1500.0
bt.DAILY_PROF  = 0.0
bt.NEWS_FILTER = 0
bt.SCALE_OUT   = False

import backtest_csv as bcsv

SRC = r"C:\Users\17034\OneDrive\Desktop\NQM6.CME [CBV][M]  3000 Volume #7_GraphData.txt"
OUT = os.path.join(bt.BASE_DIR, "IOF_NQ_oneoff_NQM6_1yr_cool10_loss1500.csv")
sys.argv = ["backtest_csv.py", SRC, OUT]

print("\n##### ONE-OFF NQM6 1yr: vol=3000 cool=10 loss=1500 noProfCap newsOFF singleLot #####")
bcsv.main()
