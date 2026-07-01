"""Does ChopReg=1 predict M8 fade losses? Segment M8 fade outcomes by
ChopReg-at-entry across NQZ25/NQM5/NQH6 under prod config (M8_FADE_FULL ON).

Pairs each ENTRY row (carries FadeType + ChopReg at entry) with its following
EXIT row (carries realized pnl via TotalPnL delta). Buckets M8 trades by
ChopReg and by FadeType. Flags type-2 (Imb-extreme harness bug => magnitude
unreliable; sign/winrate still informative).
"""
import csv, sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
SC_DATA = Path(r"C:\SierraChart\Data")
CONTRACTS = {
    "NQZ25": BASE / "NQZ25-CME.scid",
    "NQM5":  SC_DATA / "F.US.ENQM25.scid",  # Jun-2025 (data restored under Sierra continuous name)
    "NQH6":  SC_DATA / "F.US.ENQH26.scid",  # Mar-2026 (1.23GB, == old genuine NQH6.CME.scid)
}
_PROD = dict(
    NEWS_FILTER=1, C_OPEN_COOL=36, TARGET_VOL=3000, SCALE_OUT=False,
    MAX_TRADES=6, DAILY_LOSS=800.0, DAILY_PROF=0.0,
    EARLY_SCRATCH=True, ES_AT_BAR=3, ES_MFE_FRAC=0.25,
    QUAL_FLOOR=50, M8_FADE_FULL=True, DISABLE_MODES=set(), QUAL_FLOOR_M8=None,
)

def run(scid, out):
    if "backtest" in sys.modules:
        del sys.modules["backtest"]
    import backtest
    for k, v in _PROD.items():
        setattr(backtest, k, v)
    sys.argv = ["backtest.py", str(scid), str(out)]
    backtest.main()

def m8_trades(csv_path):
    """Return list of dicts: {chop, ftype, pnl, reason} for M8 trades."""
    rows = list(csv.DictReader(open(csv_path, newline="")))
    trades, prev_tot, pending = [], 0.0, None
    for row in rows:
        ev = row["Event"]
        if ev == "SETUP":
            pending = dict(chop=int(float(row["ChopReg"])),
                           ftype=int(float(row["FadeType"])),
                           mode=row["Mode"])
        elif ev == "EXIT":
            tot = float(row["TotalPnL"]); pnl = tot - prev_tot; prev_tot = tot
            if pending is not None:
                pending.update(pnl=pnl, reason=row["ExitReason"])
                trades.append(pending); pending = None
    return [t for t in trades if t["mode"] == "M8"]

def stat(ts):
    n = len(ts)
    if n == 0: return "  n=0"
    net = sum(t["pnl"] for t in ts)
    w = [t for t in ts if t["pnl"] > 0]
    wr = 100*len(w)/n
    avg = net/n
    return f"  n={n:>3}  WR={wr:5.1f}%  net={net:>+9,.0f}  avg/trade={avg:>+7,.0f}"

def main():
    tags = list(CONTRACTS) if len(sys.argv) <= 1 else [sys.argv[1]]
    allt = []
    for tag in tags:
        scid = CONTRACTS[tag]
        if not scid.exists():
            print(f"missing {scid}"); continue
        out = BASE / f"IOF_NQ_m8chop_{tag}.csv"
        run(scid, out)
        ts = m8_trades(out)
        for t in ts: t["contract"] = tag
        allt += ts
        dist = defaultdict(int)
        for t in ts: dist[t['chop']] += 1
        print(f"\n===== {tag}: {len(ts)} M8 fade trades =====")
        print(f"  ChopReg distribution among M8 trades: {dict(sorted(dist.items()))}")
        for ch in sorted(dist):
            print(f"  ChopReg={ch} {stat([t for t in ts if t['chop']==ch])}")

    print("\n" + "="*64)
    print(" POOLED across contracts")
    print("="*64)
    print(f"  ChopReg=0 {stat([t for t in allt if t['chop']==0])}")
    print(f"  ChopReg=1 {stat([t for t in allt if t['chop']==1])}")
    print("\n  By FadeType x ChopReg (type-2 magnitude unreliable - Imb bug):")
    for ft in sorted(set(t['ftype'] for t in allt)):
        for ch in (0, 1):
            sub = [t for t in allt if t['ftype']==ft and t['chop']==ch]
            if sub:
                flag = " <-- type2 mag suspect" if ft == 2 else ""
                print(f"  type{ft} chop{ch}{stat(sub)}{flag}")

if __name__ == "__main__":
    main()
