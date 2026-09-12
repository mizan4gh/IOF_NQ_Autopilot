#if defined(__has_include) && __has_include("sierrachart.h")
#include "sierrachart.h"
#else
#include <cstdint>
#define SCDLLName(name)
#endif
#include <cmath>
#include <cfloat>

// SCDLLName must sit near the top so Sierra's remote build server finds it in
// its scan window (same placement as IOF_NQ_Autopilot.cpp).
SCDLLName("IOF_NQ_VWAPPullback")

// =============================================================================
//  IOF_NQ_VWAPPullback  --  NQ VWAP Pullback / Failed-Retest
//  Sierra Chart ACSIL study (C++), single file (Sierra remote build = 1 upload)
//
//  THE PATTERN (long; short is the exact mirror)
//    1. TREND      RTH price is >= MinSeparation above session VWAP, EMA20 is
//                  above EMA65, EMA65 slope is positive, and a completed bar
//                  has closed above VWAP.
//    2. PULLBACK   Price retraces into the VWAP proximity zone. It may touch or
//                  briefly penetrate VWAP within RetestTolerance. A completed
//                  close more than RetestTolerance BELOW VWAP kills the setup.
//    3. FAILED     A completed rejection bar touches/penetrates the zone, closes
//       RETEST     back above VWAP, closes in the upper half of its own range,
//                  and closes above its open.
//    4. CONFIRM    The next completed bar closes above the rejection bar high
//                  plus ConfirmBuffer.  -> enter.
//
//  DEFAULTS ARE NOT THE LITERAL SPECIFICATION -- read this before trading it.
//  The spec as written takes 3 trades in 387 contract-days: a stop beyond the
//  rejection-bar extreme, a fixed 100-tick target and a 1.5 minimum RR together
//  require a stop <= 16.67 points, and the median 5m NQ bar range is 19-48. So
//  the shipped defaults are the best-measured configuration instead:
//
//      Geometry Denomination = 1 (ATR multiples, not points)
//      Reference Level       = 1 (unweighted mean of typical price, not VWAP)
//      Target Mode           = 2 (2.0 R multiple, not fixed ticks)
//      Chart                 = 1-MINUTE bars
//
//  Set those four back to 0/0/0 and a 5m chart to run the specification.
//
//  This config is PRE-REGISTERED, NOT VALIDATED -- see
//  PREREG_vwap_pullback_tpavg.md. It is best-of-three on a bar interval and
//  target mode themselves chosen after their first settings failed; it has
//  never been run on held-out data, because none exists in the repo; and gold
//  is negative under it. sc.SendOrdersToTradeService stays 0 for that reason.
//
//  NOTHING HERE IS VALIDATED. This file implements the specification; it makes
//  no claim that the specification has an edge. Per this repo's own bar, a cpp
//  constant ships only after a 2+ independent contract A/B, and the re-sign
//  Monte-Carlo null (topdog_null_test.py) is what separates "the geometry made
//  money" from "the signal picked the side". Run backtest_vwap_pullback.py and
//  its null before this goes anywhere near a funded account.
//
//  TIMEZONE  All session inputs are compared against the CHART clock. This
//  repo convention is that the chart timezone IS US Eastern (see RTH_OPEN=935
//  in IOF_NQ_Autopilot.cpp). If your chart is not on ET, set "Chart-to-ET Offset
//  (minutes)" rather than editing the session times -- the offset is applied to
//  the bar clock before every session comparison, so the defaults keep meaning
//  09:30 / 15:00 / 15:45 ET.
// =============================================================================


// -----------------------------------------------------------------------------
// STATE MACHINE
//
// Exactly one setup is tracked at a time. The states are ordered: a setup only
// ever moves forward through them or falls back to IDLE/COOLDOWN. That ordering
// is what prevents two entries out of one pullback -- there is no path from
// POSITION_OPEN back to CONFIRMATION_DETECTED.
// -----------------------------------------------------------------------------
enum VPStateEnum
{
    VPS_IDLE                  = 0,
    VPS_TREND_ESTABLISHED     = 1,  // separation + alignment proven, no pullback yet
    VPS_PULLBACK_ACTIVE       = 2,  // price has entered the VWAP proximity zone
    VPS_REJECTION_DETECTED    = 3,  // the failed-retest bar has closed
    VPS_CONFIRMATION_DETECTED = 4,  // transient: the confirm bar closed this update
    VPS_ORDER_PENDING         = 5,  // entry order submitted, not yet filled
    VPS_POSITION_OPEN         = 6,
    VPS_COOLDOWN              = 7,
    VPS_DAILY_LOCKOUT         = 8
};

static const char* VP_StateName(int s)
{
    switch (s)
    {
        case VPS_IDLE:                  return "IDLE";
        case VPS_TREND_ESTABLISHED:     return "TREND_ESTABLISHED";
        case VPS_PULLBACK_ACTIVE:       return "PULLBACK_ACTIVE";
        case VPS_REJECTION_DETECTED:    return "REJECTION_DETECTED";
        case VPS_CONFIRMATION_DETECTED: return "CONFIRMATION_DETECTED";
        case VPS_ORDER_PENDING:         return "ORDER_PENDING";
        case VPS_POSITION_OPEN:         return "POSITION_OPEN";
        case VPS_COOLDOWN:              return "COOLDOWN";
        case VPS_DAILY_LOCKOUT:         return "DAILY_LOCKOUT";
        default:                        return "?";
    }
}

// Why a candidate did not become a trade. Every one of these is counted into the
// end-of-session funnel, so a study that takes no trades still says WHY.
enum VPRejectEnum
{
    VPR_NONE = 0,
    VPR_TREND_LOST,      // EMA alignment or slope failed mid-setup
    VPR_VWAP_BREAK,      // a completed bar closed decisively through VWAP
    VPR_EXPIRED,         // setup exceeded MaxSetupBars
    VPR_CONFIRM_TIMEOUT, // rejection bar never got its confirmation
    VPR_EMA_CHOP,        // EMA20/EMA65 too close together
    VPR_FLAT_SLOPE,      // EMA65 effectively flat
    VPR_LOW_ATR,         // ATR below minimum
    VPR_CROSSES,         // too many VWAP crosses in the lookback
    VPR_BIG_BAR,         // confirmation bar abnormally large
    VPR_WINDOW,          // confirmation landed outside the entry window
    VPR_MAX_TRADES,
    VPR_DAILY_LOSS,
    VPR_DAILY_PROFIT,
    VPR_COOLDOWN,
    VPR_IN_POSITION,
    VPR_RR,              // reward:risk below minimum
    VPR_STOP_RANGE,      // stop distance outside [MinStop, MaxStop]
    VPR_ORDER_FAILED,
    VPR_VOL_FILTER,      // optional volume filter
    VPR_DELTA_FILTER,    // optional delta filter
    VPR_COUNT
};

static const char* VP_RejectName(int r)
{
    switch (r)
    {
        case VPR_TREND_LOST:      return "TREND_LOST";
        case VPR_VWAP_BREAK:      return "VWAP_BREAK";
        case VPR_EXPIRED:         return "SETUP_EXPIRED";
        case VPR_CONFIRM_TIMEOUT: return "CONFIRM_TIMEOUT";
        case VPR_EMA_CHOP:        return "EMA_CHOP";
        case VPR_FLAT_SLOPE:      return "FLAT_SLOPE";
        case VPR_LOW_ATR:         return "LOW_ATR";
        case VPR_CROSSES:         return "VWAP_CHOP";
        case VPR_BIG_BAR:         return "BAR_TOO_LARGE";
        case VPR_WINDOW:          return "OUTSIDE_WINDOW";
        case VPR_MAX_TRADES:      return "MAX_TRADES";
        case VPR_DAILY_LOSS:      return "DAILY_LOSS";
        case VPR_DAILY_PROFIT:    return "DAILY_PROFIT";
        case VPR_COOLDOWN:        return "COOLDOWN";
        case VPR_IN_POSITION:     return "IN_POSITION";
        case VPR_RR:              return "RR_TOO_LOW";
        case VPR_STOP_RANGE:      return "STOP_OUT_OF_RANGE";
        case VPR_ORDER_FAILED:    return "ORDER_FAILED";
        case VPR_VOL_FILTER:      return "VOL_FILTER";
        case VPR_DELTA_FILTER:    return "DELTA_FILTER";
        default:                  return "NONE";
    }
}

// -----------------------------------------------------------------------------
// Order return codes. The -899x family are SCT_SKIPPED_* -- Sierra declined to
// submit and NOTHING reached the trade service. They are not broker rejections,
// and confusing the two costs a day of chasing a problem that never happened.
// -----------------------------------------------------------------------------
static const char* VP_OrderRCText(int rc)
{
    switch (rc)
    {
        case  -1:   return "SCTRADING_ORDER_ERROR (real submission failure)";
        case  -2:   return "NOT_OCO_ORDER_TYPE";
        case  -3:   return "ATTACHED_ORDER_OFFSET_NOT_SUPPORTED_WITH_MARKET_PARENT";
        case  -4:   return "UNSUPPORTED_ATTACHED_ORDER";
        case  -5:   return "SYMBOL_SETTINGS_NOT_FOUND";
        case  -6:   return "GENERAL_NULL_POINTER_ERROR";
        case  -8:   return "UNSUPPORTED_ORDER_TYPE";
        case  -9:   return "ERROR_SETTING_ORDER_PRICES";
        case -8999: return "SKIPPED_DOWNLOADING_HISTORICAL_DATA (not sent)";
        case -8998: return "SKIPPED_FULL_RECALC -- studies cannot trade on historical "
                           "bars. Use Replay, not a chart reload.";
        case -8997: return "SKIPPED_ONLY_ONE_TRADE_PER_BAR (not sent)";
        case -8996: return "SKIPPED_INVALID_INDEX_SPECIFIED (not sent)";
        case -8995: return "SKIPPED_TOO_MANY_NEW_BARS_DURING_UPDATE (not sent)";
        case -8994: return "SKIPPED_AUTO_TRADING_DISABLED -- enable Auto Trading "
                           "and/or Trade Simulation for this chart.";
        case 0:     return "no order returned";
        default:    return "unrecognised return code";
    }
}

// -----------------------------------------------------------------------------
// Persistent storage keys
// -----------------------------------------------------------------------------
enum VPPersistInt
{
    PI_SESSION_DATE   = 1,   // calendar date of the RTH session currently loaded
    PI_STATE          = 2,
    PI_SIDE           = 3,   // +1 long setup, -1 short setup, 0 none
    PI_SETUP_BAR      = 4,   // bar the setup started ageing from
    PI_REJ_BAR        = 5,
    PI_CONF_BAR       = 6,
    PI_ORDER_BAR      = 7,
    PI_ENTRY_ID       = 8,
    PI_STOP_ID        = 9,
    PI_TARGET_ID      = 10,
    PI_TRADES_TODAY   = 11,
    PI_HALTED         = 12,
    PI_HALT_REASON    = 13,
    PI_LAST_EXIT_BAR  = 14,
    PI_FLATTEN_SENT   = 15,
    PI_RTH_BARS       = 16,  // completed RTH bars in this session (warmup + anchoring)
    PI_LAST_ACC_BAR   = 17,  // last bar folded into the VWAP accumulators
    PI_SIDE_MASK      = 18,  // bit k = close of bar (i-k) was above VWAP
    PI_POS_QTY        = 19,  // last observed position quantity (fill detection)
    PI_BE_DONE        = 20,  // break-even stop already moved for this position
    PI_QTY_PENDING    = 21,  // quantity of the working entry order
    PI_RC_LOG_SENT    = 22,
    PI_LAST_RC        = 23,
    PI_ENTRY_BAR      = 24,
    PI_LAST_LOG_BAR   = 25,  // debug line is emitted at most once per bar

    // Funnel counters. Base + reason code, so one contiguous block.
    PI_DG_BASE        = 40,  // .. PI_DG_BASE + VPR_COUNT
    PI_DG_TREND       = 70,  // times TREND_ESTABLISHED was entered
    PI_DG_PULLBACK    = 71,
    PI_DG_REJECTION   = 72,
    PI_DG_CONFIRM     = 73,
    PI_DG_ORDERS      = 74,
    PI_DG_FILLS       = 75
};

