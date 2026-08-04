#!/usr/bin/env python3
"""
IOF NQ Autopilot — Standalone Python Backtester  v1.2
Reads NQZ25-CME.scid (or extracts from .zip), builds 3000-contract volume bars,
runs strategy logic faithful to IOF_NQ_Autopilot.cpp v12.25, writes CSV journal.

Mirrored behavioural changes from cpp v12.20 → v12.25:
  - v12.21/v12.24  M5 (Trap Reversal) removed
  - v12.24         M7 (Auction Reversal) removed
  - v12.22         M1 VWAP-slope directional filter (sOKL/sOKS)
  - v12.24         M1 dead-zone 12:00–13:59 ET
  - v12.24         M1 directional trend gate (20-bar close vs ATR*0.5)
  - v12.23/09e31b2 Flatten HHMM = 1555, C_MAX_LOSSES = 2
  - v12.25         CtrlScore gates: M3 (already gated), M6 soft (±1),
                   M8 strict (±0). Mirrors cpp v12.25 fixes derived from
                   2026-05-13 live-trade analysis.

Knobs at top of file (defaults disable caps + filters for unrestricted sweeps):
  DAILY_LOSS / DAILY_PROF / NEWS_FILTER / ENTRY_ORD

Usage:
    python backtest.py                              # uses defaults below
    python backtest.py NQZ25-CME.scid out.csv       # override paths
"""

import struct, math, os, csv, sys, zipfile
from dataclasses import dataclass, field, replace
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
TARGET_VOL   = 3000          # contracts per bar
RTH_OPEN     = 935           # 09:35 ET — RTH only
FLATTEN_HHMM = 1555          # 15:55 ET
# [v12.36-ab candidate] Skip new SETUPs at/after this HHMM. Default = FLATTEN_HHMM
# (off — only blocks the bar where flatten itself runs). Set to 1500 to test the
# R2b late-entry rule from [[project_r2b_late_entry_skip]]: ≤55 min before flatten
# is too little runway for reversal modes to reach T2/trail.
LATE_ENTRY_GATE = FLATTEN_HHMM

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
QUAL_FLOOR   = 50   # [v12.30 cpp; v12.26 lowered to 40, reverted after cross-contract A/B]
QUAL_FLOOR_M1 = None  # if set, overrides QUAL_FLOOR for M1 (sel==0) only — used by backtest_m1_qf40_ab.py
QUAL_FLOOR_M2 = None  # if set, overrides QUAL_FLOOR for M2 (sel==1) only — used by backtest_m2_qual25_ab.py
QUAL_FLOOR_M3 = None  # if set, overrides QUAL_FLOOR for M3 (sel==2) only
QUAL_FLOOR_M4 = None  # if set, overrides QUAL_FLOOR for M4 (sel==3) only — used by backtest_m4floor60_ab.py
# [armdiag] If set to a path, main() writes one row per bar on which ANY mode
# armed, BEFORE the priority cascade: Date,Time,BarIdx,Armed,Sel,SelSide.
# Identical schema to the cpp WriteArmed() (IOF_NQ_Armed_<sym>.csv) so live and
# backtest arm rates are directly comparable. The live SETUP journal only ever
# records the priority WINNER, which is why the two logs cannot currently be
# reconciled (live ~89% M4 vs backtest ~3% M4 taken trades). Diagnostic only —
# does not affect selection or any gate. Written by arm_audit.py.
ARM_CSV = None
QUAL_FLOOR_M5 = None  # if set, overrides QUAL_FLOOR for M5 (sel==4) only — used by backtest_m5m7_floor_ab.py
QUAL_FLOOR_M6 = None  # if set, overrides QUAL_FLOOR for M6 (sel==5) only
QUAL_FLOOR_M7 = None  # if set, overrides QUAL_FLOOR for M7 (sel==6) only — used by backtest_m5m7_floor_ab.py
QUAL_FLOOR_M8 = None  # if set, overrides QUAL_FLOOR for M8 (sel==7) only
C_MIN_SC_M1  = 3
C_MIN_SC_ALL = 3
C_COOL_TRADE = 5
C_COOL_LOSS  = 10
C_COOL_STOP  = 10
C_OPEN_COOL  = 36
C_SPIKE_ATR_M  = 3.0   # bar range spike: range >= 3x ATR
C_VCOOL_THRESH = 7.0   # deep vol-cool: range/ATR >= 7 triggers extended pause
C_VCOOL_PAUSE  = 40    # bars suppressed by deep vol cooldown
C_SPIKE_COOL   = 20    # bars suppressed by range/delta spike state
C_VWAP_MAT   = 40
C_DELTA_MAT  = 25
# [score-port] cpp C_DELTA_LB (Autopilot.cpp:470) — lean-back delta lookback.
# Gates lbScL/lbScS, the fallback score used when this bar's own delta is flat.
C_DELTA_LB   = 15
C_CONSOL_LB  = 25
C_CONSOL_ATR = 1.5
C_SWEEP_LB   = 15
C_MAX_LOSSES = 2
C_STRUCT_LB  = 25
C_M5_MIN_SC     = 3    # min score to arm M5
C_M5_COOLDOWN   = 15   # bars between M5 arms
C_M5_PHASE3_MAX = 20   # max bars in phase 3 before reset
C_VWAP_SLP_LB   = 20   # [v12.22] VWAP slope lookback for M1 directional filter
C_VWAP_SLP_TOL  = 0.02 # [v12.22] M1 VWAP slope tolerance as ATR fraction (tight)

# [M2 geometry partition] M2 is the VP-level trigger, and its arm condition
# (bar.low <= lv + TICK) is a SUPERSET: it fires both when price merely grazes the
# level and holds ("touch"), and when price penetrates BEYOND it and closes back
# ("sweep") — M4's geometry, the one shape this strategy reliably wins on. Both
# subsets are currently pooled into M2's PF curve, which has been falsified at
# every floor 25..46. This knob partitions them so each can be measured alone.
#   "all"   = live behavior, byte-identical (default)
#   "sweep" = only arms that penetrated the level by > 1 tick (M4-like)
#   "touch" = only arms that did NOT penetrate (grazed and held)
# Hypothesis: touch has no edge (cf. the VWAP-touch engine, falsified in all 3
# flavors) and is what drags M2's curve; sweep may carry M4-like edge.
M2_GEOM = "all"

DAILY_LOSS   = 0.0           # 0 = disabled
DAILY_PROF   = 0.0           # 0 = disabled
NEWS_FILTER  = 0             # 0 = off
# [news-override-ab] If True AND NEWS_FILTER on: allow M4 "clean reclaim" shapes through
# the news blackout (M4-only, close past sweep ≥ 2 ticks, sc ≥ 4 — no div-3 fallback).
# All other modes remain blocked during news. Default off — flag-gated for A/B harness.
NEWS_OVERRIDE_M4_RECLAIM = False
ENTRY_ORD    = 2             # 0=mkt @ close, 1=lmt @ close (needs next-bar pullback), 2=lmt+2t (marketable)
MAX_TRADES   = 6

# [M4-midday parallel-admit A/B] Rescue M4 sub-floor setups (quality in
# [M4_MIDDAY_Q_MIN, floor)) ONLY inside the [START, END) HHMM ET window — the
# floor stays at its normal value for every other mode and time. Tests whether
# the isolated-candidate "M4 midday" edge (edge_subfloor_scan.py) survives the
# real MT=1 strategy's slot displacement. Default off. Harness:
# backtest_m4_midday_ab.py.
M4_MIDDAY_ADMIT = False
M4_MIDDAY_Q_MIN = 40
M4_MIDDAY_START = 1200
M4_MIDDAY_END   = 1400

# [M4-OR levels] Opening-range high/low as an additional M4 sweep-level source.
# Motivation: every level-based trigger tried here so far (M2 on POC/VAH/VAL,
# VWAP-touch in 3 flavors) used COMPUTED levels and produced no edge, while M4's
# rolling swing extremes — STRUCTURAL levels where stops actually rest — is the
# one shape that wins. ORH/ORL are structural in the same sense but were never
# tested (no opening-range code existed in this repo). This feeds them into M4's
# proven geometry: sweep past the level by >1 tick, close back inside, same
# bull/bear + sc>=m4_min + ctrl + VWAP-edge gates. NOT the retail ORB template
# (no retest/pullback entry, no scale-out — both already falsified here).
#
# Causal by construction: or_hi/or_lo accumulate only from bars CLOSING inside
# [M4_OR_START, M4_OR_END) and are consulted only on bars at/after M4_OR_END.
# M4_OR_MODE: "add"  = OR levels in addition to the rolling C_SWEEP_LB extremes
#             "only" = OR levels replace them (isolates OR-sourced edge)
# Trades armed from an OR level alone are labelled mode "M4o" for attribution.
# Harness: backtest_m4_or_ab.py.
M4_OR_LEVELS = False
M4_OR_START  = 930    # HHMM ET, inclusive
M4_OR_END    = 1000   # HHMM ET, exclusive — levels frozen and usable from here
M4_OR_MODE   = "add"

# Multi-contract TP1/TP2 scale-out (default OFF = single-lot, behaviour unchanged).
# When SCALE_OUT and the sized qty >= 2: lot1 exits at TP1, the runner exits at
# TP2 else trails after TP1; shared SL -> breakeven+buf once TP1 fills.
SCALE_OUT    = False         # True = enable multi-lot scale-out
BASE_QTY     = 1             # base contracts per entry
SIZE_BY_RM   = False         # True = qty = max(1, round(BASE_QTY * risk_mult))
DISABLE_MODES = set()        # mode indices (per MODE_NAMES) to suppress, e.g. {5}=M6

# ── [FIXED_BRACKET] Pure held-to-stop/target OCO bracket ────────────────────
# Models the IOF_MNQ_M2_M4_FixedBracket cpp variant: a fixed tick stop + fixed
# tick target attached to the whole position at entry, with NO trail / BE /
# scratch / scale-out / VP-target overrides. When on, _enter overrides sp/t1/t2
# with the fixed distances and _manage routes to _manage_fixed. Default off so
# every existing (ATR/trail) harness is byte-unchanged.
FIXED_BRACKET      = False
FIXED_STOP_TICKS   = 300      # stop distance in ticks (× TICK points)
FIXED_TARGET_TICKS = 500      # target distance in ticks
FIXED_QTY          = 3        # contracts held as one OCO bracket
# Intrabar open-P&L locks (cpp DAILY_PROF/DAILY_LOSS include OpenProfitLoss, so
# they truncate an open position at the $ threshold — modeled as a de-facto
# tighter target/stop). 0 = disabled (pure tick bracket). Dollars = account $.
FIXED_PROFIT_LOCK  = 0.0
FIXED_LOSS_LOCK    = 0.0

# ── [FIXED_SCALE] Multi-lot fixed-$ bracket with TP1/TP2 scale-out ──────────
# A 2-lot attached OCO expressed in DOLLARS PER CONTRACT rather than ticks, so
# the same numbers port between NQ ($20/pt) and MNQ ($2/pt): distance in points
# = USD / PT_VAL. Lot 1 exits at TP1, the runner at TP2, both sharing one stop.
# Differs from FIXED_BRACKET (one whole-position target, no partial) and from
# SCALE_OUT (strategy ATR/VP targets + a trailing runner): here BOTH legs are
# fixed and the runner does NOT trail, so the position is exactly what Sierra
# can attach at entry — nothing to manage after the fill.
#
# FIXED_SCALE_BE=True moves the shared stop to breakeven once TP1 fills, making
# the runner risk-free; False keeps the original stop (a true untouched OCO).
# Both are A/B'd because that is the one discretionary bit of the design.
#
# Default off -> every existing harness (ATR/trail and FIXED_BRACKET alike) is
# byte-unchanged. Harness: backtest_fixed_scale_2lot_ab.py.
FIXED_SCALE          = False
FIXED_SCALE_QTY      = 2        # contracts per entry (lot1 + runner)
FIXED_SCALE_STOP_USD = 500.0    # shared stop, $ PER CONTRACT
FIXED_SCALE_TP1_USD  = 500.0    # lot-1 target,  $ PER CONTRACT
FIXED_SCALE_TP2_USD  = 1000.0   # runner target, $ PER CONTRACT
FIXED_SCALE_BE       = False    # True = shared stop -> breakeven after TP1
# [VWAP_FILTER_ALL] Session-VWAP directional filter across EVERY mode. When on,
# a signal only survives if it is aligned with the daily VWAP: longs require
# bar.close > vwap, shorts require bar.close < vwap. Gated after the regime
# filter, before the RM/qual gates, so it counts on its own funnel line
# ("vwap_dir"). Only applies once session VWAP is valid (vwap > 0); early bars
# with no VWAP yet pass through. Default False → every existing harness stays
# byte-identical. Harness: backtest_vwap_all_m2q40_ab.py.
VWAP_FILTER_ALL = False
# [VWAP_ANCHOR_RTH] Anchor the daily/session VWAP at the RTH open (first bar with
# hhmm >= RTH_OPEN) instead of calendar midnight. Default False → VWAP keeps its
# legacy behavior (accumulates from 00:00 ET, carrying overnight volume). When
# True, SessionVWAP ignores pre-RTH bars so the VWAP resets fresh at the cash
# open — the conventional "daily VWAP" most platforms show. Affects every VWAP
# consumer (M1 bounce, M4 edge gate, VWAP_FILTER_ALL, regime). Harness:
# backtest_vwap_all_ab.py (VWAP_ANCHOR_RTH arm).
VWAP_ANCHOR_RTH = False
# [VWAP_ANCHOR_HHMM] Anchor the session VWAP at an arbitrary session-open time
# (ET HHMM) instead of RTH open or calendar midnight. Set to 1800 for the Globex
# electronic-session open (18:00 ET) — the VWAP then spans the full overnight
# session (18:00 -> next-day 17:00), resetting each day at the 18:00 crossing.
# Takes precedence over VWAP_ANCHOR_RTH. Default None → disabled.
VWAP_ANCHOR_HHMM = None
# [VWAP_NEAR_PTS] Proximity filter: when > 0, only allow an entry if the bar
# closes within VWAP_NEAR_PTS index points of the (anchored) session VWAP, i.e.
# abs(close - vwap) <= VWAP_NEAR_PTS. "Place an order only when price is very
# close to VWAP." Applies to every mode; exits unchanged. Gated after the regime
# filter (funnel line "vwap_near"). Skipped until VWAP is valid. Default 0.0 =
# off. Pair with VWAP_ANCHOR_HHMM=1800 for the Globex-anchored VWAP. Harness:
# backtest_vwap1800_near_ab.py.
VWAP_NEAR_PTS = 0.0
# [VWAP_TOUCH_TRIGGER] Standalone VWAP-touch entry ENGINE (not a filter). When
# True, the normal M1..M8 cascade is bypassed and an order is placed every time
# price RETURNS to within VWAP_NEAR_PTS of the (anchored) VWAP: long if the VWAP
# is rising over VWAP_TOUCH_SLOPE_LB bars, short if falling (flat = skip). This
# reproduces the "enter at every VWAP touch, green=long / red=short" behavior
# from the live chart. Uses the standard ATR stop + T1/T2 + trail exit machinery.
# Capped by MAX_TRADES/day and a VWAP_TOUCH_COOL-bar re-arm cooldown. Default off.
# Harness: backtest_vwap_touch_ab.py.
VWAP_TOUCH_TRIGGER  = False
VWAP_TOUCH_SLOPE_LB = 20     # bars for the VWAP-slope (trend) direction read
VWAP_TOUCH_SLOPE_TOL = 0.02  # min |slope| as ATR fraction to call rising/falling
VWAP_TOUCH_COOL     = 3      # bars to wait after an entry before re-arming
# Proximity measured in VWAP standard deviations (the SD bands on the chart):
# "close to VWAP" = abs(close - vwap) <= VWAP_NEAR_SD * vwap_sd. Takes precedence
# over VWAP_NEAR_PTS when > 0. Small values (0.25-0.5) = hug the VWAP midline.
VWAP_NEAR_SD        = 0.0
# Delta (order-flow) confirmation for the touch entry: require at least
# VWAP_TOUCH_DELTA_MIN of the last 5 bars to confirm the trade side (buyers for a
# long, sellers for a short) before entering at the VWAP. 0 = off. 3 = majority
# of last 5 bars agree. Filters VWAP touches with no flow behind them.
VWAP_TOUCH_DELTA_MIN = 0
# Require a genuine PULLBACK into the VWAP from the trend side: for a long
# (rising VWAP) price must be pulling back DOWN from above the VWAP; for a short
# (falling VWAP) pulling UP from below. Rejects momentum crosses that touch the
# VWAP from the wrong side. Default False.
VWAP_TOUCH_PULLBACK = False
# Absorption / delta-divergence gate for the touch entry (uses divergence()'s
# signed strength, −5..+5). When > 0, a long needs strength >= VWAP_TOUCH_ABSORB
# (bullish absorption: price dips to VWAP but delta holds / down-bars carry buy-
# delta), a short needs strength <= −VWAP_TOUCH_ABSORB. Opposite of delta
# confirmation — wants the pullback's flow FAILING. 1 = persist-absorb only,
# 2 = a real price/delta divergence. Default 0 = off.
VWAP_TOUCH_ABSORB = 0
# [v13] Model the v13 cpp's M8 redefinition: balance-edge fade WITHOUT the trap
# edge term (v13 removed the trap subsystem) + adds the trend-exhaustion fade
# (type 3) that backtest.py never modeled. Default off → baseline M8 unchanged,
# every existing harness stays byte-identical. NOTE: v13's *score* change (drop
# S7/S8/S10/S11) is NOT modelable here — backtest.py's score is a 5-bar delta
# count, not the cpp's 12-signal sum, so those sub-signals were never present.
V13_MODEL    = False
# [v18 M8 port] Mirror the LIVE cpp M8 "Fade Engine" in full. backtest.py only
# ever modeled fade type-1 (balance-breakout fade), which is structurally
# near-dead — type-1's bkVerifyScore<=4 gate requires a confirmed *bullish*
# breakout bar (Close>hi+bkT, bull) on the same bar its fade geometry needs a
# *bearish* reject (Close<hi+0.3bkT, bear): mutually exclusive, so bk_vs stays
# 10 and the gate never opens (true in the cpp too). The fades that actually
# fire live are type-2 (range/imbalance fade, cpp 3412-3432), type-3
# (trend-exhaustion, cpp 3433-3461) and type-4 (absorption, cpp 3462-3486).
# Setting this True ports types 2 + 4 and un-gates type 3 (otherwise V13_MODEL-
# only), so M8 fires the way it does live. Default False → every existing
# harness stays byte-identical; only backtest_m8_ab.py flips it on.
M8_FADE_FULL = False
# Per-fade-type enable filter (only meaningful when M8_FADE_FULL). Lets an A/B
# suppress individual M8 fade types, e.g. {1,3,4} to drop the Imb-corrupted
# type-2, or {4} for type-4-only. Default = all four → no behavior change.
M8_FADE_TYPES = {1, 2, 3, 4}
# [trend-long prototype] VWAP-reclaim-and-go long. Flag-gated, default OFF so
# every existing harness stays byte-identical. Rides the (disabled) M5 slot so
# it plugs into the cascade/qual/enter pipeline without a new mode index —
# requires ENABLE_M5=False (the default). See backtest_trend_long_ab.py.
TREND_LONG     = False
TREND_VWAP_LB  = 6      # bars to look back for "price was below VWAP"
TREND_T1_ATR   = 1.5
TREND_T2_ATR   = 4.0    # wide T2 so a trend winner can run (exit trail caps it)
TREND_STOP_BUF = 0.25   # swing-low stop buffer, in ATR
TREND_MIN_TS   = 0.5    # [trend filter] require trend-strength (delta flow) >= this
                        # (0.5 = data-calibrated 2026-06-29; ts weakly separates
                        # win/loss so treat as marginal, validate out-of-sample)
