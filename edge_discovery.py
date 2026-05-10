#!/usr/bin/env python3
"""
IOF NQ Edge Discovery — Python port of IOF_NQ_EdgeDiscovery.cpp v1.1

Reads a Sierra Chart .scid file, builds N-contract volume bars (default 3000),
computes the same 8 features the C++ ACSIL study plots, and writes a CSV.

Features (per RTH bar):
  dist_vwap_atr  = (close - session_vwap) / ATR
  cum_delta_z    = Z-score of session cumulative delta (200-bar window)
  bar_delta      = ask_vol - bid_vol
  pace_ref       = mean |bar_delta| over last 3 bars
  vol_ratio      = volume / avg_volume(20)
  dist_poc_atr   = (session_POC - close) / ATR
  dist_vah_atr   = (session_VAH - close) / ATR
  dist_val_atr   = (session_VAL - close) / ATR
  edge_signal    = ±1 when |dist_vwap_atr|>=SIG_DIST and |cum_delta_z|>=SIG_Z
                   and both agree in direction

Usage:
  python edge_discovery.py                                   # NQZ5 default
  python edge_discovery.py NQH26-CME.scid out.csv
  python edge_discovery.py C:\\SierraChart\\Data\\NQH26-CME.scid features.csv
"""

import struct, math, os, csv, sys
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import deque

import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
#  DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR  = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final"
SCID_PATH = r"C:\SierraChart\Data\NQZ5.CME.scid"
OUT_CSV   = os.path.join(BASE_DIR, "IOF_NQ_edge_features.csv")

# ─────────────────────────────────────────────────────────────────────────────
#  PARAMETERS  (mirrors C++ SetDefaults)
# ─────────────────────────────────────────────────────────────────────────────
TARGET_VOL  = 3000       # contracts per volume bar
RTH_OPEN    = 935        # 09:35 ET — first bar that counts as RTH
RTH_CLOSE   = 1555       # 15:55 ET — last bar before flatten

ATR_PERIOD  = 50         # Wilder ATR period
ATR_FLOOR   = 0.75       # minimum ATR in points
CUM_DZ_LB   = 200        # cum-delta Z lookback (bars, capped at session start)
CUM_DZ_MIN  = 20         # minimum bars required for Z calc
VOL_LB      = 20         # lookback for avg volume
PACE_LB     = 3          # lookback for pace_ref (mean |delta|)
VA_PCT      = 0.70       # value area fraction (70%)
SIG_DIST    = 0.35       # |dist_vwap_atr| threshold for edge_signal
SIG_Z       = 1.0        # |cum_delta_z| threshold for edge_signal

TICK        = 0.25       # NQ tick size

# ─────────────────────────────────────────────────────────────────────────────
#  SIERRA CHART .SCID FORMAT
# ─────────────────────────────────────────────────────────────────────────────
SCID_HDR_SIZE = 56
SCID_REC_SIZE = 40
SC_EPOCH_UTC  = datetime(1899, 12, 30, tzinfo=timezone.utc)
ET            = ZoneInfo("America/New_York")

SCID_DTYPE = np.dtype([
    ("dt",       "<i8"),
    ("open",     "<f4"),
    ("high",     "<f4"),
    ("low",      "<f4"),
    ("close",    "<f4"),
    ("n_trades", "<u4"),
    ("tot_vol",  "<u4"),
    ("bid_vol",  "<u4"),
    ("ask_vol",  "<u4"),
])


# ─────────────────────────────────────────────────────────────────────────────
#  BAR DATACLASS
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Bar:
    dt:       datetime
    open:     float
    high:     float
    low:      float
    close:    float
    volume:   int
    bid_vol:  int
    ask_vol:  int
    idx:      int
    date_tag: int   # YYYYMMDD
    hhmm:     int   # HHMM


# ─────────────────────────────────────────────────────────────────────────────
#  SCID READER
# ─────────────────────────────────────────────────────────────────────────────
def detect_price_scale(path: str) -> float:
    name = os.path.basename(path).upper()
    return 1.0 if "-CME" in name else 100.0


def load_scid(path: str) -> np.ndarray:
    size = os.path.getsize(path)
    print(f"  File size: {size/1e9:.2f} GB  ({(size - SCID_HDR_SIZE) // SCID_REC_SIZE:,} records)", end="  ")
    with open(path, "rb") as f:
        hdr = f.read(SCID_HDR_SIZE)
        if hdr[:4] != b"SCID":
            raise ValueError("Not a valid .scid file")
        data = np.frombuffer(f.read(), dtype=SCID_DTYPE)
    print(f"price_scale=÷{int(detect_price_scale(path))}")
    return data


