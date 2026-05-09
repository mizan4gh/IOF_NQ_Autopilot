import struct, numpy as np
from datetime import datetime, timedelta

path = "NQZ25-CME.scid"

with open(path, "rb") as f:
    hdr = f.read(56)
    raw40 = f.read(40 * 5)    # 5 records if 40-byte
    f.seek(56)
    raw24 = f.read(24 * 5)    # 5 records if 24-byte

magic, hdr_sz, rec_sz, ver, unused, utc_idx = struct.unpack_from("<4sIIHHI", hdr)
print(f"Magic:      {magic}")
print(f"HeaderSize: {hdr_sz}")
print(f"RecordSize: {rec_sz}")
print(f"Version:    {ver}")
print(f"UTCStartIdx:{utc_idx}")
print()

SC_EPOCH = datetime(1899, 12, 30)

# Try 40-byte interpretation
print("=== 40-byte records ===")
dtype40 = np.dtype([("dt","<f8"),("open","<f4"),("high","<f4"),("low","<f4"),("close","<f4"),
                    ("n_trades","<u4"),("tot_vol","<u4"),("bid_vol","<u4"),("ask_vol","<u4")])
r40 = np.frombuffer(raw40, dtype=dtype40)
for r in r40:
    dt = SC_EPOCH + timedelta(days=float(r["dt"]))
    print(f"  {dt}  o={float(r['open']):.2f}  v={int(r['tot_vol'])}")

# Try 24-byte interpretation (no volume)
print()
print("=== 24-byte records (no volume cols) ===")
dtype24 = np.dtype([("dt","<f8"),("open","<f4"),("high","<f4"),("low","<f4"),("close","<f4"),
                    ("n_trades","<u4")])
r24 = np.frombuffer(raw24, dtype=dtype24)
for r in r24:
    dt = SC_EPOCH + timedelta(days=float(r["dt"]))
    print(f"  {dt}  o={float(r['open']):.2f}")

# Print first 80 raw bytes as hex
print()
print("=== First 80 bytes after header (hex) ===")
with open(path, "rb") as f:
    f.seek(56)
    raw_hex = f.read(80)
for i in range(0, len(raw_hex), 8):
    chunk = raw_hex[i:i+8]
    hexs = " ".join(f"{b:02x}" for b in chunk)
    print(f"  [{56+i:4d}] {hexs}")
