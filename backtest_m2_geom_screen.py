"""SCREEN: is M2's problem its FLOOR, or its GEOMETRY? — 6 frozen contracts.

M2 is the VP-level trigger (backtest.py "M2 - VP level test"). Its arm condition
`bar.low <= lv + TICK` is a SUPERSET pooling two different shapes:
  touch  = price grazed the level and held        (no penetration)
  sweep  = price penetrated the level and closed back  (= M4's geometry)
M4 (sweep of a swing extreme) is the strategy's most defended mode. The
VWAP-touch engine (touch geometry, 3 flavors) is fully falsified. So the
hypothesis is that M2's famously bad PF curve is a GEOMETRY problem, not a floor
problem — every floor 25/30/33/40/46 has already been falsified
([[project_m2_qf25_falsified]], [[project_m2_only_strategy_sweep]]).

This is a SCREEN, not a ship gate:
  - M2-only book (config reused verbatim from backtest_m2_only.py) to maximize
    M2 sample. Standalone results do NOT transfer — floor-46 looked +$9.9k pooled
    standalone and then LOST on live NQU26 in-strategy via MT=1 slot displacement.
  - Floor held at the LIVE 50. Geometry is the ONLY variable. Do not add floor
    arms here; that is sweeping-to-pass ([[reference_ab_noise_floor]]).
  - Reports t per contract and pooled. At n~50, |t|<2 means "not distinguishable
    from zero", NOT "wins here / loses there".

Ship path IF the sweep arm shows real edge: in-strategy 6-contract A/B incl.
live NQU26, measuring displacement. Not this file.

Usage: python backtest_m2_geom_screen.py [TAG]   (default: all six)
"""
import csv
import statistics as st
import sys
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

# Verbatim from backtest_m2_only.py — M2-only book, live floor 50.
_M2ONLY = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    DISABLE_MODES={0, 2, 3, 5, 7},
)

# FLOOR 33, NOT the live 50 — deliberate, and NOT a ship config.
# M2 fires ZERO trades at floor 50 on all six contracts (verified), so a geometry
# gate at the live floor is an UNREACHABLE knob (cf. M4 sc-bump, M6 floor-40).
# Geometry can only be characterized where M2 actually fires, and floor 33 is the
# loosest band = max sample (71 trades pooled). Floor 33 is itself falsified
# ([[project_qf33_falsified]]) — it is used here ONLY as a lens to see the sweep/
# touch split, never as a shipping candidate.
_F33 = {**_M2ONLY, "QUAL_FLOOR_M2": 33}

SCENARIOS = {
    "geom_all":   {**_F33, "M2_GEOM": "all"},    # == existing m2_f33 (regression check)
    "geom_sweep": {**_F33, "M2_GEOM": "sweep"},  # penetrated the level (M4-like)
    "geom_touch": {**_F33, "M2_GEOM": "touch"},  # grazed only
}
BASELINE = "geom_all"

# POWER WARNING: 71 trades pooled, split ~2 ways => ~35/arm, se ~$150/trade.
# Only a LARGE separation (order +/-$300/trade) can register. A null here is
# "underpowered", NOT "proven equal" — do not read a small delta as signal.


def run(nm, ov, scid, tag):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in ov.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    out = BASE / f"IOF_NQ_m2geom_{tag}_{nm}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n===== {tag} :: {nm} =====")
    backtest.main()
    return out


def summarize(csv_path):
    trades, prev = [], 0.0
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            if r["Event"] != "EXIT":
                continue
            tot = float(r["TotalPnL"])
            trades.append(tot - prev)
            prev = tot
    if not trades:
        return dict(n=0, total=0.0, pf=0.0, wr=0.0, max_dd=0.0, t=0.0, pnls=[])
    w = [p for p in trades if p > 0]
    L = [p for p in trades if p < 0]
    n = len(trades)
    total = sum(trades)
    sd = st.pstdev(trades) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n and sd else 0.0
    peak = run_ = dd = 0.0
    for p in trades:
        run_ += p; peak = max(peak, run_); dd = min(dd, run_ - peak)
    return dict(n=n, total=total,
                pf=(sum(w) / abs(sum(L))) if L else float("inf"),
                wr=100 * len(w) / n, max_dd=dd,
                t=((total / n) / se) if se else 0.0, pnls=trades)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    res = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}")
            continue
        for nm, ov in SCENARIOS.items():
            res.setdefault(tag, {})[nm] = summarize(run(nm, ov, scid, tag))

    print("\n" + "=" * 84)
    print(" M2 GEOMETRY SCREEN — sweep vs touch @ live floor 50 (M2-only book)")
    print(" SCREEN ONLY — standalone edge does NOT imply in-strategy edge")
    print("=" * 84)
    for nm in SCENARIOS:
        print(f"\n  [{nm}]")
        print(f"  {'contract':8s} {'n':>4s} {'WR%':>6s} {'PF':>6s} {'Net':>10s} "
              f"{'MaxDD':>9s} {'t':>6s}")
        pooled = []
        for tag in res:
            r = res[tag][nm]
            pooled += r["pnls"]
            print(f"  {tag:8s} {r['n']:>4} {r['wr']:>6.1f} {r['pf']:>6.2f} "
                  f"{r['total']:>+10,.0f} {r['max_dd']:>9,.0f} {r['t']:>+6.2f}")
        if pooled:
            n = len(pooled)
            m = sum(pooled) / n
            sd = st.pstdev(pooled)
            se = sd / (n ** 0.5) if sd else 0.0
            w = [p for p in pooled if p > 0]
            L = [p for p in pooled if p < 0]
            pf = (sum(w) / abs(sum(L))) if L else float("inf")
            t = (m / se) if se else 0.0
            print(f"  POOLED   {n:>4} {100*len(w)/n:>6.1f} {pf:>6.2f} "
                  f"{sum(pooled):>+10,.0f} {'':>9s} {t:>+6.2f}"
                  f"    mean={m:+.0f}/trade"
                  + ("   <- SIGNIFICANT" if abs(t) >= 2 else "   (not distinguishable from 0)"))

    # Regression: M2_GEOM="all" must reproduce the pre-knob m2_f33 book exactly.
    print("\n  [regression] geom_all vs existing m2only m2_f33 (must be identical):")
    import hashlib
    for tag in res:
        a = BASE / f"IOF_NQ_m2geom_{tag}_geom_all.csv"
        b = BASE / f"IOF_NQ_m2only_{tag}_m2_f33.csv"
        if not (a.exists() and b.exists()):
            print(f"    {tag:8s} SKIP (missing artifact)")
            continue
        ha = hashlib.md5(a.read_bytes()).hexdigest()
        hb = hashlib.md5(b.read_bytes()).hexdigest()
        print(f"    {tag:8s} {'OK identical' if ha == hb else 'MISMATCH <- knob changed default path!'}")

    print("\n  Read: sweep >> touch with pooled |t|>=2 would support a geometry gate.")
    print("  Anything with |t|<2 is noise at this sample size — do not ship, do not sweep.")
    print("  Even a PASS here only buys an in-strategy A/B; it is not a ship signal.")


if __name__ == "__main__":
    main()
