"""A/B: in-strategy QUAL_FLOOR_M2=46 vs live 50 — SIX frozen contracts.

Follow-up to the M2-only standalone sweep ([[project_m2_only_strategy_sweep]]):
floor 46 (score>=7) was the only band with edge (pooled +$9.9k, 4/6) but the
LIVE contract NQU26 lost -$2,325. Prior in-strategy falsifications tested
floors 25/30/33/40 — never 46. This closes that last gap.

Baseline models CURRENT live config: MT=1/DL=800 (panel since 2026-07-02,
[[project_mt3_dl800_config]]) + M8 floor-60 (v12.38 deployed 2026-07-05).
Only delta in test arm: M2 (sel==1) floor 50 -> 46.

Gate: cross-contract agreement incl. live NQU26 [[feedback_cross_contract_ab]].

Usage: python backtest_m2_qf46_ab.py [TAG]   (default: all six)
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
_PROD = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, QUAL_FLOOR_M8=60, M8_FADE_FULL=True,
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False, ENABLE_M7=False,
    TREND_LONG=False, DISABLE_MODES=set(),
)
SCENARIOS = {
    "qf50_live": {**_PROD, "QUAL_FLOOR_M2": None},
    "m2qf46":    {**_PROD, "QUAL_FLOOR_M2": 46},
}
BASELINE = "qf50_live"


def run(nm, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_m2qf46_{tag}_{nm}.csv"
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
            tr.append(dict(pnl=t - pv, date=r["Date"], mode=r["Mode"])); pv = t
    if not tr:
        return dict(n=0, wr=0.0, pf=0.0, tot=0.0, md=0.0, sd=float("nan"),
                    m2n=0, m2net=0.0, modes={})
    pn = [x["pnl"] for x in tr]
    w = [q for q in pn if q > 0]; l = [q for q in pn if q < 0]
    tot = sum(pn); pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run_ = md = 0.0
    for q in pn:
        run_ += q; pk = max(pk, run_); md = min(md, run_ - pk)
    dl = defaultdict(float)
    for x in tr: dl[x["date"]] += x["pnl"]
    ds = sorted(dl); nd = wd(ds[0], ds[-1])
    dd = math.sqrt(sum(min(v, 0.0) ** 2 for v in dl.values()) / nd)
    sd = (sum(dl.values()) / nd) / dd if dd > 0 else float("inf")
    m2 = [x["pnl"] for x in tr if x["mode"] == "M2"]
    modes = defaultdict(int)
    for x in tr: modes[x["mode"]] += 1
    return dict(n=len(tr), wr=100 * len(w) / len(tr), pf=pf, tot=tot, md=md,
                sd=sd, m2n=len(m2), m2net=sum(m2), modes=dict(modes))


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}"); continue
        results[tag] = {nm: summarize(run(nm, ov, scid, tag))
                        for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 86)
    print(" IN-STRATEGY M2 QUAL_FLOOR=46 A/B (live config MT=1/DL=800, M8 floor-60)")
    print("=" * 86)
    print(f"  {'contract':8s} {'scenario':10s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s} {'M2_n':>5s} {'M2_Net':>9s}  modes")
    for tag, scn in results.items():
        for nm, r in scn.items():
            mo = " ".join(f"{m}:{c}" for m, c in sorted(r["modes"].items()))
            print(f"  {tag:8s} {nm:10s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['tot']:>+10,.0f} {r['md']:>9,.0f} {r['sd']:>10.3f} "
                  f"{r['m2n']:>5} {r['m2net']:>+9,.0f}  {mo}")
        print()

    print(" Verdict per contract (m2qf46 vs qf50_live):")
    verdicts = {}
    for tag, scn in results.items():
        b, t = scn[BASELINE], scn["m2qf46"]
        d_pnl = t["tot"] - b["tot"]
        d_sd = t["sd"] - b["sd"]
        d_dd = t["md"] - b["md"]
        if d_pnl == 0 and t["n"] == b["n"]:
            v = "tied"
        elif d_pnl > 0 and d_sd >= 0:
            v = "test_better"
        elif d_pnl < 0 or d_sd < 0:
            v = "test_worse"
        else:
            v = "tied"
        verdicts[tag] = v
        print(f"  {tag:8s} dPnL {d_pnl:+9,.0f}  dSortino/d {d_sd:+.3f}  "
              f"dMaxDD {d_dd:+9,.0f}  -> {v}")

    pooled = sum(results[t]["m2qf46"]["tot"] - results[t][BASELINE]["tot"]
                 for t in results)
    print(f"\n  pooled dPnL {pooled:+,.0f}")
    vs = set(verdicts.values())
    if vs <= {"test_better", "tied"} and "test_better" in vs:
        print("  never worse, better somewhere -- candidate to ship")
    elif vs == {"tied"}:
        print("  all tied -- vacuous, no-op")
    elif "test_better" in vs:
        print("  DISAGREE -- do not ship")
    else:
        print("  test not better anywhere -- keep floor 50")


if __name__ == "__main__":
    main()
