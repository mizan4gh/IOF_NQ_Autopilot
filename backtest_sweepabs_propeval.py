"""
Backtest of MNQ_SweepAbsorption_PropEval.cpp ("MNQ Sweep Absorption - Prop Eval").

WHAT THE CPP IS
  A liquidity-sweep / absorption reversal system. Location is a hard gate: the
  order-flow tests are only ever evaluated on a bar that is inside tolerance of
  one of 12 tracked levels. Each level runs its own state machine and can fire
  at most once per session.

    levels (6 support -> longs, 6 resistance -> shorts)
      ONL/ONH   overnight extremes, frozen at the RTH open
      PDL/PDH   previous day's RTH extremes
      VAL/VAH   prior value area  -- In_UseVA defaults OFF and needs a
                referenced VbP study, so these are never valid at defaults
      VWAP-/+1sd, VWAP-/+2sd   session VWAP bands (reset at the RTH open)
      ORL/ORH   opening range, first 15 min, valid once complete

    IDLE  -> ARMED     price tags the level: low <= P+tol and low >= P-4*tol
    ARMED -> SWEPT     low <= P - 2 ticks                    (expires 20 min)
    SWEPT -> ABSORBED  all four of, on one bar:              (expires 12 min)
                         delta <= -1.2 sd(delta, 100 prior bars)
                         |delta|/range_ticks >= 1.5 x median(100 prior bars)
                         trade intensity >= 1.3 x median(100 prior bars)
                         close position in range >= 0.35
                       dies instead if price extends 0.060*scale past the
                       extreme
    ABSORBED -> FIRE   aggression weakening (|delta| < 0.5*|sweep delta| or
                       sign flip) AND reclaim (close > level or > sweep bar
                       high). Dies on a new extreme.         (expires 10 min)

  Shorts are the exact mirror.

  Entry: market, and rejected if it lands more than 0.025*scale from the level.
  Stop = sweep extreme -/+ max(0.030*scale, 3 ticks), floored at 10 pts, and
  the trade is dropped if it needs more than 40. Target = nearest OPPOSING
  level whose distance falls in [2R, 6R], else 3R, clamped. Stop to breakeven
  +/- 1 tick at 1.5R. Size = floor(min($250, $800 + dailyPL - $50) / risk per
  contract), cap 10.

  "scale" is a 14-day mean of completed RTH day ranges, NOT the execution bar's
  ATR. Nothing can trade until 3 such days exist.

  Confluence: same-direction valid levels within 10 points of the trigger
  level. 1 is enough for the first trade of a day; 2 after any loss.

  Governors: +$700 target, -$800 loss, $350 giveback off the daily peak of
  CLOSED P/L, 3 trades/day, 2 consecutive losses, 15:55 hard flatten. Entry
  window 09:30-15:30 with 12:00-13:30 blocked out.

  rev="v1" (or SA_REV=v1) reproduces the original revision of the file: bar
  ATR as the ruler, expiries in bars, raw bar volume instead of intensity, no
  stop clamps, no entry-distance cap, giveback measured against open P/L.

WHY THIS HARNESS EXISTS AND WHAT IT IS SUSPICIOUS OF
  The repo has already falsified a sweep/absorption reversal once
  (absorption_scan.py, 2026-07-14) and the ONLY reason it ever looked good was
  harness look-ahead: the confirmation bar was used to gate an entry priced at
  the previous bar's close. So:
    * every gate on bar i uses data at or before bar i; the trailing delta and
      intensity statistics run over bars [i-100, i-1] exactly as the cpp does
    * the entry is priced at bar i's close (repo convention) AND the run always
      prints the next-bar-open fill next to it, because "close reclaims the
      level" is exactly the kind of trigger that close-fill flatters

MODELLING DECISIONS (each is a place this differs from the DLL)
  * Bars. SA_BARS=vol2000 is the cpp's own chart spec; SA_BARS=1m is the stated
    alternative. Volume bars are built here (fastbars only does fixed-clock
    bars) and carry second-resolution stamps, which the intensity and the
    minute expiries both need and which HHMM cannot supply.
  * Trade intensity uses the gap between this bar's stamp and the previous
    bar's -- i.e. the duration of the bar BEFORE it, floored at 1 s. That is
    what the cpp computes, so it is what is reproduced.
  * The state machine only advances on bars the cpp actually reaches: flat,
    unhalted, inside the entry window, with a valid scale and statistics. A
    level's expiry clock therefore keeps running across the lunch block and
    across an open trade, as it does live.
  * Entry fills at the CLOSE of the trigger bar; the bracket is live from bar
    i+1. entry_next_open=1 prices the same signals at the next bar's open,
    which is what the DLL's market order actually gets.
  * Within a bar the ADVERSE extreme is assumed first: stop before target,
    loss governor before profit governor.
  * Governors fire intrabar against the bar extremes and fill at the exact
    threshold price when the extreme overshoots it. The v2 giveback is the
    exception: measured off closed P/L, it can only bite once a trade closes.
  * 15:55 flatten fills at that bar's OPEN -- the governor fires on the first
    update of the bar, not at its close.
  * VAH/VAL are left invalid, matching In_UseVA=No at defaults.
  * Commission $1.50 RT per MNQ contract.

DATA
  FROZEN_MNQU6_0722.scid is the only genuine MNQ .scid in the tree, and one
  contract cannot decide anything on its own, so the six frozen NQ contracts
  are run alongside it in MNQ dollars ($2/point).

Usage
  SA_BARS=vol2000 python backtest_sweepabs_propeval.py --all
  SA_BARS=vol2000 SA_REV=v1 python backtest_sweepabs_propeval.py --all
  python backtest_sweepabs_propeval.py --mnq            # 1m, MNQU6 only
"""
from __future__ import annotations

import csv
import os
import random
import sys
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from backtest import Bar, detect_price_scale, read_scid
from fastbars import _SC_TO_UNIX_US, _US_PER_DAY, _et_offset_table, load_bars_cached

BASE = Path(__file__).parent

# ── contracts ───────────────────────────────────────────────────────────────
MNQ = {"MNQU6": BASE / "FROZEN_MNQU6_0722.scid"}
NQ_FROZEN = {
    "NQU25": BASE / "F.US.ENQU25.scid",
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
    "NQM6":  BASE / "F.US.ENQM26.scid",
    "NQU26": BASE / "F.US.ENQU26.scid",
}

TICK = 0.25

# ── level identity, mirroring the cpp enum ──────────────────────────────────
(LVL_ONL, LVL_PDL, LVL_VAL, LVL_VWAP_L1, LVL_VWAP_L2, LVL_ORL,
 LVL_ONH, LVL_PDH, LVL_VAH, LVL_VWAP_U1, LVL_VWAP_U2, LVL_ORH) = range(12)
LVL_COUNT = 12
LEVEL_DIR = np.array([1] * 6 + [-1] * 6, dtype=np.int64)
LEVEL_NAME = ["ONL", "PDL", "VAL", "VWAP-1", "VWAP-2", "ORL",
              "ONH", "PDH", "VAH", "VWAP+1", "VWAP+2", "ORH"]

