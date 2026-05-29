#!/usr/bin/env python3
"""Step B — within-contract walk-forward validation.

Two cuts per contract:
  1. 60/40 chronological split — train on first 60% (oldest), test on last 40%.
  2. Expanding-window monthly walk-forward — for each month M:
       train on all candidates up to end of month M-1, test on month M.
     Aggregate the test-fold predictions.

If gate lift survives the 60/40 split AND the per-month aggregate, the cross-
contract LOO result is robust (not time-leakage). If it collapses (AUC <= 0.55
or gate Net delta <= 0), the prior result was time-leaked and the signal is
weaker than it looked.
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
FEATURES = [
    "Score","CtrlScore","DivStr","Delta","BarSpeed","FadeEdge","FadeType",
    "RiskMult","TrendReg","VolReg","ChopReg","ATR","VwapDist","HHMM",
    "Mode_M1","Mode_M2","Mode_M3","Mode_M4","Mode_M6","Mode_M8","Side_LONG",
]

def featurize(df: pd.DataFrame) -> pd.DataFrame:
    X = df[["Score","CtrlScore","DivStr","Delta","BarSpeed","FadeEdge","FadeType",
            "RiskMult","TrendReg","VolReg","ChopReg","ATR","VwapDist","HHMM"]].copy()
    for m in ["M1","M2","M3","M4","M6","M8"]:
        X[f"Mode_{m}"] = (df.Mode == m).astype(int)
    X["Side_LONG"] = (df.Side == "LONG").astype(int)
    return X[FEATURES]

def fit(train_df):
    import xgboost as xgb
    m = xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        min_child_weight=10, reg_lambda=1.0,
        objective="binary:logistic", eval_metric="logloss",
        verbosity=0, random_state=42,
    )
    m.fit(featurize(train_df), train_df.Win.values)
    return m

def gate_table(test_df, probs, base_net, thresholds=(0.40, 0.45, 0.50, 0.55, 0.60)):
    print(f"    {'gate':>10} {'kept':>5} {'WR':>7} {'PF':>7} {'Net':>12} {'vs base':>12}")
    rows = []
    for th in thresholds:
        keep = probs >= th
        if keep.sum() == 0:
            print(f"    {f'p>={th:.2f}':>10} {0:>5} {'-':>7} {'-':>7} {0:>12,.0f} {-base_net:>+12,.0f}")
            continue
        sub  = test_df.iloc[keep]
        wins = sub.PnL[sub.PnL > 0].sum()
        loss = -sub.PnL[sub.PnL <= 0].sum()
        pf   = wins / loss if loss > 0 else float("inf")
        net  = sub.PnL.sum(); wr = (sub.PnL > 0).mean()
        delta = net - base_net
        print(f"    {f'p>={th:.2f}':>10} {int(keep.sum()):>5} {wr:>6.1%} {pf:>7.2f} {net:>+12,.0f} {delta:>+12,.0f}")
        rows.append((th, int(keep.sum()), wr, pf, net, delta))
    return rows

def main():
    from sklearn.metrics import roc_auc_score

    data = {}
    for c, path in CONTRACTS.items():
        if not os.path.exists(path):
            print(f"missing {c}"); continue
        df = pd.read_csv(path)
        df["dt"] = pd.to_datetime(df.Date + " " + df.Time)
        df = df.sort_values("dt").reset_index(drop=True)
        df["ym"] = df.dt.dt.to_period("M")
        data[c] = df
        print(f"loaded {c:7s} n={len(df):>4} months={df.ym.nunique()} "
              f"({df.dt.min().date()} -> {df.dt.max().date()})")

    print("\n" + "="*72)
    print(" CUT 1: 60/40 chronological split per contract")
    print("="*72)
    cut1 = {}
    for c, df in data.items():
        n = len(df); cut = int(n * 0.6)
        train_df = df.iloc[:cut]; test_df = df.iloc[cut:].reset_index(drop=True)
        if len(train_df) < 30 or len(test_df) < 15:
            print(f"\n  Skip {c}: train={len(train_df)} test={len(test_df)} too small"); continue
        model = fit(train_df)
        probs = model.predict_proba(featurize(test_df))[:, 1]
        y = test_df.Win.values
        auc = roc_auc_score(y, probs) if len(set(y)) > 1 else float("nan")
        base = test_df.PnL.sum()
        train_span = f"{train_df.dt.min().date()} -> {train_df.dt.max().date()}"
        test_span  = f"{test_df.dt.min().date()} -> {test_df.dt.max().date()}"
        print(f"\n  ── {c}  AUC={auc:.3f}  train_n={len(train_df)} test_n={len(test_df)} ──")
        print(f"  train: {train_span}   test: {test_span}")
        print(f"  baseline (all test candidates kept):  WR={y.mean():.1%}  Net=${base:+,.0f}")
        rows = gate_table(test_df, probs, base)
        # Best gate >= 20% retention
        ok = [r for r in rows if r[5] > 0 and r[1] >= max(10, int(0.2 * len(test_df)))]
        best = max(ok, key=lambda r: r[5]) if ok else None
        cut1[c] = dict(auc=auc, best=best, base=base, n_test=len(test_df))

    print("\n" + "="*72)
    print(" CUT 2: expanding-window monthly walk-forward (per contract)")
    print("="*72)
    cut2 = {}
    for c, df in data.items():
        months = sorted(df.ym.unique())
        if len(months) < 3:
            print(f"\n  Skip {c}: only {len(months)} months"); continue
        agg_p = []; agg_y = []; agg_pnl = []
        print(f"\n  ── {c}  months={[str(m) for m in months]} ──")
        for k, m in enumerate(months):
            if k == 0: continue   # need prior data
            train_df = df[df.ym < m]
            test_df  = df[df.ym == m].reset_index(drop=True)
            if len(train_df) < 20 or len(test_df) < 3: continue
            model = fit(train_df)
            probs = model.predict_proba(featurize(test_df))[:, 1]
            agg_p.append(probs); agg_y.append(test_df.Win.values); agg_pnl.append(test_df.PnL.values)
            print(f"    train<{m} n={len(train_df):>3}  test={m} n={len(test_df):>3}  "
                  f"WR_train={train_df.Win.mean():.2f} WR_test={test_df.Win.mean():.2f}")
        if not agg_p:
            print(f"  No usable folds"); continue
        probs = np.concatenate(agg_p); y = np.concatenate(agg_y); pnl = np.concatenate(agg_pnl)
        auc = roc_auc_score(y, probs) if len(set(y)) > 1 else float("nan")
        base = pnl.sum()
        print(f"\n  Aggregate held-out: n={len(y)}  AUC={auc:.3f}  WR={y.mean():.1%}  base_net=${base:+,.0f}")
        # Build a fake test_df-like for gate_table
        test_agg = pd.DataFrame({"Win": y, "PnL": pnl})
        rows = gate_table(test_agg, probs, base)
        ok = [r for r in rows if r[5] > 0 and r[1] >= max(10, int(0.2 * len(y)))]
        best = max(ok, key=lambda r: r[5]) if ok else None
        cut2[c] = dict(auc=auc, best=best, base=base, n_test=len(y))

    print("\n" + "="*72)
    print(" STEP B VERDICT")
    print("="*72)
    print("\n  60/40 chronological split:")
    for c, r in cut1.items():
        b = r["best"]
        flag = f"+${b[5]:+,.0f} @ p>={b[0]:.2f}" if b else "NO LIFT"
        print(f"    {c:8s} AUC={r['auc']:.3f}  n_test={r['n_test']:>3}  {flag}")
    print("\n  Expanding-window monthly walk-forward:")
    for c, r in cut2.items():
        b = r["best"]
        flag = f"+${b[5]:+,.0f} @ p>={b[0]:.2f}" if b else "NO LIFT"
        print(f"    {c:8s} AUC={r['auc']:.3f}  n_test={r['n_test']:>3}  {flag}")

    # Decision
    cut1_pass = sum(1 for r in cut1.values() if r["best"] and r["auc"] >= 0.55)
    cut2_pass = sum(1 for r in cut2.values() if r["best"] and r["auc"] >= 0.55)
    print(f"\n  Folds passing (lift + AUC>=0.55):")
    print(f"    60/40 split           : {cut1_pass}/{len(cut1)}")
    print(f"    expanding walk-forward: {cut2_pass}/{len(cut2)}")

    if cut1_pass == len(cut1) and cut2_pass == len(cut2):
        print("\n  STRONG — lift survives BOTH within-contract time splits.")
        print("    Cross-contract LOO was real. Tail-loss filter deployable. MBO still optional.")
    elif cut1_pass >= 1 or cut2_pass >= 1:
        print("\n  PARTIAL — lift survives some folds but not all. Likely some time-leakage in the")
        print("    cross-contract LOO; the underlying signal is real but weaker than it looked.")
        print("    Recommend Path C (hand-rule from feature importance) instead of black-box gate.")
    else:
        print("\n  FAILED — lift collapses on time-based holdout. Cross-contract LOO was time-leaked.")
        print("    Stop. Stick with current rules. MBO definitively not justified.")

if __name__ == "__main__":
    main()
