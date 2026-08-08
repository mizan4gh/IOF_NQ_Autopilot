"""Re-sign Monte-Carlo null for MNQ_SweepAbsorption_PropEval.

Same test as topdog_null_test.py / backtest_tfpe_null.py: hold the trigger bars,
the stop distance, the target distance and the position size completely fixed,
and re-sign ONLY the direction. A sweep-absorption trigger fires at a location
where price has just made a local extreme, with a 3R-ish target and a stop just
beyond that extreme -- geometry that can print money on its own. This separates
"the bracket geometry made money" from "the sweep/absorption read picked the
side".

  as_is    the real rule
  inverse  every entry flipped
  random   N draws -> the null distribution of pooled net

as_is landing near the 50th percentile of the null means the trigger knows
nothing about direction.

Unlike the trend-follow harness this one re-runs the whole walk per draw: the
cpp's level state machine is frozen while a position is open, so flipping a side
changes trade durations and therefore which levels are alive later. That is the
honest simulation, and it costs ~1 s per contract-draw.

Usage: python backtest_swabs_null.py [n_draws] [--mnq | --nq | --all]
"""
import random
import statistics
import sys
import time

import backtest_sweepabs_propeval as SA


def pooled(prepared, side_mode):
    out = []
    for bars, ser, p in prepared:
        out += [t.pnl for t in SA.walk(bars, ser, p, side_mode).trades]
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

    p = SA.params_from_env()
    which = SA.contracts_for(scope)
    prepared, tags = [], []
    for tag, scid in which.items():
        if not scid.exists():
            continue
        bars, sec = SA.load_bars(tag, scid)
        prepared.append((bars, SA.prep(bars, p, sec), p))
        tags.append(tag)

    print(f"Re-sign null [{p.rev}, bars={SA.BAR_MODE}, fill="
          f"{'next-open' if p.entry_next_open else 'signal-close'}] -- "
          f"{draws} draws over {len(prepared)} contracts ({', '.join(tags)})")
    print("  trigger bars / stop distance / target / qty held fixed; "
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
            "INVERSE EDGE" if pct <= 5 else "NOISE")

    print(f"  as_is    n={len(real):>4}  net=${net:+,.0f}")
    print(f"  inverse  n={len(inv):>4}  net=${sum(inv):+,.0f}")
    print(f"  null     mean=${mean:+,.0f}  sd=${sd:,.0f}  "
          f"p5=${nulls[len(nulls)//20]:+,.0f}  "
          f"p95=${nulls[-max(1, len(nulls)//20)]:+,.0f}")
    print(f"\n  as_is sits at the {pct:.1f}th percentile of the null "
          f"-> {read}")
    print(f"  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
