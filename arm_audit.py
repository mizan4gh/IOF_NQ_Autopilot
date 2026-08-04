"""
Armed-mode audit — step 1 of resolving the live/backtest mode-mix inversion.

The problem this exists to answer
---------------------------------
The live SETUP journal (IOF_NQ_<sym>.csv) and the 6-contract backtest baselines
disagree on mode mix by far more than sampling noise:

    live SETUPs (NQU6, 2025-06-24..2026-07-30):  M4 331 | M8  29 | M2 10
    backtest trades (6 contracts, same era):     M4   2 | M8  59 | M2 14

Both sides run the same priority ladder (M6 > M8 > M4 > M3 > M2 > M1) and the
same 3000-contract volume cadence, so the inversion should not happen. Neither
log can localize it, because BOTH only record the mode that WON the ladder:

  - live writes a SETUP row for the selected mode only (Autopilot.cpp:4030)
  - backtest writes a SETUP row only when it actually enters (backtest.py:2559)

So a mode that arms constantly but is always outranked is invisible in both,
and the two logs are not even counting the same thing (live = every qualifying
signal; backtest = filled trades only).

What this measures
------------------
Both sides now emit the FULL armed set per bar, before the ladder resolves it,
in one shared schema:

    Date,Time,BarIdx,Armed,Sel,SelSide      e.g.  ...,M4S|M8S,M8,S

  cpp:      WriteArmed()  -> IOF_NQ_Armed_<sym>.csv   (live bars only; full
            recalc/historical download suppressed so a study reload cannot
            re-append history)
  backtest: ARM_CSV       -> written by this script per contract

That makes three questions answerable that the SETUP journals cannot answer:

  1. arm rate per mode  — does M4 really arm ~11x more than M8 live?
  2. outrank rate       — how often does M4 arm and lose the ladder to M6/M8?
  3. co-arm structure   — which modes arm on the same bar

If backtest M4 arm rate matches live M4 arm rate, the inversion is a selection/
logging artifact and the backtest is sound. If it does not, the harness has a
fidelity gap in M4's trigger and every backtest-derived conclusion about mode
behavior needs re-examination.

Usage
-----
    python arm_audit.py                 # all 6 contracts
    python arm_audit.py NQH6 NQM6       # subset
    python arm_audit.py --compare       # summary only, reuse existing CSVs

Live side: copy IOF_NQ_Armed_<sym>.csv off the trading machine into this
directory as IOF_NQ_Armed_live.csv, then re-run with --compare.

Panel matches the live config (MT=1/DL=800/M8 floor-60/news on/3k vol,
IMB_MODEL=cpp_stateful, M5/M7 off) so the arm rates are comparable to what the
live study is doing right now. Scid mtimes are fingerprinted before and after —
Sierra rewrites live-dir scids mid-run, which would silently invalidate this.
"""
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).parent

CONTRACTS = {
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQU25": BASE / "F.US.ENQU25.scid",
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
    "NQM6":  BASE / "F.US.ENQM26.scid",
    "NQU26": BASE / "F.US.ENQU26.scid",
}

# Live panel — same dict the other 6-contract harnesses use.
PANEL = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=1, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_TYPES={1, 2, 3, 4},
    ENABLE_M5=False, ENABLE_M7=False, TREND_LONG=False,
    M8_FADE_FULL=True, V13_MODEL=False, QUAL_FLOOR_M8=60,
    IMB_MODEL="cpp_stateful",
    DISABLE_MODES=set(),
)

MODES = ["M1", "M2", "M3", "M4", "M6", "M8"]


def scid_fingerprint():
    return {t: (p.stat().st_mtime_ns, p.stat().st_size)
            for t, p in CONTRACTS.items() if p.exists()}


def run_contract(tag, scid):
    """Run one contract at the live panel with ARM_CSV enabled."""
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    arm_out = BASE / f"IOF_NQ_Armed_{tag}.csv"
    trade_out = BASE / f"IOF_NQ_armaudit_{tag}.csv"
    for k, v in PANEL.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)
    backtest.ARM_CSV = str(arm_out)

    sys.argv = ["backtest.py", str(scid), str(trade_out)]
    print(f"\n===== {tag} =====")
    backtest.main()
    return arm_out, trade_out


