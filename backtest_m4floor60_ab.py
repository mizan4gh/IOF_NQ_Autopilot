"""A/B: M4 quality floor 50 (live) vs 60 (skip sc=5 M4 setups).

Motivated by 2026-06-08 ENQM26 live loss: SHORT M4 Q=50 Sc=5 stopped out -$810.
Today's setup was minimum on every axis (Q=50, Sc=5, RM=0.77). M4 with sc=5
fires the most marginal M4 reject trades; raising the M4-only floor to 60
filters them out while keeping the higher-conviction sc>=6 setups.

Production config per [[project_1pd_nocap_validated]]: MAX_TRADES=1,
DAILY_LOSS=0, DAILY_PROF=0, C_OPEN_COOL=10.

Cross-contract gate per [[feedback_cross_contract_ab]]: must AGREE
test_better on NQZ25 + NQM5 + NQH6 before any cpp ship.

Usage: python backtest_m4floor60_ab.py
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "NQM5.CME.scid",
    "NQH6":  SC_DATA / "NQH6.CME.scid",
}

_PROD = dict(
    NEWS_FILTER=1,
    C_OPEN_COOL=10,
    TARGET_VOL=3000,
    SCALE_OUT=False,
    MAX_TRADES=1,
    DAILY_LOSS=0.0,
    DAILY_PROF=0.0,
    QUAL_FLOOR=50,
)

SCENARIOS = {
    "live_m4_50":  {**_PROD, "QUAL_FLOOR_M4": None},
    "test_m4_60":  {**_PROD, "QUAL_FLOOR_M4": 60},
}
BASELINE = "live_m4_50"


def run_scenario(name, overrides, scid_path, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_m4floor60_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid_path), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  QUAL_FLOOR_M4 = {overrides['QUAL_FLOOR_M4']}")
    backtest.main()
    return out


def summarize(csv_path):
    trades = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            try:
                tot = float(row["TotalPnL"])
            except (ValueError, KeyError):
                continue
            trades.append((tot, row.get("Mode", ""), row.get("ExitReason", "")))

    n = len(trades)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0, mode_count={}, m4_n=0, m4_pnl=0.0)

    per_trade = [trades[0][0]] + [trades[i][0] - trades[i-1][0] for i in range(1, n)]
    wins = [p for p in per_trade if p > 0]
    losses = [p for p in per_trade if p < 0]
    gross_win, gross_loss = sum(wins), sum(losses)
    total = trades[-1][0]
    pf = (gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf")
    wr = (len(wins) / n * 100)

    peak, max_dd = 0.0, 0.0
    for tot, _, _ in trades:
        peak = max(peak, tot)
        max_dd = min(max_dd, tot - peak)

    mode_count = {}
    for _, m, _ in trades:
        mode_count[m] = mode_count.get(m, 0) + 1

    m4_pnl = sum(p for p, (_, m, _) in zip(per_trade, trades) if m == "M4")
    m4_n = mode_count.get("M4", 0)

    return dict(n=n, total=total, pf=pf, wr=wr, max_dd=max_dd,
                mode_count=mode_count, m4_n=m4_n, m4_pnl=m4_pnl)


def main():
    contracts = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in contracts:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}"); continue
        results[tag] = {}
        for name, ovr in SCENARIOS.items():
            out = run_scenario(name, ovr, scid, tag)
            results[tag][name] = summarize(out)

    print("\n" + "=" * 90)
    print(" M4 FLOOR A/B SUMMARY  (live=50  vs  test=60)")
    print("=" * 90)
    print(f"  {'contract':8s} {'scenario':14s} {'n':>4s} {'WR%':>6s} {'PF':>6s} {'Net$':>10s} {'MaxDD$':>10s} {'M4_n':>5s} {'M4$':>9s}")
    for tag, scn in results.items():
        for name in ["live_m4_50", "test_m4_60"]:
            r = scn.get(name)
            if r is None: continue
            print(f"  {tag:8s} {name:14s} {r['n']:>4d} {r['wr']:>5.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>+10,.0f} {r['m4_n']:>5d} {r['m4_pnl']:>+9,.0f}")
        print()

    print(" Cross-contract decision (test=60 vs live=50):")
    deltas = {}
    for tag, scn in results.items():
        base = scn[BASELINE]
        test = scn["test_m4_60"]
        d_pnl = test["total"] - base["total"]
        d_dd = test["max_dd"] - base["max_dd"]
        d_n = test["n"] - base["n"]
        d_m4 = test["m4_n"] - base["m4_n"]
        verdict = "test_better" if d_pnl > 0 else "test_worse" if d_pnl < 0 else "tied"
        deltas[tag] = (d_pnl, verdict)
        print(f"  {tag:8s}  ΔPnL ${d_pnl:+8,.0f}  ΔMaxDD ${d_dd:+8,.0f}  "
              f"ΔTrades {d_n:+3d}  ΔM4 {d_m4:+3d}  -> {verdict}")

    verdicts = [v for _, v in deltas.values()]
    if all(v == "test_better" for v in verdicts):
        print("\n  ★ ALL CONTRACTS AGREE test_better  →  ship M4 floor=60 to cpp")
    elif all(v in ("test_worse", "tied") for v in verdicts):
        print("\n  ✓ all neutral/worse — do not ship")
    else:
        print("\n  ✗ DISAGREE across contracts — do not ship (per cross-contract gate)")


if __name__ == "__main__":
    main()
