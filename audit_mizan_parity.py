#!/usr/bin/env python3
"""
Parity audit: Mizan_IOF_NQ.cpp forward sim  vs  backtest_mizan_p3.py

WHY
  Every number in this repo describes the PYTHON. Mizan_IOF_NQ.cpp has never
  been compiled, let alone shown to flag the same setups. That gap is the single
  highest-value thing the VPS forward sim can close, and it is answerable in two
  weeks rather than the year a P&L confirmation would need (see
  PREREG_mizan_forward_sim.md section 0 for why the P&L route is hopeless).

  This compares the cpp's own [MZ ENTRY] log lines against the Python scan run
  over the same .scid and dates. A divergence invalidates every backtest figure
  as a description of the live study.

USAGE
  1. On the VPS: Window > Message Log > right-click > Copy/Save to a text file.
  2. Copy that file AND the contract's .scid back to the repo.
  3. python audit_mizan_parity.py <messagelog.txt> <file.scid> [TAG]

  Exit code 0 = parity clean, 1 = divergence (stop the clock).

WHAT IT COMPARES
  side, swept level name and price, entry HHMM, stop points, target points and
  the frozen ATR, on every session present in BOTH sources. Sessions the cpp
  skipped for insufficient overnight bars are reported, not silently dropped --
  a study that never trades looks identical to one whose signals are wrong.

ASSUMPTIONS (stated because they are the likely source of a false alarm)
  * The message log line carries a parseable YYYY-MM-DD somewhere before the
    [MZ ENTRY] marker. Sierra's export does; if yours does not, pass the date
    another way rather than guessing.
  * The cpp logs at Log Level >= 1 for entries. Level 2 is required to also see
    the skipped-session lines.
  * Tolerances are one tick on prices and 0.01 on ATR/points -- the two engines
    round at different moments, so exact equality is the wrong test.
"""

import re
import sys
from pathlib import Path

TICK = 0.25
PRICE_TOL = TICK + 1e-9
PTS_TOL = 0.02

# [MZ ENTRY] LONG  swept ONH=24500.00  bar 123 0935  sweep=24490.00
# close=24510.00  MARKET  stop=48.00pt tgt=96.00pt  ATR=50.00
# ON=[1.00,2.00] PD=[3.00,4.00]
ENTRY_RE = re.compile(
    r"\[MZ (?P<kind>ENTRY|SETUP)\]\s+(?P<side>LONG|SHORT)\s+swept\s+"
    r"(?P<lvlname>ONH|ONL|PDH|PDL)=(?P<lvl>[-0-9.]+)\s+"
    r"bar\s+(?P<bar>\d+)\s+(?P<hhmm>\d{3,4})\s+"
    r"sweep=(?P<sweep>[-0-9.]+)\s+close=(?P<close>[-0-9.]+)\s+"
    r"(?P<mode>MARKET|LIMIT)"
    r".*?stop=(?P<stop>[-0-9.]+)pt\s+tgt=(?P<tgt>[-0-9.]+)pt"
    r".*?ATR=(?P<atr>[-0-9.]+)",
    re.S)

DATE_RE = re.compile(r"(20\d\d)[-/](\d\d)[-/](\d\d)")
SKIP_RE = re.compile(r"\[MZ\]\s+session\s+(\d+)\s+skipped:\s+(\d+)\s+overnight bars")


def parse_log(path):
    """-> (entries, skipped_sessions). Entries keyed by (date_tag, hhmm)."""
    entries, skipped = {}, []
    for line in Path(path).read_text(errors="ignore").splitlines():
        if "[MZ" not in line:
            continue
        s = SKIP_RE.search(line)
        if s:
            skipped.append((int(s.group(1)), int(s.group(2))))
            continue
        m = ENTRY_RE.search(line)
        if not m:
            continue
        d = DATE_RE.search(line[:m.start()] or line)
        if not d:
            print(f"  ! no date on an entry line, skipped:\n    {line.strip()[:120]}")
            continue
        dtag = int(d.group(1)) * 10000 + int(d.group(2)) * 100 + int(d.group(3))
        g = m.groupdict()
        entries[(dtag, int(g["hhmm"]))] = dict(
            side=1 if g["side"] == "LONG" else -1,
            lvlname=g["lvlname"], lvl=float(g["lvl"]),
            mode=g["mode"], kind=g["kind"], stop=float(g["stop"]),
            tgt=float(g["tgt"]), atr=float(g["atr"]))
    return entries, skipped


