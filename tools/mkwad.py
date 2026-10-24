#!/usr/bin/env python3
"""
mkwad.py - build a Jaguar Doom WAD from PC-format Doom WADs.

    tools/mkwad.py --pwad jaguartc.wad --iwad freedoom1.wad -o jagdoom.wad
    tools/mkwad.py --pwad jaguartc.wad --iwad freedoom1.wad --sprites doom.wad -o jagdoom.wad

The levels, textures and status bar come from the PWAD (made for the
"Jaguar Doom TC" recreation, which runs on PC ports on top of DOOM.WAD);
everything the PWAD borrows from DOOM.WAD is taken from the IWAD instead.
With Freedoom's freedoom1.wad as the IWAD the result contains no id
Software data beyond what the PWAD itself carries.  --sprites takes the
sprites (between S_START and S_END) from a third WAD, such as your own
DOOM.WAD, over the IWAD's; everything else still comes from the IWAD.

The output follows the Jaguar engine's own WAD rules (see the loaders in
w_wad.c, r_data.c, p_setup.c, s_sound.c and the GPU decoders):

  * big-endian "IWAD" directory; a lump whose name has bit 7 of its first
    character set is LZSS-compressed and its size field is the unpacked size
  * lumps 0..N are the sprites, at the indices sprinfo.c was generated
    with: even lump = patch header + post list (read in place from ROM),
    odd lump = the post pixels, 8-bit, always compressed
  * texture and flat pixels are always compressed (the GPU unpacks them
    straight to 16-bit CRY); textures are column-major, power-of-2 wide
  * graphics are "jagobj"s: 16-byte header + 8-bit rows, index 0 = clear
  * sounds are 11025 Hz unsigned 8-bit with a 28-byte header; music is the
    DSP sequencer's event format with sampled instruments (jagmusic.py)

Run with --check to also LZSS-decode every compressed lump with a port of
the engine's decoder and compare.
"""
import argparse
import io
import multiprocessing
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

MAXLUMPS = 2048
WEAPON_SPRITES = {"PUNG", "PISG", "PISF", "SHTG", "SHTF", "CHGG", "CHGF", "MISG", "MISF",
                  "SAWG", "PLSG", "PLSF", "BFGG", "BFGF"}
MAXTEXTURES = 128
ROM_ROOM = 0x400000 - 0x40000       # the WAD lives at $840000-$BFFFFF


# ---------------------------------------------------------------------------
# PC WAD input

class PCWad:
    def __init__(self, path):
        d = open(path, "rb").read()
        ident, n, ofs = struct.unpack("<4sII", d[:12])
        if ident not in (b"IWAD", b"PWAD"):
            sys.exit(f"{path}: not a WAD file")
        self.path = path
        self.lumps = []
        for i in range(n):
            pos, size, name = struct.unpack("<II8s", d[ofs + 16 * i:ofs + 16 * i + 16])
            self.lumps.append((name.split(b"\0")[0].decode("latin-1").upper(), d[pos:pos + size]))

    def index(self, name, start=0):
        for i in range(start, len(self.lumps)):
            if self.lumps[i][0] == name:
                return i
        return None

    def get(self, name):
        for n, d in reversed(self.lumps):
            if n == name:
                return d
        return None

    def between(self, starts, ends):
        """name -> data for lumps between any start and end marker (later wins)"""
        out, inside = {}, False
        for n, d in self.lumps:
            if n in starts:
                inside = True
            elif n in ends:
                inside = False
            elif inside and d:
                out[n] = d
        return out


class Assets:
    """Name lookup with the PWAD over the IWAD, per namespace."""

    def __init__(self, pwad, iwad, sprwad=None):
        self.p, self.i = pwad, iwad
        self.patches = {**iwad.between({"P_START", "PP_START"}, {"P_END", "PP_END"}),
                        **pwad.between({"P_START", "PP_START"}, {"P_END", "PP_END"})}
        self.flats = {**iwad.between({"F_START", "FF_START"}, {"F_END", "FF_END"}),
                      **pwad.between({"F_START", "FF_START"}, {"F_END", "FF_END"})}
        self.sprites = {**iwad.between({"S_START", "SS_START"}, {"S_END", "SS_END"}),
                        **(sprwad.between({"S_START", "SS_START"}, {"S_END", "SS_END"}) if sprwad else {}),
                        **pwad.between({"S_START", "SS_START"}, {"S_END", "SS_END"})}

    def get(self, name):
        d = self.p.get(name)
        return d if d is not None else self.i.get(name)

    def patch(self, name):
        if name in self.patches:
            return self.patches[name]
        return self.get(name)


# ---------------------------------------------------------------------------
# LZSS, as unpacked by w_wad.c decode(), decomp.gas and r_phase5.gas R_decode:
# a flag byte (LSB first) per 8 items; flag 0 = literal byte, flag 1 = two
# bytes pos:12 len:4 copying len+1 bytes from (output - pos - 1); len 0 ends.

def lzss_encode(data):
    data = bytes(data)
    n = len(data)
    out = bytearray()
    i = 0
    while True:
        flagpos = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if i >= n:
                flags |= 1 << bit
                out += b"\0\0"                      # end of stream
                out[flagpos] = flags
                return bytes(out)
            lo = max(0, i - 4096)
            maxlen = min(16, n - i)
            best, dist = 0, 0
            k = 2
            while k <= maxlen:
                j = data.rfind(data[i:i + k], lo, i + k - 1)
                if j < 0:
                    break
                m = k
                while m < maxlen and data[j + m] == data[i + m]:
                    m += 1
                best, dist = m, i - j
                k = m + 1
            if best >= 2:
                flags |= 1 << bit
                pos = dist - 1
                out.append(pos >> 4)
                out.append(((pos & 15) << 4) | (best - 1))
                i += best
            else:
                out.append(data[i])
                i += 1
        out[flagpos] = flags


