#!/usr/bin/env python3
"""
profile.py - where a frame's time goes, from the game's own timers.

    tools/emu/profile.py doom.j64 MAP [low|high|ultra] [seconds]

Starts the level at that Detail setting, turns on the spot, and every
frame reads what the game already measures in DSP sample counts
(samplecount, the sound output clock): phasetime[0..8], stamped as each
GPU refresh phase begins and ends (R_Setup keeps the last frame's in
lastphasetime[]), and P_Ticker's playertics,
thinkertics, sighttics, basetics, latetics and tictics (tictics includes
the wait for the refresh to latch its data).  Prints the average of each,
in milliseconds.  Run it with VJ_OPTS set for the emulator's timing
models, or the GPU and 68000 are nearly free.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from drive import Emu                   # noqa: E402
from detail import start_map, MODES     # noqa: E402

# the DSP's I2S word clock, which samplecount counts: SCLK = 19 (jagonly.c),
# 26590906 Hz / (64 * (SCLK + 1))
SAMPLERATE = 26590906 / (64 * 20)

PHASES = ["BSP", "wall prep", "sprite prep", "late prep", "cache",
          "walls", "planes", "sprites"]
TICS = ["playertics", "thinkertics", "sighttics", "basetics", "latetics", "tictics"]


def main():
    rom, m = sys.argv[1], int(sys.argv[2])
    mode = sys.argv[3] if len(sys.argv) > 3 else "low"
    seconds = int(sys.argv[4]) if len(sys.argv) > 4 else 10
    e = Emu(rom)
    start_map(e, m, MODES[mode])
    s = e.syms
    def stamp():
        return struct.unpack(">9i", e.peek(s["_lastphasetime"], 36))
    e.run(60, "L")                  # past the first frames, which include the level's setup
    last, frames = None, []
    f0 = e.frame
    while e.frame - f0 < seconds * 60:
        e.run(1, "L")
        p = stamp()
        if last is None or p[0] != last[0]:
            if last is not None and all(p[k] >= p[k - 1] for k in range(1, 9)):
                tics = [e.sym32("_" + t) for t in TICS]
                frames.append((p, tics, p[0] - last[0]))
            last = p
    e.close()
    rate = SAMPLERATE
    ms = lambda v: 1000 * v / rate
    n = len(frames)
    print(f"MAP{m:02d} {mode}: {n} frames in {seconds} s ({n / seconds:.1f}/s)")
    print("  frame interval  %6.1f ms" % ms(sum(f[2] for f in frames) / n))
    gpu = 0
    for k, name in enumerate(PHASES, 1):
        d = sum(f[0][k] - f[0][k - 1] for f in frames) / n
        gpu += d
        print("  GPU %-12s %6.1f ms" % (name, ms(d)))
    print("  GPU phases 1-8  %6.1f ms" % ms(gpu))
    for k, name in enumerate(TICS):
        print("  68k %-12s %6.1f ms" % (name, ms(sum(f[1][k] for f in frames) / n)))
    work = sum(sum(f[1][:5]) for f in frames) / n
    print("  68k tic work    %6.1f ms (tictics less the wait for the latch)" % ms(work))


if __name__ == "__main__":
    main()
