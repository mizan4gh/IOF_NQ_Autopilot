#if defined(__has_include) && __has_include("sierrachart.h")
#include "sierrachart.h"
#else
#include <cstdint>
#define SCDLLName(name)
#endif
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cfloat>
#include <cstring>
#include <ctime>

// Sierra remote/local ACSIL: SCDLLName must appear near the top of the file (after sierrachart.h).
// Kept here above the changelog banner so the remote build server's scan window always finds it.
SCDLLName("IOF_NQ_Autopilot_FixBrkt500")

// ============================================================================
//  VARIANT BUILD — v12.38-FB500 — ONE KNOB vs PRODUCTION.
// ============================================================================
//  Production v12.38 in every respect EXCEPT the exit bracket:
//
//    fixed $500 stop / $500 target per contract, replacing the ATR stop +
//    T1/T2 + breakeven + ATR-trail machinery. Distances derive from PtVal so
//    the build is symbol-correct: NQ ($20/pt) -> 25.00 pts, MNQ ($2/pt) ->
//    250.00 pts. 1R risk:reward = 1:1.
//
//  UNCHANGED from production — deliberately, so this is a single-variable
//  test and any A/B delta is attributable to the bracket alone:
//    - All modes active (M1-M4, M6, M8; M5/M7 inactive as in v12.24+)
//    - V18A_QUALITY_FLOOR = 50, M8 floor = 60
//    - Max Trades = 1/day, Daily Loss = $800, Daily Profit off
//    - News / regime / auto-disable filters, RM floor, cooldowns, 15:00 gate
//
//  Bypassed BY the fixed bracket (a fixed target and a runner are mutually
//  exclusive — leaving these live would move the stop or cut the target and
//  the realised R would stop being 1:1):
//    - v12.37 early-scratch
//    - T1 partial exit + breakeven shift
//    - ATR trail
//    - T2 leg (T1Px == T2Px; one OCO target for the whole position)
//
//  SIDE EFFECT ON DAILY RISK — favourable, but know it: production's ATR stop
//  clamps to C_STOP_CEIL_PTS = 40 pt = $800/contract, so a worst-case NQ day
//  is currently -$800 and exactly consumes the $800 daily-loss cap. A fixed
//  $500 stop cuts that worst case to -$500. MT=1 and DL=$800 are unchanged
//  and still correct; the cap simply stops being the binding constraint.
//
//  STATUS: A/B in progress (backtest_m2m4_qf40_fixbrkt_ab.py ladder, 2026-07-30).
//  On the first 4 frozen contracts the fixed-bracket STEP measured +$1,880
//  pooled at t=+0.23 — i.e. indistinguishable from zero, tested there on top
//  of the M2+M4/QF40/MT=3 stack rather than on production. THIS build has not
//  been A/B'd on its own yet. Separate DLL; does NOT touch the live study.
// ============================================================================

// ============================================================================
//  IOF NQ — Pure Orderflow Autopilot
//  Sierra Chart ACSIL Study
//
//  Version: v12.38 (June 2026; M8 Fade Engine quality floor 50->60, sheds losing Q<=50 type-2 fades)
//  Version: v12.37 (June 2026; early-scratch exit: abandon dead pre-T1 trades at stop-eligibility bar)
//
//  CHANGES SINCE v11:
//    [v12.1] RM floor 0.80 -> 0.60, Kelly cold-start 0.8 -> 0.9.
//            Fixes post-first-loss deadlock where Kelly=0.25 override
//            pushed riskMultiplier ~0.71 below the 0.80 floor, blocking
//            all subsequent entries until Kelly recovered.
//
//    [v12.2] Quality floor 65 -> 50. Prior threshold required finalScore
//            >= 10/15 which realistic aligned setups rarely reach.
//            50 requires >= 8/15.
//
//    [v12.3] Cooldowns halved: AFTER_TRADE 10->5, AFTER_LOSS 15->10,
//            POST_STOP 15->10. Stacked they still produce meaningful
//            separation on 5000-volume bars.
//
//    [v12.4] Consecutive-loss check consolidated to single canonical
//            counter (ConsecLoss). pRisk->sessionConsecLosses is
//            advisory only, eliminating double-gate drift.
//
//    [v12.5] M1 (VWAP reclaim) toggleable via input slot 15.
//            Default OFF per manual.
//
//    [v12.6] Per-bar EVAL diagnostic log at LOG_LVL >= DBG.
//            Records scores, modes, RM, regime each evaluated bar.
//
//    [v12.7] HypoQty / HypoSide / HypoT1Hit / HypoPartialPnL cleared
//            on day-reset (were stale across sessions).
//
//    [v12.8] News blackout windows narrowed from ~45 min to ~10 min
//            around each release. Prior blocked ~35% of RTH.
//
//    [v12.9] AUTO_DISABLE threshold hardened from t-stat < -2.0 (n>=30)
//            to t-stat < -1.0 (n>=20). Benches underperforming modes
//            sooner — prior threshold required a mode to be
//            significantly-negative, letting it bleed ~30 losing
//            trades before disablement.
//
//    [v12.10] Persistent slot numbers replaced with typed enums
//             (PersistInt / PersistFloat / PersistPtr). Pure mechanical
//             rename, zero behavior change. Adding new state is now
//             append-only and slot-collision-proof.
//
//    [v12.11] Signal-only gate narrowed. Previously blocked orders when
//             IsReplayRunning or when bar wasn't the chart's very last
//             — this produced SETUP rows with no ENTRY rows during
//             replay (trade-sim use case) and after any full-recalc
//             event. New gate follows Sierra's documented auto-trading
//             pattern: suppress only during IsFullRecalculation or
//             DownloadingHistoricalData. Replay is allowed to submit;
//             Sierra's trade-sim engine fills.
//
//    [v12.12] Quality scoring rescaled for reversal modes. Previous
//             formula (score*100/15) required score >= 8 for any mode
//             to clear floor 50. Live logs showed M4/M5 arming at
//             scores 4-6 repeatedly (qScore 26-40) without ever firing.
//             Reversal modes M4 (sweep+reclaim), M5 (trap reversal),
//             M6 (balance breakout), M7 (auction reversal) now use
//             score*10 — same scale M8 fade already uses via edge*10.
//             Score 5 -> 50, score 6 -> 60. M1/M2/M3 keep stricter
//             /15 scaling since they fire more often and need selectivity.
//             Changes mode activity without changing the 50 floor itself.
//
//    [v12.13] Attached-order quantity fix. Previous code set both
//             Target1Price and Target2Price unconditionally, which creates
//             TWO attached take-profit orders. When parent order qty=1,
//             Sierra rejects with "Parent quantity 1 < total Attached
//             Orders quantity 2" (order_rc=-1 in trade service log).
//             Now: qty>=2 splits parent into T1 (partial) + T2 (runner)
//             via OCOGroup1Quantity; qty=1 attaches only T1 (no T2).
//             Fixes the REJECT order_rc=-1 that blocked all single-
//             contract entries regardless of Sim/Live mode.
//
//    [v12.14] M5 cooldown tracking fix. LastM5Bar was declared, read in
//             the cooldown check, and reset on session, but was NEVER
//             ASSIGNED after M5 armed. The effective cooldown was always
//             ignored. M5 re-fired every bar while trap phase==3 persisted,
//             producing 1064 SETUPs vs M4's 34 over 180 days — and 0
//             conversions, because M5's volume meant most signals landed
//             on non-submittable replay bars. M5 also wins same-bar
//             priority over M4, so this spam actively crowded out M4.
//             Fix: set LastM5Bar = Idx whenever M5 arms (independent of
//             entry outcome), enforcing the intended C_M5_COOLDOWN=15.
//
//    [v12.20] M5 quality tightening (derived from backtest analysis):
//             (a) Phase-3 reclaim reset: if price closes back through absorbPx the
//                 trap is invalidated (was missing in C++; Python already had it).
//             (b) Phase-3 timeout: trap auto-resets after C_M5_PHASE3_MAX=20 bars in
//                 phase 3, preventing stale traps from generating signals.
//             (c) Lookback fallback removed: m5ScL/m5ScS now require current-bar delta
//                 (DL/DS). LBL/LBS fallback made entry bar ambiguous.
//             (d) controlScore gate narrowed: ±2 → 0. Requires neutral/correct-side
//                 control at entry bar, not just "not strongly opposing".
//
//    [v12.21] M5 (Trap Reversal) mode removed entirely.
//             3000-vol-bar backtest (NQZ5 2025, NQH26 2026) showed M5 generated
//             70-90% of all trades at 12-26% WR across both regimes. M6 and M4
//             remain. TrapState struct retained (phases 0-2 feed M4 scoring via
//             S11/trapConf); phase-3 progression and all M5 signal logic removed.
//
//    [v12.22] M5 (Trap Reversal) mode restored. All modes active: M1–M8.
//             Phase-3 trap progression and M5 signal block reinstated from v12.20
//             state (reclaim-reset, phase-3 timeout, DL/DS-only entry, ctrl>=0 gate).
//
//    [v12.37] Early-scratch exit. On the first bar the stop becomes eligible
//             (Idx==EntryBar+4), a pre-T1 trade whose MFE never reached 25%
//             of stop distance is closed at market ("SCRATCH") provided price
//             is still better than the stop level. Motivated by sortino_audit:
//             100% of downside variance = pre-T1 stops, all dead by bar 3
//             (MFE<=17pt) while winners run 59-277pt. Cross-contract A/B
//             2026-06-09 (backtest_early_scratch_ab.py): NQZ25 +$410 (Sortino/
//             day 1.55->2.70), NQM5 tied (slow-start winner NOT scratched),
//             NQH6 +$440 (MaxDD -$2,480->-$2,040). Counts as a stop for
//             cooldown state when underwater.
//
//    [v12.36] Late-entry gate. Skip new SETUPs at BarHHMM >= 1500. Cross-
//             contract A/B (NQZ25 + NQM5) on 2026-05-28: NQZ25 +$395 (vetoed
//             one losing late M4), NQM5 $0 (no late trades). Backed by 3-
//             contract walk-forward over 267 candidates (+$12,370 lift),
//             extracted as the only ML-derived hand-rule that survived
//             expanding-month forward holdout (see project_r2b_late_entry_skip
//             memory). Mechanism: ≤55 min before FLAT_TIME=1555 is too little
//             runway for reversal modes (M2/M4) to reach T2/trail. Trade
//             management still runs above the gate for in-flight positions.
//
//    [v12.32] Per-bar gate on the hoisted risk-EMA block — completes the
//             v12.31(b) fix. sc.AutoLoop=1 calls the study ~50-200x per
//             3000-vol bar; the v12.31 hoisted block had no per-bar gate.
//             updateTimeDecay's barsSinceLastTrade++ inflated ~50-200x:
//             timeDecayFactor (thresholds at 50/200 BARS) pinned to 0.5
//             within ~1-2 clock-bars of every trade, halving riskMultiplier.
//             updateVolRegime's alpha=0.1 EMA also collapsed to current-bar
//             ATR (no smoothing) and baselineATR's 1% leak drifted ~63%/bar.
//             Added int lastUpdateBar on InstitutionalRiskState (init -1)
//             and gated the hoisted call by Idx != pRisk->lastUpdateBar,
//             same pattern as the v12.31(a) vcool fix.
//
//    [v12.31] Two correctness fixes from the deep audit.
//             (a) Vcool decrement per-bar, not per-call. AutoLoop=1 calls the
//                 study on every tick (~50-200 calls per 3000-vol bar); the
//                 unconditional decrement at the top of TRADE_MGMT collapsed
//                 C_VCOOL_PAUSE=40 to <1 bar in practice. Volatility cooldown
//                 was effectively disabled. Gated by Idx != lastLogBar
//                 (repurposed the previously-unused field on VolCooldownState).
//             (b) pRisk->updateVolRegime + updateTimeDecay moved from deep
//                 inside setup detection (called only on bars that pass all
//                 gates) to the top of the function (every bar). The EMA on
//                 ATR now samples regularly; volRegime classification stops
//                 going stale on no-trade days. computeRiskMultiplier still
//                 runs lazily right before the RM-floor check.
//
//    [v12.30] Revert V18A_QUALITY_FLOOR 40->50 after cross-contract A/B.
//             Python backtest (2026-05-20) on NQZ25-CME.scid and NQM5.CME.scid
//             with all-modes-at-QF=40 (the v12.26 setting): NQZ25 lost $2,955
//             over ~6 months while NQM5 made $8,145 — same magnitude flip and
//             same NQZ25-vs-NQM5 disagreement shape as the 738d5d5 revert.
//             Per [[feedback-cross-contract-ab]], not shippable. Back to 50.
//             M4 still shows positive edge at QF=40 on both contracts in
//             isolation; a future per-mode floor (M4@40, others@50) is a
//             plausible v12.31 candidate but needs a 3rd contract before
//             shipping.
//
//    [v12.29] Per-chart pre-entry latch + day-rollover reset. v12.28 stored
//             the "has entered this load" latch as a function-static, which is
//             shared across every chart running this DLL in the same Sierra
//             process. NQM6's first entry silently latched NQH6's pre-entry
//             tracker, leaving NQH6's DayOpenPnL frozen at whatever it
//             snapshotted at reload — the exact failure mode v12.27 and v12.28
//             were trying to fix, just one chart later. Same code also never
//             cleared the latch on day rollover, so day 2 onward relied on
//             the single day-reset CumPL snapshot. Fix: latch is now a
//             per-chart PersistInt (PI_HasEnteredThisLoad=92), and the day-
//             reset block clears it. Also adds silent-skip diagnostics
//             ([V18A SKIP] tag) on the pre-setup return paths to localize
//             "no SETUP / no TRADE" sessions.
//
//    [v12.28] DayOpenPnL tracks broker CumPL until first entry. v12.27
//             locked the baseline on the first flat bar after DLL load,
//             but Sierra/Rithmic position sync can lag: CumPL=$0 at first
//             bar then jumps to carryover value (e.g. $1640 from prior
//             trade) minutes later. v12.27 captured the $0, then late
//             carryover was counted as today's strategy P&L and armed
//             DAILY_PROF (observed 2026-05-20 NQM6: DLL loaded 05:10
//             with CumPL=0, broker pushed $1640 by 08:35, gate armed,
//             zero entries all RTH). New rule: keep DayOpenPnL=CumPL on
//             every pre-entry flat bar. Once LiveTradeDir transitions to
//             non-zero (strategy entered), s_HasEnteredThisLoad latches,
//             freezing the baseline so our own wins aren't zeroed. Mid-
//             trade reloads: if LiveTradeDir is non-zero on bar 1 from
//             persistent state, latch fires immediately and DayOpenPnL
//             retains its persistent pre-reload value (best available).
//
//    [v12.27] DayOpenPnL re-snapshot on DLL load. Mid-session recompile (or a
//             Sierra/Rithmic position-sync lag at first-bar-of-day) left
//             DayOpenPnL=0 while pos.CumulativeProfitLoss carried yesterday's
//             manual-trade total. brokerDayPnL = CumPL - 0 + open then
//             falsely armed DAILY_PROF, blocking all entries (observed
//             2026-05-19 NQM6: brokerDayPnL=$1640 with broker DailyP/L=$0
//             and CSV empty for the day). Fix: on first flat bar after each
//             DLL load, snapshot DayOpenPnL = pos.CumulativeProfitLoss
//             (s_LoadBaselineDone static-local at line ~1628). Gated on
//             CurQ==0 so mid-trade reloads don't capture unrealized P&L.
//             Emits [V18A INIT] log line confirming the re-snapshot.
//             (Superseded by v12.28: one-shot lock was too eager when
//             broker P&L arrived after first flat bar.)
//
//    [v12.26] V18A_QUALITY_FLOOR 50->40 + tick-snap M6/M7/M8 + daily-profit
//             gate diag logging (commits 92316b5, 04e4e0b, 2e40016).
//
//    [v12.25] CtrlScore gate retrofit on M6 and M8 (post-live analysis 2026-05-13):
//             (a) M8 (Fade Engine): strict gate. Block LONG when controlScore<0,
//                 block SHORT when controlScore>0. Mirrors the M4/M5 ±0 intent
//                 (v12.20 changelog (d)). Live trade 2026-05-13 SELL M8 fired
//                 against CtrlScore=+1 (buyer bias) and lost -$685 on full stop;
//                 this gate would have blocked it. pFade->reset() on rejection
//                 so downstream FadeEdge/FadeType logs cleanly.
//             (b) M6 (Breakout): soft gate. Block LONG only when controlScore<-1,
//                 block SHORT only when controlScore>+1. Softer threshold because
//                 M6 already enforces flow alignment via pImb->direction and the
//                 DL/DS current-bar delta requirement at line 3116-3117 — strict
//                 ±0 would suppress valid breakouts on neutral-narrow control.
//             (c) M3 (Consolidation Breakout/Rejection): strict gate. Block LONG
//                 when controlScore<0, block SHORT when controlScore>0. M3 was
//                 the third remaining ungated mode after the M6/M8 retrofit.
//             (d) M7 firing block removed. M7 was excluded from the signal
//                 combination in v12.24 but its rvVerifyScore>=5 branch still
//                 fired m7L/m7S AND cleared PrevImbDir/Str/Extreme. M8 type 2
//                 (range fade at line ~3296) reads those vars, so every
//                 M7-quality setup silently disabled M8 type 2 on subsequent
//                 bars. rvVerifyScore computation kept (M8 type 2 still uses
//                 it). m7L/m7S/m7Stop declarations kept as harmless false/0
//                 placeholders for the diagnostic log and the now-unreachable
//                 selMode==6 exit-computation branch (queued for cleanup).
//             (e) STOP/TRAIL exits now use broker-derived fill price (when
//                 available and adverse) instead of hardcoded StopPx. Previous
//                 behavior recorded the configured SL as the fill, causing
//                 CSV ExitPx to disagree with broker DayPnL by the slippage
//                 amount (1 tick on Wed 2026-05-13's M8 stop-out). pRisk and
//                 pMS now see realized PnL, keeping equity-curve and Kelly
//                 stats aligned with the broker. Guarded by the existing
//                 v12.24 5x-stop-dist plausibility check on HasBrokerPx and a
//                 direction check (broker fill must be on adverse side of SL).
//
//    [v12.24] Backtest-derived filter sync:
//             (a) M7 (Auction Reversal) removed from signal combination and
//                 priority selector. NQZ5 2025 backtest: 2 trades, -$4,981.
//             (b) M1 dead-zone block: no M1 entries 12:00-13:59 ET.
//                 NQH26 analysis: 0% WR in that window across all tested contracts.
//             (c) M1 trend direction gate: block M1 LONG when 20-bar price
//                 change < -0.5*ATR; block M1 SHORT when > +0.5*ATR.
//                 Eliminates VWAP bounce entries against prevailing trend.
//                 NQH26 Jan 21-23 cluster (-$2,015) would have been fully avoided.
//
//    [v12.23] Live-readiness pass per backtest findings:
//             (a) M5 removed from signal combination and priority selector.
//                 NQZ5 backtest: 18-20 trades at 10-12% WR, primary drag.
//                 TrapState phases 0-2 retained for M4 scoring (S11/trapConf).
//             (b) M1 enabled by default (IN_M1_ENABLE default 0→1).
//                 Backtest: 68-190 trades, 25-30% WR, $5.5K net on NQZ5/NQH26.
//             (c) Daily loss cap default $1,000→$800.
//             (d) Daily profit cap default $1,000→$0 (disabled).
//             (e) News filter default ON→OFF.
//             (f) SESSION_START input (slot 25, default 100=01:00 ET).
//                 Replaces hard BeforeRthOpen(935) entry gate.
//                 Set to 0 to revert to RTH-only (09:35+).
//                 VWAP/stats tracking still anchored to RTH_OPEN=935.
//
//    [v12.19] Default daily caps: $1000 loss / $1000 profit (iof_unified defaults).
//             ATR: clarified volume-bar semantics; floor when SG_ATR<=0 (warmup/edge).
//
//    [v12.18] Single-lot only: MaximumPositionAllowed=1, TOTQTY input clamped 1..1
//             (legacy chart files cannot scale above 1 contract).
//
//    [v12.17] CSV exit reasons: daily profit flatten vs daily loss flatten
//             are logged as DAILY_PROFIT vs DAILY_LOSS (was both PNL_LIMIT).
//             Defaults: Max Position Size 1 contract (input default), 3000 vol bars docs.
//
//    [v12.15] Two audit fixes:
//             (a) Removed premature ConsecLoss=0 reset inside the T1-hit
//                 partial-exit block. T1 hit closes only the partial leg;
//                 the runner is still open and can stop out for a net
//                 loss. Resetting the counter here could silently eat
//                 actual losses from losing runners. The canonical
//                 positive-PnL reset at the trade-close block already
//                 handles this correctly on full exits.
//             (b) T1 partial-exit quantity now computed from actual
//                 position size (absPos) not configured max (TOTQTY).
//                 Risk-multiplier commonly clamps entry baseQty below
//                 TOTQTY, so TOTQTY*C_T1_RATIO could produce an
//                 exit-qty exceeding what's open. Prior code clamped
//                 to absPos-1 after the fact so no crash occurred, but
//                 the basis was wrong. Also skip partial-exit entirely
//                 when absPos<2 (runner exits via attached T1 or trail).
//
//  PRESERVED FROM v11:
//    - Signal-only gate fix (explicit onRealtimeBar detection)
//    - SETUP/ENTRY/REJECT CSV writes
//    - GATE diagnostic
//    - LRU 32768 slots, 10 MB warmup
//    - ConsecLoss reset on win, CB single-increment
//    - DailyProfit input
//
//  REQUIREMENTS: **3000 (3k)** volume bars (iof_unified::kTargetVolumeBars), NQ or MNQ, Bid/Ask on, RTH.
//  Backtest (typical): load ~6 months of RTH on 3k volume bars; Daily Loss $ + Daily Profit $ inputs
//  at 1000/1000 for a $1k envelope; Trade >> Trade Simulation + study **Enable Auto Trading = 1**.
//
//  Archived rule-stack study: IOFv02/legacy/NQ_RTH_OrderFlow_Strategy_V1.cpp (reference only).
//  One auto-trader per symbol/account.
//
//  Headers iof_unified/* are pasted below so Sierra **remote** build (single .cpp upload)
//  succeeds; edit the standalone copies under IOFv02/iof_unified/ then mirror changes here.
// ============================================================================

// ----- Inlined from iof_unified/iof_math.h (keep in sync with IOFv02/iof_unified/iof_math.h) -----
namespace iof_unified {
inline float FAbs(float x) { return x < 0.f ? -x : x; }
inline float FMax(float a, float b) { return a > b ? a : b; }
inline float FMin(float a, float b) { return a < b ? a : b; }
}

// ----- Inlined from iof_unified/iof_defaults.h -----
// [v12.24] Synced with canonical header values. Previously the inline copy
//          had drifted: kDefaultDailyLossUsd was 800 here vs 1000 in the
//          header (the git-log-documented intent was 1000 since commit
//          09e31b2). Keeping the inline copy and header identical to avoid
//          surprising runtime defaults when SYNC_FROM_IOFv02.ps1 is skipped.
namespace iof_unified {
constexpr int kTargetVolumeBars = 3000;
constexpr float kDefaultDailyLossUsd = 1000.f;
constexpr float kDefaultDailyProfitUsd = 0.f;   // disabled — no profit cap
constexpr int kDefaultRthOpenHhmm = 935;
constexpr int kDefaultFlattenHhmm = 1555;
}

// ----- Inlined from iof_unified/iof_session_rth.h -----
namespace iof_session {
inline int BarHhmmFromSecondsSinceMidnight(int secondsSinceMidnight)
{
    constexpr int kSecPerHour = 3600;
    return (secondsSinceMidnight / kSecPerHour) * 100
         + ((secondsSinceMidnight % kSecPerHour) / 60);
}
inline bool BeforeRthOpen(int barHhmm, int rthOpenHhmm) { return barHhmm < rthOpenHhmm; }
inline bool AtOrAfterRthOpen(int barHhmm, int rthOpenHhmm) { return barHhmm >= rthOpenHhmm; }
inline bool AtOrAfterFlatten(int barHhmm, int flattenHhmmInclusive) { return barHhmm >= flattenHhmmInclusive; }
inline bool InRthEntryWindow(int barHhmm, int rthOpenHhmm, int flattenHhmmInclusive)
{
    return barHhmm >= rthOpenHhmm && barHhmm < flattenHhmmInclusive;
}
inline bool InRthThroughFlattenInclusive(int barHhmm, int rthOpenHhmm, int flattenHhmmInclusive)
{
    return barHhmm >= rthOpenHhmm && barHhmm <= flattenHhmmInclusive;
}
}

// ----- Inlined from iof_unified/iof_structured_log.h -----
namespace iof_unified {
inline void LogLine(SCStudyInterfaceRef& sc, const char* category, const SCString& body, int isError = 0)
{
    SCString m;
    m.Format("[IOF:%s] %s", category, body.GetChars());
    sc.AddMessageToLog(m, isError);
}
inline void LogSession(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "SESSION", body, isError); }
inline void LogRisk(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "RISK", body, isError); }
inline void LogEntry(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "ENTRY", body, isError); }
inline void LogExit(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "EXIT", body, isError); }
inline void LogOrder(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "ORDER", body, isError); }
inline void LogDaily(SCStudyInterfaceRef& sc, const SCString& body, int isError = 0) { LogLine(sc, "DAILY", body, isError); }
inline void LogState(SCStudyInterfaceRef& sc, const SCString& body, int isError = 1) { LogLine(sc, "STATE", body, isError); }
}


using iof_unified::FAbs;
using iof_unified::FMax;
using iof_unified::FMin;

// ============================================================================
//  HELPERS
// ============================================================================
static inline float TCeil (float p, float t) { return (t>0.f)? ceilf(p/t)*t : p; }
static inline float TFloor(float p, float t) { return (t>0.f)? floorf(p/t)*t : p; }
static inline int   IClamp(int v, int lo, int hi) { return v<lo?lo:(v>hi?hi:v); }
static inline float FClamp(float v, float lo, float hi) { return v<lo?lo:(v>hi?hi:v); }

// [v12.12] Rescaled quality for reversal modes. Prior formula (score*100/15)
//          made scores of 5-6 translate to 33-40, which never cleared the 50
//          floor. Mean "good" score in practice is ~5-6, so 8/15 was effectively
//          unreachable. M4 (sweep+reclaim), M5 (trap), and M7 (auction reversal)
//          are structural reversal setups — when they arm, the mode-specific
//          preconditions already establish conviction. Use /10 divisor so
//          score 5 -> 50, score 6 -> 60. M8 unchanged (edge*10 already works).
//          M1/M2/M3 keep stricter /15 scaling since they fire more often.
static int V18A_QualityScore100(int selMode, int finalScore, int edgeScore, bool fadeActive)
{
    if(selMode == 7 && fadeActive)
        return IClamp(edgeScore * 10, 0, 100);
    // [v12.12] Reversal modes (M4=sweep, M5=trap, M6=balance, M7=auction)
    if(selMode >= 3 && selMode <= 6)
        return IClamp(finalScore * 10, 0, 100);
    // Default: stricter scale for M1/M2/M3 (VWAP/MR/consolidation)
    return IClamp((finalScore * 100) / 15, 0, 100);
}

// ============================================================================
//  HARDCODED CONSTANTS
// ============================================================================
static const int   C_ATR_PER        = 14;
static const float C_STOP_ATR       = 1.2f;
static const float C_T1_ATR         = 1.25f;
static const float C_T2_ATR         = 3.0f;
static const float C_BE_ATR         = 0.30f;
static const float C_TRAIL_ATR      = 1.50f;
static const int   C_TRAIL_DLY      = 5;
static const float C_T1_RATIO       = 0.50f;
static const int   C_MIN_SCORE_M1   = 4;
static const int   C_MIN_SCORE_ALL  = 3;

// [v12.3] cooldowns halved (was 10/15/15)
static const int   C_COOLDOWN_AFTER_TRADE = 5;
static const int   C_COOLDOWN_AFTER_LOSS  = 10;
static const int   C_POST_STOP_COOLDOWN   = 10;

static const int   C_DELTA_LB       = 15;
static const int   C_OPEN_COOL      = 10;
static const int   C_VWAP_MATURE    = 40;
static const int   C_STRUCT_LB      = 25;
static const float C_STRUCT_MAX     = 1.5f;
static const int   C_MAX_LOSSES     = 2;
static const int   C_VWAP_SLP_LB    = 20;
static const int   C_CONSOL_LB      = 25;
static const float C_CONSOL_ATR     = 1.5f;
static const int   C_M5_MIN_SC      = 3;    // min score to arm M5 (= C_MIN_SCORE_ALL)
static const int   C_M5_COOLDOWN    = 15;   // bars between M5 arms [v12.14]
static const int   C_M5_PHASE3_MAX  = 20;   // max bars in phase 3 before reset [v12.20]
static const int   C_SWEEP_LB       = 15;
static const int   C_SPIKE_COOL     = 20;
static const float C_SPIKE_ATR_M    = 3.0f;
static const int   C_DELTA_MATURE   = 25;
static const float C_VCOOL_THRESH   = 7.0f;
static const int   C_VCOOL_PAUSE    = 40;
static const int   C_VCOOL_REENTRY  = 3;
static const float C_DAILY_PROF     = 1000.0f;
static const float C_COMMISSION_RT  = 5.00f;
static const float C_SLIPPAGE_BASE  = 0.25f;

static const float C_STOP_FLOOR_PTS = 20.0f;
static const float C_STOP_CEIL_PTS  = 40.0f;
static const float C_T1_FLOOR_PTS   = 25.0f;
static const float C_T1_CEIL_PTS    = 50.0f;
static const float C_T2_FLOOR_PTS   = 75.0f;
static const float C_T2_CEIL_PTS    = 125.0f;

// [v12.1] RM floor lowered from 0.80. Post-first-loss deadlock killer.
static const float C_RM_FLOOR       = 0.60f;

