"""
Backtest of NQ_TrendFollow_PropEval.cpp  ("NQ Trend Following - Prop Eval")
on MNQ.

WHAT THE CPP IS (read from ~/Downloads/NQ_TrendFollow_PropEval.cpp, 2026-08-06)
  A 5-minute EMA-stack trend-follower with prop-evaluation governors. Nothing
  to do with the IOF M1..M8 engine — it is a standalone ACSIL study.

  Indicators   EMA(20) fast, EMA(50) slow, EMA(200) trend, ATR(14) Wilders,
               ADX(14/14) Wilders.
  Warmup       TrendLen + SlopeBars + 5 = 215 bars.

  Long entry (short is the exact mirror), evaluated ONLY on a closed bar and
  ONLY while flat:
    1. in 09:35-15:30, outside the 12:00-13:30 lunch window
    2. ATR >= 8.0 pts and ADX >= 20
    3. bar range > 0 and bar range <= 2.5 * ATR   (climax/news-bar skip)
    4. stack aligned:  fast>slow, slow>trend, close>trend, trend>trend[i-10]
    5. pullback:  some bar in [i-6, i] had low <= EMAfast of that bar
    6. breakout:  close > max(high) over [i-3, i-1]
    7. close > EMAfast
    8. close position in bar range >= 0.55

  Exit: attached OCO bracket, fixed offsets from fill.
    stop   = clamp(1.5 * ATR, 12, 45) pts, rounded to tick
    target = 2.5 * stop
    (In_UseTrailStop defaults to 0, so the trailing variant is off. Modelled
     anyway behind USE_TRAIL=1 for completeness.)

  Sizing: qty = floor(RiskDollars / (StopPts * $/pt)), capped at MaxContracts=6,
  and NO TRADE if that floors to 0. RiskDollars = min(175, DailyLoss - Buffer
  + DailyPL) -- i.e. it shrinks as the day goes against you.

  Governors, re-evaluated on EVERY update (so they can fire intrabar against
  open P/L, not just at bar close):
    daily loss  -600  -> flatten + halt
    daily target +1000 -> flatten + halt
    giveback     400 off the daily peak -> flatten + halt
    15:55 -> flatten + halt
    5 trades/day -> halt (no flatten)
    2 consecutive losses -> halt (no flatten)

MODELLING DECISIONS (each one is a place this differs from the DLL)
  * Entry fills at the CLOSE of the signal bar. The DLL sends a market order
    after that bar closes, so the true fill is ~the next bar's open. Repo
    convention; SLIP_TICKS stresses it.
  * The bracket is live from bar i+1 onward -- never on the signal bar itself.
  * Within a bar, the ADVERSE extreme is assumed to happen first: stop before
    target, loss-governor before profit-governor. Conservative.
  * Governors are checked against the bar extremes, so an intrabar flatten
    fills at the exact threshold price when the extreme overshoots it.
  * Commission $1.50 RT per MNQ contract, so it scales with the 1-6 qty.
  * No REGIME/AUTO_DISABLE analogue exists in this cpp, so there is no
    fidelity gap of the kind the IOF harness has.

STRUCTURE
  The entry-candidate set is a pure function of the bars and the signal
  parameters -- side, stop distance and target are all fixed before a single
  dollar of P/L exists. Only the governors are path-dependent. So the run is
  split in two:

    scan()      vectorised numpy pass over every bar -> candidate list. Cached
                per (bars, signal-parameter signature).
    simulate()  walks only the candidates and the bars inside an open trade,
                applying the governors. O(trades x trade length), not O(bars).

  That split is what makes the re-sign null and the parameter sweep cheap: 200
  null draws re-run simulate() 200 times against one cached scan().

DATA
  MNQU6 == FROZEN_MNQU6_0722.scid is the only genuine MNQ .scid in the tree
  (prices stored x100; detect_price_scale handles it). It is ONE contract, so
  per the repo rule it cannot decide anything on its own -- the six frozen NQ
  contracts are the same underlying index and are run in MNQ dollars
  ($2/point) to give the cross-contract read.

Usage
  python backtest_trendfollow_propeval.py --mnq          # MNQU6 only
  python backtest_trendfollow_propeval.py --all          # MNQU6 + 6 NQ frozen
  TF_TARGET_R=2.0 TF_TAG=r2 python backtest_trendfollow_propeval.py --all
"""
from __future__ import annotations

import csv
import os
import random
import sys
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import lfilter

from backtest import Bar
from fastbars import load_bars_cached

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

BAR_MINUTES = 5
TICK = 0.25


