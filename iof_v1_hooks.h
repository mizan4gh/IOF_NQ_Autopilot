// =============================================================================
// NQ RTH OrderFlow V1-style optional gates for IOF NQ (confirmations + chop +
// rolling R-Sharpe tightening). Aligned with archived IOFv02/legacy/NQ_RTH_OrderFlow_Strategy_V1.cpp.
// =============================================================================
#pragma once

#include "sierrachart.h"
#include "iof_unified/iof_math.h"
#include <algorithm>
#include <cmath>

namespace iof_v1 {

static const int kRingCap = 32;
static const int kPiRingCount = 44;
static const int kPfRing0 = 60;

static inline float TypicalPrice(SCStudyInterfaceRef& sc, int i)
{
    return (sc.High[i] + sc.Low[i] + sc.Close[i]) / 3.f;
}

static inline float BarDelta(SCStudyInterfaceRef& sc, int i)
{
    if (i < 0) return 0.f;
    return sc.AskVolume[i] - sc.BidVolume[i];
}

static inline float SumDelta(SCStudyInterfaceRef& sc, int i, int lb)
{
    float s = 0.f;
    const int n = (std::max)(1, lb);
    for (int k = 0; k < n && i - k >= 0; k++)
        s += BarDelta(sc, i - k);
    return s;
}

static inline float AvgVol(SCStudyInterfaceRef& sc, int i, int lb)
{
    if (i < 0) return 0.f;
    double a = 0.0;
    const int n = (std::max)(1, lb);
    int c = 0;
    for (int k = 0; k < n && i - k >= 0; k++, c++)
        a += (double)sc.Volume[i - k];
    return c > 0 ? (float)(a / (double)c) : 0.f;
}

struct Confirmations
{
    bool deltaTrend, imbalanceAgg, absorptionProxy, failedAuction, volumeRel, pace;
    int nAggressive, nStructural, nTotal;
    void Clear()
    {
        deltaTrend = imbalanceAgg = absorptionProxy = failedAuction = volumeRel = pace = false;
        nAggressive = nStructural = nTotal = 0;
    }
    void Tally(int minAgg, int minStruct, int minTotal, bool& ok) const
    {
        ok = (nTotal >= minTotal) && (nAggressive >= minAgg) && (nStructural >= minStruct);
    }
};

static inline void BuildConfirmations(SCStudyInterfaceRef& sc, int Idx, int dir,
    float deltaThresh, float imbThresh, int volLb, float volMult, int paceLb, Confirmations& o)
{
    o.Clear();
    const float bd = BarDelta(sc, Idx);
    const float bdPrev = BarDelta(sc, Idx - 1);
    const float sumD = SumDelta(sc, Idx, 4);
    if (dir > 0)
    {
        o.deltaTrend = (bd > deltaThresh) || (bd > bdPrev + deltaThresh * 0.5f && bd > 0.f)
            || (sumD > deltaThresh * 2.f);
    }
    else if (dir < 0)
    {
        o.deltaTrend = (bd < -deltaThresh) || (bd < bdPrev - deltaThresh * 0.5f && bd < 0.f)
            || (sumD < -deltaThresh * 2.f);
    }
    float aSum = 0.f, bSum = 0.f;
    for (int k = 0; k < 5 && Idx - k >= 0; k++)
    {
        aSum += sc.AskVolume[Idx - k];
        bSum += sc.BidVolume[Idx - k];
    }
    const float totAB = aSum + bSum;
    if (dir > 0 && totAB > 0.f)
        o.imbalanceAgg = (aSum / totAB) >= imbThresh;
    else if (dir < 0 && totAB > 0.f)
        o.imbalanceAgg = (bSum / totAB) >= imbThresh;
    const float tick = (sc.TickSize > 0.f) ? sc.TickSize : 0.25f;
    const float rng = iof_unified::FMax(tick * 2.f, sc.High[Idx] - sc.Low[Idx]);
    if (dir > 0)
        o.absorptionProxy = (bd < 0.f) && (sc.Close[Idx] > sc.Low[Idx] + rng * 0.35f);
    else if (dir < 0)
        o.absorptionProxy = (bd > 0.f) && (sc.Close[Idx] < sc.High[Idx] - rng * 0.35f);
    if (Idx >= 1)
    {
        if (dir > 0)
            o.failedAuction = (sc.Low[Idx] < sc.Low[Idx - 1] - tick * 2.f)
                && (sc.Close[Idx] > sc.Low[Idx - 1]);
        else if (dir < 0)
            o.failedAuction = (sc.High[Idx] > sc.High[Idx - 1] + tick * 2.f)
                && (sc.Close[Idx] < sc.High[Idx - 1]);
    }
    const float av = AvgVol(sc, Idx - 1, volLb);
    o.volumeRel = (av > 0.f && sc.Volume[Idx] >= av * volMult);
    float vNow = sc.Volume[Idx], vPrev = AvgVol(sc, Idx - 1, paceLb);
    if (dir > 0)
        o.pace = vNow >= vPrev * 1.15f && sc.Close[Idx] >= sc.Open[Idx];
    else if (dir < 0)
        o.pace = vNow >= vPrev * 1.15f && sc.Close[Idx] <= sc.Open[Idx];
    o.nAggressive = (o.deltaTrend ? 1 : 0) + (o.imbalanceAgg ? 1 : 0) + (o.pace ? 1 : 0);
    o.nStructural = (o.absorptionProxy ? 1 : 0) + (o.failedAuction ? 1 : 0) + (o.volumeRel ? 1 : 0);
    o.nTotal = o.nAggressive + o.nStructural;
}

static inline float ComputeChopScore(SCStudyInterfaceRef& sc, int Idx, int vwapCrossLb,
    int emaFlatLb, float emaFlatAtr, int overlapLb, float vwap, const SCSubgraphRef& emaFast, float atr)
{
    if (Idx < 20 || atr <= 0.f) return 50.f;
    int crosses = 0;
    for (int k = 1; k <= vwapCrossLb && Idx - k >= 1; k++)
    {
        const float v0 = sc.Close[Idx - k] - vwap;
        const float v1 = sc.Close[Idx - k - 1] - vwap;
        if ((v0 > 0.f && v1 < 0.f) || (v0 < 0.f && v1 > 0.f))
            crosses++;
    }
    const float crossScore = iof_unified::FMin(40.f, (float)crosses * 8.f);
    float emaMove = atr;
    if (emaFlatLb > 0 && Idx >= emaFlatLb)
        emaMove = iof_unified::FAbs(emaFast[Idx] - emaFast[Idx - emaFlatLb]);
    const float flatScore = (emaMove < emaFlatAtr * atr) ? 25.f : 0.f;
    float overlap = 0.f;
    const float tick = (sc.TickSize > 0.f) ? sc.TickSize : 0.25f;
    for (int k = 0; k < overlapLb && Idx - k - 1 >= 0; k++)
    {
        const float h = iof_unified::FMin(sc.High[Idx - k], sc.High[Idx - k - 1]);
        const float l = iof_unified::FMax(sc.Low[Idx - k], sc.Low[Idx - k - 1]);
        if (h >= l)
            overlap += (h - l) / iof_unified::FMax(atr, tick * 4.f);
    }
    const float ovScore = iof_unified::FMin(25.f, overlap * 6.f);
    const float d0 = BarDelta(sc, Idx), d1 = BarDelta(sc, Idx - 1);
    const bool weakDeltaFollow = (iof_unified::FAbs(d0) < iof_unified::FAbs(d1) * 0.6f && iof_unified::FAbs(d0) < (sc.Volume[Idx] * 0.05f + 1.f));
    const float dScore = weakDeltaFollow ? 10.f : 0.f;
    return iof_unified::FMin(100.f, crossScore + flatScore + ovScore + dScore);
}

static inline void RingPushR(SCStudyInterfaceRef& sc, float rMult)
{
    int& count = sc.GetPersistentInt(kPiRingCount);
    if (count < kRingCap)
    {
        sc.GetPersistentFloat(kPfRing0 + count) = rMult;
        count++;
    }
    else
    {
        for (int i = 1; i < kRingCap; i++)
            sc.GetPersistentFloat(kPfRing0 + i - 1) = sc.GetPersistentFloat(kPfRing0 + i);
        sc.GetPersistentFloat(kPfRing0 + kRingCap - 1) = rMult;
    }
}

static inline float RingSharpe(SCStudyInterfaceRef& sc, int lookback)
{
    const int count = sc.GetPersistentInt(kPiRingCount);
    const int nUse = (std::min)(lookback, count);
    if (nUse < 2) return 1.f;
    float rs[kRingCap];
    const int start = count - nUse;
    for (int i = 0; i < nUse; i++)
        rs[i] = sc.GetPersistentFloat(kPfRing0 + start + i);
    double mean = 0.0;
    for (int i = 0; i < nUse; i++) mean += rs[i];
    mean /= (double)nUse;
    double var = 0.0;
    for (int i = 0; i < nUse; i++)
    {
        const double d = rs[i] - mean;
        var += d * d;
    }
    var /= (double)(nUse > 1 ? (nUse - 1) : 1);
    if (var < 0.0) var = 0.0;
    const double stdv = sqrt(var);
    if (stdv < 1e-6) return (float)(mean > 0.0 ? 1.0 : -1.0);
    return (float)(mean / stdv * sqrt((double)nUse));
}

static inline void ResetRing(SCStudyInterfaceRef& sc)
{
    sc.GetPersistentInt(kPiRingCount) = 0;
    for (int z = 0; z < kRingCap; z++)
        sc.GetPersistentFloat(kPfRing0 + z) = 0.f;
}

// Reject detail for Message Log / CSV (filled when HooksRejectCode != 0 and out != nullptr).
struct V1HookRejectDetail
{
    int code;              // 1 = confirmations, 2 = chop score
    int minCUsed;
    int nTot, nAgg, nStruct;
    bool dt, imb, absp, fa, vr, pc;
    float chopScore;
    int ringCount;
    float sharpeVal;
    int sharpeRaisedMin;   // 1 if mode 4 raised min confirmations via Sharpe warn
};

// Returns 0 = pass, 1 = confirmations failed, 2 = chop failed. mode 0 => 0 pass.
static inline int HooksRejectCode(SCStudyInterfaceRef& sc, int Idx, int n,
    int mode, bool selLong, float vwap, float atr, const SCSubgraphRef& emaFast,
    int minConf, float chopMax,
    int vwapCrossLb, int emaFlatLb, float emaFlatAtr, int overlapLb,
    float deltaTh, float imbTh, int volLb, float volMult, int paceLb,
    int sharpeLb, int sharpeMinN, float sharpeWarn, int minConfSharpeWarn,
    V1HookRejectDetail* out)
{
    if (out)
    {
        out->code = 0;
        out->nTot = out->nAgg = out->nStruct = 0;
        out->dt = out->imb = out->absp = out->fa = out->vr = out->pc = false;
        out->chopScore = 0.f;
        out->sharpeRaisedMin = 0;
    }
    if (mode <= 0) return 0;
    const bool needConf = (mode == 1 || mode == 3 || mode == 4);
    const bool needChop = (mode == 2 || mode == 3 || mode == 4);
    int minC = minConf;
    if (mode == 4)
    {
        const int ringCount = sc.GetPersistentInt(kPiRingCount);
        const float sharpeVal = RingSharpe(sc, sharpeLb);
        if (ringCount >= sharpeMinN && sharpeVal < sharpeWarn)
        {
            minC = (std::max)(minC, minConfSharpeWarn);
            if (out) out->sharpeRaisedMin = 1;
        }
    }
    if (out)
    {
        out->minCUsed = minC;
        out->ringCount = sc.GetPersistentInt(kPiRingCount);
        out->sharpeVal = RingSharpe(sc, sharpeLb);
    }
    const int dir = selLong ? +1 : -1;
    if (needConf)
    {
        Confirmations cf;
        BuildConfirmations(sc, Idx, dir, deltaTh, imbTh, volLb, volMult, paceLb, cf);
        bool confOK = false;
        cf.Tally(1, 1, minC, confOK);
        if (!confOK)
        {
            if (out)
            {
                out->code = 1;
                out->nTot = cf.nTotal;
                out->nAgg = cf.nAggressive;
                out->nStruct = cf.nStructural;
                out->dt = cf.deltaTrend;
                out->imb = cf.imbalanceAgg;
                out->absp = cf.absorptionProxy;
                out->fa = cf.failedAuction;
                out->vr = cf.volumeRel;
                out->pc = cf.pace;
            }
            return 1;
        }
    }
    if (needChop)
    {
        if (chopMax > 0.f && Idx >= 20 && atr > 0.f)
        {
            const float chop = ComputeChopScore(sc, Idx, vwapCrossLb, emaFlatLb, emaFlatAtr,
                overlapLb, vwap, emaFast, atr);
            if (chop > chopMax)
            {
                if (out)
                {
                    out->code = 2;
                    out->chopScore = chop;
                }
                return 2;
            }
        }
    }
    return 0;
}

static inline bool HooksPass(SCStudyInterfaceRef& sc, int Idx, int n,
    int mode, bool selLong, float vwap, float atr, const SCSubgraphRef& emaFast,
    int minConf, float chopMax,
    int vwapCrossLb, int emaFlatLb, float emaFlatAtr, int overlapLb,
    float deltaTh, float imbTh, int volLb, float volMult, int paceLb,
    int sharpeLb, int sharpeMinN, float sharpeWarn, int minConfSharpeWarn)
{
    return HooksRejectCode(sc, Idx, n, mode, selLong, vwap, atr, emaFast, minConf, chopMax,
            vwapCrossLb, emaFlatLb, emaFlatAtr, overlapLb, deltaTh, imbTh, volLb, volMult, paceLb,
            sharpeLb, sharpeMinN, sharpeWarn, minConfSharpeWarn, nullptr) == 0;
}

} // namespace iof_v1
