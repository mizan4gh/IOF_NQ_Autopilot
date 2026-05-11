#!/usr/bin/env python3
"""
IOF NQ EdgeDiscovery Backtest  v2.0

Faithfully replicates IOF_NQ_EdgeDiscovery.cpp signal logic, then fires
simulated trades with an adaptive regime detector that switches between
MOMENTUM (follow the signal) and FADE (counter the signal) per bar.

Regime detector (two independent factors, OR logic):
  Factor 1 — ATR expansion: if today's session ATR > REGIME_ATR_THRESH x
             rolling 20-session ATR EMA -> high-volatility/spike-revert regime -> FADE
  Factor 2 — VWAP oscillation: if dist_vwap_atr changed sign >= REGIME_CROSS_THRESH
             times in the last REGIME_CROSS_LB bars -> choppy/reverting -> FADE
  Default when neither fires -> MOMENTUM

Usage:
  python edge_backtest.py                                 # NQH26 default
  python edge_backtest.py C:\\SierraChart\\Data\\NQZ5.CME.scid
  python edge_backtest.py NQZ5.CME.scid out.csv
"""

import struct, math, os, csv, sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
#  PATHS / DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR  = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final"
SCID_PATH = r"C:\SierraChart\Data\NQH26-CME.scid"
OUT_CSV   = os.path.join(BASE_DIR, "IOF_NQ_edge_backtest.csv")

# ─────────────────────────────────────────────────────────────────────────────
#  STRATEGY PARAMETERS  (mirrors C++ defaults + backtest.py conventions)
# ─────────────────────────────────────────────────────────────────────────────
TARGET_VOL     = 3000         # contracts per volume bar
RTH_OPEN       = 935          # 09:35 ET
FLATTEN_HHMM   = 1555         # 15:55 ET

# EdgeDiscovery signal thresholds
ATR_PERIOD     = 50           # Wilder ATR
ATR_FLOOR      = 0.75
CUM_DZ_LB      = 200          # cum-delta Z lookback bars
CUM_DZ_MIN     = 20           # min bars for Z calc
SIG_DIST       = 0.35         # |dist_vwap_atr| threshold
SIG_Z          = 1.0          # |cum_delta_z| threshold
SIG_TIME_START = 1005         # skip first 30 min
SIG_TIME_END   = 1455         # skip last 60 min

# Trade management
C_STOP_ATR   = 1.20
C_T1_ATR     = 1.25
C_T2_ATR     = 3.00
C_TRAIL_ATR  = 1.50
C_TRAIL_DLY  = 5              # bars before trail activates
C_T1_RATIO   = 0.50           # fraction closed at T1
C_COOL_TRADE = 5              # bars cooldown after any trade

# Regime detector
REGIME_ATR_LB      = 20    # sessions for rolling ATR EMA
REGIME_ATR_THRESH  = 1.4   # session ATR > thresh x long-run ATR -> FADE
REGIME_CROSS_LB    = 15    # bars to count VWAP sign changes
REGIME_CROSS_THRESH= 4     # sign changes >= this -> FADE

# Risk / daily limits
DAILY_LOSS   = 800.0
MAX_LOSSES   = 2              # consecutive loss limit
TICK         = 0.25
PT_VAL       = 20.0           # NQ $20/point
COMMISSION   = 5.0            # RT per trade

# ─────────────────────────────────────────────────────────────────────────────
#  SCID READER
# ─────────────────────────────────────────────────────────────────────────────
SCID_HDR_SIZE = 56
SCID_REC_SIZE = 40
SC_EPOCH_UTC  = datetime(1899, 12, 30, tzinfo=timezone.utc)
ET            = ZoneInfo("America/New_York")

SCID_DTYPE = np.dtype([
    ("dt",  "<i8"), ("open","<f4"), ("high","<f4"), ("low","<f4"),
    ("close","<f4"), ("n_trades","<u4"), ("tot_vol","<u4"),
    ("bid_vol","<u4"), ("ask_vol","<u4"),
])


