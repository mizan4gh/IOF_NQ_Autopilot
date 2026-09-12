"""
NQ VWAP Pullback / Failed-Retest  --  parity backtest for IOF_NQ_VWAPPullback.cpp

WHAT THIS IS
  A bar-for-bar Python re-implementation of the cpp study's state machine, run
  over the repo's frozen .scid contracts through the shared simulate()/
  summarize() harness. It exists so the strategy can be measured BEFORE it is
  compiled, and so the cpp has something to be checked against.

  It is NOT a claim that the strategy works. Read the checklist at the bottom of
  this docstring before believing any number this file prints.

THE PATTERN (long; short is the exact mirror)
  1. TREND      A completed RTH bar closes >= min_sep_pts above session VWAP,
                with EMA20 > EMA65, the EMAs at least min_ema_sep_pts apart, and
                EMA65 rising by at least min_slope_pts over slope_bars.
  2. PULLBACK   A later bar's LOW reaches within proximity_pts of VWAP.
  3. FAILED     A bar touches the zone, closes back above VWAP, closes in the
     RETEST     upper half of its own range, and closes above its open.
  4. CONFIRM    The next bar closes above the rejection bar's high + confirm_buf.

FILL MODEL -- the single most important line in this file
  The confirmation rule is "a completed bar CLOSES beyond the prior bar's
  extreme". That is a breakout trigger, and filling a breakout trigger at the
  signal bar's own close hands the strategy a price no market order can get:
  the close is, by construction, already through the level. This repo has been
  burned by exactly that before. So:

      entry_px = the NEXT bar's OPEN, and the trade is managed from that same
      bar (manage_at = fill bar), so the first bar of adverse excursion is NOT
      deleted from the trade.

  This matches the cpp, which submits a market order on the confirmation bar's
  closing update -- that order fills on the next tick, not at the close.

  The stop and target distances are computed from the confirmation CLOSE (which
  is what the cpp has available at signal time) and then applied as OFFSETS from
  the actual fill. That is exactly how Sierra's attached orders behave, so the
  risk in points is identical in both engines even when the open gaps.

BEFORE BELIEVING ANY RESULT FROM THIS FILE
  1. Run the placebo/null:  python backtest_vwap_pullback.py --null 200
     Hold the entry times, stops and targets fixed and randomise only the SIDE.
     If the real run does not sit far out in the tail of that distribution, the
     geometry made the money and the signal did not.
  2. Check the per-contract gate, not the pooled number. Pooled P/L on six
     contracts can be carried by one.
  3. Leave-one-out the null percentile. A 94th-percentile lead that dies when
     two winning contracts are dropped is not a lead.
  4. min_atr_pts is denominated in POINTS. It does not port from NQ at 20k to
     NQ at 30k -- it silently selects a different subsample. Re-derive per
     contract or normalise by ATR.

Usage:
    python backtest_vwap_pullback.py [--es|--cl|--gc|--mnq|--is|--oos]
    python backtest_vwap_pullback.py --null 200
    VP_min_sep_pts=20 python backtest_vwap_pullback.py
"""

import os
import random
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from backtest import Bar
from fastbars import load_bars_cached
from backtest_mizan_iof_nq import (BAR_MINUTES, ES_FROZEN, MNQ, NQ_FROZEN,
                                   Cands, arrays, atr_wilders, simulate,
                                   summarize, write_csv)
from backtest_mizan_p3 import (CL_FROZEN, GC_FROZEN, IS_TAGS, OOS_TAGS, SPECS,
                               instrument_of, sessions_cached)
from backtest_trendfollow_propeval import ema

BASE = Path(__file__).parent


