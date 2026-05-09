#!/usr/bin/env python3
"""
IOF NQ Autopilot — Standalone Python Backtester  v1.0
Reads NQZ25-CME.scid (or extracts from .zip), builds 3000-contract volume bars,
runs strategy logic faithful to IOF_NQ_Autopilot.cpp v12.19, writes CSV journal.

Usage:
    python backtest.py                              # uses defaults below
    python backtest.py NQZ25-CME.scid out.csv       # override paths
"""

import struct, math, os, csv, sys, zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
#  DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR   = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final"
SCID_PATH  = os.path.join(BASE_DIR, "NQZ25-CME.scid")
ZIP_PATH   = os.path.join(BASE_DIR, "NQZ25-CME.zip")
OUT_CSV    = os.path.join(BASE_DIR, "IOF_NQ_backtest_NQZ25.csv")

# ─────────────────────────────────────────────────────────────────────────────
#  SIERRA CHART .SCID FORMAT
# ─────────────────────────────────────────────────────────────────────────────
# Header: 56 bytes
#   [0:4]  FileTypeUniqueHeaderID = "SCID"
#   [4:8]  HeaderSize  (uint32) = 56
#   [8:12] RecordSize  (uint32) = 40
#   [12:14] Version   (uint16)
#   [14:16] Unused
#   [16:20] UTCStartIndex (uint32)
#   [20:56] Reserved
#
# Each record: 40 bytes
#   DateTime   double (8)  — OLE Automation date: days since 1899-12-30
#   Open       float  (4)
#   High       float  (4)
#   Low        float  (4)
#   Close      float  (4)
#   NumTrades  uint32 (4)
#   TotalVol   uint32 (4)
#   BidVol     uint32 (4)
#   AskVol     uint32 (4)

SCID_HDR_FMT  = "<4sIIHHI36s"
SCID_HDR_SIZE = 56
SCID_REC_SIZE = 40
# SCDateTime in Sierra Chart SCID = int64 microseconds since 1899-12-30 00:00:00 UTC
SC_EPOCH_UTC  = datetime(1899, 12, 30, tzinfo=timezone.utc)
ET            = ZoneInfo("America/New_York")

