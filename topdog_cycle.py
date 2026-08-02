"""TopDog "Cycle" strategy — trend-filtered stochastic-hook entries.

Transcribed from two reference screenshots:

  TopDogStochasticIndicator.png  — Stochastic Full, %K=5, %D=3, Smoothing=2,
                                   bands at 20 / 80 ("Cycle Indicator").
  ZAN.png                        — 5-min chart, two MAs (fast black / slow
                                   orange). Long arrow fires where the fast MA
                                   turns up through the slow MA *and* the thick
                                   %D line hooks up out of the sub-20 zone
                                   (the red dot on the indicator panel).

RULES (long; short is the exact mirror)
  1. TREND   fast EMA > slow EMA, and the slow EMA is rising over SLOPE_LB bars.
  2. CYCLE   %D printed below OS(20) at some point in the last CYCLE_LB bars,
             has not yet recovered past MAX_ENTRY_D(50), and hooks up on this
             bar:  D[i] > D[i-1] and D[i-1] <= D[i-2].
  3. ENTRY   market on the close of the hook bar.
  4. STOP    swing low of the last SWING_LB bars minus STOP_BUF_TICKS. Trades
             whose resulting stop is outside [MIN_STOP_PTS, MAX_STOP_PTS] are
             SKIPPED, not clamped — clamping changes the geometry that the
             signal is conditioned on.
  5. TARGET  TP_R x risk. Optional break-even at BE_R, optional fast-EMA trail.
  6. FLAT    all positions closed at FLAT_BY_HHMM; no new entries after
             LAST_ENTRY_HHMM (matches the live R2b late-entry skip).

MA lengths are not legible in ZAN.png; they were supplied directly: fast = EMA
15 (black), slow = SMA 50 (orange), both on the 5-min chart.

NOTE ON STATUS: this is an unvalidated exploratory engine. Nothing here should
reach IOF_NQ_Autopilot.cpp before it clears the cross-contract ship gate
(net>0 AND PF>1 on all 6 frozen contracts) via `--all`, and even then a
per-contract delta inside +/-$8k is noise, not an edge.

Usage
  python topdog_cycle.py F.US.ENQU26.scid out.csv     # one contract
  python topdog_cycle.py --all                        # 6-contract ship gate
"""
import csv
import os
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np

from backtest import (ET, SC_EPOCH_UTC, Bar, detect_price_scale, read_scid,
                      COMMISSION, PT_VAL, TICK)

BASE = Path(__file__).parent

# ── frozen snapshots (never the live dir — Sierra rewrites those mid-run) ────
CONTRACTS = {
    "NQU25": BASE / "F.US.ENQU25.scid",   # Sep-2025
    "NQZ25": BASE / "F.US.ENQZ25.scid",   # Dec-2025
    "NQM5":  BASE / "F.US.ENQM25.scid",   # Jun-2025
    "NQH6":  BASE / "F.US.ENQH26.scid",   # Mar-2026
    "NQM6":  BASE / "F.US.ENQM26.scid",   # Jun-2026
    "NQU26": BASE / "F.US.ENQU26.scid",   # Sep-2026 (LIVE)
}

# ── config ──────────────────────────────────────────────────────────────────
BAR_MINUTES     = 5      # the screenshots are 5-min charts

FAST_EMA        = 15     # black line  — EMA 15
SLOW_SMA        = 50     # orange line — SMA 50 (simple, not exponential)
SLOPE_LB        = 5      # bars used for the slow-MA slope check

STOCH_K         = 5      # PeriodK
STOCH_SMOOTH    = 2      # Smooth
STOCH_D         = 3      # PeriodD
OS_LEVEL        = 20.0   # Lower band
OB_LEVEL        = 80.0   # Upper band
CYCLE_LB        = 8      # bars back in which %D must have visited the band
MAX_ENTRY_D     = 50.0   # don't chase a cycle that already recovered past mid

SWING_LB        = 6      # bars used for the protective swing
STOP_BUF_TICKS  = 4.0
MIN_STOP_PTS    = 5.0
MAX_STOP_PTS    = 40.0   # one live stop == $800 == 40 NQ points

