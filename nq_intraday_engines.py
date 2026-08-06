"""Two intraday NQ engines under one harness: MOM and MR.

Both are NQ-only, RTH-bounded and flat by the close, so their numbers sit on
the same axis as every A/B in the repo (5-min bars, $5 RT, slip=0 baseline,
one $800 stop, 09:35 -> 15:45 ET).

  MOM  Opening-range breakout continuation. The 09:30-10:00 range is measured,
       and the first bar to CLOSE outside it — with a conviction close and the
       fast EMA on the right side — is taken in the breakout direction. The OR
       width itself is a filter: too narrow is chop, too wide is a move that
       already happened, and both are SKIPPED rather than clamped.

  MR   Mean reversion against a rolling EMA anchor, measured in ATR units.
       Arms when |z| stretches past MR_Z_ENTRY, fires on the bar where z hooks
       back toward the anchor. A trend guard blocks fades while the fast/slow
       EMAs are strongly separated — fading a trend day is the classic way this
       family dies.

RELATION TO CLOSED THREADS — neither re-runs a falsified result, but the
overlaps are worth stating so nobody re-litigates them:
  * ORH/ORL were falsified as *M4 sweep levels* (0 better / 5 worse / 1 tied).
    That tested opening-range levels as sweep-and-reject targets inside M4. MOM
    trades the breakout continuation instead, which is the opposite sign and a
    standalone engine — a different claim, not a re-run.
  * The VWAP-touch engine is closed in all three flavours, so MR deliberately
    anchors on an EMA rather than VWAP. If you point MR at VWAP you are
    re-opening a dead thread.
  * The re-sign Monte-Carlo null (`--null`) is wired into both, because a
    trigger that only beats zero has not been shown to beat a coin toss under
    the same geometry.

STATUS as of 2026-08-05: BOTH ENGINES SCREEN AS NOISE. Ship gate MOM 2/6, MR
3/6, and neither separates from its own re-signed null:

    engine    n    as_is net    inverse    null mean   null sd   pctile
    mom     355       -6,900    -25,650      -15,457    12,094    76.0%
    mr      479      -12,192     +1,631         -492    15,265    22.0%

Nothing here reaches IOF_NQ_Autopilot.cpp before it clears the cross-contract
ship gate (net>0 AND PF>1 on all 6 frozen contracts) AND separates from that
null. A per-contract delta inside +/-$8k is noise, not an edge. Rebuild the
entry trigger if you want another shot at this — do not sweep the knobs until
a config passes.

Usage
  python nq_intraday_engines.py mom --all        # 6-contract ship gate
  python nq_intraday_engines.py mr  --all
  python nq_intraday_engines.py mom NQU26        # one contract + CSV
  python nq_intraday_engines.py --null 200       # re-sign null, both engines
  python nq_intraday_engines.py --null 200 mr    # ...one engine
  NQE_MR_Z_ENTRY=2.5 NQE_TAG=z25 python nq_intraday_engines.py mr --all
"""
import csv
import math
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import topdog_cycle as T
from topdog_cycle import (Bar, CONTRACTS, COMMISSION, PT_VAL, TICK,
                          atr_series, ema, load_bars_cached)

BASE = Path(__file__).parent

# ── session frame (identical to the live panel) ─────────────────────────────
SESS_START      = 935
LAST_ENTRY_HHMM = 1500    # R2b late-entry skip
FLAT_BY_HHMM    = 1545

MAX_TRADES_DAY  = 3
DAILY_LOSS      = 800.0   # one stop locks the day, as live
COOL_BARS       = 3
SLIP_TICKS      = 0.0     # per side; repo baseline is 0

# ── shared risk geometry ────────────────────────────────────────────────────
MIN_STOP_PTS    = 5.0
MAX_STOP_PTS    = 40.0    # one live stop == $800 == 40 NQ points
STOP_BUF_TICKS  = 4.0
TP_R            = 2.0     # 0 = no fixed target (trail only)
BE_R            = 0.0

