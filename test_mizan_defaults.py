#!/usr/bin/env python3
"""
Defaults parity: Mizan_IOF_NQ.cpp  vs  backtest_mizan_p3.py

Run with: python -m pytest test_mizan_defaults.py -v
or:       python test_mizan_defaults.py

WHY THIS EXISTS
  Mizan_IOF_NQ.cpp's header states that "the defaults below match [the
  measurement] exactly so that what runs is what was measured". That is the
  single claim the whole study rests on -- a 99.0th-percentile null means
  nothing if the shipped defaults drifted off the configuration it was measured
  on -- and until now it was only a comment.

  It is not a hypothetical risk. That same header previously quoted 105 trades /
  +$91,795 / 6-of-6 for entry mode 1. Those numbers belonged to a different rule
  (backtest_mizan_p3.py's "sweep_limit" silently required a distribution stage
  this cpp does not implement; it is now "sweep_limit_d"). A comment cannot
  catch that. A test can.

  These assertions are deliberately exact. If one fails, the correct response is
  NOT to update the expected value -- it is to work out which side drifted and
  why, because one of the two is no longer the measured strategy.
"""

import re
import sys
import unittest

sys.path.insert(0, r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final")

from backtest_mizan_p3 import Params

CPP = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final\Mizan_IOF_NQ.cpp"


def cpp_default(src, name):
    """The last SetInt/SetFloat applied to input `name` in SetDefaults."""
    m = re.findall(name + r"\.Set(?:Int|Float)\(\s*([-0-9.]+)f?\s*\)", src)
    return float(m[-1]) if m else None


class TestMizanDefaultsParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = open(CPP, encoding="utf-8").read()
        cls.p = Params()

    # cpp input -> python field. Every knob that changes which trades happen.
    PAIRS = [
        ("IN_ON_START",   "on_start"),
        ("IN_RTH_OPEN",   "rth_open"),
        ("IN_RTH_CLOSE",  "rth_close"),
        ("IN_MANIP_END",  "manip_end"),
        ("IN_FLAT_TIME",  "flatten_hhmm"),
        ("IN_MIN_ON",     "min_on_bars"),
        ("IN_MIN_RTH",    "min_rth_bars"),
        ("IN_USE_ON",     "use_on_levels"),
        ("IN_USE_PD",     "use_pd_levels"),
        ("IN_SWEEP_EPS",  "sweep_eps_atr"),
        ("IN_CLOSE_POS",  "sweep_close_pos"),
        ("IN_STOP_BUF",   "stop_buf_atr"),
        ("IN_MIN_STOP",   "min_stop_atr"),
        ("IN_MAX_STOP",   "max_stop_atr"),
        ("IN_TARGET_R",   "target_r"),
        ("IN_ATR_PER",    "atr_len"),
        ("IN_QTY",        "qty"),
        ("IN_DAILY_LOSS", "daily_loss"),
        ("IN_LIM_OFF",    "limit_off_atr"),
        ("IN_LIM_BARS",   "limit_bars"),
    ]

    def test_every_default_matches_the_measurement(self):
        for cpp_name, py_name in self.PAIRS:
            with self.subTest(input=cpp_name):
                c = cpp_default(self.src, cpp_name)
                self.assertIsNotNone(c, f"{cpp_name} has no default in SetDefaults")
                self.assertAlmostEqual(
                    c, float(getattr(self.p, py_name)), places=6,
                    msg=f"{cpp_name} != Params.{py_name} -- what runs is not what "
                        f"was measured")

    def test_entry_mode_defaults_to_market(self):
        """Mode 1 (limit) makes MORE money (+$75,340 vs +$62,730) and must still
        not be the default: it FAILS the placebo. Pulling every frozen level
        inward by 0.20 of the overnight range drops the market rule to the
        19.5th percentile -- a real level effect collapsing, as it should -- but
        leaves the limit rule at the 98.0th. Per the 2026-09-03 decomposition,
        65% of mode 1's edge is just a 3.7% tighter stop (48.40 -> 46.59) and
        the rest is level-agnostic damage control. A good score under mode 1
        says nothing about whether ONH/ONL/PDH/PDL mean anything.

        If this test ever fails because someone set mode 1 as the default to
        capture the extra $12,610: that is the trade this file exists to stop."""
        self.assertEqual(cpp_default(self.src, "IN_ENTRY_MODE"), 0.0)
        self.assertEqual(self.p.entry_mode, "sweep")

    def test_live_routing_is_off_by_default(self):
        """STATUS says NOT VALIDATED FOR A FUNDED ACCOUNT. Sim is the default."""
        self.assertEqual(cpp_default(self.src, "IN_LIVE"), 0.0)

    def test_governors_are_off_as_measured(self):
        """The measurement ran flat 1 lot with every daily governor OFF. Turning
        DAILY_LOSS on is a departure from the tested configuration, not a free
        safety upgrade -- the header says so and this pins it."""
        self.assertEqual(cpp_default(self.src, "IN_DAILY_LOSS"), 0.0)
        self.assertEqual(self.p.daily_loss, 0.0)
        self.assertEqual(self.p.max_trades_day, 0)
        self.assertEqual(self.p.max_consec_loss, 0)

    def test_python_only_fields_are_non_binding(self):
        """Fields the Python has and the cpp does not. Each must be inert at
        its default, or the two engines are running different rules.

        session_end is the one to watch: the Python refuses a fill after 15:30
        and the cpp has no equivalent gate. It is non-binding ONLY because
        manip_end=1200 stops sweeps 3.5 hours earlier. Raise manip_end past
        session_end and the engines diverge."""
        p = self.p
        self.assertEqual(p.on_max_rng_atr, 0.0, "accumulation filter must be off")
        self.assertEqual(p.max_setups_day, 1, "cpp locks the day via SetupTaken")
        self.assertEqual(p.stop_anchor, "bar", "cpp anchors on the signal bar")
        self.assertEqual(p.target_mode, "r", "cpp ships an R-multiple target")
        self.assertEqual(p.vol_bars, 0, "cpp is a time-bar study (5m chart)")
        self.assertLess(p.manip_end, p.session_end,
                        "manip_end must gate entries before session_end, which "
                        "the cpp does not implement")

    def test_sweep_limit_d_is_not_the_default(self):
        """sweep_limit_d is the highest number in the file -- 105 trades,
        +$91,795, and the ONLY configuration that passes the 6/6 ship gate.
        It is also the one with no level content at all.

        Measured 2026-09-12: under placebo shift 0.10, with every frozen level
        pulled inward and its claim to be a structural price destroyed, it
        makes 120 trades for +$92,270 at 6/6 -- MORE than on real levels, and
        it still passes the ship gate. It sits at the 100.0th percentile of its
        own re-sign null on real levels, at 0.10 AND at 0.20. Mode 0 collapses
        99.0th -> 19.5th over the same range, which is what a load-bearing
        level does.

        The ship gate therefore does not discriminate for this mode. "6/6 PASS"
        is not the missing validation.

        It is also not reachable from this cpp: it requires a close 0.5 x ATR
        clear of the overnight POC, a distribution stage needing a volume
        profile this file deliberately does not contain. Adopting it would mean
        restoring that machinery to ship the variant with the LEAST evidence
        the levels matter."""
        self.assertEqual(cpp_default(self.src, "IN_ENTRY_MODE"), 0.0)
        self.assertNotIn("sweep_limit_d", self.src.split("SetDefaults")[-1],
                         "sweep_limit_d must not appear in executable defaults")
        self.assertEqual(self.p.dist_min_atr, 0.5,
                         "the D-stage clearance sweep_limit_d needs; if this "
                         "changes the quoted 105/+$91,795 no longer reproduces")

    def test_the_falsified_poc_entry_is_not_the_default(self):
        """The full A->M->D->POC chain scored 21.5th percentile and the POC
        pullback was falsified, not omitted for effort. It must never be the
        default, and the cpp must contain no volume profile at all."""
        self.assertEqual(self.p.entry_mode, "sweep")
        self.assertNotIn("VolumeAtPrice", self.src,
                         "a volume profile reappeared in a file that must not "
                         "have one -- the POC entry was falsified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