# ── parameters ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Params:
    # -- session geometry (must match the cpp inputs) ----------------------
    on_start: int = 1800          # sessions_cached needs the overnight anchor
    rth_open: int = 930           # session VWAP anchor
    rth_close: int = 1600
    entry_end: int = 1500         # no confirmation accepted after this
    flatten_hhmm: int = 1545
    # -- indicators --------------------------------------------------------
    fast_len: int = 20
    slow_len: int = 65
    atr_len: int = 14
    slope_bars: int = 5
    # -- geometry ------------------------------------------------------------
    # geom_mode "points" is the original specification, denominated in NQ
    # points. It cannot leave NQ: a 15-point separation is impossible on $70
    # crude, so CL/GC take literally zero trades and the portability test comes
    # back vacuous rather than passed or failed.
    #
    # geom_mode "atr" re-denominates every distance as a multiple of the ATR
    # FROZEN AT THE SESSION'S 09:30 OPEN. The multiples below were derived from
    # a single reference: the pooled median frozen-at-open 1-minute ATR over the
    # six NQ contracts, ATR_ref = 9.8058 across 388 sessions. Each multiple is
    # just (original point value / ATR_ref) -- no knob was fitted individually,
    # which is what makes this a re-anchoring rather than an eight-way sweep.
    geom_mode: str = "points"     # "points" | "atr"
    # The reference level. "vwap" is the strategy; "tpavg" and "mid" are
    # placebos that strip out volume weighting and the level itself.
    # See session_level() for what each one tests.
    level_mode: str = "vwap"      # "vwap" | "tpavg" | "mid"

    proximity_pts: float = 8.0
    min_sep_pts: float = 15.0
    retest_tol_pts: float = 4.0
    confirm_buf_pts: float = 2.0
    min_slope_pts: float = 1.0
    min_ema_sep_pts: float = 2.0
    min_atr_pts: float = 8.0

    # ATR multiples, read only when geom_mode == "atr"
    proximity_atr: float = 0.816
    min_sep_atr: float = 1.530
    retest_tol_atr: float = 0.408
    confirm_buf_atr: float = 0.204
    min_slope_atr: float = 0.102
    min_ema_sep_atr: float = 0.204
    # A minimum-ATR filter cannot itself be an ATR multiple -- "atr >= k*atr" is
    # vacuous. Expressed as basis points of price instead. 3.23bps reproduces
    # the original 8 points at the NQ median price of 24,766.
    min_atr_bps: float = 3.23
    # -- chop / invalidation ------------------------------------------------
    max_crosses: int = 4          # 0 = off
    cross_lookback: int = 20
    max_bar_atr_mult: float = 2.0  # 0 = off
    max_setup_bars: int = 20
    confirm_window: int = 1       # 1 = the very next bar only
    close_half_frac: float = 0.5
    # -- risk ---------------------------------------------------------------
    stop_method: int = 0          # 0 = rejection extreme, 1 = fixed, 2 = ATR
    stop_buf_ticks: int = 4
    fixed_stop_ticks: int = 50
    stop_atr_mult: float = 1.5
    stop_inc_conf_bar: int = 1
    # ATR-mode equivalents of stop_buf_ticks / min_stop_ticks / max_stop_ticks.
    # These matter as much as the signal geometry: leaving the stop bounds in
    # ticks while normalising everything else would still reject every CL trade,
    # and the portability test would stay vacuous for a different reason.
    stop_buf_atr: float = 0.102
    min_stop_atr: float = 0.408
    max_stop_atr: float = 5.099
    target_mode: int = 0          # 0 = fixed ticks, 1 = ATR, 2 = R multiple
    fixed_tgt_ticks: int = 100
    target_atr_mult: float = 3.0
    target_r: float = 2.0
    min_rr: float = 1.5
    min_stop_ticks: int = 16
    max_stop_ticks: int = 200
    cooldown_bars: int = 8
    max_hold_bars: int = 0
    # -- contract spec ------------------------------------------------------
    tick: float = 0.25
    # -- sizing / governors (read by simulate() by name) --------------------
    qty: int = 1
    pt_val: float = 20.0
    commission: float = 5.00
    slip_ticks: float = 1.0       # 1 tick each way; the cpp enters at market
    daily_loss: float = 800.0
    daily_target: float = 1000.0
    max_trades_day: int = 4
    max_consec_loss: int = 0
    session_start: int = 930

    @property
    def sig_key(self) -> tuple:
        return tuple(getattr(self, f) for f in SIG_FIELDS)


