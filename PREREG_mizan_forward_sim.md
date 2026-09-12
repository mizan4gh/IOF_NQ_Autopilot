# PRE-REGISTRATION — Mizan_IOF_NQ mode 0, forward simulation on the VPS

**Registered:** 2026-09-12, before the study was compiled or run forward.
**Registered by:** session `01M3s8BYdB29bKzDMkhZxtfr`.

---

## 0. What this forward sim is FOR — read before starting

It is **not** a confirmation test, and it cannot be made into one. From the
backtest's own trade distribution (n=253, mean **+$248/trade**, sd **$1,597**,
win rate **40.7%**), the power to reach a 2-sigma result is:

| trades | sessions @0.65/day | ≈ | expected net | 5th pctile | **P(t > 2)** |
|---|---|---|---|---|---|
| 40 | 62 | 3 months | +$9,918 | −$6,171 | **16%** |
| 60 | 92 | 4.4 months | +$14,877 | −$4,920 | **22%** |
| 100 | 154 | 7 months | +$24,794 | −$1,618 | **34%** |
| 165 | 254 | 1 year | +$40,911 | +$6,299 | **49%** |
| 253 | 389 | 1.5 years | +$62,730 | +$20,669 | **68%** |

Even a **full year** of forward sim is a coin flip on statistical significance,
*assuming the edge is exactly as measured*. Anyone who runs this for a quarter
and reads a positive number as validation has learned nothing; anyone who reads
a negative number as falsification has also learned nothing, because at n=40 the
5th percentile of a genuinely-working strategy is −$6,171.

**What it CAN settle, and these are worth settling:**

1. **PARITY** — does the compiled cpp flag the same setups the Python does, on
   the same bars? This is answerable in *two weeks* and it is the single
   highest-value output. The cpp has never been compiled or run. Every number
   in this repo describes the Python.
2. **OPERATIONAL** — do fills, the bracket, the 15:55 flatten, the session
   grouping and the one-setup-per-day lock behave live? Does the 24-hour chart
   requirement hold up? Does it survive a data outage?
3. **GROSS FALSIFICATION** — a result far outside the band below means something
   is broken, not that the edge is weak.

Treat it as a **dress rehearsal and a parity audit**, not an experiment.

---

## 1. The frozen configuration

`Mizan_IOF_NQ.cpp` **stock defaults, changing nothing**. They are already the
measured config and `test_mizan_defaults.py` asserts it. For the record:

| Input | Value |
|---|---|
| Live Trading | **0 (sim)** |
| Entry | **0 (market next bar)** — NOT mode 1, NOT sweep_limit_d |
| Quantity | 1 |
| Overnight Start / RTH Open / RTH Close | 1800 / 930 / 1600 |
| Last Sweep / Flatten | 1200 / 1555 |
| Min Overnight / Prior RTH Bars | 60 / 40 |
| Use ONH/ONL, Use PDH/PDL | 1, 1 |
| Sweep Depth / Reclaim Close Position | 0.05 × ATR / 0.50 |
| Stop Buffer / Min / Max | 0.15 / 0.40 / 2.50 × ATR |
| Target R-multiple | 2.0 |
| Daily Loss $ | **0 (OFF — as measured)** |
| ATR Period | 14 |
| Log Level | **2 (verbose — needed for the parity audit)** |

**Chart: NQ, 5-minute, 24-HOUR session.** On an RTH-only chart the overnight bar
count never reaches 60 and the study correctly never trades (it says so at log
level 2). Chart timezone **US Eastern**.

**Do not turn the daily-loss governor on.** The measurement ran flat 1 lot with
every governor off. Turning it on is a departure from the tested configuration,
not a free safety upgrade — and in sim there is nothing to protect.

---

## 2. Runbook

