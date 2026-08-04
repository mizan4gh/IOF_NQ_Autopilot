"""
M2-ONLY strategy, MNQ (micro), floor 33 — standalone, 3 contracts.

Spec (user, 2026-07-23):
  * M2 the SOLE live mode (DISABLE_MODES = everything except 1==M2)
  * MNQ economics: PT_VAL=2.0 ($2/pt), COMMISSION=1.0 RT  (NQ harness default is
    $20/pt, $5 RT — MNQ is 1/10 notional; commission set to a retail MNQ estimate,
    adjust if your broker differs)
  * QUAL_FLOOR_M2 = 33   (NB: 33 == 30 for M2; qual grid is ...26, 33, 40...,
    band [30,33) is empty — this is the floor-30 set, [[project_m2_qf25_falsified]])
  * DAILY_LOSS = 300, DAILY_PROF = 500, MAX_TRADES = 3
  * 3 contracts: NQZ25, NQM5, NQH6  (frozen F.US.E* snapshots)

Everything else = clean live panel (VP-fixed + Imb-fixed).

Usage: python run_m2only_f33.py [NQZ25|NQM5|NQH6]   (default: all three)
"""
import csv, math, sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
}

PANEL = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    M8_FADE_FULL=True, V13_MODEL=False, QUAL_FLOOR_M8=60,
    IMB_MODEL="cpp_stateful",
    DISABLE_MODES={0, 2, 3, 4, 5, 6, 7},   # everything except 1==M2
    QUAL_FLOOR_M2=33,                       # == floor 30
    # MNQ economics
    PT_VAL=2.0,
    COMMISSION=1.0,
    # caps
    DAILY_LOSS=300.0,
    DAILY_PROF=500.0,
    MAX_TRADES=3,
)


def run_scenario(scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in PANEL.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_MNQ_m2only_f33_{tag}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: M2-only f33 MNQ =====")
    backtest.main()
    return out


def summarize(p):
    tr, prev = [], 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] != "EXIT":
            continue
        t = float(r["TotalPnL"])
        tr.append(dict(pnl=t - prev, date=r["Date"], mode=r["Mode"],
                       reason=r["ExitReason"]))
        prev = t
    if not tr:
        return dict(n=0, wr=0.0, pf=0.0, tot=0.0, md=0.0, t=float("nan"),
                    reasons={})
    pn = [x["pnl"] for x in tr]
    w = [x for x in pn if x > 0]
    l = [x for x in pn if x < 0]
    pk = run = md = 0.0
    for x in pn:
        run += x
        pk = max(pk, run)
        md = min(md, run - pk)
    n = len(pn)
    mean = sum(pn) / n
    var = sum((x - mean) ** 2 for x in pn) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 1 else 0.0
    reasons = defaultdict(int)
    for x in tr:
        reasons[x["reason"]] += 1
    return dict(n=n, wr=100 * len(w) / n,
                pf=(sum(w) / abs(sum(l)) if l else 9.99),
                tot=sum(pn), md=md,
                t=(mean / se if se > 0 else float("nan")),
                reasons=dict(reasons))


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}")
            continue
        res[tag] = summarize(run_scenario(scid, tag))

    print("\n" + "=" * 88)
    print(" M2-ONLY  floor 33  MNQ ($2/pt)  |  DL=$300 DP=$500 MT=3  |  3 contracts")
    print("=" * 88)
    print(f"  {'contract':8s} {'n':>4s} {'WR':>5s} {'PF':>5s} "
          f"{'Net':>9s} {'MaxDD':>9s} {'t':>7s}   exit-reasons")
    tot_all = 0.0
    worst_md = 0.0
    npos = 0
    for tag, r in res.items():
        rs = " ".join(f"{k}:{v}" for k, v in sorted(r["reasons"].items()))
        print(f"  {tag:8s} {r['n']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
              f"{r['tot']:>+9,.0f} {r['md']:>9,.0f} {r['t']:>7.2f}   {rs}")
        tot_all += r["tot"]
        worst_md = min(worst_md, r["md"])
        if r["tot"] > 0:
            npos += 1
    print("-" * 88)
    print(f"  POOLED   net={tot_all:>+9,.0f}   positive={npos}/{len(res)}   "
          f"worstMaxDD={worst_md:>+9,.0f}")
    print("\n  NB: floor 33 == floor 30 for M2 (empty [30,33) band). MNQ = 1/10 of NQ P&L.")


if __name__ == "__main__":
    main()
