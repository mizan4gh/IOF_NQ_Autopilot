"""Vectorised fixed-clock bar builder for Sierra .scid tick files.

topdog_cycle.build_time_bars walks ~30M tick records in a Python for-loop and
does one `astimezone(ET)` call per record. On the 2.8 GB MNQ file that is over
ten minutes; on the six frozen NQ contracts it is ~45 minutes for a full sweep.
This does the same thing with numpy group-reductions and a precomputed DST
transition table: ~10-20 s per contract.

`build_time_bars_fast` is a drop-in for `topdog_cycle.build_time_bars` and is
asserted bar-for-bar identical to it by `--verify` (see __main__ below). The
loop version stays in topdog_cycle as the reference implementation.

The one thing that is genuinely subtle is the timezone. numpy has no tz support,
so instead of converting per record we build the UTC->US/Eastern offset step
function over the file's own date range (two transitions a year) and apply it
with a single searchsorted.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import numpy as np

from backtest import ET, SC_EPOCH_UTC, Bar, detect_price_scale, read_scid

# 1899-12-30 -> 1970-01-01
_SC_TO_UNIX_US = 25569 * 86400 * 1_000_000
_US_PER_DAY = 86400 * 1_000_000


def _et_offset_table(t0_s: int, t1_s: int):
    """Transition instants (unix seconds) and the ET UTC-offset in force after
    each, covering [t0_s, t1_s]. Found by day-scan then per-second bisection, so
    it is exact rather than a 'DST starts in March' approximation."""
    def off(ts: int) -> int:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return int(dt.astimezone(ET).utcoffset().total_seconds())

    starts = [t0_s]
    offs = [off(t0_s)]
    day = 86400
    prev = offs[0]
    t = t0_s
    while t < t1_s:
        nxt = min(t + day, t1_s)
        cur = off(nxt)
        if cur != prev:
            lo, hi = t, nxt          # transition is in (lo, hi]
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if off(mid) == prev:
                    lo = mid
                else:
                    hi = mid
            starts.append(hi)
            offs.append(cur)
            prev = cur
        t = nxt
    return np.asarray(starts, dtype=np.int64), np.asarray(offs, dtype=np.int64)


def build_time_bars_fast(recs: np.ndarray, minutes: int = 5,
                         price_scale: float = 1.0) -> List[Bar]:
    """Aggregate .scid ticks into fixed-clock bars stamped in US/Eastern.

    Mirrors build_time_bars exactly, including its OHL fallback: Sierra emits
    0 / non-finite OHL on ask-side single-tick records, and min(low, 0) silently
    corrupts every range-derived series downstream.
    """
    tot = recs["tot_vol"].astype(np.int64)
    keep = tot != 0
    if not keep.any():
        return []

    dt_us = recs["dt"][keep].astype(np.int64) - _SC_TO_UNIX_US
    tot = tot[keep]

    # ── UTC -> ET, vectorised through the transition table ──────────────────
    t_s = dt_us // 1_000_000
    starts, offs = _et_offset_table(int(t_s[0]), int(t_s[-1]))
    local_us = dt_us + offs[np.searchsorted(starts, t_s, side="right") - 1] * 1_000_000

    day = local_us // _US_PER_DAY
    mod = local_us - day * _US_PER_DAY
    minute_of_day = mod // 60_000_000
    slot = minute_of_day // minutes
    key = day * (1440 // minutes + 1) + slot

    # group starts: records are time-ordered, so equal keys are contiguous
    gs = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])

    c = recs["close"][keep].astype(np.float64) / price_scale
    o = recs["open"][keep].astype(np.float64) / price_scale
    h = recs["high"][keep].astype(np.float64) / price_scale
    lo_ = recs["low"][keep].astype(np.float64) / price_scale
    for arr in (o, h, lo_):
        bad = ~np.isfinite(arr) | (arr == 0.0)
        arr[bad] = c[bad]

    b_open = o[gs]
    b_close = c[np.r_[gs[1:] - 1, len(c) - 1]]
    b_high = np.maximum.reduceat(h, gs)
    b_low = np.minimum.reduceat(lo_, gs)
    b_vol = np.add.reduceat(tot, gs)
    b_bid = np.add.reduceat(recs["bid_vol"][keep].astype(np.int64), gs)
    b_ask = np.add.reduceat(recs["ask_vol"][keep].astype(np.int64), gs)

    # bar stamp = the bucket's start minute, in local time
    b_day = day[gs]
    b_slot = slot[gs]
    b_min = b_slot * minutes
    epoch = datetime(1970, 1, 1)
    out: List[Bar] = []
    for i in range(len(gs)):
        d = epoch + timedelta(days=int(b_day[i]))
        hh, mm = divmod(int(b_min[i]), 60)
        out.append(Bar(
            dt=d.replace(hour=hh, minute=mm),
            open=float(b_open[i]), high=float(b_high[i]),
            low=float(b_low[i]), close=float(b_close[i]),
            volume=int(b_vol[i]), bid_vol=int(b_bid[i]), ask_vol=int(b_ask[i]),
            idx=i,
            date_tag=d.year * 10000 + d.month * 100 + d.day,
            hhmm=hh * 100 + mm,
        ))
    return out


# ── cache, shared with topdog_cycle (same path, same npz layout) ────────────
BASE = Path(__file__).parent


def cache_path(tag: str, minutes: int) -> Path:
    return BASE / f".topdog_bars_{tag}_{int(minutes)}m.npz"


def load_bars_cached(tag: str, scid: Path, minutes: int = 5,
                     rebuild: bool = False) -> List[Bar]:
    p = cache_path(tag, minutes)
    if p.exists() and not rebuild:
        z = np.load(p)
        return [Bar(dt=None, open=float(o), high=float(h), low=float(lo),
                    close=float(c), volume=int(v), bid_vol=int(bv),
                    ask_vol=int(av), idx=i, date_tag=int(dt), hhmm=int(hm))
                for i, (o, h, lo, c, v, bv, av, dt, hm) in enumerate(
                    zip(z["o"], z["h"], z["l"], z["c"], z["v"],
                        z["bv"], z["av"], z["dtag"], z["hhmm"]))]

    bars = build_time_bars_fast(read_scid(str(scid)), minutes,
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
    )
    return bars


def _verify(scid: Path, minutes: int = 5):
    """Assert the fast builder reproduces topdog_cycle.build_time_bars exactly."""
    import time

    from topdog_cycle import build_time_bars

    recs = read_scid(str(scid))
    ps = detect_price_scale(str(scid))
    t0 = time.time()
    fast = build_time_bars_fast(recs, minutes, ps)
    t1 = time.time()
    slow = build_time_bars(recs, minutes, ps)
    t2 = time.time()
    print(f"  {scid.name}: fast {t1-t0:.1f}s  loop {t2-t1:.1f}s  "
          f"speedup {(t2-t1)/max(t1-t0,1e-9):.0f}x")
    assert len(fast) == len(slow), f"bar count {len(fast)} != {len(slow)}"
    for a, b in zip(fast, slow):
        assert (a.date_tag, a.hhmm) == (b.date_tag, b.hhmm), (a, b)
        for f in ("open", "high", "low", "close", "volume", "bid_vol", "ask_vol"):
            assert getattr(a, f) == getattr(b, f), (f, a, b)
    print(f"  IDENTICAL over {len(fast):,} bars")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--verify":
        for s in (args[1:] or ["F.US.ENQU26.scid"]):
            _verify(BASE / s)
    else:
        print(__doc__)