TP_R            = 2.0    # target as a multiple of risk; 0 = NO fixed target
BE_R            = 0.0    # >0 moves the stop to break-even at this R

# Trailing exit. TRAIL_MODE != "none" is the alternative to a fixed target:
#   "ema"    stop rides the fast EMA (the black line) — the TopDog "stay with it
#            until it closes back through the MA" exit
#   "atr"    Chandelier: stop = running extreme -/+ TRAIL_ATR_MULT * ATR
#   "swing"  stop rides the most recent TRAIL_SWING_LB-bar swing low/high
# The trail arms only after price has run TRAIL_ARM_R in favour; before that the
# original swing stop holds. The trail is monotonic — it never loosens.
TRAIL_MODE      = "none"
TRAIL_ARM_R     = 1.0
TRAIL_ATR_MULT  = 2.5
TRAIL_ATR_PER   = 14
TRAIL_SWING_LB  = 6

SESS_START      = 935    # RTH open used by the live system
LAST_ENTRY_HHMM = 1500   # R2b late-entry skip
FLAT_BY_HHMM    = 1545

COOL_BARS       = 3      # bars to wait after a signal before another can arm
MAX_TRADES_DAY  = 3
DAILY_LOSS      = 800.0  # one stop locks the day, as live
SLIP_TICKS      = 0.0    # per side; raise to stress-test (repo baseline is 0)

# Per-run overrides so variants don't need a file edit and don't clobber each
# other's CSVs:  TOPDOG_TP_R=1.0 TOPDOG_TAG=tp1r python topdog_cycle.py --all
RUN_TAG = os.environ.get("TOPDOG_TAG", "")
for _name in ("FAST_EMA", "SLOW_SMA", "SLOPE_LB", "CYCLE_LB", "MAX_ENTRY_D",
              "SWING_LB", "STOP_BUF_TICKS", "MIN_STOP_PTS", "MAX_STOP_PTS",
              "TP_R", "BE_R", "COOL_BARS", "MAX_TRADES_DAY", "DAILY_LOSS",
              "SLIP_TICKS", "BAR_MINUTES", "TRAIL_ARM_R", "TRAIL_ATR_MULT",
              "TRAIL_ATR_PER", "TRAIL_SWING_LB"):
    _env = os.environ.get("TOPDOG_" + _name)
    if _env is not None:
        globals()[_name] = type(globals()[_name])(float(_env))
if os.environ.get("TOPDOG_TRAIL_MODE"):
    TRAIL_MODE = os.environ["TOPDOG_TRAIL_MODE"]


