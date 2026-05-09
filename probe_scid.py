import numpy as np
from datetime import datetime, timedelta

SCID_HDR_SIZE = 56
SCID_REC_SIZE = 40
SC_EPOCH = datetime(1899, 12, 30)
SCID_DTYPE = np.dtype([
    ("dt","<f8"),("open","<f4"),("high","<f4"),("low","<f4"),("close","<f4"),
    ("n_trades","<u4"),("tot_vol","<u4"),("bid_vol","<u4"),("ask_vol","<u4")
])

path = "NQZ25-CME.scid"
with open(path, "rb") as f:
    f.seek(SCID_HDR_SIZE)
    raw = f.read(SCID_REC_SIZE * 100)

recs = np.frombuffer(raw, dtype=SCID_DTYPE)
print("=== First 20 records (raw timestamps) ===")
for r in recs[:20]:
    dt = SC_EPOCH + timedelta(days=float(r["dt"]))
    vol = int(r["tot_vol"])
    if vol > 0:
        print(dt, " o=", round(float(r["open"]),2), " v=", vol)

# Check mid-file for a known session time
n_total = 34284945
mid_offset = SCID_HDR_SIZE + (n_total // 2) * SCID_REC_SIZE
print()
print("=== Mid-file sample ===")
with open(path, "rb") as f:
    f.seek(mid_offset)
    raw2 = f.read(SCID_REC_SIZE * 20)
recs2 = np.frombuffer(raw2, dtype=SCID_DTYPE)
for r in recs2[:10]:
    dt = SC_EPOCH + timedelta(days=float(r["dt"]))
    print(dt, " v=", int(r["tot_vol"]))
