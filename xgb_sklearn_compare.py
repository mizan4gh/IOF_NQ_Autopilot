#!/usr/bin/env python3
"""Compare sklearn models on the SAME walk-forward test that failed XGBoost.

Models tried:
  - LogisticRegression (L1 / Lasso)   — if non-zero coefs + lift, extract as hand-rule
  - LogisticRegression (L2 / Ridge)
  - RandomForest (shallow, regularized)
  - GradientBoosting (sklearn — different defaults than XGBoost)
  - XGBoost (reference baseline from xgb_walkforward.py)

For each model, run expanding-month walk-forward per contract, aggregate
held-out predictions, report AUC + best-gate lift.

If L1 logreg produces sparse non-zero coefs AND has positive walk-forward
lift, print the coefficient table — those are the candidate hand-rule.
"""
import os
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import roc_auc_score

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

def build_models():
    return {
        "LogReg_L1": Pipeline([("sc", StandardScaler()),
                               ("clf", LogisticRegression(
                                   penalty="l1", solver="liblinear",
                                   C=0.1, max_iter=2000, random_state=42))]),
        "LogReg_L2": Pipeline([("sc", StandardScaler()),
                               ("clf", LogisticRegression(
                                   penalty="l2", solver="lbfgs",
                                   C=0.5, max_iter=2000, random_state=42))]),
        "RandomForest": RandomForestClassifier(
            n_estimators=200, max_depth=4, min_samples_leaf=10,
            random_state=42, n_jobs=-1),
        "GradBoost_sk": GradientBoostingClassifier(
            n_estimators=200, max_depth=3, learning_rate=0.05,
            subsample=0.8, min_samples_leaf=10, random_state=42),
    }

def walk_forward_predict(df_sorted: pd.DataFrame, model_factory):
    """Expanding-window monthly walk-forward. Returns concat(probs, y, pnl)."""
    months = sorted(df_sorted.ym.unique())
    if len(months) < 2:
        return None, None, None
    pp, yy, pn = [], [], []
    for k, m in enumerate(months):
        if k == 0: continue
        train_df = df_sorted[df_sorted.ym < m]
        test_df  = df_sorted[df_sorted.ym == m].reset_index(drop=True)
        if len(train_df) < 20 or len(test_df) < 3: continue
        if len(set(train_df.Win.values)) < 2: continue  # need both classes
        model = model_factory()
        model.fit(featurize(train_df), train_df.Win.values)
        prob = model.predict_proba(featurize(test_df))[:, 1]
        pp.append(prob); yy.append(test_df.Win.values); pn.append(test_df.PnL.values)
    if not pp: return None, None, None
    return np.concatenate(pp), np.concatenate(yy), np.concatenate(pn)

def best_gate(probs, y, pnl, thresholds=(0.40, 0.45, 0.50, 0.55, 0.60)):
    base = pnl.sum()
    best = None
    for th in thresholds:
        keep = probs >= th
        if keep.sum() < max(10, int(0.2 * len(y))): continue
        sub_pnl = pnl[keep]
        sub_win = y[keep]
        wins = sub_pnl[sub_pnl > 0].sum()
        loss = -sub_pnl[sub_pnl <= 0].sum()
        pf = wins / loss if loss > 0 else float("inf")
        net = sub_pnl.sum()
        wr = sub_win.mean()
        delta = net - base
        if delta > 0 and (best is None or delta > best["delta"]):
            best = dict(th=th, kept=int(keep.sum()), wr=wr, pf=pf, net=net, delta=delta)
    return base, best

def coef_table(pipeline, features):
    """Extract logreg coefs (after StandardScaler) sorted by |coef|."""
    clf = pipeline.named_steps["clf"]
    coefs = clf.coef_[0]
    rank = sorted(zip(features, coefs), key=lambda x: -abs(x[1]))
    return rank

