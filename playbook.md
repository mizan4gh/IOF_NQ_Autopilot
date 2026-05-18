# IOF NQ Autopilot — Operator Playbook

**Study:** IOF NQ — Pure Orderflow Autopilot
**Version:** v12.25 (May 2026)
**DLL:** `IOF_NQ_Autopilot_64.dll`
**Platform:** Sierra Chart ACSIL
**Instrument:** NQ (or MNQ) — 3000 contract volume bars, RTH only

---

## 1. Quick-Start Checklist

| Step | Action |
|------|--------|
| 1 | Copy **only** `IOF_NQ_Autopilot.cpp` into Sierra Chart's `ACS_Source` folder |
| 2 | Analysis → Build Custom Studies DLL (local) or remote build (single .cpp, no subfolders) |
| 3 | Restart Sierra Chart if an old DLL was cached |
| 4 | Open a **3000 contract volume** NQ chart with Bid/Ask volume enabled |
| 5 | Add the study; confirm `SCDLLName = IOF_NQ_Autopilot` and load banner shows `v12.25` |
| 6 | Set **Enable Auto Trading = 1** for trade sim / live orders |
| 7 | Verify inputs (see Section 4) — daily caps, flatten time, CSV path |
| 8 | Run `VERIFY_PRODUCTION_BUNDLE.ps1` to confirm DLL name consistency |

---

## 2. Chart Requirements

- **Bar type:** Volume, 3000 contracts per bar (`kTargetVolumeBars`)
- **Session:** RTH — 09:35 ET open; flatten default **15:55** (input default compiled as 1555)
- **Volume data:** Bid Volume + Ask Volume must be enabled (orderflow dependency)
- **Time zone:** Chart must be aligned to **US Eastern** (exchange clock for M1 dead-zone + trend gates; banner will not warn if misconfigured — verify manually)
- **History:** Load ~6 months (180–220+ days) for backtest; data must cover full session days on volume bars
- **One autopilot per symbol/account** — do not run two instances on the same chart/account

---

## 3. Build & Sync

```powershell
# Verify DLL/source name consistency
.\IOF_NQ_Production_Final\VERIFY_PRODUCTION_BUNDLE.ps1

# Sync from development tree after editing IOFv02 sources
.\IOF_NQ_Production_Final\SYNC_FROM_IOFv02.ps1

# Options: -CppOnly, -CopySierraDocs, -WhatIf (dry run), -NoVersionStamp
# Example dry run:
.\IOF_NQ_Production_Final\SYNC_FROM_IOFv02.ps1 -WhatIf
```

