"""Entry-side screen for the TopDog Cycle setup.

The exit dimension is exhausted — 3 fixed R-multiples and 3 trail types all fail
([[project_topdog_cycle_falsified]]). The entry is what isn't producing signal,
so this screens ENTRY variants while holding the exit fixed at the least-bad
config (2R target, 6-bar swing stop).

Variants, chosen to DECOMPOSE the setup rather than sweep it:

  base        %D hooks up out of the sub-20 band, with the trend filter  (control)
  trend_only  MA cross/slope alone — no stochastic at all
  stoch_only  the %D hook alone — no trend filter
  kd_cross    %K crosses %D inside the band (the classic stochastic cross)
  band_exit   %D crosses back up THROUGH the 20 line (not a hook inside it)
  deep        same as base but requires %D < 10 (deep oversold)
  pullback    base + price must have touched the fast EMA in the last 5 bars
  fade        base with the sign FLIPPED (long on the overbought hook)
  coinflip    NULL CONTROL — same bar times as base, random side

trend_only and stoch_only are the important ones: if neither component beats the
coinflip control, the setup has no entry edge and no recombination of the two
will produce one. `fade` checks whether the edge is real but sign-inverted.
`coinflip` establishes what zero looks like under this exact stop/target
geometry and cost model, which is the honest yardstick for everything else.

Uses the cached 5-min bars, so a full 9-variant x 6-contract screen is seconds
once `python topdog_cycle.py --all` has populated the cache.

Usage: python topdog_entry_screen.py
"""
import math
import random
import sys
from typing import List, Optional

import topdog_cycle as T
from topdog_cycle import (Bar, CONTRACTS, TICK, PT_VAL, COMMISSION,
                          ema, sma, stochastic_full, load_bars_cached)

EXIT_TP_R = 2.0      # least-bad exit from the exit sweep
SEED = 20260801      # fixed so the coinflip control is reproducible


def signals(name: str, bars: List[Bar], fast, slow, k, d, i: int) -> int:
    """Return +1 long, -1 short, 0 no trade for entry variant `name` at bar i."""
    up = fast[i] > slow[i] and slow[i] > slow[i - T.SLOPE_LB]
    dn = fast[i] < slow[i] and slow[i] < slow[i - T.SLOPE_LB]
    win = d[i - T.CYCLE_LB: i + 1]
    hook_up = d[i] > d[i - 1] and d[i - 1] <= d[i - 2]
    hook_dn = d[i] < d[i - 1] and d[i - 1] >= d[i - 2]
    os_seen = min(win) < T.OS_LEVEL
    ob_seen = max(win) > T.OB_LEVEL
    lo_half = d[i] < T.MAX_ENTRY_D
    hi_half = d[i] > 100.0 - T.MAX_ENTRY_D

    if name == "base":
        if up and hook_up and os_seen and lo_half:
            return +1
        if dn and hook_dn and ob_seen and hi_half:
            return -1

    elif name == "trend_only":
        # fast MA crosses the slow MA this bar, slope-confirmed. No stochastic.
        x_up = fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
        x_dn = fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]
        if x_up and slow[i] > slow[i - T.SLOPE_LB]:
            return +1
        if x_dn and slow[i] < slow[i - T.SLOPE_LB]:
            return -1

    elif name == "stoch_only":
        if hook_up and os_seen and lo_half:
            return +1
        if hook_dn and ob_seen and hi_half:
            return -1

    elif name == "kd_cross":
        x_up = k[i] > d[i] and k[i - 1] <= d[i - 1]
        x_dn = k[i] < d[i] and k[i - 1] >= d[i - 1]
        if up and x_up and lo_half:
            return +1
        if dn and x_dn and hi_half:
            return -1

    elif name == "band_exit":
        out_up = d[i] > T.OS_LEVEL and d[i - 1] <= T.OS_LEVEL
        out_dn = d[i] < T.OB_LEVEL and d[i - 1] >= T.OB_LEVEL
        if up and out_up:
            return +1
        if dn and out_dn:
            return -1

    elif name == "deep":
        if up and hook_up and min(win) < 10.0 and d[i] < T.MAX_ENTRY_D:
            return +1
        if dn and hook_dn and max(win) > 90.0 and d[i] > 100.0 - T.MAX_ENTRY_D:
            return -1

    elif name == "pullback":
        touched_up = any(bars[j].low <= fast[j] for j in range(i - 4, i + 1))
        touched_dn = any(bars[j].high >= fast[j] for j in range(i - 4, i + 1))
        if up and hook_up and os_seen and lo_half and touched_up:
            return +1
        if dn and hook_dn and ob_seen and hi_half and touched_dn:
            return -1

    elif name == "fade":
        if up and hook_dn and ob_seen and hi_half:
            return +1
        if dn and hook_up and os_seen and lo_half:
            return -1

    elif name == "coinflip":
        if (up and hook_up and os_seen and lo_half) or \
           (dn and hook_dn and ob_seen and hi_half):
            return +1 if random.random() < 0.5 else -1

    return 0