enum VPPersistFloat
{
    PF_VWAP_PXVOL  = 1,   // sum(typical price * volume) since the RTH open
    PF_VWAP_VOL    = 2,   // sum(volume) since the RTH open
    PF_REJ_HIGH    = 3,
    PF_REJ_LOW     = 4,
    PF_CONF_HIGH   = 5,
    PF_CONF_LOW    = 6,
    PF_ENTRY_PX    = 7,
    PF_STOP_PX     = 8,
    PF_TARGET_PX   = 9,
    PF_EMA_FAST    = 10,  // session-anchored EMA carry (anchor mode 1 only)
    PF_EMA_SLOW    = 11,
    PF_ATR         = 12,
    PF_MFE         = 13,  // best price reached since the fill
    PF_RISK_PTS    = 14,
    PF_FROZEN_ATR  = 15,  // ATR captured at this session's RTH open
    PF_SESS_HI     = 16,  // running session high (level_mode 2 only)
    PF_SESS_LO     = 17
};

enum VPPersistDouble
{
    PD_DAY_START_CLOSED = 1,  // closed P/L snapshot at the RTH open
    PD_PREV_CLOSED      = 2   // previous closed P/L, to detect a completed trade
};


// -----------------------------------------------------------------------------
// Small helpers
// -----------------------------------------------------------------------------
static inline float VP_Abs(float x)          { return x < 0.f ? -x : x; }
static inline float VP_Max(float a, float b) { return a > b ? a : b; }
static inline float VP_Min(float a, float b) { return a < b ? a : b; }

// Count VWAP side changes inside the most recent Lookback completed bars.
// Mask bit k holds "close of bar (i-k) was above VWAP". A cross is any adjacent
// pair of bits that disagree, so the count is a popcount of mask XOR (mask>>1)
// over the first (Lookback-1) bit positions. Storing sides as a bitmask rather
// than an array is what keeps this O(1) per bar with zero allocation.
static int VP_CountCrosses(unsigned int Mask, int Lookback)
{
    if (Lookback < 2)
        return 0;
    if (Lookback > 32)
        Lookback = 32;

    const unsigned int Diff = (Mask ^ (Mask >> 1)) & ((Lookback >= 32)
                              ? 0x7FFFFFFFu
                              : ((1u << (Lookback - 1)) - 1u));
    int n = 0;
    for (unsigned int b = Diff; b != 0; b >>= 1)
        n += (int)(b & 1u);
    return n;
}

// Bar clock in seconds since midnight, shifted into Eastern Time. Every session
// comparison in this file goes through here so there is exactly one place where
// the timezone assumption lives.
static inline int VP_BarSecondsET(const SCDateTime& dt, int OffsetMinutes)
{
    int s = dt.GetTimeInSeconds() + OffsetMinutes * 60;
    while (s < 0)      s += 86400;
    while (s >= 86400) s -= 86400;
    return s;
}