# ── parameters ──────────────────────────────────────────────────────────────
# Split into the two halves that matter for caching: SIG_FIELDS decide the
# candidate list, the rest only affect the governed walk.
@dataclass(frozen=True)
class Params:
    # -- signal (cpp inputs 0-9, 29, 30 + the stop geometry) --
    fast_len: int = 20
    slow_len: int = 50
    trend_len: int = 200
    slope_bars: int = 10
    adx_len: int = 14
    adx_min: float = 20.0
    atr_len: int = 14
    pullback_bars: int = 6
    trigger_bars: int = 3
    close_strength: float = 0.55
    stop_atr_mult: float = 1.5
    min_stop_pts: float = 12.0
    max_stop_pts: float = 45.0
    target_r: float = 2.5
    max_bar_atr: float = 2.5
    min_atr_pts: float = 8.0
    session_start: int = 935
    session_end: int = 1530
    notrade_start: int = 1200
    notrade_end: int = 1330
    # -- governed walk --
    use_trail: int = 0
    trail_offset_r: float = 1.0
    # "fixed"  -- the cpp as written: risk_per_trade flat, qty = floor(...)
    # "budget" -- see size_budget(). Splits the day's remaining loss room over
    #             the worst-case number of losses still permitted, and rounds
    #             qty to nearest instead of truncating.
    risk_mode: str = "fixed"
    risk_per_trade: float = 175.0
    max_contracts: int = 6
    daily_target: float = 1000.0
    daily_loss: float = 600.0
    giveback: float = 400.0        # flat $; used when giveback_r == 0
    # >0 expresses the giveback as a multiple of the risk unit instead, so it
    # re-scales automatically when the sizing model changes. A flat $400 is 4.4R
    # against 1-lot sizing and 1.5R against 3-lot -- the same governor, silently
    # 3x tighter, which is what made it start cutting winners.
    giveback_r: float = 0.0
    # single-trade risk ceiling, in risk units (budget mode only)
    max_risk_r: float = 1.0
    max_trades_day: int = 5
    max_consec_loss: int = 2
    loss_buffer: float = 50.0
    flatten_hhmm: int = 1555
    pt_val: float = 2.0          # MNQ $2/point
    commission: float = 1.50     # RT per contract
    slip_ticks: float = 0.0
    # 0 = fill at the signal bar's close (repo convention).
    # 1 = fill at the NEXT bar's open, which is what a market order sent after
    #     the bar closes actually gets, and exposes the trade to that bar's
    #     range. The honest fidelity check on any close-fill result.
    entry_next_open: int = 0

    @property
    def sig_key(self) -> tuple:
        return tuple(getattr(self, f) for f in SIG_FIELDS)


SIG_FIELDS = (
    "fast_len", "slow_len", "trend_len", "slope_bars", "adx_len", "adx_min",
    "atr_len", "pullback_bars", "trigger_bars", "close_strength",
    "stop_atr_mult", "min_stop_pts", "max_stop_pts", "target_r",
    "max_bar_atr", "min_atr_pts", "session_start", "session_end",
    "notrade_start", "notrade_end",
)

_ENV_ALIASES = {f.name: "TF_" + f.name.upper() for f in fields(Params)}


def params_from_env(base: Optional[Params] = None) -> Params:
    """TF_TARGET_R=2.0 TF_MAX_STOP_PTS=200 ... -- one-off overrides."""
    p = base or Params()
    kw = {}
    for name, env in _ENV_ALIASES.items():
        v = os.environ.get(env)
        if v is None:
            continue
        t = type(getattr(p, name))
        kw[name] = v if t is str else t(float(v))
    return replace(p, **kw) if kw else p


RUN_TAG = os.environ.get("TF_TAG", "")
# "as_is" | "inverse" | "random" -- set by the null-test harness, never by a run
SIDE_MODE = "as_is"


# ── bar arrays (built once per bars object) ─────────────────────────────────
@dataclass
class Arrays:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    hhmm: np.ndarray
    dtag: np.ndarray


_ARR_CACHE: Dict[int, Tuple[List[Bar], Arrays]] = {}


def arrays(bars: List[Bar]) -> Arrays:
    hit = _ARR_CACHE.get(id(bars))
    if hit is not None and hit[0] is bars:
        return hit[1]
    a = Arrays(
        o=np.fromiter((b.open for b in bars), np.float64, len(bars)),
        h=np.fromiter((b.high for b in bars), np.float64, len(bars)),
        l=np.fromiter((b.low for b in bars), np.float64, len(bars)),
        c=np.fromiter((b.close for b in bars), np.float64, len(bars)),
        hhmm=np.fromiter((b.hhmm for b in bars), np.int64, len(bars)),
        dtag=np.fromiter((b.date_tag for b in bars), np.int64, len(bars)),
    )
    _ARR_CACHE[id(bars)] = (bars, a)   # keep a ref so id() cannot be recycled
    return a


