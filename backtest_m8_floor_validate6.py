"""Re-validate v12.38 M8 floor-60 vs baseline-50 across SIX frozen contracts
(user copied stable F.US.E* snapshots into the project dir 2026-06-29, after the
mid-run NQH6 rewrite incident — see [[project_nqh6_data_rewrite_incident]]).

Reports Net, Sortino, MaxDD per contract and BOTH a P&L/Sortino verdict and a
MaxDD verdict, since floor-60 is really a drawdown-vs-P&L trade-off. Includes
the LIVE contract NQU26.

Usage: python backtest_m8_floor_validate6.py [TAG]   (default: all six)
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
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(),
    M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False, TREND_LONG=False,
)
SCENARIOS = {
    "baseline": {**_PROD, "QUAL_FLOOR_M8": None},
    "floor60":  {**_PROD, "QUAL_FLOOR_M8": 60},
}


def run(nm, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_flr6_{tag}_{nm}.csv"
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


def s(p):
    tr, pv = [], 0.0
    for r in csv.DictReader(open(p, newline="")):
        if r["Event"] == "EXIT":
            t = float(r["TotalPnL"]); tr.append((t - pv, r["Date"], r["Mode"])); pv = t
    if not tr:
        return dict(n=0, wr=0, pf=0, tot=0, md=0, sd=float("nan"), m8net=0, m8n=0)
    pn = [x[0] for x in tr]; w = [p for p in pn if p > 0]; l = [p for p in pn if p < 0]
    tot = sum(pn); pf = sum(w) / abs(sum(l)) if l else 9.99
    pk = run = md = 0.0
    for p in pn:
        run += p; pk = max(pk, run); md = min(md, run - pk)
    dl = defaultdict(float)
    for x in tr: dl[x[1]] += x[0]
    ds = sorted(dl); nd = wd(ds[0], ds[-1])
    sd = (sum(dl.values()) / nd) / (math.sqrt(sum(min(v, 0) ** 2 for v in dl.values()) / nd) or 1)
    m8 = [x[0] for x in tr if x[2] == "M8"]
    return dict(n=len(tr), wr=100*len(w)/len(tr), pf=pf, tot=tot, md=md, sd=sd,
                m8net=sum(m8), m8n=len(m8))


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}"); continue
        res[tag] = {nm: s(run(nm, ov, scid, tag)) for nm, ov in SCENARIOS.items()}

    print("\n" + "=" * 96)
    print(" v12.38 floor-60 RE-VALIDATION — 6 frozen contracts (incl. live NQU26)")
    print("=" * 96)
    print(f"  {'contract':8s} {'arm':9s} {'n':>4s} {'WR':>5s} {'PF':>5s} {'Net':>9s} "
          f"{'MaxDD':>9s} {'Sort':>6s} {'M8net':>8s}")
    pnl_v = {}; dd_v = {}
    for tag, scn in res.items():
        b, f = scn["baseline"], scn["floor60"]
        for nm, r in scn.items():
            print(f"  {tag:8s} {nm:9s} {r['n']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
                  f"{r['tot']:>+9,.0f} {r['md']:>9,.0f} {r['sd']:>6.2f} {r['m8net']:>+8,.0f}")
        dp = f["tot"] - b["tot"]; dd = f["md"] - b["md"]; dsd = f["sd"] - b["sd"]
        pnl_v[tag] = "BETTER" if (dp > 0 and dsd >= 0) else "worse"
        dd_v[tag] = "BETTER" if dd > 0 else ("worse" if dd < 0 else "same")
        cost = f"  (give up ${-dp/dd:.1f} P&L per $1 DD saved)" if (dd > 0 and dp < 0) else ("  (pure win)" if dp > 0 and dd > 0 else "")
        print(f"           delta     dPnL={dp:>+8,.0f}[{pnl_v[tag]:6s}] dMaxDD={dd:>+8,.0f}[{dd_v[tag]:6s}] dSort={dsd:+.2f}{cost}\n")

    nb = sum(1 for v in pnl_v.values() if v == "BETTER")
    ndd = sum(1 for v in dd_v.values() if v == "BETTER")
    print(f" P&L/Sortino gate : floor60 BETTER on {nb}/{len(pnl_v)}  => "
          f"{'PASS' if nb == len(pnl_v) else 'NO-SHIP'}  {pnl_v}")
    print(f" MaxDD gate       : floor60 BETTER on {ndd}/{len(dd_v)}  => "
          f"{'PASS' if ndd == len(dd_v) else 'mixed'}  {dd_v}")


if __name__ == "__main__":
    main()
