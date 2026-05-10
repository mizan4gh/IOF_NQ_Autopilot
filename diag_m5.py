#!/usr/bin/env python3
"""Diagnostic: M5 signal breakdown — frequency, gates, trap phases."""
import sys, collections
sys.path.insert(0, r"C:\Users\17034\MyFolder\IOF_NQ_Production_Final")
import backtest as bt

# ── Patch _step to collect M5 stats ──────────────────────────────────────────
m5_fires   = []   # (i, hhmm, date_tag, sc_l, sc_s, ctrl, cool, delta_edge, trap_dir, fired)
m5_daily   = collections.defaultdict(int)
trap_phase_hist = collections.Counter()

_orig_step = bt.Backtester._step

def _patched_step(self, i):
    bars = self.bars
    bar  = bars[i]
    atr  = self.atr_v[i] if i < len(self.atr_v) else 0.0

    trap_phase_hist[self.trap.phase] += 1

    if self.trap.valid and self.trap.phase == 3 and bar.hhmm >= bt.RTH_OPEN and bar.hhmm < bt.FLATTEN_HHMM:
        lb_delta = self.cum_d[i] - self.cum_d[max(0, i - 15)]
        avg_d    = self.avg_d[i]
        cool     = self.last_m5_bar < 0 or i > self.last_m5_bar + bt.C_M5_COOL
        delta_edge = abs(lb_delta) >= avg_d * 3.0

        imb  = bt.imbalance_state(bars, i)
        bull = imb.direction == +1 if imb.active else True
        bear = imb.direction == -1 if imb.active else True
        sc_l = sum(1 for j in range(max(0, i-4), i+1) if bars[j].ask_vol > bars[j].bid_vol)
        sc_s = sum(1 for j in range(max(0, i-4), i+1) if bars[j].bid_vol > bars[j].ask_vol)
        dm   = i >= bt.C_DELTA_MAT
        ctrl = bt.control_score(bars, i, self.vp, dm)

        m5l = m5s = False
        if cool and delta_edge:
            if self.trap.direction == +1 and sc_s >= bt.C_M5_MIN_SC and ctrl <= 2:
                m5s = True
            if self.trap.direction == -1 and sc_l >= bt.C_M5_MIN_SC and ctrl >= -2:
                m5l = True

        m5_fires.append({
            'i': i, 'hhmm': bar.hhmm, 'date': bar.date_tag,
            'trap_dir': self.trap.phase,
            'cool': cool, 'delta_edge': delta_edge,
            'sc_l': sc_l, 'sc_s': sc_s, 'ctrl': ctrl,
            'fired': m5l or m5s,
            'blocked_score': not (
                (self.trap.direction == +1 and sc_s >= bt.C_M5_MIN_SC) or
                (self.trap.direction == -1 and sc_l >= bt.C_M5_MIN_SC)
            ),
        })
        if (m5l or m5s):
            m5_daily[bar.date_tag] += 1

    return _orig_step(self, i)

bt.Backtester._step = _patched_step

print("Loading NQZ25 data ...")
recs = bt.read_scid(r"C:\SierraChart\Data\NQZ5.CME.scid")
bars = bt.build_volume_bars(recs, price_scale=100.0)
print(f"  {len(bars):,} bars")

print("Running backtest ...")
eng    = bt.Backtester(bars)
trades = eng.run()

exits = [t for t in trades if t.event == 'EXIT']
m5_exits = [t for t in exits if t.mode == 5]

print(f"\nTotal trades: {len(exits)}  M5 trades: {len(m5_exits)}")
print(f"\n--- M5 per day (top 10 highest) ---")
for dt, cnt in sorted(m5_daily.items(), key=lambda x: -x[1])[:10]:
    print(f"  {dt}: {cnt}")

print(f"\n--- M5 gate breakdown (phase==3 bars seen: {len(m5_fires)}) ---")
fired       = sum(1 for x in m5_fires if x['fired'])
not_cool    = sum(1 for x in m5_fires if not x['cool'])
no_edge     = sum(1 for x in m5_fires if not x['delta_edge'])
score_block = sum(1 for x in m5_fires if x['cool'] and x['delta_edge'] and x['blocked_score'])
print(f"  Phase==3 seen: {len(m5_fires)}")
print(f"  Fired (m5l|m5s set): {fired}")
print(f"  Blocked by cooldown: {not_cool}")
print(f"  Blocked by delta_edge: {sum(1 for x in m5_fires if x['cool'] and not x['delta_edge'])}")
print(f"  Blocked by score: {score_block}")

sc_vals = [x['sc_s'] if x['trap_dir'] == 1 else x['sc_l'] for x in m5_fires if x['cool'] and x['delta_edge']]
if sc_vals:
    from collections import Counter
    sc_dist = Counter(sc_vals)
    print(f"\n  Score distribution (cool+edge bars): {dict(sorted(sc_dist.items()))}")
    print(f"  C_M5_MIN_SC = {bt.C_M5_MIN_SC}  => need score>={bt.C_M5_MIN_SC}")

print(f"\n--- M5 trap phase distribution (all bars) ---")
for ph in sorted(trap_phase_hist):
    print(f"  phase {ph}: {trap_phase_hist[ph]:,} bars")
