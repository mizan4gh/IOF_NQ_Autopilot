"""CausalImpact of the 2026-07-05 floor-60 deploy on LIVE realized P&L.

Response  : live daily realized P&L  (you provide -> live_pnl.csv: date,realized_pnl)
Control   : backtest daily P&L under the OLD config (ci_control_backtest_pnl.csv,
            from causal_impact_prep.py) — unaffected by the deploy, so post-period
            divergence = the deploy's causal effect.
Model     : tfcausalimpact (TensorFlow-Probability BSTS).

Run AFTER the deploy has ~15+ post-period trading days. Until then it will tell
you the post-period is too thin.

Setup (once, when ready to run):
    pip install tfcausalimpact        # pulls tensorflow + tensorflow_probability
Usage:
    python causal_impact_analysis.py --response live_pnl.csv --deploy 2026-07-05
    (defaults: --control ci_control_backtest_pnl.csv)
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).parent
DEPLOY_DEFAULT = "2026-07-05"
MIN_POST = 15   # minimum post-period trading days for a credible estimate
MIN_PRE = 30


def load(path, val_col_candidates):
    import pandas as pd
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "date" not in df.columns:
        sys.exit(f"{path}: needs a 'date' column (got {list(df.columns)})")
    val = next((c for c in val_col_candidates if c in df.columns), None)
    if val is None:
        sys.exit(f"{path}: needs one of {val_col_candidates} (got {list(df.columns)})")
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")[[val]].rename(columns={val: val_col_candidates[0]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--response", default="live_pnl.csv",
                    help="live P&L csv: date,realized_pnl")
    ap.add_argument("--control", default="ci_control_backtest_pnl.csv")
    ap.add_argument("--deploy", default=DEPLOY_DEFAULT)
    ap.add_argument("--plot", default="ci_result.png")
    a = ap.parse_args()

    try:
        import pandas as pd
    except ImportError:
        sys.exit("pandas required.")

    resp_path = BASE / a.response
    ctrl_path = BASE / a.control
    if not ctrl_path.exists():
        sys.exit(f"control missing: {ctrl_path.name} — run causal_impact_prep.py first.")
    if not resp_path.exists():
        sys.exit(f"response missing: {resp_path.name} — fill live_pnl_template.csv and "
                 f"save it as {a.response} (columns: date,realized_pnl).")

    y = load(resp_path, ["realized_pnl", "pnl", "live_pnl"])
    x = load(ctrl_path, ["bt_pnl", "pnl"])

    # regular weekday index spanning both, response first column, 0-fill
    idx = pd.bdate_range(min(y.index.min(), x.index.min()),
                         max(y.index.max(), x.index.max()))
    data = pd.concat([y.reindex(idx).fillna(0.0),
                      x.reindex(idx).fillna(0.0)], axis=1)
    data.columns = ["y", "control_bt_pnl"]

    deploy = pd.Timestamp(a.deploy)
    pre = [data.index.min(), deploy - pd.Timedelta(days=1)]
    post = [deploy, data.index.max()]
    pre_n = int(((data.index >= pre[0]) & (data.index <= pre[1])).sum())
    post_n = int((data.index >= post[0]).sum())
    post_trades = int((y.reindex(idx).fillna(0.0).loc[post[0]:] != 0).sum().iloc[0])

    print(f"pre-period : {pre[0].date()} .. {pre[1].date()}  ({pre_n} weekdays)")
    print(f"post-period: {post[0].date()} .. {post[1].date()}  ({post_n} weekdays, "
          f"{post_trades} with a live trade)")

    if post_n == 0:
        sys.exit("\nPOST-PERIOD IS EMPTY — the deploy hasn't produced data yet. "
                 "Re-run after ~15+ post-deploy trading days.")
    if post_trades < MIN_POST:
        print(f"\n[warn] only {post_trades} post-deploy trade-days (< {MIN_POST}) — "
              "estimate will be weak/wide. Consider waiting for more.")
    if pre_n < MIN_PRE:
        print(f"[warn] pre-period {pre_n} weekdays (< {MIN_PRE}) — BSTS fit will be shaky.")

    try:
        from causalimpact import CausalImpact
    except ImportError:
        sys.exit("\ntfcausalimpact not installed. When ready:\n"
                 "    pip install tfcausalimpact\n"
                 "(pulls tensorflow + tensorflow_probability, ~0.5GB).")

    ci = CausalImpact(data, [str(pre[0].date()), str(pre[1].date())],
                      [str(post[0].date()), str(post[1].date())])
    print("\n" + "=" * 70)
    print(ci.summary())
    print("\n" + ci.summary(output="report"))
    try:
        import matplotlib
        matplotlib.use("Agg")
        ci.plot()
        import matplotlib.pyplot as plt
        plt.savefig(BASE / a.plot, dpi=110, bbox_inches="tight")
        print(f"\nplot saved: {a.plot}")
    except Exception as e:
        print(f"(plot skipped: {e})")


if __name__ == "__main__":
    main()