# Fields that do NOT change which bars are candidates. Getting this set wrong is
# how a knob becomes a dead input: the scan cache returns a stale result and the
# A/B comes back byte-identical.
_SIM_ONLY = ("qty", "pt_val", "commission", "slip_ticks", "daily_loss",
             "daily_target", "max_trades_day", "max_consec_loss")

SIG_FIELDS = tuple(f.name for f in fields(Params) if f.name not in _SIM_ONLY)

_ENV_ALIASES = {f.name: "VP_" + f.name for f in fields(Params)}


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


RUN_TAG = os.environ.get("VP_TAG", "")
SIDE_MODE = os.environ.get("VP_SIDE_MODE", "as_is")

# Bar interval. NOT cosmetic, and not a free knob: the spec's geometry is
# denominated in points (8 proximity / 15 separation / 4 tolerance / 2 buffer)
# and a single 5-minute NQ bar has a median range of 19-48 points depending on
# the contract. On 5m the "stop beyond the rejection bar extreme" is therefore
# already wider than the 25-point fixed target, and the 1.5 minimum RR rejects
# essentially every candidate -- measured: 154 confirmations, 3 entries.
# The spec's numbers only describe a tradable geometry on a fine chart.
BAR_MIN = int(os.environ.get("VP_BAR_MINUTES", BAR_MINUTES))


# ── indicators ───────────────────────────────────────────────────────────────
def session_level(a, S, rth_open: int, rth_close: int,
                  mode: str = "vwap") -> np.ndarray:
    """The intraday reference line the whole strategy is built around.

    All modes are anchored at the RTH open, carry NO overnight data, and include
    bar i in bar i's value -- which is what the cpp folds in on the bar's close
    before evaluating it. Bars outside RTH are NaN so that an accidental read of
    one shows up as NaN rather than a plausible-looking number.

    THE PLACEBO LADDER. Each mode strips one thing out of VWAP, so comparing
    them says WHICH property is load-bearing rather than just "does it work":

      "vwap"   sum(typical x volume) / sum(volume).  The real thing.
      "tpavg"  running mean of typical price, volume-blind. Identical geometry,
               identical anchoring, zero volume information. If this scores like
               vwap, the VOLUME WEIGHTING is decoration.
      "mid"    (running session high + running session low) / 2. A pure
               geometric level that knows nothing about where price spent time
               OR about volume. If this scores like vwap, the LEVEL ITSELF is
               decoration and the edge is generic intraday mean reversion.

    A placebo that scores comparably is the finding, not a nuisance. Three
    earlier threads in this repo died exactly here.
    """
    n = len(a.c)
    out = np.full(n, np.nan)
    tp = (a.h + a.l + a.c) / 3.0
    in_rth = (a.hhmm >= rth_open) & (a.hhmm < rth_close)

    for k in range(len(S.starts)):
        g0 = int(S.starts[k])
        g1 = int(S.starts[k + 1]) if k + 1 < len(S.starts) else n
        sl = np.arange(g0, g1)
        sl = sl[in_rth[sl]]
        if len(sl) == 0:
            continue

        if mode == "mid":
            out[sl] = (np.maximum.accumulate(a.h[sl])
                       + np.minimum.accumulate(a.l[sl])) / 2.0
        elif mode == "tpavg":
            out[sl] = np.cumsum(tp[sl]) / np.arange(1, len(sl) + 1)
        else:
            pv = np.cumsum(tp[sl] * a.v[sl])
            vv = np.cumsum(a.v[sl])
            with np.errstate(divide="ignore", invalid="ignore"):
                out[sl] = np.where(vv > 0, pv / vv, a.c[sl])
    return out


def session_vwap(a, S, rth_open: int, rth_close: int) -> np.ndarray:
    """Back-compatible alias for the real VWAP."""
    return session_level(a, S, rth_open, rth_close, "vwap")


