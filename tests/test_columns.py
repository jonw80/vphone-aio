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
FB, W, H = 0x200000, 160, 180         # above the test data even at 640 wide
# main-RAM variables the overlays read; r_detail.c sets the ones for the view
SYM = {"_workingscreen": 0x100000, "_framecount": 0x100004, "_spropening_p": 0x100008,
       "_col_a1screen": 0x10000C, "_col_a2flags": 0x100010, "_screenwidth": 0x100014,
       "_clipbounds_p": 0x100018}
WIDE_SPROP = 0x110000                    # where spropening[] goes when it outgrows GPU RAM
WID = {160: 0x1D, 320: 0x21, 640: 0x25}  # blitter window width codes


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


def screen(mem, sw=W):
    return [int.from_bytes(mem[FB + 2 * i:FB + 2 * i + 2], "big") for i in range(sw * H)]


def base_memory(rng, sw=W):
    mem = bytearray(1 << 24)
    for i in range(sw * H):
        w16(mem, FB + 2 * i, rng.randrange(0x10000))
    w32(mem, SYM["_workingscreen"], FB)
    w32(mem, SYM["_screenwidth"], sw)
    return mem


def screen_flags(syms, name, sw):
    """a COL_ flag value with the screen window set for sw, as R_SetDetail makes it"""
    return syms[name] - (WID[160] << 9) + (WID[sw] << 9)


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


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def wall_reference(tw, th, scale, iscale, top_h, bot_h, mid, texcol, ceil, floor):
    """rows and texel indices R_DrawTexture should draw for one column, with
    the texture tiling down the wall (the row wraps at th)"""
    top = 90 - ((s16(scale) * s16(top_h)) >> 15)
    if not top > ceil:
        top = ceil + 1
    bottom = 89 - ((s16(scale) * s16(bot_h)) >> 15)
    if not bottom < floor:
        bottom = floor - 1
    if top > bottom:
        return []
    frac = mid - (90 - top) * iscale
    col = (texcol & (tw - 1)) * th
    h16 = th << 16
    return [(y, col + (((frac % h16) + (y - top) * iscale) % h16 >> 16)) for y in range(top, bottom + 1)]


def test_walls_tile():
    """walls taller than their texture repeat it, inside the column; the
    shipped code ran on into the next column and past the texture's end"""
    checked = wrapped = 0
    for th in (128, 72):
        rng = random.Random(th)
        mem = base_memory(rng)
        tw = 64
        tex = [rng.randrange(0x10000) for _ in range(tw * th)]
        for i, v in enumerate(tex):
            w16(mem, TEX + 2 * i, v)
        for i in range(64):                      # what follows the texture
            w16(mem, TEX + 2 * (tw * th + i), 0xDEAD)
        for x in range(W):
            # heights are world units * 64 (6 fraction bits): one texel per unit
            if x % 2:       # far and tall: more rows of texture than it has
                scale = rng.randrange(0x0100, 0x02C0)
                top_h = rng.randrange(6000, 24000)
                bot_h = rng.randrange(-24000, -2000)
            else:
                scale = rng.choice([rng.randrange(0x0100, 0x7FFF), 0x7FFF, 0x2000, 0x0800])
                top_h = rng.randrange(-2000, 24000)
                bot_h = rng.randrange(-24000, top_h + 1)
            mid = rng.randrange(-400, 400) << 16
            for k, v in enumerate((TEX, tw, th, top_h, bot_h, mid)):
                w32(mem, TEXP + 4 * k, v)
            ceil = rng.randrange(-1, 80)
            floor = rng.randrange(max(ceil + 1, 90), 181)
            texcol = rng.randrange(0, 4096)
            iscale = 0x2000000 // scale
            before = screen(mem)
            call(mem, "a1drives", 1, "r_phase6", "_R_DrawTexture", {
                15: TEXP, 19: floor, 20: ceil, 21: x, 22: scale,
                23: iscale, 24: texcol, 25: 0})          # no shading: pixel = texel
            after = screen(mem)
            want = dict(wall_reference(tw, th, scale, iscale, top_h, bot_h, mid, texcol, ceil, floor))
            for y in range(H):
                i = y * W + x
                if y in want:
                    assert after[i] == tex[want[y]], f"th {th} x {x} y {y}: {after[i]:04x}, want texel {want[y]}"
                    checked += 1
                else:
                    assert after[i] == before[i], f"th {th} x {x} y {y} drawn outside the column"
            rows = sorted(want)
            if rows and (rows[-1] - rows[0] + 1) * iscale > th << 16:
                wrapped += 1
            assert all(after[j] == before[j] for j in range(W * H) if j % W != x)
    assert checked > 5000 and wrapped > 20, (checked, wrapped)
    print(f"  ok  walls tile: {checked} pixels match the texture wrapped at its height "
          f"({wrapped} columns wrap), none read past the texture")


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