def main():
    data = {}
    for c, path in CONTRACTS.items():
        if not os.path.exists(path): continue
        df = pd.read_csv(path)
        df["dt"] = pd.to_datetime(df.Date + " " + df.Time)
        df = df.sort_values("dt").reset_index(drop=True)
        df["ym"] = df.dt.dt.to_period("M")
        data[c] = df
        print(f"loaded {c:7s}  n={len(df):>4}  months={df.ym.nunique()}")

    factories = build_models()
    # XGB reference (re-imported here for parity)
    try:
        import xgboost as xgb
        factories["XGBoost_ref"] = lambda: xgb.XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=10,
            reg_lambda=1.0, objective="binary:logistic", eval_metric="logloss",
            verbosity=0, random_state=42)
    except ImportError:
        pass

    print("\n" + "="*78)
    print(" Expanding-month walk-forward AUC + best gate (lift over 'keep all' baseline)")
    print("="*78)

    results = {}
    for c, df in data.items():
        if df.ym.nunique() < 2:
            print(f"\n  {c}: only {df.ym.nunique()} months, skipping"); continue
        print(f"\n  ── {c}  ──")
        print(f"  {'model':14s} {'n_test':>7} {'AUC':>6} {'best_gate':>20} {'kept':>5} {'WR':>6} {'PF':>6} {'lift':>10}")
        for name, mk in factories.items():
            mf = (lambda nm=name: factories[nm]) if callable(mk) else mk
            mf_call = mk if callable(mk) else (lambda nm=name: factories[nm])
            try:
                probs, y, pnl = walk_forward_predict(df, mk if callable(mk) and not isinstance(mk, type) else (lambda: mk))
            except Exception as e:
                print(f"  {name:14s} ERROR: {type(e).__name__}: {e}"); continue
            if probs is None:
                print(f"  {name:14s} no folds"); continue
            auc = roc_auc_score(y, probs) if len(set(y)) > 1 else float("nan")
            base, best = best_gate(probs, y, pnl)
            if best:
                gate_str = f"p>={best['th']:.2f}"
                print(f"  {name:14s} {len(y):>7} {auc:>6.3f} {gate_str:>20} {best['kept']:>5} {best['wr']:>5.1%} {best['pf']:>6.2f} {best['delta']:>+10,.0f}")
            else:
                print(f"  {name:14s} {len(y):>7} {auc:>6.3f} {'(no positive lift)':>20} {'-':>5} {'-':>6} {'-':>6} {'-':>10}")
            results.setdefault(name, []).append((c, auc, base, best))

    # L1 coefficient extraction — only useful if L1 had any walk-forward lift
    print("\n" + "="*78)
    print(" L1 LogReg coefficient extraction (per contract, last fold)")
    print("="*78)
    for c, df in data.items():
        if df.ym.nunique() < 2: continue
        months = sorted(df.ym.unique())
        train_df = df[df.ym < months[-1]]
        if len(train_df) < 20 or len(set(train_df.Win.values)) < 2: continue
        pipe = factories["LogReg_L1"]
        pipe.fit(featurize(train_df), train_df.Win.values)
        rank = coef_table(pipe, FEATURES)
        nz = [(f, v) for f, v in rank if abs(v) > 1e-6]
        print(f"\n  {c}: trained on {len(train_df)} samples up to month {months[-2]}")
        if not nz:
            print(f"    all coefficients ZERO — L1 found no signal worth keeping")
            continue
        print(f"    {len(nz)} non-zero coefficients:")
        for f, v in nz[:10]:
            bar = "*" * min(40, int(abs(v) * 20))
            sign = "+" if v >= 0 else "-"
            print(f"      {sign} {abs(v):.3f}  {f:14s} {bar}")

    # Overall verdict
    print("\n" + "="*78)
    print(" OVERALL")
    print("="*78)
    for name, res in results.items():
        passing = sum(1 for (c, auc, base, best) in res if best and auc >= 0.55)
        total_lift = sum(best["delta"] for (c, auc, base, best) in res if best)
        print(f"  {name:14s}  folds_passing(AUC>=0.55 + lift)={passing}/{len(res)}  total_lift=${total_lift:+,.0f}")

if __name__ == "__main__":
    main()
