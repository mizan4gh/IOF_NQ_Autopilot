#!/usr/bin/env python3
"""
IOF NQ — ML Signal Quality Classifier

Given edge_signal bars produced by edge_discovery.py, predicts whether
the signal leads to a profitable move (win/loss classification).

Approach:
  Label   : signed 3-bar forward return > 0  (next 3 RTH bars, same session)
  Train   : NQZ5 (Sep–Dec 2025)  — in-sample
  Test    : NQH26 (Jan–May 2026) — out-of-sample
  Models  : Logistic Regression, Random Forest, Gradient Boosting (via sklearn)
  Features: 10 engineered from edge_discovery CSV columns

Usage:
  python ml_signal.py                          # uses default CSV paths
  python ml_signal.py NQZ5.csv NQH26.csv
"""

import os, sys, csv, math, warnings
from typing import List, Tuple
from dataclasses import dataclass

import numpy as np

warnings.filterwarnings("ignore")

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score, classification_report
    from sklearn.model_selection import TimeSeriesSplit
    SKLEARN = True
except ImportError:
    SKLEARN = False
    print("WARNING: scikit-learn not found — falling back to pure-Python LogReg only.")

# ─────────────────────────────────────────────────────────────────────────────
#  DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR  = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final"
CSV_TRAIN = os.path.join(BASE_DIR, "IOF_NQ_edge_NQZ5.csv")
CSV_TEST  = os.path.join(BASE_DIR, "IOF_NQ_edge_NQH26.csv")
OUT_PRED  = os.path.join(BASE_DIR, "IOF_NQ_signal_predictions.csv")
OUT_MODEL = os.path.join(BASE_DIR, "IOF_NQ_signal_model.txt")

FORWARD_BARS = 3      # bars ahead for label
MIN_MOVE_ATR = 0.0    # win if signed forward return > this


# ─────────────────────────────────────────────────────────────────────────────
#  CSV LOADER
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Row:
    dt:            str
    date:          int
    hhmm:          int
    close:         float
    atr:           float
    dist_vwap_atr: float
    cum_delta_z:   float
    bar_delta:     float
    pace_ref:      float
    dist_poc_atr:  float
    dist_vah_atr:  float
    dist_val_atr:  float
    edge_signal:   int


def load_csv(path: str) -> List[Row]:
    rows = []
    with open(path, newline="") as f:
        for rec in csv.DictReader(f):
            rows.append(Row(
                dt            = rec["dt"],
                date          = int(rec["date"]),
                hhmm          = int(rec["hhmm"]),
                close         = float(rec["close"]),
                atr           = float(rec["atr"]),
                dist_vwap_atr = float(rec["dist_vwap_atr"]),
                cum_delta_z   = float(rec["cum_delta_z"]),
                bar_delta     = float(rec["bar_delta"]),
                pace_ref      = max(float(rec["pace_ref"]), 1.0),
                dist_poc_atr  = float(rec["dist_poc_atr"]),
                dist_vah_atr  = float(rec["dist_vah_atr"]),
                dist_val_atr  = float(rec["dist_val_atr"]),
                edge_signal   = int(rec["edge_signal"]),
            ))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  FEATURE ENGINEERING + LABELING
# ─────────────────────────────────────────────────────────────────────────────
FEATURE_NAMES = [
    "dist_vwap_atr_signed",   # (close - vwap)/atr × signal  — alignment strength
    "cum_delta_z_signed",     # cum_delta_z × signal          — delta alignment
    "delta_pace_ratio",       # |bar_delta| / pace_ref        — impulse vs pace
    "dist_poc_atr_signed",    # (poc-close)/atr × signal      — POC pull in signal dir
    "dist_vah_atr_signed",    # (vah-close)/atr × signal
    "dist_val_atr_signed",    # (val-close)/atr × signal
    "abs_dist_vwap",          # |dist_vwap_atr|               — extension magnitude
    "abs_cum_delta_z",        # |cum_delta_z|
    "time_sin",               # sin(2π × RTH position)        — time-of-day
    "time_cos",               # cos(2π × RTH position)
]


