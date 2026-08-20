"""
Mizan_IOF_NQ -- accumulation -> manipulation -> distribution, entered on the
pullback to the accumulation POC.

THE PATTERN, STATED SO IT CAN BE FALSIFIED
  Read the classic AMD / "power of three" story as three testable conditions on
  closed 5-minute bars. Long case throughout; short is the exact mirror.

  A. ACCUMULATION  bars [m-acc_bars, m-1] are in balance, not in a leg:
       acc_hi/acc_lo/acc_rng = high/low/range of that window
       acc_rng <= acc_max_atr * ATR            (compression)
       |close[m-1] - close[m-acc_bars]| <= acc_drift * acc_rng
     The drift clause is what separates a balance from a quiet trending
     channel -- a channel also has a small range per bar but walks somewhere.
     The window must lie inside the manipulation bar's own date_tag, so a
     profile is never stitched across two sessions.

  B. MANIPULATION  bar m sweeps the accumulation low and is rejected:
       low[m]  <  acc_lo - sweep_eps_atr * ATR   (a real break, not one tick)
       close[m] > acc_lo                         (reclaimed -> failed breakdown)
       close position in bar range >= sweep_close_pos
     The sweep low is the trade's structural invalidation and the only price
     the stop is ever derived from.

  C. DISTRIBUTION  within dist_bars after m, some bar d closes clear of value
     in the direction opposite the sweep:
       close[d] > poc + dist_min_atr * ATR
     with poc = point of control of the ACCUMULATION window's volume profile.
     Aborted if any bar first closes back below the sweep low -- that is the
     sweep succeeding, not failing.

  ENTRY  a resting limit at the POC band, poc + poc_tol_atr * ATR, armed for
  pb_bars bars after d. Filled only if the bar trades THROUGH the level by
  require_through ticks, at min(open, level).

  Using a limit is not a detail. This repo's standing finding is that filling a
  breakout trigger at its own signal-bar close hands the backtest a price no
  market order gets. A limit at a level price must trade through is the opposite
  bias: the fill is real, and what it risks instead is the setups that ran
  without ever coming back, which simply never fill and never book anything.

  STOP    sweep low - stop_buf_atr * ATR, then the DISTANCE clamped to
          [min_stop_atr, max_stop_atr] x ATR and the stop price rebuilt from it.
          Everything is ATR-denominated on purpose: the repo has a documented
          case of point thresholds silently selecting a biased subsample as the
          index went 20k -> 30k.
  TARGET  target_r x stop distance. Also a time stop (max_hold_bars) and the
          15:55 flatten.

WHAT IS AND IS NOT MODELLED
  * The volume profile is built from 5-minute bars by spreading each bar's
    volume uniformly across the bins its range covers. That is an approximation
    of a real tick profile -- it cannot see where inside the bar the volume
    actually traded. It is stable and look-ahead-free, which is what the POC has
    to be here; a tick-exact profile would move the POC by a bin or two.
  * POC ties resolve to the lowest bin. Deterministic, arbitrary, documented.
  * One position at a time. A candidate whose entry bar falls inside an open
    trade is dropped, not queued.
  * $20/point, $5 RT commission -- NQ production numbers. SLIP_TICKS stresses
    the fill.
  * Daily governors default OFF. This measures the signal first; a prop-eval
    governor set can be layered afterwards, and layering it first is how you end
    up measuring the governor.

STRUCTURE
  scan()      pure function of (bars, signal params) -> candidate list. Side,
              entry price, stop distance and target are all fixed here, before a
              dollar of P/L exists.
  simulate()  walks the candidates under the governors.
  That split is what makes backtest_mizan_null.py cheap: 200 re-sign draws
  re-run simulate() against one cached scan().

Usage
  python backtest_mizan_iof_nq.py --nq          # 6 frozen NQ contracts
  python backtest_mizan_iof_nq.py --all
  MZ_TARGET_R=3.0 MZ_TAG=r3 python backtest_mizan_iof_nq.py --nq
  python backtest_mizan_null.py 200 --nq        # the honest direction test
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
NQ_FROZEN = {
    "NQU25": BASE / "F.US.ENQU25.scid",
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
    "NQM6":  BASE / "F.US.ENQM26.scid",
    "NQU26": BASE / "F.US.ENQU26.scid",
}
MNQ = {"MNQU6": BASE / "FROZEN_MNQU6_0722.scid"}
ES_FROZEN = {
    "ESM5": BASE / "FROZEN_ESM5.CME.scid",
    "ESU5": BASE / "FROZEN_ESU5.CME.scid",
    "ESZ5": BASE / "FROZEN_ESZ5.CME.scid",
    "ESH6": BASE / "FROZEN_ESH6.CME.scid",
    "ESM6": BASE / "FROZEN_ESM6.CME.scid",
    "ESU6": BASE / "FROZEN_ESU6.CME.scid",
}

# NQU26 sits entirely inside MNQU6's date range (same index, micro vs mini), so
# a pool holding both counts that window twice. Split IS/OOS by expiry year and
# keep the pair apart.
IS_TAGS = ("NQU25", "NQZ25", "NQM5")
OOS_TAGS = ("NQH6", "NQM6", "NQU26")

BAR_MINUTES = 5
TICK = 0.25


@dataclass(frozen=True)
class Params:
    # ── A: accumulation ────────────────────────────────────────────────────
    acc_bars: int = 12            # 60 minutes of balance
    acc_max_atr: float = 3.0      # window range <= 3 ATR
    acc_drift: float = 0.5        # net close-to-close drift <= 0.5 x range
    # ── B: manipulation ────────────────────────────────────────────────────
    sweep_eps_atr: float = 0.05   # must break the extreme by this much
    sweep_close_pos: float = 0.5  # and close in the rejecting half of its range
    # ── C: distribution ────────────────────────────────────────────────────
    dist_bars: int = 6            # confirm within this many bars of the sweep
    dist_min_atr: float = 0.5     # close this far clear of the POC
    # ── entry ──────────────────────────────────────────────────────────────
    # "poc"     the strategy as specified: rest a limit at the POC band.
    # "confirm" CONTROL, not a variant to ship. Takes the same A/M/D setups at
    #           the distribution bar, filled at the NEXT bar's open. If the
    #           sequence carries directional information, this sees it; if only
    #           this one works, the POC pullback is what is broken, and if
    #           neither does, the pattern is. Costs the pullback's better price
    #           and fills every setup, including the ones that never came back.
    entry_mode: str = "poc"       # "poc" | "confirm"
    pb_bars: int = 8              # limit armed this many bars after confirmation
    poc_tol_atr: float = 0.10     # limit sits this far the near side of the POC
    require_through: float = 1.0  # ticks the bar must trade THROUGH the limit
    bin_pts: float = 1.0          # volume-profile bin width, points
    # ── stop / target ──────────────────────────────────────────────────────
    stop_buf_atr: float = 0.15    # beyond the sweep extreme
    min_stop_atr: float = 0.40
    max_stop_atr: float = 2.50
    target_r: float = 2.0
    max_hold_bars: int = 24
    # ── session ────────────────────────────────────────────────────────────
    atr_len: int = 14
    sweep_start: int = 900        # manipulation bar not before this
    session_start: int = 935      # entry fill not before this
    session_end: int = 1530       # ... nor after
    flatten_hhmm: int = 1555
    # ── sizing / governors (all-zero = off) ────────────────────────────────
    qty: int = 1
    pt_val: float = 20.0          # NQ $20/pt
    commission: float = 5.00      # RT per contract
    slip_ticks: float = 0.0
    daily_loss: float = 0.0
    daily_target: float = 0.0
    max_trades_day: int = 0
    max_consec_loss: int = 0

    @property
    def sig_key(self) -> tuple:
        return tuple(getattr(self, f) for f in SIG_FIELDS)


SIG_FIELDS = (
    "acc_bars", "acc_max_atr", "acc_drift", "sweep_eps_atr", "sweep_close_pos",
    "dist_bars", "dist_min_atr", "entry_mode", "pb_bars", "poc_tol_atr",
    "require_through", "bin_pts", "stop_buf_atr", "min_stop_atr",
    "max_stop_atr", "target_r", "atr_len", "sweep_start", "session_start",
    "session_end",
)

_ENV_ALIASES = {f.name: "MZ_" + f.name.upper() for f in fields(Params)}


def params_from_env(base: Optional[Params] = None) -> Params:
    """MZ_TARGET_R=3.0 MZ_ACC_BARS=18 ... -- one-off overrides."""
    p = base or Params()
    kw = {}
    for name, env in _ENV_ALIASES.items():
        v = os.environ.get(env)
        if v is None:
            continue
        t = type(getattr(p, name))
        kw[name] = v if t is str else t(float(v))
    return replace(p, **kw) if kw else p


RUN_TAG = os.environ.get("MZ_TAG", "")
SIDE_MODE = "as_is"       # set only by the null harness


# ── bar arrays ──────────────────────────────────────────────────────────────
@dataclass
class Arrays:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
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
        v=np.fromiter((b.volume for b in bars), np.float64, len(bars)),
        hhmm=np.fromiter((b.hhmm for b in bars), np.int64, len(bars)),
        dtag=np.fromiter((b.date_tag for b in bars), np.int64, len(bars)),
    )
    _ARR_CACHE[id(bars)] = (bars, a)      # hold a ref so id() cannot recycle
    return a


def atr_wilders(a: Arrays, period: int) -> np.ndarray:
    """sc.ATR(..., MOVAVGTYPE_WILDERS). Bar 0 has no prior close, so its TR is
    the plain range."""
    tr = np.empty(len(a.c))
    tr[0] = a.h[0] - a.l[0]
    tr[1:] = np.maximum.reduce([a.h[1:] - a.l[1:],
                                np.abs(a.h[1:] - a.c[:-1]),
                                np.abs(a.l[1:] - a.c[:-1])])
    k = (period - 1.0) / period
    return lfilter([1.0 / period], [1.0, -k], tr, zi=np.array([k * tr[0]]))[0]


def _rolling_extreme(x: np.ndarray, window: int, kind: str) -> np.ndarray:
    """max/min of x[i-window : i] -- STRICTLY prior bars. Edge values are
    garbage but always sit inside the warm-up region."""
    n = len(x)
    fill = -np.inf if kind == "max" else np.inf
    out = np.full(n, fill)
    fn = np.maximum if kind == "max" else np.minimum
    for k in range(1, window + 1):
        sh = np.full(n, fill)
        sh[k:] = x[:-k]
        out = fn(out, sh)
    return out


def round_to_tick(v):
    return np.round(np.asarray(v) / TICK) * TICK


# ── volume profile ──────────────────────────────────────────────────────────
def poc_of(a: Arrays, lo_i: int, hi_i: int, bin_pts: float) -> float:
    """Point of control of bars [lo_i, hi_i), profiled at bin_pts resolution.

    Each bar's volume is spread uniformly over the bins its range covers, laid
    down with a difference-array + cumsum so the whole window is O(bars + bins)
    rather than O(bars x bins). Ties go to the lowest bin.
    """
    lo = a.l[lo_i:hi_i].min()
    hi = a.h[lo_i:hi_i].max()
    if not np.isfinite(lo) or not np.isfinite(hi) or hi < lo:
        return float("nan")
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
    return float(lo + (int(np.argmax(prof)) + 0.5) * bin_pts)


# ── candidates ──────────────────────────────────────────────────────────────
@dataclass
class Cands:
    idx: np.ndarray         # bar the limit fills on
    side: np.ndarray        # +1 long / -1 short
    entry_px: np.ndarray
    stop_pts: np.ndarray
    tgt_pts: np.ndarray
    atr: np.ndarray
    poc: np.ndarray
    acc_rng: np.ndarray
    sweep_idx: np.ndarray   # the manipulation bar
    wait: np.ndarray        # bars from sweep to fill
    warm: int
    # First bar the bracket is live on. This is NOT cosmetic. A limit filled
    # somewhere inside bar e cannot be managed against bar e's own range -- we
    # do not know where in the bar it filled -- so it manages from e+1. A market
    # order filled at bar e's OPEN is exposed to all of bar e, and starting it
    # at e+1 would silently delete the first bar of adverse excursion from every
    # trade. That is the shape of the look-ahead that made the absorption thread
    # print +$19k before it was fixed to -$14.6k, so scan() sets this per
    # candidate rather than letting simulate() assume.
    manage_at: Optional[np.ndarray] = None


_SCAN_CACHE: Dict[Tuple[int, tuple], Tuple[List[Bar], Cands]] = {}


def _empty(warm: int) -> Cands:
    e = np.array([], dtype=np.int64)
    f = np.array([], dtype=np.float64)
    return Cands(e, e, f, f, f, f, f, f, e, e, warm)


def scan(bars: List[Bar], p: Params) -> Cands:
    key = (id(bars), p.sig_key)
    hit = _SCAN_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]

    a = arrays(bars)
    n = len(bars)
    warm = p.acc_bars + p.atr_len + 5
    if n <= warm + p.dist_bars + p.pb_bars + 2:
        c = _empty(warm)
        _SCAN_CACHE[key] = (bars, c)
        return c

    atr = atr_wilders(a, p.atr_len)
    atr_prev = np.empty(n)
    atr_prev[0] = atr[0]
    atr_prev[1:] = atr[:-1]        # ATR as of the bar BEFORE the sweep

    # ── A: accumulation over [i-acc_bars, i-1] ─────────────────────────────
    acc_hi = _rolling_extreme(a.h, p.acc_bars, "max")
    acc_lo = _rolling_extreme(a.l, p.acc_bars, "min")
    acc_rng = acc_hi - acc_lo

    c_prev = np.empty(n)
    c_prev[0] = a.c[0]
    c_prev[1:] = a.c[:-1]
    c_first = np.full(n, np.nan)
    c_first[p.acc_bars:] = a.c[:-p.acc_bars]
    d_first = np.zeros(n, dtype=np.int64)
    d_first[p.acc_bars:] = a.dtag[:-p.acc_bars]

    ok = np.zeros(n, dtype=bool)
    ok[warm:] = True
    ok &= d_first == a.dtag                       # window inside one session
    ok &= np.isfinite(acc_rng) & (acc_rng > 0.0) & (atr_prev > 0.0)
    ok &= acc_rng <= p.acc_max_atr * atr_prev
    ok &= np.abs(c_prev - c_first) <= p.acc_drift * acc_rng
    ok &= (a.hhmm >= p.sweep_start) & (a.hhmm <= p.session_end)

    # ── B: manipulation ────────────────────────────────────────────────────
    rng = a.h - a.l
    with np.errstate(divide="ignore", invalid="ignore"):
        cpos = np.where(rng > 0, (a.c - a.l) / rng, 0.0)
    eps = p.sweep_eps_atr * atr_prev
    long_sw = (ok & (a.l < acc_lo - eps) & (a.c > acc_lo)
               & (cpos >= p.sweep_close_pos))
    short_sw = (ok & (a.h > acc_hi + eps) & (a.c < acc_hi)
                & ((1.0 - cpos) >= p.sweep_close_pos))

    sweeps = np.flatnonzero(long_sw | short_sw)
    if len(sweeps) == 0:
        c = _empty(warm)
        _SCAN_CACHE[key] = (bars, c)
        return c

    # ── C + entry: short forward walk per sweep (a few hundred, not n) ─────
    out: List[tuple] = []
    for m in sweeps:
        m = int(m)
        side = 1 if long_sw[m] else -1
        A = float(atr_prev[m])
        poc = poc_of(a, m - p.acc_bars, m, p.bin_pts)
        if not np.isfinite(poc):
            continue
        invalid = a.l[m] if side > 0 else a.h[m]     # the sweep extreme

        # C: first bar closing clear of value, opposite the sweep
        d = -1
        lim_c = poc + side * p.dist_min_atr * A
        for j in range(m + 1, min(m + 1 + p.dist_bars, n)):
            if a.dtag[j] != a.dtag[m]:
                break
            if (a.c[j] < invalid) if side > 0 else (a.c[j] > invalid):
                break                                # sweep succeeded, not a trap
            if (a.c[j] > lim_c) if side > 0 else (a.c[j] < lim_c):
                d = j
                break
        if d < 0:
            continue

        e, fill = -1, 0.0
        if p.entry_mode == "confirm":
            # CONTROL: market order sent after the distribution bar closes, so
            # the fill is the next bar's open -- never that bar's own close.
            j = d + 1
            if (j < n and a.dtag[j] == a.dtag[m]
                    and p.session_start <= a.hhmm[j] <= p.session_end):
                e, fill = j, float(a.o[j])
        else:
            # entry as specified: resting limit at the POC band
            lvl = round_to_tick(poc + side * p.poc_tol_atr * A)
            thru = p.require_through * TICK
            for j in range(d + 1, min(d + 1 + p.pb_bars, n)):
                if a.dtag[j] != a.dtag[m] or a.hhmm[j] > p.session_end:
                    break
                if (a.c[j] < invalid) if side > 0 else (a.c[j] > invalid):
                    break
                touched = ((a.l[j] <= lvl - thru) if side > 0
                           else (a.h[j] >= lvl + thru))
                if touched:
                    if a.hhmm[j] < p.session_start:
                        break
                    e = j
                    fill = min(a.o[j], lvl) if side > 0 else max(a.o[j], lvl)
                    break
        if e < 0:
            continue

        # stop from the sweep extreme, distance clamped in ATR
        raw = abs(fill - invalid) + p.stop_buf_atr * A
        sp = float(round_to_tick(np.clip(raw, p.min_stop_atr * A,
                                         p.max_stop_atr * A)))
        if sp <= 0.0:
            continue
        out.append((e, side, float(fill), sp, float(round_to_tick(sp * p.target_r)),
                    A, poc, float(acc_rng[m]), m, e - m,
                    e if p.entry_mode == "confirm" else e + 1))

    if not out:
        c = _empty(warm)
        _SCAN_CACHE[key] = (bars, c)
        return c

    out.sort(key=lambda r: r[0])
    cols = list(zip(*out))
    c = Cands(
        idx=np.asarray(cols[0], dtype=np.int64),
        side=np.asarray(cols[1], dtype=np.int64),
        entry_px=np.asarray(cols[2], dtype=np.float64),
        stop_pts=np.asarray(cols[3], dtype=np.float64),
        tgt_pts=np.asarray(cols[4], dtype=np.float64),
        atr=np.asarray(cols[5], dtype=np.float64),
        poc=np.asarray(cols[6], dtype=np.float64),
        acc_rng=np.asarray(cols[7], dtype=np.float64),
        sweep_idx=np.asarray(cols[8], dtype=np.int64),
        wait=np.asarray(cols[9], dtype=np.int64),
        warm=warm,
        manage_at=np.asarray(cols[10], dtype=np.int64),
    )
    _SCAN_CACHE[key] = (bars, c)
    return c


# ── trades ──────────────────────────────────────────────────────────────────
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
    poc: float
    acc_rng: float
    wait: int
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


@dataclass
class Result:
    trades: List[Trade] = field(default_factory=list)
    days: List[DayStat] = field(default_factory=list)


def simulate(bars: List[Bar], cands: Cands, p: Params,
             side_mode: str = "as_is") -> Result:
    """Walk the candidates one position at a time.

    Within a bar the ADVERSE extreme is assumed to happen first, so a bar that
    spans both stop and target books the stop. Conservative, and it is the only
    honest choice without tick data.
    """
    res = Result()
    a = arrays(bars)
    n = len(bars)
    # p.tick, not the module TICK: 0.25 is the NQ/ES tick and quantising a CL
    # stop (0.01) or a gold stop to it would round risk into $250 steps.
    slip = p.slip_ticks * getattr(p, "tick", TICK)
    day = -1
    ds: Optional[DayStat] = None
    closed_pl = 0.0
    consec = trades_today = 0
    halted = False
    ci, ncand = 0, len(cands.idx)

    while ci < ncand:
        i = int(cands.idx[ci])
        b = bars[i]

        if b.date_tag != day:
            if ds is not None:
                ds.pnl = closed_pl
            day = b.date_tag
            ds = DayStat(date_tag=day)
            res.days.append(ds)
            closed_pl = 0.0
            consec = trades_today = 0
            halted = False

        if halted:
            ci += 1
            continue
        if p.max_trades_day > 0 and trades_today >= p.max_trades_day:
            halted, ds.halt = True, "MAX_TRADES"
            ci += 1
            continue
        if p.max_consec_loss > 0 and consec >= p.max_consec_loss:
            halted, ds.halt = True, "CONSEC_LOSS"
            ci += 1
            continue

        side = int(cands.side[ci])
        if side_mode == "inverse":
            side = -side
        elif side_mode == "random":
            side = random.choice((1, -1))

        # entry price, stop distance and target are all fixed by scan(); the
        # null test flips only `side` around them
        entry = float(cands.entry_px[ci]) + side * slip
        sp = float(cands.stop_pts[ci])
        t = Trade(date_tag=b.date_tag, side=side, qty=p.qty, entry_hhmm=b.hhmm,
                  entry_px=entry, stop_px=entry - side * sp,
                  tp_px=entry + side * float(cands.tgt_pts[ci]),
                  stop_pts=sp, atr=float(cands.atr[ci]),
                  poc=float(cands.poc[ci]), acc_rng=float(cands.acc_rng[ci]),
                  wait=int(cands.wait[ci]))
        trades_today += 1
        dpp = p.pt_val * p.qty * side

        # see Cands.manage_at -- limit fills manage from i+1, open fills from i
        j = int(cands.manage_at[ci]) if cands.manage_at is not None else i + 1
        ex: Optional[Tuple[float, str]] = None
        while j < n:
            if a.dtag[j] != t.date_tag:
                ex = (a.c[j - 1], "EOD")
                j -= 1
                break
            hi, lo = a.h[j], a.l[j]
            adverse, favorable = (lo, hi) if side > 0 else (hi, lo)
            t.mae = min(t.mae, (adverse - entry) * side)
            t.mfe = max(t.mfe, (favorable - entry) * side)

            if (lo <= t.stop_px) if side > 0 else (hi >= t.stop_px):
                ex = (t.stop_px, "STOP")
                break
            if p.daily_loss > 0.0:
                pl_adv = closed_pl + (adverse - entry) * dpp
                if pl_adv <= -p.daily_loss:
                    px = entry + (-p.daily_loss - closed_pl) / dpp
                    ex = (min(max(px, lo), hi), "LOSS_LIMIT")
                    break
            if (hi >= t.tp_px) if side > 0 else (lo <= t.tp_px):
                ex = (t.tp_px, "TARGET")
                break
            if p.daily_target > 0.0:
                pl_fav = closed_pl + (favorable - entry) * dpp
                if pl_fav >= p.daily_target:
                    px = entry + (p.daily_target - closed_pl) / dpp
                    ex = (min(max(px, lo), hi), "DAILY_TARGET")
                    break
            if a.hhmm[j] >= p.flatten_hhmm:
                ex = (a.c[j], "FLAT_1555")
                break
            if p.max_hold_bars > 0 and (j - i) >= p.max_hold_bars:
                ex = (a.c[j], "TIME")
                break
            j += 1

        if ex is None:
            j = n - 1
            ex = (a.c[j], "EOD")

        t.exit_px = ex[0] - side * slip
        t.exit_hhmm = int(a.hhmm[j])
        t.reason = ex[1]
        t.pnl = (t.exit_px - entry) * side * p.pt_val * p.qty - p.commission * p.qty
        res.trades.append(t)
        ds.n += 1
        closed_pl += t.pnl
        if t.pnl < 0.0:
            consec += 1
        elif t.pnl > 0.0:
            consec = 0
        if ex[1] in ("LOSS_LIMIT", "DAILY_TARGET", "FLAT_1555"):
            halted, ds.halt = True, ex[1]

        while ci < ncand and cands.idx[ci] <= j:    # one position at a time
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
def summarize(r: Result, p: Optional[Params] = None) -> dict:
    """p is only needed for the avg_r column: R = pnl / (stop_pts * $/point),
    and hardcoding NQ's $20 made that column meaningless on crude and gold."""
    pv = p.pt_val if p is not None else 20.0
    pnls = np.array([t.pnl for t in r.trades])
    n = len(pnls)
    days = [d for d in r.days if d.n > 0]
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0, t=0.0, avg=0.0,
                    days=0, worst_day=0.0, best_day=0.0, reasons={}, longs=0,
                    avg_r=0.0)
    wins, loss = pnls[pnls > 0], pnls[pnls < 0]
    mean = pnls.mean()
    se = pnls.std(ddof=1) / np.sqrt(n) if n > 1 else 0.0
    run = np.cumsum(pnls)
    mdd = float((run - np.maximum.accumulate(np.r_[0.0, run])[1:]).min())
    dp = [d.pnl for d in days]
    reasons: Dict[str, int] = {}
    for t in r.trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    rs = [t.pnl / (t.stop_pts * pv) for t in r.trades if t.stop_pts > 0]
    return dict(
        n=n, total=float(pnls.sum()),
        pf=(float(wins.sum() / -loss.sum()) if len(loss) else float("inf")),
        wr=100.0 * len(wins) / n, max_dd=mdd,
        t=float(mean / se) if se > 0 else 0.0, avg=float(mean),
        days=len(days), worst_day=min(dp), best_day=max(dp),
        reasons=reasons, longs=sum(1 for t in r.trades if t.side > 0),
        avg_r=float(np.mean(rs)) if rs else 0.0,
    )