TREND_MAX_CHOP = 1      # [trend filter] skip reclaims when chop regime cr > this
# [trend iter-2] Mechanistic bull-trap defenses (2026-07-02). Both default OFF →
# byte-identical baseline. See backtest_trend_iter2_ab.py.
#   TREND_FAIL_EXIT — exit a pre-T1 trend-long at market ("TFAIL") when price
#     closes back below VWAP by >= 0.10*ATR. The mode's thesis IS the reclaim;
#     a close back under VWAP invalidates it, so don't ride to the wide
#     swing-low stop. Targets the trap-loss tail (full stop = large loss).
#   TREND_MIN_DEPTH — require a genuine V-shape before entry: some close within
#     the last TREND_DEPTH_LB bars must be >= TREND_MIN_DEPTH*ATR below the
#     current VWAP. Shallow-dip reclaims are chop crossings, not V-reversals.
TREND_FAIL_EXIT = False
TREND_FAIL_BUF  = 0.10  # ATR buffer below VWAP before TFAIL triggers
TREND_MIN_DEPTH = 0.0   # ATR units; 0.0 = off
TREND_DEPTH_LB  = 20    # bars scanned for the below-VWAP excursion depth
TICK         = 0.25
PT_VAL       = 20.0          # NQ: $20/point ($5/tick)
COMMISSION   = 5.0           # RT per trade
# [overnight-slip-ab] Extra slippage, ticks per side, charged on fills that occur
# before OVERNIGHT_END_HHMM ET — models the thin overnight book for the extended-
# session A/B (backtest_ext_slip_ab.py). Charged on the entry fill and on
# market-type exits (STOP/TRAIL/CB/SCRATCH/TFAIL/FLATTEN/DAILY_*); passive limit
# target fills (T1/T2) are not charged. Default 0.0 = off -> byte-identical.
OVERNIGHT_SLIP_TICKS = 0.0
OVERNIGHT_END_HHMM   = 935

# [v12.32-ab] Risk-model switch for the v12.31-vs-v12.32 cross-check.
#   "v12_32_fixed" — per-bar EMA updates, time-decay follows the 50/200-bar curve.
#   "v12_31_buggy" — emulates the cpp bug where AutoLoop=1 over-sampled the
#                    EMAs ~50-200x/bar. Dominant effect: timeDecayFactor pins
#                    to 0.5 within ~1 clock-bar after every trade; volRegime
#                    EMA collapses (atrEMA = current ATR, no smoothing).
#   "v12_32_no_rm_gate" — disable the RM<floor gate entirely (baseline behavior
#                    of pre-port backtest.py for sanity-checking).
RISK_MODEL   = "v12_32_fixed"

# [2026-07-15] Imbalance subsystem fidelity. See ImbState (~line 949).
#   "cpp_stateful"      — faithful port of live cpp ImbalanceState (struct
#                         cpp:911, update cpp:2868-2899): imbStr>=3 threshold,
#                         0.62/0.72 aggression bands, and strength-decay
#                         persistence for (startBar+barCount+3) bars.
#   "legacy_stateless"  — pre-fix behavior: score>=2, 0.55/0.65 bands, no
#                         persistence. Diverged from live; over-generated
#                         degenerate M8 type-2 fades (targets on the wrong side
#                         of entry -> instant "T2" at a loss). Kept for A/B only.
# NOTE: imb also feeds the M6 breakout confirm (imb.active/direction), so this
# flag moves M6 as well as M7/M8 — it is not an M8-only lever.
IMB_MODEL    = "cpp_stateful"

# [v12.33/34] M1 pullback confirmation mode. Mirrors cpp sc.Input[16].
#   0 = off (legacy two-bar continuation)
#   1 = wick-reject gate (failed cross-contract A/B 2026-05-22 — kept for ref)
#   2 = prior-bar dip + reclaim (default — cross-contract A/B passed 2026-05-22)
M1_PULLBACK  = 2

# [v12.20 port-back] Re-enable M5 (Trap Reversal) and M7 (Auction Reversal),
# deleted in v12.21/v12.24. M5 fires on trap.phase==3 (failed breakout reclaim);
# M7 fires on a fading imbalance at an extreme. Fidelity caveat: iceberg-based
# components of M7's 8-check verify-score are not computable in Python (no
# iceberg detection), so the "no counter-iceberg" check is granted free, biasing
# M7 to fire slightly more than the cpp would.
ENABLE_M5    = False
ENABLE_M7    = False
C_M7_MIN_VS  = 5    # M7 verify-score floor (cpp: rvVerifyScore>=5)

# [half-MFE-giveback exit] Motivated by 2026-06-08 SELL M4 NQM26: MFE hit 29pt
# (58% of TP1), then full reversal to -40.5pt stop = -$810. v12.20's NEAR-TARGET
# EXIT armed at MFE>=70% of TP1 — wouldn't have caught this. This variant arms
# on a min-MFE-in-points threshold instead, then closes on a fixed % giveback.
# Inserted in _manage BEFORE T2/T1 hit checks, only fires while t1_hit=False
# (post-T1, trail handles the trailing exit).
HALF_MFE_EXIT    = False
HALF_MFE_MIN_PTS = 10.0  # min running MFE (in points) to arm the giveback exit
HALF_MFE_GIVEBACK = 0.50 # fraction of MFE given back to trigger exit

# [early-scratch] At the last bar before the stop becomes eligible (entry+3),
# exit at close if the trade has shown no meaningful favorable excursion.
# Loser signature in prod baselines: stop-outs die with MFE <= 17 pt while
# winners reach 59+ pt. Guarded so it only fires when the close is better
# than the stop level — it can never produce a worse fill than the stop.
EARLY_SCRATCH = False
ES_AT_BAR     = 3     # check at i == entry_bar + ES_AT_BAR
ES_MFE_FRAC   = 0.25  # scratch if MFE < frac * initial stop distance

# [vp-targets] T1/T2 at volume-profile levels instead of ATR multiples.
# For a LONG: T1 = nearest of (POC, VAH, VAL) at least VP_T_MIN_ATR*ATR above
# entry, T2 = next level beyond it; mirrored for shorts. Each target falls
# back to the ATR/mode-specific value independently when no level qualifies.
# Stops and BE/trail mechanics untouched — isolates the target variable.
# Used by backtest_vp_targets_ab.py.
VP_TARGETS   = False
VP_T_MIN_ATR = 0.5   # level must be >= this * ATR beyond entry to be a target

# [v12.35] Per-bar mode-rejection diagnostic. Mirrors cpp sc.Input[26].
#   0 = off (default — zero behavioural effect)
#   1 = collect a [V18A NOFIRE] record on bars where M1/M2/M3/M4/M6 all failed
#       to arm AND at least one mode had a "shape possibility" (sweep / VP level
#       in zone / close beyond balance edge). Diagnostic-only.
LOG_NOFIRE   = 0