def detect_price_scale(path: str) -> float:
    return 1.0 if "-CME" in os.path.basename(path).upper() else 100.0


def load_scid(path: str) -> np.ndarray:
    size = os.path.getsize(path)
    print(f"  File size: {size/1e9:.2f} GB  "
          f"({(size-SCID_HDR_SIZE)//SCID_REC_SIZE:,} records)  "
          f"price_scale=/{int(detect_price_scale(path))}")
    with open(path, "rb") as f:
        f.read(SCID_HDR_SIZE)
        data = np.frombuffer(f.read(), dtype=SCID_DTYPE)
    print(f"  Loaded {len(data):,} records.")
    return data


# ─────────────────────────────────────────────────────────────────────────────
#  BAR DATACLASS + BUILDER
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
    date_tag: int
    hhmm:     int


def build_volume_bars(raw: np.ndarray, path: str) -> List[Bar]:
    scale = detect_price_scale(path)
    bars: List[Bar] = []
    acc_open = acc_high = acc_low = acc_close = 0.0
    acc_vol = acc_bid = acc_ask = 0
    started = False
    for rec in raw:
        dt_utc = SC_EPOCH_UTC + __import__("datetime").timedelta(
            microseconds=int(rec["dt"]))
        dt_et  = dt_utc.astimezone(ET)
        o = float(rec["open"])  / scale
        h = float(rec["high"])  / scale
        l = float(rec["low"])   / scale
        c = float(rec["close"]) / scale
        v = int(rec["tot_vol"])
        bid = int(rec["bid_vol"]); ask = int(rec["ask_vol"])
        if v == 0: continue
        if not started:
            acc_open=o; acc_high=h; acc_low=l; acc_close=c
            acc_vol=v; acc_bid=bid; acc_ask=ask; started=True
        else:
            if h > acc_high: acc_high=h
            if l < acc_low:  acc_low=l
            acc_close=c; acc_vol+=v; acc_bid+=bid; acc_ask+=ask
        if acc_vol >= TARGET_VOL:
            hhmm = dt_et.hour*100 + dt_et.minute
            dtag = dt_et.year*10000 + dt_et.month*100 + dt_et.day
            bars.append(Bar(dt=dt_et, open=acc_open, high=acc_high,
                            low=acc_low, close=acc_close,
                            volume=acc_vol, bid_vol=acc_bid, ask_vol=acc_ask,
                            idx=len(bars), date_tag=dtag, hhmm=hhmm))
            started=False; acc_vol=0
    return bars


# ─────────────────────────────────────────────────────────────────────────────
#  WILDER ATR
# ─────────────────────────────────────────────────────────────────────────────
class WilderATR:
    def __init__(self, period: int):
        self.period = period; self.atr = 0.0
        self.prev_close: Optional[float] = None
        self.count = 0; self._buf: List[float] = []
    def update(self, high, low, close) -> float:
        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high-low, abs(high-self.prev_close), abs(low-self.prev_close))
        self.prev_close = close
        if self.count < self.period:
            self._buf.append(tr); self.count += 1
            if self.count == self.period:
                self.atr = sum(self._buf) / self.period
        else:
            self.atr = (self.atr*(self.period-1) + tr) / self.period
        raw = self.atr if self.count >= self.period else (sum(self._buf)/len(self._buf) if self._buf else tr)
        return max(raw, ATR_FLOOR, high-low, TICK*3)