def draw_sprite(ybug, fast, colormap, seed, framecount=0, sw=W):
    rng = random.Random(seed)
    mem = base_memory(rng, sw)
    w32(mem, SYM["_framecount"], framecount)
    width = rng.randrange(20, 40)
    make_patch(mem, rng, width)
    yscale = rng.randrange(0x6000, 0x28000)
    x1 = rng.randrange(-5, sw - 40)
    xiscale = rng.randrange(0x6000, 0x14000) // (sw // W)
    x2 = min(sw - 1, x1 + (width * 0x10000) // xiscale - 1)
    x1c = max(0, x1)
    vis = [x1c, x2, (x1c - x1) * xiscale, 0, xiscale, yscale, (1 << 32) // yscale,
           rng.randrange(10, 90) << 16, PATCH, colormap, 0, 0, 0, 0, PIX]
    for k, v in enumerate(vis):
        w32(mem, VIS + 4 * k, v)
    image, syms, _ = BUILDS[fast, "r_phase8"]
    # 160 columns of spropening[] live inside the image; wider ones in main RAM
    sprop = syms["_spropening"] if sw == W else WIDE_SPROP
    w32(mem, SYM["_spropening_p"], sprop)
    w32(mem, SYM["_col_a1screen"], screen_flags(syms, "COL_A1SCREEN", sw))
    w32(mem, SYM["_col_a2flags"], screen_flags(syms, "COL_A2FLAGS", sw))
    stack = 0xF03F00
    w32(mem, stack, VIS)
    blt = Blitter(mem, ybug)
    gpu = GPU(mem, blt)
    gpu.load_image(GPU_LOAD, image)
    for x in range(sw):
        top, bottom = rng.choice([(0, 180), (rng.randrange(0, 60), rng.randrange(100, 181))])
        w32(mem, sprop + 4 * x, (top << 8) | bottom)
    blt.write(A2_BASE, FB)
    blt.write(A2_STEP, 0x10000)
    blt.write(A1_FLAGS, syms["COL_A1FLAGS"])
    blt.write(A2_FLAGS, screen_flags(syms, "COL_A2FLAGS", sw))
    gpu.r[14] = stack
    gpu.r[28] = RET
    gpu.run(syms["_R_DrawVisSprite"], RET)
    return screen(mem, sw), blt, mem


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


def test_wide_sprites():
    """320 and 640 column views: spropening[] in main RAM, screen window from
    _col_a2flags.  Check against a direct reading of the vissprite."""
    for sw in (320, 640):
        total = 0
        for seed in range(6):
            got, blt, mem = draw_sprite("a1drives", 1, 180, seed, sw=sw)
            ref, blt0, _ = draw_sprite("independent", 0, 180, seed, sw=sw)
            assert got == ref, f"{sw} wide sprite seed {seed}: fast and shipped differ"
            before = screen(base_memory(random.Random(seed), sw), sw)
            vis = [int.from_bytes(mem[VIS + 4 * k:VIS + 4 * k + 4], "big") for k in range(2)]
            changed = [i % sw for i in range(sw * H) if got[i] != before[i]]
            assert changed, f"{sw} wide sprite seed {seed} drew nothing"
            assert vis[0] <= min(changed) and max(changed) <= vis[1], \
                f"{sw} wide sprite seed {seed} drew outside x {vis[0]}..{vis[1]}"
            total += blt.pixels
        assert total > 2000
        print(f"  ok  sprites {sw} wide: {total} pixels, inside their columns, fast == shipped")


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
    test_walls_tile()
    test_sprites_fast_equals_shipped()
    test_wide_sprites()
    test_spectre_fuzz()
    test_sky_registers()
