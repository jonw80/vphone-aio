#!/usr/bin/env python3
"""
jrisc.py - a small functional model of the Jaguar GPU/DSP (JRISC) core.

Written from the Jaguar Technical Reference Manual rev 8 (instruction set,
pp.44-58 for the GPU and pp.100-110 for the DSP).  It is a *functional*
model for testing code paths: instructions execute in order with the
one-instruction jump delay slot, and the score-board is assumed to hide
all latencies (which is what it is for).  It does not model timing, bus
contention, interrupts, or the hardware bugs in the manual's bug list.

Memory is a flat 16 MB big-endian byte array.  Local RAM behaves like
any other memory here, except that LOADB/LOADW/STOREB/STOREW to local
RAM are 32-bit transfers, as on the real chip.
"""
import struct

M32 = 0xFFFFFFFF


def s32(v):
    v &= M32
    return v - (1 << 32) if v & 0x80000000 else v


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class Halt(Exception):
    pass


class JRISC:
    def __init__(self, dsp=True, mem=None):
        self.dsp = dsp
        self.mem = mem if mem is not None else bytearray(1 << 24)
        self.bank = [[0] * 32, [0] * 32]
        self.cur = 1                      # dspbase / gpubase run jobs in bank 1
        self.z = self.n = self.c = 0
        self.pc = 0
        self.modmask = 0xFFFFE000         # dspbase sets this
        self.acc = 0
        self.steps = 0
        self.local = (0xF1B000, 0xF1E000) if dsp else (0xF03000, 0xF04000)
        self.watch = None                 # optional callback(addr, size, value) on stores

    # ---- registers -------------------------------------------------------
    @property
    def r(self):
        return self.bank[self.cur]

    @property
    def alt(self):
        return self.bank[self.cur ^ 1]

    # ---- memory ----------------------------------------------------------
    def is_local(self, a):
        return self.local[0] <= a < self.local[1]

    def rd(self, a, size):
        a &= 0xFFFFFF
        if size < 4 and self.is_local(a):
            size, a = 4, a & ~3
        return int.from_bytes(self.mem[a:a + size], "big")

    def wr(self, a, size, v):
        a &= 0xFFFFFF
        if size < 4 and self.is_local(a):
            size, a = 4, a & ~3
        v &= (1 << (8 * size)) - 1
        if self.watch:
            self.watch(a, size, v)
        self.mem[a:a + size] = v.to_bytes(size, "big")

    def rd32(self, a):
        return self.rd(a, 4)

    def wr32(self, a, v):
        self.wr(a, 4, v)

    def load_image(self, addr, data):
        self.mem[addr:addr + len(data)] = data

    # ---- flags -----------------------------------------------------------
    def zn(self, v):
        v &= M32
        self.z = int(v == 0)
        self.n = v >> 31
        return v

    def cond(self, cc):
        if cc & 1 and self.z:
            return False
        if cc & 2 and not self.z:
            return False
        f = self.n if cc & 16 else self.c
        if cc & 4 and f:
            return False
        if cc & 8 and not f:
            return False
        return True

    # ---- execution -------------------------------------------------------
    def fetch(self):
        w = struct.unpack(">H", self.mem[self.pc:self.pc + 2])[0]
        self.pc += 2
        return w

    def run(self, pc, stop, max_steps=50_000_000):
        """Run from pc until execution reaches `stop` (e.g. a return address)."""
        self.pc = pc
        pending = None
        while True:
            if pending is None and self.pc == stop:
                return
            self.steps += 1
            if self.steps > max_steps:
                raise Halt(f"step limit at pc={self.pc:06X}")
            target = self.step(self.pc)
            if pending is not None:          # this was the delay slot
                self.pc, pending = pending, None
            elif target is not None:
                pending = target

    def step(self, ipc):
        w = self.fetch()
        op, r1, r2 = w >> 10, (w >> 5) & 31, w & 31
        R = self.r
        dsp = self.dsp

        if op == 0:      # ADD
            a, b = R[r2], R[r1]
            res = a + b
            self.c = int(res > M32)
            R[r2] = self.zn(res)
        elif op == 1:    # ADDC
            a, b = R[r2], R[r1]
            res = a + b + self.c
            self.c = int(res > M32)
            R[r2] = self.zn(res)
        elif op == 2:    # ADDQ
            n = r1 or 32
            res = R[r2] + n
            self.c = int(res > M32)
            R[r2] = self.zn(res)
        elif op == 3:    # ADDQT
            R[r2] = (R[r2] + (r1 or 32)) & M32
        elif op == 4:    # SUB
            a, b = R[r2], R[r1]
            self.c = int(b > a)
            R[r2] = self.zn(a - b)
        elif op == 5:    # SUBC
            a, b = R[r2], R[r1] + self.c
            self.c = int(b > a)
            R[r2] = self.zn(a - b)
        elif op == 6:    # SUBQ
            n = r1 or 32
            self.c = int(n > R[r2])
            R[r2] = self.zn(R[r2] - n)
        elif op == 7:    # SUBQT
            R[r2] = (R[r2] - (r1 or 32)) & M32
        elif op == 8:    # NEG
            self.c = int(R[r2] != 0)
            R[r2] = self.zn(-R[r2])
        elif op == 9:    # AND
            R[r2] = self.zn(R[r2] & R[r1])
        elif op == 10:   # OR
            R[r2] = self.zn(R[r2] | R[r1])
        elif op == 11:   # XOR
            R[r2] = self.zn(R[r2] ^ R[r1])
        elif op == 12:   # NOT
            R[r2] = self.zn(~R[r2])
        elif op == 13:   # BTST
            self.z = int(not (R[r2] >> r1) & 1)
        elif op == 14:   # BSET
            R[r2] = self.zn(R[r2] | (1 << r1))
        elif op == 15:   # BCLR
            R[r2] = self.zn(R[r2] & ~(1 << r1))
        elif op == 16:   # MULT (unsigned 16x16)
            R[r2] = self.zn((R[r2] & 0xFFFF) * (R[r1] & 0xFFFF))
        elif op == 17:   # IMULT (signed 16x16)
            R[r2] = self.zn(s16(R[r2]) * s16(R[r1]))
        elif op == 18:   # IMULTN
            self.acc = s16(R[r2]) * s16(R[r1])
            self.zn(self.acc)
        elif op == 19:   # RESMAC
            R[r2] = self.acc & M32
        elif op == 20:   # IMACN
            self.acc += s16(R[r2]) * s16(R[r1])
        elif op == 21:   # DIV (unsigned, integer mode)
            d = R[r1]
            R[r2] = (R[r2] // d) & M32 if d else M32
        elif op == 22:   # ABS
            v = s32(R[r2])
            self.c = int(v < 0)
            R[r2] = abs(v) & M32
            self.z = int(R[r2] == 0)
            self.n = 0
        elif op == 23:   # SH  (positive = right)
            n = s32(R[r1])
            v = R[r2]
            if n >= 0:
                self.c = v & 1
                v = v >> n if n < 32 else 0
            else:
                self.c = v >> 31
                v = (v << -n) & M32 if -n < 32 else 0
            R[r2] = self.zn(v)
        elif op == 24:   # SHLQ (encoded 32-n)
            n = 32 - r1
            self.c = R[r2] >> 31
            R[r2] = self.zn((R[r2] << n) & M32 if n < 32 else 0)
        elif op == 25:   # SHRQ
            n = r1 or 32
            self.c = R[r2] & 1
            R[r2] = self.zn(R[r2] >> n if n < 32 else 0)
        elif op == 26:   # SHA
            n = s32(R[r1])
            v = s32(R[r2])
            if n >= 0:
                self.c = v & 1
                v = v >> min(n, 31)
            else:
                self.c = (v >> 31) & 1
                v = v << -n if -n < 32 else 0
            R[r2] = self.zn(v)
        elif op == 27:   # SHARQ
            n = r1 or 32
            self.c = R[r2] & 1
            R[r2] = self.zn(s32(R[r2]) >> min(n, 31))
        elif op == 28:   # ROR
            n = R[r1] & 31
            v = R[r2]
            self.c = v >> 31
            R[r2] = self.zn(((v >> n) | (v << (32 - n))) & M32)
        elif op == 29:   # RORQ
            n = r1 & 31
            v = R[r2]
            self.c = v >> 31
            R[r2] = self.zn(((v >> n) | (v << (32 - n))) & M32)
        elif op == 30:   # CMP
            a, b = R[r2], R[r1]
            self.c = int(b > a)
            self.zn(a - b)
        elif op == 31:   # CMPQ (signed -16..15)
            n = r1 - 32 if r1 & 16 else r1
            a, b = R[r2], n & M32
            self.c = int(b > a)
            self.zn(a - b)
        elif op == 32:
            if dsp:      # SUBQMOD
                v = R[r2]
                res = (v - (r1 or 32)) & M32
                R[r2] = self.zn((v & self.modmask) | (res & ~self.modmask & M32))
            else:        # SAT8
                v = s32(R[r2])
                R[r2] = self.zn(min(max(v, 0), 255))
                self.n = 0
        elif op == 33:
            v = s32(R[r2])
            if dsp:      # SAT16S
                R[r2] = self.zn(min(max(v, -0x8000), 0x7FFF))
            else:        # SAT16
                R[r2] = self.zn(min(max(v, 0), 0xFFFF))
            self.n = 0
        elif op == 34:   # MOVE
            R[r2] = R[r1]
        elif op == 35:   # MOVEQ
            R[r2] = r1
        elif op == 36:   # MOVETA
            self.alt[r2] = R[r1]
        elif op == 37:   # MOVEFA
            R[r2] = self.alt[r1]
        elif op == 38:   # MOVEI (low word first)
            lo = self.fetch()
            hi = self.fetch()
            R[r2] = (hi << 16) | lo
        elif op == 39:   # LOADB
            R[r2] = self.rd(R[r1], 1)
        elif op == 40:   # LOADW
            R[r2] = self.rd(R[r1], 2)
        elif op == 41:   # LOAD
            R[r2] = self.rd32(R[r1])
        elif op == 42:
            if dsp:      # SAT32S (no overflow bits modelled)
                pass
            else:        # LOADP (high half ignored)
                R[r2] = self.rd32(R[r1] + 4)
        elif op in (43, 44):   # LOAD (R14/R15+n)
            base = R[14 if op == 43 else 15]
            R[r2] = self.rd32(base + 4 * (r1 or 32))
        elif op == 45:   # STOREB
            self.wr(R[r1], 1, R[r2])
        elif op == 46:   # STOREW
            self.wr(R[r1], 2, R[r2])
        elif op == 47:   # STORE
            self.wr32(R[r1], R[r2])
        elif op == 48:
            if dsp:      # MIRROR
                R[r2] = self.zn(int(f"{R[r2]:032b}"[::-1], 2))
            else:        # STOREP (low half)
                self.wr32(R[r1] + 4, R[r2])
        elif op in (49, 50):   # STORE (R14/R15+n)
            base = R[14 if op == 49 else 15]
            self.wr32(base + 4 * (r1 or 32), R[r2])
        elif op == 51:   # MOVE PC
            R[r2] = ipc
        elif op == 52:   # JUMP cc,(Rn)
            if self.cond(r2):
                return R[r1]
        elif op == 53:   # JR cc,n
            if self.cond(r2):
                off = r1 - 32 if r1 & 16 else r1
                return (ipc + 2 + 2 * off) & 0xFFFFFF
        elif op == 57:   # NOP
            pass
        elif op in (58, 59):   # LOAD (R14/R15+Rn)
            base = R[14 if op == 58 else 15]
            R[r2] = self.rd32(base + R[r1])
        elif op in (60, 61):   # STORE (R14/R15+Rn)
            base = R[14 if op == 60 else 15]
            self.wr32(base + R[r1], R[r2])      # offset in reg1, data in reg2
        elif op == 63 and dsp:   # ADDQMOD
            v = R[r2]
            res = (v + (r1 or 32)) & M32
            R[r2] = self.zn((v & self.modmask) | (res & ~self.modmask & M32))
        else:
            raise Halt(f"unimplemented opcode {op} at {ipc:06X}")
        return None