# ─────────────────────────────────────────────────────────────────────────────
#  REGIME DETECTOR
# ─────────────────────────────────────────────────────────────────────────────
class RegimeDetector:
    """
    Per-bar regime label: "MOMENTUM" or "FADE".

    Factor 1 (ATR expansion):
      Track session-average ATR across last REGIME_ATR_LB sessions.
      If today's bar ATR > REGIME_ATR_THRESH * rolling_atr_ema -> FADE.

    Factor 2 (VWAP oscillation):
      Count sign changes in dist_vwap_atr over last REGIME_CROSS_LB bars.
      If >= REGIME_CROSS_THRESH -> FADE.

    Either factor alone triggers FADE (OR logic).
    """
    def __init__(self):
        self._sess_atr_sum  = 0.0   # sum of bar ATRs in current session
        self._sess_atr_cnt  = 0
        self._atr_ema       = 0.0   # EMA of daily average ATR
        self._atr_alpha     = 2.0 / (REGIME_ATR_LB + 1)
        self._prev_date     = -1
        self.regime_v: List[str] = []  # parallel to bars

    def update(self, i: int, b: "Bar", atr: float, dist_vwap_arr: List[float]) -> str:
        # Day rollover: finalise previous session ATR into EMA
        if b.date_tag != self._prev_date:
            if self._sess_atr_cnt > 0:
                sess_avg = self._sess_atr_sum / self._sess_atr_cnt
                if self._atr_ema == 0.0:
                    self._atr_ema = sess_avg
                else:
                    self._atr_ema += self._atr_alpha * (sess_avg - self._atr_ema)
            self._sess_atr_sum = 0.0
            self._sess_atr_cnt = 0
            self._prev_date = b.date_tag

        if b.hhmm >= RTH_OPEN:
            self._sess_atr_sum += atr
            self._sess_atr_cnt += 1

        # Factor 1: ATR expansion
        fade_vol = (self._atr_ema > 0 and
                    atr > REGIME_ATR_THRESH * self._atr_ema)

        # Factor 2: VWAP oscillation (sign changes in last REGIME_CROSS_LB bars)
        crossings = 0
        lo = max(0, i - REGIME_CROSS_LB)
        for j in range(lo + 1, i + 1):
            if dist_vwap_arr[j] * dist_vwap_arr[j - 1] < 0:
                crossings += 1
        fade_osc = (crossings >= REGIME_CROSS_THRESH)

        regime = "FADE" if (fade_vol or fade_osc) else "MOMENTUM"
        self.regime_v.append(regime)
        return regime


# ─────────────────────────────────────────────────────────────────────────────
#  INDICATOR PRE-COMPUTE
# ─────────────────────────────────────────────────────────────────────────────
def precompute(bars: List[Bar]):
    n = len(bars)
    atr_v       = [0.0]*n
    vwap_v      = [0.0]*n
    cum_d_v     = [0.0]*n
    cum_delta_z = [0.0]*n
    dist_vwap   = [0.0]*n
    edge_sig    = [0]*n
    regime_v    = ["MOMENTUM"]*n

    atr_calc = WilderATR(ATR_PERIOD)
    regime   = RegimeDetector()
    sess_cum_pv = sess_cum_v = 0.0
    sess_cum_d  = 0.0
    cum_d_hist: List[float] = []
    prev_date = -1

    for i, b in enumerate(bars):
        atr = atr_calc.update(b.high, b.low, b.close)
        atr_v[i] = atr

        if b.date_tag != prev_date:
            prev_date = b.date_tag
            sess_cum_pv = sess_cum_v = sess_cum_d = 0.0
            cum_d_hist = []

        bar_d = b.ask_vol - b.bid_vol
        sess_cum_d  += bar_d
        sess_cum_pv += b.close * b.volume
        sess_cum_v  += b.volume
        vwap = sess_cum_pv / sess_cum_v if sess_cum_v > 0 else b.close
        vwap_v[i]   = vwap
        cum_d_v[i]  = sess_cum_d
        dv = (b.close - vwap) / atr if atr > 1e-6 else 0.0
        dist_vwap[i] = dv

        # Regime label for this bar (uses dist_vwap up to index i)
        regime_v[i] = regime.update(i, b, atr, dist_vwap)

        # Z-score of session cum delta
        if b.hhmm >= RTH_OPEN:
            cum_d_hist.append(sess_cum_d)
        lo = max(0, len(cum_d_hist)-CUM_DZ_LB)
        window = cum_d_hist[lo:]
        if len(window) >= CUM_DZ_MIN:
            m = sum(window)/len(window)
            s = math.sqrt(sum((x-m)**2 for x in window)/len(window))
            cum_delta_z[i] = (sess_cum_d - m)/s if s > 1e-6 else 0.0
        else:
            cum_delta_z[i] = 0.0

        # Edge signal (momentum: price+delta agree)
        if (b.hhmm >= SIG_TIME_START and b.hhmm <= SIG_TIME_END and
                abs(dv) >= SIG_DIST and abs(cum_delta_z[i]) >= SIG_Z and
                dv * cum_delta_z[i] > 0):
            edge_sig[i] = 1 if dv > 0 else -1

    return atr_v, vwap_v, cum_d_v, cum_delta_z, dist_vwap, edge_sig, regime_v


