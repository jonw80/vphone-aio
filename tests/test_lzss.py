#!/usr/bin/env python3
"""
LZSS from tools/mkwad.py, unpacked by the game's own GPU decoders.

The 68000 never unpacks a lump itself: W_ReadLump starts decomp.gas on the
GPU, and texture/flat/sprite pixels go through R_decode in r_phase5.gas,
which also turns each byte into a 16-bit CRY pixel through _vgatojag.
Both are run here on tools/jrisc.py against data packed by mkwad.py:
edge cases (empty, one byte, long runs that overlap their own output,
matches reaching back the full 4 KB window) and random data.
"""
import os
import random
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from asmtest import assemble              # noqa: E402
from jrisc import JRISC                   # noqa: E402
from mkwad import lzss_encode, lzss_decode  # noqa: E402

GPU_LOAD, RET = 0xF03100, 0xF03000
SRC, DST, FP = 0x100000, 0x200000, 0xF03E00
SYM = {"_decomp_input": 0x0F0000, "_decomp_output": 0x0F0004}

DECOMP = assemble("decomp.gas", "_decomp_start", SYM)
PHASE5 = assemble("r_phase5.gas", "_ref5_start", SYM)       # carries its own _vgatojag


def samples():
    rng = random.Random(7)
    window = bytes(rng.randrange(256) for _ in range(4096))
    yield "empty", b""
    yield "one byte", b"\x5a"
    yield "run", b"\x11" * 5000
    yield "pattern", b"abc" * 3000
    yield "full window", window + window[:16] + bytes(rng.randrange(256) for _ in range(40)) + window
    yield "random", bytes(rng.randrange(256) for _ in range(6000))
    yield "low entropy", bytes(rng.choice(b"\0\1\2\3") for _ in range(9000))


def run_decomp(packed, n):
    mem = bytearray(1 << 24)
    mem[SRC:SRC + len(packed)] = packed
    mem[SYM["_decomp_input"]:SYM["_decomp_input"] + 4] = SRC.to_bytes(4, "big")
    mem[SYM["_decomp_output"]:SYM["_decomp_output"] + 4] = DST.to_bytes(4, "big")
    image, syms, _ = DECOMP
    gpu = JRISC(dsp=False, mem=mem)
    gpu.load_image(GPU_LOAD, image)
    gpu.r[28] = RET
    gpu.run(GPU_LOAD, RET)                     # the overlay runs from the start of GPU RAM
    return bytes(mem[DST:DST + n])


def run_rdecode(packed, n):
    mem = bytearray(1 << 24)
    mem[SRC:SRC + len(packed)] = packed
    image, syms, _ = PHASE5
    gpu = JRISC(dsp=False, mem=mem)
    gpu.load_image(GPU_LOAD, image)
    gpu.r[14] = FP
    mem[FP:FP + 8] = struct.pack(">II", SRC, DST)
    gpu.r[28] = RET
    gpu.run(syms["_R_decode"], RET)
    return [int.from_bytes(mem[DST + 2 * i:DST + 2 * i + 2], "big") for i in range(n)]


def cry_table():
    image, syms, _ = PHASE5
    o = syms["_vgatojag"] - GPU_LOAD
    return [int.from_bytes(image[o + 4 * i:o + 4 * i + 4], "big") & 0xFFFF for i in range(256)]


CRY = cry_table()


def main():
    bad = 0
    for name, data in samples():
        packed = lzss_encode(data)
        ok_py = lzss_decode(packed) == data
        ok_gpu = run_decomp(packed, len(data)) == data
        ok_r = run_rdecode(packed, len(data)) == [CRY[b] for b in data]
        ok = ok_py and ok_gpu and ok_r
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name:12s} {len(data):5d} -> {len(packed):5d} bytes"
              f"  (C port {ok_py}, decomp.gas {ok_gpu}, R_decode {ok_r})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