# ── indicators (Sierra semantics, vectorised) ───────────────────────────────
def ema(x: np.ndarray, period: int) -> np.ndarray:
    """sc.ExponentialMovAvg: seeded with the first value, k = 2/(n+1)."""
    k = 2.0 / (period + 1.0)
    # y[i] = (1-k) y[i-1] + k x[i], y[0] = x[0]
    return lfilter([k], [1.0, -(1.0 - k)], x, zi=np.array([(1.0 - k) * x[0]]))[0]


def _wilder_sum(x: np.ndarray, n: int) -> np.ndarray:
    """y[i] = y[i-1] - y[i-1]/n + x[i], seeded y[0] = x[0]. Sierra's smoothed
    TR / +DM / -DM accumulator (a sum, not a mean)."""
    a = 1.0 - 1.0 / n
    return lfilter([1.0], [1.0, -a], x, zi=np.array([a * x[0]]))[0]


def atr_wilders(a: Arrays, period: int) -> np.ndarray:
    """sc.ATR(..., MOVAVGTYPE_WILDERS). Bar 0 has no prior close, so its TR is
    the plain range -- matching the reference loop."""
    tr = np.empty(len(a.c))
    tr[0] = a.h[0] - a.l[0]
    tr[1:] = np.maximum.reduce([a.h[1:] - a.l[1:],
                                np.abs(a.h[1:] - a.c[:-1]),
                                np.abs(a.l[1:] - a.c[:-1])])
    # Wilder MEAN: y[i] = (y[i-1]*(n-1) + x[i]) / n, seeded y[0] = x[0]
    a_ = (period - 1.0) / period
    return lfilter([1.0 / period], [1.0, -a_], tr, zi=np.array([a_ * tr[0]]))[0]


def adx(a: Arrays, dx_len: int, ma_len: int) -> np.ndarray:
    """sc.ADX(BaseDataIn, Sg, DXLength, DXMovAvgLength) -- Wilder throughout.

    +DM/-DM and TR are Wilder-accumulated over dx_len; DX = 100*|DI+ - DI-| /
    (DI+ + DI-); ADX = Wilder MA of DX over ma_len, seeded with the running
    mean of the first ma_len DX values.
    """
    n = len(a.c)
    out = np.zeros(n)
    if n < 2:
        return out
    tr = np.maximum.reduce([a.h[1:] - a.l[1:],
                            np.abs(a.h[1:] - a.c[:-1]),
                            np.abs(a.l[1:] - a.c[:-1])])
    up = a.h[1:] - a.h[:-1]
    dn = a.l[:-1] - a.l[1:]
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)

    str_ = _wilder_sum(tr, dx_len)
    pdm = _wilder_sum(plus, dx_len)
    ndm = _wilder_sum(minus, dx_len)

    with np.errstate(divide="ignore", invalid="ignore"):
        di_p = np.where(str_ > 0, 100.0 * pdm / str_, 0.0)
        di_m = np.where(str_ > 0, 100.0 * ndm / str_, 0.0)
        s = di_p + di_m
        dx = np.where(s > 0, 100.0 * np.abs(di_p - di_m) / s, 0.0)

    # seed = expanding mean of the first ma_len DX values, then Wilder
    k = min(ma_len, len(dx))
    seed = np.cumsum(dx[:k]) / np.arange(1, k + 1)
    adx_run = np.empty(len(dx))
    adx_run[:k] = seed
    if len(dx) > k:
        a_ = (ma_len - 1.0) / ma_len
        adx_run[k:] = lfilter([1.0 / ma_len], [1.0, -a_], dx[k:],
                              zi=np.array([a_ * seed[-1]]))[0]
    out[1:] = adx_run
    return out


def _rolling_any(mask: np.ndarray, window: int) -> np.ndarray:
    """any(mask[i-window+1 : i+1]), left edge padded False."""
    if window <= 1:
        return mask.copy()
    cs = np.concatenate(([0], np.cumsum(mask.astype(np.int64))))
    idx = np.arange(len(mask))
    lo = np.maximum(idx - window + 1, 0)
    return (cs[idx + 1] - cs[lo]) > 0


def _rolling_extreme(x: np.ndarray, window: int, kind: str) -> np.ndarray:
    """max/min of x[i-window : i] (STRICTLY prior bars). Edge values are
    garbage but always sit inside the warm-up region."""
    n = len(x)
    out = np.full(n, -np.inf if kind == "max" else np.inf)
    fn = np.maximum if kind == "max" else np.minimum
    for k in range(1, window + 1):
        shifted = np.full(n, -np.inf if kind == "max" else np.inf)
        shifted[k:] = x[:-k]
        out = fn(out, shifted)
    return out


def round_to_tick(v):
    return np.round(np.asarray(v) / TICK) * TICK