# ── time bars ───────────────────────────────────────────────────────────────
def build_time_bars(recs: np.ndarray, minutes: int = BAR_MINUTES,
                    price_scale: float = 1.0) -> List[Bar]:
    """Aggregate .scid ticks into fixed-clock bars stamped in US/Eastern.

    Mirrors build_volume_bars' OHL fallback: Sierra emits 0 / non-finite OHL on
    ask-side single-tick records, and min(low, 0) silently corrupts every
    range-derived series downstream.
    """
    bars: List[Bar] = []
    bucket = None
    o = h = l = c = 0.0
    vol = bid = ask = 0
    bar_dt = None

    def flush():
        nonlocal bars
        bars.append(Bar(
            dt=bar_dt, open=o, high=h, low=l, close=c,
            volume=vol, bid_vol=bid, ask_vol=ask, idx=len(bars),
            date_tag=bar_dt.year * 10000 + bar_dt.month * 100 + bar_dt.day,
            hhmm=bar_dt.hour * 100 + bar_dt.minute,
        ))

    for rec in recs:
        tv = int(rec["tot_vol"])
        if tv == 0:
            continue
        dt = (SC_EPOCH_UTC + timedelta(microseconds=int(rec["dt"]))).astimezone(ET)
        key = (dt.date(), (dt.hour * 60 + dt.minute) // minutes)

        rc = float(rec["close"]) / price_scale
        ro = float(rec["open"]) / price_scale
        rh = float(rec["high"]) / price_scale
        rl = float(rec["low"]) / price_scale
        if not np.isfinite(ro) or ro == 0.0:
            ro = rc
        if not np.isfinite(rh) or rh == 0.0:
            rh = rc
        if not np.isfinite(rl) or rl == 0.0:
            rl = rc

        if key != bucket:
            if bucket is not None:
                flush()
            bucket = key
            bar_dt = dt.replace(second=0, microsecond=0,
                                minute=(dt.minute // minutes) * minutes)
            o, h, l, c = ro, rh, rl, rc
            vol, bid, ask = 0, 0, 0

        h = max(h, rh)
        l = min(l, rl)
        c = rc
        vol += tv
        bid += int(rec["bid_vol"])
        ask += int(rec["ask_vol"])

    if bucket is not None:
        flush()
    return bars


# ── indicators ──────────────────────────────────────────────────────────────
def ema(vals: List[float], period: int) -> List[float]:
    k = 2.0 / (period + 1.0)
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else prev + k * (v - prev)
        out.append(prev)
    return out


def sma(vals: List[float], period: int) -> List[float]:
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= period:
            run -= vals[i - period]
        out.append(run / min(i + 1, period))
    return out


def stochastic_full(bars: List[Bar], k_per: int, smooth: int, d_per: int):
    """Stochastic Full with NinjaTrader semantics (the screenshot's indicator):
       rawK = 100*(C-LL(k))/(HH(k)-LL(k));  K = SMA(rawK, smooth);  D = SMA(K, d)
    """
    n = len(bars)
    raw = [50.0] * n
    for i in range(n):
        lo = min(b.low for b in bars[max(0, i - k_per + 1): i + 1])
        hi = max(b.high for b in bars[max(0, i - k_per + 1): i + 1])
        raw[i] = 50.0 if hi <= lo else 100.0 * (bars[i].close - lo) / (hi - lo)

    k = sma(raw, smooth)
    return k, sma(k, d_per)


def atr_series(bars: List[Bar], period: int) -> List[float]:
    """Wilder ATR on the 5-min bars (used by the Chandelier trail)."""
    out, prev_c, run = [], None, 0.0
    for i, b in enumerate(bars):
        tr = (b.high - b.low) if prev_c is None else max(
            b.high - b.low, abs(b.high - prev_c), abs(b.low - prev_c))
        run = tr if i == 0 else (run * (period - 1) + tr) / period
        prev_c = b.close
        out.append(run)
    return out


# ── trade record ────────────────────────────────────────────────────────────
@dataclass
class Trade:
    date_tag: int
    side: int                 # +1 long, -1 short
    entry_hhmm: int
    entry_px: float
    stop_px: float
    tp_px: float
    init_stop: float = 0.0    # frozen at entry — R is measured off this, not the trail
    peak: float = 0.0         # running favourable extreme, for the trail
    trailed: bool = False     # has the trail ever moved the stop?
    exit_hhmm: int = 0
    exit_px: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0
    stoch_d: float = 0.0


# ── engine ──────────────────────────────────────────────────────────────────
def run_engine(bars: List[Bar]) -> List[Trade]:
    if len(bars) < max(SLOW_SMA, CYCLE_LB, SWING_LB) + 5:
        return []

    closes = [b.close for b in bars]
    fast = ema(closes, FAST_EMA)
    slow = sma(closes, SLOW_SMA)
    _k, d = stochastic_full(bars, STOCH_K, STOCH_SMOOTH, STOCH_D)
    atr = atr_series(bars, TRAIL_ATR_PER) if TRAIL_MODE == "atr" else None

    trades: List[Trade] = []
    open_t: Optional[Trade] = None
    day = None
    day_pnl = 0.0
    day_n = 0
    day_locked = False
    last_sig = -10 ** 9
    slip = SLIP_TICKS * TICK

    warm = max(SLOW_SMA, STOCH_K + STOCH_SMOOTH + STOCH_D, CYCLE_LB, SWING_LB, SLOPE_LB) + 1

    for i in range(warm, len(bars)):
        b = bars[i]

        if b.date_tag != day:
            # a position may not straddle the session boundary
            if open_t is not None:
                _close(open_t, bars[i - 1], bars[i - 1].close, "EOD", trades)
                open_t = None
            day, day_pnl, day_n, day_locked = b.date_tag, 0.0, 0, False

        # ── manage the open position first (stop is assumed to win a bar that
        #    touches both sides — the conservative read of a 5-min bar) ──────
        if open_t is not None:
            t = open_t
            ex = None
            stop_hit = "TRAIL" if t.trailed else "STOP"
            if t.side > 0:
                t.mae = min(t.mae, b.low - t.entry_px)
                t.mfe = max(t.mfe, b.high - t.entry_px)
                t.peak = max(t.peak, b.high)
                if b.low <= t.stop_px:
                    ex = (t.stop_px, stop_hit)
                elif TP_R > 0 and b.high >= t.tp_px:
                    ex = (t.tp_px, "TARGET")
            else:
                t.mae = min(t.mae, t.entry_px - b.high)
                t.mfe = max(t.mfe, t.entry_px - b.low)
                t.peak = min(t.peak, b.low)
                if b.high >= t.stop_px:
                    ex = (t.stop_px, stop_hit)
                elif TP_R > 0 and b.low <= t.tp_px:
                    ex = (t.tp_px, "TARGET")

            if ex is None and b.hhmm >= FLAT_BY_HHMM:
                ex = (b.close, "FLAT_BY")

            if ex is not None:
                day_pnl += _close(t, b, ex[0], ex[1], trades, slip)
                open_t = None
                day_n += 1
                if DAILY_LOSS > 0 and day_pnl <= -DAILY_LOSS:
                    day_locked = True
            else:
                # Trail/BE update uses bar i's CLOSE and applies from bar i+1 —
                # the stop check above already ran, so there is no look-ahead.
                risk = abs(t.entry_px - t.init_stop)
                run_pts = (b.close - t.entry_px) * t.side
                if BE_R > 0 and risk > 0 and run_pts >= BE_R * risk:
                    t.stop_px = (max(t.stop_px, t.entry_px) if t.side > 0
                                 else min(t.stop_px, t.entry_px))

                if (TRAIL_MODE != "none" and risk > 0
                        and run_pts >= TRAIL_ARM_R * risk):
                    cand = None
                    if TRAIL_MODE == "ema":
                        cand = fast[i]
                    elif TRAIL_MODE == "atr":
                        cand = (t.peak - TRAIL_ATR_MULT * atr[i] if t.side > 0
                                else t.peak + TRAIL_ATR_MULT * atr[i])
                    elif TRAIL_MODE == "swing":
                        win = bars[max(0, i - TRAIL_SWING_LB + 1): i + 1]
                        cand = (min(s.low for s in win) - STOP_BUF_TICKS * TICK
                                if t.side > 0 else
                                max(s.high for s in win) + STOP_BUF_TICKS * TICK)
                    if cand is not None:
                        new = (max(t.stop_px, cand) if t.side > 0
                               else min(t.stop_px, cand))
                        # never let the trail jump past price and fill instantly
                        if t.side > 0:
                            new = min(new, b.close - TICK)
                        else:
                            new = max(new, b.close + TICK)
                        if (new > t.stop_px) if t.side > 0 else (new < t.stop_px):
                            t.stop_px = new
                            t.trailed = True
                continue

        # ── entry scan ──────────────────────────────────────────────────────
        if open_t is not None or day_locked:
            continue
        if not (SESS_START <= b.hhmm <= LAST_ENTRY_HHMM):
            continue
        if day_n >= MAX_TRADES_DAY or i - last_sig < COOL_BARS:
            continue

        up = fast[i] > slow[i] and slow[i] > slow[i - SLOPE_LB]
        dn = fast[i] < slow[i] and slow[i] < slow[i - SLOPE_LB]
        if not (up or dn):
            continue

        window = d[i - CYCLE_LB: i + 1]
        hook_up = d[i] > d[i - 1] and d[i - 1] <= d[i - 2]
        hook_dn = d[i] < d[i - 1] and d[i - 1] >= d[i - 2]

        side = 0
        if up and hook_up and min(window) < OS_LEVEL and d[i] < MAX_ENTRY_D:
            side = +1
        elif dn and hook_dn and max(window) > OB_LEVEL and d[i] > 100.0 - MAX_ENTRY_D:
            side = -1
        if side == 0:
            continue

        last_sig = i
        swing = bars[i - SWING_LB + 1: i + 1]
        entry = b.close + side * slip
        if side > 0:
            stop = min(s.low for s in swing) - STOP_BUF_TICKS * TICK
        else:
            stop = max(s.high for s in swing) + STOP_BUF_TICKS * TICK

        risk = abs(entry - stop)
        if not (MIN_STOP_PTS <= risk <= MAX_STOP_PTS):
            continue

        open_t = Trade(date_tag=b.date_tag, side=side, entry_hhmm=b.hhmm,
                       entry_px=entry, stop_px=stop, init_stop=stop, peak=entry,
                       tp_px=(entry + side * TP_R * risk) if TP_R > 0 else 0.0,
                       stoch_d=d[i])

    if open_t is not None:
        _close(open_t, bars[-1], bars[-1].close, "EOD", trades, slip)
    return trades


def _close(t: Trade, bar: Bar, px: float, reason: str,
           trades: List[Trade], slip: float = 0.0) -> float:
    t.exit_px = px - t.side * slip
    t.exit_hhmm = bar.hhmm
    t.reason = reason
    t.pnl = (t.exit_px - t.entry_px) * t.side * PT_VAL - COMMISSION
    trades.append(t)
    return t.pnl


# ── reporting ───────────────────────────────────────────────────────────────
def write_csv(trades: List[Trade], path: str):
    tot = 0.0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Event", "Date", "Time", "Side", "Price", "Stop", "Target",
                    "StochD", "Reason", "PnL", "TotalPnL", "MAE", "MFE"])
        for t in trades:
            w.writerow(["ENTRY", t.date_tag, t.entry_hhmm,
                        "LONG" if t.side > 0 else "SHORT",
                        f"{t.entry_px:.2f}", f"{t.init_stop:.2f}",
                        f"{t.tp_px:.2f}" if t.tp_px else "trail",
                        f"{t.stoch_d:.1f}", "", "", f"{tot:.2f}", "", ""])
            tot += t.pnl
            w.writerow(["EXIT", t.date_tag, t.exit_hhmm,
                        "LONG" if t.side > 0 else "SHORT",
                        f"{t.exit_px:.2f}", "", "", "", t.reason,
                        f"{t.pnl:.2f}", f"{tot:.2f}",
                        f"{t.mae:.2f}", f"{t.mfe:.2f}"])


def summarize(trades: List[Trade]) -> dict:
    pnls = [t.pnl for t in trades]
    n = len(pnls)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0, t=0.0, pnls=[])
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p < 0]
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
    se = (var / n) ** 0.5 if var > 0 else 0.0
    peak = run = mdd = 0.0
    for p in pnls:
        run += p
        peak = max(peak, run)
        mdd = min(mdd, run - peak)
    return dict(n=n, total=sum(pnls),
                pf=(sum(wins) / abs(sum(loss))) if loss else float("inf"),
                wr=100.0 * len(wins) / n, max_dd=mdd,
                t=(mean / se) if se > 0 else 0.0, pnls=pnls)