def cross_counts(above: np.ndarray, lookback: int) -> np.ndarray:
    """Number of VWAP side changes in the trailing `lookback` bars.

    Mirrors VP_CountCrosses() in the cpp: a cross is any adjacent pair of bars
    whose side differs, counted over the last (lookback - 1) adjacent pairs.
    """
    flips = np.zeros(len(above), dtype=np.int64)
    flips[1:] = (above[1:] != above[:-1]).astype(np.int64)
    cs = np.cumsum(flips)
    out = np.zeros(len(above), dtype=np.int64)
    for i in range(len(above)):
        lo = max(1, i - lookback + 2)
        out[i] = cs[i] - cs[lo - 1]
    return out


# ── the scan: the cpp state machine, bar for bar ─────────────────────────────
_SCAN_CACHE: Dict[Tuple[int, tuple], Tuple[List[Bar], Cands]] = {}

IDLE, TREND, PULLBACK, REJECTION = 0, 1, 2, 3


def _empty() -> Cands:
    e = np.array([], dtype=np.int64)
    f = np.array([], dtype=np.float64)
    return Cands(e, e, f, f, f, f, f, f, e, e, 0)


def scan(bars: List[Bar], p: Params) -> Cands:
    key = (id(bars), p.sig_key)
    hit = _SCAN_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]

    a = arrays(bars)
    n = len(bars)
    S = sessions_cached(bars, p)
    atr = atr_wilders(a, p.atr_len)
    ef = ema(a.c, p.fast_len)
    es = ema(a.c, p.slow_len)
    vwap = session_level(a, S, p.rth_open, p.rth_close, p.level_mode)

    above = a.c > vwap
    crosses = cross_counts(above, p.cross_lookback)

    rng = a.h - a.l
    with np.errstate(divide="ignore", invalid="ignore"):
        cpos = np.where(rng > 0, (a.c - a.l) / rng, 0.0)

    warm = p.slow_len + p.slope_bars + 2

    # ATR frozen at each session's RTH open, broadcast across that session.
    # Frozen, not live: a threshold that tracked the session's own volatility
    # would tighten exactly as the market got busy, and the same historical bar
    # would mean different things depending on what came after it.
    frozen = np.full(n, np.nan)
    for k in range(len(S.starts)):
        g0 = int(S.starts[k])
        g1 = int(S.starts[k + 1]) if k + 1 < len(S.starts) else n
        oe = int(S.on_end[k])                 # one past the last overnight bar
        if 0 < oe <= n:
            v = atr[oe - 1]
            if np.isfinite(v) and v > 0:
                frozen[g0:g1] = v

    atr_geom = p.geom_mode == "atr"

    out: List[tuple] = []

    state, side = IDLE, 0
    setup_bar = rej_bar = -1
    rej_hi = rej_lo = 0.0
    last_exit_bar = -10 ** 6
    cur_session = -1

    for i in range(warm, n - 1):          # n-1: an entry needs a NEXT bar open
        if not (p.rth_open <= a.hhmm[i] < p.rth_close):
            continue
        if a.dtag[i] != cur_session:      # session reset, exactly as the cpp does
            cur_session = int(a.dtag[i])
            state, side = IDLE, 0
            setup_bar = rej_bar = -1
            last_exit_bar = -10 ** 6

        w = vwap[i]
        if not np.isfinite(w):
            continue

        A = float(atr[i])

        # ---- resolve the geometry for this bar ---------------------------
        if atr_geom:
            fa = float(frozen[i])
            if not np.isfinite(fa) or fa <= 0.0:
                continue
            proximity = p.proximity_atr * fa
            min_sep = p.min_sep_atr * fa
            retest_tol = p.retest_tol_atr * fa
            confirm_buf = p.confirm_buf_atr * fa
            min_slope = p.min_slope_atr * fa
            min_ema_sep = p.min_ema_sep_atr * fa
            atr_floor = p.min_atr_bps * 1e-4 * float(a.c[i])
            stop_buf = p.stop_buf_atr * fa
            min_stop = p.min_stop_atr * fa
            max_stop = p.max_stop_atr * fa
        else:
            proximity = p.proximity_pts
            min_sep = p.min_sep_pts
            retest_tol = p.retest_tol_pts
            confirm_buf = p.confirm_buf_pts
            min_slope = p.min_slope_pts
            min_ema_sep = p.min_ema_sep_pts
            atr_floor = p.min_atr_pts
            stop_buf = p.stop_buf_ticks * p.tick
            min_stop = p.min_stop_ticks * p.tick
            max_stop = p.max_stop_ticks * p.tick

        sep = float(a.c[i]) - w
        slope = float(es[i] - es[i - p.slope_bars])
        ema_spread = abs(float(ef[i] - es[i])) >= min_ema_sep
        atr_ok = atr_floor <= 0.0 or A >= atr_floor
        bull = (ef[i] > es[i]) and ema_spread and slope >= min_slope
        bear = (ef[i] < es[i]) and ema_spread and slope <= -min_slope

        # ---- IDLE: establish separation + alignment ----------------------
        if state == IDLE:
            if not atr_ok:
                continue
            if bull and sep >= min_sep:
                state, side, setup_bar = TREND, 1, i
            elif bear and sep <= -min_sep:
                state, side, setup_bar = TREND, -1, i
            continue

        align = bull if side > 0 else bear

        # ---- invalidations shared by every armed state -------------------
        if not align:
            state, side = IDLE, 0
            continue
        # a completed close decisively through VWAP kills the setup
        if (side > 0 and a.c[i] < w - retest_tol) or \
           (side < 0 and a.c[i] > w + retest_tol):
            state, side = IDLE, 0
            continue
        if i - setup_bar > p.max_setup_bars:
            state, side = IDLE, 0
            continue

        # ---- TREND -> PULLBACK: the bar's extreme enters the zone --------
        if state == TREND:
            entered = (a.l[i] <= w + proximity) if side > 0 \
                else (a.h[i] >= w - proximity)
            if entered:
                state, setup_bar = PULLBACK, i
            continue

        # ---- PULLBACK -> REJECTION: the failed retest --------------------
        if state == PULLBACK:
            if rng[i] <= 0:
                continue
            touched = (a.l[i] <= w + proximity) if side > 0 \
                else (a.h[i] >= w - proximity)
            closed_back = (a.c[i] > w) if side > 0 else (a.c[i] < w)
            right_half = (cpos[i] >= p.close_half_frac) if side > 0 \
                else (cpos[i] <= 1.0 - p.close_half_frac)
            right_body = (a.c[i] > a.o[i]) if side > 0 else (a.c[i] < a.o[i])
            if touched and closed_back and right_half and right_body:
                state, rej_bar = REJECTION, i
                rej_hi, rej_lo = float(a.h[i]), float(a.l[i])
            continue

        # ---- REJECTION -> CONFIRMATION -----------------------------------
        age = i - rej_bar
        if age < 1:
            continue
        confirmed = (a.c[i] > rej_hi + confirm_buf) if side > 0 \
            else (a.c[i] < rej_lo - confirm_buf)
        if not confirmed:
            if age >= p.confirm_window:
                # one signal per pullback: a stale rejection retires the setup
                state, side = IDLE, 0
            continue

        # ================= entry qualification =============================
        state, armed_side = IDLE, side      # the setup is consumed either way
        side = 0

        if not atr_ok or not ema_spread:
            continue
        if a.hhmm[i] > p.entry_end:
            continue
        if i - last_exit_bar < p.cooldown_bars:
            continue
        if p.max_crosses > 0 and crosses[i] > p.max_crosses:
            continue
        if p.max_bar_atr_mult > 0.0 and A > 0.0 and rng[i] > p.max_bar_atr_mult * A:
            continue

        s = armed_side
        entry_ref = float(a.c[i])           # what the cpp has at signal time

        if p.stop_method == 1:
            stop_px = entry_ref - s * p.fixed_stop_ticks * p.tick
        elif p.stop_method == 2:
            stop_px = entry_ref - s * p.stop_atr_mult * A
        else:
            extreme = rej_lo if s > 0 else rej_hi
            if p.stop_inc_conf_bar:
                extreme = min(extreme, float(a.l[i])) if s > 0 \
                    else max(extreme, float(a.h[i]))
            stop_px = extreme - s * stop_buf

        stop_pts = (entry_ref - stop_px) * s
        if not (min_stop <= stop_pts <= max_stop):
            continue

        if p.target_mode == 1:
            tgt_pts = p.target_atr_mult * A
        elif p.target_mode == 2:
            tgt_pts = p.target_r * stop_pts
        else:
            tgt_pts = p.fixed_tgt_ticks * p.tick

        if tgt_pts / stop_pts < p.min_rr:
            continue

        # Fill at the NEXT bar's open, managed from that same bar. See the
        # FILL MODEL note in the module docstring -- this is not a detail.
        j = i + 1
        if a.dtag[j] != a.dtag[i]:          # no next bar in this session
            continue
        fill = float(a.o[j])
        if not np.isfinite(fill) or not (a.l[j] <= fill <= a.h[j]):
            # Sierra's .scid OPEN field is a sentinel on tick records; a stale
            # bar cache can still carry -1.999e37 here. Refuse to price a fill
            # off it rather than book a fantasy trade.
            continue

        out.append((j, s, fill, stop_pts, tgt_pts, A, w, sep, i, 1, j))
        last_exit_bar = j                   # refined below by simulate()

    if not out:
        res = _empty()
    else:
        arr = list(zip(*out))
        res = Cands(
            idx=np.array(arr[0], dtype=np.int64),
            side=np.array(arr[1], dtype=np.int64),
            entry_px=np.array(arr[2], dtype=np.float64),
            stop_pts=np.array(arr[3], dtype=np.float64),
            tgt_pts=np.array(arr[4], dtype=np.float64),
            atr=np.array(arr[5], dtype=np.float64),
            poc=np.array(arr[6], dtype=np.float64),      # reused: session VWAP
            acc_rng=np.array(arr[7], dtype=np.float64),  # reused: separation
            sweep_idx=np.array(arr[8], dtype=np.int64),  # the confirmation bar
            wait=np.array(arr[9], dtype=np.int64),
            warm=warm,
            manage_at=np.array(arr[10], dtype=np.int64),
        )

    _SCAN_CACHE[key] = (bars, res)
    return res