def hhmm_rad(hhmm: int) -> float:
    t = (hhmm // 100) * 60 + (hhmm % 100)
    lo, hi = 9 * 60 + 35, 15 * 60 + 55
    return 2 * math.pi * max(0.0, min(1.0, (t - lo) / (hi - lo)))


def make_features(r: Row) -> List[float]:
    sig = r.edge_signal
    rad = hhmm_rad(r.hhmm)
    return [
        r.dist_vwap_atr * sig,
        r.cum_delta_z   * sig,
        abs(r.bar_delta) / r.pace_ref,
        r.dist_poc_atr  * sig,
        r.dist_vah_atr  * sig,
        r.dist_val_atr  * sig,
        abs(r.dist_vwap_atr),
        abs(r.cum_delta_z),
        math.sin(rad),
        math.cos(rad),
    ]


def label_rows(rows: List[Row]) -> Tuple[np.ndarray, np.ndarray, List[Row]]:
    X, y, sig_rows = [], [], []
    n = len(rows)
    for i, r in enumerate(rows):
        if r.edge_signal == 0:
            continue
        # Find forward bar: FORWARD_BARS ahead, same session
        j = i + FORWARD_BARS
        if j >= n or rows[j].date != r.date:
            j = i + 1
            while j < n and rows[j].date == r.date:
                j += 1
            j -= 1
            if j <= i:
                continue
        signed_ret = (rows[j].close - r.close) * r.edge_signal / max(r.atr, 0.01)
        X.append(make_features(r))
        y.append(1 if signed_ret > MIN_MOVE_ATR else 0)
        sig_rows.append(r)
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int32), sig_rows


# ─────────────────────────────────────────────────────────────────────────────
#  PURE-PYTHON LOGISTIC REGRESSION  (fallback when sklearn absent)
# ─────────────────────────────────────────────────────────────────────────────
class PyLogReg:
    def __init__(self, lr=0.05, epochs=500, l2=0.01):
        self.lr = lr; self.epochs = epochs; self.l2 = l2
        self.w = None; self.b = 0.0

    def fit(self, X, y):
        n, d = X.shape
        self.w = np.zeros(d); self.b = 0.0
        for _ in range(self.epochs):
            z = X @ self.w + self.b
            p = 1 / (1 + np.exp(-np.clip(z, -500, 500)))
            err = p - y
            self.w -= self.lr * (X.T @ err / n + self.l2 * self.w)
            self.b -= self.lr * err.mean()

    def predict_proba(self, X):
        z = X @ self.w + self.b
        p = 1 / (1 + np.exp(-np.clip(z, -500, 500)))
        return np.column_stack([1 - p, p])

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