def load_bars(scid: Path) -> List[Bar]:
    recs = read_scid(str(scid))
    return build_time_bars(recs, BAR_MINUTES, detect_price_scale(str(scid)))


# ── bar cache ───────────────────────────────────────────────────────────────
# Building bars means a Python loop over ~30M tick records per contract (~9 min).
# The strategy itself runs in under a second. Cache the bars so entry/exit
# variants cost seconds instead of a 45-minute full sweep.
def _cache_path(tag: str) -> Path:
    return BASE / f".topdog_bars_{tag}_{int(BAR_MINUTES)}m.npz"


def load_bars_cached(tag: str, scid: Path, rebuild: bool = False) -> List[Bar]:
    p = _cache_path(tag)
    if p.exists() and not rebuild:
        z = np.load(p)
        return [Bar(dt=None, open=float(o), high=float(h), low=float(lo),
                    close=float(c), volume=int(v), bid_vol=int(bv),
                    ask_vol=int(av), idx=i, date_tag=int(dt), hhmm=int(hm))
                for i, (o, h, lo, c, v, bv, av, dt, hm) in enumerate(
                    zip(z["o"], z["h"], z["l"], z["c"], z["v"],
                        z["bv"], z["av"], z["dtag"], z["hhmm"]))]

    bars = load_bars(scid)
    np.savez_compressed(
        p,
        o=np.array([b.open for b in bars], dtype=np.float64),
        h=np.array([b.high for b in bars], dtype=np.float64),
        l=np.array([b.low for b in bars], dtype=np.float64),
        c=np.array([b.close for b in bars], dtype=np.float64),
        v=np.array([b.volume for b in bars], dtype=np.int64),
        bv=np.array([b.bid_vol for b in bars], dtype=np.int64),
        av=np.array([b.ask_vol for b in bars], dtype=np.int64),
        dtag=np.array([b.date_tag for b in bars], dtype=np.int64),
        hhmm=np.array([b.hhmm for b in bars], dtype=np.int64),
    )
    return bars


