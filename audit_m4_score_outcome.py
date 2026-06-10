"""Pair finalScore (Sc=) on M4 SETUP/ENTRY with PnL outcome on EXIT.

Reads one or more SCLog.txt exports and aggregates per-Sc M4 trade outcomes:
  Sc=5: n=12, WR=33.3%, NetPnL=$-1,820, AvgPnL=$-152
  Sc=6: n= 8, WR=62.5%, NetPnL=$+1,640, AvgPnL=$+205
  Sc=7: ...

The cpp edit at IOF_NQ_Autopilot.cpp:1951 makes EXIT lines emit `Sc=N` so
score and outcome appear on the same trade index. Pairing is by sequential
ENTRY -> EXIT order within a single contract chart.

Usage:
    python audit_m4_score_outcome.py SCLog_2026-06-08.txt [SCLog_2026-06-09.txt ...]
    python audit_m4_score_outcome.py --mode all SCLog_*.txt   # all modes, not just M4

Filter rule: only count lines whose timestamp is OUTSIDE a recalc-burst window
(per [[reference_sclog_nofire_reading]] — burst lines are stamped within the
same HH:MM:SS second and are REPLAYED historical fires, not today's).
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

RX_ENTRY = re.compile(
    r"^(\S+\s+\S+)\s+\|.*\[V18A ENTRY\]\s+(LONG|SHORT)\s+(M\d)\s+Q=(\d+)"
)
RX_EXIT = re.compile(
    r"^(\S+\s+\S+)\s+\|.*\[V18A EXIT\]\s+(LONG|SHORT)\s+(M\d)\s+(\w+)\s+\$(-?\d+)\s+Sc=(\d+)"
)
RX_EXIT_NOSC = re.compile(
    r"^(\S+\s+\S+)\s+\|.*\[V18A EXIT\]\s+(LONG|SHORT)\s+(M\d)\s+(\w+)\s+\$(-?\d+)\s+Hold:"
)
RX_SETUP = re.compile(
    r"^(\S+\s+\S+)\s+\|.*\[V18A SETUP\]\s+(LONG|SHORT)\s+(M\d)\s+Q=(\d+)\s+Sc=(\d+)"
)


def detect_burst_seconds(lines):
    """Return set of 'YYYY-MM-DD HH:MM:SS' strings that look like recalc bursts
    (>=50 log lines sharing the same wall-second)."""
    counts = defaultdict(int)
    for ln in lines:
        m = re.match(r"^(\S+\s+\d\d:\d\d:\d\d)\.", ln)
        if m:
            counts[m.group(1)] += 1
    return {s for s, c in counts.items() if c >= 50}


def parse_log(path, mode_filter="M4"):
    """Yield (mode, score, pnl, exit_reason, side) tuples for live (non-burst) trades."""
    text = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    burst = detect_burst_seconds(text)

    pending_setup_sc = {}
    trades = []

    for ln in text:
        ts_match = re.match(r"^(\S+\s+\d\d:\d\d:\d\d)\.", ln)
        if not ts_match:
            continue
        ts = ts_match.group(1)
        if ts in burst:
            continue

        m = RX_SETUP.search(ln)
        if m:
            _, side, mode, q, sc = m.groups()
            pending_setup_sc[mode + side] = int(sc)
            continue

        m = RX_EXIT.search(ln)
        if m:
            _, side, mode, reason, pnl, sc = m.groups()
            if mode_filter == "all" or mode == mode_filter:
                trades.append(dict(
                    mode=mode, side=side, score=int(sc), pnl=int(pnl),
                    reason=reason, src=path,
                ))
            continue

        m = RX_EXIT_NOSC.search(ln)
        if m and not RX_EXIT.search(ln):
            _, side, mode, reason, pnl = m.groups()
            sc = pending_setup_sc.get(mode + side, -1)
            if mode_filter == "all" or mode == mode_filter:
                trades.append(dict(
                    mode=mode, side=side, score=sc, pnl=int(pnl),
                    reason=reason, src=path, score_source="setup_fallback",
                ))

    return trades


def summarize(trades, label="M4"):
    if not trades:
        print(f"\n{label}: 0 trades")
        return
    by_sc = defaultdict(list)
    for t in trades:
        by_sc[t["score"]].append(t)

    print(f"\n{label}  (n={len(trades)} trades across {len({t['src'] for t in trades})} log files)")
    print("=" * 78)
    print(f"  {'Sc':>3s}  {'n':>4s}  {'WR%':>6s}  {'NetPnL':>10s}  {'AvgPnL':>9s}  "
          f"{'Stop':>5s} {'Trail':>5s} {'Tp1':>4s} {'Tp2':>4s} {'Flat':>5s}")
    print("-" * 78)
    for sc in sorted(by_sc):
        ts = by_sc[sc]
        n = len(ts)
        wins = sum(1 for t in ts if t["pnl"] > 0)
        net = sum(t["pnl"] for t in ts)
        reasons = defaultdict(int)
        for t in ts:
            reasons[t["reason"]] += 1
        print(f"  {sc:>3d}  {n:>4d}  {wins/n*100:>5.1f}  ${net:>+9,d}  ${net//n:>+8,d}  "
              f"{reasons.get('STOP',0):>5d} {reasons.get('TRAIL',0):>5d} "
              f"{reasons.get('TP1',0):>4d} {reasons.get('TP2',0):>4d} "
              f"{reasons.get('FLATTEN',0):>5d}")

    print("-" * 78)
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    net = sum(t["pnl"] for t in trades)
    print(f"  ALL  {n:>4d}  {wins/n*100:>5.1f}  ${net:>+9,d}  ${net//n:>+8,d}")


def main():
    args = sys.argv[1:]
    mode_filter = "M4"
    if "--mode" in args:
        i = args.index("--mode")
        mode_filter = args[i + 1]
        args = args[:i] + args[i + 2:]

    if not args:
        print(__doc__)
        return

    all_trades = []
    for path in args:
        if not Path(path).exists():
            print(f"missing: {path}")
            continue
        trades = parse_log(path, mode_filter=mode_filter)
        print(f"{path}: {len(trades)} {mode_filter} trades (live, post-burst)")
        all_trades.extend(trades)

    summarize(all_trades, label=mode_filter)

    print("\nDecision rule per [[feedback_cross_contract_ab]]: need ~30+ trades "
          "per Sc bucket before raising floor; require at least 2 contracts represented.")


if __name__ == "__main__":
    main()