/*==============================================================================
    STUDY ENTRY POINT
==============================================================================*/
SCSFExport scsf_IOF_NQ_VWAPPullback(SCStudyInterfaceRef sc)
{
    // ---- Subgraphs (chart display) ------------------------------------------
    SCSubgraphRef Sg_VWAP     = sc.Subgraph[0];
    SCSubgraphRef Sg_EMAFast  = sc.Subgraph[1];
    SCSubgraphRef Sg_EMASlow  = sc.Subgraph[2];
    SCSubgraphRef Sg_ATR      = sc.Subgraph[3];   // internal, DRAWSTYLE_IGNORE
    SCSubgraphRef Sg_Pullback = sc.Subgraph[4];
    SCSubgraphRef Sg_Rejection= sc.Subgraph[5];
    SCSubgraphRef Sg_ConfBuy  = sc.Subgraph[6];
    SCSubgraphRef Sg_ConfSell = sc.Subgraph[7];
    SCSubgraphRef Sg_Entry    = sc.Subgraph[8];
    SCSubgraphRef Sg_Stop     = sc.Subgraph[9];
    SCSubgraphRef Sg_Target   = sc.Subgraph[10];
    SCSubgraphRef Sg_ZoneUp   = sc.Subgraph[11];  // VWAP + proximity
    SCSubgraphRef Sg_ZoneDn   = sc.Subgraph[12];  // VWAP - proximity

    // ---- Inputs --------------------------------------------------------------
    SCInputRef In_RthOpen        = sc.Input[0];
    SCInputRef In_EntryEnd       = sc.Input[1];
    SCInputRef In_FlattenTime    = sc.Input[2];
    SCInputRef In_RthClose       = sc.Input[3];
    SCInputRef In_TzOffsetMin    = sc.Input[4];

    SCInputRef In_FastLen        = sc.Input[5];
    SCInputRef In_SlowLen        = sc.Input[6];
    SCInputRef In_AtrLen         = sc.Input[7];
    SCInputRef In_SlopeBars      = sc.Input[8];
    SCInputRef In_AnchorMode     = sc.Input[9];

    SCInputRef In_ProximityPts   = sc.Input[10];
    SCInputRef In_MinSepPts      = sc.Input[11];
    SCInputRef In_RetestTolPts   = sc.Input[12];
    SCInputRef In_ConfirmBufPts  = sc.Input[13];
    SCInputRef In_MinSlopePts    = sc.Input[14];
    SCInputRef In_MinEmaSepPts   = sc.Input[15];
    SCInputRef In_MinAtrPts      = sc.Input[16];
    SCInputRef In_MaxCrosses     = sc.Input[17];
    SCInputRef In_CrossLookback  = sc.Input[18];
    SCInputRef In_MaxBarAtrMult  = sc.Input[19];
    SCInputRef In_MaxSetupBars   = sc.Input[20];
    SCInputRef In_ConfirmWindow  = sc.Input[21];
    SCInputRef In_CloseHalfFrac  = sc.Input[22];

    SCInputRef In_UseVolFilter   = sc.Input[23];
    SCInputRef In_VolMult        = sc.Input[24];
    SCInputRef In_VolLookback    = sc.Input[25];
    SCInputRef In_UseDeltaFilter = sc.Input[26];
    SCInputRef In_MinDelta       = sc.Input[27];

    SCInputRef In_EntryMode      = sc.Input[28];
    SCInputRef In_StopMethod     = sc.Input[29];
    SCInputRef In_StopBufTicks   = sc.Input[30];
    SCInputRef In_FixedStopTicks = sc.Input[31];
    SCInputRef In_StopAtrMult    = sc.Input[32];
    SCInputRef In_TargetMode     = sc.Input[33];
    SCInputRef In_FixedTgtTicks  = sc.Input[34];
    SCInputRef In_TargetAtrMult  = sc.Input[35];
    SCInputRef In_TargetR        = sc.Input[36];
    SCInputRef In_MinRR          = sc.Input[37];
    SCInputRef In_MinStopTicks   = sc.Input[38];
    SCInputRef In_MaxStopTicks   = sc.Input[39];
    SCInputRef In_StopIncConfBar = sc.Input[40];

    SCInputRef In_Qty            = sc.Input[41];
    SCInputRef In_MaxTradesDay   = sc.Input[42];
    SCInputRef In_DailyLoss      = sc.Input[43];
    SCInputRef In_DailyProfit    = sc.Input[44];
    SCInputRef In_CooldownBars   = sc.Input[45];
    SCInputRef In_EntryOrderBars = sc.Input[46];

    SCInputRef In_UseBreakEven   = sc.Input[47];
    SCInputRef In_BeTriggerR     = sc.Input[48];
    SCInputRef In_BeOffsetTicks  = sc.Input[49];
    SCInputRef In_UseTrail       = sc.Input[50];
    SCInputRef In_TrailTriggerR  = sc.Input[51];
    SCInputRef In_TrailDistR     = sc.Input[52];

    SCInputRef In_Debug          = sc.Input[53];
    SCInputRef In_DrawLevels     = sc.Input[54];
    SCInputRef In_DrawLabels     = sc.Input[55];
    SCInputRef In_Diagnostics    = sc.Input[56];
    SCInputRef In_AnchorWarmBars = sc.Input[57];

    // ATR-normalised geometry. See the block comment above In_GeomMode.
    SCInputRef In_GeomMode       = sc.Input[58];
    SCInputRef In_ProximityATR   = sc.Input[59];
    SCInputRef In_MinSepATR      = sc.Input[60];
    SCInputRef In_RetestTolATR   = sc.Input[61];
    SCInputRef In_ConfirmBufATR  = sc.Input[62];
    SCInputRef In_MinSlopeATR    = sc.Input[63];
    SCInputRef In_MinEmaSepATR   = sc.Input[64];
    SCInputRef In_MinAtrBps      = sc.Input[65];
    SCInputRef In_StopBufATR     = sc.Input[66];
    SCInputRef In_MinStopATR     = sc.Input[67];
    SCInputRef In_MaxStopATR     = sc.Input[68];
    SCInputRef In_LevelMode      = sc.Input[69];

    // =========================================================================
    // DEFAULTS
    // =========================================================================
    if (sc.SetDefaults)
    {
        sc.GraphName        = "IOF NQ VWAP Pullback / Failed Retest";
        sc.StudyDescription = "RTH-only NQ trend-continuation system. Enters on a "
                              "failed retest of an intraday reference level in the "
                              "direction of the established trend. Defaults to the "
                              "best-measured config (ATR geometry, unweighted "
                              "typical-price mean, 2R target) on a 1-MINUTE chart, "
                              "NOT the literal spec. Pre-registered, not validated.";
        sc.GraphRegion      = 0;
        sc.AutoLoop         = 1;
        sc.FreeDLL          = 0;
        sc.CalculationPrecedence = LOW_PREC_LEVEL;
        sc.ValueFormat      = VALUEFORMAT_INHERITED;

        // --- Trading configuration ---
        // 0 = simulated. Set to 1 ONLY after a full Replay validation.
        sc.SendOrdersToTradeService                       = 0;
        sc.AllowMultipleEntriesInSameDirection            = 0;   // one position, ever
        sc.SupportReversals                               = 0;
        sc.AllowOppositeEntryWithOpposingPositionOrOrders = 0;
        sc.SupportAttachedOrdersForTrading                = 1;   // bracket on the entry
        sc.CancelAllOrdersOnEntriesAndReversals           = 1;
        sc.AllowEntryWithWorkingOrders                    = 0;   // blocks duplicate entries
        sc.CancelAllWorkingOrdersOnExit                   = 1;
        sc.AllowOnlyOneTradePerBar                        = 1;
        sc.MaintainTradeStatisticsAndTradesData           = 1;
        sc.MaximumPositionAllowed                         = 20;

        // --- Subgraph appearance ---
        Sg_VWAP.Name = "Session VWAP";
        Sg_VWAP.DrawStyle = DRAWSTYLE_LINE;
        Sg_VWAP.PrimaryColor = RGB(212, 168, 67);
        Sg_VWAP.LineWidth = 2;
        Sg_VWAP.DrawZeros = 0;

        Sg_EMAFast.Name = "EMA Fast";
        Sg_EMAFast.DrawStyle = DRAWSTYLE_LINE;
        Sg_EMAFast.PrimaryColor = RGB(0, 200, 255);
        Sg_EMAFast.LineWidth = 2;
        Sg_EMAFast.DrawZeros = 0;

        Sg_EMASlow.Name = "EMA Slow";
        Sg_EMASlow.DrawStyle = DRAWSTYLE_LINE;
        Sg_EMASlow.PrimaryColor = RGB(255, 180, 0);
        Sg_EMASlow.LineWidth = 2;
        Sg_EMASlow.DrawZeros = 0;

        Sg_ATR.Name = "ATR (internal)";
        Sg_ATR.DrawStyle = DRAWSTYLE_IGNORE;

        Sg_Pullback.Name = "Pullback";
        Sg_Pullback.DrawStyle = DRAWSTYLE_POINT;
        Sg_Pullback.PrimaryColor = RGB(150, 150, 255);
        Sg_Pullback.LineWidth = 4;
        Sg_Pullback.DrawZeros = 0;

        Sg_Rejection.Name = "Rejection";
        Sg_Rejection.DrawStyle = DRAWSTYLE_POINT;
        Sg_Rejection.PrimaryColor = RGB(255, 255, 120);
        Sg_Rejection.LineWidth = 6;
        Sg_Rejection.DrawZeros = 0;

        Sg_ConfBuy.Name = "Confirm Long";
        Sg_ConfBuy.DrawStyle = DRAWSTYLE_ARROW_UP;
        Sg_ConfBuy.PrimaryColor = RGB(0, 255, 0);
        Sg_ConfBuy.LineWidth = 3;
        Sg_ConfBuy.DrawZeros = 0;

        Sg_ConfSell.Name = "Confirm Short";
        Sg_ConfSell.DrawStyle = DRAWSTYLE_ARROW_DOWN;
        Sg_ConfSell.PrimaryColor = RGB(255, 60, 60);
        Sg_ConfSell.LineWidth = 3;
        Sg_ConfSell.DrawZeros = 0;

        Sg_Entry.Name = "Entry";
        Sg_Entry.DrawStyle = DRAWSTYLE_DASH;
        Sg_Entry.PrimaryColor = RGB(255, 255, 255);
        Sg_Entry.DrawZeros = 0;

        Sg_Stop.Name = "Stop";
        Sg_Stop.DrawStyle = DRAWSTYLE_DASH;
        Sg_Stop.PrimaryColor = RGB(220, 80, 80);
        Sg_Stop.DrawZeros = 0;

        Sg_Target.Name = "Target";
        Sg_Target.DrawStyle = DRAWSTYLE_DASH;
        Sg_Target.PrimaryColor = RGB(0, 200, 100);
        Sg_Target.DrawZeros = 0;

        Sg_ZoneUp.Name = "VWAP Zone Upper";
        Sg_ZoneUp.DrawStyle = DRAWSTYLE_IGNORE;
        Sg_ZoneUp.PrimaryColor = RGB(90, 90, 90);
        Sg_ZoneUp.DrawZeros = 0;

        Sg_ZoneDn.Name = "VWAP Zone Lower";
        Sg_ZoneDn.DrawStyle = DRAWSTYLE_IGNORE;
        Sg_ZoneDn.PrimaryColor = RGB(90, 90, 90);
        Sg_ZoneDn.DrawZeros = 0;

        // --- Session (all times are ET once the offset below is applied) ------
        In_RthOpen.Name = "RTH Open / VWAP Anchor (ET)";
        In_RthOpen.SetTime(HMS_TIME(9, 30, 0));

        In_EntryEnd.Name = "Last Entry Time (ET)";
        In_EntryEnd.SetTime(HMS_TIME(15, 0, 0));

        In_FlattenTime.Name = "Flatten All Positions At (ET)";
        In_FlattenTime.SetTime(HMS_TIME(15, 45, 0));

        In_RthClose.Name = "RTH Close / Stop Processing (ET)";
        In_RthClose.SetTime(HMS_TIME(16, 0, 0));

        In_TzOffsetMin.Name = "Chart-to-ET Offset (minutes)";
        // 0 assumes the chart is already on US Eastern, which is this repo's
        // convention. If the chart runs on exchange (CT) time, use +60.
        In_TzOffsetMin.SetInt(0);
        In_TzOffsetMin.SetIntLimits(-1440, 1440);

        // --- Indicators -------------------------------------------------------
        In_FastLen.Name = "EMA Fast Length";
        In_FastLen.SetInt(20);
        In_FastLen.SetIntLimits(2, 500);

        In_SlowLen.Name = "EMA Slow Length";
        In_SlowLen.SetInt(65);
        In_SlowLen.SetIntLimits(3, 1000);

        In_AtrLen.Name = "ATR Length (Wilder)";
        In_AtrLen.SetInt(14);
        In_AtrLen.SetIntLimits(2, 200);

        In_SlopeBars.Name = "EMA Slow Slope Lookback (bars)";
        In_SlopeBars.SetInt(5);
        In_SlopeBars.SetIntLimits(1, 200);

        In_AnchorMode.Name = "EMA/ATR Anchoring (0=Continuous, 1=Session)";
        // 0 by default, deliberately. A session-anchored EMA65 on a 5-minute
        // chart needs 325 minutes of RTH to warm up out of a 390-minute session,
        // so mode 1 leaves almost no tradable window and the first usable signal
        // lands after 14:55. Mode 0 carries overnight bars in the average but
        // never GENERATES a signal from one -- the entry window does that job.
        In_AnchorMode.SetInt(0);
        In_AnchorMode.SetIntLimits(0, 1);

        In_AnchorWarmBars.Name = "Session-Anchored Warmup (completed RTH bars)";
        // Only read in anchor mode 1. Requiring a full SlowLen of RTH history
        // would be 65 bars = 325 minutes on a 5m chart, so the first tradable
        // bar would land at 14:55 and the LAST-ENTRY-TIME gate would then reject
        // it -- an option that silently takes zero trades is a dead knob, not a
        // conservative setting. 30 bars is an explicit trade: a session-anchored
        // EMA65 is NOT converged at 30 bars, and you are choosing a usable
        // window over a converged average. Own that choice or use mode 0.
        In_AnchorWarmBars.SetInt(30);
        In_AnchorWarmBars.SetIntLimits(2, 500);

        // ---------------------------------------------------------------------
        // GEOMETRY DENOMINATION
        //
        // Mode 0 (points) is the original specification. It cannot leave NQ: a
        // 15-point separation is impossible on $70 crude, so CL/GC take zero
        // trades and the portability test comes back vacuous rather than
        // passed or failed. It also mis-scales WITHIN NQ, between a contract
        // trading at 20k and one at 30k.
        //
        // Mode 1 re-denominates every distance as a multiple of the ATR FROZEN
        // AT THE SESSION'S RTH OPEN. Each multiple below is exactly the
        // original point value divided by one reference number -- the pooled
        // median frozen-at-open 1-minute ATR over six NQ contracts,
        // ATR_ref = 9.8058 across 388 sessions. No multiple was fitted on its
        // own, which is what makes this a re-anchoring and not an 8-way sweep.
        //
        // MEASURED, AND THIS MATTERS: normalising the geometry KILLS the edge.
        // On NQ the re-sign null percentile falls from 99.0th (points) to
        // 93.5th, and leave-one-out from a worst of 95.5th to 72.5th. The
        // apparent edge in mode 0 was the points thresholds selecting a biased
        // subsample, not the failed-retest signal. Mode 1 is the HONEST
        // geometry and it does not clear the bar. Neither mode is shippable.
        // See VWAP_PULLBACK_README.md.
        // ---------------------------------------------------------------------
        In_GeomMode.Name = "Geometry Denomination (0=Points, 1=ATR Multiples)";
        In_GeomMode.SetInt(1);   // best-measured; see the DEFAULTS banner
        In_GeomMode.SetIntLimits(0, 1);

        // ---------------------------------------------------------------------
        // REFERENCE LEVEL
        //
        // 0 = session VWAP, the literal specification.
        // 1 = tpavg: the running UNWEIGHTED mean of typical price. Identical
        //     anchor and shape; volume simply does not vote.
        // 2 = mid: running (session high + session low) / 2. A pure geometric
        //     level -- the placebo.
        //
        // MEASURED (NQ, 1m, ATR geometry, 200-draw re-sign null, 6 contracts):
        //     level   net       null     worst LOO
        //     vwap    +$14,277  93.5th   72.5th  <- dies
        //     tpavg   +$20,055  99.0th   95.0th  <- default
        //     mid     -$202     54.0th   --      <- coinflip
        //
        // So the LEVEL is load-bearing (mid is a coinflip) but the VOLUME
        // WEIGHTING is not -- deleting it improves every measure. Mode 1 is the
        // default for that reason. Mode 2 exists to re-run the placebo.
        // ---------------------------------------------------------------------
        In_LevelMode.Name = "Reference Level (0=Session VWAP, 1=TypPrice Mean, 2=Range Mid)";
        In_LevelMode.SetInt(1);
        In_LevelMode.SetIntLimits(0, 2);

        In_ProximityATR.Name = "ATR mode: VWAP Proximity (x ATR)";
        In_ProximityATR.SetFloat(0.816f);
        In_ProximityATR.SetFloatLimits(0.01f, 20.0f);

        In_MinSepATR.Name = "ATR mode: Min VWAP Separation (x ATR)";
        In_MinSepATR.SetFloat(1.530f);
        In_MinSepATR.SetFloatLimits(0.01f, 50.0f);

        In_RetestTolATR.Name = "ATR mode: Retest Tolerance (x ATR)";
        In_RetestTolATR.SetFloat(0.408f);
        In_RetestTolATR.SetFloatLimits(0.0f, 20.0f);

        In_ConfirmBufATR.Name = "ATR mode: Confirmation Buffer (x ATR)";
        In_ConfirmBufATR.SetFloat(0.204f);
        In_ConfirmBufATR.SetFloatLimits(0.0f, 20.0f);

        In_MinSlopeATR.Name = "ATR mode: Min EMA Slow Slope (x ATR)";
        In_MinSlopeATR.SetFloat(0.102f);
        In_MinSlopeATR.SetFloatLimits(0.0f, 20.0f);

        In_MinEmaSepATR.Name = "ATR mode: Min EMA Fast/Slow Separation (x ATR)";
        In_MinEmaSepATR.SetFloat(0.204f);
        In_MinEmaSepATR.SetFloatLimits(0.0f, 20.0f);

        // A minimum-ATR filter cannot itself be an ATR multiple -- "atr >= k*atr"
        // is vacuous. Denominated in basis points of price instead. 3.23bps
        // reproduces the original 8 points at the NQ median price of 24,766.
        In_MinAtrBps.Name = "ATR mode: Min ATR (basis points of price)";
        In_MinAtrBps.SetFloat(3.23f);
        In_MinAtrBps.SetFloatLimits(0.0f, 1000.0f);

        // The stop bounds must be normalised too. Leaving them in ticks while
        // normalising the signal geometry would still reject every CL trade,
        // and the portability test would stay vacuous for a different reason.
        In_StopBufATR.Name = "ATR mode: Stop Buffer Beyond Extreme (x ATR)";
        In_StopBufATR.SetFloat(0.102f);
        In_StopBufATR.SetFloatLimits(0.0f, 20.0f);

        In_MinStopATR.Name = "ATR mode: Minimum Stop (x ATR)";
        In_MinStopATR.SetFloat(0.408f);
        In_MinStopATR.SetFloatLimits(0.01f, 20.0f);

        In_MaxStopATR.Name = "ATR mode: Maximum Stop (x ATR)";
        In_MaxStopATR.SetFloat(5.099f);
        In_MaxStopATR.SetFloatLimits(0.01f, 100.0f);

        // --- Geometry (all in NQ points) -------------------------------------
        In_ProximityPts.Name = "VWAP Proximity Zone (points)";
        In_ProximityPts.SetFloat(8.0f);
        In_ProximityPts.SetFloatLimits(0.25f, 500.0f);

        In_MinSepPts.Name = "Min VWAP Separation Before Pullback (points)";
        In_MinSepPts.SetFloat(15.0f);
        In_MinSepPts.SetFloatLimits(0.25f, 1000.0f);

        In_RetestTolPts.Name = "Retest Tolerance Through VWAP (points)";
        In_RetestTolPts.SetFloat(4.0f);
        In_RetestTolPts.SetFloatLimits(0.0f, 500.0f);

        In_ConfirmBufPts.Name = "Confirmation Buffer (points)";
        In_ConfirmBufPts.SetFloat(2.0f);
        In_ConfirmBufPts.SetFloatLimits(0.0f, 500.0f);

        In_MinSlopePts.Name = "Min EMA Slow Slope Over Lookback (points)";
        In_MinSlopePts.SetFloat(1.0f);
        In_MinSlopePts.SetFloatLimits(0.0f, 1000.0f);

        In_MinEmaSepPts.Name = "Min EMA Fast/Slow Separation (points)";
        In_MinEmaSepPts.SetFloat(2.0f);
        In_MinEmaSepPts.SetFloatLimits(0.0f, 1000.0f);

        In_MinAtrPts.Name = "Min ATR To Trade (points)";
        // NOTE: a points-denominated threshold does NOT port across index
        // levels. At NQ 20k vs 30k the same number selects a different
        // subsample. Re-derive it per contract, or normalise by ATR yourself.
        In_MinAtrPts.SetFloat(8.0f);
        In_MinAtrPts.SetFloatLimits(0.0f, 1000.0f);

        In_MaxCrosses.Name = "Max VWAP Crosses In Lookback (0 = off)";
        In_MaxCrosses.SetInt(4);
        In_MaxCrosses.SetIntLimits(0, 32);

        In_CrossLookback.Name = "VWAP Cross Lookback (bars, max 32)";
        In_CrossLookback.SetInt(20);
        In_CrossLookback.SetIntLimits(2, 32);

        In_MaxBarAtrMult.Name = "Max Confirmation Bar Range (x ATR, 0 = off)";
        In_MaxBarAtrMult.SetFloat(2.0f);
        In_MaxBarAtrMult.SetFloatLimits(0.0f, 50.0f);

        In_MaxSetupBars.Name = "Max Bars A Setup May Live";
        In_MaxSetupBars.SetInt(20);
        In_MaxSetupBars.SetIntLimits(2, 500);

        In_ConfirmWindow.Name = "Bars Allowed For Confirmation (1 = next bar only)";
        In_ConfirmWindow.SetInt(1);
        In_ConfirmWindow.SetIntLimits(1, 20);

        In_CloseHalfFrac.Name = "Min Close Position In Rejection Bar Range (0-1)";
        // 0.5 is the spec's "upper half". Exposed because it is the single
        // knob most likely to be swept -- and sweeping it is how a null result
        // gets turned into a fitted one. Leave it at 0.5 unless a cross-contract
        // A/B says otherwise.
        In_CloseHalfFrac.SetFloat(0.5f);
        In_CloseHalfFrac.SetFloatLimits(0.0f, 1.0f);

        // --- Optional order-flow filters (OFF by default) --------------------
        In_UseVolFilter.Name = "Use Volume Filter (0=No, 1=Yes)";
        In_UseVolFilter.SetInt(0);
        In_UseVolFilter.SetIntLimits(0, 1);

        In_VolMult.Name = "Confirmation Volume >= x Average";
        In_VolMult.SetFloat(1.2f);
        In_VolMult.SetFloatLimits(0.0f, 20.0f);

        In_VolLookback.Name = "Volume Average Lookback (bars)";
        In_VolLookback.SetInt(20);
        In_VolLookback.SetIntLimits(2, 200);

        In_UseDeltaFilter.Name = "Use Delta Filter (0=No, 1=Yes)";
        In_UseDeltaFilter.SetInt(0);
        In_UseDeltaFilter.SetIntLimits(0, 1);

        In_MinDelta.Name = "Min Signed Delta On Confirmation Bar";
        In_MinDelta.SetFloat(0.0f);
        In_MinDelta.SetFloatLimits(-1000000.0f, 1000000.0f);

        // --- Entry / exit -----------------------------------------------------
        In_EntryMode.Name = "Entry Mode (0=Market After Confirm, 1=Stop 1 Tick Beyond)";
        In_EntryMode.SetInt(0);
        In_EntryMode.SetIntLimits(0, 1);

        In_StopMethod.Name = "Stop Method (0=Rejection Extreme, 1=Fixed Ticks, 2=ATR)";
        In_StopMethod.SetInt(0);
        In_StopMethod.SetIntLimits(0, 2);

        In_StopBufTicks.Name = "Stop Buffer Beyond Extreme (ticks)";
        In_StopBufTicks.SetInt(4);
        In_StopBufTicks.SetIntLimits(0, 400);

        In_FixedStopTicks.Name = "Fixed Stop (ticks)";
        In_FixedStopTicks.SetInt(50);           // 50 ticks = 12.5 NQ points
        In_FixedStopTicks.SetIntLimits(1, 2000);

        In_StopAtrMult.Name = "ATR Stop Multiple";
        In_StopAtrMult.SetFloat(1.5f);
        In_StopAtrMult.SetFloatLimits(0.1f, 20.0f);

        In_TargetMode.Name = "Target Mode (0=Fixed Ticks, 1=ATR, 2=R Multiple)";
        In_TargetMode.SetInt(2);   // fixed-tick target cannot satisfy MinRR
        In_TargetMode.SetIntLimits(0, 2);

        In_FixedTgtTicks.Name = "Fixed Target (ticks)";
        In_FixedTgtTicks.SetInt(100);           // 100 ticks = 25 NQ points
        In_FixedTgtTicks.SetIntLimits(1, 5000);

        In_TargetAtrMult.Name = "ATR Target Multiple";
        In_TargetAtrMult.SetFloat(3.0f);
        In_TargetAtrMult.SetFloatLimits(0.1f, 50.0f);

        In_TargetR.Name = "Target R Multiple";
        In_TargetR.SetFloat(2.0f);
        In_TargetR.SetFloatLimits(0.1f, 50.0f);

        In_MinRR.Name = "Minimum Reward:Risk";
        In_MinRR.SetFloat(1.5f);
        In_MinRR.SetFloatLimits(0.0f, 50.0f);

        In_MinStopTicks.Name = "Minimum Stop (ticks)";
        In_MinStopTicks.SetInt(16);             // 4 NQ points
        In_MinStopTicks.SetIntLimits(1, 2000);

        In_MaxStopTicks.Name = "Maximum Stop (ticks)";
        In_MaxStopTicks.SetInt(200);            // 50 NQ points
        In_MaxStopTicks.SetIntLimits(1, 5000);

        In_StopIncConfBar.Name = "Stop Also Clears Confirmation Bar (0=No, 1=Yes)";
        // 1 by default. The spec anchors the stop on the rejection bar extreme,
        // but the confirmation bar can print a lower low than the rejection bar
        // on the way up; anchoring only on the rejection bar would then place
        // the stop INSIDE the bar we entered on.
        In_StopIncConfBar.SetInt(1);
        In_StopIncConfBar.SetIntLimits(0, 1);

        // --- Risk governors ---------------------------------------------------
        In_Qty.Name = "Order Quantity (contracts)";
        In_Qty.SetInt(1);
        In_Qty.SetIntLimits(1, 100);

        In_MaxTradesDay.Name = "Max Trades Per Day (0 = off)";
        In_MaxTradesDay.SetInt(4);
        In_MaxTradesDay.SetIntLimits(0, 100);

        In_DailyLoss.Name = "Daily Loss Limit ($, positive, 0 = off)";
        In_DailyLoss.SetFloat(800.0f);
        In_DailyLoss.SetFloatLimits(0.0f, 1000000.0f);

        In_DailyProfit.Name = "Daily Profit Lockout ($, 0 = off)";
        In_DailyProfit.SetFloat(1000.0f);
        In_DailyProfit.SetFloatLimits(0.0f, 1000000.0f);

        In_CooldownBars.Name = "Cooldown After A Closed Trade (bars)";
        In_CooldownBars.SetInt(8);
        In_CooldownBars.SetIntLimits(0, 500);

        In_EntryOrderBars.Name = "Cancel Unfilled Entry Order After (bars)";
        In_EntryOrderBars.SetInt(2);
        In_EntryOrderBars.SetIntLimits(1, 50);

        // --- Optional trade management (OFF by default) ----------------------
        In_UseBreakEven.Name = "Use Break-Even Stop (0=No, 1=Yes)";
        In_UseBreakEven.SetInt(0);
        In_UseBreakEven.SetIntLimits(0, 1);

        In_BeTriggerR.Name = "Break-Even Trigger (R multiple)";
        In_BeTriggerR.SetFloat(1.0f);
        In_BeTriggerR.SetFloatLimits(0.1f, 20.0f);

        In_BeOffsetTicks.Name = "Break-Even Offset Beyond Entry (ticks)";
        In_BeOffsetTicks.SetInt(2);
        In_BeOffsetTicks.SetIntLimits(0, 200);

        In_UseTrail.Name = "Use Trailing Stop (0=No, 1=Yes)";
        In_UseTrail.SetInt(0);
        In_UseTrail.SetIntLimits(0, 1);

        In_TrailTriggerR.Name = "Trailing Stop Trigger (R multiple)";
        In_TrailTriggerR.SetFloat(1.5f);
        In_TrailTriggerR.SetFloatLimits(0.1f, 20.0f);

        In_TrailDistR.Name = "Trailing Stop Distance (R multiple)";
        In_TrailDistR.SetFloat(1.0f);
        In_TrailDistR.SetFloatLimits(0.1f, 20.0f);

        // --- Output ------------------------------------------------------------
        In_Debug.Name = "Debug Log (0=Off, 1=Transitions, 2=Every Bar)";
        In_Debug.SetInt(1);
        In_Debug.SetIntLimits(0, 2);

        In_DrawLevels.Name = "Draw Entry/Stop/Target Levels (0=No, 1=Yes)";
        In_DrawLevels.SetInt(1);
        In_DrawLevels.SetIntLimits(0, 1);

        In_DrawLabels.Name = "Draw Rejection-Reason Labels (0=No, 1=Yes)";
        In_DrawLabels.SetInt(1);
        In_DrawLabels.SetIntLimits(0, 1);

        In_Diagnostics.Name = "End-Of-Session Funnel Line (0=No, 1=Yes)";
        In_Diagnostics.SetInt(1);
        In_Diagnostics.SetIntLimits(0, 1);

        return;
    }

    // =========================================================================
    // PER-BAR PROCESSING
    // =========================================================================
    const int i = sc.Index;

    const int FastLen   = In_FastLen.GetInt();
    const int SlowLen   = In_SlowLen.GetInt();
    const int AtrLen    = In_AtrLen.GetInt();
    const int SlopeBars = In_SlopeBars.GetInt();
    const int AnchorMode= In_AnchorMode.GetInt();

    const float Tick    = (float)sc.TickSize;
    // Dollars per point, derived from the symbol rather than hard-coded, so the
    // same study prices risk correctly on NQ ($20) and MNQ ($2).
    const float PtValue = (Tick > 0.f) ? (float)(sc.CurrencyValuePerTick / sc.TickSize) : 0.f;

    // ---- Indicators are computed on EVERY bar --------------------------------
    // Continuous mode must see overnight bars or the averages would have holes.
    // Signal generation is gated separately by the RTH entry window, so nothing
    // here can produce a trade outside the session.
    if (AnchorMode == 0)
    {
        sc.ExponentialMovAvg(sc.BaseDataIn[SC_LAST], Sg_EMAFast, i, FastLen);
        sc.ExponentialMovAvg(sc.BaseDataIn[SC_LAST], Sg_EMASlow, i, SlowLen);
        sc.ATR(sc.BaseDataIn, Sg_ATR, i, AtrLen, MOVAVGTYPE_WILDERS);
    }

    if (i < 1)
        return;

    // ---- Time context --------------------------------------------------------
    const int TzOff     = In_TzOffsetMin.GetInt();
    const int BarSec    = VP_BarSecondsET(sc.BaseDateTimeIn[i], TzOff);
    const int RthOpen   = In_RthOpen.GetTime();
    const int EntryEnd  = In_EntryEnd.GetTime();
    const int FlattenAt = In_FlattenTime.GetTime();
    const int RthClose  = In_RthClose.GetTime();

    // Bars strictly inside the RTH window. Overnight bars are ignored for the
    // session VWAP, for the state machine, and for every signal test.
    const bool InRth = (BarSec >= RthOpen) && (BarSec < RthClose);

    // ---- Persistent references ----------------------------------------------
    int& r_SessionDate = sc.GetPersistentInt(PI_SESSION_DATE);
    int& r_State       = sc.GetPersistentInt(PI_STATE);
    int& r_Side        = sc.GetPersistentInt(PI_SIDE);
    int& r_SetupBar    = sc.GetPersistentInt(PI_SETUP_BAR);
    int& r_RejBar      = sc.GetPersistentInt(PI_REJ_BAR);
    int& r_ConfBar     = sc.GetPersistentInt(PI_CONF_BAR);
    int& r_OrderBar    = sc.GetPersistentInt(PI_ORDER_BAR);
    int& r_EntryID     = sc.GetPersistentInt(PI_ENTRY_ID);
    int& r_StopID      = sc.GetPersistentInt(PI_STOP_ID);
    int& r_TargetID    = sc.GetPersistentInt(PI_TARGET_ID);
    int& r_Trades      = sc.GetPersistentInt(PI_TRADES_TODAY);
    int& r_Halted      = sc.GetPersistentInt(PI_HALTED);
    int& r_HaltReason  = sc.GetPersistentInt(PI_HALT_REASON);
    int& r_LastExitBar = sc.GetPersistentInt(PI_LAST_EXIT_BAR);
    int& r_FlattenSent = sc.GetPersistentInt(PI_FLATTEN_SENT);
    int& r_RthBars     = sc.GetPersistentInt(PI_RTH_BARS);
    int& r_LastAccBar  = sc.GetPersistentInt(PI_LAST_ACC_BAR);
    int& r_SideMask    = sc.GetPersistentInt(PI_SIDE_MASK);
    int& r_PosQty      = sc.GetPersistentInt(PI_POS_QTY);
    int& r_BeDone      = sc.GetPersistentInt(PI_BE_DONE);
    int& r_QtyPending  = sc.GetPersistentInt(PI_QTY_PENDING);
    int& r_EntryBar    = sc.GetPersistentInt(PI_ENTRY_BAR);
    int& r_LastLogBar  = sc.GetPersistentInt(PI_LAST_LOG_BAR);

    float& r_PxVol     = sc.GetPersistentFloat(PF_VWAP_PXVOL);
    float& r_Vol       = sc.GetPersistentFloat(PF_VWAP_VOL);
    float& r_RejHigh   = sc.GetPersistentFloat(PF_REJ_HIGH);
    float& r_RejLow    = sc.GetPersistentFloat(PF_REJ_LOW);
    float& r_ConfHigh  = sc.GetPersistentFloat(PF_CONF_HIGH);
    float& r_ConfLow   = sc.GetPersistentFloat(PF_CONF_LOW);
    float& r_EntryPx   = sc.GetPersistentFloat(PF_ENTRY_PX);
    float& r_StopPx    = sc.GetPersistentFloat(PF_STOP_PX);
    float& r_TargetPx  = sc.GetPersistentFloat(PF_TARGET_PX);
    float& r_EmaFast   = sc.GetPersistentFloat(PF_EMA_FAST);
    float& r_EmaSlow   = sc.GetPersistentFloat(PF_EMA_SLOW);
    float& r_AtrCarry  = sc.GetPersistentFloat(PF_ATR);
    float& r_Mfe       = sc.GetPersistentFloat(PF_MFE);
    float& r_RiskPts   = sc.GetPersistentFloat(PF_RISK_PTS);
    float& r_FrozenAtr = sc.GetPersistentFloat(PF_FROZEN_ATR);
    float& r_SessHi    = sc.GetPersistentFloat(PF_SESS_HI);
    float& r_SessLo    = sc.GetPersistentFloat(PF_SESS_LO);

    double& r_DayStartClosed = sc.GetPersistentDouble(PD_DAY_START_CLOSED);
    double& r_PrevClosed     = sc.GetPersistentDouble(PD_PREV_CLOSED);

    // ---- Position and P/L ----------------------------------------------------
    s_SCPositionData Pos;
    sc.GetTradePosition(Pos);

    n_ACSIL::s_TradeStatistics DailyStats;
    sc.GetTradeStatisticsForSymbolV2(n_ACSIL::STATS_TYPE_DAILY_ALL_TRADES, DailyStats);

    const double ClosedRaw  = DailyStats.ClosedTradesProfitLoss;
    const double OpenPL     = Pos.OpenProfitLoss;
    const int    PositionQty= (int)Pos.PositionQuantity;

    // =========================================================================
    // SESSION RESET
    //
    // Everything daily resets at the RTH open, not at the exchange trading-day
    // rollover. That is what the spec asks for, and it also means the 18:00 CME
    // rollover cannot silently clear a lockout mid-evening.
    // =========================================================================
    const int BarDate = sc.BaseDateTimeIn[i].GetDate();

    if (InRth && BarDate != r_SessionDate)
    {
        // Emit the funnel for the session that just ended, before it is cleared.
        if (In_Diagnostics.GetInt() && r_SessionDate != 0)
        {
            SCString Funnel;
            Funnel.Format("VWAPPB DIAG session %d | trend %d -> pullback %d -> rejection %d "
                          "-> confirm %d -> orders %d -> fills %d | trades %d",
                          r_SessionDate,
                          sc.GetPersistentInt(PI_DG_TREND),
                          sc.GetPersistentInt(PI_DG_PULLBACK),
                          sc.GetPersistentInt(PI_DG_REJECTION),
                          sc.GetPersistentInt(PI_DG_CONFIRM),
                          sc.GetPersistentInt(PI_DG_ORDERS),
                          sc.GetPersistentInt(PI_DG_FILLS),
                          r_Trades);
            sc.AddMessageToLog(Funnel, 0);

            SCString Reasons = "VWAPPB DIAG rejections:";
            for (int rr = 1; rr < VPR_COUNT; ++rr)
            {
                const int c = sc.GetPersistentInt(PI_DG_BASE + rr);
                if (c > 0)
                {
                    SCString One;
                    One.Format(" %s=%d", VP_RejectName(rr), c);
                    Reasons += One;
                }
            }
            sc.AddMessageToLog(Reasons, 0);
        }

        r_SessionDate = BarDate;

        // VWAP accumulators start empty: the session VWAP is anchored at 09:30
        // and carries no overnight volume whatsoever.
        r_PxVol = 0.f;
        r_Vol   = 0.f;

        r_State       = VPS_IDLE;
        r_Side        = 0;
        r_SetupBar    = -1;
        r_RejBar      = -1;
        r_ConfBar     = -1;
        r_OrderBar    = -1;
        r_EntryID     = 0;
        r_StopID      = 0;
        r_TargetID    = 0;
        r_Trades      = 0;
        r_Halted      = 0;
        r_HaltReason  = 0;
        r_LastExitBar = -100000;
        r_FlattenSent = 0;
        r_RthBars     = 0;
        r_LastAccBar  = -1;
        r_SideMask    = 0;
        r_PosQty      = PositionQty;
        r_BeDone      = 0;
        r_QtyPending  = 0;
        r_EntryBar    = -1;
        r_RejHigh = r_RejLow = r_ConfHigh = r_ConfLow = 0.f;
        r_EntryPx = r_StopPx = r_TargetPx = 0.f;
        r_EmaFast = r_EmaSlow = r_AtrCarry = 0.f;
        r_Mfe = r_RiskPts = 0.f;

        // Freeze this session's ATR at the RTH open, from the last bar BEFORE
        // the open. Frozen, not live: a threshold that tracked the session's
        // own volatility would tighten exactly as the market got busy, and the
        // same historical bar would mean different things on a reload. In
        // session-anchored mode there is no pre-open ATR, so it is captured
        // from the first warm RTH bar instead (see below).
        r_FrozenAtr = 0.f;
        r_SessHi = 0.f;
        r_SessLo = 0.f;
        if (AnchorMode == 0 && i > 0)
        {
            const float PreOpenAtr = Sg_ATR[i - 1];
            if (PreOpenAtr > 0.f)
                r_FrozenAtr = PreOpenAtr;
        }

        // Baseline the P/L against whatever the platform reports right now, so
        // this study measures ITS session regardless of when Sierra's own daily
        // statistics happen to roll over.
        r_DayStartClosed = ClosedRaw;
        r_PrevClosed     = ClosedRaw;

        sc.GetPersistentInt(PI_RC_LOG_SENT) = 0;
        sc.GetPersistentInt(PI_LAST_RC)     = 0;
        sc.GetPersistentInt(PI_DG_TREND)     = 0;
        sc.GetPersistentInt(PI_DG_PULLBACK)  = 0;
        sc.GetPersistentInt(PI_DG_REJECTION) = 0;
        sc.GetPersistentInt(PI_DG_CONFIRM)   = 0;
        sc.GetPersistentInt(PI_DG_ORDERS)    = 0;
        sc.GetPersistentInt(PI_DG_FILLS)     = 0;
        for (int rr = 0; rr < VPR_COUNT; ++rr)
            sc.GetPersistentInt(PI_DG_BASE + rr) = 0;
    }

    if (!InRth)
        return;   // overnight bars contribute nothing: no VWAP, no state, no orders

    const double SessionClosedPL = ClosedRaw - r_DayStartClosed;
    const double SessionPL       = SessionClosedPL + OpenPL;

    // =========================================================================
    // SESSION VWAP
    //
    // Folded in exactly once per bar, on the bar's close, and never re-added on
    // a repeated update of the same bar. Getting this wrong double-counts volume
    // and drags VWAP toward the most-updated bar.
    // =========================================================================
    const bool BarClosed = (sc.GetBarHasClosedStatus(i) == BHCS_BAR_HAS_CLOSED);

    const float O = sc.BaseDataIn[SC_OPEN][i];
    const float H = sc.BaseDataIn[SC_HIGH][i];
    const float L = sc.BaseDataIn[SC_LOW][i];
    const float C = sc.BaseDataIn[SC_LAST][i];
    const float V = sc.BaseDataIn[SC_VOLUME][i];

    // The reference level. Mode 0 weights each bar by its volume (VWAP); mode 1
    // weights every bar equally (tpavg); mode 2 ignores price-time entirely and
    // takes the running mid of the session range. Modes 0 and 1 share the same
    // two accumulators -- the ONLY difference is the weight, which is exactly
    // the claim the placebo ladder tests.
    const int LevelMode = In_LevelMode.GetInt();

    if (BarClosed && i > r_LastAccBar)
    {
        const float Tp = (H + L + C) / 3.0f;
        if (LevelMode == 1)
        {
            r_PxVol += Tp;          // weight 1 per bar: volume does not vote
            r_Vol   += 1.0f;
        }
        else if (V > 0.f)
        {
            r_PxVol += Tp * V;
            r_Vol   += V;
        }

        if (r_SessHi <= 0.f) { r_SessHi = H; r_SessLo = L; }
        else { r_SessHi = VP_Max(r_SessHi, H); r_SessLo = VP_Min(r_SessLo, L); }

        r_LastAccBar = i;
        r_RthBars   += 1;
    }

    // The signal level uses only what has been folded in from CLOSED bars. The
    // chart line additionally blends the forming bar so the plot does not lag,
    // but the state machine never reads that value -- that separation is the
    // no-repaint guarantee for the level.
    float VwapSignal;
    if (LevelMode == 2)
        VwapSignal = (r_SessHi > 0.f) ? ((r_SessHi + r_SessLo) * 0.5f) : C;
    else
        VwapSignal = (r_Vol > 0.f) ? (r_PxVol / r_Vol) : C;

    float VwapDisplay = VwapSignal;
    if (!BarClosed)
    {
        const float Tp = (H + L + C) / 3.0f;
        if (LevelMode == 2)
        {
            const float hi = (r_SessHi > 0.f) ? VP_Max(r_SessHi, H) : H;
            const float lo = (r_SessHi > 0.f) ? VP_Min(r_SessLo, L) : L;
            VwapDisplay = (hi + lo) * 0.5f;
        }
        else if (LevelMode == 1)
        {
            VwapDisplay = (r_PxVol + Tp) / (r_Vol + 1.0f);
        }
        else if (V > 0.f)
        {
            const float dv = r_Vol + V;
            if (dv > 0.f)
                VwapDisplay = (r_PxVol + Tp * V) / dv;
        }
    }

    Sg_VWAP[i]   = VwapDisplay;
    // The plotted zone must use the geometry that is actually active, or the
    // chart shows one rule while the state machine applies another.
    const float ZoneHalf = (In_GeomMode.GetInt() == 1 && r_FrozenAtr > 0.f)
                         ? (In_ProximityATR.GetFloat() * r_FrozenAtr)
                         : In_ProximityPts.GetFloat();
    Sg_ZoneUp[i] = VwapDisplay + ZoneHalf;
    Sg_ZoneDn[i] = VwapDisplay - ZoneHalf;

    // =========================================================================
    // SESSION-ANCHORED EMA / ATR (anchor mode 1 only)
    //
    // Recursive, seeded on the first completed RTH bar, so no overnight price
    // ever enters the averages. Written into the same subgraphs as the
    // continuous mode so every downstream lookback is identical.
    // =========================================================================
    if (AnchorMode == 1)
    {
        if (BarClosed && i == r_LastAccBar)
        {
            if (r_RthBars <= 1)
            {
                r_EmaFast  = C;
                r_EmaSlow  = C;
                r_AtrCarry = H - L;
            }
            else
            {
                const float kF = 2.0f / (FastLen + 1.0f);
                const float kS = 2.0f / (SlowLen + 1.0f);
                r_EmaFast += kF * (C - r_EmaFast);
                r_EmaSlow += kS * (C - r_EmaSlow);

                const float PC = sc.BaseDataIn[SC_LAST][i - 1];
                const float TR = VP_Max(H - L, VP_Max(VP_Abs(H - PC), VP_Abs(L - PC)));
                // Wilder smoothing: RMA with alpha = 1/AtrLen.
                r_AtrCarry += (TR - r_AtrCarry) / (float)AtrLen;
            }
        }
        Sg_EMAFast[i] = r_EmaFast;
        Sg_EMASlow[i] = r_EmaSlow;
        Sg_ATR[i]     = r_AtrCarry;
    }

    // =========================================================================
    // GOVERNORS AND SESSION FLATTEN
    //
    // These run on every update, ahead of any signal logic, so a limit that has
    // already been breached can never be undone by a later signal.
    // =========================================================================
    const float DailyLoss   = In_DailyLoss.GetFloat();
    const float DailyProfit = In_DailyProfit.GetFloat();

    if (DailyLoss > 0.f && SessionPL <= -(double)DailyLoss)
    {
        if (!r_Halted) { r_Halted = 1; r_HaltReason = VPR_DAILY_LOSS; }
    }
    else if (DailyProfit > 0.f && SessionPL >= (double)DailyProfit)
    {
        if (!r_Halted) { r_Halted = 1; r_HaltReason = VPR_DAILY_PROFIT; }
    }

    if (In_MaxTradesDay.GetInt() > 0 && r_Trades >= In_MaxTradesDay.GetInt())
    {
        if (!r_Halted) { r_Halted = 1; r_HaltReason = VPR_MAX_TRADES; }
    }

    // The session flatten is the ONLY unconditional exit. The daily governors
    // deliberately do not flatten: an open position is already bracketed, and
    // tearing it out mid-trade turns a protective stop into a market exit at
    // whatever the tape happens to be. Governors block NEW entries; the bracket
    // and this clock handle the exit.
    if (BarSec >= FlattenAt)
    {
        if (PositionQty != 0 && !r_FlattenSent)
        {
            r_FlattenSent = 1;
            sc.FlattenAndCancelAllOrders();
            SCString M;
            M.Format("VWAPPB FLATTEN session end | pos %d | session P/L %.2f",
                     PositionQty, SessionPL);
            sc.AddMessageToLog(M, 0);
        }
        else if (PositionQty == 0 && !r_FlattenSent && r_EntryID != 0)
        {
            // No position, but a stale working entry order. Cancel it so it
            // cannot fill after the session is over.
            r_FlattenSent = 1;
            sc.CancelAllOrders();
            r_EntryID = r_StopID = r_TargetID = 0;
            sc.AddMessageToLog("VWAPPB CANCEL stale entry order at session end", 0);
        }
    }

    if (r_Halted && r_State != VPS_POSITION_OPEN && r_State != VPS_ORDER_PENDING)
        r_State = VPS_DAILY_LOCKOUT;

    // =========================================================================
    // ORDER LIFECYCLE
    //
    // Order state is derived from the platform every update -- never assumed.
    // A submitted order is not a filled order, and a filled order is not
    // necessarily filled in full.
    // =========================================================================
    const int PrevPosQty = r_PosQty;

    if (r_State == VPS_ORDER_PENDING)
    {
        bool Resolved = false;

        if (PositionQty != 0)
        {
            // Filled (possibly partially -- PositionQuantity is what we actually
            // own, and the bracket is sized to it by Sierra).
            r_State    = VPS_POSITION_OPEN;
            r_EntryBar = i;
            r_Trades  += 1;              // counted on FILL, never on submission
            r_BeDone   = 0;
            r_Mfe      = C;
            sc.GetPersistentInt(PI_DG_FILLS) += 1;
            Resolved   = true;

            SCString M;
            M.Format("VWAPPB FILL %s qty %d @ ~%.2f | stop %.2f | target %.2f | "
                     "risk %.2f pts ($%.0f) | trade #%d",
                     PositionQty > 0 ? "LONG" : "SHORT", PositionQty,
                     Pos.AveragePrice, r_StopPx, r_TargetPx,
                     r_RiskPts, r_RiskPts * PtValue * (PositionQty > 0 ? PositionQty : -PositionQty),
                     r_Trades);
            sc.AddMessageToLog(M, 0);

            if (PositionQty != 0 && r_QtyPending != 0
                && (PositionQty > 0 ? PositionQty : -PositionQty) < r_QtyPending)
            {
                SCString P;
                P.Format("VWAPPB PARTIAL FILL: %d of %d contracts. The attached bracket "
                         "covers the filled quantity only.",
                         PositionQty > 0 ? PositionQty : -PositionQty, r_QtyPending);
                sc.AddMessageToLog(P, 1);
            }
        }
        else if (r_EntryID != 0)
        {
            s_SCTradeOrder Ord;
            if (sc.GetOrderByOrderID((uint64_t)r_EntryID, Ord) != 0)
            {
                if (Ord.OrderStatusCode == SCT_OSC_CANCELED
                 || Ord.OrderStatusCode == SCT_OSC_ERROR)
                {
                    SCString M;
                    M.Format("VWAPPB entry order %d %s -- setup abandoned",
                             r_EntryID,
                             Ord.OrderStatusCode == SCT_OSC_CANCELED ? "CANCELED" : "REJECTED");
                    sc.AddMessageToLog(M, 1);
                    r_EntryID = r_StopID = r_TargetID = 0;
                    r_State   = VPS_IDLE;
                    r_Side    = 0;
                    Resolved  = true;
                }
                else if (!IsWorkingOrderStatus(Ord.OrderStatusCode) && Ord.FilledQuantity <= 0)
                {
                    // Terminal, not working, nothing filled: nothing to wait for.
                    r_EntryID = r_StopID = r_TargetID = 0;
                    r_State   = VPS_IDLE;
                    r_Side    = 0;
                    Resolved  = true;
                }
            }
            else
            {
                // The order ID is gone from the order list and we are flat. Give
                // it one bar of grace (order books settle asynchronously), then
                // treat the setup as dead rather than waiting forever.
                if (BarClosed && i - r_OrderBar >= 1)
                {
                    r_EntryID = r_StopID = r_TargetID = 0;
                    r_State   = VPS_IDLE;
                    r_Side    = 0;
                    Resolved  = true;
                }
            }
        }

        // Stale entry order: the stop-entry never triggered, or the market order
        // never resolved. Cancel rather than leave it working into a setup that
        // no longer exists.
        if (!Resolved && BarClosed && r_OrderBar >= 0
            && (i - r_OrderBar) >= In_EntryOrderBars.GetInt())
        {
            if (r_EntryID != 0)
                sc.CancelOrder((uint64_t)r_EntryID);
            SCString M;
            M.Format("VWAPPB CANCEL unfilled entry order %d after %d bars",
                     r_EntryID, i - r_OrderBar);
            sc.AddMessageToLog(M, 0);
            r_EntryID = r_StopID = r_TargetID = 0;
            r_State   = VPS_IDLE;
            r_Side    = 0;
        }
    }

    if (r_State == VPS_POSITION_OPEN)
    {
        if (PositionQty == 0)
        {
            // The bracket (or the session flatten) closed the trade.
            const double TradePL = ClosedRaw - r_PrevClosed;
            r_PrevClosed  = ClosedRaw;
            r_LastExitBar = i;
            r_EntryID = r_StopID = r_TargetID = 0;
            r_Side    = 0;
            r_State   = (r_Halted ? VPS_DAILY_LOCKOUT : VPS_COOLDOWN);

            SCString M;
            M.Format("VWAPPB EXIT | trade P/L %.2f | session P/L %.2f | trades %d | -> %s",
                     TradePL, SessionPL, r_Trades, VP_StateName(r_State));
            sc.AddMessageToLog(M, 0);
        }
        else
        {
            // ---- Optional break-even / trailing management -------------------
            const int  Dir  = (PositionQty > 0) ? 1 : -1;
            const float Entry = (float)Pos.AveragePrice;
            const float R     = (r_RiskPts > 0.f) ? r_RiskPts : 1.f;

            // Track the best price seen since the fill; both rules key off it.
            if (Dir > 0) r_Mfe = (r_Mfe <= 0.f) ? H : VP_Max(r_Mfe, H);
            else         r_Mfe = (r_Mfe <= 0.f) ? L : VP_Min(r_Mfe, L);

            const float Excursion = Dir > 0 ? (r_Mfe - Entry) : (Entry - r_Mfe);

            if (In_UseBreakEven.GetInt() == 1 && !r_BeDone && r_StopID != 0
                && Excursion >= In_BeTriggerR.GetFloat() * R)
            {
                const float BePx = (float)sc.RoundToTickSize(
                    Entry + Dir * In_BeOffsetTicks.GetInt() * Tick, sc.TickSize);

                s_SCNewOrder Mod;
                Mod.InternalOrderID = (uint32_t)r_StopID;
                Mod.Price1 = BePx;
                if (sc.ModifyOrder(Mod) > 0)
                {
                    r_BeDone = 1;
                    r_StopPx = BePx;
                    SCString M;
                    M.Format("VWAPPB BREAK-EVEN stop -> %.2f", BePx);
                    sc.AddMessageToLog(M, 0);
                }
            }

            if (In_UseTrail.GetInt() == 1 && r_StopID != 0
                && Excursion >= In_TrailTriggerR.GetFloat() * R)
            {
                const float TrailPx = (float)sc.RoundToTickSize(
                    r_Mfe - Dir * In_TrailDistR.GetFloat() * R, sc.TickSize);

                // Only ever tighten. A trailing stop that can loosen is not a
                // stop, it is a way to give a winner back.
                const bool Better = (Dir > 0) ? (TrailPx > r_StopPx) : (TrailPx < r_StopPx);
                if (Better)
                {
                    s_SCNewOrder Mod;
                    Mod.InternalOrderID = (uint32_t)r_StopID;
                    Mod.Price1 = TrailPx;
                    if (sc.ModifyOrder(Mod) > 0)
                    {
                        r_StopPx = TrailPx;
                        if (In_Debug.GetInt() >= 2)
                        {
                            SCString M;
                            M.Format("VWAPPB TRAIL stop -> %.2f", TrailPx);
                            sc.AddMessageToLog(M, 0);
                        }
                    }
                }
            }
        }
    }
    else if (PositionQty != 0 && r_State != VPS_ORDER_PENDING)
    {
        // A position exists that this study did not open (manual entry, or a
        // reload mid-trade). Adopt it rather than opening a second one.
        r_State    = VPS_POSITION_OPEN;
        r_EntryBar = i;
        if (r_RiskPts <= 0.f)
            r_RiskPts = In_FixedStopTicks.GetInt() * Tick;
        sc.AddMessageToLog("VWAPPB adopting a pre-existing position; no new entry "
                           "will be made until it is closed.", 1);
    }

    r_PosQty = PositionQty;

    if (r_State == VPS_COOLDOWN)
    {
        if (i - r_LastExitBar >= In_CooldownBars.GetInt())
            r_State = r_Halted ? VPS_DAILY_LOCKOUT : VPS_IDLE;
    }

    // Draw the live bracket so the chart shows what is actually working.
    if (In_DrawLevels.GetInt() && r_State == VPS_POSITION_OPEN)
    {
        Sg_Entry[i]  = r_EntryPx;
        Sg_Stop[i]   = r_StopPx;
        Sg_Target[i] = r_TargetPx;
    }

    // =========================================================================
    // SIGNAL DETECTION -- completed bars only
    //
    // Everything below this line reads bar i as a CLOSED bar. On a forming bar
    // the study does nothing but display, which is what makes the signals
    // non-repainting: a state transition is a function of data that can no
    // longer change.
    // =========================================================================
    if (!BarClosed)
        return;

    // Warmup. Continuous mode needs SlowLen bars of chart history. Session mode
    // needs completed RTH bars only, and uses its own (deliberately shorter)
    // warmup -- see the In_AnchorWarmBars comment for why a full SlowLen there
    // would make the whole mode inert.
    if (AnchorMode == 0)
    {
        if (i < SlowLen + SlopeBars + 2)
            return;
    }
    else
    {
        const int AnchorWarm = (In_AnchorWarmBars.GetInt() > SlopeBars + 2)
                             ? In_AnchorWarmBars.GetInt()
                             : SlopeBars + 2;
        if (r_RthBars < AnchorWarm)
            return;
    }

    const float EmaF  = Sg_EMAFast[i];
    const float EmaS  = Sg_EMASlow[i];
    const float Atr   = Sg_ATR[i];
    const float Slope = EmaS - Sg_EMASlow[i - SlopeBars];
    const float Sep   = C - VwapSignal;                  // signed: + above VWAP

    // ---- VWAP cross bookkeeping ---------------------------------------------
    // Bit 0 is this bar. Shift first, then set, so the mask always describes
    // bars i, i-1, i-2 ... in order.
    r_SideMask = (int)(((unsigned int)r_SideMask << 1) | (C > VwapSignal ? 1u : 0u));
    const int Crosses = VP_CountCrosses((unsigned int)r_SideMask, In_CrossLookback.GetInt());

    // ---- Resolve the geometry for this bar ----------------------------------
    // Every distance used by the state machine and the risk block comes out of
    // this one place, so "points" and "ATR multiples" cannot drift apart.
    const bool AtrGeom = (In_GeomMode.GetInt() == 1);

    // In session-anchored mode there is no pre-open ATR to freeze, so take the
    // first warm reading of the session and hold it for the rest of the day.
    if (AtrGeom && r_FrozenAtr <= 0.f && Atr > 0.f)
        r_FrozenAtr = Atr;

    // ATR mode cannot evaluate anything until it has a frozen reference.
    if (AtrGeom && r_FrozenAtr <= 0.f)
        return;

    const float Fa = r_FrozenAtr;

    const float Proximity = AtrGeom ? In_ProximityATR.GetFloat()  * Fa : In_ProximityPts.GetFloat();
    const float MinSep    = AtrGeom ? In_MinSepATR.GetFloat()     * Fa : In_MinSepPts.GetFloat();
    const float RetestTol = AtrGeom ? In_RetestTolATR.GetFloat()  * Fa : In_RetestTolPts.GetFloat();
    const float ConfirmBuf= AtrGeom ? In_ConfirmBufATR.GetFloat() * Fa : In_ConfirmBufPts.GetFloat();
    const float MinSlope  = AtrGeom ? In_MinSlopeATR.GetFloat()   * Fa : In_MinSlopePts.GetFloat();
    const float MinEmaSep = AtrGeom ? In_MinEmaSepATR.GetFloat()  * Fa : In_MinEmaSepPts.GetFloat();
    const float AtrFloor  = AtrGeom ? (In_MinAtrBps.GetFloat() * 1e-4f * C)
                                    : In_MinAtrPts.GetFloat();

    // ---- Trend alignment -----------------------------------------------------
    const bool EmaSpread  = VP_Abs(EmaF - EmaS) >= MinEmaSep;
    const bool SlopeUp    = Slope >=  MinSlope;
    const bool SlopeDown  = Slope <= -MinSlope;
    const bool AtrOk      = (AtrFloor <= 0.f) || (Atr >= AtrFloor);

    const bool BullAlign = (EmaF > EmaS) && EmaSpread && SlopeUp;
    const bool BearAlign = (EmaF < EmaS) && EmaSpread && SlopeDown;

    // One place to count a rejection reason and optionally label it on the chart.
    // Declared as a lambda so every exit path books the same diagnostics.
    //
    // The lambdas here are SAFE and do not need refactoring away, despite being
    // the only C++11 construct in this repo's studies. sierrachart.h's own
    // scstructures.h contains 407 in-class member initialisers, which are
    // C++11-only -- so Sierra's headers cannot compile below C++11 and lambdas
    // (also C++11) add no toolchain risk. precompile_check.py flags them as a
    // note, not a problem, for exactly this reason.
    int LabelSlot = 0;
    auto Reject = [&](int Reason, const char* Extra)
    {
        sc.GetPersistentInt(PI_DG_BASE + Reason) += 1;

        if (In_DrawLabels.GetInt())
        {
            s_UseTool T;
            T.Clear();
            T.ChartNumber = sc.ChartNumber;
            T.DrawingType = DRAWING_TEXT;
            T.LineNumber  = 4200000 + i * 4 + (LabelSlot++);
            T.BeginIndex  = i;
            T.BeginValue  = H + 3.f * Tick;
            T.Color       = RGB(150, 150, 150);
            T.FontSize    = 7;
            T.AddMethod   = UTAM_ADD_OR_ADJUST;
            SCString Txt;
            Txt.Format("%s%s", VP_RejectName(Reason), Extra ? Extra : "");
            T.Text = Txt;
            sc.UseTool(T);
        }

        if (In_Debug.GetInt() >= 1)
        {
            SCString M;
            M.Format("VWAPPB %s | state %s side %d | vwap %.2f sep %+.2f | emaF %.2f "
                     "emaS %.2f slope %+.2f | atr %.2f | crosses %d%s",
                     VP_RejectName(Reason), VP_StateName(r_State), r_Side,
                     VwapSignal, Sep, EmaF, EmaS, Slope, Atr, Crosses,
                     Extra ? Extra : "");
            sc.AddMessageToLog(M, 0);
        }
    };

    auto ResetSetup = [&](int Reason, const char* Extra)
    {
        if (Reason != VPR_NONE)
            Reject(Reason, Extra);
        r_State    = r_Halted ? VPS_DAILY_LOCKOUT : VPS_IDLE;
        r_Side     = 0;
        r_SetupBar = -1;
        r_RejBar   = -1;
        r_ConfBar  = -1;
    };

    // Per-bar debug line.
    if (In_Debug.GetInt() >= 2 && i != r_LastLogBar)
    {
        r_LastLogBar = i;
        SCString M;
        M.Format("VWAPPB BAR %s | state %s side %d | C %.2f vwap %.2f sep %+.2f | "
                 "emaF %.2f emaS %.2f slope %+.2f | atr %.2f | crosses %d | "
                 "trades %d | sessionPL %.2f",
                 sc.DateTimeToString(sc.BaseDateTimeIn[i], FLAG_DT_COMPLETE_DATETIME).GetChars(),
                 VP_StateName(r_State), r_Side, C, VwapSignal, Sep,
                 EmaF, EmaS, Slope, Atr, Crosses, r_Trades, SessionPL);
        sc.AddMessageToLog(M, 0);
    }

    // States that must not generate signals.
    if (r_State == VPS_ORDER_PENDING || r_State == VPS_POSITION_OPEN)
        return;
    if (r_State == VPS_DAILY_LOCKOUT || r_Halted)
        return;
    if (r_State == VPS_COOLDOWN)
        return;

    // =========================================================================
    // STATE MACHINE
    // =========================================================================
    switch (r_State)
    {
        // ---------------------------------------------------------------------
        case VPS_IDLE:
        {
            if (!AtrOk)
                break;   // counted only when a setup actually reaches confirmation

            // Rule 1: separation AND alignment AND a completed close on the
            // correct side of VWAP. The close test is implied by requiring
            // Sep >= MinSep (MinSep > 0), and is asserted explicitly so the
            // rule reads the way the spec states it.
            if (BullAlign && Sep >= MinSep && C > VwapSignal)
            {
                r_State    = VPS_TREND_ESTABLISHED;
                r_Side     = 1;
                r_SetupBar = i;
                sc.GetPersistentInt(PI_DG_TREND) += 1;
            }
            else if (BearAlign && Sep <= -MinSep && C < VwapSignal)
            {
                r_State    = VPS_TREND_ESTABLISHED;
                r_Side     = -1;
                r_SetupBar = i;
                sc.GetPersistentInt(PI_DG_TREND) += 1;
            }
            break;
        }

        // ---------------------------------------------------------------------
        case VPS_TREND_ESTABLISHED:
        {
            const bool AlignOk = (r_Side > 0) ? BullAlign : BearAlign;
            if (!AlignOk)
            {
                ResetSetup(VPR_TREND_LOST, 0);
                break;
            }

            // A completed close decisively through VWAP cancels the setup.
            if ((r_Side > 0 && C < VwapSignal - RetestTol)
             || (r_Side < 0 && C > VwapSignal + RetestTol))
            {
                ResetSetup(VPR_VWAP_BREAK, 0);
                break;
            }

            if (i - r_SetupBar > In_MaxSetupBars.GetInt())
            {
                ResetSetup(VPR_EXPIRED, 0);
                break;
            }

            // Rule 2: the bar's EXTREME reaches the proximity zone. Using the
            // extreme (not the close) is what makes this a pullback test rather
            // than a second trend test.
            const bool Entered = (r_Side > 0)
                               ? (L <= VwapSignal + Proximity)
                               : (H >= VwapSignal - Proximity);

            if (Entered)
            {
                r_State    = VPS_PULLBACK_ACTIVE;
                r_SetupBar = i;           // expiry now counts from the pullback
                sc.GetPersistentInt(PI_DG_PULLBACK) += 1;
                Sg_Pullback[i] = (r_Side > 0) ? L - 2.f * Tick : H + 2.f * Tick;
            }
            break;
        }

        // ---------------------------------------------------------------------
        case VPS_PULLBACK_ACTIVE:
        {
            const bool AlignOk = (r_Side > 0) ? BullAlign : BearAlign;
            if (!AlignOk)
            {
                ResetSetup(VPR_TREND_LOST, 0);
                break;
            }

            // Rule 2 invalidation: a completed bar closing more than the retest
            // tolerance on the wrong side of VWAP means the retest succeeded,
            // which is the opposite of the trade.
            if ((r_Side > 0 && C < VwapSignal - RetestTol)
             || (r_Side < 0 && C > VwapSignal + RetestTol))
            {
                ResetSetup(VPR_VWAP_BREAK, 0);
                break;
            }

            if (i - r_SetupBar > In_MaxSetupBars.GetInt())
            {
                ResetSetup(VPR_EXPIRED, 0);
                break;
            }

            // Rule 3: the failed retest.
            const float Range = H - L;
            if (Range <= 0.f)
                break;

            const float ClosePos = (C - L) / Range;              // 0 = at low, 1 = at high
            const bool  Touched  = (r_Side > 0)
                                 ? (L <= VwapSignal + Proximity)
                                 : (H >= VwapSignal - Proximity);
            const bool  ClosedBack = (r_Side > 0) ? (C > VwapSignal) : (C < VwapSignal);
            const bool  RightHalf  = (r_Side > 0)
                                   ? (ClosePos >= In_CloseHalfFrac.GetFloat())
                                   : (ClosePos <= 1.0f - In_CloseHalfFrac.GetFloat());
            const bool  RightBody  = (r_Side > 0) ? (C > O) : (C < O);

            if (Touched && ClosedBack && RightHalf && RightBody)
            {
                r_State   = VPS_REJECTION_DETECTED;
                r_RejBar  = i;
                r_RejHigh = H;
                r_RejLow  = L;
                sc.GetPersistentInt(PI_DG_REJECTION) += 1;
                Sg_Rejection[i] = (r_Side > 0) ? L - 4.f * Tick : H + 4.f * Tick;
            }
            break;
        }

        // ---------------------------------------------------------------------
        case VPS_REJECTION_DETECTED:
        {
            const bool AlignOk = (r_Side > 0) ? BullAlign : BearAlign;
            if (!AlignOk)
            {
                ResetSetup(VPR_TREND_LOST, 0);
                break;
            }

            if ((r_Side > 0 && C < VwapSignal - RetestTol)
             || (r_Side < 0 && C > VwapSignal + RetestTol))
            {
                ResetSetup(VPR_VWAP_BREAK, 0);
                break;
            }

            const int Age = i - r_RejBar;
            if (Age < 1)
                break;                      // still the rejection bar itself

            // Rule 4: the confirmation bar closes beyond the rejection bar's
            // extreme by the buffer.
            const bool Confirmed = (r_Side > 0)
                                 ? (C > r_RejHigh + ConfirmBuf)
                                 : (C < r_RejLow  - ConfirmBuf);

            if (!Confirmed)
            {
                if (Age >= In_ConfirmWindow.GetInt())
                {
                    // The spec forbids multiple signals out of one pullback, so a
                    // stale rejection retires the whole setup rather than arming
                    // a second rejection from the same leg.
                    ResetSetup(VPR_CONFIRM_TIMEOUT, 0);
                }
                break;
            }

            r_ConfBar  = i;
            r_ConfHigh = H;
            r_ConfLow  = L;
            r_State    = VPS_CONFIRMATION_DETECTED;
            sc.GetPersistentInt(PI_DG_CONFIRM) += 1;
            // Fall through to the entry block below on this same update: the
            // confirmation bar has closed, so the next available price is the
            // next tick, and waiting a bar would be a different strategy.
            break;
        }

        default:
            break;
    }

    if (r_State != VPS_CONFIRMATION_DETECTED)
        return;

    // =========================================================================
    // ENTRY QUALIFICATION
    //
    // Every filter below rejects the CONFIRMED candidate. They are checked here,
    // once, rather than scattered through the state machine, so the funnel has a
    // single honest denominator.
    // =========================================================================
    const int  Side  = r_Side;
    const bool Long  = (Side > 0);

    if (PositionQty != 0)                    { ResetSetup(VPR_IN_POSITION, 0); return; }
    if (r_EntryID != 0)                      { ResetSetup(VPR_IN_POSITION, " (working order)"); return; }
    if (i - r_LastExitBar < In_CooldownBars.GetInt()) { ResetSetup(VPR_COOLDOWN, 0); return; }
    if (In_MaxTradesDay.GetInt() > 0 && r_Trades >= In_MaxTradesDay.GetInt())
                                             { ResetSetup(VPR_MAX_TRADES, 0); return; }

    // The confirmation bar must close inside the entry window. A confirmation at
    // 15:01 is not a 15:00 trade.
    if (BarSec < RthOpen || BarSec > EntryEnd) { ResetSetup(VPR_WINDOW, 0); return; }

    if (!AtrOk)                              { ResetSetup(VPR_LOW_ATR, 0); return; }
    if (!EmaSpread)                          { ResetSetup(VPR_EMA_CHOP, 0); return; }
    if (Long ? !SlopeUp : !SlopeDown)        { ResetSetup(VPR_FLAT_SLOPE, 0); return; }

    if (In_MaxCrosses.GetInt() > 0 && Crosses > In_MaxCrosses.GetInt())
    {
        SCString E; E.Format(" (%d crosses)", Crosses);
        ResetSetup(VPR_CROSSES, E.GetChars());
        return;
    }

    const float ConfRange = H - L;
    if (In_MaxBarAtrMult.GetFloat() > 0.f && Atr > 0.f
        && ConfRange > In_MaxBarAtrMult.GetFloat() * Atr)
    {
        SCString E; E.Format(" (range %.2f vs %.2f)", ConfRange, In_MaxBarAtrMult.GetFloat() * Atr);
        ResetSetup(VPR_BIG_BAR, E.GetChars());
        return;
    }

    // ---- Optional order-flow filters ----------------------------------------
    if (In_UseVolFilter.GetInt() == 1)
    {
        const int LB = In_VolLookback.GetInt();
        float Sum = 0.f;
        int   n   = 0;
        for (int k = 1; k <= LB && (i - k) >= 0; ++k)
        {
            Sum += sc.BaseDataIn[SC_VOLUME][i - k];
            ++n;
        }
        const float Avg = (n > 0) ? (Sum / n) : 0.f;
        if (Avg > 0.f && V < In_VolMult.GetFloat() * Avg)
        {
            SCString E; E.Format(" (vol %.0f < %.0f)", V, In_VolMult.GetFloat() * Avg);
            ResetSetup(VPR_VOL_FILTER, E.GetChars());
            return;
        }
    }

    if (In_UseDeltaFilter.GetInt() == 1)
    {
        // Requires a chart with bid/ask volume data. On a chart without it both
        // arrays read 0 and the filter blocks everything -- which is the honest
        // failure mode, not a silent pass.
        const float Delta = sc.AskVolume[i] - sc.BidVolume[i];
        const float Need  = In_MinDelta.GetFloat();
        const bool  Ok    = Long ? (Delta >= Need) : (Delta <= -Need);
        if (!Ok)
        {
            SCString E; E.Format(" (delta %.0f)", Delta);
            ResetSetup(VPR_DELTA_FILTER, E.GetChars());
            return;
        }
    }

    // =========================================================================
    // RISK: stop, target, reward:risk
    // =========================================================================
    // Entry reference. In market mode the next available price is the next tick
    // after this close; the close is the only unbiased estimate of it available
    // at signal time. In stop mode the reference IS the resting stop price.
    const float StopBuf = AtrGeom ? (In_StopBufATR.GetFloat() * Fa)
                                  : (In_StopBufTicks.GetInt() * Tick);
    float EntryRef = C;
    if (In_EntryMode.GetInt() == 1)
        EntryRef = (float)sc.RoundToTickSize(Long ? (H + Tick) : (L - Tick), sc.TickSize);

    float StopPx = 0.f;
    switch (In_StopMethod.GetInt())
    {
        case 1:   // fixed ticks
            StopPx = EntryRef - Side * In_FixedStopTicks.GetInt() * Tick;
            break;
        case 2:   // ATR
            StopPx = EntryRef - Side * In_StopAtrMult.GetFloat() * Atr;
            break;
        default:
        {
            // Beyond the rejection bar's extreme, plus a buffer. Optionally also
            // clearing the confirmation bar, which can overshoot the rejection
            // bar's extreme and would otherwise leave the stop inside the bar we
            // are entering on.
            float Extreme = Long ? r_RejLow : r_RejHigh;
            if (In_StopIncConfBar.GetInt() == 1)
                Extreme = Long ? VP_Min(Extreme, r_ConfLow) : VP_Max(Extreme, r_ConfHigh);
            StopPx = Extreme - Side * StopBuf;
            break;
        }
    }
    StopPx = (float)sc.RoundToTickSize(StopPx, sc.TickSize);

    float StopPts = Long ? (EntryRef - StopPx) : (StopPx - EntryRef);
    const float MinStopPts = AtrGeom ? (In_MinStopATR.GetFloat() * Fa)
                                     : (In_MinStopTicks.GetInt() * Tick);
    const float MaxStopPts = AtrGeom ? (In_MaxStopATR.GetFloat() * Fa)
                                     : (In_MaxStopTicks.GetInt() * Tick);

    if (StopPts <= 0.f)
    {
        ResetSetup(VPR_STOP_RANGE, " (non-positive)");
        return;
    }
    if (StopPts < MinStopPts || StopPts > MaxStopPts)
    {
        SCString E; E.Format(" (%.2f pts, allowed %.2f-%.2f)", StopPts, MinStopPts, MaxStopPts);
        ResetSetup(VPR_STOP_RANGE, E.GetChars());
        return;
    }

    float TargetPts = 0.f;
    switch (In_TargetMode.GetInt())
    {
        case 1:  TargetPts = In_TargetAtrMult.GetFloat() * Atr;  break;
        case 2:  TargetPts = In_TargetR.GetFloat() * StopPts;    break;
        default: TargetPts = In_FixedTgtTicks.GetInt() * Tick;   break;
    }
    TargetPts = (float)sc.RoundToTickSize(TargetPts, sc.TickSize);

    const float RR = (StopPts > 0.f) ? (TargetPts / StopPts) : 0.f;
    if (RR < In_MinRR.GetFloat())
    {
        SCString E; E.Format(" (%.2f:1 vs %.2f:1)", RR, In_MinRR.GetFloat());
        ResetSetup(VPR_RR, E.GetChars());
        return;
    }

    const int   Qty      = In_Qty.GetInt();
    const float RiskDoll = StopPts * PtValue * Qty;

    // =========================================================================
    // ORDER SUBMISSION
    //
    // Attached stop and target are OFFSETS from the fill, which is how Sierra
    // brackets a parent order. The absolute prices below are what we EXPECT
    // given EntryRef; the realised bracket shifts with the actual fill, and the
    // risk in points is preserved either way. That is the intended behaviour --
    // an absolute-price bracket would silently widen risk on a bad fill.
    // =========================================================================
    s_SCNewOrder NewOrder;
    NewOrder.OrderQuantity = Qty;
    NewOrder.TimeInForce   = SCT_TIF_DAY;
    NewOrder.Stop1Offset   = StopPts;
    NewOrder.Target1Offset = TargetPts;
    NewOrder.AttachedOrderStop1Type   = SCT_ORDERTYPE_STOP;
    NewOrder.AttachedOrderTarget1Type = SCT_ORDERTYPE_LIMIT;
    NewOrder.TextTag       = "VWAPPB";

    if (In_EntryMode.GetInt() == 1)
    {
        // Stop entry one tick beyond the confirmation bar. If price never trades
        // there the order is cancelled by the stale-order rule above -- a setup
        // that needs chasing is not this setup.
        NewOrder.OrderType = SCT_ORDERTYPE_STOP;
        NewOrder.Price1    = EntryRef;
    }
    else
    {
        NewOrder.OrderType = SCT_ORDERTYPE_MARKET;
    }

    const int RC = Long ? (int)sc.BuyEntry(NewOrder) : (int)sc.SellEntry(NewOrder);

    if (RC > 0)
    {
        r_EntryID    = (int)NewOrder.InternalOrderID;
        r_StopID     = (int)NewOrder.Stop1InternalOrderID;
        r_TargetID   = (int)NewOrder.Target1InternalOrderID;
        r_OrderBar   = i;
        r_QtyPending = Qty;
        r_State      = VPS_ORDER_PENDING;
        r_EntryPx    = EntryRef;
        r_StopPx     = Long ? (EntryRef - StopPts)   : (EntryRef + StopPts);
        r_TargetPx   = Long ? (EntryRef + TargetPts) : (EntryRef - TargetPts);
        r_RiskPts    = StopPts;

        sc.GetPersistentInt(PI_DG_ORDERS) += 1;

        if (Long) Sg_ConfBuy[i]  = L - 4.f * Tick;
        else      Sg_ConfSell[i] = H + 4.f * Tick;

        SCString M;
        M.Format("VWAPPB ENTRY %s %d %s | ref %.2f stop %.2f (%.2f pts) target %.2f "
                 "(%.2f pts) RR %.2f | risk $%.0f | vwap %.2f sep %+.2f | emaF %.2f "
                 "emaS %.2f slope %+.2f | atr %.2f | crosses %d | rejBar %d confBar %d "
                 "| trade #%d of %d | sessionPL %.2f | orderID %d",
                 Long ? "LONG" : "SHORT", Qty,
                 In_EntryMode.GetInt() == 1 ? "STOP" : "MARKET",
                 EntryRef, r_StopPx, StopPts, r_TargetPx, TargetPts, RR, RiskDoll,
                 VwapSignal, Sep, EmaF, EmaS, Slope, Atr, Crosses,
                 r_RejBar, r_ConfBar, r_Trades + 1, In_MaxTradesDay.GetInt(),
                 SessionPL, r_EntryID);
        sc.AddMessageToLog(M, 0);

        if (In_DrawLevels.GetInt())
        {
            s_UseTool Mk;
            Mk.Clear();
            Mk.ChartNumber = sc.ChartNumber;
            Mk.DrawingType = DRAWING_MARKER;
            Mk.LineNumber  = 4300000 + i;
            Mk.BeginIndex  = i;
            Mk.BeginValue  = Long ? (L - 6.f * Tick) : (H + 6.f * Tick);
            Mk.MarkerType  = Long ? MARKER_ARROWUP : MARKER_ARROWDOWN;
            Mk.MarkerSize  = 10;
            Mk.Color       = Long ? RGB(0, 200, 100) : RGB(220, 80, 80);
            Mk.LineWidth   = 2;
            Mk.AddMethod   = UTAM_ADD_OR_ADJUST;
            sc.UseTool(Mk);
        }
    }
    else
    {
        // A non-positive return code means NOTHING was submitted. Counting and
        // decoding it here is the difference between "the signal is wrong" and
        // "auto trading was switched off".
        sc.GetPersistentInt(PI_LAST_RC) = RC;
        int& r_RcLogged = sc.GetPersistentInt(PI_RC_LOG_SENT);
        if (r_RcLogged == 0 || In_Debug.GetInt() >= 1)
        {
            r_RcLogged = 1;
            SCString M;
            M.Format("VWAPPB ORDER NOT SUBMITTED rc=%d %s", RC, VP_OrderRCText(RC));
            sc.AddMessageToLog(M, 1);
        }
        ResetSetup(VPR_ORDER_FAILED, 0);
    }
}
