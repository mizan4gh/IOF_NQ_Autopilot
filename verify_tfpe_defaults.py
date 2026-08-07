"""Assert the cpp SetDefaults block matches the backtested configuration.

The whole point of tuning in Python is lost if the numbers drift when they are
transcribed into ACSIL. This parses `SetDefaults` out of the .cpp and diffs it
against `backtest_trendfollow_propeval.Params` with the chosen overrides, so a
mismatch is a failed check rather than something noticed three weeks later.

Usage: python verify_tfpe_defaults.py [path/to/NQ_TrendFollow_PropEval.cpp]
"""
import re
import sys
from dataclasses import replace
from pathlib import Path

import backtest_trendfollow_propeval as TF

CPP = Path(sys.argv[1] if len(sys.argv) > 1 else "NQ_TrendFollow_PropEval.cpp")

# The configuration the shipped numbers were measured at. Keep in lockstep with
# the "CHOSEN" arm in the backtest write-up.
CHOSEN = dict(
    risk_mode="budget", adx_min=30.0, target_r=3.0, max_contracts=20,
    max_trades_day=3, daily_target=700.0, giveback_r=2.5, max_risk_r=1.0,
)

# cpp input name -> Params field. Times and cosmetic inputs are excluded.
MAP = {
    "In_FastLen": "fast_len", "In_SlowLen": "slow_len", "In_TrendLen": "trend_len",
    "In_SlopeBars": "slope_bars", "In_ADXLen": "adx_len", "In_ADXMin": "adx_min",
    "In_ATRLen": "atr_len", "In_PullbackBars": "pullback_bars",
    "In_TriggerBars": "trigger_bars", "In_CloseStrength": "close_strength",
    "In_StopATRMult": "stop_atr_mult", "In_MinStopPts": "min_stop_pts",
    "In_MaxStopPts": "max_stop_pts", "In_TargetR": "target_r",
    "In_UseTrailStop": "use_trail", "In_TrailOffsetR": "trail_offset_r",
    "In_RiskPerTrade": "risk_per_trade", "In_MaxContracts": "max_contracts",
    "In_DailyTarget": "daily_target", "In_DailyLossLimit": "daily_loss",
    "In_GivebackStop": "giveback", "In_MaxTradesDay": "max_trades_day",
    "In_MaxConsecLoss": "max_consec_loss", "In_MaxBarATRMult": "max_bar_atr",
    "In_MinATRPts": "min_atr_pts", "In_LossBuffer": "loss_buffer",
    "In_RiskMode": "risk_mode", "In_MaxRiskR": "max_risk_r",
    "In_GivebackR": "giveback_r",
}


def main():
    src = CPP.read_text(encoding="utf-8", errors="replace")
    got = {}
    for name, field in MAP.items():
        m = re.search(re.escape(name) + r"\.Set(?:Int|Float)\(\s*([-\d.]+)f?\s*\)", src)
        if not m:
            print(f"  MISSING in cpp: {name}")
            continue
        got[field] = float(m.group(1))

    want = replace(TF.Params(), **CHOSEN)
    bad = 0
    print(f"{CPP}  vs  Params(**CHOSEN)\n")
    print(f"  {'field':18} {'cpp':>10} {'python':>10}")
    for name, field in MAP.items():
        if field not in got:
            continue
        exp = getattr(want, field)
        if field == "risk_mode":                 # "budget" -> 1, "fixed" -> 0
            exp = 1.0 if exp == "budget" else 0.0
        ok = abs(got[field] - float(exp)) < 1e-6
        bad += not ok
        print(f"  {field:18} {got[field]:>10g} {float(exp):>10g}  {'' if ok else '<-- MISMATCH'}")

    print()
    if bad:
        print(f"FAIL: {bad} field(s) differ")
        return 1
    print("PASS: cpp defaults match the backtested configuration")
    print(f"  derived 1R = ${TF.risk_unit(want):.2f}, "
          f"giveback = ${TF.effective_giveback(want):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
