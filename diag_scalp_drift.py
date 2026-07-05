#!/usr/bin/env python3
"""
diag_scalp_drift.py — measure raw forward mid-price drift after order-flow
imbalance events, BEFORE designing any entry/exit. Answers: is the 65%-flow
signal momentum (join it) or reversion (fade it), at what horizon, and is the
magnitude big enough to clear retail costs (~1.3-1.8 ticks round trip)?

Buckets: side x imbalance strength x horizon. Also splits by spread==1t.
Usage: python diag_scalp_drift.py NQZ25-CME.scid
"""
import sys, numpy as np
from scalper_imbalance import (read_scid, rth_mask_and_days, window_sum,
                               TICK, IMB_WIN_MS, BURST_WIN_MS, MIN_FLOW,
                               BURST_MIN, RTH_OPEN, RTH_CLOSE)

HORIZONS_MS = [1000, 5000, 15000, 60000, 120000, 300000]
IMB_BANDS   = [(0.85, 0.90), (0.90, 0.95), (0.95, 1.01)]
FLOW_BANDS  = [(30, 100), (100, 300), (300, 10**9)]   # wtot contracts in 500ms
COOLDOWN_US = 1_000_000


def main(path):
    recs = read_scid(path)
    recs = recs[recs["tot_vol"] > 0]
    t     = recs["dt"].astype(np.int64)
    price = recs["close"].astype(np.float64)
    bid   = recs["low"].astype(np.float64)
    ask   = recs["high"].astype(np.float64)
    bvol  = recs["bid_vol"].astype(np.int64)
    avol  = recs["ask_vol"].astype(np.int64)

    rth, _ = rth_mask_and_days(t)
    cb, ca = np.cumsum(bvol), np.cumsum(avol)
    wb  = window_sum(cb, t, IMB_WIN_MS * 1000)
    wa  = window_sum(ca, t, IMB_WIN_MS * 1000)
    wtot = wb + wa
    frac_buy = np.where(wtot > 0, wa / np.maximum(wtot, 1), 0.5)
    enough = wtot >= MIN_FLOW
    bb = window_sum(cb, t, BURST_WIN_MS * 1000)
    ba = window_sum(ca, t, BURST_WIN_MS * 1000)

    quote_ok = (price >= bid) & (price <= ask) & (bid > 0) & (ask > 0)
    spread_t = np.round((ask - bid) / TICK).astype(int)
    mid = (bid + ask) / 2.0

    n = len(t)
    print(f"{path}: {n} ticks, RTH frac {rth.mean():.2f}")
    hdr = "side imb_band   spread    n      " + "".join(f"{h/1000:>7.2f}s" for h in HORIZONS_MS)
    print(hdr)

    for side, lbl in ((1, "BUY "), (-1, "SELL")):
        f = frac_buy if side == 1 else 1 - frac_buy
        burst = (ba if side == 1 else bb) >= BURST_MIN
        for lo, hi in IMB_BANDS:
            base = enough & (f > lo) & (f <= hi) & burst & quote_ok & rth
            for fl_lo, fl_hi in FLOW_BANDS:
                sp_lbl = f"fl{fl_lo}-{'inf' if fl_hi > 10**6 else fl_hi}"
                sp_mask = (wtot >= fl_lo) & (wtot < fl_hi)
                idx = np.where(base & sp_mask)[0]
                # cooldown dedup
                keep, last = [], -10**18
                for i in idx:
                    if t[i] - last >= COOLDOWN_US:
                        keep.append(i); last = t[i]
                idx = np.array(keep, dtype=np.int64)
                if len(idx) < 30:
                    print(f"{lbl} {lo:.2f}-{hi:.2f} {sp_lbl:6s} {len(idx):6d}   (too few)")
                    continue
                row = f"{lbl} {lo:.2f}-{hi:.2f} {sp_lbl:6s} {len(idx):6d} "
                m0 = mid[idx]
                for h in HORIZONS_MS:
                    j = np.searchsorted(t, t[idx] + h * 1000, side="right") - 1
                    j = np.clip(j, idx, n - 1)
                    drift = (mid[j] - m0) / TICK * side
                    row += f"{np.mean(drift):+7.2f}"
                print(row)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "NQZ25-CME.scid")
