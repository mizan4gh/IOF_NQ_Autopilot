"""Trade-frequency funnel report — where do candidate setups die?

Runs the production config (v12.37, early-scratch ON) on all three contracts
and reports, per contract:
  - shape-level near-misses (LOG_NOFIRE taxonomy: score floor, no-reclaim, ...)
  - post-cascade gate kills (regime / RM floor / stop-cooldown / qual floor)
  - qual-floor kill distribution (mode, q)
  - day coverage: RTH days vs days-with-an-arm vs days-traded

Motivated by the "trade more / trade every day" question: before touching any
lever, quantify which gate actually binds. Read-only — no scenario changes.

Usage: python funnel_report.py [NQZ25|NQM5|NQH6]   (default: all three)
"""
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")

CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "NQM5.CME.scid",
    "NQH6":  SC_DATA / "NQH6.CME.scid",
}

_PROD = dict(
    NEWS_FILTER=1,
    C_OPEN_COOL=36,
    TARGET_VOL=3000,
    SCALE_OUT=False,
    MAX_TRADES=6,
    DAILY_LOSS=800.0,
    DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,  # v12.37 shipped
    QUAL_FLOOR=50,
    LOG_NOFIRE=1,
)


def run_contract(tag, scid):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest

    for k, v in _PROD.items():
        if not hasattr(backtest, k):
            raise AttributeError(f"backtest.py has no constant {k}")
        setattr(backtest, k, v)

    out = BASE / f"IOF_NQ_funnel_{tag}.csv"
    sys.argv = ["backtest.py", str(scid), str(out)]
    print(f"\n========== {tag} ==========")
    bt = backtest.main()
    return bt


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    rows = {}
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing scid: {scid}")
            continue
        bt = run_contract(tag, scid)
        rows[tag] = dict(
            funnel=dict(bt.funnel),
            per_mode={m: dict(v) for m, v in bt.funnel_mode.items()},
            qual_killed=list(bt.funnel_qual_killed),
            nofire_bars=bt.nofire_bars,
            nofire_possible=dict(bt.nofire_possible),
            nofire_why=dict(bt.nofire_why),
            days=(len(bt.fn_days_rth), len(bt.fn_days_armed), len(bt.fn_days_entered)),
        )

    print("\n" + "=" * 78)
    print(" FUNNEL REPORT (prod config, v12.37)")
    print("=" * 78)
    for tag, r in rows.items():
        f = r["funnel"]
        d_rth, d_arm, d_ent = r["days"]
        print(f"\n--- {tag} ---")
        print(f"  shape near-miss bars (no arm at all): {r['nofire_bars']}  "
              f"possible={r['nofire_possible']}")
        why = Counter(r["nofire_why"])
        if why:
            print("  near-miss reasons (top):")
            for w, c in why.most_common(8):
                print(f"    {w:18s} {c}")
        print(f"  cascade-armed setups : {f['armed']}")
        print(f"    killed by regime   : {f['regime']}")
        print(f"    killed M1-extra    : {f['m1_extra']}")
        print(f"    killed by RM floor : {f['rm_floor']}")
        print(f"    killed stop-cool   : {f['cool_stop']}")
        print(f"    killed by qual<50  : {f['qual']}")
        print(f"    ENTERED            : {f['entered']}")
        print(f"  per-mode: " + "  ".join(
            f"{m}[arm={v['armed']} qual={v['qual']} ent={v['entered']}]"
            for m, v in sorted(r["per_mode"].items())))
        qk = Counter(f"{m}:q{q}" for m, q in r["qual_killed"])
        if qk:
            print("  qual-floor kills (mode:q):")
            for w, c in qk.most_common(12):
                print(f"    {w:12s} {c}")
        print(f"  day coverage: RTH days={d_rth}  days with arm={d_arm}  "
              f"days traded={d_ent}  ({d_ent}/{d_rth} = {d_ent / max(1, d_rth):.0%})")


if __name__ == "__main__":
    main()
