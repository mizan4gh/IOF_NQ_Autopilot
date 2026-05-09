import struct, numpy as np
from datetime import datetime, timedelta

path = "NQZ25-CME.scid"
SCID_HDR_SIZE = 56

with open(path, "rb") as f:
    f.seek(SCID_HDR_SIZE)
    raw = f.read(40 * 5)

print("=== Raw hex of first 5 records (40 bytes each) ===")
for i in range(5):
    rec = raw[i*40:(i+1)*40]
    hex_str = " ".join(f"{b:02x}" for b in rec)
    print(f"rec[{i}]: {hex_str}")

print()
print("=== Interpret dt field as different types ===")
SC_EPOCH = datetime(1899, 12, 30)
UNIX_EPOCH = datetime(1970, 1, 1)

for i in range(3):
    rec = raw[i*40:(i+1)*40]
    dt_bytes = rec[:8]

    as_float64_le = struct.unpack_from("<d", dt_bytes)[0]
    as_uint64_le  = struct.unpack_from("<Q", dt_bytes)[0]
    as_int64_le   = struct.unpack_from("<q", dt_bytes)[0]

    print(f"\nrec[{i}] dt bytes: {' '.join(f'{b:02x}' for b in dt_bytes)}")
    print(f"  as float64 (OLE days): {as_float64_le:.6f}  =>  {SC_EPOCH + timedelta(days=as_float64_le) if as_float64_le > 0 else 'zero/neg'}")
    print(f"  as uint64  (unix ms):  {as_uint64_le}  =>  ", end="")
    try:
        print(UNIX_EPOCH + timedelta(milliseconds=as_uint64_le))
    except:
        print("overflow")
    print(f"  as int64   (unix us):  {as_int64_le}  =>  ", end="")
    try:
        print(UNIX_EPOCH + timedelta(microseconds=as_int64_le))
    except:
        print("overflow")

    # Also try as microseconds since SC_EPOCH
    try:
        print(f"  as int64   (SC us):    {as_int64_le}  =>  {SC_EPOCH + timedelta(microseconds=as_int64_le)}")
    except:
        print(f"  as int64   (SC us): overflow")

    # Nanoseconds since unix
    try:
        print(f"  as uint64  (unix ns):  {as_uint64_le}  =>  {UNIX_EPOCH + timedelta(microseconds=as_uint64_le//1000)}")
    except:
        print(f"  as uint64  (unix ns): overflow")

    # Print float fields
    open_  = struct.unpack_from("<f", rec[8:12])[0]
    high_  = struct.unpack_from("<f", rec[12:16])[0]
    low_   = struct.unpack_from("<f", rec[16:20])[0]
    close_ = struct.unpack_from("<f", rec[20:24])[0]
    vol_   = struct.unpack_from("<I", rec[32:36])[0]
    print(f"  OHLC: {open_:.2f} {high_:.2f} {low_:.2f} {close_:.2f}  vol={vol_}")
