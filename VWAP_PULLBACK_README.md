# IOF_NQ_VWAPPullback — NQ VWAP Pullback / Failed-Retest

Sierra Chart ACSIL study implementing an RTH-only, trend-continuation entry on a
**failed retest of session VWAP**.

**Status: the specified strategy is falsified. A variant found by the placebo test
survives every gate run so far on NQ.** Do not ship either without reading
[Measured behaviour](#measured-behaviour). Three findings:

1. The specification's own risk defaults are arithmetically unsatisfiable on a
   5-minute NQ chart (154 confirmations → 3 trades).
2. **Session VWAP is falsified on NQ.** Once the geometry is ATR-normalised so it
   means the same thing on every contract, the re-sign null falls to 93.5th and
   leave-one-out collapses to 72.5th.
3. **But the placebo test says it is the VOLUME WEIGHTING that is dead, not the
   level.** Replacing VWAP with a volume-blind running mean of typical price
   improves every measure — 99.0th null, 95.0th worst LOO, +40% net. Replacing it
   with a pure geometric midpoint kills it outright (54.0th). So the session-anchored
   average of *traded price* is load-bearing; the `V` in VWAP is not.

---

## Files

| File | What it is |
|---|---|
| `IOF_NQ_VWAPPullback.cpp` | The study. Single self-contained file (Sierra's remote build takes one upload). |
| `backtest_vwap_pullback.py` | Bar-for-bar Python parity engine on the repo's shared `simulate()` / `summarize()` harness. Carries `geom_mode` (points/ATR) and `level_mode` (vwap/tpavg/mid) for the placebo ladder. |
| `test_vwap_pullback.py` | 29 unit tests: rule-by-rule kill tests, dead-input reachability, points/ATR mode isolation, VWAP anchoring, cross counter, no-lookahead. |

Nothing else in the repo was modified.

---

## The pattern

Long case; short is the exact mirror.

1. **TREND** — a completed RTH bar closes ≥ `MinSeparation` (15 pts) above session
   VWAP, with EMA20 > EMA65, the EMAs at least `MinEmaSep` apart, and EMA65 rising
   by at least `MinSlope` over `SlopeBars`.
2. **PULLBACK** — a later bar's **low** reaches within `Proximity` (8 pts) of VWAP.
   Price may touch or penetrate VWAP within `RetestTolerance` (4 pts). A completed
   close more than the tolerance *below* VWAP kills the setup.
3. **FAILED RETEST** — a bar touches the zone, closes back above VWAP, closes in the
   upper half of its own range, and closes above its open.
4. **CONFIRMATION** — the next completed bar closes above the rejection bar's high
   plus `ConfirmBuffer` (2 pts) → enter.

---

## Installation

1. Copy `IOF_NQ_VWAPPullback.cpp` to `C:\SierraChart\ACS_Source\`.
   *(The repo's `post-commit` hook only syncs `IOF_NQ_Autopilot.cpp`, so this one is
   a manual copy.)*
2. Sierra Chart → **Analysis ▸ Build Custom Studies DLL** → select the file → Build.
   Or **Analysis ▸ Build Custom Studies DLL ▸ Remote Build** and upload the single file.
3. Confirm the DLL actually rebuilt — check its timestamp. A synced `.cpp` with a
   stale `.dll` has cost this repo a deploy before.
4. Chart → **Studies ▸ Add Custom Study ▸ "IOF NQ VWAP Pullback / Failed Retest"**.

### Required chart settings

| Setting | Value | Why |
|---|---|---|
| Chart timezone | **US Eastern** | All session inputs are compared against the chart clock. If your chart is on CT, set `Chart-to-ET Offset (minutes)` to `60` instead of editing the times. |
| Session times | Include RTH + overnight | Overnight bars are needed to warm the continuous EMAs; they can never generate a signal. |
| Bar period | See [Measured behaviour](#measured-behaviour) | The point-denominated geometry does not work on 5-minute bars. |
| Trade ▸ Auto Trading | Enabled | Otherwise every order returns `-8994 SKIPPED_AUTO_TRADING_DISABLED`. |
| Trade Simulation | **On** until validated | `sc.SendOrdersToTradeService` defaults to `0`. Leave it there. |

---

## Configuration

> ### The defaults are NOT the literal specification
>
> The spec as written takes **3 trades in 387 contract-days** — its risk rules are
> arithmetically unsatisfiable (see below). Shipping it as the default would ship a
> strategy that does not trade. The defaults are therefore the best-measured config:
>
> | Setting | Default | Spec value |
> |---|---|---|
> | Chart bar period | **1 minute** | 5 minute |
> | `Geometry Denomination` | **1 (ATR multiples)** | 0 (points) |
> | `Reference Level` | **1 (unweighted typical-price mean)** | 0 (session VWAP) |
> | `Target Mode` | **2 (2.0 R multiple)** | 0 (fixed 100 ticks) |
>
> Set those four back and use a 5m chart to run the specification exactly.
> In Python: `VP_geom_mode=points VP_level_mode=vwap VP_target_mode=0 VP_BAR_MINUTES=5`.
>
> **This config is pre-registered, not validated** (`PREREG_vwap_pullback_tpavg.md`).
> `sc.SendOrdersToTradeService` stays at 0.


All 70 inputs are exposed in the study settings dialog. The ones that matter most:

**Session** — `RTH Open` 09:30, `Last Entry Time` 15:00, `Flatten All Positions At`
15:45, `RTH Close` 16:00, `Chart-to-ET Offset` 0.

**Indicators** — EMA fast 20, EMA slow 65, ATR 14 (Wilder), slope lookback 5.

`EMA/ATR Anchoring` defaults to **0 = Continuous**, deliberately. A session-anchored
EMA65 needs 65 completed RTH bars; on a 5-minute chart that is 325 of the session's
390 minutes, so the first tradable bar would land after the entry cutoff and the
mode would take zero trades. Mode 1 exists and uses its own shorter warmup
(`Session-Anchored Warmup`, default 30 bars) — but a session-anchored EMA65 is *not*
converged at 30 bars. That is an explicit trade of convergence for a usable window;
own it or stay on mode 0.

**Risk** — `Stop Method` 0 (rejection extreme) / 1 (fixed 50 ticks) / 2 (ATR).
`Target Mode` 0 (fixed 100 ticks) / 1 (ATR) / 2 (R multiple). `Minimum Reward:Risk`
1.5. `Max Trades Per Day` 4, `Daily Loss Limit` $800, `Daily Profit Lockout` $1000,
`Cooldown` 8 bars.

**Geometry denomination** — `Geometry Denomination` 0 = points (the spec), 1 = ATR
multiples against the ATR frozen at the RTH open. The multiples are each the original
point value divided by one reference number (pooled median frozen-at-open 1m ATR over
six NQ contracts, `ATR_ref = 9.8058`, 388 sessions) — a re-anchoring, not an 8-way
sweep. Mode 1 is the only one that means the same thing on CL, GC or an NQ contract
at a different index level. It is also the one under which session VWAP shows no
edge; see [the verdict](#session-vwap-the-edge-was-an-artifact-of-the-points-geometry).

**Reference level** — `Reference Level` in the cpp, `level_mode` in Python; both
now implement all three. `0/vwap` is the specified strategy, `1/tpavg` (**default**)
is the volume-blind running mean of typical price, `2/mid` is the running
session-range midpoint placebo. See
[the placebo test](#the-placebo-test-it-is-the-volume-weighting-that-is-dead-not-the-level).

---

## Measured behaviour

Run over the repo's six frozen NQ contracts (~387 sessions) through the shared
harness, 1 tick slippage each way, $5 commission, `entry = next bar's open`.

### The specification's defaults do not trade

| Bars | Confirmations | Entries | Killed by |
|---|---|---|---|
| 5-minute | 154 | **3** | `stop out of range` 79, `RR below 1.5` 72 |
| 1-minute | 251 | **25** | `RR below 1.5` 182, `stop out of range` 44 |

**Root cause — this is a property of the spec, not a bug.** The spec asks for a stop
*beyond the rejection bar's extreme* and simultaneously for a *fixed 100-tick
(25 point)* target at a *1.5 minimum reward:risk*. Those three together require the
stop to be ≤ 16.67 points. Measured median 5-minute NQ bar range on these contracts:

```
NQU25 19.2   NQZ25 26.8   NQM5 33.1   NQH6 30.5   NQM6 33.8   NQU26 47.8   (points)
```

A structural stop is at minimum one bar's range, so it is essentially always wider
than the fixed target allows. The strategy rejects nearly every setup it finds. The
`min_atr_pts = 8` default is the mirror-image problem — median RTH ATR is 21–53
points, so that filter is near-inert.

`test_vwap_pullback.py::TestFixedTargetVersusStructuralStop` pins this so it cannot
regress silently into "the signal has no edge".

### Making the risk model self-consistent

Switching to `Target Mode = 2` (R multiple, 2.0R) makes the 1.5 minimum RR
satisfiable by construction. This is choosing one of the spec's own listed options,
not sweeping a threshold to pass.

| Bars | Trades | Pooled net | Positive contracts |
|---|---|---|---|
| 5-minute | 61 | +$2,450 | 4/6 |
| 1-minute | 176 | +$16,560 | 5/6 |

### Session VWAP: the edge was an artifact of the points geometry

Both denominations, 1-minute bars, R-multiple target, 200-draw re-sign null.
The null holds entry times, stops and targets fixed and randomises only the **side**,
so it separates "the bracket geometry made money" from "the signal picked the side".

| | Points (spec) | **ATR-normalised** |
|---|---|---|
| Trades (NQ) | 176 | 188 |
| Pooled net (NQ) | +$16,560 | +$14,277 |
| Contracts positive | 5/6 | 5/6 |
| Null mean | −$3,035 | **+$39** |
| **Null percentile** | 99.0th | **93.5th** |
| **Worst leave-one-out** | 95.5th — survives | **72.5th — dies** |
| CL | 0 trades (vacuous) | +$1,478 (n=41, 1/1) |
| GC | 0 trades (vacuous) | **−$2,965 (n=29, 0/2)** |

### The placebo test: it is the volume weighting that is dead, not the level

A placebo swaps the one line the whole strategy is built around for a fake with the
same geometric role but less information, and re-runs everything unchanged. Two were
run, testing different claims (`level_mode`, see `session_level()`):

- **`tpavg`** — running mean of typical price, **volume-blind**. Same anchor, same
  shape, zero volume information. Tests: *does the volume weighting matter?*
- **`mid`** — (running session high + running session low) / 2. Pure geometry that
  knows nothing about where price actually traded. Tests: *does the level matter?*

NQ only, 1-minute, ATR geometry, R-multiple target, six frozen contracts, 200 draws:

| Level | n | Pooled net | Null pctile | Worst LOO | Front-month /yr | MaxDD | net/DD |
|---|---|---|---|---|---|---|---|
| `vwap` (the specified strategy) | 188 | +$14,277 | 93.5th | **72.5th — dies** | +$9,228 | −$4,993 | 2.41 |
| `tpavg` (volume-blind) | 206 | **+$20,055** | **99.0th** | **95.0th — survives** | **+$15,399** | −$5,327 | 3.77 |
| `mid` (geometric) | 133 | −$202 | 54.0th | — | ~$100 | **−$9,547** | 0.01 |

MaxDD and net/DD are single-account front-month figures (1 contract, closed-trade
drawdown over the 1.30-year span). Two things fall out of that column:

- Drawdown is ~$5.0k for all three live configs — it is set by the bracket geometry
  and the daily cap, not by the level. The level moves the net, not the risk.
- `mid` is worst on risk *and* return (−$9,547 for ~$0), which is what a genuinely
  dead signal looks like.

**Closed-trade drawdown, not intraday** — live equity DD will be worse, and a MaxDD
estimated off 178 trades is itself noisy. **Worst day is −$950 to −$991 against a
nominal $800 daily-loss cap**: the cap exits at the cap *price*, then slippage and
commission come off after it, so it overshoots by ~$150–190 every time. The daily
gate is advisory, not enforcing.

**The level passes.** `mid` is a coinflip — 54.0th, −$202 on 133 trades. A
session-anchored average of traded price carries information a range midpoint does
not. This is the test that closed three earlier threads in this repo; this one
survives it.

**VWAP fails its own placebo.** Stripping the volume weighting gives more trades,
+40% net, 93.5th → 99.0th on the null, and turns a leave-one-out death (72.5th) into
a survival (95.0th, all six contracts ≥ 95.0th). The volume weighting is not neutral
decoration — it is actively degrading the signal.

#### Chronological stability: `tpavg` holds, `vwap` collapses

The repo's `IS_TAGS`/`OOS_TAGS` form a clean time split — IS = Mar–Dec 2025,
OOS = Dec 2025–Jun 2026. 200-draw null on each half:

| Level | IS net / pctile | OOS net / pctile |
|---|---|---|
| `tpavg` | +$11,416 / 96.5th | +$8,638 / **98.5th — holds** |
| `vwap` (control) | +$13,092 / 96.5th | +$1,186 / **62.0th — collapses** |

Both are at an identical 96.5th in the first half and diverge completely in the
second. The `vwap` control was run precisely to prove the split can discriminate, so
`tpavg` passing it is not vacuous. Still in-sample: the config was chosen seeing both
halves, so this rules out time-concentration, not the selection effect.

#### Two caveats on `tpavg`, both load-bearing

- **It is the best of three variants.** A 99.0th percentile *selected* from three
  candidates is closer to ~97th once that choice is paid for. The hypothesis and a
  fixed pass/fail rule are now frozen in `PREREG_vwap_pullback_tpavg.md`. **No
  held-out data exists in this repo** — every NQ calendar date has been run, and the
  two unused `.scid` files are duplicate snapshots of contracts already tested. The
  test needs fresh `.scid` from the VPS: NQ after 2026-06-29, or before 2025-03-10.
- **NQ only**, six correlated contracts of one instrument, n = 206, ~0.4 trades/day.
  Cross-instrument numbers were measured before the focus narrowed to NQ and are
  **not** encouraging: CL +$589 (n=41), **GC −$1,488 (n=23, 0/2)**, ES pooled +$347
  (3/6). Gold is negative under both levels. Recorded here so it is not rediscovered
  as good news later.

#### These are POOLED figures, not annual

The table above sums six contracts. That is not what an account earns:

- 387 contract-days cover only **337 calendar days** — consecutive quarterly
  contracts overlap at the roll and get double-counted.
- You can hold one expiry at a time, so the six streams are not additive.

Collapsed to a single account holding the front month (each calendar day assigned to
the highest-RTH-volume contract), over 2025-03-10 → 2026-06-29 = **1.30 years**,
qty = 1:

| Geometry | Trades | Net | **Per year** | Trades/yr |
|---|---|---|---|---|
| Points | 159 | +$15,180 | **+$11,648** | 122 |
| ATR-normalised | 168 | +$12,026 | **+$9,228** | 129 |

The ATR figure sits **1.48 sigma** above its own null (pooled null sd $9,600 ≈
$7,400/yr). The annual error bar is nearly as large as the annual number — which is
the 93.5th percentile restated in dollars. It is a point estimate a coinflip on side
reproduces roughly 1 time in 15, not a $9k/year edge.

**The normalisation is what killed it, and that is the finding.** Two things move
together and both point the same way:

1. The null **mean** goes from −$3,035 to +$39. Under the points geometry, random-side
   trades on that candidate set *lose* money — the candidate set itself was skewed.
   Under ATR normalisation the null is fair, and the real run no longer stands out.
2. Leave-one-out collapses from 95.5th to **72.5th**. Dropping NQZ25 alone (+$8,660 of
   the +$14,277) takes the whole result with it.

The points thresholds, applied unchanged across contracts trading between 20k and
30k, were selecting a biased subsample. That selection — not the failed-retest
signal — was carrying the 99.0th percentile. Once the geometry is denominated in
something that means the same thing on every contract, the signal does not separate
from a coinflip.

And now that CL and GC actually trade (the normalisation worked — 41 and 29 trades
where there were zero), **gold is negative on both contracts**. Three instrument
families, and the only one that looks good is the one the thresholds were derived
from.

This is the same shape as the P3 frozen-level thread: strong on NQ, fails everywhere
else. **Do not ship either mode.**

#### A tooling bug worth knowing about

An earlier version of the leave-one-out script re-seeded the RNG *per contract*
inside each draw, giving every contract the same random side sequence. That
correlated the draws and reported **99.5th** where the correct seeding gives
**99.0th** (points) and **93.5th** (ATR) on identical data. Seed once per draw and
let the stream advance across contracts. Both tables above use the corrected seeding.

---

## Validation checklist — do this before believing anything

Done: ✅ re-sign null · ✅ leave-one-out · ✅ 3-tick slippage · ✅ per-contract gate ·
✅ ATR normalisation · ✅ uncorrelated instruments · ✅ **placebo ladder**.

**Session VWAP is closed.** 93.5th null, 72.5th worst LOO, negative on gold.
Re-tuning the ATR multiples to recover the points-mode number would just be fitting
the selection effect that produced it.

**`tpavg` (volume-blind) is open, and the next step is not another sweep.** It has
passed the null, leave-one-out and the geometric placebo, but it was *selected* out
of three variants, which is exactly the way a 99th percentile gets manufactured. In
order:

1. **Pre-register and re-run it.** Write the `tpavg` config down as the hypothesis
   *before* touching the data again, then re-run the null on it as the only
   candidate. That converts a best-of-three into a real result, or kills it.
2. **Explain why volume weighting hurts.** A result with no mechanism is a result
   waiting to be a fluke. If the volume weighting is genuinely degrading the level,
   there should be a visible reason — likely the opening-drive volume spike dragging
   VWAP toward the first 30 minutes. Check it directly.
3. **Then** the uncorrelated instruments again, on the pre-registered config. Gold is
   currently negative under both levels; that has to be faced, not averaged away.
4. **Only then** a 2+ independent contract A/B before any cpp constant ships.

`tpavg` is now implemented in the cpp too (`Reference Level`) and is the default in
both engines, so the two stay in parity. That is a change of *default*, not of
status: it is still pre-registered and unvalidated, and the study still cannot send
a live order until `sc.SendOrdersToTradeService` is set to 1 by hand.

Reproduce any of the above with:

```
# points geometry (the original spec)
VP_BAR_MINUTES=1 VP_target_mode=2 python backtest_vwap_pullback.py --null 200

# ATR-normalised geometry -- the honest one; session VWAP fails here
VP_BAR_MINUTES=1 VP_target_mode=2 VP_geom_mode=atr python backtest_vwap_pullback.py --null 200

# the placebo ladder: volume-blind, then pure geometry
VP_BAR_MINUTES=1 VP_target_mode=2 VP_geom_mode=atr VP_level_mode=tpavg python backtest_vwap_pullback.py --null 200
VP_BAR_MINUTES=1 VP_target_mode=2 VP_geom_mode=atr VP_level_mode=mid   python backtest_vwap_pullback.py --null 200
VP_BAR_MINUTES=1 VP_target_mode=2 VP_geom_mode=atr python backtest_vwap_pullback.py --cl
VP_BAR_MINUTES=1 VP_target_mode=2 VP_geom_mode=atr python backtest_vwap_pullback.py --gc
```

---

## Sierra Chart replay / backtest checklist

- [ ] **Use Replay, not a chart reload.** Studies cannot trade on historical bars
      during a full recalculation — you will get `-8998 SKIPPED_FULL_RECALC` and
      zero trades. Chart ▸ **Replay Chart** (Replay Mode: *Standard*, or *Accurate
      Trading System Back Test* for bar-by-bar order simulation).
- [ ] Trade Simulation **on**; `Send Orders To Trade Service` input left at 0.
- [ ] Flatten and clear the simulated position/order state before each run, or the
      study will adopt a leftover position and refuse to enter.
- [ ] Set `Debug Log` to 1 (transitions) and watch **Window ▸ Message Log**.
- [ ] Confirm the first log line of each session shows a VWAP that starts fresh at
      09:30 — a VWAP carrying overnight volume means the timezone offset is wrong.
- [ ] Check the end-of-session `VWAPPB DIAG` funnel line:
      `trend → pullback → rejection → confirm → orders → fills`, followed by the
      per-reason rejection counts. **If `confirm` is healthy but `orders` is ~0, you
      are hitting the fixed-target / RR inconsistency described above.**
- [ ] Verify the 15:45 flatten fires and that no order works past the session.
- [ ] Verify trade count resets at 09:30 and that the daily governors block entries
      without flattening an open bracketed position.
- [ ] Compare the replay trade list against `IOF_vwapPB_<TAG>.csv` from the Python
      engine. They will not match tick-for-tick (see limitations) but the *setup
      times* should line up.

---

## ACSIL backtesting limitations (documented, not worked around)

- **Studies cannot submit orders during a full recalculation.** Historical bars are
  replayed with `SCT_SKIPPED_FULL_RECALC`. Only Replay mode produces trades.
- **Fill model.** The study submits a market order on the confirmation bar's closing
  update; Sierra fills it at the next tick. The Python engine models this as the
  **next bar's open** and manages the trade from that same bar, so the first bar of
  adverse excursion is not deleted. Filling at the confirmation bar's *close* would
  flatter the strategy — the close is by construction already through the trigger
  level. The two engines will therefore differ on individual fills.
- **Attached orders use offsets, not absolute prices.** The bracket is
  `Stop1Offset` / `Target1Offset` from the actual fill, so the risk *in points* is
  preserved even when the open gaps, but the absolute stop price will differ from
  the level drawn at signal time. This is intentional; an absolute-price bracket
  silently widens risk on a bad fill.
- **Within-bar ordering is unknowable.** The Python engine assumes the **adverse**
  extreme happens first, so a bar spanning both stop and target books the stop.
  Conservative, and the only honest choice without tick data.
- **Partial fills.** The study logs them and manages the quantity actually held;
  Sierra sizes the attached bracket to the filled quantity. It does not chase the
  unfilled remainder.
- **`AllowOnlyOneTradePerBar = 1`** means a second signal inside the same bar is
  skipped with `-8997`, not queued.
- **Daily P/L is read from `GetTradeStatisticsForSymbolV2`** and re-baselined at each
  RTH open, so the study measures *its* session regardless of when the platform's own
  daily statistics roll over.

---

## Assumptions made

1. **Chart timezone is US Eastern**, matching this repo's existing convention
   (`RTH_OPEN = 935` compared directly against the bar clock in
   `IOF_NQ_Autopilot.cpp`). A `Chart-to-ET Offset (minutes)` input handles other
   cases; the offset is applied in exactly one helper so there is one place the
   assumption lives.
2. **"Ignore overnight data for signals"** is implemented as: the session VWAP
   carries no overnight volume, the state machine never runs on an overnight bar, and
   no entry can occur outside the window. The *EMAs* are continuous by default —
   see the anchoring note above for why a session-anchored EMA65 is not viable on a
   5-minute chart.
3. **"The next completed confirmation bar"** is taken literally:
   `Bars Allowed For Confirmation` defaults to 1. A rejection that is not confirmed
   on the very next bar retires the whole setup rather than re-arming, which is how
   "prevent multiple signals from the same pullback" is enforced.
4. **The stop also clears the confirmation bar by default**
   (`Stop Also Clears Confirmation Bar = 1`). The spec anchors the stop on the
   rejection bar's extreme, but the confirmation bar can print a lower low on the way
   up, which would put the stop *inside* the bar being entered on.
5. **Trades are counted on fill, not on submission**, so a cancelled or rejected
   entry order does not consume one of the four daily trades.
6. **The daily loss / profit governors block new entries but do not flatten.** An
   open position is already bracketed; tearing it out mid-trade converts a protective
   stop into a market exit. Only the 15:45 session flatten is unconditional.