SCID_DTYPE = np.dtype([
    ("dt",       "<i8"),   # int64 microseconds since SC_EPOCH_UTC
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
#  CONSTANTS  (mirror C++ hardcodes in IOF_NQ_Autopilot.cpp)
# ─────────────────────────────────────────────────────────────────────────────
TARGET_VOL   = 2500          # contracts per bar
RTH_OPEN     = 935           # 09:35 ET
FLATTEN_HHMM = 1555          # 15:55 ET  (inlined .cpp default)

ATR_PER      = 14
C_STOP_ATR   = 1.2
C_T1_ATR     = 1.25
C_T2_ATR     = 3.0
C_BE_ATR     = 0.30
C_TRAIL_ATR  = 1.50
C_TRAIL_DLY  = 5
C_T1_RATIO   = 0.50
C_STOP_FL    = 20.0;  C_STOP_CL = 40.0
C_T1_FL      = 25.0;  C_T1_CL   = 50.0
C_T2_FL      = 75.0;  C_T2_CL   = 125.0
C_RM_FLOOR   = 0.60
QUAL_FLOOR   = 50
C_MIN_SC_M1  = 4
C_MIN_SC_ALL = 3
C_COOL_TRADE = 5
C_COOL_LOSS  = 10
C_COOL_STOP  = 10
C_OPEN_COOL  = 36
C_VWAP_MAT   = 40
C_DELTA_MAT  = 25
C_CONSOL_LB  = 25
C_CONSOL_ATR = 1.5
C_SWEEP_LB   = 15
C_M5_COOL    = 30
C_M5_MIN_SC  = 5
C_MAX_LOSSES = 2
C_STRUCT_LB  = 25

DAILY_LOSS   = 800.0
DAILY_PROF   = 1000.0
MAX_TRADES   = 6
TICK         = 0.25
PT_VAL       = 20.0          # NQ: $20/point ($5/tick)
COMMISSION   = 5.0           # RT per trade

MODE_NAMES = ["M1","M2","M3","M4","M5","M6","M7","M8"]

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
def read_scid(path: str) -> np.ndarray:
    with open(path, "rb") as f:
        hdr_raw = f.read(SCID_HDR_SIZE)
    hdr = struct.unpack_from(SCID_HDR_FMT, hdr_raw)
    assert hdr[0] == b"SCID", f"Bad header: {hdr[0]}"
    rec_sz = hdr[2]
    assert rec_sz == SCID_REC_SIZE, f"Unexpected record size {rec_sz}"

    with open(path, "rb") as f:
        f.seek(SCID_HDR_SIZE)
        raw = f.read()
    n = len(raw) // SCID_REC_SIZE
    return np.frombuffer(raw[: n * SCID_REC_SIZE], dtype=SCID_DTYPE)

# ─────────────────────────────────────────────────────────────────────────────
#  VOLUME BAR AGGREGATOR
# ─────────────────────────────────────────────────────────────────────────────
def build_volume_bars(recs: np.ndarray, target_vol: int = TARGET_VOL) -> List[Bar]:
    """
    Aggregates .scid tick records into fixed-volume bars.
    Sierra Chart SCDateTime = int64 microseconds since 1899-12-30 00:00:00 UTC.
    RTH filter uses US/Eastern local time.
    """
    bars: List[Bar] = []
    bar_idx = 0

    o = h = l = c = 0.0
    vol = bid = ask = 0
    first_dt = None

    for rec in recs:
        tv = int(rec["tot_vol"])
        if tv == 0:
            continue

        sc_us = int(rec["dt"])
        dt_utc = SC_EPOCH_UTC + timedelta(microseconds=sc_us)
        dt    = dt_utc.astimezone(ET)          # local ET (handles DST)
        rc    = float(rec["close"])
        rh    = float(rec["high"])
        rl    = float(rec["low"])
        ro_raw = float(rec["open"])
        # For ask-side ticks open is 0 or non-finite; use close as bar open instead
        ro    = rc if (not math.isfinite(ro_raw) or ro_raw == 0.0) else ro_raw
        bv, av = int(rec["bid_vol"]), int(rec["ask_vol"])

        if first_dt is None:
            o         = ro
            h         = rh
            l         = rl
            first_dt  = dt

        h    = max(h, rh)
        l    = min(l, rl)
        c    = rc
        vol += tv
        bid += bv
        ask += av

        while vol >= target_vol:
            bar_dt   = dt
            hhmm     = bar_dt.hour * 100 + bar_dt.minute
            date_tag = bar_dt.year * 10000 + bar_dt.month * 100 + bar_dt.day

            bars.append(Bar(
                dt       = bar_dt,
                open     = o,
                high     = h,
                low      = l,
                close    = c,
                volume   = vol,
                bid_vol  = bid,
                ask_vol  = ask,
                idx      = bar_idx,
                date_tag = date_tag,
                hhmm     = hhmm,
            ))
            bar_idx += 1

            excess = vol - target_vol
            if excess > 0 and vol > 0:
                ratio = excess / vol
                o     = c; h = c; l = c
                vol   = excess
                bid   = int(bid * ratio)
                ask   = int(ask * ratio)
                first_dt = dt
            else:
                o = h = l = c = 0.0
                vol = bid = ask = 0
                first_dt = None
                break

    return bars

# ─────────────────────────────────────────────────────────────────────────────
#  WILDER ATR
# ─────────────────────────────────────────────────────────────────────────────
class WilderATR:
    def __init__(self, period: int = ATR_PER):
        self.period = period
        self._atr   = 0.0
        self._prev  = None
        self._n     = 0
        self._sum   = 0.0

    def update(self, h: float, l: float, c: float) -> float:
        if self._prev is None:
            self._prev = c
            return 0.0
        tr = max(h - l, abs(h - self._prev), abs(l - self._prev))
        self._prev = c
        self._n   += 1
        if self._n < self.period:
            self._sum += tr
        elif self._n == self.period:
            self._sum += tr
            self._atr  = self._sum / self.period
        else:
            self._atr = (self._atr * (self.period - 1) + tr) / self.period
        return self._atr

# ─────────────────────────────────────────────────────────────────────────────
#  SESSION VWAP  (resets each RTH day)
# ─────────────────────────────────────────────────────────────────────────────
class SessionVWAP:
    def __init__(self):
        self._spv = self._sv = self._spv2 = 0.0
        self._date = -1

    def update(self, bar: Bar) -> Tuple[float, float]:
        """Returns (vwap, sd)."""
        if bar.date_tag != self._date:
            self._spv = self._sv = self._spv2 = 0.0
            self._date = bar.date_tag
        typ = (bar.high + bar.low + bar.close) / 3.0
        self._sv   += bar.volume
        self._spv  += typ * bar.volume
        self._spv2 += typ * typ * bar.volume
        if self._sv <= 0:
            return bar.close, 0.0
        vwap = self._spv / self._sv
        var  = max(0.0, self._spv2 / self._sv - vwap * vwap)
        return vwap, math.sqrt(var)

# ─────────────────────────────────────────────────────────────────────────────
#  VOLUME PROFILE  (5-day composite)
# ─────────────────────────────────────────────────────────────────────────────
class VP5Day:
    BIN     = 1.0
    NBINS   = 800
    VA_PCT  = 0.70
    N_DAYS  = 5

    def __init__(self):
        self._days: list = []
        self._cur_date   = -1
        self._cur_base   = 0.0
        self._cur_bins   = np.zeros(self.NBINS)
        self.poc = self.vah = self.val = 0.0
        self.valid = False

    def _b2p(self, b: int, base: float) -> float:
        return base + (b - self.NBINS // 2) * self.BIN

    def _p2b(self, px: float, base: float) -> int:
        return max(0, min(self.NBINS - 1,
                          int((px - base) / self.BIN) + self.NBINS // 2))

    def update(self, bar: Bar):
        if bar.date_tag != self._cur_date:
            if self._cur_date > 0 and self._cur_base > 0:
                self._days.append((self._cur_date, self._cur_base, self._cur_bins.copy()))
                if len(self._days) > 50:
                    self._days.pop(0)
            self._cur_date = bar.date_tag
            self._cur_base = round(bar.open / self.BIN) * self.BIN
            self._cur_bins = np.zeros(self.NBINS)

        if self._cur_base <= 0:
            self._cur_base = round(bar.open / self.BIN) * self.BIN

        bl = self._p2b(bar.low,  self._cur_base)
        bh = self._p2b(bar.high, self._cur_base)
        per = bar.volume / max(1, bh - bl + 1)
        self._cur_bins[bl:bh + 1] += per
        self._compute()

    def _compute(self):
        days = self._days[-self.N_DAYS:]
        if not days:
            self.valid = False
            return
        bases  = [d[1] for d in days]
        g_base = round(sum(bases) / len(bases) / self.BIN) * self.BIN
        comp   = np.zeros(self.NBINS)
        for _, dbase, dbins in days:
            for b in range(self.NBINS):
                if dbins[b] <= 0:
                    continue
                px = self._b2p(b, dbase)
                cb = self._p2b(px, g_base)
                comp[cb] += dbins[b]
        total = comp.sum()
        if total <= 0:
            self.valid = False
            return
        poc_b = int(np.argmax(comp))
        self.poc = self._b2p(poc_b, g_base)
        va_target = total * self.VA_PCT
        va_vol = comp[poc_b]
        lo = hi = poc_b
        while va_vol < va_target and (lo > 0 or hi < self.NBINS - 1):
            vl = comp[lo - 1] if lo > 0 else 0
            vh = comp[hi + 1] if hi < self.NBINS - 1 else 0
            if vl >= vh and lo > 0:
                lo -= 1; va_vol += comp[lo]
            elif hi < self.NBINS - 1:
                hi += 1; va_vol += comp[hi]
            elif lo > 0:
                lo -= 1; va_vol += comp[lo]
            else:
                break
        self.vah   = self._b2p(hi, g_base)
        self.val   = self._b2p(lo, g_base)
        self.valid = True

# ─────────────────────────────────────────────────────────────────────────────
#  CONTROL SCORE
# ─────────────────────────────────────────────────────────────────────────────
def control_score(bars: List[Bar], i: int, vp: VP5Day, delta_mature: bool) -> int:
    if not delta_mature or i < 5:
        return 0
    b = bars[i]
    score = 0

    # Delta recency vs older
    d_rec = sum(bars[j].ask_vol - bars[j].bid_vol for j in range(max(0, i - 4), i + 1))
    d_old = sum(bars[j].ask_vol - bars[j].bid_vol for j in range(max(0, i - 14), max(0, i - 4)))
    if d_rec > d_old * 1.2 and d_rec > 0:   score += 1
    elif d_rec < d_old * 1.2 and d_rec < 0: score -= 1

    # Delta / price correlation
    px_up = b.close > bars[i - 1].close
    cd_up = d_rec > 0
    if px_up and cd_up:       score += 1
    elif not px_up and not cd_up: score -= 1

    # Imbalance aggression (5-bar window)
    ask5 = sum(bars[j].ask_vol for j in range(max(0, i - 4), i + 1))
    bid5 = sum(bars[j].bid_vol for j in range(max(0, i - 4), i + 1))
    tot5 = ask5 + bid5
    if tot5 > 0:
        if ask5 / tot5 >= 0.58: score += 1
        elif bid5 / tot5 >= 0.58: score -= 1

    # Absorption proxy: up-bar + down-delta (or vice versa)
    abs_buy  = sum(1 for j in range(max(0, i - 4), i + 1)
                   if bars[j].close > bars[j].open
                   and bars[j].ask_vol - bars[j].bid_vol < 0)
    abs_sell = sum(1 for j in range(max(0, i - 4), i + 1)
                   if bars[j].close < bars[j].open
                   and bars[j].ask_vol - bars[j].bid_vol > 0)
    if abs_buy  >= 3: score += 1
    if abs_sell >= 3: score -= 1

    # VP position
    if vp.valid:
        if b.close > vp.vah:   score += 1
        elif b.close < vp.val: score -= 1

    return max(-5, min(5, score))

# ─────────────────────────────────────────────────────────────────────────────
#  DIVERGENCE STATE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Div:
    strength:           int  = 0
    trend_div_bull:     bool = False
    trend_div_bear:     bool = False
    swing_div_bull:     bool = False
    swing_div_bear:     bool = False
    persist_abs_buy:    bool = False
    persist_abs_sell:   bool = False

def divergence(bars: List[Bar], i: int, cum_d: List[float], atr: float) -> Div:
    d = Div()
    if i < C_STRUCT_LB or atr <= 0 or i >= len(cum_d):
        return d
    cd = cum_d[i]
    start = max(0, i - C_STRUCT_LB)
    peak  = max(bars[j].high for j in range(start, i + 1))
    trough = min(bars[j].low  for j in range(start, i + 1))

    if bars[i].high >= peak   - atr * 0.3 and cd < 0: d.trend_div_bear = True
    if bars[i].low  <= trough + atr * 0.3 and cd > 0: d.trend_div_bull = True

    if i >= 5:
        cd_swing = cd - cum_d[i - 5]
        sw_hi = max(bars[j].high for j in range(i - 4, i + 1))
        sw_lo = min(bars[j].low  for j in range(i - 4, i + 1))
        px0   = bars[i - 5].close
        if bars[i].high >= sw_hi - atr * 0.3 and sw_hi - px0 > atr * 0.5 and cd_swing < 0:
            d.swing_div_bear = True
        if bars[i].low  <= sw_lo + atr * 0.3 and px0 - sw_lo > atr * 0.5 and cd_swing > 0:
            d.swing_div_bull = True

    up_dn = sum(1 for j in range(max(0, i - 4), i + 1)
                if bars[j].close > bars[j].open
                and bars[j].ask_vol - bars[j].bid_vol < 0)
    dn_up = sum(1 for j in range(max(0, i - 4), i + 1)
                if bars[j].close < bars[j].open
                and bars[j].ask_vol - bars[j].bid_vol > 0)
    d.persist_abs_sell = up_dn >= 3
    d.persist_abs_buy  = dn_up >= 3

    ds  = 0
    if d.trend_div_bull:   ds += 2
    if d.trend_div_bear:   ds -= 2
    if d.swing_div_bull:   ds += 2
    if d.swing_div_bear:   ds -= 2
    if d.persist_abs_buy:  ds += 1
    if d.persist_abs_sell: ds -= 1
    d.strength = max(-5, min(5, ds))
    return d

# ─────────────────────────────────────────────────────────────────────────────
#  TRAP STATE  (M5)  — mirrors TrapState / trap detection in IOF_NQ_Autopilot.cpp
# ─────────────────────────────────────────────────────────────────────────────
TB_MAX = 0.35   # trap body max ratio (C++ pAT->trapBodyMax() default)

@dataclass
class Trap:
    phase:            int   = 0    # 0=inactive 1=commit 2=absorb 3=armed
    direction:        int   = 0    # +1=bull-trap, -1=bear-trap
    valid:            bool  = False
    commit_start_bar: int   = -1
    commit_bars:      int   = 0
    commit_delta:     float = 0.0
    commit_high:      float = 0.0
    commit_low:       float = 0.0
    absorb_bar:       int   = -1
    absorb_px:        float = 0.0
    stop_target:      float = 0.0
    entry_px:         float = 0.0

    def reset(self):
        self.phase = 0; self.direction = 0; self.valid = False
        self.commit_start_bar = -1; self.commit_bars = 0; self.commit_delta = 0.0
        self.commit_high = 0.0; self.commit_low = 0.0
        self.absorb_bar = -1; self.absorb_px = 0.0
        self.stop_target = 0.0; self.entry_px = 0.0

def update_trap(trap: Trap, bars: List[Bar], i: int, atr: float) -> Trap:
    if i < 2 or atr <= 0:
        return trap
    b          = bars[i]
    bar_d      = b.ask_vol - b.bid_vol
    bar_bull   = b.close > b.open
    bar_bear   = b.close < b.open
    bar_range  = b.high - b.low
    bar_body   = abs(b.close - b.open)

    # ── Phase 0: look for commit (≥2 consecutive same-dir bars) ──────────────
    if trap.phase == 0:
        if bar_bull and bar_d > 0:
            if trap.direction == +1 and trap.commit_bars > 0:
                trap.commit_bars  += 1
                trap.commit_delta += bar_d
                if b.high > trap.commit_high: trap.commit_high = b.high
            else:
                trap.direction        = +1
                trap.commit_start_bar = i
                trap.commit_bars      = 1
                trap.commit_delta     = float(bar_d)
                trap.commit_high      = b.high
                trap.stop_target      = b.low
                for k in range(1, min(11, i + 1)):
                    if bars[i - k].low < trap.stop_target:
                        trap.stop_target = bars[i - k].low
            if trap.commit_bars >= 2:
                trap.phase = 1
        elif bar_bear and bar_d < 0:
            if trap.direction == -1 and trap.commit_bars > 0:
                trap.commit_bars  += 1
                trap.commit_delta += bar_d
                if b.low < trap.commit_low: trap.commit_low = b.low
            else:
                trap.direction        = -1
                trap.commit_start_bar = i
                trap.commit_bars      = 1
                trap.commit_delta     = float(bar_d)
                trap.commit_low       = b.low
                trap.stop_target      = b.high
                for k in range(1, min(11, i + 1)):
                    if bars[i - k].high > trap.stop_target:
                        trap.stop_target = bars[i - k].high
            if trap.commit_bars >= 2:
                trap.phase = 1
        else:
            if 0 < trap.commit_bars < 2:
                trap.reset()

    # ── Phase 1: look for stall/absorption ───────────────────────────────────
    if trap.phase == 1:
        if i > trap.commit_start_bar + trap.commit_bars + 5:
            trap.reset()
        elif trap.direction == +1:
            stalled    = b.high <= trap.commit_high + TICK
            small_body = bar_range > 0 and bar_body < bar_range * TB_MAX
            diverg     = bar_d > 0 and bar_bear
            flip       = bar_d < 0
            if stalled and (small_body or diverg or flip):
                trap.phase = 2; trap.absorb_bar = i; trap.absorb_px = b.close
            elif bar_bull and bar_d > 0:
                trap.commit_bars  += 1; trap.commit_delta += bar_d
                if b.high > trap.commit_high: trap.commit_high = b.high
        elif trap.direction == -1:
            stalled    = b.low >= trap.commit_low - TICK
            small_body = bar_range > 0 and bar_body < bar_range * TB_MAX
            diverg     = bar_d < 0 and bar_bull
            flip       = bar_d > 0
            if stalled and (small_body or diverg or flip):
                trap.phase = 2; trap.absorb_bar = i; trap.absorb_px = b.close
            elif bar_bear and bar_d < 0:
                trap.commit_bars  += 1; trap.commit_delta += bar_d
                if b.low < trap.commit_low: trap.commit_low = b.low

    # ── Phase 2: look for reversal breakout ──────────────────────────────────
    if trap.phase == 2:
        if i > trap.absorb_bar + 3:
            trap.reset()
        elif trap.direction == +1:
            if b.close < trap.absorb_px - atr * 0.15 and bar_d < 0 and bar_bear:
                trap.phase = 3; trap.valid = True; trap.entry_px = b.close
        elif trap.direction == -1:
            if b.close > trap.absorb_px + atr * 0.15 and bar_d > 0 and bar_bull:
                trap.phase = 3; trap.valid = True; trap.entry_px = b.close

    # ── Phase 3: armed — reset if price reclaims absorption level ────────────
    if trap.phase == 3:
        if trap.direction == +1 and b.close > trap.absorb_px: trap.reset()
        elif trap.direction == -1 and b.close < trap.absorb_px: trap.reset()

    return trap

# ─────────────────────────────────────────────────────────────────────────────
#  BALANCE STATE  (M6 / M8)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Balance:
    mature:  bool  = False
    hi:      float = 0.0
    lo:      float = 0.0
    poc:     float = 0.0
    avg_vol: float = 0.0

def balance_state(bars: List[Bar], i: int, atr: float) -> Balance:
    b = Balance()
    if i < 10 or atr <= 0:
        return b
    lb    = 15
    start = max(0, i - lb)
    n     = i - start  # number of prior bars (excludes current)
    if n < 2:
        return b
    # Exclude current bar: balance range is from prior bars so breakout on bar i can exceed it
    hi = max(bars[j].high for j in range(start, i))
    lo = min(bars[j].low  for j in range(start, i))
    if atr * 0.3 < (hi - lo) <= atr * 2.5:
        b.mature  = True
        b.hi      = hi; b.lo = lo
        b.poc     = (hi + lo) / 2.0
        b.avg_vol = sum(bars[j].volume for j in range(start, i)) / n
    return b

# ─────────────────────────────────────────────────────────────────────────────
#  IMBALANCE STATE  (M7)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Imb:
    active:    bool  = False
    direction: int   = 0
    strength:  int   = 0
    extreme:   float = 0.0

def imbalance_state(bars: List[Bar], i: int) -> Imb:
    imb = Imb()
    if i < 5:
        return imb
    start = max(0, i - 7)
    ask = sum(bars[j].ask_vol for j in range(start, i + 1))
    bid = sum(bars[j].bid_vol for j in range(start, i + 1))
    tot = ask + bid
    if tot <= 0:
        return imb
    pct = ask / tot
    if pct >= 0.60:
        imb.active = True; imb.direction = +1
        imb.strength = int((pct - 0.50) * 20)
        imb.extreme  = max(bars[j].high for j in range(start, i + 1))
    elif pct <= 0.40:
        imb.active = True; imb.direction = -1
        imb.strength = int((0.50 - pct) * 20)
        imb.extreme  = min(bars[j].low  for j in range(start, i + 1))
    return imb

# ─────────────────────────────────────────────────────────────────────────────
#  QUALITY SCORE
# ─────────────────────────────────────────────────────────────────────────────
def qual100(sel_mode: int, final_sc: int, edge_sc: int, fade_active: bool) -> int:
    if sel_mode == 7 and fade_active:
        return min(100, max(0, edge_sc * 10))
    if 3 <= sel_mode <= 6:
        return min(100, max(0, final_sc * 10))
    return min(100, max(0, (final_sc * 100) // 15))

# ─────────────────────────────────────────────────────────────────────────────
#  RISK STATE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Risk:
    wins:        int   = 0
    losses:      int   = 0
    avg_win:     float = 0.0
    avg_loss:    float = 0.0
    consec_loss: int   = 0
    sess_pnl:    float = 0.0
    sess_peak:   float = 0.0
    sess_dd:     float = 0.0
    in_recovery: bool  = False
    opt_kelly:   float = 0.0
    prac_kelly:  float = 0.0
    rm:          float = 1.0

    def trade_done(self, pnl: float):
        self.sess_pnl  += pnl
        self.sess_peak  = max(self.sess_peak, self.sess_pnl)
        self.sess_dd    = self.sess_peak - self.sess_pnl
        if pnl > 0:
            n = self.wins
            self.avg_win = (self.avg_win * n + pnl) / (n + 1)
            self.wins += 1; self.consec_loss = 0
        else:
            n = self.losses
            self.avg_loss = (self.avg_loss * n + abs(pnl)) / (n + 1)
            self.losses += 1; self.consec_loss += 1
        if not self.in_recovery and self.sess_dd > DAILY_LOSS * 0.3:
            self.in_recovery = True
        if self.in_recovery and self.sess_pnl >= self.sess_peak * 0.8:
            self.in_recovery = False
        self._kelly(); self._rm()

    def _kelly(self):
        if self.wins < 5 or self.losses < 2:
            self.opt_kelly = self.prac_kelly = 0.0; return
        wr = self.wins / (self.wins + self.losses)
        b  = self.avg_win / self.avg_loss if self.avg_loss > 0 else 0
        self.opt_kelly  = (wr * b - (1 - wr)) / b if b > 0 else 0
        self.prac_kelly = max(0.0, min(0.25, self.opt_kelly * 0.5))

    def _rm(self):
        n = self.wins + self.losses
        if n < 10:           km = 0.90
        elif self.prac_kelly <= 0:   km = 0.25
        elif self.prac_kelly >= 0.20: km = 1.25
        elif self.prac_kelly >= 0.15: km = 1.10
        elif self.prac_kelly >= 0.10: km = 1.00
        else:                km = 0.75
        rec = 0.60 if self.in_recovery else 1.00
        rm  = max(C_RM_FLOOR, min(2.0, km * rec))
        if self.consec_loss >= 4: rm = min(rm, 0.25)
        elif self.consec_loss >= 3: rm = min(rm, 0.50)
        self.rm = rm

    def day_reset(self):
        self.sess_pnl = self.sess_peak = self.sess_dd = 0.0
        self.consec_loss = 0; self.in_recovery = False
        self._rm()

# ─────────────────────────────────────────────────────────────────────────────
#  TRADE RECORD
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    date: str = ""; time: str = ""; event: str = ""; side: str = ""
    mode: str = ""; entry: float = 0; sl: float = 0; tp1: float = 0; tp2: float = 0
    qty: int = 1; score: int = 0; ctrl: int = 0; div_str: int = 0
    delta: float = 0; bar_spd: float = 0
    exit_px: float = 0; exit_rsn: str = ""; hold: int = 0
    mae: float = 0; mfe: float = 0
    day_pnl: float = 0; tot_pnl: float = 0
    fade_edge: int = 0; fade_type: int = 0; risk_mult: float = 1.0
    trend_reg: int = 0; vol_reg: int = 1; chop_reg: int = 0

# ─────────────────────────────────────────────────────────────────────────────
#  BACKTESTER
# ─────────────────────────────────────────────────────────────────────────────
class Backtester:

    def __init__(self, bars: List[Bar]):
        self.bars   = bars
        self.n      = len(bars)
        self.out: List[Trade] = []

        # Indicators (pre-computed)
        self.atr_v:   List[float] = []
        self.vwap_v:  List[float] = []
        self.vwap_sd: List[float] = []
        self.cum_d:   List[float] = []
        self.avg_d:   List[float] = []  # EMA-64 of |bar_delta| — mirrors C++ SG_ADELT

        # Objects
        self.vp    = VP5Day()
        self.risk  = Risk()
        self.trap  = Trap()

        # Trade state
        self.in_pos     = False
        self.is_long    = False
        self.entry_px   = 0.0
        self.stop_px    = 0.0
        self.tp1_px     = 0.0
        self.tp2_px     = 0.0
        self.entry_bar  = -1
        self.t1_hit     = False
        self.t1_bar     = -1
        self.cur_trade: Optional[Trade] = None

        # MAE / MFE
        self._mae = self._mfe = 0.0

        # Session
        self.cur_date  = -1
        self.day_pnl   = 0.0
        self.day_trades = 0
        self.day_done  = False
        self.tot_pnl   = 0.0

        # Cooldowns
        self.last_trade_bar = -1
        self.last_loss_bar  = -1
        self.last_stop_bar  = -1
        self.last_stop_dir  = 0
        self.last_m5_bar    = -1
        self.prev_imb_dir   = 0
        self.prev_imb_str   = 0
        self.prev_imb_ext   = 0.0

    # ──────────────────────────────────────────────────────────────────────────
    def run(self) -> List[Trade]:
        print(f"  Pre-computing indicators ({self.n:,} bars)...")
        atr_ind   = WilderATR(ATR_PER)
        vwap_ind  = SessionVWAP()
        cd        = 0.0
        _ad_ema   = 0.0
        _ad_alpha = 2.0 / 66.0   # EMA-64, mirrors C++ SG_ADELT update
        for bar in self.bars:
            a = atr_ind.update(bar.high, bar.low, bar.close)
            v, s = vwap_ind.update(bar)
            cd += bar.ask_vol - bar.bid_vol
            bar_abs_d = abs(bar.ask_vol - bar.bid_vol)
            _ad_ema = _ad_ema + _ad_alpha * (bar_abs_d - _ad_ema) if _ad_ema > 0 else float(bar_abs_d)
            self.vp.update(bar)
            self.atr_v.append(a)
            self.vwap_v.append(v)
            self.vwap_sd.append(s)
            self.cum_d.append(cd)
            self.avg_d.append(max(1.0, _ad_ema))

        print("  Running strategy loop...")
        warmup = max(C_STRUCT_LB, ATR_PER + 5, 40)
        for i in range(warmup, self.n):
            self._step(i)

        return self.out

    # ──────────────────────────────────────────────────────────────────────────
    def _step(self, i: int):
        bar  = self.bars[i]
        atr  = self.atr_v[i]
        vwap = self.vwap_v[i]
        vsd  = self.vwap_sd[i]
        if atr <= 0:
            return

        # ── Session reset ────────────────────────────────────────────────────
        if bar.date_tag != self.cur_date:
            self.cur_date   = bar.date_tag
            self.day_pnl    = 0.0
            self.day_trades = 0
            self.day_done   = False
            self.risk.day_reset()

        # ── RTH + flatten ────────────────────────────────────────────────────
        if bar.hhmm >= FLATTEN_HHMM:
            if self.in_pos:
                self._close(i, bar.close, "FLATTEN")
            self.day_done = True
            return
        if bar.hhmm < RTH_OPEN or self.day_done:
            return

        # ── Daily caps ───────────────────────────────────────────────────────
        if DAILY_LOSS > 0 and self.day_pnl <= -DAILY_LOSS:
            if self.in_pos: self._close(i, bar.close, "DAILY_LOSS")
            self.day_done = True; return
        if DAILY_PROF > 0 and self.day_pnl >= DAILY_PROF:
            if self.in_pos: self._close(i, bar.close, "DAILY_PROFIT")
            self.day_done = True; return

        # ── Trap update ──────────────────────────────────────────────────────
        self.trap = update_trap(self.trap, self.bars, i, atr)

        # ── Trade management ─────────────────────────────────────────────────
        if self.in_pos:
            self._manage(i, bar, atr)
            return

        # ── Entry gates ──────────────────────────────────────────────────────
        if self.day_trades >= MAX_TRADES:          return
        if self.risk.sess_dd > DAILY_LOSS * 0.8:  return
        if self.risk.consec_loss >= C_MAX_LOSSES:  return
        if not self._cool_ok(i):                   return
        if self._bars_from_open(i) < C_OPEN_COOL: return

        # ── Indicators ───────────────────────────────────────────────────────
        vwap_ok  = i >= C_VWAP_MAT and vwap > 0
        dm       = i >= C_DELTA_MAT
        dlt      = bar.ask_vol - bar.bid_vol
        bull     = bar.close > bar.open
        bear     = bar.close < bar.open

        ctrl = control_score(self.bars, i, self.vp, dm)
        div  = divergence(self.bars, i, self.cum_d, atr)
        bal  = balance_state(self.bars, i, atr)
        imb  = imbalance_state(self.bars, i)

        # Directional score (count confirming delta bars over last 5)
        sc_l = sum(1 for j in range(max(0, i - 4), i + 1)
                   if self.bars[j].ask_vol > self.bars[j].bid_vol)
        sc_s = sum(1 for j in range(max(0, i - 4), i + 1)
                   if self.bars[j].bid_vol > self.bars[j].ask_vol)

        vb2u = vwap + vsd * 2 if vsd > 0 else 0.0
        vb2l = vwap - vsd * 2 if vsd > 0 else 0.0

        # ── Modes ────────────────────────────────────────────────────────────
        m2l = m2s = m3l = m3s = m4l = m4s = False
        m5l = m5s = m6l = m6s = m7l = m7s = m8l = m8s = False
        m6_sp = m6_t1 = m6_t2 = 0.0
        m7_sp = 0.0
        fade_active = False; fade_edge = fade_type = 0
        fd_sp = fd_t1 = fd_t2 = 0.0
        bk_vs = 99  # balance verify score placeholder for M8

        # M2 — VP level test
        if vwap_ok and self.vp.valid and atr > 0:
            vp_z = atr * 0.75
            for lv in [self.vp.poc, self.vp.vah, self.vp.val]:
                if lv <= 0 or abs(bar.close - lv) > vp_z:
                    continue
                if bar.low <= lv + TICK and bar.close > lv and bull and sc_l >= C_MIN_SC_ALL and ctrl >= 0:
                    m2l = True
                if bar.high >= lv - TICK and bar.close < lv and bear and sc_s >= C_MIN_SC_ALL and ctrl <= 0:
                    m2s = True

        # M3 — Consolidation breakout / rejection
        if i >= C_CONSOL_LB and atr > 0 and vwap_ok:
            rh = max(self.bars[j].high for j in range(i - C_CONSOL_LB + 1, i + 1))
            rl = min(self.bars[j].low  for j in range(i - C_CONSOL_LB + 1, i + 1))
            rw = rh - rl
            if 0 < rw <= atr * C_CONSOL_ATR:
                rm = (rh + rl) / 2
                bo = atr * 0.25
                bk_up = bar.close > rh + bo and bull and sc_l >= C_MIN_SC_ALL
                bk_dn = bar.close < rl - bo and bear and sc_s >= C_MIN_SC_ALL
                l_bnc = bar.low <= rl + TICK and bar.close > rl and bull and bar.close > rm - rw * 0.25 and sc_l >= C_MIN_SC_ALL
                u_rej = bar.high >= rh - TICK and bar.close < rh and bear and bar.close < rm + rw * 0.25 and sc_s >= C_MIN_SC_ALL
                if (bk_up or l_bnc) and ctrl >= 0: m3l = True
                if (bk_dn or u_rej) and ctrl <= 0: m3s = True

        # M4 — Sweep + reclaim
        if i >= C_SWEEP_LB and atr > 0:
            sw_lo = min(self.bars[j].low  for j in range(i - C_SWEEP_LB, i))
            sw_hi = max(self.bars[j].high for j in range(i - C_SWEEP_LB, i))
            m4_min = C_MIN_SC_ALL if abs(div.strength) >= 2 else C_MIN_SC_ALL + 1
            # [EdgeDiscovery] gate: price must be ≥0.35 ATR from VWAP
            m4_vwap_edge = (vwap <= 0) or (abs(bar.close - vwap) >= atr * 0.35)
            if m4_vwap_edge and bar.low < sw_lo - TICK and bar.close > sw_lo and bull and sc_l >= m4_min and ctrl >= 0:
                m4l = True
            if m4_vwap_edge and bar.high > sw_hi + TICK and bar.close < sw_hi and bear and sc_s >= m4_min and ctrl <= 0:
                m4s = True

        # M5 — Trap reversal
        lb_delta = self.cum_d[i] - self.cum_d[max(0, i - 15)]
        avg_d    = self.avg_d[i]   # EMA-64 of |bar_delta|, mirrors C++ SG_ADELT
        if self.trap.valid and self.trap.phase == 3:
            cool = self.last_m5_bar < 0 or i > self.last_m5_bar + C_M5_COOL
            # [EdgeDiscovery] gate: lookback cumulative delta ≥3× avg bar delta
            m5_delta_edge = abs(lb_delta) >= avg_d * 3.0
            if cool and m5_delta_edge:
                if self.trap.direction == +1 and sc_s >= C_M5_MIN_SC and ctrl <= 2:
                    m5s = True
                if self.trap.direction == -1 and sc_l >= C_M5_MIN_SC and ctrl >= -2:
                    m5l = True
                if m5l or m5s:
                    self.last_m5_bar = i

        # M6 — Balance breakout
        bk_vs = 10  # mirrors C++ bkVerifyScore=10; stays 10 if no breakout so M8 gate (<=4) stays closed
        if bal.mature and atr > 0:
            bk_t = atr * 0.30
            bk_verify = 0; break_dir = 0
            if bar.close > bal.hi + bk_t and imb.active and imb.direction == +1 and bull and sc_l >= C_MIN_SC_ALL:
                break_dir = +1
            if bar.close < bal.lo - bk_t and imb.active and imb.direction == -1 and bear and sc_s >= C_MIN_SC_ALL:
                break_dir = -1
            if break_dir != 0:
                # Verify: delta streak (3/5 bars)
                d_streak = sum(1 for k in range(5) if i - k >= 0 and (
                    (break_dir > 0 and self.bars[i-k].ask_vol > self.bars[i-k].bid_vol) or
                    (break_dir < 0 and self.bars[i-k].bid_vol > self.bars[i-k].ask_vol)))
                if d_streak >= 3: bk_verify += 2
                # Volume above balance avg
                bk3 = sum(self.bars[i-k].volume for k in range(3) if i-k >= 0) / 3
                if bk3 > bal.avg_vol * 1.3: bk_verify += 2
                # No opposing divergence
                no_div = not (div.strength <= -2 if break_dir > 0 else div.strength >= 2)
                if no_div: bk_verify += 1
                # No opposing iceberg (skipped in Python version)
                bk_verify += 1
                # Wick small
                wick = (bar.high - bar.close) if break_dir > 0 else (bar.close - bar.low)
                if wick < atr * 0.3: bk_verify += 1
                bk_vs = bk_verify

                if bk_verify >= 5:   # simplified (production = 6)
                    rw = bal.hi - bal.lo
                    if break_dir > 0:
                        m6l = True
                        m6_sp = bal.hi - atr * 0.25
                        m6_t1 = bal.hi + rw * 0.5
                        m6_t2 = bal.hi + rw
                    else:
                        m6s = True
                        m6_sp = bal.lo + atr * 0.25
                        m6_t1 = bal.lo - rw * 0.5
                        m6_t2 = bal.lo - rw

        # M7 — Auction reversal
        if imb.active:
            self.prev_imb_dir = imb.direction
            self.prev_imb_str = imb.strength
            self.prev_imb_ext = imb.extreme
        if self.prev_imb_dir != 0 and atr > 0:
            imb_dead   = (not imb.active) and self.prev_imb_str >= 3
            imb_fading = imb.active and imb.strength < self.prev_imb_str - 1
            if imb_dead or imb_fading:
                rv = 0; rev = -self.prev_imb_dir
                if i >= 3:
                    dm0 = abs(self.bars[i].ask_vol   - self.bars[i].bid_vol)
                    dm1 = abs(self.bars[i-1].ask_vol - self.bars[i-1].bid_vol)
                    dm2 = abs(self.bars[i-2].ask_vol - self.bars[i-2].bid_vol)
                    if dm0 < dm1 < dm2: rv += 1
                # VWAP ±2SD level
                ext = self.prev_imb_ext
                if self.prev_imb_dir > 0 and vb2u > 0 and ext >= vb2u - atr * 0.3: rv += 1
                elif self.prev_imb_dir < 0 and vb2l > 0 and ext <= vb2l + atr * 0.3: rv += 1
                # Divergence
                if rev > 0 and div.strength >= 2:  rv += 1
                if rev < 0 and div.strength <= -2: rv += 1
                # Trap phase
                if self.trap.phase >= 2: rv += 1
                # Volume surge
                avg_vol = sum(self.bars[j].volume for j in range(max(0,i-7), i+1)) / 8
                if bar.volume > avg_vol * 1.8: rv += 1
                if rv >= 3:   # simplified (production = 5)
                    if rev > 0:
                        m7l = True; m7_sp = self.prev_imb_ext - atr * 0.3
                    else:
                        m7s = True; m7_sp = self.prev_imb_ext + atr * 0.3
                    self.prev_imb_dir = 0

        # M8 — Fade (balance breakout fade, type 1)
        if not m6l and not m6s and bal.mature and atr > 0 and bk_vs <= 4:
            bk_t = atr * 0.30
            if bar.high > bal.hi + bk_t * 0.5 and bar.close < bal.hi + bk_t * 0.3 and bear:
                edge = 1
                if div.strength <= -2: edge += 2
                if div.persist_abs_sell: edge += 1
                if self.trap.phase >= 2 and self.trap.direction == +1: edge += 2
                if edge >= 4:
                    m8s = True; fade_active = True; fade_type = 1; fade_edge = edge
                    fd_sp = bar.high + atr * 0.3
                    fd_t1 = bal.poc; fd_t2 = bal.lo
            if not m8s and bar.low < bal.lo - bk_t * 0.5 and bar.close > bal.lo - bk_t * 0.3 and bull:
                edge = 1
                if div.strength >= 2: edge += 2
                if div.persist_abs_buy: edge += 1
                if self.trap.phase >= 2 and self.trap.direction == -1: edge += 2
                if edge >= 4:
                    m8l = True; fade_active = True; fade_type = 1; fade_edge = edge
                    fd_sp = bar.low - atr * 0.3
                    fd_t1 = bal.poc; fd_t2 = bal.hi

        # ── Priority selection (M4, M5, M6 only) ─────────────────────────────
        sel = -1; sl = False
        if   m6l: sel = 5; sl = True
        elif m6s: sel = 5
        elif m5l: sel = 4; sl = True
        elif m5s: sel = 4
        elif m4l: sel = 3; sl = True
        elif m4s: sel = 3
        if sel < 0: return

        # ── Regime filter ────────────────────────────────────────────────────
        tr, cr = self._regime(i, atr, vwap)
        ts     = self._trend_str(i, atr)
        vr     = self._vol_reg(i, atr)
        if not self._regime_ok(sel, sl, tr, cr, ts): return

        # ── RM floor ─────────────────────────────────────────────────────────
        if self.risk.rm < C_RM_FLOOR: return

        # ── Post-stop cooldown (direction-specific) ───────────────────────────
        if self.last_stop_bar >= 0 and i <= self.last_stop_bar + C_COOL_STOP:
            same = (sl and self.last_stop_dir > 0) or (not sl and self.last_stop_dir < 0)
            if same: return

        # ── Quality score ─────────────────────────────────────────────────────
        # M1/M2/M3 use (finalScore*100)/15 formula — need score >= 6 to clear floor 40.
        # Production composite includes ctrl + multiple delta/structure components.
        # Approximate with ctrl_signed + delta count (range 0-10).
        if sel <= 2:
            ctrl_signed = max(0, ctrl if sl else -ctrl)
            final_sc = (sc_l if sl else sc_s) + ctrl_signed
        else:
            final_sc = sc_l if sl else sc_s
        q = qual100(sel, final_sc, fade_edge, fade_active)
        if q < QUAL_FLOOR: return

        # ── Enter trade ───────────────────────────────────────────────────────
        self._enter(i, bar, sel, sl, atr, final_sc, ctrl, div,
                    tr, vr, cr, fade_active, fade_edge, fade_type,
                    m6_sp, m6_t1, m6_t2, m7_sp, fd_sp, fd_t1, fd_t2)

    # ──────────────────────────────────────────────────────────────────────────
    def _enter(self, i, bar, sel, sl, atr, score, ctrl, div,
               tr, vr, cr, fade_active, fade_edge, fade_type,
               m6_sp, m6_t1, m6_t2, m7_sp, fd_sp, fd_t1, fd_t2):

        ep = bar.close  # market fill on bar close

        # Stop / target
        if sel == 5 and m6_sp:
            sp, t1, t2 = m6_sp, m6_t1, m6_t2
        elif sel == 6 and m7_sp:
            sp = m7_sp
            sd = abs(ep - sp)
            t1 = ep + sd * 1.5 if sl else ep - sd * 1.5
            t2 = ep + sd * 3.0 if sl else ep - sd * 3.0
        elif sel == 7 and fade_active:
            sp, t1, t2 = fd_sp, fd_t1, fd_t2
        else:
            sd = max(C_STOP_FL, min(C_STOP_CL, atr * C_STOP_ATR))
            t1 = max(C_T1_FL,   min(C_T1_CL,   atr * C_T1_ATR))
            t2 = max(C_T2_FL,   min(C_T2_CL,   atr * C_T2_ATR))
            if sl:
                sp = round((ep - sd) / TICK) * TICK
                t1 = round((ep + t1) / TICK) * TICK
                t2 = round((ep + t2) / TICK) * TICK
            else:
                sp = round((ep + sd) / TICK) * TICK
                t1 = round((ep - t1) / TICK) * TICK
                t2 = round((ep - t2) / TICK) * TICK

        # Sanity
        min_sd = max(atr * 0.5, C_STOP_FL)
        if sl:
            if sp >= ep - TICK: sp = round((ep - min_sd) / TICK) * TICK
            if t1 <= ep + TICK: t1 = round((ep + max(atr, C_T1_FL)) / TICK) * TICK
        else:
            if sp <= ep + TICK: sp = round((ep + min_sd) / TICK) * TICK
            if t1 >= ep - TICK: t1 = round((ep - max(atr, C_T1_FL)) / TICK) * TICK

        self.in_pos    = True
        self.is_long   = sl
        self.entry_px  = ep
        self.stop_px   = sp
        self.tp1_px    = t1
        self.tp2_px    = t2
        self.entry_bar = i
        self.t1_hit    = False
        self.t1_bar    = -1
        self._mae      = 0.0
        self._mfe      = 0.0

        t = Trade(
            date       = bar.dt.strftime("%Y-%m-%d"),
            time       = bar.dt.strftime("%H:%M:%S"),
            event      = "SETUP",
            side       = "LONG" if sl else "SHORT",
            mode       = MODE_NAMES[sel],
            entry      = round(ep, 2),
            sl         = round(sp, 2),
            tp1        = round(t1, 2),
            tp2        = round(t2, 2),
            qty        = 1,
            score      = score,
            ctrl       = ctrl,
            div_str    = div.strength,
            delta      = float(bar.ask_vol - bar.bid_vol),
            bar_spd    = float(bar.volume),
            risk_mult  = round(self.risk.rm, 3),
            trend_reg  = tr,
            vol_reg    = vr,
            chop_reg   = cr,
            fade_edge  = fade_edge,
            fade_type  = fade_type,
            day_pnl    = round(self.day_pnl, 2),
            tot_pnl    = round(self.tot_pnl, 2),
        )
        self.cur_trade = t
        self.out.append(t)
        self.day_trades += 1

    # ──────────────────────────────────────────────────────────────────────────
    def _manage(self, i: int, bar: Bar, atr: float):
        t = self.cur_trade
        if t is None: return
        il = self.is_long

        # MAE / MFE
        adv = self.entry_px - bar.low  if il else bar.high - self.entry_px
        fav = bar.high - self.entry_px if il else self.entry_px - bar.low
        self._mae = max(self._mae, adv)
        self._mfe = max(self._mfe, fav)

        # Circuit-breaker
        op = (bar.close - self.entry_px) if il else (self.entry_px - bar.close)
        max_risk = max(abs(self.entry_px - self.stop_px) * 3, atr * 3) * PT_VAL
        if op * PT_VAL < -max_risk:
            self._close(i, bar.close, "CB"); return

        # T2 hit
        if not self.t1_hit:
            if (il and bar.high >= self.tp2_px) or (not il and bar.low <= self.tp2_px):
                self._close(i, self.tp2_px, "T2"); return

        # T1 hit
        if not self.t1_hit:
            if (il and bar.high >= self.tp1_px) or (not il and bar.low <= self.tp1_px):
                self.t1_hit = True; self.t1_bar = i
                buf = atr * C_BE_ATR
                if il:
                    self.stop_px = round((self.entry_px + buf) / TICK) * TICK
                else:
                    self.stop_px = round((self.entry_px - buf) / TICK) * TICK

        # Trail (after T1, single-lot delay = 3×)
        if self.t1_hit:
            delay = C_TRAIL_DLY * 3
            if self.t1_bar >= 0 and i >= self.t1_bar + delay:
                t1d = abs(self.tp1_px - self.entry_px)
                cur = (bar.close - self.entry_px) if il else (self.entry_px - bar.close)
                bt  = C_TRAIL_ATR * 2.5 if (t1d > 0 and cur < t1d * 2) else C_TRAIL_ATR
                td  = atr * bt
                if t1d > 0 and cur > t1d * 2: td = min(td, atr * 0.75)
                min_sp = round((self.entry_px + atr * 0.3) / TICK) * TICK if il \
                         else round((self.entry_px - atr * 0.3) / TICK) * TICK
                if il:
                    ns = round((bar.close - td) / TICK) * TICK
                    self.stop_px = max(max(self.stop_px, ns), min_sp)
                else:
                    ns = round((bar.close + td) / TICK) * TICK
                    self.stop_px = min(min(self.stop_px, ns), min_sp)
            # Trail stop hit
            if i > self.entry_bar + 3:
                trail_hit = (il and bar.low <= self.stop_px + TICK) or \
                            (not il and bar.high >= self.stop_px - TICK)
                if trail_hit:
                    self._close(i, self.stop_px, "TRAIL"); return

        # Stop hit
        if not self.t1_hit and i > self.entry_bar + 3:
            sth = (il and bar.low <= self.stop_px + TICK) or \
                  (not il and bar.high >= self.stop_px - TICK)
            if sth:
                self._close(i, self.stop_px, "STOP"); return

    # ──────────────────────────────────────────────────────────────────────────
    def _close(self, i: int, ex_px: float, reason: str):
        if not self.in_pos or not self.cur_trade: return
        bar = self.bars[i]
        il  = self.is_long
        pnl = ((ex_px - self.entry_px) if il else (self.entry_px - ex_px)) * PT_VAL - COMMISSION

        self.day_pnl += pnl
        self.tot_pnl += pnl

        if pnl < 0 and reason in ("STOP", "CB", "TRAIL"):
            self.last_stop_bar = i
            self.last_stop_dir = 1 if il else -1
        if pnl < 0: self.last_loss_bar = i
        self.last_trade_bar = i
        self.risk.trade_done(pnl)

        t = self.cur_trade
        t.event    = "EXIT"
        t.exit_px  = round(ex_px, 2)
        t.exit_rsn = reason
        t.hold     = i - self.entry_bar
        t.mae      = round(self._mae, 2)
        t.mfe      = round(self._mfe, 2)
        t.day_pnl  = round(self.day_pnl, 2)
        t.tot_pnl  = round(self.tot_pnl, 2)
        self.out.append(t)

        self.in_pos = False; self.cur_trade = None
        self.t1_hit = False; self.t1_bar = -1

    # ──────────────────────────────────────────────────────────────────────────
    def _cool_ok(self, i: int) -> bool:
        if self.last_trade_bar < 0: return True
        cd = C_COOL_LOSS if (self.last_loss_bar >= 0 and
                             self.last_loss_bar >= self.last_trade_bar) else C_COOL_TRADE
        return i > self.last_trade_bar + cd

    def _bars_from_open(self, i: int) -> int:
        date = self.bars[i].date_tag
        count = 0
        for j in range(i, max(0, i - C_OPEN_COOL * 2), -1):
            if self.bars[j].date_tag != date: break
            if self.bars[j].hhmm >= RTH_OPEN: count += 1
        return count

    def _regime(self, i: int, atr: float, vwap: float) -> Tuple[int, int]:
        if i < 20 or atr <= 0: return 0, 0
        lb = min(20, i)
        pc = self.bars[i].close - self.bars[i - lb].close
        dp = sum(abs(self.bars[j].ask_vol - self.bars[j].bid_vol)
                 for j in range(i - lb + 1, i + 1)) / lb
        ts = dp / (atr * 0.1) if atr > 0 else 0
        tr = (1 if ts > 1.5 and pc > atr * 0.5 else
              -1 if ts > 1.5 and pc < -atr * 0.5 else 0)
        crosses = sum(1 for k in range(1, min(12, i))
                      if ((self.bars[i-k].close - vwap) *
                          (self.bars[i-k-1].close - vwap)) < 0)
        cs = crosses / 12.0
        cr = 3 if cs > 0.85 else 2 if cs > 0.7 else 1 if cs > 0.4 else 0
        return tr, cr

    def _trend_str(self, i: int, atr: float) -> float:
        if i < 5 or atr <= 0: return 0.0
        lb = min(20, i)
        dp = sum(abs(self.bars[j].ask_vol - self.bars[j].bid_vol)
                 for j in range(i - lb + 1, i + 1)) / lb
        return dp / (atr * 0.1)

    def _vol_reg(self, i: int, atr: float) -> int:
        if i < 30 or atr <= 0: return 1
        base = sum(self.atr_v[j] for j in range(max(0, i-30), i)) / 30
        r = atr / base if base > 0 else 1.0
        return 0 if r < 0.7 else 1 if r < 1.3 else 2 if r < 2.0 else 3

    @staticmethod
    def _regime_ok(mode: int, long: bool, tr: int, cr: int, ts: float) -> bool:
        if mode == 0 and cr >= 2 and ts < 0.3: return False
        if mode == 0 and ts > 2.5:             return False
        if mode == 2 and ts > 4.5:             return False
        if mode == 3 and cr >= 2 and ts < 1.0: return False
        if mode == 4 and cr >= 3 and ts < 0.5: return False
        if mode == 5 and cr >= 3 and ts < 0.3: return False
        if mode == 6 and ts > 5.0:             return False
        return True

# ─────────────────────────────────────────────────────────────────────────────
#  CSV OUTPUT
# ─────────────────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "Date","Time","Event","Side","Mode","Entry","SL","TP1","TP2","Qty","Score",
    "CtrlScore","DivStr","Delta","BarSpeed","ExitPx","ExitReason","HoldBars",
    "MAE","MFE","DayPnL","TotalPnL","FadeEdge","FadeType","RiskMult",
    "TrendReg","VolReg","ChopReg","Version",
]

def write_csv(trades: List[Trade], path: str):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for t in trades:
            w.writerow({
                "Date": t.date, "Time": t.time, "Event": t.event,
                "Side": t.side, "Mode": t.mode, "Entry": t.entry,
                "SL": t.sl, "TP1": t.tp1, "TP2": t.tp2, "Qty": t.qty,
                "Score": t.score, "CtrlScore": t.ctrl, "DivStr": t.div_str,
                "Delta": t.delta, "BarSpeed": t.bar_spd,
                "ExitPx": t.exit_px, "ExitReason": t.exit_rsn,
                "HoldBars": t.hold, "MAE": t.mae, "MFE": t.mfe,
                "DayPnL": t.day_pnl, "TotalPnL": t.tot_pnl,
                "FadeEdge": t.fade_edge, "FadeType": t.fade_type,
                "RiskMult": t.risk_mult, "TrendReg": t.trend_reg,
                "VolReg": t.vol_reg, "ChopReg": t.chop_reg,
                "Version": "v12.19-py",
            })

# ─────────────────────────────────────────────────────────────────────────────
#  SUMMARY PRINTER
# ─────────────────────────────────────────────────────────────────────────────
def print_summary(trades: List[Trade]):
    exits = [t for t in trades if t.event == "EXIT"]
    if not exits:
        print("  No completed trades."); return

    pnls: List[float] = []
    prev = 0.0
    for t in exits:
        pnls.append(t.tot_pnl - prev); prev = t.tot_pnl

    wins   = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    total  = len(pnls)
    wr     = wins / total if total > 0 else 0
    net    = exits[-1].tot_pnl
    avg_w  = sum(p for p in pnls if p > 0) / wins   if wins   > 0 else 0
    avg_l  = sum(p for p in pnls if p < 0) / losses if losses > 0 else 0
    pf     = (avg_w * wins) / abs(avg_l * losses) if losses > 0 and avg_l != 0 else float("inf")

    by_mode: dict = {}
    for t, p in zip(exits, pnls):
        by_mode.setdefault(t.mode, []).append(p)

    by_reason: dict = {}
    for t in exits:
        by_reason[t.exit_rsn] = by_reason.get(t.exit_rsn, 0) + 1

    print("\n" + "=" * 62)
    print("  IOF NQ AUTOPILOT — BACKTEST SUMMARY  (NQZ25-CME)")
    print("=" * 62)
    print(f"  Total trades    : {total}")
    print(f"  Win rate        : {wr:.1%}  ({wins}W / {losses}L)")
    print(f"  Net P&L         : ${net:,.2f}")
    print(f"  Avg win         : ${avg_w:,.2f}")
    print(f"  Avg loss        : ${avg_l:,.2f}")
    print(f"  Profit factor   : {pf:.2f}")
    print("-" * 62)
    print(f"  {'Mode':<8} {'Trades':>7} {'WR':>8} {'Net P&L':>11}")
    for m in sorted(by_mode):
        ps = by_mode[m]; n = len(ps); w = sum(1 for p in ps if p > 0)
        print(f"  {m:<8} {n:>7} {w/n:>8.1%} {sum(ps):>11,.2f}")
    print("-" * 62)
    print("  Exit reasons:", ", ".join(f"{k}:{v}" for k, v in sorted(by_reason.items())))
    print("=" * 62)

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    scid = sys.argv[1] if len(sys.argv) > 1 else SCID_PATH
    out  = sys.argv[2] if len(sys.argv) > 2 else OUT_CSV

    # Extract if still zipped
    if not os.path.exists(scid) and os.path.exists(ZIP_PATH):
        print(f"Extracting {ZIP_PATH} ...")
        with zipfile.ZipFile(ZIP_PATH, "r") as zf:
            zf.extractall(BASE_DIR)
        print(f"  Extracted to {BASE_DIR}")

    if not os.path.exists(scid):
        print(f"ERROR: {scid} not found."); sys.exit(1)

    fsize = os.path.getsize(scid)
    nrecs = (fsize - SCID_HDR_SIZE) // SCID_REC_SIZE
    print(f"Reading {scid}")
    print(f"  File size: {fsize/1e9:.2f} GB  ({nrecs:,} records)")
    recs = read_scid(scid)
    print(f"  Loaded {len(recs):,} records.")

    print(f"Building {TARGET_VOL}-contract volume bars ...")
    bars = build_volume_bars(recs)
    rth  = [b for b in bars if b.hhmm >= RTH_OPEN and b.hhmm < FLATTEN_HHMM]
    print(f"  Total bars: {len(bars):,}  |  RTH bars: {len(rth):,}")

    print("Running backtest ...")
    bt     = Backtester(bars)
    trades = bt.run()
    exits  = sum(1 for t in trades if t.event == "EXIT")
    print(f"  Generated {len(trades):,} records  ({exits} completed trades)")

    write_csv(trades, out)
    print_summary(trades)
    print(f"\nCSV written to: {out}")


if __name__ == "__main__":
    main()
