#!/usr/bin/env python3
"""
budget.py - check that every GPU / DSP overlay fits in local RAM.

Reads the BSD a.out objects rmac produced (verify.sh passes the directory),
computes each overlay's payload as (codeend - <name>_start), adds the scratch
arrays that live past `codeend` but are only declared with one .dc.l, and
checks the end address against the overlay's limit.

Memory map (from the Jaguar Technical Reference Manual, rev 8):
  GPU local RAM  $F03000-$F03FFF  (4K)   gpubase at $F03000, overlays at $F03100,
                                         stack (r14/FP) grows down from $F04000
  DSP local RAM  $F1B000-$F1CFFF  (8K)   dspbase at $F1B000, overlays at $F1B140,
                                         stack grows down from $F1CE00
"""
import os
import struct
import sys

GPU_LOAD, GPU_STACK = 0xF03100, 0xF04000
DSP_LOAD, DSP_STACK = 0xF1B140, 0xF1CE00
GPU_RESERVE = 0x200            # GPU refresh phases nest several FP frames
DSP_RESERVE = 0x100            # shipped p_base leaves 280 bytes, so this is proven

# name: (load address, highest allowed end address, extra tail bytes)
OVERLAYS = {
    # GPU refresh phases and the LZSS decompressor
    "r_phase1": (GPU_LOAD, GPU_STACK - GPU_RESERVE, 127 * 4),  # solidsegs[64] of 2 longs
    "r_phase2": (GPU_LOAD, GPU_STACK - GPU_RESERVE, 0),
    "r_phase3": (GPU_LOAD, GPU_STACK - GPU_RESERVE, 0),
    "r_phase4": (GPU_LOAD, GPU_STACK - GPU_RESERVE, 0),
    "r_phase5": (GPU_LOAD, GPU_STACK - GPU_RESERVE, 0),
    "r_phase6": (GPU_LOAD, GPU_STACK - GPU_RESERVE, 159 * 4),  # clipbounds[160]
    "r_phase7": (GPU_LOAD, GPU_STACK - GPU_RESERVE, 179 * 4),  # spanstart[180]
    "r_phase8": (GPU_LOAD, GPU_STACK - GPU_RESERVE, 0),
    "r_phase9": (GPU_LOAD, GPU_STACK - GPU_RESERVE, 0),
    "decomp":   (GPU_LOAD, GPU_STACK - GPU_RESERVE, 0),
    # DSP jobs: game physics and sound
    "p_base":   (DSP_LOAD, DSP_STACK - DSP_RESERVE, 0),
    "p_move":   (DSP_LOAD, DSP_STACK - DSP_RESERVE, 0),
    "p_shoot":  (DSP_LOAD, DSP_STACK - DSP_RESERVE, 0),
    "p_sight":  (DSP_LOAD, DSP_STACK - DSP_RESERVE, 0),
    "p_slide":  (DSP_LOAD, DSP_STACK - DSP_RESERVE, 0),
    "music":    (DSP_LOAD, DSP_STACK - DSP_RESERVE, 0),
    "sfx":      (DSP_LOAD, 0xF1B800, 0),                 # _internalbuffer is at $F1B800
    "mixmusic": (DSP_LOAD, 0xF1C400, 0),                 # its sfx buffer is at $F1C400
}
# resident code: (base, limit)
RESIDENT = {
    "gpubase": (0xF03000, GPU_LOAD),
    "dspbase": (0xF1B000, DSP_LOAD),
}


def read_aout(path):
    d = open(path, "rb").read()
    _magic, text, data, _bss, ssz, _entry, trs, drs = struct.unpack(">8I", d[:32])
    so = 32 + text + data + trs + drs
    st = so + ssz
    syms = {}
    for i in range(so, st, 12):
        sx, _typ, val = struct.unpack(">IIi", d[i:i + 12])
        e = d.index(b"\0", st + sx)
        syms[d[st + sx:e].decode()] = val
    return text, syms


def main(objdir):
    ok = True
    for name, (load, limit, tail) in sorted(OVERLAYS.items()):
        path = os.path.join(objdir, name + ".o")
        if not os.path.exists(path):
            continue
        _text, syms = read_aout(path)
        start = [v for k, v in syms.items() if k.endswith("_start") and k.startswith("_")]
        payload = syms["codeend"] - min(start)
        end = load + payload + tail
        status = "ok  " if end <= limit else "OVER"
        ok &= end <= limit
        print(f"  {status} {name:9s} {payload:5d} bytes code{f' +{tail} tail' if tail else '':12s}"
              f" ends ${end:06X}  limit ${limit:06X}  free {limit - end:5d}")
    for name, (base, limit) in RESIDENT.items():
        path = os.path.join(objdir, name + ".o")
        if not os.path.exists(path):
            continue
        text, _ = read_aout(path)
        end = base + text
        status = "ok  " if end <= limit else "OVER"
        ok &= end <= limit
        print(f"  {status} {name:9s} {text:5d} bytes resident            ends ${end:06X}  limit ${limit:06X}  free {limit - end:5d}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1]) else 1)