ST_IDLE, ST_ARMED, ST_SWEPT, ST_ABSORBED, ST_DEAD = range(5)


# ── parameters (cpp inputs, same names where it helps) ──────────────────────
@dataclass(frozen=True)
class Params:
    # -- levels in use --
    use_on: int = 1
    use_pd: int = 1
    use_va: int = 0            # cpp default: OFF (needs a VbP study reference)
    use_vwap: int = 1
    use_or: int = 1
    # -- session --
    rth_start: int = 93000     # HHMMSS
    or_minutes: int = 15
    last_entry: int = 153000
    flatten_time: int = 155500
    day_end: int = 170000
    notrade_start: int = 120000
    notrade_end: int = 133000
    # -- which revision of the cpp to model --
    # "v2" = the shipped file. "v1" = the original, kept so the two can be
    # A/B'd against each other on identical bars.
    rev: str = "v2"
    # -- scale source --
    # v2 measures every distance against a 14-day mean of completed RTH day
    # ranges; v1 measured them against the execution bar's ATR(14).
    scale_mode: str = "dayrange"        # "dayrange" | "atr"
    atr_len: int = 14
    daily_range_days: int = 14
    min_range_days: int = 3
    # -- location --
    tol_atr_mult: float = 0.020         # x scale
    tol_min_ticks: int = 3
    cluster_mult: float = 2.0           # v1 only: x tolerance
    cluster_points: float = 10.0        # v2: absolute points
    max_entry_dist: float = 0.025       # v2: x scale, 0 = off
    # Which reclaim confirms the fade. The cpp accepts EITHER a close back
    # through the swept level OR a close beyond the sweep bar's own extreme.
    # The second is a different event: it fires while the level is still
    # broken, i.e. before the fade has actually been confirmed.
    reclaim_mode: str = "level_or_bar"  # "level_or_bar" | "level_only"
    # -- order flow --
    delta_lookback: int = 100
    delta_sigma: float = 1.2
    abs_ratio_mult: float = 1.5
    # v2 compares contracts-per-second against its own median; v1 compared raw
    # bar volume, which is a constant on a constant-volume chart.
    intensity: int = 1
    volume_mult: float = 1.3
    close_pos_in_bar: float = 0.35
    min_sweep_ticks: int = 2
    # -- state machine --
    expiry_mode: str = "minutes"        # "minutes" (v2) | "bars" (v1)
    armed_expiry: int = 20
    swept_expiry: int = 12
    absorb_expiry: int = 10
    invalid_atr_mult: float = 0.060     # x scale
    # -- risk --
    stop_atr_mult: float = 0.030        # x scale
    stop_min_ticks: int = 3
    min_stop_pts: float = 10.0          # v2: widen the stop to this
    max_stop_pts: float = 40.0          # v2: reject the trade past this
    breakeven_r: float = 1.5
    min_target_r: float = 2.0
    max_target_r: float = 6.0
    default_target_r: float = 3.0
    risk_per_trade: float = 250.0
    max_contracts: int = 10
    # measurement only: pin size so P&L is a clean R multiple and sizing
    # variance does not add noise to a population being tested for edge
    fixed_qty: int = 0
    # -- governors --
    daily_target: float = 700.0
    daily_loss: float = 800.0
    giveback: float = 350.0
    # v2 tracks the daily peak off CLOSED P/L only, so the giveback measures
    # surrendered realised gains instead of ordinary intra-trade excursion.
    giveback_closed_only: int = 1
    max_trades_day: int = 3
    max_consec_loss: int = 2
    conf_after_first: int = 1
    conf_after_loss: int = 2
    loss_buffer: float = 50.0
    vwap_band1: float = 1.0
    vwap_band2: float = 2.0
    # -- harness only --
    pt_val: float = 2.0        # MNQ $2/point
    commission: float = 1.50   # RT per contract
    slip_ticks: float = 0.0
    entry_next_open: int = 0


# The original file, so the two revisions can be run against identical bars.
V1 = Params(
    rev="v1", scale_mode="atr", tol_atr_mult=0.20, cluster_mult=2.0,
    max_entry_dist=0.0, delta_sigma=1.5, abs_ratio_mult=1.8, intensity=0,
    volume_mult=1.5, close_pos_in_bar=0.40, expiry_mode="bars",
    armed_expiry=10, swept_expiry=5, absorb_expiry=5, invalid_atr_mult=0.5,
    stop_atr_mult=0.25, min_stop_pts=0.0, max_stop_pts=0.0,
    giveback_closed_only=0, conf_after_first=2,
)

_ENV = {f.name: "SA_" + f.name.upper() for f in fields(Params)}


def params_from_env(base: Optional[Params] = None) -> Params:
    if base is None and os.environ.get("SA_REV", "").lower() == "v1":
        base = V1
    p = base or Params()
    kw = {}
    for name, env in _ENV.items():
        v = os.environ.get(env)
        if v is None:
            continue
        t = type(getattr(p, name))
        kw[name] = v if t is str else t(float(v))
    return replace(p, **kw) if kw else p


RUN_TAG = os.environ.get("SA_TAG", "")
BAR_MODE = os.environ.get("SA_BARS", "1m")
SIDE_MODE = "as_is"


