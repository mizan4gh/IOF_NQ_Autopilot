// =============================================================================
//  NQ / MNQ Intraday Trend-Following System  --  Prop Evaluation Edition
//  Sierra Chart ACSIL study (C++)
//
//  Timeframe : 5-minute chart (works on 3m-15m; defaults tuned for 5m)
//  Instrument: NQ (E-mini Nasdaq 100) or MNQ (Micro)
//  Risk model: Daily profit target  $1000  -> stop trading for the day
//              Daily loss limit     $600   -> flatten + stop trading for the day
//              Per-trade risk is sized so the daily loss limit is never breached
//
//  NOTHING HERE IS FINANCIAL ADVICE. Futures trading involves substantial risk
//  of loss. Test in Replay / Sim before ever enabling live order routing.
// =============================================================================

#include "sierrachart.h"

SCDLLName("NQ Trend Following - Prop Eval")

// -----------------------------------------------------------------------------
// Persistent variable keys
// -----------------------------------------------------------------------------
enum PersistIntKeys
{
    PI_TRADING_DAY      = 1,   // trading-day date of the last processed bar
    PI_HALTED           = 2,   // 1 = no more entries today
    PI_HALT_REASON      = 3,   // 0 none, 1 target, 2 loss limit, 4 giveback, 5 max trades, 6 consec losses, 7 session end
    PI_TRADES_TODAY     = 4,
    PI_CONSEC_LOSSES    = 5,
    PI_LAST_ENTRY_BAR   = 6,
    PI_FLATTEN_SENT     = 7,
    PI_SIZE_WARN_SENT   = 8,   // one "cannot size a contract" warning per day

    // Diagnostic counters -- why signals were rejected today
    PI_DG_EVALUATED     = 20,
    PI_DG_ATR           = 21,
    PI_DG_ADX           = 22,
    PI_DG_RANGE         = 23,
    PI_DG_TREND         = 24,
    PI_DG_PULLBACK      = 25,
    PI_DG_TRIGGER       = 26,
    PI_DG_SIGNALS       = 27,
    PI_DG_ORDERS        = 28,

    // Funnel counters -- bars blocked BEFORE the filters are even reached
    PI_DG_BLK_HALT      = 29,  // halted for the day
    PI_DG_BLK_POS       = 30,  // a position is already open
    PI_DG_BLK_WORK      = 31,  // working orders exist but no position
    PI_DG_BLK_SESSION   = 32,  // outside the entry window
    PI_DG_BLK_SIZE      = 33,  // signal fired but could not be sized

    // Order submission outcome. Without these, a study that computes signals
    // but never trades looks identical to one whose signals are wrong: the
    // funnel ends "signals N, unsized 0, ORDERS 0" and says nothing about why.
    PI_DG_REJECTS       = 34,  // BuyEntry/SellEntry returned <= 0
    PI_DG_LAST_RC       = 35,  // the last such return code
    PI_RC_LOG_SENT      = 36   // one decoded rejection message per day
};

// Decode the ACSIL order return code. The -899x family are SCT_SKIPPED_* --
// Sierra declined to submit and nothing reached the trade service; only -1..-9
// are real submission errors. Mixing the two up wastes a day chasing a broker
// problem that never happened.
static const char* NQTF_OrderRCText(int rc)
{
    switch (rc)
    {
        case  -1:    return "SCTRADING_ORDER_ERROR (real submission failure)";
        case  -2:    return "NOT_OCO_ORDER_TYPE";
        case  -3:    return "ATTACHED_ORDER_OFFSET_NOT_SUPPORTED_WITH_MARKET_PARENT";
        case  -4:    return "UNSUPPORTED_ATTACHED_ORDER";
        case  -5:    return "SYMBOL_SETTINGS_NOT_FOUND";
        case  -6:    return "GENERAL_NULL_POINTER_ERROR";
        case  -8:    return "UNSUPPORTED_ORDER_TYPE";
        case  -9:    return "ERROR_SETTING_ORDER_PRICES";
        case -8999:  return "SKIPPED_DOWNLOADING_HISTORICAL_DATA (not sent)";
        case -8998:  return "SKIPPED_FULL_RECALC -- studies cannot trade on "
                            "historical bars. Use Replay, not a chart reload.";
        case -8997:  return "SKIPPED_ONLY_ONE_TRADE_PER_BAR (not sent)";
        case -8996:  return "SKIPPED_INVALID_INDEX_SPECIFIED (not sent)";
        case -8995:  return "SKIPPED_TOO_MANY_NEW_BARS_DURING_UPDATE (not sent)";
        case -8994:  return "SKIPPED_AUTO_TRADING_DISABLED -- enable Auto "
                            "Trading and/or Trade Simulation for this chart.";
        case 0:      return "no order returned";
        default:     return "unrecognised return code";
    }
}

enum PersistDoubleKeys
{
    PD_PEAK_DAILY_PL      = 1, // highest daily P/L reached today
    PD_PREV_CLOSED_PL     = 2, // closed P/L snapshot, used to detect win/loss
    PD_DAY_START_CLOSED   = 3  // closed P/L at the start of this trading day
};

// -----------------------------------------------------------------------------
// RISK UNIT AND BUDGET SIZING
//
// Fixes a defect in the original sizing line:
//
//     Quantity = (int)(RiskDollars / (StopPoints * DollarsPerPoint))
//
// On MNQ with the index at 20-24k the stop pins to MaxStopPts=45 on 64% of
// entries, so RiskPerContract = 45 x $2 = $90 and floor(175/90) = 1. The trade
// risks $90 against a $175 intent; 66% of all entries run at 1 lot. The system
// realises about half the risk it budgets and therefore about half the P/L --
// which is why a $1000 daily target was reached 0 times in 313 backtested days.
//
// Three parts, all needed:
//   1. size off the loss path the governors actually permit, not a constant
//   2. round the contract count to NEAREST, not down
//   3. ceiling a single trade at the base risk unit, so the last trade of a
//      winning day cannot stake everything earned so far
//
// EVERYTHING HERE IS INERT until "Sizing Model" is set to 1. Applying this
// patch without changing an input leaves behaviour bit-for-bit unchanged.
//
// Derivation and the cross-contract A/B live in the IOF repo:
//   backtest_trendfollow_propeval.py   (size_trade / risk_unit)
//   backtest_tfpe_daygoal.py           (the step-by-step table)
// NOTE: that A/B does NOT clear the repo's validation bar -- the signal itself
// is unproven. This patch makes the risk model correct; it does not make the
// strategy shippable. Leave "Sizing Model" at 0 until the signal is validated.
// -----------------------------------------------------------------------------