def write_csv(r: Result, path: Path):
    tot = 0.0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Event", "Date", "Time", "Side", "Qty", "Price", "Stop",
                    "Target", "StopPts", "ATR", "POC", "AccRng", "WaitBars",
                    "Reason", "PnL", "TotalPnL", "MAE", "MFE"])
        for t in r.trades:
            sd = "LONG" if t.side > 0 else "SHORT"
            w.writerow(["ENTRY", t.date_tag, t.entry_hhmm, sd, t.qty,
                        f"{t.entry_px:.2f}", f"{t.stop_px:.2f}",
                        f"{t.tp_px:.2f}", f"{t.stop_pts:.2f}", f"{t.atr:.2f}",
                        f"{t.poc:.2f}", f"{t.acc_rng:.2f}", t.wait, "", "",
                        f"{tot:.2f}", "", ""])
            tot += t.pnl
            w.writerow(["EXIT", t.date_tag, t.exit_hhmm, sd, t.qty,
                        f"{t.exit_px:.2f}", "", "", "", "", "", "", "",
                        t.reason, f"{t.pnl:.2f}", f"{tot:.2f}",
                        f"{t.mae:.2f}", f"{t.mfe:.2f}"])


def contracts_for(scope: str) -> Dict[str, Path]:
    env = os.environ.get("MZ_TAGS")
    everything = {**NQ_FROZEN, **MNQ, **ES_FROZEN}
    if env:
        return {t: everything[t] for t in env.split(",") if t in everything}
    if scope == "--nq":
        return dict(NQ_FROZEN)
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
        write_csv(r, BASE / f"IOF_mizan{suffix}_{tag}.csv")
    s = summarize(r)
    s["cands"] = len(c.idx)
    print(f"  {tag:7s} bars={len(bars):>7,} days={s['days']:>4} "
          f"cand={s['cands']:>4} n={s['n']:>4} "
          f"L/S={s['longs']}/{s['n']-s['longs']:<4} WR={s['wr']:>5.1f}% "
          f"PF={s['pf']:>5.2f} Net=${s['total']:>+9,.0f} "
          f"avg=${s['avg']:>+7,.0f} ({s['avg_r']:>+5.2f}R) "
          f"MaxDD=${s['max_dd']:>+9,.0f} t={s['t']:>+5.2f}")
    print(f"          exits: {s['reasons']}")
    return s


