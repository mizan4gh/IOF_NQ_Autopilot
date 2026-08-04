"""
Reachability funnel for ORH/ORL as M4 sweep levels.

Answers, per contract, BEFORE trusting any A/B verdict: does the OR-sweep shape
physically occur, and which gate kills it? Written because two prior levers here
(M4 sc-bump, M6 qual-floor 40) produced $0 deltas that looked like "neutral" but
were really "the knob is unreachable" — a vacuous pass. Mirrors the M4 arm-site
gates in backtest.py exactly, counting survivors at each stage.

Usage: python diag_m4_or_reach.py [TAG]
"""
import sys
from collections import defaultdict
from pathlib import Path

import backtest as bt

BASE = Path(__file__).parent
CONTRACTS = {
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQU25": BASE / "F.US.ENQU25.scid",
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
    "NQM6":  BASE / "F.US.ENQM26.scid",
    "NQU26": BASE / "F.US.ENQU26.scid",
}
OR_START, OR_END = 930, 1000


def funnel(tag, path):
    recs = bt.read_scid(str(path))
    bars = bt.build_volume_bars(recs, target_vol=3000,
                                price_scale=bt.detect_price_scale(str(path)))
    # ATR/VWAP are precomputed inside Backtester.run(); replicate that pass with
    # the same indicator classes so the gate arithmetic matches exactly
    # (backtest.py:1468-1484).
    atr_ind, vwap_ind = bt.WilderATR(bt.ATR_PER), bt.SessionVWAP()
    atr_v, vwap_v = [], []
    for b in bars:
        atr_v.append(atr_ind.update(b.high, b.low, b.close))
        vwap_v.append(vwap_ind.update(b)[0])

    c = defaultdict(int)
    sessions = set()
    or_hi = or_lo = None
    ready = False
    cur = -1
    for i, b in enumerate(bars):
        if b.date_tag != cur:
            cur = b.date_tag
            or_hi = or_lo = None
            ready = False
        if OR_START <= b.hhmm < OR_END:
            or_hi = b.high if or_hi is None else max(or_hi, b.high)
            or_lo = b.low  if or_lo is None else min(or_lo, b.low)
            continue
        if b.hhmm >= OR_END and or_hi is not None:
            if not ready:
                sessions.add(b.date_tag)
            ready = True
        if not ready:
            continue
        if b.hhmm < bt.RTH_OPEN or b.hhmm >= bt.FLATTEN_HHMM:
            continue
        if b.hhmm >= bt.LATE_ENTRY_GATE:
            continue
        a = atr_v[i]
        if a <= 0 or i < bt.C_SWEEP_LB:
            continue
        c["bars_eligible"] += 1

        swept_lo = b.low < or_lo - bt.TICK
        swept_hi = b.high > or_hi + bt.TICK
        if not (swept_lo or swept_hi):
            continue
        c["1_swept_or"] += 1

        recl_l = swept_lo and b.close > or_lo
        recl_s = swept_hi and b.close < or_hi
        if not (recl_l or recl_s):
            continue
        c["2_reclaimed"] += 1

        vw = vwap_v[i]
        if not ((vw <= 0) or (abs(b.close - vw) >= a * 0.35)):
            c["x_killed_vwap_edge"] += 1
            continue
        c["3_vwap_edge"] += 1

        # Survivors here still face the bull/bear + sc>=m4_min + ctrl gates,
        # which need the full Div/Imb state — not replicated. This funnel bounds
        # the geometry from ABOVE: if these counts are ~0 the lever is
        # unreachable regardless of the delta gates.
        c["side_L"] += int(recl_l)
        c["side_S"] += int(recl_s)

    return c, len(sessions), len(bars)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    print(f"{'contract':9s} {'bars':>9s} {'ORsess':>7s} {'elig':>7s} "
          f"{'swept':>7s} {'reclaim':>8s} {'vwapOK':>7s} {'killVW':>7s} "
          f"{'L':>5s} {'S':>5s}")
    for t in tags:
        p = CONTRACTS[t]
        if not p.exists():
            print(f"{t:9s} MISSING")
            continue
        c, ns, nb = funnel(t, p)
        print(f"{t:9s} {nb:>9,} {ns:>7} {c['bars_eligible']:>7,} "
              f"{c['1_swept_or']:>7,} {c['2_reclaimed']:>8,} "
              f"{c['3_vwap_edge']:>7,} {c['x_killed_vwap_edge']:>7,} "
              f"{c['side_L']:>5,} {c['side_S']:>5,}")


if __name__ == "__main__":
    main()