// [v12.2]  Quality floor lowered from 65. Required score 10/15 -> 8/15.
// [v12.26] Floor 50->40: 2026-05-18 NQM6 session had M2 peak Q=46, M4 peak Q=40 — all near-misses.
//          Test live for 1-2 sessions, then verify on a second contract before keeping.
// [v12.30] REVERTED to 50. Cross-contract Python backtest (2026-05-20) on NQZ25
//          and NQM5 showed QF=40 is contract-dependent edge-fitting, not a robust
//          edge: NQZ25 -$2,955 vs NQM5 +$8,145 over ~6 months each. Same
//          disagreement shape as the 738d5d5 revert. Per the cross-contract A/B
//          rule, QF=40 cannot ship. M4 alone looks fine at floor 40 on both
//          contracts — a future mode-specific floor (M4@40, others @50) is a
//          plausible v12.31 candidate but needs a 3rd contract first.
static const int V18A_QUALITY_FLOOR = 50;
// [v12.38] Mode-specific floor for M8 (Fade Engine), raised 50->60. The M8
//          quality scale is edgeScore*10 (V18A_QualityScore100), so floor 60
//          sheds the edge<=5 band — the type-2 range/imbalance fades that lose.
//          Evidence: 2026-06-26 live NQU26 lost -$1,150, all from two Q=50
//          (edge-5) M8 type-2 shorts run over by an uptrend; M4 won the same
//          day. 3-contract Python A/B (M8 fade engine ported into backtest.py)
//          confirmed Q<=50 type-2 fades have negative edge on NQZ25/NQM5/NQH6;
//          floor 60 keeps only the edge>=6 (type-4 absorption) fades, which are
//          ~neutral. Surgical: leaves all other modes at 50. The Python A/B
//          MAGNITUDE is inflated by a harness Imb-extreme artifact, but the
//          sign (Q<=50 type-2 loses) is corroborated live, which is what this
//          floor gates on.
static const int V18A_QUALITY_FLOOR_M8 = 60;

// ============================================================================
//  [VARIANT FB500] FIXED $ BRACKET — the only behavioural delta vs v12.38
// ============================================================================
//  Dollars PER CONTRACT, converted to points at runtime with PtVal
//  (= CurrencyValuePerTick / TickSize), so the same numbers are correct on NQ
//  and MNQ. Set either to 0, or C_VAR_FIXBRKT_ON to false, and the build
//  reverts byte-for-byte to the production ATR bracket.
static const float C_VAR_FIXBRKT_STOP_USD = 500.0f;
static const float C_VAR_FIXBRKT_TGT_USD  = 500.0f;
static const bool  C_VAR_FIXBRKT_ON       = true;

static const int LOG_CRIT = 0;
static const int LOG_SIG  = 1;
static const int LOG_DBG  = 2;

static const int FR_NONE    = 0;
static const int FR_FLATTEN = 1;
static const int FR_CB      = 2;
static const int FR_SPIKE   = 3;
static const int FR_STOP    = 5;
static const int FR_TRAIL   = 6;
static const int FR_T1      = 7;
static const int FR_T2      = 8;
static const int FR_DAILY_PROFIT = 9;
static const int FR_DAILY_LOSS   = 10;
static const int FR_SCRATCH      = 11;  // [v12.37] early-scratch exit

// [v12.37] Early-scratch threshold: abandon a pre-T1 trade that never reached
// this fraction of stop distance in MFE by the time the stop becomes eligible.
static const float C_ES_MFE_FRAC = 0.25f;

// ============================================================================
//  DELTA RING BUFFER
// ============================================================================
static const int DRING_SZ = 32;
struct DeltaRing {
    float delta[DRING_SZ];
    int   lastIdx;
    bool  primed;
    void reset(){ for(int i=0;i<DRING_SZ;i++) delta[i]=0.f; lastIdx=-1; primed=false; }
    void pushBar(SCStudyInterfaceRef& sc, int Idx){
        if(Idx==lastIdx) return;
        if(!primed){
            for(int k=0;k<DRING_SZ;k++){
                int s = Idx-k;
                delta[k] = (s>=0) ? (sc.AskVolume[s]-sc.BidVolume[s]) : 0.f;
            }
            primed=true; lastIdx=Idx; return;
        }
        if(Idx == lastIdx+1){
            for(int k=DRING_SZ-1;k>0;k--) delta[k]=delta[k-1];
            delta[0] = sc.AskVolume[Idx]-sc.BidVolume[Idx];
            lastIdx=Idx; return;
        }
        for(int k=0;k<DRING_SZ;k++){
            int s = Idx-k;
            delta[k] = (s>=0) ? (sc.AskVolume[s]-sc.BidVolume[s]) : 0.f;
        }
        lastIdx=Idx;
    }
    float sumRange(int from, int to) const {
        if(from<0) from=0; if(to>=DRING_SZ) to=DRING_SZ-1;
        float s=0.f; for(int k=from;k<=to;k++) s+=delta[k]; return s;
    }
};

// ============================================================================
//  CSV WRITER (V18A-compatible, with dedup)
// ============================================================================
static bool EnsureDir(const char* P) {
    if (!P || !P[0]) return false;
    DWORD A = GetFileAttributesA(P);
    if (A != INVALID_FILE_ATTRIBUTES && (A & FILE_ATTRIBUTE_DIRECTORY)) return true;
    if (CreateDirectoryA(P, NULL)) return true;
    return (GetLastError() == ERROR_ALREADY_EXISTS);
}

static unsigned long long g_runID = 0;
static void InitRunID() {
    if(g_runID != 0) return;
    unsigned long long t = (unsigned long long)time(NULL);
    g_runID = t * 2654435761ULL;
    if(g_runID == 0) g_runID = 1;
}

static const int CSV_LRU_SZ = 32768;
struct CSVDedupLRU {
    char keys[CSV_LRU_SZ][96];
    int  head;
    int  filled;
    bool warmed;
    void reset() {
        head = 0; filled = 0; warmed = false;
        for(int i=0; i<CSV_LRU_SZ; i++) keys[i][0] = '\0';
    }
    bool contains(const char* k) const {
        int n = (filled < CSV_LRU_SZ) ? filled : CSV_LRU_SZ;
        for(int i=0; i<n; i++) if(keys[i][0] && strcmp(keys[i],k)==0) return true;
        return false;
    }
    void add(const char* k) {
        int n = 0;
        while(n < 95 && k[n]) { keys[head][n] = k[n]; n++; }
        keys[head][n] = '\0';
        head = (head + 1) % CSV_LRU_SZ;
        if(filled < CSV_LRU_SZ) filled++;
    }
};
static CSVDedupLRU g_csvLRU;

static void WriteCSV(SCStudyInterfaceRef& sc, const char* Evt, const char* Side, const char* Mode,
    float Entry, float SL, float TP1, float TP2, int Qty, int Score,
    int CtrlSc, int DivStr, float Delta, float BarSpd,
    float ExitPx, const char* ExitR, int Hold, float MAE, float MFE,
    float DayPnL, float TotPnL, int FadeEdge, int FadeType,
    float RiskMult, int TrendReg, int VolReg, int ChopReg)
{
    InitRunID();
    int Y,Mo,D,Hr,Mi,Se;
    sc.BaseDateTimeIn[sc.Index].GetDateTimeYMDHMS(Y,Mo,D,Hr,Mi,Se);
    EnsureDir(sc.DataFilesFolder().GetChars());
    SCString Path; Path.Format("%s\\IOF_NQ_%s.csv", sc.DataFilesFolder().GetChars(), sc.Symbol.GetChars());

    if (strcmp(Evt,"SETUP")==0 || strcmp(Evt,"EXEC")==0) {
        char Key[96]; snprintf(Key,96,"%04d-%02d-%02d,%02d:%02d:%02d,%s,%s",Y,Mo,D,Hr,Mi,Se,Evt,Side);

        if(g_csvLRU.contains(Key)) return;

        if(!g_csvLRU.warmed) {
            g_csvLRU.warmed = true;
            FILE* RF = fopen(Path.GetChars(), "rb");
            if (RF) {
                const long WARMUP_BYTES = 10 * 1024 * 1024;
                fseek(RF, 0, SEEK_END);
                long FS = ftell(RF);
                long Pos = (FS > WARMUP_BYTES) ? (FS - WARMUP_BYTES) : 0;
                fseek(RF, Pos, SEEK_SET);
                int  Sz = (int)(FS - Pos);
                char* B = (char*)malloc(Sz + 1);
                if (B) {
                    int N = (int)fread(B, 1, Sz, RF);
                    B[N] = '\0';
                    char* line = B;
                    if(Pos > 0) {
                        char* nl = strchr(line, '\n');
                        if(nl) line = nl + 1;
                    }
                    while(line && *line) {
                        char* nl = strchr(line, '\n');
                        int   len = nl ? (int)(nl - line) : (int)strlen(line);
                        if(len > 25 && (
                              (line[20]=='S' && line[21]=='E' && line[22]=='T' && line[23]=='U' && line[24]=='P')
                           || (line[20]=='E' && line[21]=='X' && line[22]=='E' && line[23]=='C')
                        )) {
                            int commas = 0;
                            int keyEnd = -1;
                            for(int i=0; i<len && i<95; i++) {
                                if(line[i] == ',') {
                                    commas++;
                                    if(commas == 4) { keyEnd = i; break; }
                                }
                            }
                            if(keyEnd > 0 && keyEnd < 95) {
                                char k[96];
                                memcpy(k, line, keyEnd);
                                k[keyEnd] = '\0';
                                if(!g_csvLRU.contains(k)) g_csvLRU.add(k);
                            }
                        }
                        if(!nl) break;
                        line = nl + 1;
                    }
                    free(B);
                }
                fclose(RF);
            }
            if(g_csvLRU.contains(Key)) return;
        }
        g_csvLRU.add(Key);
    }

    FILE* TF=fopen(Path.GetChars(),"r"); bool Hdr=(TF==NULL); if(TF) fclose(TF);
    FILE* F=fopen(Path.GetChars(),"a"); if(!F) return;
    if (Hdr) fprintf(F,"Date,Time,Event,Side,Mode,Entry,SL,TP1,TP2,Qty,Score,"
                       "CtrlScore,DivStr,Delta,BarSpeed,"
                       "ExitPx,ExitReason,HoldBars,MAE,MFE,"
                       "DayPnL,TotalPnL,FadeEdge,FadeType,"
                       "RiskMult,TrendReg,VolReg,ChopReg,Version,RunID\n");
    fprintf(F,"%04d-%02d-%02d,%02d:%02d:%02d,%s,%s,%s,%.2f,%.2f,%.2f,%.2f,%d,%d,"
              "%d,%d,%.0f,%.2f,"
              "%.2f,%s,%d,%.2f,%.2f,"
              "%.2f,%.2f,%d,%d,"
              "%.2f,%d,%d,%d,v12.38,%llu\n",
        Y,Mo,D,Hr,Mi,Se,Evt,Side,Mode,Entry,SL,TP1,TP2,Qty,Score,
        CtrlSc,DivStr,Delta,BarSpd,
        ExitPx,ExitR,Hold,MAE,MFE,
        DayPnL,TotPnL,FadeEdge,FadeType,
        RiskMult,TrendReg,VolReg,ChopReg,g_runID);
    fclose(F);
}

// [live-fire audit] Append ONLY genuine real-time fires to a dedicated file.
// Called AFTER the signalOnly guard in the SETUP block, so recalc / replay /
// historical-download rows can never reach it (unlike IOF_NQ_<sym>.csv, which
// logs backfill too and is therefore useless for confirming live fires). Every
// row here is a setup for which a live order was submitted this session —
// unambiguous, append-only, no recalc pollution.
static void WriteLiveFire(SCStudyInterfaceRef& sc, const char* mode, const char* side,
                          int q100, int score, float ent, float sl, float t1, float t2,
                          int qty, float riskMult)
{
    InitRunID();
    int Y,Mo,D,Hr,Mi,Se;
    sc.BaseDateTimeIn[sc.Index].GetDateTimeYMDHMS(Y,Mo,D,Hr,Mi,Se);
    EnsureDir(sc.DataFilesFolder().GetChars());
    SCString Path; Path.Format("%s\\IOF_NQ_LiveFires_%s.csv",
                               sc.DataFilesFolder().GetChars(), sc.Symbol.GetChars());
    FILE* TF=fopen(Path.GetChars(),"r"); bool Hdr=(TF==NULL); if(TF) fclose(TF);
    FILE* F=fopen(Path.GetChars(),"a"); if(!F) return;
    if(Hdr) fprintf(F,"Date,Time,Mode,Side,Q,Score,Entry,SL,TP1,TP2,Qty,RiskMult,Version,RunID\n");
    fprintf(F,"%04d-%02d-%02d,%02d:%02d:%02d,%s,%s,%d,%d,%.2f,%.2f,%.2f,%.2f,%d,%.2f,v12.38,%llu\n",
        Y,Mo,D,Hr,Mi,Se, mode, side, q100, score, ent, sl, t1, t2, qty, riskMult, g_runID);
    fclose(F);
}

static void LogError(SCStudyInterfaceRef& sc, const char* msg) {
    SCString m; m.Format("[V18A ERROR] %s", msg);
    sc.AddMessageToLog(m, 1);
    WriteCSV(sc, "ERROR", "", "", 0,0,0,0, 0,0, 0,0,0,0,
             0, msg, 0, 0,0, 0,0, 0,0, 0, 0,0,0);
}

// ============================================================================
//  VOLUME PROFILE
// ============================================================================
static const int VP_MAX_BINS   = 800;
static const int VP_5_DAYS     = 5;
static const int VP_50_DAYS    = 50;
static const float VP_BIN_SZ   = 1.0f;
static const float VP_VA_PCT   = 0.70f;
static const int SP_MAX_ZONES  = 32;
static const float SP_VOL_THRESH = 0.15f;

struct VPDay {
    float basePx;
    float bins[VP_MAX_BINS];
    int   dateTag;
};

struct VPState {
    VPDay days[VP_50_DAYS];
    float poc, vah, val;
    float poc50, vah50, val50;
    int   activeDayIdx;
    bool  valid, valid50;

    struct SPZone { float hi, lo; bool filled; };
    SPZone spZones[SP_MAX_ZONES];
    int    spCount;

    float comp[VP_MAX_BINS];

    void reset() {
        for(int d=0; d<VP_50_DAYS; d++) {
            days[d].basePx  = 0.f;
            days[d].dateTag = 0;
            for(int b=0; b<VP_MAX_BINS; b++) days[d].bins[b] = 0.f;
        }
        poc = vah = val = 0.f;
        poc50 = vah50 = val50 = 0.f;
        activeDayIdx = 0;
        valid = valid50 = false;
        spCount = 0;
        for(int i=0; i<SP_MAX_ZONES; i++) {
            spZones[i].hi = spZones[i].lo = 0.f;
            spZones[i].filled = true;
        }
    }

    int pxToBin(float px, float base) const {
        int b = (int)((px-base)/VP_BIN_SZ) + VP_MAX_BINS/2;
        if(b < 0) b = 0;
        if(b >= VP_MAX_BINS) b = VP_MAX_BINS-1;
        return b;
    }

    float binToPx(int b, float base) const {
        return base + ((float)(b - VP_MAX_BINS/2)) * VP_BIN_SZ;
    }

    void addBar(float hi, float lo, float vol, float base) {
        if(vol <= 0.f || base <= 0.f) return;
        VPDay& d = days[activeDayIdx];
        d.basePx = base;
        int bL = pxToBin(lo, base);
        int bH = pxToBin(hi, base);
        float per = vol / (float)((bH - bL) + 1);
        for(int b=bL; b<=bH; b++) d.bins[b] += per;
    }

    void computeComposite(int numDays, float& outPOC, float& outVAH, float& outVAL, bool& outValid) {
        float sB = 0.f;
        int   nD = 0;
        for(int d=0; d<VP_50_DAYS; d++) {
            if(days[d].dateTag > 0 && days[d].basePx > 0.f) {
                sB += days[d].basePx;
                nD++;
                if(nD >= numDays) break;
            }
        }
        if(nD == 0) { outValid = false; return; }

        float gB = sB / (float)nD;
        for(int b=0; b<VP_MAX_BINS; b++) comp[b] = 0.f;

        int counted = 0;
        for(int d=0; d<VP_50_DAYS && counted<numDays; d++) {
            if(days[d].dateTag <= 0) continue;
            float dB = days[d].basePx;
            for(int b=0; b<VP_MAX_BINS; b++) {
                if(days[d].bins[b] <= 0.f) continue;
                int cb = pxToBin(binToPx(b, dB), gB);
                comp[cb] += days[d].bins[b];
            }
            counted++;
        }

        int   pB = 0;
        float mV = 0.f, tV = 0.f;
        for(int b=0; b<VP_MAX_BINS; b++) {
            tV += comp[b];
            if(comp[b] > mV) { mV = comp[b]; pB = b; }
        }
        outPOC = binToPx(pB, gB);

        if(tV <= 0.f) { outVAH = outPOC; outVAL = outPOC; outValid = true; return; }

        float vaT = tV * VP_VA_PCT;
        float vaV = comp[pB];
        int   lo  = pB, hi = pB;
        while(vaV < vaT && (lo > 0 || hi < VP_MAX_BINS-1)) {
            float vB = (lo > 0) ? comp[lo-1] : 0.f;
            float vA = (hi < VP_MAX_BINS-1) ? comp[hi+1] : 0.f;
            if(vB >= vA && lo > 0)            { lo--; vaV += comp[lo]; }
            else if(hi < VP_MAX_BINS-1)       { hi++; vaV += comp[hi]; }
            else if(lo > 0)                    { lo--; vaV += comp[lo]; }
            else break;
        }
        outVAH = binToPx(hi, gB);
        outVAL = binToPx(lo, gB);
        outValid = true;

        if(numDays <= VP_5_DAYS && mV > 0.f) {
            float spThresh = mV * SP_VOL_THRESH;
            spCount = 0;
            bool  inSP = false;
            float spLo = 0.f;
            for(int b=lo; b<=hi && spCount<SP_MAX_ZONES; b++) {
                if(comp[b] < spThresh && comp[b] > 0.f) {
                    if(!inSP) { inSP = true; spLo = binToPx(b, gB); }
                } else {
                    if(inSP) {
                        float spHi2 = binToPx(b-1, gB);
                        if(spHi2 - spLo >= 1.0f) {
                            spZones[spCount].lo = spLo;
                            spZones[spCount].hi = spHi2;
                            spZones[spCount].filled = false;
                            spCount++;
                        }
                        inSP = false;
                    }
                }
            }
            if(inSP && spCount<SP_MAX_ZONES) {
                float spHi2 = binToPx(hi, gB);
                if(spHi2 - spLo >= 1.0f) {
                    spZones[spCount].lo = spLo;
                    spZones[spCount].hi = spHi2;
                    spZones[spCount].filled = false;
                    spCount++;
                }
            }
        }
    }

    void computeLevels5()  { computeComposite(VP_5_DAYS,  poc,  vah,  val,  valid);   }
    void computeLevels50() { computeComposite(VP_50_DAYS, poc50,vah50,val50,valid50); }
    void computeLevels()   { computeLevels5(); computeLevels50(); }

    void updateSinglePrints(float hi, float lo, float vol) {
        for(int i=0; i<spCount; i++) {
            if(spZones[i].filled) continue;
            if(lo <= spZones[i].hi && hi >= spZones[i].lo && vol > 0.f)
                spZones[i].filled = true;
        }
    }

    bool nearSinglePrint(float px, float tol) const {
        for(int i=0; i<spCount; i++) {
            if(spZones[i].filled) continue;
            if(px >= spZones[i].lo - tol && px <= spZones[i].hi + tol) return true;
        }
        return false;
    }
};

// ============================================================================
//  ORDERFLOW STATE STRUCTS
// ============================================================================
struct IcebergState{bool bidIceberg,askIceberg;float bidIcebergPx,askIcebergPx,bidIcebergVol,askIcebergVol;int bidIcebergBars,askIcebergBars;
    void reset(){bidIceberg=askIceberg=false;bidIcebergPx=askIcebergPx=0.f;bidIcebergVol=askIcebergVol=0.f;bidIcebergBars=askIcebergBars=0;}};

struct TrapState{int phase,direction,commitStartBar,commitBars;float commitDelta,commitHigh,commitLow,absorbPx;int absorbBar,phase3Bar;float stopTarget,entryPx;bool valid;
    void reset(){phase=0;direction=0;commitStartBar=-1;commitBars=0;commitDelta=0.f;commitHigh=0.f;commitLow=0.f;absorbPx=0.f;absorbBar=-1;phase3Bar=-1;stopTarget=0.f;entryPx=0.f;valid=false;}};

struct BalanceState{bool active,mature;float rangeHigh,rangeLow,rangePOC,volumeTotal,vpConcentration,cumDelta;int barCount,deltaFlips;
    void reset(){active=false;mature=false;rangeHigh=rangeLow=rangePOC=0.f;volumeTotal=0.f;barCount=0;deltaFlips=0;cumDelta=0.f;vpConcentration=0.f;}};

struct ImbalanceState{bool active;int direction,startBar,barCount,strength,consecutiveAccept;float aggressionRatio;
    void reset(){active=false;direction=0;startBar=-1;barCount=0;strength=0;aggressionRatio=0.5f;consecutiveAccept=0;}};

struct CumDeltaDivState{bool trendDivBull,trendDivBear,swingDivBull,swingDivBear,persistAbsBuy,persistAbsSell;float trendDivDepth;int persistUpPxDnDelta,persistDnPxUpDelta,strength;
    void reset(){trendDivBull=trendDivBear=false;trendDivDepth=0.f;swingDivBull=swingDivBear=false;persistUpPxDnDelta=persistDnPxUpDelta=0;persistAbsBuy=persistAbsSell=false;strength=0;}};

struct FadeSetup{int type,direction,edgeScore,triggerBar;float entryPx,stopPx,t1Px,t2Px;bool active;
    void reset(){type=0;direction=0;entryPx=stopPx=t1Px=t2Px=0.f;edgeScore=0;active=false;triggerBar=-1;}};

struct InterdayLevels{float prevOpen,prevHigh,prevLow,prevClose,currOpen;int prevDate;bool valid;
    void reset(){prevOpen=prevHigh=prevLow=prevClose=currOpen=0.f;prevDate=0;valid=false;}};

struct NewsBlackout{int spikeBar;bool spikeActive;void reset(){spikeBar=-1;spikeActive=false;}
    // [v12.8] narrowed news windows from ~45 min to ~10 min around releases
    bool isNewsWindow(int hhmm) const {
        if(hhmm >=  825 && hhmm <=  835) return true;   // 08:30 econ data
        if(hhmm >=  955 && hhmm <= 1005) return true;   // 10:00 secondary releases
        if(hhmm >= 1355 && hhmm <= 1405) return true;   // 14:00 FOMC/speakers
        return false;
    }};

struct VolCooldownState{int barsRemaining,spikeBar,lastLogBar;float spikeMultiple;bool firstReentryPending;
    void reset(){barsRemaining=0;spikeBar=-1;spikeMultiple=0.f;firstReentryPending=false;lastLogBar=-1;}};

// ============================================================================
//  ADAPTIVE THRESHOLDS
// ============================================================================
struct AdaptiveThresholds {
    float emaAskBidRatio, emaAggrPct, emaBodyRatio; int samples; bool calibrated;
    static const int MIN_SAMPLES = 50; static constexpr float ALPHA = 0.02f;
    void reset() { emaAskBidRatio=1.3f; emaAggrPct=0.50f; emaBodyRatio=0.50f; samples=0; calibrated=false; }
    void update(float askV, float bidV, float body, float range, float vol) {
        if(vol<=0.f||range<=0.f) return;
        float askBid=(bidV>0.f)?(askV/bidV):1.f; float bodyR=body/range;
        float tv=askV+bidV; float aggrP=(tv>0.f)?(askV/tv):0.5f;
        emaAskBidRatio+=ALPHA*(askBid-emaAskBidRatio);
        emaAggrPct+=ALPHA*(aggrP-emaAggrPct);
        emaBodyRatio+=ALPHA*(bodyR-emaBodyRatio);
        samples++; if(samples>=MIN_SAMPLES) calibrated=true;
    }
    float icebergRatio()  const { return calibrated ? emaAskBidRatio*1.15f : 1.3f; }
    float aggrBullish()   const { return calibrated ? FMin(0.65f,emaAggrPct+0.08f) : 0.62f; }
    float aggrBearish()   const { return calibrated ? FMax(0.35f,emaAggrPct-0.08f) : 0.45f; }
    float trapBodyMax()   const { return calibrated ? FMin(0.50f,emaBodyRatio*0.70f) : 0.35f; }
};

// ============================================================================
//  VOL SCALER
// ============================================================================
struct VolScaler{float sessionAvgRange;int sessionBars;
    void reset(){sessionAvgRange=0.f;sessionBars=0;}
    void update(float range){sessionBars++;sessionAvgRange+=(range-sessionAvgRange)/(float)sessionBars;}
    float sizeMultiplier(float recentRange)const{if(sessionAvgRange<=0.f||sessionBars<20)return 1.0f;
        float ratio=recentRange/sessionAvgRange;if(ratio>2.0f)return 0.5f;if(ratio>1.5f)return 0.75f;return 1.0f;}};

// ============================================================================
//  MODE STATS
// ============================================================================
struct ModeStats {
    int wins[8], losses[8], trades[8]; float cumPnL[8];
    float sumPnL[8], sumPnLSq[8], sumNegPnLSq[8];
    float grossWins[8], grossLosses[8];
    float maxWin[8], maxLoss[8], mae[8], mfe[8];
    int consecWins[8], consecLosses[8], maxConsecWins[8], maxConsecLosses[8];
    float avgBarsInTrade[8]; int totalBars[8];

    void reset() { for(int i=0;i<8;i++){
        wins[i]=losses[i]=trades[i]=0; cumPnL[i]=0.f;
        sumPnL[i]=sumPnLSq[i]=sumNegPnLSq[i]=0.f;
        grossWins[i]=grossLosses[i]=0.f;
        maxWin[i]=0.f; maxLoss[i]=0.f; mae[i]=mfe[i]=0.f;
        consecWins[i]=consecLosses[i]=maxConsecWins[i]=maxConsecLosses[i]=0;
        avgBarsInTrade[i]=0.f; totalBars[i]=0;
    } }

    void record(int m, float pnl, float tradeMAE=0.f, float tradeMFE=0.f, int barsHeld=0) {
        if(m<0||m>=8)return;
        trades[m]++; cumPnL[m]+=pnl;
        sumPnL[m]+=pnl; sumPnLSq[m]+=pnl*pnl;
        if(pnl<0.f) sumNegPnLSq[m]+=pnl*pnl;
        totalBars[m]+=barsHeld;
        if(barsHeld>0) avgBarsInTrade[m]=(float)totalBars[m]/(float)trades[m];
        if(tradeMAE>mae[m]) mae[m]=tradeMAE;
        if(tradeMFE>mfe[m]) mfe[m]=tradeMFE;
        if(pnl>0.f){wins[m]++; grossWins[m]+=pnl; if(pnl>maxWin[m]) maxWin[m]=pnl;
            consecWins[m]++; consecLosses[m]=0;
            if(consecWins[m]>maxConsecWins[m]) maxConsecWins[m]=consecWins[m];}
        else{losses[m]++; grossLosses[m]+=FAbs(pnl); if(pnl<maxLoss[m]) maxLoss[m]=pnl;
            consecLosses[m]++; consecWins[m]=0;
            if(consecLosses[m]>maxConsecLosses[m]) maxConsecLosses[m]=consecLosses[m];}
    }
    float winRate(int m) const { return (m>=0&&m<8&&trades[m]>0)?(float)wins[m]/(float)trades[m]:0.f; }
    float expectancy(int m) const { if(m<0||m>=8||trades[m]==0) return 0.f; return cumPnL[m]/(float)trades[m]; }
    float profitFactor(int m) const { if(m<0||m>=8||grossLosses[m]<=0.f) return (grossWins[m]>0.f)?99.9f:0.f; return grossWins[m]/grossLosses[m]; }
    float stdDev(int m) const { if(m<0||m>=8||trades[m]<2) return 0.f;
        float mean=sumPnL[m]/(float)trades[m]; float var=(sumPnLSq[m]/(float)trades[m])-(mean*mean); return var>0.f?sqrtf(var):0.f; }
    float downsideDev(int m) const { if(m<0||m>=8||trades[m]<2) return 0.f;
        float var=sumNegPnLSq[m]/(float)trades[m]; return var>0.f?sqrtf(var):0.f; }
    float sharpeRatio(int m) const { if(m<0||m>=8||trades[m]<5) return 0.f;
        float mean=expectancy(m); float sd=stdDev(m); if(sd<=0.f) return 0.f;
        return FMin((mean/sd)*38.9f, 10.f); }
    float sortinoRatio(int m) const { if(m<0||m>=8||trades[m]<5) return 0.f;
        float mean=expectancy(m); float dd=downsideDev(m); if(dd<=0.f) return (mean>0.f)?10.f:0.f;
        return FMin((mean/dd)*38.9f, 15.f); }
    float tStat(int m) const { if(m<0||m>=8||trades[m]<5) return 0.f;
        float mean=expectancy(m); float sd=stdDev(m); if(sd<=0.f) return (mean>0.f)?10.f:-10.f;
        return (mean/sd)*sqrtf((float)trades[m]); }
    bool isSignificant(int m) const { return (m>=0&&m<8&&trades[m]>=20&&tStat(m)>2.0f); }
    // v12.9: AUTO_DISABLE hardened. Prior threshold -2.0 required a mode to be
    // significantly-negative before benching — by then it had already burned
    // ~30 losing trades. New threshold -1.0 with min 20 trades benches modes
    // that are trending bad before they bleed another 10 trades. A mode only
    // needs to be not-positive to be suspicious; -2.0 was too forgiving.
    bool shouldDisable(int m) const { return (m>=0&&m<8&&trades[m]>=20&&tStat(m)<-1.0f); }
};

