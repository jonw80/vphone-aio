#!/usr/bin/env python3
"""
demobench.py - speed on identical views: the attract loop's demos.

    tools/emu/demobench.py doom.j64 [low|high|ultra] [--elf build/doom.elf] [--frames N]

A demo is a fixed list of game tics, each four vblanks of game time and
each drawn once, so every build draws the same views in the same order and
only the time it takes differs.  Plays the attract loop at the Detail
setting given and prints, for each demo: the vblanks it took, frames per
second, and the average time of each GPU refresh phase (lastphasetime[]).
--frames stops after that many frames of the first demo.  Like profile.py,
run it with VJ_OPTS set for the emulator's timing models.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from drive import Emu, symbols          # noqa: E402
from detail import MODES                # noqa: E402
from profile import SAMPLERATE, PHASES  # noqa: E402


def main():
    args, opts = [], {}
    it = iter(sys.argv[1:])
    for a in it:
        if a.startswith("--"):
            opts[a[2:]] = next(it)
        else:
            args.append(a)
    rom = args[0]
    mode = MODES[args[1] if len(args) > 1 else "low"]
    maxframes = int(opts.get("frames", 1 << 30))
    e = Emu(rom)
    if "elf" in opts:
        e.syms = symbols(opts["elf"])
    s = e.syms
    res, was, cur, last = [], 0, None, None
    for f in range(20000):
        if f < 400:                     # until the first demo starts
            e.poke(s["_detailsetting"], mode.to_bytes(4, "big"))
        e.run(1)
        dp = e.sym32("_demoplayback")
        if dp and not was:
            cur = {"t0": e.sym32("_ticcount"), "f0": e.sym32("_framecount"), "ph": [0] * 8, "n": 0}
        if dp:
            p = struct.unpack(">9i", e.peek(s["_lastphasetime"], 36))
            if last is None or p[0] != last[0]:
                if all(p[k] >= p[k - 1] for k in range(1, 9)):
                    for k in range(8):
                        cur["ph"][k] += p[k + 1] - p[k]
                    cur["n"] += 1
                last = p
            if cur["n"] >= maxframes:
                dp = 0
        if was and not dp:
            cur["t1"], cur["f1"] = e.sym32("_ticcount"), e.sym32("_framecount")
            res.append(cur)
            if len(res) == 2 or maxframes < (1 << 30):
                break
        was = dp
    e.close()
    for i, r in enumerate(res):
        vbl, frames = r["t1"] - r["t0"], r["f1"] - r["f0"]
        print(f"demo {i + 1}: {frames} frames in {vbl} vblanks, {60 * frames / vbl:.2f} frames/s")
        if r["n"]:
            ms = [1000 * v / r["n"] / SAMPLERATE for v in r["ph"]]
            print("  GPU ms per frame: " + ", ".join(f"{n} {m:.1f}" for n, m in zip(PHASES, ms))
                  + f"; phases 1-8 {sum(ms):.1f}")


if __name__ == "__main__":
    main()