def hms_to_sec(hhmmss: int) -> int:
    return (hhmmss // 10000) * 3600 + (hhmmss // 100 % 100) * 60 + hhmmss % 100


# ── volume bars ─────────────────────────────────────────────────────────────
# fastbars only does fixed-clock bars. The cpp's primary chart spec is 2000
# volume bars, and the ARMED/SWEPT/ABSORBED expiries are counted in bars, so
# the bar type changes the strategy rather than just its resolution.
def build_volume_bars(recs: np.ndarray, target_vol: int,
                      price_scale: float) -> List[Bar]:
    """Aggregate .scid ticks into constant-volume bars stamped in US/Eastern.

    A bar closes once its accumulated volume reaches target_vol; ticks are
    never split. Buckets are also cut at the local calendar-date boundary, so a
    bar never spans two sessions (Sierra's "new bar at session start").
    """
    tot = recs["tot_vol"].astype(np.int64)
    keep = tot != 0
    if not keep.any():
        return []

    dt_us = recs["dt"][keep].astype(np.int64) - _SC_TO_UNIX_US
    tot = tot[keep]

    t_s = dt_us // 1_000_000
    starts, offs = _et_offset_table(int(t_s[0]), int(t_s[-1]))
    local_us = dt_us + offs[np.searchsorted(starts, t_s, side="right") - 1] * 1_000_000

    day = local_us // _US_PER_DAY
    mod = local_us - day * _US_PER_DAY

    # volume bucket index, restarted at each new day
    cum = np.cumsum(tot)
    day_first = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    base = np.zeros(len(cum), dtype=np.int64)
    base[day_first] = cum[day_first] - tot[day_first]
    base = np.maximum.accumulate(base)
    vb = (cum - tot - base) // target_vol
    key = day * (1 << 32) + vb

    gs = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])

    c = recs["close"][keep].astype(np.float64) / price_scale
    o = recs["open"][keep].astype(np.float64) / price_scale
    h = recs["high"][keep].astype(np.float64) / price_scale
    lo_ = recs["low"][keep].astype(np.float64) / price_scale
    for arr in (o, h, lo_):
        # see fastbars: the tick format's OPEN field is 0 or the -1.999e37
        # "no value" sentinel on every record, never a real price
        bad = ~np.isfinite(arr) | (arr <= 0.0)
        arr[bad] = c[bad]

    b_open = o[gs]
    b_close = c[np.r_[gs[1:] - 1, len(c) - 1]]
    b_high = np.maximum.reduceat(h, gs)
    b_low = np.minimum.reduceat(lo_, gs)
    b_vol = np.add.reduceat(tot, gs)
    b_bid = np.add.reduceat(recs["bid_vol"][keep].astype(np.int64), gs)
    b_ask = np.add.reduceat(recs["ask_vol"][keep].astype(np.int64), gs)

    b_day = day[gs]
    b_sec = mod[gs] // 1_000_000        # bar stamped at its FIRST tick
    epoch = datetime(1970, 1, 1)
    out: List[Bar] = []
    for i in range(len(gs)):
        d = epoch + timedelta(days=int(b_day[i]))
        s = int(b_sec[i])
        hh, mm = s // 3600, s // 60 % 60
        out.append(Bar(dt=None, open=float(b_open[i]), high=float(b_high[i]),
                       low=float(b_low[i]), close=float(b_close[i]),
                       volume=int(b_vol[i]), bid_vol=int(b_bid[i]),
                       ask_vol=int(b_ask[i]), idx=i,
                       date_tag=d.year * 10000 + d.month * 100 + d.day,
                       hhmm=hh * 100 + mm))
    # absolute seconds, needed for the intensity and minute-expiry terms --
    # HHMM alone cannot express a volume bar's true start
    abs_sec = (b_day * 86400 + b_sec).astype(np.int64)
    return out, abs_sec


def _vol_cache(tag: str, target: int) -> Path:
    return BASE / f".sweepabs_bars_{tag}_v{target}.npz"


def load_volume_bars_cached(tag: str, scid: Path, target: int = 2000,
                            rebuild: bool = False):
    p = _vol_cache(tag, target)
    if p.exists() and not rebuild:
        z = np.load(p)
        if "sec" in z.files:
            return [Bar(dt=None, open=float(o), high=float(h), low=float(lo),
                        close=float(c), volume=int(v), bid_vol=int(bv),
                        ask_vol=int(av), idx=i, date_tag=int(dt), hhmm=int(hm))
                    for i, (o, h, lo, c, v, bv, av, dt, hm) in enumerate(
                        zip(z["o"], z["h"], z["l"], z["c"], z["v"],
                            z["bv"], z["av"], z["dtag"], z["hhmm"]))], z["sec"]
    bars, sec = build_volume_bars(read_scid(str(scid)), target,
                                  detect_price_scale(str(scid)))
    np.savez_compressed(
        p,
        o=np.array([b.open for b in bars]), h=np.array([b.high for b in bars]),
        l=np.array([b.low for b in bars]), c=np.array([b.close for b in bars]),
        v=np.array([b.volume for b in bars], dtype=np.int64),
        bv=np.array([b.bid_vol for b in bars], dtype=np.int64),
        av=np.array([b.ask_vol for b in bars], dtype=np.int64),
        dtag=np.array([b.date_tag for b in bars], dtype=np.int64),
        hhmm=np.array([b.hhmm for b in bars], dtype=np.int64),
        sec=sec,
    )
    return bars, sec


def _abs_sec_from_stamps(bars: List[Bar]) -> np.ndarray:
    """Absolute seconds for fixed-clock bars, whose HHMM stamp IS exact."""
    out = np.empty(len(bars), dtype=np.int64)
    ord_cache: Dict[int, int] = {}
    for k, b in enumerate(bars):
        d = ord_cache.get(b.date_tag)
        if d is None:
            d = datetime(b.date_tag // 10000, b.date_tag // 100 % 100,
                         b.date_tag % 100).toordinal()
            ord_cache[b.date_tag] = d
        out[k] = d * 86400 + (b.hhmm // 100) * 3600 + (b.hhmm % 100) * 60
    return out


def load_bars(tag: str, scid: Path):
    """(bars, absolute-second stamps). The cpp's intensity and minute expiries
    both need real timestamps, which a volume bar's HHMM cannot supply."""
    if BAR_MODE.startswith("vol"):
        return load_volume_bars_cached(tag, scid, int(BAR_MODE[3:] or 2000))
    bars = load_bars_cached(tag, scid, int(BAR_MODE.rstrip("m") or 1))
    return bars, _abs_sec_from_stamps(bars)


# ── per-bar series ──────────────────────────────────────────────────────────
@dataclass
class Series:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    vol: np.ndarray
    delta: np.ndarray
    absratio: np.ndarray
    intensity: np.ndarray      # contracts per second
    atr: np.ndarray
    scale: np.ndarray          # the ruler every distance is measured against
    tod: np.ndarray            # seconds since local midnight
    abs_sec: np.ndarray        # absolute seconds, for the minute expiries
    dtag: np.ndarray
    hhmm: np.ndarray
    lvl_px: np.ndarray         # (n, 12)
    lvl_ok: np.ndarray         # (n, 12) bool
    elig: np.ndarray           # bool: cpp reaches the state machine on this bar
    delta_sd: np.ndarray       # only defined where elig
    abs_med: np.ndarray
    vol_med: np.ndarray
    day_start: np.ndarray      # first bar index of each day
    flatten_i: np.ndarray      # per bar: is this bar at/after the flatten time


def _atr_wilders(h, l, c, period):
    """sc.ATR(..., MOVAVGTYPE_WILDERS). Bar 0's TR is the plain range."""
    from scipy.signal import lfilter
    tr = np.empty(len(c))
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]),
                                np.abs(l[1:] - c[:-1])])
    a = (period - 1.0) / period
    return lfilter([1.0 / period], [1.0, -a], tr, zi=np.array([a * tr[0]]))[0]


_PREP_CACHE: Dict[Tuple[int, tuple], Tuple[List[Bar], Series]] = {}
_PREP_FIELDS = tuple(f.name for f in fields(Params)
                     if f.name not in ("pt_val", "commission", "slip_ticks",
                                       "entry_next_open"))


