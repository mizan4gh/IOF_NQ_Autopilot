"""Parameter sweep for NQ_TrendFollow_PropEval, with the holdout built in.

A grid search over a strategy whose baseline sits inside the noise floor WILL
produce a winner -- that is what a grid search does. So the point of this file
is not the winner, it is the two numbers that say whether the winner means
anything:

  1. IS->OOS rank correlation across the WHOLE grid. Fit the parameters on the
     three 2025 contracts, score them on the four 2026 ones (including the
     genuine MNQ file). If tuning transfers, good IS configs are good OOS
     configs and Spearman rho is clearly positive. If rho ~ 0, the grid is
     ranking noise and the top config is a lottery winner.
  2. How the IS-best config does OOS, next to the grid's OOS median. A winner
     that lands at the OOS median has learned nothing.

The IS/OOS split is temporal (2025 expiries -> 2026 expiries), not random, so a
config cannot borrow information from its own test set.

Cost: the smoothers are cached on their lengths and the candidate scan on the
full signal signature, so a threshold-only grid costs ~0.1 ms/scan +
~2 ms/simulate per contract. The whole grid is a couple of minutes.

Usage
  python backtest_tfpe_sweep.py             # full grid
  python backtest_tfpe_sweep.py --quick     # coarse grid, for a smoke test
"""
from __future__ import annotations

import itertools
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

import backtest_trendfollow_propeval as TF
from fastbars import load_bars_cached

BASE = Path(__file__).parent

# temporal split: fit on the 2025 expiries, test on the 2026 ones + real MNQ
IS_TAGS = ["NQM5", "NQU25", "NQZ25"]
OOS_TAGS = ["NQH6", "NQM6", "NQU26", "MNQU6"]

# ── grid ────────────────────────────────────────────────────────────────────
# Signal axes (each distinct combination costs one candidate scan).
SCAN_GRID = dict(
    stop_atr_mult=[1.0, 1.5, 2.0],
    max_stop_pts=[45.0, 200.0],     # 200 == effectively unclamped at NQ 20-24k
    target_r=[1.5, 2.0, 2.5, 3.0],
    adx_min=[15.0, 20.0, 25.0, 30.0],
    close_strength=[0.50, 0.55, 0.65],
    trigger_bars=[2, 3, 5],
)
# Governor axes (free -- they reuse the cached scan).
WALK_GRID = dict(
    use_trail=[0, 1],
    max_consec_loss=[2, 0],
    giveback=[400.0, 0.0],
)

QUICK_SCAN = dict(
    stop_atr_mult=[1.5], max_stop_pts=[45.0, 200.0], target_r=[2.0, 2.5],
    adx_min=[20.0, 25.0], close_strength=[0.55], trigger_bars=[3],
)
QUICK_WALK = dict(use_trail=[0], max_consec_loss=[2], giveback=[400.0])


def grid(d):
    keys = list(d)
    for combo in itertools.product(*(d[k] for k in keys)):
        yield dict(zip(keys, combo))


def spearman(x, y):
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rx -= rx.mean()
    ry -= ry.mean()
    den = np.sqrt((rx * rx).sum() * (ry * ry).sum())
    return float((rx * ry).sum() / den) if den > 0 else 0.0


