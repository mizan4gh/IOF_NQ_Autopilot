// ============================================================================
//  MNQ_SweepAbsorption_PropEval.cpp
//
//  Liquidity-sweep / absorption reversal system for MNQ.
//
//  Design: location is a hard gate. Order-flow conditions are never evaluated
//  on a bar that is not within tolerance of a tracked liquidity level. The
//  entire signal is built from bar-level bid/ask volume so that it can be
//  backtested; no DOM or Time & Sales dependency exists anywhere in this file.
//
//  Chart requirements (the signal is WRONG without these):
//    - Intraday Data Storage Time Unit = 1 Tick
//    - 2000-volume bars (or 1-minute) on MNQ continuous
//    - Chart timezone set consistently with the session inputs below
//
//  Governors: $700 daily shutdown target, $800 daily loss limit.
//  Ships with Send Orders To Trade Service = 0 (simulated) deliberately.
//
//  NOT FINANCIAL ADVICE. Untested. Run in Sim/Replay for a meaningful sample.
// ============================================================================

#include "sierrachart.h"
#include <algorithm>
#include <cmath>

SCDLLName("MNQ Sweep Absorption Prop Eval")

// ---------------------------------------------------------------------------
//  Level identity. Support levels generate longs, resistance levels shorts.
// ---------------------------------------------------------------------------
enum LevelId
{
    LVL_ONL = 0, LVL_PDL, LVL_VAL, LVL_VWAP_L1, LVL_VWAP_L2, LVL_ORL,
    LVL_ONH,     LVL_PDH, LVL_VAH, LVL_VWAP_U1, LVL_VWAP_U2, LVL_ORH,
    LVL_COUNT
};

// Direction of the setup a level produces: +1 long (support), -1 short.
static const int LEVEL_DIRECTION[LVL_COUNT] =
{
    +1, +1, +1, +1, +1, +1,
    -1, -1, -1, -1, -1, -1
};

enum SetupState { ST_IDLE = 0, ST_ARMED, ST_SWEPT, ST_ABSORBED, ST_DEAD };

enum HaltReason
{
    HALT_NONE = 0, HALT_TARGET = 1, HALT_LOSS = 2, HALT_GIVEBACK = 4,
    HALT_MAXTRADES = 5, HALT_CONSECLOSS = 6
};

// ---------------------------------------------------------------------------
//  Per-level setup state
// ---------------------------------------------------------------------------
struct s_LevelState
{
    float Price;
    int   Valid;          // level has a meaningful value today
    int   Burned;         // already produced a setup this session
    int   State;
    int   StateBarIndex;  // bar the current state was entered on
    double StateTimeDays; // timestamp the state was entered (SCDateTime days)
    float SweepExtreme;   // lowest low (long) / highest high (short) of the sweep
    float SweepDelta;
    float SweepBarHigh;
    float SweepBarLow;
};

// ---------------------------------------------------------------------------
//  Study-wide persistent state
// ---------------------------------------------------------------------------
struct s_StudyState
{
    s_LevelState Levels[LVL_COUNT];

    // Rolling window of completed RTH day ranges. Every distance in this study
    // is scaled off this rather than off the execution bar's ATR.
    float  DailyRanges[64];
    int    DailyRangeCount;
    int    DailyRangeIdx;
    float  DailyAtr;

    int    TradingDayNum;      // SCDateTime date of the current trading day
    int    RthOpened;          // crossed the RTH open this day
    int    OrComplete;

    // Session extremes under construction
    float  OnHigh, OnLow;      // overnight, frozen at RTH open
    float  RthHigh, RthLow;    // today's RTH, becomes tomorrow's PDH/PDL
    float  PrevRthHigh, PrevRthLow;
    float  OrHigh, OrLow;

    // Session VWAP accumulators (reset at RTH open)
    double VwapSumPV, VwapSumV, VwapSumP2V;

    // Governors
    int    TradeCount;
    int    ConsecLosses;
    int    LossesToday;
    int    Halted;
    int    HaltReason;
    double DailyPeak;

    // Position lifecycle tracking
    int    PrevPositionQty;
    double PrevClosedPL;
    int    BreakevenDone;
    double EntryPrice;
    double StopPrice;
    double RiskPoints;
    int    PositionDir;

    // Diagnostics
    int    DiagBarsSeen, DiagBlockedHalt, DiagBlockedPosition,
           DiagBlockedWindow, DiagArmed, DiagSwept, DiagAbsorbed,
           DiagTriggered, DiagUnsized, DiagOrders, DiagBurnedDead,
           DiagAbsChecks, DiagFailAggr, DiagFailRatio, DiagFailIntensity,
           DiagFailClosePos, DiagFarEntry, LastCountedBar;
};

// ---------------------------------------------------------------------------
//  Small statistics helpers over a trailing window of a subgraph array
// ---------------------------------------------------------------------------
static const int STAT_MAX = 512;

static float TrailingMedian(SCFloatArrayRef Arr, int EndIndex, int Length)
{
    if (Length <= 0 || EndIndex < 0)
        return 0.0f;

    if (Length > STAT_MAX)
        Length = STAT_MAX;

    int Start = EndIndex - Length + 1;
    if (Start < 0)
        Start = 0;

    int N = EndIndex - Start + 1;
    if (N <= 0)
        return 0.0f;

    float Buf[STAT_MAX];
    for (int k = 0; k < N; ++k)
        Buf[k] = Arr[Start + k];

    std::sort(Buf, Buf + N);
    return Buf[N / 2];
}

static float TrailingStdDev(SCFloatArrayRef Arr, int EndIndex, int Length)
{
    if (Length <= 1 || EndIndex < 0)
        return 0.0f;

    int Start = EndIndex - Length + 1;
    if (Start < 0)
        Start = 0;

    int N = EndIndex - Start + 1;
    if (N <= 1)
        return 0.0f;

    double Sum = 0.0;
    for (int k = Start; k <= EndIndex; ++k)
        Sum += Arr[k];

    double Mean = Sum / N;
    double Acc = 0.0;
    for (int k = Start; k <= EndIndex; ++k)
    {
        double D = Arr[k] - Mean;
        Acc += D * D;
    }

    return (float)std::sqrt(Acc / (N - 1));
}