# ── position sizing ─────────────────────────────────────────────────────────
_WCL: Dict[Tuple[int, int, int], int] = {}


def worst_case_losses(trades_left: int, max_consec: int, consec: int) -> int:
    """Most losing trades the day can still take before a governor halts it.

    The cpp sizes off a flat $175 with the comment "3 full losses = $525 < $600
    limit", but how many losses are actually reachable is a function of
    MaxTradesPerDay AND MaxConsecutiveLosses together -- at MT=5/MCL=2 it is 3
    (L W L W L), at MT=3/MCL=2 it is 2. Sizing off the real number is what lets
    the risk budget be spent instead of left on the table.
    """
    if max_consec > 0 and consec >= max_consec:
        return 0
    if trades_left <= 0:
        return 0
    key = (trades_left, max_consec, consec)
    hit = _WCL.get(key)
    if hit is not None:
        return hit
    if max_consec <= 0:                    # consec-loss governor disabled
        v = trades_left
    else:
        v = max(1 + worst_case_losses(trades_left - 1, max_consec, consec + 1),
                worst_case_losses(trades_left - 1, max_consec, 0))
    _WCL[key] = v
    return v


def risk_unit(p: Params) -> float:
    """The day's base risk unit -- "1R" for this config.

    In budget mode it is the day's loss room split over the worst-case number of
    losses the governors permit, evaluated at the START of the day, so it is a
    config constant rather than a path-dependent quantity. Everything that wants
    to be expressed in R (the giveback, the single-trade ceiling) anchors here,
    which is what stops a dollar-denominated governor from silently changing
    meaning when the sizing model changes.
    """
    if p.risk_mode == "fixed" or p.daily_loss <= 0.0:
        return p.risk_per_trade
    slots = worst_case_losses(min(p.max_trades_day or 20, 20),
                              p.max_consec_loss, 0)
    return (p.daily_loss - p.loss_buffer) / max(slots, 1)


def effective_giveback(p: Params) -> float:
    """Giveback in dollars: giveback_r * 1R when set, else the flat input."""
    return p.giveback_r * risk_unit(p) if p.giveback_r > 0.0 else p.giveback


def size_trade(p: Params, stop_pts: float, closed_pl: float,
               trades_today: int, consec: int) -> int:
    """Contracts for this entry. Returns 0 for "cannot size a trade"."""
    risk_per_ctr = stop_pts * p.pt_val
    if risk_per_ctr <= 0.0:
        return 0

    if p.risk_mode == "fixed":
        # cpp as written
        risk = p.risk_per_trade
        if p.daily_loss > 0.0:
            risk = min(risk, max(0.0, p.daily_loss - p.loss_buffer + closed_pl))
        qty = int(risk / risk_per_ctr)              # truncation, as in the cpp
        return min(qty, p.max_contracts) if qty >= 1 else 0

    # ── "budget" ────────────────────────────────────────────────────────────
    # room left before the day is over, split across the losses still reachable
    room = (p.daily_loss - p.loss_buffer + closed_pl if p.daily_loss > 0.0
            else p.risk_per_trade * 3.0)
    if room <= 0.0:
        return 0
    left = (p.max_trades_day - trades_today if p.max_trades_day > 0 else 20)
    slots = worst_case_losses(min(left, 20), p.max_consec_loss, consec)
    if slots <= 0:
        return 0
    risk = room / slots

    # `room` grows with the day's profit, so on the LAST permitted trade
    # (slots == 1) room/slots would stake every dollar earned so far -- a single
    # loss then walks the day from +$400 to the loss limit. Ceiling it at the
    # base risk unit keeps size flat through a winning day while still letting
    # it shrink through a losing one.
    if p.max_risk_r > 0.0:
        risk = min(risk, p.max_risk_r * risk_unit(p))

    # round to NEAREST -- truncating is what throws away half the budget when
    # the stop is wide enough that qty lands near 1
    qty = int(round(risk / risk_per_ctr))
    qty = max(qty, 1)
    # but never let the worst case for this single trade breach the room left
    qty = min(qty, int(room / risk_per_ctr))
    return min(qty, p.max_contracts) if qty >= 1 else 0


# ── candidate scan (pure function of bars + signal params) ──────────────────
@dataclass
class Cands:
    idx: np.ndarray        # bar index of the signal
    side: np.ndarray       # +1 / -1
    stop_pts: np.ndarray
    tgt_pts: np.ndarray
    atr: np.ndarray
    adx: np.ndarray
    warm: int


_SCAN_CACHE: Dict[Tuple[int, tuple], Tuple[List[Bar], Cands]] = {}
_IND_CACHE: Dict[Tuple[int, tuple], Tuple[List[Bar], tuple]] = {}