TRAIL_MODE      = "none"  # none | ema | atr | swing
TRAIL_ARM_R     = 1.0
TRAIL_ATR_MULT  = 2.5
TRAIL_ATR_PER   = 14
TRAIL_SWING_LB  = 6

FAST_EMA        = 15      # shared trend reference
SLOW_EMA        = 50

# ── MOM ─────────────────────────────────────────────────────────────────────
MOM_OR_START    = 930     # opening range window [start, end)
MOM_OR_END      = 1000
# The OR-width filter is expressed in ATR units, NOT points. Measured on all 6
# frozen contracts, OR width in points spans 92-227 median as the index level
# goes 20k -> 30k, so any fixed point band silently rejects the typical day on
# half the sample — the same mis-scaling that broke the naive ES port. In ATR
# units the same distribution is flat (median 3.9-4.3, p10~3.0, p90~6.0 on
# every contract), so the band below trims only the degenerate tails and does
# not select. Set MOM_WIDTH_MODE="pts" to use the point band instead.
MOM_WIDTH_MODE  = "atr"
MOM_OR_MIN_ATR  = 2.5     # narrower than this is chop, not a range
MOM_OR_MAX_ATR  = 6.5     # wider than this and the day already moved
MOM_OR_MIN_PTS  = 20.0    # only used when MOM_WIDTH_MODE == "pts"
MOM_OR_MAX_PTS  = 400.0
MOM_CLOSE_FRAC  = 0.60    # close must sit in the top/bottom 60% of the bar
MOM_BUF_TICKS   = 2.0     # the break must clear the level by this much
MOM_ONE_PER_SIDE = 1      # 1 = at most one long and one short attempt per day

# ── MR ──────────────────────────────────────────────────────────────────────
MR_ANCHOR_PER   = 30      # EMA the fade reverts to
MR_ATR_PER      = 14      # z denominator
MR_Z_ENTRY      = 2.0     # stretch that arms the fade
MR_Z_MAX        = 5.0     # beyond this it is a news impulse, not an overshoot
MR_TREND_GUARD  = 1.5     # skip if |fastEMA-slowEMA| / ATR exceeds this
MR_SWING_LB     = 4       # bars used for the protective extreme
MR_TARGET       = "anchor"   # "anchor" = revert to the EMA, "r" = TP_R * risk

# ── per-run overrides: NQE_MR_Z_ENTRY=2.5 python nq_intraday_engines.py mr --all
RUN_TAG = os.environ.get("NQE_TAG", "")
for _name in ("SESS_START", "LAST_ENTRY_HHMM", "FLAT_BY_HHMM", "MAX_TRADES_DAY",
              "DAILY_LOSS", "COOL_BARS", "SLIP_TICKS", "MIN_STOP_PTS",
              "MAX_STOP_PTS", "STOP_BUF_TICKS", "TP_R", "BE_R", "TRAIL_ARM_R",
              "TRAIL_ATR_MULT", "TRAIL_ATR_PER", "TRAIL_SWING_LB", "FAST_EMA",
              "SLOW_EMA", "MOM_OR_START", "MOM_OR_END", "MOM_OR_MIN_PTS",
              "MOM_OR_MAX_PTS", "MOM_CLOSE_FRAC", "MOM_BUF_TICKS",
              "MOM_OR_MIN_ATR", "MOM_OR_MAX_ATR",
              "MOM_ONE_PER_SIDE", "MR_ANCHOR_PER", "MR_ATR_PER", "MR_Z_ENTRY",
              "MR_Z_MAX", "MR_TREND_GUARD", "MR_SWING_LB"):
    _env = os.environ.get("NQE_" + _name)
    if _env is not None:
        globals()[_name] = type(globals()[_name])(float(_env))
for _name in ("TRAIL_MODE", "MR_TARGET", "MOM_WIDTH_MODE"):
    _env = os.environ.get("NQE_" + _name)
    if _env is not None:
        globals()[_name] = _env


