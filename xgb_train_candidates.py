#!/usr/bin/env python3
"""Train XGBoost on the candidate-fire labeled dataset (candidates_*.csv).

Cross-contract leave-one-out: train on N-1 contracts, score the held-out one.
Evaluate gate by stepping the model threshold and reporting kept-trades PF/Net.

Decision rule (Step-1 verdict):
  - lift on EVERY held-out contract at SAME threshold -> MBO worth exploring
  - lift only on subset / unstable threshold           -> hand-rule via feature
                                                           importance (Path B)
  - no lift on any                                     -> ML doesn't help; stop

Usage: python xgb_train_candidates.py
"""
import os, sys
import numpy as np
import pandas as pd

BASE = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final"

CONTRACTS = {
    "NQZ25":  os.path.join(BASE, "candidates_NQZ25.csv"),
    "NQH6":   os.path.join(BASE, "candidates_NQH6.csv"),
    "ENQM26": os.path.join(BASE, "candidates_ENQM26.csv"),
}

FEATURES = [
    "Score", "CtrlScore", "DivStr", "Delta", "BarSpeed", "FadeEdge", "FadeType",
    "RiskMult", "TrendReg", "VolReg", "ChopReg", "ATR", "VwapDist", "HHMM",
    "Mode_M1","Mode_M2","Mode_M3","Mode_M4","Mode_M6","Mode_M8",
    "Side_LONG",
]

def featurize(df: pd.DataFrame) -> pd.DataFrame:
    X = df[["Score","CtrlScore","DivStr","Delta","BarSpeed","FadeEdge","FadeType",
            "RiskMult","TrendReg","VolReg","ChopReg","ATR","VwapDist","HHMM"]].copy()
    for m in ["M1","M2","M3","M4","M6","M8"]:
        X[f"Mode_{m}"] = (df.Mode == m).astype(int)
    X["Side_LONG"] = (df.Side == "LONG").astype(int)
    return X[FEATURES]

def baseline(df: pd.DataFrame, label="baseline"):
    n = len(df); w = int(df.Win.sum())
    wins = df.PnL[df.PnL > 0].sum(); loss = -df.PnL[df.PnL <= 0].sum()
    pf = wins / loss if loss > 0 else float("inf")
    print(f"  {label:36s} n={n:>5} WR={w/n:6.1%} PF={pf:6.2f} Net=${df.PnL.sum():>+12,.0f}")
    return df.PnL.sum()

def gate_table(df: pd.DataFrame, probs: np.ndarray, base_net: float,
               thresholds=(0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70)):
    print(f"    {'gate':>10} {'kept':>5} {'WR':>7} {'PF':>7} {'Net':>14} {'vs base':>14}")
    rows = []
    for th in thresholds:
        keep = probs >= th
        if keep.sum() == 0:
            print(f"    {f'p>={th:.2f}':>10} {0:>5} {'-':>7} {'-':>7} {0:>14,.0f} {-base_net:>+14,.0f}")
            continue
        sub  = df.iloc[keep]
        wins = sub.PnL[sub.PnL > 0].sum()
        loss = -sub.PnL[sub.PnL <= 0].sum()
        pf   = wins / loss if loss > 0 else float("inf")
        net  = sub.PnL.sum()
        wr   = (sub.PnL > 0).mean()
        delta = net - base_net
        print(f"    {f'p>={th:.2f}':>10} {int(keep.sum()):>5} {wr:>6.1%} {pf:>7.2f} {net:>+14,.0f} {delta:>+14,.0f}")
        rows.append((th, int(keep.sum()), wr, pf, net, delta))
    return rows

def train_eval(train_df, test_df, label=""):
    import xgboost as xgb
    from sklearn.metrics import roc_auc_score
    Xtr, ytr = featurize(train_df), train_df.Win.values
    Xte, yte = featurize(test_df),  test_df.Win.values
    model = xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        min_child_weight=10, reg_lambda=1.0,
        objective="binary:logistic", eval_metric="logloss",
        verbosity=0, random_state=42,
    )
    model.fit(Xtr, ytr)
    p = model.predict_proba(Xte)[:, 1]
    auc = roc_auc_score(yte, p) if len(set(yte)) > 1 else float("nan")
    print(f"\n  [{label}]  AUC={auc:.3f}  train_n={len(train_df)}  test_n={len(test_df)}")
    return model, p

def feature_importance(model, features):
    imp = model.feature_importances_
    rank = sorted(zip(features, imp), key=lambda x: -x[1])
    print("\n  Top features (importance):")
    for f, v in rank[:12]:
        bar = "*" * int(v * 80)
        print(f"    {f:14s} {v:.4f} {bar}")

def main():
    data = {}
    for name, path in CONTRACTS.items():
        if not os.path.exists(path):
            print(f"missing  {name:7s}  ({path})"); continue
        df = pd.read_csv(path)
        data[name] = df
        print(f"loaded {name:7s}  n={len(df):>5}  net=${df.PnL.sum():>+11,.0f}")

    if len(data) < 2:
        print("\nNeed >=2 contracts. Stopping."); return

    print("\n=== Per-contract baseline (all candidates, no gate) ===")
    base_net = {}
    for c, df in data.items():
        base_net[c] = baseline(df, c)

    print("\n=== Cross-contract leave-one-out ===")
    keys = list(data.keys())
    summary = {}
    for hold in keys:
        train_df = pd.concat([data[k] for k in keys if k != hold], ignore_index=True)
        test_df  = data[hold].reset_index(drop=True)
        if len(train_df) < 30 or len(test_df) < 20:
            print(f"\n  Skip hold={hold}: train={len(train_df)} test={len(test_df)} too small")
            continue
        baseline(test_df, f"hold={hold} BASELINE (no gate)")
        model, probs = train_eval(train_df, test_df, label=f"hold={hold}")
        rows = gate_table(test_df, probs, base_net[hold])
        feature_importance(model, FEATURES)
        # Pick best gate by net P&L improvement vs baseline (positive delta only)
        improves = [r for r in rows if r[5] > 0 and r[1] >= max(20, int(0.2 * len(test_df)))]
        if improves:
            best = max(improves, key=lambda r: r[5])
            summary[hold] = best
            print(f"\n  >>> Best gate hold={hold}: p>={best[0]:.2f} keeps {best[1]}/{len(test_df)} "
                  f"WR={best[2]:.1%} PF={best[3]:.2f} Net=${best[4]:+,.0f} (+${best[5]:+,.0f})")
        else:
            summary[hold] = None
            print(f"\n  >>> hold={hold}: NO gate improves vs baseline at acceptable retention")

    print("\n" + "="*70)
    print("STEP-1 VERDICT")
    print("="*70)
    lifts = [k for k, v in summary.items() if v is not None]
    if len(lifts) == len(summary) and len(summary) >= 2:
        ths = [summary[k][0] for k in lifts]
        if max(ths) - min(ths) <= 0.10:
            print(f"  LIFT ON ALL {len(lifts)} held-out contracts at consistent threshold")
            print(f"    -> MBO worth exploring (Step 2)")
        else:
            print(f"  LIFT on all contracts but at INCONSISTENT thresholds ({ths})")
            print(f"    -> read feature importance, hand-code top split as a rule (Path B)")
    elif lifts:
        print(f"  LIFT on {len(lifts)}/{len(summary)} contracts: {lifts}")
        print(f"    -> partial signal; treat as feature-importance only (Path B)")
    else:
        print(f"  NO lift on any held-out contract")
        print(f"    -> ML doesn't help here; MBO would not change this. STOP.")

if __name__ == "__main__":
    main()
