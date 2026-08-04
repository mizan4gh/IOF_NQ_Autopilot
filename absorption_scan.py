"""Absorption-reversal edge scan (standalone, measure-before-wiring).

"Absorption" = heavy AGGRESSIVE volume hits a price extreme but price is
absorbed by a passive resister and REVERSES (the footprint markup in
absorbtionpro.png: red cells / orange down-arrows at a swept high, blue cells at
a swept low). This reconstructs the true per-price bid x ask footprint from .scid
ticks, detects absorption at swept extremes, simulates a fixed-risk reversal
trade, and reports the raw edge per contract.

Aggressor convention (Sierra .scid): ask_vol = trades at the ASK = aggressive
BUYING; bid_vol = trades at the BID = aggressive SELLING. (Careful: this is the
side the Python Imb subsystem historically got wrong -- see memory
reference_backtest_imb_extreme_bug.)

Signal shapes:
  BEARISH (short) : bar HIGH sweeps prior C_SWEEP_LB high; in the top zone of the
                    bar, ASK vol (buyers) is heavy & ask-dominant, yet close
                    rejects back down -> buyers absorbed -> fade short.
  BULLISH (long)  : bar LOW sweeps prior low; bottom-zone BID vol (sellers)
                    heavy & bid-dominant, close rejects up -> sellers absorbed.

This is NOT wired into the strategy. If it shows a cross-contract edge, THEN it
gets added to backtest.py as a gated arm and A/B'd (per feedback_cross_contract_ab).

Usage: python absorption_scan.py [TAG ...]     (default: NQZ25 NQM5 NQH6)
"""
import sys, math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import backtest as B   # reuse reader, constants, epoch/tz handling

BASE = Path(__file__).parent
CONTRACTS = {
    "NQZ25": BASE / "F.US.ENQZ25.scid",
    "NQM5":  BASE / "F.US.ENQM25.scid",
    "NQH6":  BASE / "F.US.ENQH26.scid",
}

# ── absorption knobs ────────────────────────────────────────────────────────
MODE       = "cont"          # "fade" = reverse the absorbed extreme (FALSIFIED);
                             # "cont" = trade WITH the sweep when price HOLDS the
                             #          extreme (aggressor winning, breakout).
SWEEP_LB   = B.C_SWEEP_LB      # 15 — prior-extreme lookback for the sweep
TOP_FRAC   = 0.30             # "extreme zone" = top/bottom 30% of the bar range
REJECT_FRAC= 0.50             # fade: close must retrace >= this back from extreme
HOLD_FRAC  = 0.34             # cont: close must stay within this of the extreme
ABS_MIN    = 400             # min aggressor vol in the extreme zone (absolute)
ABS_RATIO  = 1.8             # extreme-zone aggressor / opposite-side ratio
CONFIRM    = True            # require next-bar follow-through, enter at ITS close
# trade model
STOP_BUF_T = 3               # fade: stop = swept extreme +/- this many ticks
CONT_STOP_CAP_ATR = 1.5      # cont: cap stop risk at this * ATR (opposite bar end)
R_TARGET   = 1.5             # target = R_TARGET * stop distance
COST_TKS   = 2.0             # per-trade cost (entry slippage + commission), in ticks
ATR_PER    = B.ATR_PER


class FBar:
    __slots__ = ("o","h","l","c","vol","idx","date_tag","hhmm","vap","dt")
    def __init__(s): s.vap = defaultdict(lambda: [0,0])  # tick -> [bidvol, askvol]


def build_footprint_bars(recs, target_vol, price_scale):
    """Volume bars that also retain a per-price {tick:[bidvol,askvol]} footprint."""
    bars=[]; idx=0
    o=h=l=c=0.0; vol=0; vap=defaultdict(lambda:[0,0]); first=None
    for rec in recs:
        tv=int(rec["tot_vol"])
        if tv==0: continue
        dt=(B.SC_EPOCH_UTC+timedelta(microseconds=int(rec["dt"]))).astimezone(B.ET)
        c=float(rec["close"])/price_scale
        ro=float(rec["open"])/price_scale; rh=float(rec["high"])/price_scale; rl=float(rec["low"])/price_scale
        if not math.isfinite(ro) or ro==0.0: ro=c
        if not math.isfinite(rh) or rh==0.0: rh=c
        if not math.isfinite(rl) or rl==0.0: rl=c
        bv=int(rec["bid_vol"]); av=int(rec["ask_vol"])
        if first is None: o=ro; h=rh; l=rl; first=dt
        h=max(h,rh); l=min(l,rl); vol+=tv
        pk=int(round(c/B.TICK))            # footprint keyed by price tick
        cell=vap[pk]; cell[0]+=bv; cell[1]+=av
        while vol>=target_vol:
            fb=FBar(); fb.o=o; fb.h=h; fb.l=l; fb.c=c; fb.vol=vol; fb.idx=idx
            fb.dt=dt; fb.hhmm=dt.hour*100+dt.minute
            fb.date_tag=dt.year*10000+dt.month*100+dt.day
            fb.vap=vap; bars.append(fb); idx+=1
            excess=vol-target_vol
            o=c; h=c; l=c; vol=excess; vap=defaultdict(lambda:[0,0]); first=dt
            if excess>0:
                pk=int(round(c/B.TICK)); vap[pk]=[0,0]   # carry no split vol (approx)
    return bars


