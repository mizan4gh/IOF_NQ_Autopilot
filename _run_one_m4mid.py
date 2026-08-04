#!/usr/bin/env python3
"""Run ONE (contract, scenario) backtest for the M4-midday A/B and write its CSV.
Invoked as a subprocess by backtest_m4_midday_ab.py so each run gets a fresh
process (memory is released on exit — the 12-in-one-process version OOM'd on the
1.4 GB tick files). Usage: python _run_one_m4mid.py <contract> <scenario>
"""
import os, sys
import backtest as bt

LIVE = dict(
    QUAL_FLOOR=50, QUAL_FLOOR_M8=60, MAX_TRADES=1, DAILY_LOSS=800.0,
    DAILY_PROF=0.0, NEWS_FILTER=1, LATE_ENTRY_GATE=1500, ENTRY_ORD=2,
    TARGET_VOL=3000, SCALE_OUT=False,
)
SCENARIOS = {
    "baseline": {**LIVE, "M4_MIDDAY_ADMIT": False},
    "m4mid40":  {**LIVE, "M4_MIDDAY_ADMIT": True, "M4_MIDDAY_Q_MIN": 40,
                 "M4_MIDDAY_START": 1200, "M4_MIDDAY_END": 1400},
}

contract, name = sys.argv[1], sys.argv[2]
for k, v in SCENARIOS[name].items():
    if not hasattr(bt, k):
        raise AttributeError(f"backtest.py has no constant {k}")
    setattr(bt, k, v)

scid = os.path.join(bt.BASE_DIR, contract + ".scid")
out  = os.path.join(bt.BASE_DIR, f"IOF_NQ_m4mid_{contract}_{name}.csv")
sys.argv = ["backtest.py", scid, out]
bt.main()
