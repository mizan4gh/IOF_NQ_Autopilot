#!/usr/bin/env python3
"""
scalper_imbalance.py — tick-level passive order-flow-imbalance scalper (LONG + SHORT)

Spec (user, 2026-07-04), both sides symmetric:

  LONG:  bid imbalance > 65%, aggressive buying appears, spread = 1 tick,
         volatility normal, no news spike
         -> passive buy limit at best bid
         -> TP +2..4 ticks; cancel if unfilled in 300 ms
         -> exit if book flips bearish, aggressive selling returns,
            or price moves 2-4 ticks against

  SHORT: ask imbalance > 65%, market sells hitting bid, spread = 1 tick,
         volatility normal, no news spike
         -> passive sell limit at best ask
         -> cover +2..4 ticks lower; cancel if unfilled in 300 ms
         -> exit if book flips bullish, aggressive buying returns,
            or price moves 2-4 ticks against

DATA-FIDELITY LIMITS (read before trusting output):
  .scid tick records carry trade price, quoted best bid/ask, and AGGRESSOR-side
  volume only — there is NO book depth (no MBO). "Book imbalance" is therefore
  proxied by rolling aggressor-flow imbalance; "imbalance" and "aggressive
  buying/selling appears" collapse into one flow signal + a short-window burst.
  Passive-fill simulation is queue-blind:
    --mode through  = fill only when price trades THROUGH the limit (worst case:
                      heavy adverse selection, guaranteed-fill assumption)
    --mode touch    = fill once QUEUE_AHEAD contracts print at/inside the limit
                      (optimistic: assumes decent queue position)
  Real fill economics sit between the two modes. A 300 ms order lifecycle also
  assumes co-located-grade latency; retail Sierra->CQG round trips eat most of
  that budget.

Usage:
  python scalper_imbalance.py F.US.ENQH26.scid --tp 2 --stop 3 --mode through
  python scalper_imbalance.py --sweep          # 3-contract x tp{2,3,4} x mode grid
"""
import argparse, math, os, struct, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

# ── SCID layout (same as backtest.py) ───────────────────────────────────────
SCID_HDR_SIZE = 56
SCID_REC_SIZE = 40
SCID_DTYPE = np.dtype([
    ("dt",       "<i8"),
    ("open",     "<f4"),
    ("high",     "<f4"),
    ("low",      "<f4"),
    ("close",    "<f4"),
    ("n_trades", "<u4"),
    ("tot_vol",  "<u4"),
    ("bid_vol",  "<u4"),
    ("ask_vol",  "<u4"),
])
SC_EPOCH_US = int((datetime(1970, 1, 1, tzinfo=timezone.utc)
                   - datetime(1899, 12, 30, tzinfo=timezone.utc)).total_seconds() * 1e6)
ET = ZoneInfo("America/New_York")

# ── contract / cost constants ───────────────────────────────────────────────
TICK          = 0.25
TICK_VAL      = 5.0          # $ per tick per contract (NQ)
COMMISSION_RT = 4.00         # $ round turn, all-in

# ── strategy parameters (defaults per spec) ─────────────────────────────────
IMB_WIN_MS    = 500          # rolling flow-imbalance window
IMB_FRAC      = 0.65         # > 65% one-sided flow
MIN_FLOW      = 30           # min contracts in window for imbalance to count
BURST_WIN_MS  = 200          # "aggressive buying/selling appears"
BURST_MIN     = 15           # contracts of same-side aggression in burst window
SPREAD_TICKS  = 1            # spec: spread = 1 tick
VOL_WIN_MS    = 2000         # "volatility normal" lookback
VOL_MAX_TICKS = 8            # 2s range above this = spike (news filter proxy)
VOL_MIN_TICKS = 1            # below this = dead tape
ORDER_TTL_MS  = 300          # cancel unfilled order
MAX_HOLD_MS   = 15000        # hard time-stop on open position
COOLDOWN_MS   = 1000         # min gap between order attempts
FLIP_FRAC     = 0.60         # opposite-side flow fraction that counts as "book flips"
QUEUE_AHEAD   = 25           # touch mode: contracts that must print at limit first
RTH_OPEN      = 935          # match house convention (09:35-15:55 ET)
RTH_CLOSE     = 1555