# ═════════════════════════════════════════════════════════════════════════════
#  TRADE RECORD
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class Trade:
    engine:     str
    date_tag:   int
    side:       int           # +1 long NQ, -1 short NQ
    entry_hhmm: int
    entry_px:   float
    stop_px:    float = 0.0
    tp_px:      float = 0.0
    init_stop:  float = 0.0   # frozen at entry — R is measured off this, not the trail
    peak:       float = 0.0   # running favourable extreme, for the trail
    trailed:    bool = False
    sig:        float = 0.0   # z at entry (MR); OR width in points (MOM)
    exit_hhmm:  int = 0
    exit_px:    float = 0.0
    reason:     str = ""
    pnl:        float = 0.0
    mae:        float = 0.0   # points
    mfe:        float = 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  INDICATOR PREP  (side-independent, so the null test computes it once)
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class Prep:
    bars:   List[Bar]
    fast:   List[float] = field(default_factory=list)
    slow:   List[float] = field(default_factory=list)
    atr:    List[float] = field(default_factory=list)
    anchor: List[float] = field(default_factory=list)
    z:      List[float] = field(default_factory=list)
    orh:    Dict[int, float] = field(default_factory=dict)   # date_tag -> OR high
    orl:    Dict[int, float] = field(default_factory=dict)
    or_atr: Dict[int, float] = field(default_factory=dict)   # ATR at the OR close


def prep_directional(bars: List[Bar]) -> Prep:
    """Everything MOM and MR read, computed once per contract."""
    closes = [b.close for b in bars]
    p = Prep(bars=bars)
    p.fast = ema(closes, int(FAST_EMA))
    p.slow = ema(closes, int(SLOW_EMA))
    p.atr = atr_series(bars, int(MR_ATR_PER))
    p.anchor = ema(closes, int(MR_ANCHOR_PER))

    # z is the MR stretch: how many ATRs price sits away from the anchor.
    p.z = [0.0] * len(bars)
    for i, b in enumerate(bars):
        a = p.atr[i]
        p.z[i] = 0.0 if a <= 0 else (b.close - p.anchor[i]) / a

    # Opening range per session, built only from bars strictly inside the OR
    # window — a day whose data starts late has no OR and trades nothing.
    # or_atr is the ATR as of the LAST bar of that window, so the width filter
    # is normalised by a value that is fully known before the first entry bar.
    for i, b in enumerate(bars):
        if MOM_OR_START <= b.hhmm < MOM_OR_END:
            d = b.date_tag
            p.orh[d] = max(p.orh.get(d, -1e18), b.high)
            p.orl[d] = min(p.orl.get(d, 1e18), b.low)
            p.or_atr[d] = p.atr[i]
    return p


# ═════════════════════════════════════════════════════════════════════════════
#  SIGNALS
# ═════════════════════════════════════════════════════════════════════════════
def signal_mom(p: Prep, i: int, fired: set) -> int:
    """First conviction close outside the opening range, in that direction."""
    b = p.bars[i]
    if b.hhmm < MOM_OR_END:
        return 0
    hi = p.orh.get(b.date_tag)
    lo = p.orl.get(b.date_tag)
    if hi is None or lo is None:
        return 0

    width = hi - lo
    if MOM_WIDTH_MODE == "atr":
        a = p.or_atr.get(b.date_tag, 0.0)
        if a <= 0.0 or not (MOM_OR_MIN_ATR <= width / a <= MOM_OR_MAX_ATR):
            return 0
    elif not (MOM_OR_MIN_PTS <= width <= MOM_OR_MAX_PTS):
        return 0

    rng = b.high - b.low
    if rng <= 0.0:
        return 0
    # conviction: the bar closed near the end it broke through
    up_conv = (b.close - b.low) / rng >= MOM_CLOSE_FRAC
    dn_conv = (b.high - b.close) / rng >= MOM_CLOSE_FRAC
    buf = MOM_BUF_TICKS * TICK

    if (b.close > hi + buf and up_conv and p.fast[i] > p.slow[i]
            and not (MOM_ONE_PER_SIDE and +1 in fired)):
        return +1
    if (b.close < lo - buf and dn_conv and p.fast[i] < p.slow[i]
            and not (MOM_ONE_PER_SIDE and -1 in fired)):
        return -1
    return 0


