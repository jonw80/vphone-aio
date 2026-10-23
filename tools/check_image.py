#!/usr/bin/env python3
"""
check_image.py - prove the GPU/DSP code inside build/doom.bin is right.

Every GPU and DSP block is re-assembled with its externs set to the
addresses the linker actually gave them, and must match the bytes in
doom.bin exactly.  This catches anything the MOVEI fix-up in build.py (or
the linker) got wrong.
"""
import os, sys, subprocess, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
from asmtest import assemble
nm = {}
for l in subprocess.run(['m68k-linux-gnu-nm', 'build/doom.elf'], capture_output=True, text=True).stdout.splitlines():
    p = l.split()
    if len(p) == 3: nm[p[2]] = int(p[0], 16)
img = open('build/doom.bin', 'rb').read()
entries = {'decomp': '_decomp_start', 'music': '_music_dspcode', 'sfx': '_sfx_start', 'mixmusic': '_mixmusic_start'}
for i in range(1, 10): entries[f'r_phase{i}'] = f'_ref{i}_start'
for n in ('p_base', 'p_move', 'p_shoot', 'p_sight', 'p_slide'): entries[n] = f'_{n}_start'
bad = 0
for name, entry in sorted(entries.items()):
    if entry not in nm: continue
    src = open(name + '.gas', 'rb').read().decode('latin-1')
    if True:
        ext = {x.strip() for l in re.findall(r'^[ \t]*\.extern[ \t]+([^\n;]+)', src, re.M) for x in l.split(',')}
        ext = {x for x in ext if x and not re.search(r'^' + re.escape(x) + r'\s*(::?|=|\.equ)', src, re.M)}
        missing = sorted(x for x in ext if x and x not in nm and not re.search(r'^' + re.escape(x) + r'::?', src, re.M))
        assert not missing, f'unresolved externs {missing}'
        payload, syms, table = assemble(name + '.gas', entry, {k: nm[k] for k in ext if k in nm})
    off = nm[entry] - 0x4000
    ok = img[off:off + len(payload)] == payload
    bad += not ok
    print(('ok  ' if ok else 'DIFF'), name, len(payload))
    if not ok:
        got = img[off:off + len(payload)]
        d = [i for i in range(len(payload)) if got[i] != payload[i]]
        print('   first diffs at', [hex(x) for x in d[:6]], 'count', len(d))
        i = d[0] & ~1
        print('   image', got[i-4:i+8].hex(), ' expected', payload[i-4:i+8].hex())
# the resident loaders have no codeend label: compare their whole text
for name, entry in (('gpubase', '_gpubase'), ('dspbase', '_dspbase')):
    src = open(name + '.gas', 'rb').read().decode('latin-1')
    ext = {x.strip() for l in re.findall(r'^[ \t]*\.extern[ \t]+([^\n;]+)', src, re.M) for x in l.split(',')}
    ext = {x for x in ext if x and not re.search(r'^' + re.escape(x) + r'\s*(::?|=|\.equ)', src, re.M)}
    tmp = '_check_' + name + '.gas'
    open(tmp, 'w', encoding='latin-1').write(src + '\ncodeend:\n')
    try:
        payload, _, _ = assemble(tmp, entry, {k: nm[k] for k in ext if k in nm})
    finally:
        os.remove(tmp)
    off = nm[entry] - 0x4000
    ok = img[off:off + len(payload)] == payload
    bad += not ok
    print(('ok  ' if ok else 'DIFF'), name, len(payload))
print(f"  {'ok' if not bad else 'FAIL'}  {bad} GPU/DSP blocks differ from a fresh assembly at their linked addresses")
sys.exit(1 if bad else 0)