Canonical source of truth: `MyBabyBot\IOFv02\`. Edit there; sync here before shipping a build.

---

## 4. Study Inputs Reference

Defaults below reflect the **Apex Trader Funding $150K Eval** profile baked into `SetDefaults` (v12.24).

| Input | Slot | Default | Notes |
|-------|------|---------|-------|
| Enable Auto Trading | 0 | 1 | 1 = live orders / trade sim; 0 = signal-only |
| Account Capital ($) | 1 | **150000** | Apex $150K eval; used for 2% risk-per-trade calculation |
| Daily Loss $ | 2 | **800** | Apex eval cushion; drives risk budget |
| Max Trades/Day | 3 | **3** | Apex eval: caps the day after at most ~2 stops |
| Flatten HHMM | 4 | 1555 | Flatten all positions at or after this time |
| Max Position Size | 5 | 1 | Locked at 1 contract (input clamped 1..1) |
| Log Level | 6 | 1 | 0=crit, 1=sig, 2=dbg (dbg writes per-bar EVAL rows) |
| Enable CSV Journal | 7 | Yes | Writes `IOF_NQ_<symbol>.csv` to DataFilesFolder |
| Entry Order Type | 8 | **2** | 0=market, 1=limit, 2=limit+2 ticks (marketable limit — v12.24 default, avoids worst-case market fills) |
| Diagnostics | 9 | 1 | 0=off, 1=on |
| Regime Mode Filter | 10 | 1 | 0=off; filters entries by trend/vol/chop regime |
| Fade Engine | 11 | 1 | 0=off; enables M8 fade mode |
| News Filter | 12 | **1** | Apex eval: ON — protects cushion from 08:30/10:00/14:00 ET spikes |
| Auto-disable Bad Modes | 13 | 1 | Disables modes with t-stat < -1.0 (n>=20 trades) |
| Daily Profit Target $ | 14 | 1000 | Flatten + halt at profit; 0 = disabled |
| Enable M1 VWAP Reclaim | 15 | 0 | Off by default (29% WR in backtest); enable to A/B test |
| V1 Hooks Mode | 16 | 0 | 0=off, 1=conf, 2=chop, 3=both, 4=both+Sharpe warn |
| V1 Min Confirmations | 17 | 3 | Minimum total confirmations when hooks enabled |
| V1 Chop Score Max | 18 | 62 | Gate blocks entry above this chop score |
| V1 Delta Threshold | 19 | 12 | Delta threshold for confirmation signals |
| V1 Imbalance Threshold | 20 | 0.58 | Ask/bid imbalance ratio threshold |
| V1 Volume Confirm Mult | 21 | 1.1 | Volume must be >= avg x this multiplier |
| V1 Volume Avg Lookback | 22 | 20 | Lookback bars for average volume |
| V1 Pace Lookback | 23 | 3 | Lookback bars for pace check |
| V1 R-Sharpe Warn | 24 | 0.30 | Rolling R-Sharpe warning threshold (mode 4) |
| Session Start HHMM | 25 | 0 | 0=RTH 09:35; 100=01:00 ET overnight session (v12.23/v12.24: RTH only validated) |

---

## 5. Trading Modes

Modes are evaluated every bar. At most one fires per bar. Priority (v12.24): **M6 > M8 > M4 > M3 > M2 > M1**.

> **M5 (Trap Reversal) and M7 (Auction Reversal) were removed from the signal combination** in v12.24. M7's state-mutation side effect (which silently disabled M8 type 2) was fixed in v12.25. Phase-0..2 of the trap state is still computed because M4 consumes it for scoring (S11/trapConf).

| Mode | selMode | Name | Bias | Quality Scale | Notes |
|------|---------|------|------|---------------|-------|
| M1 | 0 | VWAP Level Test | Trend | /15 strict | Disabled by default in backtest; 29% WR historically. Dead-zone 12:00–13:59 ET + 20-bar trend gate added in v12.24 |
| M2 | 1 | VP Level Test | Trend | /15 strict | Reacts to VP POC/VAH/VAL, prior day levels, icebergs |
| M3 | 2 | Consolidation Breakout/Rejection | Trend | /15 strict | Range <= ATR x C_CONSOL_ATR; breakout or range rejection. CtrlScore gate added in v12.25 |
| M4 | 3 | Sweep + Reclaim | Reversal | score x10 | Stop-hunt sweep of prior swing low/high, then reclaim; blocked if iceberg order detected near sweep level (within 0.5 ATR) |
| M6 | 5 | Balance Breakout | Reversal | score x10 | Highest priority; breakout from balance structure. CtrlScore soft gate (±1) added in v12.25 |
| M8 | 7 | Fade Engine | Counter-trend | edge x10 | Enabled by Fade Engine input. CtrlScore strict gate added in v12.25 |

**Quality floor:** `V18A_QUALITY_FLOOR = 40` (v12.26 tuning, was 50) — `qScore100` must be >= 40 to submit any entry.

- M1/M2/M3 use `finalScore × 100 / 15` — needs score >= 6/15 to clear floor 40 (was 8/15 at floor 50).
- M4/M6 use `score × 10` — score >= 4 clears the floor (was >= 5 at floor 50).
- M8 (fade): `edgeScore × 10` — only applies when `pFade->active`; otherwise falls through to `/15` scale. Fade-edge fire threshold is already >= 4, so M8 is unchanged by the floor drop.

### M6 Entry Levels (balance-specific)

Stop = `rangeHigh - ATR×0.25` (long) or `rangeLow + ATR×0.25` (short)
T1 = balance edge + `rangeWidth × 0.5`
T2 = balance edge + `rangeWidth × 1.0`
Requires `bkVerifyScore >= 6` out of 10 possible points (V1–V10 checks).

### M8 Fade Sub-types

| Type | Trigger | Stop | T1 | T2 |
|------|---------|------|----|----|
| 1 | Failed M6 breakout (`bkVerifyScore <= 4`) | High/Low ± ATR×0.3 | Balance POC | Opposite balance edge |
| 2 | Range fade — uses `rvVerifyScore 2–4` + `PrevImbDir`/`PrevImbExtreme` (the state previously wiped by M7; restored in v12.25) | Imbalance extreme ± ATR×0.3 | Extreme ± ATR×0.5 | Extreme ± ATR×1.5 |
| 3 | Trend exhaustion (4+ of 8 bars same dir, delta/vol declining) | High/Low ± ATR×0.4 | Close ± ATR×1.0 | VWAP (or ± ATR×2.0) |
| 4 | Absorption divergence (3+ abs-buy/sell + price/delta divergence) | High/Low ± ATR×0.35 | Close ± ATR×0.75 | Close ± ATR×2.0 |

Edge threshold to fire: >= 4 out of possible 10. Edge score feeds `qScore100 = edgeScore × 10`.

---

## 6. Hardcoded Constants

These are compiled in; not exposed as inputs. All ATR values are Wilder(14) on OHLC price.

### Stop / Target Geometry

| Constant | Value | Meaning |
|----------|-------|---------|
| C_STOP_ATR | 1.2 | Stop distance = 1.2 × ATR |
| C_T1_ATR | 1.25 | T1 target = 1.25 × ATR |
| C_T2_ATR | 3.0 | T2 target = 3.0 × ATR |
| C_BE_ATR | 0.30 | Breakeven trigger = 0.30 × ATR past entry |
| C_TRAIL_ATR | 1.50 | Trail distance = 1.50 × ATR |
| C_TRAIL_DLY | 5 bars | Trail does not activate until 5 bars after entry |
| C_T1_RATIO | 0.50 | 50% of position exits at T1 |
| C_STOP_FLOOR_PTS | 20 pts | Minimum stop distance regardless of ATR |
| C_STOP_CEIL_PTS | 40 pts | Maximum stop distance |
| C_T1_FLOOR_PTS | 25 pts | T1 minimum |
| C_T1_CEIL_PTS | 50 pts | T1 maximum |
| C_T2_FLOOR_PTS | 75 pts | T2 minimum |
| C_T2_CEIL_PTS | 125 pts | T2 maximum |
| C_ATR_PER | 14 | ATR period (Wilder) |
| C_RM_FLOOR | 0.60 | Risk multiplier minimum (v12.1 fix, was 0.80) |

### Cooldowns & Warmup Gates

| Constant | Value | Meaning |
|----------|-------|---------|
| C_COOLDOWN_AFTER_TRADE | 5 bars | Bars blocked after any completed trade (v12.3 halved from 10) |
| C_COOLDOWN_AFTER_LOSS | 10 bars | Bars blocked after a losing trade (v12.3 halved from 15) |
| C_POST_STOP_COOLDOWN | 10 bars | Bars blocked after a stop-out (v12.3 halved from 15). Direction-specific — opposite direction is allowed immediately |
| C_OPEN_COOL | 36 bars | Bars suppressed at RTH open before entries allowed |
| C_VWAP_MATURE | 40 bars | Bars required before VWAP-dependent modes (M1, M2, M3) fire |
| C_DELTA_MATURE | 25 bars | Bars required before cumulative delta is considered mature |
| C_STRUCT_LB | 25 bars | Structure lookback for control score / swing detection |
| C_MAX_LOSSES | 2 | Hard block on new entries when `ConsecLoss >= 2` |

### Volume Spike Cooldown

After an abnormally large bar (volume >= C_SPIKE_ATR_M × ATR-equivalent):

| Constant | Value | Meaning |
|----------|-------|---------|
| C_SPIKE_ATR_M | 3.0 | Volume spike threshold multiplier |
| C_VCOOL_THRESH | 7.0 | Vol cooldown delta threshold |
| C_VCOOL_PAUSE | 40 bars | Bars blocked after a spike |
| C_VCOOL_REENTRY | 3 bars | First re-entry allowed after N bars within pause |
| C_SPIKE_COOL | 20 | Spike detection lookback |

### Signal Scoring

| Constant | Value | Meaning |
|----------|-------|---------|
| C_MIN_SCORE_M1 | 4 | Minimum raw score for M1 to arm |
| C_MIN_SCORE_ALL | 3 | Minimum raw score for M2, M3, M4 to arm |
| V18A_QUALITY_FLOOR | **40** (v12.26) | qScore100 must be >= 40 to submit. Lowered from 50 on 2026-05-18 after NQM6 session showed M2 peak Q=46 / M4 peak Q=40 all near-missing the prior floor |

### MNQ vs NQ — point values

Auto-detected from symbol name. Sierra's `sc.CurrencyValuePerTick` is the source; the strategy falls back to hardcoded values if Sierra reports zero.

> **IMPORTANT — read this carefully.** `TICK_VAL` is per **tick** (0.25 pts), not per point. The fallback values $5.00 and $0.50 are tick values. The strategy derives the dollar-per-point value as `PtVal = TICK_VAL / TICK`.

| Symbol | tick size | $/tick (fallback) | **$/point (actual)** |
|--------|-----------|-------------------|----------------------|
| NQ | 0.25 | $5.00 | **$20.00** |
| MNQ | 0.25 | $0.50 | **$2.00** |

So a 30-point NQ winner logs `DayPnL ≈ $600`, and a 30-point MNQ winner logs `DayPnL ≈ $60`. Earlier playbook revisions mislabeled the $5.00/$0.50 figures as "/pt" — those numbers are per tick. CSV `DayPnL` and `TotalPnL` use the real point value.

Commission: $5.00 RT. Slippage: 0.25 pts/side (used in hypothetical P&L only, not order logic).

---

## 7. Control Score

A composite score (-5 to +5) computed every bar that gates mode entries. The gate strictness varies by mode.

**Five components** (each +1 or -1):

| Component | Long Signal | Short Signal |
|-----------|-------------|--------------|
| Delta recency | Recent cumDelta > older × 1.2 and positive | Recent < older × 1.2 and negative |
| Delta/price correlation | Price up + cumDelta up | Price down + cumDelta down |
| Imbalance aggression | Ask% >= bull threshold | Bid% >= bear threshold |
| Absorption proxy | Persistent abs-buy bars >= 3 | Persistent abs-sell bars >= 3 |
| VP position | Close > VP Value Area High | Close < VP Value Area Low |

Score is clamped to [-5, +5]. If delta is not yet mature (< `C_DELTA_MATURE` bars), control score is forced to 0.

### Per-mode gate strictness (v12.25)

| Mode | Long blocked when | Short blocked when | Rationale |
|------|-------------------|--------------------|-----------|
| M1 | `controlScore < 0` | `controlScore > 0` | Strict — VWAP trades require flow alignment |
| M2 | `controlScore < 0` | `controlScore > 0` | Strict |
| M3 | `controlScore < 0` | `controlScore > 0` | Strict — added in v12.25 |
| M4 | `controlScore < 0` | `controlScore > 0` | Strict |
| M6 | `controlScore < -1` | `controlScore > +1` | **Soft (±1)** — M6 already has `pImb->direction` + `DL/DS` alignment; strict would over-suppress |
| M8 | `controlScore < 0` | `controlScore > 0` | Strict — added in v12.25; mirrors v12.20 M5 intent |

> **Why the M3/M6/M8 retrofit (v12.25):** A live trade on 2026-05-13 (M8 SELL @ 29301 with CtrlScore=+1) lost -$685 on a full stop. With the new gates, that trade would have been blocked. The retrofit also closes the M3 omission and adds a soft layer to M6 for symmetry.

---

## 8. Divergence Detection

Cumulative delta divergence (`pDiv`) is computed each bar and feeds M4, M6, M8 signal quality. The `DivStr` column in the CSV reflects this.

**Four divergence signals** (each ±2 strength):

| Signal | Condition |
|--------|-----------|
| Trend div bull | Price near trough but cumDelta positive |
| Trend div bear | Price near peak but cumDelta negative |
| Swing div bull | Price near swing low but swing cumDelta positive |
| Swing div bear | Price near swing high but swing cumDelta negative |

Plus ±1 for persistent absorption (3+ consecutive abs-buy or abs-sell bars). Strength clamped [-5, +5].

**How it's used:**
- M4 (sweep): `divStr >= 2` lowers required score by 1
- M6 (balance): no-divergence-confirm blocks certain breakouts
- M8 (fade): `divStr <= -2` adds +2 to fade edge

---

## 9. Risk Management

### Daily Caps
- **Daily Loss:** Flatten + halt when session P&L <= `-Daily Loss $`; logged as `DAILY_LOSS`. v12.24 added dual broker/strategy view — fires on whichever view crosses
- **Daily Profit:** Flatten + halt when session P&L >= `Daily Profit Target $`; logged as `DAILY_PROFIT`
- **Max trades:** Hard stop at `Max Trades/Day` (Apex default 3; configurable)
- **Flatten time:** All positions closed at `Flatten HHMM` (default 15:55)

### Risk Multiplier
Computed each bar; clamps position sizing between 0.10x and 2.00x:

| Condition | Effect |
|-----------|--------|
| Kelly cold-start (< 10 trades) | 0.90x base |
| Positive Kelly >= 0.20 | 1.25x Kelly boost |
| Consecutive losses >= 3 | Cap at 0.50x |
| Consecutive losses >= 4 | Cap at 0.25x |
| Recovery mode (drawdown > 30% of daily budget) | 0.60x |
| RM floor (v12.1 fix) | Minimum 0.60x (was 0.80x) |

### Kelly Sizing
- Requires 5+ wins and 2+ losses before activating
- Practical Kelly = `optimalKelly × 0.5`, clamped to [0.05, 0.25]
- Cold-start override: 0.90x until 10 cumulative trades

### Vol Regime Effect on Sizing

The risk state tracks an ATR-based volatility regime that also scales position size:

| Vol Regime | ATR Ratio | Size Multiplier |
|------------|-----------|-----------------|
| 0 (very low) | < 0.7× baseline | 1.20x |
| 1 (normal) | 0.7–1.3× baseline | 1.00x |
| 2 (elevated) | 1.3–2.0× baseline | 0.70x |
| 3 (very high) | > 2.0× baseline | Further dampened by vol-target formula |

Additionally, a **vol scaler (pVS)** checks recent 5-bar high range — if unusually large, applies an additional `sizeMultiplier < 1.0` on top of the RM.

### Capital Risk Clamp

After RM and vol scaler: `riskDollars = stopDist × PtVal × baseQty` must not exceed `Capital × 2%` (e.g. $3,000 on a $150k Apex account). If it does, `baseQty` is reduced further until the dollar risk is within the cap. This is the final sizing check before order submission.

### Entry Gate Order (checked in sequence)

All of these must pass before an order is submitted:

| Gate | Condition | Logged as |
|------|-----------|-----------|
| Bar-close | Entry only on a **completed** bar (`BarClosed = true`) | Silent |
| Session DD | `sessionDrawdown > 80%` of daily loss budget | SKIP log |
| Consecutive loss | `ConsecLoss >= C_MAX_LOSSES (2)` — hard block, not just sizing | SKIP log |
| RM floor | `riskMultiplier < 0.60` | SKIP log |
| Regime filter | Mode blocked by trend/chop/vol state | GATE CSV |
| Auto-disable | Mode t-stat < -1.0 (n>=20) | SKIP log |
| Post-stop cooldown | Same-direction entry within 10 bars of a stop-out | SKIP log |
| Quality floor | `qScore100 < 40` (v12.26; was 50) | SKIP log |
| M1 dead-zone | M1 entries 12:00–13:59 ET (v12.24) | Silent |
| M1 trend gate | M1 LONG when 20-bar pc < -0.5×ATR; M1 SHORT when > +0.5×ATR (v12.24) | Silent |

Note: post-stop cooldown is **direction-specific** — it only blocks same-direction entries. Opposite direction is allowed immediately after a stop.

### Stop / Target Placement
- **M1, M2, M3, M4:** ATR-based with floor/ceiling clamps (see Section 6 constants)
- **M6:** Balance-structure levels (stop inside range edge, targets = range projections)
- **M8:** Fade-type-specific pre-computed levels (see mode table above)
- **T1 (partial exit):** Computed from actual `absPos`, not `TOTQTY` (fixed v12.15)
- **T2 (runner):** Only attached when `qty >= 2` (fixed v12.13 — prevents OCO reject on 1-lot)
- T1 partial hit does **not** reset `ConsecLoss` — runner may still stop out for a net loss (fixed v12.15)

---

## 10. Regime Filter

When enabled (default on), gates entries by market context. The regime state tracks three dimensions:

| Dimension | How computed |
|-----------|-------------|
| `trendRegime` | ±1 or 0: `trendStrength > 1.5` and price moved `> ATR×0.5` in that direction |
| `chopRegime` | 0–3: scored from VWAP-crossing frequency and EMA flatness (0=trending, 3=extreme chop) |
| `volRegime` | 0–3: ATR ratio vs baseline (see Vol Regime table in Section 9) |
| `trendStrength` | Continuous: `|deltaPerBar| / (ATR×0.1)` |

**Per-mode allow rules** (mode number = selMode):

| Mode | Blocked when |
|------|-------------|
| M1 (0) | `chopRegime >= 2` AND `trendStrength < 0.3` — OR — `trendStrength > 2.5` |
| M2 (1) | Not regime-filtered |
| M3 (2) | `trendStrength > 4.5` (strong trending market makes consolidation breakouts unreliable) |
| M4 (3) | `chopRegime >= 2` AND `trendStrength < 1.0` |
| M6 (5) | `chopRegime >= 3` AND `trendStrength < 0.3` |
| M8 (7) | Not regime-filtered |

Modes blocked by regime are logged as `GATE` rows in the CSV with `trend=` and `chop=` values.

---

## 11. Exit Logic

| Exit Trigger | CSV ExitReason | Notes |
|-------------|----------------|-------|
| T1 target hit | `TP1` | Partial exit (50% of position); stop moves to entry + ATR×0.3; runner continues |
| T2 target hit | `TP2` | Full exit via attached order |
| Stop hit | `STOP` | Market exit submitted when Low/High crosses stop price. **v12.25:** `ExitPx` records actual broker fill (with slippage > 0.5 tick), not the configured SL price |
| Trail stop | `TRAIL` | Triggered after T1 hit when trail catches up. Same broker-fill correction as STOP (v12.25) |
| Circuit-breaker | `CB` | Emergency flatten: open P&L < -(3× stop dist or 3× ATR); resets state |
| News spike | `SPIKE` | Open position flattened within 2 bars of a news/volume spike |
| Flatten time | `FLATTEN` | End-of-day forced exit at or after Flatten HHMM |
| Daily loss cap | `DAILY_LOSS` | Session P&L <= -Daily Loss $; sets DayDone |
| Daily profit target | `DAILY_PROFIT` | Session P&L >= Daily Profit Target $; sets DayDone |
| External / broker | `EXTERNAL` | **v12.24:** position went flat, no internal flatten reason, bar H/L didn't justify SL/TP. Likely Sierra-attached SL bracket, manual flatten, or broker liquidation. `ExPx` uses broker-derived fill price (`pos.CumulativeProfitLoss - CumPnL_AtEntry`) instead of bar close |

### Trail Logic Detail

- Trail only activates after T1 hit (`T1Hit = 1`)
- Default trail delay: `C_TRAIL_DLY = 5 bars` after T1 hit
- **Single-lot runner** (absPos == 1 after partial): delay becomes `C_TRAIL_DLY × 3 = 15 bars`
- Trail distance: `ATR × C_TRAIL_ATR (1.5)` — tightens to `ATR × 0.75` when price is `> 2× T1 distance` from entry
- Stop floor: never trails below entry + ATR×0.3 (breakeven minimum)

### Circuit-Breaker

Independent of the daily cap — triggers on a single trade that is going badly. Fires if open P&L < `-(max(stopDist × 3, ATR × 3))`. Sets `LastStopBar` and direction, triggering the post-stop cooldown.

### Broker-Fill Correction (v12.25)

For `STOP` and `TRAIL` exits, when `HasBrokerPx` is true and the broker-derived fill price is on the adverse side of `StopPx` by more than 0.5 tick, the strategy substitutes `ExPx = ExPxFromBroker`. This means CSV `ExitPx` will sometimes show a price slightly worse than the configured SL — that's the real fill recorded. The internal `PnL` used by `pRisk` and `pMS` is recomputed from the corrected `ExPx`, keeping equity-curve and Kelly stats aligned with the broker. Look for `[V18A EXIT][SLIPPAGE]` in the Sierra Message Log when this fires.

---

## 12. CSV Journal

File: `<DataFilesFolder>\IOF_NQ_<symbol>.csv`

**Columns:**
```
Date, Time, Event, Side, Mode, Entry, SL, TP1, TP2, Qty, Score, CtrlScore,
DivStr, Delta, BarSpeed, ExitPx, ExitReason, HoldBars, MAE, MFE,
DayPnL, TotalPnL, FadeEdge, FadeType, RiskMult, TrendReg, VolReg, ChopReg,
Version, RunID
```

**Event types:**
- `SETUP` — signal evaluated; entry pending
- `ENTRY` — order submitted (Sierra returned > 0 from BuyEntry/SellEntry)
- `EXIT` — trade closed (with ExitReason)
- `REJECT` — order rejected by Sierra
- `GATE` — regime/mode filter blocked entry
- `SESSION` — day-rollover summary row
- `ERROR` — internal error logged
- `EVAL` — per-bar diagnostic (Log Level 2 / DBG only)

**Dedup:** LRU cache (32,768 slots) prevents duplicate SETUP rows across full recalcs. Warms up from last 10 MB of existing CSV on first bar.

**Version column:** `v12.25` (live DLL) or `v12.25-py` (Python backtester output).

---

## 13. V1 Hooks (Optional Overlay)

When `V1 Hooks Mode > 0`, an additional confirmation layer is applied:

| Mode | Behavior |
|------|----------|
| 0 | Off (default production) |
| 1 | Confirmations gate: require >= N total (agg>=1, struct>=1) |
| 2 | Chop gate: block entry when chop score > threshold |
| 3 | Both confirmations and chop gates |
| 4 | Both + rolling R-Sharpe warning |

**6 confirmation signals (3 aggressive, 3 structural):**
- Aggressive: delta trend, imbalance aggression, pace
- Structural: absorption proxy, failed auction, volume relative

---

## 14. Auto-Disable (Mode Benching)

When enabled:
- Tracks per-mode win rate and t-statistic rolling
- Disables a mode when: `t-stat < -1.0` with `n >= 20 trades` (tightened v12.9 from -2.0/30)
- Disabled modes are skipped at signal evaluation; resets on session/study reload

---

## 15. News Filter

When enabled:
- Suppresses all entries ~**10 minutes** around each scheduled release (narrowed v12.8 from ~45 min)
- Integrates with Python harness calendar; uses hardcoded windows when no calendar is wired
- Default ON in the Apex eval profile (v12.24) to protect cushion from 08:30 / 10:00 / 14:00 ET spikes

---

## 16. Backtesting Procedure

### Python backtester (`backtest.py`)

Standalone replica of the strategy in Python. Reads a Sierra `.scid` file, builds 3000-volume bars, runs the full signal/risk/exit logic, writes a CSV with the same schema as live.

```powershell
# Default: NQZ25-CME.scid → IOF_NQ_backtest_NQZ25.csv
python backtest.py