def prep(bars: List[Bar], p: Params, abs_sec: np.ndarray) -> Series:
    """Everything that is a pure function of the bars: levels, order-flow
    statistics, and the eligibility mask (the bars on which the cpp actually
    reaches its per-level state machine)."""
    key = (id(bars), tuple(getattr(p, f) for f in _PREP_FIELDS))
    hit = _PREP_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]

    n = len(bars)
    o = np.fromiter((b.open for b in bars), np.float64, n)
    h = np.fromiter((b.high for b in bars), np.float64, n)
    l = np.fromiter((b.low for b in bars), np.float64, n)
    c = np.fromiter((b.close for b in bars), np.float64, n)
    vol = np.fromiter((b.volume for b in bars), np.float64, n)
    bidv = np.fromiter((b.bid_vol for b in bars), np.float64, n)
    askv = np.fromiter((b.ask_vol for b in bars), np.float64, n)
    hhmm = np.fromiter((b.hhmm for b in bars), np.int64, n)
    dtag = np.fromiter((b.date_tag for b in bars), np.int64, n)
    tod = (hhmm // 100) * 3600 + (hhmm % 100) * 60

    delta = askv - bidv
    rng = h - l
    rng_ticks = np.maximum(rng / TICK, 1.0)
    absratio = np.abs(delta) / rng_ticks
    atr = _atr_wilders(h, l, c, p.atr_len)

    # Trade intensity = contracts per second. The cpp takes the gap between
    # THIS bar's stamp and the PREVIOUS bar's stamp, i.e. the duration of the
    # bar before it, and floors it at 1 s. Reproduced as written.
    bar_secs = np.ones(n)
    if n > 1:
        bar_secs[1:] = np.maximum(np.diff(abs_sec).astype(np.float64), 1.0)
    intensity = vol / bar_secs

    rth_s = hms_to_sec(p.rth_start)
    or_end_s = rth_s + p.or_minutes * 60
    day_end_s = hms_to_sec(p.day_end)
    last_s = hms_to_sec(p.last_entry)
    flat_s = hms_to_sec(p.flatten_time)
    nt0, nt1 = hms_to_sec(p.notrade_start), hms_to_sec(p.notrade_end)

    lvl_px = np.zeros((n, LVL_COUNT))
    lvl_ok = np.zeros((n, LVL_COUNT), dtype=bool)

    day_start = np.flatnonzero(np.r_[True, dtag[1:] != dtag[:-1]])
    bounds = np.r_[day_start, n]

    # v2's ruler: the mean of the last N completed RTH day ranges. Held
    # constant through the day, exactly as the cpp recomputes it on the roll.
    day_scale = np.zeros(n)
    day_ready = np.zeros(n, dtype=bool)
    ranges: List[float] = []

    prev_rth_hi = prev_rth_lo = 0.0
    for k in range(len(day_start)):
        s, e = bounds[k], bounds[k + 1]
        t = tod[s:e]
        recent = ranges[-p.daily_range_days:]
        day_scale[s:e] = (sum(recent) / len(recent)) if recent else 0.0
        day_ready[s:e] = len(ranges) >= p.min_range_days
        in_day = t < day_end_s
        pre = t < rth_s
        rth = (~pre) & in_day

        # -- overnight extremes: the cpp only accumulates on bars before the
        #    RTH start of the SAME trading day, so the 18:00-24:00 evening
        #    session never contributes. Reproduced as written.
        on_hi = float(h[s:e][pre].max()) if pre.any() else -np.inf
        on_lo = float(l[s:e][pre].min()) if pre.any() else np.inf

        rth_opened = np.zeros(e - s, dtype=bool)
        if rth.any():
            rth_opened[np.flatnonzero(rth)[0]:] = True

        or_mask = rth & (t < or_end_s)
        or_hi = float(h[s:e][or_mask].max()) if or_mask.any() else -np.inf
        or_lo = float(l[s:e][or_mask].min()) if or_mask.any() else np.inf
        or_done = np.zeros(e - s, dtype=bool)
        post_or = rth & (t >= or_end_s)
        if post_or.any():
            or_done[np.flatnonzero(post_or)[0]:] = True

        # -- session VWAP and its standard deviation
        tp = (h[s:e] + l[s:e] + c[s:e]) / 3.0
        w = np.where(rth & (vol[s:e] > 0), vol[s:e], 0.0)
        sv = np.cumsum(w)
        spv = np.cumsum(tp * w)
        sp2v = np.cumsum(tp * tp * w)
        with np.errstate(divide="ignore", invalid="ignore"):
            vwap = np.where(sv > 0, spv / sv, 0.0)
            var = np.where(sv > 0, sp2v / sv - vwap * vwap, 0.0)
        vwsd = np.sqrt(np.maximum(var, 0.0))
        vw_ok = vwsd > 0.0

        on_ok = bool(p.use_on) and np.isfinite(on_hi) and np.isfinite(on_lo)
        pd_ok = bool(p.use_pd) and prev_rth_hi > 0.0 and prev_rth_lo > 0.0
        or_ok = bool(p.use_or) and np.isfinite(or_hi) and np.isfinite(or_lo)

        px = lvl_px[s:e]
        ok = lvl_ok[s:e]
        px[:, LVL_ONL] = on_lo if np.isfinite(on_lo) else 0.0
        px[:, LVL_ONH] = on_hi if np.isfinite(on_hi) else 0.0
        ok[:, LVL_ONL] = ok[:, LVL_ONH] = on_ok and rth_opened
        px[:, LVL_PDL] = prev_rth_lo
        px[:, LVL_PDH] = prev_rth_hi
        ok[:, LVL_PDL] = ok[:, LVL_PDH] = pd_ok
        px[:, LVL_ORL] = or_lo if np.isfinite(or_lo) else 0.0
        px[:, LVL_ORH] = or_hi if np.isfinite(or_hi) else 0.0
        ok[:, LVL_ORL] = ok[:, LVL_ORH] = or_ok and or_done
        px[:, LVL_VWAP_L1] = vwap - p.vwap_band1 * vwsd
        px[:, LVL_VWAP_U1] = vwap + p.vwap_band1 * vwsd
        px[:, LVL_VWAP_L2] = vwap - p.vwap_band2 * vwsd
        px[:, LVL_VWAP_U2] = vwap + p.vwap_band2 * vwsd
        for L in (LVL_VWAP_L1, LVL_VWAP_U1, LVL_VWAP_L2, LVL_VWAP_U2):
            ok[:, L] = bool(p.use_vwap) and vw_ok
        # VAH/VAL: In_UseVA defaults to No, and there is no VbP study to read.
        if p.use_va:
            raise NotImplementedError(
                "use_va needs a prior-day value area series; the cpp reads it "
                "from a referenced VbP study and defaults the input to No.")

        # tomorrow's PDH/PDL, and this day's range for the rolling ruler
        if rth.any():
            prev_rth_hi = float(h[s:e][rth].max())
            prev_rth_lo = float(l[s:e][rth].min())
            ranges.append(prev_rth_hi - prev_rth_lo)

    # -- eligibility: the gates the cpp passes before touching a level -------
    in_day = tod < day_end_s
    rth_open_all = np.zeros(n, dtype=bool)
    for k in range(len(day_start)):
        s, e = bounds[k], bounds[k + 1]
        m = (tod[s:e] >= rth_s) & in_day[s:e]
        if m.any():
            rth_open_all[s + np.flatnonzero(m)[0]:e] = True

    scale = day_scale if p.scale_mode == "dayrange" else atr

    flatten_i = in_day & (tod >= flat_s)
    elig = (in_day & rth_open_all & (tod >= rth_s) & (tod <= last_s)
            & ~((tod >= nt0) & (tod < nt1)) & ~flatten_i
            & (atr > 0.0) & (rng > 0.0))
    if p.scale_mode == "dayrange":
        # cpp: "if (DailyRangeCount < 3 || DailyAtr <= 0) return;"
        elig &= day_ready & (day_scale > 0.0)
    lb = p.delta_lookback
    elig[:lb] = False

    # -- trailing statistics over [i-lb, i-1], gathered only where needed ----
    idx = np.flatnonzero(elig)
    delta_sd = np.zeros(n)
    abs_med = np.zeros(n)
    vol_med = np.zeros(n)
    flow = intensity if p.intensity else vol      # v2 vs v1
    if len(idx):
        off = np.arange(-lb, 0)
        CH = 200_000                       # keep the gather under ~200 MB
        for a0 in range(0, len(idx), CH):
            ii = idx[a0:a0 + CH]
            W = ii[:, None] + off[None, :]
            delta_sd[ii] = delta[W].std(axis=1, ddof=1)
            # TrailingMedian returns Buf[N/2] -- the UPPER median for even N
            m = lb // 2
            abs_med[ii] = np.partition(absratio[W], m, axis=1)[:, m]
            vol_med[ii] = np.partition(flow[W], m, axis=1)[:, m]
        elig[idx] &= ((delta_sd[idx] > 0.0) & (abs_med[idx] > 0.0)
                      & (vol_med[idx] > 0.0))

    ser = Series(o=o, h=h, l=l, c=c, vol=flow, delta=delta, absratio=absratio,
                 intensity=intensity, atr=atr, scale=scale, tod=tod,
                 abs_sec=abs_sec, dtag=dtag, hhmm=hhmm, lvl_px=lvl_px,
                 lvl_ok=lvl_ok, elig=elig, delta_sd=delta_sd, abs_med=abs_med,
                 vol_med=vol_med, day_start=day_start, flatten_i=flatten_i)
    _PREP_CACHE[key] = (bars, ser)
    return ser


# ── records ─────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    date_tag: int
    side: int
    qty: int
    level: int
    level_px: float
    confluence: int
    entry_hhmm: int
    entry_px: float
    stop_px: float
    tp_px: float
    stop_pts: float
    tgt_pts: float
    atr: float
    entry_dist: float = 0.0    # |entry - level|, in points
    sweep_depth: float = 0.0   # |level - sweep extreme|, in points
    reclaim: str = ""          # "level" | "sweepbar" -- which clause fired
    scale: float = 0.0         # the day-range ruler at entry
    init_stop: float = 0.0
    be_done: bool = False
    exit_hhmm: int = 0
    exit_px: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0


@dataclass
class DayStat:
    date_tag: int
    pnl: float = 0.0
    n: int = 0
    halt: str = ""
    blocked_size: int = 0
    blocked_conf: int = 0
    blocked_far: int = 0
    blocked_wide: int = 0


@dataclass
class Diag:
    armed: int = 0
    swept: int = 0
    absorbed: int = 0
    triggered: int = 0
    dead: int = 0
    # mirrors the cpp's DiagAbsChecks / DiagFail* -- every condition that
    # failed on a SWEPT bar, not just the first, so the DIAG line names the
    # binding filter instead of only reporting that absorption did not happen
    abs_checks: int = 0
    fail_aggr: int = 0
    fail_ratio: int = 0
    fail_intensity: int = 0
    fail_closepos: int = 0


@dataclass
class Result:
    trades: List[Trade] = field(default_factory=list)
    days: List[DayStat] = field(default_factory=list)
    diag: Diag = field(default_factory=Diag)
    by_level: Dict[int, List[float]] = field(default_factory=dict)


_FLATTEN_REASONS = ("LOSS_LIMIT", "GIVEBACK", "DAILY_TARGET", "FLAT_1555")


def round_tick(v: float) -> float:
    return round(v / TICK) * TICK


# ── the governed walk ───────────────────────────────────────────────────────
def walk(bars: List[Bar], s: Series, p: Params,
         side_mode: str = "as_is") -> Result:
    """One pass over the bars, reproducing the cpp's control flow.

    The per-level state machine only advances on bars where the cpp actually
    reaches it -- flat, unhalted, inside the entry window, with valid ATR and
    order-flow statistics. Everything else is skipped, which is what makes a
    level's expiry clock run across the lunch block and across an open trade
    exactly as it does live.
    """
    res = Result()
    n = len(bars)
    slip = p.slip_ticks * TICK
    tol_min = p.tol_min_ticks * TICK
    sweep_pen = p.min_sweep_ticks * TICK
    stop_min = p.stop_min_ticks * TICK
    flat_s = hms_to_sec(p.flatten_time)
    day_end_s = hms_to_sec(p.day_end)

    o, h, l, c = s.o, s.h, s.l, s.c
    vol, delta, absratio, atr = s.vol, s.delta, s.absratio, s.atr
    lvl_px, lvl_ok, tod, dtag = s.lvl_px, s.lvl_ok, s.tod, s.dtag

    # level state, reset each day
    st = np.zeros(LVL_COUNT, dtype=np.int64)
    st_bar = np.zeros(LVL_COUNT, dtype=np.int64)
    st_time = np.zeros(LVL_COUNT, dtype=np.int64)
    burned = np.zeros(LVL_COUNT, dtype=bool)
    sw_ext = np.zeros(LVL_COUNT)
    sw_delta = np.zeros(LVL_COUNT)
    sw_hi = np.zeros(LVL_COUNT)
    sw_lo = np.zeros(LVL_COUNT)

    day = -1
    ds: Optional[DayStat] = None
    closed_pl = peak_pl = 0.0
    trades_today = consec = losses_today = 0
    halted = False

    idx_all = np.flatnonzero(s.elig)
    ptr = 0
    n_elig = len(idx_all)

    while ptr < n_elig:
        i = int(idx_all[ptr])
        ptr += 1

        # ── daily roll ──────────────────────────────────────────────────────
        if dtag[i] != day:
            if ds is not None:
                ds.pnl = closed_pl
            day = int(dtag[i])
            ds = DayStat(date_tag=day)
            res.days.append(ds)
            closed_pl = peak_pl = 0.0
            trades_today = consec = losses_today = 0
            halted = False
            st[:] = ST_IDLE
            burned[:] = False

        if halted:
            continue

        A = s.scale[i]                  # day-range ruler (v2) or bar ATR (v1)
        now = s.abs_sec[i]
        tol = max(p.tol_atr_mult * A, tol_min)
        sd = s.delta_sd[i]
        d = delta[i]
        rng = h[i] - l[i]
        cp_long = (c[i] - l[i]) / rng
        cp_short = (h[i] - c[i]) / rng
        invalidation = p.invalid_atr_mult * A

        aggr_long = d <= -p.delta_sigma * sd
        aggr_short = d >= p.delta_sigma * sd
        absorbing = absratio[i] >= p.abs_ratio_mult * s.abs_med[i]
        vol_ok = vol[i] >= p.volume_mult * s.vol_med[i]

        trig_L = -1
        trig_reclaim = ""
        for L in range(LVL_COUNT):
            if burned[L] or st[L] == ST_DEAD or not lvl_ok[i, L]:
                continue
            P = lvl_px[i, L]
            up = LEVEL_DIR[L] > 0

            # -- expiry --------------------------------------------------
            state = st[L]
            if state != ST_IDLE:
                max_age = (p.armed_expiry if state == ST_ARMED else
                           p.swept_expiry if state == ST_SWEPT else
                           p.absorb_expiry)
                # v2 ages a setup in wall-clock minutes; v1 counted bars, which
                # on a volume chart is 4 minutes in fast trade and 40 in slow
                age = ((now - st_time[L]) / 60.0 if p.expiry_mode == "minutes"
                       else i - st_bar[L])
                if max_age > 0 and age > max_age:
                    state = ST_IDLE
                    st[L] = ST_IDLE
                    st_bar[L] = i
                    st_time[L] = now

            # -- IDLE -> ARMED -------------------------------------------
            if state == ST_IDLE:
                tagged = ((l[i] <= P + tol and l[i] >= P - tol * 4.0) if up
                          else (h[i] >= P - tol and h[i] <= P + tol * 4.0))
                if tagged:
                    st[L] = ST_ARMED
                    st_bar[L] = i
                    st_time[L] = now
                    res.diag.armed += 1
                continue

            # -- ARMED -> SWEPT ------------------------------------------
            if state == ST_ARMED:
                swept = (l[i] <= P - sweep_pen) if up else (h[i] >= P + sweep_pen)
                if not swept:
                    continue
                st[L] = state = ST_SWEPT
                st_bar[L] = i
                st_time[L] = now
                sw_ext[L] = l[i] if up else h[i]
                sw_delta[L] = d
                sw_hi[L] = h[i]
                sw_lo[L] = l[i]
                res.diag.swept += 1
                # fall through: absorption can qualify on the sweep bar itself

            # -- SWEPT -> ABSORBED / DEAD --------------------------------
            if state == ST_SWEPT:
                extended = ((l[i] < sw_ext[L] - invalidation) if up
                            else (h[i] > sw_ext[L] + invalidation))
                if extended:
                    st[L] = ST_DEAD
                    res.diag.dead += 1
                    continue
                if up and l[i] < sw_ext[L]:
                    sw_ext[L] = l[i]
                if (not up) and h[i] > sw_ext[L]:
                    sw_ext[L] = h[i]

                aggression = aggr_long if up else aggr_short
                close_ok = ((cp_long if up else cp_short)
                            >= p.close_pos_in_bar)
                res.diag.abs_checks += 1
                if not aggression:  res.diag.fail_aggr += 1
                if not absorbing:   res.diag.fail_ratio += 1
                if not vol_ok:      res.diag.fail_intensity += 1
                if not close_ok:    res.diag.fail_closepos += 1
                if aggression and absorbing and vol_ok and close_ok:
                    st[L] = ST_ABSORBED
                    st_bar[L] = i
                    st_time[L] = now
                    sw_delta[L] = d
                    sw_hi[L] = h[i]
                    sw_lo[L] = l[i]
                    res.diag.absorbed += 1
                continue

            # -- ABSORBED -> TRIGGER / DEAD ------------------------------
            if state == ST_ABSORBED:
                new_ext = ((l[i] < sw_ext[L] - TICK) if up
                           else (h[i] > sw_ext[L] + TICK))
                if new_ext:
                    st[L] = ST_DEAD
                    res.diag.dead += 1
                    continue
                weakening = (abs(d) < 0.5 * abs(sw_delta[L])
                             or (d > 0.0 if up else d < 0.0))
                over_level = (c[i] > P) if up else (c[i] < P)
                over_bar = ((c[i] > sw_hi[L]) if up else (c[i] < sw_lo[L]))                     and p.reclaim_mode != "level_only"
                if weakening and (over_level or over_bar):
                    trig_L = L
                    # "level" = the fade actually reclaimed the swept level;
                    # "sweepbar" = only the sweep bar's own extreme broke,
                    # which is a different (and much later) event
                    trig_reclaim = "level" if over_level else "sweepbar"
                    res.diag.triggered += 1
                    break

        if trig_L < 0:
            continue

        # ── confluence ──────────────────────────────────────────────────────
        side = int(LEVEL_DIR[trig_L])
        trig_px = lvl_px[i, trig_L]
        # a trigger always ends the level's session, whether it is taken or not
        st[trig_L] = ST_DEAD

        required = 1
        if trades_today >= 1:
            required = max(required, p.conf_after_first)
        if losses_today >= 1:
            required = max(required, p.conf_after_loss)
        # v2 uses an absolute point distance. v1's "2 x tolerance" came to
        # ~2 pts on a volume chart, so two levels were almost never that close
        # and confluence >= 2 was unreachable -- the post-loss tightening
        # silently became a one-trade-per-day cap.
        cluster = (p.cluster_points if p.scale_mode == "dayrange"
                   else p.cluster_mult * tol)
        conf = int(np.count_nonzero(
            lvl_ok[i] & (LEVEL_DIR == side)
            & (np.abs(lvl_px[i] - trig_px) <= cluster)))
        if conf < required:
            ds.blocked_conf += 1
            continue

        # ── stop / size / target ────────────────────────────────────────────
        stop_pad = max(p.stop_atr_mult * A, stop_min)
        entry_ref = c[i]
        stop_px = (sw_ext[trig_L] - stop_pad if side > 0
                   else sw_ext[trig_L] + stop_pad)
        risk_pts = (entry_ref - stop_px) * side

        # The reclaim trigger allows a break of the sweep bar's extreme, which
        # can sit a long way from the level; an entry that far past it has
        # already spent the edge it was fading.
        if p.max_entry_dist > 0.0:
            if abs(entry_ref - trig_px) > p.max_entry_dist * A:
                ds.blocked_far += 1
                continue
        # Floor the stop: a structural stop inside the instrument's own noise
        # band gets taken out whether or not the read was right.
        if p.min_stop_pts > 0.0 and risk_pts < p.min_stop_pts:
            risk_pts = p.min_stop_pts
        # A sweep too wide to stop sensibly is not a trade.
        if p.max_stop_pts > 0.0 and risk_pts > p.max_stop_pts:
            ds.blocked_wide += 1
            continue
        if risk_pts <= 0.0:
            continue
        risk_per_ctr = risk_pts * p.pt_val

        budget = p.risk_per_trade
        if p.daily_loss > 0.0:
            budget = min(budget, p.daily_loss + closed_pl - p.loss_buffer)
        if budget <= 0.0:
            ds.blocked_size += 1
            continue
        qty = (p.fixed_qty if p.fixed_qty
               else min(int(budget / risk_per_ctr), p.max_contracts))
        if qty < 1:
            ds.blocked_size += 1
            continue

        min_t = p.min_target_r * risk_pts
        max_t = p.max_target_r * risk_pts
        tgt_pts = p.default_target_r * risk_pts
        opp = lvl_ok[i] & (LEVEL_DIR == -side)
        if opp.any():
            dist = (lvl_px[i] - entry_ref) * side
            cand = dist[opp & (dist >= min_t) & (dist <= max_t)]
            if len(cand):
                tgt_pts = float(cand.min())
        tgt_pts = min(max(tgt_pts, min_t), max_t)

        # null test: hold the bar, the stop distance, the target distance and
        # the size fixed; re-sign ONLY the direction
        if side_mode == "inverse":
            side = -side
        elif side_mode == "random":
            side = random.choice((1, -1))

        # ── fill ────────────────────────────────────────────────────────────
        if p.entry_next_open:
            if i + 1 >= n or dtag[i + 1] != day:
                continue
            entry = o[i + 1] + side * slip
            j0 = i + 1
        else:
            entry = c[i] + side * slip
            j0 = i + 1
        stop_abs = entry - side * risk_pts
        tp_abs = entry + side * tgt_pts

        t = Trade(date_tag=day, side=side, qty=qty, level=trig_L,
                  level_px=float(trig_px), confluence=conf,
                  entry_hhmm=int(s.hhmm[i]), entry_px=entry,
                  stop_px=stop_abs, tp_px=tp_abs, stop_pts=float(risk_pts),
                  tgt_pts=float(tgt_pts), atr=float(A), init_stop=stop_abs,
                  entry_dist=abs(entry_ref - trig_px),
                  sweep_depth=abs(trig_px - sw_ext[trig_L]),
                  reclaim=trig_reclaim, scale=float(A))
        trades_today += 1
        dpp = p.pt_val * qty * side

        # ── manage ──────────────────────────────────────────────────────────
        j = j0
        ex: Optional[Tuple[float, str]] = None
        while j < n:
            if dtag[j] != day:
                ex = (c[j - 1], "EOD")
                j -= 1
                break
            hi, lo = h[j], l[j]
            adverse, favourable = (lo, hi) if side > 0 else (hi, lo)
            t.mae = min(t.mae, (adverse - entry) * side)
            t.mfe = max(t.mfe, (favourable - entry) * side)

            # 1. bracket stop (or the breakeven stop once it has moved)
            if (lo <= t.stop_px) if side > 0 else (hi >= t.stop_px):
                ex = (t.stop_px, "BE_STOP" if t.be_done else "STOP")
                break

            # 2. loss / giveback governors, against the adverse extreme
            pl_adv = closed_pl + (adverse - entry) * dpp
            hit = None
            if p.daily_loss > 0.0 and pl_adv <= -p.daily_loss:
                hit = ("LOSS_LIMIT", -p.daily_loss)
            elif (not p.giveback_closed_only and p.giveback > 0.0
                  and peak_pl > 0.0 and (peak_pl - pl_adv) >= p.giveback):
                hit = ("GIVEBACK", peak_pl - p.giveback)
            if hit is not None:
                px = entry + (hit[1] - closed_pl) / dpp
                ex = (min(max(px, lo), hi), hit[0])
                break

            # 3. bracket target
            if (hi >= t.tp_px) if side > 0 else (lo <= t.tp_px):
                ex = (t.tp_px, "TARGET")
                break

            # 4. daily profit target, against the favourable extreme
            if p.daily_target > 0.0:
                pl_fav = closed_pl + (favourable - entry) * dpp
                if pl_fav >= p.daily_target:
                    px = entry + (p.daily_target - closed_pl) / dpp
                    ex = (min(max(px, lo), hi), "DAILY_TARGET")
                    break

            # 5. hard flatten -- fires on the first update of the bar
            if flat_s <= tod[j] < day_end_s:
                ex = (o[j], "FLAT_1555")
                break

            # breakeven moves off this bar's CLOSE, so it applies from j+1 on
            if (not t.be_done) and p.breakeven_r > 0.0:
                fav_close = (c[j] - entry) * side
                if fav_close >= p.breakeven_r * risk_pts:
                    t.stop_px = entry + side * TICK
                    t.be_done = True

            if not p.giveback_closed_only:
                peak_pl = max(peak_pl, closed_pl + (c[j] - entry) * dpp)
            j += 1

        if ex is None:
            j = n - 1
            ex = (c[j], "EOD")

        # ── book ────────────────────────────────────────────────────────────
        t.exit_px = ex[0] - side * slip
        t.exit_hhmm = int(s.hhmm[j])
        t.reason = ex[1]
        t.pnl = (t.exit_px - entry) * side * p.pt_val * qty - p.commission * qty
        res.trades.append(t)
        res.by_level.setdefault(trig_L, []).append(t.pnl)
        ds.n += 1
        closed_pl += t.pnl
        peak_pl = max(peak_pl, closed_pl)
        if t.pnl < 0.0:
            consec += 1
            losses_today += 1
        elif t.pnl > 0.0:
            consec = 0

        # v2's giveback can only bite once a trade has closed, since both
        # the peak and the measured value move only then
        if (p.giveback_closed_only and p.giveback > 0.0 and peak_pl > 0.0
                and (peak_pl - closed_pl) >= p.giveback):
            halted, ds.halt = True, "GIVEBACK"

        if ex[1] in _FLATTEN_REASONS:
            halted, ds.halt = True, ex[1]
        elif p.max_trades_day > 0 and trades_today >= p.max_trades_day:
            halted, ds.halt = True, "MAX_TRADES"
        elif p.max_consec_loss > 0 and consec >= p.max_consec_loss:
            halted, ds.halt = True, "CONSEC_LOSS"

        # the cpp is flat again by the exit bar's CLOSE, so the state machine
        # resumes on that bar -- but never earlier
        while ptr < n_elig and idx_all[ptr] < j:
            ptr += 1

    if ds is not None:
        ds.pnl = closed_pl
    return res


def run_engine(bars: List[Bar], abs_sec: np.ndarray,
               p: Optional[Params] = None,
               side_mode: Optional[str] = None) -> Result:
    p = p or params_from_env()
    return walk(bars, prep(bars, p, abs_sec), p,
                side_mode if side_mode is not None else SIDE_MODE)


# ── reporting ───────────────────────────────────────────────────────────────
def summarize(r: Result, p: Params) -> dict:
    pnls = np.array([t.pnl for t in r.trades])
    n = len(pnls)
    days = [d for d in r.days if d.n > 0]
    base = dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0, t=0.0, days=0,
                worst_day=0.0, best_day=0.0, breaches=0, avg=0.0, reasons={},
                longs=0, diag=r.diag, by_level={},
                blocked_conf=sum(d.blocked_conf for d in r.days),
                blocked_size=sum(d.blocked_size for d in r.days),
                blocked_far=sum(d.blocked_far for d in r.days),
                blocked_wide=sum(d.blocked_wide for d in r.days))
    if n == 0:
        return base
    wins, loss = pnls[pnls > 0], pnls[pnls < 0]
    se = pnls.std(ddof=1) / np.sqrt(n) if n > 1 else 0.0
    run = np.cumsum(pnls)
    mdd = float((run - np.maximum.accumulate(np.r_[0.0, run])[1:]).min())
    day_pnls = [d.pnl for d in days]
    reasons: Dict[str, int] = {}
    for t in r.trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    base.update(
        n=n, total=float(pnls.sum()),
        pf=(float(wins.sum() / -loss.sum()) if len(loss) else float("inf")),
        wr=100.0 * len(wins) / n, max_dd=mdd,
        t=float(pnls.mean() / se) if se > 0 else 0.0, avg=float(pnls.mean()),
        days=len(days), worst_day=min(day_pnls), best_day=max(day_pnls),
        breaches=sum(1 for x in day_pnls if x <= -p.daily_loss),
        reasons=reasons, longs=sum(1 for t in r.trades if t.side > 0),
        by_level={LEVEL_NAME[k]: (len(v), float(sum(v)))
                  for k, v in sorted(r.by_level.items())},
    )
    return base