# STOP_MODEL — controls the per-trade stop-ceiling clamp.
#   "fixed_ceiling"   — production cpp behavior. Stop = clamp(ATR*1.2, [20, 40]).
#   "wide_for_hiconv" — high-conviction signals (score>=7 OR mode==M2) get a
#                       wider ceiling: clamp(ATR*1.2, [20, 50]). T1/T2 ceilings
#                       unchanged so this isolates the stop-ceiling variable.
#                       Motivated by 2026-05-21 NQM6 M2 BUY stop-out: MAE=36.75
#                       (under 40-pt stop by 3.25 pts), reversed to TP1.
STOP_MODEL   = "fixed_ceiling"
C_STOP_CL_HICONV = 50.0   # ceiling used when STOP_MODEL=="wide_for_hiconv" trigger fires

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
def detect_price_scale(path: str) -> float:
    """Return 100.0 for non-dash-CME Sierra Chart files (prices stored ×100)."""
    name = os.path.basename(path).upper()
    # Dash-CME files (e.g. NQZ25-CME.scid, NQH26-CME.scid) use normal prices.
    # Non-dash files (e.g. NQZ5.CME.scid) store prices ×100.
    # F.US.E* continuous-contract files (e.g. F.US.ENQM26.scid) use normal prices.
    if "-CME" in name or name.startswith("F.US.E"):
        return 1.0
    return 100.0

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
def build_volume_bars(recs: np.ndarray, target_vol: int = TARGET_VOL,
                      price_scale: float = 1.0) -> List[Bar]:
    """
    Aggregates .scid tick records into fixed-volume bars.
    Sierra Chart SCDateTime = int64 microseconds since 1899-12-30 00:00:00 UTC.
    RTH filter uses US/Eastern local time.
    price_scale: divide raw prices by this factor (100.0 for non-dash-CME files).
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
        rc    = float(rec["close"])  / price_scale
        rh_raw = float(rec["high"])  / price_scale
        rl_raw = float(rec["low"])   / price_scale
        ro_raw = float(rec["open"])  / price_scale
        # Sierra emits 0 / non-finite for OHL on ask-side single-tick records.
        # Fall back to close so the bar's running OHLC stays valid. Without the
        # low fallback, l=min(l,0)=0 corrupts TR/ATR for the whole session and
        # propagates forward through Wilder's 14-bar EMA (NQH6 bug, 2026-05-28).
        ro = rc if (not math.isfinite(ro_raw) or ro_raw == 0.0) else ro_raw
        rh = rc if (not math.isfinite(rh_raw) or rh_raw == 0.0) else rh_raw
        rl = rc if (not math.isfinite(rl_raw) or rl_raw == 0.0) else rl_raw
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
        self._last_hhmm = 99999   # [VWAP_ANCHOR_HHMM] prior bar's hhmm

    def update(self, bar: Bar) -> Tuple[float, float]:
        """Returns (vwap, sd)."""
        if VWAP_ANCHOR_HHMM is not None:
            # [VWAP_ANCHOR_HHMM] Reset at the session-open crossing (e.g. 18:00
            # Globex): the first bar whose hhmm reaches the anchor after the prior
            # bar was below it. Fires once/day and spans midnight, so the VWAP
            # covers the full overnight session. No date_tag reset here.
            if self._last_hhmm < VWAP_ANCHOR_HHMM <= bar.hhmm:
                self._spv = self._sv = self._spv2 = 0.0
            self._last_hhmm = bar.hhmm
        else:
            if bar.date_tag != self._date:
                self._spv = self._sv = self._spv2 = 0.0
                self._date = bar.date_tag
            # [VWAP_ANCHOR_RTH] Skip overnight bars so the daily VWAP anchors fresh
            # at the RTH open rather than at calendar midnight. Returns 0.0
            # (invalid) pre-RTH; the vwap_ok / `vwap > 0` guards read that as
            # "no VWAP yet".
            if VWAP_ANCHOR_RTH and bar.hhmm < RTH_OPEN:
                return 0.0, 0.0
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
    # [score-port] cpp VP_50_DAYS / SP_MAX_ZONES / SP_VOL_THRESH
    # (Autopilot.cpp:825-826, :842).
    N_DAYS_50      = 50
    SP_MAX_ZONES   = 32
    SP_VOL_THRESH  = 0.15

    def __init__(self):
        self._days: list = []
        self._cur_date   = -1
        self._cur_base   = 0.0
        self._cur_bins   = np.zeros(self.NBINS)
        self.poc = self.vah = self.val = 0.0
        self.valid = False
        # [score-port] 50-day composite (S10) + single-print zones (S7).
        self.poc50 = self.vah50 = self.val50 = 0.0
        self.valid50 = False
        self.sp_zones: list = []   # [lo, hi, filled]
        self._n = 0                # bar counter driving the recompute cadence

    def near_single_print(self, px: float, tol: float) -> bool:
        """Port of cpp nearSinglePrint() (:982-988)."""
        for z in self.sp_zones:
            if z[2]:
                continue
            if z[0] - tol <= px <= z[1] + tol:
                return True
        return False

    def _b2p(self, b: int, base: float) -> float:
        return base + (b - self.NBINS // 2) * self.BIN

    def _p2b(self, px: float, base: float) -> int:
        return max(0, min(self.NBINS - 1,
                          int((px - base) / self.BIN) + self.NBINS // 2))

    def update(self, bar: Bar):
        new_day = bar.date_tag != self._cur_date
        if new_day:
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

        # [score-port] Recompute cadence is a faithful port of cpp:2357-2361:
        #   computeLevels5()  — every 10 bars, or at session start
        #   computeLevels50() — only when VP50Pending (set at day rollover, :2296)
        # Both composites read ONLY completed days (never _cur_bins), so a per-bar
        # recompute changes nothing and is pure cost. Recomputing the 50-day every
        # bar was ~1000x the cpp's work and is what stalled the 6-contract run.
        # It also matters for correctness: _compute_composite resets the
        # single-print zones, so recomputing per bar would discard the fills that
        # update_single_prints() accumulates between recomputes.
        self._n += 1
        if new_day or self._n % 10 == 0:
            self.poc, self.vah, self.val, self.valid = \
                self._compute_composite(self.N_DAYS)
        if new_day:
            self.poc50, self.vah50, self.val50, self.valid50 = \
                self._compute_composite(self.N_DAYS_50)

    def _compute_composite(self, n_days: int):
        """Port of cpp computeComposite() (Autopilot.cpp:900-966).

        Returns (poc, vah, val, valid). Single-print zones are detected only on
        the <=5-day composite, matching the cpp's `if(numDays <= VP_5_DAYS)`
        guard — the 50-day composite is too dense to leave meaningful gaps.
        """
        days = self._days[-n_days:]
        if not days:
            return 0.0, 0.0, 0.0, False
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
            return 0.0, 0.0, 0.0, False
        poc_b = int(np.argmax(comp))
        poc   = self._b2p(poc_b, g_base)
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
        vah = self._b2p(hi, g_base)
        val = self._b2p(lo, g_base)

        if n_days <= self.N_DAYS:
            self._detect_single_prints(comp, lo, hi, g_base, float(comp[poc_b]))
        return poc, vah, val, True

    def _detect_single_prints(self, comp, lo: int, hi: int,
                              g_base: float, max_vol: float):
        """Port of the SP block inside cpp computeComposite (:937-966).

        Walks the value area for runs of low-but-nonzero volume bins and records
        each run >= 1.0 point wide as an unfilled zone (score component S7).
        """
        self.sp_zones = []
        if max_vol <= 0:
            return
        thresh = max_vol * self.SP_VOL_THRESH
        in_sp  = False
        sp_lo  = 0.0
        for b in range(lo, hi + 1):
            if len(self.sp_zones) >= self.SP_MAX_ZONES:
                return
            if 0.0 < comp[b] < thresh:
                if not in_sp:
                    in_sp = True
                    sp_lo = self._b2p(b, g_base)
            elif in_sp:
                sp_hi = self._b2p(b - 1, g_base)
                if sp_hi - sp_lo >= 1.0:
                    self.sp_zones.append([sp_lo, sp_hi, False])
                in_sp = False
        if in_sp and len(self.sp_zones) < self.SP_MAX_ZONES:
            sp_hi = self._b2p(hi, g_base)
            if sp_hi - sp_lo >= 1.0:
                self.sp_zones.append([sp_lo, sp_hi, False])

    def update_single_prints(self, hi: float, lo: float, vol: float):
        """Port of cpp updateSinglePrints() (:974-980) — a bar trading through an
        unfilled zone fills it, which retires it from scoring."""
        for z in self.sp_zones:
            if z[2]:
                continue
            if lo <= z[1] and hi >= z[0] and vol > 0:
                z[2] = True

# ─────────────────────────────────────────────────────────────────────────────
#  CONTROL SCORE
# ─────────────────────────────────────────────────────────────────────────────
class _VPView:
    """[vp-lookahead-fix] Immutable per-bar snapshot of the VP composite.

    The strategy loop MUST NOT read the live VP5Day object: `vp.update()` runs to
    completion over every bar in the pre-compute loop before the strategy loop
    starts, so `vp.poc/vah/val` hold the END-OF-FILE composite — i.e. the future.
    Snapshotting each bar's levels during the pre-compute pass is what makes the
    read causal. LIVE cpp recomputes inside its per-bar loop and was never wrong.
    """
    __slots__ = ("poc", "vah", "val", "valid",
                 "poc50", "vah50", "val50", "valid50", "sp_zones")

    def __init__(self, poc: float, vah: float, val: float, valid: bool,
                 poc50: float = 0.0, vah50: float = 0.0, val50: float = 0.0,
                 valid50: bool = False, sp_zones=()):
        self.poc = poc
        self.vah = vah
        self.val = val
        self.valid = valid
        # [score-port] S10 needs the 50-day levels and S7 the single-print zones;
        # both must be snapshotted per bar for exactly the same reason the 5-day
        # levels are — reading the live VP5Day would leak end-of-file state.
        self.poc50 = poc50
        self.vah50 = vah50
        self.val50 = val50
        self.valid50 = valid50
        self.sp_zones = sp_zones

    def near_single_print(self, px: float, tol: float) -> bool:
        for z in self.sp_zones:
            if z[2]:
                continue
            if z[0] - tol <= px <= z[1] + tol:
                return True
        return False


class IntradayLevels:
    """[score-port] Port of the cpp IntradayState feeding score component S9
    (Autopilot.cpp:3129-3139): prior session O/H/L/C plus the current session's
    open. Rolls on date_tag exactly like the cpp DAY RESET block (:2197-2212).
    """
    __slots__ = ("prev_open", "prev_high", "prev_low", "prev_close",
                 "curr_open", "valid", "_date", "_hi", "_lo", "_open", "_close")

    def __init__(self):
        self.prev_open = self.prev_high = 0.0
        self.prev_low = self.prev_close = 0.0
        self.curr_open = 0.0
        self.valid = False
        self._date = -1
        self._hi = self._lo = self._open = self._close = 0.0

    def update(self, bar):
        if bar.date_tag != self._date:
            if self._date > 0:
                self.prev_open, self.prev_high = self._open, self._hi
                self.prev_low, self.prev_close = self._lo, self._close
                self.valid = True
            self._date = bar.date_tag
            self._open = self._hi = self._lo = bar.open
            self.curr_open = bar.open
        if bar.high > self._hi: self._hi = bar.high
        if bar.low  < self._lo: self._lo = bar.low
        self._close = bar.close

    def levels(self):
        return (self.prev_open, self.prev_high, self.prev_low,
                self.prev_close, self.curr_open)

    def snapshot(self):
        """Frozen per-bar view. Needed for the same reason _VPView exists: the
        live object is advanced across every bar in the pre-compute pass, so the
        strategy loop must not read it directly."""
        return _IDLView(self.valid, self.levels())


class _IDLView:
    __slots__ = ("valid", "_levels")

    def __init__(self, valid: bool, levels: tuple):
        self.valid = valid
        self._levels = levels

    def levels(self):
        return self._levels


def iceberg_state(bars, i: int, atr: float, ice_ratio: float = 1.3):
    """[score-port] Port of the cpp ICEBERG block (Autopilot.cpp:2803-2821),
    feeding score component S8. Returns (bid_ice_px, ask_ice_px); 0.0 = absent.

    Five-bar price compression (< 0.5 ATR) carrying > 7.5x the current bar's
    volume, with one side dominating by `ice_ratio`, marks resting size.
    """
    if i < 5 or atr <= 0:
        return 0.0, 0.0
    px_min = bars[i].low
    px_max = bars[i].high
    tot_ask = tot_bid = 0.0
    for k in range(5):
        b = bars[i - k]
        if b.low  < px_min: px_min = b.low
        if b.high > px_max: px_max = b.high
        tot_ask += b.ask_vol
        tot_bid += b.bid_vol
    rng = px_max - px_min
    if not (0.0 < rng < atr * 0.5):
        return 0.0, 0.0
    if (tot_ask + tot_bid) <= bars[i].volume * 5.0 * 1.5:
        return 0.0, 0.0
    mid = (px_min + px_max) * 0.5
    bid_px = mid if tot_bid > tot_ask * ice_ratio else 0.0
    ask_px = mid if tot_ask > tot_bid * ice_ratio else 0.0
    return bid_px, ask_px


def directional_score(bars, i, atr, vwap, vsd, vp, idl, div, trap,
                      avgd, delta_mature, bull, bear, ice_bid_px, ice_ask_px):
    """[score-port] Faithful port of the cpp SCORING block (Autopilot.cpp:3100-3164).

    Returns (sc_l, sc_s, lb_sc_l, lb_sc_s).

    Replaces the harness's previous stand-in — a count of confirming delta bars
    over the last 5 — which was a DIFFERENT quantity from the cpp's scL/scS, not
    a coarser version of it. Because it was structurally capped at 5 and M4/M5/M6
    quality is `score * 10`, M4 could never exceed a quality of 50 and so could
    never clear QUAL_FLOOR=50: it armed 305 times across the 6 frozen contracts
    and took 2 trades. The same value also feeds every mode's arm threshold
    (C_MIN_SC_ALL / C_MIN_SC_M1), which were calibrated against the cpp's
    12-component score, so the mismatch was systemic rather than M4-specific.
    """
    bar   = bars[i]
    close = bar.close
    prev  = bars[i - 1] if i > 0 else bar
    dlt   = bar.ask_vol - bar.bid_vol

    # S2 — bonus: this bar's delta is at least half the EMA-64 of |delta|.
    s2 = 1 if abs(dlt) >= avgd * 0.5 else 0

    # S3 — near 5-day VP value area / POC.
    s3l = s3s = 0
    if vp is not None and vp.valid and atr > 0:
        z = atr * 0.75
        n_poc = abs(close - vp.poc) <= z
        if abs(close - vp.val) <= z or (n_poc and close > vp.poc): s3l = 1
        if abs(close - vp.vah) <= z or (n_poc and close < vp.poc): s3s = 1

    # S1 — side of session VWAP.
    s1l = 1 if (vwap > 0 and close > vwap) else 0
    s1s = 1 if (vwap > 0 and close < vwap) else 0

    # S4 — VWAP +/-2sd band rejection (2 pts) else wrong-side-of-VWAP (1 pt).
    vb2u = vwap + vsd * 2 if vsd > 0 else 0.0
    vb2l = vwap - vsd * 2 if vsd > 0 else 0.0
    uob_rej = vb2u > 0 and bar.high >= vb2u and close < vb2u and close < prev.close
    lob_bnc = vb2l > 0 and bar.low  <= vb2l and close > vb2l and close > prev.close
    s4l = 2 if lob_bnc else (1 if (vwap > 0 and close < vwap) else 0)
    s4s = 2 if uob_rej else (1 if (vwap > 0 and close > vwap) else 0)

    # S5 — swept the prior bar's extreme and closed back through it.
    sweep_min = atr * 0.15
    s5l = 1 if (bar.low  < prev.low  - sweep_min and close > prev.low
                and close > prev.close) else 0
    s5s = 1 if (bar.high > prev.high + sweep_min and close < prev.high
                and close < prev.close) else 0

    # S6 — prior bar's body opposed its delta (absorption).
    s6l = s6s = 0
    if i > 0:
        d1a = prev.ask_vol - prev.bid_vol
        s6l = 1 if (prev.close <= prev.open and d1a > 0) else 0
        s6s = 1 if (prev.close >= prev.open and d1a < 0) else 0

    # S7 — near an unfilled single-print zone (non-directional).
    s7 = 1 if (vp is not None and atr > 0
               and vp.near_single_print(close, atr * 0.5)) else 0

    # S8 — resting iceberg within half an ATR.
    s8l = 1 if (ice_bid_px > 0 and atr > 0
                and abs(close - ice_bid_px) <= atr * 0.5) else 0
    s8s = 1 if (ice_ask_px > 0 and atr > 0
                and abs(close - ice_ask_px) <= atr * 0.5) else 0

    # S9 — accepted through a prior-session level.
    s9l = s9s = 0
    if idl is not None and idl.valid and atr > 0:
        id_z = atr * 0.5
        for lv in idl.levels():
            if lv <= 0 or abs(close - lv) > id_z:
                continue
            if close > lv and bull: s9l = 1
            if close < lv and bear: s9s = 1

    # S10 — near the 50-day composite value area / POC.
    s10l = s10s = 0
    if vp is not None and vp.valid50 and atr > 0:
        z50 = atr * 0.75
        if abs(close - vp.poc50) <= z50:
            if close > vp.poc50: s10l = 1
            if close < vp.poc50: s10s = 1
        if abs(close - vp.val50) <= z50 and close > vp.val50: s10l = 1
        if abs(close - vp.vah50) <= z50 and close < vp.vah50: s10s = 1

    # S11 — a confirmed trap in the opposing direction is worth 2.
    trap_conf = trap is not None and trap.phase >= 2
    s11l = 2 if (trap_conf and trap.direction == -1) else 0
    s11s = 2 if (trap_conf and trap.direction == +1) else 0

    # S12 — cumulative-delta divergence strength.
    s12l = s12s = 0
    if delta_mature and div is not None:
        if div.strength >= 2: s12l = 1
        if div.strength >= 4: s12l = 2
        if div.strength <= -2: s12s = 1
        if div.strength <= -4: s12s = 2

    b_l = s4l + s1l + s3l + s5l + s6l + s7 + s8l + s9l + s10l + s11l + s12l
    b_s = s4s + s1s + s3s + s5s + s6s + s7 + s8s + s9s + s10s + s11s + s12s

    # cpp:3163-3164 — the score is gated on delta direction. DL/DS use this
    # bar's delta; the lean-back variants use the C_DELTA_LB-bar sum, and both
    # carry the same b_l/b_s body.
    d_l = dlt > 0
    d_s = dlt < 0
    lb_delta = 0.0
    if C_DELTA_LB > 0:
        for k in range(C_DELTA_LB):
            if i - k < 0:
                break
            lb_delta += bars[i - k].ask_vol - bars[i - k].bid_vol
    lb_l = lb_delta > 0
    lb_s = lb_delta < 0

    sc_l = (b_l + s2) if d_l else 0
    sc_s = (b_s + s2) if d_s else 0
    lb_sc_l = (b_l + s2) if lb_l else 0
    lb_sc_s = (b_s + s2) if lb_s else 0
    # Flags returned so the caller can reproduce cpp:3955-3957 exactly:
    #   finalScore = selLong ? (DL?scL:(LBL?lbScL:0)) : (DS?scS:(LBS?lbScS:0))
    # `sc_l or lb_sc_l` would differ when DL holds but the body sums to zero.
    return sc_l, sc_s, lb_sc_l, lb_sc_s, d_l, d_s, lb_l, lb_s


def _m2_geom_ok(swept: bool) -> bool:
    """[M2_GEOM] Admit an M2 arm based on whether it swept the VP level."""
    if M2_GEOM == "all":
        return True
    if M2_GEOM == "sweep":
        return swept
    if M2_GEOM == "touch":
        return not swept
    raise ValueError(f"bad M2_GEOM: {M2_GEOM!r} (want all|sweep|touch)")


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
    phase:            int   = 0    # 0=inactive 1=commit 2=absorb 3=reversal
    direction:        int   = 0    # +1=bull-trap, -1=bear-trap
    valid:            bool  = False
    commit_start_bar: int   = -1
    commit_bars:      int   = 0
    commit_delta:     float = 0.0
    commit_high:      float = 0.0
    commit_low:       float = 0.0
    absorb_bar:       int   = -1
    absorb_px:        float = 0.0
    phase3_bar:       int   = -1
    stop_target:      float = 0.0
    entry_px:         float = 0.0

    def reset(self):
        self.phase = 0; self.direction = 0; self.valid = False
        self.commit_start_bar = -1; self.commit_bars = 0; self.commit_delta = 0.0
        self.commit_high = 0.0; self.commit_low = 0.0
        self.absorb_bar = -1; self.absorb_px = 0.0; self.phase3_bar = -1
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

    # ── Phase 2: absorption — transition to phase 3 when price reverses ──────
    if trap.phase == 2:
        if trap.direction == +1 and b.close < trap.absorb_px:
            trap.phase = 3; trap.phase3_bar = i
        elif trap.direction == -1 and b.close > trap.absorb_px:
            trap.phase = 3; trap.phase3_bar = i
        elif i > trap.absorb_bar + 3:
            trap.reset()

    # ── Phase 3: trap reversal active — reclaim reset + timeout ──────────────
    if trap.phase == 3:
        if trap.direction == +1 and b.close > trap.absorb_px:  # reclaim up = trap invalid
            trap.reset()
        elif trap.direction == -1 and b.close < trap.absorb_px:  # reclaim down = trap invalid
            trap.reset()
        elif trap.phase3_bar >= 0 and i > trap.phase3_bar + C_M5_PHASE3_MAX:
            trap.reset()

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

class ImbState:
    """
    [IMB_MODEL=cpp_stateful] Faithful port of the LIVE cpp imbalance subsystem:
    struct ImbalanceState (IOF_NQ_Autopilot.cpp:911) + its per-bar update
    (cpp:2868-2899).

    The legacy stateless imbalance_state() below diverged from live in 3 ways:
      1. activation threshold score>=2   vs cpp imbStr>=3
      2. aggression bands 0.55/0.65      vs cpp 0.62/0.72  (its own docstring
         claimed "mirrors C++ 62%/72%" while the code used 0.55/0.65)
      3. NO persistence: it recomputed from scratch each bar, so `active` fell
         to False the instant the raw condition lapsed, and `strength` could
         never decay. cpp instead keeps active=True and decays strength by 1
         per bar for up to (startBar+barCount+3) before reset().

    (3) is what produced the degenerate M8 type-2 geometry: PrevImbExtreme is
    refreshed only while imb.active (cpp:3358 == backtest.py:1704/1771 — those
    two lines were always correct). With no persistence the extreme froze at the
    last raw-condition bar and went stale while price ran on, so type-2 targets
    anchored to it (cpp:3460-3466) landed on the wrong side of entry and the
    trade booked an instant "T2" loss. It also starved the `imb_fading` trigger
    (strength < prev-1), which in live fires off exactly that decay ramp.

    Guard/order note: cpp updates pImb at line 2868, i.e. AFTER the flatten /
    late-entry / open-cooldown / no-delta early-returns (cpp:2637-2669). This is
    called from the same post-gate point in _bar() so gated bars skip the update
    in both. reset() is wired to the session rollover (cpp:2192).
    """
    __slots__ = ("active", "direction", "startBar", "barCount", "strength",
                 "consecutiveAccept", "aggressionRatio", "extreme")

    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False; self.direction = 0; self.startBar = -1
        self.barCount = 0; self.strength = 0; self.consecutiveAccept = 0
        self.aggressionRatio = 0.5; self.extreme = 0.0

    def update(self, bars: List[Bar], i: int, atr: float) -> None:
        # cpp: if(isNewBar&&pImb&&Idx>=10&&ATR>0.f)
        if i < 10 or atr <= 0.0:
            return
        d0 = bars[i].ask_vol   - bars[i].bid_vol
        d1 = bars[i-1].ask_vol - bars[i-1].bid_vol
        d2 = bars[i-2].ask_vol - bars[i-2].bid_vol
        imb_str = 0; imb_dir = 0
        if d0 > d1 and d1 > d2 and d0 > 0.0: imb_str += 1; imb_dir = +1
        if d0 < d1 and d1 < d2 and d0 < 0.0: imb_str += 1; imb_dir = -1

        aV = bV = 0.0
        for k in range(5):
            if i - k < 0: break
            aV += bars[i-k].ask_vol; bV += bars[i-k].bid_vol
        tv = aV + bV
        # cpp: aggR=(tv>0)?((imbDir>=0)?(aV/tv):(bV/tv)):0.5  — note >=0, not >0
        aggR = ((aV / tv) if imb_dir >= 0 else (bV / tv)) if tv > 0.0 else 0.5
        if aggR >= 0.62: imb_str += 1
        if aggR >= 0.72: imb_str += 1

        accept = 0
        for k in range(8):
            if i - k - 1 < 0: break
            if   imb_dir > 0 and bars[i-k].close > bars[i-k-1].high: accept += 1
            elif imb_dir < 0 and bars[i-k].close < bars[i-k-1].low:  accept += 1
            else: break
        if accept >= 2: imb_str += 1
        if accept >= 4: imb_str += 1

        if imb_str >= 3 and imb_dir != 0:
            self.active = True; self.direction = imb_dir; self.strength = imb_str
            self.aggressionRatio = aggR; self.consecutiveAccept = accept
            self.barCount += 1
            if self.startBar < 0: self.startBar = i
        else:
            if self.active and self.startBar >= 0 and i <= self.startBar + self.barCount + 3:
                self.strength = self.strength - 1 if self.strength > 0 else 0
            else:
                self.reset()


def imbalance_state(bars: List[Bar], i: int) -> Imb:
    """
    [IMB_MODEL=legacy_stateless] PRE-FIX behavior, retained only so the fix can
    be A/B'd. Diverges from live cpp — see ImbState above. Do not use as the
    fidelity reference.
    """
    imb = Imb()
    if i < 5:
        return imb

    # 1. Delta streak: d[0] > d[1] > d[2] (accelerating) determines direction
    d = [bars[i-k].ask_vol - bars[i-k].bid_vol for k in range(3) if i - k >= 0]
    if len(d) < 3:
        return imb
    if d[0] > d[1] > d[2] and d[0] > 0:
        imb.direction = +1
    elif d[0] < d[1] < d[2] and d[0] < 0:
        imb.direction = -1
    else:
        return imb

    score = 1  # delta streak itself counts

    # 2. Aggression ratio over 5 bars (mirrors C++ 62%/72% thresholds)
    start5 = max(0, i - 4)
    ask5 = sum(bars[j].ask_vol for j in range(start5, i + 1))
    bid5 = sum(bars[j].bid_vol for j in range(start5, i + 1))
    tot5 = ask5 + bid5
    if tot5 > 0:
        aggR = (ask5 / tot5) if imb.direction > 0 else (bid5 / tot5)
        if aggR >= 0.55: score += 1
        if aggR >= 0.65: score += 1

    # 3. Consecutive price acceptance: close[k] > high[k-1] (bull) / < low[k-1] (bear)
    accept = 0
    for k in range(min(8, i)):
        if imb.direction > 0 and bars[i-k].close > bars[i-k-1].high:
            accept += 1
        elif imb.direction < 0 and bars[i-k].close < bars[i-k-1].low:
            accept += 1
        else:
            break
    if accept >= 2: score += 1
    if accept >= 4: score += 1

    if score >= 2:
        imb.active   = True
        imb.strength = score
        imb.extreme  = (max(bars[j].high for j in range(start5, i + 1)) if imb.direction > 0
                        else min(bars[j].low for j in range(start5, i + 1)))
    else:
        imb.direction = 0
    return imb

# ─────────────────────────────────────────────────────────────────────────────
#  NEWS FILTER  (mirrors C++ v12.8 windows)
# ─────────────────────────────────────────────────────────────────────────────
def is_news_window(hhmm: int) -> bool:
    if 825  <= hhmm <= 835:  return True   # 08:30 econ data
    if 955  <= hhmm <= 1005: return True   # 10:00 secondary releases
    if 1355 <= hhmm <= 1405: return True   # 14:00 FOMC / speakers
    return False

# ─────────────────────────────────────────────────────────────────────────────
#  QUALITY SCORE
# ─────────────────────────────────────────────────────────────────────────────
def qual100(sel_mode: int, final_sc: int, edge_sc: int, fade_active: bool) -> int:
    if sel_mode == 7 and fade_active:
        return min(100, max(0, edge_sc * 10))
    # [score-port] cpp:445-446 — ONLY the reversal modes M4-M7 use *10. M1 was
    # in this branch to compensate for the old score being capped at 5; with the
    # real 12-component score it belongs on the /15 scale like M2/M3.
    if 3 <= sel_mode <= 6:
        return min(100, max(0, final_sc * 10))
    return min(100, max(0, (final_sc * 100) // 15))

# ─────────────────────────────────────────────────────────────────────────────
#  RISK STATE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
# ─────────────────────────────────────────────────────────────────────────────
#  INSTITUTIONAL RISK STATE  (mirrors cpp lines 949-1206)
# ─────────────────────────────────────────────────────────────────────────────
#  Port of InstitutionalRiskState's RM-composition path. Only the fields that
#  feed computeRiskMultiplier are tracked; live-only stuff (VaR, slippage EMA,
#  realized-vol-target multiplier) is omitted. Multipliers we don't track in
#  Python (equityCurveMultiplier, antiMartingaleMultiplier, budgetMultiplier)
#  are pinned to 1.0 — the conservative assumption that maximizes RM, since
#  this A/B is about whether the bug *blocks* SETUPs, not the absolute level.
@dataclass
class InstRisk:
    # vol regime EMA
    baseline_atr: float = 0.0
    atr_ema:      float = 0.0
    vol_regime:   int   = 1     # 0=low, 1=normal, 2=high, 3=extreme
    # time decay
    bars_since_last_trade: int   = 0
    time_decay_factor:     float = 1.0
    # composed multipliers (logged for diagnostics)
    vol_multiplier:       float = 1.0
    kelly_multiplier:     float = 0.9
    drawdown_multiplier:  float = 1.0
    recovery_multiplier:  float = 1.0
    time_decay_mult:      float = 1.0
    profit_multiplier:    float = 1.0
    rm:                   float = 1.0

    def update_vol_regime(self, atr: float):
        if atr <= 0.0: return
        if self.baseline_atr <= 0.0:
            self.baseline_atr = atr; self.atr_ema = atr; self.vol_regime = 1
            return
        if RISK_MODEL == "v12_31_buggy":
            # AutoLoop=1 collapses alpha=0.1 → ~1.0 after ~50 ticks. EMA tracks
            # current bar's ATR with no smoothing; baselineATR drifts ~63%/bar
            # instead of ~1%/bar (1 - 0.99^100 ≈ 0.63).
            self.atr_ema = atr
            self.baseline_atr += 0.63 * (self.atr_ema - self.baseline_atr)
        else:
            self.atr_ema = 0.1 * atr + 0.9 * self.atr_ema
            self.baseline_atr += 0.01 * (self.atr_ema - self.baseline_atr)
        ratio = self.atr_ema / self.baseline_atr if self.baseline_atr > 0 else 1.0
        if   ratio < 0.7: self.vol_regime = 0
        elif ratio < 1.3: self.vol_regime = 1
        elif ratio < 2.0: self.vol_regime = 2
        else:             self.vol_regime = 3

    def update_time_decay(self):
        if RISK_MODEL == "v12_31_buggy":
            # AutoLoop=1: barsSinceLastTrade++ runs ~100x/bar. Within a single
            # clock-bar after any trade, counter crosses 200 → factor pins 0.5.
            # Approximation: any post-trade bar sees factor saturated to 0.5.
            self.bars_since_last_trade += 1
            self.time_decay_factor = 0.5 if self.bars_since_last_trade >= 1 else 1.0
        else:
            self.bars_since_last_trade += 1
            n = self.bars_since_last_trade
            if   n < 50:  self.time_decay_factor = 1.0
            elif n > 200: self.time_decay_factor = 0.5
            else:         self.time_decay_factor = 1.0 - (n - 50) / 150.0 * 0.5

    def compute_rm(self, prac_kelly: float, cum_trades: int,
                   sess_dd: float, in_recovery: bool,
                   sess_pnl: float, sess_trades: int, consec_losses: int):
        # volMultiplier
        if   self.vol_regime == 0: self.vol_multiplier = 1.20
        elif self.vol_regime == 1: self.vol_multiplier = 1.00
        elif self.vol_regime == 2: self.vol_multiplier = 0.70
        else:                      self.vol_multiplier = 0.40
        # kellyMultiplier ([v12.1] cold-start 0.9)
        if   cum_trades < 10:        self.kelly_multiplier = 0.90
        elif prac_kelly <= 0.0:      self.kelly_multiplier = 0.25
        elif prac_kelly >= 0.20:     self.kelly_multiplier = 1.25
        elif prac_kelly >= 0.15:     self.kelly_multiplier = 1.10
        elif prac_kelly >= 0.10:     self.kelly_multiplier = 1.00
        else:                        self.kelly_multiplier = 0.75
        # drawdownMultiplier
        if   sess_dd <= 0:    self.drawdown_multiplier = 1.00
        elif sess_dd < 500:   self.drawdown_multiplier = 0.95
        elif sess_dd < 1000:  self.drawdown_multiplier = 0.80
        elif sess_dd < 1500:  self.drawdown_multiplier = 0.60
        elif sess_dd < 2000:  self.drawdown_multiplier = 0.40
        else:                 self.drawdown_multiplier = 0.25
        self.recovery_multiplier = 0.60 if in_recovery else 1.00
        self.time_decay_mult     = self.time_decay_factor
        # profitMultiplier (session-trade scaling)
        self.profit_multiplier = 1.0
        if sess_trades >= 3 and sess_pnl > 0:
            if   sess_pnl >= 1500: self.profit_multiplier = 1.30
            elif sess_pnl >= 1000: self.profit_multiplier = 1.20
            elif sess_pnl >= 500:  self.profit_multiplier = 1.12
            elif sess_pnl >= 250:  self.profit_multiplier = 1.06
        # untracked multipliers pinned to 1.0 (see class docstring)
        core      = (self.vol_multiplier ** 0.30) * (self.kelly_multiplier ** 0.25) * (self.drawdown_multiplier ** 0.25)
        secondary = 1.0  # equityCurveMultiplier^0.10 * antiMartingaleMultiplier^0.10 → 1.0
        modifiers = self.recovery_multiplier * self.time_decay_mult * 1.0 * self.profit_multiplier
        rm = core * secondary * modifiers
        rm = max(0.10, min(2.00, rm))
        # consec-loss override
        if   consec_losses >= 4: rm = min(rm, 0.25)
        elif consec_losses >= 3: rm = min(rm, 0.50)
        self.rm = rm

    def on_trade_close(self):
        self.bars_since_last_trade = 0
        # In v12.32, factor returns to 1.0 immediately; the buggy version stays at 0.5
        # until next bar (covered by update_time_decay).
        self.time_decay_factor = 1.0

    def on_day_reset(self):
        self.bars_since_last_trade = 0
        self.time_decay_factor     = 1.0


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
    trend_ts: float = 0.0   # [trend-long instrumentation] trend-strength at arm (M5 only)
    # [regime-day instrumentation 2026-07-02] causal day-anchored features at
    # entry, for the fade-day/trend-day classifier study (all modes, every entry):
    day_ext_lo: float = 0.0  # (close - day_low)/ATR — rally off today's low
    day_ext_hi: float = 0.0  # (day_high - close)/ATR — selloff off today's high
    day_os:     float = 0.0  # one-sidedness: (bars above VWAP - below)/bars today
    day_vws:    float = 0.0  # VWAP slope over last 20 bars (clamped to day), /ATR

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
        # [vp-lookahead-fix] per-bar snapshots; strategy loop reads self.vp_v[i],
        # never self.vp (which ends the pre-compute pass holding future levels).
        self.vp_v: List[_VPView] = []
        # [score-port] intraday prior-session levels (S9), snapshotted per bar.
        # Must be advanced on EVERY bar — the running session hi/lo becomes the
        # next session's prev_high/prev_low, so feeding it only the bars that
        # reach the strategy loop would corrupt it.
        self.idl = IntradayLevels()
        self.idl_v: List[_IDLView] = []
        self.risk  = Risk()
        self.inst_risk = InstRisk()  # [v12.32-ab] full RM model for the bug A/B
        self.trap  = Trap()
        self.rm_gated = 0            # [v12.32-ab] count of SETUPs killed by RM<floor

        # [funnel] post-cascade gate kill counters — which gate eats armed setups.
        # Used by funnel_report.py; zero-cost when unread.
        self.funnel = {k: 0 for k in (
            "armed", "regime", "vwap_dir", "vwap_near", "m1_extra", "rm_floor", "cool_stop", "qual", "entered")}
        self.funnel_mode = {}        # per-mode {armed, qual, entered}
        self.funnel_qual_killed = [] # (mode, q) for every qual-floor kill
        self.funnel_audit = []       # (dt, mode, gate, detail) — dated trail of every cascade outcome
        self.nofire_audit = []       # (dt, [why…]) — dated M4 near-miss reasons (LOG_NOFIRE only)
        self.fn_days_rth = set(); self.fn_days_armed = set(); self.fn_days_entered = set()

        # Trade state
        self.in_pos     = False
        self.is_long    = False
        self.entry_px   = 0.0
        self.stop_px    = 0.0
        self._tr_stop   = 0.0   # [trend-long] swing-low stop, set at arm time
        self._tr_q      = 0     # [trend-long] custom quality 0..100, set at arm time
        self._tr_ts     = 0.0   # [trend-long instrumentation] trend-strength at arm
        self._tr_cr     = 0     # [trend-long instrumentation] chop regime at arm
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

        # [M4-OR levels] per-session opening range, accumulated causally
        self.or_hi     = None
        self.or_lo     = None
        self.or_ready  = False
        self._m4_or_only = False   # armed from an OR level and not a swing level

        # Cooldowns
        self.last_trade_bar = -1
        self.last_loss_bar  = -1
        self.last_stop_bar  = -1
        self.last_stop_dir  = 0
        self._vt_last_bar   = -1   # [VWAP_TOUCH_TRIGGER] last touch-entry bar
        # [v12.20 port-back] M5 cooldown + M7 prior-imbalance latched state.
        # Restored when ENABLE_M5/ENABLE_M7 are True; otherwise unused.
        self.last_m5_bar    = -1
        self.prev_imb_dir   = 0
        self.prev_imb_str   = 0
        self.prev_imb_extreme = 0.0
        # [v18 M8 port] isolated prev-imbalance state for the M8 type-2 fade,
        # kept separate from the M7 path (ENABLE_M7-gated) so the two never
        # interact. Updated every bar when M8_FADE_FULL.
        self.m8_prev_imb_dir   = 0
        self.m8_prev_imb_str   = 0
        self.m8_prev_imb_extreme = 0.0
        # [IMB_MODEL=cpp_stateful] persistent imbalance state (cpp pImb).
        self.imb_state = ImbState()

        # Spike / volume-cool state (mirrors C++ pNews + pVCool; NEWS_FILTER gated)
        self.spike_bar       = -1
        self.spike_active    = False
        self.vcool_remaining = 0
        self.vcool_spike_bar = -1

        # [v12.35] NOFIRE diagnostic + arm-vs-fire funnel
        self.arm_long  = {m: 0 for m in range(8)}   # mode armed a LONG shape (pre-gates)
        self.arm_short = {m: 0 for m in range(8)}   # mode armed a SHORT shape (pre-gates)
        self.arm_rows  = []                          # [armdiag] per-armed-bar detail rows
        self.nofire = []                            # sample [V18A NOFIRE] lines
        self.nofire_possible = {"M2": 0, "M4": 0, "M6": 0}
        self.nofire_why = {}                        # near-miss block-reason tally
        self.nofire_bars = 0                        # bars with a shape but no arm

        # Diagnostics
        self.dbg_range_spike   = 0   # range_spike condition fired
        self.dbg_delta_spike   = 0   # delta_spike condition fired
        self.dbg_vcool_trigger = 0   # range_ratio >= C_VCOOL_THRESH fired
        self.dbg_spike_blocked = 0   # bar suppressed by spike cooldown
        self.dbg_vcool_blocked = 0   # bar suppressed by vcool pause
        self.dbg_max_range_ratio = 0.0  # peak observed range_ratio

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
            # [score-port] Fill zones with THIS bar before snapshotting, matching
            # the cpp order (updateSinglePrints runs per bar against live data),
            # then deep-copy the zone list so later fills can't mutate the past.
            self.vp.update_single_prints(bar.high, bar.low, bar.volume)
            self.vp_v.append(_VPView(self.vp.poc, self.vp.vah, self.vp.val,
                                     self.vp.valid,
                                     self.vp.poc50, self.vp.vah50,
                                     self.vp.val50, self.vp.valid50,
                                     tuple(tuple(z) for z in self.vp.sp_zones)))
            self.idl.update(bar)
            self.idl_v.append(self.idl.snapshot())
            self.atr_v.append(a)
            self.vwap_v.append(v)
            self.vwap_sd.append(s)
            self.cum_d.append(cd)
            self.avg_d.append(max(1.0, _ad_ema))

        print("  Running strategy loop...")
        warmup = max(C_STRUCT_LB, ATR_PER + 5, 40)
        for i in range(warmup, self.n):
            self._step(i)

        print(f"  [DIAG] spike triggers: range={self.dbg_range_spike} delta={self.dbg_delta_spike} "
              f"vcool_threshold_hits={self.dbg_vcool_trigger} | "
              f"bars_blocked spike={self.dbg_spike_blocked} vcool={self.dbg_vcool_blocked} | "
              f"peak_range_ratio={self.dbg_max_range_ratio:.2f}")
        print(f"  [DIAG] risk_model={RISK_MODEL} setups_killed_by_RM_floor={self.rm_gated}")
        print(f"  [DIAG] funnel {self.funnel} | days: RTH={len(self.fn_days_rth)} "
              f"armed={len(self.fn_days_armed)} traded={len(self.fn_days_entered)}")

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
            self.inst_risk.on_day_reset()  # [v12.32-ab] reset time-decay counter
            self.trap.reset()
            self.imb_state.reset()         # cpp:2192 resets pImb on session roll
            self.or_hi = self.or_lo = None   # [M4-OR levels] new session, new OR
            self.or_ready = False

        # [M4-OR levels] Accumulate the opening range BEFORE the RTH_OPEN gate
        # below (RTH_OPEN is 09:35, so the 09:30-09:35 bars would otherwise never
        # be seen). A bar is included only if it CLOSED inside the window, and
        # the levels are not marked usable until a bar closes at/after M4_OR_END
        # — so no bar can ever both build the range and trigger on it.
        if M4_OR_LEVELS:
            if M4_OR_START <= bar.hhmm < M4_OR_END:
                self.or_hi = bar.high if self.or_hi is None else max(self.or_hi, bar.high)
                self.or_lo = bar.low  if self.or_lo is None else min(self.or_lo, bar.low)
            elif bar.hhmm >= M4_OR_END and self.or_hi is not None:
                self.or_ready = True

        # [v12.32-ab] Per-bar risk-EMA + time-decay updates (mirrors cpp 1714 hoisted block).
        # Behavior diverges by RISK_MODEL: "v12_31_buggy" emulates the AutoLoop=1
        # per-tick over-sampling; "v12_32_fixed" uses the intended per-bar cadence.
        self.inst_risk.update_vol_regime(atr)
        self.inst_risk.update_time_decay()
        self.inst_risk.compute_rm(
            prac_kelly    = self.risk.prac_kelly,
            cum_trades    = self.risk.wins + self.risk.losses,
            sess_dd       = self.risk.sess_dd,
            in_recovery   = self.risk.in_recovery,
            sess_pnl      = self.risk.sess_pnl,
            sess_trades   = self.risk.wins + self.risk.losses,
            consec_losses = self.risk.consec_loss,
        )

        # ── RTH + flatten ────────────────────────────────────────────────────
        if bar.hhmm >= FLATTEN_HHMM:
            if self.in_pos:
                self._close(i, bar.close, "FLATTEN")
            self.day_done = True
            return
        if bar.hhmm >= RTH_OPEN:
            self.fn_days_rth.add(bar.dt.date())
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
        # [v12.36-ab] Late-entry gate — blocks new SETUPs after LATE_ENTRY_GATE.
        # Trade management above this point still runs for in-flight positions.
        if bar.hhmm >= LATE_ENTRY_GATE:                                  return
        in_news = bool(NEWS_FILTER) and is_news_window(bar.hhmm)
        if in_news and not NEWS_OVERRIDE_M4_RECLAIM:                     return

        # ── [VWAP_TOUCH_TRIGGER] standalone VWAP-touch entry engine ───────────
        # Replaces the normal mode cascade: place an order at every fresh return
        # to the (anchored) VWAP, direction from the VWAP slope. Returns here so
        # the M1..M8 detection below never runs.
        if VWAP_TOUCH_TRIGGER:
            self._vwap_touch(i, bar, atr, vwap)
            return

        # Volume-spike detection + cooldown (mirrors Autopilot.cpp:2092-2120).
        # Decoupled from NEWS_FILTER — runs regardless of news filter setting
        # so spike protection survives even when news blackouts are disabled.
        if i >= 1:
            prev      = self.bars[i - 1]
            prev_atr  = self.atr_v[i - 1] if i >= 2 else atr
            prev_avgd = self.avg_d[i - 1] if i >= 2 else max(1.0, self.avg_d[i])
            prev_range  = prev.high - prev.low
            prev_delta  = prev.ask_vol - prev.bid_vol
            range_ratio = (prev_range / prev_atr) if prev_atr > 0 else 0.0
            if range_ratio > self.dbg_max_range_ratio:
                self.dbg_max_range_ratio = range_ratio
            range_spike = prev_atr > 0 and prev_range > prev_atr * C_SPIKE_ATR_M
            delta_spike = abs(prev_delta) > prev_avgd * 4.0 and range_ratio >= 2.0
            if (range_spike or delta_spike) and self.spike_bar != i - 1:
                self.spike_bar    = i - 1
                self.spike_active = True
                if range_spike: self.dbg_range_spike += 1
                if delta_spike: self.dbg_delta_spike += 1
                if range_ratio >= C_VCOOL_THRESH and self.vcool_spike_bar != i - 1:
                    self.vcool_remaining = C_VCOOL_PAUSE
                    self.vcool_spike_bar = i - 1
                    self.dbg_vcool_trigger += 1

            if self.spike_active and C_SPIKE_COOL > 0 and i <= self.spike_bar + C_SPIKE_COOL:
                self.dbg_spike_blocked += 1
                return
            else:
                self.spike_active = False

            if self.vcool_remaining > 0:
                self.vcool_remaining -= 1
                if self.vcool_remaining > 0:
                    self.dbg_vcool_blocked += 1
                    return

        if self.day_trades >= MAX_TRADES:                                return
        if DAILY_LOSS > 0 and self.risk.sess_dd > DAILY_LOSS * 0.8:      return
        if self.risk.consec_loss >= C_MAX_LOSSES:                        return
        if not self._cool_ok(i):                                         return
        if self._bars_from_open(i) < C_OPEN_COOL:                        return

        # ── Indicators ───────────────────────────────────────────────────────
        vwap_ok  = i >= C_VWAP_MAT and vwap > 0
        dm       = i >= C_DELTA_MAT
        dlt      = bar.ask_vol - bar.bid_vol
        bull     = bar.close > bar.open
        bear     = bar.close < bar.open

        ctrl = control_score(self.bars, i, self.vp_v[i], dm)
        div  = divergence(self.bars, i, self.cum_d, atr)
        bal  = balance_state(self.bars, i, atr)
        if IMB_MODEL == "cpp_stateful":
            # Update at the same point in the flow as cpp:2868 — i.e. after the
            # flatten/late-entry/open-cool/no-delta gates above, which return
            # early in both. Gated bars therefore skip the update in both.
            self.imb_state.update(self.bars, i, atr)
            imb = self.imb_state
        else:
            imb = imbalance_state(self.bars, i)

        # [score-port] Directional score — faithful 12-component port of the cpp
        # SCORING block. Was a count of confirming delta bars over the last 5,
        # which capped the score at 5 and made M4 (quality = score*10) unable to
        # clear QUAL_FLOOR=50. See directional_score() for the full rationale.
        ice_bid_px, ice_ask_px = iceberg_state(self.bars, i, atr)
        sc_l, sc_s, lb_sc_l, lb_sc_s, d_l, d_s, lb_l, lb_s = directional_score(
            self.bars, i, atr, vwap, vsd,
            self.vp_v[i] if i < len(self.vp_v) else None,
            self.idl_v[i] if i < len(self.idl_v) else None, div, self.trap,
            self.avg_d[i] if i < len(self.avg_d) else max(1.0, abs(dlt)),
            dm, bull, bear, ice_bid_px, ice_ask_px)

        vb2u = vwap + vsd * 2 if vsd > 0 else 0.0
        vb2l = vwap - vsd * 2 if vsd > 0 else 0.0

        # ── Modes ────────────────────────────────────────────────────────────
        m1l = m1s = False
        m2l = m2s = m3l = m3s = m4l = m4s = False
        m5l = m5s = False                # [v12.20 port-back]
        trl = False                      # [trend-long] lowest-priority VWAP-reclaim long
        m6l = m6s = m8l = m8s = False
        m7l = m7s = False                # [v12.20 port-back]
        m7_stop = 0.0
        m6_sp = m6_t1 = m6_t2 = 0.0
        fade_active = False; fade_edge = fade_type = 0
        fd_sp = fd_t1 = fd_t2 = 0.0
        bk_vs = 99  # balance verify score placeholder for M8

        # [v12.22] M1 VWAP-slope directional filter. Block longs when VWAP is
        # sloping down (rising tide tape conflicts with the bounce thesis) and
        # vice versa. Tolerance ATR*0.02 — tight by design; M1 is a reversal
        # mode and disagreement with VWAP direction is high-cost.
        s_ok_l = s_ok_s = True
        if i >= C_VWAP_SLP_LB and vwap > 0 and atr > 0:
            vp_lb = self.vwap_v[i - C_VWAP_SLP_LB]
            if vp_lb > 0:
                slope = vwap - vp_lb
                tol = atr * C_VWAP_SLP_TOL
                if slope < -tol: s_ok_l = False
                if slope >  tol: s_ok_s = False

        # M1 — VWAP bounce / reject
        if vwap_ok and atr > 0:
            vw_z = atr * 0.75
            touch = bar.low <= vwap + vw_z and bar.high >= vwap - vw_z
            prev_c = self.bars[i-1].close if i > 0 else bar.close
            if touch:
                if bull and bar.close > vwap and prev_c > vwap and sc_l >= C_MIN_SC_M1 and ctrl >= 0 and s_ok_l:
                    m1l = True
                if bear and bar.close < vwap and prev_c < vwap and sc_s >= C_MIN_SC_M1 and ctrl <= 0 and s_ok_s and not m1l:
                    m1s = True
        # [v12.33] mode=1: wick-reject gate. Failed cross-contract A/B 2026-05-22.
        if M1_PULLBACK == 1 and vwap_ok:
            if m1l:
                pierced = bar.low <= vwap - TICK
                wick_ge = (bar.open - bar.low) >= (bar.close - bar.open)
                if not (pierced and wick_ge):
                    m1l = False
            if m1s:
                pierced = bar.high >= vwap + TICK
                wick_ge = (bar.high - bar.open) >= (bar.open - bar.close)
                if not (pierced and wick_ge):
                    m1s = False

        # [v12.34] mode=2: prior-bar dip + reclaim. REPLACES legacy continuation.
        # Prior bar touched VWAP and closed weak; current reclaims with body
        # on trend side. Cross-contract A/B via backtest_v12_34_ab.py.
        if M1_PULLBACK == 2 and vwap_ok and i >= 1 and atr > 0:
            prev_bar = self.bars[i-1]
            vw_z2 = atr * 0.75
            tol = atr * 0.1
            prev_dip_l = (prev_bar.low  <= vwap + vw_z2) and (prev_bar.close <= vwap + tol)
            prev_dip_s = (prev_bar.high >= vwap - vw_z2) and (prev_bar.close >= vwap - tol)
            recl_l = (bar.close > vwap) and bull and s_ok_l
            rej_s  = (bar.close < vwap) and bear and s_ok_s
            dip_l = prev_dip_l and recl_l and sc_l >= C_MIN_SC_M1 and ctrl >= 0
            dip_s = prev_dip_s and rej_s  and sc_s >= C_MIN_SC_M1 and ctrl <= 0 and not dip_l
            m1l = dip_l
            m1s = dip_s

        # M2 — VP level test
        if vwap_ok and self.vp_v[i].valid and atr > 0:
            vp_z = atr * 0.75
            for lv in [self.vp_v[i].poc, self.vp_v[i].vah, self.vp_v[i].val]:
                if lv <= 0 or abs(bar.close - lv) > vp_z:
                    continue
                # [M2_GEOM] did price penetrate the level, or merely graze it?
                swept_l = bar.low  < lv - TICK
                swept_s = bar.high > lv + TICK
                if bar.low <= lv + TICK and bar.close > lv and bull and sc_l >= C_MIN_SC_ALL and ctrl >= 0:
                    if _m2_geom_ok(swept_l):
                        m2l = True
                if bar.high >= lv - TICK and bar.close < lv and bear and sc_s >= C_MIN_SC_ALL and ctrl <= 0:
                    if _m2_geom_ok(swept_s):
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
            # [M4-OR levels] identical shape + gates, sourced from ORH/ORL
            self._m4_or_only = False
            if M4_OR_LEVELS:
                m4l_or = m4s_or = False
                if self.or_ready:
                    m4l_or = (m4_vwap_edge and bar.low < self.or_lo - TICK
                              and bar.close > self.or_lo and bull
                              and sc_l >= m4_min and ctrl >= 0)
                    m4s_or = (m4_vwap_edge and bar.high > self.or_hi + TICK
                              and bar.close < self.or_hi and bear
                              and sc_s >= m4_min and ctrl <= 0)
                if M4_OR_MODE == "only":
                    # suppress the rolling-swing source ALWAYS (including before
                    # M4_OR_END, where or_ready is still False) so this arm
                    # isolates the OR source instead of mixing the two
                    self._m4_or_only = bool(m4l_or or m4s_or)
                    m4l, m4s = m4l_or, m4s_or
                else:
                    self._m4_or_only = bool((m4l_or and not m4l) or (m4s_or and not m4s))
                    m4l = m4l or m4l_or
                    m4s = m4s or m4s_or
            if (m4l or m4s):
                self._on_m4_arm(i, bool(m4l), bar.close, atr, sw_lo if m4l else sw_hi)
            if 3 in DISABLE_MODES:                      # suppress M4 (sweep+reclaim)
                m4l = m4s = False

        # [v12.20 port-back] M5 — Trap Reversal
        # Arm SHORT when trap.direction == +1 (failed up-breakout) AND trap.phase==3
        # AND short delta confirms (sc_s >= C_M5_MIN_SC) AND ctrl is not strongly bullish.
        # Mirror cpp lines 2952-2971.
        if ENABLE_M5 and self.trap.phase == 3:
            cool_ok = (C_M5_COOLDOWN <= 0) or (self.last_m5_bar < 0) or (i > self.last_m5_bar + C_M5_COOLDOWN)
            if cool_ok:
                if self.trap.direction == +1 and sc_s >= C_M5_MIN_SC and ctrl <= 2:
                    m5s = True
                if self.trap.direction == -1 and sc_l >= C_M5_MIN_SC and ctrl >= -2:
                    m5l = True
                if m5l or m5s:
                    self.last_m5_bar = i
        if 4 in DISABLE_MODES:
            m5l = m5s = False

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
                # 5-component scoring calibrated for 2500-vol bars.
                # C++ checks (acc_bars, retest, opp_vol, vBeyond, volume) require
                # multi-bar breakout accumulation — structurally 0% on single-bar
                # detection at this bar size. The 5 checks below all discriminate.
                # 1. Consecutive delta streak ≥3 (C++: breaks on first mismatch)
                d_streak = 0
                for k in range(5):
                    if i - k < 0: break
                    bd = self.bars[i-k].ask_vol - self.bars[i-k].bid_vol
                    if (break_dir > 0 and bd > 0) or (break_dir < 0 and bd < 0):
                        d_streak += 1
                    else:
                        break
                if d_streak >= 3: bk_verify += 1
                # 2. No absorption at breakout level (high-vol opposing candle)
                abs_at_bk = False
                for k in range(3):
                    if i-k < 0: break
                    bd = self.bars[i-k].ask_vol - self.bars[i-k].bid_vol
                    b_up = self.bars[i-k].close > self.bars[i-k].open
                    vol_k = self.bars[i-k].volume
                    if break_dir > 0 and bd < 0 and b_up and vol_k > bal.avg_vol * 1.2:
                        abs_at_bk = True; break
                    if break_dir < 0 and bd > 0 and not b_up and vol_k > bal.avg_vol * 1.2:
                        abs_at_bk = True; break
                if not abs_at_bk: bk_verify += 1
                # 3. No opposing iceberg at level (not tracked in Python — assume absent)
                bk_verify += 1
                # 4. No opposing divergence
                no_div = not (div.strength <= -2 if break_dir > 0 else div.strength >= 2)
                if no_div: bk_verify += 1
                # 5. Small wick in break direction
                wick = (bar.high - bar.close) if break_dir > 0 else (bar.close - bar.low)
                if wick < atr * 0.3: bk_verify += 1
                bk_vs = bk_verify

                if bk_verify >= 5:   # max=5 with this component set; quality gate enforces bk_vs≥5
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
        # [v12.25] M6 soft CtrlScore gate. Block only on clearly opposing control.
        if m6l and ctrl < -1:
            m6l = False; m6_sp = m6_t1 = m6_t2 = 0.0
        if m6s and ctrl > 1:
            m6s = False; m6_sp = m6_t1 = m6_t2 = 0.0
        if 5 in DISABLE_MODES:                      # suppress M6 entirely (top-priority)
            m6l = m6s = False; m6_sp = m6_t1 = m6_t2 = 0.0

        # [v12.20 port-back] M7 — Auction Reversal
        # Latch most recent active imbalance, then arm a reversal when it fades or dies.
        # Cpp lines 3046-3101. Iceberg-presence check (component 8) is granted free
        # since Python doesn't track icebergs — biases M7 to fire slightly more.
        if ENABLE_M7:
            if imb.active:
                self.prev_imb_dir = imb.direction
                self.prev_imb_str = imb.strength
                self.prev_imb_extreme = bar.high if imb.direction > 0 else bar.low
            rv_vs = 8
            if self.prev_imb_dir != 0 and atr > 0:
                imb_fading = imb.active and imb.strength < self.prev_imb_str - 1
                imb_dead   = (not imb.active) and self.prev_imb_str >= 3
                if imb_fading or imb_dead:
                    rv_vs = 0
                    rev_dir = -1 if self.prev_imb_dir > 0 else +1
                    # 1. Delta exhaustion (decreasing |delta| over last 3 bars)
                    if i >= 2:
                        dm0 = abs(dlt)
                        dm1 = abs(self.bars[i-1].ask_vol - self.bars[i-1].bid_vol)
                        dm2 = abs(self.bars[i-2].ask_vol - self.bars[i-2].bid_vol)
                        if dm0 < dm1 < dm2: rv_vs += 1
                    # 2. Prev-imb extreme sits at a structural level (VB2 / VP)
                    at_lev = False
                    if self.prev_imb_dir > 0 and vb2u > 0 and self.prev_imb_extreme >= vb2u - atr * 0.3:
                        at_lev = True
                    if self.prev_imb_dir < 0 and vb2l > 0 and self.prev_imb_extreme <= vb2l + atr * 0.3:
                        at_lev = True
                    if self.vp_v[i].valid:
                        for lv in (self.vp_v[i].poc, self.vp_v[i].vah, self.vp_v[i].val):
                            if lv > 0 and abs(self.prev_imb_extreme - lv) <= atr * 0.5:
                                at_lev = True; break
                    if at_lev: rv_vs += 1
                    # 3. Aggression flip on current bar
                    avgd = self.avg_d[i] if i < len(self.avg_d) else max(1.0, abs(dlt))
                    if rev_dir > 0 and dlt > 0 and abs(dlt) > avgd * 1.5: rv_vs += 1
                    if rev_dir < 0 and dlt < 0 and abs(dlt) > avgd * 1.5: rv_vs += 1
                    # 4. Body in reversal direction (>=55%)
                    rng = bar.high - bar.low
                    body = abs(bar.close - bar.open)
                    body_pct = (body / rng) if rng > 0 else 0.0
                    corr_dir = bull if rev_dir > 0 else bear
                    if body_pct >= 0.55 and corr_dir: rv_vs += 1
                    # 5. Trap confluence
                    if self.trap.phase >= 2: rv_vs += 1
                    # 6. Divergence supports reversal
                    if rev_dir > 0 and div.strength >= 2: rv_vs += 1
                    if rev_dir < 0 and div.strength <= -2: rv_vs += 1
                    # 7. Volume burst
                    if bal.avg_vol > 0 and bar.volume > bal.avg_vol * 1.8: rv_vs += 1
                    # 8. No counter-iceberg — granted free (no iceberg detection in Python)
                    rv_vs += 1
                    if rv_vs >= C_M7_MIN_VS:
                        if rev_dir > 0:
                            m7l = True
                            m7_stop = self.prev_imb_extreme - atr * 0.3
                        else:
                            m7s = True
                            m7_stop = self.prev_imb_extreme + atr * 0.3
                        self.prev_imb_dir = 0
                        self.prev_imb_str = 0
                        self.prev_imb_extreme = 0.0
        if 6 in DISABLE_MODES:
            m7l = m7s = False
            m7_stop = 0.0

        # [v18 M8 port] Maintain prev-imbalance state + rvVerifyScore for the M8
        # type-2 range fade, independent of M7 (ENABLE_M7-gated, default off).
        # Mirrors cpp IOF_NQ_Autopilot.cpp:3317-3371 (M7 firing removed in
        # v12.25 but rvVerifyScore still feeds M8 type-2). Self-contained state.
        rv_vs_m8 = 8
        if M8_FADE_FULL:
            if imb.active:
                self.m8_prev_imb_dir = imb.direction
                self.m8_prev_imb_str = imb.strength
                self.m8_prev_imb_extreme = bar.high if imb.direction > 0 else bar.low
            if self.m8_prev_imb_dir != 0 and atr > 0:
                imb_fading = imb.active and imb.strength < self.m8_prev_imb_str - 1
                imb_dead   = (not imb.active) and self.m8_prev_imb_str >= 3
                if imb_fading or imb_dead:
                    rv_vs_m8 = 0
                    rev_dir = -1 if self.m8_prev_imb_dir > 0 else +1
                    # 1. delta exhaustion (decreasing |delta| over last 3 bars)
                    if i >= 2:
                        dm0 = abs(dlt)
                        dm1 = abs(self.bars[i-1].ask_vol - self.bars[i-1].bid_vol)
                        dm2 = abs(self.bars[i-2].ask_vol - self.bars[i-2].bid_vol)
                        if dm0 < dm1 < dm2: rv_vs_m8 += 1
                    # 2. prev-imb extreme at a structural level (VB2 / VP)
                    at_lev = False
                    if self.m8_prev_imb_dir > 0 and vb2u > 0 and self.m8_prev_imb_extreme >= vb2u - atr * 0.3:
                        at_lev = True
                    if self.m8_prev_imb_dir < 0 and vb2l > 0 and self.m8_prev_imb_extreme <= vb2l + atr * 0.3:
                        at_lev = True
                    if self.vp_v[i].valid:
                        for lv in (self.vp_v[i].poc, self.vp_v[i].vah, self.vp_v[i].val):
                            if lv > 0 and abs(self.m8_prev_imb_extreme - lv) <= atr * 0.5:
                                at_lev = True; break
                    if at_lev: rv_vs_m8 += 1
                    # 3. aggression flip on current bar
                    avgd = self.avg_d[i] if i < len(self.avg_d) else max(1.0, abs(dlt))
                    if rev_dir > 0 and dlt > 0 and abs(dlt) > avgd * 1.5: rv_vs_m8 += 1
                    if rev_dir < 0 and dlt < 0 and abs(dlt) > avgd * 1.5: rv_vs_m8 += 1
                    # 4. body in reversal direction (cpp uses >=0.62)
                    rng = bar.high - bar.low
                    bpct = (abs(bar.close - bar.open) / rng) if rng > 0 else 0.0
                    corr = bull if rev_dir > 0 else bear
                    if bpct >= 0.62 and corr: rv_vs_m8 += 1
                    # 5. trap confluence
                    if self.trap.phase >= 2: rv_vs_m8 += 1
                    # 6. divergence supports reversal
                    if rev_dir > 0 and div.strength >= 2: rv_vs_m8 += 1
                    if rev_dir < 0 and div.strength <= -2: rv_vs_m8 += 1
                    # 7. volume burst
                    if bal.avg_vol > 0 and bar.volume > bal.avg_vol * 1.8: rv_vs_m8 += 1
                    # 8. no counter-iceberg — granted free (no iceberg detection)
                    rv_vs_m8 += 1

        # M8 — Fade (balance breakout fade, type 1)
        # [v13] Under V13_MODEL the trap edge term is dropped (v13 removed the
        # trap subsystem); baseline keeps it. Type-3 trend-exhaustion fade below
        # is V13_MODEL-only (backtest.py never modeled it for the v12.37 proxy).
        if 1 in M8_FADE_TYPES and not m6l and not m6s and bal.mature and atr > 0 and bk_vs <= 4:
            bk_t = atr * 0.30
            if bar.high > bal.hi + bk_t * 0.5 and bar.close < bal.hi + bk_t * 0.3 and bear:
                # v13 cpp starts edge at 2 for a very weak breakout (bkVerifyScore<=2,
                # cpp v13 line 1496); v12.37 starts flat at 1. Without this, every
                # v13 type-1 caps at edge 4 => q40 and dies at floor 50.
                edge = 2 if (V13_MODEL and bk_vs <= 2) else 1
                if div.strength <= -2: edge += 2
                if div.persist_abs_sell: edge += 1
                if not V13_MODEL and self.trap.phase >= 2 and self.trap.direction == +1: edge += 2
                if edge >= 4:
                    m8s = True; fade_active = True; fade_type = 1; fade_edge = edge
                    fd_sp = bar.high + atr * 0.3
                    fd_t1 = bal.poc; fd_t2 = bal.lo
            if not m8s and bar.low < bal.lo - bk_t * 0.5 and bar.close > bal.lo - bk_t * 0.3 and bull:
                edge = 2 if (V13_MODEL and bk_vs <= 2) else 1
                if div.strength >= 2: edge += 2
                if div.persist_abs_buy: edge += 1
                if not V13_MODEL and self.trap.phase >= 2 and self.trap.direction == -1: edge += 2
                if edge >= 4:
                    m8l = True; fade_active = True; fade_type = 1; fade_edge = edge
                    fd_sp = bar.low - atr * 0.3
                    fd_t1 = bal.poc; fd_t2 = bal.hi

        # [v18 M8 port] M8 type 2 — range / imbalance fade. cpp 3412-3432.
        # Fires on a WEAK (rvVerifyScore 2-4) faded imbalance and fades it in the
        # imbalance's *original* direction. This is the fade type that fired LIVE
        # 2026-06-26 (two SHORT M8 Q=50 => edgeScore 5). M8_FADE_FULL-gated.
        if M8_FADE_FULL and 2 in M8_FADE_TYPES and not m6l and not m6s and not m8l and not m8s \
           and 2 <= rv_vs_m8 <= 4 and self.m8_prev_imb_dir != 0 and atr > 0:
            edge = 2 if rv_vs_m8 <= 3 else 1
            if self.m8_prev_imb_dir > 0 and div.strength >= 0: edge += 2
            if self.m8_prev_imb_dir < 0 and div.strength <= 0: edge += 2
            if self.trap.phase < 2: edge += 1
            if edge >= 4:
                if self.m8_prev_imb_dir > 0:
                    m8l = True; fade_active = True; fade_type = 2; fade_edge = edge
                    fd_sp = min(bar.low, self.m8_prev_imb_extreme) - atr * 0.3
                    fd_t1 = self.m8_prev_imb_extreme + atr * 0.5
                    fd_t2 = self.m8_prev_imb_extreme + atr * 1.5
                else:
                    m8s = True; fade_active = True; fade_type = 2; fade_edge = edge
                    fd_sp = max(bar.high, self.m8_prev_imb_extreme) + atr * 0.3
                    fd_t1 = self.m8_prev_imb_extreme - atr * 0.5
                    fd_t2 = self.m8_prev_imb_extreme - atr * 1.5

        # [v13] M8 type 3 — trend-exhaustion fade. Ported from the v13 cpp
        # (= v12.37 cpp lines 3433-3461). Active under V13_MODEL or M8_FADE_FULL.
        if (V13_MODEL or M8_FADE_FULL) and 3 in M8_FADE_TYPES and not m6l and not m6s and not m8l and not m8s and i >= 8 and atr > 0:
            trend_bars = sum(1 if self.bars[i-k].close > self.bars[i-k].open else -1
                             for k in range(8))
            up_t = trend_bars >= 4
            dn_t = trend_bars <= -4
            if up_t or dn_t:
                dm0 = abs(dlt)
                dm1 = abs(self.bars[i-1].ask_vol - self.bars[i-1].bid_vol) if i >= 1 else dm0
                dm2 = abs(self.bars[i-2].ask_vol - self.bars[i-2].bid_vol) if i >= 2 else dm0
                delta_dec = dm0 < dm1 < dm2
                rng = bar.high - bar.low
                body_small = rng > 0 and abs(bar.close - bar.open) < rng * 0.4
                vol_dec = i >= 2 and self.bars[i].volume < self.bars[i-1].volume < self.bars[i-2].volume
                exh = 0
                if delta_dec: exh += 2
                if body_small: exh += 1
                if vol_dec: exh += 2
                if up_t and div.strength <= -2: exh += 2
                if dn_t and div.strength >=  2: exh += 2
                if exh >= 6:
                    if up_t:
                        m8s = True; fade_active = True; fade_type = 3; fade_edge = exh
                        fd_sp = bar.high + atr * 0.4
                        fd_t1 = bar.close - atr * 1.0
                        fd_t2 = vwap if vwap > 0 else (bar.close - atr * 2.0)
                    elif dn_t:
                        m8l = True; fade_active = True; fade_type = 3; fade_edge = exh
                        fd_sp = bar.low - atr * 0.4
                        fd_t1 = bar.close + atr * 1.0
                        fd_t2 = vwap if vwap > 0 else (bar.close + atr * 2.0)

        # [v18 M8 port] M8 type 4 — absorption fade. cpp 3462-3486. Fades a
        # persistent price/delta divergence (up-price/down-delta => short) when
        # it stalls at a structural level. M8_FADE_FULL-gated. Note Python's
        # div.persist_abs_sell == cpp persistUpPxDnDelta>=3 (up_dn>=3) and
        # div.persist_abs_buy == cpp persistDnPxUpDelta>=3.
        if M8_FADE_FULL and 4 in M8_FADE_TYPES and not m6l and not m6s and not m8l and not m8s and atr > 0:
            abs_up = div.persist_abs_sell   # up price / down delta (bearish absorption)
            abs_dn = div.persist_abs_buy    # down price / up delta (bullish absorption)
            if abs_up or abs_dn:
                abs_e = 3
                at_k = False
                if self.vp_v[i].valid:
                    for lv in (self.vp_v[i].poc, self.vp_v[i].vah, self.vp_v[i].val):
                        if lv > 0 and abs(bar.close - lv) <= atr * 0.5: at_k = True
                if vwap > 0 and abs(bar.close - vwap) <= atr * 0.5: at_k = True
                if at_k: abs_e += 2
                if abs_up and div.trend_div_bear: abs_e += 2
                if abs_dn and div.trend_div_bull: abs_e += 2
                if self.trap.phase >= 2: abs_e += 2
                if abs_e >= 6:
                    if abs_up:
                        m8s = True; fade_active = True; fade_type = 4; fade_edge = abs_e
                        fd_sp = bar.high + atr * 0.35
                        fd_t1 = bar.close - atr * 0.75; fd_t2 = bar.close - atr * 2.0
                    elif abs_dn:
                        m8l = True; fade_active = True; fade_type = 4; fade_edge = abs_e
                        fd_sp = bar.low - atr * 0.35
                        fd_t1 = bar.close + atr * 0.75; fd_t2 = bar.close + atr * 2.0

        # [v12.25] M8 strict CtrlScore gate. Fades require neutral/correct-side flow.
        if m8l and ctrl < 0:
            m8l = False; fade_active = False; fade_edge = 0; fade_type = 0
        if m8s and ctrl > 0:
            m8s = False; fade_active = False; fade_edge = 0; fade_type = 0

        # ── [trend-long prototype] VWAP-reclaim-and-go (rides the M5 slot) ─────
        # Catches V-reversal trend days the fade modes miss (e.g. 2026-06-29
        # 29,300→30,050). LONG when price was below VWAP over the last
        # TREND_VWAP_LB bars, reclaims it THIS bar (prev close < VWAP < close)
        # with net-buyer delta and a bull body. Sets `trl` (a dedicated
        # LOWEST-priority flag, mapped to sel=4 at the bottom of the cascade) so
        # it never preempts a real M1/M4 signal — only fires when nothing else
        # armed. _tr_stop (swing-low stop) and _tr_q (custom quality) are read
        # downstream. Long-only by design.
        self._tr_stop = 0.0; self._tr_q = 0; self._tr_ts = 0.0; self._tr_cr = 0
        if TREND_LONG and vwap > 0 and atr > 0 and i >= TREND_VWAP_LB \
           and not (m6l or m6s or m8l or m8s or m4l or m4s):
            tprev_c = self.bars[i-1].close
            below_recent = sum(1 for k in range(1, TREND_VWAP_LB + 1)
                               if self.bars[i-k].close < vwap)
            reclaim = tprev_c < vwap and bar.close > vwap
            tdelta  = bar.ask_vol - bar.bid_vol
            avgd    = self.avg_d[i] if i < len(self.avg_d) else max(1.0, abs(tdelta))
            # [trend iter-2] V-depth gate: demand a real excursion below VWAP
            # (not a chop crossing) before a reclaim counts. Off at depth 0.0.
            depth_ok = True
            if TREND_MIN_DEPTH > 0.0:
                lb_d = min(TREND_DEPTH_LB, i)
                lo_c = min(self.bars[i - k].close for k in range(1, lb_d + 1))
                depth_ok = lo_c <= vwap - atr * TREND_MIN_DEPTH
            if below_recent >= 2 and reclaim and tdelta > 0 and bull and depth_ok:
                # [trend filter] only fire when there's real trend behind the
                # reclaim — strong delta flow and not a chop regime. Cuts the
                # low-quality bull-trap reclaims that lost on NQZ25/NQH6.
                ts_tr = self._trend_str(i, atr)
                _, cr_tr = self._regime(i, atr, vwap)
                self._tr_ts = ts_tr; self._tr_cr = cr_tr   # instrument every reclaim
                if ts_tr >= TREND_MIN_TS and cr_tr <= TREND_MAX_CHOP:
                    trl = True
                    swlo = min(bar.low, self.bars[i-1].low)
                    self._tr_stop = round((swlo - atr * TREND_STOP_BUF) / TICK) * TICK
                    qtr = 50
                    if abs(tdelta) > avgd:               qtr += 10  # strong buyer aggression
                    if (bar.close - vwap) > atr * 0.15:  qtr += 10  # decisive reclaim
                    if bar.close > bar.open:             qtr += 10  # bull body
                    self._tr_q = qtr

        # ── [instrumentation] per-mode arm counts (before downstream gates) ────
        for _mi, _ll, _ss in ((0, m1l, m1s), (1, m2l, m2s), (2, m3l, m3s),
                              (3, m4l, m4s), (4, m5l, m5s), (5, m6l, m6s),
                              (6, m7l, m7s), (7, m8l, m8s)):
            if _ll: self.arm_long[_mi]  += 1
            if _ss: self.arm_short[_mi] += 1

        # [armdiag] Per-armed-bar detail, mirroring cpp WriteArmed(). Mirrors the
        # cascade at the bottom of this method (M6>M7>M8>M5>M4>M3>M2>M1) as a
        # read-only pass so the cascade itself stays untouched. With ENABLE_M5/M7
        # off — the live default — this reduces to the cpp ladder M6>M8>M4>M3>M2>M1.
        if ARM_CSV:
            _arms = (("M1L", m1l), ("M1S", m1s), ("M2L", m2l), ("M2S", m2s),
                     ("M3L", m3l), ("M3S", m3s), ("M4L", m4l), ("M4S", m4s),
                     ("M6L", m6l), ("M6S", m6s), ("M8L", m8l), ("M8S", m8s))
            _on = [tag for tag, flag in _arms if flag]
            if _on:
                _sel, _side = "?", "?"
                for _t, _f, _s in (("M6", m6l, "L"), ("M6", m6s, "S"),
                                   ("M8", m8l, "L"), ("M8", m8s, "S"),
                                   ("M4", m4l, "L"), ("M4", m4s, "S"),
                                   ("M3", m3l, "L"), ("M3", m3s, "S"),
                                   ("M2", m2l, "L"), ("M2", m2s, "S"),
                                   ("M1", m1l, "L"), ("M1", m1s, "S")):
                    if _f:
                        _sel, _side = _t, _s
                        break
                self.arm_rows.append((
                    bar.dt.strftime("%Y-%m-%d"), bar.dt.strftime("%H:%M:%S"),
                    i, "|".join(_on), _sel, _side,
                ))

        # ── [candidate-fire labeler hook] called whenever ANY mode arms, BEFORE
        # the priority cascade or any downstream gate (regime, RM, cool, qual).
        # Default no-op; xgb_candidate_label.py overrides to capture every armed
        # (mode, side) and forward-simulate its outcome. Passes a dict so the
        # signature stays stable as new features are added.
        if any((m1l, m1s, m2l, m2s, m3l, m3s, m4l, m4s,
                m5l, m5s, m6l, m6s, m7l, m7s, m8l, m8s)):
            self._on_any_arm(i, dict(
                bar=bar, atr=atr, vwap=vwap, ctrl=ctrl,
                sc_l=sc_l, sc_s=sc_s, div_strength=div.strength,
                m1l=m1l, m1s=m1s, m2l=m2l, m2s=m2s, m3l=m3l, m3s=m3s,
                m4l=m4l, m4s=m4s, m6l=m6l, m6s=m6s, m8l=m8l, m8s=m8s,
                m6_sp=m6_sp, m6_t1=m6_t1, m6_t2=m6_t2,
                fade_active=fade_active, fade_edge=fade_edge,
                fade_type=fade_type, fd_sp=fd_sp, fd_t1=fd_t1, fd_t2=fd_t2,
                bk_vs=bk_vs,
            ))

        # ── [v12.35] LOG_NOFIRE — per-bar mode-rejection diagnostic ────────────
        # Faithful port of the cpp v12.35 block (Autopilot.cpp:3193+). Fires when
        # M1/M2/M3/M4/M6 all failed to arm AND ≥1 mode had a shape possibility.
        if LOG_NOFIRE and not (m1l or m1s or m2l or m2s or m3l or m3s
                               or m4l or m4s or m6l or m6s):
            # M4 near-miss: did a sweep of the prior C_SWEEP_LB bars occur?
            m4_poss = False; sw_hi_d = sw_lo_d = 0.0
            if i >= C_SWEEP_LB:
                sw_lo_d = min(self.bars[j].low  for j in range(i - C_SWEEP_LB, i))
                sw_hi_d = max(self.bars[j].high for j in range(i - C_SWEEP_LB, i))
                m4_poss = (bar.high > sw_hi_d + TICK) or (bar.low < sw_lo_d - TICK)
            # M2 near-miss: a VP level within ATR*0.75 of close
            m2_poss = False
            if self.vp_v[i].valid and atr > 0:
                vpz = atr * 0.75
                for lv in (self.vp_v[i].poc, self.vp_v[i].vah, self.vp_v[i].val):
                    if lv > 0 and abs(bar.close - lv) <= vpz:
                        m2_poss = True; break
            # M6 near-miss: close beyond a balance edge by half the breakout band
            m6_poss = False
            if bal.mature and atr > 0:
                bkt = atr * 0.30
                m6_poss = (bar.close > bal.hi + bkt * 0.5) or (bar.close < bal.lo - bkt * 0.5)

            if m2_poss or m4_poss or m6_poss:
                self.nofire_bars += 1
                why = []
                if m4_poss:
                    self.nofire_possible["M4"] += 1
                    m4_min = C_MIN_SC_ALL if abs(div.strength) >= 2 else C_MIN_SC_ALL + 1
                    edge_ok = (vwap <= 0) or (abs(bar.close - vwap) >= atr * 0.35)
                    if bar.low < sw_lo_d - TICK:        # would-be M4 LONG (sweep low → reclaim)
                        if   not (bar.close > sw_lo_d): why.append("M4L:no_reclaim")
                        elif not bull:                  why.append("M4L:not_bull")
                        elif sc_l < m4_min:             why.append("M4L:scL_low")
                        elif ctrl < 0:                  why.append("M4L:ctrl_neg")
                        elif not edge_ok:               why.append("M4L:vwap_edge")
                        else:                           why.append("M4L:other")
                    if bar.high > sw_hi_d + TICK:       # would-be M4 SHORT (sweep high → reject)
                        if   not (bar.close < sw_hi_d): why.append("M4S:no_reject")
                        elif not bear:                  why.append("M4S:not_bear")
                        elif sc_s < m4_min:             why.append("M4S:scS_low")
                        elif ctrl > 0:                  why.append("M4S:ctrl_pos")
                        elif not edge_ok:               why.append("M4S:vwap_edge")
                        else:                           why.append("M4S:other")
                if m6_poss: self.nofire_possible["M6"] += 1
                if m2_poss: self.nofire_possible["M2"] += 1
                for w in why:
                    self.nofire_why[w] = self.nofire_why.get(w, 0) + 1
                if why:
                    self.nofire_audit.append((bar.dt, list(why)))
                if len(self.nofire) < 40:
                    self.nofire.append(
                        f"[V18A NOFIRE] {bar.dt:%Y-%m-%d %H:%M} Cls={bar.close:.2f} "
                        f"VWAP={vwap:.2f} ATR={atr:.2f} ctrl={ctrl} scL={sc_l} scS={sc_s} "
                        f"vwapOK={int(vwap_ok)} | M2poss={int(m2_poss)} "
                        f"M4poss={int(m4_poss)}(swHi={sw_hi_d:.2f} swLo={sw_lo_d:.2f}) "
                        f"M6poss={int(m6_poss)} | why={','.join(why) or '-'}")

        # [news-override-ab] During news window with override: M4-only clean reclaims.
        # Tighten M4 (close past sweep ≥ 2 ticks, sc ≥ 4 — no div fallback) and
        # suppress all other modes. If nothing M4-clean is armed, skip the bar.
        if in_news:  # NEWS_OVERRIDE_M4_RECLAIM is true here (gated above)
            clean_l = clean_s = False
            if i >= C_SWEEP_LB and atr > 0:
                sw_lo_n = min(self.bars[j].low  for j in range(i - C_SWEEP_LB, i))
                sw_hi_n = max(self.bars[j].high for j in range(i - C_SWEEP_LB, i))
                clean_l = bar.close > sw_lo_n + 2 * TICK and sc_l >= 4
                clean_s = bar.close < sw_hi_n - 2 * TICK and sc_s >= 4
            m4l = m4l and clean_l
            m4s = m4s and clean_s
            m1l = m1s = m2l = m2s = m3l = m3s = m6l = m6s = m8l = m8s = False
            if not (m4l or m4s):
                return

        # DISABLE_MODES suppression for the modes whose arm-sites lack an inline
        # guard (M1/M2/M3/M8). M4/M5/M6/M7 are guarded at their arm-sites above.
        if 0 in DISABLE_MODES: m1l = m1s = False
        if 1 in DISABLE_MODES: m2l = m2s = False
        if 2 in DISABLE_MODES: m3l = m3s = False
        if 7 in DISABLE_MODES: m8l = m8s = False

        # ── Priority cascade
        # [v12.20 port-back] When ENABLE_M5/ENABLE_M7 are on, restore v12.20 chain:
        # M6 > M7 > M8 (fade) > M5 > M4 > M3 > M2 > M1.
        # When both off, this collapses to the v12.24+ chain M6 > M8 > M4 > M3 > M2 > M1.
        sel = -1; sl = False
        if   m6l: sel = 5; sl = True
        elif m6s: sel = 5
        elif m7l: sel = 6; sl = True
        elif m7s: sel = 6
        elif m8l: sel = 7; sl = True
        elif m8s: sel = 7
        elif m5l: sel = 4; sl = True
        elif m5s: sel = 4
        elif m4l: sel = 3; sl = True
        elif m4s: sel = 3
        elif m3l: sel = 2; sl = True
        elif m3s: sel = 2
        elif m2l: sel = 1; sl = True
        elif m2s: sel = 1
        elif m1l: sel = 0; sl = True
        elif m1s: sel = 0
        elif trl: sel = 4; sl = True   # [trend-long] lowest priority — never preempts M1/M4
        if sel < 0: return

        _mname = ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8")[sel]
        _fm = self.funnel_mode.setdefault(_mname, {"armed": 0, "qual": 0, "entered": 0})
        self.funnel["armed"] += 1; _fm["armed"] += 1
        self.fn_days_armed.add(bar.dt.date())

        # ── Regime filter ────────────────────────────────────────────────────
        tr, cr = self._regime(i, atr, vwap)
        ts     = self._trend_str(i, atr)
        vr     = self._vol_reg(i, atr)
        if not self._regime_ok(sel, sl, tr, cr, ts):
            self.funnel["regime"] += 1
            self.funnel_audit.append((bar.dt, _mname, "regime", f"tr={tr} cr={cr} ts={ts:.2f}"))
            return

        # ── [VWAP_FILTER_ALL] daily-VWAP directional filter (all modes) ────────
        # Only take trades aligned with session VWAP: longs need close > vwap,
        # shorts need close < vwap. Skipped until VWAP is valid (vwap > 0).
        if VWAP_FILTER_ALL and vwap > 0:
            if (sl and bar.close <= vwap) or (not sl and bar.close >= vwap):
                self.funnel["vwap_dir"] += 1
                self.funnel_audit.append((bar.dt, _mname, "vwap_dir",
                                          f"close={bar.close:.2f} vwap={vwap:.2f} side={'L' if sl else 'S'}"))
                return

        # ── [VWAP_NEAR_PTS] proximity filter (all modes) ──────────────────────
        # Only enter when price is within VWAP_NEAR_PTS points of the (anchored)
        # session VWAP. Skipped until VWAP is valid (vwap > 0).
        if VWAP_NEAR_PTS > 0 and vwap > 0:
            if abs(bar.close - vwap) > VWAP_NEAR_PTS:
                self.funnel["vwap_near"] += 1
                self.funnel_audit.append((bar.dt, _mname, "vwap_near",
                                          f"close={bar.close:.2f} vwap={vwap:.2f} dist={abs(bar.close-vwap):.2f}"))
                return
        if sel == 0 and vr == 0:          # M1: no trades in flat/low-ATR market
            self.funnel["m1_extra"] += 1
            self.funnel_audit.append((bar.dt, _mname, "m1_extra", ""))
            return
        if sel == 0 and 1200 <= bar.hhmm <= 1359:  # M1: dead zone (0% WR 12-14h)
            self.funnel["m1_extra"] += 1
            self.funnel_audit.append((bar.dt, _mname, "m1_extra", ""))
            return
        # M1 directional trend gate: don't buy into a downtrend / sell into an uptrend.
        # [armdiag fix] The `return` used to sit at the same indent as the LONG
        # test rather than inside it, so EVERY M1 selection returned and M1 never
        # took a trade in any run from this harness; the SHORT test and the second
        # return below it were unreachable. Faithful port of the cpp gate at
        # IOF_NQ_Autopilot.cpp:3797-3806, including its ATR>0 / Idx>=20 guard and
        # its fixed 20-bar lookback (the old min(20, i) diverged for i < 20).
        if sel == 0 and atr > 0 and i >= 20:
            pc20 = bar.close - self.bars[i - 20].close
            if (sl and pc20 < -atr * 0.5) or (not sl and pc20 > atr * 0.5):
                self.funnel["m1_extra"] += 1
                self.funnel_audit.append((bar.dt, _mname, "m1_extra",
                                          f"pc20={pc20:.2f} atr={atr:.2f} "
                                          f"side={'L' if sl else 'S'}"))
                return

        # ── RM floor ─────────────────────────────────────────────────────────
        # [v12.32-ab] Gate by full RM model (mirrors cpp line 3453). The legacy
        # self.risk.rm is also clamped to >= C_RM_FLOOR so it never gates; use
        # inst_risk.rm which can dip below the floor under the buggy model.
        if self.inst_risk.rm < C_RM_FLOOR:
            self.rm_gated += 1
            self.funnel["rm_floor"] += 1
            self.funnel_audit.append((bar.dt, _mname, "rm_floor", f"rm={self.inst_risk.rm:.2f}"))
            self._on_rm_gated(
                i, sel, sl, atr, ctrl, div, sc_l, sc_s, bk_vs,
                m6_sp, m6_t1, m6_t2,
                fade_active, fade_edge, fade_type, fd_sp, fd_t1, fd_t2,
            )
            return

        # ── Post-stop cooldown (direction-specific) ───────────────────────────
        if self.last_stop_bar >= 0 and i <= self.last_stop_bar + C_COOL_STOP:
            same = (sl and self.last_stop_dir > 0) or (not sl and self.last_stop_dir < 0)
            if same:
                self.funnel["cool_stop"] += 1
                self.funnel_audit.append((bar.dt, _mname, "cool_stop", ""))
                return

        # ── Quality score ─────────────────────────────────────────────────────
        # M1/M2/M3: (finalScore*100)/15 — need score >= 6 to clear floor 40.
        # M4/M7: finalScore*10 — Python sc max=5, needs sc>=5.
        # M6: use bk_vs (bkVerifyScore) directly — the mode-specific quality
        #     measure. bk_vs*10 maps 5→50, 6→60; C++ scL/scS achieves this
        #     via 12-component score which Python doesn't replicate.
        # [score-port] cpp:3955-3957 — finalScore is the SAME expression for every
        # mode: this bar's delta-gated score, falling back to the lean-back score.
        # The two special cases removed here were both compensations for the old
        # capped score: M1-M3 added ctrl on top (the cpp never does), and M6
        # substituted bk_vs because "Python doesn't replicate" the 12-component
        # score. It does now.
        if sl:
            final_sc = sc_l if d_l else (lb_sc_l if lb_l else 0)
        else:
            final_sc = sc_s if d_s else (lb_sc_s if lb_s else 0)
        q = qual100(sel, final_sc, fade_edge, fade_active)
        if sel == 4 and TREND_LONG and self._tr_q:   # [trend-long] use custom quality
            q = self._tr_q
        if sel == 0 and QUAL_FLOOR_M1 is not None:
            floor = QUAL_FLOOR_M1
        elif sel == 1 and QUAL_FLOOR_M2 is not None:
            floor = QUAL_FLOOR_M2
        elif sel == 2 and QUAL_FLOOR_M3 is not None:
            floor = QUAL_FLOOR_M3
        elif sel == 3 and QUAL_FLOOR_M4 is not None:
            floor = QUAL_FLOOR_M4
        elif sel == 4 and QUAL_FLOOR_M5 is not None:
            floor = QUAL_FLOOR_M5
        elif sel == 5 and QUAL_FLOOR_M6 is not None:
            floor = QUAL_FLOOR_M6
        elif sel == 6 and QUAL_FLOOR_M7 is not None:
            floor = QUAL_FLOOR_M7
        elif sel == 7 and QUAL_FLOOR_M8 is not None:
            floor = QUAL_FLOOR_M8
        else:
            floor = QUAL_FLOOR
        if q < floor:
            _m4mid = (M4_MIDDAY_ADMIT and sel == 3 and q >= M4_MIDDAY_Q_MIN
                      and M4_MIDDAY_START <= bar.hhmm < M4_MIDDAY_END)
            if not _m4mid:
                self.funnel["qual"] += 1; _fm["qual"] += 1
                self.funnel_qual_killed.append((_mname, q))
                self.funnel_audit.append((bar.dt, _mname, "qual", f"q={q} floor={floor} side={'L' if sl else 'S'}"))
                return
            self.funnel_audit.append((bar.dt, _mname, "m4midday_admit", f"q={q} hhmm={bar.hhmm} side={'L' if sl else 'S'}"))

        # ── Enter trade ───────────────────────────────────────────────────────
        self.funnel["entered"] += 1; _fm["entered"] += 1
        self.funnel_audit.append((bar.dt, _mname, "entered", f"q={q} side={'L' if sl else 'S'}"))
        self.fn_days_entered.add(bar.dt.date())
        self._enter(i, bar, sel, sl, atr, final_sc, ctrl, div,
                    tr, vr, cr, fade_active, fade_edge, fade_type,
                    m6_sp, m6_t1, m6_t2, fd_sp, fd_t1, fd_t2, m7_stop)

    # ──────────────────────────────────────────────────────────────────────────
    def _enter(self, i, bar, sel, sl, atr, score, ctrl, div,
               tr, vr, cr, fade_active, fade_edge, fade_type,
               m6_sp, m6_t1, m6_t2, fd_sp, fd_t1, fd_t2, m7_stop=0.0):

        # Entry fill model — controlled by ENTRY_ORD (see constants block).
        # Mirrors cpp ENTRY_ORD semantics (sc.Input[8] in IOF_NQ_Autopilot.cpp).
        #   0 = MARKET      → fill at signal bar close (no slippage).
        #   1 = LIMIT       → place buy/sell limit at signal close; fill only
        #                     if the next bar trades through that price
        #                     (pullback for long, push-up for short). If the
        #                     limit isn't touched, the signal is discarded.
        #                     Conservative — passive limit, no spread paid.
        #   2 = LIMIT+2t    → marketable limit at signal close ± 2 ticks (in
        #                     direction of trade). Cpp: Price1 = entryPrice
        #                     ± TICK*2 — paying 2 ticks to ensure a cross.
        #                     We assume immediate fill at the limit price
        #                     (i.e., 2-tick slippage worse than mid).
        if ENTRY_ORD == 0:
            ep = bar.close
        elif ENTRY_ORD == 1:
            if i + 1 >= len(self.bars):
                return  # last bar — no next bar to fill against
            nb = self.bars[i + 1]
            target = bar.close
            if sl and nb.low <= target:
                ep = target
            elif (not sl) and nb.high >= target:
                ep = target
            else:
                return  # limit not touched — no entry
        elif ENTRY_ORD == 2:
            ep = bar.close + (TICK * 2.0 if sl else -TICK * 2.0)
        else:
            ep = bar.close  # unknown mode — default to market

        # Stop / target
        if FIXED_SCALE:
            # [FIXED_SCALE] fixed $-per-contract distances -> points via PT_VAL.
            # t1 != t2: lot1 books at t1, the runner at t2, shared stop at sp.
            sd  = FIXED_SCALE_STOP_USD / PT_VAL
            d1  = FIXED_SCALE_TP1_USD  / PT_VAL
            d2  = FIXED_SCALE_TP2_USD  / PT_VAL
            if sl:
                sp = round((ep - sd) / TICK) * TICK
                t1 = round((ep + d1) / TICK) * TICK
                t2 = round((ep + d2) / TICK) * TICK
            else:
                sp = round((ep + sd) / TICK) * TICK
                t1 = round((ep - d1) / TICK) * TICK
                t2 = round((ep - d2) / TICK) * TICK
        elif FIXED_BRACKET:
            # [FIXED_BRACKET] fixed tick distances; t2==t1 (single OCO target,
            # no partial). Skips the ATR / VP / sanity machinery below.
            sd = FIXED_STOP_TICKS   * TICK
            td = FIXED_TARGET_TICKS * TICK
            if sl:
                sp = round((ep - sd) / TICK) * TICK
                t1 = t2 = round((ep + td) / TICK) * TICK
            else:
                sp = round((ep + sd) / TICK) * TICK
                t1 = t2 = round((ep - td) / TICK) * TICK
        elif sel == 5 and m6_sp:
            sp, t1, t2 = m6_sp, m6_t1, m6_t2
        elif sel == 7 and fade_active:
            sp, t1, t2 = fd_sp, fd_t1, fd_t2
        elif sel == 4 and TREND_LONG and self._tr_stop:   # [trend-long] swing-low stop, wide ATR targets
            sp = self._tr_stop
            t1 = round((ep + atr * TREND_T1_ATR) / TICK) * TICK
            t2 = round((ep + atr * TREND_T2_ATR) / TICK) * TICK
        else:
            stop_cl = C_STOP_CL
            if STOP_MODEL == "wide_for_hiconv" and (score >= 7 or sel == 1):
                stop_cl = C_STOP_CL_HICONV
            sd = max(C_STOP_FL, min(stop_cl, atr * C_STOP_ATR))
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
            # [v12.20 port-back] M7 stop = beyond prior-imbalance extreme (cpp line 3465).
            # Targets stay ATR-based. Only override stop if it's tighter than the ATR
            # default — wider prev-extreme stops keep the ATR stop.
            if sel == 6 and m7_stop:
                if sl and m7_stop > sp: sp = round(m7_stop / TICK) * TICK
                if not sl and m7_stop < sp: sp = round(m7_stop / TICK) * TICK

        # [vp-targets] Override T1/T2 with VP levels (per-target ATR fallback).
        if VP_TARGETS and not FIXED_BRACKET and not FIXED_SCALE \
                and self.vp_v[i].valid and atr > 0:
            lvls = sorted(lv for lv in (self.vp_v[i].poc, self.vp_v[i].vah,
                                        self.vp_v[i].val)
                          if lv > 0)
            mind = atr * VP_T_MIN_ATR
            if sl:
                above = [lv for lv in lvls if lv >= ep + mind]
                if above:
                    t1 = round(above[0] / TICK) * TICK
                    nxt = [lv for lv in above[1:] if lv > above[0]]
                    t2 = round(nxt[0] / TICK) * TICK if nxt else max(t2, t1 + TICK)
            else:
                below = [lv for lv in lvls if lv <= ep - mind]
                if below:
                    t1 = round(below[-1] / TICK) * TICK
                    nxt = [lv for lv in below[:-1] if lv < below[-1]]
                    t2 = round(nxt[-1] / TICK) * TICK if nxt else min(t2, t1 - TICK)

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
        self.qty       = self._lot_count()
        self.lots_open = self.qty
        self._realized = 0.0
        # [overnight-slip-ab] thin-book entry haircut, booked as banked cost so
        # it lands in the final pnl at _close (works for scale-out legs too)
        if OVERNIGHT_SLIP_TICKS > 0 and bar.hhmm < OVERNIGHT_END_HHMM:
            self._realized -= OVERNIGHT_SLIP_TICKS * TICK * PT_VAL * self.qty
        # [trend iter-2] mark trend-long positions so _manage can apply TFAIL
        self._is_trend = (sel == 4 and TREND_LONG and bool(self._tr_stop))

        t = Trade(
            date       = bar.dt.strftime("%Y-%m-%d"),
            time       = bar.dt.strftime("%H:%M:%S"),
            event      = "SETUP",
            side       = "LONG" if sl else "SHORT",
            # [M4-OR levels] label OR-sourced M4 arms "M4o" so per-mode tables
            # attribute them separately. sel stays 3 — floors/gates unaffected.
            mode       = ("M4o" if (sel == 3 and self._m4_or_only)
                          else MODE_NAMES[sel]),
            entry      = round(ep, 2),
            sl         = round(sp, 2),
            tp1        = round(t1, 2),
            tp2        = round(t2, 2),
            score      = score,
            ctrl       = ctrl,
            div_str    = div.strength,
            delta      = float(bar.ask_vol - bar.bid_vol),
            bar_spd    = float(bar.volume),
            risk_mult  = round(self.risk.rm, 3),
            qty        = self.qty,
            trend_reg  = tr,
            vol_reg    = vr,
            chop_reg   = cr,
            fade_edge  = fade_edge,
            fade_type  = fade_type,
            day_pnl    = round(self.day_pnl, 2),
            tot_pnl    = round(self.tot_pnl, 2),
            trend_ts   = round(self._tr_ts, 3) if sel == 4 else 0.0,
        )
        # [regime-day instrumentation] day-anchored features at entry (all modes)
        dxl, dxh, dos, dvw = self._day_features(i, atr)
        t.day_ext_lo = round(dxl, 3); t.day_ext_hi = round(dxh, 3)
        t.day_os     = round(dos, 3); t.day_vws    = round(dvw, 3)
        self.cur_trade = t
        self.out.append(t)
        self.day_trades += 1

    # ──────────────────────────────────────────────────────────────────────────
    def _vwap_touch(self, i: int, bar: Bar, atr: float, vwap: float):
        """[VWAP_TOUCH_TRIGGER] Place an order at every fresh return to the
        (anchored) VWAP: long if the VWAP is rising, short if falling. Bypasses
        the M1..M8 cascade; reuses the standard ATR stop + T1/T2 + trail exit."""
        if vwap <= 0 or atr <= 0:                                   return
        if self.day_trades >= MAX_TRADES:                           return
        if self._vt_last_bar >= 0 and i - self._vt_last_bar < VWAP_TOUCH_COOL:
            return
        # Proximity band, measured in VWAP standard deviations (VWAP_NEAR_SD) if
        # set, else in fixed points (VWAP_NEAR_PTS).
        if VWAP_NEAR_SD > 0:
            vsd = self.vwap_sd[i]
            if vsd <= 0:                                            return
            thresh = VWAP_NEAR_SD * vsd
        else:
            thresh = VWAP_NEAR_PTS
        # This bar closes within the band …
        if abs(bar.close - vwap) > thresh:                          return
        # … and it's a FRESH return — the prior bar was OUTSIDE the band — so we
        # fire once per pullback, not every bar price sits on the VWAP.
        if i >= 1:
            pv = self.vwap_v[i - 1]
            if VWAP_NEAR_SD > 0:
                pvsd = self.vwap_sd[i - 1]
                pthr = VWAP_NEAR_SD * pvsd if pvsd > 0 else thresh
            else:
                pthr = VWAP_NEAR_PTS
            if pv > 0 and abs(self.bars[i - 1].close - pv) <= pthr:
                return
        # Direction from the VWAP slope over the lookback (rising→long, fall→short)
        lb = VWAP_TOUCH_SLOPE_LB
        if i < lb:                                                  return
        pv_lb = self.vwap_v[i - lb]
        if pv_lb <= 0:                                              return
        slope = vwap - pv_lb
        tol   = atr * VWAP_TOUCH_SLOPE_TOL
        if   slope >  tol: sl = True     # rising VWAP → long the pullback
        elif slope < -tol: sl = False    # falling VWAP → short the reject
        else:              return        # flat VWAP → no trend, skip

        # Pullback-from-trend-side gate: the prior (out-of-band) bar must be on
        # the trend side — above the VWAP for a long, below it for a short — so we
        # take a pullback INTO the VWAP, not a momentum cross THROUGH it.
        if VWAP_TOUCH_PULLBACK and i >= 1:
            pv = self.vwap_v[i - 1]
            pc = self.bars[i - 1].close
            if pv <= 0:                                             return
            if sl and pc <= pv:                                     return
            if (not sl) and pc >= pv:                               return

        # Delta (order-flow) confirmation: at least VWAP_TOUCH_DELTA_MIN of the
        # last 5 bars must lean the trade's way (buyers for long, sellers short).
        if VWAP_TOUCH_DELTA_MIN > 0:
            lo = max(0, i - 4)
            if sl:
                sc = sum(1 for j in range(lo, i + 1)
                         if self.bars[j].ask_vol > self.bars[j].bid_vol)
            else:
                sc = sum(1 for j in range(lo, i + 1)
                         if self.bars[j].bid_vol > self.bars[j].ask_vol)
            if sc < VWAP_TOUCH_DELTA_MIN:                           return

        div = divergence(self.bars, i, self.cum_d, atr)

        # Absorption / delta-divergence gate: require signed divergence strength
        # to back the trade — for a LONG, bullish absorption (price dips to VWAP
        # but delta holds / down-bars carry buy-delta = sellers absorbed); for a
        # SHORT, the bearish mirror. This is the OPPOSITE of delta confirmation:
        # we want the pullback's flow to be FAILING, not agreeing. strength maps
        # trend_div=±2, swing_div=±2, persist_abs=±1 (range −5..+5).
        if VWAP_TOUCH_ABSORB > 0:
            if sl and div.strength < VWAP_TOUCH_ABSORB:             return
            if (not sl) and div.strength > -VWAP_TOUCH_ABSORB:      return

        self.funnel["armed"] += 1
        self._enter(i, bar, 2, sl, atr, 6, 0, div,
                    0, 0, 0, False, 0.0, 0,
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        if self.in_pos and self.cur_trade is not None:
            self.cur_trade.mode = "VT"    # relabel the M3 slot as the touch engine
            self.funnel["entered"] += 1
            self._vt_last_bar = i

    # ──────────────────────────────────────────────────────────────────────────
    # Hook fired when inst_risk.rm < C_RM_FLOOR kills a SETUP. Default is a
    # no-op; rm_gated_audit.py overrides this to record the would-be setup and
    # simulate its outcome. Args mirror the locals at the gate point.
    def _on_rm_gated(self, *args, **kwargs):
        pass

    # Hook fired whenever an M4 (sweep+reclaim) shape arms, BEFORE priority/gate
    # selection — so audits see every M4 signal, not just the rare ones that fire.
    # Default no-op; audit_m4_overshoot.py overrides to forward-simulate the path.
    def _on_m4_arm(self, i: int, is_long: bool, ep: float, atr: float, sw: float = 0.0):
        pass

    # Hook fired whenever ANY mode arms (M1/M2/M3/M4/M6/M8), before priority
    # cascade and downstream gates. ctx is a dict of at-bar features + per-mode
    # arm flags. Default no-op; xgb_candidate_label.py overrides to label every
    # armed candidate's forward outcome for ML training.
    def _on_any_arm(self, i: int, ctx: dict):
        pass

    # ──────────────────────────────────────────────────────────────────────────
    # [regime-day instrumentation] Day-anchored features at bar i, causal (only
    # today's bars up to and including i). Cheap: called once per ENTRY, scans
    # back to the day boundary (~a few hundred bars max).
    def _day_features(self, i: int, atr: float):
        if atr <= 0:
            return 0.0, 0.0, 0.0, 0.0
        bar = self.bars[i]
        j0 = i
        while j0 > 0 and self.bars[j0 - 1].date_tag == bar.date_tag:
            j0 -= 1
        dlo = min(self.bars[j].low  for j in range(j0, i + 1))
        dhi = max(self.bars[j].high for j in range(j0, i + 1))
        n = above = below = 0
        for j in range(j0, i + 1):
            if j < len(self.vwap_v) and self.vwap_v[j] > 0:
                n += 1
                if   self.bars[j].close > self.vwap_v[j]: above += 1
                elif self.bars[j].close < self.vwap_v[j]: below += 1
        d_os = (above - below) / n if n else 0.0
        js = max(j0, i - 20)
        d_vws = 0.0
        if i < len(self.vwap_v) and js < len(self.vwap_v) \
           and self.vwap_v[i] > 0 and self.vwap_v[js] > 0:
            d_vws = (self.vwap_v[i] - self.vwap_v[js]) / atr
        return (bar.close - dlo) / atr, (dhi - bar.close) / atr, d_os, d_vws

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

        # [FIXED_BRACKET] pure held-to-stop/target OCO — no CB/trail/BE/scratch.
        if FIXED_BRACKET:
            self._manage_fixed(i, bar); return

        # [FIXED_SCALE] 2-lot fixed OCO — same "nothing to manage" contract as
        # FIXED_BRACKET, so it also bypasses CB / half-MFE / scratch / trail.
        if FIXED_SCALE:
            self._manage_fixed_scaled(i, bar); return

        # Circuit-breaker
        op = (bar.close - self.entry_px) if il else (self.entry_px - bar.close)
        max_risk = max(abs(self.entry_px - self.stop_px) * 3, atr * 3) * PT_VAL
        if op * PT_VAL < -max_risk:
            self._close(i, bar.close, "CB"); return

        # [half-MFE-giveback] Close at market when running MFE has retraced by
        # ≥HALF_MFE_GIVEBACK (fraction) AND MFE peak was ≥HALF_MFE_MIN_PTS.
        # Only active pre-T1; once T1 hits, trail logic owns the exit.
        if HALF_MFE_EXIT and not self.t1_hit and self._mfe >= HALF_MFE_MIN_PTS:
            if op <= self._mfe * (1.0 - HALF_MFE_GIVEBACK):
                self._close(i, bar.close, "HALF_MFE"); return

        if SCALE_OUT and self.qty >= 2:
            self._manage_scaled(i, bar, atr); return

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

        # [trend iter-2] TFAIL — reclaim-failed exit for trend-longs. The mode's
        # thesis is "price reclaimed VWAP"; a close back below VWAP (minus a
        # small ATR buffer) pre-T1 invalidates it. Exit at market instead of
        # riding to the wide swing-low stop. Runs after the T1 checks so a
        # same-bar T1 touch wins; before early-scratch (more specific signal).
        if TREND_FAIL_EXIT and not self.t1_hit and i > self.entry_bar \
           and getattr(self, "_is_trend", False):
            vw = self.vwap_v[i] if i < len(self.vwap_v) else 0.0
            if vw > 0 and bar.close < vw - atr * TREND_FAIL_BUF:
                self._close(i, bar.close, "TFAIL"); return

        # [early-scratch] see flag block; runs after T1 check so a same-bar
        # T1 touch wins, before the stop becomes eligible at entry+4.
        if EARLY_SCRATCH and not self.t1_hit and i == self.entry_bar + ES_AT_BAR:
            sd = abs(self.entry_px - self.stop_px)
            if self._mfe < sd * ES_MFE_FRAC and op > -sd:
                self._close(i, bar.close, "SCRATCH"); return

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
        # Close all remaining lots at ex_px; add any banked partials (_realized).
        lots = getattr(self, "lots_open", 1) or 1
        leg  = (((ex_px - self.entry_px) if il else (self.entry_px - ex_px)) * PT_VAL
                - COMMISSION) * lots
        # [overnight-slip-ab] thin-book haircut on market-type overnight exits;
        # T1/T2 are passive limit fills and don't cross the spread
        if OVERNIGHT_SLIP_TICKS > 0 and bar.hhmm < OVERNIGHT_END_HHMM \
                and reason not in ("T1", "T2"):
            leg -= OVERNIGHT_SLIP_TICKS * TICK * PT_VAL * lots
        pnl  = getattr(self, "_realized", 0.0) + leg

        self.day_pnl += pnl
        self.tot_pnl += pnl

        if pnl < 0 and reason in ("STOP", "CB", "TRAIL", "SCRATCH", "TFAIL"):
            self.last_stop_bar = i
            self.last_stop_dir = 1 if il else -1
        if pnl < 0: self.last_loss_bar = i
        self.last_trade_bar = i
        self.risk.trade_done(pnl)
        self.inst_risk.on_trade_close()  # [v12.32-ab] reset bars_since_last_trade

        # [v12.24] Emit a new EXIT trade via dataclasses.replace() rather than
        # mutating the SETUP record in place. The old code mutated cur_trade
        # then appended it; since the SETUP record had already been appended
        # to self.out at entry time, the same Python object ended up listed
        # twice — both showing event="EXIT" after mutation. Bug surfaced as
        # CSV double-writes and inflated W/L counts in the printed summary.
        exit_trade = replace(
            self.cur_trade,
            event   = "EXIT",
            exit_px = round(ex_px, 2),
            exit_rsn= reason,
            hold    = i - self.entry_bar,
            mae     = round(self._mae, 2),
            mfe     = round(self._mfe, 2),
            day_pnl = round(self.day_pnl, 2),
            tot_pnl = round(self.tot_pnl, 2),
        )
        self.out.append(exit_trade)

        self.in_pos = False; self.cur_trade = None
        self.t1_hit = False; self.t1_bar = -1
        self.lots_open = 0; self._realized = 0.0

    # ──────────────────────────────────────────────────────────────────────────
    def _manage_fixed(self, i: int, bar: Bar):
        """[FIXED_BRACKET] Whole-position OCO: fixed stop / fixed target, held to
        one or the other. The attached bracket goes live at the entry-bar close,
        so exits are checked from the next bar on. When both the stop and target
        lie inside one bar's range we assume the STOP filled first (conservative;
        we can't see intrabar sequence from OHLC). FIXED_PROFIT_LOCK / _LOSS_LOCK
        model the cpp daily caps that include OpenProfitLoss — they act as a
        de-facto tighter target/stop when the $ threshold is nearer than the tick
        bracket."""
        il = self.is_long
        if i <= self.entry_bar:                       # fills at entry-bar close
            return

        stop_px = self.stop_px
        tgt_px  = self.tp1_px
        stop_rsn, tgt_rsn = "STOP", "T2"

        # Open-P&L profit lock → de-facto target at the $ threshold price.
        if FIXED_PROFIT_LOCK > 0 and self.qty > 0:
            lock_pts = FIXED_PROFIT_LOCK / (PT_VAL * self.qty)
            lock_px  = round((self.entry_px + lock_pts) / TICK) * TICK if il \
                       else round((self.entry_px - lock_pts) / TICK) * TICK
            if (il and lock_px < tgt_px) or ((not il) and lock_px > tgt_px):
                tgt_px, tgt_rsn = lock_px, "DAILY_PROFIT"
        # Open-P&L loss lock → de-facto tighter stop at the $ threshold price.
        if FIXED_LOSS_LOCK > 0 and self.qty > 0:
            lock_pts = FIXED_LOSS_LOCK / (PT_VAL * self.qty)
            lock_px  = round((self.entry_px - lock_pts) / TICK) * TICK if il \
                       else round((self.entry_px + lock_pts) / TICK) * TICK
            if (il and lock_px > stop_px) or ((not il) and lock_px < stop_px):
                stop_px, stop_rsn = lock_px, "DAILY_LOSS"

        stop_hit = (bar.low <= stop_px) if il else (bar.high >= stop_px)
        tgt_hit  = (bar.high >= tgt_px) if il else (bar.low  <= tgt_px)
        if stop_hit:                                  # conservative: stop wins ties
            self._close(i, stop_px, stop_rsn); return
        if tgt_hit:
            self._close(i, tgt_px, tgt_rsn); return

    def _manage_fixed_scaled(self, i: int, bar: Bar):
        """[FIXED_SCALE] 2-lot fixed OCO: lot1 -> TP1, runner -> TP2, one shared
        stop. No trail, no scratch, no circuit-breaker — the whole bracket is
        attached at the entry-bar close, so exits are checked from the next bar
        on (same convention as _manage_fixed).

        Tie-breaking is conservative throughout, since OHLC hides the intrabar
        sequence: when a bar's range spans both the stop and a target, the STOP
        is assumed to have filled first and the whole position exits there.

        With FIXED_SCALE_BE the shared stop moves to breakeven once TP1 fills,
        but only from the FOLLOWING bar — booking the partial and re-arming the
        stop inside the same bar would be free look-ahead."""
        il = self.is_long
        if i <= self.entry_bar:                       # fills at entry-bar close
            return

        stop_hit = (bar.low <= self.stop_px) if il else (bar.high >= self.stop_px)
        if stop_hit:                                  # conservative: stop wins ties
            # Pre-TP1 this takes both lots; post-TP1 only the runner is left, and
            # _close bills whatever lots_open still holds.
            self._close(i, self.stop_px, "STOP" if not self.t1_hit else
                        ("BE" if FIXED_SCALE_BE else "STOP"))
            return

        if not self.t1_hit:
            hit1 = (bar.high >= self.tp1_px) if il else (bar.low  <= self.tp1_px)
            hit2 = (bar.high >= self.tp2_px) if il else (bar.low  <= self.tp2_px)
            # Single-lot degenerate case: there is no runner to peel, so TP1 IS
            # the whole exit. Peeling here would drop lots_open to 0 while the
            # position stayed open, and _close's `or 1` fallback would then bill
            # a SECOND lot at the eventual exit -- double-booking every winner.
            if self.qty < 2:
                if hit1: self._close(i, self.tp1_px, "T1")
                return
            if hit1 and hit2:                         # bar blew through both legs
                self._peel(self.tp1_px, 1)
                self._close(i, self.tp2_px, "T2"); return
            if hit1:                                  # book lot1, runner lives on
                self._peel(self.tp1_px, 1)
                self.t1_hit = True; self.t1_bar = i
                if FIXED_SCALE_BE:
                    self.stop_px = round(self.entry_px / TICK) * TICK
            return

        # Runner: hard TP2 only — no trail (the bracket is untouched by design).
        if (bar.high >= self.tp2_px) if il else (bar.low <= self.tp2_px):
            self._close(i, self.tp2_px, "T2"); return

    def _lot_count(self) -> int:
        """Contracts for this entry (1 unless SCALE_OUT)."""
        if FIXED_SCALE:   return FIXED_SCALE_QTY
        if FIXED_BRACKET: return FIXED_QTY
        if not SCALE_OUT: return 1
        if SIZE_BY_RM:    return max(1, int(round(BASE_QTY * self.risk.rm)))
        return BASE_QTY

    def _peel(self, px: float, lots: int):
        """Book `lots` contracts at px into _realized; reduce lots_open."""
        il  = self.is_long
        pts = (px - self.entry_px) if il else (self.entry_px - px)
        self._realized += pts * PT_VAL * lots - COMMISSION * lots
        self.lots_open -= lots

    def _manage_scaled(self, i: int, bar: Bar, atr: float):
        """2-lot scale-out: lot1 -> TP1, runner -> TP2 else trail; shared SL ->
        breakeven+buf after TP1. Engaged only when self.qty >= 2."""
        il = self.is_long
        if not self.t1_hit:
            hitT1 = (il and bar.high >= self.tp1_px) or (not il and bar.low <= self.tp1_px)
            hitT2 = (il and bar.high >= self.tp2_px) or (not il and bar.low <= self.tp2_px)
            if hitT1 and hitT2:                  # bar blew through both
                self._peel(self.tp1_px, 1)
                self._close(i, self.tp2_px, "T2"); return
            if hitT1:                            # peel lot1, run the rest
                self._peel(self.tp1_px, 1)
                self.t1_hit = True; self.t1_bar = i
                buf = atr * C_BE_ATR
                self.stop_px = (round((self.entry_px + buf) / TICK) * TICK if il
                                else round((self.entry_px - buf) / TICK) * TICK)
                return
            if i > self.entry_bar + 3:           # stop before TP1 -> all lots out
                sth = (il and bar.low <= self.stop_px + TICK) or \
                      (not il and bar.high >= self.stop_px - TICK)
                if sth: self._close(i, self.stop_px, "STOP"); return
            return
        # Runner (after TP1): TP2 hard, else trail (mirrors single-lot trail)
        if (il and bar.high >= self.tp2_px) or (not il and bar.low <= self.tp2_px):
            self._close(i, self.tp2_px, "T2"); return
        delay = C_TRAIL_DLY * 3
        if self.t1_bar >= 0 and i >= self.t1_bar + delay:
            t1d = abs(self.tp1_px - self.entry_px)
            cur = (bar.close - self.entry_px) if il else (self.entry_px - bar.close)
            btr = C_TRAIL_ATR * 2.5 if (t1d > 0 and cur < t1d * 2) else C_TRAIL_ATR
            td  = atr * btr
            if t1d > 0 and cur > t1d * 2: td = min(td, atr * 0.75)
            min_sp = round((self.entry_px + atr * 0.3) / TICK) * TICK if il \
                     else round((self.entry_px - atr * 0.3) / TICK) * TICK
            if il:
                ns = round((bar.close - td) / TICK) * TICK
                self.stop_px = max(max(self.stop_px, ns), min_sp)
            else:
                ns = round((bar.close + td) / TICK) * TICK
                self.stop_px = min(min(self.stop_px, ns), min_sp)
        if i > self.entry_bar + 3:
            trail_hit = (il and bar.low <= self.stop_px + TICK) or \
                        (not il and bar.high >= self.stop_px - TICK)
            if trail_hit: self._close(i, self.stop_px, "TRAIL"); return

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
        dp = sum(abs(self.bars[j].ask_vol - self.bars[j].bid_vol) /
                 max(1, self.bars[j].ask_vol + self.bars[j].bid_vol)
                 for j in range(i - lb + 1, i + 1)) / lb
        ts = dp / 0.1
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
        dp = sum(abs(self.bars[j].ask_vol - self.bars[j].bid_vol) /
                 max(1, self.bars[j].ask_vol + self.bars[j].bid_vol)
                 for j in range(i - lb + 1, i + 1)) / lb
        return dp / 0.1  # ts=1.0 at 10% avg delta imbalance

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
    "TrendReg","VolReg","ChopReg","Version","TrendTS",
    "DayExtLo","DayExtHi","DayOS","DayVws",
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
                "Version": "v12.25-py", "TrendTS": t.trend_ts,
                "DayExtLo": t.day_ext_lo, "DayExtHi": t.day_ext_hi,
                "DayOS": t.day_os, "DayVws": t.day_vws,
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
    # Default output CSV name derived from input filename
    stem = os.path.splitext(os.path.basename(scid))[0]
    default_out = os.path.join(BASE_DIR, f"IOF_NQ_backtest_{stem}.csv")
    out  = sys.argv[2] if len(sys.argv) > 2 else default_out

    # Extract if still zipped
    if not os.path.exists(scid) and os.path.exists(ZIP_PATH):
        print(f"Extracting {ZIP_PATH} ...")
        with zipfile.ZipFile(ZIP_PATH, "r") as zf:
            zf.extractall(BASE_DIR)
        print(f"  Extracted to {BASE_DIR}")

    if not os.path.exists(scid):
        print(f"ERROR: {scid} not found."); sys.exit(1)

    scale = detect_price_scale(scid)
    fsize = os.path.getsize(scid)
    nrecs = (fsize - SCID_HDR_SIZE) // SCID_REC_SIZE
    print(f"Reading {scid}")
    print(f"  File size: {fsize/1e9:.2f} GB  ({nrecs:,} records)  price_scale=÷{scale:.0f}")
    recs = read_scid(scid)
    print(f"  Loaded {len(recs):,} records.")

    print(f"Building {TARGET_VOL}-contract volume bars ...")
    bars = build_volume_bars(recs, price_scale=scale)
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

    if ARM_CSV:
        with open(ARM_CSV, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Time", "BarIdx", "Armed", "Sel", "SelSide"])
            w.writerows(bt.arm_rows)
        print(f"Arm CSV written to: {ARM_CSV}  ({len(bt.arm_rows):,} armed bars)")
    return bt  # [v12.32-ab] expose Backtester for harness access to rm_gated / inst_risk


if __name__ == "__main__":
    main()
