// ============================================================================
//  IOF NQ — Edge Discovery Features (ACSIL)
//
//  Session features aligned with Python `features.py` + edge_discovery output:
//    dist_vwap_atr, cum_delta_z, bar delta, pace_ref, vol/avg_vol,
//    POC/VAH/VAL via session histogram @ bar close (70% value area),
//    exposed as **(level - close) / ATR** for subgraph scaling.
//
//  Requirements: NQ/MNQ, **Bid and Ask volume**, volume bars (e.g. 3000).
//  No trading — indicator only.
//
//  Version: 1.1 (Apr 2026)
// ============================================================================

#include "sierrachart.h"
#include <algorithm>
#include <cmath>
#include <map>
#include <vector>

SCDLLName("IOF_NQ_EdgeDiscovery")

namespace {

inline float FAbs(float x) { return x < 0.f ? -x : x; }
inline float FMax(float a, float b) { return a > b ? a : b; }

inline void CalcValueArea(
    const std::map<int, float>& volAtTick,
    float valueAreaPct,
    int& outPocTick,
    float& outVah,
    float& outVal,
    float tickSize)
{
    outPocTick = 0;
    outVah = 0.f;
    outVal = 0.f;
    if (volAtTick.empty() || tickSize <= 0.f)
        return;

    float totalVol = 0.f;
    int pocTick = 0;
    float pocVol = -1.f;
    for (const auto& kv : volAtTick) {
        totalVol += kv.second;
        if (kv.second > pocVol) {
            pocVol = kv.second;
            pocTick = kv.first;
        }
    }
    if (totalVol <= 0.f || pocVol < 0.f)
        return;

    const float target = totalVol * valueAreaPct;
    std::vector<int> ticks;
    ticks.reserve(volAtTick.size());
    for (const auto& kv : volAtTick)
        ticks.push_back(kv.first);
    std::sort(ticks.begin(), ticks.end());

    int pocIdx = 0;
    for (size_t i = 0; i < ticks.size(); ++i) {
        if (ticks[i] == pocTick) {
            pocIdx = (int)i;
            break;
        }
    }

    std::vector<int> selected;
    selected.push_back(pocTick);
    float current = volAtTick.count(pocTick) ? volAtTick.at(pocTick) : 0.f;
    int left = pocIdx - 1;
    int right = pocIdx + 1;

    while (current < target && (left >= 0 || right < (int)ticks.size())) {
        float leftVol = (left >= 0 && volAtTick.count(ticks[left])) ? volAtTick.at(ticks[left]) : -1.f;
        float rightVol = (right < (int)ticks.size() && volAtTick.count(ticks[right])) ? volAtTick.at(ticks[right]) : -1.f;
        if (rightVol >= leftVol) {
            selected.push_back(ticks[right]);
            current += FMax(0.f, rightVol);
            ++right;
        } else {
            selected.push_back(ticks[left]);
            current += FMax(0.f, leftVol);
            --left;
        }
    }

    int hiTick = selected[0], loTick = selected[0];
    for (int t : selected) {
        hiTick = (t > hiTick) ? t : hiTick;
        loTick = (t < loTick) ? t : loTick;
    }
    outPocTick = pocTick;
    outVah = (float)hiTick * tickSize;
    outVal = (float)loTick * tickSize;
}

enum { PI_LastDay = 1, PI_SessionStartIdx = 2 };

} // namespace

