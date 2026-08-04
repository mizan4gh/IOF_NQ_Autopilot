#!/usr/bin/env python3
"""Sub-floor edge scan — is there a NEW admission path that adds setups without
lowering the quality floor?

For every armed (mode, side) across the 6 frozen contracts, forward-simulate the
production stop/T1/T2 outcome (reusing xgb_candidate_label's conservative sim) and
record the qual100 value + pre-trade features. Rows where q < floor are the
"sub-floor" band — the setups the floor currently rejects. The question: does any
subpopulation of that band, identified by an ORTHOGONAL pre-trade feature (never
the quality score, never an outcome-derived value), win robustly across contracts?
If yes -> candidate parallel-admit path (keep floor 50, add a narrow q<50 gate).
If no  -> sub-floor rescue is dead; a new edge must be a new trigger geometry.

Outputs pooled edge_subfloor_candidates.csv + a per-mode band baseline.
Analysis/slicing is done separately (edge_subfloor_analyze.py) so slices can be
re-cut without re-running the backtests.
"""
import os, sys, csv
import backtest as bt
import xgb_candidate_label as xcl   # reuse simulate / stop_target / final_sc

# Live-panel config (only affects the floor we tag against; gates don't touch
# the _on_any_arm capture, which fires before any gate).
bt.QUAL_FLOOR    = 50
bt.QUAL_FLOOR_M8 = 60

CONTRACTS = ["F.US.ENQZ25", "F.US.ENQM25", "F.US.ENQU25",
             "F.US.ENQH26", "F.US.ENQM26", "F.US.ENQU26"]

rows = []          # pooled candidate rows across all contracts
_cur = {"c": ""}   # current contract stem (set before each run)


def floor_for(mi: int) -> int:
    ov = getattr(bt, f"QUAL_FLOOR_M{mi + 1}", None)
    return ov if ov is not None else bt.QUAL_FLOOR


def hook(self, i: int, ctx: dict):
    bar = ctx["bar"]; atr = ctx["atr"]
    if atr <= 0:
        return
    arm_table = [
        (0, ctx["m1l"], True), (0, ctx["m1s"], False),
        (1, ctx["m2l"], True), (1, ctx["m2s"], False),
        (2, ctx["m3l"], True), (2, ctx["m3s"], False),
        (3, ctx["m4l"], True), (3, ctx["m4s"], False),
        (5, ctx["m6l"], True), (5, ctx["m6s"], False),
        (7, ctx["m8l"], True), (7, ctx["m8s"], False),
    ]
    ep = bar.close
    tr, cr = self._regime(i, atr, ctx["vwap"])
    vr     = self._vol_reg(i, atr)
    vwd    = (bar.close - ctx["vwap"]) if ctx["vwap"] > 0 else 0.0
    delta  = float(bar.ask_vol - bar.bid_vol)
    for mi, armed, sl in arm_table:
        if not armed:
            continue
        score = xcl.final_sc(mi, sl, ctx["sc_l"], ctx["sc_s"], ctx["ctrl"], ctx["bk_vs"])
        q     = bt.qual100(mi, score, ctx["fade_edge"], ctx["fade_active"])
        floor = floor_for(mi)
        sp, t1, t2 = xcl.stop_target(mi, sl, ep, atr, ctx, ctx["sc_l"], ctx["sc_s"])
        outcome, ex_px, ex_bar, mae, mfe = xcl.simulate(self.bars, i, sl, ep, sp, t1)
        pts   = (ex_px - ep) if sl else (ep - ex_px)
        gross = pts * bt.PT_VAL
        net   = gross - bt.COMMISSION            # RT commission; entry-at-close is optimistic on slip
        aligned = int((sl and delta > 0) or ((not sl) and delta < 0))
        rows.append(dict(
            Contract = _cur["c"], Date = bar.dt.strftime("%Y-%m-%d"),
            HHMM = bar.hhmm, Mode = bt.MODE_NAMES[mi], Side = "L" if sl else "S",
            Q = q, Floor = floor, Band = "sub" if q < floor else "clear",
            Score = score, Ctrl = ctx["ctrl"], Div = ctx["div_strength"],
            Delta = round(delta), Aligned = aligned,
            TrendReg = tr, VolReg = vr, ChopReg = cr,
            ATR = round(atr, 2), VwapDist = round(vwd, 2),
            VwapAtr = round(vwd / atr, 3) if atr > 0 else 0.0,
            Outcome = outcome, Gross = round(gross, 2), Net = round(net, 2),
            Win = int(net > 0),
        ))


def main():
    bt.Backtester._on_any_arm = hook
    for c in CONTRACTS:
        scid = os.path.join(bt.BASE_DIR, c + ".scid")
        if not os.path.exists(scid):
            print(f"  !! missing {scid} — skipping"); continue
        _cur["c"] = c.replace("F.US.E", "")
        sys.argv = ["backtest.py", scid, os.path.join(bt.BASE_DIR, "tmp_edgescan.csv")]
        print(f"\n===== scanning {_cur['c']} =====")
        bt.main()

    if not rows:
        print("No candidates captured."); return

    out = os.path.join(bt.BASE_DIR, "edge_subfloor_candidates.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nPooled candidates written: {out}  (n={len(rows)})")

    # Quick baseline: per mode x band, pooled net expectancy.
    print("\n" + "=" * 66)
    print("  BASELINE  per mode x band  (pooled, net of $5 RT commission)")
    print("=" * 66)
    print(f"  {'Mode':<5}{'Band':<6}{'n':>6}{'WR%':>7}{'NetTot$':>12}{'Net/setup$':>12}")
    agg = {}
    for r in rows:
        agg.setdefault((r["Mode"], r["Band"]), []).append(r["Net"])
    for k in sorted(agg):
        v = agg[k]
        wr = 100 * sum(1 for x in v if x > 0) / len(v)
        print(f"  {k[0]:<5}{k[1]:<6}{len(v):>6}{wr:>7.1f}{sum(v):>12,.0f}{sum(v)/len(v):>12,.1f}")


if __name__ == "__main__":
    main()
