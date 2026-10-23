#!/usr/bin/env python3
"""
detail.py - the Detail setting (Low 160, High 320, Ultra 640 columns) in
the emulator.

    tools/emu/detail.py doom.j64 outdir [low|high|ultra ...] [--maps 1-24]

For each mode and map: start the map from the menu at that setting, turn on
the spot for five seconds, take a screenshot, and count the frames drawn
(framecount) against emulated time.  With --extras, also the automap, the
pause plaque and the Options menu, on the first map.  Prints one line per
map and writes outdir/detail.json.

The frame rates are Virtual Jaguar's, whose GPU and bus timing is close to
but not the same as the hardware's: read them as a comparison between the
modes, not as what a console does.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from drive import Emu       # noqa: E402

MODES = {"low": 0, "high": 1, "ultra": 2}
VBLS = 60                   # NTSC


def start_map(e, m, mode):
    e.run(150)              # title
    e.run(4, "B")
    e.run(40)
    e.poke(e.syms["_maxlevel"], (24).to_bytes(4, "big"))
    e.poke(e.syms["_playermap"], m.to_bytes(4, "big"))
    e.poke(e.syms["_detailsetting"], mode.to_bytes(4, "big"))
    e.run(20)
    e.run(4, "B")
    for _ in range(200):    # until the player is in the level
        mo = e.u32(e.syms["_players"])
        if mo and e.s32(mo + 12):
            break
        e.run(10)
    e.run(30)


def measure(e, seconds=5):
    f0 = e.sym32("_framecount")
    e.run(seconds * VBLS, "L")
    return (e.sym32("_framecount") - f0) / seconds


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rom, out = args[0], args[1]
    modes = args[2:] or list(MODES)
    maps = range(1, 25)
    for a in sys.argv[1:]:
        if a.startswith("--maps="):
            lo, _, hi = a[7:].partition("-")
            maps = range(int(lo), int(hi or lo) + 1)
    os.makedirs(out, exist_ok=True)
    results = {}
    for name in modes:
        mode = MODES[name]
        for m in maps:
            e = Emu(rom)
            start_map(e, m, mode)
            fps = measure(e)
            w = e.sym32("_screenwidth")
            e.shot(os.path.join(out, f"{name}-map{m:02d}.png"), scale=1)
            ok = e.sym32("_detailmode") == mode
            results[f"{name}/{m}"] = {"fps": fps, "screenwidth": w, "ok": ok}
            print(f"{name:5s} map {m:2d}: {w} columns, {fps:5.2f} frames/s{'' if ok else '  WRONG MODE'}", flush=True)
            if "--extras" in sys.argv and m == maps[0]:
                extras(e, out, name)
            e.close()
    json.dump(results, open(os.path.join(out, "detail.json"), "w"), indent=1)


def extras(e, out, name):
    # automap: the keypad's 9 toggles it (AM_Control); do what it does
    p = e.syms["_players"]
    flags = p + PL_AUTOMAPFLAGS
    mo = e.u32(p)
    e.poke(p + PL_AUTOMAPX, e.peek(mo + 12, 8))                # automapx, y = mo->x, y
    e.poke(flags, (e.u32(flags) | 1).to_bytes(4, "big"))      # AF_ACTIVE
    e.run(40)
    e.shot(os.path.join(out, f"{name}-automap.png"), scale=1)
    e.poke(flags, (e.u32(flags) & ~1).to_bytes(4, "big"))
    e.run(40)
    e.run(4, "PAUSE")
    e.run(40)
    e.shot(os.path.join(out, f"{name}-pause.png"), scale=1)
    e.run(4, "PAUSE")
    e.run(40)
    e.run(4, "OPTION")
    e.run(40)
    for _ in range(2):                                         # down to Detail
        e.run(4, "D")
        e.run(10)
    e.run(20)
    e.shot(os.path.join(out, f"{name}-options.png"), scale=1)
    e.run(4, "OPTION")
    e.run(40)


PL_AUTOMAPX = 268          # offsetof(player_t, automapx), automapy follows
PL_AUTOMAPFLAGS = 280      # offsetof(player_t, automapflags)

if __name__ == "__main__":
    main()