# ─────────────────────────────────────────────────────────────────────────────
#  METRICS HELPER
# ─────────────────────────────────────────────────────────────────────────────
def eval_model(model, X, y, scaler=None, label=""):
    Xs = scaler.transform(X) if scaler else X
    if hasattr(model, "predict_proba"):
        prob = model.predict_proba(Xs)[:, 1]
    else:
        prob = model.predict_proba(Xs)[:, 1]
    pred = (prob >= 0.5).astype(int)

    tp = int(((pred == 1) & (y == 1)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    acc  = (tp + tn) / len(y)
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0
    rec  = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1   = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
    try:
        auc = roc_auc_score(y, prob) if SKLEARN else _auc(prob, y)
    except Exception:
        auc = 0.5
    base = y.mean()

    print(f"\n  -- {label}")
    print(f"     Baseline acc   : {base:.3f}")
    print(f"     Accuracy       : {acc:.3f}")
    print(f"     Precision      : {prec:.3f}")
    print(f"     Recall         : {rec:.3f}")
    print(f"     F1             : {f1:.3f}")
    print(f"     AUC-ROC        : {auc:.3f}")
    print(f"     Confusion      : TP={tp} TN={tn} FP={fp} FN={fn}")
    return prob, pred, auc


def _auc(prob, y):
    pairs = sorted(zip(prob, y), reverse=True)
    n_pos = y.sum(); n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0: return 0.5
    tp = area = 0
    for _, lbl in pairs:
        if lbl == 1: tp += 1
        else: area += tp
    return area / (n_pos * n_neg)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    csv_tr = sys.argv[1] if len(sys.argv) > 1 else CSV_TRAIN
    csv_te = sys.argv[2] if len(sys.argv) > 2 else CSV_TEST

    print("=" * 66)
    print("  IOF NQ — EDGE SIGNAL ML CLASSIFIER")
    print("=" * 66)
    print(f"  Train  : {os.path.basename(csv_tr)}")
    print(f"  Test   : {os.path.basename(csv_te)}")
    print(f"  Label  : {FORWARD_BARS}-bar forward signed return > {MIN_MOVE_ATR} ATR")
    print(f"  Engine : {'scikit-learn' if SKLEARN else 'pure-Python'}")

    print("\nLoading and labeling ...")
    rows_tr = load_csv(csv_tr)
    rows_te = load_csv(csv_te)
    X_tr, y_tr, sig_tr = label_rows(rows_tr)
    X_te, y_te, sig_te = label_rows(rows_te)
    print(f"  Train signal bars : {len(X_tr):,}  (win rate {y_tr.mean()*100:.1f}%)")
    print(f"  Test  signal bars : {len(X_te):,}  (win rate {y_te.mean()*100:.1f}%)")

    # ── Scaler ────────────────────────────────────────────────────────────
    if SKLEARN:
        scaler = StandardScaler().fit(X_tr)
    else:
        # numpy standardization
        mu  = X_tr.mean(axis=0); sg = X_tr.std(axis=0) + 1e-8
        class _Scaler:
            def transform(self, X): return (X - mu) / sg
        scaler = _Scaler()

    # ── Time-series CV on train ───────────────────────────────────────────
    print("\nTime-series CV (5-fold) on train set ...")
    if SKLEARN:
        tscv = TimeSeriesSplit(n_splits=5)
        cv_aucs = []
        for fold_tr, fold_val in tscv.split(X_tr):
            Xf_tr, yf_tr = X_tr[fold_tr], y_tr[fold_tr]
            Xf_val, yf_val = X_tr[fold_val], y_tr[fold_val]
            sc_f = StandardScaler().fit(Xf_tr)
            m = LogisticRegression(C=10, max_iter=500, solver="lbfgs")
            m.fit(sc_f.transform(Xf_tr), yf_tr)
            if len(np.unique(yf_val)) > 1:
                cv_aucs.append(roc_auc_score(yf_val, m.predict_proba(sc_f.transform(Xf_val))[:, 1]))
        print(f"  AUC per fold : {[round(a, 3) for a in cv_aucs]}")
        print(f"  Mean AUC     : {np.mean(cv_aucs):.3f}  +/-{np.std(cv_aucs):.3f}")

    # ── Define models ─────────────────────────────────────────────────────
    if SKLEARN:
        models = {
            "Logistic Regression": LogisticRegression(C=10, max_iter=500, solver="lbfgs"),
            "Random Forest":       RandomForestClassifier(n_estimators=200, max_depth=6,
                                       min_samples_leaf=15, random_state=42, n_jobs=-1),
            "Gradient Boosting":   GradientBoostingClassifier(n_estimators=200, max_depth=3,
                                       learning_rate=0.05, subsample=0.8, random_state=42),
        }
        Xtr_s = scaler.transform(X_tr)
    else:
        lr = PyLogReg(); lr.fit(scaler.transform(X_tr), y_tr)
        models = {"Logistic Regression": lr}
        Xtr_s = scaler.transform(X_tr)

    # ── Train + evaluate ──────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print("  MODEL RESULTS")
    print("=" * 66)
    all_results = {}
    for name, model in models.items():
        print(f"\n{'-'*46}  {name}")
        if SKLEARN:
            model.fit(Xtr_s, y_tr)
        prob_tr, pred_tr, auc_tr = eval_model(model, X_tr, y_tr, scaler, "TRAIN (NQZ5)")
        prob_te, pred_te, auc_te = eval_model(model, X_te, y_te, scaler, "TEST  (NQH26)")
        all_results[name] = {"auc_te": auc_te, "prob_te": prob_te, "pred_te": pred_te, "model": model}

    # ── Feature importance ────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print("  FEATURE IMPORTANCE")
    print("=" * 66)

    if SKLEARN and "Random Forest" in all_results:
        rf = all_results["Random Forest"]["model"]
        imp = rf.feature_importances_
        print("\n  Random Forest (mean decrease impurity):")
        for score, name in sorted(zip(imp, FEATURE_NAMES), reverse=True):
            bar = "#" * int(score * 80)
            print(f"    {name:<28}  {score:.4f}  {bar}")

    if SKLEARN and "Logistic Regression" in all_results:
        lr_m = all_results["Logistic Regression"]["model"]
        coef = lr_m.coef_[0]
        print("\n  Logistic Regression coefficients (standardized):")
        for w, name in sorted(zip(coef, FEATURE_NAMES), key=lambda x: abs(x[0]), reverse=True):
            sign = "+" if w >= 0 else "-"
            bar  = "#" * int(abs(w) * 6)
            print(f"    {sign}{abs(w):.4f}  {name:<28}  {bar}")

    if SKLEARN and "Gradient Boosting" in all_results:
        gb = all_results["Gradient Boosting"]["model"]
        imp = gb.feature_importances_
        print("\n  Gradient Boosting:")
        for score, name in sorted(zip(imp, FEATURE_NAMES), reverse=True):
            bar = "#" * int(score * 80)
            print(f"    {name:<28}  {score:.4f}  {bar}")

    # ── Best model predictions CSV ────────────────────────────────────────
    best_name = max(all_results, key=lambda k: all_results[k]["auc_te"])
    best_prob = all_results[best_name]["prob_te"]
    best_pred = all_results[best_name]["pred_te"]

    with open(OUT_PRED, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dt", "date", "hhmm", "close", "atr", "edge_signal",
                    "dist_vwap_atr", "cum_delta_z", "label", "prob_win",
                    "predicted", "correct"])
        for r, prob, pred, lbl in zip(sig_te, best_prob, best_pred, y_te):
            w.writerow([r.dt, r.date, r.hhmm, r.close, round(r.atr, 4),
                        r.edge_signal, round(r.dist_vwap_atr, 4),
                        round(r.cum_delta_z, 4), lbl, round(float(prob), 4),
                        int(pred), int(pred == lbl)])

    # ── Model params to file ──────────────────────────────────────────────
    with open(OUT_MODEL, "w") as f:
        f.write(f"IOF NQ Signal Quality Model\n")
        f.write(f"Best model : {best_name}\n")
        f.write(f"Test AUC   : {all_results[best_name]['auc_te']:.4f}\n\n")
        if SKLEARN and "Logistic Regression" in all_results:
            lr_m = all_results["Logistic Regression"]["model"]
            f.write("LR coefficients (use with StandardScaler fit on NQZ5 train):\n")
            for w, n in sorted(zip(lr_m.coef_[0], FEATURE_NAMES),
                               key=lambda x: abs(x[0]), reverse=True):
                f.write(f"  {n}: {w:+.4f}\n")
            f.write(f"\nIntercept: {lr_m.intercept_[0]:+.4f}\n")
            sc = scaler
            f.write(f"\nScaler mean: {list(np.round(sc.mean_, 4))}\n")
            f.write(f"Scaler std : {list(np.round(sc.scale_, 4))}\n")

    print("\n" + "=" * 66)
    print(f"  Best model   : {best_name}  (OOS AUC {all_results[best_name]['auc_te']:.3f})")
    print(f"  Predictions  : {OUT_PRED}")
    print(f"  Model file   : {OUT_MODEL}")
    print("=" * 66)


if __name__ == "__main__":
    main()