def signal_mr(p: Prep, i: int) -> int:
    """Fade a stretch once it hooks back toward the anchor."""
    z, zp, zpp = p.z[i], p.z[i - 1], p.z[i - 2]
    a = p.atr[i]
    if a <= 0.0:
        return 0
    # A wide fast/slow separation means trend, and fading a trend day is how
    # this family normally dies.
    if abs(p.fast[i] - p.slow[i]) / a > MR_TREND_GUARD:
        return 0

    if MR_Z_ENTRY <= zp <= MR_Z_MAX and z < zp and zpp <= zp:
        return -1                       # stretched up, now turning down
    if -MR_Z_MAX <= zp <= -MR_Z_ENTRY and z > zp and zpp >= zp:
        return +1                       # stretched down, now turning up
    return 0


# ═════════════════════════════════════════════════════════════════════════════
#  SINGLE-LEG ENGINE  (MOM, MR)
# ═════════════════════════════════════════════════════════════════════════════
def _geometry(p: Prep, i: int, side: int, engine: str
              ) -> Optional[Tuple[float, float]]:
    """(risk_pts, reward_pts) for a trade signalled `side` at bar i, or None.

    Distances, not levels, so the re-sign null can mirror the exact same
    geometry onto the opposite direction (see run_directional). Risk outside
    [MIN_STOP_PTS, MAX_STOP_PTS] SKIPS the trade rather than clamping it:
    clamping silently changes the geometry the signal was conditioned on.
    """
    b = p.bars[i]
    buf = STOP_BUF_TICKS * TICK

    if engine == "mom":
        # the breakout bar's own opposite extreme — if price comes back through
        # it, the break failed
        stop = (b.low - buf) if side > 0 else (b.high + buf)
    else:
        lb = int(MR_SWING_LB)
        win = p.bars[max(0, i - lb + 1): i + 1]
        stop = (min(s.low for s in win) - buf if side > 0
                else max(s.high for s in win) + buf)

    risk = abs(b.close - stop)
    if not (MIN_STOP_PTS <= risk <= MAX_STOP_PTS):
        return None

    if engine == "mr" and MR_TARGET == "anchor":
        reward = (p.anchor[i] - b.close) * side     # distance back to the EMA
        if reward <= 0.0:
            return None                             # anchor already crossed
    else:
        reward = TP_R * risk if TP_R > 0 else 0.0
    return risk, reward


