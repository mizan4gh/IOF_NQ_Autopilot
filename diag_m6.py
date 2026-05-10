#!/usr/bin/env python3
"""Diagnostic: trace every gate for M6 candidate bars."""
import sys, os
sys.path.insert(0, r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final")

import backtest as bt

TARGET_BARS = {2759, 4964}

_orig_step = bt.Backtester._step

def _patched_step(self, i: int):
    if i not in TARGET_BARS:
        return _orig_step(self, i)

    bars = self.bars
    bar  = bars[i]
    atr  = self.atr_v[i] if i < len(self.atr_v) else 0.0
    vwap = self.vwap_v[i] if i < len(self.vwap_v) else 0.0

    imb = bt.imbalance_state(bars, i)
    bal = bt.balance_state(bars, i, atr)

    sc_l = sum(1 for j in range(max(0, i-4), i+1) if bars[j].ask_vol > bars[j].bid_vol)
    sc_s = sum(1 for j in range(max(0, i-4), i+1) if bars[j].bid_vol > bars[j].ask_vol)

    bull = imb.direction == +1 if imb.active else True
    bear = imb.direction == -1 if imb.active else True

    bk_vs = 10
    bk_verify = 0; break_dir = 0
    if bal.mature and atr > 0:
        bk_t = atr * 0.30
        if bar.close > bal.hi + bk_t and imb.active and imb.direction == +1 and bull and sc_l >= bt.C_MIN_SC_ALL:
            break_dir = +1
        if bar.close < bal.lo - bk_t and imb.active and imb.direction == -1 and bear and sc_s >= bt.C_MIN_SC_ALL:
            break_dir = -1
        if break_dir != 0:
            d_streak = sum(1 for k in range(5) if i-k >= 0 and (
                (break_dir > 0 and bars[i-k].ask_vol > bars[i-k].bid_vol) or
                (break_dir < 0 and bars[i-k].bid_vol > bars[i-k].ask_vol)))
            if d_streak >= 3: bk_verify += 2
            bk3 = sum(bars[i-k].volume for k in range(3) if i-k >= 0) / 3
            if bk3 > bal.avg_vol * 1.3: bk_verify += 2
            no_div = True  # simplified
            if no_div: bk_verify += 1
            bk_verify += 1  # iceberg
            wick = (bar.high - bar.close) if break_dir > 0 else (bar.close - bar.low)
            if wick < atr * 0.3: bk_verify += 1
            bk_vs = bk_verify

    m6l = break_dir == +1 and bk_verify >= 5
    m6s = break_dir == -1 and bk_verify >= 5

    print(f"\n{'='*60}")
    print(f"BAR i={i}  hhmm={bar.hhmm}  close={bar.close:.2f}  atr={atr:.2f}")
    print(f"  bal.mature={bal.mature}  bal.hi={getattr(bal,'hi',0):.2f}  bal.lo={getattr(bal,'lo',0):.2f}")
    print(f"  imb.active={imb.active}  imb.direction={imb.direction}  imb.strength={getattr(imb,'strength',0)}")
    print(f"  sc_l={sc_l}  sc_s={sc_s}  C_MIN_SC_ALL={bt.C_MIN_SC_ALL}")
    print(f"  break_dir={break_dir}  bk_verify={bk_verify}")
    print(f"  m6l={m6l}  m6s={m6s}")

    if m6l or m6s:
        sl = m6l
        sel = 5
        tr, cr = self._regime(i, atr, vwap)
        ts = self._trend_str(i, atr)
        regime_ok = self._regime_ok(sel, sl, tr, cr, ts)
        rm = self.risk.rm
        lsb = self.last_stop_bar; lsd = self.last_stop_dir
        cooldown_blocked = (lsb >= 0 and i <= lsb + bt.C_COOL_STOP and
                            ((sl and lsd > 0) or (not sl and lsd < 0)))
        final_sc = sc_l if sl else sc_s
        q = bt.qual100(sel, final_sc, 0, False)

        print(f"  -- Gate results --")
        print(f"  in_pos={self.in_pos}  day_done={self.day_done}")
        print(f"  regime: tr={tr}  cr={cr}  ts={ts:.3f}  regime_ok={regime_ok}")
        print(f"  rm={rm:.3f} (>= {bt.C_RM_FLOOR} needed: {rm >= bt.C_RM_FLOOR})")
        print(f"  cooldown: last_stop_bar={lsb}  last_stop_dir={lsd}  blocked={cooldown_blocked}")
        print(f"  quality: final_sc={final_sc}  q={q}  QUAL_FLOOR={bt.QUAL_FLOOR}  q_ok={q >= bt.QUAL_FLOOR}")

        gates = {
            "in_pos": self.in_pos,
            "day_done": self.day_done,
            "regime_fail": not regime_ok,
            "rm_floor": rm < bt.C_RM_FLOOR,
            "cooldown": cooldown_blocked,
            "qual_floor": q < bt.QUAL_FLOOR,
        }
        blocked_by = [k for k, v in gates.items() if v]
        if blocked_by:
            print(f"  !! BLOCKED BY: {blocked_by}")
        else:
            print(f"  !! All gates pass — should fire! (check consec_loss/sess_dd gates in _step)")

    return _orig_step(self, i)

bt.Backtester._step = _patched_step

# Load bars the same way main() does
print("Loading SCID data ...")
scid = r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final\NQZ25-CME.scid"
recs = bt.read_scid(scid)
bars = bt.build_volume_bars(recs)
print(f"  {len(bars):,} bars built")

print(f"Running backtest with M6 gate diagnostic for bars {TARGET_BARS} ...")
eng    = bt.Backtester(bars)
trades = eng.run()
print(f"\nDone. Records: {len(trades)}  Completed trades: {sum(1 for t in trades if t.event=='EXIT')}")
print(f"M6 trades: {sum(1 for t in trades if t.mode == 6 and t.event == 'EXIT')}")