// ===========================================================================
SCSFExport scsf_MNQ_SweepAbsorption_PropEval(SCStudyInterfaceRef sc)
{
    // -- Visible level plots -------------------------------------------------
    SCSubgraphRef Sg_ONH      = sc.Subgraph[0];
    SCSubgraphRef Sg_ONL      = sc.Subgraph[1];
    SCSubgraphRef Sg_PDH      = sc.Subgraph[2];
    SCSubgraphRef Sg_PDL      = sc.Subgraph[3];
    SCSubgraphRef Sg_VWAP     = sc.Subgraph[4];
    SCSubgraphRef Sg_VWAP_U1  = sc.Subgraph[5];
    SCSubgraphRef Sg_VWAP_L1  = sc.Subgraph[6];
    SCSubgraphRef Sg_VWAP_U2  = sc.Subgraph[7];
    SCSubgraphRef Sg_VWAP_L2  = sc.Subgraph[8];
    SCSubgraphRef Sg_ORH      = sc.Subgraph[9];
    SCSubgraphRef Sg_ORL      = sc.Subgraph[10];
    SCSubgraphRef Sg_VAH      = sc.Subgraph[11];
    SCSubgraphRef Sg_VAL      = sc.Subgraph[12];
    SCSubgraphRef Sg_LongMark = sc.Subgraph[13];
    SCSubgraphRef Sg_ShortMark= sc.Subgraph[14];

    // -- Internal arrays -----------------------------------------------------
    SCSubgraphRef Sg_ATR      = sc.Subgraph[15];
    SCSubgraphRef Sg_Delta    = sc.Subgraph[16];
    SCSubgraphRef Sg_AbsRatio = sc.Subgraph[17];
    SCSubgraphRef Sg_Intensity= sc.Subgraph[18];

    // -- Inputs --------------------------------------------------------------
    SCInputRef In_SendOrders     = sc.Input[0];
    SCInputRef In_UseON          = sc.Input[1];
    SCInputRef In_UsePD          = sc.Input[2];
    SCInputRef In_UseVA          = sc.Input[3];
    SCInputRef In_UseVWAP        = sc.Input[4];
    SCInputRef In_UseOR          = sc.Input[5];
    SCInputRef In_VbPStudy       = sc.Input[6];
    SCInputRef In_VbPVahSg       = sc.Input[7];
    SCInputRef In_VbPValSg       = sc.Input[8];
    SCInputRef In_RthStart       = sc.Input[9];
    SCInputRef In_OrMinutes      = sc.Input[10];
    SCInputRef In_LastEntry      = sc.Input[11];
    SCInputRef In_FlattenTime    = sc.Input[12];
    SCInputRef In_DaySessionEnd  = sc.Input[13];
    SCInputRef In_NoTradeStart   = sc.Input[14];
    SCInputRef In_NoTradeEnd     = sc.Input[15];
    SCInputRef In_AtrLength      = sc.Input[16];
    SCInputRef In_TolAtrMult     = sc.Input[17];
    SCInputRef In_TolMinTicks    = sc.Input[18];
    SCInputRef In_ClusterMult    = sc.Input[19];
    SCInputRef In_DeltaLookback  = sc.Input[20];
    SCInputRef In_DeltaSigma     = sc.Input[21];
    SCInputRef In_AbsRatioMult   = sc.Input[22];
    SCInputRef In_IntensityMult  = sc.Input[23];
    SCInputRef In_ClosePosInBar  = sc.Input[24];
    SCInputRef In_MinSweepTicks  = sc.Input[25];
    SCInputRef In_ArmedExpiry    = sc.Input[26];
    SCInputRef In_SweptExpiry    = sc.Input[27];
    SCInputRef In_AbsorbExpiry   = sc.Input[28];
    SCInputRef In_InvalidAtrMult = sc.Input[29];
    SCInputRef In_StopAtrMult    = sc.Input[30];
    SCInputRef In_StopMinTicks   = sc.Input[31];
    SCInputRef In_BreakevenR     = sc.Input[32];
    SCInputRef In_MinTargetR     = sc.Input[33];
    SCInputRef In_MaxTargetR     = sc.Input[34];
    SCInputRef In_DefaultTargetR = sc.Input[35];
    SCInputRef In_RiskPerTrade   = sc.Input[36];
    SCInputRef In_MaxContracts   = sc.Input[37];
    SCInputRef In_DailyTarget    = sc.Input[38];
    SCInputRef In_DailyLossLimit = sc.Input[39];
    SCInputRef In_MaxGiveback    = sc.Input[40];
    SCInputRef In_MaxTrades      = sc.Input[41];
    SCInputRef In_MaxConsecLoss  = sc.Input[42];
    SCInputRef In_ConfAfterFirst = sc.Input[43];
    SCInputRef In_ConfAfterLoss  = sc.Input[44];
    SCInputRef In_LossBuffer     = sc.Input[45];
    SCInputRef In_VwapBand1      = sc.Input[46];
    SCInputRef In_VwapBand2      = sc.Input[47];
    SCInputRef In_LogDiag        = sc.Input[48];
    SCInputRef In_Debug          = sc.Input[49];
    SCInputRef In_MinStopPoints  = sc.Input[50];
    SCInputRef In_MaxStopPoints  = sc.Input[51];
    SCInputRef In_DailyRangeDays = sc.Input[52];
    SCInputRef In_MaxEntryDist   = sc.Input[53];

    // =======================================================================
    //  Defaults
    // =======================================================================
    if (sc.SetDefaults)
    {
        sc.GraphName = "MNQ Sweep Absorption - Prop Eval";
        sc.GraphRegion = 0;
        sc.AutoLoop = 1;
        sc.FreeDLL = 0;
        sc.CalculationPrecedence = LOW_PREC_LEVEL;

        // Trading configuration
        sc.MaintainTradeStatisticsAndTradesData = 1;
        sc.SupportAttachedOrdersForTrading = 1;
        sc.UseGUIAttachedOrderSetting = 0;
        sc.AllowMultipleEntriesInSameDirection = 0;
        sc.AllowEntryWithWorkingOrders = 0;
        sc.CancelAllWorkingOrdersOnExit = 1;
        sc.AllowOnlyOneTradePerBar = 1;
        sc.SupportReversals = 0;
        sc.MaximumPositionAllowed = 20;

        Sg_ONH.Name = "Overnight High";       Sg_ONH.DrawStyle = DRAWSTYLE_DASH;
        Sg_ONH.PrimaryColor = RGB(200, 80, 80);
        Sg_ONL.Name = "Overnight Low";        Sg_ONL.DrawStyle = DRAWSTYLE_DASH;
        Sg_ONL.PrimaryColor = RGB(80, 200, 80);
        Sg_PDH.Name = "Prior Day High";       Sg_PDH.DrawStyle = DRAWSTYLE_DASH;
        Sg_PDH.PrimaryColor = RGB(180, 100, 100);
        Sg_PDL.Name = "Prior Day Low";        Sg_PDL.DrawStyle = DRAWSTYLE_DASH;
        Sg_PDL.PrimaryColor = RGB(100, 180, 100);
        Sg_VWAP.Name = "VWAP";                Sg_VWAP.DrawStyle = DRAWSTYLE_LINE;
        Sg_VWAP.PrimaryColor = RGB(220, 220, 100);
        Sg_VWAP_U1.Name = "VWAP +1sd";        Sg_VWAP_U1.DrawStyle = DRAWSTYLE_LINE;
        Sg_VWAP_L1.Name = "VWAP -1sd";        Sg_VWAP_L1.DrawStyle = DRAWSTYLE_LINE;
        Sg_VWAP_U2.Name = "VWAP +2sd";        Sg_VWAP_U2.DrawStyle = DRAWSTYLE_LINE;
        Sg_VWAP_L2.Name = "VWAP -2sd";        Sg_VWAP_L2.DrawStyle = DRAWSTYLE_LINE;
        Sg_ORH.Name = "Opening Range High";   Sg_ORH.DrawStyle = DRAWSTYLE_DASH;
        Sg_ORL.Name = "Opening Range Low";    Sg_ORL.DrawStyle = DRAWSTYLE_DASH;
        Sg_VAH.Name = "Prior VAH";            Sg_VAH.DrawStyle = DRAWSTYLE_DASH;
        Sg_VAL.Name = "Prior VAL";            Sg_VAL.DrawStyle = DRAWSTYLE_DASH;

        Sg_LongMark.Name = "Long Entry";      Sg_LongMark.DrawStyle = DRAWSTYLE_ARROW_UP;
        Sg_LongMark.PrimaryColor = RGB(0, 255, 0);   Sg_LongMark.LineWidth = 3;
        Sg_ShortMark.Name = "Short Entry";    Sg_ShortMark.DrawStyle = DRAWSTYLE_ARROW_DOWN;
        Sg_ShortMark.PrimaryColor = RGB(255, 0, 0);  Sg_ShortMark.LineWidth = 3;

        Sg_ATR.Name = "ATR";                  Sg_ATR.DrawStyle = DRAWSTYLE_IGNORE;
        Sg_Delta.Name = "Bar Delta";          Sg_Delta.DrawStyle = DRAWSTYLE_IGNORE;
        Sg_AbsRatio.Name = "Absorption Ratio";Sg_AbsRatio.DrawStyle = DRAWSTYLE_IGNORE;
        Sg_Intensity.Name = "Trade Intensity"; Sg_Intensity.DrawStyle = DRAWSTYLE_IGNORE;

        In_SendOrders.Name = "Send Orders To Trade Service (0=sim)";
        In_SendOrders.SetInt(0);

        In_UseON.Name  = "Use Overnight High/Low";      In_UseON.SetYesNo(1);
        In_UsePD.Name  = "Use Prior Day High/Low";      In_UsePD.SetYesNo(1);
        In_UseVA.Name  = "Use Prior VAH/VAL";           In_UseVA.SetYesNo(0);
        In_UseVWAP.Name= "Use VWAP Deviation Bands";    In_UseVWAP.SetYesNo(1);
        In_UseOR.Name  = "Use Opening Range High/Low";  In_UseOR.SetYesNo(1);

        In_VbPStudy.Name = "Volume By Price Study Reference";
        In_VbPStudy.SetStudyID(0);
        In_VbPVahSg.Name = "VbP VAH Subgraph Index";    In_VbPVahSg.SetInt(0);
        In_VbPValSg.Name = "VbP VAL Subgraph Index";    In_VbPValSg.SetInt(1);

        In_RthStart.Name = "RTH Session Start";         In_RthStart.SetTime(HMS_TIME(9, 30, 0));
        In_OrMinutes.Name = "Opening Range Minutes";    In_OrMinutes.SetInt(15);
        In_LastEntry.Name = "Last Entry Time";          In_LastEntry.SetTime(HMS_TIME(15, 30, 0));
        In_FlattenTime.Name = "Hard Flatten Time";      In_FlattenTime.SetTime(HMS_TIME(15, 55, 0));
        In_DaySessionEnd.Name = "Day Session Ends At (must be < 18:00)";
        In_DaySessionEnd.SetTime(HMS_TIME(17, 0, 0));
        In_NoTradeStart.Name = "No-Trade Window Start"; In_NoTradeStart.SetTime(HMS_TIME(12, 0, 0));
        In_NoTradeEnd.Name = "No-Trade Window End";     In_NoTradeEnd.SetTime(HMS_TIME(13, 30, 0));

        In_AtrLength.Name = "ATR Length";               In_AtrLength.SetInt(14);
        In_TolAtrMult.Name = "Level Tolerance (x avg daily range)";
        In_TolAtrMult.SetFloat(0.020f);
        In_TolMinTicks.Name = "Level Tolerance Min (ticks)"; In_TolMinTicks.SetInt(3);
        In_ClusterMult.Name = "Confluence Cluster Distance (points)";
        In_ClusterMult.SetFloat(10.0f);

        In_DeltaLookback.Name = "Delta Statistics Lookback (bars)";
        In_DeltaLookback.SetInt(100);
        In_DeltaSigma.Name = "Delta Aggression Threshold (sigma)";
        In_DeltaSigma.SetFloat(1.2f);
        In_AbsRatioMult.Name = "Absorption Ratio Threshold (x median)";
        In_AbsRatioMult.SetFloat(1.5f);
        In_IntensityMult.Name = "Trade Intensity Threshold (x median)";
        In_IntensityMult.SetFloat(1.3f);
        In_ClosePosInBar.Name = "Min Close Position In Bar Range";
        In_ClosePosInBar.SetFloat(0.35f);
        In_MinSweepTicks.Name = "Min Sweep Penetration (ticks)";
        In_MinSweepTicks.SetInt(2);

        In_ArmedExpiry.Name  = "ARMED Expiry (minutes)";    In_ArmedExpiry.SetInt(20);
        In_SweptExpiry.Name  = "SWEPT Expiry (minutes)";    In_SweptExpiry.SetInt(12);
        In_AbsorbExpiry.Name = "ABSORBED Expiry (minutes)"; In_AbsorbExpiry.SetInt(10);
        In_InvalidAtrMult.Name = "Sweep Invalidation (x avg daily range)";
        In_InvalidAtrMult.SetFloat(0.060f);

        In_StopAtrMult.Name = "Stop Beyond Sweep Extreme (x avg daily range)";
        In_StopAtrMult.SetFloat(0.030f);
        In_StopMinTicks.Name = "Stop Beyond Sweep Extreme Min (ticks)";
        In_StopMinTicks.SetInt(3);
        In_BreakevenR.Name = "Move Stop To Breakeven At (R)";
        In_BreakevenR.SetFloat(1.5f);
        In_MinTargetR.Name = "Minimum Target (R)";       In_MinTargetR.SetFloat(2.0f);
        In_MaxTargetR.Name = "Maximum Target (R)";       In_MaxTargetR.SetFloat(6.0f);
        In_DefaultTargetR.Name = "Default Target If No Level (R)";
        In_DefaultTargetR.SetFloat(3.0f);

        In_RiskPerTrade.Name = "Risk Per Trade ($)";     In_RiskPerTrade.SetFloat(250.0f);
        In_MaxContracts.Name = "Max Contracts Per Trade";In_MaxContracts.SetInt(10);

        In_DailyTarget.Name = "Daily Shutdown Target ($, 0=off)";
        In_DailyTarget.SetFloat(700.0f);
        In_DailyLossLimit.Name = "Daily Loss Limit ($, 0=off)";
        In_DailyLossLimit.SetFloat(800.0f);
        In_MaxGiveback.Name = "Max Giveback From Daily Peak ($, 0=off)";
        In_MaxGiveback.SetFloat(350.0f);
        In_MaxTrades.Name = "Max Trades Per Day (0=off)";In_MaxTrades.SetInt(3);
        In_MaxConsecLoss.Name = "Max Consecutive Losses (0=off)";
        In_MaxConsecLoss.SetInt(2);
        In_ConfAfterFirst.Name = "Confluence Required After First Trade";
        In_ConfAfterFirst.SetInt(1);
        In_ConfAfterLoss.Name = "Confluence Required After Any Loss";
        In_ConfAfterLoss.SetInt(2);
        In_LossBuffer.Name = "Daily Loss Safety Buffer ($)";
        In_LossBuffer.SetFloat(50.0f);

        In_VwapBand1.Name = "VWAP Band 1 Multiple";      In_VwapBand1.SetFloat(1.0f);
        In_VwapBand2.Name = "VWAP Band 2 Multiple";      In_VwapBand2.SetFloat(2.0f);

        In_LogDiag.Name = "Log Daily Diagnostics";       In_LogDiag.SetYesNo(1);
        In_Debug.Name   = "Print Debug Messages To Log"; In_Debug.SetYesNo(0);

        In_MinStopPoints.Name = "Minimum Stop (points)";  In_MinStopPoints.SetFloat(10.0f);
        In_MaxStopPoints.Name = "Maximum Stop (points)";  In_MaxStopPoints.SetFloat(40.0f);
        In_DailyRangeDays.Name = "Avg Daily Range Lookback (days)";
        In_DailyRangeDays.SetInt(14);
        In_MaxEntryDist.Name = "Max Entry Distance From Level (x avg daily range)";
        In_MaxEntryDist.SetFloat(0.025f);

        return;
    }

    // =======================================================================
    //  Persistent state allocation
    // =======================================================================
    s_StudyState* St = (s_StudyState*)sc.GetPersistentPointer(1);

    if (sc.LastCallToFunction)
    {
        if (St != NULL)
        {
            delete St;
            sc.SetPersistentPointer(1, NULL);
        }
        return;
    }

    if (St == NULL)
    {
        St = new s_StudyState();
        memset(St, 0, sizeof(s_StudyState));
        St->TradingDayNum = -1;
        St->OnHigh = -FLT_MAX; St->OnLow = FLT_MAX;
        St->RthHigh = -FLT_MAX; St->RthLow = FLT_MAX;
        St->OrHigh = -FLT_MAX;  St->OrLow = FLT_MAX;
        sc.SetPersistentPointer(1, St);
    }

    sc.SendOrdersToTradeService = (In_SendOrders.GetInt() != 0);
    sc.MaximumPositionAllowed   = In_MaxContracts.GetInt();

    const int i = sc.Index;
    if (i < 2)
        return;

    // =======================================================================
    //  Per-bar derived series (computed on every bar, closed or not)
    // =======================================================================
    sc.ATR(sc.BaseDataIn, Sg_ATR, i, In_AtrLength.GetInt(), MOVAVGTYPE_WILDERS);

    const float High  = sc.High[i];
    const float Low   = sc.Low[i];
    const float Close = sc.Close[i];
    const float Range = High - Low;
    const float Atr   = Sg_ATR[i];
    const float Tick  = sc.TickSize;

    const float Delta = (float)(sc.AskVolume[i] - sc.BidVolume[i]);
    Sg_Delta[i]  = Delta;
    // Trade intensity: contracts per second. On TIME bars this is proportional
    // to bar volume. On CONSTANT-VOLUME bars, volume is identical on every bar
    // by construction, so raw volume carries no information at all — intensity
    // becomes the inverse of bar duration, which is the meaningful analogue.
    // Never compare raw sc.Volume against its own median on a volume chart.
    double BarSeconds = 1.0;
    if (i > 0)
    {
        // SCDateTime subtraction yields another SCDateTime, not a scalar, so
        // each timestamp is converted first. GetAsDouble() returns days.
        const double ThisBarTime = sc.BaseDateTimeIn[i].GetAsDouble();
        const double PrevBarTime = sc.BaseDateTimeIn[i - 1].GetAsDouble();
        BarSeconds = (ThisBarTime - PrevBarTime) * 86400.0;
        if (BarSeconds < 1.0)
            BarSeconds = 1.0;
    }
    Sg_Intensity[i] = (float)(sc.Volume[i] / BarSeconds);

    float RangeTicks = (Tick > 0.0f) ? (Range / Tick) : 1.0f;
    if (RangeTicks < 1.0f)
        RangeTicks = 1.0f;
    Sg_AbsRatio[i] = std::fabs(Delta) / RangeTicks;

    // =======================================================================
    //  Trading day / session bookkeeping
    // =======================================================================
    const double BarTimeDays    = sc.BaseDateTimeIn[i].GetAsDouble();
    const SCDateTime BarDT      = sc.BaseDateTimeIn[i];
    const SCDateTime TradingDay = sc.GetTradingDayDate(BarDT);
    const int DayNum            = TradingDay.GetDate();
    const int TimeOfDay         = BarDT.GetTimeInSeconds();

    const int RthStartSec   = In_RthStart.GetTime();
    const int LastEntrySec  = In_LastEntry.GetTime();
    const int FlattenSec    = In_FlattenTime.GetTime();
    const int DayEndSec     = In_DaySessionEnd.GetTime();
    const int NoTradeStart  = In_NoTradeStart.GetTime();
    const int NoTradeEnd    = In_NoTradeEnd.GetTime();
    const int OrEndSec      = RthStartSec + In_OrMinutes.GetInt() * 60;

    // -- New trading day -----------------------------------------------------
    if (DayNum != St->TradingDayNum)
    {
        if (St->TradingDayNum != -1 && In_LogDiag.GetYesNo())
        {
            SCString D;
            D.Format("DIAG day %d | BLOCKED: halted %d (reason %d), in-position %d, "
                     "out-of-window %d || levels: armed %d, swept %d, absorbed %d, "
                     "burned/dead %d || ABS-CHECKS %d -> failed: aggression %d, "
                     "ratio %d, intensity %d, close-pos %d || triggered %d, "
                     "far-entry %d, unsized %d, ORDERS %d || bars %d",
                     St->TradingDayNum, St->DiagBlockedHalt, St->HaltReason,
                     St->DiagBlockedPosition, St->DiagBlockedWindow,
                     St->DiagArmed, St->DiagSwept, St->DiagAbsorbed,
                     St->DiagBurnedDead, St->DiagAbsChecks, St->DiagFailAggr,
                     St->DiagFailRatio, St->DiagFailIntensity,
                     St->DiagFailClosePos, St->DiagTriggered, St->DiagFarEntry,
                     St->DiagUnsized, St->DiagOrders, St->DiagBarsSeen);
            sc.AddMessageToLog(D, 0);
        }

        // Yesterday's RTH range becomes today's PDH/PDL
        if (St->RthHigh > -FLT_MAX && St->RthLow < FLT_MAX)
        {
            St->PrevRthHigh = St->RthHigh;
            St->PrevRthLow  = St->RthLow;

            const int Window = In_DailyRangeDays.GetInt();
            St->DailyRanges[St->DailyRangeIdx % 64] = St->RthHigh - St->RthLow;
            St->DailyRangeIdx++;
            if (St->DailyRangeCount < 64)
                St->DailyRangeCount++;

            int N = St->DailyRangeCount;
            if (N > Window) N = Window;
            double Acc = 0.0;
            for (int k = 1; k <= N; ++k)
            {
                int Slot = (St->DailyRangeIdx - k) % 64;
                if (Slot < 0) Slot += 64;
                Acc += St->DailyRanges[Slot];
            }
            St->DailyAtr = (N > 0) ? (float)(Acc / N) : 0.0f;
        }

        St->TradingDayNum = DayNum;
        St->RthOpened = 0;
        St->OrComplete = 0;
        St->OnHigh = -FLT_MAX; St->OnLow = FLT_MAX;
        St->RthHigh = -FLT_MAX; St->RthLow = FLT_MAX;
        St->OrHigh = -FLT_MAX;  St->OrLow = FLT_MAX;
        St->VwapSumPV = St->VwapSumV = St->VwapSumP2V = 0.0;

        St->TradeCount = 0;
        St->ConsecLosses = 0;
        St->LossesToday = 0;
        St->Halted = 0;
        St->HaltReason = HALT_NONE;
        St->DailyPeak = 0.0;
        St->PrevClosedPL = 0.0;

        St->DiagBarsSeen = St->DiagBlockedHalt = St->DiagBlockedPosition = 0;
        St->DiagBlockedWindow = St->DiagArmed = St->DiagSwept = 0;
        St->DiagAbsorbed = St->DiagTriggered = St->DiagUnsized = 0;
        St->DiagOrders = St->DiagBurnedDead = 0;
        St->DiagAbsChecks = St->DiagFailAggr = St->DiagFailRatio = 0;
        St->DiagFailIntensity = St->DiagFailClosePos = St->DiagFarEntry = 0;

        for (int L = 0; L < LVL_COUNT; ++L)
        {
            St->Levels[L].State = ST_IDLE;
            St->Levels[L].Burned = 0;
            St->Levels[L].Valid = 0;
        }
    }

    // With AutoLoop the study is re-called on every incoming tick for the
    // forming bar, so an unguarded counter reports study calls rather than
    // bars - which is why the previous run logged 77,000 'bars' per day.
    if (i != St->LastCountedBar)
    {
        St->DiagBarsSeen++;
        St->LastCountedBar = i;
    }

    const bool InDaySession = (TimeOfDay < DayEndSec);

    // -- Overnight extremes (bars before RTH open, inside this trading day) --
    if (TimeOfDay < RthStartSec)
    {
        if (High > St->OnHigh) St->OnHigh = High;
        if (Low  < St->OnLow)  St->OnLow  = Low;
    }
    else if (InDaySession)
    {
        if (!St->RthOpened)
            St->RthOpened = 1;

        if (High > St->RthHigh) St->RthHigh = High;
        if (Low  < St->RthLow)  St->RthLow  = Low;

        // Opening range
        if (TimeOfDay < OrEndSec)
        {
            if (High > St->OrHigh) St->OrHigh = High;
            if (Low  < St->OrLow)  St->OrLow  = Low;
        }
        else
        {
            St->OrComplete = 1;
        }

        // Session VWAP
        const double TypicalPrice = (High + Low + Close) / 3.0;
        const double Vol = (double)sc.Volume[i];
        if (Vol > 0.0)
        {
            St->VwapSumPV  += TypicalPrice * Vol;
            St->VwapSumV   += Vol;
            St->VwapSumP2V += TypicalPrice * TypicalPrice * Vol;
        }
    }

    // -- VWAP and bands ------------------------------------------------------
    float Vwap = 0.0f, VwapSd = 0.0f;
    if (St->VwapSumV > 0.0)
    {
        Vwap = (float)(St->VwapSumPV / St->VwapSumV);
        double Var = (St->VwapSumP2V / St->VwapSumV) - ((double)Vwap * Vwap);
        if (Var > 0.0)
            VwapSd = (float)std::sqrt(Var);
    }

    const float B1 = In_VwapBand1.GetFloat();
    const float B2 = In_VwapBand2.GetFloat();

    // -- Prior day value area (referenced study) -----------------------------
    float PriorVah = 0.0f, PriorVal = 0.0f;
    if (In_UseVA.GetYesNo() && In_VbPStudy.GetStudyID() != 0)
    {
        SCFloatArray VahArr, ValArr;
        sc.GetStudyArrayUsingID(In_VbPStudy.GetStudyID(), In_VbPVahSg.GetInt(), VahArr);
        sc.GetStudyArrayUsingID(In_VbPStudy.GetStudyID(), In_VbPValSg.GetInt(), ValArr);
        if (VahArr.GetArraySize() > i) PriorVah = VahArr[i];
        if (ValArr.GetArraySize() > i) PriorVal = ValArr[i];
    }

    // =======================================================================
    //  Publish levels into the state struct and the plots
    // =======================================================================
    {
        const bool OnReady = St->RthOpened && St->OnHigh > -FLT_MAX && St->OnLow < FLT_MAX;
        const bool PdReady = St->PrevRthHigh > 0.0f && St->PrevRthLow > 0.0f;
        const bool OrReady = St->OrComplete && St->OrHigh > -FLT_MAX && St->OrLow < FLT_MAX;
        const bool VwReady = (VwapSd > 0.0f);

        struct { int Id; float Price; bool Ready; } Src[] =
        {
            { LVL_ONL,     St->OnLow,           In_UseON.GetYesNo()   && OnReady },
            { LVL_PDL,     St->PrevRthLow,      In_UsePD.GetYesNo()   && PdReady },
            { LVL_VAL,     PriorVal,            In_UseVA.GetYesNo()   && PriorVal > 0.0f },
            { LVL_VWAP_L1, Vwap - B1 * VwapSd,  In_UseVWAP.GetYesNo() && VwReady },
            { LVL_VWAP_L2, Vwap - B2 * VwapSd,  In_UseVWAP.GetYesNo() && VwReady },
            { LVL_ORL,     St->OrLow,           In_UseOR.GetYesNo()   && OrReady },
            { LVL_ONH,     St->OnHigh,          In_UseON.GetYesNo()   && OnReady },
            { LVL_PDH,     St->PrevRthHigh,     In_UsePD.GetYesNo()   && PdReady },
            { LVL_VAH,     PriorVah,            In_UseVA.GetYesNo()   && PriorVah > 0.0f },
            { LVL_VWAP_U1, Vwap + B1 * VwapSd,  In_UseVWAP.GetYesNo() && VwReady },
            { LVL_VWAP_U2, Vwap + B2 * VwapSd,  In_UseVWAP.GetYesNo() && VwReady },
            { LVL_ORH,     St->OrHigh,          In_UseOR.GetYesNo()   && OrReady }
        };

        for (int k = 0; k < LVL_COUNT; ++k)
        {
            St->Levels[Src[k].Id].Price = Src[k].Price;
            St->Levels[Src[k].Id].Valid = Src[k].Ready ? 1 : 0;
        }

        Sg_ONH[i]     = St->Levels[LVL_ONH].Valid ? St->Levels[LVL_ONH].Price : 0.0f;
        Sg_ONL[i]     = St->Levels[LVL_ONL].Valid ? St->Levels[LVL_ONL].Price : 0.0f;
        Sg_PDH[i]     = St->Levels[LVL_PDH].Valid ? St->Levels[LVL_PDH].Price : 0.0f;
        Sg_PDL[i]     = St->Levels[LVL_PDL].Valid ? St->Levels[LVL_PDL].Price : 0.0f;
        Sg_ORH[i]     = St->Levels[LVL_ORH].Valid ? St->Levels[LVL_ORH].Price : 0.0f;
        Sg_ORL[i]     = St->Levels[LVL_ORL].Valid ? St->Levels[LVL_ORL].Price : 0.0f;
        Sg_VAH[i]     = St->Levels[LVL_VAH].Valid ? St->Levels[LVL_VAH].Price : 0.0f;
        Sg_VAL[i]     = St->Levels[LVL_VAL].Valid ? St->Levels[LVL_VAL].Price : 0.0f;
        Sg_VWAP[i]    = Vwap;
        Sg_VWAP_U1[i] = St->Levels[LVL_VWAP_U1].Valid ? St->Levels[LVL_VWAP_U1].Price : 0.0f;
        Sg_VWAP_L1[i] = St->Levels[LVL_VWAP_L1].Valid ? St->Levels[LVL_VWAP_L1].Price : 0.0f;
        Sg_VWAP_U2[i] = St->Levels[LVL_VWAP_U2].Valid ? St->Levels[LVL_VWAP_U2].Price : 0.0f;
        Sg_VWAP_L2[i] = St->Levels[LVL_VWAP_L2].Valid ? St->Levels[LVL_VWAP_L2].Price : 0.0f;
    }

    // =======================================================================
    //  Position and P/L
    // =======================================================================
    s_SCPositionData Pos;
    sc.GetTradePosition(Pos);

    const double ClosedPL = Pos.DailyProfitLoss;
    const double OpenPL   = Pos.OpenProfitLoss;
    const double DailyPL  = ClosedPL + OpenPL;

    // Peak tracks CLOSED P/L only. Giveback is about surrendering realised
    // gains; including open P/L makes it measure ordinary intra-trade
    // excursion instead, and with ~$230 risk per trade a single normal swing
    // trips a $350 giveback and force-flattens a position mid-trade.
    if (ClosedPL > St->DailyPeak)
        St->DailyPeak = ClosedPL;

    // -- Detect a closed trade ----------------------------------------------
    if (St->PrevPositionQty != 0 && Pos.PositionQuantity == 0)
    {
        const double TradePL = ClosedPL - St->PrevClosedPL;
        St->TradeCount++;

        if (TradePL < 0.0)
        {
            St->ConsecLosses++;
            St->LossesToday++;
        }
        else
        {
            St->ConsecLosses = 0;
        }

        St->PrevClosedPL = ClosedPL;
        St->BreakevenDone = 0;
        St->PositionDir = 0;

        if (In_Debug.GetYesNo())
        {
            SCString M;
            M.Format("Trade closed. P/L %.2f | trades %d | consec losses %d | daily %.2f",
                     TradePL, St->TradeCount, St->ConsecLosses, DailyPL);
            sc.AddMessageToLog(M, 0);
        }
    }
    St->PrevPositionQty = Pos.PositionQuantity;

    // =======================================================================
    //  Governors - checked before any signal logic
    // =======================================================================
    if (!St->Halted)
    {
        int Reason = HALT_NONE;

        if (In_DailyTarget.GetFloat() > 0.0f && DailyPL >= In_DailyTarget.GetFloat())
            Reason = HALT_TARGET;
        else if (In_DailyLossLimit.GetFloat() > 0.0f && DailyPL <= -In_DailyLossLimit.GetFloat())
            Reason = HALT_LOSS;
        else if (In_MaxGiveback.GetFloat() > 0.0f && St->DailyPeak > 0.0 &&
                 (St->DailyPeak - ClosedPL) >= In_MaxGiveback.GetFloat())
            Reason = HALT_GIVEBACK;
        else if (In_MaxTrades.GetInt() > 0 && St->TradeCount >= In_MaxTrades.GetInt())
            Reason = HALT_MAXTRADES;
        else if (In_MaxConsecLoss.GetInt() > 0 && St->ConsecLosses >= In_MaxConsecLoss.GetInt())
            Reason = HALT_CONSECLOSS;

        if (Reason != HALT_NONE)
        {
            St->Halted = 1;
            St->HaltReason = Reason;

            if (Pos.PositionQuantity != 0 || Pos.WorkingOrdersExist != 0)
                sc.FlattenAndCancelAllOrders();

            SCString M;
            M.Format("HALT day %d reason %d | daily P/L %.2f | peak %.2f | trades %d",
                     St->TradingDayNum, Reason, DailyPL, St->DailyPeak, St->TradeCount);
            sc.AddMessageToLog(M, 1);
        }
    }

    // =======================================================================
    //  End-of-day flatten. Deliberately does NOT set the halt flag, so the
    //  flag can never carry across the trading-day roll.
    // =======================================================================
    if (InDaySession && TimeOfDay >= FlattenSec)
    {
        if (Pos.PositionQuantity != 0 || Pos.WorkingOrdersExist != 0)
            sc.FlattenAndCancelAllOrders();
        return;
    }

    // Only act on closed bars from here down.
    if (sc.GetBarHasClosedStatus(i) != BHCS_BAR_HAS_CLOSED)
        return;

    // =======================================================================
    //  Breakeven stop management
    // =======================================================================
    if (Pos.PositionQuantity != 0 && !St->BreakevenDone &&
        In_BreakevenR.GetFloat() > 0.0f && St->RiskPoints > 0.0)
    {
        const double Favourable = (St->PositionDir > 0)
            ? (Close - St->EntryPrice)
            : (St->EntryPrice - Close);

        if (Favourable >= In_BreakevenR.GetFloat() * St->RiskPoints)
        {
            const double BePrice = (St->PositionDir > 0)
                ? (St->EntryPrice + Tick)
                : (St->EntryPrice - Tick);

            // Find the working stop order attached to this position and move it.
            s_SCTradeOrder Order;
            int OrderIdx = 0;
            while (sc.GetOrderByIndex(OrderIdx, Order) != 0)
            {
                const bool IsStop =
                    (Order.OrderTypeAsInt == SCT_ORDERTYPE_STOP ||
                     Order.OrderTypeAsInt == SCT_ORDERTYPE_STOP_LIMIT);

                // This block only runs with a filled position, so the attached
                // stop has already been promoted to OPEN. The pending-child
                // status is checked as well in case of a partial-fill race.
                const bool IsWorking =
                    (Order.OrderStatusCode == SCT_OSC_OPEN ||
                     Order.OrderStatusCode == SCT_OSC_PENDING_CHILD_SERVER);

                if (IsStop && IsWorking)
                {
                    s_SCNewOrder Mod;
                    Mod.InternalOrderID = Order.InternalOrderID;
                    Mod.Price1 = BePrice;
                    sc.ModifyOrder(Mod);

                    St->BreakevenDone = 1;

                    if (In_Debug.GetYesNo())
                    {
                        SCString M;
                        M.Format("Stop moved to breakeven at %.2f", BePrice);
                        sc.AddMessageToLog(M, 0);
                    }
                    break;
                }
                ++OrderIdx;
            }
        }
    }

    // =======================================================================
    //  Entry gating
    // =======================================================================
    if (St->Halted)
    {
        St->DiagBlockedHalt++;
        return;
    }

    if (Pos.PositionQuantity != 0 || Pos.WorkingOrdersExist != 0)
    {
        St->DiagBlockedPosition++;
        return;
    }

    const bool InEntryWindow =
        InDaySession &&
        St->RthOpened &&
        TimeOfDay >= RthStartSec &&
        TimeOfDay <= LastEntrySec &&
        !(TimeOfDay >= NoTradeStart && TimeOfDay < NoTradeEnd);

    if (!InEntryWindow)
    {
        St->DiagBlockedWindow++;
        return;
    }

    if (Atr <= 0.0f || Range <= 0.0f)
        return;

    // =======================================================================
    //  Order-flow statistics for this bar
    // =======================================================================
    const int Lookback = In_DeltaLookback.GetInt();
    const float DeltaSd     = TrailingStdDev(Sg_Delta, i - 1, Lookback);
    const float AbsRatioMed = TrailingMedian(Sg_AbsRatio, i - 1, Lookback);
    const float IntensityMed= TrailingMedian(Sg_Intensity, i - 1, Lookback);

    if (DeltaSd <= 0.0f || AbsRatioMed <= 0.0f || IntensityMed <= 0.0f)
        return;

    const float ClosePosLong  = (Close - Low)  / Range;   // 1.0 = closed on the high
    const float ClosePosShort = (High - Close) / Range;

    // Tolerance in price terms
    // Scale source. The execution bar's ATR is the wrong ruler here: on a
    // 2000-volume MNQ chart it is only a few points, so every distance derived
    // from it lands inside the noise band. A sweep of the overnight low is a
    // feature of the DAY's range, so the day's range is what it gets measured
    // against. Requires a few days of history before anything can trade.
    if (St->DailyRangeCount < 3 || St->DailyAtr <= 0.0f)
        return;

    const float VolScale = St->DailyAtr;

    float Tolerance = In_TolAtrMult.GetFloat() * VolScale;
    const float MinTol = In_TolMinTicks.GetInt() * Tick;
    if (Tolerance < MinTol)
        Tolerance = MinTol;

    const float SweepPenetration = In_MinSweepTicks.GetInt() * Tick;

    // =======================================================================
    //  Per-level state machine
    // =======================================================================
    int TriggerLevel = -1;
    int TriggerDir = 0;

    for (int L = 0; L < LVL_COUNT; ++L)
    {
        s_LevelState& Lv = St->Levels[L];

        if (!Lv.Valid || Lv.Burned || Lv.State == ST_DEAD)
            continue;

        const int Dir = LEVEL_DIRECTION[L];   // +1 long setup, -1 short setup
        const float P = Lv.Price;

        // -- Expiry ----------------------------------------------------------
        if (Lv.State != ST_IDLE)
        {
            // Expiry in wall-clock minutes. Bar counts are meaningless on a
            // volume chart: 8 bars is ~4 minutes in fast trade and ~40 in slow,
            // so a bar-count window let setups expire before they could form.
            int MaxMinutes = 0;
            if (Lv.State == ST_ARMED)         MaxMinutes = In_ArmedExpiry.GetInt();
            else if (Lv.State == ST_SWEPT)    MaxMinutes = In_SweptExpiry.GetInt();
            else if (Lv.State == ST_ABSORBED) MaxMinutes = In_AbsorbExpiry.GetInt();

            const double AgeMinutes = (BarTimeDays - Lv.StateTimeDays) * 1440.0;

            if (MaxMinutes > 0 && AgeMinutes > (double)MaxMinutes)
            {
                Lv.State = ST_IDLE;
                Lv.StateBarIndex = i;
                Lv.StateTimeDays = BarTimeDays;
            }
        }

        // -- IDLE -> ARMED : price came within tolerance of a fresh level -----
        if (Lv.State == ST_IDLE)
        {
            const bool Tagged = (Dir > 0)
                ? (Low  <= P + Tolerance && Low  >= P - Tolerance * 4.0f)
                : (High >= P - Tolerance && High <= P + Tolerance * 4.0f);

            if (Tagged)
            {
                Lv.State = ST_ARMED;
                Lv.StateBarIndex = i;
                Lv.StateTimeDays = BarTimeDays;
                St->DiagArmed++;
            }
            continue;
        }

        // -- ARMED -> SWEPT : price traded through the level ------------------
        if (Lv.State == ST_ARMED)
        {
            const bool Swept = (Dir > 0)
                ? (Low  <= P - SweepPenetration)
                : (High >= P + SweepPenetration);

            if (Swept)
            {
                Lv.State = ST_SWEPT;
                Lv.StateBarIndex = i;
                Lv.StateTimeDays = BarTimeDays;
                Lv.SweepExtreme = (Dir > 0) ? Low : High;
                Lv.SweepDelta = Delta;
                Lv.SweepBarHigh = High;
                Lv.SweepBarLow = Low;
                St->DiagSwept++;
                // fall through: absorption can qualify on the sweep bar itself
            }
            else
            {
                continue;
            }
        }

        // -- SWEPT -> ABSORBED / DEAD ----------------------------------------
        if (Lv.State == ST_SWEPT)
        {
            // Extended too far past the extreme: this was a move, not a sweep.
            const float Invalidation = In_InvalidAtrMult.GetFloat() * St->DailyAtr;
            const bool Extended = (Dir > 0)
                ? (Low  < Lv.SweepExtreme - Invalidation)
                : (High > Lv.SweepExtreme + Invalidation);

            if (Extended)
            {
                Lv.State = ST_DEAD;
                St->DiagBurnedDead++;
                continue;
            }

            // Track the running extreme while still in SWEPT.
            if (Dir > 0 && Low  < Lv.SweepExtreme) Lv.SweepExtreme = Low;
            if (Dir < 0 && High > Lv.SweepExtreme) Lv.SweepExtreme = High;

            const bool Aggression = (Dir > 0)
                ? (Delta <= -In_DeltaSigma.GetFloat() * DeltaSd)
                : (Delta >=  In_DeltaSigma.GetFloat() * DeltaSd);

            const bool Absorbing =
                Sg_AbsRatio[i] >= In_AbsRatioMult.GetFloat() * AbsRatioMed;

            const bool IntensityOk =
                Sg_Intensity[i] >= In_IntensityMult.GetFloat() * IntensityMed;

            const bool ClosePosOk = (Dir > 0)
                ? (ClosePosLong  >= In_ClosePosInBar.GetFloat())
                : (ClosePosShort >= In_ClosePosInBar.GetFloat());

            // Count every condition that failed, not just the first. The DIAG
            // line then names which filter is the bottleneck instead of only
            // reporting that absorption did not happen.
            St->DiagAbsChecks++;
            if (!Aggression)   St->DiagFailAggr++;
            if (!Absorbing)    St->DiagFailRatio++;
            if (!IntensityOk)  St->DiagFailIntensity++;
            if (!ClosePosOk)   St->DiagFailClosePos++;

            if (Aggression && Absorbing && IntensityOk && ClosePosOk)
            {
                Lv.State = ST_ABSORBED;
                Lv.StateBarIndex = i;
                Lv.StateTimeDays = BarTimeDays;
                Lv.SweepDelta = Delta;
                Lv.SweepBarHigh = High;
                Lv.SweepBarLow = Low;
                St->DiagAbsorbed++;
            }
            continue;
        }

        // -- ABSORBED -> TRIGGER / DEAD --------------------------------------
        if (Lv.State == ST_ABSORBED)
        {
            const bool NewExtreme = (Dir > 0)
                ? (Low  < Lv.SweepExtreme - Tick)
                : (High > Lv.SweepExtreme + Tick);

            if (NewExtreme)
            {
                Lv.State = ST_DEAD;
                St->DiagBurnedDead++;
                continue;
            }

            // Selling/buying intensity must be weakening.
            const bool Weakening =
                (std::fabs(Delta) < 0.5f * std::fabs(Lv.SweepDelta)) ||
                (Dir > 0 ? Delta > 0.0f : Delta < 0.0f);

            // Reclaim of the swept level, or a break of the sweep bar's extreme.
            const bool Reclaimed = (Dir > 0)
                ? (Close > P || Close > Lv.SweepBarHigh)
                : (Close < P || Close < Lv.SweepBarLow);

            if (Weakening && Reclaimed)
            {
                TriggerLevel = L;
                TriggerDir = Dir;
                St->DiagTriggered++;
                break;
            }
        }
    }

    if (TriggerLevel < 0)
        return;

    s_LevelState& Trig = St->Levels[TriggerLevel];

    // =======================================================================
    //  Confluence requirement. Standards tighten as the day progresses;
    //  nothing here ever loosens them.
    // =======================================================================
    // scstructures.h defines a max(a,b) preprocessor macro, which mangles any
    // call to std::max. Do not reintroduce std::max or std::min in this file.
    int RequiredConfluence = 1;
    if (St->TradeCount >= 1 && In_ConfAfterFirst.GetInt() > RequiredConfluence)
        RequiredConfluence = In_ConfAfterFirst.GetInt();
    if (St->LossesToday >= 1 && In_ConfAfterLoss.GetInt() > RequiredConfluence)
        RequiredConfluence = In_ConfAfterLoss.GetInt();

    // Absolute distance in points. Previously this was a multiple of the ATR-
    // scaled tolerance, which on volume bars came to ~2 points — two distinct
    // levels are almost never that close, so confluence >= 2 was unreachable
    // and the post-loss tightening silently became a one-trade-per-day cap.
    const float ClusterDist = In_ClusterMult.GetFloat();
    int Confluence = 0;
    for (int L = 0; L < LVL_COUNT; ++L)
    {
        if (!St->Levels[L].Valid)
            continue;
        if (LEVEL_DIRECTION[L] != TriggerDir)
            continue;
        if (std::fabs(St->Levels[L].Price - Trig.Price) <= ClusterDist)
            Confluence++;
    }

    if (Confluence < RequiredConfluence)
    {
        Trig.State = ST_DEAD;
        St->DiagBurnedDead++;
        if (In_Debug.GetYesNo())
        {
            SCString M;
            M.Format("Trigger rejected: confluence %d < required %d",
                     Confluence, RequiredConfluence);
            sc.AddMessageToLog(M, 0);
        }
        return;
    }

    // =======================================================================
    //  Stop, sizing, target
    // =======================================================================
    float StopPad = In_StopAtrMult.GetFloat() * St->DailyAtr;
    const float MinPad = In_StopMinTicks.GetInt() * Tick;
    if (StopPad < MinPad)
        StopPad = MinPad;

    const double EntryPrice = Close;
    double StopPrice = (TriggerDir > 0)
        ? (Trig.SweepExtreme - StopPad)
        : (Trig.SweepExtreme + StopPad);

    double RiskPoints = (TriggerDir > 0)
        ? (EntryPrice - StopPrice)
        : (StopPrice - EntryPrice);

    // Absolute clamp on the stop, in points. Without this, everything scales
    // off ATR — and on a 2000-volume MNQ chart ATR(14) is only a few points, so
    // the structural stop lands inside the instrument's own noise band and gets
    // taken out on essentially every trade regardless of whether the read was
    // right. The trend-following study clamps its stop to 12-45 points for the
    // same reason; omitting a floor here was an error.
    // The reclaim trigger allows a microstructure break of the sweep bar's
    // extreme, which on a volume chart can sit a long way from the level. An
    // entry 13-16 points past the level has already spent the edge it was
    // fading, so cap how far from the level an entry may be taken.
    const double EntryDistance = std::fabs(EntryPrice - Trig.Price);
    const double MaxEntryDist = In_MaxEntryDist.GetFloat() * St->DailyAtr;

    if (MaxEntryDist > 0.0 && EntryDistance > MaxEntryDist)
    {
        Trig.State = ST_DEAD;
        St->DiagFarEntry++;
        if (In_Debug.GetYesNo())
        {
            SCString M;
            M.Format("Trigger rejected: entry %.2f pts from level, max %.2f",
                     EntryDistance, MaxEntryDist);
            sc.AddMessageToLog(M, 0);
        }
        return;
    }

    const double MinStopPts = In_MinStopPoints.GetFloat();
    const double MaxStopPts = In_MaxStopPoints.GetFloat();

    if (MinStopPts > 0.0 && RiskPoints < MinStopPts)
    {
        RiskPoints = MinStopPts;
        StopPrice = (TriggerDir > 0)
            ? (EntryPrice - RiskPoints)
            : (EntryPrice + RiskPoints);
    }

    // A sweep too wide to stop sensibly is not a trade. Widening the stop to
    // fit would keep risk constant only by shrinking size until the winner
    // can't pay for the losers.
    if (MaxStopPts > 0.0 && RiskPoints > MaxStopPts)
    {
        Trig.State = ST_DEAD;
        St->DiagBurnedDead++;
        if (In_Debug.GetYesNo())
        {
            SCString M;
            M.Format("Trigger rejected: stop %.2f pts exceeds maximum %.2f",
                     RiskPoints, MaxStopPts);
            sc.AddMessageToLog(M, 0);
        }
        return;
    }

    if (RiskPoints <= 0.0 || Tick <= 0.0f)
        return;

    const double RiskPerContract = (RiskPoints / Tick) * sc.CurrencyValuePerTick;
    if (RiskPerContract <= 0.0)
        return;

    // Risk budget: the configured amount, additionally clipped to the room
    // remaining before the daily loss limit. The loss limit cannot be breached
    // by an entry this study places.
    double RiskBudget = In_RiskPerTrade.GetFloat();
    if (In_DailyLossLimit.GetFloat() > 0.0f)
    {
        const double Room = In_DailyLossLimit.GetFloat() + DailyPL
                            - In_LossBuffer.GetFloat();
        if (Room < RiskBudget)
            RiskBudget = Room;
    }

    if (RiskBudget <= 0.0)
    {
        St->DiagUnsized++;
        return;
    }

    int Qty = (int)std::floor(RiskBudget / RiskPerContract);
    if (Qty > In_MaxContracts.GetInt())
        Qty = In_MaxContracts.GetInt();

    if (Qty < 1)
    {
        St->DiagUnsized++;
        SCString M;
        M.Format("Cannot size a contract: risk/contract %.2f exceeds budget %.2f "
                 "(stop %.2f pts). Check symbol and Risk Per Trade.",
                 RiskPerContract, RiskBudget, RiskPoints);
        sc.AddMessageToLog(M, 1);
        return;
    }

    // -- Target: nearest opposing level inside the R clamp -------------------
    double TargetPoints = In_DefaultTargetR.GetFloat() * RiskPoints;
    const double MinTargetPts = In_MinTargetR.GetFloat() * RiskPoints;
    const double MaxTargetPts = In_MaxTargetR.GetFloat() * RiskPoints;

    {
        double Best = -1.0;
        for (int L = 0; L < LVL_COUNT; ++L)
        {
            if (!St->Levels[L].Valid)
                continue;
            if (LEVEL_DIRECTION[L] == TriggerDir)
                continue;   // opposing levels only

            const double Dist = (TriggerDir > 0)
                ? (St->Levels[L].Price - EntryPrice)
                : (EntryPrice - St->Levels[L].Price);

            if (Dist >= MinTargetPts && Dist <= MaxTargetPts)
            {
                if (Best < 0.0 || Dist < Best)
                    Best = Dist;
            }
        }
        if (Best > 0.0)
            TargetPoints = Best;
    }

    if (TargetPoints < MinTargetPts) TargetPoints = MinTargetPts;
    if (TargetPoints > MaxTargetPts) TargetPoints = MaxTargetPts;

    // =======================================================================
    //  Submit
    // =======================================================================
    s_SCNewOrder NewOrder;
    NewOrder.OrderQuantity = Qty;
    NewOrder.OrderType = SCT_ORDERTYPE_MARKET;
    NewOrder.TimeInForce = SCT_TIF_DAY;
    NewOrder.Stop1Offset = RiskPoints;
    NewOrder.Target1Offset = TargetPoints;

    const int Result = (TriggerDir > 0)
        ? sc.BuyEntry(NewOrder)
        : sc.SellEntry(NewOrder);

    Trig.Burned = 1;
    Trig.State = ST_DEAD;

    if (Result > 0)
    {
        St->DiagOrders++;
        St->EntryPrice = EntryPrice;
        St->StopPrice = StopPrice;
        St->RiskPoints = RiskPoints;
        St->PositionDir = TriggerDir;
        St->BreakevenDone = 0;
        St->PrevClosedPL = ClosedPL;

        if (TriggerDir > 0) Sg_LongMark[i]  = Low  - Atr * 0.5f;
        else                Sg_ShortMark[i] = High + Atr * 0.5f;

        SCString M;
        M.Format("ENTRY %s qty %d @ %.2f | level %d @ %.2f | stop %.2f (%.2f pts, $%.2f) "
                 "| target %.2f pts (%.2fR) | confluence %d | risk $%.2f",
                 TriggerDir > 0 ? "LONG" : "SHORT", Qty, EntryPrice,
                 TriggerLevel, Trig.Price, StopPrice, RiskPoints, RiskPerContract,
                 TargetPoints, TargetPoints / RiskPoints, Confluence,
                 RiskPerContract * Qty);
        sc.AddMessageToLog(M, 0);
    }
    else
    {
        SCString M;
        M.Format("Order rejected by Sierra, code %d", Result);
        sc.AddMessageToLog(M, 1);
    }
}
