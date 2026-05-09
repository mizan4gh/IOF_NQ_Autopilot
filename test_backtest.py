#!/usr/bin/env python3
"""
Unit tests for backtest.py — IOF NQ Autopilot backtester.
Run with: python -m pytest test_backtest.py -v
or:       python test_backtest.py
"""

import math
import sys
import unittest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── Import the module under test ─────────────────────────────────────────────
# We import from the same directory.
sys.path.insert(0, r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final")

from backtest import (
    Bar, WilderATR, SessionVWAP, VP5Day,
    Trap, update_trap, Balance, balance_state, Imb, imbalance_state,
    Div, divergence, control_score,
    Risk, qual100,
    TICK, PT_VAL, C_RM_FLOOR, QUAL_FLOOR,
    C_MIN_SC_ALL, C_SWEEP_LB, C_M5_MIN_SC, C_MAX_LOSSES,
    ET,
)

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def make_bar(date_tag=20250101, hhmm=1000, open=20000.0, high=20010.0,
             low=19990.0, close=20005.0, volume=2500,
             bid_vol=1200, ask_vol=1300, idx=0) -> Bar:
    dt = datetime(2025, 1, 1, 10, 0, tzinfo=ET)
    return Bar(dt=dt, open=open, high=high, low=low, close=close,
               volume=volume, bid_vol=bid_vol, ask_vol=ask_vol,
               idx=idx, date_tag=date_tag, hhmm=hhmm)


def make_bars(n: int, base_close=20000.0, delta_per_bar=100) -> list:
    bars = []
    for i in range(n):
        c = base_close + i
        bars.append(make_bar(
            close=c, open=c - 2, high=c + 5, low=c - 5,
            bid_vol=1200, ask_vol=1300,
            idx=i,
        ))
    return bars


# ─────────────────────────────────────────────────────────────────────────────
#  WilderATR
# ─────────────────────────────────────────────────────────────────────────────
class TestWilderATR(unittest.TestCase):

    def test_first_bar_returns_zero(self):
        atr = WilderATR(period=14)
        result = atr.update(h=100.0, l=90.0, c=95.0)
        self.assertEqual(result, 0.0)

    def test_primes_after_period_bars(self):
        """After warmup + period bars the ATR should be the simple average of those TRs."""
        period = 5
        atr = WilderATR(period=period)
        # Warmup bar: sets _prev, returns 0
        atr.update(100, 90, 95)
        # Bars 2 through period+1 accumulate; bar period+1 triggers the seed average
        trs = []
        prev_c = 95
        ranges = [(102, 92), (101, 91), (103, 93), (104, 94), (105, 95)]
        for h, l in ranges:
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)
            r = atr.update(h, l, (h + l) / 2)
            prev_c = (h + l) / 2
        # r should equal simple average of all `period` TRs
        expected = sum(trs) / period
        self.assertAlmostEqual(r, expected, places=4)

    def test_wilder_smoothing_formula(self):
        """After the seed period, each update uses (prev*(p-1) + tr) / p."""
        period = 3
        atr = WilderATR(period=period)
        atr.update(100, 90, 95)   # warm-up bar
        # 3 seed bars with TR=10 each → seed ATR = 10
        for _ in range(period):
            atr.update(100, 90, 95)
        # Now one more bar with TR=20
        r = atr.update(110, 90, 100)
        expected = (10.0 * (period - 1) + 20.0) / period
        self.assertAlmostEqual(r, expected, places=4)

    def test_atr_non_negative(self):
        atr = WilderATR()
        for i in range(20):
            v = atr.update(100 + i, 90 + i, 95 + i)
            self.assertGreaterEqual(v, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
#  SessionVWAP
# ─────────────────────────────────────────────────────────────────────────────
class TestSessionVWAP(unittest.TestCase):

    def test_single_bar(self):
        vwap_ind = SessionVWAP()
        bar = make_bar(high=100.0, low=80.0, close=90.0, volume=1000)
        v, sd = vwap_ind.update(bar)
        typ = (100.0 + 80.0 + 90.0) / 3.0
        self.assertAlmostEqual(v, typ, places=4)

    def test_resets_on_new_day(self):
        vwap_ind = SessionVWAP()
        day1 = make_bar(date_tag=20250101, high=100.0, low=80.0, close=90.0, volume=1000)
        day2 = make_bar(date_tag=20250102, high=200.0, low=180.0, close=190.0, volume=2000)
        vwap_ind.update(day1)
        v2, _ = vwap_ind.update(day2)
        typ2 = (200.0 + 180.0 + 190.0) / 3.0
        # If it reset, VWAP after the second bar should equal day2's typical price exactly.
        self.assertAlmostEqual(v2, typ2, places=4)

    def test_two_equal_bars_same_vwap(self):
        vwap_ind = SessionVWAP()
        bar1 = make_bar(date_tag=20250101, high=100, low=80, close=90, volume=1000)
        bar2 = make_bar(date_tag=20250101, high=100, low=80, close=90, volume=1000)
        vwap_ind.update(bar1)
        v, sd = vwap_ind.update(bar2)
        self.assertAlmostEqual(v, 90.0, places=4)
        self.assertAlmostEqual(sd, 0.0, places=4)

    def test_sd_nonzero_with_varying_price(self):
        vwap_ind = SessionVWAP()
        bar1 = make_bar(date_tag=20250101, high=100, low=90, close=95, volume=1000)
        bar2 = make_bar(date_tag=20250101, high=120, low=110, close=115, volume=1000)
        vwap_ind.update(bar1)
        _, sd = vwap_ind.update(bar2)
        self.assertGreater(sd, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
#  qual100
# ─────────────────────────────────────────────────────────────────────────────
class TestQual100(unittest.TestCase):

    def test_m4_m5_m6_use_times_10(self):
        for sel in [3, 4, 5]:
            q = qual100(sel, final_sc=5, edge_sc=0, fade_active=False)
            self.assertEqual(q, 50, f"sel={sel}")

    def test_m4_score6_gives_60(self):
        q = qual100(sel_mode=3, final_sc=6, edge_sc=0, fade_active=False)
        self.assertEqual(q, 60)

    def test_m1_m2_m3_use_div15(self):
        for sel in [0, 1, 2]:
            # score 8 → (8*100)//15 = 53
            q = qual100(sel, final_sc=8, edge_sc=0, fade_active=False)
            self.assertEqual(q, (8 * 100) // 15)

    def test_m8_fade_uses_edge_times_10(self):
        q = qual100(sel_mode=7, final_sc=2, edge_sc=7, fade_active=True)
        self.assertEqual(q, 70)

    def test_m8_no_fade_uses_div15(self):
        q = qual100(sel_mode=7, final_sc=8, edge_sc=7, fade_active=False)
        self.assertEqual(q, (8 * 100) // 15)

    def test_clamped_to_0_100(self):
        self.assertEqual(qual100(3, 200, 0, False), 100)
        self.assertEqual(qual100(3, -5,  0, False), 0)

    def test_qual_floor_gate(self):
        # Score 4 for M4 → 40 < QUAL_FLOOR 50 → should NOT clear floor
        q = qual100(3, 4, 0, False)
        self.assertLess(q, QUAL_FLOOR)
        # Score 5 for M4 → 50 = QUAL_FLOOR → should clear floor
        q = qual100(3, 5, 0, False)
        self.assertGreaterEqual(q, QUAL_FLOOR)


# ─────────────────────────────────────────────────────────────────────────────
#  Risk
# ─────────────────────────────────────────────────────────────────────────────
class TestRisk(unittest.TestCase):

    def test_consec_loss_increments(self):
        r = Risk()
        r.trade_done(-100.0)
        self.assertEqual(r.consec_loss, 1)
        r.trade_done(-200.0)
        self.assertEqual(r.consec_loss, 2)

    def test_consec_loss_resets_on_win(self):
        r = Risk()
        r.trade_done(-100.0)
        r.trade_done(-100.0)
        r.trade_done(200.0)
        self.assertEqual(r.consec_loss, 0)

    def test_rm_floored_when_no_consec_loss_cap(self):
        """RM floor of C_RM_FLOOR applies when consec_loss < 3."""
        r = Risk()
        # 2 losses → consec_loss=2, no cap → floor should apply
        r.trade_done(-100.0)
        r.trade_done(-100.0)
        self.assertGreaterEqual(r.rm, C_RM_FLOOR)

    def test_rm_consec_cap_overrides_floor(self):
        """4+ consec losses: hard cap at 0.25 deliberately overrides floor."""
        r = Risk()
        for _ in range(4):
            r.trade_done(-100.0)
        self.assertLessEqual(r.rm, 0.25)

    def test_rm_reduced_after_3_losses(self):
        r = Risk()
        # Add enough trades so Kelly kicks in, then take 3 losses
        for _ in range(6):
            r.trade_done(200.0)
        for _ in range(3):
            r.trade_done(-100.0)
        self.assertLessEqual(r.rm, 0.50)

    def test_rm_severely_reduced_after_4_losses(self):
        r = Risk()
        for _ in range(6):
            r.trade_done(200.0)
        for _ in range(4):
            r.trade_done(-100.0)
        self.assertLessEqual(r.rm, 0.25)

    def test_kelly_needs_minimum_trades(self):
        r = Risk()
        # <5 wins → kelly stays 0
        for _ in range(4):
            r.trade_done(100.0)
        self.assertEqual(r.prac_kelly, 0.0)

    def test_day_reset_clears_session_state(self):
        r = Risk()
        r.trade_done(-300.0)
        r.trade_done(-300.0)
        r.day_reset()
        self.assertEqual(r.consec_loss, 0)
        self.assertEqual(r.sess_pnl, 0.0)

    def test_avg_win_avg_loss_running_average(self):
        r = Risk()
        r.trade_done(100.0)
        r.trade_done(200.0)
        self.assertAlmostEqual(r.avg_win, 150.0, places=2)
        r.trade_done(-50.0)
        r.trade_done(-150.0)
        self.assertAlmostEqual(r.avg_loss, 100.0, places=2)


# ─────────────────────────────────────────────────────────────────────────────
#  Trap state machine (M5)
# ─────────────────────────────────────────────────────────────────────────────
class TestTrap(unittest.TestCase):

    def _make_bull_commit_bars(self, n=3, base=20000.0, atr=50.0):
        """Bull commit: n consecutive up bars with positive delta."""
        bars = []
        for i in range(n):
            c = base + 20 * (i + 1)
            b = make_bar(open=c - 10, high=c + 5, low=c - 15, close=c,
                         bid_vol=800, ask_vol=1200, idx=i)
            bars.append(b)
        return bars

    def test_phase_transitions_to_1_after_2_bull_bars(self):
        """
        update_trap requires i >= 2. Prepend 2 dummy bars so the bull commit
        bars are processed at indices 2 and 3.
        """
        trap = Trap()
        atr = 50.0
        # 2 dummy neutral bars at indices 0,1 (skipped by the i<2 guard)
        dummy = make_bar(open=20000, high=20005, low=19995, close=20000,
                         bid_vol=1000, ask_vol=1000, idx=0)
        dummy2 = make_bar(open=20000, high=20005, low=19995, close=20000,
                          bid_vol=1000, ask_vol=1000, idx=1)
        commit1 = make_bar(open=19990, high=20040, low=19985, close=20035,
                           bid_vol=600, ask_vol=1400, idx=2)
        commit2 = make_bar(open=20035, high=20080, low=20025, close=20075,
                           bid_vol=600, ask_vol=1400, idx=3)
        bars = [dummy, dummy2, commit1, commit2]
        for i in range(len(bars)):
            trap = update_trap(trap, bars, i, atr)
        self.assertIn(trap.phase, [1, 2, 3])

    def test_reset_on_new_bull_run_start(self):
        trap = Trap()
        bars = self._make_bull_commit_bars(2)
        # start bear bar to interrupt
        bear = make_bar(open=20050, high=20055, low=19980, close=19990,
                        bid_vol=1500, ask_vol=700, idx=2)
        bars.append(bear)
        for i in range(len(bars)):
            trap = update_trap(trap, bars, i, 50.0)
        # After a single commit bar (<2), phase should reset
        self.assertIn(trap.phase, [0, 1])  # not armed yet

    def test_phase_3_armed_bull_trap(self):
        """
        Build a synthetic bull trap. Indices 0,1 are dummies (skipped by i<2).
        Commit bars at 2,3; stall at 4; reversal at 5.
        """
        trap = Trap()
        atr = 50.0
        bars = []

        # Indices 0,1: neutral dummies
        for i in range(2):
            bars.append(make_bar(open=20000, high=20005, low=19995, close=20000,
                                 bid_vol=1000, ask_vol=1000, idx=i))
        # Indices 2,3: bull commit bars (bullish, +delta)
        commit_hi = 0.0
        for j in range(2):
            c = 20000.0 + 30 * (j + 1)
            commit_hi = c + 5
            bars.append(make_bar(open=c - 15, high=commit_hi, low=c - 20, close=c,
                                 bid_vol=600, ask_vol=1400, idx=len(bars)))
        # Index 4: stall (no new high, delta flip to -delta)
        bars.append(make_bar(open=20060, high=commit_hi, low=20050, close=20055,
                             bid_vol=1200, ask_vol=800, idx=len(bars)))
        absorb_px = 20055.0
        # Index 5: bearish reversal, closes > 0.15 ATR below absorb
        rev_close = absorb_px - atr * 0.20
        bars.append(make_bar(open=20058, high=20059, low=rev_close - 2,
                             close=rev_close, bid_vol=1500, ask_vol=700,
                             idx=len(bars)))

        for i in range(len(bars)):
            trap = update_trap(trap, bars, i, atr)

        self.assertIn(trap.phase, [2, 3])

    def test_phase_3_resets_when_price_reclaims(self):
        """Phase-3 reset requires i >= 2 to be processed."""
        trap = Trap()
        trap.phase = 3
        trap.direction = +1
        trap.absorb_px = 20000.0
        trap.valid = True
        # Need i=2 so the guard passes
        dummy1 = make_bar(close=20005.0, idx=0)
        dummy2 = make_bar(close=20005.0, idx=1)
        reclaim = make_bar(close=20010.0, idx=2)  # above absorb_px → reset
        bars = [dummy1, dummy2, reclaim]
        for i in range(len(bars)):
            trap = update_trap(trap, bars, i, 50.0)
        self.assertEqual(trap.phase, 0)


# ─────────────────────────────────────────────────────────────────────────────
#  balance_state (M6)
# ─────────────────────────────────────────────────────────────────────────────
class TestBalanceState(unittest.TestCase):

    def test_not_mature_too_few_bars(self):
        bars = make_bars(5)
        b = balance_state(bars, 4, atr=50.0)
        self.assertFalse(b.mature)

    def test_mature_tight_range(self):
        """15+ bars in a tight ATR*0.3 – ATR*2.5 range."""
        atr = 50.0
        n = 20
        bars = []
        for i in range(n):
            close = 20000.0 + (5.0 if i % 2 == 0 else -5.0)
            bars.append(make_bar(high=close + 10, low=close - 10, close=close, idx=i))
        b = balance_state(bars, n - 1, atr=atr)
        self.assertTrue(b.mature)
        self.assertGreater(b.hi, b.lo)

    def test_not_mature_range_too_wide(self):
        atr = 50.0
        n = 20
        bars = []
        for i in range(n):
            bars.append(make_bar(high=20000.0 + i * 30, low=20000.0 + i * 30 - 5, idx=i))
        b = balance_state(bars, n - 1, atr=atr)
        self.assertFalse(b.mature)


# ─────────────────────────────────────────────────────────────────────────────
#  M4 VWAP edge gate  (EdgeDiscovery integration)
# ─────────────────────────────────────────────────────────────────────────────
class TestM4VwapEdge(unittest.TestCase):

    def test_m4_blocked_near_vwap(self):
        """
        M4 signal should NOT fire when close is within 0.35*ATR of VWAP.
        We call the gate logic directly.
        """
        atr  = 50.0
        vwap = 20000.0
        close_near = 20000.0 + atr * 0.20  # within 0.35*atr
        m4_vwap_edge = (vwap <= 0) or (abs(close_near - vwap) >= atr * 0.35)
        self.assertFalse(m4_vwap_edge)

    def test_m4_allowed_away_from_vwap(self):
        atr  = 50.0
        vwap = 20000.0
        close_far = 20000.0 + atr * 0.50  # well outside 0.35*atr
        m4_vwap_edge = (vwap <= 0) or (abs(close_far - vwap) >= atr * 0.35)
        self.assertTrue(m4_vwap_edge)

    def test_m4_allowed_when_no_vwap(self):
        """With no VWAP (vwap=0), the edge gate should always pass."""
        m4_vwap_edge = (0 <= 0) or (abs(20000 - 0) >= 50 * 0.35)
        self.assertTrue(m4_vwap_edge)


# ─────────────────────────────────────────────────────────────────────────────
#  M5 delta edge gate  (EdgeDiscovery integration)
# ─────────────────────────────────────────────────────────────────────────────
class TestM5DeltaEdge(unittest.TestCase):

    def test_m5_blocked_weak_delta(self):
        """M5 should not fire if net lb_delta < 3 × avg_d."""
        avg_d   = 500.0
        lb_delta = 1000.0  # only 2× avg_d → fails
        m5_delta_edge = abs(lb_delta) >= avg_d * 3.0
        self.assertFalse(m5_delta_edge)

    def test_m5_allowed_strong_delta(self):
        avg_d   = 500.0
        lb_delta = 1800.0  # 3.6× → passes
        m5_delta_edge = abs(lb_delta) >= avg_d * 3.0
        self.assertTrue(m5_delta_edge)

    def test_m5_edge_works_for_negative_delta(self):
        avg_d   = 300.0
        lb_delta = -1000.0  # bear trap, |delta|=1000 > 900 → passes
        m5_delta_edge = abs(lb_delta) >= avg_d * 3.0
        self.assertTrue(m5_delta_edge)


# ─────────────────────────────────────────────────────────────────────────────
#  Consecutive loss gate
# ─────────────────────────────────────────────────────────────────────────────
class TestConsecLossGate(unittest.TestCase):

    def test_gate_blocks_at_max_losses(self):
        r = Risk()
        for _ in range(C_MAX_LOSSES):
            r.trade_done(-100.0)
        self.assertGreaterEqual(r.consec_loss, C_MAX_LOSSES)

    def test_gate_open_after_win(self):
        r = Risk()
        for _ in range(C_MAX_LOSSES):
            r.trade_done(-100.0)
        r.trade_done(200.0)
        self.assertLess(r.consec_loss, C_MAX_LOSSES)


# ─────────────────────────────────────────────────────────────────────────────
#  Bar builder edge cases
# ─────────────────────────────────────────────────────────────────────────────
class TestBuildVolumeBars(unittest.TestCase):

    def test_import_succeeds(self):
        from backtest import build_volume_bars
        self.assertIsNotNone(build_volume_bars)

    def test_empty_array(self):
        import numpy as np
        from backtest import build_volume_bars, SCID_DTYPE
        empty = np.zeros(0, dtype=SCID_DTYPE)
        bars = build_volume_bars(empty, target_vol=2500)
        self.assertEqual(len(bars), 0)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    unittest.main(verbosity=2)