# ─────────────────────────────────────────────────────────────────────────────
#  TRADE RECORD
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    entry_bar:  int
    entry_dt:   str
    direction:  int       # +1=long, -1=short
    entry_px:   float
    stop_px:    float
    t1_px:      float
    t2_px:      float
    exit_bar:   int   = -1
    exit_dt:    str   = ""
    exit_px:    float = 0.0
    exit_reason:str   = ""
    qty:        int   = 1
    t1_hit:     bool  = False
    pnl:        float = 0.0
    mode:       str   = "ED"   # EdgeDiscovery
    regime:     str   = "MOMENTUM"


# ─────────────────────────────────────────────────────────────────────────────
#  STRATEGY LOOP
# ─────────────────────────────────────────────────────────────────────────────
def run_strategy(bars: List[Bar], atr_v, vwap_v, cum_d_v, cum_dz, dist_vwap,
                 edge_sig, regime_v: List[str]) -> List[Trade]:
    trades: List[Trade] = []
    in_pos       = False
    pos_dir      = 0
    entry_px     = stop_px = t1_px = t2_px = 0.0
    entry_bar    = -1
    t1_hit       = False
    trail_px     = 0.0
    trail_active = False
    last_trade_bar = -1
    day_pnl      = 0.0
    day_done     = False
    consec_loss  = 0
    prev_date    = -1

    entry_regime = "MOMENTUM"

    def close_trade(i, px, reason):
        nonlocal in_pos, pos_dir, t1_hit, trail_active
        nonlocal day_pnl, consec_loss, day_done
        b = bars[i]
        pnl_pts = (px - entry_px) * pos_dir
        pnl_dollar = pnl_pts * PT_VAL - COMMISSION
        day_pnl += pnl_dollar
        t = Trade(
            entry_bar=entry_bar,
            entry_dt=bars[entry_bar].dt.strftime("%Y-%m-%d %H:%M"),
            direction=pos_dir,
            entry_px=entry_px,
            stop_px=stop_px,
            t1_px=t1_px,
            t2_px=t2_px,
            exit_bar=i,
            exit_dt=b.dt.strftime("%Y-%m-%d %H:%M"),
            exit_px=px,
            exit_reason=reason,
            t1_hit=t1_hit,
            pnl=round(pnl_dollar, 2),
            regime=entry_regime,
        )
        trades.append(t)
        if pnl_dollar < 0:
            consec_loss += 1
        else:
            consec_loss = 0
        if day_pnl <= -DAILY_LOSS:
            day_done = True
        in_pos = False; pos_dir = 0; t1_hit = False
        trail_active = False

    for i, b in enumerate(bars):
        if b.date_tag != prev_date:
            prev_date = b.date_tag
            day_pnl   = 0.0
            day_done  = False
            consec_loss = 0

        if b.hhmm < RTH_OPEN:
            continue

        atr = atr_v[i]

        # Flatten
        if b.hhmm >= FLATTEN_HHMM and in_pos:
            close_trade(i, b.close, "FLATTEN")
            continue

        # Manage open position
        if in_pos:
            bars_held = i - entry_bar

            # Trail activation
            if t1_hit and not trail_active and bars_held >= C_TRAIL_DLY:
                trail_active = True
                trail_px = (b.close - atr * C_TRAIL_ATR * pos_dir)

            if trail_active:
                new_trail = b.close - atr * C_TRAIL_ATR * pos_dir
                if pos_dir == +1 and new_trail > trail_px: trail_px = new_trail
                if pos_dir == -1 and new_trail < trail_px: trail_px = new_trail

            # T2 check (full exit)
            if pos_dir == +1 and b.high >= t2_px:
                close_trade(i, t2_px, "T2"); last_trade_bar = i; continue
            if pos_dir == -1 and b.low  <= t2_px:
                close_trade(i, t2_px, "T2"); last_trade_bar = i; continue

            # T1 check (mark T1 hit; for qty=1 this just sets flag for trail)
            if not t1_hit:
                if pos_dir == +1 and b.high >= t1_px: t1_hit = True
                if pos_dir == -1 and b.low  <= t1_px: t1_hit = True

            # Trail stop
            if trail_active:
                if pos_dir == +1 and b.low <= trail_px:
                    close_trade(i, trail_px, "TRAIL"); last_trade_bar = i; continue
                if pos_dir == -1 and b.high >= trail_px:
                    close_trade(i, trail_px, "TRAIL"); last_trade_bar = i; continue

            # Hard stop
            if pos_dir == +1 and b.low <= stop_px:
                close_trade(i, stop_px, "STOP"); last_trade_bar = i; continue
            if pos_dir == -1 and b.high >= stop_px:
                close_trade(i, stop_px, "STOP"); last_trade_bar = i; continue

            continue  # still in trade, no new entry

        # Entry gates
        if day_done: continue
        if consec_loss >= MAX_LOSSES: continue
        if edge_sig[i] == 0: continue
        if last_trade_bar >= 0 and i < last_trade_bar + C_COOL_TRADE: continue

        sig = edge_sig[i]
        cur_regime = regime_v[i]
        if cur_regime == "FADE":
            sig = -sig   # counter-trend

        entry_dir = sig   # +1=long, -1=short
        ep   = b.close
        sp   = ep - atr * C_STOP_ATR  * entry_dir
        tp1  = ep + atr * C_T1_ATR    * entry_dir
        tp2  = ep + atr * C_T2_ATR    * entry_dir

        in_pos       = True
        pos_dir      = entry_dir
        entry_px     = ep; stop_px = sp; t1_px = tp1; t2_px = tp2
        entry_bar    = i
        entry_regime = cur_regime
        t1_hit       = False
        trail_active = False
        trail_px     = sp

    # Close any open at end
    if in_pos and bars:
        close_trade(len(bars)-1, bars[-1].close, "EOD")

    return trades