# Or override paths:
python backtest.py NQZ25-CME.scid IOF_NQ_backtest_NQZ25_v1225.csv
```

CSV `Version` column reads `v12.25-py` to distinguish from live DLL output.

### Sierra Chart replay

1. Chart: Volume 3000 contracts, Bid/Ask on, RTH session, US Eastern TZ
2. Load >= 180 days of history
3. Inputs: Daily Loss $1000, Daily Profit $1000, Max Trades 6, Flatten 1555, Entry = Market (or 2 for live realism)
4. Trade > Trade Simulation on; study `Enable Auto Trading = 1`
5. Run full replay or full recalculation
6. Open `IOF_NQ_<symbol>.csv` — filter on `ENTRY`/`EXIT` for trade log; check `GATE`/`REJECT` for suppressed signals

**Standard 6-month baseline** (see `IOF_NQ_Backtest_Input_Profile.txt`):
- Regime filter: on | Fade engine: on | News filter: on | Auto-disable: on
- M1 VWAP reclaim: off

---

## 17. Version History (Key Fixes)

| Version | Fix |
|---------|-----|
| v12.1 | RM floor 0.80 → 0.60; Kelly cold-start 0.8 → 0.9. Prevented post-loss deadlock. |
| v12.2 | Quality floor 65 → 50. Real aligned setups rarely scored >= 10/15. |
| v12.3 | Cooldowns halved: after-trade 10→5, after-loss 15→10, post-stop 15→10. |
| v12.4 | Consolidated consecutive-loss counter; removed double-gate drift. |
| v12.5 | M1 VWAP reclaim toggleable via input slot 15 (default off). |
| v12.6 | Per-bar EVAL diagnostic log written at LOG_LVL >= DBG (records scores, modes, RM, regime). |
| v12.7 | HypoQty / HypoSide / HypoT1Hit / HypoPartialPnL cleared on day-reset (were stale across sessions). |
| v12.8 | News blackout narrowed ~45 min → ~10 min; was blocking ~35% of RTH. |
| v12.9 | Auto-disable threshold: t-stat < -1.0 at n>=20 (was -2.0 at n>=30). |
| v12.10 | Persistent slot numbers replaced with typed enums (PersistInt/Float/Ptr); append-only, collision-proof. Zero behavior change. |
| v12.11 | Signal gate: suppresses only full recalc / downloading; replay can submit. |
| v12.12 | Reversal mode quality scaling (M4, M6): score × 10 instead of /15. |
| v12.13 | T2 only attached when qty>=2; prevents order_rc=-1 reject on 1-lot. |
| v12.14 | M5 cooldown: LastM5Bar was never assigned; caused 1064 SETUPs / 0 ENTRYs over 180 days. |
| v12.15 | ConsecLoss not reset on T1 partial; T1 qty from absPos not TOTQTY. |
| v12.17 | DAILY_PROFIT vs DAILY_LOSS CSV reasons split (were both PNL_LIMIT). |
| v12.18 | MaximumPositionAllowed=1; TOTQTY input clamped 1..1. |
| v12.19 | Default daily caps $1000/$1000 from iof_defaults.h; ATR floor when SG_ATR<=0. |
| v12.20 | M5 tightening: phase-3 reclaim reset, phase-3 timeout, no LBL/LBS fallback, controlScore gate narrowed ±2 → 0. |
| v12.21 | M5 removed entirely (later restored in v12.22). |
| v12.22 | M5 restored with v12.20 hardening. M1 VWAP-slope directional filter added (sOKL/sOKS). |
| v12.23 | Live-readiness: M5 removed from selector. M1 enabled by default. C_MAX_LOSSES=2. Night session input slot 25 added. |
| v12.24 | M7 (Auction Reversal) removed. M1 dead-zone 12:00–13:59 ET. M1 trend direction gate (20-bar pc vs ±0.5×ATR). Apex $150K eval profile baked into SetDefaults. EXTERNAL exit tag with broker-derived fill. CumPnL_AtEntry stale-snapshot guard. Dual broker/strategy day-P/L view. Marketable-limit entry default. Strategy-internal daily-loss cap. |
| v12.25 | CtrlScore gate retrofit: M3 (strict), M6 (soft ±1), M8 (strict). M7 firing block removed (was wiping `PrevImbDir/Str/Extreme` and silently disabling M8 type 2). Broker-fill correction on STOP/TRAIL exits — `ExPx` now reflects actual fill when slippage > 0.5 tick. |
| v12.26 | `V18A_QUALITY_FLOOR` 50 → 40 (live tuning). NQM6 2026-05-18 session produced only one passing signal (M4 BUY @ 28837.50); M2 peaked Q=46 / M4 peaked Q=40 / M1 peaked Q=40, all near-missing the prior floor. **Banner string still reads v12.25 — version label not bumped.** Test 1–2 live sessions on NQM6, then verify on a second contract before keeping. |

---

## 18. File Map

```
IOF_NQ_Production_Final/
├── IOF_NQ_Autopilot.cpp              <- compile this only (Sierra ACSIL study)
├── IOF_NQ_EdgeDiscovery.cpp          <- optional separate indicator DLL (plots dist_vwap_atr, cum_delta_z from Python edge_discovery.py pool study)
├── backtest.py                       <- standalone Python backtester (replica of cpp logic)
├── edge_discovery.py                 <- Python feature/pool study
├── edge_backtest.py                  <- edge feature backtest tooling
├── ml_signal.py                      <- ML signal prediction helpers
├── playbook.md                       <- this file
├── IOF_NQ_Backtest_Input_Profile.txt
├── IOF_NQ_NQM6.CME.csv               <- live trade journal (current contract)
├── iof_unified/
│   ├── iof_defaults.h                <- risk constants
│   ├── iof_math.h                    <- FAbs/FMax/FMin
│   ├── iof_session_rth.h             <- RTH bar clock helpers
│   └── iof_structured_log.h          <- LogLine/LogEntry/etc.
├── archive/
│   └── IOF_NQ_Autopilot_v12.xx.cpp   <- versioned snapshots (do not build)
├── SYNC_FROM_IOFv02.ps1
├── VERIFY_PRODUCTION_BUNDLE.ps1
├── README_PRODUCTION_FINAL.txt
└── VERSION.txt
```

---

## 19. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| No trades, all REJECT order_rc=-1 | Two attached TP orders on 1-lot | Rebuild with v12.13+ |
| All entries blocked after one loss | RM floor too high + Kelly=0.25 override | Rebuild with v12.1+ |
| Reversal modes never fire in backtest | Quality score too strict for reversal modes | Rebuild with v12.12+ |
| DAILY_PROFIT and DAILY_LOSS both show PNL_LIMIT in CSV | Pre-v12.17 | Rebuild with v12.17+ |
| No orders submitted during replay | Old gate blocked replay submissions | Rebuild with v12.11+ |
| DLL name mismatch on load | Source rename without updating SCDLLName | Run `VERIFY_PRODUCTION_BUNDLE.ps1` |
| Duplicate SETUP rows in CSV | First-bar LRU warmup; expected behavior | LRU covers last 10 MB of log |
| All entries skipped after 2 losses | `ConsecLoss >= C_MAX_LOSSES=2` hard gate | Expected; resets on winning trade or session reset |
| Entries skipped with SKIP DD log | sessionDrawdown > 80% of daily budget | Expected; tighter than the full daily loss cap |
| M8 firing but qScore too low | `pFade->active` is false; falls to /15 scale | Verify Fade Engine input = 1; check EVAL log for `edgeScore` |
| Trail stops too early on runner | `absPos==1` uses 3× trail delay but market turns first | Expected for 1-lot — trail is intentionally delayed |
| CB exit (circuit-breaker) appearing | Open P&L < -(3× stop or 3× ATR) | Trade went well past normal stop range; check stop placement |
| ExitReason `EXTERNAL` showing up | Sierra-attached SL bracket filled before strategy detected it, manual flatten, or broker liquidation | Inspect Sierra Trade Activity Log; correlate timestamp with broker journal |
| CSV `ExitPx` doesn't match my expectation of SL fill | v12.25 broker-fill correction is active — `ExPx` shows real broker fill, not configured SL | Expected; look for `[V18A EXIT][SLIPPAGE]` in Sierra Message Log |
| M8 fade never fires on range setups | Pre-v12.25: M7 was silently wiping `PrevImbDir/Str/Extreme` state | Rebuild with v12.25+ |
| Banner shows `v12.24` after rebuild | Sierra cached old DLL | Restart Sierra fully; verify `IOF_NQ_Autopilot_64.dll` rebuild timestamp |
| M1 dead-zone firing at wrong hour | Chart timezone is not US Eastern | Sierra → Global Settings → Time Zone → New York (US Eastern) |
| `DayPnL` in CSV looks 4× too high vs playbook expectation | Old playbook revisions labeled $5.00/$0.50 as "/pt" — those are tick values | See Section 6 — actual point values are $20 (NQ) and $2 (MNQ) |
