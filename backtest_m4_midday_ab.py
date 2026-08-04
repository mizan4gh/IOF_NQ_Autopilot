#!/usr/bin/env python3
"""In-strategy A/B: M4 midday sub-floor parallel-admit vs live baseline.

The decisive test for the one lead from edge_subfloor_scan.py — does admitting
M4 q=40 setups in the 12:00-14:00 window (floor still 50 everywhere else)
survive the REAL strategy's MT=1 slot displacement (the test that killed M2 f46),
across all 6 frozen contracts?

Each backtest runs in its OWN subprocess (_run_one_m4mid.py) so the 1.4 GB tick
files don't accumulate in one process. Completed CSVs are reused (resume-safe).

Baseline = live panel config (MT=1 / DL=$800 / floor 50 / M8 floor 60 / news on /
R2b late-entry 15:00 / ENTRY_ORD=2). Test = baseline + M4_MIDDAY_ADMIT.

Usage: python backtest_m4_midday_ab.py
"""
import csv, os, subprocess, sys
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = ["F.US.ENQZ25", "F.US.ENQM25", "F.US.ENQU25",
             "F.US.ENQH26", "F.US.ENQM26", "F.US.ENQU26"]
SCENARIOS = ["baseline", "m4mid40"]
BASELINE = "baseline"


def ensure(contract: str, name: str) -> Path:
    out = BASE / f"IOF_NQ_m4mid_{contract}_{name}.csv"
    if out.exists() and out.stat().st_size > 0:
        print(f"  (reuse) {out.name}", flush=True)
        return out
    print(f"  running {contract} / {name} ...", flush=True)
    subprocess.run([sys.executable, str(BASE / "_run_one_m4mid.py"), contract, name],
                   cwd=str(BASE), check=True)
    return out


def summarize(csv_path: Path) -> dict:
    tots = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("Event") != "EXIT":
                continue
            try:
                tots.append(float(row["TotalPnL"]))
            except (ValueError, KeyError):
                continue
    n = len(tots)
    if n == 0:
        return dict(n=0, total=0.0, wr=0.0, pf=0.0, maxdd=0.0)
    per = [tots[0]] + [tots[i] - tots[i - 1] for i in range(1, n)]
    wins = [p for p in per if p > 0]
    losses = [p for p in per if p < 0]
    gw, gl = sum(wins), sum(losses)
    peak = mdd = 0.0
    for t in tots:
        peak = max(peak, t)
        mdd = min(mdd, t - peak)
    return dict(n=n, total=tots[-1], wr=100 * len(wins) / n,
                pf=(gw / abs(gl)) if gl < 0 else float("inf"), maxdd=mdd)


def main():
    results = {}
    for c in CONTRACTS:
        if not (BASE / (c + ".scid")).exists():
            print(f"  !! missing {c}.scid — skipping", flush=True); continue
        results[c] = {name: summarize(ensure(c, name)) for name in SCENARIOS}

    print("\n" + "=" * 84)
    print("  M4-MIDDAY PARALLEL-ADMIT A/B  (live MT=1 config)")
    print("=" * 84)
    hdr = (f"  {'Contract':<9}{'baseN':>6}{'base P&L':>11}{'testN':>6}{'test P&L':>11}"
           f"{'dP&L':>10}{'dMaxDD':>10}  verdict")
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    pooled_b = pooled_t = pdd_b = pdd_t = 0.0
    worse = better = neval = 0
    live_v = None
    for c in CONTRACTS:
        if c not in results:
            continue
        neval += 1
        b, t = results[c][BASELINE], results[c]["m4mid40"]
        dp, ddd = t["total"] - b["total"], t["maxdd"] - b["maxdd"]
        pooled_b += b["total"]; pooled_t += t["total"]
        pdd_b += b["maxdd"]; pdd_t += t["maxdd"]
        v = "test_better" if dp > 0 else "test_worse" if dp < 0 else "tied"
        better += dp > 0; worse += dp < 0
        if c == "F.US.ENQU26":
            live_v = (dp, v)
        print(f"  {c:<9}{b['n']:>6}{b['total']:>11,.0f}{t['n']:>6}{t['total']:>11,.0f}"
              f"{dp:>+10,.0f}{ddd:>+10,.0f}  {v}")
    print("  " + "-" * (len(hdr) - 2))
    print(f"  {'POOLED':<9}{'':>6}{pooled_b:>11,.0f}{'':>6}{pooled_t:>11,.0f}"
          f"{pooled_t - pooled_b:>+10,.0f}{pdd_t - pdd_b:>+10,.0f}")

    print("\n  VERDICT:")
    print(f"    P&L: test_better {better}/{neval}, test_worse {worse}")
    print(f"    Pooled dP&L ${pooled_t - pooled_b:+,.0f}   Pooled dMaxDD ${pdd_t - pdd_b:+,.0f}")
    if live_v:
        print(f"    LIVE NQU26: dP&L ${live_v[0]:+,.0f} -> {live_v[1]}")
    ship = (pooled_t - pooled_b > 0) and worse == 0 and (live_v and live_v[0] >= 0)
    print(f"    => {'SHIP-CANDIDATE (pooled+, no contract worse, live not worse)' if ship else 'NO SHIP'}")


if __name__ == "__main__":
    main()