def main():
    quick = "--quick" in sys.argv
    sg, wg = (QUICK_SCAN, QUICK_WALK) if quick else (SCAN_GRID, WALK_GRID)
    scan_combos = list(grid(sg))
    walk_combos = list(grid(wg))
    total = len(scan_combos) * len(walk_combos)

    all_tags = IS_TAGS + OOS_TAGS
    paths = {**TF.MNQ, **TF.NQ_FROZEN}
    bars = {}
    for t in all_tags:
        if paths[t].exists():
            bars[t] = load_bars_cached(t, paths[t], TF.BAR_MINUTES)
        else:
            print(f"  MISSING {t}")
    is_tags = [t for t in IS_TAGS if t in bars]
    oos_tags = [t for t in OOS_TAGS if t in bars]

    base = TF.params_from_env()
    print(f"TFPE sweep -- {len(scan_combos)} signal x {len(walk_combos)} "
          f"governor = {total:,} configs")
    print(f"  IS  (fit) : {', '.join(is_tags)}")
    print(f"  OOS (test): {', '.join(oos_tags)}\n")

    rows = []
    t0 = time.time()
    for si, sc in enumerate(scan_combos):
        p_sig = replace(base, **sc)
        cands = {t: TF.scan(bars[t], p_sig) for t in bars}
        for wc in walk_combos:
            p = replace(p_sig, **wc)
            per = {}
            for t in bars:
                r = TF.simulate(bars[t], cands[t], p)
                pnls = [x.pnl for x in r.trades]
                wins = sum(x for x in pnls if x > 0)
                lose = -sum(x for x in pnls if x < 0)
                per[t] = (sum(pnls), len(pnls),
                          (wins / lose) if lose > 0 else float("inf"))
            rows.append(dict(
                cfg={**sc, **wc},
                is_net=sum(per[t][0] for t in is_tags),
                oos_net=sum(per[t][0] for t in oos_tags),
                is_n=sum(per[t][1] for t in is_tags),
                oos_n=sum(per[t][1] for t in oos_tags),
                gate=sum(1 for t in bars if per[t][0] > 0 and per[t][2] > 1.0),
                per=per,
            ))
        if (si + 1) % max(1, len(scan_combos) // 10) == 0:
            done = (si + 1) * len(walk_combos)
            print(f"    {done:>6,}/{total:,}  {time.time()-t0:5.1f}s")

    is_net = np.array([r["is_net"] for r in rows])
    oos_net = np.array([r["oos_net"] for r in rows])
    rho = spearman(is_net, oos_net)

    print(f"\n  grid done in {time.time()-t0:.1f}s\n")
    print(f"  IS  net across grid: median ${np.median(is_net):+,.0f}  "
          f"sd ${is_net.std():,.0f}  best ${is_net.max():+,.0f}  "
          f"frac>0 {(is_net > 0).mean():.2f}")
    print(f"  OOS net across grid: median ${np.median(oos_net):+,.0f}  "
          f"sd ${oos_net.std():,.0f}  best ${oos_net.max():+,.0f}  "
          f"frac>0 {(oos_net > 0).mean():.2f}")
    print(f"\n  IS->OOS Spearman rho = {rho:+.3f}   "
          + ("tuning transfers" if rho > 0.3 else
             "tuning does NOT transfer -- the grid is ranking noise"))

    order = np.argsort(-is_net)
    print(f"\n  Top 10 by IS net (and what they did OOS):")
    print(f"  {'IS net':>9} {'OOS net':>9} {'OOS n':>6} {'gate':>5}  config")
    for k in order[:10]:
        r = rows[k]
        cfg = " ".join(f"{a}={b}" for a, b in r["cfg"].items())
        print(f"  ${r['is_net']:>+8,.0f} ${r['oos_net']:>+8,.0f} "
              f"{r['oos_n']:>6} {r['gate']}/{len(bars)}  {cfg}")

    best = rows[int(order[0])]
    oos_pct = 100.0 * (oos_net < best["oos_net"]).mean()
    print(f"\n  IS-best config OOS net ${best['oos_net']:+,.0f} sits at the "
          f"{oos_pct:.0f}th percentile of the grid's own OOS distribution")

    full_pass = [r for r in rows if r["gate"] == len(bars)]
    print(f"  configs passing net>0 AND PF>1 on all {len(bars)} contracts: "
          f"{len(full_pass)}/{total:,} ({100.0*len(full_pass)/total:.2f}%)")
    if full_pass:
        b2 = max(full_pass, key=lambda r: r["is_net"] + r["oos_net"])
        print("    best full-gate config: "
              + " ".join(f"{a}={b}" for a, b in b2["cfg"].items()))
        print(f"    IS ${b2['is_net']:+,.0f}  OOS ${b2['oos_net']:+,.0f}  "
              f"n={b2['is_n']+b2['oos_n']}")
        print("    -> re-sign-null it before believing it:")
        env = " ".join(f"TF_{a.upper()}={b}" for a, b in b2["cfg"].items())
        print(f"       {env} python backtest_tfpe_null.py 200 --all")


if __name__ == "__main__":
    main()
