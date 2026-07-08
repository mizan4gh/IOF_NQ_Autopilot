"""Instrumented run for the fade-day/trend-day classifier study.

Runs ONLY the trend arm (TREND_LONG on, prod config, M8 floor-60) per frozen
contract so every entry row carries the day-anchored features (DayExtLo,
DayExtHi, DayOS, DayVws). Segmentation happens offline in
regime_segment_day.py — no intervention here, measurement only.

Usage: python run_regday_instrument.py [NQZ25|NQM5|NQH6]   (default: all)
"""
import sys
from pathlib import Path

BASE = Path(__file__).parent
CONTRACTS = {
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
}
_CFG = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(),
    QUAL_FLOOR_M8=60, M8_FADE_TYPES={1, 2, 3, 4}, ENABLE_M5=False,
    TREND_LONG=True, TREND_FAIL_EXIT=False, TREND_MIN_DEPTH=0.0,
)


def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}"); continue
        if "backtest" in sys.modules:
            del sys.modules["backtest"]
        import backtest
        for k, v in _CFG.items():
            if not hasattr(backtest, k):
                raise AttributeError(f"backtest.py has no constant {k}")
            setattr(backtest, k, v)
        out = BASE / f"IOF_NQ_regday_{tag}.csv"
        sys.argv = ["backtest.py", str(scid), str(out)]
        print(f"\n===== regday :: {tag} =====")
        backtest.main()


if __name__ == "__main__":
    main()
