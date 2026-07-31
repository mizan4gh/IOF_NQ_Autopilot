"""Re-run ONLY the fix1lot_ctl control after the qty<2 double-booking fix.

The first pass booked TP1 as a peel even at FIXED_SCALE_QTY=1, which dropped
lots_open to 0 without closing the position; _close's `or 1` fallback then
billed a second full lot at the eventual exit. Every winner paid ~2x.
backtest.py now closes the whole position at TP1 when qty < 2.

Baseline / 2-lot CSVs from the first pass are unaffected (qty>=2 path was
never wrong -- verified: stop -$1,010 = 2x-$500-$10, T2 +$1,490 = $495+$995)
so only this one scenario is replayed. Writes *_fix1lot_ctl.csv in place.
"""
import sys
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
import backtest_fixed_scale_2lot_ab as H

TAGS = ["NQZ25", "NQM5", "NQH6", "NQU25", "NQM6", "NQU26"]
NAME = "fix1lot_ctl"

for tag in TAGS:
    fname = H.CONTRACTS[tag]
    if not (BASE / fname).exists():
        print(f"missing: {fname}"); continue
    print(f"\n########## {tag} ##########", flush=True)
    bars = H.load_bars(fname)
    H.run(NAME, H.SCENARIOS[NAME], bars, tag)
    del bars
print("\nDONE", flush=True)
