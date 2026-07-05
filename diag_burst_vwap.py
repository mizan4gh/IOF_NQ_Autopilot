#!/usr/bin/env python3
"""
diag_burst_vwap.py — forward mid-drift of mega-burst events (>=300 contracts /
>=90% one-sided in 500ms) conditioned on where price sits vs RTH-anchored VWAP.

Cells: burst side x VWAP relation (above +4t / near ±4t / below -4t).
"with"    = buy burst above VWAP, sell burst below (trend-aligned)
"counter" = buy burst below VWAP, sell burst above (reclaim/fade-side)

Reports mean AND median drift (tail check), n, per horizon.
Usage: python diag_burst_vwap.py NQZ25-CME.scid
"""
import sys, numpy as np
from scalper_imbalance import read_scid, rth_mask_and_days, window_sum, TICK

IMB_MIN, FLOW_MIN = 0.90, 300
IMB_WIN_MS        = 500
MAX_SPREAD_T      = 6
NEAR_T            = 4
HORIZONS_MS       = [1000, 5000, 15000, 60000, 120000]
COOLDOWN_US       = 1_000_000


def main(path):
    recs = read_scid(path)
    recs = recs[recs["tot_vol"] > 0]
    t     = recs["dt"].astype(np.int64)
    price = recs["close"].astype(np.float64)
    bid   = recs["low"].astype(np.float64)
    ask   = recs["high"].astype(np.float64)
    bvol  = recs["bid_vol"].astype(np.int64)
    avol  = recs["ask_vol"].astype(np.int64)
    tvol  = recs["tot_vol"].astype(np.int64)
    n = len(t)

    rth, date_tag = rth_mask_and_days(t)

    # RTH-anchored session VWAP (per local date, RTH ticks only)
    vwap = np.full(n, np.nan)
    ri = np.where(rth)[0]
    dts = date_tag[ri]
    day_starts = np.where(np.diff(dts, prepend=dts[0] - 1) != 0)[0]
    pv = price[ri] * tvol[ri]
    cpv, cv = np.cumsum(pv), np.cumsum(tvol[ri])
    for s_i, s in enumerate(day_starts):
        e = day_starts[s_i + 1] if s_i + 1 < len(day_starts) else len(ri)
        base_pv = cpv[s - 1] if s > 0 else 0.0
        base_v  = cv[s - 1] if s > 0 else 0
        seg = slice(s, e)
        vwap[ri[seg]] = (cpv[seg] - base_pv) / np.maximum(cv[seg] - base_v, 1)

    cb, ca = np.cumsum(bvol), np.cumsum(avol)
    wb = window_sum(cb, t, IMB_WIN_MS * 1000)
    wa = window_sum(ca, t, IMB_WIN_MS * 1000)
    wtot = wb + wa
    frac_buy = np.where(wtot > 0, wa / np.maximum(wtot, 1), 0.5)
    quote_ok = (price >= bid) & (price <= ask) & (bid > 0) & (ask > 0)
    spread_ok = np.round((ask - bid) / TICK) <= MAX_SPREAD_T
    big = (wtot >= FLOW_MIN) & quote_ok & spread_ok & rth

    mid = (bid + ask) / 2.0
    dist_t = (price - vwap) / TICK

    print(f"{path}")
    print("side vwap_rel     n     " + "".join(f"{h/1000:>6.0f}s_mean{h/1000:>5.0f}s_med" for h in HORIZONS_MS))
    for side, lbl in ((1, "BUY "), (-1, "SELL")):
        sig = big & ((frac_buy if side == 1 else 1 - frac_buy) >= IMB_MIN)
        for rel, rmask in (("above", dist_t > NEAR_T),
                           ("near ", np.abs(dist_t) <= NEAR_T),
                           ("below", dist_t < -NEAR_T)):
            idx = np.where(sig & rmask & np.isfinite(vwap))[0]
            keep, last = [], -10**18
            for i in idx:
                if t[i] - last >= COOLDOWN_US:
                    keep.append(i); last = t[i]
            idx = np.array(keep, dtype=np.int64)
            if len(idx) < 15:
                print(f"{lbl} {rel}  {len(idx):5d}   (too few)")
                continue
            row = f"{lbl} {rel}  {len(idx):5d} "
            m0 = mid[idx]
            for h in HORIZONS_MS:
                j = np.searchsorted(t, t[idx] + h * 1000, side="right") - 1
                j = np.clip(j, idx, n - 1)
                d = (mid[j] - m0) / TICK * side
                row += f"{np.mean(d):+11.2f}{np.median(d):+10.2f}"
            print(row)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "NQZ25-CME.scid")
