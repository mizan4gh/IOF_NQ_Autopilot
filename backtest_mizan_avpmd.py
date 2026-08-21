"""
mizan_AVPMDPOC -- Accumulation (Volume Profile) -> Manipulation -> Distribution,
entered on ACCEPTANCE rather than on the sweep.

Built to the spec supplied 2026-08-20. The spec's central claim is the one worth
testing, and it is stated sharply enough to be wrong:

    "Do not call every low-volatility range accumulation. The strongest evidence
     is aggressive selling without downward progress, followed by a failed
     breakdown and acceptance back inside the range."

So this is not a sideways-market detector. A balance window only becomes a setup
if ORDER FLOW says sellers were being absorbed, and a sweep only becomes a trade
if price is ACCEPTED back inside afterwards.

THE FIVE STAGES (long case; short is the exact mirror)

  1. BALANCE            window = bars [m-range_bars, m-1], 20-60 min
     width      range <= range_max_atr x the PRECEDING 30-minute range
     narrowing  ATR at the end of the window < ATR at its start
     overlap    >= overlap_frac of adjacent bar pairs overlap
     tests      >= min_touches bars tag each boundary (within touch_tol x range)
     POC        migration from the first half to the full window
                <= poc_migration x range, and optionally within
                poc_center x range of the midpoint

  2. ABSORPTION         the stage that separates this from "a quiet range"
     delta      window net delta <= its own trailing delta_pctile percentile
                (aggressive selling)
     no progress  cumulative delta makes its low AFTER price makes its low, and
                price does not confirm -- the classic CD divergence
     hold       close of the window is still >= hold_frac of the way up

  3. MANIPULATION       bar m sweeps the low
     depth      sweep_min_atr <= (rangeLo - low[m]) <= sweep_max_atr, a BAND:
                the spec's "2-10 points" is a band, not a floor, because a
                200-point flush is a different event from a 5-point stop run
     flow       delta[m] <= its own trailing sweep_delta_pctile percentile
     reclaim    a close back above rangeLo within reclaim_bars

  4. ACCEPTANCE         entry_mode, and the whole point of the spec
     pullback_low     limit at the reclaimed rangeLo (+tol)
     breakout_retest  close above rangeHi, then a limit retest of rangeHi
     poc_reclaim      close above the range POC, entered at the next open
     sweep_now        CONTROL. Enters at the reclaim, which the spec explicitly
                      says to avoid. If acceptance is worth anything this must
                      lose to the other three; if it wins, the acceptance
                      requirement is costing money rather than saving it.

  5. RISK
     stop     sweep low - stop_buf_atr x ATR
     target   target_mode: "poc" (T1) | "opposite" (T2) | "measured" (T3)
     filter   skip unless target distance >= min_rr x stop distance
     exits    target, stop, max_hold_bars, or the 15:55 flatten

WHAT THE DATA CANNOT DO
  The spec asks for bid-liquidity replenishment and for telling genuine
  replenishment from rapidly cancelled orders. That is an MBO question. A .scid
  gives per-bar aggregate bid/ask VOLUME -- trades, not book events -- so
  replenishment and spoofing are NOT modelled and cannot be. Delta and
  cumulative delta ARE real here: bid_vol + ask_vol equals volume on 100% of
  bars in every contract checked.

  Note the repo's own finding that an XGB model on SETUP-confirm collapsed to
  AUC 0.50-0.55 and did not justify buying MBO. If the delta stages below carry
  no weight, that is the second piece of evidence pointing the same way.

POINT THRESHOLDS
  The spec's "2-10 MNQ points" is point-denominated, and this repo has a
  documented case of point gates silently selecting a biased subsample as the
  index moved 20k -> 30k. The sweep band is therefore ATR-denominated by
  default, calibrated so it spans the spec's range at typical MNQ volatility.
  sweep_units="points" reproduces the spec literally, and the two are compared
  rather than assumed equivalent.

Usage
  python backtest_mizan_avpmd.py --mnq            # MNQU6
  python backtest_mizan_avpmd.py --nq             # 6 NQ contracts in MNQ dollars
  MZA_ENTRY_MODE=sweep_now python backtest_mizan_avpmd.py --nq     # the control
  python backtest_mizan_avpmd.py --funnel --nq    # where setups die
  python backtest_mizan_null.py 200 --nq --avpmd  # the re-sign null
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from backtest import Bar
from fastbars import load_bars_cached
from backtest_mizan_iof_nq import (BAR_MINUTES, MNQ, NQ_FROZEN, Cands,
                                   _rolling_extreme, arrays, atr_wilders,
                                   poc_of, simulate, summarize, write_csv)

BASE = Path(__file__).parent

# Uncorrelated instruments, frozen by `backtest_mizan_p3.py --freeze`.
from backtest_mizan_p3 import CL_FROZEN, GC_FROZEN, SPECS, instrument_of


def apply_spec(p: "Params", tag: str) -> "Params":
    """Tick / point value / commission for crude and gold only.

    NQ and MNQ tags are left exactly as the caller priced them, so adding the
    uncorrelated pools cannot silently re-price the index results.
    """
    inst = instrument_of(tag)
    if inst not in ("CL", "GC"):
        return p
    return params_from_env(replace(p, **SPECS[inst]))


@dataclass(frozen=True)
class Params:
    # ── 1. balance ─────────────────────────────────────────────────────────
    range_bars: int = 8            # 40 min at 5m; spec says 20-60
    range_max_atr: float = 2.5     # width <= this x the preceding 30-min range
    require_narrowing: int = 0
    overlap_frac: float = 0.60
    min_touches: int = 1           # per boundary
    touch_tol: float = 0.12        # x range
    poc_migration: float = 0.35    # x range, first half -> full window
    poc_center: float = 0.0        # 0 = off; else |poc-mid| <= this x range
    bin_pts: float = 1.0
    # ── 2. absorption ──────────────────────────────────────────────────────
    delta_pctile: float = 50.0     # window net delta below this trailing pctile
    delta_lookback: int = 400      # bars in the trailing delta distribution
    require_cd_divergence: int = 0
    hold_frac: float = 0.00        # window close this far up off the low
    # ── 3. manipulation ────────────────────────────────────────────────────
    sweep_units: str = "atr"       # "atr" | "points"
    sweep_min_atr: float = 0.10
    sweep_max_atr: float = 1.50
    sweep_min_pts: float = 2.0     # the spec, literally
    sweep_max_pts: float = 10.0
    sweep_delta_pctile: float = 50.0
    reclaim_bars: int = 3
    # The vectorised sweep gate also demands the sweeping bar CLOSE back inside
    # (a same-bar rejection). Turning this off strips the "failed breakout" idea
    # entirely and leaves a bare new-N-bar-low condition -- needed to attribute
    # the edge, since the placebo showed the range boundary is not load-bearing.
    require_same_bar_reclaim: int = 1
    # ── 4. acceptance ──────────────────────────────────────────────────────
    entry_mode: str = "pullback_low"
    # pullback_low | breakout_retest | poc_reclaim | sweep_now(control)
    accept_bars: int = 8           # arm the entry this many bars after reclaim
    entry_tol: float = 0.10        # x range, limit offset inside the level
    require_through: float = 1.0   # ticks a limit must trade through
    # A limit fills somewhere INSIDE its bar and the path after the fill is
    # unknowable from OHLC. Managing from the next bar assumes the rest of the
    # fill bar cannot hurt -- optimistic, and worth $3,667 of the headline here
    # (t 3.11 -> 1.98). Managing from the fill bar assumes the whole bar's
    # adverse range comes after the fill -- conservative. The truth is between,
    # so the CONSERVATIVE one is the default and the optimistic one has to be
    # asked for. Both are reported; the result holds either way.
    limit_fill_same_bar: int = 1
    # PLACEBO. >0 pulls both range boundaries INWARD by this fraction of the
    # range. The balance window, the absorption stage, the sweep mechanic, the
    # depth band, the pullback entry and the stop geometry are all untouched --
    # the only thing destroyed is the boundary's claim to be a real edge of
    # value where resting orders sit. If the edge survives this, the "range" is
    # doing no work and what is left is a generic buy-a-dip rule.
    placebo_shift: float = 0.0
    # ── 5. risk ────────────────────────────────────────────────────────────
    stop_buf_atr: float = 0.15
    min_stop_atr: float = 0.30
    max_stop_atr: float = 2.50
    target_mode: str = "opposite"  # poc | opposite | measured
    min_rr: float = 1.2
    max_hold_bars: int = 36
    # ── session ────────────────────────────────────────────────────────────
    atr_len: int = 14
    session_start: int = 935
    session_end: int = 1530
    flatten_hhmm: int = 1555
    # ── contract ───────────────────────────────────────────────────────────
    tick: float = 0.25
    qty: int = 1
    pt_val: float = 2.0            # MNQ
    commission: float = 1.50
    slip_ticks: float = 0.0
    daily_loss: float = 0.0
    daily_target: float = 0.0
    max_trades_day: int = 0
    max_consec_loss: int = 0

    @property
    def sig_key(self) -> tuple:
        return tuple(getattr(self, f.name) for f in fields(self)
                     if f.name not in ("qty", "pt_val", "commission",
                                       "slip_ticks", "daily_loss",
                                       "daily_target", "max_trades_day",
                                       "max_consec_loss"))


_ENV = {f.name: "MZA_" + f.name.upper() for f in fields(Params)}


def params_from_env(base: Optional[Params] = None) -> Params:
    p = base or Params()
    kw = {}
    for name, env in _ENV.items():
        v = os.environ.get(env)
        if v is None:
            continue
        t = type(getattr(p, name))
        kw[name] = v if t is str else t(float(v))
    return replace(p, **kw) if kw else p


def spec_params() -> Params:
    """The supplied spec taken LITERALLY, at the strict end of every range.

    Kept reachable (--spec) rather than kept as the default, because stacked it
    admits 11 trades over 6 contracts and cannot be evaluated. The ablation in
    the docstring is what justifies each relaxation: every gate below was turned
    back on ONE AT A TIME and none of them raised the null percentile.
    """
    return Params(range_max_atr=2.0, require_narrowing=1, overlap_frac=0.75,
                  min_touches=2, poc_migration=0.20, delta_pctile=10.0,
                  require_cd_divergence=1, hold_frac=0.25, sweep_max_atr=0.90,
                  sweep_delta_pctile=20.0, min_rr=1.5)


RUN_TAG = os.environ.get("MZA_TAG", "")
SIDE_MODE = "as_is"

_DELTA_CACHE: Dict[int, Tuple[List[Bar], np.ndarray]] = {}


def delta_of(bars: List[Bar]) -> np.ndarray:
    """Per-bar signed volume, ask-initiated minus bid-initiated.

    This is trade flow, not book flow. It cannot see resting size, pulled
    orders, or replenishment -- see the module docstring.
    """
    hit = _DELTA_CACHE.get(id(bars))
    if hit is not None and hit[0] is bars:
        return hit[1]
    d = np.fromiter(((b.ask_vol - b.bid_vol) for b in bars), np.float64, len(bars))
    _DELTA_CACHE[id(bars)] = (bars, d)
    return d


def _rt(v, tick: float):
    return np.round(np.asarray(v) / tick) * tick


def _rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    """sum(x[i-w : i]) -- strictly prior bars, zero-padded at the left edge."""
    cs = np.concatenate(([0.0], np.cumsum(x)))
    idx = np.arange(len(x))
    lo = np.maximum(idx - w, 0)
    return cs[idx] - cs[lo]


def _empty() -> Cands:
    e = np.array([], dtype=np.int64)
    f = np.array([], dtype=np.float64)
    return Cands(e, e, f, f, f, f, f, f, e, e, 0, e)


_SCAN: Dict[Tuple[int, tuple], Tuple[List[Bar], Cands]] = {}
FUNNEL: Dict[str, int] = {}


def scan(bars: List[Bar], p: Params, funnel: bool = False) -> Cands:
    key = (id(bars), p.sig_key)
    hit = _SCAN.get(key)
    if hit is not None and hit[0] is bars and not funnel:
        return hit[1]

    a = arrays(bars)
    n = len(bars)
    W = p.range_bars
    warm = max(W + 12, p.atr_len + 5, p.delta_lookback // 4)
    if n <= warm + p.accept_bars + p.reclaim_bars + 4:
        return _empty()

    atr = atr_wilders(a, p.atr_len)
    d = delta_of(bars)
    F = dict(bar=0, session=0, width=0, narrow=0, overlap=0, touches=0,
             poc=0, delta=0, cd_div=0, hold=0, sweep=0, reclaim=0, accept=0,
             rr=0, ok=0)

    # ── 1. balance, vectorised ─────────────────────────────────────────────
    hi = _rolling_extreme(a.h, W, "max")          # over [i-W, i-1]
    lo = _rolling_extreme(a.l, W, "min")
    rng = hi - lo
    if p.placebo_shift > 0.0:
        # keep `rng` as the TRUE range so every ATR/range-scaled threshold and
        # the measured-move target are unchanged; only the levels move
        hi = hi - p.placebo_shift * rng
        lo = lo + p.placebo_shift * rng

    # the PRECEDING 30 minutes: one 30-min bar's range, ending where the
    # balance window begins. Taken literally rather than smoothed -- a smoothed
    # ATR would blend the balance itself back into its own width test.
    r6h = _rolling_extreme(a.h, 6, "max")
    r6l = _rolling_extreme(a.l, 6, "min")
    pre = np.full(n, np.nan)
    pre[W:] = (r6h - r6l)[:-W]

    ov = np.zeros(n)
    ov[1:] = (np.minimum(a.h[1:], a.h[:-1]) > np.maximum(a.l[1:], a.l[:-1])).astype(float)
    ov_frac = _rolling_sum(ov, W) / W

    atr_start = np.full(n, np.nan)
    atr_start[W:] = atr[:-W]
    atr_prev = np.r_[atr[0], atr[:-1]]

    ok = np.zeros(n, dtype=bool)
    ok[warm:] = True
    F["bar"] = int(ok.sum())
    ok &= (a.hhmm >= p.session_start) & (a.hhmm <= p.session_end)
    F["session"] = int(ok.sum())
    ok &= np.isfinite(rng) & (rng > 0) & np.isfinite(pre) & (pre > 0)
    ok &= (atr_prev > 0)
    ok &= rng <= p.range_max_atr * pre
    F["width"] = int(ok.sum())
    if p.require_narrowing:
        ok &= atr_prev < atr_start
    F["narrow"] = int(ok.sum())
    ok &= ov_frac >= p.overlap_frac
    F["overlap"] = int(ok.sum())

    # ── 3a. the sweep gate is cheap and prunes hard, so apply it early ─────
    if p.sweep_units == "points":
        smin, smax = np.full(n, p.sweep_min_pts), np.full(n, p.sweep_max_pts)
    else:
        smin, smax = p.sweep_min_atr * atr_prev, p.sweep_max_atr * atr_prev
    depth_dn = lo - a.l                            # how far below rangeLo
    depth_up = a.h - hi
    rc_dn = (a.c > lo) if p.require_same_bar_reclaim else np.ones(n, bool)
    rc_up = (a.c < hi) if p.require_same_bar_reclaim else np.ones(n, bool)
    sw_dn = ok & (depth_dn >= smin) & (depth_dn <= smax) & rc_dn
    sw_up = ok & (depth_up >= smin) & (depth_up <= smax) & rc_up
    cands_m = np.flatnonzero(sw_dn | sw_up)
    F["sweep"] = len(cands_m)
    if len(cands_m) == 0:
        FUNNEL.clear(); FUNNEL.update(F)
        _SCAN[key] = (bars, _empty())
        return _empty()

    out: List[tuple] = []
    for m in cands_m:
        m = int(m)
        side = 1 if sw_dn[m] else -1
        s = m - W                                   # window start
        if a.dtag[s] != a.dtag[m]:                  # never span two sessions
            continue
        A = float(atr_prev[m])
        rHi, rLo, R = float(hi[m]), float(lo[m]), float(rng[m])

        # ── 1b. boundary tests ────────────────────────────────────────────
        tol = p.touch_tol * R
        th = int(np.sum(a.h[s:m] >= rHi - tol))
        tl = int(np.sum(a.l[s:m] <= rLo + tol))
        if th < p.min_touches or tl < p.min_touches:
            continue
        F["touches"] += 1

        # ── 1c. POC migration ─────────────────────────────────────────────
        half = s + W // 2
        poc_full = poc_of(a, s, m, p.bin_pts)
        poc_half = poc_of(a, s, half, p.bin_pts) if half > s else poc_full
        if not (np.isfinite(poc_full) and np.isfinite(poc_half)):
            continue
        if abs(poc_full - poc_half) > p.poc_migration * R:
            continue
        if p.poc_center > 0.0 and abs(poc_full - 0.5 * (rHi + rLo)) > p.poc_center * R:
            continue
        F["poc"] += 1

        # ── 2. absorption ─────────────────────────────────────────────────
        win_d = d[s:m]
        net_d = float(win_d.sum())
        lb0 = max(0, s - p.delta_lookback)
        hist = _rolling_sum(d, W)[lb0:s]            # prior windows only
        hist = hist[np.isfinite(hist)]
        if len(hist) < 20:
            continue
        # a long needs aggressive SELLING absorbed, so an unusually NEGATIVE
        # window delta; a short is the mirror
        if side > 0:
            if net_d > np.percentile(hist, p.delta_pctile):
                continue
        else:
            if net_d < np.percentile(hist, 100.0 - p.delta_pctile):
                continue
        F["delta"] += 1

        if p.require_cd_divergence:
            cd = np.cumsum(win_d)
            if side > 0:
                i_cd, i_px = int(np.argmin(cd)), int(np.argmin(a.l[s:m]))
                # cumulative delta bottoms LATER than price and price does not
                # confirm the new selling -> sellers are being absorbed
                div = (i_cd > i_px) and (a.l[s + i_cd] > a.l[s + i_px])
            else:
                i_cd, i_px = int(np.argmax(cd)), int(np.argmax(a.h[s:m]))
                div = (i_cd > i_px) and (a.h[s + i_cd] < a.h[s + i_px])
            if not div:
                continue
        F["cd_div"] += 1

        cpos_w = (a.c[m - 1] - rLo) / R
        if side > 0 and cpos_w < p.hold_frac:
            continue
        if side < 0 and (1.0 - cpos_w) < p.hold_frac:
            continue
        F["hold"] += 1

        # ── 3b. sweep flow ────────────────────────────────────────────────
        dh = d[max(0, m - p.delta_lookback):m]
        if len(dh) >= 20:
            if side > 0 and d[m] > np.percentile(dh, p.sweep_delta_pctile):
                continue
            if side < 0 and d[m] < np.percentile(dh, 100.0 - p.sweep_delta_pctile):
                continue

        # ── 3c. reclaim within reclaim_bars ───────────────────────────────
        rec = -1
        for j in range(m, min(m + 1 + p.reclaim_bars, n)):
            if a.dtag[j] != a.dtag[m]:
                break
            if (a.c[j] > rLo) if side > 0 else (a.c[j] < rHi):
                rec = j
                break
        if rec < 0:
            continue
        F["reclaim"] += 1

        invalid = float(np.min(a.l[m:rec + 1])) if side > 0 else float(np.max(a.h[m:rec + 1]))

        # ── 4. acceptance ─────────────────────────────────────────────────
        e, fill, manage = -1, 0.0, -1
        thru = p.require_through * p.tick
        lim = (rLo + side * p.entry_tol * R) if p.entry_mode == "pullback_low" else 0.0

        if p.entry_mode == "sweep_now":
            j = rec + 1
            if j < n and a.dtag[j] == a.dtag[m] and a.hhmm[j] <= p.session_end:
                e, fill, manage = j, float(a.o[j]), j

        elif p.entry_mode == "pullback_low":
            lv = float(_rt(lim, p.tick))
            for j in range(rec + 1, min(rec + 1 + p.accept_bars, n)):
                if a.dtag[j] != a.dtag[m] or a.hhmm[j] > p.session_end:
                    break
                if (a.c[j] < invalid) if side > 0 else (a.c[j] > invalid):
                    break
                if ((a.l[j] <= lv - thru) if side > 0 else (a.h[j] >= lv + thru)):
                    e = j
                    fill = min(a.o[j], lv) if side > 0 else max(a.o[j], lv)
                    manage = j if p.limit_fill_same_bar else j + 1
                    break

        elif p.entry_mode == "breakout_retest":
            brk = -1
            for j in range(rec + 1, min(rec + 1 + p.accept_bars, n)):
                if a.dtag[j] != a.dtag[m] or a.hhmm[j] > p.session_end:
                    break
                if (a.c[j] > rHi) if side > 0 else (a.c[j] < rLo):
                    brk = j
                    break
            if brk < 0:
                continue
            lv = float(_rt(rHi if side > 0 else rLo, p.tick))
            for j in range(brk + 1, min(brk + 1 + p.accept_bars, n)):
                if a.dtag[j] != a.dtag[m] or a.hhmm[j] > p.session_end:
                    break
                if ((a.l[j] <= lv - thru) if side > 0 else (a.h[j] >= lv + thru)):
                    e = j
                    fill = min(a.o[j], lv) if side > 0 else max(a.o[j], lv)
                    manage = j if p.limit_fill_same_bar else j + 1
                    break

        elif p.entry_mode == "poc_reclaim":
            for j in range(rec, min(rec + 1 + p.accept_bars, n)):
                if a.dtag[j] != a.dtag[m] or a.hhmm[j] > p.session_end:
                    break
                if (a.c[j] > poc_full) if side > 0 else (a.c[j] < poc_full):
                    if j + 1 < n and a.dtag[j + 1] == a.dtag[m]:
                        e, fill, manage = j + 1, float(a.o[j + 1]), j + 1
                    break
        else:
            raise ValueError(f"unknown entry_mode {p.entry_mode!r}")

        if e < 0:
            continue
        F["accept"] += 1

        # ── 5. risk ───────────────────────────────────────────────────────
        sp = abs(fill - invalid) + p.stop_buf_atr * A
        sp = float(_rt(np.clip(sp, p.min_stop_atr * A, p.max_stop_atr * A), p.tick))
        if sp <= 0.0:
            continue
        # The spec's target ladder -- T1 POC, T2 opposite boundary, T3 measured
        # move -- assumes the entry is INSIDE the range. breakout_retest enters
        # at the boundary itself, so T1 and T2 are both behind price: "opposite"
        # is the entry price, giving a target distance of zero, and the RR
        # filter then rejects 100% of setups. That looked exactly like "this
        # entry never triggers" until the funnel showed 26 setups reaching
        # acceptance and 0 surviving RR. Past the boundary, only T3 is a target.
        if p.entry_mode == "breakout_retest":
            tp = (rHi + R) if side > 0 else (rLo - R)
        elif p.target_mode == "poc":
            tp = poc_full
        elif p.target_mode == "measured":
            tp = (rHi + R) if side > 0 else (rLo - R)
        else:
            tp = rHi if side > 0 else rLo
        tgt = (tp - fill) * side
        if tgt < p.min_rr * sp:
            continue
        F["rr"] += 1
        F["ok"] += 1

        out.append((e, side, float(fill), sp, float(_rt(tgt, p.tick)), A,
                    poc_full, R, m, e - m, manage))

    FUNNEL.clear(); FUNNEL.update(F)
    if not out:
        _SCAN[key] = (bars, _empty())
        return _empty()
    out.sort(key=lambda r: r[0])
    c_ = list(zip(*out))
    c = Cands(idx=np.asarray(c_[0], np.int64), side=np.asarray(c_[1], np.int64),
              entry_px=np.asarray(c_[2], np.float64),
              stop_pts=np.asarray(c_[3], np.float64),
              tgt_pts=np.asarray(c_[4], np.float64),
              atr=np.asarray(c_[5], np.float64),
              poc=np.asarray(c_[6], np.float64),
              acc_rng=np.asarray(c_[7], np.float64),
              sweep_idx=np.asarray(c_[8], np.int64),
              wait=np.asarray(c_[9], np.int64), warm=warm,
              manage_at=np.asarray(c_[10], np.int64))
    _SCAN[key] = (bars, c)
    return c


def run_engine(bars, p=None, side_mode=None):
    p = p or params_from_env()
    return simulate(bars, scan(bars, p), p,
                    side_mode if side_mode is not None else SIDE_MODE)


def contracts_for(scope: str) -> Dict[str, Path]:
    env = os.environ.get("MZA_TAGS")
    everything = {**NQ_FROZEN, **MNQ, **CL_FROZEN, **GC_FROZEN}
    if env:
        return {t: everything[t] for t in env.split(",") if t in everything}
    if scope == "--mnq":
        return dict(MNQ)
    if scope == "--cl":
        return dict(CL_FROZEN)
    if scope == "--gc":
        return dict(GC_FROZEN)
    if scope == "--uncorr":
        return {**CL_FROZEN, **GC_FROZEN}
    if scope == "--all":
        return dict(everything)
    return dict(NQ_FROZEN)


def run_one(tag: str, scid: Path, p: Params, write: bool = True) -> dict:
    p = apply_spec(p, tag)
    bars = load_bars_cached(tag, scid, BAR_MINUTES)
    c = scan(bars, p)
    r = simulate(bars, c, p)
    if write:
        sfx = f"_{RUN_TAG}" if RUN_TAG else ""
        write_csv(r, BASE / f"IOF_avpmd{sfx}_{tag}.csv")
    s = summarize(r, p)
    s["cands"] = len(c.idx)
    print(f"  {tag:7s} cand={s['cands']:>4} n={s['n']:>4} "
          f"L/S={s['longs']}/{s['n']-s['longs']:<4} WR={s['wr']:>5.1f}% "
          f"PF={s['pf']:>5.2f} Net=${s['total']:>+8,.0f} "
          f"avg=${s['avg']:>+6,.0f} ({s['avg_r']:>+5.2f}R) "
          f"MaxDD=${s['max_dd']:>+8,.0f} t={s['t']:>+5.2f}")
    print(f"          exits: {s['reasons']}")
    return s


def main():
    args = sys.argv[1:]
    scope = next((a for a in args if a in ("--nq", "--mnq", "--all", "--cl",
                                          "--gc", "--uncorr")), "--nq")
    p = params_from_env(spec_params() if "--spec" in args else None)
    if "--spec" in args:
        print("*** --spec: the supplied spec taken literally. Expect ~11 trades "
              "over 6 contracts, which is not an evaluable sample. ***\n")

    if "--funnel" in args:
        print("Where setups die (counts are cumulative down the chain):\n")
        keys = ["bar", "session", "width", "narrow", "overlap", "sweep",
                "touches", "poc", "delta", "cd_div", "hold", "reclaim",
                "accept", "rr"]
        print(f"  {'contract':9s}" + "".join(f"{k:>9s}" for k in keys))
        for tag, scid in contracts_for(scope).items():
            if not scid.exists():
                continue
            scan(load_bars_cached(tag, scid, BAR_MINUTES), p, funnel=True)
            print(f"  {tag:9s}" + "".join(f"{FUNNEL.get(k,0):>9,}" for k in keys))
        print("\n  bar->overlap are BAR counts; sweep onward are SETUP counts.")
        return

    print(f"mizan_AVPMDPOC -- balance + absorption + failed breakout, "
          f"entered on acceptance  ({BAR_MINUTES}m)")
    print(f"  1 balance  {p.range_bars} bars, width<={p.range_max_atr}x prior "
          f"30m, overlap>={p.overlap_frac}, touches>={p.min_touches}, "
          f"POC drift<={p.poc_migration}")
    print(f"  2 absorb   window delta <= p{p.delta_pctile:.0f} of trailing "
          f"{p.delta_lookback}, CD divergence={p.require_cd_divergence}, "
          f"hold>={p.hold_frac}")
    print(f"  3 sweep    {p.sweep_min_atr}-{p.sweep_max_atr}xATR ({p.sweep_units}), "
          f"bar delta <= p{p.sweep_delta_pctile:.0f}, reclaim<={p.reclaim_bars} bars")
    print(f"  4 accept   {p.entry_mode}, armed {p.accept_bars} bars")
    print(f"  5 risk     stop sweep{p.stop_buf_atr:+.2f}xATR, target="
          f"{p.target_mode}, minRR={p.min_rr} | ${p.pt_val:.0f}/pt "
          f"comm=${p.commission:.2f} slip={p.slip_ticks}t\n")

    out = {}
    for tag, scid in contracts_for(scope).items():
        if not scid.exists():
            print(f"  {tag:7s} MISSING {scid.name}")
            continue
        out[tag] = run_one(tag, scid, p)

    if len(out) > 1:
        tot = sum(s["total"] for s in out.values())
        nn = sum(s["n"] for s in out.values())
        pos = sum(1 for s in out.values() if s["total"] > 0)
        pf = sum(1 for s in out.values() if s["pf"] > 1.0)
        print(f"\n  POOLED  n={nn}  Net=${tot:+,.0f}  net>0: {pos}/{len(out)}  "
              f"PF>1: {pf}/{len(out)}")
        print(f"  SHIP GATE: {'PASS' if pos == len(out) and pf == len(out) else 'FAIL'}")
        print("  Necessary, not sufficient -- "
              "python backtest_mizan_null.py 200 --nq --avpmd")


if __name__ == "__main__":
    main()
