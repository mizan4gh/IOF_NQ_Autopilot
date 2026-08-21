Builds three strategies in the accumulation → manipulation → distribution family, evaluates all of them, and ships **none**. The value here is the negatives, the harness, and five bugs — two of which would have produced fake edges.

**Nothing deploys.** `Mizan_IOF_NQ.cpp` is uncompiled, defaults to sim, and is not in the post-commit sync path (that hook only copies `IOF_NQ_Autopilot.cpp`).

## The three strategies

| | what it is | verdict |
|---|---|---|
| `backtest_mizan_iof_nq.py` | rolling balance → sweep → distribution → POC pullback | **falsified** — 14 configs, all 37th–75th pctile |
| `backtest_mizan_p3.py` | daily power-of-three on levels frozen at 09:30 | **NQ-only** — 99th on NQ, fails ES/CL/GC |
| `backtest_mizan_avpmd.py` | balance + absorption + failed breakout, entry on acceptance | **not the stated mechanism** — beaten by its own baseline |

## 1. Rolling AMD — falsified

12-cell frequency grid, every cell between the 37th and 75th percentile of its own re-sign null. The entry-agnostic control (same setups, market at next open, POC skipped) sits at the 33rd — so the POC entry isn't what's broken, the A→M→D sequence is. Frequency tops out near 0.5 trades/day even with the gates loosened, so it could never have been validated regardless.

## 2. Daily P3 on frozen levels — the closest thing to shippable, and still not

The pattern as specified fails; the stage inside it works:

| `entry_mode` | n | pooled net | null pctile |
|---|---|---|---|
| `poc` (full A→M→D→POC) | 66 | −$3,675 | 21.5% |
| `confirm` (POC removed) | 112 | +$13,390 | 63.5% |
| **`sweep` (D and POC removed)** | **253** | **+$62,730** | **99.0%** |

Every stage after the manipulation destroys value. Survives 3t slippage (98.5th), OOS-2026 3/3 at 99.5th, either level family alone, 15 stress cells all 89.5–100th.

**It is the only strategy here that passed its placebo.** Pulling every level inward by a fraction of the overnight range — preserving the mechanic and the trade count (259/266/251 vs 253) — collapses it to the 83rd/19.5th/18th percentile. The frozen levels are genuinely load-bearing. That single fact is why this ranks above the others despite worse headline numbers.

**Why it still doesn't ship:** it does not leave NQ. ES −$392 (41.5th), crude −$2,880 (37.5th, and −$7,160 at crude's own pit open), gold +$1,045–3,453 (66th–80th). Three instrument families fail. Also 5/6 on the ship gate, not 6/6.

CL and GC were already in `C:\SierraChart\Data`; `--freeze` copies them into the repo so a running Sierra cannot rewrite them mid-run.

## 3. AVPMDPOC — works, but not for the reason it claims

Built to a supplied spec: balance → absorption (delta percentile, cumulative-delta divergence) → sweep in a depth band → reclaim → entry on acceptance. Delta is real — `bid_vol + ask_vol` equals volume on 100% of bars. MBO questions (bid replenishment, spoof-vs-genuine) are **not** modelled and can't be from `.scid`.

First 6/6 ship-gate pass in the family: n=845, +$72,220 at NQ pricing, 100th percentile. The spec's acceptance claim is **validated** — the `sweep_now` control that enters at the reclaim lands at the 55th and loses money.

Then two tests killed it.

**The placebo fails.** Boundaries shifted inward 10/20/35% leave it at the 100th percentile every time.

**Its own baseline beats it.** Stripping the strategy one stage at a time, params only, same code path:

| rung | n | net | $/trade | t | gate |
|---|---|---|---|---|---|
| FULL | 845 | +$72,220 | +85 | +2.24 | 6/6 |
| − balance gates | 1084 | +$62,915 | +58 | +1.62 | 4/6 |
| − balance − flow | 2075 | +$133,245 | +64 | +2.34 | 4/6 |
| − balance − flow − band | 2425 | **+$202,095** | +83 | **+3.26** | 5/6 |
| BASELINE: N-bar low + pullback | 2635 | +$179,840 | +68 | +2.52 | 6/6 |

