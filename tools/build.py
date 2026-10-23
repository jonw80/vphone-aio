#!/usr/bin/env python3
"""
build.py - build doom.bin (the 68000 program, loaded at $4000) with free tools.

The original build used Atari's mac/aln and the Jaguar port of GCC 2.6.
This uses rmac for the .gas/.s files, a stock m68k gcc for the C files and
GNU ld to link, in the object order of mydoom.lnk.

The one thing GNU ld cannot do is the GPU/DSP MOVEI relocation: MOVEI
stores its 32-bit immediate low word first (TRM p.51), while ld patches
big-endian.  aln handled this through a flag in rmac's a.out relocations
(bit 0 of r_info).  So every .gas file is also assembled to a.out, its
flagged relocations are collected, and after linking the two halves of each
of those words are swapped in the final image.

Outputs (build/):
  doom.elf  linked program with symbols
  doom.bin  raw image of $4000.._enddata, ready to copy to $4000
  doom.map  linker map
"""
import json
import os
import re
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B = os.path.join(ROOT, "build")
RMAC = os.environ.get("RMAC", os.path.join(ROOT, "tools", "bin", "rmac"))
CROSS = os.environ.get("CROSS", "m68k-linux-gnu-")
LOAD = 0x4000

CFLAGS = [
    "-m68000", "-std=gnu89", "-Os", "-DJAGUAR", "-DMODERN_GCC", "-w",
    "-fleading-underscore",          # asm refers to C symbols as _name
    "-ffreestanding", "-fno-builtin", "-fno-pic", "-fno-pie",
    "-fcommon", "-fno-strict-aliasing", "-fwrapv",
    "-fno-delete-null-pointer-checks", "-fno-tree-loop-distribute-patterns",
    "-fno-stack-protector", "-fno-asynchronous-unwind-tables",
]


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    out = r.stdout + r.stderr
    if r.returncode or re.search(r"\berror\b", out, re.I):
        sys.exit(f"FAILED: {' '.join(cmd)}\n{out}")
    return out


def objects():
    names = []
    for line in open(os.path.join(ROOT, "mydoom.lnk")):
        line = line.strip()
        if line.endswith(".o") and line != "lastobj.o":
            names.append(line[:-2])       # lastobj's end labels come from the linker script
    return names


def source_of(name):
    for ext in (".c", ".gas", ".s"):
        if os.path.exists(os.path.join(ROOT, name + ext)):
            return name + ext
    sys.exit(f"no source for {name}.o")


def risc_relocs(aout):
    """Offsets in .text of relocations rmac flagged as MOVEI (word-swapped)."""
    d = open(aout, "rb").read()
    _m, text, data, _b, _s, _e, trs, _drs = struct.unpack(">8I", d[:32])
    out = []
    for i in range(32 + text + data, 32 + text + data + trs, 8):
        addr, info = struct.unpack(">II", d[i:i + 8])
        if info & 1:
            out.append(addr)
    return out


def swap16(v):
    return ((v & 0xFFFF) << 16) | (v >> 16)


def elf_addends(obj):
    """offset -> addend for the R_68K_32 relocations in an object's .text"""
    out = {}
    text = False
    for line in run([CROSS + "readelf", "-rW", obj]).splitlines():
        if line.startswith("Relocation section"):
            text = "TEXT" in line.upper().split("'")[1]
            continue
        f = line.split()
        if text and len(f) >= 3 and f[2] == "R_68K_32":
            m = re.search(r"([+-])\s*([0-9a-f]+)\s*$", line)
            a = int(m.group(2), 16) if m else 0
            out[int(f[0], 16)] = (-a if m and m.group(1) == "-" else a) & 0xFFFFFFFF
    return out


