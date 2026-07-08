"""A/B: re-enable M5 (Trap Reversal) + M7 (Auction Reversal) at LOWER quality
floors — 40 and 30 — vs live (both off).

Background: the v12.20 port-back at the default floor 50 was FALSIFIED
2026-06-09 ([[project_m5_m7_portback_falsified]]): M5 lost on NQH6, M7 fired
zero anywhere. But M5/M7 quality = score*10 with Python score capped at 5, so
floor 50 admits ONLY score-5 arms and 60+ is unreachable. M7's zero fires may
have been floor kills, not absent setups. The only untested floor values are
40 (score>=4) and 30 (score>=3, M5's arm-gate minimum). This harness tests
them under the CURRENT deployed baseline (MT=1, DL=800, M8 fade floor 60),
which also differs from the June run (pre-M8-port, MT=6).

Arms:
  base_off   live config, M5/M7 disabled
  m5m7_f50   M5+M7 on, floors 50 (falsified config re-run under new baseline)
  m5m7_f40   M5+M7 on, floors 40
  m5m7_f30   M5+M7 on, floors 30

Data = frozen F.US.E* snapshots. Gate: NQZ25+NQM5+NQH6 must agree test_better
[[feedback_cross_contract_ab]].

Usage: python backtest_m5m7_floor_ab.py [NQZ25|NQM5|NQH6]   (default: all)
"""
import csv
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent

CONTRACTS = {
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
}

# Deployed live panel: MT=1, DL=800, M8 fade engine at floor 60 (2026-07-05).
_PANEL = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, QUAL_FLOOR_M8=60,
    M8_FADE_TYPES={1, 2, 3, 4}, DISABLE_MODES=set(),
    TREND_LONG=False,
)

SCENARIOS = {
    "base_off": {**_PANEL, "ENABLE_M5": False, "ENABLE_M7": False,
                 "QUAL_FLOOR_M5": None, "QUAL_FLOOR_M7": None},
    "m5m7_f50": {**_PANEL, "ENABLE_M5": True, "ENABLE_M7": True,
                 "QUAL_FLOOR_M5": 50, "QUAL_FLOOR_M7": 50},
    "m5m7_f40": {**_PANEL, "ENABLE_M5": True, "ENABLE_M7": True,
                 "QUAL_FLOOR_M5": 40, "QUAL_FLOOR_M7": 40},
    "m5m7_f30": {**_PANEL, "ENABLE_M5": True, "ENABLE_M7": True,
                 "QUAL_FLOOR_M5": 30, "QUAL_FLOOR_M7": 30},
}
BASELINE = "base_off"

# Per-contract volume-bar cache (pattern from run_v13_projection.py): bars are
# identical across arms (same TARGET_VOL, same frozen scid) and the engine
# never mutates Bar objects. Keyed on (path, TARGET_VOL).
_BAR_CACHE = {}


def run_scenario(name, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    key = (str(scid), ov["TARGET_VOL"])
    if key in _BAR_CACHE:
        bars = _BAR_CACHE[key]
        backtest.read_scid = lambda path: []
        backtest.build_volume_bars = \
            lambda recs, target_vol=ov["TARGET_VOL"], price_scale=1.0: bars
        print(f"  [cache] reusing {len(bars):,} pre-built bars for {tag}")
    else:
        _orig = backtest.build_volume_bars
        def _build_and_cache(recs, target_vol=ov["TARGET_VOL"], price_scale=1.0,
                             _orig=_orig, _key=key):
            b = _orig(recs, target_vol=target_vol, price_scale=price_scale)
            _BAR_CACHE[_key] = b
            return b
        backtest.build_volume_bars = _build_and_cache
    out = BASE / f"IOF_NQ_m5m7_{tag}_{name}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {name} =====")
    backtest.main()
    return out


def weekdays_between(d0, d1):
    n, d = 0, d0
    while d <= d1:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def summarize(csv_path):
    trades, prev = [], 0.0
    for row in csv.DictReader(open(csv_path, newline="")):
        if row["Event"] != "EXIT":
            continue
        tot = float(row["TotalPnL"])
        trades.append(dict(pnl=tot - prev, date=row["Date"], mode=row["Mode"]))
        prev = tot
    n = len(trades)
    if n == 0:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0,
                    s_day=float("nan"), mode_net={}, mode_n={})
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    peak = run = max_dd = 0.0
    for p in pnls:
        run += p
        peak = max(peak, run)
        max_dd = min(max_dd, run - peak)
    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["pnl"]
    days = sorted(daily)
    n_days = weekdays_between(date.fromisoformat(days[0]),
                              date.fromisoformat(days[-1]))
    mu = sum(daily.values()) / n_days
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in daily.values()) / n_days)
    s_day = mu / dd if dd > 0 else float("inf")
    mode_net, mode_n = defaultdict(float), defaultdict(int)
    for t in trades:
        mode_net[t["mode"]] += t["pnl"]
        mode_n[t["mode"]] += 1
    return dict(n=n, total=total, pf=pf, wr=100 * len(wins) / n,
                max_dd=max_dd, s_day=s_day,
                mode_net=dict(mode_net), mode_n=dict(mode_n))


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    results = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}")
            continue
        mtime_before = scid.stat().st_mtime
        results[tag] = {}
        for name, ov in SCENARIOS.items():
            out = run_scenario(name, ov, scid, tag)
            results[tag][name] = summarize(out)
        if scid.stat().st_mtime != mtime_before:
            print(f"WARNING: {scid} mtime changed during run — data not frozen!")

    print("\n" + "=" * 84)
    print(" M5+M7 FLOOR SWEEP A/B (deployed panel: MT=1 DL=800 M8f60; frozen data)")
    print("=" * 84)
    print(f"  {'contract':8s} {'scenario':10s} {'n':>4s} {'WR%':>6s} {'PF':>6s} "
          f"{'Net':>10s} {'MaxDD':>9s} {'Sortino/d':>10s}  M5(n/net)   M7(n/net)")
    for tag, scn in results.items():
        for name, r in scn.items():
            m5n, m5v = r["mode_n"].get("M5", 0), r["mode_net"].get("M5", 0.0)
            m7n, m7v = r["mode_n"].get("M7", 0), r["mode_net"].get("M7", 0.0)
            print(f"  {tag:8s} {name:10s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['s_day']:>10.3f}  "
                  f"{m5n}/{m5v:+,.0f}   {m7n}/{m7v:+,.0f}")
        print()

    print(" Verdicts vs base_off:")
    for test in ("m5m7_f50", "m5m7_f40", "m5m7_f30"):
        verdicts = {}
        for tag, scn in results.items():
            b, t = scn[BASELINE], scn[test]
            d_pnl = t["total"] - b["total"]
            d_sd = t["s_day"] - b["s_day"]
            if d_pnl == 0 and t["n"] == b["n"]:
                v = "no_op"
            elif d_pnl > 0 and d_sd >= 0:
                v = "test_better"
            else:
                v = "test_worse"
            verdicts[tag] = v
            print(f"  {test:10s} {tag:8s} PnL {d_pnl:+9,.0f}  "
                  f"Sortino/d {d_sd:+.3f}  -> {v}")
        vs = set(verdicts.values())
        if vs == {"test_better"}:
            print(f"  => {test}: ALL AGREE test_better — candidate")
        elif vs == {"no_op"}:
            print(f"  => {test}: NO-OP everywhere (floor unreachable)")
        elif "test_better" in vs:
            print(f"  => {test}: DISAGREE — do not ship")
        else:
            print(f"  => {test}: not better anywhere")
        print()


if __name__ == "__main__":
    main()
