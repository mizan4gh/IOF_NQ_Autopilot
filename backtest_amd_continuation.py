"""
Accumulation -> volume profile -> MANIPULATION -> continuation entry.

THE PATTERN, AS DRAWN (long case; short is the exact mirror)
  1. ACCUMULATION.  30 minutes on the 5-minute chart -- six bars.  Its high,
     low and volume profile are FROZEN the moment the window ends and never
     update again.
  2. VOLUME PROFILE.  POC, VAH and VAL of that window, built from the six
     bars' volume.  These are the lines "drawn on the chart".
  3. MANIPULATION.  A bar takes out one edge of the accumulation -- trades
     through it by more than sweep_eps_atr x ATR -- and CLOSES BACK INSIDE,
     in the rejecting half of its own range.  A failed break.  The side that
     got swept is the side that was trapped.
  4. CONTINUATION.  Price then closes clear of the OPPOSITE edge.  That is the
     real move: the manipulation picked the direction, this confirms it.
  5. ENTRY.  A resting limit at the frozen profile level on the pullback, in
     the direction of the continuation.  Stop the far side of the
     accumulation, target target_r x that distance.

WHAT IS NEW HERE, AND WHAT IS ALREADY FALSIFIED
  Both halves of this have been measured in this repo and neither survived:

    backtest_consol_poc.py     30-min window -> break -> POC pullback,
                               CONTINUATION, no manipulation step.
                               t=+0.55 (clock) / +0.12 (compress), gate 3/6,
                               and the geometric-midpoint placebo scored a
                               BETTER gate (4/6).  NO SHIP.
    backtest_mizan_iof_nq.py   rolling accumulation -> manipulation ->
                               distribution -> POC entry, a FADE.  14 configs,
                               every one 37th-75th null percentile.  The
                               entry-agnostic control lost $10k, which is what
                               proved the A/M/D sequence itself carries no
                               direction.
    backtest_mizan_avpmd.py    AMD + POC pullback: a bare "buy an N-bar low"
                               baseline BEAT the full five-stage strategy.

  The one cell none of them covers is this one: the manipulation used as a
  DIRECTION FILTER on a range break, with the entry taken WITH the break
  rather than against it.  consol_poc broke out with no trap requirement;
  mizan_iof_nq required the trap but entered on the retrace to value without
  ever requiring the range to actually go.  Requiring both -- trap one side,
  then leave through the other -- is a different and testable claim: that a
  breakout preceded by a failed break in the opposite direction is a better
  breakout than one that is not.

  The prior is bad.  Read the CONTROLS before the P/L.

THE CONTROLS AND THE PLACEBO -- pre-registered, before any number was seen
  Four runs decide this, and each one deletes exactly one ingredient:

    manip     PRIMARY.  The pattern as specified.
    nomanip   CONTROL.  Identical, except the manipulation is not required --
              the first close beyond either edge sets the direction.  This is
              backtest_consol_poc.py re-expressed inside this file, and it is
              the load-bearing comparison: if `manip` does not beat it, the
              trap step is decoration and the answer is already in the repo.
    break     CONTROL.  Same setups as `manip`, but entered at the NEXT bar's
              OPEN on the continuation break, with no pullback and no profile
              at all.  If the sequence carries direction, this sees it.  If
              only the POC version works, the entry is doing the work; if
              neither works, the pattern is.  Filled at the next open on
              purpose -- filling a "close beyond the edge" rule at its own
              signal-bar close hands the backtest a price no market order gets.
    mid/edge  PLACEBO.  Replace the volume POC with the geometric MIDPOINT of
              the same window, or with the broken EDGE itself.  Same window,
              same range, same stop, same trade shape -- only the volume
              information is gone.  If "mid" scores what "volume" scores, the
              profile is decoration and this is a retracement rule wearing a
              POC costume.  That is exactly how poc_now, AVPMD and consol_poc
              died.

  A gate pass is necessary, not sufficient.  If and only if the primary shows
  pooled t > 1 with a gate better than 3/6 does the re-sign Monte-Carlo null
  get run:  python backtest_mizan_null.py 200 --amd

ENTRY LEVEL -- two specifications, neither privileged
  entry_level="poc"  the point of control.  PRIMARY: it is the line the three
                     prior threads used, so the result is comparable to them.
  entry_level="va"   the value-area edge on the pullback side (VAH for a long,
                     VAL for a short).  A shallower, more tradeable retrace,
                     and arguably what a human draws.  Reported alongside, NOT
                     selected on P/L -- two looks at the same data is two
                     chances to find noise, and it is recorded here that both
                     were pre-registered rather than one being kept.

STOP ANCHOR
  Default is the FAR SIDE OF THE ACCUMULATION, not the manipulation extreme.
  The repo has rejected a wider "anchor the stop at the sweep excursion" stop
  three times, most recently on the P3 sweep thread (+$62,730 -> +$52,245,
  t=-1.04, MaxDD worse 5/6).  stop_anchor="sweep" exists to re-measure that
  here, and it is not the default.

  Every threshold in this file is an ATR MULTIPLE.  ATR is Wilders(14) via
  atr_wilders() -- what sc.ATR(MOVAVGTYPE_WILDERS) computes in the live cpp --
  frozen on the last bar before 09:30.  Points-denominated gates silently
  select a biased subsample as the index runs 20k -> 30k, and hand-rolling an
  ATR the repo already has inflated every threshold in consol_poc by 22%.

RESULTS (2026-09-03, clock window, $5 RT, slip 0)  ---  NO SHIP
  Six frozen NQ contracts, 386 sessions.  THE VERDICT IS DECIDED ON NQ ALONE
  -- points 1, 2, 3 and 5 below need no other instrument.  Point 4 records
  ES/GC/CL runs that were made before the scope was set to NQ; they are kept
  because measured results should not be deleted, but nothing rests on them.

  Pre-registered run set, in full:

    config                        n    /day     WR    PF        Net      t  gate
    manip   POC       [PRIMARY]   8    0.02  62.5%  2.80    +6,785  +1.29   2/6
    manip   POC wick-only         4    0.01  50.0%  3.20    +3,540  +0.87   2/6
    manip   POC reclaim-only      5    0.01  60.0%  1.43    +1,635  +0.36   2/6
    manip   VA edge   [spec 2]   13    0.03  38.5%  1.23    +2,300  +0.33   3/6
    nomanip POC       [CONTROL] 104    0.27  41.3%  1.29   +20,105  +1.07   3/6
    nomanip VA edge   [CONTROL] 170    0.44  38.8%  1.16   +19,380  +0.78   3/6
    manip   break open[CONTROL]  30    0.08  43.3%  1.65   +12,450  +1.18   3/6
    nomanip break open[CONTROL] 301    0.78  37.5%  1.07   +14,455  +0.47   5/6
    manip   midpoint  [PLACEBO]   9    0.02  44.4%  1.67    +4,040  +0.66   3/6
    manip   broken edg[PLACEBO]  27    0.07  44.4%  1.78   +12,445  +1.25   3/6

  1. THE STRATEGY AS SPECIFIED IS NOT A STRATEGY.  Eight trades in 386
     sessions.  t=+1.29 on n=8 is not a number.  The cause is in the funnel
     (amd_funnel.py): the POC of a 30-minute accumulation sits 0.54 x the
     window range back from the edge that broke, so the continuation has to
     give back more than half the range to fill it, and it does that 8 times
     in 30 opportunities.  The volume profile is not merely non-load-bearing
     here -- it is the clause that prevents the trade from existing.

  2. THE PROFILE IS DECORATION, for the fourth time.  Entering at the BROKEN
     PRICE EDGE -- no profile at all -- scores +$12,445 on 27 trades against
     the POC's +$6,785 on 8, and the geometric midpoint fills more often too.
     Same finding as consol_poc, poc_now and AVPMD.  A POC-pullback entry has
     now failed on four independent constructions in this repo.  Stop
     proposing it.

  3. THE MANIPULATION STEP, HOWEVER, CARRIES DIRECTION.  This is new, and it
     is the only positive result here.  Re-sign null, 200 draws:

       cell                          n    as_is    inverse   null mean   pctile
       manip   break open           30  +12,450   -13,055        -928    94.0th
       manip   broken edge          27  +12,445    -9,895      +2,033    87.0th
       nomanip break open          301  +14,455   +19,295     +13,627    50.5th
       nomanip POC                 104  +20,105    -3,595      +9,378    71.5th
       nomanip VA edge             170  +19,380   +31,595     +25,291    42.5th

     Read the `inverse` column.  With the trap required, flipping every side
     turns +$12,450 into -$13,055 -- the rule is picking a side.  Without it,
     inverse EARNS MORE than as_is (+$19,295 vs +$14,455) and the null mean
     (+$13,627) accounts for the entire result: that cell's 5/6 gate, the best
     in the table, is pure stop/target geometry.  A gate is not evidence.
     Both prior AMD threads concluded the A/M/D sequence carried nothing;
     on this construction -- trap one edge, then require the OPPOSITE edge to
     go -- it carries something.

  4. AND IT FAILS THE SAME WAY THE P3 SWEEP FAILED: equity indices only.

       pool          n    net       null pctile   inverse
       NQ  (6)      30   +12,450       94.0th     -13,055
       ES  (6)      53   +12,710       96.5th      -5,928
       GC  (2)      17    +3,860       61.0th      +1,670
       CL  (1)      15    -4,055       12.5th        +465

     ES clears the 95th bar and NQ is a whisker under it -- but ES/NQ daily
     returns correlate 0.954, so ES is a PORTABILITY check, not independent
     evidence.  The two genuinely uncorrelated instruments both fail, and CL
     is negative at t=-2.15 on its 255 sessions.  (CL's 09:30 is not its pit
     open, which softens that but does not rescue it -- the same caveat was
     already logged for CL in the P3 thread, which reached the identical
     NQ-ONLY verdict.)

  5. FREQUENCY IS THE BINDING CONSTRAINT ANYWAY.  0.08 trades/day on NQ,
     0.13 on ES.  The repo has already retired one strategy at 0.36/day as
     structurally unvalidatable.  Even if the direction call is real, 30
     trades over two years of six contracts can never resolve it, and no
     amount of re-running these six contracts will change that.

  WHAT WOULD AND WOULD NOT BE A LEGITIMATE NEXT STEP
  Not legitimate: sweeping brk_bars, manip_reclaim_bars, pb_bars, target_r or
  the eps thresholds until a cell passes.  Six knobs against a 94th percentile
  at n=30 will produce a 99th, and it will be noise.
  Not legitimate: keeping `manip break open` because it is the best cell.  It
  was chosen after seeing ten cells, and 94.0 < 95.
  Legitimate: MORE DATA on the trap-picks-the-side claim specifically, on
  instruments that are not equity indices -- RTY and 6E are already wired in
  backtest_mizan_p3.py, and CL wants its 09:00 pit open rather than 09:30.
  The claim to test is narrow and does not involve a volume profile at all:
  "a range break preceded by a failed break the other way is a better break."

  SPECIFICATION FIX MADE MID-INVESTIGATION, on the funnel and not on P/L
  v1 counted only single-bar WICK sweeps as manipulation, which discarded
  56.5% of sessions as "broke without trapping" -- a multi-bar trap (close
  beyond the edge, reclaim within 6 bars) is still a trap, and a human drawing
  this pattern counts it.  Adding it took manipulation detection from 39.9% of
  sessions to 70.2%.  It was chosen on FREQUENCY, before any v2 P/L existed,
  the same way consol_poc's compression threshold was.  The funnel also shows
  what actually kills the pattern and it is not the detector: of 271 traps,
  180 (66%) resolve as the swept edge giving way FOR REAL rather than the
  opposite edge going.  Note before over-reading that number -- the swept edge
  is one bar away and the opposite edge is a full range away, so some of that
  asymmetry is geometry, not information.

NO LOOK-AHEAD
  ATR frozen pre-09:30.  The window, its POC and its value area are frozen
  before the manipulation test runs.  The manipulation and the break are
  tested on CLOSED bars.  The limit fill takes min(open, limit) for a long so
  a gap through the level cannot book a better price than the open, and it is
  managed from its OWN bar+1 -- price reached the limit somewhere inside that
  bar and we do not know where.  Market fills at a bar's open manage from that
  same bar, so no adverse excursion is deleted.  That distinction was worth
  $6.4k of fiction in the P3 file and flipped the absorption thread from
  +$19k to -$14.6k.

Usage
  python backtest_amd_continuation.py --nq            # primary, 6 frozen NQ
  python backtest_amd_continuation.py --matrix        # all pre-registered runs
  AM_ENTRY_LEVEL=va AM_TAG=va python backtest_amd_continuation.py --nq
  python backtest_mizan_null.py 200 --amd             # only if the gate passes
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
from backtest_mizan_iof_nq import (BAR_MINUTES, ES_FROZEN, MNQ, NQ_FROZEN,
                                   Arrays, Cands, arrays, atr_wilders, poc_of,
                                   simulate, summarize, write_csv)
from backtest_mizan_p3 import (CL_FROZEN, GC_FROZEN, IS_TAGS, OOS_TAGS, SPECS,
                               instrument_of, sessions_cached, _rt)

BASE = Path(__file__).parent


@dataclass(frozen=True)
class Params:
    # ── session geometry ───────────────────────────────────────────────────
    on_start: int = 1800
    rth_open: int = 930
    rth_close: int = 1600
    session_end: int = 1530       # no fill after this
    flatten_hhmm: int = 1555
    min_on_bars: int = 60         # ATR needs a real overnight behind it
    # ── A: the accumulation window ─────────────────────────────────────────
    acc_mode: str = "clock"       # "clock" | "compress"
    acc_end: int = 1000           # clock: window ends here (09:30-10:00 = 30m)
                                  # compress: last hhmm a window may end at
    acc_bars: int = 6             # compress: window length (6 x 5m = 30 min)
    acc_max_rng_atr: float = 2.75  # compress: range must be under this
    # ── VP ─────────────────────────────────────────────────────────────────
    bin_pts: float = 1.0
    va_pct: float = 0.70          # value area = this share of window volume
    # ── M: the manipulation ────────────────────────────────────────────────
    # A trap is not always one bar.  "wick" is the single-bar version -- take
    # the edge out and close back inside on the SAME bar.  "reclaim" is the
    # multi-bar version -- CLOSE beyond the edge, then close back inside the
    # range within manip_reclaim_bars.  A human drawing this pattern counts
    # both; the first version of this file counted only the first, and threw
    # away 56.5% of sessions as "broke without trapping".  That was a
    # specification error, and it was found on the FUNNEL (amd_funnel.py),
    # before any P/L for the second version existed.  "either" is the default
    # because both are the pattern as drawn, not because it scored better.
    require_manip: bool = True    # False -> the `nomanip` control
    manip_mode: str = "either"    # "wick" | "reclaim" | "either"
    sweep_eps_atr: float = 0.05   # must trade through the edge by this much
    sweep_close_pos: float = 0.5  # wick: close in the rejecting half of its bar
    manip_reclaim_bars: int = 6   # reclaim: 30 min, the accumulation's own length
    manip_end: int = 1200         # the trap must happen by here
    # ── the continuation break ─────────────────────────────────────────────
    brk_eps_atr: float = 0.05     # close must clear the far edge by this
    brk_bars: int = 12            # within this many bars of the manipulation
    brk_end: int = 1400           # and by this time
    # ── entry ──────────────────────────────────────────────────────────────
    # "poc"  limit at the point of control          (primary)
    # "va"   limit at the value-area edge           (second specification)
    # "mid"  limit at the geometric midpoint        (PLACEBO)
    # "edge" limit at the broken accumulation edge  (PLACEBO)
    entry_level: str = "poc"
    entry_mode: str = "limit"     # "limit" | "break" (control: next bar open)
    pb_bars: int = 12             # limit armed this many bars after the break
    lvl_tol_atr: float = 0.10     # limit sits this far toward price from level
    require_through: float = 1.0  # ticks price must trade THROUGH to fill
    # ── stop / target ──────────────────────────────────────────────────────
    stop_anchor: str = "window"   # "window" | "sweep"
    stop_buf_atr: float = 0.15
    min_stop_atr: float = 0.40
    max_stop_atr: float = 2.50
    target_r: float = 2.0
    max_hold_bars: int = 0
    atr_len: int = 14
    # ── contract spec ──────────────────────────────────────────────────────
    tick: float = 0.25
    # ── sizing / governors (simulate() reads these by name; all-zero = off) ─
    qty: int = 1
    pt_val: float = 20.0
    commission: float = 5.00
    slip_ticks: float = 0.0
    daily_loss: float = 0.0
    daily_target: float = 0.0
    max_trades_day: int = 0
    max_consec_loss: int = 0
    session_start: int = 930

    @property
    def sig_key(self) -> tuple:
        return tuple(getattr(self, f) for f in SIG_FIELDS)


SIG_FIELDS = tuple(f.name for f in fields(Params)
                   if f.name not in ("qty", "pt_val", "commission",
                                     "slip_ticks", "daily_loss", "daily_target",
                                     "max_trades_day", "max_consec_loss"))

_ENV_ALIASES = {f.name: "AM_" + f.name.upper() for f in fields(Params)}


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


RUN_TAG = os.environ.get("AM_TAG", "")

_SCAN_CACHE: Dict[Tuple[int, tuple], Tuple[List[Bar], Cands]] = {}


def _empty() -> Cands:
    e = np.array([], dtype=np.int64)
    f = np.array([], dtype=np.float64)
    return Cands(e, e, f, f, f, f, f, f, e, e, 0)


# ── volume profile: POC + value area ────────────────────────────────────────
def profile_of(a: Arrays, lo_i: int, hi_i: int, bin_pts: float,
               va_pct: float) -> Tuple[float, float, float]:
    """POC, VAH and VAL of bars [lo_i, hi_i).

    The histogram is built exactly the way poc_of() builds it -- each bar's
    volume spread uniformly over the bins its range covers, laid down with a
    difference array + cumsum, O(bars + bins) -- and the POC returned here is
    asserted equal to poc_of()'s in the smoke test.  The value area then grows
    outward from the POC, at each step taking whichever neighbouring bin holds
    more volume, until va_pct of the window's volume is enclosed.  That is the
    standard construction; ties go to the lower bin, which is arbitrary,
    deterministic and stated.

    Like the POC, this can only see WHICH BINS a 5-minute bar covered, not
    where inside the bar the volume actually printed.  A tick profile would
    move these lines by a bin or two.  It is look-ahead-free, which is the
    property that matters here.
    """
    lo = float(a.l[lo_i:hi_i].min())
    hi = float(a.h[lo_i:hi_i].max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi < lo:
        return float("nan"), float("nan"), float("nan")
    nb = int((hi - lo) / bin_pts) + 1
    diff = np.zeros(nb + 1)
    b0 = np.floor((a.l[lo_i:hi_i] - lo) / bin_pts).astype(np.int64)
    b1 = np.floor((a.h[lo_i:hi_i] - lo) / bin_pts).astype(np.int64)
    np.clip(b0, 0, nb - 1, out=b0)
    np.clip(b1, 0, nb - 1, out=b1)
    share = a.v[lo_i:hi_i] / (b1 - b0 + 1)
    np.add.at(diff, b0, share)
    np.add.at(diff, b1 + 1, -share)
    prof = np.cumsum(diff[:nb])

    k = int(np.argmax(prof))
    poc = lo + (k + 0.5) * bin_pts
    total = float(prof.sum())
    if total <= 0.0:
        return poc, poc, poc
    need = va_pct * total
    acc = float(prof[k])
    up, dn = k, k
    while acc < need and (up < nb - 1 or dn > 0):
        v_up = prof[up + 1] if up < nb - 1 else -1.0
        v_dn = prof[dn - 1] if dn > 0 else -1.0
        if v_up > v_dn:
            up += 1
            acc += float(prof[up])
        else:
            dn -= 1
            acc += float(prof[dn])
    return poc, lo + (up + 0.5) * bin_pts, lo + (dn + 0.5) * bin_pts


def scan(bars: List[Bar], p: Params) -> Cands:
    key = (id(bars), p.sig_key)
    hit = _SCAN_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]

    a = arrays(bars)
    S = sessions_cached(bars, p)
    atr = atr_wilders(a, p.atr_len)
    rng_bar = a.h - a.l
    with np.errstate(divide="ignore", invalid="ignore"):
        cpos = np.where(rng_bar > 0, (a.c - a.l) / rng_bar, 0.0)
    out: List[tuple] = []

    for k in range(len(S.starts)):
        g0, on_end = int(S.starts[k]), int(S.on_end[k])
        rth_end = int(S.rth_end[k])
        if on_end - g0 < p.min_on_bars or rth_end <= on_end:
            continue
        A = float(atr[on_end - 1])              # frozen before 09:30
        if not np.isfinite(A) or A <= 0:
            continue

        # ── A: the accumulation window, frozen once it ends ────────────────
        if p.acc_mode == "clock":
            w = np.flatnonzero(a.hhmm[on_end:rth_end] < p.acc_end)
            if len(w) < 2:
                continue
            w0, w1 = on_end, on_end + int(w[-1]) + 1     # [w0, w1)
        else:
            w0, w1 = -1, -1
            nb = p.acc_bars
            for j in range(on_end, rth_end - nb + 1):
                if a.hhmm[j + nb - 1] > p.acc_end:
                    break
                if (a.h[j:j + nb].max() - a.l[j:j + nb].min()) \
                        < p.acc_max_rng_atr * A:
                    w0, w1 = j, j + nb
                    break
            if w0 < 0:
                continue
        if w1 >= rth_end:
            continue

        chi = float(a.h[w0:w1].max())
        clo = float(a.l[w0:w1].min())
        rng = chi - clo
        if rng <= 0:
            continue
        poc, vah, val = profile_of(a, w0, w1, p.bin_pts, p.va_pct)
        if not np.isfinite(poc):
            continue

        eps_s = p.sweep_eps_atr * A
        eps_b = p.brk_eps_atr * A

        # ── M: the manipulation -- take an edge out, close back inside ─────
        # Scanned forward until one produces a continuation break.  A trap
        # that instead becomes a REAL break (a close beyond the edge it swept)
        # ends the session: the accumulation is gone, and anything after it is
        # a different pattern being measured under this one's name.
        m, side, swept_px = -1, 0, 0.0
        if p.require_manip:
            want_wick = p.manip_mode in ("wick", "either")
            want_rcl = p.manip_mode in ("reclaim", "either")
            for j in range(w1, rth_end):
                if a.hhmm[j] > p.manip_end:
                    break
                if want_wick:
                    if (a.l[j] < clo - eps_s and a.c[j] > clo
                            and cpos[j] >= p.sweep_close_pos):
                        m, side, swept_px = j, 1, float(a.l[j])
                        break
                    if (a.h[j] > chi + eps_s and a.c[j] < chi
                            and (1.0 - cpos[j]) >= p.sweep_close_pos):
                        m, side, swept_px = j, -1, float(a.h[j])
                        break
                broke = (-1 if a.c[j] < clo - eps_b else
                         1 if a.c[j] > chi + eps_b else 0)
                if broke == 0:
                    continue
                if not want_rcl:
                    break                      # broke without ever trapping
                # a CLOSE beyond the edge -- a trap only if price closes back
                # inside within manip_reclaim_bars.  If it never does, the
                # break was real and this session is over.
                ext = a.l[j] if broke < 0 else a.h[j]
                r = -1
                for q in range(j + 1, min(j + 1 + p.manip_reclaim_bars,
                                          rth_end)):
                    ext = (min(ext, a.l[q]) if broke < 0
                           else max(ext, a.h[q]))
                    if (a.c[q] > clo) if broke < 0 else (a.c[q] < chi):
                        r = q
                        break
                if r < 0:
                    break
                # the trap is confirmed on the RECLAIM bar, never earlier
                m, side, swept_px = r, -broke, float(ext)
                break
            if m < 0:
                continue
        else:
            m = w1 - 1                         # CONTROL: no trap required

        # ── the continuation break, in the direction the trap implies ──────
        b = -1
        for j in range(m + 1, min(m + 1 + p.brk_bars, rth_end)):
            if a.hhmm[j] > p.brk_end:
                break
            if p.require_manip:
                # the trap failing to fail: the swept edge gives way for real
                if (a.c[j] < clo - eps_b) if side > 0 else (a.c[j] > chi + eps_b):
                    break
                if (a.c[j] > chi + eps_b) if side > 0 else (a.c[j] < clo - eps_b):
                    b = j
                    break
            else:
                if a.c[j] > chi + eps_b:
                    b, side, swept_px = j, 1, clo
                    break
                if a.c[j] < clo - eps_b:
                    b, side, swept_px = j, -1, chi
                    break
        if b < 0:
            continue

        # ── the entry ──────────────────────────────────────────────────────
        if p.entry_level == "mid":
            base_lv = 0.5 * (chi + clo)
        elif p.entry_level == "edge":
            base_lv = chi if side > 0 else clo
        elif p.entry_level == "va":
            base_lv = vah if side > 0 else val
        else:
            base_lv = poc
        if not np.isfinite(base_lv):
            continue
        lv = float(_rt(base_lv + side * p.lvl_tol_atr * A, p.tick))

        e, fill = -1, 0.0
        if p.entry_mode == "break":
            # CONTROL: market sent once the break bar closes -> next bar's open
            j = b + 1
            if (j < rth_end and a.hhmm[j] <= p.session_end
                    and a.hhmm[j] >= p.session_start):
                e, fill = j, float(a.o[j])
        else:
            thru = p.require_through * p.tick
            for j in range(b + 1, min(b + 1 + p.pb_bars, rth_end)):
                if a.hhmm[j] > p.session_end:
                    break
                # the break that set the direction is gone -> so is the trade
                if (a.c[j] < clo) if side > 0 else (a.c[j] > chi):
                    break
                if ((a.l[j] <= lv - thru) if side > 0
                        else (a.h[j] >= lv + thru)):
                    e = j
                    fill = min(a.o[j], lv) if side > 0 else max(a.o[j], lv)
                    break
        if e < 0:
            continue

        # ── stop the far side, target target_r x that ──────────────────────
        if p.stop_anchor == "sweep" and p.require_manip:
            invalid = swept_px
        else:
            invalid = clo if side > 0 else chi
        raw = abs(fill - invalid) + p.stop_buf_atr * A
        sp = float(_rt(np.clip(raw, p.min_stop_atr * A, p.max_stop_atr * A),
                       p.tick))
        if sp <= 0.0:
            continue
        tgt = float(_rt(sp * p.target_r, p.tick))

        out.append((e, side, float(fill), sp, tgt, A, poc, rng, m, e - m,
                    e if p.entry_mode == "break" else e + 1))

    if not out:
        c = _empty()
        _SCAN_CACHE[key] = (bars, c)
        return c

    out.sort(key=lambda r: r[0])
    col = list(zip(*out))
    c = Cands(idx=np.asarray(col[0], np.int64),
              side=np.asarray(col[1], np.int64),
              entry_px=np.asarray(col[2], np.float64),
              stop_pts=np.asarray(col[3], np.float64),
              tgt_pts=np.asarray(col[4], np.float64),
              atr=np.asarray(col[5], np.float64),
              poc=np.asarray(col[6], np.float64),
              acc_rng=np.asarray(col[7], np.float64),
              sweep_idx=np.asarray(col[8], np.int64),   # the manipulation bar
              wait=np.asarray(col[9], np.int64), warm=0,
              manage_at=np.asarray(col[10], np.int64))
    _SCAN_CACHE[key] = (bars, c)
    return c


def contracts_for(scope: str) -> Dict[str, Path]:
    everything = {**NQ_FROZEN, **MNQ, **ES_FROZEN, **CL_FROZEN, **GC_FROZEN}
    env = os.environ.get("AM_TAGS")
    if env:
        return {t: everything[t] for t in env.split(",") if t in everything}
    return {"--es": dict(ES_FROZEN), "--cl": dict(CL_FROZEN),
            "--gc": dict(GC_FROZEN), "--mnq": dict(MNQ),
            "--uncorr": {**CL_FROZEN, **GC_FROZEN},
            "--is": {t: everything[t] for t in IS_TAGS},
            "--oos": {t: everything[t] for t in OOS_TAGS},
            }.get(scope, dict(NQ_FROZEN))


def apply_spec(p: Params, tag: str) -> Params:
    return params_from_env(replace(p, **SPECS[instrument_of(tag)]))


def run_one(tag: str, scid: Path, p: Params, write: bool = True,
            quiet: bool = False) -> dict:
    p = apply_spec(p, tag)
    bars = load_bars_cached(tag, scid, BAR_MINUTES)
    c = scan(bars, p)
    r = simulate(bars, c, p)
    if write:
        suffix = f"_{RUN_TAG}" if RUN_TAG else ""
        write_csv(r, BASE / f"IOF_amdCont{suffix}_{tag}.csv")
    s = summarize(r, p)
    s["cands"] = len(c.idx)
    s["sessions"] = max(len(sessions_cached(bars, p).starts) - 1, 0)
    # kept so the pooled t is computed from TRADES, not from averaging the
    # per-contract t statistics -- those are not poolable and quietly flatter
    # a result whose trades are unevenly spread across contracts.
    s["pnls"] = np.array([t.pnl for t in r.trades])
    if not quiet:
        print(f"  {tag:7s} sess={s['sessions']:>4} cand={s['cands']:>4} "
              f"n={s['n']:>4} L/S={s['longs']}/{s['n']-s['longs']:<4} "
              f"WR={s['wr']:>5.1f}% PF={s['pf']:>5.2f} Net=${s['total']:>+9,.0f} "
              f"avg=${s['avg']:>+7,.0f} ({s['avg_r']:>+5.2f}R) "
              f"MaxDD=${s['max_dd']:>+9,.0f} t={s['t']:>+5.2f}")
    return s


def pooled_of(out: Dict[str, dict]) -> dict:
    tot = sum(s["total"] for s in out.values())
    nn = sum(s["n"] for s in out.values())
    sess = sum(s["sessions"] for s in out.values())
    pos = sum(1 for s in out.values() if s["total"] > 0)
    pf_ok = sum(1 for s in out.values() if s["pf"] > 1.0)
    gate = sum(1 for s in out.values() if s["total"] > 0 and s["pf"] > 1.0)
    allp = np.concatenate([s["pnls"] for s in out.values()
                           if len(s.get("pnls", ()))]) \
        if any(len(s.get("pnls", ())) for s in out.values()) else np.array([])
    if len(allp) > 1:
        se = allp.std(ddof=1) / np.sqrt(len(allp))
        tt = float(allp.mean() / se) if se > 0 else 0.0
        wr = 100.0 * float((allp > 0).sum()) / len(allp)
        w, l = allp[allp > 0].sum(), -allp[allp < 0].sum()
        pf = float(w / l) if l > 0 else float("inf")
    else:
        tt, wr, pf = 0.0, 0.0, 0.0
    return dict(net=tot, n=nn, sessions=sess, pos=pos, pf_ok=pf_ok, gate=gate,
                k=len(out), per_day=nn / max(sess, 1),
                avg=tot / nn if nn else 0.0, t=tt, wr=wr, pf=pf)


def describe(p: Params) -> List[str]:
    win = (f"clock {p.rth_open:04d}-{p.acc_end:04d}" if p.acc_mode == "clock"
           else f"first {p.acc_bars}-bar window under {p.acc_max_rng_atr}xATR "
                f"by {p.acc_end:04d}")
    m = (f"{p.manip_mode}: take an edge out by >{p.sweep_eps_atr}xATR and get "
         f"back inside (wick cpos>={p.sweep_close_pos} | reclaim within "
         f"{p.manip_reclaim_bars} bars), by {p.manip_end}"
         if p.require_manip else "NOT REQUIRED  [nomanip CONTROL]")
    e = (f"limit at {p.entry_level}{p.lvl_tol_atr:+.2f}xATR, armed {p.pb_bars} "
         f"bars, through {p.require_through:.0f}t"
         if p.entry_mode == "limit"
         else "next bar's OPEN on the break  [break CONTROL]")
    return [f"  A: {win} -> frozen POC/VAH/VAL + ATR",
            f"  M: {m}",
            f"  C: close clear of the OPPOSITE edge by >{p.brk_eps_atr}xATR "
            f"within {p.brk_bars} bars, by {p.brk_end}",
            f"  E: {e}",
            f"  stop: {p.stop_anchor} side{p.stop_buf_atr:+.2f}xATR "
            f"clamp[{p.min_stop_atr},{p.max_stop_atr}]xATR  tgt={p.target_r}R "
            f"| flat {p.flatten_hhmm}"]


# The pre-registered run set.  Written down BEFORE any P/L was read; the whole
# point is that no cell here gets to be chosen after the fact.
MATRIX = [
    ("manip   POC        [PRIMARY]", dict()),
    ("manip   POC  wick-only     ", dict(manip_mode="wick")),
    ("manip   POC  reclaim-only  ", dict(manip_mode="reclaim")),
    ("manip   VA edge    [spec 2] ", dict(entry_level="va")),
    ("nomanip POC        [CONTROL]", dict(require_manip=False)),
    ("nomanip VA edge    [CONTROL]", dict(require_manip=False,
                                          entry_level="va")),
    ("manip   break open [CONTROL]", dict(entry_mode="break")),
    ("nomanip break open [CONTROL]", dict(require_manip=False,
                                          entry_mode="break")),
    ("manip   midpoint   [PLACEBO]", dict(entry_level="mid")),
    ("manip   broken edge[PLACEBO]", dict(entry_level="edge")),
]


def run_matrix(scope: str, base: Params):
    cons = contracts_for(scope)
    print("Accumulation -> VP -> MANIPULATION -> continuation "
          f"({BAR_MINUTES}m)  |  pre-registered run set\n")
    print(f"  {'config':<28} {'n':>5} {'/day':>5} {'WR':>6} {'PF':>5} "
          f"{'Net':>10} {'$/trd':>7} {'t':>6}  gate")
    print(f"  {'-' * 28} {'-' * 5} {'-' * 5} {'-' * 6} {'-' * 5} "
          f"{'-' * 10} {'-' * 7} {'-' * 6}  ----")
    rows = []
    for label, kw in MATRIX:
        p = replace(base, **kw)
        out = {}
        for tag, scid in cons.items():
            if scid.exists():
                out[tag] = run_one(tag, scid, p, write=False, quiet=True)
        if not out:
            continue
        g = pooled_of(out)
        print(f"  {label:<28} {g['n']:>5} {g['per_day']:>5.2f} "
              f"{g['wr']:>5.1f}% {g['pf']:>5.2f} ${g['net']:>+9,.0f} "
              f"${g['avg']:>+6,.0f} {g['t']:>+6.2f}  {g['gate']}/{g['k']}")
        rows.append((label, g))
    print("\n  gate = contracts with net>0 AND PF>1.  consol_poc scored 3/6 "
          "and was a NO SHIP.")
    print("  Read t first: this harness's se is ~$110-170/trade at these "
          "sample sizes.")
    print("  If PRIMARY does not beat BOTH nomanip controls, the manipulation "
          "step is decoration.")
    print("  If the midpoint PLACEBO scores what POC scores, the profile is "
          "decoration.")
    return rows


def main():
    args = sys.argv[1:]
    scope = next((a for a in args if a.startswith("--") and a != "--matrix"),
                 "--nq")
    p = params_from_env()

    if "--matrix" in args:
        run_matrix(scope, p)
        return

    print("Accumulation -> VP -> MANIPULATION -> continuation "
          f"({BAR_MINUTES}m)")
    for line in describe(p):
        print(line)
    print(f"  ${p.pt_val:.0f}/pt comm=${p.commission:.2f} "
          f"slip={p.slip_ticks}t\n")

    out = {}
    for tag, scid in contracts_for(scope).items():
        if not scid.exists():
            print(f"  {tag:7s} MISSING {scid.name}")
            continue
        out[tag] = run_one(tag, scid, p)

    if len(out) > 1:
        g = pooled_of(out)
        print(f"\n  POOLED  n={g['n']} over {g['sessions']} sessions "
              f"({g['per_day']:.2f}/day)  Net=${g['net']:+,.0f}  "
              f"net>0: {g['pos']}/{g['k']}  PF>1: {g['pf_ok']}/{g['k']}")
        print(f"  SHIP GATE (net>0 AND PF>1 on every contract): "
              f"{'PASS' if g['gate'] == g['k'] else 'FAIL'}")
        print("  A gate pass is necessary, not sufficient -- run "
              "backtest_mizan_null.py 200 --amd before believing it.")


if __name__ == "__main__":
    main()
