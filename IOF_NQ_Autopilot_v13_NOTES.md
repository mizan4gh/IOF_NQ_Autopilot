# IOF NQ Autopilot v13 — rewrite notes

Clean-room refactor of `IOF_NQ_Autopilot.cpp` (v12.37) → `IOF_NQ_Autopilot_v13.cpp`.
New file, new study function (`scsf_IOF_NQ_Autopilot_v13`), new DLL name, new CSV
path (`IOF_NQ_v13_<symbol>.csv`). **Production v12.37 is untouched** and the
commit-sync hook does not mirror this file to Sierra.

1625 lines vs ~3950 in v12.37 (~59% smaller). Six sections, each labelled in
the source: (1) Session management, (2) Risk management, (3) Signal detection,
(4) Order placement, (5) Trade management, (6) Logging.

---

## ⚠️ Read this before going live

v13 is a **behavioral change**, not a drop-in equivalent. Two of the four modes
were redefined because their v12.37 implementations were entangled with the
subsystems you asked to delete:

- **M8 is narrower.** v12.37 M8 had 4 fade types built on the Trap machine, the
  M7 auction-reversal scoring, and iceberg detection — all removed here. v13 M8
  keeps only the failed-breakout fade (at a balance edge) and the
  trend-exhaustion fade. Fewer M8 fires, different ones.
- **The confirmation score lost 4 sub-signals** (single-print, iceberg, 50-day
  VP, trap) whose subsystems are gone. The score ceiling drops slightly, so at
  the same floor (50) v13 is marginally *more* selective on M1/M4 than v12.37.

Net effect: the existing `backtest.py` oracle models **v12.37** behavior, so it
does **not** validate v13's M8 or the new score. **v13 must get its own
3-contract A/B before any live session.** Do not assume prior A/B verdicts carry
over.

### Defaults deliberately differ from your spec
You asked for floors M1=55 / M4=40 / M6=50 / M8=60. The file exposes all four as
inputs, but ships them at **50**. Reason: the M1=55/M4=40/M6=50/M8=60 profile was
A/B-tested 2026-06-12 and lost ~$6K on all three contracts — M4=40 dumps the
low-quality 30–49 sweep band (4th independent M4-floor falsification). Set the
inputs to your profile any time to A/B it; it just isn't the shipped default.

`VP-Level Targets` input defaults **off** (that A/B is still pending). Structure
stops beyond the swept extreme were *not* added — that was falsified 2026-05-27.

---

## 1. Removed logic

| Removed | Why |
|---|---|
| **M2** (VP-level mean-reversion) | Floor lever twice-falsified; not in your keep-list |
| **M3** (consolidation breakout/rejection) | Not in keep-list; near-zero fires in funnel audit |
| **M5** (trap reversal) + whole TrapState machine | Requested removal; was a backtest drag |
| **M7** (auction reversal) + rvVerifyScore machinery | Requested removal; -$4,981 in backtest |
| **Anti-martingale** (streak-based size scaling) | Requested removal |
| **Recovery mode** (drawdown re-entry target) | Requested removal |
| **Time-decay** (barsSinceLastTrade size taper) | Requested removal |
| **Budget multiplier** (riskBudget ratio scaling) | Requested removal; ran at $0 anyway |
| **Equity-curve & profit-scale multipliers** | Curve-fit defensive layers |
| **Consec-loss RM taper** | Replaced by a hard 2-loss halt (cleaner) |
| **VolScaler** (range-based size dampening) | Single-lot makes it a no-op |
| **Hypo shadow P&L** (parallel paper-trade journal) | Dead weight; manual-exec workflow doesn't use it |
| **AUTO_DISABLE / ModeStats** (t-stat mode benching) | Overfit; per-mode enable inputs replace it |
| **AdaptiveThresholds** (EMA-calibrated gates) | Replaced by fixed constants |
| **Iceberg detection** | Experimental; only fed removed modes |
| **50-day composite VP** | Only fed M2/M10 scoring |
| **M1 pullback variants** (wick-reject) | dip+reclaim is THE M1 trigger |
| **Overnight session** (SESSION_START=100) | Untested; RTH-only |
| **Stacked cooldowns** (separate after-trade/after-loss/post-stop returns) | Unified into one `max()` |
| **`goto TRADE_MGMT`** | Replaced by structured `volBlock` flag + early returns |

## 2. Preserved logic (hard-won — kept verbatim in behavior)

- **DayOpenPnL pre-entry re-snapshot latch** (v12.27/28/29) — stops carryover
  P&L masquerading as today's, which once armed the profit cap on bar 1.
- **Risk-EMA per-bar gate** (v12.32) — AutoLoop=1 fires the study 50–200×/bar;
  vol-regime EMA and any decrement logic must advance once per bar only.
- **vcool decrement once-per-bar** (v12.31) — same AutoLoop hazard.
- **Broker-fill exit reconciliation + EXTERNAL tagging** (v12.24/25) — derives
  the true fill from realized P&L; corrects STOP/TRAIL slippage; flags
  Sierra-bracket/manual exits instead of mislabeling them STOP.
