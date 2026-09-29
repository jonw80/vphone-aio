#!/usr/bin/env python3
"""
blitter.py - functional model of the Jaguar blitter, pixel mode, 16 bpp.

Written from the Technical Reference Manual rev 8, pp.64-82.  Covers what
Jaguar Doom's column and span drawers use: A1/A2 windows, pixel pointers
with fractions, inner-loop X/Y add modes (pixel, zero, increment), the
outer-loop step registers (UPDA1F/UPDA1/UPDA2), DSTA2, SRCEN, LFU "source",
and SRCSHADE intensity adjustment of CRY pixels.

TRM bug #11 ("Blitter Y Add Control Bits"): the A1 and A2 Y add bits are
not decoded separately.  `ybug` selects how that is modelled:
  "independent"  each generator uses its own bit (the documented intent)
  "a1drives"     A1's bit drives both generators, A2's bit is ignored
Code that follows the manual's work-around (set it on both, or on neither)
behaves the same under both.
"""

BASE = 0xF02200
A1_BASE, A1_FLAGS, A1_CLIP, A1_PIXEL, A1_STEP, A1_FSTEP, A1_FPIXEL, A1_INC, A1_FINC = (
    BASE + 4 * i for i in range(9))
A2_BASE, A2_FLAGS, A2_MASK, A2_PIXEL, A2_STEP = (BASE + 0x24 + 4 * i for i in range(5))
B_CMD, B_COUNT = BASE + 0x38, BASE + 0x3C
B_IINC = BASE + 0x70


def lo16s(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def hi16s(v):
    return lo16s(v >> 16)


def width_of(flags):
    w = (flags >> 9) & 0x3F
    e, m = w >> 2, w & 3
    return ((4 | m) << e) >> 2


class Blitter:
    def __init__(self, mem, ybug="independent"):
        self.mem = mem
        self.regs = {}
        self.ybug = ybug
        self.blits = 0
        self.pixels = 0

    def write(self, addr, value):
        self.regs[addr] = value & 0xFFFFFFFF
        if addr == B_CMD:
            self.run(value)

    def read(self, addr):
        if addr == B_CMD:
            return 1                         # always idle: blits complete instantly
        return self.regs.get(addr, 0)

    # -- pixel access ---------------------------------------------------------
    def addr(self, base, flags, x, y):
        assert (flags >> 3) & 7 == 4, "model only handles 16 bpp"
        assert 0 <= x < 32768 and 0 <= y < 4096, f"pointer out of range ({x},{y})"
        return base + 2 * (y * width_of(flags) + x)

    def rd16(self, a):
        return int.from_bytes(self.mem[a:a + 2], "big")

    def wr16(self, a, v):
        self.mem[a:a + 2] = (v & 0xFFFF).to_bytes(2, "big")

    # -- one blit ---------------------------------------------------------------
    def run(self, cmd):
        g = self.regs.get
        self.blits += 1
        f1, f2 = g(A1_FLAGS, 0), g(A2_FLAGS, 0)
        a1x, a1y = lo16s(g(A1_PIXEL, 0)), hi16s(g(A1_PIXEL, 0))
        a1fx, a1fy = g(A1_FPIXEL, 0) & 0xFFFF, g(A1_FPIXEL, 0) >> 16
        a2x, a2y = lo16s(g(A2_PIXEL, 0)), hi16s(g(A2_PIXEL, 0))
        inner = g(B_COUNT, 0) & 0xFFFF or 0x10000
        outer = g(B_COUNT, 0) >> 16 or 0x10000
        dsta2 = cmd >> 11 & 1
        srcen = cmd & 1
        assert (cmd >> 21) & 15 == 12, "model only handles LFU = source"
        assert not cmd & (1 << 16 | 1 << 17 | 1 << 12), "PATDSEL/ADDDSEL/GOURD not modelled"
        iinc = g(B_IINC, 0)
        shade = (iinc >> 16) & 0xFF
        shade = shade - 0x100 if shade & 0x80 else shade

        a1yadd_bit, a2yadd_bit = f1 >> 18 & 1, f2 >> 18 & 1
        if self.ybug == "a1drives":
            a2yadd_bit = a1yadd_bit

        def step_inner(x, y, fx, fy, flags, yadd, is_a1):
            xmode = flags >> 16 & 3
            if xmode == 3:
                assert is_a1, "add increment is A1 only"
                fx += g(A1_FINC, 0) & 0xFFFF
                fy += g(A1_FINC, 0) >> 16
                x += lo16s(g(A1_INC, 0)) + (fx >> 16)
                y += hi16s(g(A1_INC, 0)) + (fy >> 16)
                return x, y, fx & 0xFFFF, fy & 0xFFFF
            assert xmode != 0, "phrase mode not modelled"
            if xmode == 1:
                x += -1 if flags >> 19 & 1 else 1
            if yadd:
                y += -1 if flags >> 20 & 1 else 1
            return x, y, fx, fy

        for _ in range(outer):
            for _ in range(inner):
                if dsta2:
                    src = self.addr(g(A1_BASE, 0), f1, a1x, a1y)
                    dst = self.addr(g(A2_BASE, 0), f2, a2x, a2y)
                else:
                    src = self.addr(g(A2_BASE, 0), f2, a2x, a2y)
                    dst = self.addr(g(A1_BASE, 0), f1, a1x, a1y)
                v = self.rd16(src) if srcen else 0
                if cmd >> 30 & 1:           # SRCSHADE: adjust CRY intensity, saturating
                    i = max(0, min(255, (v & 0xFF) + shade))
                    v = (v & 0xFF00) | i
                self.wr16(dst, v)
                self.pixels += 1
                a1x, a1y, a1fx, a1fy = step_inner(a1x, a1y, a1fx, a1fy, f1, a1yadd_bit, True)
                a2x, a2y, _, _ = step_inner(a2x, a2y, 0, 0, f2, a2yadd_bit, False)
            if cmd >> 8 & 1:                # UPDA1F
                a1fx += g(A1_FSTEP, 0) & 0xFFFF
                a1fy += g(A1_FSTEP, 0) >> 16
            if cmd >> 9 & 1:                # UPDA1 (plus carries from the fraction)
                a1x += lo16s(g(A1_STEP, 0)) + (a1fx >> 16)
                a1y += hi16s(g(A1_STEP, 0)) + (a1fy >> 16)
            a1fx &= 0xFFFF
            a1fy &= 0xFFFF
            if cmd >> 10 & 1:               # UPDA2
                a2x += lo16s(g(A2_STEP, 0))
                a2y += hi16s(g(A2_STEP, 0))