// Most losing trades the day can still absorb before a governor halts it.
// This is a function of MaxTradesPerDay AND MaxConsecLosses TOGETHER -- 3 at
// MT=5/MCL=2 (the L W L W L path), but only 2 at MT=3/MCL=2 -- and not the flat
// "3 full losses" that the original Risk Per Trade default assumes.
static int NQTF_WorstCaseLosses(int TradesLeft, int MaxConsec, int Consec)
{
    if (TradesLeft <= 0)
        return 0;
    if (MaxConsec <= 0)                       // consec-loss governor disabled
        return TradesLeft;
    if (Consec >= MaxConsec)                  // already halted
        return 0;

    if (TradesLeft > 20) TradesLeft = 20;     // bound the table; 20 >> any real MT
    if (MaxConsec  > 20) MaxConsec  = 20;
    if (Consec >= MaxConsec)                  // re-check after the clamp
        return 0;

    // dp[t][c] = most further losses reachable with t trades left and c
    // consecutive losses already on the counter.
    int dp[21][21];
    for (int c = 0; c <= MaxConsec; ++c)
        dp[0][c] = 0;

    for (int t = 1; t <= TradesLeft; ++t)
    {
        for (int c = 0; c <= MaxConsec; ++c)
        {
            if (c >= MaxConsec) { dp[t][c] = 0; continue; }
            const int Lose = 1 + ((c + 1 >= MaxConsec) ? 0 : dp[t - 1][c + 1]);
            const int Win  = dp[t - 1][0];    // a win resets the counter
            dp[t][c] = (Lose > Win) ? Lose : Win;
        }
    }
    return dp[TradesLeft][Consec];
}

// The day's base risk unit -- "1R" for this configuration: the day's loss room
// split over the worst-case number of losses the governors permit, evaluated at
// the START of the day so that it is a constant rather than a path-dependent
// quantity. Anything that wants to be expressed in R (the giveback, the
// per-trade ceiling) anchors here. That is what stops a dollar-denominated
// governor from silently changing meaning when the sizing model changes: a flat
// $400 giveback is 4.4R against 1-lot sizing and 1.45R against 3-lot.
static double NQTF_RiskUnit(int RiskMode, double DailyLoss, double LossBuffer,
                            double RiskPerTrade, int MaxTradesDay,
                            int MaxConsecLoss)
{
    if (RiskMode == 0 || DailyLoss <= 0.0)
        return RiskPerTrade;

    int Slots = NQTF_WorstCaseLosses(MaxTradesDay > 0 ? MaxTradesDay : 20,
                                     MaxConsecLoss, 0);
    if (Slots < 1) Slots = 1;
    return (DailyLoss - LossBuffer) / Slots;
}

