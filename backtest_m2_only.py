"""M2-ONLY standalone strategy — floor sweep across SIX frozen contracts.

User manually observed M2 (VP level test) signals look correct but rarely
reach QUAL_FLOOR=50. Prior M2-only floor drops INSIDE the full strategy were
falsified 4x (25/30/33/40, [[project_m2_qf25_falsified]]) — but a pure
M2-only book (all other modes suppressed via DISABLE_MODES) is a different
config: no priority competition, no cap-sharing with other modes' trades.

Quality grid for M2 is (score*100)//15 -> bands 33,40,46,53,... so the only
distinct floors below 50 are 46, 40, 33. Floor 50 = M2 needs score>=8.

Config = standard harness prod caps (MT=6/DL=800; mt1==mt6 proven byte-
identical on this dataset for the full book, but NOT necessarily for a
higher-frequency M2-only book — so flag if any day has >1 M2 trade).

Usage: python backtest_m2_only.py [TAG]   (default: all six)
"""
import csv, math, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQU25": BASE / "F.US.ENQU25.scid",   # Sep-2025
    "NQZ25": BASE / "F.US.ENQZ25.scid",   # Dec-2025
    "NQM5":  BASE / "F.US.ENQM25.scid",   # Jun-2025
    "NQH6":  BASE / "F.US.ENQH26.scid",   # Mar-2026
    "NQM6":  BASE / "F.US.ENQM26.scid",   # Jun-2026
    "NQU26": BASE / "F.US.ENQU26.scid",   # Sep-2026 (LIVE)
}

# M2-only: suppress M1(0), M3(2), M4(3), M6(5), M8(7); M5/M7 default-off.
_M2ONLY = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    DISABLE_MODES={0, 2, 3, 5, 7},
)

SCENARIOS = {
    "m2_f50": {**_M2ONLY, "QUAL_FLOOR_M2": None},  # global 50 applies
    "m2_f46": {**_M2ONLY, "QUAL_FLOOR_M2": 46},    # score>=7
    "m2_f40": {**_M2ONLY, "QUAL_FLOOR_M2": 40},    # score>=6
    "m2_f33": {**_M2ONLY, "QUAL_FLOOR_M2": 33},    # score>=5 (= C_MIN_SCORE_ALL)
}


def run(nm, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_m2only_{tag}_{nm}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {nm} =====")
    backtest.main()
    return out


def wd(a, b):
    d = date.fromisoformat(a); e = date.fromisoformat(b); n = 0
    while d <= e:
        if d.weekday() < 5: n += 1
        d += timedelta(days=1)
    return n


def summarize(p):
    tr, pv = [], 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] == "EXIT":
            t = float(r["TotalPnL"])
            tr.append(dict(pnl=t - pv, date=r["Date"], mode=r["Mode"],
                           reason=r["ExitReason"])); pv = t
    if not tr:
        return dict(n=0, wr=0.0, pf=0.0, tot=0.0, md=0.0, sd=float("nan"),
                    reasons={}, multi_days=0, ndays=0)
    pn = [x["pnl"] for x in tr]
    w = [q for q in pn if q > 0]; l = [q for q in pn if q < 0]
    tot = sum(pn); pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run_ = md = 0.0
    for q in pn:
        run_ += q; pk = max(pk, run_); md = min(md, run_ - pk)
    dl = defaultdict(float); dn = defaultdict(int)
    for x in tr:
        dl[x["date"]] += x["pnl"]; dn[x["date"]] += 1
    ds = sorted(dl); nd = wd(ds[0], ds[-1])
    dd = math.sqrt(sum(min(v, 0.0) ** 2 for v in dl.values()) / nd)
    sd = (sum(dl.values()) / nd) / dd if dd > 0 else float("inf")
    reasons = defaultdict(int)
    for x in tr: reasons[x["reason"]] += 1
    return dict(n=len(tr), wr=100 * len(w) / len(tr), pf=pf, tot=tot, md=md,
                sd=sd, reasons=dict(reasons),
                multi_days=sum(1 for c in dn.values() if c > 1), ndays=nd)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}"); continue
        results[tag] = {nm: summarize(run(nm, ov, scid, tag))
                        for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 84)
    print(" M2-ONLY STANDALONE STRATEGY — floor sweep (prod caps MT=6/DL=800)")
    print("=" * 84)
    print(f"  {'contract':8s} {'floor':7s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s} {'multiTrDays':>11s}  exits")
    for tag, scn in results.items():
        for nm, r in scn.items():
            ex = " ".join(f"{k}:{v}" for k, v in sorted(r["reasons"].items()))
            print(f"  {tag:8s} {nm:7s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['tot']:>+10,.0f} {r['md']:>9,.0f} {r['sd']:>10.3f} "
                  f"{r['multi_days']:>11}  {ex}")
        print()

    print(" Pooled across contracts (per floor):")
    for nm in SCENARIOS:
        tot = sum(results[t][nm]["tot"] for t in results)
        n = sum(results[t][nm]["n"] for t in results)
        pos = sum(1 for t in results if results[t][nm]["tot"] > 0)
        print(f"  {nm:7s} pooled Net {tot:>+10,.0f}  trades {n:>4}  "
              f"contracts positive {pos}/{len(results)}")


if __name__ == "__main__":
    main()
