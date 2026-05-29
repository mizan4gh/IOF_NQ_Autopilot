#!/usr/bin/env python3
"""Validation A — does XGB confirm layer keep production winners / veto losers?

For each contract:
  1. Load production-gated trades (xgb_<C>.csv: 5/10/3 SETUP+EXIT pairs).
  2. Load same-contract candidates (candidates_<C>.csv: all armed candidates).
  3. Match production SETUPs to candidate rows by (Date, Time, Mode, Side).
  4. Train XGB on the OTHER contracts' candidates (cross-contract holdout).
  5. Score each production trade, tabulate keep/veto at p>=0.50 and p>=0.55.

Output: how many real wins / real losses are kept/vetoed per threshold.
"""
import os
import numpy as np
import pandas as pd

BASE = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final"

CONTRACTS = {
    "NQZ25":  dict(prod=os.path.join(BASE, "IOF_NQ_backtest_NQZ25-CME.csv"),
                   cand=os.path.join(BASE, "candidates_NQZ25.csv")),
    "NQH6":   dict(prod=os.path.join(BASE, "xgb_NQH6.csv"),
                   cand=os.path.join(BASE, "candidates_NQH6.csv")),
    "ENQM26": dict(prod=os.path.join(BASE, "xgb_ENQM26.csv"),
                   cand=os.path.join(BASE, "candidates_ENQM26.csv")),
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

def load_prod_pairs(path: str) -> pd.DataFrame:
    """SETUP+EXIT pairs from production backtest, with realized PnL per trade."""
    df = pd.read_csv(path)
    setups = df[df.Event == "SETUP"].reset_index(drop=True)
    exits  = df[df.Event == "EXIT"].reset_index(drop=True)
    n = min(len(setups), len(exits))
    s = setups.iloc[:n].copy()
    e = exits.iloc[:n].copy()
    pnl_cum = e.TotalPnL.values
    pnl = np.diff(np.concatenate([[0.0], pnl_cum]))
    s["pnl"] = pnl
    s["win"] = (pnl > 0).astype(int)
    s["exit_rsn"] = e.ExitReason.values
    return s

def match_to_candidate(prod_row, cand_df):
    """Find the candidate row matching this production setup by (Date, Time, Mode, Side)."""
    m = cand_df[(cand_df.Date == prod_row.Date) &
                (cand_df.Time == prod_row.Time) &
                (cand_df.Mode == prod_row.Mode) &
                (cand_df.Side == prod_row.Side)]
    if len(m) == 0: return None
    return m.iloc[0]

def main():
    import xgboost as xgb

    cand = {k: pd.read_csv(v["cand"]) for k, v in CONTRACTS.items() if os.path.exists(v["cand"])}
    prod = {k: load_prod_pairs(v["prod"]) for k, v in CONTRACTS.items() if os.path.exists(v["prod"])}
    print("Datasets:")
    for k in CONTRACTS:
        c = len(cand.get(k, []))
        p = len(prod.get(k, []))
        print(f"  {k:8s} candidates={c:>4}  production_trades={p:>3}")

    print("\n=== Cross-contract validation: score prod trades with XGB trained on OTHER contracts ===\n")
    rows = []
    for hold, prod_df in prod.items():
        # Train on the candidates from the OTHER contracts
        train_dfs = [cand[k] for k in cand if k != hold]
        if not train_dfs:
            print(f"  Skip {hold}: no other contracts to train on"); continue
        train_df = pd.concat(train_dfs, ignore_index=True)

        model = xgb.XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            min_child_weight=10, reg_lambda=1.0,
            objective="binary:logistic", eval_metric="logloss",
            verbosity=0, random_state=42,
        )
        model.fit(featurize(train_df), train_df.Win.values)

        # Match each production trade to its candidate row, score
        unmatched = 0; scored = []
        for _, p in prod_df.iterrows():
            c = match_to_candidate(p, cand[hold])
            if c is None: unmatched += 1; continue
            X1 = featurize(pd.DataFrame([c]))
            prob = model.predict_proba(X1)[0, 1]
            scored.append(dict(
                date=p.Date, time=p.Time, mode=p.Mode, side=p.Side,
                pnl=p.pnl, win=int(p.win), exit_rsn=p.exit_rsn,
                xgb_prob=prob,
            ))

        if not scored:
            print(f"  {hold}: no matches"); continue
        sd = pd.DataFrame(scored)
        print(f"  ── {hold} ──  ({len(sd)} prod trades scored; {unmatched} unmatched)")
        print(f"  {'date':10s} {'time':8s} {'mode':4s} {'side':5s} {'pnl':>9s} {'exit':>8s} {'xgb_p':>6s}")
        for _, r in sd.sort_values("xgb_prob", ascending=False).iterrows():
            print(f"  {r['date']:10s} {r['time']:8s} {r['mode']:4s} {r['side']:5s} "
                  f"{r['pnl']:>+9,.0f} {r['exit_rsn']:>8s} {r['xgb_prob']:>6.3f}")

        # Threshold analysis
        for th in (0.40, 0.50, 0.55, 0.60):
            keep = sd[sd.xgb_prob >= th]
            veto = sd[sd.xgb_prob <  th]
            kw = int(keep.win.sum()); kl = len(keep) - kw
            vw = int(veto.win.sum()); vl = len(veto) - vw
            kept_pnl = keep.pnl.sum(); base_pnl = sd.pnl.sum()
            rows.append(dict(contract=hold, threshold=th,
                             kept=len(keep), kept_wins=kw, kept_losses=kl, kept_pnl=kept_pnl,
                             vetoed=len(veto), vetoed_wins=vw, vetoed_losses=vl,
                             baseline_pnl=base_pnl, delta=kept_pnl - base_pnl))
            print(f"    p>={th:.2f}: KEEP {len(keep)} ({kw}W/{kl}L ${kept_pnl:+,.0f})   "
                  f"VETO {len(veto)} ({vw}W/{vl}L)   delta vs prod ${kept_pnl - base_pnl:+,.0f}")
        print()

    print("\n=== Aggregate across all 3 contracts ===")
    agg = pd.DataFrame(rows)
    if agg.empty: return
    for th, g in agg.groupby("threshold"):
        kept_w = g.kept_wins.sum(); kept_l = g.kept_losses.sum()
        ve_w   = g.vetoed_wins.sum(); ve_l   = g.vetoed_losses.sum()
        kept_p = g.kept_pnl.sum(); base_p = g.baseline_pnl.sum()
        print(f"  p>={th:.2f}: KEEP {kept_w+kept_l} ({kept_w}W/{kept_l}L ${kept_p:+,.0f})   "
              f"VETO {ve_w+ve_l} ({ve_w}W/{ve_l}L)   "
              f"prod-net ${base_p:+,.0f}  gated-net ${kept_p:+,.0f}  delta ${kept_p - base_p:+,.0f}")

    print("\nReading:")
    print("  - If 'KEEP n W / 0 L' at some threshold -> XGB perfectly filters losers, deployable.")
    print("  - If 'VETO' column has wins -> XGB kills production winners (bad).")
    print("  - If 'delta' is positive -> XGB confirm gate adds money on top of prod gate.")

if __name__ == "__main__":
    main()
