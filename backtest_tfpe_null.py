"""Re-sign Monte-Carlo null for NQ_TrendFollow_PropEval.

Same test the TopDog thread used (topdog_null_test.py): hold the entry bars, the
ATR stop distance, the 2.5R target and the position size completely fixed, and
re-sign ONLY the direction. That separates "the bracket geometry made money" —
which any directionless rule with a 2.5R target and a ~31% hit rate can do —
from "the EMA-stack / pullback / breakout trigger actually picked the side".

  as_is    the real rule
  inverse  every entry flipped
  random   N draws -> the null distribution of pooled net

as_is landing near the 50th percentile of the null means the trigger knows
nothing about direction.

This is cheap because the candidate scan is a pure function of the bars: it runs
once per contract and all N draws re-use it, re-running only the governed walk.

Note: the candidate set is deterministic in the bars alone; only the daily-halt
state (2 consecutive losses / giveback / loss limit) filters it, so each draw's
trade count moves a little. That is the honest simulation of this cpp, whose
governors are path-dependent by design.

Usage: python backtest_tfpe_null.py [n_draws] [--mnq | --nq | --all]
"""
import random
import statistics
import sys
import time

import backtest_trendfollow_propeval as TF
from fastbars import load_bars_cached


def pooled(prepared, side_mode):
    out = []
    for bars, cands, p in prepared:
        out += [t.pnl for t in TF.simulate(bars, cands, p, side_mode).trades]
    return out


def main():
    argv = sys.argv[1:]
    draws = 200
    scope = "--all"
    for a in argv:
        if a.startswith("--"):
            scope = a
        else:
            draws = int(a)

    p = TF.params_from_env()
    which = TF.contracts_for(scope)
    prepared = []
    for tag, scid in which.items():
        if not scid.exists():
            continue
        bars = load_bars_cached(tag, scid, TF.BAR_MINUTES)
        prepared.append((bars, TF.scan(bars, p), p))
    tags = [t for t, s in which.items() if s.exists()]

    print(f"Re-sign null -- {draws} draws over {len(prepared)} contracts "
          f"({', '.join(tags)})")
    print("  entry bars / stop distance / target / qty held fixed; "
          "SIDE re-signed\n")

    t0 = time.time()
    real = pooled(prepared, "as_is")
    inv = pooled(prepared, "inverse")

    nulls = []
    for s in range(draws):
        random.seed(10_000 + s)
        nulls.append(sum(pooled(prepared, "random")))
    nulls.sort()
    mean = statistics.fmean(nulls)
    sd = statistics.stdev(nulls)
    net = sum(real)
    pct = 100.0 * sum(1 for x in nulls if x < net) / len(nulls)
    read = ("EDGE" if pct >= 95 else
            "ANTI-EDGE" if pct <= 5 else "INDISTINGUISHABLE FROM NOISE")

    print(f"  as_is    n={len(real):>5}  net=${net:>+10,.0f}")
    print(f"  inverse  n={len(inv):>5}  net=${sum(inv):>+10,.0f}")
    print(f"  null     mean=${mean:>+9,.0f}  sd=${sd:>,.0f}  "
          f"p05=${nulls[int(.05 * len(nulls))]:>+,.0f}  "
          f"p95=${nulls[int(.95 * len(nulls))]:>+,.0f}")
    print(f"\n  as_is percentile in null: {pct:.1f}%  ->  {read}")
    print(f"  ({draws} draws in {time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