# ── runners ──────────────────────────────────────────────────────────────────
_BARS: Dict[Tuple[str, int], List[Bar]] = {}


def bars_for(tag: str, scid: Path) -> List[Bar]:
    """Memoised bar load.

    The repo's scan cache is keyed on id(bars), and load_bars_cached() returns a
    fresh list every call -- so without this the null test re-scans every
    contract on all 200 draws instead of re-simulating a cached scan.
    """
    key = (tag, BAR_MIN)
    if key not in _BARS:
        _BARS[key] = load_bars_cached(tag, scid, BAR_MIN)
    return _BARS[key]


def contracts_for(scope: str) -> Dict[str, Path]:
    everything = {**NQ_FROZEN, **MNQ, **ES_FROZEN, **CL_FROZEN, **GC_FROZEN}
    env = os.environ.get("VP_TAGS")
    if env:
        return {t: everything[t] for t in env.split(",") if t in everything}
    return {"--es": dict(ES_FROZEN), "--cl": dict(CL_FROZEN),
            "--gc": dict(GC_FROZEN), "--mnq": dict(MNQ),
            "--uncorr": {**CL_FROZEN, **GC_FROZEN},
            "--is": {t: everything[t] for t in IS_TAGS},
            "--oos": {t: everything[t] for t in OOS_TAGS},
            }.get(scope, dict(NQ_FROZEN))