def main():
    args = sys.argv[1:]
    scope = next((a for a in args if a.startswith("--")), "--nq")
    p = params_from_env()

    print(f"Mizan_IOF_NQ -- accumulation -> manipulation -> distribution, "
          f"POC pullback  ({BAR_MINUTES}m)")
    print(f"  A: {p.acc_bars} bars, rng<={p.acc_max_atr}xATR, "
          f"drift<={p.acc_drift}xrng | "
          f"B: sweep>{p.sweep_eps_atr}xATR + reclaim, cpos>={p.sweep_close_pos}")
    entry_desc = (f"limit at POC{p.poc_tol_atr:+.2f}xATR, armed {p.pb_bars} "
                  f"bars, through {p.require_through:.0f}t"
                  if p.entry_mode == "poc" else
                  "CONTROL -- next bar's open after confirmation")
    print(f"  C: close {p.dist_min_atr}xATR clear of POC within {p.dist_bars} "
          f"bars | entry: {entry_desc}")
    print(f"  stop: sweep{p.stop_buf_atr:+.2f}xATR clamp[{p.min_stop_atr},"
          f"{p.max_stop_atr}]xATR  tgt={p.target_r}R  hold<={p.max_hold_bars} "
          f"bars | ${p.pt_val:.0f}/pt comm=${p.commission:.2f} "
          f"slip={p.slip_ticks}t qty={p.qty}\n")

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
              f"net>0: {pos}/{len(out)}  PF>1: {pf_ok}/{len(out)}")
        print(f"  SHIP GATE (net>0 AND PF>1 on every contract): "
              f"{'PASS' if pos == len(out) and pf_ok == len(out) else 'FAIL'}")
        print("  A gate pass is necessary, not sufficient -- run "
              "backtest_mizan_null.py before believing the direction call.")


if __name__ == "__main__":
    main()
