"""Calibration probe: run trend-long with the ts/chop filter OFF, capture the
trend-strength (ts) at each trend-long arm + its realized P&L, and tabulate the
ts distribution of winners vs losers per contract + pooled. Pick a threshold.
"""
import csv, sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")
CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "F.US.ENQM25.scid",
    "NQH6":  SC_DATA / "F.US.ENQH26.scid",
}
_PROD = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(), QUAL_FLOOR_M8=None,
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False,
    TREND_LONG=True, TREND_MIN_TS=0.0, TREND_MAX_CHOP=99,   # filter OFF
)


def run(scid, out):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in _PROD.items():
        setattr(backtest, k, v)
    sys.argv = ["backtest.py", str(scid), str(out)]
    backtest.main()


def trend_trades(p):
    """[(ts, pnl), ...] for trend-long (M5) trades."""
    rows = list(csv.DictReader(open(p, newline="")))
    out, prev, pend = [], 0.0, None
    for r in rows:
        if r["Event"] == "SETUP" and r["Mode"] == "M5":
            pend = float(r["TrendTS"])
        elif r["Event"] == "EXIT":
            tot = float(r["TotalPnL"]); pnl = tot - prev; prev = tot
            if r["Mode"] == "M5" and pend is not None:
                out.append((pend, pnl)); pend = None
            elif r["Mode"] == "M5":
                out.append((0.0, pnl))
    return out


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    allt = []
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}"); continue
        out = BASE / f"IOF_NQ_trlcal_{tag}.csv"
        run(scid, out)
        tt = trend_trades(out)
        allt += [(tag, ts, pnl) for ts, pnl in tt]
        w = [p for ts, p in tt if p > 0]; l = [p for ts, p in tt if p <= 0]
        print(f"\n{tag}: {len(tt)} trend-longs | net {sum(p for _,p in tt):+,.0f} | "
              f"win ts avg {sum(ts for ts,p in tt if p>0)/max(1,len(w)):.2f} "
              f"| loss ts avg {sum(ts for ts,p in tt if p<=0)/max(1,len(l)):.2f}")

    print("\n" + "=" * 70)
    print(" Pooled: net P&L kept vs trades kept at each ts threshold")
    print("=" * 70)
    print(f"  {'thresh':>7s} {'kept_n':>7s} {'kept_net':>10s} {'cut_n':>6s} {'cut_net':>9s} {'kept_WR':>8s}")
    for th in [0.0, 0.3, 0.5, 0.7, 0.9, 1.0, 1.2, 1.5, 2.0]:
        kept = [(ts, p) for _, ts, p in allt if ts >= th]
        cut  = [(ts, p) for _, ts, p in allt if ts < th]
        kw = [p for ts, p in kept if p > 0]
        print(f"  {th:>7.1f} {len(kept):>7} {sum(p for _,p in kept):>+10,.0f} "
              f"{len(cut):>6} {sum(p for _,p in cut):>+9,.0f} "
              f"{100*len(kw)/max(1,len(kept)):>7.1f}%")

    print("\n Per-contract net at candidate thresholds:")
    for th in [0.0, 0.5, 0.7, 0.9, 1.0]:
        line = f"  ts>={th:.1f}: "
        for tag in tags:
            net = sum(p for t, ts, p in allt if t == tag and ts >= th)
            n = sum(1 for t, ts, p in allt if t == tag and ts >= th)
            line += f"{tag} {net:+,.0f}({n})  "
        print(line)


if __name__ == "__main__":
    main()
