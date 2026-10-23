#!/usr/bin/env python3
"""
mkrom.py - make a Jaguar Doom Ultimate cartridge image (.j64).

    tools/mkrom.py --dump  jagdoom.j64   [-o doom-ultimate.j64]
    tools/mkrom.py --wad   jagdoom.wad   [-o doom-ultimate.j64]

The game data is id Software's and is not part of this repository: bring a
dump of your own Jaguar Doom cartridge (4 MB, $800000-$BFFFFF) or the WAD
dumped from it ($840000 to the end, per the source release's readme).

Cartridge layout ($800000 = offset 0):
  $800000-$801FFF  boot header (taken from --dump, or from --header)
  $802000-         romstub.S, then build/doom.bin (copied to $4000 at boot)
  $840000-         jagdoom.wad, unchanged

Run tools/build.py first (or set DOOM_BUILD to a folder holding a\nprebuilt doom.bin and doom.json).

About booting on a real console: the Jaguar boot ROM only starts a
cartridge whose header carries an Atari-encrypted boot block, and a
commercial header may verify the contents it was made for.  This image runs
in emulators and on flash carts that boot .j64 files themselves.  For a
plain console use --header with a header that does not check the contents.
"""
import argparse
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROM_SIZE = 0x400000
HEADER = 0x2000
WAD_AT = 0x40000                    # $840000
ENTRY = 0x802000


# romstub.S assembled, with IMG_LONGS at offset 14 and BSS_LONGS at offset 26
# (tools/build.py checks on every build that this still matches the source).
# Carrying the bytes means this tool needs nothing but Python.
STUB = bytes.fromhex("46fc270041fa002643f84000203c0000000022d8538066fa"
                     "203c0000000067064299538066fa4ef840000000")
STUB_IMG, STUB_BSS = 14, 26


def stub(img_longs, bss_longs):
    s = bytearray(STUB)
    s[STUB_IMG:STUB_IMG + 4] = img_longs.to_bytes(4, "big")
    s[STUB_BSS:STUB_BSS + 4] = bss_longs.to_bytes(4, "big")
    return bytes(s)


def check_wad(wad):
    """The Jaguar WAD is big-endian: id, numlumps, infotableofs."""
    ident, num, ofs = wad[:4], *struct.unpack(">II", wad[4:12])
    if ident not in (b"IWAD", b"PWAD"):
        sys.exit(f"no IWAD/PWAD header at the WAD start (found {ident!r}); "
                 "is the dump byte-swapped or from another game?")
    if not (0 < num < 5000) or ofs + 16 * num > len(wad):
        sys.exit(f"WAD directory looks wrong: {num} lumps at offset {ofs:#x}")
    names = set()
    for i in range(num):
        e = wad[ofs + 16 * i: ofs + 16 * i + 16]
        names.add(bytes(c & 0x7F for c in e[8:16]).rstrip(b"\0").decode("latin-1"))
    for need in ("CRYPAL", "M_TITLE", "INSTSTRT", "INSTEND"):
        if need not in {n.upper() for n in names}:
            sys.exit(f"WAD has no {need} lump: not a Jaguar Doom WAD?")
    return num


def unswap(data):
    """Accept dumps stored with 16-bit words byte-swapped."""
    if data[WAD_AT:WAD_AT + 4] in (b"WIDA", b"WPDA"):
        b = bytearray(data)
        b[0::2], b[1::2] = data[1::2], data[0::2]
        return bytes(b)
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dump", help="full 4 MB dump of your Jaguar Doom cartridge")
    src.add_argument("--wad", help="jagdoom.wad dumped from your cartridge ($840000-end)")
    ap.add_argument("--header", help="8 KB boot header to use instead of the dump's")
    ap.add_argument("-o", "--output", default="doom-ultimate.j64")
    a = ap.parse_args()

    if a.dump:
        dump = unswap(open(a.dump, "rb").read())
        if len(dump) < WAD_AT + 12:
            sys.exit("dump is too small to be a Jaguar Doom cartridge")
        header, wad = dump[:HEADER], dump[WAD_AT:]
    else:
        wad = open(a.wad, "rb").read()
        if wad[:4] in (b"WIDA", b"WPDA"):
            b = bytearray(wad)
            b[0::2], b[1::2] = wad[1::2], wad[0::2]
            wad = bytes(b)
        # emulators that skip the boot ROM read the entry point at $800404
        header = bytearray(HEADER)
        header[0x400:0x404] = (0x04040404).to_bytes(4, "big")   # 32-bit ROM, as in Atari's headers
    if a.header:
        header = open(a.header, "rb").read()
        if len(header) != HEADER:
            sys.exit("--header must be exactly 8192 bytes")
    header = bytearray(header)
    header[0x404:0x408] = ENTRY.to_bytes(4, "big")
    lumps = check_wad(wad)

    build = os.environ.get("DOOM_BUILD", os.path.join(ROOT, "build"))
    img = open(os.path.join(build, "doom.bin"), "rb").read()
    info = json.load(open(os.path.join(build, "doom.json")))
    assert info["load"] == 0x4000 and info["size"] == len(img), "doom.bin and doom.json disagree"
    img_longs = (len(img) + 3) // 4
    bss_longs = max(0, (info["enddata"] - (0x4000 + 4 * img_longs) + 3) // 4)
    code = stub(img_longs, bss_longs) + img
    code += bytes(-len(code) % 4)
    room = WAD_AT - HEADER
    if len(code) > room:
        sys.exit(f"program is {len(code) - room} bytes too big for $802000-$83FFFF")

    rom = bytearray(b"\xff" * max(ROM_SIZE, WAD_AT + len(wad)))
    rom[:HEADER] = header
    rom[HEADER:HEADER + len(code)] = code
    rom[WAD_AT:WAD_AT + len(wad)] = wad
    open(a.output, "wb").write(rom)
    print(f"{a.output}: {len(rom) // 1024} KB; program {len(img)} bytes "
          f"({room - len(code)} bytes free below $840000); WAD {lumps} lumps")


if __name__ == "__main__":
    main()
