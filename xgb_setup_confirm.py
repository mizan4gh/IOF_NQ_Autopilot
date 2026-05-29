#!/usr/bin/env python3
"""
XGBoost SETUP-confirm experiment — Step 1 of the "is MBO worth buying?" decision.

Reads backtest CSVs (multiple contracts), pairs SETUP rows with their EXIT rows,
labels each trade win/loss, trains XGBoost on existing features, and measures
lift vs the current QUAL_FLOOR baseline on a held-out contract.

Inputs: xgb_<contract>.csv produced by backtest.py
Outputs: console report + xgb_results.txt
"""
import os, sys, csv, json
from collections import defaultdict
import numpy as np
import pandas as pd

BASE = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final"

# Features available in CSV at SETUP time (BEFORE outcome is known)
FEATURES = [
    "Score", "CtrlScore", "DivStr", "Delta", "BarSpeed",
    "FadeEdge", "FadeType", "RiskMult", "TrendReg", "VolReg", "ChopReg",
    "Mode_M1", "Mode_M2", "Mode_M4", "Mode_M6", "Mode_M8",
    "Side_LONG",
]

def load_pairs(csv_path: str) -> pd.DataFrame:
    """Load a backtest CSV and pair SETUP rows with their EXIT rows."""
    df = pd.read_csv(csv_path)
    setups = df[df.Event == "SETUP"].reset_index(drop=True)
    exits  = df[df.Event == "EXIT"].reset_index(drop=True)
    if len(setups) != len(exits):
        print(f"  WARN: {len(setups)} setups != {len(exits)} exits in {os.path.basename(csv_path)}")
    n = min(len(setups), len(exits))
    s = setups.iloc[:n].copy()
    e = exits.iloc[:n].copy()
    # Per-trade PnL = exits.TotalPnL - prev exits.TotalPnL (TotalPnL is cumulative)
    pnl_cum = e.TotalPnL.values
    pnl = np.diff(np.concatenate([[0.0], pnl_cum]))
    s["pnl"]      = pnl
    s["win"]      = (pnl > 0).astype(int)
    s["exit_rsn"] = e.ExitReason.values
    s["hold"]     = e.HoldBars.values
    return s

def to_features(df: pd.DataFrame) -> pd.DataFrame:
    """One-hot Mode/Side, return feature matrix."""
    X = df[["Score","CtrlScore","DivStr","Delta","BarSpeed",
            "FadeEdge","FadeType","RiskMult","TrendReg","VolReg","ChopReg"]].copy()
    for m in ["M1","M2","M4","M6","M8"]:
        X[f"Mode_{m}"] = (df.Mode == m).astype(int)
    X["Side_LONG"] = (df.Side == "LONG").astype(int)
    return X[FEATURES]

def baseline_metrics(df: pd.DataFrame, label="baseline"):
    """Stats for an unfiltered cohort."""
    n = len(df); w = int(df.win.sum())
    wins  = df.pnl[df.pnl > 0].sum()
    loss  = -df.pnl[df.pnl <= 0].sum()
    pf    = wins / loss if loss > 0 else float("inf")
    total = df.pnl.sum()
    print(f"  {label:30s} n={n:>4} WR={w/n:5.1%} PF={pf:5.2f} Net=${total:>+9,.2f}")
    return dict(n=n, wr=w/n if n else 0, pf=pf, net=total)

def train_one(train_df, test_df, label=""):
    """Train XGB on train_df, evaluate gate on test_df. Returns metrics."""
    import xgboost as xgb
    from sklearn.metrics import roc_auc_score

    Xtr, ytr = to_features(train_df), train_df.win.values
    Xte, yte = to_features(test_df),  test_df.win.values

    # Shallow model — small dataset, must constrain
    model = xgb.XGBClassifier(
        n_estimators=200, max_depth=3, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        min_child_weight=4, reg_lambda=1.0,
        objective="binary:logistic", eval_metric="logloss",
        verbosity=0, random_state=42,
    )
    model.fit(Xtr, ytr)

    p = model.predict_proba(Xte)[:, 1]
    auc = roc_auc_score(yte, p) if len(set(yte)) > 1 else float("nan")

    out = {"auc": auc, "n_train": len(train_df), "n_test": len(test_df),
           "model": model, "features": FEATURES}
    print(f"\n  [{label}]  AUC={auc:.3f}  train={len(train_df)} test={len(test_df)}")
    return out, p