1. Copy `Mizan_IOF_NQ.cpp` to the VPS `ACS_Source\`. The repo's post-commit hook
   only syncs `IOF_NQ_Autopilot.cpp`, so this is manual.
2. **Analysis ▸ Build Custom Studies DLL.** It has never been compiled — expect
   errors on the first pass and fix them before anything else. Record what they
   were; if any fix changes behaviour rather than syntax, this pre-registration
   is void and needs re-issuing.
3. Verify the DLL timestamp actually moved. A synced `.cpp` against a stale
   `.dll` has cost this repo a deploy before.
4. New chart: NQ front month, 5-minute, 24-hour session, ET.
5. Add study. Confirm every input matches §1. Confirm **Live Trading = 0**.
6. Enable Auto Trading + Trade Simulation for that chart. Without it every order
   returns `-8994 SKIPPED_AUTO_TRADING_DISABLED`.
7. Flatten and reset the simulated account before the first session.
8. Let it run. **Do not touch the inputs mid-run.** Any change restarts the clock.

**Day-1 checks** (before trusting anything):
- Message log shows `[MZ]` lines and no `session skipped: N overnight bars < 60`.
- A sweep produces `[MZ ENTRY] LONG|SHORT swept ONH|ONL|PDH|PDL=... MARKET
  stop=Xpt tgt=Ypt ATR=Z ON=[...] PD=[...]`.
- `[MZ] FLAT_BY 1555` appears at the session end.
- At most one entry per session.

---

## 3. The parity audit — the actual deliverable

Weekly, and it is the reason to run this at all:

1. Copy the VPS `.scid` for the live contract back to the repo.
2. Run `python backtest_mizan_p3.py` over exactly those dates.
3. Export the Sierra Message Log and extract the `[MZ ENTRY]` lines.
4. **Every setup the Python flags must appear in the cpp log, and vice versa**,
   with the same side, level, stop and target to the tick.

Any mismatch stops the clock. Log it, find which engine is wrong, fix, restart.
A divergence here invalidates every backtest number as a description of the cpp
— which is precisely the thing that has never been checked.

Expect **~0.65 entries/day**. A materially different rate is a parity failure
even if the P&L looks fine.

---

## 4. The falsification band — fixed in advance

Cumulative sim P&L, 1 lot, evaluated **only** at the checkpoints below. These are
the 5th/95th percentiles of the backtest's own bootstrap, so "inside the band"
means "consistent with the measured edge", NOT "working".

| after | STOP if below | investigate if above |
|---|---|---|
| 40 trades | **−$6,171** | +$27,071 |
| 60 trades | **−$4,920** | +$35,409 |
| 100 trades | **−$1,618** | +$51,417 |

- **Below the floor → stop and diagnose.** Most likely causes, in order: a parity
  bug, fill quality, a regime the 2025-03→2026-06 sample did not contain.
- **Above the ceiling → also investigate.** Beating the backtest by that much in
  sim usually means the sim is not modelling something real (optimistic fills).
- **Inside the band → it is working as specified. This is not validation.**

**Forbidden after starting:** changing any input; switching to entry mode 1 or
sweep_limit_d because the P&L is disappointing; turning the daily-loss governor
on; adding contracts; changing the bar period; or evaluating at a checkpoint not
listed above. Any of these ends the run and voids it.

---

## 5. What a completed run does and does not license

| Outcome | Means |
|---|---|
| Parity clean + inside band at 100 trades | The cpp implements the measured strategy and behaves operationally. **Still not validated.** Cross-instrument and 6/6 ship gate remain unmet. |
| Parity clean + below floor | The edge does not survive out of sample, or something is broken. Diagnose before concluding which. |
| Parity broken | Every number in this repo describes the Python, not the cpp. Fix and restart; nothing else is interpretable. |

**No outcome of this forward sim licenses a funded account.** The outstanding
gates are unchanged and are not the kind a forward sim can close:

- ES fails (−$392 over 276 trades, 41.5th percentile, 3/6).
- No uncorrelated instrument — six NQ contracts are one index.
- Ship gate 5/6, not 6/6. NQZ25 loses −$8,115 with a −$12,220 drawdown.
- Per-contract t is below 2 on **all six** contracts; only pooled t reaches +2.47.
- Single-account front month: **−$14,335 max drawdown, −$4,030 worst day**, with
  every governor off.