// ============================================================================
//  INSTITUTIONAL RISK STATE
// ============================================================================
struct InstitutionalRiskState {
    float sessionPeakEquity, sessionDrawdown, maxSessionDrawdown, sessionPnL;
    int sessionTrades, sessionConsecLosses, sessionConsecWins;
    float cumPnL; int cumTrades; float maxDrawdown, allTimeHigh;
    static const int MAX_TRACK_TRADES = 50;
    float tradeReturns[MAX_TRACK_TRADES]; int tradeReturnCount, tradeReturnIdx;
    float sumReturns, sumReturnsSq, sumNegReturnsSq;
    int winCount, lossCount; float grossWin, grossLoss;
    float baselineATR, currentATRRatio; int volRegime; float atrEMA;
    int equityCurveRegime; float recentWinRate; int recentTradesWon, recentTradesCount;
    float equityEMA10, equityEMA30;
    float cumSlippage, avgSlippage; int slippageSamples;
    float historicalVaR95, historicalCVaR95;
    float optimalKelly, practicalKelly;
    float targetVolatility, realizedVolatility, volTargetMultiplier;
    float antiMartingaleLevel; int streakDirection, streakLength;
    int barsSinceLastTrade; float timeDecayFactor;
    int lastUpdateBar; // [v12.32] per-bar gate for updateVolRegime/updateTimeDecay (AutoLoop=1 calls ~50-200x/bar)
    bool inRecoveryMode; float recoveryTarget, recoveryMultiplier;
    float dailyRiskBudget, riskBudgetUsed, riskBudgetRemaining;
    float riskMultiplier; bool profitScaleEnabled;
    float volMultiplier, kellyMultiplier, equityCurveMultiplier, antiMartingaleMultiplier;
    float drawdownMultiplier, recoveryMultiplier_, timeDecayMultiplier, budgetMultiplier;

    void reset() {
        sessionPeakEquity=sessionDrawdown=maxSessionDrawdown=sessionPnL=0.f;
        sessionTrades=sessionConsecLosses=sessionConsecWins=0;
        cumPnL=0.f; cumTrades=0; maxDrawdown=allTimeHigh=0.f;
        baselineATR=0.f; currentATRRatio=1.f; volRegime=1; atrEMA=0.f;
        equityCurveRegime=1; recentWinRate=0.5f; recentTradesWon=recentTradesCount=0;
        equityEMA10=equityEMA30=0.f;
        cumSlippage=avgSlippage=0.f; slippageSamples=0;
        riskMultiplier=1.0f; profitScaleEnabled=true;
        for(int i=0;i<MAX_TRACK_TRADES;i++) tradeReturns[i]=0.f;
        tradeReturnCount=tradeReturnIdx=0;
        sumReturns=sumReturnsSq=sumNegReturnsSq=0.f;
        winCount=lossCount=0; grossWin=grossLoss=0.f;
        historicalVaR95=historicalCVaR95=0.f;
        optimalKelly=practicalKelly=0.f;
        targetVolatility=200.f; realizedVolatility=0.f; volTargetMultiplier=1.f;
        antiMartingaleLevel=1.f; streakDirection=0; streakLength=0;
        barsSinceLastTrade=0; timeDecayFactor=1.f; lastUpdateBar=-1;
        inRecoveryMode=false; recoveryTarget=0.f; recoveryMultiplier=1.f;
        dailyRiskBudget=2000.f; riskBudgetUsed=0.f; riskBudgetRemaining=2000.f;
        volMultiplier=kellyMultiplier=equityCurveMultiplier=antiMartingaleMultiplier=1.f;
        drawdownMultiplier=recoveryMultiplier_=timeDecayMultiplier=budgetMultiplier=1.f;
    }
    void newSession() {
        sessionPeakEquity=sessionDrawdown=maxSessionDrawdown=sessionPnL=0.f;
        sessionTrades=sessionConsecLosses=sessionConsecWins=0;
        barsSinceLastTrade=0; timeDecayFactor=1.f;
        inRecoveryMode=false; recoveryTarget=0.f; recoveryMultiplier=1.f;
        riskBudgetUsed=0.f; riskBudgetRemaining=dailyRiskBudget;
    }
    void setDailyBudget(float budget) {
        if(budget>0.f){dailyRiskBudget=budget; riskBudgetRemaining=budget;}
        else{dailyRiskBudget=0.f; riskBudgetRemaining=999999.f;}
    }

    void updatePnL(float tradePnL) {
        sessionPnL+=tradePnL; sessionTrades++; cumPnL+=tradePnL; cumTrades++; barsSinceLastTrade=0;
        tradeReturns[tradeReturnIdx]=tradePnL;
        tradeReturnIdx=(tradeReturnIdx+1)%MAX_TRACK_TRADES;
        if(tradeReturnCount<MAX_TRACK_TRADES) tradeReturnCount++;
        sumReturns+=tradePnL; sumReturnsSq+=tradePnL*tradePnL;
        if(tradePnL<0.f) sumNegReturnsSq+=tradePnL*tradePnL;
        if(tradePnL>0.f){winCount++; grossWin+=tradePnL;}
        else{lossCount++; grossLoss+=FAbs(tradePnL);}
        if(sessionPnL>sessionPeakEquity) sessionPeakEquity=sessionPnL;
        sessionDrawdown=sessionPeakEquity-sessionPnL;
        if(sessionDrawdown>maxSessionDrawdown) maxSessionDrawdown=sessionDrawdown;
        if(sessionDrawdown>maxDrawdown) maxDrawdown=sessionDrawdown;
        if(cumPnL>allTimeHigh) allTimeHigh=cumPnL;

        if(tradePnL>0.f){sessionConsecWins++;sessionConsecLosses=0;
            if(streakDirection==1)streakLength++;else{streakDirection=1;streakLength=1;}}
        else{sessionConsecLosses++;sessionConsecWins=0;
            if(streakDirection==-1)streakLength++;else{streakDirection=-1;streakLength=1;}}

        if(tradePnL>0.f) recentTradesWon++;
        recentTradesCount++;
        if(recentTradesCount>10){recentTradesWon=(int)(recentTradesWon*0.9f); recentTradesCount=10;}
        recentWinRate=(recentTradesCount>0)?(float)recentTradesWon/(float)recentTradesCount:0.5f;

        float alpha10=2.f/11.f, alpha30=2.f/31.f;
        if(cumTrades<=1){equityEMA10=cumPnL; equityEMA30=cumPnL;}
        else{equityEMA10=alpha10*cumPnL+(1.f-alpha10)*equityEMA10;
             equityEMA30=alpha30*cumPnL+(1.f-alpha30)*equityEMA30;}
        if(equityEMA10>equityEMA30*1.02f) equityCurveRegime=2;
        else if(equityEMA10<equityEMA30*0.98f) equityCurveRegime=0;
        else equityCurveRegime=1;

        if(tradePnL<0.f) riskBudgetUsed+=FAbs(tradePnL);
        riskBudgetRemaining=FMax(0.f, dailyRiskBudget-riskBudgetUsed);

        if(dailyRiskBudget>0.f){
            if(!inRecoveryMode && sessionDrawdown>dailyRiskBudget*0.3f){
                inRecoveryMode=true; recoveryTarget=sessionPeakEquity*0.8f;}
            if(inRecoveryMode && sessionPnL>=recoveryTarget) inRecoveryMode=false;
        }

        computeVaRCVaR(); computeKelly(); computeVolatilityTarget();
        computeAntiMartingale(); computeRiskMultiplier();
    }

    float sharpeRatio() const {
        if(cumTrades<5) return 0.f;
        float avgRet=sumReturns/(float)cumTrades;
        float variance=(sumReturnsSq/(float)cumTrades)-(avgRet*avgRet);
        if(variance<=0.f) return 0.f; float sd=sqrtf(variance);
        return FMin((avgRet/sd)*38.9f, 10.f);
    }
    float sortinoRatio() const {
        if(cumTrades<5||lossCount<1) return 0.f;
        float avgRet=sumReturns/(float)cumTrades;
        float downsideVar=sumNegReturnsSq/(float)cumTrades;
        if(downsideVar<=0.f) return (avgRet>0.f)?10.f:0.f;
        return FMin((avgRet/sqrtf(downsideVar))*38.9f, 15.f);
    }
    float profitFactor() const { if(grossLoss<=0.f) return (grossWin>0.f)?99.9f:0.f; return grossWin/grossLoss; }
    float expectedValue() const { return cumTrades<=0 ? 0.f : sumReturns/(float)cumTrades; }
    float winRate() const { return cumTrades<=0 ? 0.f : (float)winCount/(float)cumTrades; }

    void computeVaRCVaR() {
        if(tradeReturnCount<10){historicalVaR95=historicalCVaR95=0.f; return;}
        float sorted[MAX_TRACK_TRADES]; int n=tradeReturnCount;
        for(int i=0;i<n;i++) sorted[i]=tradeReturns[i];
        for(int i=1;i<n;i++){
            float key=sorted[i]; int j=i-1;
            while(j>=0 && sorted[j]>key){sorted[j+1]=sorted[j]; j--;}
            sorted[j+1]=key;
        }
        int varIdx=(int)(n*0.05f); if(varIdx<0) varIdx=0;
        historicalVaR95=-sorted[varIdx];
        float tailSum=0.f; int tailCount=0;
        for(int i=0;i<=varIdx;i++){tailSum+=sorted[i];tailCount++;}
        historicalCVaR95=(tailCount>0)?-tailSum/(float)tailCount:historicalVaR95;
    }

    void computeKelly() {
        if(winCount<5||lossCount<2){optimalKelly=practicalKelly=0.f; return;}
        float wr=winRate(), avgWin=grossWin/(float)winCount, avgLoss=grossLoss/(float)lossCount;
        if(avgLoss<=0.f){optimalKelly=practicalKelly=0.f; return;}
        float b=avgWin/avgLoss;
        optimalKelly=(wr*b-(1.f-wr))/b;
        practicalKelly=FClamp(optimalKelly*0.5f, 0.05f, 0.25f);
        if(optimalKelly<0.f) practicalKelly=0.f;
    }

    void computeVolatilityTarget() {
        if(tradeReturnCount<5){realizedVolatility=200.f; volTargetMultiplier=1.f; return;}
        float mean=sumReturns/(float)tradeReturnCount;
        float variance=(sumReturnsSq/(float)tradeReturnCount)-(mean*mean);
        realizedVolatility=(variance>0.f)?sqrtf(variance):100.f;
        if(realizedVolatility>0.f)
            volTargetMultiplier=FClamp(targetVolatility/realizedVolatility, 0.5f, 2.0f);
    }

    void computeAntiMartingale() {
        if(streakDirection==1) antiMartingaleLevel=FMin(1.5f, 1.f+streakLength*0.1f);
        else if(streakDirection==-1) antiMartingaleLevel=FMax(0.4f, 1.f-streakLength*0.15f);
        else antiMartingaleLevel=1.f;
    }

    void updateSlippage(float slippage) {
        cumSlippage+=slippage; slippageSamples++; avgSlippage=cumSlippage/(float)slippageSamples;
    }

    void updateVolRegime(float atr) {
        if(baselineATR<=0.f){baselineATR=atr;atrEMA=atr;currentATRRatio=1.f;volRegime=1;return;}
        float alpha=0.1f;
        atrEMA=alpha*atr+(1.f-alpha)*atrEMA;
        baselineATR+=0.01f*(atrEMA-baselineATR);
        currentATRRatio=atrEMA/baselineATR;
        if(currentATRRatio<0.7f) volRegime=0;
        else if(currentATRRatio<1.3f) volRegime=1;
        else if(currentATRRatio<2.0f) volRegime=2;
        else volRegime=3;
        computeRiskMultiplier();
    }

    void updateTimeDecay(int currentBar) {
        barsSinceLastTrade++;
        if(barsSinceLastTrade<50) timeDecayFactor=1.f;
        else if(barsSinceLastTrade>200) timeDecayFactor=0.5f;
        else{float decay=(float)(barsSinceLastTrade-50)/150.f; timeDecayFactor=1.f-decay*0.5f;}
    }

    void computeRiskMultiplier() {
        if(volRegime==0) volMultiplier=1.20f;
        else if(volRegime==1) volMultiplier=1.00f;
        else if(volRegime==2) volMultiplier=0.70f;
        else volMultiplier=0.40f;

        // [v12.1] Kelly cold-start raised 0.8 -> 0.9. Combined with RM_FLOOR=0.60,
        // this lets the strategy actually fire on a fresh account before any
        // history accumulates. Once cumTrades>=10 the real Kelly formula takes over.
        if(cumTrades<10) kellyMultiplier=0.9f;
        else if(practicalKelly<=0.f) kellyMultiplier=0.25f;
        else if(practicalKelly>=0.20f) kellyMultiplier=1.25f;
        else if(practicalKelly>=0.15f) kellyMultiplier=1.10f;
        else if(practicalKelly>=0.10f) kellyMultiplier=1.00f;
        else kellyMultiplier=0.75f;

        if(equityCurveRegime==2) equityCurveMultiplier=1.15f;
        else if(equityCurveRegime==1) equityCurveMultiplier=1.00f;
        else equityCurveMultiplier=0.60f;

        antiMartingaleMultiplier=antiMartingaleLevel;

        if(sessionDrawdown<=0.f) drawdownMultiplier=1.00f;
        else if(sessionDrawdown<500.f) drawdownMultiplier=0.95f;
        else if(sessionDrawdown<1000.f) drawdownMultiplier=0.80f;
        else if(sessionDrawdown<1500.f) drawdownMultiplier=0.60f;
        else if(sessionDrawdown<2000.f) drawdownMultiplier=0.40f;
        else drawdownMultiplier=0.25f;

        recoveryMultiplier_ = inRecoveryMode ? 0.60f : 1.00f;
        timeDecayMultiplier = timeDecayFactor;

        if(dailyRiskBudget>0.f){
            float budgetRatio=riskBudgetRemaining/dailyRiskBudget;
            if(budgetRatio>=0.75f) budgetMultiplier=1.00f;
            else if(budgetRatio>=0.50f) budgetMultiplier=0.80f;
            else if(budgetRatio>=0.25f) budgetMultiplier=0.50f;
            else budgetMultiplier=0.25f;
        } else budgetMultiplier=1.00f;

        float profitMultiplier=1.0f;
        if(profitScaleEnabled && sessionTrades>=3 && sessionPnL>0.f){
            if(sessionPnL>=1500.f) profitMultiplier=1.30f;
            else if(sessionPnL>=1000.f) profitMultiplier=1.20f;
            else if(sessionPnL>=500.f) profitMultiplier=1.12f;
            else if(sessionPnL>=250.f) profitMultiplier=1.06f;
        }

        float core = powf(volMultiplier,0.30f) * powf(kellyMultiplier,0.25f) * powf(drawdownMultiplier,0.25f);
        float secondary = powf(equityCurveMultiplier,0.10f) * powf(antiMartingaleMultiplier,0.10f);
        float modifiers = recoveryMultiplier_ * timeDecayMultiplier * budgetMultiplier * profitMultiplier;
        riskMultiplier = FClamp(core * secondary * modifiers, 0.10f, 2.00f);

        if(sessionConsecLosses>=4) riskMultiplier=FMin(riskMultiplier, 0.25f);
        else if(sessionConsecLosses>=3) riskMultiplier=FMin(riskMultiplier, 0.50f);
        if(dailyRiskBudget>0.f && sessionDrawdown>dailyRiskBudget*0.8f)
            riskMultiplier=FMin(riskMultiplier, 0.25f);
    }

    void getRiskDiagnostics(char* buf, int bufSize) const {
        snprintf(buf, bufSize,
            "RM=%.2f [V=%.2f K=%.2f EC=%.2f AM=%.2f DD=%.2f Rec=%.2f TD=%.2f Bud=%.2f] "
            "Kelly=%.1f%% VaR=$%.0f CVaR=$%.0f Strk=%s%d",
            riskMultiplier, volMultiplier, kellyMultiplier, equityCurveMultiplier,
            antiMartingaleMultiplier, drawdownMultiplier, recoveryMultiplier_,
            timeDecayMultiplier, budgetMultiplier,
            practicalKelly*100.f, historicalVaR95, historicalCVaR95,
            streakDirection>0?"W":streakDirection<0?"L":"", streakLength);
    }
};

// ============================================================================
//  REGIME CLASSIFIER
// ============================================================================
struct RegimeClassifier {
    int trendRegime, volRegime, chopRegime;
    float trendStrength, rangeWidth, chopScore;
    void reset(){trendRegime=0;volRegime=1;chopRegime=0;trendStrength=rangeWidth=chopScore=0.f;}
    void update(float cumDelta, float priceChange, float atr, float range, int lookback){
        if(lookback<=0||atr<=0.f) return;
        float deltaPerBar=cumDelta/(float)lookback;
        trendStrength=FAbs(deltaPerBar)/(atr*0.1f);
        if(trendStrength>1.5f&&priceChange>atr*0.5f) trendRegime=1;
        else if(trendStrength>1.5f&&priceChange<-atr*0.5f) trendRegime=-1;
        else trendRegime=0;
        float follow=FAbs(priceChange)/(range>0.f?range:1.f);
        chopScore=1.f-follow;
        if(chopScore>0.85f) chopRegime=3;
        else if(chopScore>0.7f) chopRegime=2;
        else if(chopScore>0.4f) chopRegime=1;
        else chopRegime=0;
        rangeWidth=range;
    }
    bool allowMode(int mode, bool isLong) const {
        if(mode==0 && chopRegime>=2 && trendStrength<0.3f) return false;
        if(mode==0 && trendStrength>2.5f) return false;
        if(mode==2 && trendStrength>4.5f) return false;
        if(mode==3 && chopRegime>=2 && trendStrength<1.f) return false;
        if(mode==4 && chopRegime>=3 && trendStrength<0.5f) return false;
        if(mode==5 && chopRegime>=3 && trendStrength<0.3f) return false;
        if(mode==6 && trendStrength>5.f) return false;
        return true;
    }
};

// ============================================================================
//  PERSISTENT SLOT ENUMS
//
//  Typed slot identifiers replacing magic numbers. Three separate enum
//  spaces — int, float, pointer — each starting at 1 because slot 0 is
//  reserved by Sierra. When adding a new state variable, append to the
//  matching enum before _COUNT; never reuse a retired slot number or
//  state from a previous build may appear in the wrong variable.
// ============================================================================
enum PersistInt {
    PI_TradeState      = 1,
    PI_T1Hit           = 2,
    PI_Reserved_3      = 3,   // historically unused; preserved for wire compat
    PI_Trades          = 4,
    PI_LastDay         = 5,
    PI_EntryBar        = 6,
    PI_DiagBar         = 7,
    PI_LastExitBar     = 8,
    PI_LastTradeDir    = 9,
    PI_DayDone         = 10,
    PI_ConsecLoss      = 11,
    PI_LastSigBar      = 12,
    PI_VWAPBars        = 13,
    PI_T1HitBar        = 14,
    PI_LastM5Bar       = 15,
    PI_LastSkipLogBar  = 16,
    PI_LastM5LogBar    = 17,
    PI_LastBlockLogBar = 18,
    PI_LastVPBar       = 19,
    PI_LastCalcBar     = 20,
    PI_LastStopBar     = 21,
    PI_LastStopDir     = 22,
    PI_TradeMode       = 23,
    PI_TradeScore      = 24,
    PI_EntryQty        = 25,
    PI_HypoSide        = 26,
    PI_HypoWins        = 27,
    PI_HypoLosses      = 28,
    PI_HypoCount       = 29,
    PI_PrevPosQty      = 30,
    PI_FlattenReason   = 31,
    PI_PrevImbDir      = 32,
    PI_PrevImbStr      = 33,
    PI_LastWaitLogBar  = 34,
    PI_NewsLogSession  = 35,
    PI_NewsLogWindow   = 36,
    PI_LiveTradeDir    = 37,
    PI_HypoQty         = 38,
    PI_HypoT1Hit       = 39,
    PI_VP50Pending     = 40,
    PI_LastSymbolHash  = 41,
    PI_BannerShown     = 42,
    PI_LastExitWasLoss = 43,
    // 44–91: previously reserved for iof_v1_hooks.h ring (removed v12.26).
    // Slots remain unused to preserve PI numbering for any in-flight chartbooks.
    PI_HasEnteredThisLoad = 92,   // [v12.29] per-chart latch for pre-entry DayOpenPnL tracking
};

enum PersistFloat {
    PF_EntryPx        = 1,
    PF_StopPx         = 2,
    PF_T1Px           = 3,
    PF_T2Px           = 4,
    PF_DayOpenPnL     = 5,
    PF_VWAPPxVol      = 6,
    PF_VWAPVol        = 7,
    PF_PrevSettle     = 8,
    PF_VWAPSqVol      = 9,
    PF_SessHigh       = 10,
    PF_SessLow        = 11,
    PF_SessOpen       = 12,
    PF_TradeMAE       = 13,
    PF_TradeMFE       = 14,
    PF_ExpectedFillPx = 15,
    PF_HypoEntryPx    = 16,
    PF_HypoSLPx       = 17,
    PF_HypoTP1Px      = 18,
    PF_HypoTotalPnL   = 19,
    PF_HypoMaxDD      = 20,
    PF_HypoPeakEq     = 21,
    PF_HypoGrossWin   = 22,
    PF_HypoGrossLoss  = 23,
    PF_PrevImbExtreme = 24,
    PF_HypoTP2Px      = 25,
    PF_HypoBEPx       = 26,
    PF_HypoPartialPnL = 27,
    // [v12.24] Snapshot of pos.CumulativeProfitLoss taken at entry. Used at
    //          exit to back-compute the actual broker-side average fill price
    //          when the strategy's reason-resolution falls through to UNKNOWN.
    PF_CumPnL_AtEntry = 28,
};

enum PersistPtr {
    PP_VPState                  = 1,
    PP_TrapState                = 2,
    PP_BalanceState             = 3,
    PP_ImbalanceState           = 4,
    PP_CumDeltaDivState         = 5,
    PP_FadeSetup                = 6,
    PP_IcebergState             = 7,
    PP_InterdayLevels           = 8,
    PP_NewsBlackout             = 9,
    PP_VolCooldownState         = 10,
    PP_ModeStats                = 11,
    PP_AdaptiveThresholds       = 12,
    PP_VolScaler                = 13,
    PP_InstitutionalRiskState   = 14,
    PP_RegimeClassifier         = 15,
    PP_DeltaRing                = 16,
};