// -----------------------------------------------------------------------------
SCSFExport scsf_NQTrendFollowPropEval(SCStudyInterfaceRef sc)
{
    // ---- Subgraphs ----------------------------------------------------------
    SCSubgraphRef Sg_EMAFast    = sc.Subgraph[0];
    SCSubgraphRef Sg_EMASlow    = sc.Subgraph[1];
    SCSubgraphRef Sg_EMATrend   = sc.Subgraph[2];
    SCSubgraphRef Sg_ATR        = sc.Subgraph[3];
    SCSubgraphRef Sg_ADX        = sc.Subgraph[4];
    SCSubgraphRef Sg_BuyArrow   = sc.Subgraph[5];
    SCSubgraphRef Sg_SellArrow  = sc.Subgraph[6];

    // ---- Inputs -------------------------------------------------------------
    SCInputRef In_FastLen        = sc.Input[0];
    SCInputRef In_SlowLen        = sc.Input[1];
    SCInputRef In_TrendLen       = sc.Input[2];
    SCInputRef In_SlopeBars      = sc.Input[3];
    SCInputRef In_ADXLen         = sc.Input[4];
    SCInputRef In_ADXMin         = sc.Input[5];
    SCInputRef In_ATRLen         = sc.Input[6];
    SCInputRef In_PullbackBars   = sc.Input[7];
    SCInputRef In_TriggerBars    = sc.Input[8];
    SCInputRef In_CloseStrength  = sc.Input[9];

    SCInputRef In_StopATRMult    = sc.Input[10];
    SCInputRef In_MinStopPts     = sc.Input[11];
    SCInputRef In_MaxStopPts     = sc.Input[12];
    SCInputRef In_TargetR        = sc.Input[13];
    SCInputRef In_UseTrailStop   = sc.Input[14];
    SCInputRef In_TrailTrigR     = sc.Input[15];
    SCInputRef In_TrailOffsetR   = sc.Input[16];

    SCInputRef In_RiskPerTrade   = sc.Input[17];
    SCInputRef In_MaxContracts   = sc.Input[18];
    SCInputRef In_DailyTarget    = sc.Input[19];
    SCInputRef In_DailyLossLimit = sc.Input[20];
    SCInputRef In_GivebackStop   = sc.Input[21];
    SCInputRef In_MaxTradesDay   = sc.Input[22];
    SCInputRef In_MaxConsecLoss  = sc.Input[23];

    SCInputRef In_SessionStart   = sc.Input[24];
    SCInputRef In_SessionEnd     = sc.Input[25];
    SCInputRef In_FlattenTime    = sc.Input[26];
    SCInputRef In_NoTradeStart   = sc.Input[27];
    SCInputRef In_NoTradeEnd     = sc.Input[28];
    SCInputRef In_MaxBarATRMult  = sc.Input[29];
    SCInputRef In_MinATRPts      = sc.Input[30];
    SCInputRef In_ShowDebug      = sc.Input[31];
    SCInputRef In_LossBuffer     = sc.Input[32];
    SCInputRef In_DayEndTime     = sc.Input[33];
    SCInputRef In_Diagnostics    = sc.Input[34];

    // Appended at the end so existing chart studies keep their saved settings.
    SCInputRef In_RiskMode       = sc.Input[35];
    SCInputRef In_MaxRiskR       = sc.Input[36];
    SCInputRef In_GivebackR      = sc.Input[37];

    // =========================================================================
    // DEFAULTS
    // =========================================================================
    if (sc.SetDefaults)
    {
        sc.GraphName       = "NQ Trend Following - Prop Eval";
        sc.StudyDescription= "Intraday trend-following system for NQ/MNQ with "
                             "daily profit target and daily loss governor.";
        sc.GraphRegion     = 0;
        sc.AutoLoop        = 1;
        sc.FreeDLL         = 0;
        sc.CalculationPrecedence = LOW_PREC_LEVEL;

        // --- Trading configuration ---
        sc.SendOrdersToTradeService                        = 0;  // 0 = simulated. Set 1 only when live.
        sc.AllowMultipleEntriesInSameDirection             = 0;
        sc.SupportReversals                                = 0;
        sc.AllowOppositeEntryWithOpposingPositionOrOrders  = 0;
        sc.SupportAttachedOrdersForTrading                 = 1;
        sc.CancelAllOrdersOnEntriesAndReversals            = 1;
        sc.AllowEntryWithWorkingOrders                     = 0;
        sc.CancelAllWorkingOrdersOnExit                    = 1;
        sc.AllowOnlyOneTradePerBar                         = 1;
        sc.MaintainTradeStatisticsAndTradesData            = 1;
        sc.MaximumPositionAllowed                          = 50;

        // --- Subgraph appearance ---
        Sg_EMAFast.Name = "EMA Fast";
        Sg_EMAFast.DrawStyle = DRAWSTYLE_LINE;
        Sg_EMAFast.PrimaryColor = RGB(0, 200, 255);
        Sg_EMAFast.LineWidth = 2;

        Sg_EMASlow.Name = "EMA Slow";
        Sg_EMASlow.DrawStyle = DRAWSTYLE_LINE;
        Sg_EMASlow.PrimaryColor = RGB(255, 180, 0);
        Sg_EMASlow.LineWidth = 2;

        Sg_EMATrend.Name = "EMA Trend";
        Sg_EMATrend.DrawStyle = DRAWSTYLE_LINE;
        Sg_EMATrend.PrimaryColor = RGB(200, 200, 200);
        Sg_EMATrend.LineWidth = 3;

        Sg_ATR.Name = "ATR";
        Sg_ATR.DrawStyle = DRAWSTYLE_IGNORE;

        Sg_ADX.Name = "ADX";
        Sg_ADX.DrawStyle = DRAWSTYLE_IGNORE;

        Sg_BuyArrow.Name = "Long Entry";
        Sg_BuyArrow.DrawStyle = DRAWSTYLE_ARROW_UP;
        Sg_BuyArrow.PrimaryColor = RGB(0, 255, 0);
        Sg_BuyArrow.LineWidth = 3;

        Sg_SellArrow.Name = "Short Entry";
        Sg_SellArrow.DrawStyle = DRAWSTYLE_ARROW_DOWN;
        Sg_SellArrow.PrimaryColor = RGB(255, 60, 60);
        Sg_SellArrow.LineWidth = 3;

        // --- Trend / signal inputs ---
        In_FastLen.Name = "EMA Fast Length";
        In_FastLen.SetInt(20);
        In_FastLen.SetIntLimits(2, 200);

        In_SlowLen.Name = "EMA Slow Length";
        In_SlowLen.SetInt(50);
        In_SlowLen.SetIntLimits(3, 400);

        In_TrendLen.Name = "EMA Trend Filter Length";
        In_TrendLen.SetInt(200);
        In_TrendLen.SetIntLimits(5, 1000);

        In_SlopeBars.Name = "Trend EMA Slope Lookback (bars)";
        In_SlopeBars.SetInt(10);
        In_SlopeBars.SetIntLimits(1, 200);

        In_ADXLen.Name = "ADX Length";
        In_ADXLen.SetInt(14);
        In_ADXLen.SetIntLimits(2, 100);

        In_ADXMin.Name = "Minimum ADX to Trade";
        In_ADXMin.SetFloat(20.0f);
        In_ADXMin.SetFloatLimits(0.0f, 100.0f);

        In_ATRLen.Name = "ATR Length";
        In_ATRLen.SetInt(14);
        In_ATRLen.SetIntLimits(2, 200);

        In_PullbackBars.Name = "Pullback Lookback (bars touching EMA Fast)";
        In_PullbackBars.SetInt(6);
        In_PullbackBars.SetIntLimits(1, 50);

        In_TriggerBars.Name = "Breakout Trigger Lookback (bars)";
        In_TriggerBars.SetInt(3);
        In_TriggerBars.SetIntLimits(1, 50);

        In_CloseStrength.Name = "Min Close Position In Bar Range (0-1)";
        In_CloseStrength.SetFloat(0.55f);
        In_CloseStrength.SetFloatLimits(0.0f, 1.0f);

        // --- Exit inputs ---
        In_StopATRMult.Name = "Stop Loss = ATR x";
        In_StopATRMult.SetFloat(1.5f);
        In_StopATRMult.SetFloatLimits(0.1f, 10.0f);

        In_MinStopPts.Name = "Minimum Stop (points)";
        In_MinStopPts.SetFloat(12.0f);
        In_MinStopPts.SetFloatLimits(0.25f, 500.0f);

        In_MaxStopPts.Name = "Maximum Stop (points)";
        In_MaxStopPts.SetFloat(45.0f);
        In_MaxStopPts.SetFloatLimits(0.25f, 1000.0f);

        In_TargetR.Name = "Take Profit (R multiple of stop)";
        In_TargetR.SetFloat(2.5f);   // 2.5R x ~$150 risk = ~$375; 3 winners clears $1000
        In_TargetR.SetFloatLimits(0.25f, 20.0f);

        In_UseTrailStop.Name = "Use Trailing Stop Instead Of Fixed (0=No, 1=Yes)";
        In_UseTrailStop.SetInt(0);
        In_UseTrailStop.SetIntLimits(0, 1);

        In_TrailTrigR.Name = "(reserved - not used by trailing stop)";
        In_TrailTrigR.SetFloat(1.0f);
        In_TrailTrigR.SetFloatLimits(0.1f, 20.0f);

        // Keep this at 1.0 so the trailing distance equals the stop distance that
        // position size was calculated from. Lowering it tightens the stop below
        // what the sizing math assumed.
        In_TrailOffsetR.Name = "Trailing Stop Distance (R multiple)";
        In_TrailOffsetR.SetFloat(1.0f);
        In_TrailOffsetR.SetFloatLimits(0.1f, 20.0f);

        // --- Risk / prop-eval governors ---
        In_RiskPerTrade.Name = "Risk Per Trade (account currency)";
        In_RiskPerTrade.SetFloat(175.0f);   // 3 full losses = $525 < $600 limit
        In_RiskPerTrade.SetFloatLimits(1.0f, 100000.0f);

        In_MaxContracts.Name = "Max Contracts Per Trade";
        In_MaxContracts.SetInt(6);          // sized for MNQ; use 1 for full-size NQ
        In_MaxContracts.SetIntLimits(1, 50);

        In_DailyTarget.Name = "Daily Profit Target ($, 0 = off)";
        In_DailyTarget.SetFloat(1000.0f);
        In_DailyTarget.SetFloatLimits(0.0f, 1000000.0f);

        In_DailyLossLimit.Name = "Daily Loss Limit ($, positive number)";
        In_DailyLossLimit.SetFloat(600.0f);
        In_DailyLossLimit.SetFloatLimits(0.0f, 1000000.0f);

        In_GivebackStop.Name = "Max Giveback From Daily Peak ($, 0 = off)";
        In_GivebackStop.SetFloat(400.0f);
        In_GivebackStop.SetFloatLimits(0.0f, 1000000.0f);

        In_MaxTradesDay.Name = "Max Trades Per Day (0 = off)";
        In_MaxTradesDay.SetInt(5);
        In_MaxTradesDay.SetIntLimits(0, 100);

        In_MaxConsecLoss.Name = "Max Consecutive Losses (0 = off)";
        In_MaxConsecLoss.SetInt(2);
        In_MaxConsecLoss.SetIntLimits(0, 100);

        // --- Session inputs (chart timezone) ---
        In_SessionStart.Name = "Entries Allowed From";
        In_SessionStart.SetTime(HMS_TIME(9, 35, 0));

        In_SessionEnd.Name = "Entries Allowed Until";
        In_SessionEnd.SetTime(HMS_TIME(15, 30, 0));

        In_FlattenTime.Name = "Flatten All Positions At";
        In_FlattenTime.SetTime(HMS_TIME(15, 55, 0));

        In_NoTradeStart.Name = "No-Trade Window Start (lunch chop)";
        In_NoTradeStart.SetTime(HMS_TIME(12, 0, 0));

        In_NoTradeEnd.Name = "No-Trade Window End";
        In_NoTradeEnd.SetTime(HMS_TIME(13, 30, 0));

        In_MaxBarATRMult.Name = "Skip Entry If Bar Range > ATR x (0 = off)";
        In_MaxBarATRMult.SetFloat(2.5f);
        In_MaxBarATRMult.SetFloatLimits(0.0f, 20.0f);

        In_MinATRPts.Name = "Minimum ATR To Trade (points)";
        In_MinATRPts.SetFloat(8.0f);
        In_MinATRPts.SetFloatLimits(0.0f, 500.0f);

        In_ShowDebug.Name = "Print Debug Messages To Log";
        In_ShowDebug.SetInt(0);
        In_ShowDebug.SetIntLimits(0, 1);

        // Keeps the worst case a few dollars short of the hard limit instead of
        // landing exactly on it. Prop firms fail you AT the number, not past it.
        In_LossBuffer.Name = "Daily Loss Safety Buffer ($)";
        In_LossBuffer.SetFloat(50.0f);
        In_LossBuffer.SetFloatLimits(0.0f, 100000.0f);

        // End of the day-session block. Bars at or after this time are treated as
        // the overnight/evening session: no entries, no flatten logic. This MUST be
        // earlier than the time the exchange starts a new trading day (18:00 for CME),
        // otherwise evening bars are mistaken for the end of the day session.
        In_DayEndTime.Name = "Day Session Ends At";
        In_DayEndTime.SetTime(HMS_TIME(17, 0, 0));

        In_Diagnostics.Name = "Log Daily Signal Diagnostics (0=No, 1=Yes)";
        In_Diagnostics.SetInt(1);
        In_Diagnostics.SetIntLimits(0, 1);

        // --- Risk model -------------------------------------------------------
        // 0 reproduces the original sizing exactly. Defaults keep it at 0 so
        // this patch is a no-op until it is deliberately switched on.
        In_RiskMode.Name = "Sizing Model (0=Fixed Risk Per Trade, 1=Budget)";
        In_RiskMode.SetInt(0);
        In_RiskMode.SetIntLimits(0, 1);

        // Budget mode only. Ceilings a single trade at this multiple of 1R so
        // the last permitted trade of a winning day cannot stake the day's
        // profit. 1.0 keeps size flat through a winning day while still letting
        // it shrink through a losing one.
        In_MaxRiskR.Name = "Budget: Max Risk Per Trade (R multiple)";
        In_MaxRiskR.SetFloat(1.0f);
        In_MaxRiskR.SetFloatLimits(0.0f, 10.0f);

        // >0 expresses the giveback as a multiple of 1R instead of the flat
        // dollar value above, so it re-scales with the sizing model rather than
        // silently tightening. Backtested response is monotone and plateaus
        // around 2.5R; below ~2R the governor destroys more than it protects.
        In_GivebackR.Name = "Giveback As R Multiple (0 = use $ value above)";
        In_GivebackR.SetFloat(0.0f);
        In_GivebackR.SetFloatLimits(0.0f, 20.0f);

        return;
    }

    // =========================================================================
    // INDICATORS
    // =========================================================================
    sc.ExponentialMovAvg(sc.Close, Sg_EMAFast,  In_FastLen.GetInt());
    sc.ExponentialMovAvg(sc.Close, Sg_EMASlow,  In_SlowLen.GetInt());
    sc.ExponentialMovAvg(sc.Close, Sg_EMATrend, In_TrendLen.GetInt());
    sc.ATR(sc.BaseDataIn, Sg_ATR, In_ATRLen.GetInt(), MOVAVGTYPE_WILDERS);
    sc.ADX(sc.BaseDataIn, Sg_ADX, In_ADXLen.GetInt(), In_ADXLen.GetInt());

    const int i = sc.Index;

    // Need enough history for the slowest average.
    const int MinBars = In_TrendLen.GetInt() + In_SlopeBars.GetInt() + 5;
    if (i < MinBars)
        return;

    // =========================================================================
    // POSITION AND RAW P/L  -- read first, so the day reset can baseline against it
    // =========================================================================
    s_SCPositionData PositionData;
    sc.GetTradePosition(PositionData);

    n_ACSIL::s_TradeStatistics DailyStats;
    sc.GetTradeStatisticsForSymbolV2(n_ACSIL::STATS_TYPE_DAILY_ALL_TRADES, DailyStats);

    const double ClosedPLRaw = DailyStats.ClosedTradesProfitLoss;
    const double OpenPL      = PositionData.OpenProfitLoss;
    const int    PositionQty = (int)PositionData.PositionQuantity;

    // =========================================================================
    // DAILY STATE / RESET
    // =========================================================================
    // sc.GetTradingDayDate() already returns an int date value.
    const int CurrentTradingDay = sc.GetTradingDayDate(sc.BaseDateTimeIn[i]);

    int& r_TradingDay    = sc.GetPersistentInt(PI_TRADING_DAY);
    int& r_Halted        = sc.GetPersistentInt(PI_HALTED);
    int& r_HaltReason    = sc.GetPersistentInt(PI_HALT_REASON);
    int& r_TradesToday   = sc.GetPersistentInt(PI_TRADES_TODAY);
    int& r_ConsecLosses  = sc.GetPersistentInt(PI_CONSEC_LOSSES);
    int& r_LastEntryBar  = sc.GetPersistentInt(PI_LAST_ENTRY_BAR);
    int& r_FlattenSent   = sc.GetPersistentInt(PI_FLATTEN_SENT);

    double& r_PeakDailyPL     = sc.GetPersistentDouble(PD_PEAK_DAILY_PL);
    double& r_PrevClosedPL    = sc.GetPersistentDouble(PD_PREV_CLOSED_PL);
    double& r_DayStartClosed  = sc.GetPersistentDouble(PD_DAY_START_CLOSED);

    const bool NewDay = (CurrentTradingDay != r_TradingDay)
                     || (sc.IsFullRecalculation && i == MinBars);

    if (NewDay)
    {
        // Emit yesterday's diagnostics before the counters are cleared.
        const int DiagTouched = sc.GetPersistentInt(PI_DG_EVALUATED)
                              + sc.GetPersistentInt(PI_DG_BLK_HALT)
                              + sc.GetPersistentInt(PI_DG_BLK_POS)
                              + sc.GetPersistentInt(PI_DG_BLK_WORK);

        if (In_Diagnostics.GetInt() && DiagTouched > 0)
        {
            // Funnel first: bars never reach the filters if they are blocked.
            SCString Msg;
            Msg.Format("DIAG day %d | BLOCKED: halted %d (reason %d), in-position %d, "
                       "working-orders %d, out-of-window %d || EVALUATED %d -> rejected: "
                       "ATR %d, ADX %d, range %d, trend %d, pullback %d, trigger %d "
                       "|| signals %d, unsized %d, ORDERS %d, "
                       "not-submitted %d (last rc=%d %s)",
                       r_TradingDay,
                       sc.GetPersistentInt(PI_DG_BLK_HALT),
                       r_HaltReason,
                       sc.GetPersistentInt(PI_DG_BLK_POS),
                       sc.GetPersistentInt(PI_DG_BLK_WORK),
                       sc.GetPersistentInt(PI_DG_BLK_SESSION),
                       sc.GetPersistentInt(PI_DG_EVALUATED),
                       sc.GetPersistentInt(PI_DG_ATR),
                       sc.GetPersistentInt(PI_DG_ADX),
                       sc.GetPersistentInt(PI_DG_RANGE),
                       sc.GetPersistentInt(PI_DG_TREND),
                       sc.GetPersistentInt(PI_DG_PULLBACK),
                       sc.GetPersistentInt(PI_DG_TRIGGER),
                       sc.GetPersistentInt(PI_DG_SIGNALS),
                       sc.GetPersistentInt(PI_DG_BLK_SIZE),
                       sc.GetPersistentInt(PI_DG_ORDERS),
                       sc.GetPersistentInt(PI_DG_REJECTS),
                       sc.GetPersistentInt(PI_DG_LAST_RC),
                       NQTF_OrderRCText(sc.GetPersistentInt(PI_DG_LAST_RC)));
            sc.AddMessageToLog(Msg, 0);
        }

        r_TradingDay    = CurrentTradingDay;
        r_Halted        = 0;
        r_HaltReason    = 0;
        r_TradesToday   = 0;
        r_ConsecLosses  = 0;
        r_LastEntryBar  = -1;
        r_FlattenSent   = 0;
        r_PeakDailyPL   = 0.0;

        // Baseline against whatever the platform currently reports, so this study
        // measures P/L for ITS trading day regardless of when the platform's own
        // daily statistics happen to reset.
        r_DayStartClosed = ClosedPLRaw;
        r_PrevClosedPL   = ClosedPLRaw;

        sc.GetPersistentInt(PI_SIZE_WARN_SENT) = 0;
        sc.GetPersistentInt(PI_DG_EVALUATED)   = 0;
        sc.GetPersistentInt(PI_DG_ATR)         = 0;
        sc.GetPersistentInt(PI_DG_ADX)         = 0;
        sc.GetPersistentInt(PI_DG_RANGE)       = 0;
        sc.GetPersistentInt(PI_DG_TREND)       = 0;
        sc.GetPersistentInt(PI_DG_PULLBACK)    = 0;
        sc.GetPersistentInt(PI_DG_TRIGGER)     = 0;
        sc.GetPersistentInt(PI_DG_SIGNALS)     = 0;
        sc.GetPersistentInt(PI_DG_ORDERS)      = 0;
        sc.GetPersistentInt(PI_DG_BLK_HALT)    = 0;
        sc.GetPersistentInt(PI_DG_BLK_POS)     = 0;
        sc.GetPersistentInt(PI_DG_BLK_WORK)    = 0;
        sc.GetPersistentInt(PI_DG_BLK_SESSION) = 0;
        sc.GetPersistentInt(PI_DG_REJECTS)     = 0;
        sc.GetPersistentInt(PI_DG_LAST_RC)     = 0;
        sc.GetPersistentInt(PI_RC_LOG_SENT)    = 0;
        sc.GetPersistentInt(PI_DG_BLK_SIZE)    = 0;
    }

    const double ClosedPL = ClosedPLRaw - r_DayStartClosed;
    const double DailyPL  = ClosedPL + OpenPL;

    if (DailyPL > r_PeakDailyPL)
        r_PeakDailyPL = DailyPL;

    // ---- Detect a completed trade to maintain the consecutive-loss counter ---
    if (ClosedPLRaw != r_PrevClosedPL)
    {
        const double TradeDelta = ClosedPLRaw - r_PrevClosedPL;
        if (TradeDelta < 0.0)
            r_ConsecLosses += 1;
        else if (TradeDelta > 0.0)
            r_ConsecLosses = 0;

        r_PrevClosedPL = ClosedPLRaw;
    }

    // =========================================================================
    // TIME CONTEXT
    // =========================================================================
    const int BarTime      = sc.BaseDateTimeIn[i].GetTimeInSeconds();
    const int SessionStart = In_SessionStart.GetTime();
    const int SessionEnd   = In_SessionEnd.GetTime();
    const int FlattenTime  = In_FlattenTime.GetTime();
    const int NoTradeStart = In_NoTradeStart.GetTime();
    const int NoTradeEnd   = In_NoTradeEnd.GetTime();
    const int DayEndTime   = In_DayEndTime.GetTime();

    // Bars belonging to the day session. Evening/overnight bars fall outside this
    // and must be ignored entirely -- on CME the exchange rolls the trading day at
    // 18:00, so an 18:00 bar is the START of a new trading day, not the end of one.
    const bool InDaySession = (BarTime >= SessionStart) && (BarTime < DayEndTime);

    const bool InEntryWindow =
        InDaySession && (BarTime <= SessionEnd) &&
        !((NoTradeEnd > NoTradeStart) && (BarTime >= NoTradeStart) && (BarTime < NoTradeEnd));

    // =========================================================================
    // GOVERNORS -- these run on every update, ahead of any entry logic
    // =========================================================================
    const double DailyTarget = In_DailyTarget.GetFloat();
    const double DailyLoss   = In_DailyLossLimit.GetFloat();

    // 1R for this configuration, and the giveback expressed against it. Both are
    // config constants -- they do not vary with the day's path.
    const double RiskUnit = NQTF_RiskUnit(In_RiskMode.GetInt(), DailyLoss,
                                          In_LossBuffer.GetFloat(),
                                          In_RiskPerTrade.GetFloat(),
                                          In_MaxTradesDay.GetInt(),
                                          In_MaxConsecLoss.GetInt());

    const double Giveback = (In_GivebackR.GetFloat() > 0.0f)
                          ? In_GivebackR.GetFloat() * RiskUnit
                          : In_GivebackStop.GetFloat();

    bool MustFlatten = false;

    if (DailyLoss > 0.0 && DailyPL <= -DailyLoss)
    {
        MustFlatten = true;
        if (!r_Halted) { r_Halted = 1; r_HaltReason = 2; }
    }
    else if (DailyTarget > 0.0 && DailyPL >= DailyTarget)
    {
        MustFlatten = true;
        if (!r_Halted) { r_Halted = 1; r_HaltReason = 1; }
    }
    else if (Giveback > 0.0 && r_PeakDailyPL > 0.0 && (r_PeakDailyPL - DailyPL) >= Giveback)
    {
        MustFlatten = true;
        if (!r_Halted) { r_Halted = 1; r_HaltReason = 4; }
    }

    // End-of-day flatten. Bounded to the day session: an unbounded "BarTime >=
    // FlattenTime" test is also true for every evening bar, which would halt the
    // brand-new trading day the moment it starts at 18:00 and never trade again.
    // This deliberately does NOT set r_Halted -- entries are already gated by
    // InEntryWindow, and setting the flag here would carry the halt overnight.
    if (InDaySession && BarTime >= FlattenTime)
        MustFlatten = true;

    if (In_MaxTradesDay.GetInt() > 0 && r_TradesToday >= In_MaxTradesDay.GetInt())
    {
        if (!r_Halted) { r_Halted = 1; r_HaltReason = 5; }
    }

    if (In_MaxConsecLoss.GetInt() > 0 && r_ConsecLosses >= In_MaxConsecLoss.GetInt())
    {
        if (!r_Halted) { r_Halted = 1; r_HaltReason = 6; }
    }

    if (MustFlatten)
    {
        if (PositionQty != 0 || PositionData.WorkingOrdersExist != 0)
        {
            sc.FlattenAndCancelAllOrders();

            if (r_FlattenSent == 0)
            {
                r_FlattenSent = 1;
                SCString Msg;
                Msg.Format("HALT (reason %d). Daily P/L %.2f | Peak %.2f | Trades %d | Consec losses %d. Flattening.",
                           r_HaltReason, DailyPL, r_PeakDailyPL, r_TradesToday, r_ConsecLosses);
                sc.AddMessageToLog(Msg, 1);
            }
        }
        return;
    }

    // =========================================================================
    // ENTRY EVALUATION -- only on a closed bar, only when flat
    // Every early return below is counted so the daily DIAG line shows exactly
    // where bars are being consumed.
    // =========================================================================
    if (sc.GetBarHasClosedStatus(i) == BHCS_BAR_HAS_NOT_CLOSED)
        return;

    if (r_Halted)
    {
        if (InDaySession)
            sc.GetPersistentInt(PI_DG_BLK_HALT) += 1;
        return;
    }

    if (PositionQty != 0)
    {
        if (InDaySession)
            sc.GetPersistentInt(PI_DG_BLK_POS) += 1;
        return;
    }

    if (PositionData.WorkingOrdersExist != 0)
    {
        if (InDaySession)
            sc.GetPersistentInt(PI_DG_BLK_WORK) += 1;
        return;
    }

    if (!InEntryWindow)
    {
        if (InDaySession)
            sc.GetPersistentInt(PI_DG_BLK_SESSION) += 1;
        return;
    }

    if (r_LastEntryBar == i)
        return;

    // This bar is a genuine candidate: in session, flat, bar closed.
    sc.GetPersistentInt(PI_DG_EVALUATED) += 1;

    const double ATRVal = Sg_ATR[i];
    const double ADXVal = Sg_ADX[i];

    if (ATRVal <= 0.0 || ATRVal < In_MinATRPts.GetFloat())
    {
        sc.GetPersistentInt(PI_DG_ATR) += 1;
        return;
    }

    if (ADXVal < In_ADXMin.GetFloat())
    {
        sc.GetPersistentInt(PI_DG_ADX) += 1;
        return;
    }

    const double Fast  = Sg_EMAFast[i];
    const double Slow  = Sg_EMASlow[i];
    const double Trend = Sg_EMATrend[i];
    const double TrendPrior = Sg_EMATrend[i - In_SlopeBars.GetInt()];

    const double BarHigh  = sc.High[i];
    const double BarLow   = sc.Low[i];
    const double BarClose = sc.Close[i];
    const double BarRange = BarHigh - BarLow;

    if (BarRange <= 0.0)
        return;

    // Skip climax / news bars
    if (In_MaxBarATRMult.GetFloat() > 0.0 && BarRange > In_MaxBarATRMult.GetFloat() * ATRVal)
    {
        sc.GetPersistentInt(PI_DG_RANGE) += 1;
        return;
    }

    const double ClosePos = (BarClose - BarLow) / BarRange;   // 0 = at low, 1 = at high

    // ---- Trend alignment ----------------------------------------------------
    const bool UpAligned   = (Fast > Slow) && (Slow > Trend) && (BarClose > Trend) && (Trend > TrendPrior);
    const bool DownAligned = (Fast < Slow) && (Slow < Trend) && (BarClose < Trend) && (Trend < TrendPrior);

    if (!UpAligned && !DownAligned)
    {
        sc.GetPersistentInt(PI_DG_TREND) += 1;
        return;
    }

    // ---- Pullback: price must have come back to the fast EMA recently -------
    const int PB = In_PullbackBars.GetInt();
    bool PulledBackLong  = false;
    bool PulledBackShort = false;
    for (int k = i - PB; k <= i; ++k)
    {
        if (k < 0) continue;
        if (sc.Low[k]  <= Sg_EMAFast[k]) PulledBackLong  = true;
        if (sc.High[k] >= Sg_EMAFast[k]) PulledBackShort = true;
    }

    // ---- Breakout trigger over the prior N bars ----------------------------
    const int TB = In_TriggerBars.GetInt();
    double PriorHigh = sc.High[i - 1];
    double PriorLow  = sc.Low[i - 1];
    for (int k = i - TB; k <= i - 1; ++k)
    {
        if (k < 0) continue;
        if (sc.High[k] > PriorHigh) PriorHigh = sc.High[k];
        if (sc.Low[k]  < PriorLow)  PriorLow  = sc.Low[k];
    }

    if ((UpAligned && !PulledBackLong) || (DownAligned && !PulledBackShort))
    {
        sc.GetPersistentInt(PI_DG_PULLBACK) += 1;
        return;
    }

    const double MinClosePos = In_CloseStrength.GetFloat();

    const bool LongSignal =
        UpAligned && PulledBackLong &&
        (BarClose > PriorHigh) &&
        (BarClose > Fast) &&
        (ClosePos >= MinClosePos);

    const bool ShortSignal =
        DownAligned && PulledBackShort &&
        (BarClose < PriorLow) &&
        (BarClose < Fast) &&
        ((1.0 - ClosePos) >= MinClosePos);

    if (!LongSignal && !ShortSignal)
    {
        sc.GetPersistentInt(PI_DG_TRIGGER) += 1;
        return;
    }

    sc.GetPersistentInt(PI_DG_SIGNALS) += 1;

    // =========================================================================
    // POSITION SIZING
    // =========================================================================
    double StopPoints = In_StopATRMult.GetFloat() * ATRVal;
    if (StopPoints < In_MinStopPts.GetFloat()) StopPoints = In_MinStopPts.GetFloat();
    if (StopPoints > In_MaxStopPts.GetFloat()) StopPoints = In_MaxStopPts.GetFloat();
    StopPoints = sc.RoundToTickSize(StopPoints, sc.TickSize);

    const double TargetPoints = sc.RoundToTickSize(StopPoints * In_TargetR.GetFloat(), sc.TickSize);

    // Currency value of one full point for this symbol (NQ = $20, MNQ = $2)
    double DollarsPerPoint = 0.0;
    if (sc.TickSize > 0.0)
        DollarsPerPoint = sc.CurrencyValuePerTick / sc.TickSize;

    if (DollarsPerPoint <= 0.0)
    {
        sc.AddMessageToLog("Cannot determine currency value per point. Check symbol settings.", 1);
        return;
    }

    const double RiskPerContract = StopPoints * DollarsPerPoint;
    if (RiskPerContract <= 0.0)
        return;

    // Room left before the day is over. Entry evaluation only runs while flat,
    // so OpenProfitLoss is 0 here and DailyPL == closed P/L.
    double RoomLeft = DailyLoss - In_LossBuffer.GetFloat() + DailyPL;  // DailyPL < 0 when down
    if (RoomLeft < 0.0)
        RoomLeft = 0.0;

    double RiskDollars = 0.0;
    int Quantity = 0;

    if (In_RiskMode.GetInt() == 0)
    {
        // ---- Original: flat risk per trade, truncated to whole contracts ----
        RiskDollars = In_RiskPerTrade.GetFloat();
        if (DailyLoss > 0.0 && RiskDollars > RoomLeft)
            RiskDollars = RoomLeft;

        Quantity = (int)(RiskDollars / RiskPerContract);   // truncation = round down
    }
    else
    {
        // ---- Budget: split the remaining room over the losses still reachable
        if (DailyLoss <= 0.0)
            RoomLeft = In_RiskPerTrade.GetFloat() * 3.0;   // no limit configured

        const int TradesLeft = (In_MaxTradesDay.GetInt() > 0)
                             ? (In_MaxTradesDay.GetInt() - r_TradesToday)
                             : 20;

        const int Slots = NQTF_WorstCaseLosses(TradesLeft,
                                               In_MaxConsecLoss.GetInt(),
                                               r_ConsecLosses);
        if (RoomLeft <= 0.0 || Slots <= 0)
        {
            // No budget or no permitted losses left -- the day is done.
            sc.GetPersistentInt(PI_DG_BLK_SIZE) += 1;
            return;
        }

        RiskDollars = RoomLeft / Slots;

        // RoomLeft grows with the day's profit, so on the LAST permitted trade
        // (Slots == 1) RoomLeft/Slots would stake every dollar earned so far --
        // one loss then walks a +$400 day to the loss limit, which is precisely
        // what the giveback governor exists to prevent.
        if (In_MaxRiskR.GetFloat() > 0.0f)
        {
            const double Ceiling = In_MaxRiskR.GetFloat() * RiskUnit;
            if (RiskDollars > Ceiling)
                RiskDollars = Ceiling;
        }

        // Round to NEAREST. Truncating is what silently halves the budget
        // whenever the contract count lands near 1.
        Quantity = (int)(RiskDollars / RiskPerContract + 0.5);
        if (Quantity < 1)
            Quantity = 1;

        // ...but never let the worst case for this one trade exceed the room.
        const int RoomCap = (int)(RoomLeft / RiskPerContract);
        if (Quantity > RoomCap)
            Quantity = RoomCap;
    }

    if (Quantity < 1)
    {
        // Not enough risk budget for even one contract at this stop distance.
        // Most common cause: running full-size NQ ($20/pt) against a $600 daily
        // limit. One 25-point stop is $500 on NQ but only $50 on MNQ.
        sc.GetPersistentInt(PI_DG_BLK_SIZE) += 1;

        int& r_SizeWarn = sc.GetPersistentInt(PI_SIZE_WARN_SENT);
        if (r_SizeWarn == 0)
        {
            r_SizeWarn = 1;
            SCString Msg;
            Msg.Format("NO TRADE - cannot size a contract. Budget $%.2f, but 1 contract "
                       "risks $%.2f (stop %.2f pts x $%.2f/pt). Trade MNQ, widen 'Risk Per "
                       "Trade', or tighten the stop.",
                       RiskDollars, RiskPerContract, StopPoints, DollarsPerPoint);
            sc.AddMessageToLog(Msg, 1);
        }
        return;
    }
    if (Quantity > In_MaxContracts.GetInt())
        Quantity = In_MaxContracts.GetInt();

    // =========================================================================
    // ORDER SUBMISSION
    // =========================================================================
    s_SCNewOrder NewOrder;
    NewOrder.OrderQuantity = Quantity;
    NewOrder.OrderType     = SCT_ORDERTYPE_MARKET;
    NewOrder.TimeInForce   = SCT_TIF_DAY;
    NewOrder.Stop1Offset   = StopPoints;
    NewOrder.Target1Offset = TargetPoints;

    if (In_UseTrailStop.GetInt() == 1)
    {
        // s_SCNewOrder exposes Stop1Offset and Stop1Offset_2 only -- there is no
        // Stop1Offset_3 -- so the 3-offset triggered trailing stop cannot be built
        // here. Use the single-offset trailing stop instead: Stop1Offset becomes
        // the trailing distance, which trails from the moment of the fill.
        NewOrder.AttachedOrderStop1Type = SCT_ORDERTYPE_TRAILING_STOP;
        NewOrder.Stop1Offset = sc.RoundToTickSize(StopPoints * In_TrailOffsetR.GetFloat(), sc.TickSize);
    }

    int Result = 0;

    if (LongSignal)
        Result = (int)sc.BuyEntry(NewOrder);
    else
        Result = (int)sc.SellEntry(NewOrder);

    if (Result > 0)
    {
        r_TradesToday += 1;
        r_LastEntryBar = i;
        sc.GetPersistentInt(PI_DG_ORDERS) += 1;

        if (LongSignal)  Sg_BuyArrow[i]  = BarLow  - ATRVal * 0.35;
        else             Sg_SellArrow[i] = BarHigh + ATRVal * 0.35;

        SCString Msg;
        Msg.Format("%s %d @ market | stop %.2f pts ($%.0f/ctr) | target %.2f pts | ADX %.1f | ATR %.2f | trade #%d | daily P/L %.2f",
                   LongSignal ? "LONG" : "SHORT",
                   Quantity, StopPoints, RiskPerContract, TargetPoints,
                   ADXVal, ATRVal, r_TradesToday, DailyPL);
        sc.AddMessageToLog(Msg, 0);
    }
    else
    {
        // Count and remember every non-fill so the daily DIAG line can report
        // it. This branch used to be gated behind In_ShowDebug, which defaults
        // to 0 -- so a study that submitted nothing all year looked silent.
        sc.GetPersistentInt(PI_DG_REJECTS) += 1;
        sc.GetPersistentInt(PI_DG_LAST_RC)  = Result;

        int& r_RCLogSent = sc.GetPersistentInt(PI_RC_LOG_SENT);
        if (r_RCLogSent == 0 || In_ShowDebug.GetInt())
        {
            r_RCLogSent = 1;
            SCString Msg;
            Msg.Format("ORDER NOT SUBMITTED. rc=%d  %s",
                       Result, NQTF_OrderRCText(Result));
            sc.AddMessageToLog(Msg, 1);
        }
    }
}