def lzss_decode(src):
    """Port of w_wad.c decode()."""
    out = bytearray()
    p = 0
    getid = 0
    idbyte = 0
    while True:
        if not getid:
            idbyte = src[p]
            p += 1
        getid = (getid + 1) & 7
        if idbyte & 1:
            pos = (src[p] << 4) | (src[p + 1] >> 4)
            length = (src[p + 1] & 15) + 1
            p += 2
            if length == 1:
                return bytes(out)
            s = len(out) - pos - 1
            if s < 0:
                raise ValueError("LZSS reference before the start of the output")
            for k in range(length):
                out.append(out[s + k])
        else:
            out.append(src[p])
            p += 1
        idbyte >>= 1


# ---------------------------------------------------------------------------
# palette and pictures

class Palette:
    def __init__(self, playpal):
        self.rgb = [tuple(playpal[3 * i:3 * i + 3]) for i in range(256)]
        self.cache = {}

    def nearest(self, rgb, avoid=()):
        key = (rgb, avoid)
        c = self.cache.get(key)
        if c is None:
            r, g, b = rgb
            best = None
            for i, (pr, pg, pb) in enumerate(self.rgb):
                if i in avoid:
                    continue
                d = 3 * (pr - r) ** 2 + 4 * (pg - g) ** 2 + 2 * (pb - b) ** 2
                if best is None or d < best[0]:
                    best = (d, i)
            c = self.cache[key] = best[1]
        return c


