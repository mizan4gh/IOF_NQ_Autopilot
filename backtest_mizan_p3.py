"""
Mizan_IOF_NQ_P3 -- the DAILY power-of-three: overnight accumulation, opening
manipulation of a FROZEN level, entered immediately on the reclaim.

The name still says power-of-three, but the measured strategy is A->M. The
distribution stage and the pullback-to-POC entry that motivated the file both
turned out to destroy value, and both are retained only as named entry_modes so
the comparison stays runnable. See RESULTS.

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
  E. Market at the open of bar j+1, which is what an order sent after bar j
     closes actually gets. The trade is managed against bar j+1's OWN range --
     see Cands.manage_at; starting it a bar later deletes the first bar of
     adverse excursion from every trade and is worth $6.4k of fiction here.

  The two stages this file was built to test sit behind entry_mode and are OFF:
  D (a close dist_min_atr x ATR clear of ON_POC within dist_bars) under
  "confirm", and the resting limit at ON_POC + poc_tol_atr x ATR under "poc".
  ON_POC is still computed every day -- it costs nothing, it lands in the CSV,
  and it is what those two modes need.

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

RESULTS (2026-08-20, 6 frozen NQ contracts, 387 sessions, $20/pt, $5 RT)
  The pattern as originally specified does NOT work. What is inside it does.

    entry_mode   n    pooled net   null pctile
    poc         66      -$3,675       21.5%     the full A->M->D->POC chain
    confirm    112     +$13,390       63.5%     A->M->D, POC entry removed
    sweep      253     +$62,730       99.0%     A->M only, D and POC removed
                                                <- the default

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

POC-TOUCH ENTRY AND VOLUME BARS (2026-08-21)
  "poc_now" is the A->M->POC rule without the distribution stage: profile the
  accumulation, take the POC as the edge, wait for the manipulation, then rest a
  limit AT value. It is the honest version of what "poc" was trying to be --
  dropping D turns -$3,675 over 66 trades into +$73,370 over 204, which is one
  more datapoint for the distribution stage being subtractive everywhere it has
  been measured.

  All six frozen NQ contracts, 387 sessions, $20/pt, $5 RT:

    variant              bars      n      net    gate   null   placebo .10/.20
    sweep      (market)   5m     253  +$62,730   5/6   99.0th   83.0 / 19.5  ok
    sweep_limit           5m     222  +$75,340   5/6  100.0th  100.0 / 98.0  NO
    sweep_limit_d         5m     105  +$91,795   6/6  100.0th  100.0 /100.0  NO
    poc        (full)     5m      66   -$3,675   2/6   21.5th        --
    poc_now               5m     204  +$73,370   5/6   99.5th   96.5 / 85.0  NO
    sweep      (market) 2500v    229     -$855   4/6   77.0th        --
    poc_now             2500v    176   +$6,490   4/6   98.5th  100.0 / 27.0  ok

  Read the last two columns together and the picture is a clean trade-off with
  no shippable corner:

  * 2,500-contract bars DESTROY the base rule. +$62,730 -> -$855 and 99.0th ->
    77.0th, i.e. straight into the noise. The POC entry is the only thing that
    rescues it, to +$6,490 at the 98.5th -- but that is +$37 a trade, and the
    null it beats has a MEAN of -$17,070, so the percentile is measuring "less
    bad than a coinflip through a losing bracket", not a living edge. It does
    survive 3 ticks of slip (+$3,850) and it is the ONLY variant here whose
    placebo collapses (27.0th at the discriminating shift). Levels load-bearing,
    money absent.
  * On 5-minute bars poc_now is the mirror image: real money (+$73,370, 3t slip
    +$65,075) and a placebo that does NOT collapse (96.5 / 85.0). Same disease
    as sweep_limit -- what carries it is "price came back to a line", and any
    line will do. NQZ25 is the 1/6 failure in both, -$12,260.
  * Shift 0.10 is a weak placebo generally (the market rule scores 83.0 there
    and still passes on 0.20). Judge on 0.20.

  Gate rescaling, stated because it is not free: min_on_bars=60 is 5 HOURS at 5m
  and roughly 25 HOURS at 2,500 contracts, so on volume bars it is a volume
  demand, not a duration, and at 60 it rejects 85% of sessions. It was re-set to
  10 (>=25,000 contracts overnight), which passes 87% pooled against 100% for
  the 5-minute gate. The shortfall is almost entirely NQU26, a thin back-month
  that clears only 54% at any threshold. pb_bars is live but saturated: 174 of
  the 176 fills land on the FIRST bar after the sweep, because on volume bars
  the POC is usually already within a bar of price -- so "enter on the POC
  touch" is barely a filter there, which is part of why it adds so little.

  THE OPEN PROBLEM: it is NQ-only. Same rules, all thresholds already
  ATR-denominated, three instrument families tested and three failures:

    ES  ($50/pt)   -$392 over 276 trades   41.5th pctile   3/6
    CL  (--cl)   -$2,880 over  64 trades   37.5th pctile   0/1
                 -$7,160 at crude's own 09:00 pit open, 29.5th
    GC  (--gc)   +$1,045 over  51 trades   66.0th pctile   1/2
                 +$3,453 at gold's own 08:20 open, 80.0th, 2/2

  ES and NQ are 0.954 correlated on daily returns, so ES was only ever a
  portability check; CL and GC are the genuinely uncorrelated tests. Crude is
  actively negative rather than merely absent. Both are thin back-month
  contracts (~120 bars/day against NQ's ~273, and only 31-35% of their sessions
  clear the data gates against 76-80%), so n is 51-68 and gold in particular is
  underpowered -- but a real liquidity-structure effect ought to show up
  somewhere other than one index, and it does not.

DAILY LOSS CAP -- DO NOT TURN IT ON HERE
  DAILY_LOSS defaults to 0 because switching it on is not a free safety upgrade
  for THIS strategy, it is a 48% tax:

    cap        n     net      worst MaxDD   worst day   cap fires
    off      253  +$62,730      -$14,215     -$4,030        0
    $800     253  +$32,765      -$14,470     -$2,300       50
    $1,600   253  +$49,170      -$15,820     -$2,470        6

  Half the P/L gone and the drawdown NOT improved -- MaxDD is marginally worse.
  Only the worst single day gets better. That combination is the signature of
  cutting winners rather than cutting losses, and the damage is monotone in how
  tight the cap is.

  The cause is structural and will not tune away. This strategy takes ONE setup
  per session, so a daily cap can never do the thing a daily cap is for -- stop
  the second and third bad trade of the day. All it can do is truncate the only
  trade there is, including trades that dip past the cap intrabar and then
  recover to target. And it truncates most of them: median risk per trade is
  $905 at $20/pt and 59% of trades risk more than $800, so an $800 cap sits
  BELOW the median stop and front-runs it.

  The practical consequence: with the cap off, the worst day is -$4,030, which
  fails an $800-daily-limit account outright. So this strategy cannot be run
  under an $800 regime at NQ size in either state -- capped it loses half its
  edge, uncapped it breaches. The fix is to shrink the risk unit rather than
  the cap: in MNQ dollars that $905 median becomes ~$90 and the cap stops
  binding. UNTESTED as of 2026-08-21.

  (The same cap on backtest_mizan_avpmd.py is a genuine trade rather than a
  giveaway -- -21% net for -18% MaxDD and a 65% better worst day, gate holding
  6/6 -- because that strategy takes ~2.2 trades/day, so the cap can actually
  halt a day. The difference is trades per day, not the cap.)

Usage
  python backtest_mizan_p3.py --nq                      # the sweep default
  python backtest_mizan_null.py 200 --nq --p3           # its re-sign null
  MZ3_PLACEBO_SHIFT=0.2 python backtest_mizan_p3.py --nq
  python backtest_mizan_p3.py --cl                      # the uncorrelated tests
  python backtest_mizan_p3.py --freeze                  # copy CL/GC in first
  MZ3_ENTRY_MODE=poc python backtest_mizan_p3.py --nq   # the falsified chain
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

# ── uncorrelated instruments ────────────────────────────────────────────────
# The standing blocker on this strategy is that all six NQ contracts are one
# index, and ES (0.954 daily correlation) is a portability check rather than
# independent evidence. Crude and gold are the first genuinely uncorrelated
# tests available. Both live in the Sierra data directory, not the repo, and
# .scid is gitignored -- FROZEN copies are made by --freeze so a live Sierra
# cannot rewrite them mid-run.
SC_DATA = Path("C:/SierraChart/Data")
CL_FROZEN = {"CLG3": BASE / "FROZEN_CLG3.NYMEX.scid"}      # 2022-01 -> 2023-01
GC_FROZEN = {                                              # miNY gold
    "QOQ5": BASE / "FROZEN_QOQ5.COMEX.scid",               # 2025-02 -> 2025-07
    "QOG6": BASE / "FROZEN_QOG6.COMEX.scid",               # 2025-08 -> 2026-01
}
_FREEZE_SRC = {"CLG3": "CLG3.NYMEX.scid", "QOQ5": "QOQ5.COMEX.scid",
               "QOG6": "QOG6.COMEX.scid"}

# Per-instrument contract specs. pt_val scales every dollar figure linearly and
# therefore cannot move a null percentile or a net>0 count -- only the
# commission, expressed in POINTS, changes the ranking. tick matters much more:
# rounding a CL stop (0.01) to the NQ tick would quantise risk into $250 steps.
SPECS = {
    "NQ": dict(tick=0.25, pt_val=20.0,   commission=5.00, bin_pts=1.0),
    "MNQ": dict(tick=0.25, pt_val=2.0,   commission=1.50, bin_pts=1.0),
    "ES": dict(tick=0.25, pt_val=50.0,   commission=5.00, bin_pts=1.0),
    "CL": dict(tick=0.01, pt_val=1000.0, commission=5.00, bin_pts=0.05),
    "GC": dict(tick=0.10, pt_val=50.0,   commission=5.00, bin_pts=1.0),
}


def _rt(v, tick: float):
    return np.round(np.asarray(v) / tick) * tick


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
    # "sweep"   DEFAULT. Enter at the next bar's open after the manipulation
    #           bar; the distribution stage and the POC pullback are both
    #           skipped. It began as the control and became the strategy: it is
    #           the only variant that clears its own re-sign null (99.0th), and
    #           at ~0.65 fills/day against 0.17 it is also the only one with the
    #           trade count to be judged at all.
    # "confirm" A->M->D, entering at the next open after the distribution close.
    #           63.5th percentile -- the D stage costs more than it adds.
    # "poc"     the full chain with the pullback-to-value entry. 21.5th
    #           percentile. RETAINED AS A FALSIFIED REFERENCE, not an option:
    #           the setups that come back to value are disproportionately the
    #           ones that failed. Do not run this expecting the numbers above.
    # "poc_now" A->M->POC. The sweep arms a resting limit at ON_POC directly,
    #           with NO distribution stage: value is the target of the pullback
    #           rather than something price must first leave. This is "find the
    #           edge with the volume profile, see the manipulation, enter on the
    #           POC touch". Managed from its own fill bar, unlike "poc".
    # "sweep_limit_d" the D-requiring sweep_limit that 069d4e0 measured, kept
    #           only so that number stays reproducible. See the D block.
    entry_mode: str = "sweep"     # sweep | sweep_limit | sweep_limit_d
    #                             # confirm | poc | poc_now
    # ── bar construction ───────────────────────────────────────────────────
    # 0 = BAR_MINUTES time bars. >0 = constant-volume bars of that many
    # contracts. NOTE that min_on_bars, min_rth_bars, dist_bars and pb_bars are
    # all BAR COUNTS: on a volume chart they stop being durations and become
    # volume demands, so they have to be re-set from data rather than carried
    # over. 60 overnight bars is 5 hours at 5m and ~25 hours at 2,500.
    vol_bars: int = 0
    # sweep_limit: instead of a market order at the next open, rest a LIMIT
    # limit_off_atr x ATR back toward the sweep extreme, armed limit_bars bars.
    # Better price on the setups that come back; NO FILL on the ones that run.
    # That second half is the whole question -- a reclaim that works often does
    # not retrace, so this can quietly select for the weaker half of the sample.
    limit_off_atr: float = 0.15
    limit_bars: int = 3
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
    # ── contract spec ──────────────────────────────────────────────────────
    tick: float = 0.25
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


def sessions(a, on_start: int = 1800, rth_open: int = 930,
             rth_close: int = 1600) -> Sessions:
    """Group bars into 18:00 -> 17:59 trading days.

    The evening block carries the previous ET calendar date, so it is pushed one
    date forward -- to the next date PRESENT IN THE FILE, which is what makes
    weekends, holidays and the repo's known .scid coverage gaps fall out for
    free instead of needing a calendar.
    """
    uniq, inv = np.unique(a.dtag, return_inverse=True)
    sid = inv + (a.hhmm >= on_start).astype(np.int64)
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
        is_on = (hh >= on_start) | (hh < rth_open)
        nf = np.flatnonzero(~is_on)
        on_end[k] = lo + (int(nf[0]) if len(nf) else hi - lo)
        rth = np.flatnonzero((hh >= rth_open) & (hh < rth_close))
        rth_end[k] = lo + int(rth[-1]) + 1 if len(rth) else on_end[k]
    return Sessions(starts=g0, ends=g1, on_end=on_end, rth_end=rth_end,
                    sid=sid[g0])


_SCAN_CACHE: Dict[Tuple[int, tuple], Tuple[List[Bar], Cands]] = {}
_SESS_CACHE: Dict[tuple, Tuple[List[Bar], Sessions]] = {}


def sessions_cached(bars: List[Bar], p: Optional[Params] = None) -> Sessions:
    """Session split, cached on the BOUNDARIES as well as the bars.

    These used to be hardcoded to 1800/0930/1600, which silently made rth_open
    a dead input -- a test of "use each instrument's own pit open" came back
    byte-identical to the 09:30 run, which is the only reason it was caught.
    """
    p = p or Params()
    key = (id(bars), p.on_start, p.rth_open, p.rth_close)
    hit = _SESS_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]
    s = sessions(arrays(bars), p.on_start, p.rth_open, p.rth_close)
    _SESS_CACHE[key] = (bars, s)
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
    S = sessions_cached(bars, p)
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
        # Only the modes named here skip it, and the list is explicit because
        # the alternative bit once: "sweep_limit" was added without being added
        # HERE, so it fell into the else-branch and quietly became an A->M->D
        # rule whose entry loop then ignored d completely. The cpp implements
        # A->M with no distribution stage, so the two were measuring different
        # strategies while reporting one number. Use "sweep_limit_d" for the
        # D-requiring variant that 069d4e0 actually measured.
        if p.entry_mode in ("sweep", "sweep_limit", "poc_now"):
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
        manage = -1
        if p.entry_mode in ("sweep_limit", "sweep_limit_d"):
            lv = float(round_to_tick(a.c[m] - side * p.limit_off_atr * A))
            thru = p.require_through * TICK
            for j in range(m + 1, min(m + 1 + p.limit_bars, rth_end)):
                if a.hhmm[j] > p.session_end:
                    break
                if (a.c[j] < invalid) if side > 0 else (a.c[j] > invalid):
                    break
                if ((a.l[j] <= lv - thru) if side > 0 else (a.h[j] >= lv + thru)):
                    e = j
                    fill = min(a.o[j], lv) if side > 0 else max(a.o[j], lv)
                    manage = j          # limit fill -> conservative, own bar
                    break
        elif p.entry_mode in ("confirm", "sweep"):
            j = d + 1
            if j < rth_end and a.hhmm[j] <= p.session_end:
                e, fill = j, float(a.o[j])
        else:
            lv = float(_rt(poc + side * p.poc_tol_atr * A, p.tick))
            thru = p.require_through * p.tick
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
        if manage < 0:
            # A limit fill is managed from its OWN bar: price reached the limit
            # somewhere inside that bar and the rest of it can still reach the
            # stop, so assuming otherwise deletes real adverse excursion. The
            # legacy "poc" mode keeps e+1 only so its falsified number stays
            # reproducible -- that convention is the generous one, and it is
            # worth roughly $6k of fiction over a run this size.
            manage = (e + 1) if p.entry_mode == "poc" else e

        raw = abs(fill - invalid) + p.stop_buf_atr * A
        sp = float(_rt(np.clip(raw, p.min_stop_atr * A,
                               p.max_stop_atr * A), p.tick))
        if sp <= 0.0:
            continue

        tgt = sp * p.target_r
        if p.target_mode == "level":
            # the far-side frozen liquidity is what the distribution leg aims at
            far = [L for L in (ups if side > 0 else downs)
                   if (L - fill) * side >= p.min_level_tgt_r * sp]
            if far:
                tgt = abs((min(far) if side > 0 else max(far)) - fill)
        out.append((e, side, float(fill), sp, float(_rt(tgt, p.tick)), A, poc,
                    onh - onl, m, e - m, manage))

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


def instrument_of(tag: str) -> str:
    if tag in CL_FROZEN:  return "CL"
    if tag in GC_FROZEN:  return "GC"
    if tag in ES_FROZEN:  return "ES"
    if tag in MNQ:        return "MNQ"
    return "NQ"


def apply_spec(p: Params, tag: str) -> Params:
    """Tick / point value / commission for the instrument the tag belongs to.

    Applied per contract rather than per run so a mixed pool cannot silently
    price crude with the NQ tick. Env overrides still win: params_from_env is
    re-applied on top, so MZ3_COMMISSION=0 is honoured everywhere.
    """
    return params_from_env(replace(p, **SPECS[instrument_of(tag)]))


def freeze(tags=None) -> None:
    """Copy the Sierra live-directory files into the repo as FROZEN_ copies.

    Sierra rewrites files in its data directory while it runs, which has
    silently invalidated a run in this repo before. Everything downstream reads
    only the frozen copy. .scid is gitignored, so nothing large is committed.
    """
    import shutil
    for tag, src in _FREEZE_SRC.items():
        if tags and tag not in tags:
            continue
        dst = {**CL_FROZEN, **GC_FROZEN}[tag]
        s = SC_DATA / src
        if not s.exists():
            print(f"  {tag:6s} MISSING {s}")
            continue
        if dst.exists() and dst.stat().st_size == s.stat().st_size:
            print(f"  {tag:6s} already frozen ({dst.stat().st_size:,} bytes)")
            continue
        shutil.copy2(s, dst)
        print(f"  {tag:6s} froze {s.name} -> {dst.name} "
              f"({dst.stat().st_size:,} bytes)")


def contracts_for(scope: str) -> Dict[str, Path]:
    env = os.environ.get("MZ3_TAGS")
    everything = {**NQ_FROZEN, **MNQ, **ES_FROZEN, **CL_FROZEN, **GC_FROZEN}
    if env:
        return {t: everything[t] for t in env.split(",") if t in everything}
    if scope == "--mnq":
        return dict(MNQ)
    if scope == "--es":
        return dict(ES_FROZEN)
    if scope == "--cl":
        return dict(CL_FROZEN)
    if scope == "--gc":
        return dict(GC_FROZEN)
    if scope == "--uncorr":
        return {**CL_FROZEN, **GC_FROZEN}
    if scope == "--is":
        return {t: everything[t] for t in IS_TAGS}
    if scope == "--oos":
        return {t: everything[t] for t in OOS_TAGS}
    if scope == "--all":
        return {**NQ_FROZEN, **MNQ}
    return dict(NQ_FROZEN)


def load_bars(tag: str, scid: Path, p: Params):
    """Time bars, or constant-volume bars, per p.vol_bars.

    backtest_mizan_null.py looks for exactly this name. Loading bars any other
    way there once null-tested 5-minute bars against a volume-bar result.
    """
    if p.vol_bars > 0:
        from backtest_sweepabs_propeval import load_volume_bars_cached
        return load_volume_bars_cached(tag, scid, p.vol_bars)[0]
    return load_bars_cached(tag, scid, BAR_MINUTES)


def run_one(tag: str, scid: Path, p: Params, write: bool = True) -> dict:
    p = apply_spec(p, tag)
    bars = load_bars(tag, scid, p)
    c = scan(bars, p)
    r = simulate(bars, c, p)
    if write:
        suffix = f"_{RUN_TAG}" if RUN_TAG else ""
        write_csv(r, BASE / f"IOF_mizanP3{suffix}_{tag}.csv")
    s = summarize(r, p)
    s["cands"] = len(c.idx)
    s["sessions"] = max(len(sessions_cached(bars, p).starts) - 1, 0)
    print(f"  {tag:7s} sess={s['sessions']:>4} cand={s['cands']:>4} "
          f"n={s['n']:>4} L/S={s['longs']}/{s['n']-s['longs']:<4} "
          f"WR={s['wr']:>5.1f}% PF={s['pf']:>5.2f} Net=${s['total']:>+9,.0f} "
          f"avg=${s['avg']:>+7,.0f} ({s['avg_r']:>+5.2f}R) "
          f"MaxDD=${s['max_dd']:>+9,.0f} t={s['t']:>+5.2f}")
    print(f"          exits: {s['reasons']}")
    return s


def main():
    args = sys.argv[1:]
    if "--freeze" in args:
        print("Freezing Sierra data-directory copies into the repo:")
        freeze()
        return
    scope = next((a for a in args if a.startswith("--")), "--nq")
    p = params_from_env()

    lv = ("ONH/ONL" if p.use_on_levels else "") + \
         ("+PDH/PDL" if p.use_pd_levels else "")
    entry = {"sweep": "next open after the sweep (D stage skipped)",
             "confirm": "next open after the distribution close [63.5th pctile]",
             "poc": f"limit at ON_POC{p.poc_tol_atr:+.2f}xATR, armed "
                    f"{p.pb_bars} bars [FALSIFIED, 21.5th pctile]",
             "poc_now": f"limit at ON_POC{p.poc_tol_atr:+.2f}xATR, armed "
                        f"{p.pb_bars} bars, NO distribution stage",
             "sweep_limit": f"limit {p.limit_off_atr:+.2f}xATR back toward the "
                            f"sweep, armed {p.limit_bars} bars",
             "sweep_limit_d": f"as sweep_limit but the D stage is REQUIRED "
                              f"[what 069d4e0 measured]",
             }.get(p.entry_mode, p.entry_mode)
    barsrc = f"{p.vol_bars}-contract volume bars" if p.vol_bars else f"{BAR_MINUTES}m"
    print("Mizan_IOF_NQ_P3 -- daily power-of-three on FROZEN levels "
          f"({barsrc})")
    print(f"  A: overnight 18:00-09:29 (>={p.min_on_bars} bars) -> frozen "
          f"{lv} + ON_POC + ATR")
    dist = ("D: SKIPPED"
            if p.entry_mode in ("sweep", "sweep_limit", "poc_now") else
            f"D: close {p.dist_min_atr}xATR clear of ON_POC within "
            f"{p.dist_bars} bars")
    print(f"  M: sweep a frozen level by >{p.sweep_eps_atr}xATR and reclaim, "
          f"by {p.manip_end} | {dist}")
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
