"""Where do AMD-continuation setups die?

The primary run took 4 trades in 387 sessions, which is a DETECTOR result, not
a strategy result -- a rule that fires 0.01 times a day cannot be validated or
falsified, only mis-stated.  This walks the same session loop as
backtest_amd_continuation.scan() and counts the survivors at each stage, so the
frequency collapse is attributed to a specific clause instead of guessed at.

Usage: python amd_funnel.py [--nq|--es|...]
"""
from __future__ import annotations

import sys
from collections import Counter

import numpy as np

from fastbars import load_bars_cached
from backtest_mizan_iof_nq import BAR_MINUTES, arrays, atr_wilders
from backtest_mizan_p3 import sessions_cached
from backtest_amd_continuation import (Params, apply_spec, contracts_for,
                                       params_from_env, profile_of)


def funnel(tag, scid, p: Params) -> Counter:
    p = apply_spec(p, tag)
    bars = load_bars_cached(tag, scid, BAR_MINUTES)
    a = arrays(bars)
    S = sessions_cached(bars, p)
    atr = atr_wilders(a, p.atr_len)
    rb = a.h - a.l
    with np.errstate(divide="ignore", invalid="ignore"):
        cpos = np.where(rb > 0, (a.c - a.l) / rb, 0.0)
    c = Counter()

    for k in range(len(S.starts)):
        g0, on_end, rth_end = int(S.starts[k]), int(S.on_end[k]), int(S.rth_end[k])
        if on_end - g0 < p.min_on_bars or rth_end <= on_end:
            continue
        A = float(atr[on_end - 1])
        if not np.isfinite(A) or A <= 0:
            continue
        c["sessions"] += 1

        w = np.flatnonzero(a.hhmm[on_end:rth_end] < p.acc_end)
        if len(w) < 2:
            continue
        w0, w1 = on_end, on_end + int(w[-1]) + 1
        if w1 >= rth_end:
            continue
        chi, clo = float(a.h[w0:w1].max()), float(a.l[w0:w1].min())
        if chi - clo <= 0:
            continue
        c["window"] += 1
        poc, vah, val = profile_of(a, w0, w1, p.bin_pts, p.va_pct)

        eps_s, eps_b = p.sweep_eps_atr * A, p.brk_eps_atr * A

        # -- stage M, with the cause of death recorded -----------------------
        m, side, died, kind = -1, 0, "", ""
        want_wick = p.manip_mode in ("wick", "either")
        want_rcl = p.manip_mode in ("reclaim", "either")
        for j in range(w1, rth_end):
            if a.hhmm[j] > p.manip_end:
                died = "M_timeout"
                break
            if want_wick:
                lo_sw = (a.l[j] < clo - eps_s and a.c[j] > clo
                         and cpos[j] >= p.sweep_close_pos)
                hi_sw = (a.h[j] > chi + eps_s and a.c[j] < chi
                         and (1.0 - cpos[j]) >= p.sweep_close_pos)
                if lo_sw or hi_sw:
                    m, side, kind = j, (1 if lo_sw else -1), "wick"
                    break
            broke = (-1 if a.c[j] < clo - eps_b else
                     1 if a.c[j] > chi + eps_b else 0)
            if broke == 0:
                continue
            if not want_rcl:
                died = "M_clean_break_first"
                break
            r = -1
            for q in range(j + 1, min(j + 1 + p.manip_reclaim_bars, rth_end)):
                if (a.c[q] > clo) if broke < 0 else (a.c[q] < chi):
                    r = q
                    break
            if r < 0:
                died = "M_break_never_reclaimed"
                break
            m, side, kind = r, -broke, "reclaim"
            break
        if m < 0:
            c[died or "M_none"] += 1
            continue
        c["manipulation"] += 1
        c[f"M_via_{kind}"] += 1
        c[f"manip_{'lo' if side > 0 else 'hi'}"] += 1

        # how far into the session was the trap?
        c[f"manip_hhmm_{int(a.hhmm[m]) // 100:02d}xx"] += 1

        b, died = -1, ""
        for j in range(m + 1, min(m + 1 + p.brk_bars, rth_end)):
            if a.hhmm[j] > p.brk_end:
                died = "C_timeout"
                break
            if (a.c[j] < clo - eps_b) if side > 0 else (a.c[j] > chi + eps_b):
                died = "C_trap_was_real"      # the swept edge gave way for real
                break
            if (a.c[j] > chi + eps_b) if side > 0 else (a.c[j] < clo - eps_b):
                b = j
                break
        if b < 0:
            c[died or "C_no_break_in_window"] += 1
            continue
        c["continuation"] += 1
        c[f"brk_wait_{min(b - m, 12)}"] += 1

        # -- stage E, per level, so the fill rate is attributed to the level --
        for name, lvl0 in (("poc", poc), ("va", vah if side > 0 else val),
                           ("mid", 0.5 * (chi + clo)),
                           ("edge", chi if side > 0 else clo)):
            lv = lvl0 + side * p.lvl_tol_atr * A
            thru = p.require_through * p.tick
            hit = False
            for j in range(b + 1, min(b + 1 + p.pb_bars, rth_end)):
                if a.hhmm[j] > p.session_end:
                    break
                if (a.c[j] < clo) if side > 0 else (a.c[j] > chi):
                    c[f"E_{name}_cancel"] += 1
                    break
                if (a.l[j] <= lv - thru) if side > 0 else (a.h[j] >= lv + thru):
                    hit = True
                    break
            if hit:
                c[f"E_{name}_FILL"] += 1
        # how deep a retrace the POC actually is, in units of the break
        c["depth_poc_x_rng"] += (chi - poc) / (chi - clo) if side > 0 \
            else (poc - clo) / (chi - clo)
    return c


