"""A/B: DAILY_LOSS=500 + DAILY_PROF=500 vs production baseline (loss800/prof0).

Requested run: model IOF_NQ_Autopilot with a $500 daily-loss AND $500
daily-profit cap. Prod config otherwise (3k vol, single-lot, news ON,
C_OPEN_COOL=36, MAX_TRADES=6 default).

Note: in backtest.py the DAILY_LOSS/DAILY_PROF caps are checked on *realized*
day_pnl at bar start (they block a subsequent setup / flatten an open pos);
they do NOT truncate an open winner intrabar. So they can only bite on days
with a 2nd setup after the 1st already crossed +/-$500. Prior sweeps
{800,1200,1600,2000} and {0,800,1000,1200} were NO-OPs for this reason. This
tests whether the tighter $500 threshold changes anything.

Runs across the 6 frozen NQ contracts and pools.

Usage: python backtest_caps500_ab.py
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = [
    "F.US.ENQZ25.scid", "F.US.ENQM25.scid", "F.US.ENQU25.scid",
    "F.US.ENQH26.scid", "F.US.ENQM26.scid", "F.US.ENQU26.scid",
]

_PROD = dict(
    NEWS_FILTER=1,
    C_OPEN_COOL=36,
    TARGET_VOL=3000,
    SCALE_OUT=False,
)

SCENARIOS = {
    "base_800_0":   {**_PROD, "DAILY_LOSS": 800.0, "DAILY_PROF": 0.0},
    "caps_500_500": {**_PROD, "DAILY_LOSS": 500.0, "DAILY_PROF": 500.0},
}
BASELINE = "base_800_0"


def run_scenario(scid, name, overrides):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_caps500_{scid.stem}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
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
        return dict(n=0, wins=0, losses=0, total=0.0, pf=0.0, wr=0.0,
                    max_dd=0.0, per_trade=[], reasons={})

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

    reasons = {}
    for _, _, r in trades:
        reasons[r] = reasons.get(r, 0) + 1

    return dict(n=n, wins=len(wins), losses=len(losses), total=total,
                pf=pf, wr=wr, max_dd=max_dd, per_trade=per_trade, reasons=reasons)


def main():
    print("A/B: caps_500_500 vs prod base_800_0 across 6 frozen NQ contracts\n")
    table = {}  # scid -> {scenario -> summary}
    for cname in CONTRACTS:
        scid = BASE / cname
        if not scid.exists():
            print(f"  MISSING: {cname}")
            continue
        table[scid.stem] = {}
        for sname, ov in SCENARIOS.items():
            table[scid.stem][sname] = summarize(run_scenario(scid, sname, ov))

    # Per-contract comparison
    print("\n" + "=" * 78)
    print(f"{'Contract':<14}{'Scenario':<14}{'N':>4}{'W/L':>8}{'PnL $':>11}"
          f"{'PF':>7}{'MaxDD $':>11}")
    print("-" * 78)
    pooled = {s: [] for s in SCENARIOS}
    dl_fires = {s: 0 for s in SCENARIOS}
    dp_fires = {s: 0 for s in SCENARIOS}
    for stem, per in table.items():
        for sname in SCENARIOS:
            r = per[sname]
            wl = f"{r['wins']}/{r['losses']}"
            print(f"{stem:<14}{sname:<14}{r['n']:>4}{wl:>8}"
                  f"{r['total']:>11,.0f}{r['pf']:>7.2f}{r['max_dd']:>11,.0f}")
            pooled[sname].extend(r["per_trade"])
            dl_fires[sname] += r["reasons"].get("DAILY_LOSS", 0)
            dp_fires[sname] += r["reasons"].get("DAILY_PROFIT", 0)
        print("-" * 78)

    # Pooled
    print("\nPOOLED (all contracts chained):")
    for sname in SCENARIOS:
        pt = pooled[sname]
        tot = sum(pt)
        w = [p for p in pt if p > 0]; l = [p for p in pt if p < 0]
        pf = sum(w) / abs(sum(l)) if l else float("inf")
        print(f"  {sname:<14} N={len(pt):<4} PnL ${tot:>10,.0f}  "
              f"PF={pf:.2f}  WR={100*len(w)/len(pt) if pt else 0:.1f}%  "
              f"DAILY_LOSS fires={dl_fires[sname]}  DAILY_PROFIT fires={dp_fires[sname]}")

    b = sum(pooled[BASELINE]); t = sum(pooled["caps_500_500"])
    print(f"\n  Pooled delta (caps_500_500 - base): ${t - b:+,.0f}")

    # Byte-identical check per contract
    print("\nPer-contract verdict:")
    identical = True
    for stem, per in table.items():
        d = per["caps_500_500"]["total"] - per[BASELINE]["total"]
        dn = per["caps_500_500"]["n"] - per[BASELINE]["n"]
        v = "IDENTICAL" if (abs(d) < 0.01 and dn == 0) else ("better" if d > 0 else "worse")
        if v != "IDENTICAL":
            identical = False
        print(f"  {stem:<14} dPnL ${d:>+9,.0f}  dN {dn:>+3d}  -> {v}")
    print(f"\n  {'ALL BYTE-IDENTICAL -> caps are a NO-OP on this dataset' if identical else 'Caps changed results on >=1 contract'}")


if __name__ == "__main__":
    main()