def write_csv(r: Result, path: Path):
    tot = 0.0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Event", "Date", "Time", "Side", "Qty", "Level", "LevelPx",
                    "Conf", "Price", "Stop", "Target", "StopPts", "ATR",
                    "Reason", "PnL", "TotalPnL", "MAE", "MFE"])
        for t in r.trades:
            sd = "LONG" if t.side > 0 else "SHORT"
            w.writerow(["ENTRY", t.date_tag, t.entry_hhmm, sd, t.qty,
                        LEVEL_NAME[t.level], f"{t.level_px:.2f}", t.confluence,
                        f"{t.entry_px:.2f}", f"{t.init_stop:.2f}",
                        f"{t.tp_px:.2f}", f"{t.stop_pts:.2f}", f"{t.atr:.2f}",
                        "", "", f"{tot:.2f}", "", ""])
            tot += t.pnl
            w.writerow(["EXIT", t.date_tag, t.exit_hhmm, sd, t.qty,
                        LEVEL_NAME[t.level], "", "", f"{t.exit_px:.2f}", "", "",
                        "", "", t.reason, f"{t.pnl:.2f}", f"{tot:.2f}",
                        f"{t.mae:.2f}", f"{t.mfe:.2f}"])


def contracts_for(scope: str) -> Dict[str, Path]:
    if scope == "--mnq":
        return dict(MNQ)
    if scope == "--nq":
        return dict(NQ_FROZEN)
    return {**MNQ, **NQ_FROZEN}