# ─────────────────────────────────────────────────────────────────────────────
#  SUMMARY PRINTER
# ─────────────────────────────────────────────────────────────────────────────
def print_summary(trades: List[Trade], sym: str, label: str):
    n = len(trades)
    if n == 0:
        print(f"  {label}: 0 trades")
        return
    wins  = [t for t in trades if t.pnl > 0]
    loses = [t for t in trades if t.pnl <= 0]
    net   = sum(t.pnl for t in trades)
    aw    = sum(t.pnl for t in wins) / len(wins) if wins else 0
    al    = sum(t.pnl for t in loses) / len(loses) if loses else 0
    gw    = sum(t.pnl for t in wins)
    gl    = abs(sum(t.pnl for t in loses))
    pf    = gw / gl if gl > 0 else float("inf")

    exits = {}
    for t in trades:
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
    exit_str = "  ".join(f"{k}:{v}" for k, v in sorted(exits.items()))

    # Regime breakdown
    regimes = {}
    for t in trades:
        r = t.regime
        if r not in regimes:
            regimes[r] = {"n": 0, "pnl": 0.0, "wins": 0}
        regimes[r]["n"]    += 1
        regimes[r]["pnl"]  += t.pnl
        regimes[r]["wins"] += 1 if t.pnl > 0 else 0

    print()
    print("=" * 62)
    print(f"  EDGE DISCOVERY BACKTEST -- ADAPTIVE REGIME  ({sym})")
    print("=" * 62)
    print(f"  Total trades    : {n}")
    print(f"  Win rate        : {len(wins)/n*100:.1f}%  ({len(wins)}W / {len(loses)}L)")
    print(f"  Net P&L         : ${net:,.2f}")
    print(f"  Avg win         : ${aw:,.2f}")
    print(f"  Avg loss        : ${al:,.2f}")
    print(f"  Profit factor   : {pf:.2f}")
    print(f"  Exit reasons    : {exit_str}")
    print("-" * 62)
    print(f"  {'Regime':<12}  {'Trades':>6}  {'WR':>6}  {'Net P&L':>10}")
    for r, d in sorted(regimes.items()):
        wr = d["wins"] / d["n"] * 100 if d["n"] else 0
        print(f"  {r:<12}  {d['n']:>6}  {wr:>5.1f}%  ${d['pnl']:>9,.2f}")
    print("=" * 62)


