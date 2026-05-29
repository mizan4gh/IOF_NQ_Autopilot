#!/usr/bin/env python3
"""Option 3 — test specific hand-rules suggested by L1 LogReg's NQH6 coefs.

L1 (StandardScaled) coefs on NQH6: ATR +0.401, HHMM +0.180, ChopReg +0.165,
CtrlScore +0.075, Score +0.014. All positive → higher value = higher win prob.

For each candidate threshold of each feature, run an expanding-month
walk-forward across all three contracts. A rule deploys ONLY if it produces
positive net P&L delta on ALL 3 contracts at the same threshold value.

Tests:
  R1: skip if ATR < T                 (T ∈ percentile sweep)
  R2: skip if HHMM < T                (no-early-trades rule)
  R3: skip if ChopReg < T             (T ∈ {1, 2})
  R4: skip if (ATR < TA) OR (ChopReg < TC)   (combined)

Reports per-contract lift and the count of contracts where each rule helps.
"""
import os
import numpy as np
import pandas as pd

BASE = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final"
CONTRACTS = {
    "NQZ25":  os.path.join(BASE, "candidates_NQZ25.csv"),
    "NQH6":   os.path.join(BASE, "candidates_NQH6.csv"),
    "ENQM26": os.path.join(BASE, "candidates_ENQM26.csv"),
}

def load(c, path):
    df = pd.read_csv(path)
    df["dt"] = pd.to_datetime(df.Date + " " + df.Time)
    df = df.sort_values("dt").reset_index(drop=True)
    df["ym"] = df.dt.dt.to_period("M")
    return df

def wf_apply_rule(df, gate_fn):
    """Expanding-month walk-forward: gate_fn(row) -> True=keep.
    Returns (kept_pnl, kept_n, base_pnl, base_n) aggregated across all test months."""
    months = sorted(df.ym.unique())
    if len(months) < 2:
        return None
    kept_pnl = []; base_pnl = []; n_kept = 0; n_base = 0
    for k, m in enumerate(months):
        if k == 0: continue
        test_df = df[df.ym == m]
        if len(test_df) < 3: continue
        mask = test_df.apply(gate_fn, axis=1)
        n_kept += int(mask.sum()); n_base += len(test_df)
        kept_pnl.extend(test_df.PnL[mask].values)
        base_pnl.extend(test_df.PnL.values)
    if n_base == 0: return None
    return (sum(kept_pnl), n_kept, sum(base_pnl), n_base)

def sweep(data, feature, candidates, label_prefix, gate_factory):
    """Sweep thresholds; print per-contract lift; identify all-pass."""
    print(f"\n{'='*84}")
    print(f"  {label_prefix}")
    print(f"{'='*84}")
    header = f"  {'T':>8} | " + " | ".join(f"{c:>22}" for c in data) + f" | {'cross-cnt':>10}"
    print(header)
    print(f"  {'-'*8} | " + " | ".join("-"*22 for _ in data) + f" | {'-'*10}")
    all_pass = []
    for T in candidates:
        gate = gate_factory(T)
        deltas = {}; passing = 0
        for c, df in data.items():
            r = wf_apply_rule(df, gate)
            if r is None: deltas[c] = None; continue
            kp, kn, bp, bn = r
            delta = kp - bp
            deltas[c] = (delta, kn, bn)
            if delta > 0: passing += 1
        row = f"  {T!r:>8} | "
        for c in data:
            d = deltas[c]
            if d is None:
                row += f"{'(no folds)':>22} | "
            else:
                delta, kn, bn = d
                row += f"{f'${delta:+,.0f} ({kn}/{bn})':>22} | "
        row += f"{passing:>10}"
        print(row)
        if passing == len(data):
            all_pass.append((T, deltas))
    if all_pass:
        print(f"\n  ★ ALL-PASS thresholds for {label_prefix.split(':')[0]}:")
        for T, deltas in all_pass:
            total = sum(d[0] for d in deltas.values() if d)
            print(f"      T={T!r}  total_lift=${total:+,.0f}")
    else:
        print(f"\n  ✗ No threshold produces positive lift on all {len(data)} contracts.")
    return all_pass

def main():
    data = {c: load(c, p) for c, p in CONTRACTS.items() if os.path.exists(p)}
    print(f"loaded contracts: {list(data)}")
    for c, df in data.items():
        print(f"  {c:8s} n={len(df):>4}  ATR mean={df.ATR.mean():5.1f} sd={df.ATR.std():5.1f}  "
              f"HHMM med={int(df.HHMM.median())}  ChopReg counts={df.ChopReg.value_counts().to_dict()}")

    # ATR thresholds: distribution-based + a few absolute values
    atr_thresholds = [10, 15, 20, 25, 30, 35, 40, 45, 50, 60]
    sweep(data, "ATR", atr_thresholds,
          "R1: skip if ATR < T  (low-vol filter)",
          lambda T: lambda r: r.ATR >= T)

    # HHMM thresholds: skip-before-this-time
    hhmm_thresholds = [935, 1000, 1030, 1100, 1130, 1200, 1300, 1400]
    sweep(data, "HHMM", hhmm_thresholds,
          "R2: skip if HHMM < T  (no-early-trades, no-late-trades)",
          lambda T: lambda r: r.HHMM >= T)

    # HHMM upper threshold: skip-after-time (since L1 said higher HHMM = better,
    # the rule "skip BEFORE T" makes sense; complement worth checking)
    sweep(data, "HHMM_upper", [1300, 1400, 1500, 1530],
          "R2b: skip if HHMM > T  (no-late-trades)",
          lambda T: lambda r: r.HHMM <= T)

    # ChopReg: 3-level categorical (0,1,2)
    sweep(data, "ChopReg", [1, 2],
          "R3: skip if ChopReg < T",
          lambda T: lambda r: r.ChopReg >= T)

    # Combined: ATR low OR ChopReg low
    print("\n" + "="*84)
    print("  R4: COMBINED — skip if ATR < TA  OR  ChopReg < TC")
    print("="*84)
    best = None
    for TA in [15, 20, 25, 30]:
        for TC in [1, 2]:
            gate = lambda TA_=TA, TC_=TC: (lambda r: r.ATR >= TA_ and r.ChopReg >= TC_)()
            # need fresh closure per-iter
            def make_gate(ta, tc):
                return lambda r: (r.ATR >= ta) and (r.ChopReg >= tc)
            g = make_gate(TA, TC)
            deltas = {}
            passing = 0
            for c, df in data.items():
                r = wf_apply_rule(df, g)
                if r is None: deltas[c] = None; continue
                kp, kn, bp, bn = r
                delta = kp - bp
                deltas[c] = (delta, kn, bn)
                if delta > 0: passing += 1
            total = sum(d[0] for d in deltas.values() if d)
            mark = "★" if passing == len(data) else " "
            print(f"  {mark} ATR>={TA:>3} & ChopReg>={TC}  passing={passing}/{len(data)}  total_lift=${total:+,.0f}")
            if passing == len(data) and (best is None or total > best[1]):
                best = ((TA, TC), total, deltas)
    if best:
        (TA, TC), total, deltas = best
        print(f"\n  ★ BEST COMBINED: ATR>={TA} AND ChopReg>={TC}, total ${total:+,.0f}")
        for c, d in deltas.items():
            if d: print(f"      {c:8s}: lift ${d[0]:+,.0f}  kept {d[1]}/{d[2]}")
    else:
        print(f"\n  ✗ No combined ATR+ChopReg threshold passes all contracts.")

if __name__ == "__main__":
    main()
