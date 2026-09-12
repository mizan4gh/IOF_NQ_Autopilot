# PRE-REGISTRATION — VWAP-Pullback, ATR geometry + volume-blind level (`tpavg`)

**Registered:** 2026-09-12, before any held-out data was run.
**Registered by:** session `01M3s8BYdB29bKzDMkhZxtfr`.

This file exists because the `tpavg` result was **selected out of three candidate
levels after the first two failed**, on a bar interval and target mode that were
themselves chosen after their first settings failed. Three data-informed choices
stacked is exactly how a 97th-percentile result gets built out of nothing. The only
thing that converts it into evidence is running the frozen config, once, on data it
has never touched — with the pass/fail rule written down *first*, which is what this
file is.

**Do not edit the hypothesis or the decision rule below after seeing held-out
results.** If either needs to change, that is a new pre-registration with a new date,
and the old one stays in the repo as a record.

---

## 1. The hypothesis

> On NQ RTH, entering on a failed retest of the **session-anchored unweighted mean of
> typical price** (`tpavg`), with all distances denominated in ATR frozen at the
> 09:30 open, produces returns that are not explained by the bracket geometry alone.

The competing explanation this is designed to kill: the trades make money because of
where the stops and targets sit, and the level contributes nothing — which is what
the re-sign null measures.

## 2. The frozen configuration

**These are now the shipped defaults in both engines** (2026-09-12), so a bare run
reproduces the registered config. The env vars below are redundant but kept explicit
so the command is self-documenting and survives a future default change.

Exact, no free parameters. Run with:

```
VP_BAR_MINUTES=1 VP_geom_mode=atr VP_level_mode=tpavg VP_target_mode=2 \
  python backtest_vwap_pullback.py --null 200
```

| Parameter | Value |
|---|---|
| `bar_minutes` | 1 |
| `geom_mode` | `atr` |
| `level_mode` | `tpavg` |
| `target_mode` | 2 (R multiple) |
| `target_r` | 2.0 |
| `min_rr` | 1.5 |
| `proximity_atr` | 0.816 |
| `min_sep_atr` | 1.530 |
| `retest_tol_atr` | 0.408 |
| `confirm_buf_atr` | 0.204 |
| `min_slope_atr` | 0.102 |
| `min_ema_sep_atr` | 0.204 |
| `min_atr_bps` | 3.23 |
| `stop_buf_atr` / `min_stop_atr` / `max_stop_atr` | 0.102 / 0.408 / 5.099 |
| `stop_method` / `stop_inc_conf_bar` | 0 (rejection extreme) / 1 |
| `fast_len` / `slow_len` / `atr_len` / `slope_bars` | 20 / 65 / 14 / 5 |
| `max_crosses` / `cross_lookback` | 4 / 20 |
| `max_bar_atr_mult` / `max_setup_bars` / `confirm_window` | 2.0 / 20 / 1 |
| `close_half_frac` | 0.5 |
| `cooldown_bars` / `max_trades_day` | 8 / 4 |
| `daily_loss` / `daily_target` | 800 / 1000 |
| `qty` / `commission` / `slip_ticks` | 1 / $5 / 1 tick each way |
| Entry fill | next bar's OPEN, managed from that same bar |

**The ATR multiples are themselves in-sample.** `ATR_ref = 9.8058` was the pooled
median frozen-at-open 1m ATR over the same 388 sessions the config was chosen on.
They are frozen here as constants and **must not be re-derived** on the held-out set.

## 3. What counts as held-out

Data meeting ALL of:

- **NQ** (or MNQ), 1-minute bars buildable from `.scid`.
- **Calendar dates disjoint from 2025-03-10 → 2026-06-29**, which is the span already
  consumed. A different *contract* covering the same dates is NOT held out — the
  tested set already had six overlapping contracts and only 337 unique days.
- Not `FROZEN_NQU6_0709.scid` or `NQZ25-CME.scid`, which are duplicate snapshots of
  `NQU26` and `NQZ25`; between them they add ~7 unseen trading days, which is noise.

Practically this means pulling fresh `.scid` from the VPS: NQ contracts trading after
2026-06-29, or archived contracts predating 2025-03-10.