class Pic:
    """An 8-bit picture; pixels are palette indices or None (clear)."""

    def __init__(self, w, h, px=None, xoff=0, yoff=0):
        self.w, self.h = w, h
        self.px = px if px is not None else [None] * (w * h)
        self.xoff, self.yoff = xoff, yoff

    def at(self, x, y):
        return self.px[y * self.w + x]

    def put(self, x, y, v):
        if 0 <= x < self.w and 0 <= y < self.h and v is not None:
            self.px[y * self.w + x] = v

    def blit(self, src, x0, y0):
        for y in range(src.h):
            for x in range(src.w):
                v = src.px[y * src.w + x]
                if v is not None:
                    self.put(x0 + x, y0 + y, v)

    def scaled(self, w, h):
        out = Pic(w, h)
        for y in range(h):
            sy = min(self.h - 1, y * self.h // h)
            for x in range(w):
                out.px[y * w + x] = self.px[sy * self.w + min(self.w - 1, x * self.w // w)]
        return out

    def flipped(self):
        out = Pic(self.w, self.h, xoff=self.xoff, yoff=self.yoff)
        for y in range(self.h):
            row = self.px[y * self.w:(y + 1) * self.w]
            out.px[y * self.w:(y + 1) * self.w] = row[::-1]
        return out

    def bbox(self):
        xs = [i % self.w for i, v in enumerate(self.px) if v is not None]
        ys = [i // self.w for i, v in enumerate(self.px) if v is not None]
        if not xs:
            return 0, 0, 1, 1
        return min(xs), min(ys), max(xs) + 1, max(ys) + 1

    def crop(self, x0, y0, x1, y1):
        out = Pic(x1 - x0, y1 - y0)
        for y in range(y0, y1):
            out.px[(y - y0) * out.w:(y - y0 + 1) * out.w] = self.px[y * self.w + x0:y * self.w + x1]
        return out

    def map(self, f):
        return Pic(self.w, self.h, [None if v is None else f(v) for v in self.px], self.xoff, self.yoff)


def read_patch(d):
    """PC patch_t -> Pic (offsets kept)."""
    w, h, lo, to = struct.unpack("<hhhh", d[:8])
    pic = Pic(w, h, xoff=lo, yoff=to)
    cols = struct.unpack(f"<{w}I", d[8:8 + 4 * w])
    for x, p in enumerate(cols):
        top = -1
        while p < len(d) and d[p] != 0xFF:
            td, ln = d[p], d[p + 1]
            if td <= top:                       # "tall patch" extension
                td += top
            top = td
            for y in range(ln):
                pic.put(x, td + y, d[p + 3 + y])
            p += ln + 4
    return pic


def read_png(d, pal):
    from PIL import Image
    im = Image.open(io.BytesIO(d))
    w, h = im.size
    if im.mode == "P":
        ppal = im.getpalette()
        trans = im.info.get("transparency")
        tset = set()
        if isinstance(trans, int):
            tset = {trans}
        elif isinstance(trans, (bytes, bytearray)):
            tset = {i for i, a in enumerate(trans) if a == 0}
        remap = {}
        for i in range(256):
            rgb = tuple(ppal[3 * i:3 * i + 3]) if 3 * i + 2 < len(ppal) else (0, 0, 0)
            remap[i] = i if pal.rgb[i] == rgb else pal.nearest(rgb)
        px = [None if v in tset else remap[v] for v in im.tobytes()]
    else:
        im = im.convert("RGBA")
        px = [None if a < 128 else pal.nearest((r, g, b)) for r, g, b, a in im.getdata()]
    return Pic(w, h, px)


def read_flat(d):
    return Pic(64, 64, list(d[:4096]))


def read_fon2(d, pal):
    """ZDoom FON2 font -> {char: Pic}."""
    if d[:4] != b"FON2":
        raise ValueError("not a FON2 font")
    h = struct.unpack("<H", d[4:6])[0]
    first, last, constw, _shading, palsize, flags = d[6:12]
    p = 12 + (2 if flags & 1 else 0)
    n = last - first + 1
    if constw:
        widths = [struct.unpack("<H", d[p:p + 2])[0]] * n
        p += 2
    else:
        widths = list(struct.unpack(f"<{n}H", d[p:p + 2 * n]))
        p += 2 * n
    fpal = [tuple(d[p + 3 * i:p + 3 * i + 3]) for i in range(palsize + 1)]
    p += 3 * (palsize + 1)
    remap = [None] + [pal.nearest(c, avoid=(0,)) for c in fpal[1:]]
    glyphs = {}
    for i, w in enumerate(widths):
        if not w:
            continue
        need, raw = w * h, bytearray()
        while len(raw) < need:
            c = d[p]
            p += 1
            if c < 128:
                raw += d[p:p + c + 1]
                p += c + 1
            elif c > 128:
                raw += bytes([d[p]]) * (257 - c)
                p += 1
        glyphs[chr(first + i)] = Pic(w, h, [remap[v] for v in raw[:need]])
    return glyphs


# ---------------------------------------------------------------------------
# Jaguar output formats

def be16(*v):
    return struct.pack(f">{len(v)}h", *v)


def jagobj(pic, clear=0, black=247):
    """jagobj_t: width, height, depth (3 = 8 bit), index, 4 pads, then rows.
    Clear pixels become `clear`; opaque index 0 is moved to `black` so it
    does not turn transparent."""
    data = bytes(clear if v is None else (black if v == 0 and clear == 0 else v) for v in pic.px)
    return be16(pic.w, pic.h, 3, 0, 0, 0, 0, 0) + data


def jag_sprite(pic):
    """-> (patch lump, pixel lump).  Posts are 4 bytes: topdelta, length,
    offset of the post's pixels in the pixel lump; a long of $FFFFFFFF ends
    a column (the GPU tests bit 15, the 68000 tests topdelta == $FF)."""
    w, h = pic.w, pic.h
    if h > 254:
        raise ValueError("sprite taller than 254 pixels")
    head = (8 + 2 * w + 3) & ~3                  # posts are read as longs
    body, pixels, ofs = bytearray(), bytearray(), []
    for x in range(w):
        ofs.append(head + len(body))
        y = 0
        while y < h:
            if pic.px[y * w + x] is None:
                y += 1
                continue
            start = y
            while y < h and pic.px[y * w + x] is not None and y - start < 255:
                y += 1
            body += bytes([start, y - start]) + struct.pack(">H", len(pixels))
            pixels += bytes(pic.px[r * w + x] for r in range(start, y))
        body += b"\xff\xff\xff\xff"
    if len(pixels) >= 0x8000:
        raise ValueError("sprite has more than 32767 opaque pixels")
    patch = be16(w, h, pic.xoff, pic.yoff) + struct.pack(f">{w}H", *ofs)
    patch += bytes(head - len(patch)) + body
    return patch, bytes(pixels) or b"\0"


def sfx_lump(samples11k):
    s = bytes(samples11k)
    s += b"\x80" * (-len(s) % 4)
    return struct.pack(">7i", len(s), 0, 0, 0, 0, 0, 0) + s


def dmx_to_11k(d):
    fmt, rate, count = struct.unpack("<HHI", d[:8])
    if fmt != 3:
        raise ValueError("not a DMX sound")
    raw = d[8:8 + count]
    if count > 32 and raw[:16] == bytes([raw[16]]) * 16:
        raw = raw[16:-16]                         # DMX padding
    if rate == 11025:
        return raw
    if rate == 22050:
        return bytes((raw[i] + raw[i + 1] + 1) >> 1 for i in range(0, len(raw) - 1, 2))
    n = int(len(raw) * 11025 / rate)
    return bytes(raw[min(len(raw) - 1, int(i * rate / 11025))] for i in range(n))


# ---------------------------------------------------------------------------
# engine tables read from the source, so they cannot drift

def engine_tables():
    def src(name):
        return open(os.path.join(ROOT, name), "rb").read().decode("latin-1").replace("\r", "")

    info = src("info.c")
    names = re.findall(r'"(\w{4})"', info[info.index("sprnames"):info.index("};")])
    spr = src("sprinfo.c")
    jag = spr[spr.index("#else"):]
    frames = []
    for rot, lumps, flips in re.findall(r"\{(\d+), \{([\d,]+)\}, \{([\d,]+)\} \}", jag):
        frames.append((int(rot), [int(x) for x in lumps.split(",")], [int(x) for x in flips.split(",")]))
    defs = [(int(a), int(b)) for a, b in re.findall(r"\{(\d+), &spriteframes\[(\d+)\]\}", jag)]
    p5 = src("r_phase5.gas")
    vga = [int(x) for x in re.findall(r"\.dc\.l\s+(-?\d+)", p5[p5.index("_vgatojag::"):])[:256]]
    sfx = re.findall(r'\{ "(\w+)",', src("sounds.c")[src("sounds.c").index("S_sfx"):])
    mus = re.findall(r'\{ "(\w+)", 0 \}', src("sounds.c"))
    code = "".join(src(f) for f in os.listdir(ROOT) if f.endswith(".c") and f != "sounds.c")
    unused = {s for s in sfx if not re.search(r"\bsfx_" + s + r"\b", code)}
    things = set()
    for m in re.finditer(r"\{\s*/\*\s*MT_\w+\s*\*/\s*(-?\d+)", info):
        things.add(int(m.group(1)))
    return dict(sprnames=names, frames=frames, defs=defs, vgatojag=vga, sfx=sfx, music=mus,
                things=things, unused_sfx=unused)


# ---------------------------------------------------------------------------
# the builder

class JagWad:
    def __init__(self):
        self.lumps = []                           # [name, data, compress]

    def add(self, name, data, compress=False):
        if len(name) > 8:
            raise ValueError(f"lump name {name} too long")
        self.lumps.append([name.upper(), bytes(data), compress])
        return len(self.lumps) - 1

    def set(self, index, name, data, compress=False):
        while len(self.lumps) <= index:
            self.lumps.append(["-", b"", False])
        self.lumps[index] = [name.upper(), bytes(data), compress]

    def find(self, name):
        """W_CheckNumForName: the last lump with this name."""
        name = name.upper()
        for i in range(len(self.lumps) - 1, -1, -1):
            if self.lumps[i][0] == name:
                return i
        return -1

    def build(self, jobs=None, check=False):
        todo = [i for i, l in enumerate(self.lumps) if l[2] and l[1]]
        with multiprocessing.Pool(jobs) as pool:
            packed = pool.map(lzss_encode, [self.lumps[i][1] for i in todo], chunksize=4)
        packed = dict(zip(todo, packed))
        if check:
            for i, p in packed.items():
                if lzss_decode(p) != self.lumps[i][1]:
                    sys.exit(f"LZSS round trip failed for {self.lumps[i][0]}")
        body = bytearray(b"\0" * 12)
        dirents = []
        for i, (name, data, comp) in enumerate(self.lumps):
            body += bytes(-len(body) % 8)
            raw = name.encode("latin-1").ljust(8, b"\0")
            if i in packed:
                raw = bytes([raw[0] | 0x80]) + raw[1:]
                dirents.append(struct.pack(">II", len(body), len(data)) + raw)
                body += packed[i]
            else:
                dirents.append(struct.pack(">II", len(body), len(data)) + raw)
                body += data
        body += bytes(-len(body) % 8)
        body[:12] = b"IWAD" + struct.pack(">II", len(self.lumps), len(body))
        return bytes(body + b"".join(dirents)), packed


class Converter:
    def __init__(self, pwad, iwad, log=print, sprwad=None):
        self.a = Assets(pwad, iwad, sprwad)
        self.pwad, self.iwad = pwad, iwad
        self.log = log
        self.t = engine_tables()
        playpal = iwad.get("PLAYPAL") or pwad.get("PLAYPAL")
        self.pal = Palette(playpal)
        self.out = JagWad()
        self.notes = []
        self.demo_files = {}
        self.spectres = False

    def warn(self, msg):
        self.notes.append(msg)
        self.log("  warning: " + msg)

    # ---- sprites ----------------------------------------------------------
    def pc_sprite(self, name, frame, rot):
        """-> Pic for sprite name, frame letter, PC rotation (0 = any)."""
        sp = self.a.sprites
        cands = []
        for lname, d in sp.items():
            if lname[:4] != name:
                continue
            for k, flip in ((4, False), (6, True)):
                if len(lname) >= k + 2 and lname[k] == frame:
                    cands.append((int(lname[k + 1]), flip, d))
        if not cands:
            return None
        for want in ([rot] if rot else []) + [0, 1]:
            for r, flip, d in cands:
                if r == want:
                    pic = read_patch(d)
                    return pic.flipped() if flip else pic
        return None

    def weapon_view(self, pic):
        """PC weapon sprites are drawn 1:1 on a 320x168 view; the Jaguar
        draws them 1:1 on its 160x180 view, whose pixels are twice as wide
        (R_FinishPSprite: x1 = sx - leftoffset, texturemid = 100 - sy +
        topoffset, scale 1).  Halve the width (averaging pixel pairs) so the
        gun keeps its proportions, put it where the PC puts it, and keep its
        bottom edge the same distance above the status bar."""
        w2 = (pic.w + 1) // 2
        out = Pic(w2, pic.h)
        rgb = self.pal.rgb
        for y in range(pic.h):
            for x in range(w2):
                a = pic.px[y * pic.w + 2 * x]
                b = pic.px[y * pic.w + 2 * x + 1] if 2 * x + 1 < pic.w else None
                if a is not None and b is not None and a != b:
                    c = tuple((p + q + 1) // 2 for p, q in zip(rgb[a], rgb[b]))
                    v = self.pal.nearest(c)
                else:
                    v = a if a is not None else b
                out.px[y * w2 + x] = v
        out.xoff = int(round(1 - (1 - pic.xoff) / 2))
        out.yoff = pic.yoff - 6
        return out

    def sprites(self):
        names, frames, defs = self.t["sprnames"], self.t["frames"], self.t["defs"]
        uses = {}                                  # jag lump -> [(name, frame, pc rot, flip)]
        for s, (numframes, first) in enumerate(defs):
            for f in range(numframes):
                rotate, lumps, flips = frames[first + f]
                for r in range(8 if rotate else 1):
                    uses.setdefault(lumps[r], []).append(
                        (names[s], chr(ord("A") + f), r + 1 if rotate else 0, flips[r]))
        # a lump holds the view its first unflipped use asks for (the
        # engine mirrors it for the uses flagged flip)
        need = {}
        for lump, u in uses.items():
            need[lump] = next((x for x in u if not x[3]), u[0])
        top = max(need) + 2
        made = 0
        for lump in range(0, top, 2):
            if lump not in need:
                self.out.set(lump, "UNUSED", b"")
                self.out.set(lump + 1, "UNUSED", b"\0")
                continue
            name, frame, rot, flip = need[lump]
            pic = self.pc_sprite(name, frame, rot)
            if pic is None:
                self.warn(f"no sprite {name}{frame}{rot}; using {name}A")
                pic = self.pc_sprite(name, "A", rot) or Pic(1, 1, [0])
            if flip:
                pic = pic.flipped()
            if name in WEAPON_SPRITES:
                pic = self.weapon_view(pic)
            patch, pixels = jag_sprite(pic)
            tag = f"{name}{frame}{rot}"
            self.out.set(lump, tag, patch)
            self.out.set(lump + 1, tag, pixels, compress=True)
            made += 1
        self.log(f"  sprites: {made} frames in lumps 0-{top - 1}")

    # ---- textures and flats -------------------------------------------------
    @staticmethod
    def texture_defs(wad):
        pn = wad.get("PNAMES")
        if not pn:
            return {}
        n = struct.unpack("<I", pn[:4])[0]
        pnames = [pn[4 + 8 * i:12 + 8 * i].split(b"\0")[0].decode("latin-1").upper() for i in range(n)]
        out = {}
        for lump in ("TEXTURE1", "TEXTURE2"):
            t = wad.get(lump)
            if not t:
                continue
            for i in range(struct.unpack("<I", t[:4])[0]):
                o = struct.unpack("<I", t[4 + 4 * i:8 + 4 * i])[0]
                name = t[o:o + 8].split(b"\0")[0].decode("latin-1").upper()
                w, h = struct.unpack("<hh", t[o + 12:o + 16])
                count = struct.unpack("<h", t[o + 20:o + 22])[0]
                ps = []
                for j in range(count):
                    ox, oy, pi = struct.unpack("<hhh", t[o + 22 + 10 * j:o + 28 + 10 * j])
                    ps.append((ox, oy, pnames[pi]))
                out[name] = (w, h, ps)
        return out

    def compose(self, name, defs_p, defs_i):
        own = set(self.pwad.between({"P_START", "PP_START"}, {"P_END", "PP_END"}))
        d = None
        if name in defs_p and any(p in own for _, _, p in defs_p[name][2]):
            d = defs_p[name]                        # the PWAD's own artwork
        elif name in defs_i:
            d = defs_i[name]                        # consistent with the IWAD's patches
        elif name in defs_p:
            d = defs_p[name]
        if d is None:
            return None
        w, h, ps = d
        pic = Pic(w, h, [0] * (w * h))
        for ox, oy, pn in ps:
            data = self.a.patch(pn)
            if data is None:
                self.warn(f"texture {name}: no patch {pn}")
                continue
            pic.blit(read_patch(data), ox, oy)
        return pic

    def textures_and_flats(self, maps):
        # a texture's pixels are found by W_CheckNumForName(texture name),
        # which scans from the end of the WAD: a sound of the same name
        # (the PC "METAL" texture and the "metal" sound) would win, so
        # such textures are renamed here and in the sidedefs
        clash = {s.upper() for s in self.t["sfx"]}
        rename = {}
        used_tex, used_flat = [], []
        for lumps in maps.values():
            sd = bytearray(lumps[2])
            for i in range(0, len(sd), 30):
                for k in (4, 12, 20):
                    t = sd[i + k:i + k + 8].split(b"\0")[0].decode("latin-1").upper()
                    if t in clash:
                        new = rename.setdefault(t, (t + "_T")[:8])
                        sd[i + k:i + k + 8] = new.encode().ljust(8, b"\0")
                        t = new
                    if t and t != "-" and t not in used_tex:
                        used_tex.append(t)
            lumps[2] = bytes(sd)
            se = lumps[7]
            for i in range(0, len(se), 26):
                for k, ceil in ((4, False), (12, True)):
                    f = se[i + k:i + k + 8].split(b"\0")[0].decode("latin-1").upper()
                    if ceil and f == "F_SKY1":
                        continue
                    if f not in used_flat:
                        used_flat.append(f)
        switches = [("SW1BRN1", "SW2BRN1"), ("SW1GARG", "SW2GARG"), ("SW1GSTON", "SW2GSTON"),
                    ("SW1HOT", "SW2HOT"), ("SW1STAR", "SW2STAR"), ("SW1WOOD", "SW2WOOD")]
        for a, b in switches:
            if a in used_tex or b in used_tex:
                for t in (a, b):
                    if t not in used_tex:
                        used_tex.append(t)
        defs_p, defs_i = self.texture_defs(self.pwad), self.texture_defs(self.iwad)
        names = ["AASTINKY"] + [t for t in ("SKY1", "SKY2", "SKY3") if t not in used_tex] + used_tex
        if len(names) > MAXTEXTURES:
            sys.exit(f"{len(names)} textures, the engine holds {MAXTEXTURES}")
        back = {v: k for k, v in rename.items()}
        for k, v in rename.items():
            self.log(f"  texture {k} renamed {v} (a sound has its name)")
        tex = []
        for name in names:
            pic = self.compose(back.get(name, name), defs_p, defs_i)
            if pic is None:
                self.warn(f"no texture {name}; using AASTINKY")
                pic = self.compose("AASTINKY", defs_p, defs_i) or Pic(64, 128, [0] * 8192)
            if pic.w & (pic.w - 1):
                w2 = 1 << (pic.w.bit_length())
                self.warn(f"texture {name} is {pic.w} wide; stretched to {w2}")
                pic = pic.scaled(w2, pic.h)
            tex.append((name, pic))
        # TEXTURE1: only name/width/height are read by R_InitTextures
        hdr = struct.pack("<I", len(tex))
        offs, body = [], b""
        base = 4 + 4 * len(tex)
        for name, pic in tex:
            offs.append(base + len(body))
            body += name.encode().ljust(8, b"\0") + struct.pack("<ihhih", 0, pic.w, pic.h, 0, 0)
        self.out.add("TEXTURE1", hdr + struct.pack(f"<{len(offs)}I", *offs) + body, compress=True)
        for name, pic in tex:
            col = bytes(pic.px[y * pic.w + x] for x in range(pic.w) for y in range(pic.h))
            self.out.add(name, col, compress=True)
        # flats, with the animated ranges p_spec.c cycles kept contiguous
        anims = [["NUKAGE1", "NUKAGE2", "NUKAGE3"], ["FWATER1", "FWATER2", "FWATER3", "FWATER4"],
                 ["LAVA1", "LAVA2", "LAVA3", "LAVA4"]]
        order = []
        for f in used_flat:
            group = next((g for g in anims if f in g), [f])
            for g in group:
                if g not in order:
                    order.append(g)
        self.out.add("F_START", b"")
        for f in order:
            d = self.a.flats.get(f)
            if d is None:
                self.warn(f"no flat {f}")
                d = bytes(4096)
            self.out.add(f, d[:4096].ljust(4096, b"\0"), compress=True)
        self.out.add("F_END", b"")
        self.log(f"  textures: {len(tex)} (limit {MAXTEXTURES}); flats: {len(order)}")
        return [n for n, _ in tex]

    # ---- maps ---------------------------------------------------------------
    def map_order(self):
        """Jaguar MAPnn -> PWAD map marker, from the PWAD's MAPINFO levelnums."""
        order = {}
        for lump in ("ZMAPINFO", "EMAPINFO", "MAPINFO"):
            txt = self.pwad.get(lump)
            if not txt:
                continue
            txt = txt.decode("latin-1")
            for m in re.finditer(r"map\s+(\w+)[^{]*\{([^}]*)\}", txt, re.I):
                lv = re.search(r"levelnum\s*=\s*(\d+)", m.group(2), re.I)
                if lv:
                    order[int(lv.group(1))] = m.group(1).upper()
            if order:
                break
        if not order:
            seq = [f"E{e}M{m}" for e in (1, 2, 3) for m in range(1, 9)]
            order = {i + 1: n for i, n in enumerate(seq)}
        return dict(sorted(order.items()))

    def read_maps(self):
        maps = {}
        for num, marker in self.map_order().items():
            i = self.pwad.index(marker)
            if i is None:
                self.warn(f"no map {marker} for MAP{num:02d}")
                continue
            lumps = [d for _, d in self.pwad.lumps[i + 1:i + 11]]
            names = [n for n, _ in self.pwad.lumps[i + 1:i + 11]]
            if names != ["THINGS", "LINEDEFS", "SIDEDEFS", "VERTEXES", "SEGS", "SSECTORS",
                         "NODES", "SECTORS", "REJECT", "BLOCKMAP"]:
                sys.exit(f"{marker}: unexpected lumps {names}")
            maps[num] = lumps
        return maps

    def write_maps(self, maps):
        known = self.t["things"]
        biggest = 0
        for num, lumps in maps.items():
            if self.spectres and num >= 9:
                # the Jaguar levels have no spectres (its renderer could not
                # draw them); this build can, so on the episode 2 and 3 levels
                # every second demon becomes one
                th, k = bytearray(lumps[0]), 0
                for i in range(0, len(th), 10):
                    if struct.unpack("<h", th[i + 6:i + 8])[0] == 3002:
                        if k % 2 == 0:
                            th[i + 6:i + 8] = struct.pack("<h", 58)
                        k += 1
                lumps[0] = bytes(th)
            th = lumps[0]
            for i in range(0, len(th), 10):
                t = struct.unpack("<h", th[i + 6:i + 8])[0]
                if known and t not in known and t not in (1, 2, 3, 4, 11):
                    self.warn(f"MAP{num:02d}: thing type {t} unknown to the engine")
            self.out.add(f"MAP{num:02d}", b"")
            for n, d in zip(("THINGS", "LINEDEFS", "SIDEDEFS", "VERTEXES", "SEGS", "SSECTORS",
                             "NODES", "SECTORS", "REJECT", "BLOCKMAP"), lumps):
                self.out.add(n, d, compress=True)
                if n != "BLOCKMAP" and n != "REJECT":
                    biggest = max(biggest, len(d))
        # P_Load* unpack into I_TempBuffer() = a 64 KB frame buffer
        if biggest > 0x10000:
            sys.exit(f"a map lump is {biggest} bytes; the level loader's buffer is 64 KB")
        self.log(f"  maps: {len(maps)} (largest lump {biggest} bytes)")

    # ---- pictures -----------------------------------------------------------
    def pic(self, name):
        d = self.a.get(name)
        if d is None:
            return None
        if d[:4] == b"\x89PNG":
            return read_png(d, self.pal)
        return read_patch(d)

    def font(self):
        if not hasattr(self, "_font"):
            d = self.a.get("DBIGFONT")
            self._font = read_fon2(d, self.pal) if d and d[:4] == b"FON2" else None
            if self._font is None:
                # fall back to the small HUD font, doubled
                g = {}
                for c in range(33, 96):
                    p = self.pic(f"STCFN{c:03d}")
                    if p:
                        g[chr(c)] = p.scaled(p.w * 2, p.h * 2)
                g[" "] = Pic(8, 14)
                self._font = g
            # the Jaguar's menu font is red: keep each glyph's shading, in red
            lum = [sum(c) / 3 for c in self.pal.rgb]
            top = max((lum[v] for p in self._font.values() for v in p.px if v is not None), default=1)

            def red(v):
                t = min(1.0, lum[v] / max(1, top) * 1.1)
                return self.pal.nearest((int(40 + 215 * t), int(100 * t * t), int(100 * t * t)), avoid=(0,))
            self._font = {c: p.map(red) for c, p in self._font.items()}
        return self._font

    def text(self, s, scale=1.0, xscale=None, spacing=0):
        f = self.font()
        xs = scale if xscale is None else xscale
        glyphs = []
        for ch in s:
            g = f.get(ch.upper()) or f.get(ch) or Pic(8, 15)
            if scale != 1 or xs != 1:
                g = g.scaled(max(1, round(g.w * xs)), max(1, round(g.h * scale)))
            glyphs.append(g)
        w = sum(g.w for g in glyphs) + spacing * max(0, len(glyphs) - 1)
        h = max(g.h for g in glyphs)
        out = Pic(max(1, w), h)
        x = 0
        for g in glyphs:
            out.blit(g, x, h - g.h)
            x += g.w + spacing
        return out

    def plaque(self, s, half=False):
        """A framed text box.  DrawPlaque doubles every pixel horizontally,
        DrawSinglePlaque does not."""
        t = self.text(s, xscale=0.5 if half else 1.0)
        pw, ph = t.w + (8 if half else 16), t.h + 12
        p = Pic(pw, ph, [0] * (pw * ph))
        frame = self.pal.nearest((100, 100, 100))
        fill = self.pal.nearest((24, 24, 24), avoid=(0,))
        for y in range(ph):
            for x in range(pw):
                edge = x == 0 or y == 0 or x == pw - 1 or y == ph - 1
                p.px[y * pw + x] = frame if edge else fill
        p.blit(t, (pw - t.w) // 2, 6)
        return p

    def obj(self, name, pic, compress=True):
        self.out.add(name, jagobj(pic), compress=compress)

    def background(self):
        """M_TITLE: 4-bit, 320 wide, colours = CRYPAL[80..95] (light greys),
        drawn by the object processor behind every menu screen."""
        src = None
        for f in ("FLOOR7_2", "FLAT5_4", "GRNROCK", "ROCKS", "FLAT1"):
            d = self.a.flats.get(f)
            if d:
                src = d
                break
        lum = [sum(c) / 3 for c in self.pal.rgb]
        w, h = 320, 224
        vals = []
        for y in range(h):
            for x in range(w):
                v = lum[src[(y % 64) * 64 + (x % 64)]] if src else 128
                vals.append(v)
        lo, hi = min(vals), max(vals)
        nib = []                                    # 0 = CRYPAL[80], brightest
        for v in vals:
            t = (v - lo) / max(1, hi - lo)          # 0 dark .. 1 bright
            nib.append(int(round(4 + 11 * (1 - t))))
        data = bytearray()
        for i in range(0, len(nib), 2):
            data.append(((nib[i] & 15) << 4) | (nib[i + 1] & 15))
        self.out.add("M_TITLE", be16(w, h, 2, 80, 0, 0, 0, 0) + bytes(data))

    def graphics(self):
        P = self.pic
        pal = self.pal
        title = P("TITLEPIC")
        self.obj("title", title.map(lambda v: 247 if v == 0 else v))
        credit = P("CREDIT") or title
        self.obj("credits", credit.map(lambda v: 247 if v == 0 else v))
        self.background()

        # main menu
        logo = P("M_DOOM")
        self.obj("M_DOOM", logo)
        self.obj("M_SKULL1", P("M_SKULL1"))
        self.obj("M_SKULL2", P("M_SKULL2"))
        self.obj("M_GAMMOD", self.text("Game Mode"))
        self.obj("M_LEVEL", self.text("Area"))
        self.obj("M_DIFF", self.text("Difficulty"))
        for n, s in (("M_SINGLE", "Single"), ("M_COOP", "Cooperative"), ("M_DEATH", "Deathmatch")):
            self.obj(n, self.text(s))
        for i, n in enumerate(("M_JKILL", "M_ROUGH", "M_HURT", "M_ULTRA", "M_NMARE")):
            p = P(n)
            x0, y0, x1, y1 = p.bbox()
            self.obj(f"SKILL{i}", p.crop(x0, y0, x1, y1))
        for i in range(10):
            p = P(f"WINUM{i}")
            self.obj(f"NUM_{i}", p)

        # options sliders
        l, m, r, o = P("M_THERML"), P("M_THERMM"), P("M_THERMR"), P("M_THERMO")
        track = Pic(l.w + 12 * m.w + r.w, max(l.h, m.h, r.h))
        track.blit(l, 0, 0)
        for i in range(12):
            track.blit(m, l.w + i * m.w, 0)
        track.blit(r, l.w + 12 * m.w, 0)
        self.obj("O_STRACK", track)
        self.obj("O_SLIDER", o)

        # fonts: 26 capitals, 26 small capitals (drawn 4 lower), '.', '!'
        f = self.font()
        caps = {}
        for c in range(65, 91):
            g = f.get(chr(c)) or Pic(10, 15)
            caps[c] = g
            self.obj(f"CHAR_{c:03d}", g)
        for c in range(97, 123):
            g = caps[c - 32]
            self.obj(f"CHAR_{c:03d}", g.scaled(max(1, round(g.w * 0.75)), max(1, round(g.h * 0.75))))
        for ch, code in ((".", 46), ("!", 33)):
            g = f.get(ch) or Pic(4, 15)
            self.obj(f"CHAR_{code:03d}", g.scaled(max(1, round(g.w * 0.75)), max(1, round(g.h * 0.75))))

        # intermission
        self.obj("I_SECRET", P("WISCRT2"))
        self.obj("PERCENT", P("WIPCNT"))
        self.obj("I_LEVEL", self.text("Level"))
        self.obj("I_KILLS", P("WIOSTK"))
        self.obj("I_ITEMS", P("WIOSTI"))
        self.obj("I_FINISH", P("WIF"))
        self.obj("I_FRAGS", P("WIFRGS"))

        # status bar faces: 5 pain levels x (fwd, right, left, turn left,
        # turn right, ouch, evil grin, rampage), god, dead, then 6 gib frames
        n = 0
        for lv in range(5):
            for name in (f"STFST{lv}1", f"STFST{lv}0", f"STFST{lv}2", f"STFTL{lv}0",
                         f"STFTR{lv}0", f"STFOUCH{lv}", f"STFEVL{lv}", f"STFKILL{lv}"):
                self.obj(f"FACE{n:02d}", P(name))
                n += 1
        self.obj("FACE40", P("STFGOD0"))
        self.obj("FACE41", P("STFDEAD0"))
        for i in range(6):
            p = P(f"STFXDTH{i}") or P("STFDEAD0")
            self.obj(f"STSPLAT{i}", p)

        # status bar numbers etc. (consecutive from MINUS, see sbobj_e)
        # ST_Drawer erases old numbers sbobj[0]->height (MINUS) rows tall and
        # DrawJagobj ignores patch offsets: put each glyph in a 14x16 cell
        def cell(p, w=14, h=16):
            c = Pic(w, h)
            c.blit(p, -p.xoff, -p.yoff)
            return c
        self.obj("MINUS", cell(P("STTMINUS")))
        for i in range(10):
            self.obj(f"STNUM{i}", cell(P(f"STTNUM{i}")))
        self.obj("STPRCNT", cell(P("STTPRCNT")))
        for c in ("B", "Y", "R"):
            self.obj(f"CARD_{c}", P(f"CKEY_{c}") or P("STKEYS0"))
        for c in ("B", "Y", "R"):
            self.obj(f"SKUL_{c}", P(f"SKEY_{c}") or P("STKEYS3"))
        for i in range(2, 8):
            self.obj(f"MICRO_{i}", P(f"STYSNUM{i}"))

        # status bar background (read straight into a 320x40 buffer)
        sb = P("STATUS") or P("STBAR")
        if (sb.w, sb.h) != (320, 40):
            sb = sb.scaled(320, 40)
        self.out.add("STBAR", jagobj(sb.map(lambda v: v), clear=0), compress=True)
        net = P("STBARNET")
        if net is None or (net.w, net.h) != (80, 40):
            net = sb.crop(240, 0, 320, 40)
        self.out.add("STBARNET", jagobj(net, clear=0), compress=True)

        # plaques
        self.obj("loading", self.plaque("Loading", half=True))
        self.obj("PAUSED", self.plaque("Paused", half=True))
        self.obj("neterror", self.plaque("Network Error", half=True))
        self.obj("defaults", self.plaque("Defaults Restored"))
        self.obj("connect", self.plaque("Connecting"))

    # ---- sound --------------------------------------------------------------
    def sounds(self):
        n = 0
        stand_in = {"firbal": "firsht"}            # Jaguar-only sounds
        for name in self.t["sfx"]:
            if name in self.t["unused_sfx"]:
                self.out.add(name, sfx_lump(b"\x80" * 64))     # never played; keep a stub
                continue
            d = self.a.get("DS" + name.upper())
            if d is None and name in stand_in:
                d = self.a.get("DS" + stand_in[name].upper())
            if d is None:
                self.warn(f"no sound DS{name.upper()}; using silence")
                s = bytes([128]) * 64
            else:
                s = dmx_to_11k(d)
            self.out.add(name, sfx_lump(s))
            n += 1
        self.log(f"  sounds: {n} ({len(self.t['unused_sfx'])} the engine never plays left as stubs)")

    def music(self):
        import jagmusic
        songs = {}
        src = {"m_intro": "D_INTRO"}
        for name in self.t["music"]:
            pc = src.get(name, "D_" + name[2:].upper())
            d = self.a.get(pc)
            if d is None:
                self.warn(f"no music {pc} for {name}")
            songs[name] = d
        insts, events = jagmusic.convert(songs, self.a.get("GENMIDI"), log=self.log,
                                         loop={n: n != "m_intro" for n in songs})
        self.out.add("INSTSTRT", b"")
        for name, data in insts:
            self.out.add(name, data)
        self.out.add("INSTEND", b"")
        for name, data in events.items():
            self.out.add(name, data)

    # ---- demos --------------------------------------------------------------
    def demos(self):
        U, D, L, R = 0x100000, 0x200000, 0x400000, 0x800000
        A, B, C = 0x20000000, 0x2000000, 0x2000           # speed, attack, use
        PAUSE = 0x10000000

        def demo(skill, mapnum, script):
            cmds = []
            for tics, buttons in script:
                cmds += [buttons] * tics
            cmds.append(PAUSE)
            return struct.pack(f">{2 + len(cmds)}I", skill, mapnum, *cmds)

        walk = [(20, 0), (25, U | A), (6, U | L), (20, U | A), (4, B), (8, R), (6, B),
                (30, U | A), (8, L | U), (25, U | A), (6, B), (10, R | U), (30, U | A | C),
                (8, L), (40, U | A), (6, B | U), (12, R), (40, U | A), (10, L | U), (30, U | A)]
        for n, d in ((1, demo(2, 1, walk)), (2, demo(2, 3, walk[::-1]))):
            path = self.demo_files.get(n)
            if path:                               # recorded by tools/emu/autoplay.py --record
                d = open(path, "rb").read()
            self.out.add(f"DEMO{n}", d, compress=True)

    # ---- everything -----------------------------------------------------------
    def run(self):
        self.log("sprites")
        self.sprites()
        self.out.add("CRYPAL", struct.pack(">256H", *[v & 0xFFFF for v in self.t["vgatojag"]]))
        self.log("graphics")
        self.graphics()
        maps = self.read_maps()
        self.log("textures, flats, maps")
        self.textures_and_flats(maps)
        self.write_maps(maps)
        self.log("sounds and music")
        self.sounds()
        self.music()
        self.demos()
        self.audit()

    def audit(self):
        """Every name the engine looks up must resolve to the lump meant."""
        o = self.out
        if len(o.lumps) > MAXLUMPS:
            sys.exit(f"{len(o.lumps)} lumps; the engine holds {MAXLUMPS}")
        for name in self.t["sfx"]:
            i = o.find(name)
            if i < 0 or len(o.lumps[i][1]) < 28:
                sys.exit(f"sound {name} does not resolve")
        tex1 = o.find("TEXTURE1")
        t = o.lumps[tex1][1]
        for k in range(struct.unpack("<I", t[:4])[0]):
            off = struct.unpack("<I", t[4 + 4 * k:8 + 4 * k])[0]
            name = t[off:off + 8].split(b"\0")[0].decode()
            i = o.find(name)
            if i <= tex1 or o.lumps[i][2] is False:
                sys.exit(f"texture {name} resolves to lump {i} ({o.lumps[i][0] if i >= 0 else '-'})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pwad", required=True, help="PC-format levels/art, e.g. jaguartc.wad")
    ap.add_argument("--iwad", required=True, help="PC IWAD for everything else, e.g. freedoom1.wad")
    ap.add_argument("--sprites", help="take the sprites from this WAD instead, e.g. your DOOM.WAD")
    ap.add_argument("-o", "--output", default="jagdoom.wad")
    ap.add_argument("--check", action="store_true", help="verify every compressed lump decodes")
    ap.add_argument("-j", "--jobs", type=int, default=None)
    ap.add_argument("--demo1", help="attract-mode demo lump (skill, map, tic commands)")
    ap.add_argument("--demo2")
    ap.add_argument("--spectres", action="store_true",
                    help="make every second demon on MAP09 and later a spectre")
    a = ap.parse_args()
    conv = Converter(PCWad(a.pwad), PCWad(a.iwad), sprwad=PCWad(a.sprites) if a.sprites else None)
    conv.demo_files = {1: a.demo1, 2: a.demo2}
    conv.spectres = a.spectres
    conv.run()
    print("compressing")
    wad, packed = conv.out.build(a.jobs, a.check)
    open(a.output, "wb").write(wad)
    print(f"{a.output}: {len(wad)} bytes, {len(conv.out.lumps)} lumps "
          f"({len(packed)} compressed); {ROM_ROOM - len(wad)} bytes left in the cartridge")
    if len(wad) > ROM_ROOM:
        sys.exit("too big for a 4 MB cartridge")
    if conv.notes:
        print(f"{len(conv.notes)} warnings")


if __name__ == "__main__":
    main()