def atr_series(bars, per=14):
    atr=[0.0]*len(bars); tr_ema=None; pc=None
    for i,b in enumerate(bars):
        tr=(b.h-b.l) if pc is None else max(b.h-b.l, abs(b.h-pc), abs(b.l-pc))
        tr_ema=tr if tr_ema is None else (tr_ema*(per-1)+tr)/per
        atr[i]=tr_ema; pc=b.c
    return atr


def zone_vol(fb, top):
    """Sum [bid,ask] over the top (or bottom) TOP_FRAC of the bar's range."""
    rng=fb.h-fb.l
    if rng<=0: return 0,0
    cut = fb.h-rng*TOP_FRAC if top else fb.l+rng*TOP_FRAC
    bid=ask=0
    for pk,(b,a) in fb.vap.items():
        px=pk*B.TICK
        if (top and px>=cut) or ((not top) and px<=cut):
            bid+=b; ask+=a
    return bid,ask


def detect(fb):
    """Return (side, swept) or None.  side in {'S','L'} = trade direction;
    swept in {'high','low'} = which prior extreme the bar must have taken out."""
    rng=fb.h-fb.l
    if rng<=0: return None
    bid_t,ask_t=zone_vol(fb,True)     # top zone
    bid_b,ask_b=zone_vol(fb,False)    # bottom zone
    if MODE=="fade":
        # buyers heavy at top but price rejects down -> fade SHORT (needs high sweep)
        if ask_t>=ABS_MIN and ask_t>=ABS_RATIO*(bid_t+1) and fb.c<=fb.h-REJECT_FRAC*rng:
            return ("S","high")
        # sellers heavy at bottom but price rejects up -> fade LONG (needs low sweep)
        if bid_b>=ABS_MIN and bid_b>=ABS_RATIO*(ask_b+1) and fb.c>=fb.l+REJECT_FRAC*rng:
            return ("L","low")
    else:  # cont: aggressor drives the sweep AND price holds the extreme
        # buyers heavy at top, close holds high -> LONG continuation (high sweep)
        if ask_t>=ABS_MIN and ask_t>=ABS_RATIO*(bid_t+1) and fb.c>=fb.h-HOLD_FRAC*rng:
            return ("L","high")
        # sellers heavy at bottom, close holds low -> SHORT continuation (low sweep)
        if bid_b>=ABS_MIN and bid_b>=ABS_RATIO*(ask_b+1) and fb.c<=fb.l+HOLD_FRAC*rng:
            return ("S","low")
    return None