def apply_spec(p: Params, tag: str) -> Params:
    spec = SPECS[instrument_of(tag)]
    known = {k: v for k, v in spec.items() if k in {f.name for f in fields(p)}}
    return params_from_env(replace(p, **known))


def run_one(tag: str, scid: Path, p: Params, write: bool = True,
            side_mode: str = "as_is", quiet: bool = False) -> dict:
    p = apply_spec(p, tag)
    bars = bars_for(tag, scid)
    c = scan(bars, p)
    r = simulate(bars, c, p, side_mode)
    if write:
        suffix = f"_{RUN_TAG}" if RUN_TAG else ""
        write_csv(r, BASE / f"IOF_vwapPB{suffix}_{tag}.csv")
    s = summarize(r, p)
    s["cands"] = len(c.idx)
    if not quiet:
        print(f"  {tag:7s} cand={s['cands']:>4} n={s['n']:>4} "
              f"L/S={s['longs']}/{s['n'] - s['longs']:<4} "
              f"WR={s['wr']:>5.1f}% PF={s['pf']:>5.2f} "
              f"Net=${s['total']:>+9,.0f} MaxDD=${s['max_dd']:>+9,.0f}")
    return s


def run_null(scope: str, p: Params, draws: int) -> None:
    """Re-sign Monte-Carlo null: hold entry times, stops and targets fixed and
    randomise only the SIDE. Separates "the geometry made money" from "the
    signal picked the side". If the real run is not out in the tail, stop."""
    cons = contracts_for(scope)
    real = 0.0
    for tag, scid in cons.items():
        if scid.exists():
            real += run_one(tag, scid, p, write=False, quiet=True)["total"]

    sims = []
    for d in range(draws):
        random.seed(9_000 + d)
        tot = 0.0
        for tag, scid in cons.items():
            if scid.exists():
                tot += run_one(tag, scid, p, write=False,
                               side_mode="random", quiet=True)["total"]
        sims.append(tot)

    sims_a = np.array(sims)
    pct = float((sims_a < real).mean() * 100.0)
    print(f"\n  RE-SIGN NULL over {draws} draws")
    print(f"    real pooled net   ${real:+,.0f}")
    print(f"    null mean / sd    ${sims_a.mean():+,.0f} / ${sims_a.std():,.0f}")
    print(f"    percentile        {pct:.1f}th")
    if pct < 95.0:
        print("    -> NOT separated from a coinflip on side. Do not ship.")


