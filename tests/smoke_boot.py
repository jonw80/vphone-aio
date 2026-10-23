#!/usr/bin/env python3
"""
smoke_boot.py - boot the cartridge image's 68000 code in a CPU emulator.

Runs build/doom.bin through tools/mkrom.py with a stand-in WAD (just the
lump names the ROM tool checks for), then executes the result on Unicorn's
68000 model from the cartridge entry point.  Nothing but the 68000 is
emulated: TOM/JERRY registers are plain memory, every GPU or DSP job the
68000 starts is marked finished at once, and the vblank tick counter is
advanced periodically.

What it shows: the boot stub copies the program and clears BSS, init.s
runs, the C code runs (vsprintf, WAD parsing, zone allocator, the 68000
math helpers) without a CPU fault, and the run ends where a stand-in WAD
must end -- in I_Error, with the message the game prints.

Usage: tools/build.py && python3 tests/smoke_boot.py
"""
import bisect
import os
import struct
import subprocess
import sys
import tempfile

from unicorn import Uc, UcError, UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, UC_HOOK_BLOCK, UC_HOOK_MEM_WRITE
from unicorn.m68k_const import UC_CPU_M68K_M68000, UC_M68K_REG_PC, UC_M68K_REG_A7, UC_M68K_REG_SR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CROSS = os.environ.get("CROSS", "m68k-linux-gnu-")


def fake_wad():
    names = [b"CRYPAL", b"M_TITLE", b"INSTSTRT", b"INSTEND"]
    data = b"\0" * 512                                   # one lump body, shared
    ofs = 12 + len(data)
    hdr = b"IWAD" + struct.pack(">II", len(names), ofs)
    dirent = b"".join(struct.pack(">II", 12, len(data)) + n.ljust(8, b"\0") for n in names)
    return hdr + data + dirent


def symbols():
    out = subprocess.run([CROSS + "nm", "-n", os.path.join(ROOT, "build", "doom.elf")],
                         capture_output=True, text=True).stdout
    syms = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 3:
            syms[p[2]] = int(p[0], 16)
    funcs = sorted((v, k) for k, v in syms.items() if 0x4000 <= v < syms["_endtext"])
    return syms, funcs


def main():
    syms, funcs = symbols()
    addrs = [a for a, _ in funcs]
    with tempfile.TemporaryDirectory() as td:
        wad, rom = os.path.join(td, "fake.wad"), os.path.join(td, "t.j64")
        open(wad, "wb").write(fake_wad())
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "mkrom.py"), "--wad", wad, "-o", rom],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        image = open(rom, "rb").read()

    mu = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, UC_CPU_M68K_M68000)
    mu.mem_map(0x000000, 0x200000)                       # 2 MB DRAM
    mu.mem_map(0x800000, 0x400000)                       # cartridge
    mu.mem_map(0xF00000, 0x20000)                        # TOM + JERRY, incl. GPU/DSP RAM
    mu.mem_write(0x800000, image[:0x400000])
    mu.mem_write(0x4000, b"\xde\xad" * 0x2000)           # junk, so BSS clearing is visible

    seen, order = set(), []

    def on_block(uc, addr, size, user):
        i = bisect.bisect_right(addrs, addr) - 1
        if i >= 0 and addr < syms["_endtext"]:
            name = funcs[i][1]
            if name not in seen:
                seen.add(name)
                order.append(name)

    def rd32(a):
        return struct.unpack(">I", mu.mem_read(a, 4))[0]

    def on_write(uc, access, addr, size, value, user):
        # the GPU and DSP are not emulated: a started job is finished at once
        if addr == syms["_dspcodestart"] and value:
            uc.mem_write(syms["_dspfinished"], struct.pack(">I", 0xDEF6))
        if addr == syms["_gpucodestart"] and value:
            uc.mem_write(syms["_gpufinished"], struct.pack(">I", 1))

    mu.hook_add(UC_HOOK_BLOCK, on_block)
    mu.hook_add(UC_HOOK_MEM_WRITE, on_write)
    entry = rd32(0x800404)
    mu.reg_write(UC_M68K_REG_SR, 0x2700)
    mu.reg_write(UC_M68K_REG_A7, 0x4000)
    fault = None
    dis = subprocess.run([CROSS + "objdump", "-d", "-m", "m68k:68000", "--start-address", hex(syms["Frame"]),
                          "--stop-address", hex(syms["Frame"] + 0x200), os.path.join(ROOT, "build", "doom.elf")],
                         capture_output=True, text=True).stdout
    frame_rte = int(next(l for l in dis.splitlines() if l.rstrip().endswith("rte")).split(":")[0], 16)
    interrupts = [0]
    # run the boot stub alone, and check what it left before the game starts
    mu.emu_start(entry, 0x4000, count=5_000_000)
    copied = bytes(mu.mem_read(0x4000, 0x400)) == open(os.path.join(ROOT, "build", "doom.bin"), "rb").read(0x400)
    img_end = 0x4000 + os.path.getsize(os.path.join(ROOT, "build", "doom.bin"))
    bss_clear = not any(mu.mem_read(img_end, syms["_enddata"] - img_end))
    pc = 0x4000
    for slice_ in range(2000):                            # 400 slices of 50k instructions
        try:
            mu.emu_start(pc, frame_rte, count=50_000)
        except UcError as e:
            fault = f"{e} at ${mu.reg_read(UC_M68K_REG_PC):X}"
            break
        pc = mu.reg_read(UC_M68K_REG_PC)
        if pc == frame_rte:
            # Unicorn's 68000 model mishandles RTE; do it here (SR, then PC)
            sp = mu.reg_read(UC_M68K_REG_A7)
            sr, pc = struct.unpack(">HI", mu.mem_read(sp, 6))
            mu.reg_write(UC_M68K_REG_A7, sp + 6)
            mu.reg_write(UC_M68K_REG_SR, sr)
            continue
        sr = mu.reg_read(UC_M68K_REG_SR)
        vector = rd32(0x100)
        if (sr & 0x0700) == 0 and vector:
            # vblank: take the interrupt as a 68000 does (TOM bug #7: always
            # vector $40) so init.s's own Frame handler runs and RTEs back
            sp = mu.reg_read(UC_M68K_REG_A7) - 6
            mu.mem_write(sp, struct.pack(">HI", sr, pc))
            mu.reg_write(UC_M68K_REG_A7, sp)
            mu.reg_write(UC_M68K_REG_SR, (sr & ~0x0700) | 0x2100)
            pc = vector
            interrupts[0] += 1
        if "_I_Error" in seen and slice_ > 2:
            break

    msg = bytes(mu.mem_read(syms["_errormessage"], 80)).split(b"\0")[0].decode("latin-1")
    print(f"  entry ${entry:X}; program copied to $4000: {copied}; BSS cleared: {bss_clear}; "
          f"vblank interrupts taken: {interrupts[0]}, ticcount {rd32(syms['_ticcount'])}")
    print(f"  {len(seen)} functions reached, in order: {' '.join(order[:40])}{' ...' if len(order) > 40 else ''}")
    print(f"  I_Error message: {msg!r}" if msg else "  no I_Error")
    if fault:
        print(f"  CPU FAULT: {fault}")
    ok = copied and bss_clear and not fault and "_Jag68k_main" in seen and "_I_Error" in seen and msg
    print("  ok  boot smoke test" if ok else "  FAIL boot smoke test")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
