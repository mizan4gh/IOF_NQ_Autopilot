#!/usr/bin/env python3
"""
Unit tests for backtest_vwap_pullback.py -- the parity engine for
IOF_NQ_VWAPPullback.cpp.

Run with: python -m pytest test_vwap_pullback.py -v
or:       python test_vwap_pullback.py

STRATEGY OF THESE TESTS
  A synthetic session is built that contains exactly ONE textbook long setup.
  Then each rule is broken, one at a time, and the setup must disappear. That
  shape matters more than it looks: a rule that can be broken without changing
  the output is a DEAD INPUT, and a dead input is indistinguishable from a
  working one until something like this asserts on it. Several of the repo's
  past "null results" were dead knobs, not null effects.
"""

import sys
import unittest
from dataclasses import replace

import numpy as np

sys.path.insert(0, r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final")

from backtest import Bar
import backtest_vwap_pullback as VP


# ─────────────────────────────────────────────────────────────────────────────
#  SYNTHETIC SESSION BUILDER
# ─────────────────────────────────────────────────────────────────────────────
D1, D2 = 20260101, 20260102


def _hhmm_seq(start_hhmm: int, count: int):
    """`count` consecutive 1-minute HHMM stamps starting at start_hhmm."""
    h, m = divmod(start_hhmm, 100)
    out = []
    for _ in range(count):
        out.append(h * 100 + m)
        m += 1
        if m == 60:
            m, h = 0, (h + 1) % 24
    return out


def _bar(dtag, hhmm, o, h, l, c, v=1000):
    return Bar(dt=None, open=float(o), high=float(h), low=float(l),
               close=float(c), volume=int(v), bid_vol=v // 2, ask_vol=v // 2,
               idx=0, date_tag=int(dtag), hhmm=int(hhmm))


def build_session(mutate=None, ramp=0.5, n_ramp=90, rng_half=6.0):
    """One full 18:00 -> 16:00 session containing a single long setup.

    `mutate` is called as mutate(name, fields) for each of the three signal bars
    ("pullback", "rejection", "confirm") and may return modified OHLC, which is
    how the rule-by-rule tests break exactly one thing.

    Returns (bars, index_of_confirmation_bar).
    """
    bars = []

    # -- evening 18:00-23:59 and overnight 00:00-09:29 ---------------------
    # A steady ramp so EMA20 sits above EMA65 with a positive slope well before
    # the RTH open. Bar range 12 pts keeps ATR comfortably above min_atr_pts.
    px = 20000.0
    for hhmm in _hhmm_seq(1800, 360):
        bars.append(_bar(D1, hhmm, px, px + rng_half, px - rng_half, px + ramp))
        px += ramp
    for hhmm in _hhmm_seq(0, 570):
        bars.append(_bar(D2, hhmm, px, px + rng_half, px - rng_half, px + ramp))
        px += ramp

    # -- RTH ---------------------------------------------------------------
    rth = _hhmm_seq(930, 390)
    k = 0

    # running session VWAP, computed exactly as the engine does, so the setup
    # bars below can be placed relative to it instead of guessed at
    pv = vv = 0.0

    def push(o, h, l, c, v=1000):
        nonlocal pv, vv, k
        bars.append(_bar(D2, rth[k], o, h, l, c, v))
        tp = (h + l + c) / 3.0
        pv += tp * v
        vv += v
        k += 1

    def vwap():
        return pv / vv if vv > 0 else px

    # 1. trend leg: price pulls away from VWAP until separation clears 15 pts
    for _ in range(n_ramp):
        push(px, px + rng_half, px - rng_half, px + ramp)
        px += ramp

    w = vwap()

    # 2. the pullback bar: its LOW reaches the proximity zone, close stays above
    f = dict(o=w + 16, h=w + 17, l=w + 6, c=w + 7)
    if mutate:
        f = mutate("pullback", f) or f
    push(f["o"], f["h"], f["l"], f["c"])

    w = vwap()

    # 3. the rejection bar: touches the zone, closes back above VWAP, closes in
    #    the upper half of its range, and closes above its open
    f = dict(o=w + 4, h=w + 11, l=w + 3, c=w + 10)
    if mutate:
        f = mutate("rejection", f) or f
    push(f["o"], f["h"], f["l"], f["c"])
    rej_h = f["h"]

    # 4. the confirmation bar: closes above the rejection high + buffer
    f = dict(o=w + 11, h=w + 15, l=w + 11, c=rej_h + 3.0)
    if mutate:
        f = mutate("confirm", f) or f
    push(f["o"], f["h"], f["l"], f["c"])
    confirm_idx = len(bars) - 1

    # 5. tail: enough bars for the trade to resolve, drifting up
    px = f["c"]
    while k < len(rth):
        push(px, px + rng_half, px - rng_half, px + 0.5)
        px += 0.5

    return bars, confirm_idx


# Every rule test pins its own config rather than inheriting whatever currently
# ships as the default. Otherwise changing a default silently changes what the
# rule tests assert, and a green suite stops meaning what it used to mean.
# SPEC is the literal specification: points geometry, session VWAP, fixed target.
SPEC = replace(VP.Params(), geom_mode="points", level_mode="vwap", target_mode=0)
P = SPEC


def count(bars, p=None):
    p = p or P
    # id(bars)-keyed caches must not leak between synthetic fixtures
    VP._SCAN_CACHE.clear()
    return VP.scan(bars, p)


# ─────────────────────────────────────────────────────────────────────────────
#  THE BASELINE SETUP MUST FIRE
# ─────────────────────────────────────────────────────────────────────────────
class TestBaselineSetup(unittest.TestCase):

    def test_one_long_candidate(self):
        bars, ci = build_session()
        c = count(bars)
        self.assertEqual(len(c.idx), 1, "the textbook setup must produce exactly one candidate")
        self.assertEqual(int(c.side[0]), 1, "it is a LONG setup")
        self.assertEqual(int(c.sweep_idx[0]), ci, "signal must be the confirmation bar")

    def test_fill_is_next_bar_open(self):
        """No lookahead: the fill price is the NEXT bar's open, never the
        confirmation bar's close. Filling a close-through-a-level trigger at
        that close is the bias that has voided results in this repo before."""
        bars, ci = build_session()
        c = count(bars)
        self.assertEqual(int(c.idx[0]), ci + 1)
        self.assertAlmostEqual(float(c.entry_px[0]), bars[ci + 1].open, places=6)

    def test_managed_from_the_fill_bar(self):
        """A market fill at a bar's open is exposed to that whole bar. Managing
        from the following bar would silently delete the first bar of adverse
        excursion from every trade."""
        bars, ci = build_session()
        c = count(bars)
        self.assertIsNotNone(c.manage_at)
        self.assertEqual(int(c.manage_at[0]), int(c.idx[0]))

    def test_stop_and_target_are_sane(self):
        bars, _ = build_session()
        c = count(bars)
        stop = float(c.stop_pts[0])
        tgt = float(c.tgt_pts[0])
        self.assertGreater(stop, 0.0)
        self.assertGreaterEqual(stop, P.min_stop_ticks * P.tick)
        self.assertLessEqual(stop, P.max_stop_ticks * P.tick)
        self.assertGreaterEqual(tgt / stop, P.min_rr)


# ─────────────────────────────────────────────────────────────────────────────
#  EVERY RULE MUST BE LOAD-BEARING
# ─────────────────────────────────────────────────────────────────────────────
class TestRulesAreLoadBearing(unittest.TestCase):
    """Break one rule; the setup must vanish. A rule whose violation changes
    nothing is not a filter, it is decoration."""

    def _assert_killed(self, mutate, why):
        bars, _ = build_session(mutate=mutate)
        c = count(bars)
        self.assertEqual(len(c.idx), 0, f"setup should be rejected: {why}")

    def test_rejection_bar_must_close_above_its_open(self):
        def m(name, f):
            if name == "rejection":
                f["o"] = f["c"] + 1.0      # red body
            return f
        self._assert_killed(m, "rejection bar closed below its open")

    def test_rejection_close_must_be_in_upper_half(self):
        def m(name, f):
            if name == "rejection":
                # keep it green and still touching, but close near the low
                f["l"] = f["c"] - 0.5
                f["h"] = f["c"] + 10.0
                f["o"] = f["c"] - 0.25
            return f
        self._assert_killed(m, "close sat in the lower half of the range")

    def test_rejection_must_close_back_above_vwap(self):
        def m(name, f):
            if name == "rejection":
                shift = f["c"] - (f["l"] - 1.0)   # drag the whole bar under VWAP
                f["o"] -= shift
                f["h"] -= shift
                f["l"] -= shift
                f["c"] -= shift
            return f
        self._assert_killed(m, "rejection bar closed below VWAP")

    def test_rejection_must_touch_the_proximity_zone(self):
        def m(name, f):
            if name == "rejection":
                lift = 40.0                  # far above the zone
                for key in ("o", "h", "l", "c"):
                    f[key] += lift
            return f
        self._assert_killed(m, "the bar never reached the VWAP zone")

    def test_confirmation_must_clear_the_buffer(self):
        def m(name, f):
            if name == "confirm":
                # close exactly AT the rejection high: inside the buffer
                f["c"] = f["c"] - 3.0
                f["h"] = max(f["h"], f["c"])
            return f
        self._assert_killed(m, "confirmation did not clear rejection high + buffer")

    def test_pullback_must_reach_the_zone(self):
        def m(name, f):
            if name == "pullback":
                for key in ("o", "h", "l", "c"):
                    f[key] += 40.0
            return f
        self._assert_killed(m, "price never pulled back into the zone")


# ─────────────────────────────────────────────────────────────────────────────
#  PARAMETERS MUST NOT BE DEAD INPUTS
# ─────────────────────────────────────────────────────────────────────────────
class TestParametersAreReachable(unittest.TestCase):

    def test_confirm_buffer_is_reachable(self):
        bars, _ = build_session()
        self.assertEqual(len(count(bars).idx), 1)
        # the confirmation clears the high by 3.0, so a 5.0 buffer must kill it
        big = replace(P, confirm_buf_pts=5.0)
        self.assertEqual(len(count(bars, big).idx), 0,
                         "confirm_buf_pts did not change the outcome -- dead input")

    def test_min_separation_is_reachable(self):
        bars, _ = build_session()
        self.assertEqual(len(count(bars).idx), 1)
        huge = replace(P, min_sep_pts=500.0)
        self.assertEqual(len(count(bars, huge).idx), 0,
                         "min_sep_pts did not change the outcome -- dead input")

    def test_proximity_is_reachable(self):
        bars, _ = build_session()
        tiny = replace(P, proximity_pts=0.25)
        self.assertEqual(len(count(bars, tiny).idx), 0,
                         "proximity_pts did not change the outcome -- dead input")

    def test_entry_window_blocks_late_confirmations(self):
        bars, _ = build_session()
        self.assertEqual(len(count(bars).idx), 1)
        early = replace(P, entry_end=931)      # before the setup can form
        self.assertEqual(len(count(bars, early).idx), 0)

    def test_min_rr_is_reachable(self):
        bars, _ = build_session()
        self.assertEqual(len(count(bars).idx), 1)
        strict = replace(P, min_rr=50.0)
        self.assertEqual(len(count(bars, strict).idx), 0)

    def test_max_setup_bars_expires_the_setup(self):
        # 1 bar is shorter than the pullback -> rejection -> confirm sequence
        bars, _ = build_session()
        self.assertEqual(len(count(bars, replace(P, max_setup_bars=1)).idx), 0)


# ─────────────────────────────────────────────────────────────────────────────
#  THE SPEC'S OWN DEFAULTS ARE ARITHMETICALLY INCONSISTENT ON COARSE BARS
# ─────────────────────────────────────────────────────────────────────────────
class TestFixedTargetVersusStructuralStop(unittest.TestCase):
    """A regression guard for the finding that a structural stop (beyond the
    rejection bar's extreme) cannot satisfy min_rr=1.5 against a FIXED 100-tick
    (25 point) target once the stop exceeds 16.67 points.

    On 5-minute NQ the median bar range is 19-48 points, so the structural stop
    is essentially always wider than that and the strategy takes ~0 trades.
    This is a property of the specification, not a bug -- but it must not be
    allowed to regress silently into "the signal has no edge"."""

    def test_wide_structural_stop_fails_fixed_target_rr(self):
        # widen the rejection bar so the structural stop is ~20 points
        def m(name, f):
            if name == "rejection":
                f["l"] = f["c"] - 18.0
                f["o"] = f["c"] - 1.0
                f["h"] = f["c"] + 1.0
            return f
        bars, _ = build_session(mutate=m)
        fixed = replace(P, target_mode=0, fixed_tgt_ticks=100, min_rr=1.5)
        self.assertEqual(len(count(bars, fixed).idx), 0,
                         "a >16.67pt stop must fail a 25pt target at 1.5 RR")

        # the same setup is tradable once the target is R-denominated
        r_mode = replace(P, target_mode=2, target_r=2.0, min_rr=1.5)
        self.assertEqual(len(count(bars, r_mode).idx), 1,
                         "an R-multiple target makes min_rr satisfiable by construction")


# ─────────────────────────────────────────────────────────────────────────────
#  ATR-NORMALISED GEOMETRY
# ─────────────────────────────────────────────────────────────────────────────
class TestAtrGeometry(unittest.TestCase):
    """geom_mode='atr' re-denominates every distance against the ATR frozen at
    the session's RTH open, so the same rules can run on an instrument that is
    not priced like NQ."""

    ATR = replace(P, geom_mode="atr")

    def test_atr_mode_is_wired_and_fires(self):
        bars, ci = build_session()
        c = count(bars, self.ATR)
        self.assertEqual(len(c.idx), 1, "the textbook setup must still fire in ATR mode")
        self.assertEqual(int(c.side[0]), 1)
        self.assertEqual(int(c.sweep_idx[0]), ci)

    def test_atr_multiples_are_reachable(self):
        bars, _ = build_session()
        self.assertEqual(len(count(bars, self.ATR).idx), 1)
        huge = replace(self.ATR, min_sep_atr=50.0)
        self.assertEqual(len(count(bars, huge).idx), 0,
                         "min_sep_atr did not change the outcome -- dead input")
        tiny = replace(self.ATR, proximity_atr=0.001)
        self.assertEqual(len(count(bars, tiny).idx), 0,
                         "proximity_atr did not change the outcome -- dead input")

    def test_points_knobs_are_inert_in_atr_mode(self):
        """The two denominations must not silently blend. Cranking a POINTS
        knob while geom_mode='atr' must change nothing at all."""
        bars, _ = build_session()
        base = count(bars, self.ATR)
        poisoned = replace(self.ATR, min_sep_pts=9999.0, proximity_pts=0.001,
                           confirm_buf_pts=9999.0, min_stop_ticks=9999)
        got = count(bars, poisoned)
        self.assertEqual(list(base.idx), list(got.idx))

    def test_atr_knobs_are_inert_in_points_mode(self):
        bars, _ = build_session()
        base = count(bars, P)
        poisoned = replace(P, min_sep_atr=9999.0, proximity_atr=0.001,
                           confirm_buf_atr=9999.0, min_stop_atr=9999.0)
        got = count(bars, poisoned)
        self.assertEqual(list(base.idx), list(got.idx))

    def test_thresholds_scale_with_the_frozen_atr(self):
        """Double the instrument's volatility and the ATR-mode thresholds must
        double with it -- that is the whole point of the normalisation. A
        points-mode run on the same data does NOT rescale, which is exactly the
        mis-scaling this mode exists to remove."""
        quiet, _ = build_session(rng_half=6.0)
        loud, _ = build_session(rng_half=15.0)   # a materially wider bar

        # the ATR frozen at the open must differ materially between the two
        from backtest_mizan_iof_nq import arrays, atr_wilders
        from backtest_mizan_p3 import sessions_cached
        fa = []
        for bars in (quiet, loud):
            a = arrays(bars)
            S = sessions_cached(bars, P)
            oe = int(S.on_end[0])
            fa.append(float(atr_wilders(a, P.atr_len)[oe - 1]))
        self.assertGreater(fa[1], fa[0] * 1.3,
                           "fixture did not actually change the volatility")


# ─────────────────────────────────────────────────────────────────────────────
#  SESSION VWAP
# ─────────────────────────────────────────────────────────────────────────────
class TestSessionVWAP(unittest.TestCase):

    def test_vwap_ignores_overnight_volume(self):
        """The session VWAP is anchored at 09:30 and must carry no overnight
        volume. If it did, the first RTH bar's VWAP would not equal that bar's
        own typical price."""
        from backtest_mizan_iof_nq import arrays
        from backtest_mizan_p3 import sessions_cached

        bars, _ = build_session()
        a = arrays(bars)
        S = sessions_cached(bars, P)
        w = VP.session_vwap(a, S, P.rth_open, P.rth_close)

        first = int(np.flatnonzero(a.hhmm == P.rth_open)[0])
        tp = (a.h[first] + a.l[first] + a.c[first]) / 3.0
        self.assertAlmostEqual(w[first], tp, places=6)

    def test_vwap_is_nan_outside_rth(self):
        from backtest_mizan_iof_nq import arrays
        from backtest_mizan_p3 import sessions_cached

        bars, _ = build_session()
        a = arrays(bars)
        S = sessions_cached(bars, P)
        w = VP.session_vwap(a, S, P.rth_open, P.rth_close)
        overnight = (a.hhmm < P.rth_open) | (a.hhmm >= P.rth_close)
        self.assertTrue(np.all(np.isnan(w[overnight])),
                        "an overnight bar must have no session VWAP at all")


# ─────────────────────────────────────────────────────────────────────────────
#  VWAP CROSS COUNTER  (mirrors VP_CountCrosses in the cpp)
# ─────────────────────────────────────────────────────────────────────────────
class TestCrossCounts(unittest.TestCase):

    def test_matches_brute_force(self):
        rng = np.random.default_rng(7)
        above = rng.random(300) > 0.5
        lookback = 20
        got = VP.cross_counts(above, lookback)
        for i in range(len(above)):
            lo = max(0, i - lookback + 1)
            win = above[lo:i + 1]
            want = int(np.sum(win[1:] != win[:-1]))
            self.assertEqual(int(got[i]), want, f"mismatch at bar {i}")

    def test_no_crosses_when_side_never_changes(self):
        above = np.ones(100, dtype=bool)
        self.assertTrue(np.all(VP.cross_counts(above, 20) == 0))

    def test_alternating_is_saturated(self):
        above = np.arange(100) % 2 == 0
        out = VP.cross_counts(above, 20)
        self.assertEqual(int(out[-1]), 19, "every adjacent pair differs")


# ─────────────────────────────────────────────────────────────────────────────
#  NO LOOKAHEAD
# ─────────────────────────────────────────────────────────────────────────────
class TestNoLookahead(unittest.TestCase):

    def test_truncating_the_future_does_not_change_the_past(self):
        """Scanning the first k bars must produce exactly the candidates that a
        scan of the full series produced at indices < k. If a future bar can
        change a past signal, the signal repaints."""
        bars, ci = build_session()
        full = count(bars)
        self.assertEqual(len(full.idx), 1)

        cut = ci + 2                      # just past the fill bar
        head = count(list(bars[:cut]))

        # A candidate is indexed by its FILL bar, so every candidate whose fill
        # bar survives the truncation must be reproduced identically.
        want = full.idx[full.idx < cut]
        self.assertEqual(list(head.idx), list(want))
        if len(want):
            self.assertAlmostEqual(float(head.entry_px[0]),
                                   float(full.entry_px[0]), places=6)

    def test_no_candidate_without_a_following_bar(self):
        """An entry needs a NEXT bar to open on. Truncating at the confirmation
        bar must therefore produce nothing, not a close-priced fill."""
        bars, ci = build_session()
        head = count(list(bars[:ci + 1]))
        self.assertEqual(len(head.idx), 0)


# ─────────────────────────────────────────────────────────────────────────────
#  THE SHIPPED DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
class TestShippedDefaults(unittest.TestCase):
    """The defaults are deliberately the best-measured config, not the literal
    spec -- the spec takes 3 trades in 387 contract-days. This pins them so a
    stray edit cannot quietly ship a different strategy."""

    def test_defaults_are_the_preregistered_config(self):
        d = VP.Params()
        self.assertEqual(d.geom_mode, "atr")
        self.assertEqual(d.level_mode, "tpavg")
        self.assertEqual(d.target_mode, 2)
        self.assertAlmostEqual(d.target_r, 2.0)
        self.assertAlmostEqual(d.min_rr, 1.5)
        self.assertEqual(VP.BAR_MIN, 1, "defaults assume a 1-minute chart")

    def test_atr_multiples_match_the_registered_anchor(self):
        """Each multiple is the original point value / ATR_ref (9.8058). If one
        of these drifts, the geometry is no longer the registered one and the
        pre-registration in PREREG_vwap_pullback_tpavg.md is void."""
        d = VP.Params()
        ref = 9.8058
        for mult, pts in ((d.proximity_atr, 8.0), (d.min_sep_atr, 15.0),
                          (d.retest_tol_atr, 4.0), (d.confirm_buf_atr, 2.0),
                          (d.stop_buf_atr, 1.0), (d.min_stop_atr, 4.0),
                          (d.max_stop_atr, 50.0)):
            self.assertAlmostEqual(mult, pts / ref, places=3)

    def test_the_literal_spec_is_still_reachable(self):
        """Reverting to the specification must remain possible, since that is
        what the cpp's own default geometry still implements."""
        bars, _ = build_session()
        self.assertEqual(len(count(bars, SPEC).idx), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
