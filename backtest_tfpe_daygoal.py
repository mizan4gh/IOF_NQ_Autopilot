"""Can NQ_TrendFollow_PropEval produce $700 days? -- sizing-model A/B.

THE DEFECT IN THE SHIPPED SIZING
  qty = (int)(RiskDollars / (StopPoints * DollarsPerPoint))

  On MNQ at NQ 20-24k the stop pins to the MaxStopPts=45 ceiling on 64% of
  entries, so RiskPerContract = 45 * $2 = $90 and the truncation gives
  floor(175/90) = 1 lot. The trade risks $90 against a $175 intent. 66% of all
  trades run at 1 lot, so the system realises roughly half the risk it budgets
  and therefore roughly half the P/L. A 2.5R winner on 1 lot is $225; a $1,000
  day needs four and a half of them in a row against a 31% hit rate with
  MaxConsecLoss=2. It is arithmetically unreachable, which is why 313 backtested
  days produced zero.

THE FIX  (Params.risk_mode = "budget"; see size_trade())
  1. Size off the loss path the governors actually permit. How many losses a day
     can absorb is a function of MaxTradesPerDay AND MaxConsecLosses together --
     3 at MT=5/MCL=2, 2 at MT=3/MCL=2 -- not the flat $175 the cpp assumes.
     risk = (remaining loss room) / (worst-case losses still reachable).
  2. Round qty to NEAREST, not floor. Truncation is what silently halves the
     budget whenever qty lands near 1.
  3. Cap a single trade's risk at the giveback allowance. Without this, `room`
     grows with the day's profit and on the last permitted trade (1 slot left)
     the model stakes every dollar earned -- one loss walks +$400 to the loss
     limit, which is precisely what the giveback governor exists to stop.
     This cap was added after a slip stress showed a -$915 single trade.

WHAT IT DOES AND DOES NOT DO
  It makes the target reachable. It does NOT create edge -- position sizing is a
  scalar on P/L, so it multiplies both tails. On a signal whose expectancy is
  ~zero the net gets worse, not better, and that is the honest result below.

Usage: python backtest_tfpe_daygoal.py [--goal 700] [--slip 1.0]
"""
from __future__ import annotations

import sys
from dataclasses import replace

import numpy as np

import backtest_trendfollow_propeval as TF
from fastbars import load_bars_cached

ALL = ["NQM5", "NQU25", "NQZ25", "NQH6", "NQM6", "NQU26", "MNQU6"]


def stats(bars, p, goal):
    days, trades, qty = [], [], []
    for t in ALL:
        r = TF.simulate(bars[t], TF.scan(bars[t], p), p)
        days += [d.pnl for d in r.days if d.n > 0]
        trades += [x.pnl for x in r.trades]
        qty += [x.qty for x in r.trades]
    d, a, q = np.array(days), np.array(trades), np.array(qty)
    # the profit lock flattens AT the threshold and commission is booked after,
    # so a locked day lands a few dollars short -- count within one round turn
    hit = d >= goal - 10.0
    return dict(days=len(d), medq=np.median(q), maxq=q.max(),
                goal=100.0 * hit.mean(), bad=100.0 * (d <= -500).mean(),
                worst=d.min(), p90=np.percentile(d, 90),
                breach=int((d <= -p.daily_loss).sum()) if p.daily_loss > 0 else 0,
                net=a.sum(), worst_trade=a.min())


def main():
    goal = 700.0
    slip = 1.0
    for i, a in enumerate(sys.argv):
        if a == "--goal":
            goal = float(sys.argv[i + 1])
        if a == "--slip":
            slip = float(sys.argv[i + 1])

    paths = {**TF.MNQ, **TF.NQ_FROZEN}
    bars = {t: load_bars_cached(t, paths[t], TF.BAR_MINUTES) for t in ALL
            if paths[t].exists()}

    FIX = dict(risk_mode="budget", max_contracts=20, max_trades_day=3)
    LOCK = {**FIX, "target_r": 3.0, "daily_target": goal}
    steps = [
        ("cpp as written (fixed $175, floor, cap 6)", dict()),
        ("+ round-to-nearest & budget sizing", dict(risk_mode="budget",
                                                    max_contracts=20)),
        ("+ MT 5->3  (2 loss slots, bigger each)", FIX),
        ("+ 3R target", {**FIX, "target_r": 3.0}),
        (f"+ ${goal:.0f} profit lock", LOCK),
        ("+ giveback 2R instead of flat $400", {**LOCK, "giveback_r": 2.0}),
        ("+ giveback 2.5R  [FINAL]", {**LOCK, "giveback_r": 2.5}),
    ]

    for sig_label, sig in (("ADX>=20 (as shipped)", {}),
                           ("ADX>=30", dict(adx_min=30.0))):
        print(f"\n{sig_label} -- next-bar-open fill, {slip} tick/side slippage")
        print(f"  {'step':42s} {'days':>5} {'medQ':>5} {'>=' + str(int(goal)):>7} "
              f"{'<=-500':>7} {'worstday':>9} {'breach':>7} {'p90':>7} {'net':>9}")
        for label, kw in steps:
            p = replace(TF.Params(), entry_next_open=1, slip_ticks=slip,
                        **sig, **kw)
            s = stats(bars, p, goal)
            print(f"  {label:42s} {s['days']:>5} {s['medq']:>5.0f} "
                  f"{s['goal']:>6.1f}% {s['bad']:>6.1f}% ${s['worst']:>+8,.0f} "
                  f"{s['breach']:>7} ${s['p90']:>+6,.0f} ${s['net']:>+8,.0f}")

    print(f"\n  breach = days closing at or past the -$600 daily loss limit.")
    print(f"  Sizing is a scalar on P/L: it moves the goal rate and both tails "
          f"together.\n  It cannot turn a negative-expectancy signal positive, "
          f"and the net column shows it.")


if __name__ == "__main__":
    main()