def build_volume_bars(raw: np.ndarray, path: str) -> List[Bar]:
    scale = detect_price_scale(path)
    bars: List[Bar] = []
    acc_open = acc_high = acc_low = acc_close = 0.0
    acc_vol = acc_bid = acc_ask = 0
    bar_started = False

    for rec in raw:
        dt_us = int(rec["dt"])
        dt_utc = SC_EPOCH_UTC + __import__("datetime").timedelta(microseconds=dt_us)
        dt_et  = dt_utc.astimezone(ET)

        o = float(rec["open"])  / scale
        h = float(rec["high"])  / scale
        l = float(rec["low"])   / scale
        c = float(rec["close"]) / scale
        v = int(rec["tot_vol"])
        bid = int(rec["bid_vol"])
        ask = int(rec["ask_vol"])

        if v == 0:
            continue

        if not bar_started:
            acc_open = o; acc_high = h; acc_low = l; acc_close = c
            acc_vol = v; acc_bid = bid; acc_ask = ask
            bar_started = True
        else:
            if h > acc_high: acc_high = h
            if l < acc_low:  acc_low  = l
            acc_close = c
            acc_vol  += v
            acc_bid  += bid
            acc_ask  += ask

        if acc_vol >= TARGET_VOL:
            hhmm = dt_et.hour * 100 + dt_et.minute
            date_tag = dt_et.year * 10000 + dt_et.month * 100 + dt_et.day
            bars.append(Bar(
                dt=dt_et, open=acc_open, high=acc_high, low=acc_low, close=acc_close,
                volume=acc_vol, bid_vol=acc_bid, ask_vol=acc_ask,
                idx=len(bars), date_tag=date_tag, hhmm=hhmm
            ))
            bar_started = False
            acc_vol = 0

    return bars


# ─────────────────────────────────────────────────────────────────────────────
#  WILDER ATR  (incremental)
# ─────────────────────────────────────────────────────────────────────────────
class WilderATR:
    def __init__(self, period: int):
        self.period = period
        self.atr: float = 0.0
        self.prev_close: Optional[float] = None
        self.count: int = 0
        self._init_buf: List[float] = []

    def update(self, high: float, low: float, close: float) -> float:
        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close

        if self.count < self.period:
            self._init_buf.append(tr)
            self.count += 1
            if self.count == self.period:
                self.atr = sum(self._init_buf) / self.period
        else:
            self.atr = (self.atr * (self.period - 1) + tr) / self.period

        raw = self.atr if self.count >= self.period else (sum(self._init_buf) / len(self._init_buf) if self._init_buf else tr)
        return max(raw, ATR_FLOOR, high - low, TICK * 3)


# ─────────────────────────────────────────────────────────────────────────────
#  VALUE AREA CALCULATOR  (mirrors C++ CalcValueArea)
# ─────────────────────────────────────────────────────────────────────────────
def calc_value_area(vol_at_tick: Dict[int, float], va_pct: float, tick: float
                    ) -> Tuple[float, float, float]:
    """Returns (poc_price, vah_price, val_price). All 0 if empty."""
    if not vol_at_tick:
        return 0.0, 0.0, 0.0

    total_vol = sum(vol_at_tick.values())
    poc_tick  = max(vol_at_tick, key=vol_at_tick.get)
    target    = total_vol * va_pct

    ticks = sorted(vol_at_tick.keys())
    poc_idx = ticks.index(poc_tick)

    selected = [poc_tick]
    current  = vol_at_tick[poc_tick]
    left     = poc_idx - 1
    right    = poc_idx + 1

    while current < target and (left >= 0 or right < len(ticks)):
        lv = vol_at_tick.get(ticks[left],  -1.0) if left >= 0          else -1.0
        rv = vol_at_tick.get(ticks[right], -1.0) if right < len(ticks) else -1.0
        if rv >= lv:
            selected.append(ticks[right]); current += max(0.0, rv); right += 1
        else:
            selected.append(ticks[left]);  current += max(0.0, lv); left  -= 1

    hi = max(selected); lo = min(selected)
    return poc_tick * tick, hi * tick, lo * tick