def run_directional(p: Prep, engine: str, side_mode: str = "as_is") -> List[Trade]:
    """MOM/MR entry plus the shared exit manager.

    side_mode re-signs the SAME trigger without touching its timestamps or its
    risk/reward distances — "random" coin-tosses the direction, "inverse" flips
    it. Any directional information the trigger holds has to show up as a gap
    between as_is and those.
    """
    bars = p.bars
    warm = max(int(SLOW_EMA), int(MR_ANCHOR_PER), int(MR_ATR_PER),
               int(MR_SWING_LB), int(TRAIL_SWING_LB)) + 2
    if len(bars) <= warm:
        return []

    trades: List[Trade] = []
    open_t: Optional[Trade] = None
    day = None
    day_pnl = 0.0
    day_n = 0
    locked = False
    last_sig = -10 ** 9
    fired: set = set()
    slip = SLIP_TICKS * TICK

    for i in range(warm, len(bars)):
        b = bars[i]

        if b.date_tag != day:
            if open_t is not None:                  # never straddle sessions
                _close(open_t, bars[i - 1], bars[i - 1].close, "EOD", trades, slip)
                open_t = None
            day, day_pnl, day_n, locked = b.date_tag, 0.0, 0, False
            fired = set()

        # ── manage the open position first. A bar that touches both sides is
        #    scored as a stop — the conservative read of a 5-min bar. ─────────
        if open_t is not None:
            t = open_t
            ex = None
            hit = "TRAIL" if t.trailed else "STOP"
            if t.side > 0:
                t.mae = min(t.mae, b.low - t.entry_px)
                t.mfe = max(t.mfe, b.high - t.entry_px)
                t.peak = max(t.peak, b.high)
                if b.low <= t.stop_px:
                    ex = (t.stop_px, hit)
                elif t.tp_px > 0.0 and b.high >= t.tp_px:
                    ex = (t.tp_px, "TARGET")
            else:
                t.mae = min(t.mae, t.entry_px - b.high)
                t.mfe = max(t.mfe, t.entry_px - b.low)
                t.peak = min(t.peak, b.low)
                if b.high >= t.stop_px:
                    ex = (t.stop_px, hit)
                elif t.tp_px > 0.0 and b.low <= t.tp_px:
                    ex = (t.tp_px, "TARGET")

            if ex is None and b.hhmm >= FLAT_BY_HHMM:
                ex = (b.close, "FLAT_BY")

            if ex is not None:
                day_pnl += _close(t, b, ex[0], ex[1], trades, slip)
                open_t = None
                day_n += 1
                if DAILY_LOSS > 0 and day_pnl <= -DAILY_LOSS:
                    locked = True
            else:
                _update_stop(t, p, i)
                continue

        # ── entry scan ──────────────────────────────────────────────────────
        if open_t is not None or locked:
            continue
        if not (SESS_START <= b.hhmm <= LAST_ENTRY_HHMM):
            continue
        if day_n >= MAX_TRADES_DAY or i - last_sig < COOL_BARS:
            continue

        sig_side = signal_mom(p, i, fired) if engine == "mom" else signal_mr(p, i)
        if sig_side == 0:
            continue

        last_sig = i

        # Geometry is measured off the SIGNAL side, then mirrored onto whatever
        # side is actually traded. That keeps the null's stop and target the
        # same distances as the real trade instead of re-deriving a swing that
        # happens to be tighter on one side.
        g = _geometry(p, i, sig_side, engine)
        if g is None:
            continue        # untradeable geometry does not burn the day's
                            # one-per-side attempt — only a real fill does
        risk, reward = g
        fired.add(sig_side)

        side = sig_side
        if side_mode == "random":
            side = +1 if random.random() < 0.5 else -1
        elif side_mode == "inverse":
            side = -side

        entry = b.close + side * slip
        stop = entry - side * risk
        open_t = Trade(engine=engine, date_tag=b.date_tag, side=side,
                       entry_hhmm=b.hhmm, entry_px=entry, stop_px=stop,
                       tp_px=(entry + side * reward) if reward > 0.0 else 0.0,
                       init_stop=stop, peak=entry,
                       sig=(p.orh[b.date_tag] - p.orl[b.date_tag]
                            if engine == "mom" else p.z[i]))

    if open_t is not None:
        _close(open_t, bars[-1], bars[-1].close, "EOD", trades, slip)
    return trades


def _update_stop(t: Trade, p: Prep, i: int) -> None:
    """Break-even and trail, driven off bar i's CLOSE.

    The stop check for bar i has already run above, so a stop moved here can
    only affect bar i+1 onward. Monotonic — the trail never loosens — and held
    a tick away from price so it cannot fill instantly.
    """
    b = p.bars[i]
    risk = abs(t.entry_px - t.init_stop)
    if risk <= 0.0:
        return
    run_pts = (b.close - t.entry_px) * t.side

    if BE_R > 0 and run_pts >= BE_R * risk:
        t.stop_px = (max(t.stop_px, t.entry_px) if t.side > 0
                     else min(t.stop_px, t.entry_px))

    if TRAIL_MODE == "none" or run_pts < TRAIL_ARM_R * risk:
        return

    cand = None
    if TRAIL_MODE == "ema":
        cand = p.fast[i]
    elif TRAIL_MODE == "atr":
        cand = (t.peak - TRAIL_ATR_MULT * p.atr[i] if t.side > 0
                else t.peak + TRAIL_ATR_MULT * p.atr[i])
    elif TRAIL_MODE == "swing":
        lb = int(TRAIL_SWING_LB)
        win = p.bars[max(0, i - lb + 1): i + 1]
        cand = (min(s.low for s in win) - STOP_BUF_TICKS * TICK if t.side > 0
                else max(s.high for s in win) + STOP_BUF_TICKS * TICK)
    if cand is None:
        return

    new = max(t.stop_px, cand) if t.side > 0 else min(t.stop_px, cand)
    new = min(new, b.close - TICK) if t.side > 0 else max(new, b.close + TICK)
    if (new > t.stop_px) if t.side > 0 else (new < t.stop_px):
        t.stop_px = new
        t.trailed = True


