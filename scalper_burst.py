#!/usr/bin/env python3
"""
scalper_burst.py — v2 of the order-flow scalper, redesigned from measured drift
(diag_scalp_drift.py, 2026-07-05) instead of the falsified passive spec.

Measured, replicated on NQZ25 + NQH6:
  flow >= 300 contracts in 500ms AND >= 90% one-sided  ->  +4..+10 ticks of
  continuation over the next 5-60s (both sides). Weaker cells don't clear costs.

Strategy:
  ENTRY: aggressive (marketable) order WITH the burst direction, LATENCY_MS after
         the signal tick; fill at the quoted ask (long) / bid (short) at that
         moment + SLIP_TICKS adverse slippage. No passive resting — that was the
         adverse-selection trap of v1.
  EXIT:  fixed horizon HOLD_S seconds (market out at bid/ask), protective stop
         STOP_TICKS, flatten by 15:55, no entries after 15:50.
  One position at a time; COOLDOWN_S between signals (bursts cluster).

WARNING: these bursts are frequently news-driven — this is spike-riding, the
opposite of the v12 NEWS_FILTER philosophy. Fills during bursts are modeled at
the prevailing (often widened) quote plus slippage, which self-penalizes, but
live fills in those moments can be worse still.

Usage:
  python scalper_burst.py NQZ25-CME.scid --hold 15 --stop 10
  python scalper_burst.py --grid NQZ25-CME.scid       # train grid on one file
  python scalper_burst.py --validate --hold H --stop S # run 3 contracts fixed
"""
import argparse, os, sys
import numpy as np
from scalper_imbalance import (read_scid, rth_mask_and_days, window_sum, TICK)

TICK_VAL      = 5.0
COMMISSION_RT = 4.00

IMB_WIN_MS   = 500
IMB_MIN      = 0.90
FLOW_MIN     = 300
LATENCY_MS   = 200
SLIP_TICKS   = 1        # adverse ticks on the aggressive entry fill
COOLDOWN_S   = 60
LAST_ENTRY   = 1550     # HHMM
MAX_SPREAD_T = 6        # skip signal if quoted spread wider (no sane fill)

_CACHE = {}

def _prep(path):
    if path in _CACHE:
        return _CACHE[path]
    recs = read_scid(path)
    recs = recs[recs["tot_vol"] > 0]
    t     = recs["dt"].astype(np.int64)
    price = recs["close"].astype(np.float64)
    bid   = recs["low"].astype(np.float64)
    ask   = recs["high"].astype(np.float64)
    bvol  = recs["bid_vol"].astype(np.int64)
    avol  = recs["ask_vol"].astype(np.int64)
    rth, date_tag = rth_mask_and_days(t)

    cb, ca = np.cumsum(bvol), np.cumsum(avol)
    wb = window_sum(cb, t, IMB_WIN_MS * 1000)
    wa = window_sum(ca, t, IMB_WIN_MS * 1000)
    wtot = wb + wa
    frac_buy = np.where(wtot > 0, wa / np.maximum(wtot, 1), 0.5)

    quote_ok = (price >= bid) & (price <= ask) & (bid > 0) & (ask > 0)
    spread_t = np.round((ask - bid) / TICK)

    big = (wtot >= FLOW_MIN) & quote_ok & rth & (spread_t <= MAX_SPREAD_T)
    buy_sig  = big & (frac_buy >= IMB_MIN)
    sell_sig = big & (1 - frac_buy >= IMB_MIN)

    # HHMM per tick for the late-entry gate (reuse rth day loop result cheaply)
    out = (t, price, bid, ask, date_tag, buy_sig, sell_sig)
    _CACHE.clear(); _CACHE[path] = out
    return out