def main():
    scope = next((a for a in sys.argv[1:] if a.startswith("--")), "--nq")
    p = params_from_env()
    tot = Counter()
    for tag, scid in contracts_for(scope).items():
        if not scid.exists():
            continue
        c = funnel(tag, scid, p)
        tot.update(c)
        print(f"  {tag:7s} sess={c['sessions']:>4} win={c['window']:>4} "
              f"manip={c['manipulation']:>4} cont={c['continuation']:>4} "
              f"fill(poc)={c['E_poc_FILL']:>3} fill(va)={c['E_va_FILL']:>3} "
              f"fill(edge)={c['E_edge_FILL']:>3}")

    print("\n  -- pooled funnel " + "-" * 40)
    s = tot["window"] or 1
    for kk in ("sessions", "window", "M_clean_break_first",
               "M_break_never_reclaimed", "M_timeout",
               "M_none", "manipulation", "M_via_wick", "M_via_reclaim",
               "manip_lo", "manip_hi",
               "C_trap_was_real", "C_no_break_in_window", "C_timeout",
               "continuation", "E_poc_FILL", "E_va_FILL", "E_mid_FILL",
               "E_edge_FILL", "E_poc_cancel", "E_va_cancel"):
        if tot[kk]:
            print(f"    {kk:<24} {tot[kk]:>6}   {100.0 * tot[kk] / s:>5.1f}% "
                  f"of windows")
    if tot["continuation"]:
        print(f"\n    mean POC retrace depth: "
              f"{tot['depth_poc_x_rng'] / tot['continuation']:.2f} x the "
              f"accumulation range, measured from the broken edge")
    print("\n  trap arrival time:")
    for kk in sorted(k for k in tot if k.startswith("manip_hhmm_")):
        print(f"    {kk[11:]}  {tot[kk]:>5}")
    print("\n  bars from trap to continuation break:")
    for kk in sorted((k for k in tot if k.startswith("brk_wait_")),
                     key=lambda x: int(x.split("_")[-1])):
        print(f"    {kk[9:]:>3}  {tot[kk]:>5}")


if __name__ == "__main__":
    main()
