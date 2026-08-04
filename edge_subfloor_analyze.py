#!/usr/bin/env python3
"""Slice the sub-floor candidate band (edge_subfloor_scan.py output) by pre-trade
features and test for a subpopulation that wins ROBUSTLY across contracts.

Rules of the search (to avoid the overfit that has killed every prior lever):
  * Slice only on features known BEFORE entry: time-of-day, regime, VWAP
    extension, delta alignment, quality level. Never on Outcome/Net/MAE/MFE.
  * A slice is a real candidate only if it is positive POOLED *and* positive on
    >= 4 of the 6 contracts individually (each with enough setups to matter).
  * The bar to beat is the "clear" band's net/setup — a new admit path should be
    at least comparably positive, not merely less-negative than the sub-floor avg.
"""
import csv, os
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
CSV  = os.path.join(BASE, "edge_subfloor_candidates.csv")

rows = []
with open(CSV, newline="") as f:
    for r in csv.DictReader(f):
        for k in ("HHMM", "Q", "Floor", "Score", "Ctrl", "Div", "Delta",
                  "Aligned", "TrendReg", "VolReg", "ChopReg", "Win"):
            r[k] = int(float(r[k]))
        for k in ("ATR", "VwapDist", "VwapAtr", "Gross", "Net"):
            r[k] = float(r[k])
        rows.append(r)

CONTRACTS = sorted({r["Contract"] for r in rows})
N_CON = len(CONTRACTS)


def tod(h):
    if h < 935:  return "PRE"
    if h < 1030: return "OPEN"
    if h < 1200: return "MID"
    if h < 1400: return "DEAD"
    if h < 1545: return "AFT"
    return "LATE"


def vwb(x):
    a = abs(x)
    return "NEAR" if a < 0.5 else "MID" if a < 1.0 else "FAR"


def stat(subset):
    n = len(subset)
    if n == 0:
        return 0, 0.0, 0.0, 0.0
    net = sum(r["Net"] for r in subset)
    wr = 100 * sum(1 for r in subset if r["Net"] > 0) / n
    return n, wr, net, net / n


def per_contract(subset, min_n=8):
    """Return (#contracts positive, #contracts evaluable, detail dict)."""
    byc = defaultdict(list)
    for r in subset:
        byc[r["Contract"]].append(r["Net"])
    pos = ev = 0
    detail = {}
    for c in CONTRACTS:
        v = byc.get(c, [])
        if len(v) >= min_n:
            ev += 1
            npx = sum(v) / len(v)
            detail[c] = (len(v), npx)
            if npx > 0:
                pos += 1
        else:
            detail[c] = (len(v), None)
    return pos, ev, detail


# ── Overall bands ────────────────────────────────────────────────────────────
print("=" * 74)
print("  BAND BASELINE (pooled, net of $5 RT)")
print("=" * 74)
for band in ("clear", "sub"):
    ss = [r for r in rows if r["Band"] == band]
    n, wr, net, npx = stat(ss)
    print(f"  {band:<6} n={n:>5}  WR={wr:5.1f}%  net=${net:>+10,.0f}  net/setup=${npx:>+7.1f}")

clear_npx = stat([r for r in rows if r["Band"] == "clear"])[3]
sub = [r for r in rows if r["Band"] == "sub"]

# ── Slice definitions (pre-trade features only) ──────────────────────────────
slices = [
    ("mode",         lambda r: r["Mode"]),
    ("time-of-day",  lambda r: tod(r["HHMM"])),
    ("vol-regime",   lambda r: f"V{r['VolReg']}"),
    ("trend-regime", lambda r: f"T{r['TrendReg']}"),
    ("chop-regime",  lambda r: f"C{r['ChopReg']}"),
    ("vwap-extension", lambda r: vwb(r["VwapAtr"])),
    ("delta-aligned", lambda r: "align" if r["Aligned"] else "against"),
    ("q-level",      lambda r: f"q{r['Q']}"),
    ("mode x tod",   lambda r: f"{r['Mode']}:{tod(r['HHMM'])}"),
    ("mode x vol",   lambda r: f"{r['Mode']}:V{r['VolReg']}"),
    ("mode x vwap",  lambda r: f"{r['Mode']}:{vwb(r['VwapAtr'])}"),
    ("mode x aligned", lambda r: f"{r['Mode']}:{'al' if r['Aligned'] else 'ag'}"),
]

print("\n" + "=" * 74)
print(f"  SUB-FLOOR SLICES  (bar to beat = clear band net/setup ${clear_npx:+.1f})")
print(f"  robust = positive pooled AND positive on >=4 of {N_CON} contracts (min 8/contract)")
print("=" * 74)

candidates = []
for sname, keyfn in slices:
    groups = defaultdict(list)
    for r in sub:
        groups[keyfn(r)].append(r)
    printed = False
    for g in sorted(groups, key=lambda k: -stat(groups[k])[3]):
        ss = groups[g]
        n, wr, net, npx = stat(ss)
        if n < 30:
            continue
        pos, ev, detail = per_contract(ss)
        robust = npx > 0 and pos >= 4 and ev >= 4
        if not printed:
            print(f"\n  [{sname}]")
            printed = True
        flag = "  <== ROBUST+" if robust else ("  (pooled+ only)" if npx > 0 else "")
        print(f"    {g:<14} n={n:>4} WR={wr:4.1f}% net/setup=${npx:>+7.1f}  "
              f"contracts+={pos}/{ev}{flag}")
        if robust:
            candidates.append((sname, g, n, npx, pos, ev, detail))

# ── Verdict ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 74)
print("  VERDICT")
print("=" * 74)
if not candidates:
    print("  No sub-floor slice is positive pooled AND on >=4/6 contracts.")
    print("  -> No parallel-admit edge in the sub-floor band. A new edge must be a")
    print("     new TRIGGER GEOMETRY, not a rescue of floor-rejected setups.")
else:
    print(f"  {len(candidates)} robust sub-floor slice(s) found — per-contract detail:")
    for sname, g, n, npx, pos, ev, detail in sorted(candidates, key=lambda c: -c[3]):
        print(f"\n  [{sname}] {g}: pooled net/setup ${npx:+.1f} over n={n}")
        for c in CONTRACTS:
            cn, cnpx = detail[c]
            s = f"${cnpx:+7.1f}" if cnpx is not None else "  (thin)"
            print(f"      {c:<8} n={cn:>3}  {s}")
    print("\n  -> Promising. NEXT: wire the slice as a parallel-admit gate (keep floor")
    print("     50) and run the full cross-contract A/B at MT=1 to check it survives")
    print("     slot-displacement, the exact test that killed M2 f46.")
