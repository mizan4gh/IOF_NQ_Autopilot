#!/usr/bin/env python3
"""
RM-gated SETUP audit.

When inst_risk.rm < C_RM_FLOOR kills a SETUP, capture the setup and simulate
its hypothetical outcome by walking forward bars with the same management
logic as Backtester._manage. Reports whether the RM gate is systematically
killing winners (over-tight) or losers (correctly tight).

Usage:
    python rm_gated_audit.py <input.txt> [out.csv]
"""
import os, sys, csv
from typing import List, Optional, Dict

from backtest import (
    Backtester, Bar, MODE_NAMES,
    C_STOP_FL, C_STOP_CL, C_STOP_CL_HICONV,
    C_T1_FL, C_T1_CL, C_T2_FL, C_T2_CL,
    C_STOP_ATR, C_T1_ATR, C_T2_ATR, C_BE_ATR,
    C_TRAIL_ATR, C_TRAIL_DLY,
    TICK, PT_VAL, COMMISSION,
    FLATTEN_HHMM, ENTRY_ORD, STOP_MODEL, BASE_DIR,
    read_scid, build_volume_bars, detect_price_scale,
)
from backtest_csv import load_bars as load_bars_csv


def load_any(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".txt", ".csv"):
        return load_bars_csv(path)
    if ext == ".scid":
        scale = detect_price_scale(path)
        recs  = read_scid(path)
        return build_volume_bars(recs, price_scale=scale)
    raise ValueError(f"Unsupported input extension: {ext}")


def _build_levels(sel: int, is_long: bool, entry_px: float, atr: float,
                  final_sc: int,
                  m6_sp: float, m6_t1: float, m6_t2: float,
                  fade_active: bool, fd_sp: float, fd_t1: float, fd_t2: float):
    """Mirror Backtester._enter stop/target derivation."""
    if sel == 5 and m6_sp:
        return m6_sp, m6_t1, m6_t2
    if sel == 7 and fade_active:
        return fd_sp, fd_t1, fd_t2

    stop_cl = C_STOP_CL
    if STOP_MODEL == "wide_for_hiconv" and (final_sc >= 7 or sel == 1):
        stop_cl = C_STOP_CL_HICONV
    sd = max(C_STOP_FL, min(stop_cl, atr * C_STOP_ATR))
    t1 = max(C_T1_FL,   min(C_T1_CL,   atr * C_T1_ATR))
    t2 = max(C_T2_FL,   min(C_T2_CL,   atr * C_T2_ATR))
    if is_long:
        sp  = round((entry_px - sd) / TICK) * TICK
        tp1 = round((entry_px + t1) / TICK) * TICK
        tp2 = round((entry_px + t2) / TICK) * TICK
    else:
        sp  = round((entry_px + sd) / TICK) * TICK
        tp1 = round((entry_px - t1) / TICK) * TICK
        tp2 = round((entry_px - t2) / TICK) * TICK
    return sp, tp1, tp2


def _entry_px(bar: Bar, is_long: bool) -> float:
    """Mirror Backtester._enter entry fill — ENTRY_ORD 0/1/2.
       For ENTRY_ORD=1 (passive limit) we'd need next-bar lookahead and could
       fail to fill; in audit we treat that as 'no fill', returning None."""
    if ENTRY_ORD == 0:
        return bar.close
    if ENTRY_ORD == 2:
        return bar.close + (TICK * 2.0 if is_long else -TICK * 2.0)
    return bar.close  # ENTRY_ORD=1 handled by caller via lookahead


