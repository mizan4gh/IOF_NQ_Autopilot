// ============================================================================
//  Mizan_IOF_NQ.cpp  —  frozen-level sweep-and-reclaim
//  Sierra Chart ACSIL.  NQ, 5-minute bars, 24-hour chart session REQUIRED.
// ============================================================================
//
//  WHAT THIS IS
//  ------------
//  The measured half of the daily power-of-three. Overnight trade builds a
//  range; the RTH open runs one of yesterday's or last night's extremes to take
//  the resting orders behind it; if that break is rejected and the level is
//  reclaimed on the same bar, the study buys (or sells) the reclaim.
//
//      ACCUMULATION   18:00 -> 09:29    freezes ONH, ONL and ATR at 09:30
//      MANIPULATION   09:30 -> 12:00    a frozen level is swept and reclaimed
//      ENTRY                            limit, 0.15xATR back toward the sweep,
//                                       armed 3 bars   (or market, next bar)
//
//  WHAT IS DELIBERATELY MISSING
//  ----------------------------
//  The distribution stage and the pullback-to-POC entry that the strategy was
//  originally built around. Both were measured and both DESTROY value:
//
//      full chain (A->M->D->POC)     66 trades   -$3,675   21.5th pctile
//      distribution only (no POC)   112 trades  +$13,390   63.5th pctile
//      this file (A->M only)        253 trades  +$62,730   99.0th pctile
//
//  There is no volume profile in this file for that reason. Do not "restore"
//  the POC entry — it was falsified, not omitted for effort. The Python that
//  measured all three is backtest_mizan_p3.py; the re-sign null is
//  backtest_mizan_null.py.
//
//  THE FROZEN LEVELS
//  -----------------
//  ONH / ONL   overnight high / low, 18:00 -> 09:29
//  PDH / PDL   the previous session's RTH high / low
//  ATR(14)     Wilders, sampled on the LAST BAR BEFORE 09:30
//
//  All four prices and the ATR stop being updated at 09:30 is the whole point.
//  A placebo that keeps the sweep mechanic and the trade count but pulls each
//  level inward by a fraction of the overnight range collapses the result to
//  the 83rd / 19th / 18th percentile, so the levels are load-bearing rather
//  than decorative. That holds for the MARKET entry. Under the limit entry the
//  placebo does not collapse at all — see THE ENTRY below, because it is the
//  reason to think twice about the default.
//
//  Every threshold below is an ATR multiple and never a point count: this index
//  traded 20k to 30k across the test contracts, and a points-denominated gate
//  silently selects a biased subsample when it does.
//
//  SESSION MODEL
//  -------------
//  A session day runs 18:00 -> 17:59. Bars are stamped on the local date, so
//  the 18:00-23:59 block belongs to the NEXT date and is pushed forward one
//  calendar day to be grouped with the morning it precedes. The chart must
//  therefore carry evening data — on an RTH-only chart the overnight bar count
//  never reaches MIN_ON_BARS and this study will correctly never trade. It logs
//  that condition once per session at log level 2.
//
//  (The Python groups by the next date PRESENT IN THE DATA; this uses the next
//  CALENDAR date. They differ only when a holiday follows an evening session,
//  where this file orphans that evening and skips the day on the bar count.
//  Skipping is the safe side of that difference.)
//
//  THE ENTRY — LIMIT (default) OR MARKET
//  -------------------------------------
//  Entry Mode 1 (LIMIT, the default) rests a limit 0.15 x ATR back toward the
//  sweep extreme — under the reclaim bar's close for a long, over it for a
//  short — armed for 3 bars. The order is pulled if the arm window expires, if
//  a later bar CLOSES back beyond the sweep extreme, or at the flatten time.
//  Exits are unchanged: the stop is a stop-market, the target a resting limit.
//
//  Entry Mode 0 restores the market order at the next bar's open. READ THIS
//  BEFORE CHOOSING: mode 0 is the configuration every number in STATUS below
//  was measured on. Mode 1 makes more money and is worse where it counts.
//  Both columns re-run on the 6 frozen NQ contracts, 387 sessions, 2026-08-21:
//
//                          mode 0 MARKET       mode 1 LIMIT
//      trades              253 (0.65/day)      222 (0.57/day)
//      pooled net             +$62,730            +$75,340
//      ship gate               5/6 FAIL            5/6 FAIL
//      re-sign null              99.0th             100.0th
//      PLACEBO shift 0.10        83.0th             100.0th
//      PLACEBO shift 0.20        19.5th              98.0th
//
//  (An earlier revision of this header quoted 105 trades / +$91,795 / 6/6 for
//  mode 1. Those belong to a DIFFERENT rule. backtest_mizan_p3.py's
//  "sweep_limit" was never added to the list of modes that skip the
//  distribution stage, so it silently required a close 0.5 x ATR clear of the
//  overnight POC before arming the limit — a stage this file does not
//  implement and never did. That mode is now "sweep_limit_d" and still
//  reproduces 105 / +$91,795; "sweep_limit" is what runs here.)
//
//  The last two rows are the whole story, and the correction sharpens them.
//  Pull every frozen level inward by a fraction of the overnight range — keep
//  the sweep mechanic, destroy the level's claim to be a structural price —
//  and the market rule collapses to noise at the 19.5th, which is what a real
//  level effect is supposed to do. The limit rule does not collapse. It scores
//  at the ceiling whether the levels mean anything or not, and it does so at
//  222 trades against the market rule's 253, so this can no longer be waved off
//  as a small-sample artifact. What carries mode 1 is the retrace filter, not
//  the level: a setup that never comes back 0.15 x ATR is never entered, and
//  per 069d4e0 that unfilled group is the losing group. Price improvement on
//  the fills is about $16 a trade, i.e. nothing.
//
//  So mode 1's extra $12,610 is a filter wearing a fill's clothing. It may well
//  be a real filter — the order fills or it does not, there is no hindsight in
//  it — but it is a DIFFERENT claim from the one this file's levels make, and
//  it has never been tested as one.
//
//  One setup per session is consumed by the SWEEP, not by the fill: a limit
//  that expires unfilled does not free the day for a second sweep. That is what
//  backtest_mizan_p3.py measures (its scan breaks on the first sweep of the
//  day regardless of what the entry does afterwards) and the two have to agree.
//
//  The backtest also demands the price trade one tick THROUGH the limit before
//  it books a fill. That is a fill-model conservatism, not part of the rule, so
//  it is deliberately not reproduced here; a real resting order at the same
//  price fills at least as often as the measurement assumed.
//
//  NO LOOK-AHEAD
//  -------------
//  Every decision is made from bars that have CLOSED. The bar loop walks
//  (LastBar, Idx-1] rather than trusting sc.Index to be re-entered after it
//  closes, so no bar is ever evaluated while it is still forming and none is
//  silently skipped when a new bar arrives.
//
//  The entry is submitted after the signal bar closes — a market order fills
//  around the next bar's open, a limit rests from that bar forward — and the
//  bracket is attached to the parent as OFFSETS from the actual fill. That
//  matters: the measurement manages the trade against the FILL BAR'S OWN RANGE.
//  Managing from the bar after the fill instead deletes the first bar of
//  adverse excursion from every trade and was worth $6,400 of fiction in the
//  Python before it was found. If you port this logic anywhere else, check
//  that first.
//
//  STATUS — NOT VALIDATED FOR A FUNDED ACCOUNT
//  -------------------------------------------
//  Everything below describes ENTRY MODE 0. Mode 1 has no line of its own here
//  because it has not earned one — see THE ENTRY above.
//
//  What it has:  99.0th percentile against a 200-draw re-sign null on 6 frozen
//                NQ contracts (253 trades, +$62,730, pooled t=+2.47); survives
//                3 ticks of slippage at the 98.5th; OOS-2026 3/3 at the 99.5th;
//                either level family alone; a passed placebo test.
//  What it lacks:
//    * the ship gate. 5/6 contracts net>0 and PF>1, not 6/6. NQZ25 loses.
//    * ES. The identical rule, thresholds already ATR-denominated, returns
//      -$392 over 276 trades at the 41.5th percentile, 3/6. NQ and ES are 0.954
//      correlated on daily returns, so ES was never independent evidence — but
//      a real liquidity-structure effect ought to appear in both, and it does
//      not. Unexplained.
//    * an uncorrelated instrument. Six NQ contracts are one index.
//    * any governor or sizing model. The measurement ran flat 1 lot with every
//      daily governor OFF, and the defaults below match it exactly so that what
//      runs is what was measured. Turning DAILY_LOSS on is a departure from the
//      tested configuration, not a free safety upgrade.
//
//  Sim first. This is a research artifact with a good null, not a validated
//  system, and a per-contract delta inside +/-$8k is noise.
// ============================================================================