def load_arms(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def summarize(rows, label):
    """arm counts, outrank counts, and co-arm pairs from an arm CSV."""
    armed = Counter()
    selected = Counter()
    outranked = defaultdict(Counter)   # mode -> Counter(winner)
    coarm = Counter()

    for r in rows:
        tags = r["Armed"].split("|") if r["Armed"] else []
        modes = sorted({t[:2] for t in tags})
        for m in modes:
            armed[m] += 1
        sel = r["Sel"]
        selected[sel] += 1
        for m in modes:
            if m != sel:
                outranked[m][sel] += 1
        if len(modes) > 1:
            coarm["+".join(modes)] += 1

    total = len(rows)
    print(f"\n=== {label} ===")
    print(f"armed bars: {total:,}")
    if not total:
        return armed, selected

    print(f"\n{'mode':<6} {'armed':>8} {'% of bars':>10} {'selected':>9} "
          f"{'outranked':>10} {'win rate of ladder':>19}")
    print("-" * 66)
    for m in MODES:
        a = armed[m]
        if not a:
            continue
        s = selected[m]
        o = sum(outranked[m].values())
        print(f"{m:<6} {a:>8,} {100.0*a/total:>9.1f}% {s:>9,} {o:>10,} "
              f"{100.0*s/a:>18.1f}%")

    print("\noutranked by:")
    for m in MODES:
        if not outranked[m]:
            continue
        by = ", ".join(f"{k} x{v:,}" for k, v in outranked[m].most_common())
        print(f"  {m}: {by}")

    if coarm:
        print("\nco-arm combinations (top 8):")
        for k, v in coarm.most_common(8):
            print(f"  {k:<20} {v:>6,}")
    return armed, selected


def compare(bt_armed, bt_total, live_path):
    """Side-by-side arm SHARE — the number that resolves the inversion."""
    if not live_path.exists():
        print(f"\n[live] {live_path.name} not found — copy "
              f"IOF_NQ_Armed_<sym>.csv off the trading machine to compare.")
        return
    live_rows = load_arms(live_path)
    live_armed, _ = summarize(live_rows, f"LIVE ({live_path.name})")
    live_total = len(live_rows)
    if not live_total or not bt_total:
        return

    print("\n=== ARM SHARE: backtest vs live ===")
    print(f"{'mode':<6} {'backtest %':>11} {'live %':>9} {'ratio':>8}")
    print("-" * 38)
    for m in MODES:
        b = 100.0 * bt_armed[m] / bt_total
        l = 100.0 * live_armed[m] / live_total
        ratio = (l / b) if b > 0 else float("inf")
        print(f"{m:<6} {b:>10.1f}% {l:>8.1f}% {ratio:>8.2f}x")
    print("\nratio ~1.0 => arm rates agree; the mode-mix inversion is a")
    print("selection/logging artifact and the backtest is sound.")
    print("ratio far from 1.0 for M4 => harness fidelity gap in M4's trigger.")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    compare_only = "--compare" in sys.argv

    targets = {t: p for t, p in CONTRACTS.items() if not args or t in args}
    missing = [t for t, p in targets.items() if not p.exists()]
    if missing:
        print(f"[warn] missing scid, skipping: {', '.join(missing)}")
        targets = {t: p for t, p in targets.items() if p.exists()}

    all_rows = []
    if compare_only:
        for tag in targets:
            p = BASE / f"IOF_NQ_Armed_{tag}.csv"
            if p.exists():
                all_rows += load_arms(p)
            else:
                print(f"[warn] {p.name} not found — run without --compare first")
    else:
        fp_before = scid_fingerprint()
        for tag, scid in targets.items():
            arm_out, _ = run_contract(tag, scid)
            all_rows += load_arms(arm_out)
        fp_after = scid_fingerprint()
        changed = [t for t in fp_before if fp_before[t] != fp_after.get(t)]
        if changed:
            print(f"\n*** SCID REWRITTEN MID-RUN: {', '.join(changed)} — "
                  f"results invalid, re-run against frozen copies ***")
            return 1

    bt_armed, _ = summarize(all_rows, "BACKTEST (pooled, 6 contracts)")
    compare(bt_armed, len(all_rows), BASE / "IOF_NQ_Armed_live.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