def run_one(tag: str, scid: Path, out: Optional[str] = None) -> dict:
    bars = load_bars_cached(tag, scid)
    trades = run_engine(bars)
    if out:
        write_csv(trades, out)
    s = summarize(trades)
    by = {}
    for t in trades:
        by[t.reason] = by.get(t.reason, 0) + 1
    print(f"  {tag:8s} bars={len(bars):>7,}  n={s['n']:>4}  WR={s['wr']:>5.1f}%  "
          f"PF={s['pf']:>5.2f}  Net={s['total']:>+10,.0f}  MaxDD={s['max_dd']:>9,.0f}  "
          f"t={s['t']:>+5.2f}  {by}")
    return s


def main():
    args = [a for a in sys.argv[1:]]
    print(f"TopDog Cycle — {BAR_MINUTES}m bars, EMA{FAST_EMA}/SMA{SLOW_SMA}, "
          f"Stoch({STOCH_D},{STOCH_K},{STOCH_SMOOTH}) {OS_LEVEL:.0f}/{OB_LEVEL:.0f}, "
          f"swing{SWING_LB} stop, "
          + (f"{TP_R:.1f}R target" if TP_R > 0 else "NO fixed target")
          + (f", trail={TRAIL_MODE}"
             + (f"x{TRAIL_ATR_MULT}" if TRAIL_MODE == "atr" else "")
             + f" arm@{TRAIL_ARM_R:.1f}R" if TRAIL_MODE != "none" else "")
          + f", slip={SLIP_TICKS}t")

    if args and args[0] == "--all":
        res = {}
        for tag, scid in CONTRACTS.items():
            if not scid.exists():
                print(f"  {tag:8s} MISSING {scid.name}")
                continue
            suffix = f"_{RUN_TAG}" if RUN_TAG else ""
            res[tag] = run_one(tag, scid, str(BASE / f"IOF_topdog{suffix}_{tag}.csv"))

        pooled = [p for r in res.values() for p in r["pnls"]]
        w = [p for p in pooled if p > 0]
        L = [p for p in pooled if p < 0]
        npos = sum(1 for r in res.values() if r["total"] > 0 and r["pf"] > 1.0)
        print("-" * 100)
        print(f"  POOLED  n={len(pooled)}  "
              f"PF={(sum(w) / abs(sum(L))) if L else float('inf'):.2f}  "
              f"Net={sum(pooled):+,.0f}")
        print(f"  contracts net>0 & PF>1: {npos}/{len(res)}  "
              f"-> {'PASS' if res and npos == len(res) else 'FAIL'} (ship gate)")
        return

    if not args:
        print(__doc__)
        sys.exit(2)
    scid = Path(args[0])
    out = args[1] if len(args) > 1 else str(BASE / "IOF_topdog.csv")
    run_one(scid.stem, scid, out)
    print(f"  -> {out}")


if __name__ == "__main__":
    main()
