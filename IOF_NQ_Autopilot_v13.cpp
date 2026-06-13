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

// SCDLLName near top so the remote build server's scan window finds it.
SCDLLName("IOF_NQ_Autopilot_v13")

// ============================================================================
//  IOF NQ — Pure Orderflow Autopilot v13
//
//  Clean rewrite of v12.37. Four modes only: M1 VWAP reclaim, M4 sweep+reclaim,
//  M6 balance breakout, M8 fade. Filters: delta, VWAP, volume profile, ATR.
//  1 contract, $800 daily loss, 2 consec losses max, 2 losing trades/day max,
//  no entries >= 15:00 ET, flatten 15:55 ET, RTH only (09:35+).
//
//  REMOVED vs v12.37 (see commit message for the full list):
//    M2/M3/M5/M7, anti-martingale, recovery mode, time-decay, budget
//    multiplier, equity-curve/profit-scale multipliers, VolScaler, Hypo
//    shadow P&L, AUTO_DISABLE, adaptive thresholds (fixed defaults), M1
//    pullback variants (dip+reclaim is THE M1 trigger), overnight session,
//    duplicate consec-loss gates, stacked overlapping cooldowns.
//
//  PRESERVED (hard-won live fixes — do not remove):
//    - DayOpenPnL pre-entry re-snapshot latch    (v12.27/28/29)
//    - per-bar gate on risk EMA updates           (v12.32, AutoLoop=1)
//    - vcool decrement once per bar               (v12.31)
//    - broker-fill exit reconciliation + EXTERNAL (v12.24/25)
//    - tick-snap of all bracket levels            (v12.26)
//    - single-target attach when qty==1           (v12.13)
//    - early-scratch exit                         (v12.37, A/B validated)
//    - M1 dip+reclaim trigger                     (v12.34, A/B validated)
//    - M1 dead-zone 12:00-13:59 + trend gate      (v12.24)
//    - late-entry gate >= 15:00                   (v12.36, A/B validated)
//    - signal-only gate = full-recalc/hist-DL only (v12.11)
//    - CSV LRU dedup with 10MB warmup             (v11)
//
//  RISK MULTIPLIER = Kelly x Volatility x Drawdown (three factors only).
//  Sized to 1 contract regardless; RM gates entries via RM_FLOOR.
//
//  Requires: 3000-5000 volume bars, NQ/MNQ, Bid/Ask volume, RTH session.
// ============================================================================

namespace iof13 {
inline float FAbs(float x){ return x < 0.f ? -x : x; }
inline float FMax(float a, float b){ return a > b ? a : b; }
inline float FMin(float a, float b){ return a < b ? a : b; }
inline int   IClamp(int v, int lo, int hi){ return v<lo?lo:(v>hi?hi:v); }
inline float FClamp(float v, float lo, float hi){ return v<lo?lo:(v>hi?hi:v); }
inline float TCeil (float p, float t){ return (t>0.f)? ceilf(p/t)*t : p; }
inline float TFloor(float p, float t){ return (t>0.f)? floorf(p/t)*t : p; }
inline int HhmmFromSec(int s){ return (s/3600)*100 + ((s%3600)/60); }
}
using namespace iof13;

// ============================================================================
//  ENUMS — modes, exit reasons, reject reasons
// ============================================================================
enum Mode {
    MODE_NONE = -1,
    MODE_M1   = 0,   // VWAP dip+reclaim
    MODE_M4   = 1,   // sweep + reclaim
    MODE_M6   = 2,   // balance breakout
    MODE_M8   = 3,   // fade / reversal
    MODE_COUNT = 4,
};
static const char* ModeName(int m){
    static const char* n[MODE_COUNT] = {"M1","M4","M6","M8"};
    return (m>=0 && m<MODE_COUNT) ? n[m] : "M?";
}

enum ExitReason {
    XR_NONE = 0, XR_FLATTEN, XR_CIRCUIT_BREAKER, XR_SPIKE,
    XR_STOP, XR_TRAIL, XR_T1, XR_T2,
    XR_DAILY_PROFIT, XR_DAILY_LOSS, XR_SCRATCH, XR_EXTERNAL,
};
static const char* ExitReasonName(int r){
    switch(r){
        case XR_FLATTEN:        return "FLATTEN";
        case XR_CIRCUIT_BREAKER:return "CB";
        case XR_SPIKE:          return "SPIKE";
        case XR_STOP:           return "STOP";
        case XR_TRAIL:          return "TRAIL";
        case XR_T1:             return "TP1";
        case XR_T2:             return "TP2";
        case XR_DAILY_PROFIT:   return "DAILY_PROFIT";
        case XR_DAILY_LOSS:     return "DAILY_LOSS";
        case XR_SCRATCH:        return "SCRATCH";
        case XR_EXTERNAL:       return "EXTERNAL";
        default:                return "UNKNOWN";
    }
}

enum RejectReason {
    RJ_NONE = 0,
    RJ_REGIME,        // chop / trend-strength regime blocked the mode
    RJ_VOL_EXTREME,   // ATR regime extreme (volRegime==3)
    RJ_M1_DEADZONE,   // M1 12:00-13:59 ET
    RJ_M1_TREND,      // M1 against 20-bar trend
    RJ_COOLDOWN,      // unified cooldown active
    RJ_RM_FLOOR,      // risk multiplier below floor
    RJ_SESSION_DD,    // session drawdown > 80% of daily budget
    RJ_QUAL_FLOOR,    // per-mode quality floor
    RJ_ORDER_RC,      // Sierra rejected the order
};
static const char* RejectReasonName(int r){
    switch(r){
        case RJ_REGIME:      return "REGIME";
        case RJ_VOL_EXTREME: return "VOL_EXTREME";
        case RJ_M1_DEADZONE: return "M1_DEADZONE";
        case RJ_M1_TREND:    return "M1_TREND";
        case RJ_COOLDOWN:    return "COOLDOWN";
        case RJ_RM_FLOOR:    return "RM_FLOOR";
        case RJ_SESSION_DD:  return "SESSION_DD";
        case RJ_QUAL_FLOOR:  return "QUAL_FLOOR";
        case RJ_ORDER_RC:    return "ORDER_RC";
        default:             return "";
    }
}

// ============================================================================
//  CONSTANTS (values carried from v12.37 — all A/B-tested or long-validated)
// ============================================================================
static const int   C_ATR_PER        = 14;
static const float C_STOP_ATR       = 1.2f;
static const float C_T1_ATR         = 1.25f;
static const float C_T2_ATR         = 3.0f;
static const float C_BE_ATR         = 0.30f;   // breakeven offset after T1
static const float C_TRAIL_ATR      = 1.50f;
static const int   C_TRAIL_DLY      = 5;       // bars after T1 before trail arms (x3 when qty==1)
static const int   C_MIN_SCORE_M1   = 4;
static const int   C_MIN_SCORE_ALL  = 3;

// Unified cooldown inputs — combined via max(), never stacked.
static const int   C_CD_AFTER_TRADE = 5;
static const int   C_CD_AFTER_LOSS  = 10;
static const int   C_CD_POST_STOP   = 10;      // same-direction only
static const int   C_CD_VOL_SPIKE   = 20;      // after range/delta spike
static const int   C_CD_VOL_MAJOR   = 40;      // after major spike (range/ATR >= 7)

static const int   C_DELTA_LB       = 15;
static const int   C_OPEN_COOL_MIN  = 10;      // minutes after open, no entries
static const int   C_VWAP_MATURE    = 40;
static const int   C_DELTA_MATURE   = 25;
static const int   C_SWEEP_LB       = 15;
static const float C_SPIKE_ATR_M    = 3.0f;
static const float C_VCOOL_THRESH   = 7.0f;
static const int   C_MAX_LOSSES     = 2;       // consecutive losses halt
static const int   C_MAX_DAY_LOSSES = 2;       // total losing trades/day halt
static const int   C_VWAP_SLP_LB    = 20;
static const int   C_LATE_ENTRY     = 1500;    // no new entries >= 15:00 ET

static const float C_STOP_FLOOR_PTS = 20.0f;
static const float C_STOP_CEIL_PTS  = 40.0f;
static const float C_T1_FLOOR_PTS   = 25.0f;
static const float C_T1_CEIL_PTS    = 50.0f;
static const float C_T2_FLOOR_PTS   = 75.0f;
static const float C_T2_CEIL_PTS    = 125.0f;

static const float C_RM_FLOOR       = 0.60f;
static const float C_ES_MFE_FRAC    = 0.25f;   // early-scratch MFE threshold

static const int LOG_CRIT = 0;
static const int LOG_SIG  = 1;
static const int LOG_DBG  = 2;

// Quality scale: M1 (frequent) uses /15; M4/M6 (structural reversal/breakout)
// use *10; M8 uses edgeScore*10. Same mapping as v12.12 — floors are
// calibrated against this scale, do not change one without the other.
static int Quality100(int mode, int finalScore, int edgeScore){
    if(mode == MODE_M8) return IClamp(edgeScore * 10, 0, 100);
    if(mode == MODE_M4 || mode == MODE_M6) return IClamp(finalScore * 10, 0, 100);
    return IClamp((finalScore * 100) / 15, 0, 100);
}

// ============================================================================
//  6. LOGGING — message log helpers + CSV journal
// ============================================================================
static void LogMsg(SCStudyInterfaceRef& sc, const char* tag, const SCString& body, int isErr = 0){
    SCString m; m.Format("[V13 %s] %s", tag, body.GetChars());
    sc.AddMessageToLog(m, isErr);
}

static bool EnsureDir(const char* P){
    if(!P || !P[0]) return false;
    DWORD A = GetFileAttributesA(P);
    if(A != INVALID_FILE_ATTRIBUTES && (A & FILE_ATTRIBUTE_DIRECTORY)) return true;
    if(CreateDirectoryA(P, NULL)) return true;
    return (GetLastError() == ERROR_ALREADY_EXISTS);
}

static unsigned long long g_runID = 0;
static void InitRunID(){
    if(g_runID != 0) return;
    g_runID = (unsigned long long)time(NULL) * 2654435761ULL;
    if(g_runID == 0) g_runID = 1;
}

// CSV dedup LRU — prevents duplicate SETUP rows on recalc (v11 mechanism).
static const int CSV_LRU_SZ = 32768;
struct CSVDedupLRU {
    char keys[CSV_LRU_SZ][96];
    int  head, filled;
    bool warmed;
    void reset(){ head=0; filled=0; warmed=false; for(int i=0;i<CSV_LRU_SZ;i++) keys[i][0]='\0'; }
    bool contains(const char* k) const {
        int n = (filled < CSV_LRU_SZ) ? filled : CSV_LRU_SZ;
        for(int i=0;i<n;i++) if(keys[i][0] && strcmp(keys[i],k)==0) return true;
        return false;
    }
    void add(const char* k){
        int n=0; while(n<95 && k[n]){ keys[head][n]=k[n]; n++; }
        keys[head][n]='\0';
        head=(head+1)%CSV_LRU_SZ;
        if(filled<CSV_LRU_SZ) filled++;
    }
};
static CSVDedupLRU g_csvLRU;

// One CSV row. Every event uses the same column set; unused fields are 0/"".
// Columns: Date,Time,Event,Side,Mode,Entry,SL,TP1,TP2,Qty,Score,Qual,
//          CtrlScore,DivStr,Delta,VwapRel,PocRel,VahRel,ValRel,
//          RejectReason,ExitPx,ExitReason,HoldBars,MAE,MFE,
//          DayPnL,TotalPnL,RiskMult,TrendReg,VolReg,ChopReg,Version,RunID
struct CsvRow {
    const char *evt, *side, *mode;
    float entry, sl, tp1, tp2;
    int   qty, score, qual, ctrl, divStr;
    float delta, vwapRel, pocRel, vahRel, valRel;
    const char *rejR;
    float exitPx; const char *exitR;
    int   hold; float mae, mfe, dayPnL, totPnL, rm;
    int   trendReg, volReg, chopReg;
    CsvRow(){ memset(this, 0, sizeof(*this));
              evt=""; side=""; mode=""; rejR=""; exitR=""; }
};