def _close(t: Trade, bar: Bar, px: float, reason: str,
           trades: List[Trade], slip: float = 0.0) -> float:
    t.exit_px = px - t.side * slip
    t.exit_hhmm = bar.hhmm
    t.reason = reason
    t.pnl = (t.exit_px - t.entry_px) * t.side * PT_VAL - COMMISSION
    trades.append(t)
    return t.pnl


# ═════════════════════════════════════════════════════════════════════════════
#  DATA
# ═════════════════════════════════════════════════════════════════════════════
def bars_for(tag: str, rebuild: bool = False) -> List[Bar]:
    """5-min NQ bars. Shares topdog_cycle's .npz cache — same builder, same
    timeframe, so a cache built by either script serves both."""
    return load_bars_cached(tag, CONTRACTS[tag], rebuild)


_PREP_CACHE: Dict[Tuple[str, str], Optional[Prep]] = {}


def prep_for(engine: str, tag: str) -> Optional[Prep]:
    """Indicators are side-independent, so the null test reuses them across
    every Monte-Carlo draw instead of recomputing per draw."""
    key = (engine, tag)
    if key in _PREP_CACHE:
        return _PREP_CACHE[key]

    p = prep_directional(bars_for(tag)) if CONTRACTS[tag].exists() else None
    _PREP_CACHE[key] = p
    return p


# ═════════════════════════════════════════════════════════════════════════════
#  STATS / REPORTING
# ═════════════════════════════════════════════════════════════════════════════
def stats(pnls: List[float]) -> dict:
    n = len(pnls)
    if n == 0:
        return dict(n=0, net=0.0, pf=0.0, wr=0.0, max_dd=0.0, t=0.0, pnls=[])
    w = [x for x in pnls if x > 0]
    L = [x for x in pnls if x < 0]
    m = sum(pnls) / n
    var = sum((x - m) ** 2 for x in pnls) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if var > 0 else 0.0
    peak = run = mdd = 0.0
    for x in pnls:
        run += x
        peak = max(peak, run)
        mdd = min(mdd, run - peak)
    return dict(n=n, net=sum(pnls), wr=100.0 * len(w) / n,
                pf=(sum(w) / abs(sum(L))) if L else float("inf"),
                max_dd=mdd, t=(m / se) if se > 0 else 0.0, pnls=pnls)


def write_csv(trades: List[Trade], path: str) -> None:
    tot = 0.0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Event", "Date", "Time", "Side", "Price", "Stop", "Target",
                    "Signal", "Reason", "PnL", "TotalPnL", "MAE", "MFE"])
        for t in trades:
            sd = "LONG" if t.side > 0 else "SHORT"
            w.writerow(["ENTRY", t.date_tag, t.entry_hhmm, sd,
                        f"{t.entry_px:.2f}",
                        f"{t.init_stop:.2f}" if t.init_stop else "-",
                        f"{t.tp_px:.2f}" if t.tp_px else "-",
                        f"{t.sig:.2f}", "", "", f"{tot:.2f}", "", ""])
            tot += t.pnl
            w.writerow(["EXIT", t.date_tag, t.exit_hhmm, sd,
                        f"{t.exit_px:.2f}", "", "", "",
                        t.reason, f"{t.pnl:.2f}", f"{tot:.2f}",
                        f"{t.mae:.2f}", f"{t.mfe:.2f}"])