def main():
    for sub in ("elf", "aout"):
        os.makedirs(os.path.join(B, sub), exist_ok=True)
    names = objects()
    swaps = {}
    for n in names:
        src = source_of(n)
        elf = os.path.join(B, "elf", n + ".o")
        if src.endswith(".c"):
            run([CROSS + "gcc", *CFLAGS, "-c", src, "-o", elf], cwd=ROOT)
        else:
            run([RMAC, "-fe", src, "-o", elf], cwd=ROOT)
            aout = os.path.join(B, "aout", n + ".o")
            run([RMAC, "-fb", src, "-o", aout], cwd=ROOT)
            swaps[n] = risc_relocs(aout)
    # a relocation against the null symbol means the assembler lost the
    # target (rmac's ELF writer drops 68000 .data sections): refuse to link
    for n in names:
        rel = run([CROSS + "readelf", "-rW", os.path.join(B, "elf", n + ".o")])
        bad = [l for l in rel.splitlines() if "R_68K_" in l and len(l.split()) <= 4]
        if bad:
            sys.exit(f"{n}.o: relocation with no target symbol:\n" + "\n".join(bad))
    lib = os.path.join(B, "elf", "lib68k.o")
    run([CROSS + "gcc", "-m68000", "-c", "lib68k.S", "-o", lib], cwd=ROOT)

    script = os.path.join(B, "doom.ld")
    open(script, "w").write(f"""
OUTPUT_FORMAT("elf32-m68k")
SECTIONS
{{
  . = 0x{LOAD:x};
  .text : {{ {' '.join(f'build/elf/{n}.o(.text)' for n in names)} build/elf/lib68k.o(.text) }}
  _endtext = .;
  .data : ALIGN(8) {{ *(.data) *(.rodata*) }}
  .bss  : ALIGN(8) {{ *(.bss) *(COMMON) }}
  . = ALIGN(8);
  _enddata = .;
  /DISCARD/ : {{ *(.comment) *(.note*) *(.eh_frame*) }}
}}
""")
    objs = [f"build/elf/{n}.o" for n in names] + ["build/elf/lib68k.o"]
    # aln accepted the same absolute symbol (e.g. _G_FixedMul at its GPU RAM
    # address) from several overlays; allow exactly that and nothing else
    seen = {}
    for o in objs:
        for line in run([CROSS + "nm", "-g", "--defined-only", o], cwd=ROOT).splitlines():
            parts = line.split()
            if len(parts) == 3:
                seen.setdefault(parts[2], []).append((o, parts[1]))
    for sym, defs in seen.items():
        if len(defs) > 1 and any(t not in "AC" for _, t in defs):
            sys.exit(f"duplicate non-absolute symbol {sym}: {defs}")
    run([CROSS + "ld", "-m", "m68kelf", "-T", script, "-Map", "build/doom.map",
         "-z", "muldefs", "--no-warn-rwx-segments", "-e", "0x4000",
         "-o", "build/doom.elf", *objs], cwd=ROOT)

    # where did each object's .text land?
    placed = {}
    for m in re.finditer(r"^ \.text\s+0x([0-9a-f]+)\s+0x([0-9a-f]+)\s+build/elf/(\w+)\.o",
                         open(os.path.join(B, "doom.map")).read(), re.M):
        placed[m.group(3)] = int(m.group(1), 16)

    run([CROSS + "objcopy", "-O", "binary", "build/doom.elf", "build/doom.bin"], cwd=ROOT)
    img = bytearray(open(os.path.join(B, "doom.bin"), "rb").read())
    count = 0
    for n, offs in swaps.items():
        addends = elf_addends(os.path.join(B, "elf", n + ".o"))
        for off in offs:
            # rmac's ELF output stores a MOVEI relocation's addend already
            # word-swapped, so ld computed S + swap16(A).  Recover S, add the
            # real addend, and store the result low word first.
            a_elf = addends[off]
            p = placed[n] + off - LOAD
            s = (int.from_bytes(img[p:p + 4], "big") - a_elf) & 0xFFFFFFFF
            v = (s + swap16(a_elf)) & 0xFFFFFFFF
            img[p:p + 4] = (v & 0xFFFF).to_bytes(2, "big") + (v >> 16).to_bytes(2, "big")
            count += 1
    open(os.path.join(B, "doom.bin"), "wb").write(img)

    syms = {}
    for line in run([CROSS + "nm", "build/doom.elf"], cwd=ROOT).splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = int(parts[0], 16)
    json.dump({"load": LOAD, "size": len(img), "enddata": syms["_enddata"]},
              open(os.path.join(B, "doom.json"), "w"))

    # mkrom.py carries romstub.S pre-assembled; make sure it still matches
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import mkrom
    st = os.path.join(B, "romstub")
    run([CROSS + "gcc", "-m68000", "-c", "romstub.S", "-o", st + ".o",
         "-Wa,--defsym,IMG_LONGS=0x11223344", "-Wa,--defsym,BSS_LONGS=0x55667788"], cwd=ROOT)
    run([CROSS + "objcopy", "-O", "binary", st + ".o", st + ".bin"])
    if open(st + ".bin", "rb").read() != mkrom.stub(0x11223344, 0x55667788):
        sys.exit("tools/mkrom.py STUB no longer matches romstub.S")
    print(f"doom.bin: {len(img)} bytes at ${LOAD:X}-${LOAD + len(img):X}; "
          f"{count} MOVEI relocations fixed; entry ${LOAD:X}; "
          f"_enddata ${syms.get('_enddata', 0):X}")
    return syms


if __name__ == "__main__":
    main()
