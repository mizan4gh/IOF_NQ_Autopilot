#!/usr/bin/env python3
"""Candidate-fire labeler — captures EVERY armed (mode, side) via _on_any_arm,
forward-simulates the production stop/T1/T2 path over N bars, and writes one
labeled row per candidate for ML training.

Multiplies the gated-fire dataset by ~5-10x (also captures ones killed by
priority cascade, regime filter, RM floor, cooldown, qual floor).

Outcome semantics (first touch wins):
  - 'T1'  : favorable excursion >= T1 distance before stop
  - 'STOP': adverse excursion >= stop distance before T1
  - 'TIME': neither hit within WIN bars (flat exit at last close)

Label `win` = pnl > 0 (T1 = win, STOP = loss, TIME = sign(pnl)).
Pnl is in dollars: pts × PT_VAL × 1 lot.

Usage: python xgb_candidate_label.py <scid> [out.csv]
"""
import os, sys, csv
import backtest as bt

OUT_DEFAULT_PREFIX = "candidates_"
WIN = 40   # forward bars to evaluate each candidate

candidates = []  # list of dict rows

def stop_target(sel: int, sl: bool, ep: float, atr: float, ctx: dict, sc_l: int, sc_s: int):
    """Mirror _enter's stop/T1/T2 calc, accounting for M6 / M8-fade special cases."""
    score = sc_l if sl else sc_s
    if sel == 5 and ctx["m6_sp"]:                                # M6 has mode-specific sp/t1/t2
        return ctx["m6_sp"], ctx["m6_t1"], ctx["m6_t2"]
    if sel == 7 and ctx["fade_active"]:                          # M8 fade
        return ctx["fd_sp"], ctx["fd_t1"], ctx["fd_t2"]
    stop_cl = bt.C_STOP_CL
    if bt.STOP_MODEL == "wide_for_hiconv" and (score >= 7 or sel == 1):
        stop_cl = bt.C_STOP_CL_HICONV
    sd = max(bt.C_STOP_FL, min(stop_cl, atr * bt.C_STOP_ATR))
    t1 = max(bt.C_T1_FL,   min(bt.C_T1_CL,   atr * bt.C_T1_ATR))
    t2 = max(bt.C_T2_FL,   min(bt.C_T2_CL,   atr * bt.C_T2_ATR))
    if sl:
        sp = round((ep - sd) / bt.TICK) * bt.TICK
        t1p = round((ep + t1) / bt.TICK) * bt.TICK
        t2p = round((ep + t2) / bt.TICK) * bt.TICK
    else:
        sp = round((ep + sd) / bt.TICK) * bt.TICK
        t1p = round((ep - t1) / bt.TICK) * bt.TICK
        t2p = round((ep - t2) / bt.TICK) * bt.TICK
    # sanity (mirror _enter lines 1591-1597)
    min_sd = max(atr * 0.5, bt.C_STOP_FL)
    if sl:
        if sp >= ep - bt.TICK: sp = round((ep - min_sd) / bt.TICK) * bt.TICK
        if t1p <= ep + bt.TICK: t1p = round((ep + max(atr, bt.C_T1_FL)) / bt.TICK) * bt.TICK
    else:
        if sp <= ep + bt.TICK: sp = round((ep + min_sd) / bt.TICK) * bt.TICK
        if t1p >= ep - bt.TICK: t1p = round((ep - max(atr, bt.C_T1_FL)) / bt.TICK) * bt.TICK
    return sp, t1p, t2p

def final_sc(sel: int, sl: bool, sc_l: int, sc_s: int, ctrl: int, bk_vs: int) -> int:
    """Mirror line 1518-1524 quality-score composition per mode."""
    if sel <= 2:
        ctrl_signed = max(0, ctrl if sl else -ctrl)
        return (sc_l if sl else sc_s) + ctrl_signed
    if sel == 5:
        return bk_vs
    return sc_l if sl else sc_s

def simulate(bars, i: int, sl: bool, ep: float, sp: float, t1: float):
    """Forward-simulate first-touch outcome over WIN bars.
    Returns (outcome, exit_px, exit_bar, mae, mfe).
    """
    mae = 0.0; mfe = 0.0
    end = min(len(bars), i + 1 + WIN)
    for j in range(i + 1, end):
        bj = bars[j]
        if sl:
            adv = ep - bj.low
            fav = bj.high - ep
            stop_hit = bj.low <= sp
            t1_hit   = bj.high >= t1
        else:
            adv = bj.high - ep
            fav = ep - bj.low
            stop_hit = bj.high >= sp
            t1_hit   = bj.low  <= t1
        mae = max(mae, adv); mfe = max(mfe, fav)
        # Intra-bar ambiguity: if both hit on same bar, assume STOP first
        # (conservative — matches typical adverse-first scan in audits).
        if stop_hit: return "STOP", sp, j, mae, mfe
        if t1_hit:   return "T1",   t1, j, mae, mfe
    return "TIME", bars[end - 1].close if end > 0 else ep, end - 1, mae, mfe

