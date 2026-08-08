"""Does the sweep-absorption trigger predict ANYTHING? A measurement pass.

The shipped config takes 44 trades across 7 contracts. Nothing can be learned
from 44 trades -- a real +$100/trade edge and a coin flip are indistinguishable
at that n. So this strips every governor and cap that exists to protect an
account rather than to select a signal, and pins size at 1 contract so P&L is a
clean R multiple:

    no daily target / loss / giveback, no max-trades, no consec-loss halt,
    no entry-distance cap, confluence 1 throughout

What remains is exactly the trigger: location -> sweep -> absorption ->
weakening + reclaim, with its own stop and target. That is the population whose
edge is in question, and it is the largest honest sample the rule can produce.

Then it asks, in order:
  1. does the whole population beat its own re-signed null?
  2. does ANY covariate measurable at entry separate winners from losers --
     level type, side, hour, confluence, sweep depth, entry distance, stop
     size, and above all WHICH reclaim clause fired?

(2) matters because filtering a directionless population cannot create edge;
it can only sub-select noise. A covariate that separates cross-contract is the
only thing that would justify a cpp change.

Usage: python swabs_measure.py [--all | --nq | --mnq]
"""
from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import replace

import numpy as np

import backtest_sweepabs_propeval as SA

# env overrides layer ON TOP, so SA_RECLAIM_MODE=level_only etc. still apply
MEASURE = SA.params_from_env(replace(
    SA.Params(),
    daily_target=0.0, daily_loss=0.0, giveback=0.0,
    max_trades_day=0, max_consec_loss=0,
    conf_after_first=1, conf_after_loss=1,
    max_entry_dist=0.0, fixed_qty=1,
))


def r_mult(t) -> float:
    return (t.exit_px - t.entry_px) * t.side / t.stop_pts


def block(title, groups, min_n=8):
    """groups: {label: [(r, tag), ...]}  -- prints per-group expectancy in R
    plus how many contracts it is positive on, which is the only thing that
    separates a real effect from one contract's drift."""
    print(f"\n  {title}")
    print(f"    {'group':<14} {'n':>5} {'meanR':>7} {'t':>6} {'win%':>6} "
          f"{'net>0 by contract':>18}")
    for lab in sorted(groups, key=lambda k: -len(groups[k])):
        rs = np.array([r for r, _ in groups[lab]])
        if len(rs) < min_n:
            continue
        per = defaultdict(float)
        for r, tag in groups[lab]:
            per[tag] += r
        pos = sum(1 for v in per.values() if v > 0)
        se = rs.std(ddof=1) / np.sqrt(len(rs)) if len(rs) > 1 else 0.0
        t = rs.mean() / se if se > 0 else 0.0
        print(f"    {lab:<14} {len(rs):>5} {rs.mean():>+7.3f} {t:>+6.2f} "
              f"{100*(rs > 0).mean():>5.1f}% {f'{pos}/{len(per)}':>18}")


def main():
    scope = next((a for a in sys.argv[1:] if a.startswith("--")), "--all")
    trades = []
    print(f"Measurement pass [bars={SA.BAR_MODE}] -- governors and caps OFF, "
          f"qty pinned to 1\n")
    for tag, scid in SA.contracts_for(scope).items():
        if not scid.exists():
            continue
        bars, sec = SA.load_bars(tag, scid)
        r = SA.walk(bars, SA.prep(bars, MEASURE, sec), MEASURE)
        rs = np.array([r_mult(t) for t in r.trades])
        net = float(rs.sum())
        print(f"  {tag:7s} n={len(rs):>4}  meanR={rs.mean() if len(rs) else 0:>+6.3f}"
              f"  netR={net:>+8.1f}  win={100*(rs > 0).mean() if len(rs) else 0:>5.1f}%")
        trades += [(t, tag) for t in r.trades]

    rs = np.array([r_mult(t) for t, _ in trades])
    n = len(rs)
    se = rs.std(ddof=1) / np.sqrt(n)
    print(f"\n  POOLED n={n}  meanR={rs.mean():+.3f}  se={se:.3f}  "
          f"t={rs.mean()/se:+.2f}  netR={rs.sum():+.1f}  "
          f"win={100*(rs > 0).mean():.1f}%")
    print(f"  A real edge would need meanR > {2*se:.3f} to clear 2se at this n.")

    g = lambda f: {k: v for k, v in _group(trades, f).items()}
    block("by reclaim clause (the split that is a real hypothesis)",
          g(lambda t: t.reclaim))
    block("by side", g(lambda t: "LONG" if t.side > 0 else "SHORT"))
    block("by level", g(lambda t: SA.LEVEL_NAME[t.level]))
    block("by hour", g(lambda t: f"{t.entry_hhmm//100:02d}:00"))
    block("by confluence", g(lambda t: f"conf={t.confluence}"))
    block("by entry distance (x day range)",
          g(lambda t: _bucket(t.entry_dist / t.scale,
                              [0.005, 0.010, 0.020], "d")))
    block("by sweep depth (x day range)",
          g(lambda t: _bucket(t.sweep_depth / t.scale,
                              [0.004, 0.008, 0.015], "s")))
    block("by stop size (points)",
          g(lambda t: _bucket(t.stop_pts, [15, 25, 40], "stop")))


def _group(trades, f):
    out = defaultdict(list)
    for t, tag in trades:
        out[f(t)].append((r_mult(t), tag))
    return out


def _bucket(v, edges, name):
    for i, e in enumerate(edges):
        if v < e:
            return f"{name}<{e:g}"
    return f"{name}>={edges[-1]:g}"


if __name__ == "__main__":
    main()