# The EMA/ATR/ADX lengths are a much coarser key than the full signal
# signature, so a sweep over thresholds (adx_min, close_strength, target_r ...)
# recomputes the gates but never the smoothers.
IND_FIELDS = ("fast_len", "slow_len", "trend_len", "atr_len", "adx_len")


def indicators(bars: List[Bar], p: Params):
    """(fast, slow, trend, atr, adx) -- cached on the smoother lengths alone."""
    key = (id(bars), tuple(getattr(p, f) for f in IND_FIELDS))
    hit = _IND_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]
    a = arrays(bars)
    v = (ema(a.c, p.fast_len), ema(a.c, p.slow_len), ema(a.c, p.trend_len),
         atr_wilders(a, p.atr_len), adx(a, p.adx_len, p.adx_len))
    _IND_CACHE[key] = (bars, v)
    return v


def scan(bars: List[Bar], p: Params) -> Cands:
    key = (id(bars), p.sig_key)
    hit = _SCAN_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]

    a = arrays(bars)
    n = len(bars)
    warm = p.trend_len + p.slope_bars + 5      # cpp MinBars
    if n <= warm:
        empty = np.array([], dtype=np.int64)
        c = Cands(empty, empty, empty.astype(float), empty.astype(float),
                  empty.astype(float), empty.astype(float), warm)
        _SCAN_CACHE[key] = (bars, c)
        return c

    fast, slow, trend, atr_v, adx_v = indicators(bars, p)

    ok = np.zeros(n, dtype=bool)
    ok[warm:] = True
    ok &= (a.hhmm >= p.session_start) & (a.hhmm <= p.session_end)
    ok &= ~((a.hhmm >= p.notrade_start) & (a.hhmm < p.notrade_end))
    ok &= (atr_v > 0.0) & (atr_v >= p.min_atr_pts) & (adx_v >= p.adx_min)

    rng = a.h - a.l
    ok &= rng > 0.0
    if p.max_bar_atr > 0.0:
        ok &= rng <= p.max_bar_atr * atr_v
    if not ok.any():
        empty = np.array([], dtype=np.int64)
        c = Cands(empty, empty, empty.astype(float), empty.astype(float),
                  empty.astype(float), empty.astype(float), warm)
        _SCAN_CACHE[key] = (bars, c)
        return c

    trend_prior = np.empty(n)
    trend_prior[:p.slope_bars] = np.inf          # never aligns inside warm-up
    trend_prior[p.slope_bars:] = trend[:-p.slope_bars]

    up_al = (fast > slow) & (slow > trend) & (a.c > trend) & (trend > trend_prior)
    dn_al = (fast < slow) & (slow < trend) & (a.c < trend) & (trend < trend_prior)

    pb_long = _rolling_any(a.l <= fast, p.pullback_bars + 1)
    pb_short = _rolling_any(a.h >= fast, p.pullback_bars + 1)

    prior_hi = _rolling_extreme(a.h, p.trigger_bars, "max")
    prior_lo = _rolling_extreme(a.l, p.trigger_bars, "min")

    with np.errstate(divide="ignore", invalid="ignore"):
        close_pos = np.where(rng > 0, (a.c - a.l) / rng, 0.0)

    long_sig = (ok & up_al & pb_long & (a.c > prior_hi) & (a.c > fast)
                & (close_pos >= p.close_strength))
    short_sig = (ok & dn_al & pb_short & (a.c < prior_lo) & (a.c < fast)
                 & ((1.0 - close_pos) >= p.close_strength))

    idx = np.flatnonzero(long_sig | short_sig)
    side = np.where(long_sig[idx], 1, -1).astype(np.int64)
    sp = round_to_tick(np.clip(p.stop_atr_mult * atr_v[idx],
                               p.min_stop_pts, p.max_stop_pts))
    c = Cands(idx=idx, side=side, stop_pts=sp,
              tgt_pts=round_to_tick(sp * p.target_r),
              atr=atr_v[idx], adx=adx_v[idx], warm=warm)
    _SCAN_CACHE[key] = (bars, c)
    return c


# ── trade record ────────────────────────────────────────────────────────────
@dataclass
class Trade:
    date_tag: int
    side: int
    qty: int
    entry_hhmm: int
    entry_px: float
    stop_px: float
    tp_px: float
    stop_pts: float
    atr: float
    adx: float
    init_stop: float = 0.0
    peak: float = 0.0
    trailed: bool = False
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
    blocked_size: int = 0     # signals killed by "cannot size a contract"


@dataclass
class Result:
    trades: List[Trade] = field(default_factory=list)
    days: List[DayStat] = field(default_factory=list)


_FLATTEN_REASONS = ("LOSS_LIMIT", "GIVEBACK", "DAILY_TARGET", "FLAT_1555")