def hook(self, i: int, ctx: dict):
    bar = ctx["bar"]; atr = ctx["atr"]
    # Loop over each armed (mode_idx, is_long); a single bar can arm multiple
    arm_table = [
        (0, ctx["m1l"], True), (0, ctx["m1s"], False),
        (1, ctx["m2l"], True), (1, ctx["m2s"], False),
        (2, ctx["m3l"], True), (2, ctx["m3s"], False),
        (3, ctx["m4l"], True), (3, ctx["m4s"], False),
        (5, ctx["m6l"], True), (5, ctx["m6s"], False),
        (7, ctx["m8l"], True), (7, ctx["m8s"], False),
    ]
    ep = bar.close  # ENTRY_ORD=0 (market) — matches default config
    for mi, armed, sl in arm_table:
        if not armed: continue
        score = final_sc(mi, sl, ctx["sc_l"], ctx["sc_s"], ctx["ctrl"], ctx["bk_vs"])
        sp, t1, t2 = stop_target(mi, sl, ep, atr, ctx, ctx["sc_l"], ctx["sc_s"])
        outcome, ex_px, ex_bar, mae, mfe = simulate(self.bars, i, sl, ep, sp, t1)
        pts = (ex_px - ep) if sl else (ep - ex_px)
        pnl = pts * bt.PT_VAL
        # Regime context (trend / chop / vol) — useful features
        tr, cr = self._regime(i, atr, ctx["vwap"])
        vr     = self._vol_reg(i, atr)
        candidates.append(dict(
            Date     = bar.dt.strftime("%Y-%m-%d"),
            Time     = bar.dt.strftime("%H:%M:%S"),
            HHMM     = bar.hhmm,
            Mode     = bt.MODE_NAMES[mi],
            Side     = "LONG" if sl else "SHORT",
            Entry    = round(ep, 2),
            SL       = round(sp, 2),
            TP1      = round(t1, 2),
            TP2      = round(t2, 2),
            Score    = score,
            CtrlScore= ctx["ctrl"],
            DivStr   = ctx["div_strength"],
            Delta    = float(bar.ask_vol - bar.bid_vol),
            BarSpeed = float(bar.volume),
            FadeEdge = ctx["fade_edge"],
            FadeType = ctx["fade_type"],
            RiskMult = round(self.inst_risk.rm, 3),
            TrendReg = tr,
            VolReg   = vr,
            ChopReg  = cr,
            ATR      = round(atr, 2),
            VwapDist = round(bar.close - ctx["vwap"], 2) if ctx["vwap"] > 0 else 0.0,
            HoldBars = ex_bar - i,
            Outcome  = outcome,
            ExitPx   = round(ex_px, 2),
            MAE      = round(mae, 2),
            MFE      = round(mfe, 2),
            PnL      = round(pnl, 2),
            Win      = int(pnl > 0),
        ))

def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <scid> [out.csv]"); sys.exit(1)
    scid = sys.argv[1]
    stem = os.path.splitext(os.path.basename(scid))[0]
    out  = sys.argv[2] if len(sys.argv) > 2 else \
           os.path.join(bt.BASE_DIR, f"{OUT_DEFAULT_PREFIX}{stem}.csv")

    # Install hook
    bt.Backtester._on_any_arm = hook

    # Run backtest (we ignore the regular trade CSV; we want candidates only)
    sys.argv = ["backtest.py", scid, os.path.join(bt.BASE_DIR, "tmp_candlabel.csv")]
    bt.main()

    if not candidates:
        print("\nNo candidates captured."); return

    # Write candidate CSV
    fields = list(candidates[0].keys())
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in candidates: w.writerow(c)

    # Summary
    n = len(candidates)
    wins = sum(1 for c in candidates if c["Win"])
    by_mode = {}
    by_outcome = {}
    for c in candidates:
        by_mode.setdefault(c["Mode"], []).append(c["PnL"])
        by_outcome[c["Outcome"]] = by_outcome.get(c["Outcome"], 0) + 1
    print(f"\n===== CANDIDATE LABEL  {stem}  WIN={WIN} bars =====")
    print(f"  Total candidates: {n}")
    print(f"  Wins: {wins} ({wins/n*100:.1f}%)  Losses: {n-wins}")
    print(f"  Outcomes: " + ", ".join(f"{k}:{v}" for k, v in sorted(by_outcome.items())))
    print(f"  By mode:")
    for m in sorted(by_mode):
        pn = by_mode[m]
        w = sum(1 for p in pn if p > 0)
        print(f"    {m:4s} n={len(pn):>4} WR={w/len(pn)*100:5.1f}% NetPnL=${sum(pn):>+10,.0f}")
    print(f"\n  CSV written to: {out}")

if __name__ == "__main__":
    main()
