"""Monte-Carlo null test for the TopDog entry variants.

The 9-variant screen threw up `deep` at 5/6 contracts — but screening 9 variants
guarantees the best one looks good, so "5/6" needs to be judged against the
distribution of what a DIRECTIONLESS rule produces at the same timestamps, not
against zero.

For each variant this holds the entry timestamps, the swing stop and the 2R
target completely fixed, and only re-signs the direction:

  as_is    the real rule
  inverse  every entry flipped  (if the rule is anti-predictive this wins)
  random   N Monte-Carlo draws  -> the null distribution of pooled net

The percentile of `as_is` within the random draws is the honest p-value for that
variant's direction call. ~50th percentile means the trigger knows nothing about
which way price is about to go.

Usage: python topdog_null_test.py [n_draws] [variant ...]
"""
import random
import sys

import topdog_cycle as T
from topdog_entry_screen import run_variant, stats
from topdog_cycle import CONTRACTS, load_bars_cached

DRAWS = int(sys.argv[1]) if len(sys.argv) > 1 else 200
VARIANTS = sys.argv[2:] or ["base", "deep", "pullback"]


def pooled(name, bars_by_tag, side_mode):
    out = []
    for bars in bars_by_tag.values():
        out += run_variant(name, bars, side_mode)
    return out


def main():
    bars_by_tag = {t: load_bars_cached(t, s) for t, s in CONTRACTS.items()
                   if s.exists()}
    print(f"  {DRAWS} random-sign draws per variant, entries/stops/targets held "
          f"fixed\n")
    print(f"  {'variant':10s} {'n':>5s} {'as_is net':>11s} {'inverse':>11s} "
          f"{'null mean':>10s} {'null sd':>9s} {'pctile':>7s}  read")
    print("  " + "-" * 86)

    for v in VARIANTS:
        random.seed(1)
        real = stats(pooled(v, bars_by_tag, "as_is"))
        inv = stats(pooled(v, bars_by_tag, "inverse"))

        nulls = []
        for s in range(DRAWS):
            random.seed(10_000 + s)
            nulls.append(sum(pooled(v, bars_by_tag, "random")))
        nulls.sort()
        mean = sum(nulls) / len(nulls)
        sd = (sum((x - mean) ** 2 for x in nulls) / (len(nulls) - 1)) ** 0.5
        pct = 100.0 * sum(1 for x in nulls if x < real["net"]) / len(nulls)
        read = ("edge" if pct >= 95 else
                "anti-edge" if pct <= 5 else "INDISTINGUISHABLE FROM NOISE")
        print(f"  {v:10s} {real['n']:>5} {real['net']:>+11,.0f} "
              f"{inv['net']:>+11,.0f} {mean:>+10,.0f} {sd:>9,.0f} "
              f"{pct:>6.1f}%  {read}")


if __name__ == "__main__":
    main()