- **Tick-snap of all bracket levels** (v12.26) — detector and broker agree on grid.
- **Single-target attach when qty==1** (v12.13) — avoids "parent 1, attached 2".
- **Early-scratch exit** (v12.37, A/B validated) — abandon dead pre-T1 trades.
- **M1 dip+reclaim trigger** (v12.34, A/B validated).
- **M1 dead-zone 12:00–13:59 + 20-bar trend gate** (v12.24).
- **Late-entry gate ≥15:00** (v12.36, A/B validated + observed live).
- **Open cooldown 10 min, flatten 15:55, RTH 09:35** session frame.
- **Signal-only gate = full-recalc / hist-download only** (v12.11) — the fix for
  the "29 SETUPs, 0 ENTRYs" bug. Replay is allowed to submit.
- **CSV LRU dedup with 10 MB warmup** (v11) — no duplicate SETUP rows on recalc.
- **Spike + major-vol circuit breakers** — range/delta spike → 20-bar block;
  range/ATR≥7 → 40-bar block; in-trade spike → flatten.
- **RiskMultiplier = Kelly × Volatility × Drawdown** (your spec), RM_FLOOR 0.60.

## 3. Backtest checklist

1. **Re-validate, don't assume.** Port the v13 M8 + reduced score into
   `backtest.py` (or write a v13 model) before trusting any number. The current
   `backtest.py` is a v12.37 oracle.
2. **3-contract gate.** Run NQZ25 + NQM5 + NQH6. Ship only if all three agree
   `better` (or non-worse) on net P&L *and* daily Sortino. This is the standing
   rule that has reverted every overfit so far.
3. **Floors at 50.** Confirm the shipped default (all modes 50) reproduces
   v12.37-class selectivity. Then, if you want, A/B your 55/40/50/60 profile —
   expect M4=40 to fail again.
4. **Mode isolation.** Use the per-mode enable inputs to A/B each mode's
   contribution (e.g. M8-on vs M8-off) on all three contracts.
5. **VP-targets lever.** Run the pending `backtest_vp_targets_ab.py` before
   flipping `VP-Level Targets` on.
6. **Data hygiene.** Use NQH6.CME.scid (not the NQH26 duplicate). Confirm the
   bar-builder low=0 fix is in your harness for price_scale=100 files.
7. **Sanity counts.** Expect ~1 trade/week per contract under prod caps. A v13
   that suddenly fires 3–5×/week means a gate regressed — investigate before
   celebrating.

## 4. Debug checklist — "SETUP appears but no order submitted"

Walk these in order; each maps to a specific early-return in section 4 / trade
management. Set Log Level = 2 (dbg) and read the `[V13 ...]` message log.

1. **`[V13 SETUP]` logged but no `[V13 ENTRY]`?** → look for a `signalOnly`
   return. If `IsFullRecalculation` or historical-download is active, the SETUP
   is logged but the order is intentionally suppressed. Live bar only submits.
2. **`[V13 REJECT] ... ORDER_RC`** → Sierra refused the order. Check
   `Trade >> Auto Trading enabled`, account selected, `Enable Auto Trading`
   input = 1, and that `SendOrdersToTradeService` is on (it follows IN_LIVE).
3. **No SETUP at all, but you expected one?** → a gate fired before selection.
   Likely culprits, each with a `[V13 SKIP]` or `[V13 REJECT]` line:
   - `no delta dir` — flat delta bar (both Delta0 and lookback delta zero).
   - `open cooldown` / `late-entry gate` / `news window` — session timing.
   - `vol cooldown` — spike or major-vol pause active.
   - `COOLDOWN` — unified post-trade/loss/stop cooldown still running.
   - `RM_FLOOR` — riskMultiplier < 0.60 (check Kelly/vol/dd factors in the line).
   - `SESSION_DD` — session drawdown > 80% of daily loss budget.
   - `QUAL_FLOOR` — quality < the mode's floor (line shows q vs floor).
   - `REGIME` / `VOL_EXTREME` — regime filter or ATR-extreme block.
   - `M1_DEADZONE` / `M1_TREND` — M1-specific time/trend gates.
4. **SETUP and ENTRY logged, but flat next bar?** → check the exit line. A
   `SCRATCH` at EntryBar+4 means early-scratch fired (no favorable excursion).
   An `EXTERNAL` exit means Sierra's bracket or a manual flatten closed it —
   reconcile against the Trade Activity Log.
5. **Position stuck / state desync** → look for `LiveTradeDir nonzero at
   rollover` (forced reset) or `symbol changed; reload study` (stale per-symbol
   state — reload the study).
6. **Counts capped early** → `Trades>=MAX_TRADES`, `DayLosses>=MAX_DAY_LOSS`,
   or `ConsecLoss>=MAX_CONSEC` silently halt new entries for the day.

---

*Generated 2026-06-12 alongside the v13 rewrite. The v13 .cpp and this file are
uncommitted; nothing has been synced to Sierra.*