def run_variant(name: str, bars: List[Bar], side_mode: str = "as_is") -> List[float]:
    """Same management as topdog_cycle.run_engine, entry swapped for `name`.

    side_mode lets the SAME trigger be re-signed, which is what makes a null
    honest: "random" keeps every entry timestamp and stop/target but picks the
    direction by coin toss; "inverse" flips it. Anything the trigger knows about
    direction has to show up as a gap between as_is and these.
    """
    closes = [b.close for b in bars]
    fast = ema(closes, T.FAST_EMA)
    slow = sma(closes, T.SLOW_SMA)
    k, d = stochastic_full(bars, T.STOCH_K, T.STOCH_SMOOTH, T.STOCH_D)

    pnls: List[float] = []
    pos = None
    day = None
    day_pnl = 0.0
    day_n = 0
    locked = False
    last_sig = -10 ** 9
    warm = max(T.SLOW_SMA, T.STOCH_K + T.STOCH_SMOOTH + T.STOCH_D,
               T.CYCLE_LB, T.SWING_LB, T.SLOPE_LB) + 1

    for i in range(warm, len(bars)):
        b = bars[i]
        if b.date_tag != day:
            if pos is not None:
                pnls.append(_pnl(pos, bars[i - 1].close))
                pos = None
            day, day_pnl, day_n, locked = b.date_tag, 0.0, 0, False

        if pos is not None:
            side, entry, stop, tp = pos
            ex = None
            if side > 0:
                if b.low <= stop:
                    ex = stop
                elif b.high >= tp:
                    ex = tp
            else:
                if b.high >= stop:
                    ex = stop
                elif b.low <= tp:
                    ex = tp
            if ex is None and b.hhmm >= T.FLAT_BY_HHMM:
                ex = b.close
            if ex is not None:
                p = _pnl(pos, ex)
                pnls.append(p)
                day_pnl += p
                day_n += 1
                pos = None
                if T.DAILY_LOSS > 0 and day_pnl <= -T.DAILY_LOSS:
                    locked = True
            else:
                continue

        if pos is not None or locked:
            continue
        if not (T.SESS_START <= b.hhmm <= T.LAST_ENTRY_HHMM):
            continue
        if day_n >= T.MAX_TRADES_DAY or i - last_sig < T.COOL_BARS:
            continue

        side = signals(name, bars, fast, slow, k, d, i)
        if side == 0:
            continue
        last_sig = i
        if side_mode == "random":
            side = +1 if random.random() < 0.5 else -1
        elif side_mode == "inverse":
            side = -side

        sw = bars[i - T.SWING_LB + 1: i + 1]
        entry = b.close
        stop = (min(s.low for s in sw) - T.STOP_BUF_TICKS * TICK if side > 0
                else max(s.high for s in sw) + T.STOP_BUF_TICKS * TICK)
        risk = abs(entry - stop)
        if not (T.MIN_STOP_PTS <= risk <= T.MAX_STOP_PTS):
            continue
        pos = (side, entry, stop, entry + side * EXIT_TP_R * risk)

    return pnls


def _pnl(pos, ex_px: float) -> float:
    side, entry, _stop, _tp = pos
    return (ex_px - entry) * side * PT_VAL - COMMISSION


def stats(pnls: List[float]) -> dict:
    n = len(pnls)
    if n == 0:
        return dict(n=0, net=0.0, pf=0.0, wr=0.0, t=0.0)
    w = [p for p in pnls if p > 0]
    L = [p for p in pnls if p < 0]
    m = sum(pnls) / n
    var = sum((p - m) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if var > 0 else 0.0
    return dict(n=n, net=sum(pnls), wr=100.0 * len(w) / n,
                pf=(sum(w) / abs(sum(L))) if L else float("inf"),
                t=(m / se) if se > 0 else 0.0)


VARIANTS = ["base", "trend_only", "stoch_only", "kd_cross", "band_exit",
            "deep", "pullback", "fade", "coinflip"]


def main():
    only = sys.argv[1:] or VARIANTS
    bars_by_tag = {}
    for tag, scid in CONTRACTS.items():
        if not scid.exists():
            print(f"  missing {scid.name}")
            continue
        bars_by_tag[tag] = load_bars_cached(tag, scid)
        print(f"  loaded {tag}: {len(bars_by_tag[tag]):,} bars")

    print(f"\n  exit held fixed at {EXIT_TP_R:.1f}R / {T.SWING_LB}-bar swing stop, "
          f"slip=0, ${COMMISSION:.0f} RT\n")
    print(f"  {'variant':11s} {'n':>5s} {'WR%':>6s} {'PF':>6s} {'pooled Net':>11s} "
          f"{'t':>6s}  {'gate':>5s}   per-contract net")
    print("  " + "-" * 96)

    for v in only:
        random.seed(SEED)
        pooled, per, npos = [], [], 0
        for tag in bars_by_tag:
            p = run_variant(v, bars_by_tag[tag])
            s = stats(p)
            pooled += p
            per.append(f"{tag}:{s['net']:+,.0f}")
            if s["net"] > 0 and s["pf"] > 1.0:
                npos += 1
        s = stats(pooled)
        print(f"  {v:11s} {s['n']:>5} {s['wr']:>6.1f} {s['pf']:>6.2f} "
              f"{s['net']:>+11,.0f} {s['t']:>+6.2f}  {npos}/{len(bars_by_tag):<3}  "
              + "  ".join(per))


if __name__ == "__main__":
    main()
