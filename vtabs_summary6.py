"""Rebuild the 6-contract VWAP-touch+ABSORPTION summary from the per-contract CSVs.

The validate6 run was interrupted after 4 contracts, so its in-process summary was
lost. The CSVs survive, so recompute the table from disk instead of re-running.
Reuses backtest_vt_absorb_validate6.summarize() so numbers match the harness exactly.

Usage: python vtabs_summary6.py
"""
from pathlib import Path

from backtest_vt_absorb_validate6 import CONTRACTS, OOS, SCENARIOS, summarize

BASE = Path(__file__).parent


def main():
    res = {}
    for tag in CONTRACTS:
        for name in SCENARIOS:
            p = BASE / f"IOF_NQ_vtabs_{tag}_{name}.csv"
            if not p.exists():
                print(f"missing csv: {p.name}")
                continue
            res.setdefault(tag, {})[name] = summarize(p)

    print("\n" + "=" * 80)
    print(" VWAP-touch + ABSORPTION — 6 FROZEN contracts (OOS never tuned)")
    print("=" * 80)
    for name in SCENARIOS:
        have = [t for t in res if name in res[t]]
        print(f"\n  [{name}]  absorb>={SCENARIOS[name]['VWAP_TOUCH_ABSORB']}, 0.50 SD")
        print(f"  {'contract':8s} {'kind':8s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
              f"{'Net':>10s} {'MaxDD':>9s}")
        pooled = []
        npos = 0
        for tag in have:
            r = res[tag][name]
            pooled += r["pnls"]
            ok = r["total"] > 0 and r["pf"] > 1.0
            npos += 1 if ok else 0
            flag = "" if ok else " <-"
            kind = "OOS" if tag in OOS else "recheck"
            print(f"  {tag:8s} {kind:8s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f}{flag}")
        w = [p for p in pooled if p > 0]
        L = [p for p in pooled if p < 0]
        ppf = (sum(w) / abs(sum(L))) if L else float("inf")
        print(f"  POOLED {len(pooled):>10} trades  PF={ppf:5.2f}  Net={sum(pooled):>+10,.0f}"
              f"   -> {npos}/{len(have)} pass"
              + ("  ALL PASS" if npos == len(have) and len(have) == len(CONTRACTS) else "  FAIL"))


if __name__ == "__main__":
    main()