#include "sierrachart.h"

#include <cfloat>
#include <cmath>

SCDLLName("Mizan_IOF_NQ")

// ---------------------------------------------------------------- helpers --
static inline float TRound(float p, float t) { return (t > 0.f) ? roundf(p / t) * t : p; }

static inline int HhmmFromSeconds(int s)
{
    return (s / 3600) * 100 + (s % 3600) / 60;
}

enum {
    PI_SessId = 1,
    PI_LastBar,       // newest bar already evaluated
    PI_OnBars,
    PI_RthBars,
    PI_PrevRthBars,   // bar count of the session PDH/PDL came from
    PI_SetupTaken,
    PI_Locked,
    PI_Dir,
    PI_WarnedNoOn,
    PI_PendID,        // internal id of a resting entry limit, 0 = none
    PI_PendBar,       // bar the limit was submitted on (the sweep bar)
    PI_PendSide
};
enum {
    PF_OnHigh = 1,
    PF_OnLow,
    PF_RthHigh,
    PF_RthLow,
    PF_PdHigh,
    PF_PdLow,
    PF_AtrFrozen,
    PF_DayOpenPnL,
    PF_StopPts,
    PF_PendInvalid,   // the sweep extreme: a close beyond it kills the limit
    PF_PendLimit
};

// ============================================================================
SCSFExport scsf_Mizan_IOF_NQ(SCStudyInterfaceRef sc)
{
    SCSubgraphRef SG_ATR  = sc.Subgraph[0];
    SCSubgraphRef SG_ONH  = sc.Subgraph[1];
    SCSubgraphRef SG_ONL  = sc.Subgraph[2];
    SCSubgraphRef SG_PDH  = sc.Subgraph[3];
    SCSubgraphRef SG_PDL  = sc.Subgraph[4];
    SCSubgraphRef SG_LONG = sc.Subgraph[5];
    SCSubgraphRef SG_SHRT = sc.Subgraph[6];

    SCInputRef IN_LIVE       = sc.Input[0];
    SCInputRef IN_QTY        = sc.Input[1];
    SCInputRef IN_ON_START   = sc.Input[2];
    SCInputRef IN_RTH_OPEN   = sc.Input[3];
    SCInputRef IN_RTH_CLOSE  = sc.Input[4];
    SCInputRef IN_MANIP_END  = sc.Input[5];
    SCInputRef IN_FLAT_TIME  = sc.Input[6];
    SCInputRef IN_MIN_ON     = sc.Input[7];
    SCInputRef IN_MIN_RTH    = sc.Input[8];
    SCInputRef IN_USE_ON     = sc.Input[9];
    SCInputRef IN_USE_PD     = sc.Input[10];
    SCInputRef IN_SWEEP_EPS  = sc.Input[11];
    SCInputRef IN_CLOSE_POS  = sc.Input[12];
    SCInputRef IN_STOP_BUF   = sc.Input[13];
    SCInputRef IN_MIN_STOP   = sc.Input[14];
    SCInputRef IN_MAX_STOP   = sc.Input[15];
    SCInputRef IN_TARGET_R   = sc.Input[16];
    SCInputRef IN_DAILY_LOSS = sc.Input[17];
    SCInputRef IN_ATR_PER    = sc.Input[18];
    SCInputRef IN_LOG        = sc.Input[19];
    // Appended, never inserted: Sierra maps saved chart settings by input
    // INDEX, so renumbering 0-19 would silently re-point every existing chart.
    SCInputRef IN_ENTRY_MODE = sc.Input[20];
    SCInputRef IN_LIM_OFF    = sc.Input[21];
    SCInputRef IN_LIM_BARS   = sc.Input[22];

    if (sc.SetDefaults)
    {
        sc.GraphName = "Mizan IOF NQ - Frozen Level Sweep";
        sc.StudyDescription =
            "Sweep-and-reclaim of a frozen overnight or prior-day extreme. "
            "ONH/ONL/PDH/PDL and ATR freeze at 09:30; the first RTH bar to "
            "break one of them and close back inside is the setup. Entry is a "
            "limit 0.15xATR back toward the sweep, armed 3 bars (Entry Mode 0 "
            "for the market order at the next open, which is the configuration "
            "that was measured). Stop-market off the sweep extreme, ATR-clamped, "
            "resting limit at 2R. One setup per session, consumed by the sweep "
            "whether or not the limit fills. Needs a 24-hour chart session, and "
            "the bar-count gates assume 5-minute bars. NOT validated for a "
            "funded account - 5/6 on the ship gate, does not port to ES, no "
            "uncorrelated instrument, and the limit entry fails the placebo. "
            "Sim first.";

        sc.AutoLoop = 1;
        sc.GraphRegion = 0;
        sc.SendOrdersToTradeService = 0;      // IN_LIVE arms this at runtime
        sc.AllowOnlyOneTradePerBar = 1;
        sc.MaximumPositionAllowed = 1;
        sc.SupportReversals = 0;
        sc.CancelAllOrdersOnEntriesAndReversals = 0;
        sc.UseGUIAttachedOrderSetting = 0;
        sc.AllowMultipleEntriesInSameDirection = 0;

        SG_ATR.Name = "ATR";  SG_ATR.DrawStyle = DRAWSTYLE_IGNORE;
        SG_ONH.Name = "ONH";  SG_ONH.DrawStyle = DRAWSTYLE_DASH;
        SG_ONH.PrimaryColor = RGB(0, 172, 193);  SG_ONH.DrawZeros = 0;
        SG_ONL.Name = "ONL";  SG_ONL.DrawStyle = DRAWSTYLE_DASH;
        SG_ONL.PrimaryColor = RGB(0, 172, 193);  SG_ONL.DrawZeros = 0;
        SG_PDH.Name = "PDH";  SG_PDH.DrawStyle = DRAWSTYLE_DASH;
        SG_PDH.PrimaryColor = RGB(255, 165, 0);  SG_PDH.DrawZeros = 0;
        SG_PDL.Name = "PDL";  SG_PDL.DrawStyle = DRAWSTYLE_DASH;
        SG_PDL.PrimaryColor = RGB(255, 165, 0);  SG_PDL.DrawZeros = 0;
        SG_LONG.Name = "Reclaim Long";  SG_LONG.DrawStyle = DRAWSTYLE_POINT;
        SG_LONG.PrimaryColor = RGB(0, 200, 83);
        SG_LONG.LineWidth = 4;  SG_LONG.DrawZeros = 0;
        SG_SHRT.Name = "Reclaim Short"; SG_SHRT.DrawStyle = DRAWSTYLE_POINT;
        SG_SHRT.PrimaryColor = RGB(229, 115, 115);
        SG_SHRT.LineWidth = 4;  SG_SHRT.DrawZeros = 0;

        IN_LIVE.Name      = "Live Trading (0=sim)";        IN_LIVE.SetInt(0);
        IN_QTY.Name       = "Quantity";                    IN_QTY.SetInt(1);
        IN_ON_START.Name  = "Overnight Start HHMM";        IN_ON_START.SetInt(1800);
        IN_RTH_OPEN.Name  = "RTH Open HHMM";               IN_RTH_OPEN.SetInt(930);
        IN_RTH_CLOSE.Name = "RTH Close HHMM";              IN_RTH_CLOSE.SetInt(1600);
        IN_MANIP_END.Name = "Last Sweep HHMM";             IN_MANIP_END.SetInt(1200);
        IN_FLAT_TIME.Name = "Flatten HHMM";                IN_FLAT_TIME.SetInt(1555);
        IN_MIN_ON.Name    = "Min Overnight Bars";          IN_MIN_ON.SetInt(60);
        IN_MIN_RTH.Name   = "Min Prior RTH Bars";          IN_MIN_RTH.SetInt(40);
        IN_USE_ON.Name    = "Use ONH/ONL";                 IN_USE_ON.SetInt(1);
        IN_USE_PD.Name    = "Use PDH/PDL";                 IN_USE_PD.SetInt(1);
        IN_SWEEP_EPS.Name = "Sweep Depth (x ATR)";         IN_SWEEP_EPS.SetFloat(0.05f);
        IN_CLOSE_POS.Name = "Reclaim Close Position";      IN_CLOSE_POS.SetFloat(0.50f);
        IN_STOP_BUF.Name  = "Stop Buffer (x ATR)";         IN_STOP_BUF.SetFloat(0.15f);
        IN_MIN_STOP.Name  = "Min Stop (x ATR)";            IN_MIN_STOP.SetFloat(0.40f);
        IN_MAX_STOP.Name  = "Max Stop (x ATR)";            IN_MAX_STOP.SetFloat(2.50f);
        IN_TARGET_R.Name  = "Target R-multiple";           IN_TARGET_R.SetFloat(2.0f);
        // 0 = OFF, which is the configuration that was measured. See STATUS.
        IN_DAILY_LOSS.Name = "Daily Loss $ (0=off, UNTESTED)";
        IN_DAILY_LOSS.SetFloat(0.f);
        IN_ATR_PER.Name   = "ATR Period";                  IN_ATR_PER.SetInt(14);
        IN_LOG.Name       = "Log Level (0=off,1=trades,2=verbose)";
        IN_LOG.SetInt(1);
        // Mode 0 is the configuration STATUS was measured on and the one that
        // passes the placebo. Mode 1 is the default because it is what was
        // asked for, not because it is better evidenced. See THE ENTRY.
        IN_ENTRY_MODE.Name = "Entry (0=market next bar, 1=limit pullback)";
        IN_ENTRY_MODE.SetInt(1);
        IN_LIM_OFF.Name   = "Limit Offset (x ATR)";        IN_LIM_OFF.SetFloat(0.15f);
        IN_LIM_BARS.Name  = "Limit Armed Bars";            IN_LIM_BARS.SetInt(3);
        return;
    }

    // ------------------------------------------------------------ runtime --
    const int   Idx        = sc.Index;
    const float TICK       = sc.TickSize;
    const int   QTY        = IN_QTY.GetInt();
    const int   ON_START   = IN_ON_START.GetInt();
    const int   RTH_OPEN   = IN_RTH_OPEN.GetInt();
    const int   RTH_CLOSE  = IN_RTH_CLOSE.GetInt();
    const int   MANIP_END  = IN_MANIP_END.GetInt();
    const int   FLAT_TIME  = IN_FLAT_TIME.GetInt();
    const int   MIN_ON     = IN_MIN_ON.GetInt();
    const int   MIN_RTH    = IN_MIN_RTH.GetInt();
    const float DAILY_LOSS = IN_DAILY_LOSS.GetFloat();
    const int   LOG        = IN_LOG.GetInt();
    const int   USE_LIMIT  = (IN_ENTRY_MODE.GetInt() != 0);
    const float LIM_OFF    = IN_LIM_OFF.GetFloat();
    const int   LIM_BARS   = (IN_LIM_BARS.GetInt() > 0) ? IN_LIM_BARS.GetInt() : 1;

    sc.SendOrdersToTradeService = (IN_LIVE.GetInt() != 0);

    sc.ATR(sc.BaseData, SG_ATR, IN_ATR_PER.GetInt(), MOVAVGTYPE_WILDERS);

    int&   SessIdP    = sc.GetPersistentInt(PI_SessId);
    int&   LastBar    = sc.GetPersistentInt(PI_LastBar);
    int&   OnBars     = sc.GetPersistentInt(PI_OnBars);
    int&   RthBars    = sc.GetPersistentInt(PI_RthBars);
    int&   PrevRthBar = sc.GetPersistentInt(PI_PrevRthBars);
    int&   SetupTaken = sc.GetPersistentInt(PI_SetupTaken);
    int&   Locked     = sc.GetPersistentInt(PI_Locked);
    int&   Dir        = sc.GetPersistentInt(PI_Dir);
    int&   WarnedNoOn = sc.GetPersistentInt(PI_WarnedNoOn);
    int&   PendID     = sc.GetPersistentInt(PI_PendID);
    int&   PendBar    = sc.GetPersistentInt(PI_PendBar);
    int&   PendSide   = sc.GetPersistentInt(PI_PendSide);
    float& PendInv    = sc.GetPersistentFloat(PF_PendInvalid);
    float& PendLim    = sc.GetPersistentFloat(PF_PendLimit);
    float& OnHigh     = sc.GetPersistentFloat(PF_OnHigh);
    float& OnLow      = sc.GetPersistentFloat(PF_OnLow);
    float& RthHigh    = sc.GetPersistentFloat(PF_RthHigh);
    float& RthLow     = sc.GetPersistentFloat(PF_RthLow);
    float& PdHigh     = sc.GetPersistentFloat(PF_PdHigh);
    float& PdLow      = sc.GetPersistentFloat(PF_PdLow);
    float& AtrFrozen  = sc.GetPersistentFloat(PF_AtrFrozen);
    float& DayOpenPnL = sc.GetPersistentFloat(PF_DayOpenPnL);
    float& StopPts    = sc.GetPersistentFloat(PF_StopPts);

    if (Idx == 0)
    {
        SessIdP = -1;  LastBar = -1;  OnBars = 0;  RthBars = 0;
        PrevRthBar = 0;  SetupTaken = 0;  Locked = 0;  Dir = 0;
        WarnedNoOn = 0;
        PendID = 0;  PendBar = -1;  PendSide = 0;
        PendInv = 0.f;  PendLim = 0.f;
        OnHigh = -FLT_MAX;  OnLow = FLT_MAX;
        RthHigh = -FLT_MAX; RthLow = FLT_MAX;
        PdHigh = 0.f;  PdLow = 0.f;  AtrFrozen = 0.f;  StopPts = 0.f;
        DayOpenPnL = 0.f;
        return;
    }

    s_SCPositionData pos;
    sc.GetTradePosition(pos);
    const int   flat   = (pos.PositionQuantity == 0);
    const float dayPnL = (pos.CumulativeProfitLoss - DayOpenPnL) + pos.OpenProfitLoss;

    // A resting limit that has filled is no longer ours to cancel. Checked here
    // rather than only in the bar loop because the bar loop runs once per CLOSED
    // bar: a limit that fills and then hits its target inside the same 5-minute
    // bar would be flat again by the time the loop next looked, and would get
    // logged as expired or invalidated. The order state is what the trade log
    // gets audited from, so it has to be right.
    if (PendID != 0 && !flat) PendID = 0;

    // ── hard flatten. Intrabar on purpose: a time stop that waits for a bar
    //    close is not a time stop. ────────────────────────────────────────────
    {
        const int hhmmNow = HhmmFromSeconds(sc.BaseDateTimeIn[Idx].GetTimeInSeconds());
        const int atFlat  = (hhmmNow >= FLAT_TIME && hhmmNow < ON_START);
        if (atFlat && !flat)
        {
            sc.FlattenAndCancelAllOrders();
            if (LOG >= 1)
            {
                SCString m; m.Format("[MZ] FLAT_BY %d", hhmmNow);
                sc.AddMessageToLog(m, 0);
            }
        }
        if (DAILY_LOSS > 0.f && dayPnL <= -DAILY_LOSS)
        {
            if (!Locked)
            {
                Locked = 1;
                SCString m; m.Format("[MZ] day locked at %.0f", dayPnL);
                sc.AddMessageToLog(m, 0);
            }
            if (!flat) sc.FlattenAndCancelAllOrders();
        }
        // Belt and braces on an unfilled entry limit. The arm window makes this
        // unreachable at the defaults — a sweep must land by 12:00 and the order
        // lives 3 bars — but a widened MANIP_END or Limit Armed Bars would make
        // it reachable, and a resting entry order that outlives the flatten time
        // is the one way this study can put on a position it never intended.
        if (PendID != 0 && flat && (atFlat || Locked))
        {
            sc.CancelAllOrders();
            if (LOG >= 1)
            {
                SCString m; m.Format("[MZ] LIMIT CANCEL id=%d (%s)", PendID,
                                     atFlat ? "flat time" : "day locked");
                sc.AddMessageToLog(m, 0);
            }
            PendID = 0;
        }
    }

    // ========================================================================
    //  Walk every bar that has CLOSED and not yet been evaluated. Driving off
    //  an explicit high-water mark rather than sc.Index is what guarantees the
    //  overnight high/low accumulate over EVERY bar: a bar that is only ever
    //  visited while forming would otherwise never be counted, and the frozen
    //  levels would quietly be built from a subset of the session.
    // ========================================================================
    const int newest = Idx - 1;
    if (newest <= LastBar) return;
    const int first = (LastBar < 0) ? newest : LastBar + 1;

    for (int b = first; b <= newest; ++b)
    {
        const int hhmm   = HhmmFromSeconds(sc.BaseDateTimeIn[b].GetTimeInSeconds());
        const int isOn   = (hhmm >= ON_START || hhmm < RTH_OPEN);
        const int isRth  = (hhmm >= RTH_OPEN && hhmm < RTH_CLOSE);
        const int sessId = sc.BaseDateTimeIn[b].GetDate() + ((hhmm >= ON_START) ? 1 : 0);

        // ── session rollover ────────────────────────────────────────────────
        if (sessId != SessIdP)
        {
            // the session that just ended becomes the prior day
            if (RthBars > 0) { PdHigh = RthHigh; PdLow = RthLow; }
            PrevRthBar = RthBars;

            SessIdP    = sessId;
            OnBars     = 0;   RthBars = 0;
            OnHigh     = -FLT_MAX;  OnLow  = FLT_MAX;
            RthHigh    = -FLT_MAX;  RthLow = FLT_MAX;
            AtrFrozen  = 0.f;
            SetupTaken = 0;
            Locked     = 0;
            WarnedNoOn = 0;
            PendID     = 0;   PendBar = -1;  PendSide = 0;
            PendInv    = 0.f; PendLim = 0.f;
            DayOpenPnL = pos.CumulativeProfitLoss;
        }

        // ── accumulate the overnight range ──────────────────────────────────
        if (isOn)
        {
            if (sc.High[b] > OnHigh) OnHigh = sc.High[b];
            if (sc.Low[b]  < OnLow)  OnLow  = sc.Low[b];
            ++OnBars;
            LastBar = b;
            continue;
        }
        if (!isRth) { LastBar = b; continue; }      // 16:00-17:59 tail

        // ── first RTH bar: freeze ATR off the LAST OVERNIGHT bar ────────────
        if (RthBars == 0)
        {
            AtrFrozen = (b >= 1) ? SG_ATR[b - 1] : 0.f;
            if (LOG >= 2 && OnBars < MIN_ON && !WarnedNoOn)
            {
                WarnedNoOn = 1;
                SCString m;
                m.Format("[MZ] session %d skipped: %d overnight bars < %d "
                         "(is the chart a 24-hour session?)",
                         sessId, OnBars, MIN_ON);
                sc.AddMessageToLog(m, 0);
            }
        }
        if (sc.High[b] > RthHigh) RthHigh = sc.High[b];
        if (sc.Low[b]  < RthLow)  RthLow  = sc.Low[b];
        ++RthBars;
        LastBar = b;

        // ── draw the frozen levels across the RTH ───────────────────────────
        if (OnBars >= MIN_ON)
        {
            if (IN_USE_ON.GetInt()) { SG_ONH[b] = OnHigh; SG_ONL[b] = OnLow; }
            if (IN_USE_PD.GetInt() && PrevRthBar >= MIN_RTH)
            { SG_PDH[b] = PdHigh; SG_PDL[b] = PdLow; }
        }

        // ── a resting entry limit: filled, invalidated, or expired ──────────
        // Aged in CLOSED bars off the sweep bar, which is how the Python counts
        // the arm window. PendBar+1 .. PendBar+LIM_BARS are the bars the order
        // is live for, so cancelling at the close of PendBar+LIM_BARS retires it
        // after its last chance and before the bar that has none.
        //
        // The Python tests invalidation BEFORE the fill on the same bar, so a
        // bar that closes back through the sweep extreme voids a touch that
        // happened earlier inside it. A real resting order cannot be that
        // clever — intrabar it is simply filled. Live is marginally more
        // permissive than the measurement here and cannot be made otherwise.
        if (PendID != 0)
        {
            if (!flat)
            {
                PendID = 0;         // filled; the attached bracket owns it now
            }
            else
            {
                const char* why = 0;
                if ((PendSide > 0) ? (sc.Close[b] < PendInv)
                                   : (sc.Close[b] > PendInv))
                    why = "INVALIDATED";
                else if (b - PendBar >= LIM_BARS)
                    why = "EXPIRED";

                if (why)
                {
                    sc.CancelAllOrders();
                    if (LOG >= 1)
                    {
                        SCString m;
                        m.Format("[MZ] LIMIT %s id=%d  %s lim=%.2f sweep=%.2f  "
                                 "bar %d %04d (%d bars armed)",
                                 why, PendID,
                                 (PendSide > 0) ? "LONG" : "SHORT",
                                 PendLim, PendInv, b, hhmm, b - PendBar);
                        sc.AddMessageToLog(m, 0);
                    }
                    PendID = 0;
                }
            }
        }

        // ── entry gates ─────────────────────────────────────────────────────
        if (SetupTaken || Locked)           continue;
        if (!flat)                          continue;
        if (hhmm > MANIP_END)               continue;
        if (AtrFrozen <= 0.f)               continue;
        if (OnBars < MIN_ON)                continue;
        if (PrevRthBar < MIN_RTH && IN_USE_PD.GetInt()) continue;

        float downs[2], ups[2];
        int nd = 0, nu = 0;
        if (IN_USE_ON.GetInt()) { downs[nd++] = OnLow; ups[nu++] = OnHigh; }
        if (IN_USE_PD.GetInt()) { downs[nd++] = PdLow; ups[nu++] = PdHigh; }
        if (nd == 0 || nu == 0) continue;

        const float rng = sc.High[b] - sc.Low[b];
        if (rng <= 0.f) continue;
        const float cpos = (sc.Close[b] - sc.Low[b]) / rng;
        const float eps  = IN_SWEEP_EPS.GetFloat() * AtrFrozen;
        const float cp   = IN_CLOSE_POS.GetFloat();

        // Longs tested first, matching the reference implementation: on a bar
        // that sweeps both ends the tie has to break the same way or the two
        // stop measuring the same rule.
        int   side = 0;
        float lvl  = 0.f;
        if (cpos >= cp)
        {
            for (int i = 0; i < nd; ++i)
                if (sc.Low[b] < downs[i] - eps && sc.Close[b] > downs[i])
                    if (!side || downs[i] > lvl) { side = 1; lvl = downs[i]; }
        }
        if (!side && (1.f - cpos) >= cp)
        {
            for (int i = 0; i < nu; ++i)
                if (sc.High[b] > ups[i] + eps && sc.Close[b] < ups[i])
                    if (!side || ups[i] < lvl) { side = -1; lvl = ups[i]; }
        }
        if (!side) continue;

        // ── risk geometry ───────────────────────────────────────────────────
        // The stop rides the sweep extreme; the CLAMP is on the distance, and
        // the distance is what the bracket carries, so an out-of-band setup is
        // clamped rather than skipped. That is not this repo's usual convention
        // and it is deliberate: clamping is what was measured.
        const float invalid = (side > 0) ? sc.Low[b] : sc.High[b];

        // The limit sits LIM_OFF x ATR back toward the sweep extreme: under the
        // reclaim close for a long, over it for a short. The stop is then
        // measured from where the entry will actually be, not from a close the
        // order is resting under — market mode has no better proxy than the
        // close, but limit mode knows its own price, and the Python builds the
        // stop from the fill in both.
        const float limitPx  = TRound(sc.Close[b] - side * LIM_OFF * AtrFrozen,
                                      TICK);
        const float entryRef = USE_LIMIT ? limitPx : sc.Close[b];

        float sp = fabsf(entryRef - invalid)
                 + IN_STOP_BUF.GetFloat() * AtrFrozen;
        const float loStop = IN_MIN_STOP.GetFloat() * AtrFrozen;
        const float hiStop = IN_MAX_STOP.GetFloat() * AtrFrozen;
        if (sp < loStop) sp = loStop;
        if (sp > hiStop) sp = hiStop;
        sp = TRound(sp, TICK);
        if (sp <= 0.f) continue;
        const float tgt = TRound(sp * IN_TARGET_R.GetFloat(), TICK);

        // ── submit, bracket attached as OFFSETS from the real fill ──────────
        // Offsets, not absolute prices: the entry price is not known until the
        // parent fills — a market order is only proxied by this close, and a
        // limit can be improved by a gap through it — and pricing the bracket
        // off the proxy would leave the R-multiple drifting.
        //
        // Both exits stay as they were: Stop1Offset is a stop-market, so it
        // leaves at market once triggered, and Target1Offset is a resting limit
        // that fills at the target price or better. Only the entry is passive.
        s_SCNewOrder o;
        o.OrderQuantity = QTY;
        o.TimeInForce   = SCT_TIF_DAY;
        o.Stop1Offset   = sp;
        o.Target1Offset = tgt;
        if (USE_LIMIT)
        {
            o.OrderType = SCT_ORDERTYPE_LIMIT;
            o.Price1    = limitPx;
        }
        else
        {
            o.OrderType = SCT_ORDERTYPE_MARKET;
        }

        const int rc = (side > 0) ? (int)sc.BuyEntry(o) : (int)sc.SellEntry(o);
        if (rc <= 0)
        {
            // rc <= 0 means the order was never sent (-8999 = still downloading
            // historical data, for one). It is NOT a broker rejection, and it
            // must not silently consume the day: leaving SetupTaken clear lets
            // a later qualifying bar in the same window try again.
            if (LOG >= 1)
            {
                SCString m;
                m.Format("[MZ] ENTRY NOT SENT rc=%d  %s lvl=%.2f sp=%.2f",
                         rc, (side > 0) ? "LONG" : "SHORT", lvl, sp);
                sc.AddMessageToLog(m, 1);
            }
            continue;
        }

        // The SWEEP consumes the day, not the fill. A limit that expires
        // unfilled does NOT re-open the session for a second sweep — the
        // Python's scan breaks on the first sweep of the day whatever the entry
        // does afterwards, and a cpp that retried would be measuring a
        // different rule. The marker below therefore marks the SETUP; on a
        // limit it is drawn before anyone knows whether it fills.
        SetupTaken = 1;
        Dir        = side;
        StopPts    = sp;
        if (USE_LIMIT)
        {
            PendID   = rc;      // BuyEntry/SellEntry return the internal id
            PendBar  = b;
            PendSide = side;
            PendInv  = invalid;
            PendLim  = limitPx;
        }
        if (side > 0) SG_LONG[b] = sc.Low[b]  - AtrFrozen * 0.25f;
        else          SG_SHRT[b] = sc.High[b] + AtrFrozen * 0.25f;

        if (LOG >= 1)
        {
            const char* which =
                (side > 0) ? ((lvl == OnLow)  ? "ONL" : "PDL")
                           : ((lvl == OnHigh) ? "ONH" : "PDH");
            SCString m;
            if (USE_LIMIT)
                m.Format("[MZ SETUP] %-5s swept %s=%.2f  bar %d %04d  "
                         "sweep=%.2f close=%.2f  LIMIT %.2f armed %d bars  "
                         "stop=%.2fpt tgt=%.2fpt  ATR=%.2f  "
                         "ON=[%.2f,%.2f] PD=[%.2f,%.2f]",
                         (side > 0) ? "LONG" : "SHORT", which, lvl, b, hhmm,
                         invalid, sc.Close[b], limitPx, LIM_BARS, sp, tgt,
                         AtrFrozen, OnLow, OnHigh, PdLow, PdHigh);
            else
                m.Format("[MZ ENTRY] %-5s swept %s=%.2f  bar %d %04d  "
                         "sweep=%.2f close=%.2f  MARKET  "
                         "stop=%.2fpt tgt=%.2fpt  ATR=%.2f  "
                         "ON=[%.2f,%.2f] PD=[%.2f,%.2f]",
                         (side > 0) ? "LONG" : "SHORT", which, lvl, b, hhmm,
                         invalid, sc.Close[b], sp, tgt, AtrFrozen,
                         OnLow, OnHigh, PdLow, PdHigh);
            sc.AddMessageToLog(m, 0);
        }
    }
}