def run_one(tag: str, scid: Path, p: Params, write: bool = True) -> dict:
    bars, sec = load_bars(tag, scid)
    r = walk(bars, prep(bars, p, sec), p)
    if write:
        suffix = f"_{RUN_TAG}" if RUN_TAG else ""
        write_csv(r, BASE / f"IOF_swabs{suffix}_{tag}.csv")
    s = summarize(r, p)
    d = s["diag"]
    print(f"  {tag:7s} bars={len(bars):>8,} days={s['days']:>4} n={s['n']:>4} "
          f"L/S={s['longs']}/{s['n']-s['longs']:<4} WR={s['wr']:>5.1f}% "
          f"PF={s['pf']:>5.2f} Net=${s['total']:>+9,.0f} "
          f"avg=${s['avg']:>+7,.1f} MaxDD=${s['max_dd']:>+8,.0f} "
          f"t={s['t']:>+5.2f} worstday=${s['worst_day']:>+7,.0f} "
          f"breach={s['breaches']}")
    ck = max(d.abs_checks, 1)
    print(f"          abs-checks {d.abs_checks:,} -> failed: aggression "
          f"{100*d.fail_aggr/ck:.0f}%, ratio {100*d.fail_ratio/ck:.0f}%, "
          f"intensity {100*d.fail_intensity/ck:.0f}%, close-pos "
          f"{100*d.fail_closepos/ck:.0f}%")
    print(f"          funnel: armed {d.armed:,} -> swept {d.swept:,} -> "
          f"absorbed {d.absorbed:,} -> triggered {d.triggered:,} "
          f"(rejected: conf {s['blocked_conf']}, far {s['blocked_far']}, "
          f"wide-stop {s['blocked_wide']}, unsized {s['blocked_size']})")
    if s["n"]:
        print(f"          exits: {s['reasons']}")
        print(f"          levels: {s['by_level']}")
    return s


