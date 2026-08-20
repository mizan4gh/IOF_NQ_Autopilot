"""
Mizan_IOF_NQ_P3 -- the DAILY power-of-three: overnight accumulation, opening
manipulation of a FROZEN level, RTH distribution, entered on the pullback to the
frozen overnight POC.

WHY THIS EXISTS
  The rolling intraday version (backtest_mizan_iof_nq.py) is falsified: 14
  configurations all landed between the 37th and 75th percentile of their own
  re-sign null, and the entry-agnostic control came in at the 42nd. That test
  used ROLLING windows -- a 12-bar range and a POC recomputed every bar. This
  repo's level-taxonomy finding is that rolling computed levels do not carry the
  edge that FROZEN ones do, so the one axis the falsification did not cover is
  the daily framing, where every level is fixed at 09:30 and never moves again.

  That is the whole point of this file. It is the same A/M/D story told once per
  day against levels that stop updating, instead of continuously against levels
  that never stop.

THE SESSION MODEL
  A "session day" runs 18:00 ET to 17:59 ET. Bars are stamped on the ET calendar
  date, so the 18:00-23:59 block carries the PREVIOUS date and has to be pushed
  forward one trading date to be grouped with the morning it belongs to. The
  next date is taken from the dates actually present in the file, which handles
  weekends and holidays without a calendar.

    ACCUMULATION  18:00 -> 09:29   the overnight range. Nothing is required of
                                   its shape by default -- in the daily framing
                                   the overnight IS the accumulation. The
                                   on_max_rng_atr filter exists but is OFF, so
                                   the pattern is measured before the filter.
    MANIPULATION  09:30 -> manip_end
    DISTRIBUTION  and the entry, through session_end, flat at 15:55.

THE FROZEN LEVELS -- all computed BEFORE 09:30 and never updated
    ONH / ONL   overnight high / low
    ON_POC      point of control of the overnight volume profile
    PDH / PDL   prior session's RTH high / low
  Downside liquidity = {ONL, PDL}, upside = {ONH, PDH}.

THE RULES (long case; short is the exact mirror)
  M. Some RTH bar j at or before manip_end takes out a downside frozen level L
     by more than sweep_eps_atr x ATR and closes back above it, closing in the
     upper sweep_close_pos of its own range. First such bar in the day wins, and
     it is the only setup the day gets. low[j] is the invalidation.
  D. Within dist_bars, a close lands dist_min_atr x ATR clear of ON_POC on the
     upside. Aborted if a close first goes back under low[j] -- that is the
     sweep succeeding, which is a different day.
  E. A resting limit at ON_POC + poc_tol_atr x ATR, armed pb_bars bars, filled
     only if a bar trades THROUGH it by require_through ticks, at min(open, lvl).

  ATR is frozen too: the value on the last bar before 09:30. Every threshold is
  an ATR multiple, never a point count -- the index went 20k to 30k across these
  six contracts and a points-denominated gate silently selects a biased
  subsample when it does.

  STOP    low[j] - stop_buf_atr x ATR, distance clamped to
          [min_stop_atr, max_stop_atr] x ATR and the price rebuilt from it.
  TARGET  target_mode="r"      target_r x the stop distance
          target_mode="level"  the nearest frozen level on the far side (ONH or
                               PDH), which is where the distribution leg is
                               actually aiming; falls back to the R target when
                               no level sits far enough away.
  No time stop by default: the daily thesis is that the distribution leg runs
  into the close, so the trade is held to 15:55 unless the bracket resolves it.

CONTROL
  entry_mode="confirm" takes the same setups at the next bar's open after
  confirmation, skipping the POC entirely. If the sequence carries direction,
  that sees it; if only the POC entry works, the POC is doing the work; if
  neither does, the pattern is dead. Run it before believing anything here.

RESULTS (2026-08-20, 6 frozen NQ contracts, 387 sessions, $20/pt, $5 RT)
  The pattern as specified does NOT work. What is inside it does.

    entry_mode   n    pooled net   null pctile
    poc         66      -$3,675       21.5%     the full A->M->D->POC chain
    confirm    112     +$13,390       63.5%     A->M->D, POC entry removed
    sweep      253     +$62,730       99.0%     A->M only, D and POC removed

  So each stage AFTER the manipulation destroys value. The overnight range and
  the prior day's range locate liquidity; the sweep-and-reclaim of one of those
  frozen levels is the signal; waiting for a distribution close throws away most
  of the move and the POC pullback throws away the rest, because the setups that
  come back to value are disproportionately the ones that failed.

  The sweep result survives what was thrown at it: 3 ticks of slippage (98.5th,
  +$49,875), OOS-2026 3/3 at 99.5th, either level family alone (ON 94.0th, PD
  97.5th), and every target and window variant tried (15 cells, 89.5-100th, all
  reported in the thread -- none dropped). inverse is -$28k to -$59k throughout,
  which is the signature of a direction call doing real work.

  It also passes the placebo: pulling every level inward by a fraction of the
  overnight range, which preserves the sweep mechanic and the trade count
  (259/266/251 vs 253) while destroying the level's structural meaning, takes it
  to the 83.0th / 19.5th / 18.0th percentile. The frozen levels are load-bearing,
  not decoration.

  THE OPEN PROBLEM: it does not port to ES. Same rules, all thresholds already
  ATR-denominated, $50/pt: -$392 over 276 trades, 41.5th percentile, 3/6. ES and
  NQ are 0.954 correlated on daily returns, so ES was only ever a portability
  check and never independent evidence -- but a genuine liquidity-structure
  effect ought to show up in both, and it does not. Resolve that before any cpp.

Usage
  python backtest_mizan_p3.py --nq
  MZ3_ENTRY_MODE=sweep python backtest_mizan_p3.py --nq
  MZ3_ENTRY_MODE=sweep MZ3_PLACEBO_SHIFT=0.2 python backtest_mizan_p3.py --nq
  python backtest_mizan_null.py 200 --nq --p3
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

# The rolling-window thread owns the shared machinery: bar arrays, Wilder ATR,
# the look-ahead-free bar-volume profile, the governed walk and the reporting.
# Only the pattern is different here, so only the pattern is re-written.
from backtest_mizan_iof_nq import (BAR_MINUTES, ES_FROZEN, MNQ, NQ_FROZEN, TICK,
                                   Cands, arrays, atr_wilders, poc_of,
                                   round_to_tick, simulate, summarize,
                                   write_csv)

BASE = Path(__file__).parent

IS_TAGS = ("NQU25", "NQZ25", "NQM5")
OOS_TAGS = ("NQH6", "NQM6", "NQU26")


@dataclass(frozen=True)
class Params:
    # ── session geometry ───────────────────────────────────────────────────
    on_start: int = 1800          # bars at/after this belong to the NEXT day
    rth_open: int = 930
    rth_close: int = 1600
    manip_end: int = 1200         # sweep must happen by here
    session_end: int = 1530       # no fill after this
    flatten_hhmm: int = 1555
    min_on_bars: int = 60         # 5h of overnight, or the day is skipped
    min_rth_bars: int = 40        # prior RTH must be real for PDH/PDL
    # ── accumulation filter (OFF by default: the overnight IS accumulation) ─
    on_max_rng_atr: float = 0.0   # >0: skip days whose ON range exceeds K x ATR
    # ── manipulation ───────────────────────────────────────────────────────
    use_on_levels: int = 1        # ONH / ONL in the liquidity set
    use_pd_levels: int = 1        # PDH / PDL in the liquidity set
    sweep_eps_atr: float = 0.05
    sweep_close_pos: float = 0.5
    # PLACEBO. >0 pulls every liquidity level INWARD by this fraction of the
    # overnight range: downside levels rise, upside levels fall. The sweep
    # mechanic, the stop geometry, the target and the session are all untouched
    # -- the only thing destroyed is the level's claim to be a structural price
    # where resting orders actually sit. If the edge survives this, it is
    # "price reverts after poking any line" and the frozen levels are doing no
    # work; if it dies, the structure is load-bearing. Run it before believing
    # any level-based result.
    placebo_shift: float = 0.0
    # ── distribution ───────────────────────────────────────────────────────
    dist_bars: int = 12
    dist_min_atr: float = 0.5
    # ── entry ──────────────────────────────────────────────────────────────
    # "poc"     the strategy as specified
    # "confirm" CONTROL: next bar's open after the distribution close
    # "sweep"   CONTROL: next bar's open after the manipulation bar, with the
    #           distribution stage skipped entirely. This is the only variant
    #           with real statistical power -- ~0.83 setups/day against 0.17 for
    #           the full chain -- so it, not the P/L of the full chain, is what
    #           actually decides whether a frozen-level sweep-and-reclaim
    #           predicts direction.
    entry_mode: str = "poc"       # "poc" | "confirm" | "sweep"
    pb_bars: int = 12
    poc_tol_atr: float = 0.10
    require_through: float = 1.0
    bin_pts: float = 1.0
    # ── stop / target ──────────────────────────────────────────────────────
    stop_buf_atr: float = 0.15
    min_stop_atr: float = 0.40
    max_stop_atr: float = 2.50
    target_mode: str = "r"        # "r" | "level"
    target_r: float = 2.0
    min_level_tgt_r: float = 1.0  # a level nearer than this is not a target
    max_hold_bars: int = 0        # 0 = hold to the 15:55 flatten
    atr_len: int = 14
    # ── sizing / governors (simulate() reads these by name) ────────────────
    qty: int = 1
    pt_val: float = 20.0
    commission: float = 5.00
    slip_ticks: float = 0.0
    daily_loss: float = 0.0
    daily_target: float = 0.0
    max_trades_day: int = 0
    max_consec_loss: int = 0
    # simulate() reads session_start when clamping nothing; kept for parity
    session_start: int = 930

    @property
    def sig_key(self) -> tuple:
        return tuple(getattr(self, f) for f in SIG_FIELDS)


SIG_FIELDS = tuple(f.name for f in fields(Params)
                   if f.name not in ("qty", "pt_val", "commission",
                                     "slip_ticks", "daily_loss", "daily_target",
                                     "max_trades_day", "max_consec_loss"))

_ENV_ALIASES = {f.name: "MZ3_" + f.name.upper() for f in fields(Params)}


def params_from_env(base: Optional[Params] = None) -> Params:
    p = base or Params()
    kw = {}
    for name, env in _ENV_ALIASES.items():
        v = os.environ.get(env)
        if v is None:
            continue
        t = type(getattr(p, name))
        kw[name] = v if t is str else t(float(v))
    return replace(p, **kw) if kw else p


RUN_TAG = os.environ.get("MZ3_TAG", "")
SIDE_MODE = "as_is"


# ── session grouping ────────────────────────────────────────────────────────
@dataclass
class Sessions:
    starts: np.ndarray      # bar index each session day begins at (18:00)
    ends: np.ndarray        # one past the last bar of that session day
    on_end: np.ndarray      # one past the last OVERNIGHT bar (= 09:30 open)
    rth_end: np.ndarray     # one past the last RTH bar
    sid: np.ndarray         # session ordinal, for adjacency checks


def sessions(a) -> Sessions:
    """Group bars into 18:00 -> 17:59 trading days.

    The evening block carries the previous ET calendar date, so it is pushed one
    date forward -- to the next date PRESENT IN THE FILE, which is what makes
    weekends, holidays and the repo's known .scid coverage gaps fall out for
    free instead of needing a calendar.
    """
    uniq, inv = np.unique(a.dtag, return_inverse=True)
    sid = inv + (a.hhmm >= 1800).astype(np.int64)
    keep = sid < len(uniq)                 # last evening has no morning
    if not keep.all():
        sid = sid.copy()
        sid[~keep] = -1
    # bars are time-ordered, so sid is non-decreasing and its groups contiguous
    assert np.all(np.diff(sid[sid >= 0]) >= 0), "session ids not monotone"

    idx = np.flatnonzero(sid >= 0)
    if len(idx) == 0:
        e = np.array([], dtype=np.int64)
        return Sessions(e, e, e, e, e)
    s = sid[idx]
    g0 = idx[np.flatnonzero(np.r_[True, s[1:] != s[:-1]])]
    g1 = np.r_[g0[1:], idx[-1] + 1]

    on_end = np.empty(len(g0), dtype=np.int64)
    rth_end = np.empty(len(g0), dtype=np.int64)
    for k in range(len(g0)):
        lo, hi = int(g0[k]), int(g1[k])
        hh = a.hhmm[lo:hi]
        # overnight = the contiguous prefix of 18:00-23:59 then 00:00-09:29
        is_on = (hh >= 1800) | (hh < 930)
        nf = np.flatnonzero(~is_on)
        on_end[k] = lo + (int(nf[0]) if len(nf) else hi - lo)
        rth = np.flatnonzero((hh >= 930) & (hh < 1600))
        rth_end[k] = lo + int(rth[-1]) + 1 if len(rth) else on_end[k]
    return Sessions(starts=g0, ends=g1, on_end=on_end, rth_end=rth_end,
                    sid=sid[g0])


_SCAN_CACHE: Dict[Tuple[int, tuple], Tuple[List[Bar], Cands]] = {}
_SESS_CACHE: Dict[int, Tuple[List[Bar], Sessions]] = {}


def sessions_cached(bars: List[Bar]) -> Sessions:
    hit = _SESS_CACHE.get(id(bars))
    if hit is not None and hit[0] is bars:
        return hit[1]
    s = sessions(arrays(bars))
    _SESS_CACHE[id(bars)] = (bars, s)
    return s


def _empty() -> Cands:
    e = np.array([], dtype=np.int64)
    f = np.array([], dtype=np.float64)
    return Cands(e, e, f, f, f, f, f, f, e, e, 0)


# ── the scan ────────────────────────────────────────────────────────────────
def scan(bars: List[Bar], p: Params) -> Cands:
    """One setup per session day, at most. Pure function of bars + params."""
    key = (id(bars), p.sig_key)
    hit = _SCAN_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]

    a = arrays(bars)
    n = len(bars)
    S = sessions_cached(bars)
    atr = atr_wilders(a, p.atr_len)
    rng = a.h - a.l
    with np.errstate(divide="ignore", invalid="ignore"):
        cpos = np.where(rng > 0, (a.c - a.l) / rng, 0.0)

    out: List[tuple] = []
    for k in range(1, len(S.starts)):
        if S.sid[k] != S.sid[k - 1] + 1:      # a gap: no trustworthy prior RTH
            continue
        g0, on_end, rth_end = int(S.starts[k]), int(S.on_end[k]), int(S.rth_end[k])
        if on_end - g0 < p.min_on_bars or rth_end <= on_end:
            continue
        pg0, p_on_end, p_rth_end = (int(S.starts[k - 1]), int(S.on_end[k - 1]),
                                    int(S.rth_end[k - 1]))
        if p_rth_end - p_on_end < p.min_rth_bars:
            continue

        # ── frozen at 09:30, never updated again ───────────────────────────
        A = float(atr[on_end - 1])
        if not np.isfinite(A) or A <= 0.0:
            continue
        onh = float(a.h[g0:on_end].max())
        onl = float(a.l[g0:on_end].min())
        if p.on_max_rng_atr > 0.0 and (onh - onl) > p.on_max_rng_atr * A:
            continue
        poc = poc_of(a, g0, on_end, p.bin_pts)
        if not np.isfinite(poc):
            continue
        pdh = float(a.h[p_on_end:p_rth_end].max())
        pdl = float(a.l[p_on_end:p_rth_end].min())

        downs = ([onl] if p.use_on_levels else []) + ([pdl] if p.use_pd_levels else [])
        ups = ([onh] if p.use_on_levels else []) + ([pdh] if p.use_pd_levels else [])
        if not downs or not ups:
            continue
        if p.placebo_shift > 0.0:
            sh = p.placebo_shift * (onh - onl)
            downs = [L + sh for L in downs]
            ups = [L - sh for L in ups]

        # ── M: first frozen-level sweep-and-reclaim of the day ─────────────
        eps = p.sweep_eps_atr * A
        m, side, lvl = -1, 0, 0.0
        for j in range(on_end, rth_end):
            if a.hhmm[j] > p.manip_end:
                break
            hit_dn = [L for L in downs if a.l[j] < L - eps and a.c[j] > L]
            if hit_dn and cpos[j] >= p.sweep_close_pos:
                m, side, lvl = j, 1, max(hit_dn)
                break
            hit_up = [L for L in ups if a.h[j] > L + eps and a.c[j] < L]
            if hit_up and (1.0 - cpos[j]) >= p.sweep_close_pos:
                m, side, lvl = j, -1, min(hit_up)
                break
        if m < 0:
            continue
        invalid = a.l[m] if side > 0 else a.h[m]

        # ── D: a close clear of the frozen overnight POC ───────────────────
        if p.entry_mode == "sweep":
            d = m                        # stage skipped: the sweep IS the entry
        else:
            d = -1
            lim_c = poc + side * p.dist_min_atr * A
            for j in range(m + 1, min(m + 1 + p.dist_bars, rth_end)):
                if (a.c[j] < invalid) if side > 0 else (a.c[j] > invalid):
                    break
                if (a.c[j] > lim_c) if side > 0 else (a.c[j] < lim_c):
                    d = j
                    break
            if d < 0:
                continue

        # ── E: pullback to the frozen POC ──────────────────────────────────
        e, fill = -1, 0.0
        if p.entry_mode in ("confirm", "sweep"):
            j = d + 1
            if j < rth_end and a.hhmm[j] <= p.session_end:
                e, fill = j, float(a.o[j])
        else:
            lv = float(round_to_tick(poc + side * p.poc_tol_atr * A))
            thru = p.require_through * TICK
            for j in range(d + 1, min(d + 1 + p.pb_bars, rth_end)):
                if a.hhmm[j] > p.session_end:
                    break
                if (a.c[j] < invalid) if side > 0 else (a.c[j] > invalid):
                    break
                if ((a.l[j] <= lv - thru) if side > 0 else (a.h[j] >= lv + thru)):
                    e = j
                    fill = min(a.o[j], lv) if side > 0 else max(a.o[j], lv)
                    break
        if e < 0:
            continue

        raw = abs(fill - invalid) + p.stop_buf_atr * A
        sp = float(round_to_tick(np.clip(raw, p.min_stop_atr * A,
                                         p.max_stop_atr * A)))
        if sp <= 0.0:
            continue

        tgt = sp * p.target_r
        if p.target_mode == "level":
            # the far-side frozen liquidity is what the distribution leg aims at
            far = [L for L in (ups if side > 0 else downs)
                   if (L - fill) * side >= p.min_level_tgt_r * sp]
            if far:
                tgt = abs((min(far) if side > 0 else max(far)) - fill)
        out.append((e, side, float(fill), sp, float(round_to_tick(tgt)), A, poc,
                    onh - onl, m, e - m,
                    e if p.entry_mode in ("confirm", "sweep") else e + 1))

    if not out:
        c = _empty()
        _SCAN_CACHE[key] = (bars, c)
        return c

    out.sort(key=lambda r: r[0])
    col = list(zip(*out))
    c = Cands(idx=np.asarray(col[0], np.int64), side=np.asarray(col[1], np.int64),
              entry_px=np.asarray(col[2], np.float64),
              stop_pts=np.asarray(col[3], np.float64),
              tgt_pts=np.asarray(col[4], np.float64),
              atr=np.asarray(col[5], np.float64),
              poc=np.asarray(col[6], np.float64),
              acc_rng=np.asarray(col[7], np.float64),   # overnight range
              sweep_idx=np.asarray(col[8], np.int64),
              wait=np.asarray(col[9], np.int64), warm=0,
              manage_at=np.asarray(col[10], np.int64))
    _SCAN_CACHE[key] = (bars, c)
    return c


def run_engine(bars: List[Bar], p: Optional[Params] = None,
               side_mode: Optional[str] = None):
    p = p or params_from_env()
    return simulate(bars, scan(bars, p), p,
                    side_mode if side_mode is not None else SIDE_MODE)


def contracts_for(scope: str) -> Dict[str, Path]:
    env = os.environ.get("MZ3_TAGS")
    everything = {**NQ_FROZEN, **MNQ, **ES_FROZEN}
    if env:
        return {t: everything[t] for t in env.split(",") if t in everything}
    if scope == "--mnq":
        return dict(MNQ)
    if scope == "--es":
        return dict(ES_FROZEN)
    if scope == "--is":
        return {t: everything[t] for t in IS_TAGS}
    if scope == "--oos":
        return {t: everything[t] for t in OOS_TAGS}
    if scope == "--all":
        return {**NQ_FROZEN, **MNQ}
    return dict(NQ_FROZEN)


def run_one(tag: str, scid: Path, p: Params, write: bool = True) -> dict:
    bars = load_bars_cached(tag, scid, BAR_MINUTES)
    c = scan(bars, p)
    r = simulate(bars, c, p)
    if write:
        suffix = f"_{RUN_TAG}" if RUN_TAG else ""
        write_csv(r, BASE / f"IOF_mizanP3{suffix}_{tag}.csv")
    s = summarize(r)
    s["cands"] = len(c.idx)
    s["sessions"] = max(len(sessions_cached(bars).starts) - 1, 0)
    print(f"  {tag:7s} sess={s['sessions']:>4} cand={s['cands']:>4} "
          f"n={s['n']:>4} L/S={s['longs']}/{s['n']-s['longs']:<4} "
          f"WR={s['wr']:>5.1f}% PF={s['pf']:>5.2f} Net=${s['total']:>+9,.0f} "
          f"avg=${s['avg']:>+7,.0f} ({s['avg_r']:>+5.2f}R) "
          f"MaxDD=${s['max_dd']:>+9,.0f} t={s['t']:>+5.2f}")
    print(f"          exits: {s['reasons']}")
    return s


def main():
    args = sys.argv[1:]
    scope = next((a for a in args if a.startswith("--")), "--nq")
    p = params_from_env()

    lv = ("ONH/ONL" if p.use_on_levels else "") + \
         ("+PDH/PDL" if p.use_pd_levels else "")
    entry = {"poc": f"limit at ON_POC{p.poc_tol_atr:+.2f}xATR, armed "
                    f"{p.pb_bars} bars",
             "confirm": "CONTROL -- next open after the distribution close",
             "sweep": "CONTROL -- next open after the sweep, D stage SKIPPED",
             }.get(p.entry_mode, p.entry_mode)
    print("Mizan_IOF_NQ_P3 -- daily power-of-three on FROZEN levels "
          f"({BAR_MINUTES}m)")
    print(f"  A: overnight 18:00-09:29 (>={p.min_on_bars} bars) -> frozen "
          f"{lv} + ON_POC + ATR")
    print(f"  M: sweep a frozen level by >{p.sweep_eps_atr}xATR and reclaim, "
          f"by {p.manip_end} | D: close {p.dist_min_atr}xATR clear of ON_POC "
          f"within {p.dist_bars} bars")
    print(f"  E: {entry} | stop sweep{p.stop_buf_atr:+.2f}xATR "
          f"clamp[{p.min_stop_atr},{p.max_stop_atr}]xATR | "
          f"tgt={p.target_mode}({p.target_r}R) | flat {p.flatten_hhmm} | "
          f"${p.pt_val:.0f}/pt comm=${p.commission:.2f} slip={p.slip_ticks}t\n")

    out = {}
    for tag, scid in contracts_for(scope).items():
        if not scid.exists():
            print(f"  {tag:7s} MISSING {scid.name}")
            continue
        out[tag] = run_one(tag, scid, p)

    if len(out) > 1:
        tot = sum(s["total"] for s in out.values())
        nn = sum(s["n"] for s in out.values())
        sess = sum(s["sessions"] for s in out.values())
        pos = sum(1 for s in out.values() if s["total"] > 0)
        pf_ok = sum(1 for s in out.values() if s["pf"] > 1.0)
        print(f"\n  POOLED  n={nn} over {sess} sessions "
              f"({nn/max(sess,1):.2f}/day)  Net=${tot:+,.0f}  "
              f"net>0: {pos}/{len(out)}  PF>1: {pf_ok}/{len(out)}")
        print(f"  SHIP GATE (net>0 AND PF>1 on every contract): "
              f"{'PASS' if pos == len(out) and pf_ok == len(out) else 'FAIL'}")
        print("  Necessary, not sufficient -- "
              "python backtest_mizan_null.py 200 --nq --p3")


if __name__ == "__main__":
    main()
