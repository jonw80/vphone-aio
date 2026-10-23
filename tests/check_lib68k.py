#!/usr/bin/env python3
"""
check_lib68k.py - run lib68k.S's multiply/divide helpers under qemu-m68k and
compare them with a transcription of the C originals in jagonly.c.

Needs m68k-linux-gnu-gcc and qemu-m68k (user mode).
"""
import os, random, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = r"""#include <stdio.h>
#include <stdlib.h>
unsigned t____mulsi3(unsigned,unsigned), t____udivsi3(unsigned,unsigned), t____umodsi3(unsigned,unsigned), t____modsi3(unsigned,unsigned);
int t____divsi3(int,int);
int main(void){ unsigned a,b;
  while (scanf("%x %x",&a,&b)==2)
    printf("%08x %08x %08x %08x %08x\n", t____mulsi3(a,b), t____udivsi3(a,b), (unsigned)t____divsi3((int)a,(int)b), t____umodsi3(a,b), t____modsi3(a,b));
  return 0; }
"""
td = tempfile.mkdtemp()
subprocess.run(["m68k-linux-gnu-gcc", "-m68000", "-c", os.path.join(ROOT, "lib68k.S"), "-o", td + "/lib.o"], check=True)
subprocess.run(["m68k-linux-gnu-objcopy", "--prefix-symbols=t_", td + "/lib.o", td + "/libp.o"], check=True)
open(td + "/h.c", "w").write(HARNESS)
exe = td + "/h"
subprocess.run(["m68k-linux-gnu-gcc", "-O1", "-static", td + "/h.c", td + "/libp.o", "-o", exe], check=True, capture_output=True)
M = 0xFFFFFFFF
def udiv(aa, bb):            # jagonly.c __udivsi3, transcribed
    if (aa >> 30) >= bb: return 0x7fffffff
    bit = 1
    while aa > bb and bb < 0x80000000:
        bb = (bb << 1) & M; bit = (bit << 1) & M
    c = 0
    while True:
        if aa >= bb: aa -= bb; c |= bit
        bb >>= 1; bit >>= 1
        if not (bit and aa): break
    return c
def s(v): return v - (1 << 32) if v & 0x80000000 else v
def divsi(a, b):
    sign = s(a ^ b)
    aa = (-s(a)) & M if s(a) < 0 else a
    bb = (-s(b)) & M if s(b) < 0 else b
    c = udiv(aa, bb)
    return (-c) & M if sign < 0 else c
def umod(a, b): return (a - udiv(a, b) * b) & M
rng = random.Random(7)
edge = [0, 1, 2, 3, 7, 0xff, 0xffff, 0x10000, 0x10001, 0x3fffffff, 0x40000000, 0x7fffffff, 0x80000000, 0x80000001, 0xfffffffe, M]
cases = [(a, b) for a in edge for b in edge]
for _ in range(200000):
    k = rng.random()
    a = rng.getrandbits(32) >> rng.randrange(32)
    b = rng.getrandbits(32) >> rng.randrange(32)
    if k < 0.3: a = (-a) & M
    if k > 0.7: b = (-b) & M
    cases.append((a, b))
inp = "".join(f"{a:x} {b:x}\n" for a, b in cases)
out = subprocess.run(["qemu-m68k", exe], input=inp, capture_output=True, text=True).stdout.split("\n")
out = [l for l in out if l]
assert len(out) == len(cases), f"only {len(out)} of {len(cases)} results"
bad = 0
for (a, b), line in zip(cases, out):
    want = [(a * b) & M, udiv(a, b), divsi(a, b), umod(a, b), umod(a, b)]
    got = [int(x, 16) for x in line.split()]
    if got != want:
        bad += 1
        if bad <= 5: print("MISMATCH", hex(a), hex(b), [hex(x) for x in got], [hex(x) for x in want])
print(f"  {'ok' if not bad else 'FAIL'}  lib68k.S helpers match jagonly.c semantics on {len(cases)} cases under qemu-m68k")
sys.exit(1 if bad else 0)