def run(tag, scid):
    scale=B.detect_price_scale(str(scid))
    recs=B.read_scid(str(scid))
    bars=build_footprint_bars(recs, B.TARGET_VOL, scale)
    rth=[b for b in bars if B.RTH_OPEN<=b.hhmm<B.FLATTEN_HHMM]
    atr=atr_series(bars, ATR_PER)
    aidx={b.idx:atr[i] for i,b in enumerate(bars)}

    trades=[]
    i=0
    n=len(rth)
    while i<n:
        b=rth[i]
        if b.idx<SWEEP_LB: i+=1; continue
        # prior-extreme sweep test using the full bar list around this bar
        gi=b.idx
        prev=bars[gi-SWEEP_LB:gi]
        if not prev: i+=1; continue
        ph=max(x.h for x in prev); pl=min(x.l for x in prev)
        det=detect(b)
        if det is None: i+=1; continue
        sig,swept=det
        if swept=="high" and b.h<=ph: i+=1; continue   # need a genuine high sweep
        if swept=="low"  and b.l>=pl: i+=1; continue    # need a genuine low sweep
        # Entry bar: signal bar by default; if CONFIRM, the NEXT bar must follow
        # through in the TRADE direction and we enter at ITS close (no look-ahead
        # — entry is priced on the same bar whose close we tested).
        ebar=i
        if CONFIRM:
            if i+1>=n: break
            nb=rth[i+1]
            if sig=="S" and nb.c>=b.c: i+=1; continue
            if sig=="L" and nb.c<=b.c: i+=1; continue
            ebar=i+1
        ep=rth[ebar].c
        atrv=aidx.get(b.idx,0.0)
        # ── stop / target ─────────────────────────────────────────────────
        if MODE=="fade":
            if sig=="S": sp=b.h+STOP_BUF_T*B.TICK
            else:        sp=b.l-STOP_BUF_T*B.TICK
        else:  # cont: invalidate at the opposite end of the signal bar, ATR-capped
            cap=atrv*CONT_STOP_CAP_ATR
            if sig=="L": sp=max(b.l-STOP_BUF_T*B.TICK, ep-cap) if cap>0 else b.l-STOP_BUF_T*B.TICK
            else:        sp=min(b.h+STOP_BUF_T*B.TICK, ep+cap) if cap>0 else b.h+STOP_BUF_T*B.TICK
        if sig=="S":
            risk=sp-ep; tp=ep-R_TARGET*risk
        else:
            risk=ep-sp; tp=ep+R_TARGET*risk
        if risk<=0: i+=1; continue
        # walk forward within the same session to resolve
        exit_px=None; j=ebar+1
        while j<n and rth[j].date_tag==b.date_tag and rth[j].hhmm<B.FLATTEN_HHMM:
            x=rth[j]
            if sig=="S":
                if x.h>=sp: exit_px=sp; break
                if x.l<=tp: exit_px=tp; break
            else:
                if x.l<=sp: exit_px=sp; break
                if x.h>=tp: exit_px=tp; break
            j+=1
        if exit_px is None:
            exit_px=rth[min(j,n-1)].c            # flat at session end
        pnl=((ep-exit_px) if sig=="S" else (exit_px-ep))*B.PT_VAL
        pnl-=COST_TKS*B.TICK*B.PT_VAL            # entry slippage + commission
        r  = pnl/(risk*B.PT_VAL)
        trades.append((b.dt, sig, ep, exit_px, pnl, r))
        i=j+1                                    # no overlap; next after exit
    return trades


def summarize(tag, trades):
    if not trades:
        print(f"  {tag:6s}  no signals"); return dict(n=0,net=0,pf=0,wr=0,exp=0)
    pnl=[t[4] for t in trades]; w=[p for p in pnl if p>0]; lo=[p for p in pnl if p<=0]
    net=sum(pnl); pf=sum(w)/abs(sum(lo)) if lo else 9.99
    wr=100*len(w)/len(pnl); exp=net/len(pnl); avgR=sum(t[5] for t in trades)/len(trades)
    s=sum(1 for t in trades if t[1]=="S"); l=len(trades)-s
    print(f"  {tag:6s}  n={len(pnl):>3}  (S={s} L={l})  WR={wr:>4.0f}%  "
          f"PF={pf:>4.2f}  exp=${exp:>+6.0f}/tr  avgR={avgR:>+4.2f}  net=${net:>+8,.0f}")
    return dict(n=len(pnl),net=net,pf=pf,wr=wr,exp=exp)


def main():
    tags=sys.argv[1:] or list(CONTRACTS)
    print(f"Absorption scan [MODE={MODE}] — SWEEP_LB={SWEEP_LB} TOP_FRAC={TOP_FRAC} "
          f"REJECT={REJECT_FRAC} HOLD={HOLD_FRAC} ABS_MIN={ABS_MIN} RATIO={ABS_RATIO} "
          f"CONFIRM={CONFIRM} R={R_TARGET} cost={COST_TKS}t")
    res={}
    for tag in tags:
        scid=CONTRACTS[tag]
        if not scid.exists(): print(f"  missing {scid}"); continue
        res[tag]=summarize(tag, run(tag, scid))
    print("-"*78)
    nets=[r["net"] for r in res.values() if r["n"]>0]
    if nets:
        pos=sum(n>0 for n in nets)
        print(f"  Cross-contract: net>0 on {pos}/{len(nets)}   pooled=${sum(nets):+,.0f}   "
              + ("EDGE-CANDIDATE" if pos==len(nets) else "MIXED/NO-EDGE"))


if __name__=="__main__":
    main()
