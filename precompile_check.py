#!/usr/bin/env python3
"""
Pre-compile static check for ACSIL studies.

WHY THIS EXISTS
  This box has no C++ compiler (see the repo notes: the local SierraChart
  install is dormant, the build happens on the VPS). So a .cpp can sit in the
  repo for months, be edited repeatedly, and never once be told it is wrong.
  Both IOF_NQ_VWAPPullback.cpp and Mizan_IOF_NQ.cpp reached "done" that way.

  This does not replace a compiler. It catches the errors that are cheap to
  catch without one, so a VPS build round-trip is not spent on a typo:

    * unbalanced braces / parens / brackets
    * any sc.Member, SCT_/SC_/DRAWING_/MARKER_/... constant or ACSIL type that
      does not exist in C:\\SierraChart\\ACS_Source\\*.h
    * sc.Input[] slots that are DUPLICATED -- a duplicate silently makes two
      inputs the same control. Gaps are legal and only noted.
    * sc.Subgraph[] slots that are duplicated
    * persistent-key enums whose values collide  (compiles fine, corrupts state)
    * C++11+ constructs, which are fine on a modern toolchain but are the first
      suspect if Sierra's bundled compiler is older than you assume
    * a missing SCDLLName or SCSFExport entry point

USAGE
    python precompile_check.py IOF_NQ_VWAPPullback.cpp
    python precompile_check.py *.cpp

  Exit 0 = nothing found. Exit 1 = something to fix before building.
"""

import collections
import glob
import re
import sys
from pathlib import Path

HEADER_GLOB = r"C:\SierraChart\ACS_Source\*.h"

CONST_RE = re.compile(
    r"\b(?:SCT_[A-Z0-9_]+|SC_[A-Z0-9_]+|MARKER_[A-Z]+|DRAWING_[A-Z]+"
    r"|DRAWSTYLE_[A-Z_]+|MOVAVGTYPE_[A-Z]+|UTAM_[A-Z_]+|BHCS_[A-Z_]+"
    r"|VALUEFORMAT_[A-Z]+|LOW_PREC_LEVEL|FLAG_DT_[A-Z_]+)\b")
TYPE_RE = re.compile(
    r"\b(s_SC[A-Za-z]+|SCString|SCDateTime|SCSubgraphRef|SCInputRef"
    r"|SCStudyInterfaceRef|SCFloatArrayRef|SCSFExport|IsWorkingOrderStatus"
    r"|HMS_TIME)\b")
CXX11 = [("lambda", r"\[[&=][^\]]*\]\s*\("), ("auto", r"\bauto\b"),
         ("nullptr", r"\bnullptr\b"), ("constexpr", r"\bconstexpr\b"),
         ("range-for", r"for\s*\(\s*(?:auto|const)\b[^;]*:\s"),
         ("override", r"\boverride\b")]

_HDR = None


def headers():
    global _HDR
    if _HDR is None:
        _HDR = "".join(
            open(f, encoding="utf-8", errors="ignore").read()
            for f in glob.glob(HEADER_GLOB))
        if not _HDR:
            print(f"  ! no headers found at {HEADER_GLOB} -- symbol check skipped")
    return _HDR


def strip(src):
    """Remove comments and string/char literals so they cannot skew counts."""
    s = re.sub(r"//[^\n]*", "", src)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
    s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)
    return s


def enum_blocks(src):
    """-> list of {name: value} for each enum, resolving auto-increment."""
    out = []
    for body in re.findall(r"enum\s*(?:\w+\s*)?\{(.*?)\}", src, re.S):
        body = re.sub(r"//[^\n]*", "", body)
        vals, nxt = {}, 0
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            m = re.match(r"([A-Za-z_]\w*)\s*(?:=\s*(-?\d+))?$", item)
            if not m:
                continue
            nxt = int(m.group(2)) if m.group(2) is not None else nxt
            vals[m.group(1)] = nxt
            nxt += 1
        if vals:
            out.append(vals)
    return out


def check(path):
    src = open(path, encoding="utf-8").read()
    s = strip(src)
    problems = []
    notes = []

    for o, c in (("{", "}"), ("(", ")"), ("[", "]")):
        if s.count(o) != s.count(c):
            problems.append(f"unbalanced {o}{c}: {s.count(o)} vs {s.count(c)}")
    d = 0
    for n, line in enumerate(s.split("\n"), 1):
        d += line.count("{") - line.count("}")
        if d < 0:
            problems.append(f"brace depth goes negative at line {n}")
            break

    hdr = headers()
    if hdr:
        used = (set(re.findall(r"\bsc\.([A-Za-z_]\w*)", s))
                | set(CONST_RE.findall(s)) | set(TYPE_RE.findall(s)))
        missing = [n for n in sorted(used)
                   if not re.search(r"\b" + re.escape(n) + r"\b", hdr)]
        if missing:
            problems.append(f"not in ACS_Source headers: {missing}")

    for kind, pat in (("Input", r"sc\.Input\[(\d+)\]"),
                      ("Subgraph", r"sc\.Subgraph\[(\d+)\]")):
        slots = [int(x) for x in re.findall(pat, s)]
        dup = [v for v, c in collections.Counter(slots).items() if c > 1]
        if dup:
            problems.append(f"duplicate sc.{kind}[] slots: {sorted(dup)}")
        # NOT an error: unused sc.Input[] slots are legal ACSIL and simply
        # render as unnamed inputs. The live IOF_NQ_Autopilot.cpp has gaps at
        # 17-24 and compiles and trades in production, which is exactly why
        # this is a note -- a checker that flags the shipping study is a
        # checker that gets ignored.
        if kind == "Input" and slots and sorted(set(slots)) != list(range(max(slots) + 1)):
            gaps = sorted(set(range(max(slots) + 1)) - set(slots))
            notes.append(f"unused sc.Input[] slots {gaps} (legal; they show as "
                         f"unnamed inputs)")

    for vals in enum_blocks(s):
        if not any(k.startswith(("PI_", "PF_", "PD_")) for k in vals):
            continue
        dup = [v for v, c in collections.Counter(vals.values()).items() if c > 1]
        for v in dup:
            names = [k for k, vv in vals.items() if vv == v]
            problems.append(f"persistent keys collide at {v}: {names}")

    cxx = [name for name, pat in CXX11 if re.search(pat, s)]

    if not re.search(r"SCDLLName\s*\(", src):
        problems.append("no SCDLLName(...)")
    if not re.search(r"SCSFExport\s+\w+", src):
        problems.append("no SCSFExport entry point")

    name = Path(path).name
    if problems:
        print(f"{name}: {len(problems)} PROBLEM(S)")
        for p in problems:
            print(f"    - {p}")
    else:
        print(f"{name}: OK")
    for n in notes:
        print(f"    note: {n}")
    if cxx:
        print(f"    note: uses C++11+ ({', '.join(cxx)}) -- fine on a modern "
              f"toolchain, first suspect if Sierra's compiler is older")
    return len(problems)


def main():
    args = sys.argv[1:] or sorted(glob.glob("*.cpp"))
    bad = sum(check(a) for a in args)
    print("\nRESULT:", "nothing to fix before building" if bad == 0
          else f"{bad} problem(s) -- fix before spending a VPS build")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