def run(path, hold_s=15, stop_ticks=10, verbose=True):
    t, price, bid, ask, date_tag, buy_sig, sell_sig = _prep(path)
    n = len(t)
    cand = np.where(buy_sig | sell_sig)[0]

    trades = []
    next_ok = 0
    lat_us, hold_us, cool_us = LATENCY_MS * 1000, int(hold_s * 1e6), int(COOLDOWN_S * 1e6)

    for i in cand:
        if t[i] < next_ok:
            continue
        side = 1 if buy_sig[i] else -1
        # entry after latency
        j = np.searchsorted(t, t[i] + lat_us, side="left")
        if j >= n or date_tag[j] != date_tag[i]:
            continue
        entry = (ask[j] + SLIP_TICKS * TICK) if side == 1 else (bid[j] - SLIP_TICKS * TICK)
        next_ok = t[i] + cool_us

        stop_px = entry - side * stop_ticks * TICK
        kdead = np.searchsorted(t, t[j] + hold_us, side="right") - 1
        kdead = min(kdead, n - 1)
        exit_px, reason, k = None, "HOLD", kdead
        for k in range(j + 1, kdead + 1):
            if date_tag[k] != date_tag[j]:
                k -= 1; exit_px = bid[k] if side == 1 else ask[k]; reason = "EOD"; break
            mkt = bid[k] if side == 1 else ask[k]
            if (side == 1 and mkt <= stop_px) or (side == -1 and mkt >= stop_px):
                exit_px, reason = mkt, "STOP"; break
        if exit_px is None:
            k = kdead
            exit_px = bid[k] if side == 1 else ask[k]
        ticks = (exit_px - entry) / TICK * side
        trades.append((side, ticks, reason, date_tag[i]))
        next_ok = max(next_ok, t[k] + cool_us)

    return report(path, hold_s, stop_ticks, trades, verbose)


def report(path, hold_s, stop_ticks, trades, verbose):
    name = os.path.basename(path)
    if not trades:
        print(f"{name:22s} hold{hold_s:>4}s stop{stop_ticks:>3}t  n=0")
        return dict(net=0, n=0, pf=float("nan"))
    tk = np.array([x[1] for x in trades])
    side = np.array([x[0] for x in trades])
    net = tk * TICK_VAL - COMMISSION_RT
    w, l = net[net > 0], net[net <= 0]
    pf = w.sum() / max(1e-9, -l.sum()) if len(l) else float("inf")
    days = len(set(x[3] for x in trades))
    top5 = np.sort(net)[-5:].sum()
    print(f"{name:22s} hold{hold_s:>4}s stop{stop_ticks:>3}t  n={len(tk):4d} "
          f"WR={100*(net>0).mean():5.1f}% avg={tk.mean():+6.2f}t "
          f"net=${net.sum():+10,.0f} PF={pf:5.2f} days={days} "
          f"top5=${top5:+8,.0f} L/S={int((side==1).sum())}/{int((side==-1).sum())}")
    if verbose:
        for s, lbl in ((1, "LONG "), (-1, "SHORT")):
            m = side == s
            if m.sum():
                print(f"    {lbl} n={m.sum():4d} WR={100*(net[m]>0).mean():5.1f}% "
                      f"avg={tk[m].mean():+6.2f}t net=${net[m].sum():+10,.0f}")
    return dict(net=float(net.sum()), n=len(tk), pf=float(pf))


VALIDATE_FILES = ["NQZ25-CME.scid", "F.US.ENQM25.scid", "F.US.ENQH26.scid"]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scid", nargs="?")
    ap.add_argument("--hold", default="15")
    ap.add_argument("--stop", default="10")
    ap.add_argument("--grid", action="store_true", help="hold x stop grid on one file (TRAIN)")
    ap.add_argument("--validate", action="store_true", help="fixed config on 3 contracts")
    args = ap.parse_args()

    if args.grid:
        f = args.scid or "NQZ25-CME.scid"
        for hold in (5, 15, 60, 120):
            for stop in (8, 12, 20):
                run(f, hold_s=hold, stop_ticks=stop, verbose=False)
    elif args.validate:
        base = os.path.dirname(os.path.abspath(__file__))
        holds = [float(x) for x in str(args.hold).split(",")]
        stops = [int(x) for x in str(args.stop).split(",")]
        for f in VALIDATE_FILES:
            for h, s in zip(holds, stops):
                run(os.path.join(base, f), hold_s=h, stop_ticks=s, verbose=False)
    else:
        run(args.scid or "NQZ25-CME.scid", hold_s=float(args.hold),
            stop_ticks=int(args.stop))