def header(engine: str) -> str:
    if engine == "mom":
        wid = (f"width[{MOM_OR_MIN_ATR:.1f},{MOM_OR_MAX_ATR:.1f}]xATR"
               if MOM_WIDTH_MODE == "atr" else
               f"width[{MOM_OR_MIN_PTS:.0f},{MOM_OR_MAX_PTS:.0f}]pt")
        cfg = (f"OR {int(MOM_OR_START)}-{int(MOM_OR_END)} {wid}, "
               f"close-frac {MOM_CLOSE_FRAC:.2f}, EMA{int(FAST_EMA)}/{int(SLOW_EMA)}")
        risk = (f"stop=breakout bar +{STOP_BUF_TICKS:g}t, clamp"
                f"[{MIN_STOP_PTS:.0f},{MAX_STOP_PTS:.0f}]pt=SKIP, "
                + (f"{TP_R:.1f}R" if TP_R > 0 else "no target")
                + (f", trail={TRAIL_MODE}" if TRAIL_MODE != "none" else ""))
    else:
        cfg = (f"anchor EMA{int(MR_ANCHOR_PER)}, z[{MR_Z_ENTRY:.1f},{MR_Z_MAX:.1f}] "
               f"in ATR{int(MR_ATR_PER)}, trend-guard {MR_TREND_GUARD:.1f}")
        risk = (f"stop={int(MR_SWING_LB)}-bar swing +{STOP_BUF_TICKS:g}t, clamp"
                f"[{MIN_STOP_PTS:.0f},{MAX_STOP_PTS:.0f}]pt=SKIP, target="
                + ("anchor" if MR_TARGET == "anchor" else f"{TP_R:.1f}R")
                + (f", trail={TRAIL_MODE}" if TRAIL_MODE != "none" else ""))
    return (f"{engine.upper()} — {int(T.BAR_MINUTES)}m bars, {cfg}\n"
            f"       {risk}\n"
            f"       {int(SESS_START)}-{int(LAST_ENTRY_HHMM)} entries, flat "
            f"{int(FLAT_BY_HHMM)}, MT={int(MAX_TRADES_DAY)}, "
            f"DL=${DAILY_LOSS:.0f}, cool={int(COOL_BARS)}b, "
            f"slip={SLIP_TICKS:g}t, ${COMMISSION:.0f} RT")


def run_one(engine: str, tag: str, out: Optional[str] = None) -> Optional[dict]:
    p = prep_for(engine, tag)
    if p is None or not p.bars:
        print(f"  {tag:8s} MISSING data")
        return None
    trades = run_directional(p, engine)
    if out:
        write_csv(trades, out)
    s = stats([t.pnl for t in trades])
    by: Dict[str, int] = {}
    for t in trades:
        by[t.reason] = by.get(t.reason, 0) + 1
    print(f"  {tag:8s} bars={len(p.bars):>7,}  n={s['n']:>4}  WR={s['wr']:>5.1f}%  "
          f"PF={s['pf']:>5.2f}  Net={s['net']:>+10,.0f}  "
          f"MaxDD={s['max_dd']:>9,.0f}  t={s['t']:>+5.2f}  {by}")
    return s