SCSFExport scsf_IOF_NQ_EdgeDiscovery(SCStudyInterfaceRef sc)
{
    SCSubgraphRef SG_DistVwapAtr = sc.Subgraph[0];
    SCSubgraphRef SG_CumDeltaZ = sc.Subgraph[1];
    SCSubgraphRef SG_BarDelta = sc.Subgraph[2];
    SCSubgraphRef SG_PaceRef = sc.Subgraph[3];
    SCSubgraphRef SG_VolRatio = sc.Subgraph[4];
    SCSubgraphRef SG_DistPocAtr = sc.Subgraph[5];
    SCSubgraphRef SG_DistVahAtr = sc.Subgraph[6];
    SCSubgraphRef SG_DistValAtr = sc.Subgraph[7];
    SCSubgraphRef SG_Signal = sc.Subgraph[8];
    SCSubgraphRef SG_CumDelta = sc.Subgraph[9];
    SCSubgraphRef SG_Vwap = sc.Subgraph[10];
    SCSubgraphRef SG_Atr = sc.Subgraph[11];

    SCInputRef IN_AtrPeriod = sc.Input[0];
    SCInputRef IN_AtrMin = sc.Input[1];
    SCInputRef IN_CumDzLb = sc.Input[2];
    SCInputRef IN_CumDzMin = sc.Input[3];
    SCInputRef IN_VolLb = sc.Input[4];
    SCInputRef IN_PaceLb = sc.Input[5];
    SCInputRef IN_VaPct = sc.Input[6];
    SCInputRef IN_SigDist = sc.Input[7];
    SCInputRef IN_SigZ = sc.Input[8];
    SCInputRef IN_ShowSignal = sc.Input[9];

    if (sc.SetDefaults) {
        sc.GraphName = "IOF NQ Edge Discovery Features";
        sc.StudyDescription =
            "Edge-discovery suite: dist_vwap_atr, cum_delta_z, bar delta, pace_ref, vol ratio, "
            "(POC/VAH/VAL minus close)/ATR. Matches Python pool study. Bid/ask vol required.";
        sc.AutoLoop = 1;
        sc.GraphRegion = 1;
        sc.MaintainAdditionalChartDataArrays = 1;

        SG_DistVwapAtr.Name = "Dist VWAP / ATR";
        SG_DistVwapAtr.DrawStyle = DRAWSTYLE_LINE;
        SG_DistVwapAtr.PrimaryColor = RGB(33, 150, 243);
        SG_DistVwapAtr.LineWidth = 2;

        SG_CumDeltaZ.Name = "Cum delta Z";
        SG_CumDeltaZ.DrawStyle = DRAWSTYLE_LINE;
        SG_CumDeltaZ.PrimaryColor = RGB(156, 39, 176);
        SG_CumDeltaZ.LineWidth = 2;

        SG_BarDelta.Name = "Bar delta";
        SG_BarDelta.DrawStyle = DRAWSTYLE_BAR;
        SG_BarDelta.PrimaryColor = RGB(0, 172, 193);
        SG_BarDelta.SecondaryColor = RGB(229, 115, 115);
        SG_BarDelta.DrawZeros = 0;

        SG_PaceRef.Name = "Pace ref (mean abs dlt)";
        SG_PaceRef.DrawStyle = DRAWSTYLE_LINE;
        SG_PaceRef.PrimaryColor = RGB(255, 152, 0);
        SG_PaceRef.LineWidth = 1;

        SG_VolRatio.Name = "Vol / AvgVol (lbk)";
        SG_VolRatio.DrawStyle = DRAWSTYLE_LINE;
        SG_VolRatio.PrimaryColor = RGB(76, 175, 80);
        SG_VolRatio.LineWidth = 1;

        SG_DistPocAtr.Name = "(POC - Close) / ATR";
        SG_DistPocAtr.DrawStyle = DRAWSTYLE_LINE;
        SG_DistPocAtr.PrimaryColor = RGB(244, 67, 54);
        SG_DistPocAtr.LineWidth = 1;

        SG_DistVahAtr.Name = "(VAH - Close) / ATR";
        SG_DistVahAtr.DrawStyle = DRAWSTYLE_DASH;
        SG_DistVahAtr.PrimaryColor = RGB(129, 199, 132);
        SG_DistVahAtr.LineWidth = 1;

        SG_DistValAtr.Name = "(VAL - Close) / ATR";
        SG_DistValAtr.DrawStyle = DRAWSTYLE_DASH;
        SG_DistValAtr.PrimaryColor = RGB(129, 199, 132);
        SG_DistValAtr.LineWidth = 1;

        SG_Signal.Name = "Edge signal";
        SG_Signal.DrawStyle = DRAWSTYLE_POINT;
        SG_Signal.PrimaryColor = RGB(255, 235, 59);
        SG_Signal.LineWidth = 3;
        SG_Signal.DrawZeros = 0;

        SG_CumDelta.Name = "_CumDelta";
        SG_CumDelta.DrawStyle = DRAWSTYLE_IGNORE;
        SG_Vwap.Name = "_Vwap";
        SG_Vwap.DrawStyle = DRAWSTYLE_IGNORE;
        SG_Atr.Name = "_Atr";
        SG_Atr.DrawStyle = DRAWSTYLE_IGNORE;

        IN_AtrPeriod.Name = "ATR period (Wilder)";
        IN_AtrPeriod.SetInt(50);
        IN_AtrPeriod.SetIntLimits(5, 300);

        IN_AtrMin.Name = "ATR floor (pts)";
        IN_AtrMin.SetFloat(0.75f);

        IN_CumDzLb.Name = "Cum delta Z lookback (bars)";
        IN_CumDzLb.SetInt(200);
        IN_CumDzLb.SetIntLimits(30, 500);

        IN_CumDzMin.Name = "Cum delta Z min bars";
        IN_CumDzMin.SetInt(20);
        IN_CumDzMin.SetIntLimits(5, 100);

        IN_VolLb.Name = "Avg volume lookback";
        IN_VolLb.SetInt(20);
        IN_VolLb.SetIntLimits(1, 200);

        IN_PaceLb.Name = "Pace lookback (abs delta)";
        IN_PaceLb.SetInt(3);
        IN_PaceLb.SetIntLimits(1, 50);

        IN_VaPct.Name = "Value area fraction";
        IN_VaPct.SetFloat(0.70f);

        IN_SigDist.Name = "Signal: |dist_vwap_atr| >=";
        IN_SigDist.SetFloat(0.35f);

        IN_SigZ.Name = "Signal: |cum_delta_z| >=";
        IN_SigZ.SetFloat(1.0f);

        IN_ShowSignal.Name = "Plot edge signal markers";
        IN_ShowSignal.SetYesNo(1);

        return;
    }

    const float TICK = sc.TickSize;
    if (TICK <= 0.f)
        return;

    const int atrPer = IN_AtrPeriod.GetInt();
    sc.ATR(sc.BaseData, SG_Atr, atrPer, MOVAVGTYPE_WILDERS);

    const int Idx = sc.Index;
    const float High0 = sc.High[Idx];
    const float Low0 = sc.Low[Idx];
    const float Close0 = sc.Close[Idx];
    const float Vol0 = sc.Volume[Idx];
    const float d0 = sc.AskVolume[Idx] - sc.BidVolume[Idx];

    float atr = SG_Atr[Idx];
    {
        const float loAtr = IN_AtrMin.GetFloat();
        atr = FMax(atr, loAtr);
        atr = FMax(atr, High0 - Low0);
        atr = FMax(atr, TICK * 3.f);
    }

    SCDateTime barDT = sc.BaseDateTimeIn[Idx];
    const int barDate = barDT.GetDate();

    int& lastDay = sc.GetPersistentInt(PI_LastDay);
    int& sessStart = sc.GetPersistentInt(PI_SessionStartIdx);

    if (lastDay != barDate) {
        lastDay = barDate;
        sessStart = Idx;
    }

    const float prevCum = (Idx > sessStart) ? SG_CumDelta[Idx - 1] : 0.f;
    const float cumDelta = (Idx == sessStart) ? d0 : prevCum + d0;
    SG_CumDelta[Idx] = cumDelta;
    SG_BarDelta[Idx] = d0;

    float cumPv = 0.f, cumV = 0.f;
    for (int j = sessStart; j <= Idx; ++j) {
        cumPv += sc.Close[j] * sc.Volume[j];
        cumV += sc.Volume[j];
    }
    const float vwap = (cumV > 0.f) ? cumPv / cumV : Close0;
    SG_Vwap[Idx] = vwap;

    std::map<int, float> volAtTick;
    for (int j = sessStart; j <= Idx; ++j) {
        int k = (int)floor(sc.Close[j] / TICK + 0.5f);
        volAtTick[k] += sc.Volume[j];
    }
    int pocTick = 0;
    float vahPx = 0.f, valPx = 0.f;
    CalcValueArea(volAtTick, IN_VaPct.GetFloat(), pocTick, vahPx, valPx, TICK);
    const float pocPx = (pocTick != 0) ? (float)pocTick * TICK : Close0;

    const float invAtr = (atr > 1e-6f) ? (1.f / atr) : 0.f;
    SG_DistVwapAtr[Idx] = (Close0 - vwap) * invAtr;
    SG_DistPocAtr[Idx] = (pocPx - Close0) * invAtr;
    SG_DistVahAtr[Idx] = (vahPx > 0.f) ? (vahPx - Close0) * invAtr : 0.f;
    SG_DistValAtr[Idx] = (valPx > 0.f) ? (valPx - Close0) * invAtr : 0.f;

    const int zLb = IN_CumDzLb.GetInt();
    const int zMin = IN_CumDzMin.GetInt();
    int lo = (Idx - zLb + 1 > sessStart) ? (Idx - zLb + 1) : sessStart;
    float m = 0.f;
    int cnt = 0;
    for (int j = lo; j <= Idx; ++j) {
        m += SG_CumDelta[j];
        ++cnt;
    }
    if (cnt >= zMin && cnt > 1) {
        m /= (float)cnt;
        float v = 0.f;
        for (int j = lo; j <= Idx; ++j) {
            float e = SG_CumDelta[j] - m;
            v += e * e;
        }
        v /= (float)cnt;
        float s = (float)sqrt(v);
        SG_CumDeltaZ[Idx] = (s > 1e-6f) ? (cumDelta - m) / s : 0.f;
    } else {
        SG_CumDeltaZ[Idx] = 0.f;
    }

    const int pLb = IN_PaceLb.GetInt();
    float paceSum = 0.f;
    int pc = 0;
    for (int k = 0; k < pLb && Idx - k >= sessStart; ++k) {
        paceSum += FAbs(sc.AskVolume[Idx - k] - sc.BidVolume[Idx - k]);
        ++pc;
    }
    SG_PaceRef[Idx] = (pc > 0) ? paceSum / (float)pc : FAbs(d0);

    const int vLb = IN_VolLb.GetInt();
    float vsum = 0.f;
    int vc = 0;
    for (int k = 0; k < vLb && Idx - k >= 0; ++k) {
        vsum += sc.Volume[Idx - k];
        ++vc;
    }
    const float avgV = (vc > 0) ? vsum / (float)vc : Vol0;
    SG_VolRatio[Idx] = (avgV > 0.f) ? Vol0 / avgV : 1.f;

    SG_Signal[Idx] = 0.f;
    if (IN_ShowSignal.GetYesNo()) {
        const float dz = SG_DistVwapAtr[Idx];
        const float zz = SG_CumDeltaZ[Idx];
        if (FAbs(dz) >= IN_SigDist.GetFloat() && FAbs(zz) >= IN_SigZ.GetFloat()
            && dz * zz > 0.f)
            SG_Signal[Idx] = dz > 0.f ? 2.f : -2.f;
    }
}