# ─────────────────────────────────────────────────────────────────────────────
#  CSV WRITER
# ─────────────────────────────────────────────────────────────────────────────
def write_csv(trades: List[Trade], path: str):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_dt","exit_dt","direction","regime","entry_px","stop_px",
                    "t1_px","t2_px","exit_px","exit_reason","t1_hit","pnl"])
        for t in trades:
            w.writerow([t.entry_dt, t.exit_dt,
                        "L" if t.direction==1 else "S",
                        t.regime,
                        t.entry_px, t.stop_px, t.t1_px, t.t2_px,
                        t.exit_px, t.exit_reason, t.t1_hit, t.pnl])
    print(f"  CSV: {path}")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    scid = sys.argv[1] if len(sys.argv) > 1 else SCID_PATH
    out  = sys.argv[2] if len(sys.argv) > 2 else OUT_CSV

    if not os.path.isabs(scid) and not os.path.exists(scid):
        scid = os.path.join(r"C:\SierraChart\Data", scid)
    if not os.path.isabs(out):
        out  = os.path.join(BASE_DIR, out)

    sym = os.path.splitext(os.path.basename(scid))[0]

    print(f"Reading {scid}")
    raw  = load_scid(scid)

    print(f"Building {TARGET_VOL}-contract volume bars ...")
    bars = build_volume_bars(raw, scid)
    rth  = sum(1 for b in bars if b.hhmm >= RTH_OPEN)
    print(f"  Total bars: {len(bars):,}  |  RTH bars: {rth:,}")

    print("Pre-computing indicators ...")
    atr_v, vwap_v, cum_d_v, cum_dz, dist_vwap, edge_sig, regime_v = precompute(bars)
    sigs  = sum(1 for s in edge_sig if s != 0)
    n_mom = sum(1 for r in regime_v if r == "MOMENTUM")
    n_fad = sum(1 for r in regime_v if r == "FADE")
    print(f"  Edge signals: {sigs:,}  |  regime bars: {n_mom:,} MOMENTUM / {n_fad:,} FADE")

    # ── Adaptive: regime_v decides direction per bar
    print("Running backtest (ADAPTIVE REGIME) ...")
    trades = run_strategy(bars, atr_v, vwap_v, cum_d_v, cum_dz,
                          dist_vwap, edge_sig, regime_v)
    print_summary(trades, sym, "ADAPTIVE")
    write_csv(trades, out)


if __name__ == "__main__":
    main()