**Minimum size:** the held-out set must yield **n ≥ 60 trades**. Below that the test
returns INCONCLUSIVE — not a pass. At ~0.4 trades/session that is ~150 sessions, so
roughly two full quarterly contracts.

## 4. The decision rule — fixed in advance

Run the frozen config once. Record all four:

| # | Criterion | Pass |
|---|---|---|
| 1 | Re-sign null percentile, 200 draws, seeded once per draw | **≥ 95.0th** |
| 2 | Contracts with positive net | **≥ 2/3** of the held-out set |
| 3 | Single-account front-month MaxDD | **not worse than −$8,000** (1.5× the in-sample −$5,327) |
| 4 | Net per trade | **> 0** after $5 commission and 1 tick each way |

- **All four pass → the hypothesis survives.** Next step is a 2+ contract A/B, then
  the cpp port. Not a deploy.
- **Criterion 1 fails → the hypothesis is dead.** Record it and close the thread.
  Do not re-run with a different bar interval, level, or target mode; that is what
  produced this candidate in the first place.
- **1 passes but any of 2-4 fails → INCONCLUSIVE**, needs more held-out data. Not a
  pass, and explicitly not license to sweep.

**Forbidden after unblinding**, because each would silently convert this into another
best-of-N: changing any parameter above; changing the bar interval; re-deriving
`ATR_ref`; adding or removing a filter; changing the fill model; dropping a
contract from the held-out set for any reason not written here; or reporting a
different level mode's result from the same held-out data.

## 5. In-sample results being tested against

NQ, six frozen contracts, 337 unique sessions, 2025-03-10 → 2026-06-29:

| | `tpavg` (this hypothesis) | `vwap` (falsified) | `mid` (placebo, dead) |
|---|---|---|---|
| n | 206 | 188 | 133 |
| Pooled net | +$20,055 | +$14,277 | −$202 |
| Null percentile | 99.0th | 93.5th | 54.0th |
| Worst leave-one-out | 95.0th | 72.5th | — |
| Front-month /yr | +$15,399 | +$9,228 | ~$100 |
| MaxDD (single account) | −$5,327 | −$4,993 | −$9,547 |

Known negatives that the held-out test does **not** excuse: GC −$1,488 (0/2),
CL +$589 (n=41), ES pooled +$347 (3/6).

## 6. Why the mechanism was checked first

A result with no mechanism is a fluke waiting to happen. Measured on the in-sample
set: the first 30 minutes carry **16.5%** of RTH volume against a 7.7% share of clock
time (~2.1× over-weight); VWAP and `tpavg` sit **1.07 × ATR** apart on average; and
after 13:00 price sits 6.68 × ATR from VWAP but only 5.47 × ATR from `tpavg`.

So VWAP is measurably dragged toward the morning and tracks afternoon price less
closely. For a pullback-to-the-level strategy that is a coherent reason the volume
weighting would hurt. **Directionally confirmed, modest in size** — supportive, not
itself evidence of edge.

---

## APPENDIX — chronological stability check (NOT the pre-registered test)

Run 2026-09-12, immediately after registering, on the **already-seen** in-sample
data. Recorded here for completeness. It is **not** the held-out test of section 4
and does not satisfy any criterion in it: the config was selected while seeing both
halves, so this can only rule out time-concentration, never the selection effect.

The repo's existing `IS_TAGS` / `OOS_TAGS` happen to form a clean chronological
split — IS = NQM5/NQU25/NQZ25 (Mar–Dec 2025), OOS = NQH6/NQM6/NQU26 (Dec 2025–Jun
2026). 200-draw re-sign null on each half:

| Level | IS net / pctile | OOS net / pctile |
|---|---|---|
| `tpavg` (hypothesis) | +$11,416 / 96.5th | +$8,638 / **98.5th — holds** |
| `vwap` (control, already falsified) | +$13,092 / 96.5th | +$1,186 / **62.0th — collapses** |

**The control is the point.** `vwap` was run on the same split specifically to check
the split has discriminating power. Both levels sit at an identical 96.5th in the
first half and diverge completely in the second, so this is not a test that everything
passes — it kills the candidate already known to be bad while the hypothesis holds.

That is meaningful support for `tpavg` being stable over time, and it is the strongest
statement the existing data can make. It does not shorten section 4. The held-out test
still requires dates outside 2025-03-10 → 2026-06-29.