A bare "buy an N-bar low, enter on the pullback, 2R target" makes 2.5× the money on 3× the trades with a higher t-stat. The five-stage apparatus removes 68% of the sample to buy 25%/trade. Balance detection, absorption, CD divergence, the depth band and the POC are decoration.

The one stage that carries information is the **same-bar reclaim**. Isolated (rung 4) it is the best net and t of anything tested — and it fails the placebo too, and its NQ edge is 13× concentrated in 2026 ($14/trade IS vs $188/trade OOS).

## Limit entry — a filter, not a better fill

`entry_mode="sweep_limit"`. Decomposing the 253 market trades by whether a limit would have filled:

| | n | net | $/trade |
|---|---|---|---|
| limit filled these, taken at market | 111 | +$101,650 | +$916 |
| limit never filled these, at market | 142 | −$38,920 | −$274 |
| the limit trades themselves | 111 | +$103,485 | +$932 |

Price improvement is $16/trade. The rest is selection — setups that never retrace to the reclaim close are the losing half. Causal and tradeable, but it costs the placebo pass (100th at both shifts) and drops the fill rate to 44%, leaving 20 trades on CL+GC.

## Constant-volume bars

5,000 contracts is **7.2 min on MNQU6, 36.4 on NQM6, 60.7 on NQU25** — the spec's 5k is MNQ-calibrated; the notional equivalent on the minis is 500. Both clear their nulls and both fragment the edge: $5/trade on MNQ (under the cost floor) and $35/trade on the NQ pool at 21 trades/day, which slippage takes from +$288,995 to +$7,430 across 1/2/3 ticks.

## Bugs found — two would have produced fake edges

1. **Fill-bar exposure.** An open-fill was managed from the *next* bar, deleting the first bar of adverse excursion from every trade — the same shape as the look-ahead that made the absorption thread print +$19k. Worth **$6,400**. `Cands.manage_at` now carries the first managed bar per candidate. The limit-fill analogue is worth **$3,667** and now defaults to the conservative treatment.
2. **`sessions()` hardcoded 1800/0930/1600**, making `rth_open` a dead input. A pre-registered "use each instrument's own pit open" arm returned byte-identical results — the only reason it was caught.
3. **The null harness called `load_bars_cached` directly**, null-testing 5-minute bars against a volume-bar result. Same tell: an identical null mean.
4. **`TICK` hardcoded to 0.25** in stop/target rounding — would have quantised a crude stop into $250 steps. Caught before the CL run.
5. **`breakout_retest` returned exactly 0 trades** — not thin data; the entry *is* the range high and `target_mode="opposite"` is that same high, so target distance was ~0 and the RR filter rejected 100% of setups.

Rule extracted: **an A/B with an identical trade count is a dead knob, not a null result.**

## Reusable

- `poc_of()` — look-ahead-free volume profile from bars, difference-array + cumsum, O(bars + bins).
- `sessions()` — 18:00→17:59 grouping deriving the next session from dates present in the file, so weekends, holidays and known `.scid` coverage gaps fall out without a calendar.
- `placebo_shift` — the level-placebo pattern generalises to any level-based signal in this repo, and is the single most decisive test run here.
- `SPECS` / `apply_spec` — per-instrument tick, point value and commission, applied per contract so a mixed pool can't be priced with the wrong spec.
- `backtest_mizan_null.py` — one re-sign null driving all three threads via `--p3` / `--avpmd`.
- The scan/simulate split throughout: 200 null draws re-run only the governed walk against one cached scan.

## Notes for review

- **`Mizan_IOF_NQ.cpp` is not compiled.** No C++ compiler on the dev box. Every ACSIL symbol used was verified present in `ACS_Source` headers, but that is not a build. Needs a 24-hour chart session; on an RTH-only chart it correctly never trades and logs so at log level 2.
- Governors default OFF everywhere, matching what was measured. Turning `DAILY_LOSS` on is a departure from the tested configuration, not a free safety upgrade.
- Output CSVs and `.scid` copies are untracked/gitignored, matching the repo.
- `backtest_mizan_p3.py` defaults to `sweep` and `backtest_mizan_avpmd.py` to its measured baseline, both with the falsified variants reachable and labelled with their percentile at the point of use. An earlier revision defaulted to the falsified chain while the docstring reported the working numbers.
