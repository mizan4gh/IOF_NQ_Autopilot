"""Re-sign Monte-Carlo null for Mizan_IOF_NQ.

The AMD pattern makes a directional claim: after a downside sweep of a balance
that gets reclaimed and then leaves value upward, the pullback to the POC is a
LONG. The pooled net alone cannot test that claim -- a 2R bracket on a
directionless rule at a ~35% hit rate also prints a positive number, and this
repo has already retired two strategies that looked profitable and screened at
the 50th percentile of their own null.

So: hold the entry bars, the entry PRICES, the stop distances and the targets
completely fixed, and re-sign ONLY the direction.

  as_is    the real rule
  inverse  every entry flipped (if the pattern is anti-predictive this wins)
  random   N draws -> the null distribution of pooled net

as_is landing near the 50th percentile means the A/M/D sequence knows nothing
about which way price goes from the POC; >=95th is the first real evidence it
does. Cheap because scan() is a pure function of the bars: it runs once per
contract and every draw re-uses it.

Applies to both threads: the rolling intraday version by default, and the daily
power-of-three on frozen levels with --p3.

Usage: python backtest_mizan_null.py [n_draws] [--nq|--all|--is|--oos] [--p3]
"""
import random
import statistics
import sys
import time

from fastbars import load_bars_cached


def pooled(mod, prepared, side_mode):
    out = []
    for bars, cands, p in prepared:
        out += [t.pnl for t in mod.simulate(bars, cands, p, side_mode).trades]
    return out


def main():
    argv = sys.argv[1:]
    draws, scope, which = 200, "--nq", "rolling"
    for a in argv:
        if a in ("--p3", "--avpmd"):
            which = a[2:]
        elif a.startswith("--"):
            scope = a
        else:
            draws = int(a)

    if which == "p3":
        import backtest_mizan_p3 as MZ
    elif which == "avpmd":
        import backtest_mizan_avpmd as MZ
    else:
        import backtest_mizan_iof_nq as MZ

    p = MZ.params_from_env()
    which = MZ.contracts_for(scope)
    prepared, tags = [], []
    for tag, scid in which.items():
        if not scid.exists():
            continue
        # per-contract tick / point value, so a mixed or non-index pool is not
        # priced with the NQ spec
        q = MZ.apply_spec(p, tag) if hasattr(MZ, "apply_spec") else p
        # MUST go through the module's own loader: calling load_bars_cached
        # directly silently ignored vol_bars and null-tested 5-minute bars
        # against a volume-bar result. The tell was a null mean identical to
        # the previous run's -- see the dead-knob note.
        bars = (MZ.load_bars(tag, scid, q) if hasattr(MZ, "load_bars")
                else load_bars_cached(tag, scid, MZ.BAR_MINUTES))
        prepared.append((bars, MZ.scan(bars, q), q))
        tags.append(tag)

    print(f"{MZ.__name__} re-sign null -- "
          f"{draws} draws over {len(prepared)} contracts ({', '.join(tags)})")
    print("  entry bar / entry price / stop distance / target held fixed; "
          "SIDE re-signed\n")

    t0 = time.time()
    real = pooled(MZ, prepared, "as_is")
    inv = pooled(MZ, prepared, "inverse")

    nulls = []
    for s in range(draws):
        random.seed(10_000 + s)
        nulls.append(sum(pooled(MZ, prepared, "random")))
    nulls.sort()
    mean = statistics.fmean(nulls)
    sd = statistics.stdev(nulls) if len(nulls) > 1 else 0.0
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