# ─────────────────────────────────────────────────────────────────────────────
#  FEATURE COMPUTER
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FeatureRow:
    dt:           str
    date:         int
    hhmm:         int
    open:         float
    high:         float
    low:          float
    close:        float
    volume:       int
    bid_vol:      int
    ask_vol:      int
    bar_delta:    float
    cum_delta:    float
    vwap:         float
    atr:          float
    dist_vwap_atr: float
    cum_delta_z:  float
    pace_ref:     float
    vol_ratio:    float
    poc:          float
    vah:          float
    val:          float
    dist_poc_atr: float
    dist_vah_atr: float
    dist_val_atr: float
    edge_signal:  int   # +1 long edge, -1 short edge, 0 none


def compute_features(bars: List[Bar]) -> List[FeatureRow]:
    atr_calc = WilderATR(ATR_PERIOD)
    rows: List[FeatureRow] = []

    # Session-reset state
    sess_start_idx: int = -1
    sess_cum_delta: float = 0.0
    sess_cum_pv:    float = 0.0
    sess_cum_v:     float = 0.0
    vol_at_tick:    Dict[int, float] = {}
    cum_delta_hist: List[float] = []   # session cumulative deltas for Z-score

    prev_date: int = -1

    for i, bar in enumerate(bars):
        atr = atr_calc.update(bar.high, bar.low, bar.close)

        # Day rollover
        if bar.date_tag != prev_date:
            prev_date        = bar.date_tag
            sess_start_idx   = i
            sess_cum_delta   = 0.0
            sess_cum_pv      = 0.0
            sess_cum_v       = 0.0
            vol_at_tick      = {}
            cum_delta_hist   = []

        # Only process RTH bars
        if bar.hhmm < RTH_OPEN or bar.hhmm >= RTH_CLOSE:
            continue

        bar_d = bar.ask_vol - bar.bid_vol

        # Incremental session cum delta + VWAP
        sess_cum_delta += bar_d
        sess_cum_pv    += bar.close * bar.volume
        sess_cum_v     += bar.volume
        vwap = sess_cum_pv / sess_cum_v if sess_cum_v > 0 else bar.close

        # Incremental session VP
        tick_key = round(bar.close / TICK)
        vol_at_tick[tick_key] = vol_at_tick.get(tick_key, 0.0) + bar.volume
        poc, vah, val = calc_value_area(vol_at_tick, VA_PCT, TICK)

        # Cum delta Z-score (windowed within session)
        cum_delta_hist.append(sess_cum_delta)
        lo = max(0, len(cum_delta_hist) - CUM_DZ_LB)
        window = cum_delta_hist[lo:]
        if len(window) >= CUM_DZ_MIN:
            m = sum(window) / len(window)
            s = math.sqrt(sum((x - m) ** 2 for x in window) / len(window))
            cum_delta_z = (sess_cum_delta - m) / s if s > 1e-6 else 0.0
        else:
            cum_delta_z = 0.0

        # Pace ref — mean |delta| over last PACE_LB bars (RTH only)
        pace_bars = [r.ask_vol - r.bid_vol for r in rows[-PACE_LB:]] if rows else [bar_d]
        pace_ref  = sum(abs(d) for d in pace_bars) / len(pace_bars) if pace_bars else abs(bar_d)

        # Volume ratio — avg over last VOL_LB bars (any, not session-restricted)
        vol_window = [bars[max(0, i - k)].volume for k in range(min(VOL_LB, i + 1))]
        avg_v      = sum(vol_window) / len(vol_window)
        vol_ratio  = bar.volume / avg_v if avg_v > 0 else 1.0

        # Distances
        inv_atr       = 1.0 / atr if atr > 1e-6 else 0.0
        dist_vwap_atr = (bar.close - vwap) * inv_atr
        dist_poc_atr  = (poc - bar.close)  * inv_atr if poc > 0 else 0.0
        dist_vah_atr  = (vah - bar.close)  * inv_atr if vah > 0 else 0.0
        dist_val_atr  = (val - bar.close)  * inv_atr if val > 0 else 0.0

        # Edge signal (mirrors C++ IN_ShowSignal logic)
        edge_signal = 0
        if abs(dist_vwap_atr) >= SIG_DIST and abs(cum_delta_z) >= SIG_Z:
            if dist_vwap_atr * cum_delta_z > 0:   # same sign = aligned
                edge_signal = 1 if dist_vwap_atr > 0 else -1

        rows.append(FeatureRow(
            dt           = bar.dt.strftime("%Y-%m-%d %H:%M"),
            date         = bar.date_tag,
            hhmm         = bar.hhmm,
            open         = round(bar.open,  2),
            high         = round(bar.high,  2),
            low          = round(bar.low,   2),
            close        = round(bar.close, 2),
            volume       = bar.volume,
            bid_vol      = bar.bid_vol,
            ask_vol      = bar.ask_vol,
            bar_delta    = round(bar_d, 0),
            cum_delta    = round(sess_cum_delta, 0),
            vwap         = round(vwap,   2),
            atr          = round(atr,    4),
            dist_vwap_atr= round(dist_vwap_atr, 4),
            cum_delta_z  = round(cum_delta_z,   4),
            pace_ref     = round(pace_ref,       2),
            vol_ratio    = round(vol_ratio,      4),
            poc          = round(poc, 2),
            vah          = round(vah, 2),
            val          = round(val, 2),
            dist_poc_atr = round(dist_poc_atr,  4),
            dist_vah_atr = round(dist_vah_atr,  4),
            dist_val_atr = round(dist_val_atr,  4),
            edge_signal  = edge_signal,
        ))

    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  CSV WRITER