def read_scid(path: str) -> np.ndarray:
    with open(path, "rb") as f:
        hdr = f.read(SCID_HDR_SIZE)
    assert hdr[:4] == b"SCID", f"bad header in {path}"
    with open(path, "rb") as f:
        f.seek(SCID_HDR_SIZE)
        raw = f.read()
    n = len(raw) // SCID_REC_SIZE
    return np.frombuffer(raw[: n * SCID_REC_SIZE], dtype=SCID_DTYPE)


def rth_mask_and_days(t_us: np.ndarray):
    """Boolean RTH mask + per-tick YYYYMMDD tags, DST-correct via per-day offsets."""
    epoch_us = t_us - SC_EPOCH_US                      # unix µs
    day_idx = epoch_us // 86_400_000_000               # coarse UTC day bucket
    mask = np.zeros(len(t_us), bool)
    date_tag = np.zeros(len(t_us), np.int32)
    for d in np.unique(day_idx):
        sel = day_idx == d
        mid = datetime.fromtimestamp(int(epoch_us[sel][0]) / 1e6, tz=timezone.utc)
        off_us = int(mid.astimezone(ET).utcoffset().total_seconds() * 1e6)
        loc = epoch_us[sel] + off_us
        secs = (loc // 1_000_000) % 86_400
        hhmm = (secs // 3600) * 100 + (secs % 3600) // 60
        mask[sel] = (hhmm >= RTH_OPEN) & (hhmm < RTH_CLOSE)
        days = loc // 86_400_000_000
        base = datetime(1970, 1, 1)
        tags = np.zeros(len(days), np.int32)
        for dd in np.unique(days):
            dloc = base + timedelta(days=int(dd))
            tags[days == dd] = dloc.year * 10000 + dloc.month * 100 + dloc.day
        date_tag[sel] = tags
    return mask, date_tag


def window_sum(cum: np.ndarray, t: np.ndarray, win_us: int) -> np.ndarray:
    """Rolling sum over trailing time window via cumsum + searchsorted."""
    j = np.searchsorted(t, t - win_us, side="left")
    return cum - np.where(j > 0, cum[j - 1], 0)


_PREP_CACHE = {}

def _prepare(path: str):
    if path in _PREP_CACHE:
        return _PREP_CACHE[path]
    recs = read_scid(path)
    recs = recs[recs["tot_vol"] > 0]

    t     = recs["dt"].astype(np.int64)
    price = recs["close"].astype(np.float64)
    bid   = recs["low"].astype(np.float64)
    ask   = recs["high"].astype(np.float64)
    bvol  = recs["bid_vol"].astype(np.int64)   # sell-aggressor volume
    avol  = recs["ask_vol"].astype(np.int64)   # buy-aggressor volume

    rth, date_tag = rth_mask_and_days(t)

    cb, ca = np.cumsum(bvol), np.cumsum(avol)
    wb  = window_sum(cb, t, IMB_WIN_MS * 1000)
    wa  = window_sum(ca, t, IMB_WIN_MS * 1000)
    wtot = wb + wa
    with np.errstate(invalid="ignore", divide="ignore"):
        frac_buy = np.where(wtot > 0, wa / np.maximum(wtot, 1), 0.5)
    enough = wtot >= MIN_FLOW

    bb  = window_sum(cb, t, BURST_WIN_MS * 1000)
    ba  = window_sum(ca, t, BURST_WIN_MS * 1000)

    spread_ok = np.abs((ask - bid) - SPREAD_TICKS * TICK) < TICK * 0.01
    quote_ok  = (price >= bid) & (price <= ask) & (bid > 0) & (ask > 0)

    long_sig  = enough & (frac_buy > IMB_FRAC)       & (ba >= BURST_MIN) & spread_ok & quote_ok & rth
    short_sig = enough & (1 - frac_buy > IMB_FRAC)   & (bb >= BURST_MIN) & spread_ok & quote_ok & rth

    flip_bear = enough & (1 - frac_buy > FLIP_FRAC)  # book flips bearish -> exits longs
    flip_bull = enough & (frac_buy > FLIP_FRAC)      # book flips bullish -> exits shorts

    cand = np.where(long_sig | short_sig)[0]
    prep = (t, price, bid, ask, bvol, avol, date_tag,
            long_sig, flip_bear, flip_bull, cand)
    _PREP_CACHE.clear()          # hold at most one file (they're ~1 GB each)
    _PREP_CACHE[path] = prep
    return prep


def run(path: str, tp_ticks=2, stop_ticks=3, mode="through", verbose=True,
        hold_ms=MAX_HOLD_MS):
    (t, price, bid, ask, bvol, avol, date_tag,
     long_sig, flip_bear, flip_bull, cand) = _prepare(path)
    n = len(t)

    ttl_us, hold_us = ORDER_TTL_MS * 1000, hold_ms * 1000
    vol_us = VOL_WIN_MS * 1000

    trades = []          # (side, entry_px, exit_px, reason, date_tag, ticks)
    attempts = fills = 0
    next_ok_t = 0

    for i in cand:
        ti = t[i]
        if ti < next_ok_t:
            continue
        # volatility-normal check (lazy — only at candidates)
        v0 = np.searchsorted(t, ti - vol_us, side="left")
        seg = price[v0:i + 1]
        rng_t = (seg.max() - seg.min()) / TICK if len(seg) > 1 else 0
        if not (VOL_MIN_TICKS <= rng_t <= VOL_MAX_TICKS):
            continue

        side = 1 if long_sig[i] else -1
        lim  = bid[i] if side == 1 else ask[i]
        attempts += 1
        next_ok_t = ti + COOLDOWN_MS * 1000

        # ── fill scan (order lives ORDER_TTL_MS) ────────────────────────────
        jend = np.searchsorted(t, ti + ttl_us, side="right")
        fill_j = -1
        qty = 0
        for j in range(i + 1, jend):
            if (side == 1 and flip_bear[j]) or (side == -1 and flip_bull[j]):
                break                      # cancel: book flipped before fill
            if mode == "through":
                if (side == 1 and price[j] < lim - TICK * 0.01) or \
                   (side == -1 and price[j] > lim + TICK * 0.01):
                    fill_j = j; break
            else:  # touch: count contracts printing at/inside our limit
                if side == 1 and price[j] <= lim + TICK * 0.01 and bvol[j] > 0:
                    qty += bvol[j]
                elif side == -1 and price[j] >= lim - TICK * 0.01 and avol[j] > 0:
                    qty += avol[j]
                if qty >= QUEUE_AHEAD:
                    fill_j = j; break
        if fill_j < 0:
            continue                       # cancelled after 300 ms / flip
        fills += 1

        # ── position management ─────────────────────────────────────────────
        tp_px   = lim + side * tp_ticks * TICK
        stop_px = lim - side * stop_ticks * TICK
        kend = np.searchsorted(t, t[fill_j] + hold_us, side="right")
        kend = min(kend, n)
        exit_px, reason = None, "TIME"
        for k in range(fill_j + 1, kend):
            mkt_out = bid[k] if side == 1 else ask[k]   # cross-spread exit px
            # 1. hard adverse stop (checked first — conservative)
            if (side == 1 and bid[k] <= stop_px + TICK * 0.01) or \
               (side == -1 and ask[k] >= stop_px - TICK * 0.01):
                exit_px, reason = mkt_out, "STOP"; break
            # 2. book flips against us
            if (side == 1 and flip_bear[k]) or (side == -1 and flip_bull[k]):
                exit_px, reason = mkt_out, "FLIP"; break
            # 3. take profit at limit
            if mode == "through":
                hit = price[k] > tp_px + TICK * 0.01 if side == 1 else \
                      price[k] < tp_px - TICK * 0.01
            else:
                hit = price[k] >= tp_px - TICK * 0.01 if side == 1 else \
                      price[k] <= tp_px + TICK * 0.01
            if hit:
                exit_px, reason = tp_px, "TP"; break
        if exit_px is None:
            k = kend - 1
            exit_px = bid[k] if side == 1 else ask[k]
        ticks = (exit_px - lim) / TICK * side
        trades.append((side, lim, exit_px, reason, date_tag[fill_j], ticks))
        next_ok_t = max(next_ok_t, t[min(k, n - 1)] + COOLDOWN_MS * 1000)

    return summarize(path, tp_ticks, stop_ticks, mode, attempts, fills, trades, verbose)


def summarize(path, tp, stop, mode, attempts, fills, trades, verbose):
    name = os.path.basename(path)
    if not trades:
        line = f"{name:22s} tp{tp} stop{stop} {mode:8s} attempts={attempts:6d} fills=0  — no trades"
        print(line)
        return dict(file=name, tp=tp, stop=stop, mode=mode, attempts=attempts,
                    fills=0, net=0.0, pf=float("nan"), wr=float("nan"), n=0)
    tk   = np.array([x[5] for x in trades])
    side = np.array([x[0] for x in trades])
    gross = tk * TICK_VAL
    net   = gross - COMMISSION_RT
    wins, losses = net[net > 0], net[net <= 0]
    pf = wins.sum() / max(1e-9, -losses.sum()) if len(losses) else float("inf")
    days = len(set(x[4] for x in trades))
    reasons = {}
    for x in trades:
        reasons[x[3]] = reasons.get(x[3], 0) + 1
    line = (f"{name:22s} tp{tp} stop{stop} {mode:8s} "
            f"att={attempts:6d} fill={len(trades):5d} ({len(trades)/max(1,attempts)*100:4.1f}%) "
            f"WR={len(wins)/len(trades)*100:5.1f}% avg={tk.mean():+5.2f}t "
            f"net=${net.sum():+10,.0f} PF={pf:4.2f} days={days} "
            f"L/S={int((side==1).sum())}/{int((side==-1).sum())} {reasons}")
    print(line)
    if verbose:
        for s, lbl in ((1, "LONG "), (-1, "SHORT")):
            m = side == s
            if m.sum():
                print(f"    {lbl}: n={m.sum():5d} WR={(net[m]>0).mean()*100:5.1f}% "
                      f"avg={tk[m].mean():+5.2f}t net=${net[m].sum():+10,.0f}")
    return dict(file=name, tp=tp, stop=stop, mode=mode, attempts=attempts,
                fills=len(trades), net=float(net.sum()), pf=float(pf),
                wr=float(len(wins) / len(trades)), n=len(trades))


SWEEP_FILES = ["NQZ25-CME.scid", "F.US.ENQM25.scid", "F.US.ENQH26.scid"]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scid", nargs="?", default=None)
    ap.add_argument("--tp", type=int, default=2)
    ap.add_argument("--stop", type=int, default=3)
    ap.add_argument("--mode", choices=["through", "touch"], default="through")
    ap.add_argument("--hold", type=int, default=MAX_HOLD_MS, help="max hold ms")
    ap.add_argument("--tps", default="2,3,4", help="sweep TP list, e.g. 4,6,8")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    if args.sweep:
        base = os.path.dirname(os.path.abspath(__file__))
        tps = [int(x) for x in args.tps.split(",")]
        for f in SWEEP_FILES:
            p = os.path.join(base, f)
            if not os.path.exists(p):
                print(f"skip {f} (missing)"); continue
            for md in ("through", "touch"):
                for tp in tps:
                    run(p, tp_ticks=tp, stop_ticks=args.stop, mode=md,
                        verbose=False, hold_ms=args.hold)
    else:
        if not args.scid:
            print("need a .scid path or --sweep"); sys.exit(1)
        run(args.scid, tp_ticks=args.tp, stop_ticks=args.stop, mode=args.mode,
            hold_ms=args.hold)