// ============================================================================
//  MAIN STUDY
// ============================================================================
SCSFExport scsf_IOF_NQ_Autopilot_FixBrkt500(SCStudyInterfaceRef sc)
{
    // ---- Subgraphs ----
    SCSubgraphRef SG_VWAP=sc.Subgraph[0]; SCSubgraphRef SG_BUY=sc.Subgraph[1]; SCSubgraphRef SG_SELL=sc.Subgraph[2];
    SCSubgraphRef SG_STOP=sc.Subgraph[3]; SCSubgraphRef SG_T1=sc.Subgraph[4]; SCSubgraphRef SG_T2=sc.Subgraph[5];
    SCSubgraphRef SG_DELTA=sc.Subgraph[6]; SCSubgraphRef SG_ATR=sc.Subgraph[7]; SCSubgraphRef SG_ADELT=sc.Subgraph[8];
    SCSubgraphRef SG_VPOC=sc.Subgraph[9]; SCSubgraphRef SG_VAH=sc.Subgraph[10]; SCSubgraphRef SG_VAL=sc.Subgraph[11];
    SCSubgraphRef SG_VB2U=sc.Subgraph[12]; SCSubgraphRef SG_VB2L=sc.Subgraph[13]; SCSubgraphRef SG_VWAPV=sc.Subgraph[14];
    SCSubgraphRef SG_VPOC50=sc.Subgraph[15]; SCSubgraphRef SG_CTRL=sc.Subgraph[16];
    SCSubgraphRef SG_EMAF=sc.Subgraph[17]; SCSubgraphRef SG_EMAS=sc.Subgraph[18];

    // ---- Inputs ----
    SCInputRef IN_LIVE=sc.Input[0];
    SCInputRef IN_CAPITAL=sc.Input[1];
    SCInputRef IN_DAILY_LOSS=sc.Input[2];
    SCInputRef IN_MAX_TRADES=sc.Input[3];
    SCInputRef IN_FLAT_TIME=sc.Input[4];
    SCInputRef IN_TOTQTY=sc.Input[5];
    SCInputRef IN_LOG_LVL=sc.Input[6];
    SCInputRef IN_CSV=sc.Input[7];
    SCInputRef IN_ENTRY_ORD=sc.Input[8];
    SCInputRef IN_DIAG=sc.Input[9];
    SCInputRef IN_REGIME_FILTER=sc.Input[10];
    SCInputRef IN_FADE_ENABLE=sc.Input[11];
    SCInputRef IN_NEWS_FILTER=sc.Input[12];
    SCInputRef IN_AUTO_DISABLE=sc.Input[13];
    SCInputRef IN_DAILY_PROF=sc.Input[14];
    SCInputRef IN_M1_ENABLE=sc.Input[15];   // [v12.5] toggle M1 on/off
    SCInputRef IN_M1_PULLBACK=sc.Input[16]; // [v12.33] M1 wick-reject pullback gate
    // sc.Input[17..24] previously held the V1 hooks layer (removed v12.26 after
    // backtest showed legacy confirmation thresholds incompatible with v12.25
    // modes on 3k volume bars). Gap is deliberate — keeps Input[25] addressable
    // from existing chartbook saves.
    SCInputRef IN_SESSION_START=sc.Input[25];  // [v12.23] 0=RTH only, 100=01:00 ET
    SCInputRef IN_LOG_NOFIRE=sc.Input[26];     // [v12.35] per-bar mode-rejection diag

    if(sc.SetDefaults){
        sc.GraphName="IOF NQ Autopilot [FixBrkt 500/500 - VARIANT]";
        sc.StudyDescription="IOF NQ Pure Orderflow — VARIANT: production v12.38 with a FIXED "
            "$500 stop / $500 target bracket (NQ 25pt, MNQ 250pt, 1:1 R:R) replacing the "
            "ATR stop + T1/T2 + breakeven + trail. No partial, no trail, no early-scratch. "
            "Everything else is production: all modes, floor 50 (M8 60), MT=1, $800 daily loss. "
            "v12.25: Active modes M1-M4,M6,M8 (M5+M7 removed). CtrlScore gates M3/M6/M8. "
            "M1 ON dip+reclaim default (v12.34) + dead-zone(12-14h) + trend gate. News ON, $1000 daily loss cap, no profit cap. "
            "RTH only (09:35 ET open, 15:55 flatten). Apex $150K eval Phase 1: 1 contract, 3 trades/day. "
            "3k (3000) vol bars. CSV IOF_NQ_*.csv.";
        sc.AutoLoop=1; sc.GraphRegion=0;
        sc.SendOrdersToTradeService=0; sc.AllowOnlyOneTradePerBar=0;
        sc.MaximumPositionAllowed=1; sc.SupportReversals=0;
        sc.CancelAllOrdersOnEntriesAndReversals=0;
        sc.UseGUIAttachedOrderSetting=0; sc.MaintainAdditionalChartDataArrays=1;

        SG_VWAP.Name="aVWAP"; SG_VWAP.DrawStyle=DRAWSTYLE_IGNORE;
        SG_VWAPV.Name="VWAP"; SG_VWAPV.DrawStyle=DRAWSTYLE_LINE;
        SG_VWAPV.PrimaryColor=RGB(212,168,67); SG_VWAPV.LineWidth=2;
        SG_BUY.Name="Long"; SG_BUY.DrawStyle=DRAWSTYLE_IGNORE;
        SG_SELL.Name="Short"; SG_SELL.DrawStyle=DRAWSTYLE_IGNORE;
        SG_STOP.Name="Stop"; SG_STOP.DrawStyle=DRAWSTYLE_IGNORE;
        SG_T1.Name="T1"; SG_T1.DrawStyle=DRAWSTYLE_IGNORE;
        SG_T2.Name="T2"; SG_T2.DrawStyle=DRAWSTYLE_IGNORE;
        SG_DELTA.Name="CumDelta"; SG_DELTA.DrawStyle=DRAWSTYLE_BAR; SG_DELTA.DrawZeros=0;
        SG_DELTA.PrimaryColor=RGB(0,172,193); SG_DELTA.SecondaryColor=RGB(229,115,115);
        SG_ATR.Name="ATR"; SG_ATR.DrawStyle=DRAWSTYLE_IGNORE;
        SG_ADELT.Name="|Dlt|Adpt"; SG_ADELT.DrawStyle=DRAWSTYLE_IGNORE;
        SG_VPOC.Name="VP POC 5d"; SG_VPOC.DrawStyle=DRAWSTYLE_DASH;
        SG_VPOC.PrimaryColor=RGB(255,64,129); SG_VPOC.LineWidth=2; SG_VPOC.DrawZeros=0;
        SG_VAH.Name="VP VAH"; SG_VAH.DrawStyle=DRAWSTYLE_DASH;
        SG_VAH.PrimaryColor=RGB(156,126,184); SG_VAH.LineWidth=1; SG_VAH.DrawZeros=0;
        SG_VAL.Name="VP VAL"; SG_VAL.DrawStyle=DRAWSTYLE_DASH;
        SG_VAL.PrimaryColor=RGB(156,126,184); SG_VAL.LineWidth=1; SG_VAL.DrawZeros=0;
        SG_VB2U.Name="VWAP+2sd"; SG_VB2U.DrawStyle=DRAWSTYLE_IGNORE;
        SG_VB2L.Name="VWAP-2sd"; SG_VB2L.DrawStyle=DRAWSTYLE_IGNORE;
        SG_VPOC50.Name="VP POC 50d"; SG_VPOC50.DrawStyle=DRAWSTYLE_DASH;
        SG_VPOC50.PrimaryColor=RGB(255,165,0); SG_VPOC50.LineWidth=1; SG_VPOC50.DrawZeros=0;
        SG_CTRL.Name="Control"; SG_CTRL.DrawStyle=DRAWSTYLE_IGNORE;
        SG_EMAF.Name="V1 EMA fast (internal)"; SG_EMAF.DrawStyle=DRAWSTYLE_IGNORE;
        SG_EMAS.Name="V1 EMA slow (internal)"; SG_EMAS.DrawStyle=DRAWSTYLE_IGNORE;

        // ====================================================================
        //  APEX TRADER FUNDING — $150K EVAL ACCOUNT PROFILE  [v12.24]
        // ====================================================================
        //  Account math:
        //    Starting balance ........ $150,000
        //    Trailing drawdown ....... $5,000 (EOD trailing)
        //    Profit target to pass ... +$9,000
        //    Half-size scaling rule .. trade <= half max until +$3K above start
        //    Max contracts (post +3K)  ~15 NQ minis
        //    Apex flat-by-rule ....... 4:59 PM ET (we use 15:55 as buffer)
        //
        //  The SetDefaults below are PHASE 1 — survival-first while the $5K
        //  trailing DD is at its tightest and scaling rules force half-size.
        //  Promote to PHASE 2 only after the conditions in the table below.
        //
        //  ─── PHASE 1 — DEFAULTS BELOW (balance < $153K) ─────────────────────
        //    Quantity           : 1
        //    Daily Loss $       : 800         (= one 40pt stop; 5-day cushion)
        //    Daily Profit $     : 0 (off)     (don't truncate right-tail days)
        //    Max Trades         : 1           (1pd config 2026-07-02; DL=800 kept as misconfig insurance)
        //    News Filter        : 1 (ON)
        //    Entry Order        : 2 (lmt+2t)
        //    Session Start      : 0 (RTH only)
        //
        //  ─── PHASE 2 — ENTER ONLY WHEN ALL OF THESE HOLD ─────────────────────
        //    1. Balance >= $153,000 (Apex full-size unlocked)
        //    2. >= 30 live trades completed
        //    3. Live profit factor > 1.5 over those 30 trades
        //    4. No unresolved EXTERNAL exits in the journal
        //  Then change:
        //    IN_TOTQTY          : 1   ->  2   (cautious step; NOT 5+)
        //    IN_DAILY_LOSS      : 1000 -> 1500 (~2 stops @ 2 contracts)
        //  Leave Max Trades, News Filter, Entry Ord, Session Start unchanged.
        //
        //  ─── PHASE 3 — APPROACHING PASS (balance >= $156K) ──────────────────
        //  Trailing DD floor is now $151K — a real buffer above starting bal.
        //  STAY at Phase 2 settings; do NOT scale to 3+ contracts. The math
        //  doesn't need it: 2 contracts x backtest edge = ~$6K / 5 months,
        //  which buys the last $3K to pass in ~2.5 months. Speed isn't worth
        //  blowup risk at the finish line.
        //
        //  ─── DO NOT ─────────────────────────────────────────────────────────
        //    - Scale past 2 contracts during eval (even though Apex allows 15)
        //    - Enable Daily Profit cap (cuts the M6 outlier days that fund PF)
        //    - Trade through FOMC / NFP / earnings (disable strategy manually)
        //    - Enable overnight session (SESSION_START=100) — untested
        //    - Trust passing pace > backtest's +$3K / 5 months at 1 contract.
        //      Median expected pass time ~10-14 months. Plan accordingly.
        // ====================================================================

        IN_LIVE.Name="Enable Auto Trading (1=live orders)"; IN_LIVE.SetInt(1);
        IN_CAPITAL.Name="Account Capital ($)"; IN_CAPITAL.SetFloat(150000.0f);  // [v12.24] Apex $150K eval
        IN_DAILY_LOSS.Name="Daily Loss $ (drives risk budget)"; IN_DAILY_LOSS.SetFloat(800.f);  // Apex $150K eval: $800/day caps you at ~5 stop-out days before liquidation
        IN_MAX_TRADES.Name="Max Trades/Day"; IN_MAX_TRADES.SetInt(1);  // [2026-07-02] 1pd config: MT=1 + DL=800 byte-identical to validated 1pd/no-caps on 3 frozen contracts; cap stays as misconfig insurance
        IN_FLAT_TIME.Name="Flatten HHMM"; IN_FLAT_TIME.SetInt(iof_unified::kDefaultFlattenHhmm);
        IN_TOTQTY.Name="Max Position Size (1 lot only)"; IN_TOTQTY.SetInt(1); IN_TOTQTY.SetIntLimits(1, 1);
        IN_LOG_LVL.Name="Log Level (0=crit,1=sig,2=dbg)"; IN_LOG_LVL.SetInt(1); IN_LOG_LVL.SetIntLimits(0,2);
        IN_CSV.Name="Enable CSV Journal"; IN_CSV.SetYesNo(1);
        IN_ENTRY_ORD.Name="Entry (0=mkt,1=lmt,2=lmt+2t)"; IN_ENTRY_ORD.SetInt(2);  // [v12.24] marketable limit — avoids worst-case market fills
        IN_DIAG.Name="Diagnostics (0=off,1=on)"; IN_DIAG.SetInt(1);
        IN_REGIME_FILTER.Name="Regime Mode Filter (0=off)"; IN_REGIME_FILTER.SetInt(1);
        IN_FADE_ENABLE.Name="Fade Engine (0=off)"; IN_FADE_ENABLE.SetInt(1);
        IN_NEWS_FILTER.Name="News Filter (0=off)"; IN_NEWS_FILTER.SetInt(1);   // [v12.24] Apex eval: ON — protect cushion from 08:30/10:00/14:00 ET spikes
        IN_AUTO_DISABLE.Name="Auto-disable bad modes (0=off)"; IN_AUTO_DISABLE.SetInt(1);
        IN_DAILY_PROF.Name="Daily Profit Target $ (0=disabled)"; IN_DAILY_PROF.SetFloat(0.f);  // [v12.38] OFF — validated config; profit cap truncates the M6 outlier days that fund PF (see DO-NOT table above; sweep 2026-06-05 = no-op-to-harmful)
        IN_M1_ENABLE.Name="Enable M1 VWAP Reclaim"; IN_M1_ENABLE.SetInt(1);   // [v12.23] default ON
        IN_M1_PULLBACK.Name="M1 Pullback Mode (0=off,1=wick,2=dip)"; IN_M1_PULLBACK.SetInt(2);  // [v12.34] default=2 dip+reclaim (cross-contract A/B passed 2026-05-22). 1=wick-reject (failed A/B).
        IN_SESSION_START.Name="Session Start HHMM (0=RTH 09:35, 100=01:00 ET)"; IN_SESSION_START.SetInt(0);  // [v12.24] RTH only — overnight session unvalidated
        IN_LOG_NOFIRE.Name="Log mode-rejection reasons (0=off,1=on)"; IN_LOG_NOFIRE.SetInt(0);  // [v12.35] per-bar M2/M4/M6 NOFIRE diag — verbose, off by default
        return;
    }

    sc.SendOrdersToTradeService = (IN_LIVE.GetInt() != 0) ? 1 : 0;

    const float TICK=sc.TickSize;
    float TICK_VAL=sc.CurrencyValuePerTick;
    bool isMNQ=(strstr(sc.Symbol.GetChars(),"MNQ")||strstr(sc.Symbol.GetChars(),"mnq"));
    if(TICK_VAL<=0.f)TICK_VAL=isMNQ?0.50f:5.0f;
    const float PtVal = (TICK>0.f) ? (TICK_VAL/TICK) : 0.f;
    // [VARIANT FB500] Single source of truth for "this build runs a fixed $
    //   bracket". Read by BOTH the position-management block (to bypass the
    //   partial/breakeven/trail/scratch machinery) and the entry block (to
    //   bypass the ATR bracket). One predicate means the two halves cannot
    //   disagree about which exit regime a live trade is under.
    const bool FIXBRKT_ACTIVE = C_VAR_FIXBRKT_ON && PtVal>0.f &&
                                C_VAR_FIXBRKT_STOP_USD>0.f && C_VAR_FIXBRKT_TGT_USD>0.f &&
                                (C_VAR_FIXBRKT_STOP_USD/PtVal)>=TICK &&
                                (C_VAR_FIXBRKT_TGT_USD /PtVal)>=TICK;
    const float Capital=IN_CAPITAL.GetFloat();
    const float RiskPct=0.02f;
    const float MaxRiskPerTrade=Capital*RiskPct;
    const float DAILY_LOSS=IN_DAILY_LOSS.GetFloat();
    const int MAX_TRADES=IN_MAX_TRADES.GetInt();
    const int FLAT_TIME=IN_FLAT_TIME.GetInt();
    const int TOTQTY=IClamp(IN_TOTQTY.GetInt(), 1, 1);
    const int DIAG=IN_DIAG.GetInt();
    const int LOG_LVL=IN_LOG_LVL.GetInt();
    const bool CSV_ON=IN_CSV.GetYesNo();
    const int ENTRY_ORD=IN_ENTRY_ORD.GetInt();
    const int REGIME_FILTER=IN_REGIME_FILTER.GetInt();
    const int FADE_ENABLE=IN_FADE_ENABLE.GetInt();
    const int NEWS_FILTER=IN_NEWS_FILTER.GetInt();
    const int AUTO_DISABLE=IN_AUTO_DISABLE.GetInt();
    const float DAILY_PROF=IN_DAILY_PROF.GetFloat();
    const int M1_ENABLE=IN_M1_ENABLE.GetInt();   // [v12.5]
    const int M1_PULLBACK=IN_M1_PULLBACK.GetInt(); // [v12.33]
    const int LOG_NOFIRE=IN_LOG_NOFIRE.GetInt();   // [v12.35]
    if(TICK<=0.f)return;

    // Wilder ATR on chart OHLC (same units as price). Works on any bar type including
    // NQ **volume bars** (e.g. 3k contracts/bar): ATR is **not** derived from kTargetVolumeBars
    // and needs no rescaling when you change chart volume per bar — only bar count/time change.
    sc.ATR(sc.BaseData, SG_ATR, C_ATR_PER, MOVAVGTYPE_WILDERS);
    sc.ExponentialMovAvg(sc.BaseDataIn[SC_LAST], SG_EMAF, 9);
    sc.ExponentialMovAvg(sc.BaseDataIn[SC_LAST], SG_EMAS, 21);
    const int Idx=sc.Index;
    const bool BarClosed = (sc.GetBarHasClosedStatus(Idx) == BHCS_BAR_HAS_CLOSED);
    const float Delta0=sc.AskVolume[Idx]-sc.BidVolume[Idx];
    { const float ad=FAbs(Delta0); const float prev=(Idx>0)?SG_ADELT[Idx-1]:ad;
      SG_ADELT[Idx]=prev+2.f/66.f*(ad-prev); }
    const float Close0=sc.Close[Idx],High0=sc.High[Idx],Low0=sc.Low[Idx],Open0=sc.Open[Idx];
    float ATR=SG_ATR[Idx];
    if(ATR<=0.f){
        const float barR=High0-Low0;
        ATR=FMax(TICK*2.f,barR>TICK*0.25f?barR:TICK*4.f);
    }
    const float Close1=(Idx>0)?sc.Close[Idx-1]:Close0;
    const float High1=(Idx>0)?sc.High[Idx-1]:High0;
    const float Low1=(Idx>0)?sc.Low[Idx-1]:Low0;
    const float Open1=(Idx>0)?sc.Open[Idx-1]:Open0;
    const float Vol0=sc.Volume[Idx];
    const bool barBullish=(Close0>Open0),barBearish=(Close0<Open0);
    const float barBody=FAbs(Close0-Open0),barRange=High0-Low0;
    const int RTH_OPEN = iof_unified::kDefaultRthOpenHhmm;   // 935 — anchors VWAP/stats
    const int SESS_START = (IN_SESSION_START.GetInt() > 0) ? IN_SESSION_START.GetInt() : RTH_OPEN;  // [v12.23] entry gate
    SCDateTime barDT=sc.BaseDateTimeIn[Idx];
    int BarDate=barDT.GetDate(),BarTime=barDT.GetTime();
    const int BarHHMM=iof_session::BarHhmmFromSecondsSinceMidnight(BarTime);

    // ============================================================================
    //  PERSISTENT STATE
    // ============================================================================
    int& TradeState     = sc.GetPersistentInt(PI_TradeState);
    int& T1Hit          = sc.GetPersistentInt(PI_T1Hit);
    int& Trades         = sc.GetPersistentInt(PI_Trades);
    int& LastDay        = sc.GetPersistentInt(PI_LastDay);
    int& EntryBar       = sc.GetPersistentInt(PI_EntryBar);
    int& DiagBar        = sc.GetPersistentInt(PI_DiagBar);
    int& LastExitBar    = sc.GetPersistentInt(PI_LastExitBar);
    int& LastTradeDir   = sc.GetPersistentInt(PI_LastTradeDir);
    int& DayDone        = sc.GetPersistentInt(PI_DayDone);
    int& ConsecLoss     = sc.GetPersistentInt(PI_ConsecLoss);
    int& LastSigBar     = sc.GetPersistentInt(PI_LastSigBar);
    int& VWAPBars       = sc.GetPersistentInt(PI_VWAPBars);
    int& T1HitBar       = sc.GetPersistentInt(PI_T1HitBar);
    int& LastM5Bar       = sc.GetPersistentInt(PI_LastM5Bar);
    int& LastM5LogBar    = sc.GetPersistentInt(PI_LastM5LogBar);
    int& LastSkipLogBar  = sc.GetPersistentInt(PI_LastSkipLogBar);
    int& LastBlockLogBar = sc.GetPersistentInt(PI_LastBlockLogBar);
    int& LastVPBar       = sc.GetPersistentInt(PI_LastVPBar);
    int& LastCalcBar     = sc.GetPersistentInt(PI_LastCalcBar);
    int& LastStopBar  = sc.GetPersistentInt(PI_LastStopBar);
    int& LastStopDir  = sc.GetPersistentInt(PI_LastStopDir);
    int& TradeMode  = sc.GetPersistentInt(PI_TradeMode);
    int& TradeScore = sc.GetPersistentInt(PI_TradeScore);
    int& EntryQty   = sc.GetPersistentInt(PI_EntryQty);
    int& HypoSide   = sc.GetPersistentInt(PI_HypoSide);
    int& HypoWins   = sc.GetPersistentInt(PI_HypoWins);
    int& HypoLosses = sc.GetPersistentInt(PI_HypoLosses);
    int& HypoCount  = sc.GetPersistentInt(PI_HypoCount);
    int& PrevPosQty    = sc.GetPersistentInt(PI_PrevPosQty);
    int& FlattenReason = sc.GetPersistentInt(PI_FlattenReason);
    int& PrevImbDir = sc.GetPersistentInt(PI_PrevImbDir);
    int& PrevImbStr = sc.GetPersistentInt(PI_PrevImbStr);
    int& LastWaitLogBar = sc.GetPersistentInt(PI_LastWaitLogBar);
    int& NewsLogSession = sc.GetPersistentInt(PI_NewsLogSession);
    int& NewsLogWindow  = sc.GetPersistentInt(PI_NewsLogWindow);
    int& LiveTradeDir   = sc.GetPersistentInt(PI_LiveTradeDir);
    int& HypoQty        = sc.GetPersistentInt(PI_HypoQty);
    int& HypoT1Hit      = sc.GetPersistentInt(PI_HypoT1Hit);
    int& VP50Pending    = sc.GetPersistentInt(PI_VP50Pending);
    int& LastSymbolHash = sc.GetPersistentInt(PI_LastSymbolHash);
    int& BannerShown    = sc.GetPersistentInt(PI_BannerShown);
    int& LastExitWasLoss = sc.GetPersistentInt(PI_LastExitWasLoss);
    int& HasEnteredThisLoad = sc.GetPersistentInt(PI_HasEnteredThisLoad);  // [v12.29]

    float& EntryPx     = sc.GetPersistentFloat(PF_EntryPx);
    float& StopPx      = sc.GetPersistentFloat(PF_StopPx);
    float& T1Px        = sc.GetPersistentFloat(PF_T1Px);
    float& T2Px        = sc.GetPersistentFloat(PF_T2Px);
    float& DayOpenPnL  = sc.GetPersistentFloat(PF_DayOpenPnL);
    float& VWAPPxVol   = sc.GetPersistentFloat(PF_VWAPPxVol);
    float& VWAPVol     = sc.GetPersistentFloat(PF_VWAPVol);
    float& PrevSettle  = sc.GetPersistentFloat(PF_PrevSettle);
    float& VWAPSqVol   = sc.GetPersistentFloat(PF_VWAPSqVol);
    float& sessHigh = sc.GetPersistentFloat(PF_SessHigh);
    float& sessLow  = sc.GetPersistentFloat(PF_SessLow);
    float& sessOpen = sc.GetPersistentFloat(PF_SessOpen);
    float& TradeMAE = sc.GetPersistentFloat(PF_TradeMAE);
    float& TradeMFE = sc.GetPersistentFloat(PF_TradeMFE);
    float& ExpectedFillPx = sc.GetPersistentFloat(PF_ExpectedFillPx);
    float& CumPnL_AtEntry = sc.GetPersistentFloat(PF_CumPnL_AtEntry);
    float& HypoEntryPx    = sc.GetPersistentFloat(PF_HypoEntryPx);
    float& HypoSLPx       = sc.GetPersistentFloat(PF_HypoSLPx);
    float& HypoTP1Px      = sc.GetPersistentFloat(PF_HypoTP1Px);
    float& HypoTotalPnL   = sc.GetPersistentFloat(PF_HypoTotalPnL);
    float& HypoMaxDD      = sc.GetPersistentFloat(PF_HypoMaxDD);
    float& HypoPeakEq     = sc.GetPersistentFloat(PF_HypoPeakEq);
    float& HypoGrossWin   = sc.GetPersistentFloat(PF_HypoGrossWin);
    float& HypoGrossLoss  = sc.GetPersistentFloat(PF_HypoGrossLoss);
    float& PrevImbExtreme = sc.GetPersistentFloat(PF_PrevImbExtreme);
    float& HypoTP2Px      = sc.GetPersistentFloat(PF_HypoTP2Px);
    float& HypoBEPx       = sc.GetPersistentFloat(PF_HypoBEPx);
    float& HypoPartialPnL = sc.GetPersistentFloat(PF_HypoPartialPnL);

    #define GETPTR(T, idx) T* p##T = (T*)sc.GetPersistentPointer(idx); \
        if(!p##T){ p##T = (T*)sc.AllocateMemory(sizeof(T)); \
            if(!p##T){ LogError(sc, "AllocateMemory failed for " #T); return; } \
            sc.SetPersistentPointer(idx, p##T); p##T->reset(); }

    GETPTR(VPState, PP_VPState)
    GETPTR(TrapState, PP_TrapState)
    GETPTR(BalanceState, PP_BalanceState)
    GETPTR(ImbalanceState, PP_ImbalanceState)
    GETPTR(CumDeltaDivState, PP_CumDeltaDivState)
    GETPTR(FadeSetup, PP_FadeSetup)
    GETPTR(IcebergState, PP_IcebergState)
    GETPTR(InterdayLevels, PP_InterdayLevels)
    GETPTR(NewsBlackout, PP_NewsBlackout)
    GETPTR(VolCooldownState, PP_VolCooldownState)
    GETPTR(ModeStats, PP_ModeStats)
    GETPTR(AdaptiveThresholds, PP_AdaptiveThresholds)
    GETPTR(VolScaler, PP_VolScaler)
    GETPTR(InstitutionalRiskState, PP_InstitutionalRiskState)
    GETPTR(RegimeClassifier, PP_RegimeClassifier)
    GETPTR(DeltaRing, PP_DeltaRing)

    #undef GETPTR

    VPState* pVP = pVPState;
    TrapState* pTrap = pTrapState;
    BalanceState* pBal = pBalanceState;
    ImbalanceState* pImb = pImbalanceState;
    CumDeltaDivState* pDiv = pCumDeltaDivState;
    FadeSetup* pFade = pFadeSetup;
    IcebergState* pIce = pIcebergState;
    InterdayLevels* pID = pInterdayLevels;
    NewsBlackout* pNews = pNewsBlackout;
    VolCooldownState* pVCool = pVolCooldownState;
    ModeStats* pMS = pModeStats;
    AdaptiveThresholds* pAT = pAdaptiveThresholds;
    VolScaler* pVS = pVolScaler;
    InstitutionalRiskState* pRisk = pInstitutionalRiskState;
    RegimeClassifier* pRegime = pRegimeClassifier;
    DeltaRing* pDR = pDeltaRing;

    if(pDR) pDR->pushBar(sc, Idx);

    {
        const char* sym = sc.Symbol.GetChars();
        int h = 0;
        for(int i=0; sym[i] && i<16; i++) h = h*31 + (unsigned char)sym[i];
        if(h == 0) h = 1;
        if(LastSymbolHash == 0) {
            LastSymbolHash = h;
        } else if(LastSymbolHash != h) {
            LogError(sc, "Symbol changed mid-chart; state is stale. Reload the study.");
            LastSymbolHash = h;
        }
    }

    if(!BannerShown && DIAG) {
        InitRunID();
        SCString m;
        m.Format("=== V18A v12.37 LOAD sym=%s tick=%.4f tickval=$%.2f Cap=$%.0f DailyLoss=$%.0f DailyProf=$%.0f "
                 "MaxTr=%d FlatT=%d Qty=%d Live=%d Regime=%d Fade=%d News=%d AutoDis=%d M1=%d "
                 "RM_FLOOR=%.2f QUAL_FLOOR=%d CD_trd=%d CD_loss=%d CD_stop=%d RunID=%llu ===",
            sc.Symbol.GetChars(), TICK, TICK_VAL, Capital, DAILY_LOSS, DAILY_PROF,
            MAX_TRADES, FLAT_TIME, TOTQTY, IN_LIVE.GetInt(),
            REGIME_FILTER, FADE_ENABLE, NEWS_FILTER, AUTO_DISABLE, M1_ENABLE,
            C_RM_FLOOR, V18A_QUALITY_FLOOR,
            C_COOLDOWN_AFTER_TRADE, C_COOLDOWN_AFTER_LOSS, C_POST_STOP_COOLDOWN,
            g_runID);
        sc.AddMessageToLog(m, 0);
        BannerShown = 1;
    }

    if(pRisk) pRisk->profitScaleEnabled = true;

    if(pAT&&barRange>0.f) pAT->update(sc.AskVolume[Idx],sc.BidVolume[Idx],barBody,barRange,Vol0);
    if(pVS&&barRange>0.f&&iof_session::AtOrAfterRthOpen(BarHHMM,RTH_OPEN)) pVS->update(barRange);

    // [v12.31] Risk-state EMA updates were previously called only inside the
    //          setup-detection path (around line ~3390). On bars where setup
    //          detection silent-returns (cooldown, news, sessionDD, etc.),
    //          the volRegime EMA went un-sampled — leading to jumpy/stale
    //          regime classification. Moved here so the EMA tracks every bar.
    //          updateVolRegime internally calls computeRiskMultiplier, so
    //          riskMultiplier also stays fresh for the dashboard.
    // [v12.32] Gated by Idx != pRisk->lastUpdateBar. Without this gate
    //          AutoLoop=1 caused updateTimeDecay's barsSinceLastTrade++ to
    //          inflate ~50-200x per bar; timeDecayFactor pinned to 0.5 within
    //          a couple of clock-bars after every trade. updateVolRegime's
    //          alpha=0.1 EMA also collapsed to current-bar ATR (no smoothing)
    //          and baselineATR drifted ~63%/bar instead of ~1%/bar.
    if(pRisk && ATR > 0.f && Idx != pRisk->lastUpdateBar){
        pRisk->lastUpdateBar = Idx;
        pRisk->updateVolRegime(ATR);
        pRisk->updateTimeDecay(Idx);
    }

    s_SCPositionData pos; sc.GetTradePosition(pos);
    int CurQ=pos.PositionQuantity;

    // [v12.29] Pre-entry baseline tracking. Keep DayOpenPnL synced to broker
    // CumPL on every flat bar UNTIL the strategy fires its first live entry
    // of the *current day*. Once LiveTradeDir transitions to non-zero, latch
    // freezes so subsequent winning trades aren't zeroed out of brokerDayPnL.
    //
    // v12.29 changes from v12.28:
    //   (a) Latch is now a per-chart PersistInt (PI_HasEnteredThisLoad), not a
    //       DLL-wide function-static. v12.28's static was shared by every chart
    //       running this study in the same Sierra process — NQM6's first entry
    //       silently latched NQH6's pre-entry tracker, leaving NQH6's
    //       DayOpenPnL frozen at whatever it held at reload.
    //   (b) Latch is reset to 0 at every day-rollover (see DAY RESET block).
    //       v12.28 only cleared the latch on DLL reload, so day-2 onward
    //       relied entirely on the day-reset's single CumPL snapshot —
    //       which is the very lag-prone read that v12.27 already proved
    //       unreliable when Sierra/Rithmic sync is slow.
    //
    // Why the underlying mechanism exists: v12.27 locked the baseline on the
    // first flat bar after DLL load, which captured CumPL=0 when Sierra/Rithmic
    // hadn't synced position yet. Late-arriving carryover P&L (e.g. $1640 from
    // a prior-day trade) then read as today's strategy P&L and armed DAILY_PROF
    // (observed 2026-05-20 NQM6: DLL loaded 05:10 CumPL=0, broker pushed $1640
    // by 08:35, gate armed, zero entries all RTH).
    //
    // Mid-trade reload caveat: if LiveTradeDir is non-zero on bar 1 from
    // persistent state, the latch flips immediately and we never re-snapshot.
    // DayOpenPnL retains its pre-reload persistent value, which is the best
    // we can do without knowing the true pre-trade baseline.
    if(LiveTradeDir != 0) HasEnteredThisLoad = 1;

    if(!HasEnteredThisLoad && CurQ == 0){
        float prior = DayOpenPnL;
        DayOpenPnL = pos.CumulativeProfitLoss;
        if(LOG_LVL >= LOG_SIG && prior != DayOpenPnL){
            SCString g; g.Format("[V18A INIT] DayOpenPnL re-snapshot (pre-entry): "
                                 "prior=%.2f new=%.2f (CumPL=%.2f)",
                                 prior, DayOpenPnL, pos.CumulativeProfitLoss);
            sc.AddMessageToLog(g, 0);
        }
    }

    if(LiveTradeDir!=0&&CurQ!=0){
        bool isL=(LiveTradeDir>0);
        float adv=isL?(EntryPx-Low0):(High0-EntryPx);
        float fav=isL?(High0-EntryPx):(EntryPx-Low0);
        if(adv>TradeMAE)TradeMAE=adv;
        if(fav>TradeMFE)TradeMFE=fav;
    }

    // ============================================================================
    //  EXIT DETECTION
    // ============================================================================
    if(PrevPosQty!=0&&CurQ==0&&LiveTradeDir!=0){
        bool isL=(LiveTradeDir>0);
        int Hold=Idx-EntryBar;

        // [v12.24] Reconstruct the broker's *actual* average exit fill price
        //          from realized P/L. This is the ground truth — independent
        //          of our reason-resolution. Used as the final fallback for
        //          ExPx whenever we can't tag the exit definitively. Guarded
        //          against EntryQty<=0 and PtVal<=0 to avoid divide-by-zero.
        float ExPxFromBroker = 0.f;
        bool  HasBrokerPx    = false;
        if(EntryQty > 0 && PtVal > 0.f){
            float realizedTradePnL = pos.CumulativeProfitLoss - CumPnL_AtEntry;
            float dollarsPerPt     = PtVal * (float)EntryQty;
            if(dollarsPerPt > 0.f){
                float pts = realizedTradePnL / dollarsPerPt;
                ExPxFromBroker = isL ? (EntryPx + pts) : (EntryPx - pts);
                HasBrokerPx    = true;

                // [v12.24] Sanity guard against stale CumPnL_AtEntry snapshot.
                //          Failure mode: the DLL is reloaded mid-trade so slot
                //          28 was never written under v12.24 → defaults to 0 →
                //          realizedTradePnL absorbs the entire session's prior
                //          P/L → ExPxFromBroker lands wildly far from EntryPx.
                //          Threshold = 5x configured stop distance, with ATR
                //          fallback if StopPx is unset. Stale snapshots are
                //          silently rejected; the EXTERNAL path below then
                //          uses bar Close instead.
                float scale = FAbs(EntryPx - StopPx);
                if(scale < TICK) scale = (ATR > 0.f) ? ATR : 10.f;
                if(FAbs(ExPxFromBroker - EntryPx) > 5.f * scale){
                    HasBrokerPx = false;
                    if(LOG_LVL >= LOG_SIG){
                        SCString w; w.Format(
                            "[V18A EXIT][SNAPSHOT_STALE] Broker-derived "
                            "ExPx=%.2f implausibly far from EntryPx=%.2f "
                            "(scale=%.2f). Likely v12.24 DLL reloaded "
                            "mid-trade. Falling back to bar Close=%.2f.",
                            ExPxFromBroker, EntryPx, scale, Close0);
                        sc.AddMessageToLog(w, 1);
                    }
                }
            }
        }

        // Default: bar close, used only if nothing better resolves below.
        const char* ExR="UNKNOWN"; float ExPx=Close0;

        if(FlattenReason==FR_FLATTEN){ExR="FLATTEN"; FlattenReason=FR_NONE;}
        else if(FlattenReason==FR_CB){ExR="CB"; FlattenReason=FR_NONE;}
        else if(FlattenReason==FR_SPIKE){ExR="SPIKE"; FlattenReason=FR_NONE;}
        else if(FlattenReason==FR_DAILY_PROFIT){ExR="DAILY_PROFIT"; FlattenReason=FR_NONE;}
        else if(FlattenReason==FR_DAILY_LOSS){ExR="DAILY_LOSS"; FlattenReason=FR_NONE;}
        else if(FlattenReason==FR_STOP){ExR="STOP"; ExPx=StopPx; FlattenReason=FR_NONE;}
        else if(FlattenReason==FR_TRAIL){ExR="TRAIL"; ExPx=StopPx; FlattenReason=FR_NONE;}
        else if(FlattenReason==FR_SCRATCH){ExR="SCRATCH"; FlattenReason=FR_NONE;}
        else if(FlattenReason==FR_T1){ExR="TP1"; ExPx=T1Px; FlattenReason=FR_NONE;}
        else if(FlattenReason==FR_T2){ExR="TP2"; ExPx=T2Px; FlattenReason=FR_NONE;}
        else if(isL){
            if(Low0<=StopPx+TICK){ExR="STOP"; ExPx=StopPx;}
            else if(High0>=T2Px){ExR="TP2"; ExPx=T2Px;}
            else if(High0>=T1Px){ExR="TP1"; ExPx=T1Px;}
        } else {
            if(High0>=StopPx-TICK){ExR="STOP"; ExPx=StopPx;}
            else if(Low0<=T2Px){ExR="TP2"; ExPx=T2Px;}
            else if(Low0<=T1Px){ExR="TP1"; ExPx=T1Px;}
        }

        // [v12.24] Reason still UNKNOWN means: position went flat, no internal
        //          flatten path fired, AND the bar's H/L doesn't justify any
        //          of our SL/TP levels. The only remaining sources are
        //          Sierra's attached SL bracket, a manual flatten click, or
        //          a broker-side liquidation. Tag as EXTERNAL so this stops
        //          masquerading as a clean STOP in journals/stats. Also swap
        //          the bar-close fallback for the broker-derived fill price
        //          (line ~1872 above), which is the truth of record.
        if(strcmp(ExR, "UNKNOWN") == 0){
            ExR = "EXTERNAL";
            FlattenReason = FR_NONE;
            if(HasBrokerPx) ExPx = ExPxFromBroker;
            if(LOG_LVL >= LOG_SIG){
                SCString warn; warn.Format(
                    "[V18A EXIT][EXTERNAL] No internal flatten reason; bar "
                    "H/L=%.2f/%.2f did not touch SL=%.2f or T1/T2=%.2f/%.2f. "
                    "Hold=%d. ExPx(bar Close)=%.2f, ExPx(broker)=%.2f. "
                    "Likely Sierra-attached SL bracket or manual flatten — "
                    "verify in Sierra Trade Activity Log.",
                    High0, Low0, StopPx, T1Px, T2Px,
                    Hold, Close0, HasBrokerPx ? ExPxFromBroker : Close0);
                sc.AddMessageToLog(warn, 1);
            }
        }

        // [v12.25] Broker-fill correction for STOP/TRAIL exits.
        //          STOP/TRAIL paths previously hardcoded ExPx=StopPx — the
        //          "ideal" stop level — even when the broker bracket actually
        //          filled past it in fast markets. Live trade 2026-05-13 Wed:
        //          SL=29334.72, recorded ExPx=29334.72, but broker DayPnL=-$685
        //          implied actual fill ≈ 29335.25 (1 tick of slippage). The
        //          local PnL was off by ~$10, and equity-curve / Kelly stats
        //          (driven by pRisk->updatePnL(PnL) below) drift slightly low.
        //
        //          Fix: when HasBrokerPx is true (sanity-guarded earlier at the
        //          5x-stop-dist threshold) AND the broker fill is on the adverse
        //          side of StopPx AND the slip is meaningful (> 0.5 tick), use
        //          the broker fill as ExPx. PnL is recomputed from ExPx in the
        //          next statement, so this also corrects local accounting.
        if((strcmp(ExR,"STOP")==0 || strcmp(ExR,"TRAIL")==0) && HasBrokerPx){
            bool adverse = isL ? (ExPxFromBroker <= StopPx + TICK)
                               : (ExPxFromBroker >= StopPx - TICK);
            float slip = isL ? (StopPx - ExPxFromBroker)
                             : (ExPxFromBroker - StopPx);
            if(adverse && FAbs(ExPxFromBroker - StopPx) > TICK*0.5f){
                if(LOG_LVL >= LOG_SIG){
                    SCString s; s.Format(
                        "[V18A EXIT][SLIPPAGE] %s broker fill=%.2f vs "
                        "StopPx=%.2f (slip=%.2f pts, $%.0f). Using broker "
                        "fill for ExPx and recomputing local PnL.",
                        ExR, ExPxFromBroker, StopPx, slip,
                        slip * PtVal * (float)EntryQty);
                    sc.AddMessageToLog(s, 0);
                }
                ExPx = ExPxFromBroker;
            }
        }

        float PnL=isL?(ExPx-EntryPx)*PtVal:(EntryPx-ExPx)*PtVal;
        PnL *= (float)EntryQty;
        float todayPnL2=pos.CumulativeProfitLoss-DayOpenPnL;

        const char* modeNames[]={"M1","M2","M3","M4","M5","M6","M7","M8"};
        const char* mName=(TradeMode>=0&&TradeMode<8)?modeNames[TradeMode]:"M?";

        if(pMS) pMS->record(TradeMode, PnL, TradeMAE, TradeMFE, Hold);
        if(pRisk) pRisk->updatePnL(PnL);

        if(LOG_LVL>=LOG_SIG){
            SCString M; M.Format("[V18A EXIT] %s %s %s $%.0f Sc=%d Hold:%d MAE:%.1f MFE:%.1f",
                isL?"LONG":"SHORT", mName, ExR, PnL, TradeScore, Hold, TradeMAE, TradeMFE);
            sc.AddMessageToLog(M,0);
        }

        if(CSV_ON){
            WriteCSV(sc, "EXIT", isL?"BUY":"SELL", mName, EntryPx, StopPx, T1Px, T2Px,
                EntryQty, TradeScore, 0, 0, 0, 0,
                ExPx, ExR, Hold, TradeMAE, TradeMFE,
                todayPnL2, pRisk?pRisk->cumPnL:0.f, 0, 0,
                pRisk?pRisk->riskMultiplier:1.f,
                pRegime?pRegime->trendRegime:0,
                pRegime?pRegime->volRegime:1,
                pRegime?pRegime->chopRegime:0);
        }

        const bool isLoss = (PnL < 0.f);
        const bool isStop = (strcmp(ExR, "STOP") == 0);
        if(isStop) {
            LastStopBar = Idx;
            LastStopDir = isL ? 1 : -1;
        }
        if(isLoss) {
            ConsecLoss++;
        } else if(PnL > 0.f) {
            ConsecLoss = 0;
        }
        LastExitWasLoss = isLoss ? 1 : 0;

        TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
        TradeMAE=0.f; TradeMFE=0.f; TradeMode=-1;
        LiveTradeDir=0;
    }
    PrevPosQty=CurQ;

    // ============================================================================
    //  HYPO P&L TRACKING
    // ============================================================================
    if(HypoSide!=0){
        float BH=High0,BL=Low0;
        int qty = (HypoQty>0)?HypoQty:1;
        int t1Leg = (qty>=2) ? (int)floorf((float)qty*C_T1_RATIO) : 0;
        if(t1Leg>=qty) t1Leg = qty-1;
        int t2Leg = qty - t1Leg;

        if(!HypoT1Hit){
            bool SLH=(HypoSide==1)?(BL<=HypoSLPx):(BH>=HypoSLPx);
            bool TPH=(HypoSide==1)?(BH>=HypoTP1Px):(BL<=HypoTP1Px);
            float perUnitSL=(HypoSide==1)?(HypoSLPx-HypoEntryPx)*PtVal:(HypoEntryPx-HypoSLPx)*PtVal;
            float perUnitTP=(HypoSide==1)?(HypoTP1Px-HypoEntryPx)*PtVal:(HypoEntryPx-HypoTP1Px)*PtVal;

            bool closedFull=false, wonT1=false; float legPnL=0.f;
            if(SLH&&TPH){
                float DSL=FAbs(Open0-HypoSLPx),DTP=FAbs(Open0-HypoTP1Px);
                if(DSL<=DTP){legPnL=perUnitSL*(float)qty; closedFull=true;}
                else{legPnL=perUnitTP*(float)t1Leg; wonT1=true;}
            } else if(SLH){
                legPnL=perUnitSL*(float)qty; closedFull=true;
            } else if(TPH){
                if(t1Leg>0){legPnL=perUnitTP*(float)t1Leg; wonT1=true;}
                else{legPnL=perUnitTP*(float)qty; closedFull=true;}
            }

            if(closedFull){
                float slipAndComm = (C_SLIPPAGE_BASE*2.0f*PtVal+C_COMMISSION_RT)*(float)qty;
                legPnL -= slipAndComm;
                HypoTotalPnL += legPnL;
                if(legPnL>0.f){HypoWins++; HypoGrossWin+=legPnL;}
                else{HypoLosses++; HypoGrossLoss+=FAbs(legPnL);}
                HypoSide=0; HypoQty=0; HypoT1Hit=0; HypoPartialPnL=0.f;
            } else if(wonT1){
                float slipAndComm = (C_SLIPPAGE_BASE*PtVal+C_COMMISSION_RT*0.5f)*(float)t1Leg;
                legPnL -= slipAndComm;
                HypoPartialPnL = legPnL;
                HypoT1Hit = 1;
                HypoBEPx = (HypoSide==1)? TFloor(HypoEntryPx+ATR*0.3f, TICK)
                                        : TCeil (HypoEntryPx-ATR*0.3f, TICK);
            }
        } else {
            bool BEH=(HypoSide==1)?(BL<=HypoBEPx):(BH>=HypoBEPx);
            bool T2H=(HypoSide==1)?(BH>=HypoTP2Px):(BL<=HypoTP2Px);
            float perUnitBE=(HypoSide==1)?(HypoBEPx-HypoEntryPx)*PtVal:(HypoEntryPx-HypoBEPx)*PtVal;
            float perUnitT2=(HypoSide==1)?(HypoTP2Px-HypoEntryPx)*PtVal:(HypoEntryPx-HypoTP2Px)*PtVal;

            bool closeRunner=false; float runnerPnL=0.f;
            if(BEH&&T2H){
                float DBE=FAbs(Open0-HypoBEPx),DT2=FAbs(Open0-HypoTP2Px);
                if(DBE<=DT2){runnerPnL=perUnitBE*(float)t2Leg; closeRunner=true;}
                else{runnerPnL=perUnitT2*(float)t2Leg; closeRunner=true;}
            } else if(BEH){runnerPnL=perUnitBE*(float)t2Leg; closeRunner=true;}
            else if(T2H){runnerPnL=perUnitT2*(float)t2Leg; closeRunner=true;}

            if(closeRunner){
                float slipAndComm = (C_SLIPPAGE_BASE*PtVal+C_COMMISSION_RT*0.5f)*(float)t2Leg;
                runnerPnL -= slipAndComm;
                float totalPnL = HypoPartialPnL + runnerPnL;
                HypoTotalPnL += totalPnL;
                if(totalPnL>0.f){HypoWins++; HypoGrossWin+=totalPnL;}
                else{HypoLosses++; HypoGrossLoss+=FAbs(totalPnL);}
                HypoSide=0; HypoQty=0; HypoT1Hit=0; HypoPartialPnL=0.f;
            }
        }
        if(HypoSide==0){
            if(HypoTotalPnL>HypoPeakEq) HypoPeakEq=HypoTotalPnL;
            float HDD=HypoPeakEq-HypoTotalPnL; if(HDD>HypoMaxDD) HypoMaxDD=HDD;
        }
    }

    // ============================================================================
    //  DAY RESET
    // ============================================================================
    if(BarDate!=LastDay){
        if(LastDay>0&&pID){
            pID->prevDate=LastDay;
            pID->prevHigh=sessHigh; pID->prevLow=sessLow;
            pID->prevOpen=sessOpen; pID->prevClose=Close1;
            pID->valid=true;
        }
        if(Trades>0&&CSV_ON){
            float sessPnL = pRisk ? pRisk->sessionPnL : 0.f;
            float cumPnL  = pRisk ? pRisk->cumPnL    : 0.f;
            WriteCSV(sc, "SESSION", "", "", 0,0,0,0, Trades, 0, 0,0,0,0,
                0, "", 0, 0, 0, sessPnL, cumPnL, 0, 0,
                pRisk?pRisk->riskMultiplier:1.f, 0, 1, 0);
        }
        DayOpenPnL=0.f; sc.GetTradePosition(pos);
        DayOpenPnL=pos.CumulativeProfitLoss;
        if(pVP){
            bool found=false;
            for(int d=0;d<VP_50_DAYS;d++) if(pVP->days[d].dateTag==BarDate){pVP->activeDayIdx=d;found=true;break;}
            if(!found){
                int emptySlot = -1;
                for(int d=0; d<VP_50_DAYS; d++) {
                    if(pVP->days[d].dateTag == 0) { emptySlot = d; break; }
                }
                int victim;
                if(emptySlot >= 0) {
                    victim = emptySlot;
                } else {
                    victim = 0;
                    int ot = pVP->days[0].dateTag;
                    for(int d=1; d<VP_50_DAYS; d++) {
                        if(pVP->days[d].dateTag < ot) { victim = d; ot = pVP->days[d].dateTag; }
                    }
                }
                pVP->activeDayIdx = victim;
                VPDay& nd = pVP->days[victim];
                nd.dateTag = BarDate;
                nd.basePx  = Close0;
                for(int b=0; b<VP_MAX_BINS; b++) nd.bins[b] = 0.f;
            }
        }
        LastDay=BarDate; Trades=0; DayDone=0; EntryBar=-1;
        LastExitBar=-1; LastTradeDir=0; DiagBar=-1;
        TradeState=0; T1Hit=0; T1HitBar=-1;
        if(LiveTradeDir != 0) {
            LogError(sc, "LiveTradeDir non-zero at day rollover; forcing reset");
        }
        LiveTradeDir=0;
        // [v12.29] Clear pre-entry latch so day N+1 re-syncs DayOpenPnL on
        //          flat bars until the strategy's first entry of the new
        //          day. Without this, the snapshot above is the only chance
        //          to capture pos.CumulativeProfitLoss — which is exactly
        //          the Sierra/Rithmic-lag scenario v12.27 already proved
        //          unreliable.
        HasEnteredThisLoad=0;
        ConsecLoss=0; LastExitWasLoss=0; LastSigBar=-1;
        LastM5Bar=-1; LastM5LogBar=-1;
        LastSkipLogBar=-1; LastBlockLogBar=-1;
        LastVPBar=-1; LastCalcBar=-1;
        LastStopBar=-1; LastStopDir=0;
        FlattenReason=FR_NONE;
        VP50Pending=1;
        PrevImbDir=0; PrevImbStr=0; PrevImbExtreme=0.f;
        LastWaitLogBar=-1; NewsLogSession=0; NewsLogWindow=0;
        TradeMode=-1; TradeScore=0; EntryQty=0;
        TradeMAE=0.f; TradeMFE=0.f;

        // [v12.7] clear stale hypo state so next session starts clean
        HypoSide=0; HypoQty=0; HypoT1Hit=0; HypoPartialPnL=0.f;
        HypoEntryPx=0.f; HypoSLPx=0.f; HypoTP1Px=0.f; HypoTP2Px=0.f; HypoBEPx=0.f;

        EntryPx=0.f; StopPx=0.f; T1Px=0.f; T2Px=0.f; VWAPBars=0;
        float seedPx=(PrevSettle>0.f)?PrevSettle:Close0;
        VWAPPxVol=seedPx*1000.f; VWAPVol=1000.f; VWAPSqVol=seedPx*seedPx*1000.f;

        pTrap->reset(); pBal->reset(); pImb->reset();
        pDiv->reset(); pFade->reset(); pIce->reset();
        pNews->reset();
        if(pVS) pVS->reset();
        if(pDR) pDR->reset();
        if(pRisk){pRisk->newSession(); pRisk->setDailyBudget(DAILY_LOSS);}
        if(pRegime) pRegime->reset();
        if(pID) pID->currOpen=Close0;

        if(DIAG){SCString m;m.Format("=== V18A SESSION %d Capital=$%.0f Budget=$%.0f ===",
            BarDate, Capital, DAILY_LOSS);
            sc.AddMessageToLog(m,0);}
    }
    if(pDR && !pDR->primed) pDR->pushBar(sc, Idx);

    if(pID&&iof_session::AtOrAfterRthOpen(BarHHMM,RTH_OPEN)){
        if(VWAPBars<=1){sessOpen=Open0; sessHigh=High0; sessLow=Low0;}
        if(High0>sessHigh)sessHigh=High0;
        if(Low0<sessLow)sessLow=Low0;
        pID->currOpen=sessOpen;
    }

    // ============================================================================
    //  VWAP ACCUMULATION
    // ============================================================================
    {
        float tp=(High0+Low0+Close0)/3.f,bv=Vol0;
        VWAPPxVol+=tp*bv; VWAPVol+=bv; VWAPSqVol+=tp*tp*bv;
        if(iof_session::AtOrAfterRthOpen(BarHHMM,RTH_OPEN)) VWAPBars++;
        if(BarHHMM>=1755&&BarHHMM<=1805) PrevSettle=Close0;
        else if(iof_session::InRthThroughFlattenInclusive(BarHHMM,RTH_OPEN,FLAT_TIME)&&PrevSettle<=0.f) PrevSettle=Close0;
    }
    const float VWAP=(VWAPVol>0.f)?(VWAPPxVol/VWAPVol):0.f;
    SG_VWAP[Idx]=(VWAP>0.f)?VWAP:Close0; SG_VWAPV[Idx]=SG_VWAP[Idx];
    float vwapSD=0.f;
    if(VWAPVol>0.f&&VWAP>0.f){float var=(VWAPSqVol/VWAPVol)-VWAP*VWAP;if(var>0.f)vwapSD=sqrtf(var);}
    SG_VB2U[Idx]=VWAP+vwapSD*2.f; SG_VB2L[Idx]=VWAP-vwapSD*2.f;

    // ============================================================================
    //  VOLUME PROFILE
    // ============================================================================
    if(pVP&&BarHHMM>=930&&BarHHMM<=1600){
        if(Idx!=LastVPBar){
            LastVPBar=Idx;
            VPDay&cd=pVP->days[pVP->activeDayIdx];
            if(cd.basePx>0.f) pVP->addBar(High0,Low0,Vol0,cd.basePx);
            pVP->updateSinglePrints(High0,Low0,Vol0);
            if(Idx%10==0||VWAPBars<=1) pVP->computeLevels5();
            if(VP50Pending || VWAPBars<=1){
                pVP->computeLevels50();
                VP50Pending=0;
            }
        }
    }
    if(pVP&&pVP->valid&&pVP->poc>1000.f){SG_VPOC[Idx]=pVP->poc;SG_VAH[Idx]=pVP->vah;SG_VAL[Idx]=pVP->val;}
    else{SG_VPOC[Idx]=0.f;SG_VAH[Idx]=0.f;SG_VAL[Idx]=0.f;}
    if(pVP&&pVP->valid50&&pVP->poc50>1000.f) SG_VPOC50[Idx]=pVP->poc50; else SG_VPOC50[Idx]=0.f;

    if(TradeState!=0){SG_STOP[Idx]=StopPx;SG_T1[Idx]=T1Px;SG_T2[Idx]=T2Px;}
    else{SG_STOP[Idx]=0.f;SG_T1[Idx]=0.f;SG_T2[Idx]=0.f;}
    SG_DELTA[Idx]=Delta0;
    if(Delta0<0.f) SG_DELTA.DataColor[Idx]=SG_DELTA.SecondaryColor;
    else SG_DELTA.DataColor[Idx]=SG_DELTA.PrimaryColor;

    if(BarHHMM < SESS_START) return;   // [v12.23] session gate (SESS_START=100 → 01:00 ET)
    if(ATR<=0.f) return;

    // [v12.11] Signal-only gate — fixes the 29-SETUPs-zero-ENTRYs issue.
    //
    // Previous logic: signalOnly = IsFullRecalculation || IsReplayRunning || !onRealtimeBar
    // That blocked orders during replay, which is wrong — Sierra's trade-sim engine
    // is specifically designed to fill orders submitted during replay. It also blocked
    // orders any time the chart wasn't on its very last bar, which happens routinely
    // (study reloads, chartbook opens, etc.) and left SETUPs with no ENTRYs.
    //
    // Sierra's recommended pattern for auto-trading studies is to suppress live
    // order submission only during full recalc and historical-download states
    // (see Sierra docs / ACSILProgrammingConcepts.html):
    //     if(sc.IsFullRecalculation || sc.ChartIsDownloadingHistoricalData(sc.ChartNumber)) return;
    //
    // We keep signalOnly for diagnostic purposes but *narrow* the gate to only
    // block during full-recalc / historical-download. Replay is allowed to
    // submit — Sierra's trade-sim handles fills.
    const bool onRealtimeBar = (Idx >= sc.ArraySize - 1);
    const bool inReplay = sc.IsReplayRunning() != 0;
    const bool inHistoricalDownload = sc.ChartIsDownloadingHistoricalData(sc.ChartNumber) != 0;
    const bool inFullRecalc = sc.IsFullRecalculation != 0;
    // Block submissions only during full recalc or historical download.
    // Replay is allowed. Non-latest bars during normal iteration are allowed
    // (Sierra dedups repeated BuyEntry calls on the same bar).
    const bool signalOnly = inFullRecalc || inHistoricalDownload;

    if(DIAG && LOG_LVL>=LOG_SIG && Idx==sc.ArraySize-1){
        static int lastLoggedBar = -1;
        if(Idx != lastLoggedBar){
            lastLoggedBar = Idx;
            SCString dm; dm.Format("[V18A GATE] signalOnly=%d FullRecalc=%d HistDL=%d Replay=%d RTbar=%d Idx=%d Size=%d",
                signalOnly?1:0, inFullRecalc?1:0, inHistoricalDownload?1:0,
                inReplay?1:0, onRealtimeBar?1:0, Idx, sc.ArraySize);
            sc.AddMessageToLog(dm,0);
        }
    }
    const float avgD=(SG_ADELT[Idx]>0.f)?SG_ADELT[Idx]:1.f;

    // ============================================================================
    //  DASHBOARD
    // ============================================================================
    if(Idx==sc.ArraySize-1 && pRisk){
        float WR=pRisk->cumTrades>0?pRisk->winRate()*100:0;
        float PF=pRisk->profitFactor();
        float todayPnL3=pos.CumulativeProfitLoss-DayOpenPnL+pos.OpenProfitLoss;
        s_UseTool TT; TT.Clear(); TT.ChartNumber=sc.ChartNumber;
        TT.DrawingType=DRAWING_TEXT; TT.LineNumber=99999;
        TT.BeginIndex=Idx-3; TT.BeginValue=High0+TICK*20;
        TT.Color=RGB(140,140,140); TT.FontSize=8; TT.AddMethod=UTAM_ADD_OR_ADJUST;
        SCString D;
        D.Format("V18A %s $%.0f W:%d L:%d %.0f%% PF:%.2f DD:$%.0f RM:%.2f T:%d",
            Close0>VWAP?"LONG_BIAS":"SHORT_BIAS",
            IN_LIVE.GetInt()?todayPnL3:HypoTotalPnL,
            HypoWins, HypoLosses, WR, PF, pRisk->maxSessionDrawdown,
            pRisk->riskMultiplier, Trades);
        TT.Text=D; sc.UseTool(TT);
    }

    // ============================================================================
    //  NEWS BLACKOUT (NEWS_FILTER-gated) + SPIKE/VCOOL PROTECTION (always on)
    //  Decoupled so disabling News Filter keeps volume-spike safety net.
    // ============================================================================
    if(NEWS_FILTER && pNews && pNews->isNewsWindow(BarHHMM)){
        int currentWindow=0;
        if(BarHHMM>=825&&BarHHMM<=835) currentWindow=1;
        else if(BarHHMM>=955&&BarHHMM<=1005) currentWindow=2;
        else if(BarHHMM>=1355&&BarHHMM<=1405) currentWindow=3;
        bool newWindow=(BarDate!=NewsLogSession)||(currentWindow!=NewsLogWindow);
        if(DIAG&&newWindow){
            NewsLogSession=BarDate; NewsLogWindow=currentWindow;
            sc.AddMessageToLog("NEWS BLACKOUT — no new entries",0);
        }
        goto TRADE_MGMT;
    }
    if(pNews){
        if(Idx>=1){
            float prevRange=sc.High[Idx-1]-sc.Low[Idx-1];
            float prevATR=(Idx>=2)?SG_ATR[Idx-1]:ATR;
            float prevAvgD=(Idx>=2&&SG_ADELT[Idx-1]>0.f)?SG_ADELT[Idx-1]:avgD;
            float rangeRatio=(prevATR>0.f)?(prevRange/prevATR):0.f;
            float prevDelta2=sc.AskVolume[Idx-1]-sc.BidVolume[Idx-1];
            bool rangeSpike=(prevATR>0.f&&prevRange>prevATR*C_SPIKE_ATR_M);
            bool deltaSpike=(FAbs(prevDelta2)>prevAvgD*4.f&&rangeRatio>=2.0f);
            if(rangeSpike||deltaSpike){
                if(pNews->spikeBar!=Idx-1){
                    pNews->spikeBar=Idx-1; pNews->spikeActive=true;
                    if(pVCool&&rangeRatio>=C_VCOOL_THRESH){
                        if(pVCool->spikeBar!=Idx-1){
                            pVCool->barsRemaining=C_VCOOL_PAUSE;
                            pVCool->spikeBar=Idx-1;
                            pVCool->spikeMultiple=rangeRatio;
                            pVCool->firstReentryPending=true;
                        }
                    }
                }
            }
        }
        if(pNews->spikeActive&&C_SPIKE_COOL>0&&Idx<=pNews->spikeBar+C_SPIKE_COOL){
            goto TRADE_MGMT;
        } else pNews->spikeActive=false;
        if(pVCool&&pVCool->barsRemaining>0){
            // [v12.31] Decrement only on new bar. AutoLoop=1 calls this study
            //          on every tick (~50-200 calls per 3000-vol bar); without
            //          this gate, C_VCOOL_PAUSE=40 collapsed to <1 bar in
            //          practice and the volatility cooldown was effectively
            //          disabled. lastLogBar was declared in VolCooldownState
            //          but previously unused — repurposed as last-decrement-
            //          bar tracker. Safety net for spike events now intact.
            if(Idx != pVCool->lastLogBar){
                pVCool->lastLogBar = Idx;
                pVCool->barsRemaining--;
            }
            if(pVCool->barsRemaining>0) goto TRADE_MGMT;
        }
    }

    // ============================================================================
    //  TRADE MANAGEMENT
    // ============================================================================
    TRADE_MGMT:
    if(!signalOnly){
        sc.GetTradePosition(pos); const int posQty=pos.PositionQuantity;

        if(iof_session::AtOrAfterFlatten(BarHHMM,FLAT_TIME)){
            if(posQty!=0||TradeState!=0){
                FlattenReason=FR_FLATTEN;
                sc.FlattenAndCancelAllOrders();
                TradeState=0; T1Hit=0; T1HitBar=-1;
            }
            DayDone=1; return;
        }
        if(DayDone) return;

        // [v12.24] Two independent day-P/L views.
        //   - brokerDayPnL: pos.CumulativeProfitLoss minus the session-start
        //     snapshot. Reflects ALL activity on the symbol (this strategy,
        //     other charts, manual trades, ATM exits, ...). Can be skewed
        //     by external activity or a stale DayOpenPnL after a mid-day
        //     reload (e.g. 2026-05-11 SETUP row logged DayPnL=+560 with no
        //     prior CSV trades that day).
        //   - stratSessPnL: strategy-internal accounting of just *its own*
        //     realized P/L. Immune to external trades and reload skew.
        // The DAILY_PROF / DAILY_LOSS caps fire on whichever view crosses
        // the threshold — conservative by design. Better to halt early
        // than to over-trade.
        float brokerDayPnL = pos.CumulativeProfitLoss - DayOpenPnL + pos.OpenProfitLoss;
        float stratSessPnL = pRisk ? pRisk->sessionPnL : 0.f;
        // [v12.26] Diag: trace gate inputs to reconcile against CSV DayPnL
        //          column. Investigating 2026-05-15 NQM6 case where CSV row
        //          logged brokerDayPnL=$1035 at SETUP yet daily-profit cap
        //          ($1000) did not fire. Same formula both sides — this
        //          log captures what the gate actually saw at evaluation.
        //          One line per new bar; LOG_SIG so it shows by default.
        static int s_LastGateLogBar = -1;
        if(LOG_LVL >= LOG_SIG && DAILY_PROF > 0.f && Idx != s_LastGateLogBar){
            s_LastGateLogBar = Idx;
            SCString g; g.Format(
                "[V18A GATE] Idx=%d DAILY_PROF=%.0f brokerDayPnL=%.2f "
                "stratSessPnL=%.2f DayOpenPnL=%.2f CumPL=%.2f OpenPL=%.2f "
                "posQty=%d",
                Idx, DAILY_PROF, brokerDayPnL, stratSessPnL,
                DayOpenPnL, pos.CumulativeProfitLoss, pos.OpenProfitLoss,
                posQty);
            sc.AddMessageToLog(g, 0);
        }
        if(DAILY_PROF>0.f && (brokerDayPnL>=DAILY_PROF || stratSessPnL>=DAILY_PROF)){
            if(posQty!=0){
                FlattenReason=FR_DAILY_PROFIT;
                sc.FlattenAndCancelAllOrders();
                SCString dl;
                dl.Format("profit lock hit (broker=%.2f strat=%.2f) cap=%.0f — flatten",
                    brokerDayPnL, stratSessPnL, DAILY_PROF);
                iof_unified::LogDaily(sc, dl, 0);
            }
            DayDone=1; return;
        }
        if(DAILY_LOSS>0.f && (brokerDayPnL<=-DAILY_LOSS || stratSessPnL<=-DAILY_LOSS)){
            if(posQty!=0){
                FlattenReason=FR_DAILY_LOSS;
                sc.FlattenAndCancelAllOrders();
                SCString dl;
                dl.Format("max loss lock hit (broker=%.2f strat=%.2f) cap=%.0f — flatten",
                    brokerDayPnL, stratSessPnL, DAILY_LOSS);
                iof_unified::LogDaily(sc, dl, 0);
            }
            DayDone=1; return;
        }

        if(pNews&&pNews->spikeActive&&posQty!=0&&TradeState!=0){
            if(pNews->spikeBar>=0&&Idx<=pNews->spikeBar+2){
                FlattenReason=FR_SPIKE;
                sc.FlattenAndCancelAllOrders();
                TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
                return;
            }
        }

        if(TradeState!=0&&posQty!=0&&EntryPx>0.f){
            bool isLcb=(TradeState==1||TradeState==2);
            float openPnLcb=isLcb?(Close0-EntryPx):(EntryPx-Close0);
            float stopDist=(StopPx>0.f)?FAbs(EntryPx-StopPx):ATR;
            float maxTradeRisk=FMax(stopDist*3.f, ATR*3.f);
            if(openPnLcb<-maxTradeRisk){
                FlattenReason=FR_CB;
                sc.FlattenAndCancelAllOrders();
                LastStopBar=Idx; LastStopDir=isLcb?1:-1;
                TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
                return;
            }
        }

        if(TradeState!=0&&Idx!=LastExitBar){
            const bool isL=(TradeState==1||TradeState==2);
            if((isL?(posQty<=0):(posQty>=0))&&Idx>EntryBar+3){
                TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx; return;
            }

            // ================================================================
            //  [VARIANT FB500] FIXED-BRACKET POSITION MANAGEMENT
            // ================================================================
            //  Under a fixed $500/$500 bracket there is nothing to manage: the
            //  two levels set at entry are the only two exits. This block
            //  replaces — and deliberately bypasses — the v12.37 early-scratch,
            //  the T1 partial + breakeven shift, the ATR trail and the T2 leg.
            //  Those all exist to shape a runner; a fixed target has no runner.
            //
            //  As in production this is a BACKSTOP: the broker-attached stop
            //  and target from the entry order are what normally fill. The
            //  stop arm is gated to Idx>EntryBar+3 to match the production
            //  detector (avoids double-exiting on the entry bar's own range).
            if(FIXBRKT_ACTIVE){
                if(Idx>EntryBar+3){
                    bool sHfb = isL ? (Low0<=StopPx+TICK) : (High0>=StopPx-TICK);
                    if(sHfb){
                        FlattenReason=FR_STOP;
                        s_SCNewOrder em; em.OrderType=SCT_ORDERTYPE_MARKET;
                        em.OrderQuantity=(int)FAbs((float)posQty);
                        if(isL) sc.BuyExit(em); else sc.SellExit(em);
                        LastStopBar=Idx; LastStopDir=isL?1:-1;
                        TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
                        return;
                    }
                }
                bool tHfb = isL ? (High0>=T1Px) : (Low0<=T1Px);
                if(tHfb){
                    FlattenReason=FR_T1;
                    s_SCNewOrder tx; tx.OrderType=SCT_ORDERTYPE_MARKET;
                    tx.OrderQuantity=(int)FAbs((float)posQty);
                    if(isL) sc.BuyExit(tx); else sc.SellExit(tx);
                    TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
                    return;
                }
                return;   // no trail, no partial, no scratch — hold to a level
            }

            // [v12.37] Early-scratch. Pre-T1 losers show MFE <= 17pt by bar 3
            //          while winners reach 59pt+ (sortino_audit.py on prod
            //          baselines). On the first bar the stop becomes eligible
            //          (EntryBar+4 == bar-3 close in backtest terms), exit at
            //          market if the trade never reached C_ES_MFE_FRAC of stop
            //          distance in MFE and price is still better than the stop
            //          level — so this can never fill worse than the stop the
            //          trade was already going to take. Counts as a stop for
            //          cooldown state when underwater. Cross-contract A/B
            //          2026-06-09 (backtest_early_scratch_ab.py): NQZ25 +$410,
            //          NQM5 tied, NQH6 +$440, no contract worse.
            if(!T1Hit&&Idx==EntryBar+4){
                float esStopDist=(StopPx>0.f)?FAbs(EntryPx-StopPx):ATR;
                float esOpenPnL=isL?(Close0-EntryPx):(EntryPx-Close0);
                if(TradeMFE<esStopDist*C_ES_MFE_FRAC&&esOpenPnL>-esStopDist){
                    FlattenReason=FR_SCRATCH;
                    s_SCNewOrder em; em.OrderType=SCT_ORDERTYPE_MARKET;
                    em.OrderQuantity=(int)FAbs((float)posQty);
                    if(isL) sc.BuyExit(em); else sc.SellExit(em);
                    if(esOpenPnL<0.f){ LastStopBar=Idx; LastStopDir=isL?1:-1; }
                    TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
                    return;
                }
            }
            if(!T1Hit&&Idx>EntryBar+3){
                bool sH=isL?(Low0<=StopPx+TICK):(High0>=StopPx-TICK);
                if(sH){
                    FlattenReason=FR_STOP;
                    s_SCNewOrder em; em.OrderType=SCT_ORDERTYPE_MARKET;
                    em.OrderQuantity=(int)FAbs((float)posQty);
                    if(isL) sc.BuyExit(em); else sc.SellExit(em);
                    LastStopBar=Idx; LastStopDir=isL?1:-1;
                    TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
                    return;
                }
            }
            if(!T1Hit){
                bool t1H=isL?(High0>=T1Px):(Low0<=T1Px);
                if(t1H){
                    int absPos=(int)FAbs((float)posQty);
                    // [v12.15] Compute T1-leg size from ACTUAL position (absPos),
                    //          not configured max (TOTQTY). Risk-multiplier
                    //          commonly clamps baseQty below TOTQTY at entry
                    //          time, so using TOTQTY here could produce an
                    //          exit-qty larger than what's open. The prior
                    //          code clamped to absPos-1 two lines down, so
                    //          no crash, but the basis was wrong. If
                    //          absPos<2 we skip the partial entirely (the
                    //          runner will exit via attached T1 or trail).
                    int q=0;
                    if(absPos>=2){
                        q=(int)FMax(1.f, floorf((float)absPos*C_T1_RATIO));
                        if(q>=absPos) q=absPos-1;
                        s_SCNewOrder t1x; t1x.OrderType=SCT_ORDERTYPE_MARKET;
                        t1x.OrderQuantity=q;
                        if(isL) sc.BuyExit(t1x); else sc.SellExit(t1x);
                    }
                    T1Hit=1; T1HitBar=Idx; TradeState=isL?2:4;
                    float bd=ATR*0.3f;
                    StopPx=isL?TFloor(EntryPx+bd,TICK):TCeil(EntryPx-bd,TICK);
                    // [v12.15] Removed premature ConsecLoss=0 here. T1-hit
                    //          closes only the partial leg; the runner is
                    //          still open and can stop out for a net loss.
                    //          ConsecLoss is canonically reset at line ~1441
                    //          on positive closed-trade PnL.
                }
            }
            if(T1Hit&&C_TRAIL_ATR>0.f){
                int absPos=(int)FAbs((float)posQty);
                int effectiveDelay=(absPos==1)?C_TRAIL_DLY*3:C_TRAIL_DLY;
                if(effectiveDelay<=0||(T1HitBar>=0&&Idx>=T1HitBar+effectiveDelay)){
                    const bool isL2=(TradeState==1||TradeState==2);
                    float t1Dist=FAbs(T1Px-EntryPx);
                    float curDist=isL2?(Close0-EntryPx):(EntryPx-Close0);
                    float baseTrail=C_TRAIL_ATR;
                    if(absPos==1&&t1Dist>0.f&&curDist<t1Dist*2.0f) baseTrail=C_TRAIL_ATR*2.5f;
                    float td=ATR*baseTrail;
                    if(t1Dist>0.f&&curDist>t1Dist*2.0f) td=FMin(td,ATR*0.75f);
                    float minStop=isL2?TFloor(EntryPx+ATR*0.3f,TICK):TCeil(EntryPx-ATR*0.3f,TICK);
                    if(isL2&&StopPx<minStop) StopPx=minStop;
                    if(!isL2&&StopPx>minStop) StopPx=minStop;
                    if(isL2){float ns=TFloor(Close0-td,TICK); if(ns>StopPx) StopPx=ns;}
                    else{float ns=TCeil(Close0+td,TICK); if(ns<StopPx) StopPx=ns;}
                }
            }
            if(T1Hit&&Idx>EntryBar+3){
                const bool isL3=(TradeState==1||TradeState==2);
                bool sH=isL3?(Low0<=StopPx+TICK):(High0>=StopPx-TICK);
                if(sH){
                    FlattenReason=FR_TRAIL;
                    s_SCNewOrder em; em.OrderType=SCT_ORDERTYPE_MARKET;
                    em.OrderQuantity=(int)FAbs((float)posQty);
                    if(isL3) sc.BuyExit(em); else sc.SellExit(em);
                    TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
                    return;
                }
            }
            if(Idx>EntryBar+2){
                const bool isL4=(TradeState==1||TradeState==2);
                bool t2H=isL4?(High0>=T2Px):(Low0<=T2Px);
                if(t2H){
                    FlattenReason=FR_T2;
                    s_SCNewOrder t2x; t2x.OrderType=SCT_ORDERTYPE_MARKET;
                    t2x.OrderQuantity=(int)FAbs((float)posQty);
                    if(isL4) sc.BuyExit(t2x); else sc.SellExit(t2x);
                    TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
                    return;
                }
            }
            return;
        }
        if(posQty!=0){
            if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] posQty=%d (external position)", posQty); sc.AddMessageToLog(sm,0); }
            return;
        }
        if(Trades>=MAX_TRADES) {
            if(DIAG && LOG_LVL>=LOG_DBG && Idx!=LastSkipLogBar) {
                LastSkipLogBar=Idx;
                sc.AddMessageToLog("[V18A] Skip — MAX_TRADES reached for session", 0);
            }
            return;
        }
        if(Idx==LastExitBar){
            if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] Idx==LastExitBar=%d", LastExitBar); sc.AddMessageToLog(sm,0); }
            return;
        }
        {
            // [v12.3] cooldowns halved (constants changed at top)
            const int cdBars = (LastExitWasLoss == 1) ? C_COOLDOWN_AFTER_LOSS : C_COOLDOWN_AFTER_TRADE;
            if(cdBars>0&&EntryBar>=0&&Idx<=EntryBar+cdBars){
                if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] post-entry cooldown EntryBar=%d cd=%d Idx=%d", EntryBar, cdBars, Idx); sc.AddMessageToLog(sm,0); }
                return;
            }
            if(cdBars>0&&LastExitBar>=0&&Idx<=LastExitBar+cdBars){
                if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] post-exit cooldown LastExitBar=%d cd=%d Idx=%d", LastExitBar, cdBars, Idx); sc.AddMessageToLog(sm,0); }
                return;
            }
        }
        if(C_MAX_LOSSES>0&&ConsecLoss>=C_MAX_LOSSES) {
            if(DIAG && LOG_LVL>=LOG_DBG && Idx!=LastSkipLogBar) {
                LastSkipLogBar=Idx;
                SCString m; m.Format("[V18A] Skip — consec losses %d >= MAX %d", ConsecLoss, C_MAX_LOSSES);
                sc.AddMessageToLog(m, 0);
            }
            return;
        }
    }

    if(NEWS_FILTER&&pNews){
        if(pNews->isNewsWindow(BarHHMM)){
            if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] news window BarHHMM=%d", BarHHMM); sc.AddMessageToLog(sm,0); }
            return;
        }
        if(pNews->spikeActive&&C_SPIKE_COOL>0&&Idx<=pNews->spikeBar+C_SPIKE_COOL){
            if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] spike active spikeBar=%d cool=%d Idx=%d", pNews->spikeBar, C_SPIKE_COOL, Idx); sc.AddMessageToLog(sm,0); }
            return;
        }
        if(pVCool&&pVCool->barsRemaining>0){
            if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] vcool barsRemaining=%d", pVCool->barsRemaining); sc.AddMessageToLog(sm,0); }
            return;
        }
    }

    if(iof_session::AtOrAfterFlatten(BarHHMM,FLAT_TIME)){
        if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] at/after flatten BarHHMM=%d FLAT=%d", BarHHMM, FLAT_TIME); sc.AddMessageToLog(sm,0); }
        return;
    }
    // [v12.36] Late-entry gate. Skip new SETUPs at BarHHMM >= 1500. Reversal
    // modes (M2/M4) need >55 min before flatten to reach T2/trail; trades that
    // fire in 1500-1555 get cut short and become net losers cross-contract.
    if(BarHHMM >= 1500){
        if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] late-entry gate BarHHMM=%d (>=1500)", BarHHMM); sc.AddMessageToLog(sm,0); }
        return;
    }
    if(C_OPEN_COOL>0){
        int oM=(SESS_START/100)*60+(SESS_START%100);   // [v12.23] cooldown from session start
        int bM=(BarHHMM/100)*60+(BarHHMM%100);
        if(bM<oM+C_OPEN_COOL){
            if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] open cooldown bM=%d oM=%d cool=%d", bM, oM, C_OPEN_COOL); sc.AddMessageToLog(sm,0); }
            return;
        }
    }
    const bool vwapOK=(C_VWAP_MATURE<=0)||(VWAPBars>=C_VWAP_MATURE);
    const bool isNewBar=(Idx!=LastCalcBar);
    if(isNewBar) LastCalcBar=Idx;
    const bool deltaMature=(C_DELTA_MATURE<=0)||(VWAPBars>=C_DELTA_MATURE);
    const bool DL=(Delta0>0.f), DS=(Delta0<0.f);
    float lbDelta=0.f;
    if(C_DELTA_LB>0){
        if(pDR) lbDelta = pDR->sumRange(0, C_DELTA_LB-1);
        else for(int k=0;k<C_DELTA_LB&&Idx>=k;k++) lbDelta+=sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k];
    }
    const bool LBL=(lbDelta>0.f), LBS=(lbDelta<0.f);
    if(!DL&&!DS&&!LBL&&!LBS){
        if(LOG_LVL>=LOG_SIG){ SCString sm; sm.Format("[V18A SKIP] no delta direction Delta0=%.2f lbDelta=%.2f", Delta0, lbDelta); sc.AddMessageToLog(sm,0); }
        return;
    }

    float vwapSlope=0.f; bool sOKL=true, sOKS=true;
    if(C_VWAP_SLP_LB>0&&Idx>=C_VWAP_SLP_LB){
        float vp=SG_VWAP[Idx-C_VWAP_SLP_LB];
        if(vp>0.f&&VWAP>0.f){
            vwapSlope=VWAP-vp;
            float tol=ATR*0.02f;
            if(vwapSlope<-tol) sOKL=false;
            if(vwapSlope>tol) sOKS=false;
        }
    }

    // ============================================================================
    //  ICEBERG
    // ============================================================================
    if(isNewBar&&pIce&&Idx>=5){
        pIce->reset();
        float pxMin=Low0,pxMax=High0,totAskV=0.f,totBidV=0.f;
        for(int k=0;k<5&&Idx>=k;k++){
            if(sc.Low[Idx-k]<pxMin)pxMin=sc.Low[Idx-k];
            if(sc.High[Idx-k]>pxMax)pxMax=sc.High[Idx-k];
            totAskV+=sc.AskVolume[Idx-k];
            totBidV+=sc.BidVolume[Idx-k];
        }
        float pxRange5=pxMax-pxMin;
        if(pxRange5<ATR*0.5f&&pxRange5>0.f){
            float totV=totAskV+totBidV;
            if(totV>Vol0*5.f*1.5f){
                float iceR=pAT?pAT->icebergRatio():1.3f;
                if(totBidV>totAskV*iceR){pIce->bidIceberg=true;pIce->bidIcebergPx=(pxMin+pxMax)*0.5f;}
                if(totAskV>totBidV*iceR){pIce->askIceberg=true;pIce->askIcebergPx=(pxMin+pxMax)*0.5f;}
            }
        }
    }

    // ============================================================================
    //  CONTROL SCORE
    // ============================================================================
    int controlScore=0;
    if(isNewBar){
        float cdRecent=0.f, cdOlder=0.f;
        if(pDR){
            cdRecent = pDR->sumRange(0, 7);
            cdOlder  = pDR->sumRange(8, 14);
        } else {
            for(int k=0;k<8&&Idx>=k;k++) cdRecent+=sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k];
            for(int k=8;k<15&&Idx>=k;k++) cdOlder+=sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k];
        }
        if(cdRecent>cdOlder*1.2f) controlScore++;
        if(cdRecent<cdOlder*1.2f&&cdRecent<0.f) controlScore--;

        float pxMid=(High0+Low0)*0.5f, pxMidBack=pxMid;
        float cdTotal=cdRecent+cdOlder;
        if(Idx>=15) pxMidBack=(sc.High[Idx-15]+sc.Low[Idx-15])*0.5f;
        bool pxUp=(pxMid>pxMidBack), cdUp=(cdTotal>0.f);
        if(pxUp&&cdUp) controlScore++;
        if(!pxUp&&!cdUp) controlScore--;

        float tA=0.f, tB=0.f;
        for(int k=0;k<10&&Idx>=k;k++){tA+=sc.AskVolume[Idx-k];tB+=sc.BidVolume[Idx-k];}
        float tV=tA+tB;
        if(tV>0.f){
            float aP=tA/tV;
            float bullTh=pAT?pAT->aggrBullish():0.55f;
            float bearTh=pAT?pAT->aggrBearish():0.45f;
            if(aP>bullTh) controlScore++;
            if(aP<bearTh) controlScore--;
        }

        int absBuy=0, absSell=0;
        for(int k=0;k<5&&Idx>=k;k++){
            float bd = pDR ? pDR->delta[k] : (sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
            bool bUp=(sc.Close[Idx-k]>=sc.Open[Idx-k]);
            if(bd<0.f&&bUp) absBuy++;
            if(bd>0.f&&!bUp) absSell++;
        }
        if(absBuy>=3) controlScore++;
        if(absSell>=3) controlScore--;

        if(pVP&&pVP->valid){
            if(Close0>pVP->vah) controlScore++;
            if(Close0<pVP->val) controlScore--;
        }
        controlScore=IClamp(controlScore,-5,5);
        if(!deltaMature) controlScore=0;
        SG_CTRL[Idx]=(float)controlScore;
    } else {
        controlScore=(int)SG_CTRL[Idx];
    }

    // ============================================================================
    //  CUMDELTA DIVERGENCE
    // ============================================================================
    if(isNewBar&&pDiv){
        pDiv->reset();
        if(deltaMature){
            const int TREND_LB=20, SWING_LB=8;
            if(Idx>=TREND_LB){
                float cumD=0.f, peakPx=-1e9f, troughPx=1e9f;
                for(int k=TREND_LB;k>=0;k--){
                    cumD += pDR ? pDR->delta[k] : (sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                    if(sc.High[Idx-k]>peakPx) peakPx=sc.High[Idx-k];
                    if(sc.Low[Idx-k]<troughPx) troughPx=sc.Low[Idx-k];
                }
                if(High0>=peakPx-ATR*0.3f&&cumD<0.f) pDiv->trendDivBear=true;
                if(Low0<=troughPx+ATR*0.3f&&cumD>0.f) pDiv->trendDivBull=true;
            }
            if(Idx>=SWING_LB){
                float pxStart=sc.Close[Idx-SWING_LB], cdSwing=0.f;
                float swHi=-1e9f, swLo=1e9f;
                for(int k=SWING_LB;k>=0;k--){
                    cdSwing += pDR ? pDR->delta[k] : (sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                    if(sc.High[Idx-k]>swHi) swHi=sc.High[Idx-k];
                    if(sc.Low[Idx-k]<swLo) swLo=sc.Low[Idx-k];
                }
                if(High0>=swHi-ATR*0.3f&&swHi-pxStart>ATR*0.5f&&cdSwing<0.f) pDiv->swingDivBear=true;
                if(Low0<=swLo+ATR*0.3f&&pxStart-swLo>ATR*0.5f&&cdSwing>0.f) pDiv->swingDivBull=true;
            }
            int upDn=0, dnUp=0;
            for(int k=0;k<6&&Idx>=k;k++){
                float bd = pDR ? pDR->delta[k] : (sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                bool pUp2=(sc.Close[Idx-k]>sc.Open[Idx-k]);
                if(pUp2&&bd<0.f) upDn++; else if(k>0) break;
            }
            for(int k=0;k<6&&Idx>=k;k++){
                float bd = pDR ? pDR->delta[k] : (sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                bool pDn2=(sc.Close[Idx-k]<sc.Open[Idx-k]);
                if(pDn2&&bd>0.f) dnUp++; else if(k>0) break;
            }
            pDiv->persistUpPxDnDelta=upDn;
            pDiv->persistDnPxUpDelta=dnUp;
            pDiv->persistAbsBuy=(dnUp>=3);
            pDiv->persistAbsSell=(upDn>=3);

            int ds=0;
            if(pDiv->trendDivBull) ds+=2;
            if(pDiv->trendDivBear) ds-=2;
            if(pDiv->swingDivBull) ds+=2;
            if(pDiv->swingDivBear) ds-=2;
            if(pDiv->persistAbsBuy) ds+=1;
            if(pDiv->persistAbsSell) ds-=1;
            pDiv->strength=IClamp(ds,-5,5);
        }
    }

    // ============================================================================
    //  BALANCE DETECTION
    // ============================================================================
    if(isNewBar&&pBal&&Idx>=30&&ATR>0.f){
        pBal->reset();
        const int BLB=30;
        float rH=High0, rL=Low0;
        float totalVol=0.f, netDelta=0.f;
        int dFlips=0; float prevD=0.f;
        const int NBINS=20;
        float bins[NBINS]; for(int b=0;b<NBINS;b++) bins[b]=0.f;
        for(int k=0;k<BLB&&Idx>=k;k++){
            if(sc.High[Idx-k]>rH) rH=sc.High[Idx-k];
            if(sc.Low[Idx-k]<rL) rL=sc.Low[Idx-k];
        }
        float rW=rH-rL;
        if(rW>0.f&&rW<=ATR*1.5f){
            for(int k=0;k<BLB&&Idx>=k;k++){
                float bd = pDR ? pDR->delta[k] : (sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                float vol=sc.Volume[Idx-k];
                netDelta+=bd; totalVol+=vol;
                if(k>0&&((bd>0.f&&prevD<0.f)||(bd<0.f&&prevD>0.f))) dFlips++;
                prevD=bd;
                float mid=(sc.High[Idx-k]+sc.Low[Idx-k])*0.5f;
                int bin=(int)(((mid-rL)/rW)*(float)(NBINS-1));
                bin=IClamp(bin,0,NBINS-1);
                bins[bin]+=vol;
            }
            int pocBin=0; float maxBV=0.f;
            for(int b=0;b<NBINS;b++) if(bins[b]>maxBV){maxBV=bins[b];pocBin=b;}
            float centerVol=0.f;
            for(int b=NBINS*3/10;b<=NBINS*7/10;b++) centerVol+=bins[b];
            float conc=(totalVol>0.f)?(centerVol/totalVol):0.f;
            if(dFlips>=4&&conc>=0.45f&&FAbs(netDelta)<totalVol*0.15f){
                pBal->active=true; pBal->mature=true;
                pBal->rangeHigh=rH; pBal->rangeLow=rL;
                pBal->rangePOC=rL+rW*((float)pocBin/(float)(NBINS-1));
                pBal->barCount=BLB; pBal->deltaFlips=dFlips;
                pBal->cumDelta=netDelta; pBal->vpConcentration=conc;
                pBal->volumeTotal=totalVol;
            }
        }

        if(pRegime){
            float priceChange=Close0-sc.Close[Idx-BLB<0?0:Idx-BLB];
            pRegime->update(netDelta, priceChange, ATR, rW, BLB);
        }
    }

    // ============================================================================
    //  IMBALANCE DETECTION
    // ============================================================================
    if(isNewBar&&pImb&&Idx>=10&&ATR>0.f){
        int imbStr=0, imbDir=0;
        float d0 = pDR ? pDR->delta[0] : Delta0;
        float d1 = pDR ? pDR->delta[1] : ((Idx>=1)?sc.AskVolume[Idx-1]-sc.BidVolume[Idx-1]:0.f);
        float d2 = pDR ? pDR->delta[2] : ((Idx>=2)?sc.AskVolume[Idx-2]-sc.BidVolume[Idx-2]:0.f);
        if(d0>d1&&d1>d2&&d0>0.f){imbStr++;imbDir=+1;}
        if(d0<d1&&d1<d2&&d0<0.f){imbStr++;imbDir=-1;}
        float aV=0.f, bV=0.f;
        for(int k=0;k<5&&Idx>=k;k++){aV+=sc.AskVolume[Idx-k];bV+=sc.BidVolume[Idx-k];}
        float tv=aV+bV;
        float aggR=(tv>0.f)?((imbDir>=0)?(aV/tv):(bV/tv)):0.5f;
        if(aggR>=0.62f) imbStr++;
        if(aggR>=0.72f) imbStr++;
        int accept=0;
        for(int k=0;k<8&&Idx>=k+1;k++){
            if(imbDir>0&&sc.Close[Idx-k]>sc.High[Idx-k-1]) accept++;
            else if(imbDir<0&&sc.Close[Idx-k]<sc.Low[Idx-k-1]) accept++;
            else break;
        }
        if(accept>=2) imbStr++;
        if(accept>=4) imbStr++;
        if(imbStr>=3&&imbDir!=0){
            pImb->active=true; pImb->direction=imbDir; pImb->strength=imbStr;
            pImb->aggressionRatio=aggR; pImb->consecutiveAccept=accept;
            pImb->barCount++;
            if(pImb->startBar<0) pImb->startBar=Idx;
        } else {
            if(pImb->active&&pImb->startBar>=0&&Idx<=pImb->startBar+pImb->barCount+3)
                pImb->strength=(pImb->strength>0)?pImb->strength-1:0;
            else pImb->reset();
        }
    }

    // ============================================================================
    //  TRAP STATE MACHINE
    // ============================================================================
    if(isNewBar&&pTrap&&ATR>0.f){
        float barD=Delta0;
        float tbMax=pAT?pAT->trapBodyMax():0.35f;
        if(pTrap->phase==0){
            if(barBullish&&barD>0.f){
                if(pTrap->direction==+1&&pTrap->commitBars>0){
                    pTrap->commitBars++; pTrap->commitDelta+=barD;
                    if(High0>pTrap->commitHigh) pTrap->commitHigh=High0;
                } else {
                    pTrap->direction=+1; pTrap->commitStartBar=Idx;
                    pTrap->commitBars=1; pTrap->commitDelta=barD;
                    pTrap->commitHigh=High0; pTrap->stopTarget=Low0;
                    for(int k=1;k<=10&&Idx>=k;k++) if(sc.Low[Idx-k]<pTrap->stopTarget) pTrap->stopTarget=sc.Low[Idx-k];
                }
                if(pTrap->commitBars>=2) pTrap->phase=1;
            } else if(barBearish&&barD<0.f){
                if(pTrap->direction==-1&&pTrap->commitBars>0){
                    pTrap->commitBars++; pTrap->commitDelta+=barD;
                    if(Low0<pTrap->commitLow) pTrap->commitLow=Low0;
                } else {
                    pTrap->direction=-1; pTrap->commitStartBar=Idx;
                    pTrap->commitBars=1; pTrap->commitDelta=barD;
                    pTrap->commitLow=Low0; pTrap->stopTarget=High0;
                    for(int k=1;k<=10&&Idx>=k;k++) if(sc.High[Idx-k]>pTrap->stopTarget) pTrap->stopTarget=sc.High[Idx-k];
                }
                if(pTrap->commitBars>=2) pTrap->phase=1;
            } else {
                if(pTrap->commitBars>0&&pTrap->commitBars<2) pTrap->reset();
            }
        }
        if(pTrap->phase==1){
            if(Idx>pTrap->commitStartBar+pTrap->commitBars+5){pTrap->reset();}
            else if(pTrap->direction==+1){
                bool stalled=(High0<=pTrap->commitHigh+TICK);
                bool smallBody=(barRange>0.f&&barBody<barRange*tbMax);
                bool diverg=(barD>0.f&&barBearish);
                bool flip=(barD<0.f);
                if(stalled&&(smallBody||diverg||flip)){
                    pTrap->phase=2; pTrap->absorbBar=Idx; pTrap->absorbPx=Close0;
                } else if(barBullish&&barD>0.f){
                    pTrap->commitBars++; pTrap->commitDelta+=barD;
                    if(High0>pTrap->commitHigh) pTrap->commitHigh=High0;
                }
            } else if(pTrap->direction==-1){
                bool stalled=(Low0>=pTrap->commitLow-TICK);
                bool smallBody=(barRange>0.f&&barBody<barRange*tbMax);
                bool diverg=(barD<0.f&&barBullish);
                bool flip=(barD>0.f);
                if(stalled&&(smallBody||diverg||flip)){
                    pTrap->phase=2; pTrap->absorbBar=Idx; pTrap->absorbPx=Close0;
                } else if(barBearish&&barD<0.f){
                    pTrap->commitBars++; pTrap->commitDelta+=barD;
                    if(Low0<pTrap->commitLow) pTrap->commitLow=Low0;
                }
            }
        }
        if(pTrap->phase==2){
            // Transition to phase 3 when price reverses through absorbPx
            if(pTrap->direction==+1 && Close0<pTrap->absorbPx){
                pTrap->phase=3; pTrap->phase3Bar=Idx;
            } else if(pTrap->direction==-1 && Close0>pTrap->absorbPx){
                pTrap->phase=3; pTrap->phase3Bar=Idx;
            } else if(Idx>pTrap->absorbBar+3){
                pTrap->reset();
            }
        }
        if(pTrap->phase==3){
            // [v12.20a] Reclaim reset: price returned to absorption side → trap invalid
            bool reclaimL=(pTrap->direction==+1 && Close0>pTrap->absorbPx);
            bool reclaimS=(pTrap->direction==-1 && Close0<pTrap->absorbPx);
            if(reclaimL||reclaimS){ pTrap->reset(); }
            // [v12.20b] Phase-3 timeout
            else if(pTrap->phase3Bar>=0 && Idx>pTrap->phase3Bar+C_M5_PHASE3_MAX){ pTrap->reset(); }
        }
    }

    // ============================================================================
    //  SCORING
    // ============================================================================
    const bool S2=(FAbs(Delta0)>=avgD*0.5f);
    bool S3L=false,S3S=false;
    if(pVP&&pVP->valid&&ATR>0.f){
        float z=ATR*0.75f;
        bool nPOC=FAbs(Close0-pVP->poc)<=z;
        bool nVAL=FAbs(Close0-pVP->val)<=z;
        bool nVAH=FAbs(Close0-pVP->vah)<=z;
        S3L=nVAL||(nPOC&&Close0>pVP->poc);
        S3S=nVAH||(nPOC&&Close0<pVP->poc);
    }
    const bool S1L=(VWAP>0.f&&Close0>VWAP), S1S=(VWAP>0.f&&Close0<VWAP);
    float vb2u=SG_VB2U[Idx],vb2l=SG_VB2L[Idx];
    bool UOBRej=(vb2u>0.f)&&(High0>=vb2u)&&(Close0<vb2u)&&(Close0<Close1);
    bool LOBBnc=(vb2l>0.f)&&(Low0<=vb2l)&&(Close0>vb2l)&&(Close0>Close1);
    int S4ptL=LOBBnc?2:((VWAP>0.f&&Close0<VWAP)?1:0);
    int S4ptS=UOBRej?2:((VWAP>0.f&&Close0>VWAP)?1:0);
    float sweepMin=ATR*0.15f;
    bool S5L=(Low0<Low1-sweepMin)&&(Close0>Low1)&&(Close0>Close1);
    bool S5S=(High0>High1+sweepMin)&&(Close0<High1)&&(Close0<Close1);
    bool S6L=false,S6S=false;
    if(Idx>0){
        float d1a=sc.AskVolume[Idx-1]-sc.BidVolume[Idx-1];
        S6L=(Close1<=Open1)&&(d1a>0.f);
        S6S=(Close1>=Open1)&&(d1a<0.f);
    }
    bool S7=(pVP&&pVP->nearSinglePrint(Close0,ATR*0.5f));
    bool S8L=(pIce&&pIce->bidIceberg&&FAbs(Close0-pIce->bidIcebergPx)<=ATR*0.5f);
    bool S8S=(pIce&&pIce->askIceberg&&FAbs(Close0-pIce->askIcebergPx)<=ATR*0.5f);
    bool S9L=false,S9S=false;
    if(pID&&pID->valid&&ATR>0.f){
        float idZ=ATR*0.5f;
        float idLevels[5]={pID->prevOpen,pID->prevHigh,pID->prevLow,pID->prevClose,pID->currOpen};
        for(int i=0;i<5;i++){
            if(idLevels[i]<=0.f) continue;
            if(FAbs(Close0-idLevels[i])>idZ) continue;
            if(Close0>idLevels[i]&&barBullish) S9L=true;
            if(Close0<idLevels[i]&&barBearish) S9S=true;
        }
    }
    bool S10L=false,S10S=false;
    if(pVP&&pVP->valid50&&ATR>0.f){
        float z50=ATR*0.75f;
        if(FAbs(Close0-pVP->poc50)<=z50){
            if(Close0>pVP->poc50) S10L=true;
            if(Close0<pVP->poc50) S10S=true;
        }
        if(FAbs(Close0-pVP->val50)<=z50&&Close0>pVP->val50) S10L=true;
        if(FAbs(Close0-pVP->vah50)<=z50&&Close0<pVP->vah50) S10S=true;
    }
    bool trapConf=(pTrap&&pTrap->phase>=2);
    int S11L=(trapConf&&pTrap->direction==-1)?2:0;
    int S11S=(trapConf&&pTrap->direction==+1)?2:0;
    int S12L=0,S12S=0;
    if(deltaMature&&pDiv){
        if(pDiv->strength>=2) S12L=1;
        if(pDiv->strength>=4) S12L=2;
        if(pDiv->strength<=-2) S12S=1;
        if(pDiv->strength<=-4) S12S=2;
    }
    int bonus=(int)S2;
    int bL=S4ptL+(int)S1L+(int)S3L+(int)S5L+(int)S6L+(int)S7+(int)S8L+(int)S9L+(int)S10L+S11L+S12L;
    int bS=S4ptS+(int)S1S+(int)S3S+(int)S5S+(int)S6S+(int)S7+(int)S8S+(int)S9S+(int)S10S+S11S+S12S;
    int scL=DL?(bL+bonus):0, scS=DS?(bS+bonus):0;
    int lbScL=LBL?(bL+bonus):0, lbScS=LBS?(bS+bonus):0;

    // ============================================================================
    //  M1 — VWAP LEVEL TEST
    // ============================================================================
    float vwZ=ATR*0.75f;
    bool barTouchVWAP=(VWAP>0.f)&&(Low0<=VWAP+vwZ)&&(High0>=VWAP-vwZ);
    bool vwBnc=vwapOK&&barTouchVWAP&&sOKL&&(Close0>VWAP)&&(Close1>VWAP)&&barBullish;
    bool vwRej=vwapOK&&barTouchVWAP&&sOKS&&(Close0<VWAP)&&(Close1<VWAP)&&barBearish;
    bool m1L=(scL>=C_MIN_SCORE_M1)&&vwBnc&&sOKL&&(controlScore>=0);
    bool m1S=(scS>=C_MIN_SCORE_M1)&&vwRej&&sOKS&&!m1L&&(controlScore<=0);
    // [v12.5] was hard-coded m1L=false;m1S=false. Now toggleable via input slot 15.
    // Default OFF matches manual (29% WR in backtest). Enable to A/B test.
    if(!M1_ENABLE){ m1L = false; m1S = false; }

    // [v12.33] mode=1: wick-reject pullback gate. Tested 2026-05-22, both
    // contracts AGREE fixed_worse — kept here for reference, not default.
    if(M1_PULLBACK == 1){
        if(m1L){
            bool pierced = (Low0 <= VWAP - TICK);
            bool wickGE  = ((Open0 - Low0) >= (Close0 - Open0));
            if(!(pierced && wickGE)) m1L = false;
        }
        if(m1S){
            bool pierced = (High0 >= VWAP + TICK);
            bool wickGE  = ((High0 - Open0) >= (Open0 - Close0));
            if(!(pierced && wickGE)) m1S = false;
        }
    }

    // [v12.34] mode=2: prior-bar dip + reclaim. REPLACES legacy two-bar
    // continuation trigger. Prior bar must have touched VWAP and closed at/
    // near VWAP; current bar reclaims with bullish/bearish body. Cross-
    // contract A/B via backtest_v12_34_ab.py before flipping live.
    if(M1_ENABLE && M1_PULLBACK == 2 && vwapOK && Idx >= 1 && ATR > 0.f){
        const float Low1  = sc.Low[Idx-1];
        const float High1 = sc.High[Idx-1];
        const float tol = ATR * 0.1f;
        bool prevDipL = (Low1  <= VWAP + vwZ) && (Close1 <= VWAP + tol);
        bool prevDipS = (High1 >= VWAP - vwZ) && (Close1 >= VWAP - tol);
        bool reclL   = (Close0 > VWAP) && barBullish && sOKL;
        bool rejS    = (Close0 < VWAP) && barBearish && sOKS;
        bool dipL = prevDipL && reclL && (scL>=C_MIN_SCORE_M1) && (controlScore>=0);
        bool dipS = prevDipS && rejS  && (scS>=C_MIN_SCORE_M1) && (controlScore<=0) && !dipL;
        m1L = dipL;
        m1S = dipS;
    }

    // ============================================================================
    //  M2 — VP LEVEL TEST
    // ============================================================================
    bool m2L=false, m2S=false;
    bool macroTrendOK_L=true, macroTrendOK_S=true;
    if(pVP&&pVP->valid&&ATR>0.f){
        float lowestRef=pVP->val;
        if(pVP->valid50&&pVP->val50>0.f) lowestRef=FMin(lowestRef,pVP->val50);
        lowestRef=FMin(lowestRef,pVP->poc);
        float highestRef=pVP->vah;
        if(pVP->valid50&&pVP->vah50>0.f) highestRef=FMax(highestRef,pVP->vah50);
        highestRef=FMax(highestRef,pVP->poc);
        if(Close0<lowestRef-ATR*2.0f) macroTrendOK_L=false;
        if(Close0>highestRef+ATR*2.0f) macroTrendOK_S=false;
    }
    if(ATR>0.f&&vwapOK){
        float vpZ=ATR*0.75f;
        const int MXL=16;
        float levels[MXL]; int nLev=0;
        if(pVP&&pVP->valid){levels[nLev++]=pVP->poc;levels[nLev++]=pVP->vah;levels[nLev++]=pVP->val;}
        if(pVP&&pVP->valid50&&nLev<MXL-3){levels[nLev++]=pVP->poc50;levels[nLev++]=pVP->vah50;levels[nLev++]=pVP->val50;}
        if(pID&&pID->valid&&nLev<MXL-5){
            if(pID->prevHigh>0.f) levels[nLev++]=pID->prevHigh;
            if(pID->prevLow>0.f) levels[nLev++]=pID->prevLow;
            if(pID->prevClose>0.f) levels[nLev++]=pID->prevClose;
            if(pID->currOpen>0.f) levels[nLev++]=pID->currOpen;
            if(pID->prevOpen>0.f) levels[nLev++]=pID->prevOpen;
        }
        if(pIce&&pIce->bidIceberg&&nLev<MXL) levels[nLev++]=pIce->bidIcebergPx;
        if(pIce&&pIce->askIceberg&&nLev<MXL) levels[nLev++]=pIce->askIcebergPx;
        for(int i=0;i<nLev;i++){
            float lv=levels[i]; if(lv<=0.f) continue;
            if(FAbs(Close0-lv)>vpZ) continue;
            if(Low0<=lv+TICK&&Close0>lv&&barBullish&&(DL||LBL)&&controlScore>=0&&macroTrendOK_L){
                int es=DL?scL:lbScL; if(es>=C_MIN_SCORE_ALL) m2L=true;
            }
            if(High0>=lv-TICK&&Close0<lv&&barBearish&&(DS||LBS)&&controlScore<=0&&macroTrendOK_S){
                int es=DS?scS:lbScS; if(es>=C_MIN_SCORE_ALL) m2S=true;
            }
        }
    }

    // ============================================================================
    //  M3 — CONSOLIDATION BREAKOUT / REJECTION
    // ============================================================================
    bool m3L=false, m3S=false;
    if(ATR>0.f&&Idx>=C_CONSOL_LB&&vwapOK){
        float rH=High0, rL=Low0;
        for(int k=1;k<C_CONSOL_LB&&Idx>=k;k++){
            if(sc.High[Idx-k]>rH) rH=sc.High[Idx-k];
            if(sc.Low[Idx-k]<rL) rL=sc.Low[Idx-k];
        }
        float rW=rH-rL;
        if(rW>0.f&&rW<=ATR*C_CONSOL_ATR){
            float boT=ATR*0.25f, rM=(rH+rL)*0.5f;
            bool uRej=(High0>=rH-TICK)&&(Close0<rH)&&barBearish&&(Close0<rM+rW*0.25f)&&(DS||LBS);
            bool lBnc=(Low0<=rL+TICK)&&(Close0>rL)&&barBullish&&(Close0>rM-rW*0.25f)&&(DL||LBL);
            bool bkDn=(Close0<rL-boT)&&barBearish&&(DS||LBS);
            bool bkUp=(Close0>rH+boT)&&barBullish&&(DL||LBL);
            int cL=DL?scL:lbScL, cS=DS?scS:lbScS;
            if((lBnc||bkUp)&&cL>=C_MIN_SCORE_ALL) m3L=true;
            if((uRej||bkDn)&&cS>=C_MIN_SCORE_ALL) m3S=true;
        }
    }
    // [v12.25] M3 strict CtrlScore gate. Consolidation breakouts/rejections
    //          require neutral/correct-side flow (mirrors M1/M2/M4/M5).
    if(m3L && controlScore < 0) m3L=false;
    if(m3S && controlScore > 0) m3S=false;

    // ============================================================================
    //  M4 — SWEEP + RECLAIM
    // ============================================================================
    bool m4L=false, m4S=false;
    if(C_SWEEP_LB>0&&Idx>=C_SWEEP_LB&&ATR>0.f){
        bool m4CtrlL=(controlScore>=0), m4CtrlS=(controlScore<=0);
        int divStr=pDiv?(pDiv->strength<0?-pDiv->strength:pDiv->strength):0;
        int M4_MIN_SCORE=(divStr>=2)?C_MIN_SCORE_ALL:(C_MIN_SCORE_ALL+1);
        // [EdgeDiscovery] Require price to be at least 0.35 ATR from VWAP — avoids
        // taking sweep-reclaims when price is grinding through the VWAP mid-zone.
        bool m4VwapEdge=(VWAP<=0.f||FAbs(Close0-VWAP)>=ATR*0.35f);
        float swLo=sc.Low[Idx-1], swHi=sc.High[Idx-1];
        for(int k=2;k<=C_SWEEP_LB&&Idx>=k;k++){
            if(sc.Low[Idx-k]<swLo) swLo=sc.Low[Idx-k];
            if(sc.High[Idx-k]>swHi) swHi=sc.High[Idx-k];
        }
        if(m4VwapEdge&&Low0<swLo-TICK&&Close0>swLo&&barBullish&&(DL||LBL)&&m4CtrlL){
            int es=DL?scL:lbScL;
            bool iceBlock=(pIce&&pIce->askIceberg&&FAbs(pIce->askIcebergPx-swLo)<=ATR*0.5f);
            if(es>=M4_MIN_SCORE&&!iceBlock) m4L=true;
        }
        if(m4VwapEdge&&High0>swHi+TICK&&Close0<swHi&&barBearish&&(DS||LBS)&&m4CtrlS){
            int es=DS?scS:lbScS;
            bool iceBlock=(pIce&&pIce->bidIceberg&&FAbs(pIce->bidIcebergPx-swHi)<=ATR*0.5f);
            if(es>=M4_MIN_SCORE&&!iceBlock) m4S=true;
        }
    }

    // ============================================================================
    //  M5 — TRAP REVERSAL  [v12.14/v12.20/v12.22 restored]
    // ============================================================================
    bool m5L=false, m5S=false;
    if(pTrap&&pTrap->phase==3&&ATR>0.f&&BarHHMM>=RTH_OPEN){
        bool m5Cool=(LastM5Bar<0||Idx>LastM5Bar+C_M5_COOLDOWN);
        if(m5Cool){
            // Bullish trap (committed up move absorbed) → fade = short
            if(pTrap->direction==+1&&controlScore<=0){
                int es=DS?scS:0;  // [v12.20c] no LBS fallback
                if(es>=C_M5_MIN_SC) m5S=true;
            }
            // Bearish trap (committed down move absorbed) → fade = long
            if(pTrap->direction==-1&&controlScore>=0){
                int es=DL?scL:0;  // [v12.20c] no LBL fallback
                if(es>=C_M5_MIN_SC) m5L=true;
            }
            if(m5L||m5S) LastM5Bar=Idx;  // [v12.14] arm immediately regardless of entry outcome
        }
    }

    // ============================================================================
    //  M6 — BALANCE BREAKOUT (V1-V10)
    // ============================================================================
    bool m6L=false, m6S=false;
    float m6Stop=0.f, m6T1=0.f, m6T2=0.f;
    int bkVerifyScore=10;
    if(pBal&&pBal->mature&&pImb&&ATR>0.f){
        float bkT=ATR*0.30f;
        int breakDir=0;
        if(Close0>pBal->rangeHigh+bkT&&pImb->active&&pImb->direction==+1&&barBullish&&DL) breakDir=+1;
        if(Close0<pBal->rangeLow-bkT&&pImb->active&&pImb->direction==-1&&barBearish&&DS) breakDir=-1;
        if(breakDir!=0){
            bkVerifyScore=0;
            float balAvg=(pBal->volumeTotal>0.f&&pBal->barCount>0)?(pBal->volumeTotal/(float)pBal->barCount):Vol0;
            float bkLev=(breakDir>0)?pBal->rangeHigh:pBal->rangeLow;
            int dStreak=0;
            for(int k=0;k<5&&Idx>=k;k++){
                float bd = pDR ? pDR->delta[k] : (sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                if((breakDir>0&&bd>0.f)||(breakDir<0&&bd<0.f)) dStreak++; else break;
            }
            if(dStreak>=3) bkVerifyScore++;
            float bkVol=0.f;
            for(int k=0;k<3&&Idx>=k;k++) bkVol+=sc.Volume[Idx-k];
            if(bkVol/3.f>balAvg*1.3f) bkVerifyScore++;
            int accBars=0;
            for(int k=0;k<3&&Idx>=k;k++){
                if(breakDir>0&&sc.Close[Idx-k]>bkLev) accBars++;
                if(breakDir<0&&sc.Close[Idx-k]<bkLev) accBars++;
            }
            if(accBars>=2) bkVerifyScore++;
            bool retest=false;
            for(int k=0;k<2&&Idx>=k;k++){
                if(breakDir>0&&sc.Low[Idx-k]<bkLev) retest=true;
                if(breakDir<0&&sc.High[Idx-k]>bkLev) retest=true;
            }
            if(!retest) bkVerifyScore++;
            float oN=0.f, oP=0.f;
            for(int k=0;k<3&&Idx>=k;k++){if(breakDir>0) oN+=sc.BidVolume[Idx-k]; else oN+=sc.AskVolume[Idx-k];}
            for(int k=3;k<6&&Idx>=k;k++){if(breakDir>0) oP+=sc.BidVolume[Idx-k]; else oP+=sc.AskVolume[Idx-k];}
            if(oP>0.f&&oN<oP*0.65f) bkVerifyScore++;
            float vBeyond=0.f, vAt=0.f;
            for(int k=0;k<3&&Idx>=k;k++){
                float mid=(sc.High[Idx-k]+sc.Low[Idx-k])*0.5f;
                if((breakDir>0&&mid>bkLev)||(breakDir<0&&mid<bkLev)) vBeyond+=sc.Volume[Idx-k];
                else vAt+=sc.Volume[Idx-k];
            }
            if(vBeyond>vAt*1.5f) bkVerifyScore++;
            bool absAtBk=false;
            for(int k=0;k<3&&Idx>=k;k++){
                float bd = pDR ? pDR->delta[k] : (sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                bool bUp2=(sc.Close[Idx-k]>sc.Open[Idx-k]);
                if(breakDir>0&&bd<0.f&&bUp2&&sc.Volume[Idx-k]>balAvg*1.2f) absAtBk=true;
                if(breakDir<0&&bd>0.f&&!bUp2&&sc.Volume[Idx-k]>balAvg*1.2f) absAtBk=true;
            }
            if(!absAtBk) bkVerifyScore++;
            bool oppIce=false;
            if(breakDir>0&&pIce&&pIce->askIceberg&&FAbs(pIce->askIcebergPx-bkLev)<=ATR) oppIce=true;
            if(breakDir<0&&pIce&&pIce->bidIceberg&&FAbs(pIce->bidIcebergPx-bkLev)<=ATR) oppIce=true;
            if(!oppIce) bkVerifyScore++;
            bool noDivConf=true;
            if(breakDir>0&&pDiv&&pDiv->strength<=-2) noDivConf=false;
            if(breakDir<0&&pDiv&&pDiv->strength>=2) noDivConf=false;
            if(noDivConf) bkVerifyScore++;
            float swRet=(breakDir>0)?(High0-Close0):(Close0-Low0);
            if(swRet<ATR*0.3f) bkVerifyScore++;
            if(bkVerifyScore>=6){
                float rangeW=pBal->rangeHigh-pBal->rangeLow;
                if(breakDir>0){m6L=true; m6Stop=pBal->rangeHigh-ATR*0.25f; m6T1=pBal->rangeHigh+rangeW*0.5f; m6T2=pBal->rangeHigh+rangeW;}
                else{m6S=true; m6Stop=pBal->rangeLow+ATR*0.25f; m6T1=pBal->rangeLow-rangeW*0.5f; m6T2=pBal->rangeLow-rangeW;}
            }
        }
    }
    // [v12.25] M6 soft CtrlScore gate. Block only on clearly opposing control.
    if(m6L && controlScore < -1){ m6L=false; m6Stop=0.f; m6T1=0.f; m6T2=0.f; }
    if(m6S && controlScore >  1){ m6S=false; m6Stop=0.f; m6T1=0.f; m6T2=0.f; }

    // ============================================================================
    //  [v12.35] PER-BAR MODE-REJECTION DIAGNOSTIC (LOG_NOFIRE, sc.Input[26])
    // ============================================================================
    // Verbose per-bar log showing why each of M2/M4/M6 failed to arm on the
    // current bar. Off by default. Use case: chart visually shows a setup,
    // CSV has no SETUP row — toggle this on, reload, and the Message Log
    // surfaces which gate killed each mode. Fires at most one line per bar,
    // gated on "shape possibility" so noise stays low.
    if(LOG_NOFIRE && !(m1L||m1S||m2L||m2S||m3L||m3S||m4L||m4S||m6L||m6S)){
        // M4 near-miss: did a sweep actually happen this bar?
        float swHi_dbg=0.f, swLo_dbg=0.f;
        bool m4Possible=false;
        if(C_SWEEP_LB>0 && Idx>=C_SWEEP_LB){
            swHi_dbg=sc.High[Idx-1]; swLo_dbg=sc.Low[Idx-1];
            for(int k=2;k<=C_SWEEP_LB&&Idx>=k;k++){
                if(sc.Low[Idx-k]<swLo_dbg)  swLo_dbg=sc.Low[Idx-k];
                if(sc.High[Idx-k]>swHi_dbg) swHi_dbg=sc.High[Idx-k];
            }
            m4Possible=(High0>swHi_dbg+TICK)||(Low0<swLo_dbg-TICK);
        }
        // M2 near-miss: at least one VP/ID level within ATR*0.75 of Close
        bool m2Possible=false;
        if(pVP&&pVP->valid&&ATR>0.f){
            float vpZ=ATR*0.75f;
            float lvs[8]; int nL=0;
            lvs[nL++]=pVP->poc; lvs[nL++]=pVP->vah; lvs[nL++]=pVP->val;
            if(pID&&pID->valid){
                if(pID->prevHigh>0.f&&nL<8) lvs[nL++]=pID->prevHigh;
                if(pID->prevLow >0.f&&nL<8) lvs[nL++]=pID->prevLow;
                if(pID->prevClose>0.f&&nL<8) lvs[nL++]=pID->prevClose;
            }
            for(int i=0;i<nL;i++) if(lvs[i]>0.f&&FAbs(Close0-lvs[i])<=vpZ){m2Possible=true;break;}
        }
        // M6 near-miss: close beyond range edge by at least half the breakout threshold
        bool m6Possible=false;
        if(pBal&&ATR>0.f){
            float bkT=ATR*0.30f;
            m6Possible=(Close0>pBal->rangeHigh+bkT*0.5f)||(Close0<pBal->rangeLow-bkT*0.5f);
        }
        if(m2Possible||m4Possible||m6Possible){
            SCString m;
            m.Format(
                "[V18A NOFIRE] Idx=%d Cls=%.2f VWAP=%.2f ATR=%.2f "
                "bear=%d bull=%d DL=%d DS=%d LBL=%d LBS=%d ctrl=%d scL=%d scS=%d vwapOK=%d | "
                "M2(poss=%d nLev~ok) | "
                "M4(poss=%d swHi=%.2f swLo=%.2f) | "
                "M6(poss=%d balMat=%d imbAct=%d imbDir=%d bkVerify=%d)",
                Idx, Close0, VWAP, ATR,
                (int)barBearish, (int)barBullish,
                (int)DL, (int)DS, (int)LBL, (int)LBS,
                controlScore, scL, scS, (int)vwapOK,
                (int)m2Possible,
                (int)m4Possible, swHi_dbg, swLo_dbg,
                (int)m6Possible,
                (int)(pBal && pBal->mature),
                (int)(pImb && pImb->active),
                pImb ? pImb->direction : 0,
                bkVerifyScore);
            sc.AddMessageToLog(m, 0);
        }
    }

    // ============================================================================
    //  M7 — AUCTION REVERSAL (R1-R8)
    // ============================================================================
    bool m7L=false, m7S=false;
    float m7Stop=0.f;
    if(pImb&&pImb->active){
        PrevImbDir=pImb->direction;
        PrevImbStr=pImb->strength;
        PrevImbExtreme=(pImb->direction>0)?High0:Low0;
    }
    int rvVerifyScore=8;
    if(PrevImbDir!=0&&ATR>0.f){
        bool imbFading=(pImb&&pImb->active&&pImb->strength<PrevImbStr-1);
        bool imbDead=(!pImb||!pImb->active)&&PrevImbStr>=3;
        if(imbFading||imbDead){
            rvVerifyScore=0;
            int revDir=(PrevImbDir>0)?-1:+1;
            bool exh=false;
            if(Idx>=3){
                float dm0=FAbs(pDR?pDR->delta[0]:Delta0);
                float dm1=FAbs(pDR?pDR->delta[1]:(sc.AskVolume[Idx-1]-sc.BidVolume[Idx-1]));
                float dm2=FAbs(pDR?pDR->delta[2]:(sc.AskVolume[Idx-2]-sc.BidVolume[Idx-2]));
                exh=(dm0<dm1&&dm1<dm2);
            }
            if(exh) rvVerifyScore++;
            bool atLev=false;
            if(PrevImbDir>0&&vb2u>0.f&&PrevImbExtreme>=vb2u-ATR*0.3f) atLev=true;
            if(PrevImbDir<0&&vb2l>0.f&&PrevImbExtreme<=vb2l+ATR*0.3f) atLev=true;
            if(pVP&&pVP->valid){
                float vpL[3]={pVP->poc,pVP->vah,pVP->val};
                for(int i=0;i<3;i++) if(vpL[i]>0.f&&FAbs(PrevImbExtreme-vpL[i])<=ATR*0.5f) atLev=true;
            }
            if(atLev) rvVerifyScore++;
            bool aggFlip=false;
            if(revDir>0&&Delta0>0.f&&FAbs(Delta0)>avgD*1.5f) aggFlip=true;
            if(revDir<0&&Delta0<0.f&&FAbs(Delta0)>avgD*1.5f) aggFlip=true;
            if(aggFlip) rvVerifyScore++;
            float bPct=(barRange>0.f)?(barBody/barRange):0.f;
            bool corrDir=(revDir>0)?barBullish:barBearish;
            if(bPct>=0.62f&&corrDir) rvVerifyScore++;
            if(pTrap&&pTrap->phase>=2) rvVerifyScore++;
            if(revDir>0&&pDiv&&pDiv->strength>=2) rvVerifyScore++;
            if(revDir<0&&pDiv&&pDiv->strength<=-2) rvVerifyScore++;
            float balAvg2=(pBal&&pBal->volumeTotal>0.f&&pBal->barCount>0)?(pBal->volumeTotal/(float)pBal->barCount):Vol0;
            if(Vol0>balAvg2*1.8f) rvVerifyScore++;
            bool ctrIce=false;
            if(revDir>0&&pIce&&pIce->askIceberg) ctrIce=true;
            if(revDir<0&&pIce&&pIce->bidIceberg) ctrIce=true;
            if(!ctrIce) rvVerifyScore++;
            // [v12.25] M7 firing removed. rvVerifyScore is still consumed by
            //          M8 type 2 (range fade) at lines below — that block
            //          requires PrevImb* state, which the old m7L/m7S firing
            //          path used to wipe via `PrevImbDir=0; PrevImbStr=0;
            //          PrevImbExtreme=0.f;`. Result: any M7-quality setup
            //          silently disabled M8 type 2 on subsequent bars.
            //          M7 was removed from goS/goL in v12.24 but its
            //          state-mutation lingered. Fix: do nothing here; let
            //          rvVerifyScore propagate to M8 type 2 cleanly.
        }
    }

    // ============================================================================
    //  M8 — FADE ENGINE
    // ============================================================================
    bool m8L=false, m8S=false;
    if(FADE_ENABLE&&pFade){
        pFade->reset();
        if(pBal&&pBal->mature&&ATR>0.f&&bkVerifyScore<=4){
            float bkT=ATR*0.30f;
            if(High0>pBal->rangeHigh+bkT*0.5f&&Close0<pBal->rangeHigh+bkT*0.3f&&barBearish){
                int edge=0;
                if(bkVerifyScore<=2) edge+=2; else edge+=1;
                if(pDiv&&pDiv->strength<=-2) edge+=2;
                if(pIce&&pIce->askIceberg&&FAbs(pIce->askIcebergPx-pBal->rangeHigh)<=ATR) edge+=2;
                if(pTrap&&pTrap->phase>=2&&pTrap->direction==+1) edge+=2;
                if(pDiv&&pDiv->persistAbsSell) edge+=1;
                if(edge>=4){
                    m8S=true; pFade->type=1; pFade->direction=-1;
                    pFade->edgeScore=IClamp(edge,0,10); pFade->active=true;
                    pFade->stopPx=High0+ATR*0.3f;
                    pFade->t1Px=pBal->rangePOC; pFade->t2Px=pBal->rangeLow;
                    pFade->entryPx=Close0; pFade->triggerBar=Idx;
                }
            }
            if(!m8S&&Low0<pBal->rangeLow-bkT*0.5f&&Close0>pBal->rangeLow-bkT*0.3f&&barBullish){
                int edge=0;
                if(bkVerifyScore<=2) edge+=2; else edge+=1;
                if(pDiv&&pDiv->strength>=2) edge+=2;
                if(pIce&&pIce->bidIceberg&&FAbs(pIce->bidIcebergPx-pBal->rangeLow)<=ATR) edge+=2;
                if(pTrap&&pTrap->phase>=2&&pTrap->direction==-1) edge+=2;
                if(pDiv&&pDiv->persistAbsBuy) edge+=1;
                if(edge>=4){
                    m8L=true; pFade->type=1; pFade->direction=+1;
                    pFade->edgeScore=IClamp(edge,0,10); pFade->active=true;
                    pFade->stopPx=Low0-ATR*0.3f;
                    pFade->t1Px=pBal->rangePOC; pFade->t2Px=pBal->rangeHigh;
                    pFade->entryPx=Close0; pFade->triggerBar=Idx;
                }
            }
        }
        if(!m8L&&!m8S&&rvVerifyScore>=2&&rvVerifyScore<=4&&PrevImbDir!=0){
            int edge=0;
            if(rvVerifyScore<=3) edge+=2; else edge+=1;
            if(PrevImbDir>0&&pDiv&&pDiv->strength>=0) edge+=2;
            if(PrevImbDir<0&&pDiv&&pDiv->strength<=0) edge+=2;
            if(pTrap&&pTrap->phase<2) edge+=1;
            if(edge>=4){
                if(PrevImbDir>0){
                    m8L=true; pFade->type=2; pFade->direction=+1;
                    pFade->edgeScore=IClamp(edge,0,10); pFade->active=true;
                    pFade->stopPx=FMin(Low0,PrevImbExtreme)-ATR*0.3f;
                    pFade->t1Px=PrevImbExtreme+ATR*0.5f; pFade->t2Px=PrevImbExtreme+ATR*1.5f;
                } else {
                    m8S=true; pFade->type=2; pFade->direction=-1;
                    pFade->edgeScore=IClamp(edge,0,10); pFade->active=true;
                    pFade->stopPx=FMax(High0,PrevImbExtreme)+ATR*0.3f;
                    pFade->t1Px=PrevImbExtreme-ATR*0.5f; pFade->t2Px=PrevImbExtreme-ATR*1.5f;
                }
                pFade->entryPx=Close0; pFade->triggerBar=Idx;
            }
        }
        if(!m8L&&!m8S&&Idx>=5&&ATR>0.f){
            int trendBars=0;
            for(int k=0;k<8&&Idx>=k;k++) if(sc.Close[Idx-k]>sc.Open[Idx-k]) trendBars++; else trendBars--;
            bool upT=(trendBars>=4), dnT=(trendBars<=-4);
            if(upT||dnT){
                float dm0=FAbs(pDR?pDR->delta[0]:Delta0);
                float dm1=(Idx>=1)?FAbs(pDR?pDR->delta[1]:(sc.AskVolume[Idx-1]-sc.BidVolume[Idx-1])):dm0;
                float dm2=(Idx>=2)?FAbs(pDR?pDR->delta[2]:(sc.AskVolume[Idx-2]-sc.BidVolume[Idx-2])):dm0;
                bool deltaDec=(dm0<dm1&&dm1<dm2);
                bool bodySmall=(barRange>0.f&&barBody<barRange*0.4f);
                bool volDec=(Idx>=2&&sc.Volume[Idx]<sc.Volume[Idx-1]&&sc.Volume[Idx-1]<sc.Volume[Idx-2]);
                int exhE=0;
                if(deltaDec) exhE+=2;
                if(bodySmall) exhE+=1;
                if(volDec) exhE+=2;
                if(upT&&pDiv&&pDiv->strength<=-2) exhE+=2;
                if(dnT&&pDiv&&pDiv->strength>=2) exhE+=2;
                if(upT&&pDiv&&pDiv->persistAbsSell) exhE+=1;
                if(dnT&&pDiv&&pDiv->persistAbsBuy) exhE+=1;
                if(exhE>=6){
                    if(upT){m8S=true; pFade->direction=-1; pFade->stopPx=High0+ATR*0.4f;
                        pFade->t1Px=Close0-ATR*1.0f; pFade->t2Px=(VWAP>0.f)?VWAP:(Close0-ATR*2.f);}
                    if(dnT&&!m8S){m8L=true; pFade->direction=+1; pFade->stopPx=Low0-ATR*0.4f;
                        pFade->t1Px=Close0+ATR*1.0f; pFade->t2Px=(VWAP>0.f)?VWAP:(Close0+ATR*2.f);}
                    if(m8L||m8S){pFade->type=3; pFade->edgeScore=IClamp(exhE,0,10); pFade->active=true;
                        pFade->triggerBar=Idx; pFade->entryPx=Close0;}
                }
            }
        }
        if(!m8L&&!m8S&&pDiv&&ATR>0.f){
            bool absUp=(pDiv->persistUpPxDnDelta>=3);
            bool absDn=(pDiv->persistDnPxUpDelta>=3);
            if(absUp||absDn){
                int absE=3;
                bool atK=false;
                if(pVP&&pVP->valid){
                    float vL[3]={pVP->poc,pVP->vah,pVP->val};
                    for(int i=0;i<3;i++) if(vL[i]>0.f&&FAbs(Close0-vL[i])<=ATR*0.5f) atK=true;
                }
                if(VWAP>0.f&&FAbs(Close0-VWAP)<=ATR*0.5f) atK=true;
                if(atK) absE+=2;
                if(absUp&&pDiv->trendDivBear) absE+=2;
                if(absDn&&pDiv->trendDivBull) absE+=2;
                if(pTrap&&pTrap->phase>=2) absE+=2;
                if(absE>=6){
                    if(absUp){m8S=true; pFade->direction=-1; pFade->stopPx=High0+ATR*0.35f;
                        pFade->t1Px=Close0-ATR*0.75f; pFade->t2Px=Close0-ATR*2.f;}
                    if(absDn&&!m8S){m8L=true; pFade->direction=+1; pFade->stopPx=Low0-ATR*0.35f;
                        pFade->t1Px=Close0+ATR*0.75f; pFade->t2Px=Close0+ATR*2.f;}
                    if(m8L||m8S){pFade->type=4; pFade->edgeScore=IClamp(absE,0,10); pFade->active=true;
                        pFade->triggerBar=Idx; pFade->entryPx=Close0;}
                }
            }
        }
        // [v12.25] M8 strict CtrlScore gate. Fades require neutral/correct-side flow.
        //          Mirrors v12.20 (d) intent for M5. Covers all FadeTypes 1-4.
        if(m8L && controlScore < 0){ m8L=false; pFade->reset(); }
        if(m8S && controlScore > 0){ m8S=false; pFade->reset(); }
    }

    // ============================================================================
    //  [v12.6] PER-BAR EVAL DIAGNOSTIC LOG
    // ============================================================================
    if(DIAG && LOG_LVL >= LOG_DBG && isNewBar){
        int bestSc = (int)FMax((float)(DL?scL:(LBL?lbScL:0)), (float)(DS?scS:(LBS?lbScS:0)));
        int q_preview = V18A_QualityScore100(-1, bestSc,
            (pFade && pFade->active) ? pFade->edgeScore : 0,
            (pFade && pFade->active));
        SCString em;
        em.Format("[V18A EVAL] %02d:%02d scL=%d scS=%d ctrl=%d div=%d "
                  "m1=%d%d m2=%d%d m3=%d%d m4=%d%d m5=%d%d m6=%d%d m7=%d%d m8=%d%d "
                  "RM=%.2f Q~%d trend=%d chop=%d CL=%d",
            BarHHMM/100, BarHHMM%100,
            DL?scL:(LBL?lbScL:0), DS?scS:(LBS?lbScS:0),
            controlScore, pDiv?pDiv->strength:0,
            m1L?1:0, m1S?1:0, m2L?1:0, m2S?1:0,
            m3L?1:0, m3S?1:0, m4L?1:0, m4S?1:0,
            m5L?1:0, m5S?1:0,
            m6L?1:0, m6S?1:0,
            m7L?1:0, m7S?1:0, m8L?1:0, m8S?1:0,
            pRisk?pRisk->riskMultiplier:1.0f, q_preview,
            pRegime?pRegime->trendRegime:0,
            pRegime?pRegime->chopRegime:0,
            ConsecLoss);
        sc.AddMessageToLog(em, 0);
    }

    // ============================================================================
    //  SIGNAL COMBINATION
    // ============================================================================
    bool goL=m1L||m2L||m3L||m4L||m6L||m8L;   // [v12.24] M5+M7 removed
    bool goS=m1S||m2S||m3S||m4S||m6S||m8S;
    if(!goL&&!goS) return;

    int selMode=-1;
    bool selLong=false;
    // priority: M6 > M8 (fade) > M4 > M3 > M2 > M1  [v12.24: M5+M7 removed]
    if(m6L){selMode=5; selLong=true;}
    else if(m6S){selMode=5; selLong=false;}
    else if(m8L){selMode=7; selLong=true;}
    else if(m8S){selMode=7; selLong=false;}
    else if(m4L){selMode=3; selLong=true;}
    else if(m4S){selMode=3; selLong=false;}
    else if(m3L){selMode=2; selLong=true;}
    else if(m3S){selMode=2; selLong=false;}
    else if(m2L){selMode=1; selLong=true;}
    else if(m2S){selMode=1; selLong=false;}
    else if(m1L){selMode=0; selLong=true;}
    else if(m1S){selMode=0; selLong=false;}
    if(selMode<0) return;

    // Regime filter (optional)
    if(REGIME_FILTER&&pRegime&&!pRegime->allowMode(selMode, selLong)){
        if(DIAG&&LOG_LVL>=LOG_DBG&&Idx!=LastBlockLogBar){
            LastBlockLogBar=Idx;
            SCString m; m.Format("[V18A] Skip M%d %s — regime blocked trend=%d chop=%d ts=%.2f",
                selMode+1, selLong?"L":"S",
                pRegime->trendRegime, pRegime->chopRegime, pRegime->trendStrength);
            sc.AddMessageToLog(m,0);
        }
        return;
    }

    // [v12.24] M1-specific filters derived from backtest cluster analysis
    if(selMode==0){
        // Dead zone: 12:00-13:59 ET — 0% WR confirmed on NQH26/NQM25/NQZ5
        if(BarHHMM>=1200 && BarHHMM<=1359) return;
        // Trend direction gate: no LONG into falling 20-bar trend, no SHORT into rising
        if(ATR>0.f && Idx>=20){
            float pc20 = sc.Close[Idx] - sc.Close[Idx-20];
            if(selLong  && pc20 < -ATR*0.5f) return;
            if(!selLong && pc20 >  ATR*0.5f) return;
        }
    }

    // Auto-disable bad modes
    if(AUTO_DISABLE&&pMS&&pMS->shouldDisable(selMode)){
        if(DIAG&&LOG_LVL>=LOG_SIG&&Idx!=LastBlockLogBar){
            LastBlockLogBar=Idx;
            SCString m; m.Format("[V18A] Skip M%d %s — AUTO-DISABLED t=%.2f n=%d",
                selMode+1, selLong?"L":"S", pMS->tStat(selMode), pMS->trades[selMode]);
            sc.AddMessageToLog(m,0);
        }
        return;
    }

    // Post-stop cooldown (same-direction only)
    if(C_POST_STOP_COOLDOWN>0&&LastStopBar>=0&&Idx<=LastStopBar+C_POST_STOP_COOLDOWN){
        bool sameDir=(selLong&&LastStopDir>0)||(!selLong&&LastStopDir<0);
        if(sameDir){
            if(DIAG&&LOG_LVL>=LOG_DBG&&Idx!=LastBlockLogBar){
                LastBlockLogBar=Idx;
                sc.AddMessageToLog("[V18A] Skip — post-stop same-dir cooldown", 0);
            }
            return;
        }
    }

    // Bar-close gate (wait for close on entries)
    if(!BarClosed){
        return;
    }

    // [v12.4] canonical single consec-loss check (no double-gate drift)
    {
        const int consec = ConsecLoss;
        if(consec >= C_MAX_LOSSES){
            if(DIAG&&LOG_LVL>=LOG_DBG&&Idx!=LastBlockLogBar){
                LastBlockLogBar=Idx;
                SCString m; m.Format("[V18A] Skip — canonical consec %d>=%d",
                    consec, C_MAX_LOSSES);
                sc.AddMessageToLog(m,0);
            }
            return;
        }
    }

    // [v12.31] updateVolRegime / updateTimeDecay moved to the top of the
    //          function so they sample every bar (not only bars that reach
    //          this point). Keep computeRiskMultiplier here for freshness
    //          right before the floor check below.
    if(pRisk) pRisk->computeRiskMultiplier();

    // Risk multiplier floor ([v12.1] lowered to 0.60)
    if(pRisk && pRisk->riskMultiplier < C_RM_FLOOR){
        if(DIAG&&LOG_LVL>=LOG_SIG&&Idx!=LastBlockLogBar){
            LastBlockLogBar=Idx;
            char diag[256]; pRisk->getRiskDiagnostics(diag, 256);
            SCString m; m.Format("[V18A] Skip — RM=%.2f below floor %.2f | %s",
                pRisk->riskMultiplier, C_RM_FLOOR, diag);
            sc.AddMessageToLog(m,0);
        }
        return;
    }

    // Session DD gate — exceeds 80% of daily budget
    if(pRisk && DAILY_LOSS>0.f && pRisk->sessionDrawdown > DAILY_LOSS*0.8f){
        if(DIAG&&LOG_LVL>=LOG_SIG&&Idx!=LastBlockLogBar){
            LastBlockLogBar=Idx;
            SCString m; m.Format("[V18A] Skip — session DD $%.0f > 80%% of budget $%.0f",
                pRisk->sessionDrawdown, DAILY_LOSS*0.8f);
            sc.AddMessageToLog(m,0);
        }
        return;
    }

    // ============================================================================
    //  QUALITY SCORE + RISK-ADJUSTED BASE QUANTITY  ([v12.2] applies here)
    // ============================================================================
    int finalScore = selLong
        ? (DL?scL:(LBL?lbScL:0))
        : (DS?scS:(LBS?lbScS:0));
    int edgeScore  = (pFade && pFade->active) ? pFade->edgeScore : 0;
    int qScore100  = V18A_QualityScore100(selMode, finalScore, edgeScore, pFade && pFade->active);

    // [v12.38] M8 (selMode==7) uses a raised floor of 60; all other modes 50.
    int effFloor = (selMode==7) ? V18A_QUALITY_FLOOR_M8 : V18A_QUALITY_FLOOR;
    if(qScore100 < effFloor){
        if(DIAG&&LOG_LVL>=LOG_SIG&&Idx!=LastBlockLogBar){
            LastBlockLogBar=Idx;
            SCString m; m.Format("[V18A] Skip M%d %s — quality %d < floor %d (score=%d)",
                selMode+1, selLong?"L":"S", qScore100, effFloor, finalScore);
            sc.AddMessageToLog(m,0);
        }
        return;
    }

    // Base quantity = TOTQTY * RM, clamped to session max
    int baseQty = TOTQTY;
    if(pRisk){
        float rm = pRisk->riskMultiplier;
        baseQty = (int)floorf((float)TOTQTY * rm + 0.5f);
        baseQty = IClamp(baseQty, 1, TOTQTY);
    }

    // Vol-scaler dampening on big-range sessions
    if(pVS){
        float recentRange=0.f;
        for(int k=0;k<5&&Idx>=k;k++) recentRange = FMax(recentRange, sc.High[Idx-k]-sc.Low[Idx-k]);
        float vmult = pVS->sizeMultiplier(recentRange);
        if(vmult < 1.0f){
            baseQty = (int)floorf((float)baseQty * vmult + 0.5f);
            baseQty = IClamp(baseQty, 1, TOTQTY);
        }
    }

    // ============================================================================
    //  ENTRY / STOP / TARGET COMPUTATION (mode-specific)
    // ============================================================================
    float entryPrice=Close0, stopPrice=0.f, tp1Price=0.f, tp2Price=0.f;

    // ---- [VARIANT FB500] FIXED $ BRACKET -----------------------------------
    //  Overrides every mode-specific stop/target path below, including the
    //  pre-computed M6 balance-breakout, M7 auction and M8 fade levels. T1 and
    //  T2 are set to the SAME price: there is one target, and the exit manager
    //  is gated (see the FIXBRKT block in position management) so no partial,
    //  breakeven shift, trail or early-scratch can move it.
    const bool fixBrkt = FIXBRKT_ACTIVE;
    if(fixBrkt){
        const float fbStopPts = C_VAR_FIXBRKT_STOP_USD / PtVal;
        const float fbTgtPts  = C_VAR_FIXBRKT_TGT_USD  / PtVal;
        if(selLong){
            stopPrice = TFloor(entryPrice - fbStopPts, TICK);
            tp1Price  = TCeil (entryPrice + fbTgtPts,  TICK);
        } else {
            stopPrice = TCeil (entryPrice + fbStopPts, TICK);
            tp1Price  = TFloor(entryPrice - fbTgtPts,  TICK);
        }
        tp2Price = tp1Price;
    }
    else if(selMode==5 && m6Stop!=0.f){
        // M6 uses pre-computed balance-breakout levels
        stopPrice = m6Stop;
        tp1Price  = m6T1;
        tp2Price  = m6T2;
    }
    else if(selMode==6 && m7Stop!=0.f){
        // M7 auction reversal — stop beyond prior imbalance extreme
        stopPrice = m7Stop;
        float stopDist = FAbs(entryPrice - stopPrice);
        if(selLong){tp1Price = entryPrice + stopDist*1.5f; tp2Price = entryPrice + stopDist*3.0f;}
        else       {tp1Price = entryPrice - stopDist*1.5f; tp2Price = entryPrice - stopDist*3.0f;}
    }
    else if(selMode==7 && pFade && pFade->active){
        // M8 fade — use pre-computed fade targets
        stopPrice = pFade->stopPx;
        tp1Price  = pFade->t1Px;
        tp2Price  = pFade->t2Px;
    }
    // [v12.26] Tick-snap M6/M7/M8 levels. Live 2026-05-15 NQM6 BUY M8: TP1
    //          computed at 29311.09 (off-tick); broker snapped the bracket to
    //          29311.00, filled at 29310.50, and the internal exit detector
    //          (High0>=29311.09) missed it — tagged EXTERNAL instead of TP1,
    //          poisoning M8 win-rate stats. M1-M5 path below already snaps;
    //          mirror it here so brackets and detector agree on tick grid.
    // [VARIANT FB500] fixBrkt short-circuits this second chain too — without
    //   the guard, M1-M5 would fall through to the ATR `else` below and
    //   silently overwrite the fixed levels.
    if(fixBrkt){
        // already tick-snapped at construction
    }
    else if(selMode==5 || selMode==6 || selMode==7){
        if(selLong){
            stopPrice = TFloor(stopPrice, TICK);
            tp1Price  = TCeil (tp1Price,  TICK);
            tp2Price  = TCeil (tp2Price,  TICK);
        } else {
            stopPrice = TCeil (stopPrice, TICK);
            tp1Price  = TFloor(tp1Price,  TICK);
            tp2Price  = TFloor(tp2Price,  TICK);
        }
    }
    else {
        // M1-M5 use ATR-based stop/target with floor/ceiling caps
        float stopDist = ATR * C_STOP_ATR;
        stopDist = FClamp(stopDist, C_STOP_FLOOR_PTS, C_STOP_CEIL_PTS);
        float t1Dist = ATR * C_T1_ATR;
        t1Dist = FClamp(t1Dist, C_T1_FLOOR_PTS, C_T1_CEIL_PTS);
        float t2Dist = ATR * C_T2_ATR;
        t2Dist = FClamp(t2Dist, C_T2_FLOOR_PTS, C_T2_CEIL_PTS);

        if(selLong){
            stopPrice = TFloor(entryPrice - stopDist, TICK);
            tp1Price  = TCeil (entryPrice + t1Dist,   TICK);
            tp2Price  = TCeil (entryPrice + t2Dist,   TICK);
        } else {
            stopPrice = TCeil (entryPrice + stopDist, TICK);
            tp1Price  = TFloor(entryPrice - t1Dist,   TICK);
            tp2Price  = TFloor(entryPrice - t2Dist,   TICK);
        }
    }

    // Sanity — ensure stop is on correct side of entry and meets minimum distance
    // [VARIANT FB500] Skipped under the fixed bracket. The levels are already
    //   correct-sided and a fixed distance from entry, and the `tp2 <= tp1`
    //   rule here would push T2 away from T1 — reintroducing a second target
    //   the fixed bracket exists to eliminate. Note C_STOP_FLOOR_PTS is 20pt
    //   ($400) and the fixed stop is 25pt ($500), so the floor is satisfied
    //   anyway; the skip is about T2, not the stop.
    float minStopDist = FMax(ATR*0.5f, C_STOP_FLOOR_PTS);
    if(fixBrkt){
        // no adjustment
    }
    else if(selLong){
        if(stopPrice >= entryPrice - TICK) stopPrice = TFloor(entryPrice - minStopDist, TICK);
        if(tp1Price  <= entryPrice + TICK) tp1Price  = TCeil (entryPrice + FMax(ATR*1.0f, C_T1_FLOOR_PTS), TICK);
        if(tp2Price  <= tp1Price)          tp2Price  = TCeil (tp1Price + FMax(ATR*1.0f, 25.0f), TICK);
    } else {
        if(stopPrice <= entryPrice + TICK) stopPrice = TCeil (entryPrice + minStopDist, TICK);
        if(tp1Price  >= entryPrice - TICK) tp1Price  = TFloor(entryPrice - FMax(ATR*1.0f, C_T1_FLOOR_PTS), TICK);
        if(tp2Price  >= tp1Price)          tp2Price  = TFloor(tp1Price - FMax(ATR*1.0f, 25.0f), TICK);
    }

    // Capital-based risk clamp — don't risk more than 2% of account per trade
    {
        float stopDist = FAbs(entryPrice - stopPrice);
        float riskDollars = stopDist * PtVal * (float)baseQty;
        if(riskDollars > MaxRiskPerTrade && PtVal>0.f && stopDist>0.f){
            int qtyCap = (int)floorf(MaxRiskPerTrade / (stopDist * PtVal));
            if(qtyCap < 1) {
                if(DIAG&&LOG_LVL>=LOG_SIG && Idx!=LastBlockLogBar){
                    LastBlockLogBar=Idx;
                    SCString m; m.Format("[V18A] Skip M%d %s — min qty 1 exceeds capital risk %.0f",
                        selMode+1, selLong?"L":"S", MaxRiskPerTrade);
                    sc.AddMessageToLog(m,0);
                }
                return;
            }
            baseQty = IClamp(qtyCap, 1, baseQty);
        }
    }

    // Same-bar dedup — one signal per bar
    if(LastSigBar == Idx){
        return;
    }

    // ============================================================================
    //  DIAGNOSTIC LOG + CSV SETUP ROW
    // ============================================================================
    const char* modeNames2[]={"M1","M2","M3","M4","M5","M6","M7","M8"};
    const char* mN = (selMode>=0&&selMode<8)?modeNames2[selMode]:"M?";

    if(LOG_LVL>=LOG_SIG){
        // [VARIANT FB500] Report the bracket in DOLLARS as well as price, so a
        //   live log can be checked against the intended $500/$500 without
        //   re-deriving PtVal by hand.
        SCString m; m.Format("[V18A SETUP] %s %s Q=%d Sc=%d Ent=%.2f SL=%.2f TGT=%.2f Qty=%d RM=%.2f | %s risk=$%.0f rwd=$%.0f",
            selLong?"LONG":"SHORT", mN, qScore100, finalScore,
            entryPrice, stopPrice, tp1Price, baseQty,
            pRisk?pRisk->riskMultiplier:1.f,
            fixBrkt?"FIXBRKT":"ATR",
            FAbs(entryPrice-stopPrice)*PtVal*(float)baseQty,
            FAbs(tp1Price-entryPrice)*PtVal*(float)baseQty);
        sc.AddMessageToLog(m, 0);
    }

    if(CSV_ON){
        float dayPnL_now = pos.CumulativeProfitLoss - DayOpenPnL + pos.OpenProfitLoss;
        WriteCSV(sc, "SETUP", selLong?"BUY":"SELL", mN,
            entryPrice, stopPrice, tp1Price, tp2Price, baseQty, finalScore,
            controlScore, pDiv?pDiv->strength:0, Delta0, 0.f,
            0.f, "", 0, 0.f, 0.f,
            dayPnL_now, pRisk?pRisk->cumPnL:0.f,
            (pFade&&pFade->active)?pFade->edgeScore:0,
            (pFade&&pFade->active)?pFade->type:0,
            pRisk?pRisk->riskMultiplier:1.f,
            pRegime?pRegime->trendRegime:0,
            pRegime?pRegime->volRegime:1,
            pRegime?pRegime->chopRegime:0);
    }

    // If signalOnly (historical/replay), do not submit live orders
    if(signalOnly){
        if(DIAG&&LOG_LVL>=LOG_DBG){
            SCString m; m.Format("[V18A] Replay/historical — SETUP logged, order NOT submitted");
            sc.AddMessageToLog(m,0);
        }
        return;
    }

    // [live-fire audit] Reached only when !signalOnly — a genuine live fire.
    // Logged before order submission so the SIGNAL is captured regardless of the
    // fill outcome. Recalc/replay never reaches here (returned above). See
    // WriteLiveFire — file IOF_NQ_LiveFires_<sym>.csv.
    if(CSV_ON){
        WriteLiveFire(sc, mN, selLong?"BUY":"SELL", qScore100, finalScore,
                      entryPrice, stopPrice, tp1Price, tp2Price, baseQty,
                      pRisk?pRisk->riskMultiplier:1.f);
    }

    // Live order submission
    s_SCNewOrder newOrder;
    newOrder.OrderQuantity = baseQty;
    if(ENTRY_ORD == 0){
        newOrder.OrderType = SCT_ORDERTYPE_MARKET;
    } else if(ENTRY_ORD == 1){
        newOrder.OrderType = SCT_ORDERTYPE_LIMIT;
        newOrder.Price1 = entryPrice;
    } else {
        // Limit +/- 2 ticks (more aggressive on crossing)
        newOrder.OrderType = SCT_ORDERTYPE_LIMIT;
        newOrder.Price1 = selLong ? (entryPrice + TICK*2.f) : (entryPrice - TICK*2.f);
    }
    newOrder.Stop1Price   = stopPrice;
    newOrder.TimeInForce  = SCT_TIF_DAY;
    // [v12.13] Attached-order quantities must sum to <= parent qty.
    //          Previous code set both Target1Price and Target2Price unconditionally,
    //          which implicitly attached 2 target orders. When parent qty=1,
    //          Sierra rejects with: "Parent is 1, Attached Orders total 2".
    //          Fix: when qty=1, only attach Target1 (single runner to T1).
    //          When qty>=2, split: OCOGroup1Quantity to T1, remainder to T2.
    // [VARIANT FB500] The fixed bracket has ONE target for the whole position,
    //   at any quantity. Never split into T1/T2 legs here — T1Px == T2Px in
    //   this build, so a split would attach two orders at the same price and
    //   turn a 1:1 trade into an unintended scale-out.
    if(fixBrkt){
        newOrder.Target1Price = tp1Price;
        newOrder.Target2Price = 0.f;
        newOrder.OCOGroup1Quantity = 0;
    }
    else if(baseQty >= 2){
        newOrder.Target1Price = tp1Price;
        newOrder.Target2Price = tp2Price;
        newOrder.OCOGroup1Quantity = (int)floorf((float)baseQty*C_T1_RATIO);
        if(newOrder.OCOGroup1Quantity < 1)           newOrder.OCOGroup1Quantity = 1;
        if(newOrder.OCOGroup1Quantity >= baseQty)    newOrder.OCOGroup1Quantity = baseQty-1;
    } else {
        // Single-contract trade: only one target, go to T1.
        newOrder.Target1Price = tp1Price;
        newOrder.Target2Price = 0.f;
        newOrder.OCOGroup1Quantity = 0;
    }

    int orderResult = 0;
    if(selLong) orderResult = (int)sc.BuyEntry(newOrder);
    else        orderResult = (int)sc.SellEntry(newOrder);

    if(orderResult > 0){
        // Entry accepted — commit trade state
        TradeState    = selLong ? 1 : 3;
        EntryPx       = entryPrice;
        ExpectedFillPx= entryPrice;
        StopPx        = stopPrice;
        T1Px          = tp1Price;
        T2Px          = tp2Price;
        EntryBar      = Idx;
        LastTradeDir  = selLong ? 1 : -1;
        LiveTradeDir  = selLong ? 1 : -1;
        TradeMode     = selMode;
        TradeScore    = finalScore;
        EntryQty      = baseQty;
        TradeMAE      = 0.f;
        TradeMFE      = 0.f;
        // [v12.24] Snapshot cumulative realized P/L. At exit, the delta gives
        //          the true average broker fill price even when our reason
        //          resolution falls through to UNKNOWN/EXTERNAL.
        CumPnL_AtEntry = pos.CumulativeProfitLoss;
        Trades++;
        LastSigBar    = Idx;

        if(LOG_LVL>=LOG_CRIT){
            SCString m; m.Format("[V18A ENTRY] %s %s Q=%d Ent=%.2f SL=%.2f T1=%.2f T2=%.2f Qty=%d Trade#%d",
                selLong?"LONG":"SHORT", mN, qScore100,
                entryPrice, stopPrice, tp1Price, tp2Price, baseQty, Trades);
            sc.AddMessageToLog(m, 0);
        }

        if(CSV_ON){
            float dayPnL_now = pos.CumulativeProfitLoss - DayOpenPnL + pos.OpenProfitLoss;
            WriteCSV(sc, "ENTRY", selLong?"BUY":"SELL", mN,
                entryPrice, stopPrice, tp1Price, tp2Price, baseQty, finalScore,
                controlScore, pDiv?pDiv->strength:0, Delta0, 0.f,
                0.f, "", 0, 0.f, 0.f,
                dayPnL_now, pRisk?pRisk->cumPnL:0.f,
                (pFade&&pFade->active)?pFade->edgeScore:0,
                (pFade&&pFade->active)?pFade->type:0,
                pRisk?pRisk->riskMultiplier:1.f,
                pRegime?pRegime->trendRegime:0,
                pRegime?pRegime->volRegime:1,
                pRegime?pRegime->chopRegime:0);
        }

        // Hypo shadow — mirror live entry for comparison journal
        HypoSide    = selLong ? 1 : -1;
        HypoEntryPx = entryPrice;
        HypoSLPx    = stopPrice;
        HypoTP1Px   = tp1Price;
        HypoTP2Px   = tp2Price;
        HypoQty     = baseQty;
        HypoT1Hit   = 0;
        HypoPartialPnL = 0.f;

        // Draw entry marker on chart (arrow-style via DRAWING_MARKER).
        // Note: DRAWING_ARROW has no ArrowHeadType field in current Sierra
        // builds — it's a two-point drawing. A single-point directional arrow
        // is better expressed as a MARKER with MARKER_ARROWUP/ARROWDOWN.
        s_UseTool arrow; arrow.Clear();
        arrow.ChartNumber  = sc.ChartNumber;
        arrow.DrawingType  = DRAWING_MARKER;
        arrow.LineNumber   = 100000 + Trades;
        arrow.BeginIndex   = Idx;
        arrow.BeginValue   = selLong ? Low0 - TICK*4 : High0 + TICK*4;
        arrow.MarkerType   = selLong ? MARKER_ARROWUP : MARKER_ARROWDOWN;
        arrow.MarkerSize   = 10;
        arrow.Color        = selLong ? RGB(0,200,100) : RGB(220,80,80);
        arrow.LineWidth    = 2;
        arrow.AddMethod    = UTAM_ADD_OR_ADJUST;
        sc.UseTool(arrow);

        if(selLong) SG_BUY[Idx]  = Low0  - TICK*4;
        else        SG_SELL[Idx] = High0 + TICK*4;
    } else {
        // Entry rejected by Sierra
        if(LOG_LVL>=LOG_CRIT){
            SCString m; m.Format("[V18A REJECT] %s %s — sc.BuyEntry/SellEntry returned %d",
                selLong?"LONG":"SHORT", mN, orderResult);
            sc.AddMessageToLog(m, 1);
        }
        if(CSV_ON){
            float dayPnL_now = pos.CumulativeProfitLoss - DayOpenPnL + pos.OpenProfitLoss;
            char reason[64]; snprintf(reason, 64, "order_rc=%d", orderResult);
            WriteCSV(sc, "REJECT", selLong?"BUY":"SELL", mN,
                entryPrice, stopPrice, tp1Price, tp2Price, baseQty, finalScore,
                controlScore, pDiv?pDiv->strength:0, Delta0, 0.f,
                0.f, reason, 0, 0.f, 0.f,
                dayPnL_now, pRisk?pRisk->cumPnL:0.f,
                (pFade&&pFade->active)?pFade->edgeScore:0,
                (pFade&&pFade->active)?pFade->type:0,
                pRisk?pRisk->riskMultiplier:1.f,
                pRegime?pRegime->trendRegime:0,
                pRegime?pRegime->volRegime:1,
                pRegime?pRegime->chopRegime:0);
        }
    }
}

// ============================================================================
//  END OF IOF NQ — Pure Orderflow Autopilot
// ============================================================================
