"""A/B test: news-window blackout vs clean-M4-reclaim override.

Motivated by the 2026-06-02 no-fire chart: a textbook M4 LONG reclaim of the
overnight low fired exactly inside the 09:55-10:05 news window and got
filtered. The question: are news-window M4 reclaims net-positive across
historical data, or is the news blackout earning its keep?

Override definition (strict, M4-only):
  - close past sweep level by >= 2 ticks  (LONG: close > swLo+2t; SHORT: close < swHi-2t)
  - sc_l/sc_s >= 4 explicit  (no |div|>=2 fallback to 3)
  - all other modes (M1/M2/M3/M6/M8) remain blocked during news window
  - all normal M4 gates still apply (bull/bear, ctrl, vwap-edge)

Per [[feedback_cross_contract_ab]]: NQZ25 + NQM5 + NQH6 must all be
non-worse with at least one strictly positive before the cpp branch ships.

Usage:
    python backtest_news_override_ab.py
"""
import csv
import sys
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "NQM5.CME.scid",
    "NQH6":  SC_DATA / "NQH6.CME.scid",  # genuine Mar-2026 per project_nqh26_duplicate_data
}

_APEX = dict(
    C_COOL_TRADE=5, C_COOL_LOSS=10, C_COOL_STOP=10,
    C_OPEN_COOL=36, C_VCOOL_PAUSE=40,
    DAILY_LOSS=800.0, DAILY_PROF=1000.0,
    MAX_TRADES=3, ENTRY_ORD=2,
    NEWS_FILTER=1,            # mirrors live cpp: news blackout ON in both scenarios
    LATE_ENTRY_GATE=1500,     # v12.36 R2b — current production
    RISK_MODEL="v12_32_fixed",
    M1_PULLBACK=2,            # v12.34 dip+reclaim
    QUAL_FLOOR=50,
)

SCENARIOS = {
    "baseline_news_off":       {**_APEX, "NEWS_OVERRIDE_M4_RECLAIM": False},
    "override_m4_clean":       {**_APEX, "NEWS_OVERRIDE_M4_RECLAIM": True},
}


def run_scenario(name, overrides, scid_path, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in overrides.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_backtest_news_ovr_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid_path), str(out)]
    print(f"\n========== {tag} :: {name} ==========")
    print(f"  NEWS_OVERRIDE_M4_RECLAIM = {overrides['NEWS_OVERRIDE_M4_RECLAIM']}")
    bt = backtest.main()
    return out, bt.rm_gated


def is_news_hhmm(hhmm: int) -> bool:
    if  825 <= hhmm <=  835: return True
    if  955 <= hhmm <= 1005: return True
    if 1355 <= hhmm <= 1405: return True
    return False


def summarize(csv_path):
    trades = []
    news_trades = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["Event"] != "EXIT":
                continue
            try:
                pnl = float(row["DayPnL"])
            except (ValueError, KeyError):
                continue
            time = row.get("Time", "")
            mode = row.get("Mode", "")
            trades.append((pnl, mode, time))
            hhmm = 0
            try:
                hh, mm = time.split(":")[:2]
                hhmm = int(hh) * 100 + int(mm)
            except: pass
            if is_news_hhmm(hhmm):
                news_trades.append((pnl, mode))
    if not trades:
        return dict(n=0, net=0.0, wr=0.0, pf=0.0, news_n=0, news_pnl=0.0, news_wr=0.0)
    pnls = [t[0] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    news_wins = sum(1 for p, _ in news_trades if p > 0)
    news_wr = (news_wins / len(news_trades)) if news_trades else 0.0
    return dict(n=n, net=sum(pnls), wr=wins / n, pf=pf,
                news_n=len(news_trades),
                news_pnl=sum(p for p, _ in news_trades),
                news_wr=news_wr)


def main():
    contracts = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in contracts:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}"); continue
        results[tag] = {}
        for name, ovr in SCENARIOS.items():
            out, rm_gated = run_scenario(name, ovr, scid, tag)
            results[tag][name] = summarize(out)
            results[tag][name]["rm_gated"] = rm_gated

    print("\n" + "=" * 92)
    print(" NEWS-WINDOW OVERRIDE (M4 clean reclaim) — A/B SUMMARY")
    print("=" * 92)
    print(f"  {'contract':8s} {'scenario':22s} {'n':>4} {'WR':>7} {'PF':>6} "
          f"{'Net':>11}  {'news_n':>7} {'news_WR':>8} {'news_PnL':>10}  {'rm_gtd':>7}")
    for tag, scn in results.items():
        for name in ["baseline_news_off", "override_m4_clean"]:
            r = scn.get(name)
            if r is None: continue
            print(f"  {tag:8s} {name:22s} {r['n']:>4} {r['wr']:>6.1%} {r['pf']:>6.2f} "
                  f"{r['net']:>+11,.0f}  {r['news_n']:>7} {r['news_wr']:>7.1%} "
                  f"{r['news_pnl']:>+10,.0f}  {r['rm_gated']:>7}")
        print()

    print(" Cross-contract verdict:")
    deltas = {}
    for tag in results:
        base = results[tag]["baseline_news_off"]["net"]
        ovr  = results[tag]["override_m4_clean"]["net"]
        deltas[tag] = ovr - base
        nnews = results[tag]["override_m4_clean"]["news_n"]
        npnl  = results[tag]["override_m4_clean"]["news_pnl"]
        print(f"   {tag:8s} delta = ${deltas[tag]:+,.0f}  "
              f"(baseline ${base:+,.0f} -> override ${ovr:+,.0f})  "
              f"news-bar fires: {nnews} @ ${npnl:+,.0f}")
    if all(d > 0 for d in deltas.values()):
        print("\n  AGREE override_better -- safe to add news-override branch in cpp.")
    elif all(d >= 0 for d in deltas.values()):
        print("\n  AGREE neutral-or-better -- override does no harm. Ship if mechanism is sound.")
    elif all(d < 0 for d in deltas.values()):
        print("\n  AGREE override_worse -- DO NOT ship; news blackout is earning its keep.")
    else:
        print("\n  DISAGREE -- do not ship per [[feedback_cross_contract_ab]].")


if __name__ == "__main__":
    main()