def _simulate(bars: List[Bar], atr_v: List[float], entry_idx: int,
              is_long: bool, entry_px: float,
              stop_px: float, tp1_px: float, tp2_px: float) -> dict:
    """Standalone trade simulator — mirrors Backtester._manage minus state."""
    n = len(bars)
    mae = mfe = 0.0
    t1_hit = False
    t1_bar = -1
    cur_stop = stop_px

    def result(exit_px, reason, hold):
        pnl = ((exit_px - entry_px) if is_long else (entry_px - exit_px)) * PT_VAL - COMMISSION
        return {"exit_px": round(exit_px, 2), "exit_reason": reason,
                "hold": hold, "mae": round(mae, 2), "mfe": round(mfe, 2),
                "pnl": round(pnl, 2)}

    for i in range(entry_idx + 1, n):
        bar = bars[i]
        atr = atr_v[i] if i < len(atr_v) else (atr_v[-1] if atr_v else 0.0)

        adv = entry_px - bar.low  if is_long else bar.high - entry_px
        fav = bar.high - entry_px if is_long else entry_px - bar.low
        if adv > mae: mae = adv
        if fav > mfe: mfe = fav

        # End-of-day flatten
        if bar.hhmm >= FLATTEN_HHMM:
            return result(bar.close, "FLATTEN", i - entry_idx)

        # Circuit-breaker
        op = (bar.close - entry_px) if is_long else (entry_px - bar.close)
        max_risk = max(abs(entry_px - stop_px) * 3, atr * 3) * PT_VAL
        if op * PT_VAL < -max_risk:
            return result(bar.close, "CB", i - entry_idx)

        # T2 (only checked before T1)
        if not t1_hit:
            if (is_long and bar.high >= tp2_px) or (not is_long and bar.low <= tp2_px):
                return result(tp2_px, "T2", i - entry_idx)

        # T1
        if not t1_hit:
            if (is_long and bar.high >= tp1_px) or (not is_long and bar.low <= tp1_px):
                t1_hit = True
                t1_bar = i
                buf = atr * C_BE_ATR
                cur_stop = (round((entry_px + buf) / TICK) * TICK if is_long
                            else round((entry_px - buf) / TICK) * TICK)

        # Trail
        if t1_hit:
            delay = C_TRAIL_DLY * 3
            if t1_bar >= 0 and i >= t1_bar + delay:
                t1d = abs(tp1_px - entry_px)
                cur = (bar.close - entry_px) if is_long else (entry_px - bar.close)
                bt  = C_TRAIL_ATR * 2.5 if (t1d > 0 and cur < t1d * 2) else C_TRAIL_ATR
                td  = atr * bt
                if t1d > 0 and cur > t1d * 2: td = min(td, atr * 0.75)
                min_sp = (round((entry_px + atr * 0.3) / TICK) * TICK if is_long
                          else round((entry_px - atr * 0.3) / TICK) * TICK)
                if is_long:
                    ns = round((bar.close - td) / TICK) * TICK
                    cur_stop = max(max(cur_stop, ns), min_sp)
                else:
                    ns = round((bar.close + td) / TICK) * TICK
                    cur_stop = min(min(cur_stop, ns), min_sp)
            if i > entry_idx + 3:
                hit = ((is_long and bar.low  <= cur_stop + TICK) or
                       (not is_long and bar.high >= cur_stop - TICK))
                if hit:
                    return result(cur_stop, "TRAIL", i - entry_idx)

        # Pre-T1 stop
        if not t1_hit and i > entry_idx + 3:
            hit = ((is_long and bar.low  <= cur_stop + TICK) or
                   (not is_long and bar.high >= cur_stop - TICK))
            if hit:
                return result(cur_stop, "STOP", i - entry_idx)

    last = bars[-1]
    return result(last.close, "EOD", n - 1 - entry_idx)


class AuditBacktester(Backtester):
    def __init__(self, bars):
        super().__init__(bars)
        self.gated: List[dict] = []

    def _on_rm_gated(self, i, sel, is_long, atr, ctrl, div, sc_l, sc_s, bk_vs,
                     m6_sp, m6_t1, m6_t2,
                     fade_active, fade_edge, fade_type, fd_sp, fd_t1, fd_t2):
        if sel < 0 or atr <= 0:
            return
        bar = self.bars[i]
        if sel == 5:
            final_sc = bk_vs
        elif sel <= 2:
            ctrl_signed = max(0, ctrl if is_long else -ctrl)
            final_sc = (sc_l if is_long else sc_s) + ctrl_signed
        else:
            final_sc = sc_l if is_long else sc_s

        entry_px = _entry_px(bar, is_long)
        sp, tp1, tp2 = _build_levels(sel, is_long, entry_px, atr, final_sc,
                                     m6_sp, m6_t1, m6_t2,
                                     fade_active, fd_sp, fd_t1, fd_t2)
        out = _simulate(self.bars, self.atr_v, i, is_long,
                        entry_px, sp, tp1, tp2)

        self.gated.append({
            "bar_idx": i,
            "date":    bar.dt.strftime("%Y-%m-%d"),
            "time":    bar.dt.strftime("%H:%M:%S"),
            "mode":    MODE_NAMES[sel] if 0 <= sel < len(MODE_NAMES) else f"sel{sel}",
            "side":    "LONG" if is_long else "SHORT",
            "entry":   round(entry_px, 2),
            "sl":      round(sp, 2),
            "tp1":     round(tp1, 2),
            "tp2":     round(tp2, 2),
            "atr":     round(atr, 2),
            "rm_at_gate": round(self.inst_risk.rm, 3),
            "score":   final_sc,
            "ctrl":    ctrl,
            "div_str": div.strength,
            **out,
        })