def run_all(engine: str) -> None:
    print(header(engine))
    res = {}
    for tag in CONTRACTS:
        suffix = f"_{RUN_TAG}" if RUN_TAG else ""
        s = run_one(engine, tag, str(BASE / f"IOF_{engine}{suffix}_{tag}.csv"))
        if s is not None:
            res[tag] = s
    if not res:
        print("  no contracts ran")
        return

    pooled = [x for r in res.values() for x in r["pnls"]]
    ps = stats(pooled)
    npos = sum(1 for r in res.values() if r["net"] > 0 and r["pf"] > 1.0)
    print("-" * 108)
    print(f"  POOLED  n={ps['n']}  PF={ps['pf']:.2f}  Net={ps['net']:+,.0f}  "
          f"MaxDD={ps['max_dd']:+,.0f}  t={ps['t']:+.2f}")
    print(f"  contracts net>0 & PF>1: {npos}/{len(res)}  "
          f"-> {'PASS' if npos == len(res) else 'FAIL'} (ship gate)")
    print("  Reminder: a per-contract delta inside +/-$8k is noise. Run --null "
          "before believing this.")


# ═════════════════════════════════════════════════════════════════════════════
#  RE-SIGN MONTE-CARLO NULL
# ═════════════════════════════════════════════════════════════════════════════
def null_test(engines: List[str], draws: int) -> None:
    """Hold entry times, exits and risk/reward distances fixed; randomise only
    the SIDE.

    This separates "the geometry made money" from "the signal picked the side".
    A percentile near 50 means the trigger knows nothing about direction, no
    matter how good the raw net looks.
    """
    print(f"  {draws} random-sign draws per engine, entries/stops/targets held "
          f"fixed, slip={SLIP_TICKS:g}t\n")
    print(f"  {'engine':8s} {'n':>5s} {'as_is net':>12s} {'inverse':>12s} "
          f"{'null mean':>12s} {'null sd':>11s} {'pctile':>7s}  read")
    print("  " + "-" * 96)

    for e in engines:
        preps = [p for p in (prep_for(e, t) for t in CONTRACTS)
                 if p is not None and p.bars]
        if not preps:
            print(f"  {e:8s} MISSING data")
            continue

        def pooled(mode: str) -> List[float]:
            return [t.pnl for p in preps for t in run_directional(p, e, mode)]

        random.seed(1)
        real = pooled("as_is")
        inv = pooled("inverse")
        if not real:
            print(f"  {e:8s} {'0':>5}  no trades")
            continue

        nulls = []
        for s in range(draws):
            random.seed(10_000 + s)
            nulls.append(sum(pooled("random")))
        nulls.sort()
        mean = sum(nulls) / len(nulls)
        sd = (sum((x - mean) ** 2 for x in nulls) / (len(nulls) - 1)) ** 0.5
        net = sum(real)
        pct = 100.0 * sum(1 for x in nulls if x < net) / len(nulls)
        read = ("edge" if pct >= 95 else
                "anti-edge" if pct <= 5 else "INDISTINGUISHABLE FROM NOISE")
        print(f"  {e:8s} {len(real):>5} {net:>+12,.0f} {sum(inv):>+12,.0f} "
              f"{mean:>+12,.0f} {sd:>11,.0f} {pct:>6.1f}%  {read}")


# ═════════════════════════════════════════════════════════════════════════════
ENGINES = ["mom", "mr"]


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)

    if args[0] == "--null":
        draws = int(args[1]) if len(args) > 1 and args[1].isdigit() else 200
        want = [a for a in args[1:] if a in ENGINES]
        null_test(want or ENGINES, draws)
        return

    engine = args[0].lower()
    if engine not in ENGINES:
        print(f"unknown engine {engine!r}; expected one of {ENGINES}")
        sys.exit(2)

    rest = args[1:]
    if rest and rest[0] == "--all":
        run_all(engine)
        return
    if not rest:
        print(f"usage: python {Path(__file__).name} {engine} "
              f"[--all | <TAG> [out.csv]]\n  TAGs: {list(CONTRACTS)}")
        sys.exit(2)

    tag = rest[0]
    if tag not in CONTRACTS:
        print(f"unknown contract {tag!r}; expected one of {list(CONTRACTS)}")
        sys.exit(2)
    out = rest[1] if len(rest) > 1 else str(BASE / f"IOF_{engine}_{tag}.csv")
    print(header(engine))
    run_one(engine, tag, out)
    print(f"  -> {out}")


if __name__ == "__main__":
    main()