def gate_and_score(test_df, p, thresholds=(0.40, 0.50, 0.55, 0.60)):
    """Apply each threshold as a gate; report PF/net on the kept trades."""
    print(f"  {'gate':>10} {'kept':>6} {'WR':>7} {'PF':>7} {'Net':>11} {'vs base':>10}")
    base_net = test_df.pnl.sum()
    for th in thresholds:
        keep = p >= th
        if keep.sum() == 0:
            print(f"  {f'p>={th:.2f}':>10} {0:>6} {'-':>7} {'-':>7} {0:>10,.2f} {0-base_net:>+10,.2f}")
            continue
        sub  = test_df.iloc[keep]
        wins = sub.pnl[sub.pnl > 0].sum()
        loss = -sub.pnl[sub.pnl <= 0].sum()
        pf   = wins / loss if loss > 0 else float("inf")
        net  = sub.pnl.sum()
        wr   = (sub.pnl > 0).mean()
        delta = net - base_net
        print(f"  {f'p>={th:.2f}':>10} {keep.sum():>6} {wr:>6.1%} {pf:>7.2f} {net:>+10,.2f} {delta:>+10,.2f}")

def feature_importance(model, features):
    imp = model.feature_importances_
    rank = sorted(zip(features, imp), key=lambda x: -x[1])
    print("\n  Feature importance:")
    for f, v in rank:
        bar = "*" * int(v * 60)
        print(f"    {f:14s} {v:.3f} {bar}")

def main():
    # Find available backtest CSVs
    contracts = {}
    for fname, path in [
        ("NQZ25",  os.path.join(BASE, "IOF_NQ_backtest_NQZ25-CME.csv")),
        ("NQH6",   os.path.join(BASE, "xgb_NQH6.csv")),
        ("ENQM26", os.path.join(BASE, "xgb_ENQM26.csv")),
    ]:
        if os.path.exists(path):
            contracts[fname] = load_pairs(path)
            print(f"loaded {fname}: {len(contracts[fname])} trades")
        else:
            print(f"missing  {fname}: {path}")

    if not contracts:
        print("\nNO DATA — backtest CSVs not generated yet."); return

    print("\n=== Per-contract baselines ===")
    for c, df in contracts.items():
        baseline_metrics(df, c)

    total = sum(len(d) for d in contracts.values())
    print(f"\nTotal labels available: {total}")
    if total < 40:
        print("\n*** WARNING: <40 labels total. Any XGBoost result will be noise. ***")
        print("Recommended: hook into _on_m4_arm etc. to label CANDIDATE fires, not just gated fires.")

    if len(contracts) < 2:
        print("\nNeed >=2 contracts for cross-contract validation. Stopping.")
        return

    # Cross-contract: train on N-1 contracts, test on the held-out one
    print("\n=== Cross-contract leave-one-out ===")
    keys = list(contracts.keys())
    for hold in keys:
        train_dfs = [contracts[k] for k in keys if k != hold]
        train_df  = pd.concat(train_dfs, ignore_index=True)
        test_df   = contracts[hold].reset_index(drop=True)

        if len(train_df) < 10 or len(test_df) < 5:
            print(f"\n  Skip hold={hold}: train={len(train_df)} test={len(test_df)} too small")
            continue

        baseline_metrics(test_df, f"hold={hold} (baseline, no gate)")
        res, probs = train_one(train_df, test_df, label=f"hold={hold}")
        gate_and_score(test_df, probs)
        feature_importance(res["model"], res["features"])

    print("\n=== Done ===")

if __name__ == "__main__":
    main()