def main() -> None:
    args = [a for a in sys.argv[1:]]
    draws = 0
    if "--null" in args:
        k = args.index("--null")
        draws = int(args[k + 1]) if k + 1 < len(args) else 200
        del args[k:k + 2]
    scope = args[0] if args else ""

    p = params_from_env()
    if p.geom_mode == "atr":
        geom = (f"ATR-normalised  sep={p.min_sep_atr}x prox={p.proximity_atr}x "
                f"tol={p.retest_tol_atr}x buf={p.confirm_buf_atr}x "
                f"(ATR frozen at 09:30)")
    else:
        geom = (f"POINTS  sep={p.min_sep_pts} prox={p.proximity_pts} "
                f"tol={p.retest_tol_pts} buf={p.confirm_buf_pts}")
    lvl = "" if p.level_mode == "vwap" else f"  |  *** PLACEBO LEVEL: {p.level_mode} ***"
    print(f"VWAP Pullback / Failed Retest  |  {BAR_MIN}m bars  |  {geom}"
          f"  |  fill = NEXT BAR OPEN{lvl}")

    if draws:
        run_null(scope, p, draws)
        return

    pooled = {"n": 0, "total": 0.0, "wins": 0}
    better = 0
    tested = 0
    for tag, scid in contracts_for(scope).items():
        if not scid.exists():
            print(f"  {tag:7s} MISSING {scid.name}")
            continue
        s = run_one(tag, scid, p, side_mode=SIDE_MODE)
        pooled["n"] += s["n"]
        pooled["total"] += s["total"]
        tested += 1
        if s["total"] > 0:
            better += 1

    if tested:
        print(f"\n  POOLED n={pooled['n']} net=${pooled['total']:+,.0f} "
              f"| positive on {better}/{tested} contracts")
        print("  Reminder: pooled P/L is not a verdict. Run --null, then LOO it.")


if __name__ == "__main__":
    main()