# ── governed walk ───────────────────────────────────────────────────────────
def simulate(bars: List[Bar], cands: Cands, p: Params,
             side_mode: str = "as_is") -> Result:
    """Walk the candidate list under the daily governors.

    Only bars inside an open trade are visited; flat bars cannot change any
    governor state (marked-to-market P/L equals closed P/L while flat), so
    skipping them is exact, not an approximation.
    """
    res = Result()
    a = arrays(bars)
    n = len(bars)
    slip = p.slip_ticks * TICK
    ppv = p.pt_val
    giveback = effective_giveback(p)

    day = -1
    ds: Optional[DayStat] = None
    closed_pl = peak_pl = 0.0
    consec_losses = trades_today = 0
    halted = False
    ci = 0
    ncand = len(cands.idx)

    while ci < ncand:
        i = int(cands.idx[ci])
        b = bars[i]

        # ── daily reset ─────────────────────────────────────────────────────
        if b.date_tag != day:
            if ds is not None:
                ds.pnl = closed_pl
            day = b.date_tag
            ds = DayStat(date_tag=day)
            res.days.append(ds)
            closed_pl = peak_pl = 0.0
            consec_losses = trades_today = 0
            halted = False

        if halted:
            ci += 1
            continue
        if p.max_trades_day > 0 and trades_today >= p.max_trades_day:
            halted, ds.halt = True, "MAX_TRADES"
            ci += 1
            continue
        if p.max_consec_loss > 0 and consec_losses >= p.max_consec_loss:
            halted, ds.halt = True, "CONSEC_LOSS"
            ci += 1
            continue

        # ── sizing ──────────────────────────────────────────────────────────
        stop_pts = float(cands.stop_pts[ci])
        qty = size_trade(p, stop_pts, closed_pl, trades_today, consec_losses)
        if qty < 1:
            ds.blocked_size += 1
            ci += 1
            continue

        side = int(cands.side[ci])
        if side_mode == "inverse":
            side = -side
        elif side_mode == "random":
            side = random.choice((1, -1))

        stop_eff = (round_to_tick(stop_pts * p.trail_offset_r) if p.use_trail
                    else stop_pts)
        if p.entry_next_open:
            if i + 1 >= n or a.dtag[i + 1] != b.date_tag:
                ci += 1
                continue
            entry = a.o[i + 1] + side * slip
            first_managed = i + 1     # the fill bar's own range can stop us out
        else:
            entry = b.close + side * slip
            first_managed = i + 1
        t = Trade(date_tag=b.date_tag, side=side, qty=qty, entry_hhmm=b.hhmm,
                  entry_px=entry, stop_px=entry - side * stop_eff,
                  tp_px=entry + side * float(cands.tgt_pts[ci]),
                  stop_pts=stop_pts, atr=float(cands.atr[ci]),
                  adx=float(cands.adx[ci]),
                  init_stop=entry - side * stop_eff, peak=entry)
        trades_today += 1
        dpp = ppv * qty * side          # $ per point of favourable move

        # ── manage: bracket live from the first managed bar ─────────────────
        j = first_managed
        ex: Optional[Tuple[float, str]] = None
        while j < n:
            if a.dtag[j] != t.date_tag:
                # 15:55 flatten should have fired first; this is the guard
                ex = (bars[j - 1].close, "EOD")
                j -= 1
                break
            hi, lo = a.h[j], a.l[j]
            adverse, favorable = (lo, hi) if side > 0 else (hi, lo)
            t.mae = min(t.mae, (adverse - entry) * side)
            t.mfe = max(t.mfe, (favorable - entry) * side)
            t.peak = max(t.peak, hi) if side > 0 else min(t.peak, lo)

            # 1. protective stop (or trail) -- the bracket, hit first on a tie
            if (lo <= t.stop_px) if side > 0 else (hi >= t.stop_px):
                ex = (t.stop_px, "TRAIL" if t.trailed else "STOP")
                break

            # 2. loss / giveback governors against the adverse extreme
            pl_adv = closed_pl + (adverse - entry) * dpp
            hit = None
            if p.daily_loss > 0.0 and pl_adv <= -p.daily_loss:
                hit = ("LOSS_LIMIT", -p.daily_loss)
            elif (giveback > 0.0 and peak_pl > 0.0
                  and (peak_pl - pl_adv) >= giveback):
                hit = ("GIVEBACK", peak_pl - giveback)
            if hit is not None:
                px = entry + (hit[1] - closed_pl) / dpp
                ex = (min(max(px, lo), hi), hit[0])
                break

            # 3. bracket target
            if (hi >= t.tp_px) if side > 0 else (lo <= t.tp_px):
                ex = (t.tp_px, "TARGET")
                break

            # 4. daily profit target against the favourable extreme
            if p.daily_target > 0.0:
                pl_fav = closed_pl + (favorable - entry) * dpp
                if pl_fav >= p.daily_target:
                    px = entry + (p.daily_target - closed_pl) / dpp
                    ex = (min(max(px, lo), hi), "DAILY_TARGET")
                    break

            # 5. hard flatten time
            if a.hhmm[j] >= p.flatten_hhmm:
                ex = (a.c[j], "FLAT_1555")
                break

            # trailing stop -- updates off this bar's close and applies from
            # bar j+1, so the checks above cannot look ahead
            if p.use_trail:
                dist = float(round_to_tick(stop_pts * p.trail_offset_r))
                cand_px = (t.peak - dist) if side > 0 else (t.peak + dist)
                if (cand_px > t.stop_px) if side > 0 else (cand_px < t.stop_px):
                    t.stop_px, t.trailed = cand_px, True

            peak_pl = max(peak_pl, closed_pl + (a.c[j] - entry) * dpp)
            j += 1

        if ex is None:                       # ran out of bars
            j = n - 1
            ex = (a.c[j], "EOD")

        # ── book it ─────────────────────────────────────────────────────────
        t.exit_px = ex[0] - side * slip
        t.exit_hhmm = int(a.hhmm[j])
        t.reason = ex[1]
        t.pnl = (t.exit_px - entry) * side * ppv * qty - p.commission * qty
        res.trades.append(t)
        ds.n += 1
        closed_pl += t.pnl
        peak_pl = max(peak_pl, closed_pl)
        if t.pnl < 0.0:
            consec_losses += 1
        elif t.pnl > 0.0:
            consec_losses = 0
        if ex[1] in _FLATTEN_REASONS:
            halted, ds.halt = True, ex[1]

        # a bar that closes a trade may also open the next one, as in the cpp
        while ci < ncand and cands.idx[ci] < j:
            ci += 1

    if ds is not None:
        ds.pnl = closed_pl
    return res