def write_csv(rows: List[dict], path: str):
    if not rows:
        return
    fields = ["bar_idx","date","time","mode","side","entry","sl","tp1","tp2",
              "atr","rm_at_gate","score","ctrl","div_str",
              "exit_px","exit_reason","hold","mae","mfe","pnl"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def summarise(rows: List[dict]):
    n = len(rows)
    if n == 0:
        print("  No RM-gated setups.")
        return
    wins   = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    net    = sum(r["pnl"] for r in rows)
    avg_w  = sum(r["pnl"] for r in wins)   / len(wins)   if wins   else 0.0
    avg_l  = sum(r["pnl"] for r in losses) / len(losses) if losses else 0.0
    gross_w = sum(r["pnl"] for r in wins)
    gross_l = sum(r["pnl"] for r in losses)
    pf      = (gross_w / abs(gross_l)) if gross_l != 0 else float("inf")
    print("\n" + "=" * 62)
    print("  RM-GATED SHADOW SUMMARY  (would-have-traded outcomes)")
    print("=" * 62)
    print(f"  Setups gated   : {n:,}")
    print(f"  Win rate       : {len(wins)/n:.1%}  ({len(wins):,}W / {len(losses):,}L)")
    print(f"  Net P&L (unit) : ${net:,.2f}")
    print(f"  Avg win        : ${avg_w:,.2f}")
    print(f"  Avg loss       : ${avg_l:,.2f}")
    print(f"  Profit factor  : {pf:.2f}")
    print(f"  Expectancy     : ${net/n:,.2f} per gated setup")
    print("-" * 62)

    # Per-mode
    by_mode: Dict[str, list] = {}
    for r in rows:
        by_mode.setdefault(r["mode"], []).append(r["pnl"])
    print(f"  {'Mode':<6} {'N':>6} {'WR':>7} {'Net P&L':>12} {'AvgPnL':>10}")
    for m in sorted(by_mode):
        ps = by_mode[m]
        w  = sum(1 for p in ps if p > 0)
        n_ = len(ps)
        net_ = sum(ps)
        print(f"  {m:<6} {n_:>6} {w/n_:>6.1%} {net_:>12,.2f} {net_/n_:>10,.2f}")
    print("-" * 62)

    # By exit reason
    by_rsn: Dict[str, int] = {}
    for r in rows:
        by_rsn[r["exit_reason"]] = by_rsn.get(r["exit_reason"], 0) + 1
    print("  Exit reasons   :", ", ".join(f"{k}:{v}" for k, v in sorted(by_rsn.items())))
    print("=" * 62)


def main():
    if len(sys.argv) < 2:
        print("usage: python rm_gated_audit.py <input.txt> [out.csv]")
        sys.exit(1)
    src = sys.argv[1]
    stem = os.path.splitext(os.path.basename(src))[0]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE_DIR, f"rm_gated_audit_{stem}.csv")

    print(f"Loading bars from {src}")
    bars = load_any(src)
    if not bars:
        print("ERROR: no bars parsed."); sys.exit(1)
    print(f"  {len(bars):,} bars  |  {bars[0].dt.date()} -> {bars[-1].dt.date()}")

    print("Running auditing backtest ...")
    bt = AuditBacktester(bars)
    trades = bt.run()
    real_exits = sum(1 for t in trades if t.event == "EXIT")
    print(f"  Real trades : {real_exits}")
    print(f"  RM-gated    : {len(bt.gated)}  (matches bt.rm_gated={bt.rm_gated})")

    write_csv(bt.gated, out)
    summarise(bt.gated)
    print(f"\nShadow CSV written to: {out}")


if __name__ == "__main__":
    main()
