#!/usr/bin/env python3
"""
drive.py - control Virtual Jaguar (tools/emu/run.c in control mode) from Python.

    from drive import Emu
    emu = Emu("doom.j64")
    emu.run(60)                     # one second, no buttons
    emu.run(4, "B")                 # Jaguar B for 4 frames
    emu.shot("title.png")
    x = emu.sym32("_ticcount")

Buttons are Jaguar names: U D L R A B C PAUSE OPTION (the core's default
map: RetroPad A/B/Y/Select/Start = Jaguar A/B/C/Pause/Option).  RAM
addresses come from build/doom.elf.
"""
import os
import subprocess
import struct

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CROSS = os.environ.get("CROSS", "m68k-linux-gnu-")
W = os.path.join(ROOT, "build", "emu")

PAD = {"B": 0, "C": 1, "PAUSE": 2, "OPTION": 3, "U": 4, "D": 5, "L": 6, "R": 7, "A": 8}


def symbols(elf=os.path.join(ROOT, "build", "doom.elf")):
    out = subprocess.run([CROSS + "nm", elf], capture_output=True, text=True).stdout
    syms = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 3:
            syms[p[2]] = int(p[0], 16)
    return syms


class Emu:
    def __init__(self, rom, core=None):
        core = core or os.path.join(W, "virtualjaguar-libretro", "virtualjaguar_libretro.so")
        os.makedirs(os.path.join(W, "sys"), exist_ok=True)
        self.p = subprocess.Popen([os.path.join(W, "run"), core, rom, os.path.join(W, "sys"), "0", "-"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.syms = symbols()
        self.frame = 0

    def _cmd(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        return self.p.stdout.readline().strip()

    def run(self, frames, buttons=""):
        mask = 0
        for b in (buttons.split() if isinstance(buttons, str) else buttons):
            mask |= 1 << PAD[b]
        r = self._cmd(f"run {frames} {mask}")
        assert r == "ok", r
        self.frame += frames

    def peek(self, addr, n):
        return bytes.fromhex(self._cmd(f"peek {addr} {n}"))

    def poke(self, addr, data):
        assert self._cmd(f"poke {addr} {bytes(data).hex()}") == "ok"

    def u32(self, addr):
        return struct.unpack(">I", self.peek(addr, 4))[0]

    def s32(self, addr):
        return struct.unpack(">i", self.peek(addr, 4))[0]

    def sym32(self, name):
        return self.s32(self.syms[name])

    def shot(self, path, scale=2):
        ppm = path + ".ppm"
        assert self._cmd(f"shot {ppm}") == "ok"
        from PIL import Image
        im = Image.open(ppm)
        im.resize((im.width * scale, im.height * scale), Image.NEAREST).save(path)
        os.remove(ppm)
        return path

    def hash(self, y0=0, y1=1 << 20):
        """(width, height, hash of rows y0..y1-1) of the last frame"""
        w, h, v = self._cmd(f"hash {y0} {y1}").split()
        return int(w), int(h), v

    def wav(self, path):
        assert self._cmd(f"wav {path}") == "ok"

    def close(self):
        try:
            self._cmd("quit")
        except Exception:
            pass
        self.p.wait(timeout=10)