def run_engine(bars: List[Bar], p: Optional[Params] = None,
               side_mode: Optional[str] = None) -> Result:
    p = p or params_from_env()
    return simulate(bars, scan(bars, p), p,
                    side_mode if side_mode is not None else SIDE_MODE)


# ── reporting ───────────────────────────────────────────────────────────────
def summarize(r: Result, p: Params) -> dict:
    pnls = np.array([t.pnl for t in r.trades])
    n = len(pnls)
    days = [d for d in r.days if d.n > 0]
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0, t=0.0,
                    days=0, worst_day=0.0, best_day=0.0, breaches=0,
                    avg=0.0, reasons={}, longs=0)
    wins, loss = pnls[pnls > 0], pnls[pnls < 0]
    mean = pnls.mean()
    se = pnls.std(ddof=1) / np.sqrt(n) if n > 1 else 0.0
    run = np.cumsum(pnls)
    mdd = float((run - np.maximum.accumulate(np.r_[0.0, run])[1:]).min())
    day_pnls = [d.pnl for d in days]
    reasons: Dict[str, int] = {}
    for t in r.trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return dict(
        n=n, total=float(pnls.sum()),
        pf=(float(wins.sum() / -loss.sum()) if len(loss) else float("inf")),
        wr=100.0 * len(wins) / n, max_dd=mdd,
        t=float(mean / se) if se > 0 else 0.0, avg=float(mean),
        days=len(days),
        worst_day=min(day_pnls) if day_pnls else 0.0,
        best_day=max(day_pnls) if day_pnls else 0.0,
        # a prop account fails AT the number: count days closing past the limit
        breaches=sum(1 for x in day_pnls if x <= -p.daily_loss),
        reasons=reasons,
        longs=sum(1 for t in r.trades if t.side > 0),
    )


def write_csv(r: Result, path: Path):
    tot = 0.0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Event", "Date", "Time", "Side", "Qty", "Price", "Stop",
                    "Target", "StopPts", "ATR", "ADX", "Reason", "PnL",
                    "TotalPnL", "MAE", "MFE"])
        for t in r.trades:
            w.writerow(["ENTRY", t.date_tag, t.entry_hhmm,
                        "LONG" if t.side > 0 else "SHORT", t.qty,
                        f"{t.entry_px:.2f}", f"{t.init_stop:.2f}",
                        f"{t.tp_px:.2f}", f"{t.stop_pts:.2f}",
                        f"{t.atr:.2f}", f"{t.adx:.1f}", "", "",
                        f"{tot:.2f}", "", ""])
            tot += t.pnl
            w.writerow(["EXIT", t.date_tag, t.exit_hhmm,
                        "LONG" if t.side > 0 else "SHORT", t.qty,
                        f"{t.exit_px:.2f}", "", "", "", "", "", t.reason,
                        f"{t.pnl:.2f}", f"{tot:.2f}",
                        f"{t.mae:.2f}", f"{t.mfe:.2f}"])