# ─────────────────────────────────────────────────────────────────────────────
FIELDNAMES = [
    "dt", "date", "hhmm",
    "open", "high", "low", "close", "volume", "bid_vol", "ask_vol",
    "bar_delta", "cum_delta",
    "vwap", "atr",
    "dist_vwap_atr", "cum_delta_z",
    "pace_ref", "vol_ratio",
    "poc", "vah", "val",
    "dist_poc_atr", "dist_vah_atr", "dist_val_atr",
    "edge_signal",
]

def write_csv(rows: List[FeatureRow], path: str):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for r in rows:
            w.writerow(r.__dict__)
    print(f"CSV written to: {path}")


# ─────────────────────────────────────────────────────────────────────────────
#  SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
def print_summary(rows: List[FeatureRow], scid_path: str):
    sym = os.path.splitext(os.path.basename(scid_path))[0]
    total = len(rows)
    long_sig  = sum(1 for r in rows if r.edge_signal == +1)
    short_sig = sum(1 for r in rows if r.edge_signal == -1)

    if total == 0:
        print("No RTH bars computed.")
        return

    dvwap = [r.dist_vwap_atr for r in rows]
    cdz   = [r.cum_delta_z   for r in rows]
    vr    = [r.vol_ratio      for r in rows]

    def stats(v):
        a = sorted(v)
        n = len(a)
        return min(a), a[n//4], a[n//2], a[3*n//4], max(a)

    print()
    print("=" * 66)
    print(f"  IOF NQ EDGE DISCOVERY — FEATURE SUMMARY  ({sym})")
    print("=" * 66)
    print(f"  RTH bars        : {total:,}")
    print(f"  Edge signals    : {long_sig} long  /  {short_sig} short  ({long_sig+short_sig} total, {(long_sig+short_sig)/total*100:.1f}%)")
    print()
    print(f"  {'Feature':<20}  {'Min':>8}  {'P25':>8}  {'Med':>8}  {'P75':>8}  {'Max':>8}")
    print(f"  {'-'*20}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
    for name, vals in [("dist_vwap_atr", dvwap), ("cum_delta_z", cdz), ("vol_ratio", vr)]:
        lo, p25, med, p75, hi = stats(vals)
        print(f"  {name:<20}  {lo:>8.3f}  {p25:>8.3f}  {med:>8.3f}  {p75:>8.3f}  {hi:>8.3f}")
    print("=" * 66)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    scid = sys.argv[1] if len(sys.argv) > 1 else SCID_PATH
    out  = sys.argv[2] if len(sys.argv) > 2 else OUT_CSV

    # Resolve relative paths against BASE_DIR
    if not os.path.isabs(scid) and not os.path.exists(scid):
        scid = os.path.join(r"C:\SierraChart\Data", scid)
    if not os.path.isabs(out):
        out  = os.path.join(BASE_DIR, out)

    print(f"Reading {scid}")
    raw  = load_scid(scid)
    print(f"  Loaded {len(raw):,} records.")

    print(f"Building {TARGET_VOL}-contract volume bars ...")
    bars = build_volume_bars(raw, scid)
    rth  = sum(1 for b in bars if RTH_OPEN <= b.hhmm < RTH_CLOSE)
    print(f"  Total bars: {len(bars):,}  |  RTH bars: {rth:,}")

    print("Computing features ...")
    rows = compute_features(bars)
    print(f"  Feature rows: {len(rows):,}")

    print_summary(rows, scid)
    write_csv(rows, out)


if __name__ == "__main__":
    main()
