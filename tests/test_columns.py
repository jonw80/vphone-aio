#!/usr/bin/env python3
"""
Functional tests for the wall and sprite column drawers (r_phase6 /
r_phase8) running on the JRISC model with the blitter model.

1. FASTCOLUMNS=1 (one inner-loop blit per column) must draw exactly the
   same pixels as FASTCOLUMNS=0 (the shipped per-pixel outer-loop blits),
   for walls and sprites, under both readings of TRM bug #11.
2. Spectres (colormap -1) must draw a darkened copy of the screen from 0-2
   rows below each opaque pixel and touch nothing else.
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from asmtest import assemble          # noqa: E402
from blitter import Blitter, A2_BASE, A2_STEP, A1_FLAGS, A2_FLAGS   # noqa: E402
from jrisc import JRISC               # noqa: E402

GPU_LOAD, RET = 0xF03100, 0xF03000
FB, W, H = 0x080000, 160, 180
SYM = {"_workingscreen": 0x100000, "_framecount": 0x100004}


class GPU(JRISC):
    """JRISC GPU whose blitter registers are backed by the blitter model."""
    def __init__(self, mem, blitter):
        super().__init__(dsp=False, mem=mem)
        self.blt = blitter

    def rd(self, a, size):
        if 0xF02200 <= a < 0xF022A0:
            return self.blt.read(a)
        return super().rd(a, size)

    def wr(self, a, size, v):
        if 0xF02200 <= a < 0xF022A0:
            return self.blt.write(a, v)
        super().wr(a, size, v)


BUILDS = {}
for fast in (0, 1):
    for name, entry in (("r_phase6", "_R_DrawTexture"), ("r_phase8", "_R_DrawVisSprite")):
        BUILDS[fast, name] = assemble(name + ".gas", "_ref%s_start" % name[-1], SYM,
                                      overrides={"FASTCOLUMNS": fast})


def call(mem, ybug, fast, name, entry, regs):
    image, syms, _ = BUILDS[fast, name]
    blt = Blitter(mem, ybug)
    gpu = GPU(mem, blt)
    gpu.load_image(GPU_LOAD, image)
    # state set up elsewhere in the frame: R_Setup (68000) and the phase's entry
    blt.write(A2_BASE, FB)
    blt.write(A2_STEP, 0x10000)
    blt.write(A1_FLAGS, syms["COL_A1FLAGS"])
    blt.write(A2_FLAGS, syms["COL_A2FLAGS"])
    for k, v in regs.items():
        gpu.r[k] = v & 0xFFFFFFFF
    gpu.r[28] = RET
    gpu.run(syms[entry], RET)
    return blt


def w32(mem, a, v):
    mem[a:a + 4] = (v & 0xFFFFFFFF).to_bytes(4, "big")


def w16(mem, a, v):
    mem[a:a + 2] = (v & 0xFFFF).to_bytes(2, "big")


def screen(mem):
    return [int.from_bytes(mem[FB + 2 * i:FB + 2 * i + 2], "big") for i in range(W * H)]


def base_memory(rng):
    mem = bytearray(1 << 24)
    for i in range(W * H):
        w16(mem, FB + 2 * i, rng.randrange(0x10000))
    w32(mem, SYM["_workingscreen"], FB)
    return mem


# ---- walls -------------------------------------------------------------------
TEX, TEXP = 0x090000, 0x0A0000


def draw_walls(ybug, fast, seed=5):
    rng = random.Random(seed)
    mem = base_memory(rng)
    tw, th = 64, 128
    for i in range(tw * th):
        w16(mem, TEX + 2 * i, rng.randrange(0x10000))
    pixels = 0
    for x in range(W):
        scale = rng.choice([rng.randrange(0x0400, 0x7FFF), 0x7FFF, 0x2000])
        top_h = rng.randrange(-40, 300)
        bot_h = rng.randrange(-300, top_h + 1)
        for k, v in enumerate((TEX, tw, th, top_h, bot_h, rng.randrange(-100, 200) << 16)):
            w32(mem, TEXP + 4 * k, v)
        light = (-(rng.randrange(0, 256) << 14)) & 0xFFFFFF
        ceil = rng.randrange(-1, 80)
        floor = rng.randrange(max(ceil + 1, 90), 181)
        blt = call(mem, ybug, fast, "r_phase6", "_R_DrawTexture", {
            15: TEXP, 19: floor, 20: ceil, 21: x, 22: scale,
            23: 0x2000000 // scale, 24: rng.randrange(0, 4096), 25: light})
        pixels += blt.pixels
    return screen(mem), pixels


# ---- sprites -----------------------------------------------------------------
VIS, PATCH, PIX = 0x0A1000, 0x0A2000, 0x0A8000


def make_patch(mem, rng, width):
    """Jaguar patch: shorts width,height,leftoffset,topoffset, columnofs[],
    then per column a list of 4-byte posts (topdelta, length, pixel offset)
    ended by a post whose pixel offset has bit 15 set."""
    w16(mem, PATCH, width)
    p = PATCH + 8 + 2 * width
    pix = 0
    for c in range(width):
        w16(mem, PATCH + 8 + 2 * c, p - PATCH)
        y = rng.randrange(0, 6)
        while y < 60 and rng.random() < 0.8:
            length = rng.randrange(1, 14)
            mem[p], mem[p + 1] = y, length
            w16(mem, p + 2, pix)
            for _ in range(length):
                w16(mem, PIX + 2 * pix, rng.randrange(0x10000))
                pix += 1
            p += 4
            y += length + rng.randrange(1, 6)
        mem[p], mem[p + 1] = 0xFF, 0
        w16(mem, p + 2, 0xFFFF)
        p += 4


def draw_sprite(ybug, fast, colormap, seed, framecount=0):
    rng = random.Random(seed)
    mem = base_memory(rng)
    w32(mem, SYM["_framecount"], framecount)
    width = rng.randrange(20, 40)
    make_patch(mem, rng, width)
    yscale = rng.randrange(0x6000, 0x28000)
    x1 = rng.randrange(-5, 120)
    xiscale = rng.randrange(0x6000, 0x14000)
    x2 = min(159, x1 + (width * 0x10000) // xiscale - 1)
    x1c = max(0, x1)
    vis = [x1c, x2, (x1c - x1) * xiscale, 0, xiscale, yscale, (1 << 32) // yscale,
           rng.randrange(10, 90) << 16, PATCH, colormap, 0, 0, 0, 0, PIX]
    for k, v in enumerate(vis):
        w32(mem, VIS + 4 * k, v)
    image, syms, _ = BUILDS[fast, "r_phase8"]
    sprop = syms["_spropening"]
    stack = 0xF03F00
    w32(mem, stack, VIS)
    blt = Blitter(mem, ybug)
    gpu = GPU(mem, blt)
    gpu.load_image(GPU_LOAD, image)        # spropening[] lives inside the image
    for x in range(W):
        top, bottom = rng.choice([(0, 180), (rng.randrange(0, 60), rng.randrange(100, 181))])
        w32(mem, sprop + 4 * x, (top << 8) | bottom)
    blt.write(A2_BASE, FB)
    blt.write(A2_STEP, 0x10000)
    blt.write(A1_FLAGS, syms["COL_A1FLAGS"])
    blt.write(A2_FLAGS, syms["COL_A2FLAGS"])
    gpu.r[14] = stack
    gpu.r[28] = RET
    gpu.run(syms["_R_DrawVisSprite"], RET)
    return screen(mem), blt, mem


def shade(v, amount):
    return (v & 0xFF00) | max(0, min(255, (v & 0xFF) + amount))


# ---- tests -------------------------------------------------------------------
def test_walls_fast_equals_shipped():
    ref, n = draw_walls("independent", 0)
    assert n > 5000, "walls drew too little to be a useful test"
    for ybug in ("independent", "a1drives"):
        got, _ = draw_walls(ybug, 1)
        bad = [i for i in range(W * H) if got[i] != ref[i]]
        assert not bad, f"walls ({ybug}): {len(bad)} pixels differ, first at {divmod(bad[0], W)}"
    print(f"  ok  walls: FASTCOLUMNS draws the same {n} pixels as the shipped blits (both bug-11 readings)")


def test_sprites_fast_equals_shipped():
    total = 0
    for seed in range(12):
        ref, blt, _ = draw_sprite("independent", 0, 180, seed)
        total += blt.pixels
        for ybug in ("independent", "a1drives"):
            got, _, _ = draw_sprite(ybug, 1, 180, seed)
            assert got == ref, f"sprite seed {seed} ({ybug}) differs"
    assert total > 2000
    print(f"  ok  sprites: FASTCOLUMNS draws the same {total} pixels as the shipped blits (both bug-11 readings)")


# the spectre darkening, as r_phase8.gas defines it (8.16 iinc, signed)
FUZZ = ((BUILDS[1, "r_phase8"][1]["FUZZDARK"] >> 16) & 0xFF) - 256 * bool(BUILDS[1, "r_phase8"][1]["FUZZDARK"] & 0x800000)


def test_spectre_fuzz():
    checked = shifted = 0
    for seed in range(12):
        fc = seed * 7
        before = screen(base_memory(random.Random(seed)))
        results = {}
        for fast in (0, 1):
            results[fast], blt, _ = draw_sprite("a1drives", fast, -1, seed, framecount=fc)
        assert results[0] == results[1], f"spectre seed {seed}: fast and shipped differ"
        after = results[1]
        for i in range(W * H):
            if after[i] == before[i]:
                continue
            y, x = divmod(i, W)
            off = 1 + (((x + fc) >> 1) & 1)
            cands = [shade(before[(y + o) * W + x], FUZZ) for o in (off, 0) if y + o < H]
            assert after[i] in cands, f"seed {seed}: pixel ({x},{y}) is not a darkened copy from below"
            checked += 1
            shifted += after[i] == cands[0]
    assert checked > 1000 and shifted > checked // 2
    print(f"  ok  spectres: {checked} pixels are darkened copies of the screen 0-2 rows below "
          f"({shifted} displaced), nothing else touched")


def test_sky_registers():
    """The sky column blit is inlined in R_SegLoop (between L194 and L190);
    check statically that it programs exactly the right blitter registers."""
    for fast in (0, 1):
        image, syms, _ = BUILDS[fast, "r_phase6"]
        lo, hi = syms["L194"] - GPU_LOAD, syms["L190"] - GPU_LOAD
        words = [int.from_bytes(image[i:i + 2], "big") for i in range(0, len(image), 2)]
        regs, imms, i = set(), [], lo // 2
        while i < hi // 2:
            if words[i] >> 10 == 38:        # movei: low word, then high word
                v = words[i + 1] | words[i + 2] << 16
                imms.append(v)
                if 0xF02200 <= v < 0xF022A0:
                    regs.add(v)
                i += 3
            else:
                i += 1
        step = (0xF02210, 0xF02214) if not fast else (0xF0221C, 0xF02220)
        want = {0xF02238, 0xF02200, 0xF0220C, 0xF02218, *step, 0xF02230, 0xF02270, 0xF0223C}
        assert regs == want, f"FASTCOLUMNS={fast}: sky writes {sorted(map(hex, regs))}"
        assert syms["COL_CMD"] in imms
    print("  ok  sky column blit programs the expected blitter registers in both builds")


if __name__ == "__main__":
    test_walls_fast_equals_shipped()
    test_sprites_fast_equals_shipped()
    test_spectre_fuzz()
    test_sky_registers()