# The IS/OOS split used to test ADX_MIN=30: fit on the three 2025 expiries,
# hold out everything that expires in 2026 plus the genuine MNQ contract.
IS_TAGS = ("NQU25", "NQZ25", "NQM5")
OOS_TAGS = ("NQH6", "NQM6", "NQU26", "MNQU6")


def contracts_for(scope: str) -> Dict[str, Path]:
    # TF_TAGS=NQH6,NQM6,MNQU6 -- explicit subset, overrides scope. NQU26 sits
    # entirely inside MNQU6's date range (28 of 28 days, same index, micro vs
    # mini), so any pool holding both counts that window twice.
    env = os.environ.get("TF_TAGS")
    if env:
        everything = {**MNQ, **NQ_FROZEN}
        return {t: everything[t] for t in env.split(",") if t in everything}
    if scope == "--mnq":
        return dict(MNQ)
    if scope == "--nq":
        return dict(NQ_FROZEN)
    everything = {**MNQ, **NQ_FROZEN}
    if scope == "--is":
        return {t: everything[t] for t in IS_TAGS}
    if scope == "--oos":
        return {t: everything[t] for t in OOS_TAGS}
    return everything


def run_one(tag: str, scid: Path, p: Params, write: bool = True) -> dict:
    bars = load_bars_cached(tag, scid, BAR_MINUTES)
    r = simulate(bars, scan(bars, p), p)
    if write:
        suffix = f"_{RUN_TAG}" if RUN_TAG else ""
        write_csv(r, BASE / f"IOF_tfpe{suffix}_{tag}.csv")
    s = summarize(r, p)
    print(f"  {tag:7s} bars={len(bars):>7,} days={s['days']:>4} n={s['n']:>4} "
          f"L/S={s['longs']}/{s['n']-s['longs']:<4} WR={s['wr']:>5.1f}% "
          f"PF={s['pf']:>5.2f} Net=${s['total']:>+9,.0f} avg=${s['avg']:>+7,.1f} "
          f"MaxDD=${s['max_dd']:>+8,.0f} t={s['t']:>+5.2f} "
          f"worstday=${s['worst_day']:>+7,.0f} breach={s['breaches']}")
    print(f"          exits: {s['reasons']}")
    return s


def main():
    args = sys.argv[1:]
    scope = next((a for a in args if a.startswith("--")), "--mnq")
    p = params_from_env()

    print(f"NQ_TrendFollow_PropEval -- {BAR_MINUTES}m, EMA{p.fast_len}/"
          f"{p.slow_len}/{p.trend_len}, ADX>={p.adx_min:.0f}, "
          f"ATR>={p.min_atr_pts:.0f}, stop={p.stop_atr_mult}xATR"
          f"[{p.min_stop_pts:.0f},{p.max_stop_pts:.0f}], tgt={p.target_r}R, "
          f"trail={p.use_trail}")
    print(f"  risk=${p.risk_per_trade:.0f}/trade maxQ={p.max_contracts} "
          f"DT=${p.daily_target:.0f} DL=${p.daily_loss:.0f} "
          f"GB=${p.giveback:.0f} MT={p.max_trades_day} "
          f"MCL={p.max_consec_loss} | ${p.pt_val:.0f}/pt "
          f"comm=${p.commission:.2f}/ctr slip={p.slip_ticks}t\n")

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

    # The entry trigger is "close beyond the prior N-bar extreme", so filling AT
    # that close hands the backtest a price no market order can get. Always show
    # the honest fill next to it -- for this strategy the gap is not cosmetic.
    if not p.entry_next_open:
        q = replace(p, entry_next_open=1)
        alt = {}
        for tag, scid in contracts_for(scope).items():
            if scid.exists():
                bars = load_bars_cached(tag, scid, BAR_MINUTES)
                alt[tag] = summarize(simulate(bars, scan(bars, q), q), q)
        a_tot = sum(s["total"] for s in alt.values())
        a_pos = sum(1 for s in alt.values() if s["total"] > 0)
        print(f"\n  FILL CHECK  same signals, filled at the NEXT BAR'S OPEN "
              f"(what the DLL's market order actually gets):")
        print(f"    Net=${a_tot:+,.0f}  n={sum(s['n'] for s in alt.values())}  "
              f"net>0: {a_pos}/{len(alt)}   "
              f"[close-fill was ${sum(s['total'] for s in out.values()):+,.0f}]")
        for tag in alt:
            print(f"      {tag:7s} ${alt[tag]['total']:>+8,.0f}  "
                  f"(close-fill ${out[tag]['total']:>+8,.0f})")


if __name__ == "__main__":
    main()