static void WriteCsv(SCStudyInterfaceRef& sc, const CsvRow& r){
    InitRunID();
    int Y,Mo,D,Hr,Mi,Se;
    sc.BaseDateTimeIn[sc.Index].GetDateTimeYMDHMS(Y,Mo,D,Hr,Mi,Se);
    EnsureDir(sc.DataFilesFolder().GetChars());
    SCString Path; Path.Format("%s\\IOF_NQ_v13_%s.csv", sc.DataFilesFolder().GetChars(), sc.Symbol.GetChars());

    // Dedup SETUP rows only (they can re-fire on recalc; ENTRY/EXIT are
    // guarded by live trade state, REJECT/SESSION duplication is harmless
    // but SETUPs feed analysis tooling).
    if(strcmp(r.evt,"SETUP")==0){
        char Key[96]; snprintf(Key,96,"%04d-%02d-%02d,%02d:%02d:%02d,%s,%s",Y,Mo,D,Hr,Mi,Se,r.evt,r.side);
        if(g_csvLRU.contains(Key)) return;
        if(!g_csvLRU.warmed){
            g_csvLRU.warmed = true;
            FILE* RF = fopen(Path.GetChars(), "rb");
            if(RF){
                const long WARMUP_BYTES = 10 * 1024 * 1024;
                fseek(RF, 0, SEEK_END);
                long FS = ftell(RF);
                long Pos = (FS > WARMUP_BYTES) ? (FS - WARMUP_BYTES) : 0;
                fseek(RF, Pos, SEEK_SET);
                int Sz = (int)(FS - Pos);
                char* B = (char*)malloc(Sz + 1);
                if(B){
                    int N = (int)fread(B, 1, Sz, RF);
                    B[N] = '\0';
                    char* line = B;
                    if(Pos > 0){ char* nl = strchr(line,'\n'); if(nl) line = nl+1; }
                    while(line && *line){
                        char* nl = strchr(line,'\n');
                        int len = nl ? (int)(nl-line) : (int)strlen(line);
                        if(len > 25 && line[20]=='S' && line[21]=='E' && line[22]=='T' && line[23]=='U' && line[24]=='P'){
                            int commas=0, keyEnd=-1;
                            for(int i=0;i<len && i<95;i++){
                                if(line[i]==','){ commas++; if(commas==4){ keyEnd=i; break; } }
                            }
                            if(keyEnd>0 && keyEnd<95){
                                char k[96]; memcpy(k,line,keyEnd); k[keyEnd]='\0';
                                if(!g_csvLRU.contains(k)) g_csvLRU.add(k);
                            }
                        }
                        if(!nl) break;
                        line = nl+1;
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
    if(Hdr) fprintf(F,"Date,Time,Event,Side,Mode,Entry,SL,TP1,TP2,Qty,Score,Qual,"
                      "CtrlScore,DivStr,Delta,VwapRel,PocRel,VahRel,ValRel,"
                      "RejectReason,ExitPx,ExitReason,HoldBars,MAE,MFE,"
                      "DayPnL,TotalPnL,RiskMult,TrendReg,VolReg,ChopReg,Version,RunID\n");
    fprintf(F,"%04d-%02d-%02d,%02d:%02d:%02d,%s,%s,%s,%.2f,%.2f,%.2f,%.2f,%d,%d,%d,"
              "%d,%d,%.0f,%.2f,%.2f,%.2f,%.2f,"
              "%s,%.2f,%s,%d,%.2f,%.2f,"
              "%.2f,%.2f,%.2f,%d,%d,%d,v13.0,%llu\n",
        Y,Mo,D,Hr,Mi,Se, r.evt, r.side, r.mode, r.entry, r.sl, r.tp1, r.tp2,
        r.qty, r.score, r.qual,
        r.ctrl, r.divStr, r.delta, r.vwapRel, r.pocRel, r.vahRel, r.valRel,
        r.rejR, r.exitPx, r.exitR, r.hold, r.mae, r.mfe,
        r.dayPnL, r.totPnL, r.rm, r.trendReg, r.volReg, r.chopReg, g_runID);
    fclose(F);
}

// ============================================================================
//  3. SIGNAL DETECTION â€” supporting state structs
//
//  Only the subsystems the four kept modes actually read are retained:
//    VWAP (M1,M4,control)       VolumeProfile (control + VP targets)
//    ControlScore (all gates)   Divergence (M4 min-score, M6 verify, M8 edge)
//    Balance (M6,M8)            Imbalance (M6)
//  Dropped vs v12.37: Trap (M5-only), the M7 auction-reversal machinery,
//  Iceberg, AdaptiveThresholds (fixed constants now), VolScaler, ModeStats.
// ============================================================================

// 5-day composite volume profile -> POC / VAH / VAL.
static const int VP_MAX_BINS = 800;
static const int VP_DAYS     = 5;
static const float VP_BIN_SZ = 1.0f;
static const float VP_VA_PCT = 0.70f;

struct VPDay { float basePx; float bins[VP_MAX_BINS]; int dateTag; };

struct VPState {
    VPDay days[VP_DAYS];
    float poc, vah, val;
    int   activeDayIdx;
    bool  valid;
    void reset(){
        for(int d=0;d<VP_DAYS;d++){ days[d].basePx=0.f; days[d].dateTag=0;
            for(int b=0;b<VP_MAX_BINS;b++) days[d].bins[b]=0.f; }
        poc=vah=val=0.f; activeDayIdx=0; valid=false;
    }
    int pxToBin(float px, float base) const {
        int b=(int)((px-base)/VP_BIN_SZ)+VP_MAX_BINS/2;
        return IClamp(b,0,VP_MAX_BINS-1);
    }
    float binToPx(int b, float base) const { return base + (float)(b-VP_MAX_BINS/2)*VP_BIN_SZ; }
    void addBar(float hi, float lo, float vol, float base){
        if(vol<=0.f||base<=0.f) return;
        VPDay& d=days[activeDayIdx]; d.basePx=base;
        int bL=pxToBin(lo,base), bH=pxToBin(hi,base);
        float per=vol/(float)((bH-bL)+1);
        for(int b=bL;b<=bH;b++) d.bins[b]+=per;
    }
    void compute(){
        float sB=0.f; int nD=0;
        for(int d=0;d<VP_DAYS;d++) if(days[d].dateTag>0 && days[d].basePx>0.f){ sB+=days[d].basePx; nD++; }
        if(nD==0){ valid=false; return; }
        float gB=sB/(float)nD;
        static float comp[VP_MAX_BINS];
        for(int b=0;b<VP_MAX_BINS;b++) comp[b]=0.f;
        for(int d=0;d<VP_DAYS;d++){
            if(days[d].dateTag<=0) continue;
            float dB=days[d].basePx;
            for(int b=0;b<VP_MAX_BINS;b++){
                if(days[d].bins[b]<=0.f) continue;
                comp[pxToBin(binToPx(b,dB),gB)] += days[d].bins[b];
            }
        }
        int pB=0; float mV=0.f, tV=0.f;
        for(int b=0;b<VP_MAX_BINS;b++){ tV+=comp[b]; if(comp[b]>mV){ mV=comp[b]; pB=b; } }
        poc=binToPx(pB,gB);
        if(tV<=0.f){ vah=val=poc; valid=true; return; }
        float vaT=tV*VP_VA_PCT, vaV=comp[pB];
        int lo=pB, hi=pB;
        while(vaV<vaT && (lo>0||hi<VP_MAX_BINS-1)){
            float vB=(lo>0)?comp[lo-1]:0.f, vA=(hi<VP_MAX_BINS-1)?comp[hi+1]:0.f;
            if(vB>=vA && lo>0){ lo--; vaV+=comp[lo]; }
            else if(hi<VP_MAX_BINS-1){ hi++; vaV+=comp[hi]; }
            else if(lo>0){ lo--; vaV+=comp[lo]; }
            else break;
        }
        vah=binToPx(hi,gB); val=binToPx(lo,gB); valid=true;
    }
};

struct BalanceState {
    bool active, mature;
    float rangeHigh, rangeLow, rangePOC, volumeTotal;
    int barCount, deltaFlips;
    void reset(){ active=mature=false; rangeHigh=rangeLow=rangePOC=0.f;
                  volumeTotal=0.f; barCount=0; deltaFlips=0; }
};

struct ImbalanceState {
    bool active; int direction, startBar, barCount, strength;
    void reset(){ active=false; direction=0; startBar=-1; barCount=0; strength=0; }
};

struct DivState {
    bool trendDivBull, trendDivBear, swingDivBull, swingDivBear;
    int  persistUpPxDnDelta, persistDnPxUpDelta, strength;
    bool persistAbsBuy, persistAbsSell;
    void reset(){ trendDivBull=trendDivBear=swingDivBull=swingDivBear=false;
                  persistUpPxDnDelta=persistDnPxUpDelta=0; strength=0;
                  persistAbsBuy=persistAbsSell=false; }
};

struct FadeSetup {
    int type, direction, edgeScore, triggerBar;
    float entryPx, stopPx, t1Px, t2Px;
    bool active;
    void reset(){ type=direction=edgeScore=0; triggerBar=-1;
                  entryPx=stopPx=t1Px=t2Px=0.f; active=false; }
};

struct InterdayLevels {
    float prevHigh, prevLow, prevClose, currOpen;
    int prevDate; bool valid;
    void reset(){ prevHigh=prevLow=prevClose=currOpen=0.f; prevDate=0; valid=false; }
};

// Delta ring â€” last 32 bars of (ask-bid), O(1) rolling sums.
static const int DRING_SZ = 32;
struct DeltaRing {
    float delta[DRING_SZ]; int lastIdx; bool primed;
    void reset(){ for(int i=0;i<DRING_SZ;i++) delta[i]=0.f; lastIdx=-1; primed=false; }
    void pushBar(SCStudyInterfaceRef& sc, int Idx){
        if(Idx==lastIdx) return;
        if(!primed || Idx!=lastIdx+1){
            for(int k=0;k<DRING_SZ;k++){ int s=Idx-k; delta[k]=(s>=0)?(sc.AskVolume[s]-sc.BidVolume[s]):0.f; }
            primed=true; lastIdx=Idx; return;
        }
        for(int k=DRING_SZ-1;k>0;k--) delta[k]=delta[k-1];
        delta[0]=sc.AskVolume[Idx]-sc.BidVolume[Idx];
        lastIdx=Idx;
    }
    float sumRange(int from, int to) const {
        if(from<0) from=0; if(to>=DRING_SZ) to=DRING_SZ-1;
        float s=0.f; for(int k=from;k<=to;k++) s+=delta[k]; return s;
    }
};

// Lightweight chop/trend regime classifier (used by the optional regime gate).
struct RegimeClassifier {
    int trendRegime, volRegime, chopRegime;
    float trendStrength, chopScore;
    void reset(){ trendRegime=0; volRegime=1; chopRegime=0; trendStrength=chopScore=0.f; }
    void update(float cumDelta, float priceChange, float atr, float range, int lookback){
        if(lookback<=0||atr<=0.f) return;
        float deltaPerBar=cumDelta/(float)lookback;
        trendStrength=FAbs(deltaPerBar)/(atr*0.1f);
        if(trendStrength>1.5f && priceChange>atr*0.5f) trendRegime=1;
        else if(trendStrength>1.5f && priceChange<-atr*0.5f) trendRegime=-1;
        else trendRegime=0;
        float follow=FAbs(priceChange)/(range>0.f?range:1.f);
        chopScore=1.f-follow;
        if(chopScore>0.85f) chopRegime=3;
        else if(chopScore>0.7f) chopRegime=2;
        else if(chopScore>0.4f) chopRegime=1;
        else chopRegime=0;
    }
    // Gate for the four kept modes only (indices are Mode enum values).
    bool allowMode(int mode, bool /*isLong*/) const {
        if(mode==MODE_M1 && chopRegime>=2 && trendStrength<0.3f) return false; // M1: no reclaim in dead chop
        if(mode==MODE_M1 && trendStrength>2.5f) return false;                  // M1: no fade of a strong trend
        if(mode==MODE_M6 && trendStrength>5.f) return false;                   // M6: no breakout into a blow-off
        return true;
    }
};

// ============================================================================
//  2. RISK MANAGEMENT â€” RiskMultiplier = Kelly x Volatility x Drawdown
//
//  Three factors only. Removed vs v12.37: anti-martingale, recovery mode,
//  time-decay, budget multiplier, equity-curve regime, profit-scaling, and
//  the consec-loss RM taper (consec-loss is now a hard halt, not a size cut).
//  Sizing is always 1 contract; RM only gates entry through RM_FLOOR.
// ============================================================================
struct SimpleRisk {
    float sessionPnL, sessionPeak, sessionDD, maxSessionDD;
    int   sessionTrades, sessionConsecLosses;
    float cumPnL; int cumTrades, winCount, lossCount;
    float grossWin, grossLoss;
    float baselineATR, atrEMA, atrRatio; int volRegime;
    int   lastUpdateBar;
    float riskMultiplier, kellyMult, volMult, ddMult, practicalKelly;

    void reset(){
        sessionPnL=sessionPeak=sessionDD=maxSessionDD=0.f;
        sessionTrades=sessionConsecLosses=0;
        cumPnL=0.f; cumTrades=winCount=lossCount=0; grossWin=grossLoss=0.f;
        baselineATR=atrEMA=0.f; atrRatio=1.f; volRegime=1; lastUpdateBar=-1;
        riskMultiplier=1.f; kellyMult=volMult=ddMult=1.f; practicalKelly=0.f;
    }
    void newSession(){
        sessionPnL=sessionPeak=sessionDD=maxSessionDD=0.f;
        sessionTrades=sessionConsecLosses=0;
    }
    float winRate() const { return cumTrades>0 ? (float)winCount/(float)cumTrades : 0.f; }
    float profitFactor() const { return grossLoss>0.f ? grossWin/grossLoss : (grossWin>0.f?99.9f:0.f); }

    void updatePnL(float pnl){
        sessionPnL+=pnl; sessionTrades++; cumPnL+=pnl; cumTrades++;
        if(pnl>0.f){ winCount++; grossWin+=pnl; sessionConsecLosses=0; }
        else { lossCount++; grossLoss+=FAbs(pnl); sessionConsecLosses++; }
        if(sessionPnL>sessionPeak) sessionPeak=sessionPnL;
        sessionDD=sessionPeak-sessionPnL;
        if(sessionDD>maxSessionDD) maxSessionDD=sessionDD;
        computeKelly(); computeRM();
    }
    void computeKelly(){
        if(winCount<5||lossCount<2){ practicalKelly=0.f; return; }
        float wr=winRate(), avgW=grossWin/(float)winCount, avgL=grossLoss/(float)lossCount;
        if(avgL<=0.f){ practicalKelly=0.f; return; }
        float b=avgW/avgL, k=(wr*b-(1.f-wr))/b;
        practicalKelly = (k<0.f) ? 0.f : FClamp(k*0.5f, 0.05f, 0.25f);
    }
    void updateVolRegime(float atr){
        if(baselineATR<=0.f){ baselineATR=atrEMA=atr; atrRatio=1.f; volRegime=1; return; }
        atrEMA = 0.1f*atr + 0.9f*atrEMA;
        baselineATR += 0.01f*(atrEMA-baselineATR);
        atrRatio = atrEMA/baselineATR;
        if(atrRatio<0.7f) volRegime=0;
        else if(atrRatio<1.3f) volRegime=1;
        else if(atrRatio<2.0f) volRegime=2;
        else volRegime=3;
        computeRM();
    }
    void computeRM(){
        if(volRegime==0) volMult=1.20f;
        else if(volRegime==1) volMult=1.00f;
        else if(volRegime==2) volMult=0.70f;
        else volMult=0.40f;
        if(cumTrades<10) kellyMult=0.9f;
        else if(practicalKelly<=0.f) kellyMult=0.25f;
        else if(practicalKelly>=0.20f) kellyMult=1.25f;
        else if(practicalKelly>=0.15f) kellyMult=1.10f;
        else if(practicalKelly>=0.10f) kellyMult=1.00f;
        else kellyMult=0.75f;
        if(sessionDD<=0.f) ddMult=1.00f;
        else if(sessionDD<500.f) ddMult=0.95f;
        else if(sessionDD<1000.f) ddMult=0.80f;
        else if(sessionDD<1500.f) ddMult=0.60f;
        else if(sessionDD<2000.f) ddMult=0.40f;
        else ddMult=0.25f;
        riskMultiplier = FClamp(volMult*kellyMult*ddMult, 0.10f, 2.00f);
    }
};

// ============================================================================
//  PERSISTENT SLOT ENUMS
// ============================================================================
enum PersistInt {
    PI_TradeState=1, PI_T1Hit, PI_Trades, PI_LastDay, PI_EntryBar,
    PI_LastExitBar, PI_LastTradeDir, PI_DayDone, PI_ConsecLoss, PI_DayLosses,
    PI_LastSigBar, PI_VWAPBars, PI_T1HitBar, PI_LastVPBar, PI_LastCalcBar,
    PI_LastStopBar, PI_LastStopDir, PI_TradeMode, PI_TradeScore, PI_EntryQty,
    PI_PrevPosQty, PI_FlattenReason, PI_LiveTradeDir, PI_LastSymbolHash,
    PI_BannerShown, PI_LastExitWasLoss, PI_HasEnteredThisLoad, PI_SpikeBar,
    PI_SpikeActive, PI_VCoolRemaining, PI_VCoolBar, PI_LastBlockLogBar,
};
enum PersistFloat {
    PF_EntryPx=1, PF_StopPx, PF_T1Px, PF_T2Px, PF_DayOpenPnL,
    PF_VWAPPxVol, PF_VWAPVol, PF_VWAPSqVol, PF_PrevSettle,
    PF_SessHigh, PF_SessLow, PF_SessOpen, PF_TradeMAE, PF_TradeMFE,
    PF_CumPnLAtEntry,
};
enum PersistPtr {
    PP_VPState=1, PP_BalanceState, PP_ImbalanceState, PP_DivState,
    PP_FadeSetup, PP_InterdayLevels, PP_RegimeClassifier, PP_DeltaRing,
    PP_SimpleRisk,
};


// ============================================================================
//  MAIN STUDY
// ============================================================================
SCSFExport scsf_IOF_NQ_Autopilot_v13(SCStudyInterfaceRef sc)
{
    // ---- Subgraphs ----
    SCSubgraphRef SG_VWAP=sc.Subgraph[0]; SCSubgraphRef SG_BUY=sc.Subgraph[1]; SCSubgraphRef SG_SELL=sc.Subgraph[2];
    SCSubgraphRef SG_STOP=sc.Subgraph[3]; SCSubgraphRef SG_T1=sc.Subgraph[4]; SCSubgraphRef SG_T2=sc.Subgraph[5];
    SCSubgraphRef SG_DELTA=sc.Subgraph[6]; SCSubgraphRef SG_ATR=sc.Subgraph[7]; SCSubgraphRef SG_ADELT=sc.Subgraph[8];
    SCSubgraphRef SG_POC=sc.Subgraph[9]; SCSubgraphRef SG_VAH=sc.Subgraph[10]; SCSubgraphRef SG_VAL=sc.Subgraph[11];
    SCSubgraphRef SG_VB2U=sc.Subgraph[12]; SCSubgraphRef SG_VB2L=sc.Subgraph[13]; SCSubgraphRef SG_CTRL=sc.Subgraph[14];

    // ---- Inputs (config block â€” backtest sweepable) ----
    SCInputRef IN_LIVE         = sc.Input[0];
    SCInputRef IN_CAPITAL      = sc.Input[1];
    SCInputRef IN_DAILY_LOSS   = sc.Input[2];
    SCInputRef IN_DAILY_PROF   = sc.Input[3];
    SCInputRef IN_RTH_OPEN     = sc.Input[4];
    SCInputRef IN_FLAT_TIME    = sc.Input[5];
    SCInputRef IN_LATE_ENTRY   = sc.Input[6];
    SCInputRef IN_MAX_TRADES   = sc.Input[7];
    SCInputRef IN_MAX_CONSEC   = sc.Input[8];
    SCInputRef IN_MAX_DAY_LOSS = sc.Input[9];
    SCInputRef IN_QF_M1        = sc.Input[10];
    SCInputRef IN_QF_M4        = sc.Input[11];
    SCInputRef IN_QF_M6        = sc.Input[12];
    SCInputRef IN_QF_M8        = sc.Input[13];
    SCInputRef IN_EN_M1        = sc.Input[14];
    SCInputRef IN_EN_M4        = sc.Input[15];
    SCInputRef IN_EN_M6        = sc.Input[16];
    SCInputRef IN_EN_M8        = sc.Input[17];
    SCInputRef IN_ENTRY_ORD    = sc.Input[18];
    SCInputRef IN_REGIME_FILT  = sc.Input[19];
    SCInputRef IN_NEWS_FILTER  = sc.Input[20];
    SCInputRef IN_VP_TARGETS   = sc.Input[21];
    SCInputRef IN_LOG_LVL      = sc.Input[22];
    SCInputRef IN_CSV          = sc.Input[23];
    SCInputRef IN_DIAG         = sc.Input[24];

    if(sc.SetDefaults){
        sc.GraphName="IOF NQ Autopilot v13";
        sc.StudyDescription="IOF NQ Pure Orderflow v13 (clean rewrite). Modes M1/M4/M6/M8. "
            "Filters delta+VWAP+VP+ATR. 1 contract, $800 daily loss, 2 consec / 2 day losses, "
            "no entries >=15:00, flatten 15:55, RTH 09:35+. Per-mode quality floors (default 50). "
            "3000-5000 vol bars. CSV IOF_NQ_v13_*.csv.";
        sc.AutoLoop=1; sc.GraphRegion=0;
        sc.SendOrdersToTradeService=0; sc.AllowOnlyOneTradePerBar=0;
        sc.MaximumPositionAllowed=1; sc.SupportReversals=0;
        sc.CancelAllOrdersOnEntriesAndReversals=0;
        sc.UseGUIAttachedOrderSetting=0; sc.MaintainAdditionalChartDataArrays=1;

        SG_VWAP.Name="aVWAP"; SG_VWAP.DrawStyle=DRAWSTYLE_IGNORE;
        SG_BUY.Name="Long"; SG_BUY.DrawStyle=DRAWSTYLE_IGNORE;
        SG_SELL.Name="Short"; SG_SELL.DrawStyle=DRAWSTYLE_IGNORE;
        SG_STOP.Name="Stop"; SG_STOP.DrawStyle=DRAWSTYLE_IGNORE;
        SG_T1.Name="T1"; SG_T1.DrawStyle=DRAWSTYLE_IGNORE;
        SG_T2.Name="T2"; SG_T2.DrawStyle=DRAWSTYLE_IGNORE;
        SG_DELTA.Name="CumDelta"; SG_DELTA.DrawStyle=DRAWSTYLE_BAR; SG_DELTA.DrawZeros=0;
        SG_DELTA.PrimaryColor=RGB(0,172,193); SG_DELTA.SecondaryColor=RGB(229,115,115);
        SG_ATR.Name="ATR"; SG_ATR.DrawStyle=DRAWSTYLE_IGNORE;
        SG_ADELT.Name="|Dlt|Adpt"; SG_ADELT.DrawStyle=DRAWSTYLE_IGNORE;
        SG_POC.Name="VP POC"; SG_POC.DrawStyle=DRAWSTYLE_DASH; SG_POC.PrimaryColor=RGB(255,64,129); SG_POC.LineWidth=2; SG_POC.DrawZeros=0;
        SG_VAH.Name="VP VAH"; SG_VAH.DrawStyle=DRAWSTYLE_DASH; SG_VAH.PrimaryColor=RGB(156,126,184); SG_VAH.DrawZeros=0;
        SG_VAL.Name="VP VAL"; SG_VAL.DrawStyle=DRAWSTYLE_DASH; SG_VAL.PrimaryColor=RGB(156,126,184); SG_VAL.DrawZeros=0;
        SG_VB2U.Name="VWAP+2sd"; SG_VB2U.DrawStyle=DRAWSTYLE_IGNORE;
        SG_VB2L.Name="VWAP-2sd"; SG_VB2L.DrawStyle=DRAWSTYLE_IGNORE;
        SG_CTRL.Name="Control"; SG_CTRL.DrawStyle=DRAWSTYLE_IGNORE;

        IN_LIVE.Name="Enable Auto Trading (1=live)"; IN_LIVE.SetInt(1);
        IN_CAPITAL.Name="Account Capital ($)"; IN_CAPITAL.SetFloat(150000.f);
        IN_DAILY_LOSS.Name="Daily Loss $ (0=off)"; IN_DAILY_LOSS.SetFloat(800.f);
        IN_DAILY_PROF.Name="Daily Profit $ (0=off)"; IN_DAILY_PROF.SetFloat(0.f);
        IN_RTH_OPEN.Name="Session Start HHMM"; IN_RTH_OPEN.SetInt(935);
        IN_FLAT_TIME.Name="Flatten HHMM"; IN_FLAT_TIME.SetInt(1555);
        IN_LATE_ENTRY.Name="No New Entries After HHMM"; IN_LATE_ENTRY.SetInt(1500);
        IN_MAX_TRADES.Name="Max Trades/Day"; IN_MAX_TRADES.SetInt(3);
        IN_MAX_CONSEC.Name="Max Consecutive Losses"; IN_MAX_CONSEC.SetInt(2);
        IN_MAX_DAY_LOSS.Name="Max Losing Trades/Day"; IN_MAX_DAY_LOSS.SetInt(2);
        // Per-mode quality floors. Default 50 (validated production value).
        // NOTE: the M1=55/M4=40/M6=50/M8=60 profile was A/B-tested 2026-06-12
        // and FAILED on all three contracts (~-$6K each) â€” M4=40 dumps
        // low-quality sweeps. Floors are inputs so you can A/B them, but the
        // shipped default stays 50. Do not set M4 below 50 for live trading.
        IN_QF_M1.Name="Quality Floor M1"; IN_QF_M1.SetInt(50); IN_QF_M1.SetIntLimits(0,100);
        IN_QF_M4.Name="Quality Floor M4"; IN_QF_M4.SetInt(50); IN_QF_M4.SetIntLimits(0,100);
        IN_QF_M6.Name="Quality Floor M6"; IN_QF_M6.SetInt(50); IN_QF_M6.SetIntLimits(0,100);
        IN_QF_M8.Name="Quality Floor M8"; IN_QF_M8.SetInt(50); IN_QF_M8.SetIntLimits(0,100);
        IN_EN_M1.Name="Enable M1 (VWAP reclaim)"; IN_EN_M1.SetYesNo(1);
        IN_EN_M4.Name="Enable M4 (sweep+reclaim)"; IN_EN_M4.SetYesNo(1);
        IN_EN_M6.Name="Enable M6 (balance breakout)"; IN_EN_M6.SetYesNo(1);
        IN_EN_M8.Name="Enable M8 (fade)"; IN_EN_M8.SetYesNo(1);
        IN_ENTRY_ORD.Name="Entry (0=mkt,1=lmt,2=lmt+2t)"; IN_ENTRY_ORD.SetInt(2);
        IN_REGIME_FILT.Name="Regime Filter (0=off)"; IN_REGIME_FILT.SetInt(1);
        IN_NEWS_FILTER.Name="News Filter (0=off)"; IN_NEWS_FILTER.SetInt(1);
        // VP-level targets (POC/VAH/VAL) instead of ATR targets. A/B PENDING
        // as of 2026-06-12 â€” default OFF until validated cross-contract.
        IN_VP_TARGETS.Name="VP-Level Targets (0=ATR, 1=POC/VAH/VAL)"; IN_VP_TARGETS.SetInt(0);
        IN_LOG_LVL.Name="Log Level (0=crit,1=sig,2=dbg)"; IN_LOG_LVL.SetInt(1); IN_LOG_LVL.SetIntLimits(0,2);
        IN_CSV.Name="Enable CSV Journal"; IN_CSV.SetYesNo(1);
        IN_DIAG.Name="Diagnostics (0=off,1=on)"; IN_DIAG.SetInt(1);
        return;
    }

    sc.SendOrdersToTradeService = (IN_LIVE.GetInt()!=0) ? 1 : 0;

    const float TICK=sc.TickSize;
    if(TICK<=0.f) return;
    float TICK_VAL=sc.CurrencyValuePerTick;
    bool isMNQ=(strstr(sc.Symbol.GetChars(),"MNQ")||strstr(sc.Symbol.GetChars(),"mnq"));
    if(TICK_VAL<=0.f) TICK_VAL=isMNQ?0.50f:5.0f;
    const float PtVal=(TICK>0.f)?(TICK_VAL/TICK):0.f;
    const float Capital=IN_CAPITAL.GetFloat();
    const float MaxRiskPerTrade=Capital*0.02f;
    const float DAILY_LOSS=IN_DAILY_LOSS.GetFloat();
    const float DAILY_PROF=IN_DAILY_PROF.GetFloat();
    const int   RTH_OPEN=IN_RTH_OPEN.GetInt();
    const int   FLAT_TIME=IN_FLAT_TIME.GetInt();
    const int   LATE_ENTRY=IN_LATE_ENTRY.GetInt();
    const int   MAX_TRADES=IN_MAX_TRADES.GetInt();
    const int   MAX_CONSEC=IN_MAX_CONSEC.GetInt();
    const int   MAX_DAY_LOSS=IN_MAX_DAY_LOSS.GetInt();
    const int   QF[MODE_COUNT]={IN_QF_M1.GetInt(),IN_QF_M4.GetInt(),IN_QF_M6.GetInt(),IN_QF_M8.GetInt()};
    const int   EN[MODE_COUNT]={IN_EN_M1.GetYesNo(),IN_EN_M4.GetYesNo(),IN_EN_M6.GetYesNo(),IN_EN_M8.GetYesNo()};
    const int   ENTRY_ORD=IN_ENTRY_ORD.GetInt();
    const int   REGIME_FILT=IN_REGIME_FILT.GetInt();
    const int   NEWS_FILTER=IN_NEWS_FILTER.GetInt();
    const int   VP_TARGETS=IN_VP_TARGETS.GetInt();
    const int   LOG_LVL=IN_LOG_LVL.GetInt();
    const bool  CSV_ON=IN_CSV.GetYesNo();
    const int   DIAG=IN_DIAG.GetInt();

    sc.ATR(sc.BaseData, SG_ATR, C_ATR_PER, MOVAVGTYPE_WILDERS);

    const int Idx=sc.Index;
    const bool BarClosed=(sc.GetBarHasClosedStatus(Idx)==BHCS_BAR_HAS_CLOSED);
    const float Close0=sc.Close[Idx], High0=sc.High[Idx], Low0=sc.Low[Idx], Open0=sc.Open[Idx];
    const float Delta0=sc.AskVolume[Idx]-sc.BidVolume[Idx];
    { const float ad=FAbs(Delta0); const float prev=(Idx>0)?SG_ADELT[Idx-1]:ad;
      SG_ADELT[Idx]=prev+2.f/66.f*(ad-prev); }
    float ATR=SG_ATR[Idx];
    if(ATR<=0.f){ const float barR=High0-Low0; ATR=FMax(TICK*2.f, barR>TICK*0.25f?barR:TICK*4.f); }
    const float Close1=(Idx>0)?sc.Close[Idx-1]:Close0;
    const float Vol0=sc.Volume[Idx];
    const bool barBullish=(Close0>Open0), barBearish=(Close0<Open0);
    const float barBody=FAbs(Close0-Open0), barRange=High0-Low0;
    SCDateTime barDT=sc.BaseDateTimeIn[Idx];
    const int BarDate=barDT.GetDate();
    const int BarHHMM=HhmmFromSec(barDT.GetTime());

    // ---- Persistent state ----
    int& TradeState=sc.GetPersistentInt(PI_TradeState);
    int& T1Hit=sc.GetPersistentInt(PI_T1Hit);
    int& Trades=sc.GetPersistentInt(PI_Trades);
    int& LastDay=sc.GetPersistentInt(PI_LastDay);
    int& EntryBar=sc.GetPersistentInt(PI_EntryBar);
    int& LastExitBar=sc.GetPersistentInt(PI_LastExitBar);
    int& LastTradeDir=sc.GetPersistentInt(PI_LastTradeDir);
    int& DayDone=sc.GetPersistentInt(PI_DayDone);
    int& ConsecLoss=sc.GetPersistentInt(PI_ConsecLoss);
    int& DayLosses=sc.GetPersistentInt(PI_DayLosses);
    int& LastSigBar=sc.GetPersistentInt(PI_LastSigBar);
    int& VWAPBars=sc.GetPersistentInt(PI_VWAPBars);
    int& T1HitBar=sc.GetPersistentInt(PI_T1HitBar);
    int& LastVPBar=sc.GetPersistentInt(PI_LastVPBar);
    int& LastCalcBar=sc.GetPersistentInt(PI_LastCalcBar);
    int& LastStopBar=sc.GetPersistentInt(PI_LastStopBar);
    int& LastStopDir=sc.GetPersistentInt(PI_LastStopDir);
    int& TradeMode=sc.GetPersistentInt(PI_TradeMode);
    int& TradeScore=sc.GetPersistentInt(PI_TradeScore);
    int& EntryQty=sc.GetPersistentInt(PI_EntryQty);
    int& PrevPosQty=sc.GetPersistentInt(PI_PrevPosQty);
    int& FlattenReason=sc.GetPersistentInt(PI_FlattenReason);
    int& LiveTradeDir=sc.GetPersistentInt(PI_LiveTradeDir);
    int& LastSymbolHash=sc.GetPersistentInt(PI_LastSymbolHash);
    int& BannerShown=sc.GetPersistentInt(PI_BannerShown);
    int& LastExitWasLoss=sc.GetPersistentInt(PI_LastExitWasLoss);
    int& HasEnteredThisLoad=sc.GetPersistentInt(PI_HasEnteredThisLoad);
    int& SpikeBar=sc.GetPersistentInt(PI_SpikeBar);
    int& SpikeActive=sc.GetPersistentInt(PI_SpikeActive);
    int& VCoolRemaining=sc.GetPersistentInt(PI_VCoolRemaining);
    int& VCoolBar=sc.GetPersistentInt(PI_VCoolBar);
    int& LastBlockLogBar=sc.GetPersistentInt(PI_LastBlockLogBar);

    float& EntryPx=sc.GetPersistentFloat(PF_EntryPx);
    float& StopPx=sc.GetPersistentFloat(PF_StopPx);
    float& T1Px=sc.GetPersistentFloat(PF_T1Px);
    float& T2Px=sc.GetPersistentFloat(PF_T2Px);
    float& DayOpenPnL=sc.GetPersistentFloat(PF_DayOpenPnL);
    float& VWAPPxVol=sc.GetPersistentFloat(PF_VWAPPxVol);
    float& VWAPVol=sc.GetPersistentFloat(PF_VWAPVol);
    float& VWAPSqVol=sc.GetPersistentFloat(PF_VWAPSqVol);
    float& PrevSettle=sc.GetPersistentFloat(PF_PrevSettle);
    float& sessHigh=sc.GetPersistentFloat(PF_SessHigh);
    float& sessLow=sc.GetPersistentFloat(PF_SessLow);
    float& sessOpen=sc.GetPersistentFloat(PF_SessOpen);
    float& TradeMAE=sc.GetPersistentFloat(PF_TradeMAE);
    float& TradeMFE=sc.GetPersistentFloat(PF_TradeMFE);
    float& CumPnLAtEntry=sc.GetPersistentFloat(PF_CumPnLAtEntry);

    #define GETPTR(T, idx) T* p##T=(T*)sc.GetPersistentPointer(idx); \
        if(!p##T){ p##T=(T*)sc.AllocateMemory(sizeof(T)); \
            if(!p##T){ LogMsg(sc,"ERROR",SCString("alloc "#T" failed"),1); return; } \
            sc.SetPersistentPointer(idx,p##T); p##T->reset(); }
    GETPTR(VPState, PP_VPState)
    GETPTR(BalanceState, PP_BalanceState)
    GETPTR(ImbalanceState, PP_ImbalanceState)
    GETPTR(DivState, PP_DivState)
    GETPTR(FadeSetup, PP_FadeSetup)
    GETPTR(InterdayLevels, PP_InterdayLevels)
    GETPTR(RegimeClassifier, PP_RegimeClassifier)
    GETPTR(DeltaRing, PP_DeltaRing)
    GETPTR(SimpleRisk, PP_SimpleRisk)
    #undef GETPTR
    VPState* pVP=pVPState; BalanceState* pBal=pBalanceState; ImbalanceState* pImb=pImbalanceState;
    DivState* pDiv=pDivState; FadeSetup* pFade=pFadeSetup; InterdayLevels* pID=pInterdayLevels;
    RegimeClassifier* pRegime=pRegimeClassifier; DeltaRing* pDR=pDeltaRing; SimpleRisk* pRisk=pSimpleRisk;

    if(pDR) pDR->pushBar(sc, Idx);

    // Symbol-change guard â€” persistent state is per-symbol.
    {
        const char* sym=sc.Symbol.GetChars(); int h=0;
        for(int i=0; sym[i] && i<16; i++) h=h*31+(unsigned char)sym[i];
        if(h==0) h=1;
        if(LastSymbolHash==0) LastSymbolHash=h;
        else if(LastSymbolHash!=h){ LogMsg(sc,"ERROR",SCString("symbol changed; reload study"),1); LastSymbolHash=h; }
    }

    if(!BannerShown && DIAG){
        InitRunID();
        SCString m; m.Format("LOAD sym=%s tick=%.4f tickval=$%.2f Cap=$%.0f Loss=$%.0f Prof=$%.0f "
            "MaxTr=%d Consec=%d DayLoss=%d QF[M1/M4/M6/M8]=%d/%d/%d/%d EN=%d%d%d%d RM_FLOOR=%.2f RunID=%llu",
            sc.Symbol.GetChars(), TICK, TICK_VAL, Capital, DAILY_LOSS, DAILY_PROF,
            MAX_TRADES, MAX_CONSEC, MAX_DAY_LOSS, QF[0],QF[1],QF[2],QF[3],
            EN[0],EN[1],EN[2],EN[3], C_RM_FLOOR, g_runID);
        LogMsg(sc,"INIT",m,0); BannerShown=1;
    }

    // Risk EMA â€” once per bar (AutoLoop=1 fires this study many times/bar).
    if(pRisk && ATR>0.f && Idx!=pRisk->lastUpdateBar){
        pRisk->lastUpdateBar=Idx;
        pRisk->updateVolRegime(ATR);
    }

    s_SCPositionData pos; sc.GetTradePosition(pos);
    int CurQ=pos.PositionQuantity;

    // Pre-entry DayOpenPnL re-snapshot latch (v12.27/28/29). Keep DayOpenPnL
    // synced to broker CumPL on flat bars until the first live entry of the
    // day, so late-arriving carryover P&L can't masquerade as today's result.
    if(LiveTradeDir!=0) HasEnteredThisLoad=1;
    if(!HasEnteredThisLoad && CurQ==0){
        float prior=DayOpenPnL; DayOpenPnL=pos.CumulativeProfitLoss;
        if(LOG_LVL>=LOG_SIG && prior!=DayOpenPnL){
            SCString g; g.Format("DayOpenPnL re-snapshot prior=%.2f new=%.2f", prior, DayOpenPnL);
            LogMsg(sc,"INIT",g,0);
        }
    }

    // MAE / MFE tracking while in a live position.
    if(LiveTradeDir!=0 && CurQ!=0){
        bool isL=(LiveTradeDir>0);
        float adv=isL?(EntryPx-Low0):(High0-EntryPx);
        float fav=isL?(High0-EntryPx):(EntryPx-Low0);
        if(adv>TradeMAE) TradeMAE=adv;
        if(fav>TradeMFE) TradeMFE=fav;
    }

    // ========================================================================
    //  5/2. EXIT DETECTION + LOGGING (position went flat)
    // ========================================================================
    if(PrevPosQty!=0 && CurQ==0 && LiveTradeDir!=0){
        bool isL=(LiveTradeDir>0);
        int Hold=Idx-EntryBar;

        // Broker-derived fill price from realized P/L â€” ground truth (v12.24).
        float ExPxFromBroker=0.f; bool HasBrokerPx=false;
        if(EntryQty>0 && PtVal>0.f){
            float realized=pos.CumulativeProfitLoss-CumPnLAtEntry;
            float dollarsPerPt=PtVal*(float)EntryQty;
            if(dollarsPerPt>0.f){
                float pts=realized/dollarsPerPt;
                ExPxFromBroker=isL?(EntryPx+pts):(EntryPx-pts);
                HasBrokerPx=true;
                float scale=FAbs(EntryPx-StopPx); if(scale<TICK) scale=(ATR>0.f)?ATR:10.f;
                if(FAbs(ExPxFromBroker-EntryPx)>5.f*scale) HasBrokerPx=false; // stale snapshot guard
            }
        }

        const char* ExR="UNKNOWN"; int ExRcode=XR_NONE; float ExPx=Close0;
        switch(FlattenReason){
            case XR_FLATTEN:      ExR="FLATTEN"; ExRcode=XR_FLATTEN; break;
            case XR_CIRCUIT_BREAKER: ExR="CB"; ExRcode=XR_CIRCUIT_BREAKER; break;
            case XR_SPIKE:        ExR="SPIKE"; ExRcode=XR_SPIKE; break;
            case XR_DAILY_PROFIT: ExR="DAILY_PROFIT"; ExRcode=XR_DAILY_PROFIT; break;
            case XR_DAILY_LOSS:   ExR="DAILY_LOSS"; ExRcode=XR_DAILY_LOSS; break;
            case XR_STOP:         ExR="STOP"; ExRcode=XR_STOP; ExPx=StopPx; break;
            case XR_TRAIL:        ExR="TRAIL"; ExRcode=XR_TRAIL; ExPx=StopPx; break;
            case XR_SCRATCH:      ExR="SCRATCH"; ExRcode=XR_SCRATCH; break;
            case XR_T1:           ExR="TP1"; ExRcode=XR_T1; ExPx=T1Px; break;
            case XR_T2:           ExR="TP2"; ExRcode=XR_T2; ExPx=T2Px; break;
            default:
                if(isL){
                    if(Low0<=StopPx+TICK){ ExR="STOP"; ExRcode=XR_STOP; ExPx=StopPx; }
                    else if(High0>=T2Px){ ExR="TP2"; ExRcode=XR_T2; ExPx=T2Px; }
                    else if(High0>=T1Px){ ExR="TP1"; ExRcode=XR_T1; ExPx=T1Px; }
                } else {
                    if(High0>=StopPx-TICK){ ExR="STOP"; ExRcode=XR_STOP; ExPx=StopPx; }
                    else if(Low0<=T2Px){ ExR="TP2"; ExRcode=XR_T2; ExPx=T2Px; }
                    else if(Low0<=T1Px){ ExR="TP1"; ExRcode=XR_T1; ExPx=T1Px; }
                }
                break;
        }
        FlattenReason=XR_NONE;

        if(strcmp(ExR,"UNKNOWN")==0){
            ExR="EXTERNAL"; ExRcode=XR_EXTERNAL;
            if(HasBrokerPx) ExPx=ExPxFromBroker;
            if(LOG_LVL>=LOG_SIG){
                SCString w; w.Format("EXTERNAL exit H/L=%.2f/%.2f no SL/TP touched; verify Trade Activity Log", High0, Low0);
                LogMsg(sc,"EXIT",w,1);
            }
        }
        // Broker-fill correction for STOP/TRAIL (v12.25 slippage reconciliation).
        if((ExRcode==XR_STOP||ExRcode==XR_TRAIL) && HasBrokerPx){
            bool adverse=isL?(ExPxFromBroker<=StopPx+TICK):(ExPxFromBroker>=StopPx-TICK);
            if(adverse && FAbs(ExPxFromBroker-StopPx)>TICK*0.5f) ExPx=ExPxFromBroker;
        }

        float PnL=(isL?(ExPx-EntryPx):(EntryPx-ExPx))*PtVal*(float)EntryQty;
        float todayPnL=pos.CumulativeProfitLoss-DayOpenPnL;
        const char* mName=ModeName(TradeMode);

        if(pRisk) pRisk->updatePnL(PnL);

        if(LOG_LVL>=LOG_SIG){
            SCString M; M.Format("%s %s %s $%.0f Sc=%d Hold:%d MAE:%.1f MFE:%.1f",
                isL?"LONG":"SHORT", mName, ExR, PnL, TradeScore, Hold, TradeMAE, TradeMFE);
            LogMsg(sc,"EXIT",M,0);
        }
        if(CSV_ON){
            CsvRow r; r.evt="EXIT"; r.side=isL?"BUY":"SELL"; r.mode=mName;
            r.entry=EntryPx; r.sl=StopPx; r.tp1=T1Px; r.tp2=T2Px; r.qty=EntryQty; r.score=TradeScore;
            r.exitPx=ExPx; r.exitR=ExR; r.hold=Hold; r.mae=TradeMAE; r.mfe=TradeMFE;
            r.dayPnL=todayPnL; r.totPnL=pRisk?pRisk->cumPnL:0.f; r.rm=pRisk?pRisk->riskMultiplier:1.f;
            r.trendReg=pRegime?pRegime->trendRegime:0; r.volReg=pRisk?pRisk->volRegime:1; r.chopReg=pRegime?pRegime->chopRegime:0;
            WriteCsv(sc, r);
        }

        const bool isLoss=(PnL<0.f), isStop=(ExRcode==XR_STOP);
        if(isStop){ LastStopBar=Idx; LastStopDir=isL?1:-1; }
        if(isLoss){ ConsecLoss++; DayLosses++; }
        else if(PnL>0.f){ ConsecLoss=0; }
        LastExitWasLoss=isLoss?1:0;

        TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx;
        TradeMAE=0.f; TradeMFE=0.f; TradeMode=MODE_NONE; LiveTradeDir=0;
    }
    PrevPosQty=CurQ;


    // ========================================================================
    //  1. SESSION MANAGEMENT â€” day reset
    // ========================================================================
    if(BarDate!=LastDay){
        if(LastDay>0 && pID){
            pID->prevDate=LastDay; pID->prevHigh=sessHigh; pID->prevLow=sessLow;
            pID->prevClose=Close1; pID->valid=true;
        }
        if(Trades>0 && CSV_ON){
            CsvRow r; r.evt="SESSION"; r.qty=Trades;
            r.dayPnL=pRisk?pRisk->sessionPnL:0.f; r.totPnL=pRisk?pRisk->cumPnL:0.f;
            r.rm=pRisk?pRisk->riskMultiplier:1.f; r.volReg=pRisk?pRisk->volRegime:1;
            WriteCsv(sc, r);
        }
        DayOpenPnL=0.f; sc.GetTradePosition(pos); DayOpenPnL=pos.CumulativeProfitLoss;
        if(pVP){
            bool found=false;
            for(int d=0;d<VP_DAYS;d++) if(pVP->days[d].dateTag==BarDate){ pVP->activeDayIdx=d; found=true; break; }
            if(!found){
                int slot=-1;
                for(int d=0;d<VP_DAYS;d++) if(pVP->days[d].dateTag==0){ slot=d; break; }
                if(slot<0){ slot=0; int ot=pVP->days[0].dateTag;
                    for(int d=1;d<VP_DAYS;d++) if(pVP->days[d].dateTag<ot){ slot=d; ot=pVP->days[d].dateTag; } }
                pVP->activeDayIdx=slot;
                VPDay& nd=pVP->days[slot]; nd.dateTag=BarDate; nd.basePx=Close0;
                for(int b=0;b<VP_MAX_BINS;b++) nd.bins[b]=0.f;
            }
        }
        LastDay=BarDate; Trades=0; DayDone=0; EntryBar=-1; LastExitBar=-1;
        LastTradeDir=0; TradeState=0; T1Hit=0; T1HitBar=-1;
        if(LiveTradeDir!=0) LogMsg(sc,"ERROR",SCString("LiveTradeDir nonzero at rollover; reset"),1);
        LiveTradeDir=0; HasEnteredThisLoad=0;
        ConsecLoss=0; DayLosses=0; LastExitWasLoss=0; LastSigBar=-1;
        LastVPBar=-1; LastCalcBar=-1; LastStopBar=-1; LastStopDir=0;
        FlattenReason=XR_NONE; SpikeBar=-1; SpikeActive=0; VCoolRemaining=0; VCoolBar=-1;
        LastBlockLogBar=-1; TradeMode=MODE_NONE; TradeScore=0; EntryQty=0;
        TradeMAE=0.f; TradeMFE=0.f;
        EntryPx=StopPx=T1Px=T2Px=0.f; VWAPBars=0;
        float seedPx=(PrevSettle>0.f)?PrevSettle:Close0;
        VWAPPxVol=seedPx*1000.f; VWAPVol=1000.f; VWAPSqVol=seedPx*seedPx*1000.f;
        if(pBal) pBal->reset(); if(pImb) pImb->reset(); if(pDiv) pDiv->reset();
        if(pFade) pFade->reset(); if(pDR) pDR->reset(); if(pRegime) pRegime->reset();
        if(pRisk) pRisk->newSession();
        if(pID) pID->currOpen=Close0;
        if(DIAG){ SCString m; m.Format("SESSION %d Cap=$%.0f Loss=$%.0f", BarDate, Capital, DAILY_LOSS); LogMsg(sc,"SESSION",m,0); }
    }
    if(pDR && !pDR->primed) pDR->pushBar(sc, Idx);

    if(pID && BarHHMM>=RTH_OPEN){
        if(VWAPBars<=1){ sessOpen=Open0; sessHigh=High0; sessLow=Low0; }
        if(High0>sessHigh) sessHigh=High0;
        if(Low0<sessLow) sessLow=Low0;
        pID->currOpen=sessOpen;
    }

    // ---- VWAP accumulation ----
    {
        float tp=(High0+Low0+Close0)/3.f, bv=Vol0;
        VWAPPxVol+=tp*bv; VWAPVol+=bv; VWAPSqVol+=tp*tp*bv;
        if(BarHHMM>=RTH_OPEN) VWAPBars++;
        if(BarHHMM>=1755 && BarHHMM<=1805) PrevSettle=Close0;
        else if(BarHHMM>=RTH_OPEN && BarHHMM<=FLAT_TIME && PrevSettle<=0.f) PrevSettle=Close0;
    }
    const float VWAP=(VWAPVol>0.f)?(VWAPPxVol/VWAPVol):0.f;
    SG_VWAP[Idx]=(VWAP>0.f)?VWAP:Close0;
    float vwapSD=0.f;
    if(VWAPVol>0.f && VWAP>0.f){ float var=(VWAPSqVol/VWAPVol)-VWAP*VWAP; if(var>0.f) vwapSD=sqrtf(var); }
    SG_VB2U[Idx]=VWAP+vwapSD*2.f; SG_VB2L[Idx]=VWAP-vwapSD*2.f;

    // ---- Volume profile ----
    if(pVP && BarHHMM>=930 && BarHHMM<=1600 && Idx!=LastVPBar){
        LastVPBar=Idx;
        VPDay& cd=pVP->days[pVP->activeDayIdx];
        if(cd.basePx>0.f) pVP->addBar(High0,Low0,Vol0,cd.basePx);
        if(Idx%10==0 || VWAPBars<=1) pVP->compute();
    }
    if(pVP && pVP->valid && pVP->poc>1000.f){ SG_POC[Idx]=pVP->poc; SG_VAH[Idx]=pVP->vah; SG_VAL[Idx]=pVP->val; }
    else { SG_POC[Idx]=0.f; SG_VAH[Idx]=0.f; SG_VAL[Idx]=0.f; }

    if(TradeState!=0){ SG_STOP[Idx]=StopPx; SG_T1[Idx]=T1Px; SG_T2[Idx]=T2Px; }
    else { SG_STOP[Idx]=0.f; SG_T1[Idx]=0.f; SG_T2[Idx]=0.f; }
    SG_DELTA[Idx]=Delta0;
    SG_DELTA.DataColor[Idx]=(Delta0<0.f)?SG_DELTA.SecondaryColor:SG_DELTA.PrimaryColor;

    if(BarHHMM<RTH_OPEN) return;
    if(ATR<=0.f) return;

    // Signal-only gate: block live submission only during full recalc / hist DL.
    const bool inFullRecalc=sc.IsFullRecalculation!=0;
    const bool inHistDL=sc.ChartIsDownloadingHistoricalData(sc.ChartNumber)!=0;
    const bool signalOnly=inFullRecalc||inHistDL;
    const float avgD=(SG_ADELT[Idx]>0.f)?SG_ADELT[Idx]:1.f;

    // ========================================================================
    //  Spike / volatility cooldown (always-on safety net; not news-gated)
    // ========================================================================
    bool volBlock=false;
    if(Idx>=1){
        float prevRange=sc.High[Idx-1]-sc.Low[Idx-1];
        float prevATR=(Idx>=2)?SG_ATR[Idx-1]:ATR;
        float prevAvgD=(Idx>=2 && SG_ADELT[Idx-1]>0.f)?SG_ADELT[Idx-1]:avgD;
        float rangeRatio=(prevATR>0.f)?(prevRange/prevATR):0.f;
        float prevDelta=sc.AskVolume[Idx-1]-sc.BidVolume[Idx-1];
        bool rangeSpike=(prevATR>0.f && prevRange>prevATR*C_SPIKE_ATR_M);
        bool deltaSpike=(FAbs(prevDelta)>prevAvgD*4.f && rangeRatio>=2.0f);
        if((rangeSpike||deltaSpike) && SpikeBar!=Idx-1){
            SpikeBar=Idx-1; SpikeActive=1;
            if(rangeRatio>=C_VCOOL_THRESH && VCoolBar!=Idx-1){
                VCoolRemaining=C_CD_VOL_MAJOR; VCoolBar=Idx-1;
            }
        }
        if(SpikeActive && Idx<=SpikeBar+C_CD_VOL_SPIKE) volBlock=true;
        else SpikeActive=0;
        if(VCoolRemaining>0){
            if(Idx!=VCoolBar){ VCoolBar=Idx; VCoolRemaining--; } // decrement once/bar
            if(VCoolRemaining>0) volBlock=true;
        }
    }

    // ========================================================================
    //  5. TRADE MANAGEMENT (runs every bar; manages the open position)
    // ========================================================================
    if(!signalOnly){
        sc.GetTradePosition(pos); const int posQty=pos.PositionQuantity;

        // Flatten at session end.
        if(BarHHMM>=FLAT_TIME){
            if(posQty!=0 || TradeState!=0){
                FlattenReason=XR_FLATTEN; sc.FlattenAndCancelAllOrders();
                TradeState=0; T1Hit=0; T1HitBar=-1;
            }
            DayDone=1; return;
        }
        if(DayDone) return;

        // Daily caps (broker view OR strategy-internal view, conservative).
        float brokerDayPnL=pos.CumulativeProfitLoss-DayOpenPnL+pos.OpenProfitLoss;
        float stratSessPnL=pRisk?pRisk->sessionPnL:0.f;
        if(DAILY_PROF>0.f && (brokerDayPnL>=DAILY_PROF || stratSessPnL>=DAILY_PROF)){
            if(posQty!=0){ FlattenReason=XR_DAILY_PROFIT; sc.FlattenAndCancelAllOrders();
                LogMsg(sc,"DAILY",SCString("profit cap hit â€” flatten"),0); }
            DayDone=1; return;
        }
        if(DAILY_LOSS>0.f && (brokerDayPnL<=-DAILY_LOSS || stratSessPnL<=-DAILY_LOSS)){
            if(posQty!=0){ FlattenReason=XR_DAILY_LOSS; sc.FlattenAndCancelAllOrders();
                LogMsg(sc,"DAILY",SCString("loss cap hit â€” flatten"),0); }
            DayDone=1; return;
        }

        // Spike circuit-breaker: flatten a fresh position caught in a spike.
        if(SpikeActive && posQty!=0 && TradeState!=0 && SpikeBar>=0 && Idx<=SpikeBar+2){
            FlattenReason=XR_SPIKE; sc.FlattenAndCancelAllOrders();
            TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx; return;
        }

        // Catastrophic adverse-excursion circuit-breaker (3x stop distance).
        if(TradeState!=0 && posQty!=0 && EntryPx>0.f){
            bool isL=(TradeState==1||TradeState==2);
            float openPnL=isL?(Close0-EntryPx):(EntryPx-Close0);
            float stopDist=(StopPx>0.f)?FAbs(EntryPx-StopPx):ATR;
            if(openPnL<-FMax(stopDist*3.f, ATR*3.f)){
                FlattenReason=XR_CIRCUIT_BREAKER; sc.FlattenAndCancelAllOrders();
                LastStopBar=Idx; LastStopDir=isL?1:-1;
                TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx; return;
            }
        }

        // In-position management: scratch / stop / T1 / trail / T2.
        if(TradeState!=0 && Idx!=LastExitBar){
            const bool isL=(TradeState==1||TradeState==2);
            // Position closed externally but state stale â†’ clear.
            if((isL?(posQty<=0):(posQty>=0)) && Idx>EntryBar+3){
                TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx; return;
            }
            // Early-scratch (v12.37): pre-T1 trade with no favorable excursion
            // by the bar the stop becomes eligible â€” exit at market (never
            // worse than the stop it was going to take anyway).
            if(!T1Hit && Idx==EntryBar+4){
                float esStopDist=(StopPx>0.f)?FAbs(EntryPx-StopPx):ATR;
                float esOpenPnL=isL?(Close0-EntryPx):(EntryPx-Close0);
                if(TradeMFE<esStopDist*C_ES_MFE_FRAC && esOpenPnL>-esStopDist){
                    FlattenReason=XR_SCRATCH;
                    s_SCNewOrder em; em.OrderType=SCT_ORDERTYPE_MARKET; em.OrderQuantity=(int)FAbs((float)posQty);
                    if(isL) sc.BuyExit(em); else sc.SellExit(em);
                    if(esOpenPnL<0.f){ LastStopBar=Idx; LastStopDir=isL?1:-1; }
                    TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx; return;
                }
            }
            // Hard stop (pre-T1).
            if(!T1Hit && Idx>EntryBar+3){
                bool sH=isL?(Low0<=StopPx+TICK):(High0>=StopPx-TICK);
                if(sH){
                    FlattenReason=XR_STOP;
                    s_SCNewOrder em; em.OrderType=SCT_ORDERTYPE_MARKET; em.OrderQuantity=(int)FAbs((float)posQty);
                    if(isL) sc.BuyExit(em); else sc.SellExit(em);
                    LastStopBar=Idx; LastStopDir=isL?1:-1;
                    TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx; return;
                }
            }
            // T1 hit â†’ move stop to breakeven+offset. (Single-lot: no partial.)
            if(!T1Hit){
                bool t1H=isL?(High0>=T1Px):(Low0<=T1Px);
                if(t1H){
                    T1Hit=1; T1HitBar=Idx; TradeState=isL?2:4;
                    float bd=ATR*C_BE_ATR;
                    StopPx=isL?TFloor(EntryPx+bd,TICK):TCeil(EntryPx-bd,TICK);
                }
            }
            // Trail after T1 (delay x3 for single lot, per v12.37).
            if(T1Hit && C_TRAIL_ATR>0.f){
                int effDelay=C_TRAIL_DLY*3; // single-lot
                if(T1HitBar>=0 && Idx>=T1HitBar+effDelay){
                    const bool isL2=(TradeState==1||TradeState==2);
                    float t1Dist=FAbs(T1Px-EntryPx);
                    float curDist=isL2?(Close0-EntryPx):(EntryPx-Close0);
                    float baseTrail=C_TRAIL_ATR;
                    if(t1Dist>0.f && curDist<t1Dist*2.0f) baseTrail=C_TRAIL_ATR*2.5f;
                    float td=ATR*baseTrail;
                    if(t1Dist>0.f && curDist>t1Dist*2.0f) td=FMin(td,ATR*0.75f);
                    float minStop=isL2?TFloor(EntryPx+ATR*C_BE_ATR,TICK):TCeil(EntryPx-ATR*C_BE_ATR,TICK);
                    if(isL2 && StopPx<minStop) StopPx=minStop;
                    if(!isL2 && StopPx>minStop) StopPx=minStop;
                    if(isL2){ float ns=TFloor(Close0-td,TICK); if(ns>StopPx) StopPx=ns; }
                    else { float ns=TCeil(Close0+td,TICK); if(ns<StopPx) StopPx=ns; }
                }
            }
            // Trailing stop hit (post-T1).
            if(T1Hit && Idx>EntryBar+3){
                const bool isL3=(TradeState==1||TradeState==2);
                bool sH=isL3?(Low0<=StopPx+TICK):(High0>=StopPx-TICK);
                if(sH){
                    FlattenReason=XR_TRAIL;
                    s_SCNewOrder em; em.OrderType=SCT_ORDERTYPE_MARKET; em.OrderQuantity=(int)FAbs((float)posQty);
                    if(isL3) sc.BuyExit(em); else sc.SellExit(em);
                    TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx; return;
                }
            }
            // T2 hit.
            if(Idx>EntryBar+2){
                const bool isL4=(TradeState==1||TradeState==2);
                bool t2H=isL4?(High0>=T2Px):(Low0<=T2Px);
                if(t2H){
                    FlattenReason=XR_T2;
                    s_SCNewOrder em; em.OrderType=SCT_ORDERTYPE_MARKET; em.OrderQuantity=(int)FAbs((float)posQty);
                    if(isL4) sc.BuyExit(em); else sc.SellExit(em);
                    TradeState=0; T1Hit=0; T1HitBar=-1; LastExitBar=Idx; return;
                }
            }
            return; // managing an open position â€” no new entries this bar
        }

        // ---- Pre-entry gates (no open position) ----
        if(posQty!=0){ return; } // external position present
        if(Trades>=MAX_TRADES) return;
        if(MAX_DAY_LOSS>0 && DayLosses>=MAX_DAY_LOSS) return;
        if(MAX_CONSEC>0 && ConsecLoss>=MAX_CONSEC) return;
        if(Idx==LastExitBar) return;

        // Unified cooldown = max(after-trade, after-loss, post-stop[same dir], vol).
        {
            int cd=C_CD_AFTER_TRADE;
            if(LastExitWasLoss==1) cd=FMax((float)cd,(float)C_CD_AFTER_LOSS);
            if(EntryBar>=0 && Idx<=EntryBar+cd) return;
            if(LastExitBar>=0 && Idx<=LastExitBar+cd) return;
        }
    }

    if(volBlock){
        if(LOG_LVL>=LOG_SIG){ SCString s; s.Format("vol cooldown spike=%d vcool=%d", SpikeActive, VCoolRemaining); LogMsg(sc,"SKIP",s,0); }
        return;
    }


    // ---- Remaining entry-time gates ----
    bool inNews = NEWS_FILTER && ((BarHHMM>=825&&BarHHMM<=835)||(BarHHMM>=955&&BarHHMM<=1005)||(BarHHMM>=1355&&BarHHMM<=1405));
    if(inNews){ if(LOG_LVL>=LOG_SIG){ SCString s; s.Format("news window %d", BarHHMM); LogMsg(sc,"SKIP",s,0);} return; }
    if(BarHHMM>=FLAT_TIME){ return; }
    if(BarHHMM>=LATE_ENTRY){ if(LOG_LVL>=LOG_SIG){ SCString s; s.Format("late-entry gate %d>=%d", BarHHMM, LATE_ENTRY); LogMsg(sc,"SKIP",s,0);} return; }
    {
        int oM=(RTH_OPEN/100)*60+(RTH_OPEN%100), bM=(BarHHMM/100)*60+(BarHHMM%100);
        if(bM<oM+C_OPEN_COOL_MIN){ if(LOG_LVL>=LOG_SIG){ SCString s; s.Format("open cooldown bM=%d oM=%d", bM, oM); LogMsg(sc,"SKIP",s,0);} return; }
    }

    const bool vwapOK=(C_VWAP_MATURE<=0)||(VWAPBars>=C_VWAP_MATURE);
    const bool isNewBar=(Idx!=LastCalcBar);
    if(isNewBar) LastCalcBar=Idx;
    const bool deltaMature=(C_DELTA_MATURE<=0)||(VWAPBars>=C_DELTA_MATURE);
    const bool DL=(Delta0>0.f), DS=(Delta0<0.f);
    float lbDelta=pDR?pDR->sumRange(0,C_DELTA_LB-1):0.f;
    const bool LBL=(lbDelta>0.f), LBS=(lbDelta<0.f);
    if(!DL&&!DS&&!LBL&&!LBS){ if(LOG_LVL>=LOG_SIG){ SCString s; s.Format("no delta dir d0=%.0f lb=%.0f", Delta0, lbDelta); LogMsg(sc,"SKIP",s,0);} return; }

    // VWAP slope â€” directional filter for M1.
    bool sOKL=true, sOKS=true;
    if(C_VWAP_SLP_LB>0 && Idx>=C_VWAP_SLP_LB){
        float vp=SG_VWAP[Idx-C_VWAP_SLP_LB];
        if(vp>0.f && VWAP>0.f){ float slope=VWAP-vp, tol=ATR*0.02f;
            if(slope<-tol) sOKL=false; if(slope>tol) sOKS=false; }
    }

    // ========================================================================
    //  3. SIGNAL DETECTION â€” control score
    // ========================================================================
    int controlScore=0;
    if(isNewBar){
        float cdRecent=pDR?pDR->sumRange(0,7):0.f, cdOlder=pDR?pDR->sumRange(8,14):0.f;
        if(cdRecent>cdOlder*1.2f) controlScore++;
        if(cdRecent<cdOlder*1.2f && cdRecent<0.f) controlScore--;
        float pxMid=(High0+Low0)*0.5f, pxMidBack=pxMid;
        if(Idx>=15) pxMidBack=(sc.High[Idx-15]+sc.Low[Idx-15])*0.5f;
        bool pxUp=(pxMid>pxMidBack), cdUp=((cdRecent+cdOlder)>0.f);
        if(pxUp&&cdUp) controlScore++;
        if(!pxUp&&!cdUp) controlScore--;
        float tA=0.f, tB=0.f;
        for(int k=0;k<10&&Idx>=k;k++){ tA+=sc.AskVolume[Idx-k]; tB+=sc.BidVolume[Idx-k]; }
        float tV=tA+tB;
        if(tV>0.f){ float aP=tA/tV; if(aP>0.55f) controlScore++; if(aP<0.45f) controlScore--; }
        int absBuy=0, absSell=0;
        for(int k=0;k<5&&Idx>=k;k++){
            float bd=pDR?pDR->delta[k]:(sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
            bool bUp=(sc.Close[Idx-k]>=sc.Open[Idx-k]);
            if(bd<0.f&&bUp) absBuy++; if(bd>0.f&&!bUp) absSell++;
        }
        if(absBuy>=3) controlScore++; if(absSell>=3) controlScore--;
        if(pVP&&pVP->valid){ if(Close0>pVP->vah) controlScore++; if(Close0<pVP->val) controlScore--; }
        controlScore=IClamp(controlScore,-5,5);
        if(!deltaMature) controlScore=0;
        SG_CTRL[Idx]=(float)controlScore;
    } else controlScore=(int)SG_CTRL[Idx];

    // ---- Cumulative-delta divergence ----
    if(isNewBar && pDiv){
        pDiv->reset();
        if(deltaMature){
            const int TREND_LB=20, SWING_LB=8;
            if(Idx>=TREND_LB){
                float cumD=0.f, peak=-1e9f, trough=1e9f;
                for(int k=TREND_LB;k>=0;k--){ cumD+=pDR?pDR->delta[k]:(sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                    if(sc.High[Idx-k]>peak) peak=sc.High[Idx-k]; if(sc.Low[Idx-k]<trough) trough=sc.Low[Idx-k]; }
                if(High0>=peak-ATR*0.3f && cumD<0.f) pDiv->trendDivBear=true;
                if(Low0<=trough+ATR*0.3f && cumD>0.f) pDiv->trendDivBull=true;
            }
            if(Idx>=SWING_LB){
                float pxStart=sc.Close[Idx-SWING_LB], cdSwing=0.f, swHi=-1e9f, swLo=1e9f;
                for(int k=SWING_LB;k>=0;k--){ cdSwing+=pDR?pDR->delta[k]:(sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                    if(sc.High[Idx-k]>swHi) swHi=sc.High[Idx-k]; if(sc.Low[Idx-k]<swLo) swLo=sc.Low[Idx-k]; }
                if(High0>=swHi-ATR*0.3f && swHi-pxStart>ATR*0.5f && cdSwing<0.f) pDiv->swingDivBear=true;
                if(Low0<=swLo+ATR*0.3f && pxStart-swLo>ATR*0.5f && cdSwing>0.f) pDiv->swingDivBull=true;
            }
            int upDn=0, dnUp=0;
            for(int k=0;k<6&&Idx>=k;k++){ float bd=pDR?pDR->delta[k]:(sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                if((sc.Close[Idx-k]>sc.Open[Idx-k])&&bd<0.f) upDn++; else if(k>0) break; }
            for(int k=0;k<6&&Idx>=k;k++){ float bd=pDR?pDR->delta[k]:(sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                if((sc.Close[Idx-k]<sc.Open[Idx-k])&&bd>0.f) dnUp++; else if(k>0) break; }
            pDiv->persistUpPxDnDelta=upDn; pDiv->persistDnPxUpDelta=dnUp;
            pDiv->persistAbsBuy=(dnUp>=3); pDiv->persistAbsSell=(upDn>=3);
            int ds=0;
            if(pDiv->trendDivBull) ds+=2; if(pDiv->trendDivBear) ds-=2;
            if(pDiv->swingDivBull) ds+=2; if(pDiv->swingDivBear) ds-=2;
            if(pDiv->persistAbsBuy) ds+=1; if(pDiv->persistAbsSell) ds-=1;
            pDiv->strength=IClamp(ds,-5,5);
        }
    }

    // ---- Balance detection (M6/M8) + regime update ----
    if(isNewBar && pBal && Idx>=30 && ATR>0.f){
        pBal->reset();
        const int BLB=30; float rH=High0, rL=Low0, totalVol=0.f, netDelta=0.f;
        int dFlips=0; float prevD=0.f;
        const int NBINS=20; float bins[NBINS]; for(int b=0;b<NBINS;b++) bins[b]=0.f;
        for(int k=0;k<BLB&&Idx>=k;k++){ if(sc.High[Idx-k]>rH) rH=sc.High[Idx-k]; if(sc.Low[Idx-k]<rL) rL=sc.Low[Idx-k]; }
        float rW=rH-rL;
        if(rW>0.f && rW<=ATR*1.5f){
            for(int k=0;k<BLB&&Idx>=k;k++){
                float bd=pDR?pDR->delta[k]:(sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                float vol=sc.Volume[Idx-k]; netDelta+=bd; totalVol+=vol;
                if(k>0 && ((bd>0.f&&prevD<0.f)||(bd<0.f&&prevD>0.f))) dFlips++;
                prevD=bd;
                float mid=(sc.High[Idx-k]+sc.Low[Idx-k])*0.5f;
                int bin=IClamp((int)(((mid-rL)/rW)*(float)(NBINS-1)),0,NBINS-1); bins[bin]+=vol;
            }
            int pocBin=0; float maxBV=0.f;
            for(int b=0;b<NBINS;b++) if(bins[b]>maxBV){ maxBV=bins[b]; pocBin=b; }
            float centerVol=0.f;
            for(int b=NBINS*3/10;b<=NBINS*7/10;b++) centerVol+=bins[b];
            float conc=(totalVol>0.f)?(centerVol/totalVol):0.f;
            if(dFlips>=4 && conc>=0.45f && FAbs(netDelta)<totalVol*0.15f){
                pBal->active=true; pBal->mature=true; pBal->rangeHigh=rH; pBal->rangeLow=rL;
                pBal->rangePOC=rL+rW*((float)pocBin/(float)(NBINS-1));
                pBal->barCount=BLB; pBal->deltaFlips=dFlips; pBal->volumeTotal=totalVol;
            }
        }
        if(pRegime){ float pc=Close0-sc.Close[Idx-BLB<0?0:Idx-BLB]; pRegime->update(netDelta, pc, ATR, rW, BLB); pRegime->volRegime=pRisk?pRisk->volRegime:1; }
    }

    // ---- Imbalance detection (M6) ----
    if(isNewBar && pImb && Idx>=10 && ATR>0.f){
        int imbStr=0, imbDir=0;
        float d0=pDR?pDR->delta[0]:Delta0;
        float d1=pDR?pDR->delta[1]:((Idx>=1)?sc.AskVolume[Idx-1]-sc.BidVolume[Idx-1]:0.f);
        float d2=pDR?pDR->delta[2]:((Idx>=2)?sc.AskVolume[Idx-2]-sc.BidVolume[Idx-2]:0.f);
        if(d0>d1&&d1>d2&&d0>0.f){ imbStr++; imbDir=+1; }
        if(d0<d1&&d1<d2&&d0<0.f){ imbStr++; imbDir=-1; }
        float aV=0.f, bV=0.f;
        for(int k=0;k<5&&Idx>=k;k++){ aV+=sc.AskVolume[Idx-k]; bV+=sc.BidVolume[Idx-k]; }
        float tv=aV+bV, aggR=(tv>0.f)?((imbDir>=0)?(aV/tv):(bV/tv)):0.5f;
        if(aggR>=0.62f) imbStr++; if(aggR>=0.72f) imbStr++;
        int accept=0;
        for(int k=0;k<8&&Idx>=k+1;k++){
            if(imbDir>0&&sc.Close[Idx-k]>sc.High[Idx-k-1]) accept++;
            else if(imbDir<0&&sc.Close[Idx-k]<sc.Low[Idx-k-1]) accept++; else break;
        }
        if(accept>=2) imbStr++; if(accept>=4) imbStr++;
        if(imbStr>=3 && imbDir!=0){
            pImb->active=true; pImb->direction=imbDir; pImb->strength=imbStr;
            pImb->barCount++; if(pImb->startBar<0) pImb->startBar=Idx;
        } else {
            if(pImb->active && pImb->startBar>=0 && Idx<=pImb->startBar+pImb->barCount+3)
                pImb->strength=(pImb->strength>0)?pImb->strength-1:0;
            else pImb->reset();
        }
    }

    // ---- Directional confirmation score (surviving sub-signals) ----
    // Dropped vs v12.37: S7 single-print, S8 iceberg, S10 50d-VP, S11 trap
    // (their subsystems were removed). Kept: VWAP, delta, 5d-VP, VWAP-band,
    // sweep, prior-bar-delta, interday, divergence. This lowers the score
    // ceiling slightly â€” quality floors are unchanged, so v13 is marginally
    // more selective than v12.37 on M1/M4. Re-validate before live.
    const bool S2=(FAbs(Delta0)>=avgD*0.5f);
    bool S3L=false, S3S=false;
    if(pVP&&pVP->valid&&ATR>0.f){
        float z=ATR*0.75f;
        bool nPOC=FAbs(Close0-pVP->poc)<=z, nVAL=FAbs(Close0-pVP->val)<=z, nVAH=FAbs(Close0-pVP->vah)<=z;
        S3L=nVAL||(nPOC&&Close0>pVP->poc); S3S=nVAH||(nPOC&&Close0<pVP->poc);
    }
    const bool S1L=(VWAP>0.f&&Close0>VWAP), S1S=(VWAP>0.f&&Close0<VWAP);
    float vb2u=SG_VB2U[Idx], vb2l=SG_VB2L[Idx];
    bool UOBRej=(vb2u>0.f)&&(High0>=vb2u)&&(Close0<vb2u)&&(Close0<Close1);
    bool LOBBnc=(vb2l>0.f)&&(Low0<=vb2l)&&(Close0>vb2l)&&(Close0>Close1);
    int S4L=LOBBnc?2:((VWAP>0.f&&Close0<VWAP)?1:0);
    int S4S=UOBRej?2:((VWAP>0.f&&Close0>VWAP)?1:0);
    float sweepMin=ATR*0.15f;
    bool S5L=(Low0<sc.Low[Idx>0?Idx-1:Idx]-sweepMin)&&(Close0>sc.Low[Idx>0?Idx-1:Idx])&&(Close0>Close1);
    bool S5S=(High0>sc.High[Idx>0?Idx-1:Idx]+sweepMin)&&(Close0<sc.High[Idx>0?Idx-1:Idx])&&(Close0<Close1);
    bool S6L=false, S6S=false;
    if(Idx>0){ float d1a=sc.AskVolume[Idx-1]-sc.BidVolume[Idx-1];
        S6L=(sc.Close[Idx-1]<=sc.Open[Idx-1])&&(d1a>0.f); S6S=(sc.Close[Idx-1]>=sc.Open[Idx-1])&&(d1a<0.f); }
    bool S9L=false, S9S=false;
    if(pID&&pID->valid&&ATR>0.f){
        float idZ=ATR*0.5f; float lv[4]={pID->prevHigh,pID->prevLow,pID->prevClose,pID->currOpen};
        for(int i=0;i<4;i++){ if(lv[i]<=0.f||FAbs(Close0-lv[i])>idZ) continue;
            if(Close0>lv[i]&&barBullish) S9L=true; if(Close0<lv[i]&&barBearish) S9S=true; }
    }
    int S12L=0, S12S=0;
    if(deltaMature&&pDiv){ if(pDiv->strength>=2) S12L=1; if(pDiv->strength>=4) S12L=2;
        if(pDiv->strength<=-2) S12S=1; if(pDiv->strength<=-4) S12S=2; }
    int bonus=(int)S2;
    int bL=S4L+(int)S1L+(int)S3L+(int)S5L+(int)S6L+(int)S9L+S12L;
    int bS=S4S+(int)S1S+(int)S3S+(int)S5S+(int)S6S+(int)S9S+S12S;
    int scL=DL?(bL+bonus):0, scS=DS?(bS+bonus):0;
    int lbScL=LBL?(bL+bonus):0, lbScS=LBS?(bS+bonus):0;

    // ========================================================================
    //  3. MODES
    // ========================================================================
    bool mL[MODE_COUNT]={false,false,false,false}, mS[MODE_COUNT]={false,false,false,false};
    float m6Stop=0.f, m6T1=0.f, m6T2=0.f; int bkVerifyScore=10;

    // ---- M1: VWAP dip + reclaim (v12.34) ----
    if(EN[MODE_M1] && vwapOK && Idx>=1 && ATR>0.f){
        float vwZ=ATR*0.75f, tol=ATR*0.1f;
        float Low1=sc.Low[Idx-1], High1=sc.High[Idx-1];
        bool prevDipL=(Low1<=VWAP+vwZ)&&(Close1<=VWAP+tol);
        bool prevDipS=(High1>=VWAP-vwZ)&&(Close1>=VWAP-tol);
        bool reclL=(Close0>VWAP)&&barBullish&&sOKL;
        bool rejS=(Close0<VWAP)&&barBearish&&sOKS;
        bool dipL=prevDipL&&reclL&&(scL>=C_MIN_SCORE_M1)&&(controlScore>=0);
        bool dipS=prevDipS&&rejS&&(scS>=C_MIN_SCORE_M1)&&(controlScore<=0)&&!dipL;
        mL[MODE_M1]=dipL; mS[MODE_M1]=dipS;
    }

    // ---- M4: sweep + reclaim ----
    if(EN[MODE_M4] && C_SWEEP_LB>0 && Idx>=C_SWEEP_LB && ATR>0.f){
        bool ctrlL=(controlScore>=0), ctrlS=(controlScore<=0);
        int divStr=pDiv?(pDiv->strength<0?-pDiv->strength:pDiv->strength):0;
        int minSc=(divStr>=2)?C_MIN_SCORE_ALL:(C_MIN_SCORE_ALL+1);
        bool vwapEdge=(VWAP<=0.f||FAbs(Close0-VWAP)>=ATR*0.35f);
        float swLo=sc.Low[Idx-1], swHi=sc.High[Idx-1];
        for(int k=2;k<=C_SWEEP_LB&&Idx>=k;k++){ if(sc.Low[Idx-k]<swLo) swLo=sc.Low[Idx-k]; if(sc.High[Idx-k]>swHi) swHi=sc.High[Idx-k]; }
        if(vwapEdge&&Low0<swLo-TICK&&Close0>swLo&&barBullish&&(DL||LBL)&&ctrlL){
            int es=DL?scL:lbScL; if(es>=minSc) mL[MODE_M4]=true;
        }
        if(vwapEdge&&High0>swHi+TICK&&Close0<swHi&&barBearish&&(DS||LBS)&&ctrlS){
            int es=DS?scS:lbScS; if(es>=minSc) mS[MODE_M4]=true;
        }
    }

    // ---- M6: balance breakout ----
    if(EN[MODE_M6] && pBal && pBal->mature && pImb && ATR>0.f){
        float bkT=ATR*0.30f; int breakDir=0;
        if(Close0>pBal->rangeHigh+bkT && pImb->active && pImb->direction==+1 && barBullish && DL) breakDir=+1;
        if(Close0<pBal->rangeLow-bkT && pImb->active && pImb->direction==-1 && barBearish && DS) breakDir=-1;
        if(breakDir!=0){
            bkVerifyScore=0;
            float balAvg=(pBal->volumeTotal>0.f&&pBal->barCount>0)?(pBal->volumeTotal/(float)pBal->barCount):Vol0;
            float bkLev=(breakDir>0)?pBal->rangeHigh:pBal->rangeLow;
            int dStreak=0;
            for(int k=0;k<5&&Idx>=k;k++){ float bd=pDR?pDR->delta[k]:(sc.AskVolume[Idx-k]-sc.BidVolume[Idx-k]);
                if((breakDir>0&&bd>0.f)||(breakDir<0&&bd<0.f)) dStreak++; else break; }
            if(dStreak>=3) bkVerifyScore++;
            float bkVol=0.f; for(int k=0;k<3&&Idx>=k;k++) bkVol+=sc.Volume[Idx-k];
            if(bkVol/3.f>balAvg*1.3f) bkVerifyScore++;
            int accBars=0;
            for(int k=0;k<3&&Idx>=k;k++){ if(breakDir>0&&sc.Close[Idx-k]>bkLev) accBars++; if(breakDir<0&&sc.Close[Idx-k]<bkLev) accBars++; }
            if(accBars>=2) bkVerifyScore++;
            bool retest=false;
            for(int k=0;k<2&&Idx>=k;k++){ if(breakDir>0&&sc.Low[Idx-k]<bkLev) retest=true; if(breakDir<0&&sc.High[Idx-k]>bkLev) retest=true; }
            if(!retest) bkVerifyScore++;
            float oN=0.f, oP=0.f;
            for(int k=0;k<3&&Idx>=k;k++){ if(breakDir>0) oN+=sc.BidVolume[Idx-k]; else oN+=sc.AskVolume[Idx-k]; }
            for(int k=3;k<6&&Idx>=k;k++){ if(breakDir>0) oP+=sc.BidVolume[Idx-k]; else oP+=sc.AskVolume[Idx-k]; }
            if(oP>0.f&&oN<oP*0.65f) bkVerifyScore++;
            float vBeyond=0.f, vAt=0.f;
            for(int k=0;k<3&&Idx>=k;k++){ float mid=(sc.High[Idx-k]+sc.Low[Idx-k])*0.5f;
                if((breakDir>0&&mid>bkLev)||(breakDir<0&&mid<bkLev)) vBeyond+=sc.Volume[Idx-k]; else vAt+=sc.Volume[Idx-k]; }
            if(vBeyond>vAt*1.5f) bkVerifyScore++;
            bool noDivConf=true;
            if(breakDir>0&&pDiv&&pDiv->strength<=-2) noDivConf=false;
            if(breakDir<0&&pDiv&&pDiv->strength>=2) noDivConf=false;
            if(noDivConf) bkVerifyScore++;
            float swRet=(breakDir>0)?(High0-Close0):(Close0-Low0);
            if(swRet<ATR*0.3f) bkVerifyScore++;
            if(bkVerifyScore>=6){
                float rangeW=pBal->rangeHigh-pBal->rangeLow;
                if(breakDir>0){ mL[MODE_M6]=true; m6Stop=pBal->rangeHigh-ATR*0.25f; m6T1=pBal->rangeHigh+rangeW*0.5f; m6T2=pBal->rangeHigh+rangeW; }
                else { mS[MODE_M6]=true; m6Stop=pBal->rangeLow+ATR*0.25f; m6T1=pBal->rangeLow-rangeW*0.5f; m6T2=pBal->rangeLow-rangeW; }
            }
        }
    }
    // M6 soft control gate.
    if(mL[MODE_M6] && controlScore<-1){ mL[MODE_M6]=false; m6Stop=m6T1=m6T2=0.f; }
    if(mS[MODE_M6] && controlScore> 1){ mS[MODE_M6]=false; m6Stop=m6T1=m6T2=0.f; }

    // ---- M8: fade (simplified â€” balance-edge fade + trend exhaustion) ----
    // Removed vs v12.37 M8: trap-based (type1 edge), the M7 auction-reversal
    // (type2), iceberg edge terms, absorption(type4). Those subsystems are
    // gone. Retained: failed-breakout fade at a balance edge, and trend
    // exhaustion. This is a NARROWER M8 than v12.37 â€” re-validate before live.
    if(EN[MODE_M8] && pFade){
        pFade->reset();
        // Balance-edge fade: weak breakout that closes back inside.
        if(pBal && pBal->mature && ATR>0.f && bkVerifyScore<=4){
            float bkT=ATR*0.30f;
            if(High0>pBal->rangeHigh+bkT*0.5f && Close0<pBal->rangeHigh+bkT*0.3f && barBearish){
                int edge=(bkVerifyScore<=2)?2:1;
                if(pDiv&&pDiv->strength<=-2) edge+=2;
                if(pDiv&&pDiv->persistAbsSell) edge+=1;
                if(edge>=4){ mS[MODE_M8]=true; pFade->type=1; pFade->direction=-1; pFade->edgeScore=IClamp(edge,0,10);
                    pFade->active=true; pFade->stopPx=High0+ATR*0.3f; pFade->t1Px=pBal->rangePOC; pFade->t2Px=pBal->rangeLow;
                    pFade->entryPx=Close0; pFade->triggerBar=Idx; }
            }
            if(!mS[MODE_M8] && Low0<pBal->rangeLow-bkT*0.5f && Close0>pBal->rangeLow-bkT*0.3f && barBullish){
                int edge=(bkVerifyScore<=2)?2:1;
                if(pDiv&&pDiv->strength>=2) edge+=2;
                if(pDiv&&pDiv->persistAbsBuy) edge+=1;
                if(edge>=4){ mL[MODE_M8]=true; pFade->type=1; pFade->direction=+1; pFade->edgeScore=IClamp(edge,0,10);
                    pFade->active=true; pFade->stopPx=Low0-ATR*0.3f; pFade->t1Px=pBal->rangePOC; pFade->t2Px=pBal->rangeHigh;
                    pFade->entryPx=Close0; pFade->triggerBar=Idx; }
            }
        }
        // Trend-exhaustion fade.
        if(!mL[MODE_M8] && !mS[MODE_M8] && Idx>=8 && ATR>0.f){
            int trendBars=0;
            for(int k=0;k<8&&Idx>=k;k++) trendBars+=(sc.Close[Idx-k]>sc.Open[Idx-k])?1:-1;
            bool upT=(trendBars>=4), dnT=(trendBars<=-4);
            if(upT||dnT){
                float dm0=FAbs(pDR?pDR->delta[0]:Delta0);
                float dm1=(Idx>=1)?FAbs(pDR?pDR->delta[1]:(sc.AskVolume[Idx-1]-sc.BidVolume[Idx-1])):dm0;
                float dm2=(Idx>=2)?FAbs(pDR?pDR->delta[2]:(sc.AskVolume[Idx-2]-sc.BidVolume[Idx-2])):dm0;
                bool deltaDec=(dm0<dm1&&dm1<dm2);
                bool bodySmall=(barRange>0.f&&barBody<barRange*0.4f);
                bool volDec=(Idx>=2&&sc.Volume[Idx]<sc.Volume[Idx-1]&&sc.Volume[Idx-1]<sc.Volume[Idx-2]);
                int exhE=0;
                if(deltaDec) exhE+=2; if(bodySmall) exhE+=1; if(volDec) exhE+=2;
                if(upT&&pDiv&&pDiv->strength<=-2) exhE+=2;
                if(dnT&&pDiv&&pDiv->strength>=2) exhE+=2;
                if(exhE>=6){
                    if(upT){ mS[MODE_M8]=true; pFade->direction=-1; pFade->stopPx=High0+ATR*0.4f;
                        pFade->t1Px=Close0-ATR*1.0f; pFade->t2Px=(VWAP>0.f)?VWAP:(Close0-ATR*2.f); }
                    if(dnT&&!mS[MODE_M8]){ mL[MODE_M8]=true; pFade->direction=+1; pFade->stopPx=Low0-ATR*0.4f;
                        pFade->t1Px=Close0+ATR*1.0f; pFade->t2Px=(VWAP>0.f)?VWAP:(Close0+ATR*2.f); }
                    if(mL[MODE_M8]||mS[MODE_M8]){ pFade->type=3; pFade->edgeScore=IClamp(exhE,0,10); pFade->active=true; pFade->triggerBar=Idx; pFade->entryPx=Close0; }
                }
            }
        }
        // M8 strict control gate.
        if(mL[MODE_M8] && controlScore<0){ mL[MODE_M8]=false; pFade->reset(); }
        if(mS[MODE_M8] && controlScore>0){ mS[MODE_M8]=false; pFade->reset(); }
    }

    // ========================================================================
    //  4. ORDER PLACEMENT â€” selection, final gates, sizing, submission
    // ========================================================================
    // Priority: M6 > M8 > M4 > M1 (breakout > fade > reversal > pullback).
    int selMode=MODE_NONE; bool selLong=false;
    if(mL[MODE_M6]){ selMode=MODE_M6; selLong=true; }
    else if(mS[MODE_M6]){ selMode=MODE_M6; selLong=false; }
    else if(mL[MODE_M8]){ selMode=MODE_M8; selLong=true; }
    else if(mS[MODE_M8]){ selMode=MODE_M8; selLong=false; }
    else if(mL[MODE_M4]){ selMode=MODE_M4; selLong=true; }
    else if(mS[MODE_M4]){ selMode=MODE_M4; selLong=false; }
    else if(mL[MODE_M1]){ selMode=MODE_M1; selLong=true; }
    else if(mS[MODE_M1]){ selMode=MODE_M1; selLong=false; }
    if(selMode==MODE_NONE) return;

    const char* mN=ModeName(selMode);
    int rejCode=RJ_NONE;

    // Regime filter.
    if(REGIME_FILT && pRegime && !pRegime->allowMode(selMode, selLong)) rejCode=RJ_REGIME;
    // Abnormal volatility regime (ATR ratio extreme).
    else if(pRisk && pRisk->volRegime==3) rejCode=RJ_VOL_EXTREME;
    // M1-specific: dead zone + trend gate.
    else if(selMode==MODE_M1 && BarHHMM>=1200 && BarHHMM<=1359) rejCode=RJ_M1_DEADZONE;
    else if(selMode==MODE_M1 && ATR>0.f && Idx>=20 &&
            ((selLong && (sc.Close[Idx]-sc.Close[Idx-20])<-ATR*0.5f) ||
             (!selLong && (sc.Close[Idx]-sc.Close[Idx-20])> ATR*0.5f))) rejCode=RJ_M1_TREND;
    // Post-stop same-direction cooldown (part of the unified cooldown).
    else if(LastStopBar>=0 && Idx<=LastStopBar+C_CD_POST_STOP &&
            ((selLong&&LastStopDir>0)||(!selLong&&LastStopDir<0))) rejCode=RJ_COOLDOWN;
    // Wait for bar close.
    else if(!BarClosed) return;

    if(rejCode==RJ_NONE && pRisk){
        pRisk->computeRM();
        if(pRisk->riskMultiplier<C_RM_FLOOR) rejCode=RJ_RM_FLOOR;
        else if(DAILY_LOSS>0.f && pRisk->sessionDD>DAILY_LOSS*0.8f) rejCode=RJ_SESSION_DD;
    }

    // Quality score + per-mode floor.
    int finalScore = selLong ? (DL?scL:(LBL?lbScL:0)) : (DS?scS:(LBS?lbScS:0));
    int edgeScore  = (pFade&&pFade->active)?pFade->edgeScore:0;
    int qScore     = Quality100(selMode, finalScore, edgeScore);
    if(rejCode==RJ_NONE && qScore<QF[selMode]) rejCode=RJ_QUAL_FLOOR;

    // VWAP / VP relation (for logging).
    float vwapRel=(VWAP>0.f)?(Close0-VWAP):0.f;
    float pocRel=(pVP&&pVP->valid)?(Close0-pVP->poc):0.f;
    float vahRel=(pVP&&pVP->valid)?(Close0-pVP->vah):0.f;
    float valRel=(pVP&&pVP->valid)?(Close0-pVP->val):0.f;

    if(rejCode!=RJ_NONE){
        if(LOG_LVL>=LOG_SIG && Idx!=LastBlockLogBar){
            LastBlockLogBar=Idx;
            SCString m; m.Format("%s %s %s q=%d floor=%d sc=%d", mN, selLong?"L":"S", RejectReasonName(rejCode), qScore, QF[selMode], finalScore);
            LogMsg(sc,"REJECT",m,0);
        }
        if(CSV_ON){
            CsvRow r; r.evt="REJECT"; r.side=selLong?"BUY":"SELL"; r.mode=mN;
            r.score=finalScore; r.qual=qScore; r.ctrl=controlScore; r.divStr=pDiv?pDiv->strength:0;
            r.delta=Delta0; r.vwapRel=vwapRel; r.pocRel=pocRel; r.vahRel=vahRel; r.valRel=valRel;
            r.rejR=RejectReasonName(rejCode);
            r.dayPnL=pos.CumulativeProfitLoss-DayOpenPnL+pos.OpenProfitLoss; r.totPnL=pRisk?pRisk->cumPnL:0.f;
            r.rm=pRisk?pRisk->riskMultiplier:1.f; r.trendReg=pRegime?pRegime->trendRegime:0;
            r.volReg=pRisk?pRisk->volRegime:1; r.chopReg=pRegime?pRegime->chopRegime:0;
            WriteCsv(sc, r);
        }
        return;
    }

    // ---- Entry / stop / target ----
    float entryPx=Close0, stopPx=0.f, tp1Px=0.f, tp2Px=0.f;
    if(selMode==MODE_M6 && m6Stop!=0.f){ stopPx=m6Stop; tp1Px=m6T1; tp2Px=m6T2; }
    else if(selMode==MODE_M8 && pFade && pFade->active){ stopPx=pFade->stopPx; tp1Px=pFade->t1Px; tp2Px=pFade->t2Px; }
    if(selMode==MODE_M6 || selMode==MODE_M8){
        if(selLong){ stopPx=TFloor(stopPx,TICK); tp1Px=TCeil(tp1Px,TICK); tp2Px=TCeil(tp2Px,TICK); }
        else { stopPx=TCeil(stopPx,TICK); tp1Px=TFloor(tp1Px,TICK); tp2Px=TFloor(tp2Px,TICK); }
    } else {
        // M1 / M4 â€” ATR stop (1.2) with floor/ceil; ATR targets by default.
        float sd=FClamp(ATR*C_STOP_ATR, C_STOP_FLOOR_PTS, C_STOP_CEIL_PTS);
        float t1d=FClamp(ATR*C_T1_ATR, C_T1_FLOOR_PTS, C_T1_CEIL_PTS);
        float t2d=FClamp(ATR*C_T2_ATR, C_T2_FLOOR_PTS, C_T2_CEIL_PTS);
        if(selLong){ stopPx=TFloor(entryPx-sd,TICK); tp1Px=TCeil(entryPx+t1d,TICK); tp2Px=TCeil(entryPx+t2d,TICK); }
        else { stopPx=TCeil(entryPx+sd,TICK); tp1Px=TFloor(entryPx-t1d,TICK); tp2Px=TFloor(entryPx-t2d,TICK); }
        // Optional VP-level targets (A/B pending â€” default off).
        if(VP_TARGETS && pVP && pVP->valid && ATR>0.f){
            float mind=ATR*0.5f;
            float lv[4]={pVP->poc,pVP->vah,pVP->val,VWAP};
            if(selLong){
                float best1=0.f, best2=0.f;
                for(int i=0;i<4;i++){ if(lv[i]<=entryPx+mind) continue;
                    if(best1<=0.f||lv[i]<best1){ best2=best1; best1=lv[i]; } else if(best2<=0.f||lv[i]<best2) best2=lv[i]; }
                if(best1>0.f) tp1Px=TCeil(best1,TICK);
                if(best2>0.f) tp2Px=TCeil(best2,TICK);
            } else {
                float best1=0.f, best2=0.f;
                for(int i=0;i<4;i++){ if(lv[i]<=0.f||lv[i]>=entryPx-mind) continue;
                    if(lv[i]>best1){ best2=best1; best1=lv[i]; } else if(lv[i]>best2) best2=lv[i]; }
                if(best1>0.f) tp1Px=TFloor(best1,TICK);
                if(best2>0.f && best2<best1) tp2Px=TFloor(best2,TICK);
            }
        }
    }
    // Sanity â€” stop on correct side, minimum distances, T2 beyond T1.
    float minStopDist=FMax(ATR*0.5f, C_STOP_FLOOR_PTS);
    if(selLong){
        if(stopPx>=entryPx-TICK) stopPx=TFloor(entryPx-minStopDist,TICK);
        if(tp1Px<=entryPx+TICK) tp1Px=TCeil(entryPx+FMax(ATR,C_T1_FLOOR_PTS),TICK);
        if(tp2Px<=tp1Px) tp2Px=TCeil(tp1Px+FMax(ATR,25.f),TICK);
    } else {
        if(stopPx<=entryPx+TICK) stopPx=TCeil(entryPx+minStopDist,TICK);
        if(tp1Px>=entryPx-TICK) tp1Px=TFloor(entryPx-FMax(ATR,C_T1_FLOOR_PTS),TICK);
        if(tp2Px>=tp1Px) tp2Px=TFloor(tp1Px-FMax(ATR,25.f),TICK);
    }

    // Sizing: always 1 contract (RM gates entry, does not scale size).
    int baseQty=1;
    {
        float stopDist=FAbs(entryPx-stopPx), riskDollars=stopDist*PtVal*(float)baseQty;
        if(riskDollars>MaxRiskPerTrade && PtVal>0.f && stopDist>0.f){
            if(LOG_LVL>=LOG_SIG){ SCString m; m.Format("%s %s 1-lot risk $%.0f exceeds cap $%.0f", mN, selLong?"L":"S", riskDollars, MaxRiskPerTrade); LogMsg(sc,"REJECT",m,0); }
            return;
        }
    }
    if(LastSigBar==Idx) return; // one signal per bar

    // ---- SETUP log + CSV ----
    if(LOG_LVL>=LOG_SIG){
        SCString m; m.Format("%s %s Q=%d Sc=%d Ent=%.2f SL=%.2f T1=%.2f T2=%.2f Qty=%d RM=%.2f",
            selLong?"LONG":"SHORT", mN, qScore, finalScore, entryPx, stopPx, tp1Px, tp2Px, baseQty, pRisk?pRisk->riskMultiplier:1.f);
        LogMsg(sc,"SETUP",m,0);
    }
    if(CSV_ON){
        CsvRow r; r.evt="SETUP"; r.side=selLong?"BUY":"SELL"; r.mode=mN;
        r.entry=entryPx; r.sl=stopPx; r.tp1=tp1Px; r.tp2=tp2Px; r.qty=baseQty; r.score=finalScore; r.qual=qScore;
        r.ctrl=controlScore; r.divStr=pDiv?pDiv->strength:0; r.delta=Delta0;
        r.vwapRel=vwapRel; r.pocRel=pocRel; r.vahRel=vahRel; r.valRel=valRel;
        r.dayPnL=pos.CumulativeProfitLoss-DayOpenPnL+pos.OpenProfitLoss; r.totPnL=pRisk?pRisk->cumPnL:0.f;
        r.rm=pRisk?pRisk->riskMultiplier:1.f; r.trendReg=pRegime?pRegime->trendRegime:0;
        r.volReg=pRisk?pRisk->volRegime:1; r.chopReg=pRegime?pRegime->chopRegime:0;
        WriteCsv(sc, r);
    }

    if(signalOnly) return; // historical / replay â€” logged, not submitted

    // ---- Submit live order ----
    s_SCNewOrder o; o.OrderQuantity=baseQty;
    if(ENTRY_ORD==0) o.OrderType=SCT_ORDERTYPE_MARKET;
    else if(ENTRY_ORD==1){ o.OrderType=SCT_ORDERTYPE_LIMIT; o.Price1=entryPx; }
    else { o.OrderType=SCT_ORDERTYPE_LIMIT; o.Price1=selLong?(entryPx+TICK*2.f):(entryPx-TICK*2.f); }
    o.Stop1Price=stopPx; o.TimeInForce=SCT_TIF_DAY;
    o.Target1Price=tp1Px; o.Target2Price=0.f; o.OCOGroup1Quantity=0; // single lot â†’ one target to T1

    int rc = selLong ? (int)sc.BuyEntry(o) : (int)sc.SellEntry(o);
    if(rc>0){
        TradeState=selLong?1:3; EntryPx=entryPx; StopPx=stopPx; T1Px=tp1Px; T2Px=tp2Px;
        EntryBar=Idx; LastTradeDir=selLong?1:-1; LiveTradeDir=selLong?1:-1;
        TradeMode=selMode; TradeScore=finalScore; EntryQty=baseQty;
        TradeMAE=0.f; TradeMFE=0.f; CumPnLAtEntry=pos.CumulativeProfitLoss;
        Trades++; LastSigBar=Idx;
        if(LOG_LVL>=LOG_CRIT){
            SCString m; m.Format("%s %s Q=%d Ent=%.2f SL=%.2f T1=%.2f T2=%.2f Qty=%d Trade#%d",
                selLong?"LONG":"SHORT", mN, qScore, entryPx, stopPx, tp1Px, tp2Px, baseQty, Trades);
            LogMsg(sc,"ENTRY",m,0);
        }
        if(CSV_ON){
            CsvRow r; r.evt="ENTRY"; r.side=selLong?"BUY":"SELL"; r.mode=mN;
            r.entry=entryPx; r.sl=stopPx; r.tp1=tp1Px; r.tp2=tp2Px; r.qty=baseQty; r.score=finalScore; r.qual=qScore;
            r.ctrl=controlScore; r.divStr=pDiv?pDiv->strength:0; r.delta=Delta0;
            r.vwapRel=vwapRel; r.pocRel=pocRel; r.vahRel=vahRel; r.valRel=valRel;
            r.dayPnL=pos.CumulativeProfitLoss-DayOpenPnL+pos.OpenProfitLoss; r.totPnL=pRisk?pRisk->cumPnL:0.f;
            r.rm=pRisk?pRisk->riskMultiplier:1.f; r.trendReg=pRegime?pRegime->trendRegime:0;
            r.volReg=pRisk?pRisk->volRegime:1; r.chopReg=pRegime?pRegime->chopRegime:0;
            WriteCsv(sc, r);
        }
        if(selLong) SG_BUY[Idx]=Low0-TICK*4; else SG_SELL[Idx]=High0+TICK*4;
    } else {
        if(LOG_LVL>=LOG_CRIT){ SCString m; m.Format("%s %s order rc=%d", selLong?"LONG":"SHORT", mN, rc); LogMsg(sc,"REJECT",m,1); }
        if(CSV_ON){
            CsvRow r; r.evt="REJECT"; r.side=selLong?"BUY":"SELL"; r.mode=mN;
            r.entry=entryPx; r.sl=stopPx; r.tp1=tp1Px; r.tp2=tp2Px; r.qty=baseQty; r.score=finalScore; r.qual=qScore;
            r.ctrl=controlScore; r.divStr=pDiv?pDiv->strength:0; r.delta=Delta0;
            r.vwapRel=vwapRel; r.pocRel=pocRel; r.vahRel=vahRel; r.valRel=valRel;
            r.rejR="ORDER_RC";
            r.dayPnL=pos.CumulativeProfitLoss-DayOpenPnL+pos.OpenProfitLoss; r.totPnL=pRisk?pRisk->cumPnL:0.f;
            r.rm=pRisk?pRisk->riskMultiplier:1.f; r.trendReg=pRegime?pRegime->trendRegime:0;
            r.volReg=pRisk?pRisk->volRegime:1; r.chopReg=pRegime?pRegime->chopRegime:0;
            WriteCsv(sc, r);
        }
    }
}

// ============================================================================
//  END â€” IOF NQ Pure Orderflow Autopilot v13
// ============================================================================

