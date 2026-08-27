"""
Consolidation -> breakout -> pullback-to-POC CONTINUATION.

THE PATTERN (long case; short is the exact mirror)
  1. A 30-minute consolidation is profiled. Its POC, high and low are frozen
     the moment the window ends and never update.
  2. Price BREAKS OUT of the window: a bar closes above the window high.
  3. Price PULLS BACK to the frozen POC.
  4. Enter LONG at the POC, with the breakout. Stop the far side of the
     window, target target_r x that distance.

WHY THIS IS NOT ALREADY FALSIFIED
  POC-pullback entries have been measured three times in this repo and none
  survived intact:

    backtest_mizan_iof_nq.py   rolling 12-bar range + rolling POC   37-75th
    backtest_mizan_avpmd.py    AMD + POC pullback                   placebo fail
    backtest_mizan_p3.py       poc_now: frozen overnight POC        99.5th, but
                               the placebo does NOT collapse (96.5 / 85.0)

  Every one of those enters at the POC as a FADE -- price sweeps a level or
  reclaims it and the POC is where the reversion is bought. This file enters
  at the POC as a CONTINUATION: the direction is set by a range BREAK, and the
  POC is a retracement inside a move that has already picked its side. That is
  a different claim about what the POC does, and it is the axis none of the
  three covered. It is worth exactly one honest test.

  The prior is still bad. Read the placebo section before the P/L.

THE WINDOW -- consol_mode
  "clock"     09:30-10:00. The first 30 minutes of RTH, every session, no
              shape test. Frozen at 10:00. One window per day, which makes it
              directly comparable to the P3 frozen-level work.
  "compress"  the first window of consol_bars bars whose range is narrower
              than consol_max_rng_atr x ATR, searched from the RTH open to
              consol_end. A consolidation is a shape, not a clock -- but the
              threshold is a knob, and a knob is a place to overfit. Both modes
              are measured; neither is privileged.

  ATR is Wilders(14) -- atr_wilders(), which is what sc.ATR(MOVAVGTYPE_WILDERS)
  computes in the live cpp -- frozen on the last bar before 09:30. Every
  threshold in the file is an ATR multiple. The index ran 20k -> 30k across these six contracts and a
  points-denominated gate silently selects a biased subsample when it does.

THE PLACEBO -- run it before believing any number below
  The claim this file makes is not "price retraces after a breakout". It is
  "price retraces TO THE POC", i.e. to the price where the consolidation
  actually traded its volume. Two ways to break that claim while leaving the
  mechanic, the stop, the target and roughly the trade count alone:

    poc_mode="mid"        replace the volume POC with the geometric midpoint
                          of the same window. Same window, same range, same
                          stop, a line in the same neighbourhood -- only the
                          volume information is gone.
    placebo_shift=K       move the POC K x the window range AGAINST the
                          breakout direction, the P3 convention.

  If "mid" scores what "volume" scores, the profile is decoration and this is
  a retracement rule wearing a POC costume.

RESULTS (2026-08-26, 6 frozen NQ contracts, 387 sessions, $20/pt, $5 RT)
  NO SHIP. There is no edge here to placebo-test in the first place.

    config                        n      net    $/trade    t    gate
    clock     POC=volume        116  +$10,530     +$91   +0.55   3/6
    clock     POC=mid    [plac] 104   +$7,470     +$72   +0.39   4/6
    clock     shift 0.20 [plac]  66   +$2,400     +$36   +0.20   3/6
    compress  POC=volume        110   +$1,330     +$12   +0.12   3/6
    compress  POC=mid    [plac] 109  -$12,645    -$116   -1.23   2/6
    compress  shift 0.20 [plac]  82     +$600      +$7   +0.08   3/6

  Read the t column first. +0.55 and +0.12 are noise -- this harness's se is
  ~$110-170 a trade at these sample sizes, so nothing here is distinguishable
  from zero. No null test was run because there is no positive result to null.

  The gate says the same thing. No cell reaches better than 3/6 contracts
  net>0 AND PF>1, against 5/6 for the P3 frozen-level sweep, and no single
  contract is individually significant in either direction.

  The placebo does not discriminate, because there is nothing to discriminate.
  Replacing the volume POC with the geometric MIDPOINT of the same window --
  deleting every bit of volume information while keeping the window, the range,
  the breakout test and the stop -- costs $3,060 on clock and IMPROVES the
  gate, 3/6 to 4/6. Whatever little is happening is "price retraces into a
  range after leaving it", and the profile is decoration. Same disease that
  killed poc_now and AVPMD.

  compress's threshold was calibrated on TRADE COUNT ALONE and P/L was never
  read during that step: at the 1.25xATR default it takes 6 trades in 387
  sessions, so the 6-bar range cap was walked out to 2.75xATR, which yields
  110 setups against clock's 116. Not a tuned number.

  HARNESS BUG, FOUND AND FIXED AFTER THE FIRST RUN (kept here as a warning)
  The first version of this file computed ATR as a simple average of true
  range instead of calling the repo's atr_wilders(). Wilders is what
  sc.ATR(MOVAVGTYPE_WILDERS) computes in the live cpp, and the two differ by
  ~22% -- 32.61 against 26.66 on NQU6 2026-08-26. Since EVERY threshold in
  this file is an ATR multiple, that inflated the breakout epsilon, the POC
  tolerance, the stop buffer, both stop clamps and the compression cap all at
  once. The pre-fix numbers were clock +$17,350 t=+0.87 and compress +$13,770
  t=+0.92; corrected they are +$10,530 t=+0.55 and +$1,330 t=+0.12. The
  verdict did not change, but it would have been reported ~65% too generous.
  Do not hand-roll an indicator that the repo already has.

  WHAT WOULD AND WOULD NOT BE A LEGITIMATE NEXT STEP
  Not legitimate: sweeping brk_end, pb_bars, target_r, or the stop placement
  until a cell passes. Four knobs against six contracts at t<1 will always
  produce a winner and it will always be noise.
  Legitimate: checking that this detector actually fires where a human would
  draw the pattern. If the specific sessions the reference charts came from are
  known, replay them and confirm the window, the break bar and the POC match
  what was drawn by eye. A specification bug is the one failure mode the
  numbers above cannot rule out.

NO LOOK-AHEAD
  ATR frozen pre-09:30. The window is frozen before the breakout test runs.
  The breakout is tested on CLOSED bars. The limit fill takes min(open, limit)
  for a long so a gap through the level does not book a better price than the
  open, and it is managed from its OWN bar -- price reached the limit somewhere
  inside that bar and the rest of it can still reach the stop. Managing from
  the next bar instead deletes the first bar of adverse excursion from every
  trade and was worth $6.4k of fiction in the P3 file.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from backtest import Bar
from fastbars import load_bars_cached
from backtest_mizan_iof_nq import (BAR_MINUTES, ES_FROZEN, MNQ, NQ_FROZEN,
                                   Cands, arrays, atr_wilders, poc_of,
                                   simulate, summarize, write_csv)
from backtest_mizan_p3 import (CL_FROZEN, GC_FROZEN, IS_TAGS, OOS_TAGS, SPECS,
                               instrument_of, sessions_cached, _rt)

BASE = Path(__file__).parent


@dataclass(frozen=True)
class Params:
    # -- session geometry --------------------------------------------------
    on_start: int = 1800
    rth_open: int = 930
    rth_close: int = 1600
    session_end: int = 1530       # no fill after this
    flatten_hhmm: int = 1555
    min_on_bars: int = 60         # ATR needs a real overnight behind it
    # -- the consolidation window ------------------------------------------
    consol_mode: str = "clock"    # "clock" | "compress"
    consol_end: int = 1000        # clock: window ends here.  compress: last
                                  # hhmm a qualifying window may end at
    consol_bars: int = 6          # compress: window length (6 x 5m = 30 min)
    consol_max_rng_atr: float = 1.25   # compress: range must be under this
    # -- the breakout ------------------------------------------------------
    brk_eps_atr: float = 0.05     # close must clear the edge by this
    brk_end: int = 1200           # the break must happen by here
    # -- the pullback entry ------------------------------------------------
    pb_bars: int = 12             # limit armed this many bars after the break
    poc_tol_atr: float = 0.10     # limit sits this far toward price from POC
    require_through: float = 1.0  # ticks price must trade THROUGH to fill
    bin_pts: float = 1.0
    # -- placebo -----------------------------------------------------------
    poc_mode: str = "volume"      # "volume" | "mid"
    placebo_shift: float = 0.0    # x window range, AGAINST the break
    # -- stop / target -----------------------------------------------------
    stop_buf_atr: float = 0.15
    min_stop_atr: float = 0.40
    max_stop_atr: float = 2.50
    target_r: float = 2.0
    max_hold_bars: int = 0
    atr_len: int = 14
    # -- contract spec -----------------------------------------------------
    tick: float = 0.25
    # -- sizing / governors (simulate() reads these by name) ---------------
    qty: int = 1
    pt_val: float = 20.0
    commission: float = 5.00
    slip_ticks: float = 0.0
    daily_loss: float = 0.0
    daily_target: float = 0.0
    max_trades_day: int = 0
    max_consec_loss: int = 0
    session_start: int = 930

    @property
    def sig_key(self) -> tuple:
        return tuple(getattr(self, f) for f in SIG_FIELDS)


SIG_FIELDS = tuple(f.name for f in fields(Params)
                   if f.name not in ("qty", "pt_val", "commission",
                                     "slip_ticks", "daily_loss", "daily_target",
                                     "max_trades_day", "max_consec_loss"))

_ENV_ALIASES = {f.name: "CP_" + f.name.upper() for f in fields(Params)}


def params_from_env(base: Optional[Params] = None) -> Params:
    p = base or Params()
    kw = {}
    for name, env in _ENV_ALIASES.items():
        v = os.environ.get(env)
        if v is None:
            continue
        t = type(getattr(p, name))
        kw[name] = v if t is str else t(float(v))
    return replace(p, **kw) if kw else p


RUN_TAG = os.environ.get("CP_TAG", "")

_SCAN_CACHE: Dict[Tuple[int, tuple], Tuple[List[Bar], Cands]] = {}


def _empty() -> Cands:
    e = np.array([], dtype=np.int64)
    f = np.array([], dtype=np.float64)
    return Cands(e, e, f, f, f, f, f, f, e, e, 0)


def scan(bars: List[Bar], p: Params) -> Cands:
    key = (id(bars), p.sig_key)
    hit = _SCAN_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]

    a = arrays(bars)
    S = sessions_cached(bars, p)
    atr = atr_wilders(a, p.atr_len)
    out = []

    for k in range(len(S.starts)):
        g0, on_end = int(S.starts[k]), int(S.on_end[k])
        rth_end = int(S.rth_end[k])
        if on_end - g0 < p.min_on_bars or rth_end <= on_end:
            continue
        A = float(atr[on_end - 1])              # frozen before 09:30
        if not np.isfinite(A) or A <= 0:
            continue

        # -- the consolidation window, frozen once it ends -----------------
        if p.consol_mode == "clock":
            w = np.flatnonzero(a.hhmm[on_end:rth_end] < p.consol_end)
            if len(w) < 2:
                continue
            w0, w1 = on_end, on_end + int(w[-1]) + 1     # [w0, w1)
        else:
            w0, w1 = -1, -1
            nb = p.consol_bars
            for j in range(on_end, rth_end - nb + 1):
                if a.hhmm[j + nb - 1] > p.consol_end:
                    break
                if (a.h[j:j + nb].max() - a.l[j:j + nb].min()) \
                        < p.consol_max_rng_atr * A:
                    w0, w1 = j, j + nb
                    break
            if w0 < 0:
                continue
        if w1 >= rth_end:
            continue

        chi = float(a.h[w0:w1].max())
        clo = float(a.l[w0:w1].min())
        rng = chi - clo
        if rng <= 0:
            continue
        if p.poc_mode == "mid":
            poc = 0.5 * (chi + clo)
        else:
            poc = poc_of(a, w0, w1, p.bin_pts)
        if not np.isfinite(poc):
            continue

        # -- the breakout: first CLOSE clear of an edge --------------------
        eps = p.brk_eps_atr * A
        b, side = -1, 0
        for j in range(w1, rth_end):
            if a.hhmm[j] > p.brk_end:
                break
            if a.c[j] > chi + eps:
                b, side = j, 1
                break
            if a.c[j] < clo - eps:
                b, side = j, -1
                break
        if b < 0:
            continue

        # PLACEBO: move the POC against the break, leaving everything else
        lv_poc = poc - side * p.placebo_shift * rng
        lv = float(_rt(lv_poc + side * p.poc_tol_atr * A, p.tick))

        # -- the pullback: a limit at the POC, armed pb_bars bars ----------
        # Cancelled if price closes clean THROUGH the window the other way:
        # the breakout that set the direction is then gone, and holding the
        # order would be entering on a signal that no longer exists.
        e, fill = -1, 0.0
        thru = p.require_through * p.tick
        for j in range(b + 1, min(b + 1 + p.pb_bars, rth_end)):
            if a.hhmm[j] > p.session_end:
                break
            if (a.c[j] < clo) if side > 0 else (a.c[j] > chi):
                break
            if ((a.l[j] <= lv - thru) if side > 0 else (a.h[j] >= lv + thru)):
                e = j
                fill = min(a.o[j], lv) if side > 0 else max(a.o[j], lv)
                break
        if e < 0:
            continue

        # -- stop the far side of the window, target target_r x that -------
        invalid = clo if side > 0 else chi
        raw = abs(fill - invalid) + p.stop_buf_atr * A
        sp = float(_rt(np.clip(raw, p.min_stop_atr * A, p.max_stop_atr * A),
                       p.tick))
        if sp <= 0.0:
            continue
        tgt = float(_rt(sp * p.target_r, p.tick))

        out.append((e, side, float(fill), sp, tgt, A, poc, rng, b, e - b, e))

    if not out:
        c = _empty()
        _SCAN_CACHE[key] = (bars, c)
        return c

    out.sort(key=lambda r: r[0])
    col = list(zip(*out))
    c = Cands(idx=np.asarray(col[0], np.int64),
              side=np.asarray(col[1], np.int64),
              entry_px=np.asarray(col[2], np.float64),
              stop_pts=np.asarray(col[3], np.float64),
              tgt_pts=np.asarray(col[4], np.float64),
              atr=np.asarray(col[5], np.float64),
              poc=np.asarray(col[6], np.float64),
              acc_rng=np.asarray(col[7], np.float64),   # consolidation range
              sweep_idx=np.asarray(col[8], np.int64),   # the breakout bar
              wait=np.asarray(col[9], np.int64), warm=0,
              manage_at=np.asarray(col[10], np.int64))
    _SCAN_CACHE[key] = (bars, c)
    return c


def contracts_for(scope: str) -> Dict[str, Path]:
    everything = {**NQ_FROZEN, **MNQ, **ES_FROZEN, **CL_FROZEN, **GC_FROZEN}
    env = os.environ.get("CP_TAGS")
    if env:
        return {t: everything[t] for t in env.split(",") if t in everything}
    return {"--es": dict(ES_FROZEN), "--cl": dict(CL_FROZEN),
            "--gc": dict(GC_FROZEN), "--mnq": dict(MNQ),
            "--uncorr": {**CL_FROZEN, **GC_FROZEN},
            "--is": {t: everything[t] for t in IS_TAGS},
            "--oos": {t: everything[t] for t in OOS_TAGS},
            }.get(scope, dict(NQ_FROZEN))


def apply_spec(p: Params, tag: str) -> Params:
    return params_from_env(replace(p, **SPECS[instrument_of(tag)]))


def run_one(tag: str, scid: Path, p: Params, write: bool = True) -> dict:
    p = apply_spec(p, tag)
    bars = load_bars_cached(tag, scid, BAR_MINUTES)
    c = scan(bars, p)
    r = simulate(bars, c, p)
    if write:
        suffix = f"_{RUN_TAG}" if RUN_TAG else ""
        write_csv(r, BASE / f"IOF_consolPOC{suffix}_{tag}.csv")
    s = summarize(r, p)
    s["cands"] = len(c.idx)
    s["sessions"] = max(len(sessions_cached(bars, p).starts) - 1, 0)
    print(f"  {tag:7s} sess={s['sessions']:>4} cand={s['cands']:>4} "
          f"n={s['n']:>4} L/S={s['longs']}/{s['n']-s['longs']:<4} "
          f"WR={s['wr']:>5.1f}% PF={s['pf']:>5.2f} Net=${s['total']:>+9,.0f} "
          f"avg=${s['avg']:>+7,.0f} ({s['avg_r']:>+5.2f}R) "
          f"MaxDD=${s['max_dd']:>+9,.0f} t={s['t']:>+5.2f}")
    return s


def main():
    args = sys.argv[1:]
    scope = next((a for a in args if a.startswith("--")), "--nq")
    p = params_from_env()

    win = (f"clock {p.rth_open:04d}-{p.consol_end:04d}"
           if p.consol_mode == "clock" else
           f"first {p.consol_bars}-bar window under "
           f"{p.consol_max_rng_atr}xATR by {p.consol_end:04d}")
    print("Consolidation -> breakout -> pullback-to-POC CONTINUATION "
          f"({BAR_MINUTES}m)")
    print(f"  W: {win} -> frozen POC({p.poc_mode})/hi/lo + ATR")
    print(f"  B: close clear of an edge by >{p.brk_eps_atr}xATR, by {p.brk_end}")
    print(f"  E: limit at POC{p.poc_tol_atr:+.2f}xATR armed {p.pb_bars} bars | "
          f"stop far side{p.stop_buf_atr:+.2f}xATR "
          f"clamp[{p.min_stop_atr},{p.max_stop_atr}]xATR | tgt={p.target_r}R")
    if p.placebo_shift:
        print(f"  PLACEBO: POC moved {p.placebo_shift} x window range "
              f"AGAINST the break")
    print(f"  flat {p.flatten_hhmm} | ${p.pt_val:.0f}/pt "
          f"comm=${p.commission:.2f} slip={p.slip_ticks}t\n")

    out = {}
    for tag, scid in contracts_for(scope).items():
        if not scid.exists():
            print(f"  {tag:7s} MISSING {scid.name}")
            continue
        out[tag] = run_one(tag, scid, p)

    if len(out) > 1:
        tot = sum(s["total"] for s in out.values())
        nn = sum(s["n"] for s in out.values())
        sess = sum(s["sessions"] for s in out.values())
        pos = sum(1 for s in out.values() if s["total"] > 0)
        pf_ok = sum(1 for s in out.values() if s["pf"] > 1.0)
        print(f"\n  POOLED  n={nn} over {sess} sessions "
              f"({nn/max(sess,1):.2f}/day)  Net=${tot:+,.0f}  "
              f"net>0: {pos}/{len(out)}  PF>1: {pf_ok}/{len(out)}")
        print(f"  SHIP GATE (net>0 AND PF>1 on every contract): "
              f"{'PASS' if pos == len(out) and pf_ok == len(out) else 'FAIL'}")


if __name__ == "__main__":
    main()