def main():
    args = sys.argv[1:]
    scope = next((a for a in args if a.startswith("--")), "--mnq")
    p = params_from_env()

    print(f"MNQ_SweepAbsorption_PropEval [{p.rev}] -- bars={BAR_MODE}  "
          f"tol={p.tol_atr_mult}xATR/min{p.tol_min_ticks}t  "
          f"delta>={p.delta_sigma}sd  absratio>={p.abs_ratio_mult}xmed  "
          f"vol>={p.volume_mult}xmed  closepos>={p.close_pos_in_bar}")
    print(f"  expiry armed/swept/absorbed {p.armed_expiry}/{p.swept_expiry}/"
          f"{p.absorb_expiry}  stop={p.stop_atr_mult}xATR+ext  BE@{p.breakeven_r}R"
          f"  tgt={p.default_target_r}R in [{p.min_target_r},{p.max_target_r}]")
    print(f"  risk=${p.risk_per_trade:.0f} maxQ={p.max_contracts} "
          f"DT=${p.daily_target:.0f} DL=${p.daily_loss:.0f} "
          f"GB=${p.giveback:.0f} MT={p.max_trades_day} MCL={p.max_consec_loss} "
          f"| ${p.pt_val:.0f}/pt comm=${p.commission:.2f}/ctr "
          f"slip={p.slip_ticks}t fill="
          f"{'next-open' if p.entry_next_open else 'signal-close'}\n")

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
        pf_ok = sum(1 for s in out.values() if s["pf"] > 1.0)
        print(f"\n  POOLED  n={nn}  Net=${tot:+,.0f}  "
              f"contracts net>0: {pos}/{len(out)}  PF>1: {pf_ok}/{len(out)}")
        print(f"  SHIP GATE (net>0 AND PF>1 on every contract): "
              f"{'PASS' if pos == len(out) and pf_ok == len(out) else 'FAIL'}")

    # The trigger is "close reclaims the level / the sweep bar's extreme", so
    # filling AT that close hands the backtest a price no market order gets.
    if not p.entry_next_open:
        q = replace(p, entry_next_open=1)
        alt = {}
        for tag, scid in contracts_for(scope).items():
            if scid.exists():
                bars, sec = load_bars(tag, scid)
                alt[tag] = summarize(walk(bars, prep(bars, q, sec), q), q)
        print(f"\n  FILL CHECK  same signals, filled at the NEXT BAR'S OPEN "
              f"(what the DLL's market order actually gets):")
        print(f"    Net=${sum(s['total'] for s in alt.values()):+,.0f}  "
              f"n={sum(s['n'] for s in alt.values())}  "
              f"net>0: {sum(1 for s in alt.values() if s['total'] > 0)}/"
              f"{len(alt)}   [close-fill was "
              f"${sum(s['total'] for s in out.values()):+,.0f}]")
        for tag in alt:
            print(f"      {tag:7s} ${alt[tag]['total']:>+8,.0f}  "
                  f"(close-fill ${out[tag]['total']:>+8,.0f})")


if __name__ == "__main__":
    main()