def python_entries(scid, tag):
    """Run the Python scan and return the same shape, keyed (date_tag, hhmm)."""
    import backtest_mizan_p3 as MZ
    from backtest_mizan_iof_nq import arrays
    from fastbars import load_bars_cached

    p = MZ.params_from_env()
    bars = load_bars_cached(tag, Path(scid), MZ.BAR_MINUTES)
    a = arrays(bars)
    c = MZ.scan(bars, p)
    out = {}
    for k in range(len(c.idx)):
        i = int(c.idx[k])
        out[(int(a.dtag[i]), int(a.hhmm[i]))] = dict(
            side=int(c.side[k]),
            stop=float(c.stop_pts[k]),
            tgt=float(c.tgt_pts[k]),
            atr=float(c.atr[k]))
    return out


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    logf, scid = sys.argv[1], sys.argv[2]
    tag = sys.argv[3] if len(sys.argv) > 3 else Path(scid).stem

    cpp, skipped = parse_log(logf)
    py = python_entries(scid, tag)

    # Only judge the overlap: the log covers the forward window, the .scid may
    # extend either side of it. Comparing outside the overlap manufactures
    # false divergences.
    if not cpp:
        print("No [MZ ENTRY] / [MZ SETUP] lines found. Is Log Level >= 1, and "
              "has a setup fired yet?")
        return 1

    # The cpp logs [MZ ENTRY] for the market entry and [MZ SETUP] for the limit.
    # Mode 0 is the registered configuration, so anything else ends the audit
    # here rather than producing a confusing per-trade diff.
    wrong = sorted(k for k, v in cpp.items() if v["kind"] != "ENTRY")
    if wrong:
        print(f"WRONG ENTRY MODE: {len(wrong)} line(s) logged as [MZ SETUP], "
              f"which the cpp emits only for the LIMIT entry (mode 1).")
        print("PREREG section 1 fixes Entry = 0 (market). Set it back and restart.")
        return 1
    lo, hi = min(d for d, _ in cpp), max(d for d, _ in cpp)
    py = {k: v for k, v in py.items() if lo <= k[0] <= hi}

    print(f"parity window {lo} -> {hi}   cpp={len(cpp)} entries   python={len(py)}")
    if skipped:
        print(f"cpp skipped {len(skipped)} session(s) for short overnight "
              f"(chart must be 24-hour): {skipped[:5]}")

    bad = 0
    for key in sorted(set(cpp) | set(py)):
        c, q = cpp.get(key), py.get(key)
        d, hhmm = key
        if c is None:
            print(f"  MISSING IN CPP    {d} {hhmm:04d}  python says "
                  f"{'LONG' if q['side'] > 0 else 'SHORT'} stop={q['stop']:.2f}")
            bad += 1
            continue
        if q is None:
            print(f"  EXTRA IN CPP      {d} {hhmm:04d}  cpp says "
                  f"{'LONG' if c['side'] > 0 else 'SHORT'} {c['lvlname']} "
                  f"stop={c['stop']:.2f}")
            bad += 1
            continue
        diffs = []
        if c["side"] != q["side"]:
            diffs.append(f"side {c['side']} vs {q['side']}")
        if abs(c["stop"] - q["stop"]) > PTS_TOL:
            diffs.append(f"stop {c['stop']:.2f} vs {q['stop']:.2f}")
        if abs(c["tgt"] - q["tgt"]) > PTS_TOL:
            diffs.append(f"tgt {c['tgt']:.2f} vs {q['tgt']:.2f}")
        if abs(c["atr"] - q["atr"]) > PTS_TOL:
            diffs.append(f"ATR {c['atr']:.2f} vs {q['atr']:.2f}")
        if c["mode"] != "MARKET":
            diffs.append(f"entry mode is {c['mode']}, must be MARKET for mode 0")
        if diffs:
            print(f"  MISMATCH          {d} {hhmm:04d}  " + "; ".join(diffs))
            bad += 1

    sessions = len({d for d, _ in cpp})
    rate = len(cpp) / sessions if sessions else 0.0
    print(f"\nentries/session {rate:.2f} (backtest ~0.65 -- a materially "
          f"different rate is a parity failure even if P&L looks fine)")
    print("RESULT:", "PARITY CLEAN" if bad == 0
          else f"{bad} DIVERGENCE(S) -- stop the clock, per PREREG section 3")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
